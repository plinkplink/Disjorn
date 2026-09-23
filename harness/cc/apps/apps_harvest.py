#!/usr/bin/python3
"""apps_harvest — the host side of an apps-builder turn (APPS v1 stage 2, §E).

INSTALLED AT /usr/local/lib/disjorn/apps_harvest.py, root:root 0755, and
called by /usr/local/lib/disjorn/run-apps.sh from INSIDE the transient unit,
as `res-appsbuilding`. It never runs as root and never touches podman.

WHY IT IS A PYTHON MODULE AND NOT MORE BASH
-------------------------------------------
Everything in here is a decision about a turn's OUTPUT — did the tree change,
does the change carry the seat's own API key, what gets committed, what gets
copied to the preview root, what the broker will read back. Those are the
rules SPECS/2026-09-06-apps-builder-seat.md §E spells out one by one, and a
rule that cannot be tested is a rule nobody can check. So the whole harvest is
importable functions over a real git repo in a temp dir
(harness/cc/tests/test_apps_harvest.py), and the shell wrapper only wires
podman to it.

THE ORDER IS THE CONTRACT (§E, restated so nothing is invented):

  exit != 0      commit the tree as `turn N (halted)` so the NEXT turn can see
                 the work; leave the preview root UNTOUCHED (a half-built app
                 must never replace a preview that worked);
                 halted = "timeout" (RuntimeMaxSec fired), "stopped" (the
                 same SIGTERM, with the launcher's `stop-requested` marker in
                 the result dir — slice (iv)), or "error".
  exit 0, clean  no_changes = True. Nothing committed, nothing copied.
  exit 0, dirty  SECRET SCAN first (raw key, its standard base64, its lowercase
                 hex). A hit: NO commit (the value must not enter history), NO
                 copy, the dirty files MOVE to the quarantine root (never
                 mounted into any seat, because the brief tells the next turn
                 to read /work first and would read the injection back in),
                 the tree resets to HEAD, halted = "secret".
                 Otherwise: commit `turn N`, rsync to the preview root
                 excluding `.git` anywhere and every dotfile at the repo ROOT
                 (`.env` is the file a model writes a key into by habit).

  result.json is then written ATOMICALLY (tmp + rename) so a broker that reads
  the directory at any instant sees either no file or a whole one.

  A turn that has ENDED always leaves a result. If the harvest itself fails
  partway (git broken, rsync gone, a disk full), the record is
  halted = "error" with an `error` string and whatever `commit` was made —
  a TERMINATED turn, not a claimed success (Gable #2327, Claudette #2329).
  Absence of result.json is §E's synthesized halt on the broker side; this
  module's job is to make that path unreachable from here.

THE RUNNER'S LAST WORD (slice (ii), §C1). The same final `result` line the
usage comes from also carries the runner's own closing TEXT, and the record
lifts two things out of it, because the spools are 0600 seat-only and the
broker (plink) must never open one:

  summary  the last non-empty line the runner wrote — the server renders it
           as a second sentence on §H's room line;
  flag     a line the builder opened with `FLAG:` — the broker narrates it
           to #custodian.

Both are PLAIN TEXT to everyone downstream and are made so here, once:
control characters removed, the seat's credential redacted exactly as in a
spool, 300 characters at most. Both keys are always present in the record
(null when the runner said nothing), because "the runner was silent" and
"this build is too old to have the key" are two different answers and no
reader should have to tell them apart by luck (Claudette #2336).

THE SENTENCE SLICE (ii) STARTS WITH (Claudette #2325, #2329 — three folds in
a row were the same defect): WE KEEP VALIDATING WHERE A THING IS INSTEAD OF
WHAT IT IS. A name is not a file, a path is not an owner, an rsync that
finished is not a preview that works. Decide on the object in hand (an fd,
an inode, a tree that is entirely ours), never on the name we were given.

THE PREVIEW IS PUBLISHED BY RENAME, NEVER EDITED IN PLACE (Gable #2327,
Claudette #2329): rsync into a fresh sibling `preview.tmp.<turn>` with
`--no-links --no-D --chmod=D0755,F0644`, then rename the sibling over the
live root. A fresh destination makes every file a transfer, which makes
`--chmod` total; `--no-links` drops symlinks as a CLASS (a link is never a
legitimate static asset, and `--safe-links` would judge by where the target
points — the predicate that failed three times); and a failed rsync leaves
the live preview exactly as it was, which is the §E guarantee `--delete`
into a live directory could never give.

ONE HARDENING BEYOND THE SPEC, called out because it is a deviation:
`git status --porcelain` does not list IGNORED files, so a turn that writes
`.gitignore` with `*` and then a key-bearing file would show a clean-ish tree
to the scan while `rsync` still copied the file into the preview (rsync knows
nothing about gitignore, and the file need not be a root dotfile). The dirty
set used for the SCAN and for QUARANTINE therefore includes ignored files
(`--ignored=matching`); the set used to decide `no_changes` does not, so the
"a turn that changed nothing" answer is unchanged.

THE WALL (SPECS/2026-09-13-apps-gitdir-outside-the-mount.md, D7)
----------------------------------------------------------------
THE TURN'S WRITABLE SURFACE IS THE WORK TREE AND NOTHING ELSE. The app's
repository lives at /srv/apps-git/<app-id>.git, which is mounted into no
container, ever; /srv/apps/<app-id> is the work tree and the only persistent
rw mount. Git metadata is host-only BY LOCATION, not by a check.

That is the whole fold. Until 2026-09-13 the repository sat at
/srv/apps/<app-id>/.git — inside the mount — so the turn wrote the metadata
that every host-side git command then read: a repo-local `core.fsmonitor` in
`.git/config` was HOST CODE EXECUTION as res-appsbuilding under this module's
own `git add -A` (and `filter.*.clean`, `core.sshCommand`,
`uploadpack.packObjectsHook` and `diff.*.textconv` are the same door;
`core.hooksPath=/dev/null` closes hooks and nothing else). Blocklisting config
keys was never the shape — the list is git's to extend.

So: every git invocation in this module names BOTH trees explicitly, in argv
AND in the environment (see `git` below), and no path the turn can write is
ever consulted to find the repository. A SHAPE CHECK ON A PATH THE TURN CAN
WRITE IS NOT A BOUNDARY AND IS NEVER WRITTEN DOWN AS ONE (the gate.py #2561
discipline): three folds in a row — `.exists()`, then "a real directory",
then "a real directory whose realpath is inside" — were each a better guess
about a name the adversary owns. The adversary no longer owns the name.

A `.git` entry the turn writes into the work tree is therefore a STRAY, and
strays are quarantined as a class (`sweep_stray_git` below) rather than
inspected. Work-tree files that name git machinery (`.gitattributes` filters,
`.gitmodules`) reference drivers the host-only config never defines, and stay
inert for that reason and not because anything scanned them.
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

# ------------------------------------------------------------------ constants

# The commit identity of the seat. Not a person and not a resident: every app
# repo's history is written by the same hand, and `git log` should say so.
GIT_USER_NAME = "apps-builder"
GIT_USER_EMAIL = "apps-builder@disjorn.local"

# The host-only git root (D1). NOTHING under it is mounted into any container,
# ever. The runtime value travels from the launcher through run-apps.sh's
# APPS_GIT_ROOT, exactly like the four roots that came before it; this constant
# is the default that a hand-run at the keyboard gets, and it is pinned equal to
# the launcher's GIT_ROOT by tests/test_apps_launch.py — the same discipline
# GIT_USER_NAME lives under, and for the same reason.
GIT_ROOT = "/srv/apps-git"

# `<git-root>/<app-id>.git` — the one place a repository is ever named from.
GIT_DIR_SUFFIX = ".git"

# A credential shorter than this cannot be scanned for without matching half
# the tree by accident (a 3-character "key" would flag every file that happens
# to contain those three bytes). Real Anthropic keys are ~100 chars, so this
# only ever fires on a malformed drop file — and when it does we say so on
# stderr rather than pretending a scan happened.
MIN_SCANNABLE_KEY_LEN = 12

# systemd sends SIGTERM when RuntimeMaxSec fires; bash reports a signalled
# child as 128+signum, and GNU `timeout` uses 124. Either means the clock ran
# out rather than the runner deciding to stop.
TIMEOUT_EXIT_CODES = frozenset({124, 137, 143})
# Slice (iv): the launcher's `stop` drops this in the result dir before
# `systemctl stop`. A SIGTERM with the marker present is the user's stop; the
# same SIGTERM without it is the clock. Existence is the whole signal.
STOP_MARKER = "stop-requested"

RESULT_NAME = "result.json"
SCAFFOLDED_NAME = "scaffolded"

# The turn's own runner. Named here (rather than derived) because the ledger
# and the stage detail carry it and slice (ii) keys its usage parser off it.
RUNNER = "claude-code"

# The builder raises something for a human by opening a line with this. It is
# a CONVENTION of the builder brief, not a protocol: a turn that never writes
# one is the normal turn, and a line that merely mentions the word is not one
# (the prefix is anchored at the start of the line).
FLAG_PREFIX = "FLAG:"

# Both lifted lines are bounded before they leave this process. 300 is §H's
# number for the room line, and the same bound on `flag` keeps a narration to
# #custodian one sentence rather than a pasted transcript.
REPORT_LINE_MAX = 300


def _now_iso() -> str:
    """UTC, second resolution, with an explicit offset. Timestamps in
    result.json are read by the broker and by a human at 3am; both want to know
    the zone without guessing."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _warn(msg: str) -> None:
    sys.stderr.write(f"apps-harvest: {msg}\n")
    sys.stderr.flush()


