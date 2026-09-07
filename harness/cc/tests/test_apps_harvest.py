"""The apps-builder harvest: what a turn's output becomes.

SPECS/2026-09-06-apps-builder-seat.md §E. The harvest is the half of a turn
that runs after the container is gone, and its branch ORDER is the contract —
a halted turn must not deploy, a clean turn must end the bar's wait, and a turn
whose output carries the seat's own API key must not enter history, must not
reach the preview root, and must not be left in `/work` for the next turn to
read back in (Gable #2286 BLOCK, Claudette #2288/#2293).

REAL GIT, NOT MOCKS — the same reasoning as test_build_harvest.py. Every repo
here is a real repo in tmp_path with real commits, because the properties under
test are git's: what `git status --porcelain` reports, what `git clean` leaves
behind, whether a file entered history. A mocked git would agree with whatever
this file believed on the day it was written. Only podman is absent, and the
container is the one thing these tests do not need.

rsync is the ONE thing that may be missing on the box running this suite
(Debian trixie does not ship it by default; harness/keyboard/10-appsbuilding.sh
installs it). So the exclusion contract is asserted TWICE: once against the
exact argv, which always runs, and once against real rsync behaviour, which
skips when rsync is not installed. The argv test is the one that can never be
skipped, and it is the one that would catch a dropped `--exclude`.
"""

from __future__ import annotations

import base64
import binascii
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

CC_DIR = Path(__file__).resolve().parent.parent
HARVEST_PY = CC_DIR / "apps" / "apps_harvest.py"

# A key shaped like the real thing: long enough to scan for, and with no
# accidental substring of ordinary source in it.
FAKE_KEY = "sk-ant-api03-Zq7fN2vX8pLm4TcR9wYbHkJs6DgA1eUiO0nMxPvQr5W"


@pytest.fixture(scope="session")
def ah():
    """apps_harvest imported as a module (it is also a CLI, so `main` guards
    the script path and importing it runs nothing)."""
    spec = importlib.util.spec_from_file_location("apps_harvest", HARVEST_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["apps_harvest"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def turn(tmp_path, ah):
    """A provisioned turn: an initialised app repo with one commit, a result
    directory, a preview root, a quarantine root, and the seat's env file."""
    repo = tmp_path / "srv-apps" / "abc234567xyz"
    ah.ensure_repo(repo)
    (repo / "index.html").write_text("<h1>v1</h1>\n", encoding="utf-8")
    ah.commit_all(repo, "turn 1")

    result_dir = tmp_path / "srv-apps-turns" / "12" / "3"
    result_dir.mkdir(parents=True)
    preview = tmp_path / "srv-apps-www" / "abc234567xyz" / "preview"
    preview.mkdir(parents=True)
    quarantine = tmp_path / "srv-apps-quarantine" / "abc234567xyz" / "3"
    key_file = tmp_path / "env"
    key_file.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    return {
        "repo": repo, "result_dir": result_dir, "preview": preview,
        "quarantine": quarantine, "key_file": key_file, "tmp": tmp_path,
    }


def do_harvest(ah, turn, exit_code=0, **kw):
    """Every call the wrapper makes, with the wrapper's own defaults."""
    kwargs = dict(
        session=12, app_id="abc234567xyz", started_at="2026-09-06T10:00:00+00:00",
        model="claude-opus-5", quarantine_dir=turn["quarantine"],
        spool_stdout=str(turn["result_dir"] / "stdout.log"),
        spool_stderr=str(turn["result_dir"] / "stderr.log"),
    )
    kwargs.update(kw)
    return ah.harvest(turn["repo"], exit_code, turn["result_dir"],
                      turn["key_file"], turn["preview"], 3, **kwargs)


def head_sha(ah, repo):
    return ah.git(repo, "rev-parse", "HEAD").stdout.strip()


def log_subjects(ah, repo):
    return ah.git(repo, "log", "--pretty=format:%s").stdout.splitlines()


# ─────────────────────────────────────────────────────── the repo, at first ──

def test_ensure_repo_is_idempotent_and_sets_the_seat_identity(ah, tmp_path):
    repo = tmp_path / "fresh"
    assert ah.ensure_repo(repo) is True
    assert (repo / ".git").is_dir()
    assert ah.ensure_repo(repo) is False        # second turn: already there
    cfg = ah.git(repo, "config", "user.name").stdout.strip()
    assert cfg == "apps-builder"
    assert ah.git(repo, "config", "user.email").stdout.strip() \
        == "apps-builder@disjorn.local"


# ─────────────────────────────────────────────────── branch 1: halted turns ──

def test_halted_turn_commits_the_work_and_leaves_the_preview_untouched(ah, turn):
    """A half-built app must never replace a preview that worked (Claudette
    #2284) — and the work is still committed, so the NEXT turn can see it."""
    (turn["preview"] / "index.html").write_text("<h1>the good one</h1>\n",
                                                encoding="utf-8")
    (turn["repo"] / "index.html").write_text("<h1>half done</h1>\n",
                                             encoding="utf-8")
    (turn["repo"] / "app.js").write_text("// half done\n", encoding="utf-8")

    result = do_harvest(ah, turn, exit_code=1)

    assert result["halted"] == "error"
    assert result["no_changes"] is False
    assert result["commit"] == head_sha(ah, turn["repo"])
    assert log_subjects(ah, turn["repo"])[0] == "turn 3 (halted)"
    assert sorted(result["files"]) == ["app.js", "index.html"]
    assert ah.is_clean(turn["repo"])
    # The preview root is byte-for-byte what it was.
    assert (turn["preview"] / "index.html").read_text() == "<h1>the good one</h1>\n"
    assert not (turn["preview"] / "app.js").exists()


@pytest.mark.parametrize("exit_code, expected", [
    (1, "error"), (2, "error"), (127, "error"),
    (124, "timeout"), (137, "timeout"), (143, "timeout"),
])
def test_halt_reason_from_the_exit_code(ah, turn, exit_code, expected):
    (turn["repo"] / "app.js").write_text("// x\n", encoding="utf-8")
    assert do_harvest(ah, turn, exit_code=exit_code)["halted"] == expected


def test_timed_out_flag_wins_over_a_plain_exit_code(ah, turn):
    """run-apps.sh knows the unit's clock fired even when podman still managed
    to report a tidy status; the flag is that knowledge."""
    (turn["repo"] / "app.js").write_text("// x\n", encoding="utf-8")
    assert do_harvest(ah, turn, exit_code=1, timed_out=True)["halted"] == "timeout"


def test_halted_turn_that_changed_nothing_commits_nothing(ah, turn):
    """`git commit` exits 1 on an empty commit. That is a normal outcome here —
    a turn that died before writing anything — not a harvest failure."""
    before = head_sha(ah, turn["repo"])
    result = do_harvest(ah, turn, exit_code=1)
    assert result["commit"] is None
    assert result["files"] == []
    assert result["halted"] == "error"
    assert head_sha(ah, turn["repo"]) == before


# ─────────────────────────────────────────────────── branch 2: no changes ────

def test_clean_exit_with_an_empty_diff_commits_and_copies_nothing(ah, turn):
    """The runner answered, refused, or decided nothing needed changing. That
    must END the bar's wait rather than leave the user watching a clock (§A),
    and it must not fabricate a commit."""
    before = head_sha(ah, turn["repo"])
    result = do_harvest(ah, turn, exit_code=0)

    assert result["no_changes"] is True
    assert result["halted"] is None
    assert result["commit"] is None
    assert result["files"] == []
    assert result["quarantine"] is None
    assert head_sha(ah, turn["repo"]) == before
    assert list(turn["preview"].iterdir()) == []


def test_a_turn_that_only_wrote_an_ignored_file_is_no_changes(ah, turn):
    """`no_changes` is asked WITHOUT ignored files on purpose: a turn that
    wrote only what its own .gitignore hides has changed nothing this house
    will ever publish. (The secret SCAN uses the wider set — see below.)"""
    (turn["repo"] / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    ah.commit_all(turn["repo"], "turn 1b")
    (turn["repo"] / "scratch").mkdir()
    (turn["repo"] / "scratch" / "notes.txt").write_text("mm\n", encoding="utf-8")
    assert do_harvest(ah, turn, exit_code=0)["no_changes"] is True


def test_an_ignored_only_turn_carrying_the_key_is_still_quarantined(ah, turn):
    """Claudette, slice (i) review: an ignored-only turn came back clean, so it
    was reported no_changes and never scanned, and the payload sat in /work
    for the next turn's "read /work first". The scan set is wider than the
    cleanliness test, and runs even when git says nothing changed."""
    (turn["repo"] / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    ah.commit_all(turn["repo"], "turn 1b")
    (turn["repo"] / "scratch").mkdir()
    (turn["repo"] / "scratch" / "k.txt").write_text(FAKE_KEY + "\n", encoding="utf-8")
    result = do_harvest(ah, turn, exit_code=0)
    assert result["halted"] == "secret"
    assert result["no_changes"] is False
    assert (turn["quarantine"] / "scratch" / "k.txt").exists()
    assert not (turn["repo"] / "scratch").exists()


def test_the_spools_are_redacted_and_usage_is_lifted_into_the_result(ah, turn):
    """Claudette, slice (i) review: the runner's raw stream is exactly where an
    auth failure prints a key, and the spools are 0600 seat-only while the
    broker runs as plink — so the key is redacted in place and usage is
    parsed HERE into result.json; nothing else ever needs to open a spool."""
    import base64 as _b64
    b64 = _b64.b64encode(FAKE_KEY.encode()).decode()
    result_line = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 3, "duration_ms": 4321, "total_cost_usd": 0.42,
        "usage": {"input_tokens": 10, "output_tokens": 20,
                  "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40},
        "result": "done",
    })
    (turn["result_dir"] / "stdout.log").write_text(
        '{"type":"system"}\n' + result_line + "\n", encoding="utf-8")
    (turn["result_dir"] / "stderr.log").write_text(
        f"auth failed for key {FAKE_KEY} (b64 {b64})\n", encoding="utf-8")
    (turn["repo"] / "index.html").write_text("<h1>v2</h1>\n", encoding="utf-8")

    result = do_harvest(ah, turn, exit_code=0)

    assert result["halted"] is None and result["commit"]
    assert result["spool_redacted"] is True
    err = (turn["result_dir"] / "stderr.log").read_text()
    assert FAKE_KEY not in err and b64 not in err
    assert "[REDACTED:raw]" in err and "[REDACTED:base64]" in err
    assert result["usage"] == {
        "input_tokens": 10, "output_tokens": 20,
        "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40,
        "total_cost_usd": 0.42, "num_turns": 3, "duration_ms": 4321,
        "is_error": False,
    }


def test_usage_is_none_when_the_spool_has_no_result_line(ah, turn):
    (turn["result_dir"] / "stdout.log").write_text("garbage\n{not json\n", encoding="utf-8")
    result = do_harvest(ah, turn, exit_code=1)
    assert result["usage"] is None
    assert result["spool_redacted"] is False


# ─────────────────────────────────────────────── branch 3a: the secret scan ──

def test_secret_patterns_are_raw_base64_and_lowercase_hex(ah):
    labels = dict((label, needle) for label, needle in ah.secret_patterns(FAKE_KEY))
    assert set(labels) == {"raw", "base64", "hex"}
    assert labels["raw"] == FAKE_KEY.encode()
    assert labels["base64"] == base64.b64encode(FAKE_KEY.encode()).rstrip(b"=")
    assert labels["hex"] == binascii.hexlify(FAKE_KEY.encode())
    assert labels["hex"] == labels["hex"].lower()


def test_a_short_or_absent_key_yields_no_patterns(ah):
    """Scanning for a 3-byte needle would quarantine honest files. Refusing to
    scan is loud (stderr) but it is not a false positive."""
    assert ah.secret_patterns(None) == []
    assert ah.secret_patterns("") == []
    assert ah.secret_patterns("sk-abc") == []


def test_read_key_takes_everything_after_the_first_equals(ah, tmp_path):
    """podman's env-file semantics, matched exactly: no quote stripping, no
    trimming, last assignment wins."""
    f = tmp_path / "env"
    f.write_text("OTHER=x\nANTHROPIC_API_KEY=a=b=c\nANTHROPIC_API_KEY=final\n",
                 encoding="utf-8")
    assert ah.read_key(f) == "final"
    f.write_text('ANTHROPIC_API_KEY="quoted"\n', encoding="utf-8")
    assert ah.read_key(f) == '"quoted"'


@pytest.mark.parametrize("encoding", ["raw", "base64", "hex"])
def test_a_secret_in_the_output_quarantines_and_halts(ah, turn, encoding):
    """The BLOCK, in all three encodings. No commit — the value must not enter
    history; no copy; the dirty files leave /work entirely; the tree comes back
    to HEAD; halted = "secret"."""
    payload = {
        "raw": FAKE_KEY,
        "base64": base64.b64encode(FAKE_KEY.encode()).decode(),
        "hex": binascii.hexlify(FAKE_KEY.encode()).decode(),
    }[encoding]
    before = head_sha(ah, turn["repo"])
    (turn["repo"] / "index.html").write_text("<h1>v2</h1>\n", encoding="utf-8")
    (turn["repo"] / "sneaky.js").write_text(
        f"const k = '{payload}';\nexport default k;\n", encoding="utf-8")
    (turn["repo"] / "vendor").mkdir()
    (turn["repo"] / "vendor" / "chart.js").write_text("// honest\n",
                                                      encoding="utf-8")

    result = do_harvest(ah, turn, exit_code=0)

    assert result["halted"] == "secret"
    assert result["commit"] is None
    assert result["files"] == []
    assert result["no_changes"] is False
    assert result["quarantine"] == str(turn["quarantine"])
    # nothing entered history
    assert head_sha(ah, turn["repo"]) == before
    assert log_subjects(ah, turn["repo"]) == ["turn 1"]
    # the quarantine has the files, relative paths preserved
    assert (turn["quarantine"] / "sneaky.js").read_text().count(payload) == 1
    assert (turn["quarantine"] / "vendor" / "chart.js").exists()
    assert turn["quarantine"].stat().st_mode & 0o777 == 0o700
    # ... and /work is back to HEAD, with nothing poisoned left for the next
    # turn to read in (the brief tells it to read /work first — #2293)
    assert ah.is_clean(turn["repo"])
    assert not (turn["repo"] / "sneaky.js").exists()
    assert not (turn["repo"] / "vendor").exists()
    assert (turn["repo"] / "index.html").read_text() == "<h1>v1</h1>\n"
    # nothing reached the preview
    assert list(turn["preview"].iterdir()) == []


def test_a_halted_turn_is_scanned_before_its_halted_commit(ah, turn):
    """Keyboard fold, 2026-09-06 (hand A's own observation): a timed-out turn
    is the one most likely to be mid-way through writing something it should
    not, and "commit as halted" would put the value beyond recall. The scan
    runs on any dirty tree, whatever the exit code; a hit wins over the halt
    reason, quarantines, and commits nothing."""
    before = head_sha(ah, turn["repo"])
    (turn["repo"] / "half.js").write_text(
        f"// partial\nconst k = '{FAKE_KEY}';\n", encoding="utf-8")

    result = do_harvest(ah, turn, exit_code=124)   # the timeout code

    assert result["halted"] == "secret"          # not "timeout"
    assert result["commit"] is None
    assert head_sha(ah, turn["repo"]) == before
    assert log_subjects(ah, turn["repo"]) == ["turn 1"]
    assert (turn["quarantine"] / "half.js").exists()
    assert ah.is_clean(turn["repo"])
    assert list(turn["preview"].iterdir()) == []


def test_a_secret_in_a_modification_to_a_tracked_file_is_caught(ah, turn):
    """The diff half of the scan, not the untracked half."""
    (turn["repo"] / "index.html").write_text(
        f"<script>const k='{FAKE_KEY}'</script>\n", encoding="utf-8")
    result = do_harvest(ah, turn, exit_code=0)
    assert result["halted"] == "secret"
    assert (turn["quarantine"] / "index.html").exists()
    # the working copy is restored from HEAD, not deleted
    assert (turn["repo"] / "index.html").read_text() == "<h1>v1</h1>\n"


def test_a_secret_hidden_by_a_gitignore_is_still_caught(ah, turn):
    """The hardening this module documents: `git status --porcelain` does not
    list ignored files, so a turn could write `.gitignore` and then a key-
    bearing file that rsync would happily copy into the preview. The scan's
    dirty set includes ignored paths for exactly this."""
    (turn["repo"] / ".gitignore").write_text("secrets/\n", encoding="utf-8")
    (turn["repo"] / "secrets").mkdir()
    (turn["repo"] / "secrets" / "k.txt").write_text(FAKE_KEY, encoding="utf-8")
    (turn["repo"] / "index.html").write_text("<h1>v2</h1>\n", encoding="utf-8")

    result = do_harvest(ah, turn, exit_code=0)

    assert result["halted"] == "secret"
    assert (turn["quarantine"] / "secrets" / "k.txt").exists()
    assert not (turn["repo"] / "secrets").exists()
    assert list(turn["preview"].iterdir()) == []


def test_a_secret_in_a_binary_file_is_caught(ah, turn):
    """The scan reads bytes, never decoded text: a diff of a binary blob is not
    UTF-8 and a lenient decode would mangle exactly the bytes we look for."""
    (turn["repo"] / "sprite.bin").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\xff" + FAKE_KEY.encode() + b"\x00\xfe")
    assert do_harvest(ah, turn, exit_code=0)["halted"] == "secret"


def test_an_honest_turn_is_not_flagged(ah, turn, tmp_path):
    """The scan must not fire on ordinary source. `sk-ant` appearing as prose
    in a comment is not the key."""
    (turn["repo"] / "app.js").write_text(
        "// never hardcode an sk-ant-... key here\nconst x = 1;\n",
        encoding="utf-8")
    result = do_harvest(ah, turn, exit_code=0, rsync_bin=_fake_rsync(tmp_path))
    assert result["halted"] is None
    assert result["commit"] is not None


def test_no_key_file_means_no_scan_and_a_normal_publish(ah, turn, tmp_path):
    """A missing drop file is a provisioning fault, not a reason to halt every
    turn. It warns and publishes — and the drift block in
    10-appsbuilding.sh is where the absence gets noticed."""
    (turn["repo"] / "app.js").write_text("const x = 1;\n", encoding="utf-8")
    turn["key_file"] = tmp_path / "no-such-env"
    result = do_harvest(ah, turn, exit_code=0, rsync_bin=_fake_rsync(tmp_path))
    assert result["halted"] is None
    assert result["commit"] is not None


# ────────────────────────────────────────────────── branch 3b: the normal turn ──

def _fake_rsync(tmp_path) -> str:
    """An rsync that records its argv and does nothing else. Used where the
    test is about the harvest's decisions rather than about rsync."""
    path = tmp_path / "fake-rsync"
    log = tmp_path / "rsync-argv"
    path.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{log}"\nexit 0\n', encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_normal_turn_commits_and_copies(ah, turn, tmp_path):
    rsync = _fake_rsync(tmp_path)
    (turn["repo"] / "index.html").write_text("<h1>v2</h1>\n", encoding="utf-8")
    (turn["repo"] / "app.js").write_text("const x = 1;\n", encoding="utf-8")

    result = do_harvest(ah, turn, exit_code=0, rsync_bin=rsync)

    assert result["halted"] is None
    assert result["no_changes"] is False
    assert result["quarantine"] is None
    assert result["commit"] == head_sha(ah, turn["repo"])
    assert log_subjects(ah, turn["repo"])[0] == "turn 3"
    assert sorted(result["files"]) == ["app.js", "index.html"]
    assert ah.is_clean(turn["repo"])


def test_the_rsync_argv_is_the_exclusion_contract(ah, turn):
    """`--exclude .git` unanchored (history, and any nested .git a vendored
    copy dragged in) and `--exclude /.*` anchored to the transfer root (`.env`
    is the file a model writes a key into by habit — Gable #2286). Asserted
    against the argv so a dropped flag cannot pass on a box without rsync."""
    argv = ah.preview_argv(turn["repo"], turn["preview"], rsync_bin="rsync")
    assert argv == ["rsync", "-a", "--delete",
                    "--exclude", ".git", "--exclude", "/.*",
                    f"{turn['repo']}/", f"{turn['preview']}/"]


@pytest.mark.skipif(shutil.which("rsync") is None,
                    reason="rsync is not installed on this box "
                           "(harness/keyboard/10-appsbuilding.sh installs it)")
def test_real_rsync_excludes_git_and_root_dotfiles(ah, turn):
    """The same contract, executed. Skipped where rsync is missing; the argv
    test above is the one that always runs."""
    repo = turn["repo"]
    (repo / "index.html").write_text("<h1>v2</h1>\n", encoding="utf-8")
    (repo / ".env").write_text("ANTHROPIC_API_KEY=whatever\n", encoding="utf-8")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (repo / "assets").mkdir()
    (repo / "assets" / ".keep").write_text("", encoding="utf-8")
    (repo / "assets" / "logo.svg").write_text("<svg/>", encoding="utf-8")
    (repo / "vendor").mkdir()
    (repo / "vendor" / ".git").mkdir()
    (repo / "vendor" / ".git" / "config").write_text("x", encoding="utf-8")
    (repo / "vendor" / "chart.js").write_text("// chart\n", encoding="utf-8")
    (turn["preview"] / "stale.html").write_text("old", encoding="utf-8")

    result = do_harvest(ah, turn, exit_code=0)
    assert result["halted"] is None

    preview = turn["preview"]
    assert (preview / "index.html").read_text() == "<h1>v2</h1>\n"
    assert (preview / "assets" / "logo.svg").exists()
    assert (preview / "vendor" / "chart.js").exists()
    # excluded: the repo's history, a nested .git, and every ROOT dotfile
    assert not (preview / ".git").exists()
    assert not (preview / ".env").exists()
    assert not (preview / ".gitignore").exists()
    assert not (preview / "vendor" / ".git").exists()
    # NOT excluded: a dotfile that is not at the root
    assert (preview / "assets" / ".keep").exists()
    # --delete: last turn's leftovers go
    assert not (preview / "stale.html").exists()


def test_a_failing_rsync_raises_rather_than_reporting_success(ah, turn, tmp_path):
    """The one thing the harvest may not do is write a result that says
    `deployed` when nothing was copied."""
    bad = tmp_path / "bad-rsync"
    bad.write_text("#!/bin/sh\necho boom >&2\nexit 23\n", encoding="utf-8")
    bad.chmod(0o755)
    (turn["repo"] / "app.js").write_text("const x = 1;\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="rsync"):
        do_harvest(ah, turn, exit_code=0, rsync_bin=str(bad))


# ────────────────────────────────────────────────────────── result.json ──────

def test_result_json_matches_the_schema_and_is_atomic(ah, turn, tmp_path):
    rsync = _fake_rsync(tmp_path)
    (turn["repo"] / "app.js").write_text("const x = 1;\n", encoding="utf-8")
    returned = do_harvest(ah, turn, exit_code=0, rsync_bin=rsync)

    path = turn["result_dir"] / "result.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == returned
    assert set(on_disk) == {
        "session", "turn", "app_id", "exit", "halted", "no_changes", "files",
        "commit", "quarantine", "started_at", "ended_at", "model", "runner",
        "spool", "spool_redacted", "usage",
    }
    assert on_disk["session"] == 12 and on_disk["turn"] == 3
    assert on_disk["app_id"] == "abc234567xyz"
    assert on_disk["exit"] == 0
    assert on_disk["model"] == "claude-opus-5"
    assert on_disk["runner"] == "claude-code"
    assert set(on_disk["spool"]) == {"stdout", "stderr"}
    assert on_disk["spool"]["stdout"].endswith("/stdout.log")
    assert on_disk["started_at"] == "2026-09-06T10:00:00+00:00"
    assert on_disk["ended_at"].endswith("+00:00")
    # 0644: the broker reads this directory without owning it (spec §C).
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    # atomic = tmp + rename; no .tmp survives a finished harvest
    assert not (turn["result_dir"] / "result.json.tmp").exists()


def test_write_result_atomic_leaves_no_partial_file(ah, tmp_path):
    """A reader that opens the path at any instant sees a whole file or no
    file. Asserted by racing a reader against a hundred writes."""
    d = tmp_path / "r"
    d.mkdir()
    for i in range(100):
        ah.write_result_atomic(d, {"turn": i, "files": ["a"] * 200})
        loaded = json.loads((d / "result.json").read_text(encoding="utf-8"))
        assert loaded["turn"] == i
    assert list(p.name for p in d.iterdir()) == ["result.json"]


# ─────────────────────────────────────────────────────────── the watcher ─────

def test_watcher_fires_on_a_write_outside_dot_git(ah, turn):
    marker = turn["result_dir"] / "scaffolded"
    started = time.time()
    time.sleep(0.01)
    (turn["repo"] / "new.html").write_text("<p>hi</p>\n", encoding="utf-8")
    stamp = ah.watch_for_scaffolded(turn["repo"], marker, started,
                                    deadline=time.time() + 5, interval=0.05)
    assert stamp
    assert marker.read_text().strip() == stamp
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644


def test_watcher_does_not_fire_on_an_untouched_repo(ah, turn):
    """The definition that matters (§A, Claudette #2284): the repo exists
    BEFORE the turn starts and turn 2 opens with a full tree, so "a path
    exists" would fire at t=0 every time. Nothing has been written since
    `started`, so nothing is reported."""
    marker = turn["result_dir"] / "scaffolded"
    started = time.time() + 1          # everything on disk predates this
    assert ah.watch_for_scaffolded(turn["repo"], marker, started,
                                   deadline=time.time() + 0.2,
                                   interval=0.05) is None
    assert not marker.exists()


def test_watcher_ignores_the_git_init_that_precedes_the_turn(ah, tmp_path):
    """Measured at the keyboard's proving turn: `git init` creates `.git/` in
    the repo root moments before the watch starts, which bumps the ROOT
    directory's own mtime and fired the marker at t=0 with no file written.
    The root's timestamps do not count; a fresh repo with nothing in it stays
    quiet until a real entry appears."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    started = time.time()
    ah.ensure_repo(repo)                 # git init AFTER `started`, like run-apps.sh
    marker = tmp_path / "scaffolded"
    assert ah.watch_for_scaffolded(repo, marker, started,
                                   deadline=time.time() + 0.3,
                                   interval=0.05) is None
    assert not marker.exists()
    (repo / "index.html").write_text("<h1>hi</h1>\n", encoding="utf-8")
    assert ah.watch_for_scaffolded(repo, marker, started,
                                   deadline=time.time() + 0.3,
                                   interval=0.05) is not None


def test_watcher_ignores_changes_inside_dot_git(ah, turn):
    marker = turn["result_dir"] / "scaffolded"
    started = time.time()
    time.sleep(0.01)
    (turn["repo"] / ".git" / "COMMIT_EDITMSG").write_text("noise\n",
                                                          encoding="utf-8")
    assert ah.watch_for_scaffolded(turn["repo"], marker, started,
                                   deadline=time.time() + 0.3,
                                   interval=0.05) is None


# ──────────────────────────────────────────────────────────────── the CLI ────

def test_cli_harvest_runs_the_whole_thing_from_a_json_spec(ah, turn, tmp_path):
    """run-apps.sh's actual call: thirteen arguments in one JSON file, because
    a positional call with thirteen slots is a defect waiting for its first
    reorder."""
    (turn["repo"] / "app.js").write_text("const x = 1;\n", encoding="utf-8")
    spec = tmp_path / "harvest-input.json"
    spec.write_text(json.dumps({
        "repo": str(turn["repo"]), "exit_code": 0,
        "result_dir": str(turn["result_dir"]), "key_file": str(turn["key_file"]),
        "preview_dir": str(turn["preview"]), "turn": 3, "session": 12,
        "app_id": "abc234567xyz", "started_at": "2026-09-06T10:00:00+00:00",
        "model": "claude-opus-5", "quarantine_dir": str(turn["quarantine"]),
        "spool_stdout": "/srv/apps-turns/12/3/stdout.log",
        "spool_stderr": "/srv/apps-turns/12/3/stderr.log",
        "timed_out": False, "rsync_bin": _fake_rsync(tmp_path),
    }), encoding="utf-8")

    proc = subprocess.run([sys.executable, str(HARVEST_PY), "harvest", str(spec)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["commit"] and payload["halted"] is None
    assert json.loads((turn["result_dir"] / "result.json").read_text()) == payload


def test_cli_ensure_repo(ah, tmp_path):
    repo = tmp_path / "cli-repo"
    proc = subprocess.run([sys.executable, str(HARVEST_PY), "ensure-repo",
                           str(repo)], capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == "created"
    assert (repo / ".git").is_dir()


def test_cli_rejects_an_unknown_mode():
    proc = subprocess.run([sys.executable, str(HARVEST_PY), "publish"],
                          capture_output=True, text=True)
    assert proc.returncode == 64