# ----------------------------------------------------------------------- git

class Repo(NamedTuple):
    """THE TWO TREES, ALWAYS TOGETHER AND NEITHER EVER DERIVED FROM THE OTHER.

    `git_dir` is /srv/apps-git/<app-id>.git, host-only and mounted nowhere.
    `work_tree` is /srv/apps/<app-id>, the turn's /work.

    They travel as one value because the failure this whole fold is about is a
    git call that names one of them and lets git find the other — and the one
    git finds by looking is the one the turn wrote."""
    git_dir: Path
    work_tree: Path


def repo_at(git_dir: str | os.PathLike, work_tree: str | os.PathLike) -> Repo:
    """The one constructor. A Repo is never half-built."""
    return Repo(Path(git_dir), Path(work_tree))


def repo_for(app_id: str, *, work_tree_root: str | os.PathLike,
             git_root: str | os.PathLike = GIT_ROOT) -> Repo:
    """The pair for one app id, from the two roots. The ONLY place the layout
    `<git-root>/<app-id>.git` beside `<apps-root>/<app-id>` is spelled."""
    return repo_at(Path(git_root) / f"{app_id}{GIT_DIR_SUFFIX}",
                   Path(work_tree_root) / app_id)


def _git_env(repo: Repo, *, identity: bool) -> dict:
    """The environment every git call in this module runs under.

    GIT_DIR / GIT_WORK_TREE ARE THE BELT; the flags in argv are the statement.
    Measured on git 2.47.3: a flagless `git status` with its cwd in the work
    tree reads a turn-written `.git` (pointer file or symlink) and FOLLOWS it,
    while the same call with these two variables set binds to the host-only
    git dir and never reads the entry at all. Both are set, on every call, so
    a git invocation that is one day written without the flags still cannot be
    pointed at a repository the turn chose.

    GIT_CONFIG_NOSYSTEM + GIT_CONFIG_GLOBAL=/dev/null: a config file this seat
    can write is a config file that can name a program to run. The seat owns
    its own HOME, so /dev/null is the global config and there is no system one.
    (The launcher's `_git` has set both since stage 3; this being the weaker of
    the two was a real difference, not a stylistic one.)

    The rest is the old reflex: nothing this process inherited decides who the
    committer is. `identity=False` for the read-only byte calls, where an
    author is not a thing that can be committed anyway.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_DIR": str(repo.git_dir),
        "GIT_WORK_TREE": str(repo.work_tree),
        "LC_ALL": "C",
    }
    if identity:
        env.update({
            "GIT_AUTHOR_NAME": GIT_USER_NAME,
            "GIT_AUTHOR_EMAIL": GIT_USER_EMAIL,
            "GIT_COMMITTER_NAME": GIT_USER_NAME,
            "GIT_COMMITTER_EMAIL": GIT_USER_EMAIL,
        })
    return env


def _git_argv(repo: Repo, args, git_bin: str) -> list:
    """`git --git-dir=<G> --work-tree=<W> -c core.hooksPath=/dev/null <args>`.

    BOTH TREES ARE NAMED, ALWAYS. `-C <repo>` used to be the whole statement,
    which meant git discovered the git dir from the work tree — from a path the
    turn writes. Nothing here looks at `<W>/.git` for any purpose.

    `core.hooksPath=/dev/null` stays, unchanged and unpromoted: it closed hooks
    and it never closed anything else (fsmonitor, clean filters, textconv,
    sshCommand). What closes those is that the config file now lives where the
    turn cannot write it."""
    return [git_bin, f"--git-dir={repo.git_dir}",
            f"--work-tree={repo.work_tree}",
            "-c", "core.hooksPath=/dev/null", *args]


def git(repo: Repo, *args: str, check: bool = True,
        git_bin: str = "git") -> subprocess.CompletedProcess:
    """Run git against the split repo, with a scrubbed environment.

    cwd is the WORK TREE, because a relative pathspec (`clean`, `checkout -- .`)
    is resolved against the cwd and the old `-C <repo>` put it there. It is not
    how the repository is found — the flags and the env decide that, and the
    probe above measured that they win over anything at the cwd."""
    return subprocess.run(
        _git_argv(repo, args, git_bin),
        check=check, capture_output=True, text=True,
        cwd=str(repo.work_tree), env=_git_env(repo, identity=True),
    )


def git_bytes(repo: Repo, *args: str, git_bin: str = "git") -> bytes:
    """git output as raw BYTES. The secret scan must not go through a decoder:
    a diff of a binary file is not valid UTF-8, and a `errors="replace"` decode
    would mangle exactly the bytes we are looking for."""
    proc = subprocess.run(
        _git_argv(repo, args, git_bin),
        capture_output=True, cwd=str(repo.work_tree),
        env=_git_env(repo, identity=False),
    )
    return proc.stdout


class GitDirIncomplete(RuntimeError):
    """`<git-root>/<app-id>.git` exists and is not a repository this house
    made. An INCIDENT, never an input — see `gitdir_state`."""


def _under(parent: str, child: str) -> bool:
    """`child` is `parent` or lives beneath it. Both must already be real
    paths; this compares strings and follows nothing."""
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def gitdir_state(git_dir: str | os.PathLike,
                 git_root: str | os.PathLike = GIT_ROOT) -> str:
    """`"absent"` or `"repo"`. EVERY OTHER SHAPE RAISES.

    G IS WRITTEN BY HOST CODE ONLY. It is not under any mount, so a turn
    cannot have made it — which means G existing as a symlink, as a file, as a
    directory whose realpath is outside the git root, or as a directory with no
    HEAD is an interrupted init or an interrupted migration. That is an
    incident and the keyboard decides what it was; it is not a signal to
    re-init, because re-initialising over a directory that might hold objects
    is the branch that loses an app's history.

    The refusal is by state, not by shape-of-a-name: there is no adversary on
    this path, and the check exists to stop US from writing over OUR own
    half-made tree."""
    git_dir = Path(git_dir)
    try:
        st = os.lstat(git_dir)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise GitDirIncomplete(f"gitdir_incomplete: {git_dir} ({exc})")
    if stat.S_ISLNK(st.st_mode):
        raise GitDirIncomplete(f"gitdir_incomplete: {git_dir} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise GitDirIncomplete(f"gitdir_incomplete: {git_dir} is not a directory")
    real = os.path.realpath(git_dir)
    if not _under(os.path.realpath(git_root), real):
        raise GitDirIncomplete(
            f"gitdir_incomplete: {git_dir} resolves to {real}, outside {git_root}")
    if not (Path(real) / "HEAD").is_file():
        raise GitDirIncomplete(f"gitdir_incomplete: {git_dir} has no HEAD")
    return "repo"


def ensure_repo(repo: Repo, git_root: str | os.PathLike = GIT_ROOT,
                git_bin: str = "git") -> bool:
    """Create the SPLIT repo if there is not one yet. True if it created.

    Called from run-apps.sh BEFORE the container starts, as res-appsbuilding —
    never as root and never by the broker, so §E's "the broker never writes
    under /srv/apps" is true from the first turn (spec §C, Gable #2295).

    "Already a repo?" is a fact about the HOST-ONLY tree (`<G>/HEAD` exists),
    never about anything in the work tree: `(work_tree / ".git").exists()` was
    the old question and the turn owned the answer.

    END STATE, whichever branch ran: `G` holds HEAD, `core.bare=false`,
    `core.worktree` UNSET, and there is no `.git` entry of any kind in `W`.
    Measured (git 2.47.3): `git --git-dir=G --work-tree=W init` writes nothing
    into `W` but DOES write `core.worktree=W` into `G/config`, so the unset is
    part of the init and not a tidy-up. The work tree is argv and environment,
    every call, and a config key that also names it is a second place for the
    answer to live. `--separate-git-dir` is not used and must not be: it writes
    a `.git` POINTER FILE into `W`, which is the exact entry this fold forbids.
    """
    repo.work_tree.mkdir(parents=True, exist_ok=True)
    if gitdir_state(repo.git_dir, git_root) == "repo":
        return False
    repo.git_dir.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main", git_bin=git_bin)
    # `config --unset` exits 5 on a key that is not there; the end state is
    # what matters and the suite pins it.
    git(repo, "config", "--unset", "core.worktree", check=False, git_bin=git_bin)
    git(repo, "config", "user.name", GIT_USER_NAME, git_bin=git_bin)
    git(repo, "config", "user.email", GIT_USER_EMAIL, git_bin=git_bin)
    os.chmod(repo.git_dir, 0o700)
    return True


def has_commits(repo: Repo, git_bin: str = "git") -> bool:
    return git(repo, "rev-parse", "--verify", "-q", "HEAD",
               check=False, git_bin=git_bin).returncode == 0


def porcelain(repo: Repo, *, ignored: bool = False,
              git_bin: str = "git") -> list[tuple[str, str]]:
    """`git status --porcelain=v1 -z -uall` as [(xy, path)].

    -z because a filename may contain a newline or a quote and the non-z form
    quotes them into something this parser would have to un-quote; -uall
    because an untracked DIRECTORY is reported as one entry otherwise, and both
    the scan and the quarantine want files.

    `ignored=True` adds `--ignored=matching`, which is the hardening documented
    in this module's docstring: a `.gitignore` written by the turn must not be
    able to hide a file from the secret scan.
    """
    args = ["status", "--porcelain=v1", "-z", "-uall"]
    if ignored:
        args.append("--ignored=matching")
    out = git(repo, *args, git_bin=git_bin).stdout
    entries: list[tuple[str, str]] = []
    fields = [f for f in out.split("\0") if f]
    i = 0
    while i < len(fields):
        field = fields[i]
        xy, path = field[:2], field[3:]
        # A rename/copy entry is followed by its ORIGIN path in the next NUL
        # field. We want the destination (the file that exists now), so the
        # origin is consumed and dropped.
        if xy and xy[0] in ("R", "C"):
            i += 1
        entries.append((xy, path))
        i += 1
    return entries


def dirty_paths(repo: Repo, *, ignored: bool = False,
                git_bin: str = "git") -> list[str]:
    """Repo-relative FILE paths that exist on disk and are not what HEAD says.

    Deletions are excluded: there is no file to scan and none to quarantine.

    Directory entries are EXPANDED. `--ignored=matching` reports a directory
    when the ignore pattern named a directory (`secrets/` in .gitignore comes
    back as the single entry `secrets/`), and a scan that skipped it because it
    is not a regular file would be a hole exactly where the hardening was
    supposed to be. So every directory entry is walked and its files listed.

    The list is what the secret scan reads and what quarantine moves.

    Nothing is pruned from the walk. `.git` used to be skipped here, on the
    reasoning that it was the repository; the repository is elsewhere now and
    a `.git` under the work tree is a stray — swept into quarantine before
    this function is ever called, and if one somehow survived the sweep,
    SCANNING it is the safe direction.
    """
    work = repo.work_tree
    paths: list[str] = []
    for xy, path in porcelain(repo, ignored=ignored, git_bin=git_bin):
        if not (xy == "!!" or xy.strip()):
            continue
        rel = path.rstrip("/")
        full = work / rel
        if full.is_symlink() or full.is_file():
            paths.append(rel)
        elif full.is_dir():
            for dirpath, dirnames, filenames in os.walk(full):
                for name in filenames:
                    paths.append(os.path.relpath(
                        os.path.join(dirpath, name), work))
    return sorted(set(paths))


def is_clean(repo: Repo, git_bin: str = "git") -> bool:
    """The `no_changes` question, and deliberately WITHOUT ignored files: a
    turn that wrote only files its own .gitignore hides has still changed
    nothing this house will ever publish. (The SCAN uses the wider set.)"""
    return not porcelain(repo, ignored=False, git_bin=git_bin)


def commit_all(repo: Repo, message: str,
               git_bin: str = "git") -> str | None:
    """`git add -A && git commit`. Returns the sha, or None if there was
    nothing to commit (git exits 1 on an empty commit, which is not an error
    here — a halted turn that touched nothing is a normal outcome)."""
    git(repo, "add", "-A", git_bin=git_bin)
    proc = git(repo, "commit", "--no-verify", "-q", "-m", message,
               check=False, git_bin=git_bin)
    if proc.returncode != 0:
        if not has_commits(repo, git_bin=git_bin) or "nothing to commit" in (
                proc.stdout + proc.stderr):
            return None
        raise RuntimeError(
            f"git commit failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}")
    return git(repo, "rev-parse", "HEAD", git_bin=git_bin).stdout.strip()


def commit_files(repo: Repo, sha: str,
                 git_bin: str = "git") -> list[str]:
    """The paths a commit touched — `git show --name-only`, which is what §E
    names as the source of `detail.files`."""
    out = git(repo, "show", "--name-only", "--pretty=format:", sha,
              git_bin=git_bin).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


# -------------------------------------------------------------- secret scan

def secret_patterns(key: str | bytes | None) -> list[tuple[str, bytes]]:
    """The three encodings §E names, as (label, bytes) needles.

    raw     the key as written in the drop file;
    base64  STANDARD base64 of the raw bytes (the one `base64` and every
            language's default emit), stripped of trailing '=' padding so a
            value embedded mid-string still matches;
    hex     lowercase hex of the raw bytes.

    A short or absent key yields NO patterns and says so on stderr. Scanning
    for a 3-byte needle would quarantine honest files, and silently scanning
    for nothing would be worse than either.
    """
    if key is None:
        return []
    raw = key.encode() if isinstance(key, str) else bytes(key)
    raw = raw.strip()
    if len(raw) < MIN_SCANNABLE_KEY_LEN:
        if raw:
            _warn(f"credential is {len(raw)} bytes, under the "
                  f"{MIN_SCANNABLE_KEY_LEN}-byte floor — NOT scanning for it "
                  f"(a needle this short matches honest files)")
        return []
    b64 = base64.b64encode(raw).rstrip(b"=")
    hexed = binascii.hexlify(raw)
    return [("raw", raw), ("base64", b64), ("hex", hexed)]


def read_key(key_file: str | os.PathLike, var: str = "ANTHROPIC_API_KEY") -> str | None:
    """The credential VALUE out of the seat's env drop file.

    Read here, in the harvest, and never handed to the container: this process
    is the only one that needs the plaintext, and it needs it purely to look
    for it in the turn's output. Parsing matches podman's env-file semantics
    (everything after the first '=' is literal, no quote stripping), because
    the same file is what podman reads.
    """
    try:
        text = Path(key_file).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _warn(f"cannot read credential file {key_file}: {exc} — "
              f"NOT scanning (see the refusal in the result)")
        return None
    value = None
    for line in text.splitlines():
        m = re.match(r"^\s*" + re.escape(var) + r"=(.*)$", line)
        if m:
            value = m.group(1)          # last assignment wins, like podman
    return value


def scan_for_secret(repo: Repo,
                    patterns: list[tuple[str, bytes]],
                    *, paths: list[str] | None = None,
                    git_bin: str = "git") -> dict | None:
    """Look for the seat's credential in everything this turn produced.

    Reads BYTES, never text: a diff of a binary file is not UTF-8 and the
    needle must survive verbatim.

    Covered: `git diff` (tracked modifications), `git diff --cached` (anything
    already staged), and the full contents of every dirty/untracked/ignored
    path. Returns {"encoding", "where"} on the FIRST hit, else None.
    """
    if not patterns:
        return None

    def hit(blob: bytes, where: str) -> dict | None:
        for label, needle in patterns:
            if needle and needle in blob:
                return {"encoding": label, "where": where}
        return None

    for what in (["diff"], ["diff", "--cached"]):
        found = hit(git_bytes(repo, *what, git_bin=git_bin),
                    f"git {' '.join(what)}")
        if found:
            return found

    if paths is None:
        paths = dirty_paths(repo, ignored=True, git_bin=git_bin)
    for rel in paths:
        full = repo.work_tree / rel
        try:
            if full.is_symlink():
                found = hit(os.readlink(full).encode(), f"{rel} (symlink target)")
            elif full.is_file():
                found = hit(full.read_bytes(), rel)
            else:
                continue
        except OSError as exc:
            _warn(f"cannot read {rel} for the secret scan: {exc}")
            continue
        if found:
            return found
    return None


def quarantine(work_tree: str | os.PathLike, paths: list[str],
               dest: str | os.PathLike) -> list[str]:
    """Move every named path OUT of the work tree into `dest`, paths kept.

    The quarantine root is 0700 and is never mounted into any seat: the brief
    tells the next turn to read /work first, so leaving a poisoned file in
    place would feed the injection straight back in (Claudette #2293).
    Returns the paths actually moved. It moves NAMES IT WAS GIVEN and
    follows nothing: a symlink is renamed, never resolved.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    moved = []
    for rel in paths:
        src = Path(work_tree) / rel
        if not (src.exists() or src.is_symlink()):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        moved.append(rel)
    return moved


# ------------------------------------------------------------ stray .git

# Where a swept `.git` goes, under the turn's own quarantine directory. A
# subdirectory of its own because a secret hit quarantines dirty PATHS into the
# same root and the two are different findings.
STRAY_GIT_DEST = "stray-git"
STRAY_GIT_NAME = ".git"


def find_stray_git(work_tree: str | os.PathLike) -> list[str]:
    """Every entry named `.git` under the work tree, as relative paths.

    THE MEASUREMENT THIS EXISTS FOR (git 2.47.3, 2026-09-13). In a split work
    tree, a `.git` entry — real directory, symlink to a directory, or a
    `gitdir:` pointer file, at the top level or nested — is listed by NONE of
    `status --porcelain -uall --ignored=matching`, `add -A`, or `ls-files`, and
    `clean` never removes one. A file beside it is listed normally. So a turn
    that writes `.git/payload` writes the one place in the tree that the secret
    scan, the commit, the reset and the preview all walk straight past.

    Invisible is not harmless: it is a payload that survives every turn.
    (It is also, with a `gitdir:` pointer, the one shape `add -A` reacts to at
    all — it records the enclosing directory as a GITLINK, so a nested pointer
    file puts a commit of somebody else's repository into this app's history.)

    LSTAT, NEVER FOLLOW. A symlinked `.git` is collected by NAME and the walk
    does not descend through it; a real `.git` directory is collected whole and
    not walked into either — it is moved as one thing, and enumerating a tree
    the turn wrote in order to move it piecewise is work with no reader."""
    work_tree = Path(work_tree)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(work_tree, followlinks=False):
        for name in list(dirnames):
            if name == STRAY_GIT_NAME:
                dirnames.remove(name)          # collected whole, never entered
                found.append(os.path.relpath(
                    os.path.join(dirpath, name), work_tree))
        for name in filenames:                 # a pointer file, or a symlink
            if name == STRAY_GIT_NAME:
                found.append(os.path.relpath(
                    os.path.join(dirpath, name), work_tree))
    return sorted(found)


def sweep_stray_git(work_tree: str | os.PathLike,
                    quarantine_dir: str | os.PathLike) -> list[str]:
    """Move every stray `.git` out of the work tree. Returns what moved.

    A CLASS, NOT A CASE. Nothing here reads the entry to decide whether this
    particular one was dangerous — that is the shape-check reflex this spec
    exists to end. The turn's repository is not in this tree, so no `.git`
    here has a legitimate author, and each one is moved to the turn's
    quarantine where a human can read it at leisure.

    The turn is not halted for it: a vendored dependency that dragged a `.git`
    in is a builder mistake, not an attack, and the two are indistinguishable
    from here. The record names the paths (`stray_git`), which is what makes
    it reviewable."""
    paths = find_stray_git(work_tree)
    if not paths:
        return []
    moved = quarantine(work_tree, paths, Path(quarantine_dir) / STRAY_GIT_DEST)
    if moved:
        _warn(f"{len(moved)} stray .git entr(ies) moved out of the work tree: "
              + ", ".join(moved))
    return moved


def reset_tree(repo: Repo, git_bin: str = "git") -> None:
    """Back to HEAD, exactly. `checkout -- .` needs a HEAD to check out; a
    first turn that tripped the scan has none, and `clean -fd` alone is the
    whole reset in that case."""
    if has_commits(repo, git_bin=git_bin):
        git(repo, "reset", "-q", git_bin=git_bin)       # unstage anything add'ed
        git(repo, "checkout", "--", ".", check=False, git_bin=git_bin)
    # -x too: the quarantine moved every ignored FILE out, and an ignored
    # directory left standing is exactly the shape the next turn's "read
    # /work first" would wander into. After a secret hit the session ends,
    # so nothing legitimate is lost by clearing ignored leftovers.
    git(repo, "clean", "-fdxq", check=False, git_bin=git_bin)


# ------------------------------------------------------------------- preview

def preview_argv(work_tree: str | os.PathLike, dest: str | os.PathLike,
                 rsync_bin: str = "rsync") -> list[str]:
    """The exact rsync argv, factored out so a test can assert it without
    needing rsync installed. `dest` is the FRESH sibling, never the live root.

    `--no-links`      symlinks are dropped as a class. A served static preview
                      has no legitimate use for one, and any filter on where
                      a link points (`--safe-links`) is a decision about a
                      name — `x -> .env` is "inside the tree" and lands in
                      the served root dangling (Gable #2327, Claudette #2329).
    `--no-D`          no devices, no FIFOs, no sockets either: the preview
                      holds regular files and directories, nothing else.
    `--chmod=D0755,F0644`
                      the modes spec §C wants, applied by rsync to every path
                      it transfers — and because the destination is fresh,
                      that is every path. No chmod walk afterwards, so no
                      `os.chmod` on a name that might not be the thing.
    `--exclude .git`  unanchored. The repository itself is no longer in this
                      tree at all, and the stray sweep has already moved every
                      `.git` entry out — this stays because a published tree
                      must never carry one whatever else changes, and because a
                      preview is the one artefact a stranger can fetch.
    `--exclude /.*`   anchored to the transfer root by the leading slash, so it
                      drops `.env`, `.gitignore`, `.claude` and friends at the
                      repo root — `.env` is the file a model writes a key into
                      by habit (Gable #2286) — while leaving a legitimate
                      `assets/.keep` alone.
    No `--delete`: the destination is empty, and `--delete` into a LIVE root
    is exactly the half-updated preview §E forbids.
    Trailing slash on the source: copy the CONTENTS of the repo, not the repo
    directory into the sibling.
    """
    return [rsync_bin, "-a", "--no-links", "--no-D", "--chmod=D0755,F0644",
            "--exclude", ".git", "--exclude", "/.*",
            f"{str(work_tree).rstrip('/')}/", f"{str(dest).rstrip('/')}/"]


def _remove(path: Path) -> None:
    """Remove a leftover of OURS (a stale sibling or moved-aside root from a
    crashed harvest), whatever shape it has. A symlink is unlinked, never
    followed — the one lstat-then-act in this module, and it only ever runs
    on names this module itself creates beside the preview root."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


STAGING_NAME = ".staging"


def staging_root(preview_dir: str | os.PathLike) -> Path:
    """`<www-root>/.staging/<app-id>/` — where the sibling and the moved-aside
    old root live while a publish is in flight. NOT under `<app-id>/`: the
    served set is `<www-root>/<app-id>/preview/` and stage 3 may well serve
    the app DIRECTORY, in which case a sibling beside `preview/` is a URL for
    as long as it exists — a half-transferred tree, or a superseded copy
    forever if its removal ever failed (Claudette #2336). `.staging` can never
    collide with an app id (`^[a-z2-7]{12}$`) and is 0700: nothing but this
    user reads a tree that has not been published. Same filesystem as the
    live root, so the rename is still a rename."""
    preview_dir = Path(preview_dir)
    app_dir = preview_dir.parent
    return app_dir.parent / STAGING_NAME / app_dir.name


def preview_sibling(preview_dir: str | os.PathLike, turn: int) -> Path:
    """`preview.tmp.<turn>` under the staging root. Ours alone: created empty
    by this process, filled by this process's rsync, renamed by this
    process."""
    preview_dir = Path(preview_dir)
    return staging_root(preview_dir) / f"{preview_dir.name}.tmp.{int(turn)}"


def preview_old(preview_dir: str | os.PathLike, turn: int) -> Path:
    """Where the superseded live root goes for the instant between the two
    renames, and until it is removed."""
    preview_dir = Path(preview_dir)
    return staging_root(preview_dir) / f"{preview_dir.name}.old.{int(turn)}"


def copy_preview(work_tree: str | os.PathLike, preview_dir: str | os.PathLike,
                 turn: int, rsync_bin: str = "rsync") -> None:
    """Publish the committed tree to the preview root BY RENAME.

    1. a fresh sibling `preview.tmp.<turn>` under the staging root (any
       stale one from a crashed harvest removed first — it was ours and
       never went live);
    2. rsync into it with the argv above (modes set by rsync, symlinks and
       specials dropped, nothing walked afterwards);
    3. the live root is moved aside, the sibling takes its name, the old
       root is removed. A rename cannot replace a non-empty directory in one
       syscall, so this is two renames; the window between them is the only
       instant a reader can see no preview at all, and it is the same
       instant either way. If the second rename fails the old root is put
       back and the error propagates — the preview that worked is still the
       preview that is served. Removing the old root afterwards has nothing
       left to protect, so its failure is a warning, not a demoted turn
       (Claudette #2336).

    The app directory `<www-root>/<app-id>/` is created HERE, by the
    publisher, on the first publish — not by the wrapper on every turn. "A
    preview exists" and "a preview worked" are the same observable
    (Claudette #2336).

    A failed rsync removes the sibling (the tree is in the commit; there is
    nothing to inspect that `git show` does not have) and raises; the live
    root has not been touched. harvest() turns that raise into a
    halted = "error" record.
    """
    preview_dir = Path(preview_dir)
    staging = staging_root(preview_dir)
    staging.mkdir(parents=True, exist_ok=True)
    os.chmod(staging.parent, 0o700)      # <www-root>/.staging: ours only
    sibling = preview_sibling(preview_dir, turn)
    _remove(sibling)
    os.mkdir(sibling, 0o755)
    os.chmod(sibling, 0o755)             # our own fresh directory, umask-proof

    argv = preview_argv(work_tree, sibling, rsync_bin=rsync_bin)
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(sibling, ignore_errors=True)
        raise RuntimeError(
            f"rsync to the preview sibling failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}")

    old = preview_old(preview_dir, turn)
    _remove(old)
    app_dir = preview_dir.parent
    if not app_dir.is_dir():
        os.makedirs(app_dir, exist_ok=True)
        os.chmod(app_dir, 0o755)         # the served path; a house process reads it
    had_live = preview_dir.is_symlink() or preview_dir.exists()
    if had_live:
        os.rename(preview_dir, old)
    try:
        os.rename(sibling, preview_dir)
    except OSError:
        if had_live:
            os.rename(old, preview_dir)
        raise
    if had_live:
        try:
            _remove(old)
        except OSError as exc:
            _warn(f"published, but could not remove the superseded root "
                  f"{old}: {exc} — it is under the unserved staging root and "
                  f"the next publish will retry")


# ------------------------------------------------------------------- watcher

def watch_for_scaffolded(work_tree: str | os.PathLike,
                         marker_path: str | os.PathLike,
                         started_at: float, *, deadline: float | None = None,
                         interval: float = 0.5,
                         _clock=time.time) -> str | None:
    """Write the `scaffolded` marker on the first change under /work.

    Runs INSIDE the unit as a background process, because the broker runs as
    plink and must never watch (or own) anything under /srv/apps (Gable #2295,
    Claudette #2299). The broker reads the marker; it does not hold the watch.

    "First change" is §A's definition and not the obvious one: turn 2 begins
    with a full tree, so "a path exists" would fire at t=0 every turn
    (Claudette #2284). What counts is a path whose mtime or ctime is at or
    after `started_at`.

    Returns the ISO timestamp written, or None if the deadline passed first.
    A poll, not inotify: 0.5s is invisible next to a model turn, and it needs
    no package in the image and no fd budget on a tree the turn is rewriting.
    """
    work_tree = Path(work_tree)
    marker_path = Path(marker_path)
    while True:
        if _changed_since(work_tree, started_at):
            stamp = _now_iso()
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = marker_path.with_name(marker_path.name + ".tmp")
            tmp.write_text(stamp + "\n", encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, marker_path)
            return stamp
        if deadline is not None and _clock() >= deadline:
            return None
        time.sleep(interval)


def _changed_since(work_tree: Path, started_at: float) -> bool:
    """Any ENTRY under the work tree created or modified at or after
    `started_at`. The root's own mtime does NOT count: a directory's mtime
    moves when anything is created in it, and a file created directly in the
    root registers through its own timestamps anyway, so nothing is lost.

    `.git` USED TO BE SKIPPED HERE and no longer is. The reason for the skip
    was `git init` touching `<work-tree>/.git` moments before the watch
    started, which fired the marker at t=0 on turn 1 (measured at the
    keyboard's proving turn). The init writes nothing into the work tree now —
    the git dir is outside it — so the reason is gone, and what is left is that
    a turn whose only write is `.git/x` has still written, and the room should
    say so."""
    root = Path(work_tree)
    for dirpath, dirnames, filenames in os.walk(root):
        entries = [os.path.join(dirpath, d) for d in dirnames]
        entries += [os.path.join(dirpath, f) for f in filenames]
        for name in entries:
            try:
                st = os.lstat(name)
            except OSError:
                continue
            if max(st.st_mtime, st.st_ctime) >= started_at:
                return True
    return False


# -------------------------------------------------------------------- result

def write_result_atomic(result_dir: str | os.PathLike, payload: dict) -> str:
    """result.json, whole or absent, never half.

    The broker inotifies this directory and will read the file the instant it
    appears, from another process that this one cannot coordinate with. A
    tmp-file rename inside the same directory is atomic on every filesystem
    this house runs on; a plain open-and-write is a race with a reader that
    would then report a turn as corrupt.
    """
    result_dir = Path(result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    final = result_dir / RESULT_NAME
    tmp = result_dir / (RESULT_NAME + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o644)                # the broker reads it and does not own it
    os.replace(tmp, final)
    return str(final)


# -------------------------------------------------------------------- spools

def redact_text(blob, patterns: list[tuple[str, bytes]]):
    """Replace every encoding of the credential in `blob` with a labelled mark.

    THE one needle loop, and it decides on the OBJECT IN HAND rather than on
    which caller it came from: the spools arrive as bytes (a needle has to
    survive verbatim through a stream that is not valid UTF-8), a summary line
    or an exception message arrives as str. Same rule, same marker, both
    shapes — so the next place that has to be redacted cannot acquire a third,
    slightly different copy of it. Returns the same type it was given.

    latin-1 is the decode for the needles on the str side because it is TOTAL:
    every byte maps to one code point, so a str haystack is searched for
    exactly the bytes the scan looks for and nothing here can raise.
    """
    if isinstance(blob, bytes):
        for label, needle in patterns:
            if needle:
                blob = blob.replace(needle, b"[REDACTED:" + label.encode() + b"]")
        return blob
    for label, needle in patterns:
        if needle:
            blob = blob.replace(needle.decode("latin-1"), f"[REDACTED:{label}]")
    return blob


def redact_spools(paths: list[str], patterns: list[tuple[str, bytes]]) -> bool:
    """Replace every encoding of the key in the spool files, in place.

    Returns True if anything was redacted. Spools are the runner's raw
    stdout/stderr; an authentication failure is the classic place a client
    library echoes a credential. Redacting keeps a later reader (a human at
    3am, the v2 scrollback) from meeting the value — and it happens BEFORE the
    result line is parsed, so the summary lifted out of it is already clean
    before `runner_report` cleans it again.
    """
    redacted = False
    for p in paths:
        if not p or not patterns:
            continue
        try:
            blob = Path(p).read_bytes()
        except OSError:
            continue
        out = redact_text(blob, patterns)
        if out != blob:
            tmp = Path(p + ".tmp")
            tmp.write_bytes(out)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
            redacted = True
    return redacted


def parse_result_claude_code(spool_stdout: str) -> tuple[dict | None, str | None]:
    """Claude Code's final stream-json `result` line, as (usage, text).

    ONE object carries both things the record needs out of the spool: `usage`
    (input/output/cache_creation/cache_read) with `total_cost_usd`, and
    `result` — the runner's own closing text, from which `summary` and `flag`
    are lifted. They are read together because they ARE together; two passes
    over the same file could disagree about which line was the last one.

    The ONE runner-specific parser the spec allows (§A); a different runner
    means a sibling function keyed by [apps].runner, not a change here.
    Absent, unreadable or unparsable → (None, None), never a guess; a result
    line with no `result` string → (usage, None), which is not the same thing
    and is not flattened into it.
    """
    if not spool_stdout:
        return None, None
    try:
        lines = Path(spool_stdout).read_bytes().splitlines()
    except OSError:
        return None, None
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw.startswith(b"{"):
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("type") != "result":
            continue
        usage = obj.get("usage") or {}
        text = obj.get("result")
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "total_cost_usd": obj.get("total_cost_usd"),
            "num_turns": obj.get("num_turns"),
            "duration_ms": obj.get("duration_ms"),
            "is_error": obj.get("is_error"),
        }, (text if isinstance(text, str) else None)
    return None, None


def _one_line(text: str, patterns: list[tuple[str, bytes]]) -> str:
    """A line of runner-authored text, made safe to hand anyone.

    Control characters OUT (the room line, the audit line and #custodian all
    take this as plain text, and an escape sequence is not text); the
    credential redacted; then, and only then, the 300-character bound —
    truncating first could leave the head of a key past the end of a needle
    that no longer matches.
    """
    clean = "".join(c for c in text if c >= " " and c != "\x7f").strip()
    return redact_text(clean, patterns)[:REPORT_LINE_MAX]


def runner_report(text: str | None,
                  patterns: list[tuple[str, bytes]]) -> tuple[str | None, str | None]:
    """(summary, flag) out of the runner's closing text.

    `flag`     the FIRST line that opens with `FLAG:`, the prefix removed. A
               builder raises one thing per turn; a second one is the same
               concern restated, and taking the first keeps the narration
               deterministic.
    `summary`  the LAST non-empty line that is not a FLAG line. Last, because
               a runner's closing paragraph ends with its conclusion; not a
               FLAG line, because the flag is already being narrated and the
               room line should not repeat it as a summary. (§C1 says "not the
               FLAG line"; every FLAG line is excluded rather than only the
               one that was lifted, so a turn whose last line is a second flag
               does not smuggle one into the room by position.)

    Either may be None, and None means the runner did not say it. Nothing is
    invented to fill the gap.
    """
    if not text:
        return None, None
    lines = [ln.strip() for ln in text.strip().splitlines()]
    lines = [ln for ln in lines if ln]

    flag = None
    for ln in lines:
        if ln.startswith(FLAG_PREFIX):
            flag = _one_line(ln[len(FLAG_PREFIX):], patterns) or None
            break

    summary = None
    for ln in reversed(lines):
        if ln.startswith(FLAG_PREFIX):
            continue
        # "Non-empty" is decided on the line AFTER it is cleaned, not on the
        # raw one: a trailing line that cleans away to nothing (a stray bell,
        # a lone DEL) is a blank line wearing bytes, and taking it would
        # silently drop the sentence the runner actually ended on.
        summary = _one_line(ln, patterns) or None
        if summary is not None:
            break
    return summary, flag


# ------------------------------------------------------------------- harvest

def _skeleton(exit_code: int, *, session: int, turn: int, app_id: str,
              started_at: str, ended_at: str | None, model: str,
              spool_stdout: str, spool_stderr: str) -> dict:
    """Every key result.json ever has, with its "nothing happened" value.

    ONE builder, because there are two writers: the harvest proper, and the
    pre-flight refusal (`refuse`) for a turn that never started. A record with
    a key missing and a record with the key set to null are two different
    answers to a broker reading with .get(), and a second hand-rolled dict is
    how they drift (Claudette #2336).
    """
    return {
        "session": int(session),
        "turn": int(turn),
        "app_id": app_id,
        "exit": int(exit_code),
        "halted": None,
        "no_changes": False,
        "files": [],
        "commit": None,
        "quarantine": None,
        # Every `.git` entry the turn left in the work tree, moved out before
        # anything read the tree (D5). Always present, empty on the normal
        # turn: "no strays" and "this build predates the sweep" are two
        # different answers.
        "stray_git": [],
        "error": None,
        "started_at": started_at,
        "ended_at": ended_at or _now_iso(),
        "model": model,
        "runner": RUNNER,
        "spool": {"stdout": spool_stdout, "stderr": spool_stderr},
        # Set again inside _branches; present HERE so an error record has the
        # same shape as a success record (Claudette #2336).
        "spool_redacted": False,
        "usage": None,
        # The runner's last word (slice (ii), §C1). Always present, null when
        # the runner said nothing — the broker reads these with .get() for the
        # records written before this key existed, and reads a real answer
        # from every record written after it.
        "summary": None,
        "flag": None,
    }


def refuse(result_dir: str | os.PathLike, reason: str, *, session: int,
           turn: int, app_id: str, started_at: str, model: str = "",
           spool_stdout: str = "", spool_stderr: str = "",
           exit_code: int = 1) -> dict:
    """The record for a turn that was REFUSED BEFORE IT STARTED.

    The one caller is run-apps.sh, when `ensure-repo` raises — today only
    `GitDirIncomplete`, a half-made host-only git dir that the keyboard has to
    look at. No container ran, so there is nothing to commit, nothing to scan
    and nothing to publish; what the house needs is the same record every other
    ended turn leaves, saying `halted = "error"` and why.

    A turn that ends without result.json is §E's synthesized halt on the
    broker side, and it says "the unit died" — which is true and useless. This
    says which directory to go and look at."""
    payload = _skeleton(exit_code, session=session, turn=turn, app_id=app_id,
                        started_at=started_at, ended_at=None, model=model,
                        spool_stdout=spool_stdout, spool_stderr=spool_stderr)
    payload["halted"] = "error"
    payload["error"] = reason
    _warn(f"REFUSED before the container: {reason}")
    return _finish(result_dir, payload)


def harvest(repo: Repo, exit_code: int,
            result_dir: str | os.PathLike, key_file: str | os.PathLike | None,
            preview_dir: str | os.PathLike, turn: int, *,
            session: int, app_id: str, started_at: str, ended_at: str | None = None,
            model: str = "", quarantine_dir: str | os.PathLike | None = None,
            spool_stdout: str = "", spool_stderr: str = "",
            timed_out: bool = False, rsync_bin: str = "rsync",
            git_bin: str = "git") -> dict:
    """§E's branch order, in §E's order, and then result.json.

    Returns the payload it wrote. A turn that failed is a result, not an
    exception — and so is a HARVEST that failed: git or rsync breaking
    partway becomes halted = "error" with the exception in `error` and any
    commit already made in `commit` (Gable #2327, Claudette #2329). The
    preview root is untouched on every such path (copy_preview publishes by
    rename). The only thing that can still raise is writing result.json
    itself, and _cli_harvest makes that loud.
    """
    payload = _skeleton(
        exit_code, session=session, turn=turn, app_id=app_id,
        started_at=started_at, ended_at=ended_at, model=model,
        spool_stdout=spool_stdout, spool_stderr=spool_stderr)
    halted_reason = None
    if exit_code != 0:
        if timed_out or exit_code in TIMEOUT_EXIT_CODES:
            halted_reason = ("stopped"
                             if (Path(result_dir) / STOP_MARKER).exists()
                             else "timeout")
        else:
            halted_reason = "error"

    patterns = secret_patterns(read_key(key_file)) if key_file else []
    try:
        return _branches(repo, payload, halted_reason, patterns, result_dir,
                         key_file, preview_dir, turn, quarantine_dir,
                         spool_stdout, spool_stderr, rsync_bin, git_bin)
    except Exception as exc:          # noqa: BLE001 — the record is the point
        # An error string is still a string, and an exception message quotes
        # its input: same needle loop as the spools.
        text = redact_text(f"{type(exc).__name__}: {exc}", patterns)
        payload["halted"] = payload["halted"] or "error"
        payload["error"] = text
        _warn(f"harvest FAILED partway — recording a halted turn: {text}")
        return _finish(result_dir, payload)


def _branches(repo, payload, halted_reason, patterns, result_dir, key_file,
              preview_dir, turn, quarantine_dir, spool_stdout, spool_stderr,
              rsync_bin, git_bin) -> dict:
    """The §E branch order proper. Everything in here may raise; harvest()
    turns a raise into a record."""
    if key_file and not patterns:
        _warn("no scannable credential — the turn's output is being published "
              "WITHOUT a secret scan; check the drop file")

    # The spools first: the runner's raw stream is exactly where an auth
    # failure prints a key (Claudette, slice (i) review). They are 0600 and
    # seat-only, never published, but a value sitting in a file is a value
    # sitting in a file — redact in place, record that it happened.
    payload["spool_redacted"] = redact_spools(
        [spool_stdout, spool_stderr], patterns)
    # Usage AND the runner's closing text live in result.json, parsed HERE as
    # the seat, so the spools can stay 0600 seat-only and the broker (plink)
    # never needs to open them. Parsed AFTER the redaction above, so the text
    # comes off a file the key has already left; `runner_report` redacts it
    # again anyway, because a spool that could not be rewritten is a warning,
    # not a reason to publish the value.
    payload["usage"], report_text = parse_result_claude_code(spool_stdout)
    payload["summary"], payload["flag"] = runner_report(report_text, patterns)

    # BEFORE THE SCAN, and before anything else reads the tree (D5). A `.git`
    # entry the turn wrote is invisible to every porcelain this module drives —
    # status, add, ls-files, clean all walk past it — so it is the one place a
    # payload could sit unscanned across turns. The sweep moves the entry out
    # by lstat; what is left behind is an ordinary tree that the scan below
    # sees whole. This never halts the turn: the paths are in the record.
    quar = Path(quarantine_dir) if quarantine_dir else Path(result_dir) / "quarantine"
    payload["stray_git"] = sweep_stray_git(repo.work_tree, quar)

    # The scan set is WIDER than "is the tracked tree dirty": --ignored=matching
    # includes files a self-written .gitignore would hide. An ignored-only
    # turn is `no_changes` to git and to the bar, but it is still scanned, and
    # a hit still quarantines — otherwise the payload sits in /work for the
    # next turn's "read /work first" (Claudette, slice (i) review).
    scan_paths = dirty_paths(repo, ignored=True, git_bin=git_bin)
    found = scan_for_secret(repo, patterns, paths=scan_paths, git_bin=git_bin) \
        if scan_paths else None
    if found:
        # The headline is decided HERE, on the hit; a quarantine or reset
        # that then fails adds an `error` and does not downgrade it.
        dest = quar
        payload["halted"] = "secret"
        payload["quarantine"] = str(dest)
        payload["files"] = []
        moved = quarantine(repo.work_tree, scan_paths, dest)
        reset_tree(repo, git_bin=git_bin)
        _warn(f"SECRET in the turn's output ({found['encoding']} in "
              f"{found['where']}) — {len(moved)} path(s) quarantined at {dest}, "
              f"nothing committed, nothing copied")
        return _finish(result_dir, payload)

    if is_clean(repo, git_bin=git_bin):
        if halted_reason:
            # Halted before it wrote anything trackable: nothing to keep,
            # preview untouched.
            payload["halted"] = halted_reason
            return _finish(result_dir, payload)
        # The runner answered, refused, or decided nothing needed changing.
        # This is a TERMINAL, non-error outcome and it must end the bar's wait
        # (§A: `files_written` with detail.no_changes).
        payload["no_changes"] = True
        return _finish(result_dir, payload)

    if halted_reason:
        # HALTED with clean output: commit so the next turn inherits the
        # work; do NOT touch the preview root — a half-built app must never
        # replace one that worked (Claudette #2284).
        sha = commit_all(repo, f"turn {int(turn)} (halted)", git_bin=git_bin)
        payload["commit"] = sha
        payload["files"] = commit_files(repo, sha, git_bin=git_bin) if sha else []
        payload["halted"] = halted_reason
        return _finish(result_dir, payload)

    sha = commit_all(repo, f"turn {int(turn)}", git_bin=git_bin)
    payload["commit"] = sha
    payload["files"] = commit_files(repo, sha, git_bin=git_bin) if sha else []
    copy_preview(repo.work_tree, preview_dir, int(turn), rsync_bin=rsync_bin)
    return _finish(result_dir, payload)


def _finish(result_dir, payload: dict) -> dict:
    write_result_atomic(result_dir, payload)
    return payload


# ----------------------------------------------------------------------- CLI

def _cli_watch(argv: list[str]) -> int:
    repo, marker, started_at = argv[0], argv[1], float(argv[2])
    deadline = float(argv[3]) if len(argv) > 3 and argv[3] else None
    stamp = watch_for_scaffolded(repo, marker, started_at, deadline=deadline)
    if stamp:
        print(stamp)
        return 0
    return 1


def _cli_ensure_repo(argv: list[str]) -> int:
    """`ensure-repo <git-dir> <work-tree> [git-root]` — both trees, named.

    The order is (metadata, content), the same order every git argv in this
    module is written in. A GitDirIncomplete reaches the wrapper as exit 2 with
    the reason on stderr; the wrapper turns that into the turn's record.
    """
    git_root = argv[2] if len(argv) > 2 else GIT_ROOT
    try:
        created = ensure_repo(repo_at(argv[0], argv[1]), git_root)
    except GitDirIncomplete as exc:
        _warn(str(exc))
        return 2
    print("created" if created else "present")
    return 0


def _cli_refuse(argv: list[str]) -> int:
    """`refuse <spec.json> <reason>` — the record for a turn that never ran.

    It takes the SAME spec file the harvest takes, because the fields are the
    same fields and a second argument list is a second thing to keep in step.
    """
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    payload = refuse(
        spec["result_dir"], argv[1],
        session=int(spec["session"]), turn=int(spec["turn"]),
        app_id=spec["app_id"], started_at=spec["started_at"],
        model=spec.get("model", ""),
        spool_stdout=spec.get("spool_stdout", ""),
        spool_stderr=spec.get("spool_stderr", ""),
        exit_code=int(spec.get("exit_code", 1)) or 1)
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cli_harvest(argv: list[str]) -> int:
    """`harvest <json-file>` — every argument in one JSON object, because there
    are fifteen of them and a positional bash call with fifteen slots is a
    defect waiting for its first reorder."""
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    try:
        payload = _harvest_from_spec(spec)
    except Exception as exc:          # noqa: BLE001 — last resort, loud
        # harvest() already turned every branch failure into a record; what
        # reaches here is result.json ITSELF failing to be written (or the
        # spec being unreadable). There is nothing left to write it with, so
        # the one thing this process can still do is say so where the unit's
        # journal will keep it. run-apps.sh exits non-zero and the broker
        # treats a unit that ended with no result.json as a halt (§E).
        _warn("FATAL: could not write result.json — the broker must "
              f"synthesize the halt for this turn: {type(exc).__name__}: {exc}")
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


def _harvest_from_spec(spec: dict) -> dict:
    return harvest(
        repo_at(spec["git_dir"], spec["repo"]),
        int(spec["exit_code"]), spec["result_dir"],
        spec.get("key_file"), spec["preview_dir"], int(spec["turn"]),
        session=int(spec["session"]), app_id=spec["app_id"],
        started_at=spec["started_at"], ended_at=spec.get("ended_at"),
        model=spec.get("model", ""), quarantine_dir=spec.get("quarantine_dir"),
        spool_stdout=spec.get("spool_stdout", ""),
        spool_stderr=spec.get("spool_stderr", ""),
        timed_out=bool(spec.get("timed_out")),
        rsync_bin=spec.get("rsync_bin", "rsync"),
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write(
            "usage: apps_harvest.py watch <work-tree> <marker> "
            "<started-at-epoch> [deadline-epoch]\n"
            "       apps_harvest.py ensure-repo <git-dir> <work-tree> "
            "[git-root]\n"
            "       apps_harvest.py harvest <spec.json>\n"
            "       apps_harvest.py refuse <spec.json> <reason>\n")
        return 64
    mode, rest = argv[1], argv[2:]
    if mode == "watch":
        return _cli_watch(rest)
    if mode == "ensure-repo":
        return _cli_ensure_repo(rest)
    if mode == "harvest":
        return _cli_harvest(rest)
    if mode == "refuse":
        return _cli_refuse(rest)
    sys.stderr.write(f"apps_harvest.py: unknown mode {mode!r}\n")
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
