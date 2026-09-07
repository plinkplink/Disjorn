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
                 halted = "timeout" (RuntimeMaxSec fired) or "error".
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
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ------------------------------------------------------------------ constants

# The commit identity of the seat. Not a person and not a resident: every app
# repo's history is written by the same hand, and `git log` should say so.
GIT_USER_NAME = "apps-builder"
GIT_USER_EMAIL = "apps-builder@disjorn.local"

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

RESULT_NAME = "result.json"
SCAFFOLDED_NAME = "scaffolded"

# The turn's own runner. Named here (rather than derived) because the ledger
# and the stage detail carry it and slice (ii) keys its usage parser off it.
RUNNER = "claude-code"


def _now_iso() -> str:
    """UTC, second resolution, with an explicit offset. Timestamps in
    result.json are read by the broker and by a human at 3am; both want to know
    the zone without guessing."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _warn(msg: str) -> None:
    sys.stderr.write(f"apps-harvest: {msg}\n")
    sys.stderr.flush()


# ----------------------------------------------------------------------- git

def git(repo: str | os.PathLike, *args: str, check: bool = True,
        git_bin: str = "git") -> subprocess.CompletedProcess:
    """Run git in `repo` with a scrubbed environment.

    -c core.hooksPath=/dev/null: the repo under /work is written BY the turn,
    so it could contain hooks. A commit is not the place to run code the turn
    authored. The rest of the env scrub is the same reflex: nothing this
    process inherited decides who the committer is.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": GIT_USER_NAME,
        "GIT_AUTHOR_EMAIL": GIT_USER_EMAIL,
        "GIT_COMMITTER_NAME": GIT_USER_NAME,
        "GIT_COMMITTER_EMAIL": GIT_USER_EMAIL,
        "LC_ALL": "C",
    }
    return subprocess.run(
        [git_bin, "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        check=check, capture_output=True, text=True, env=env,
    )


def git_bytes(repo: str | os.PathLike, *args: str,
              git_bin: str = "git") -> bytes:
    """git output as raw BYTES. The secret scan must not go through a decoder:
    a diff of a binary file is not valid UTF-8, and a `errors="replace"` decode
    would mangle exactly the bytes we are looking for."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    proc = subprocess.run(
        [git_bin, "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        capture_output=True, env=env,
    )
    return proc.stdout


def ensure_repo(repo: str | os.PathLike, git_bin: str = "git") -> bool:
    """`git init` the app repo if it is not one yet. Returns True if it created.

    Called from run-apps.sh BEFORE the container starts, as res-appsbuilding —
    never as root and never by the broker, so §E's "the broker never writes
    under /srv/apps" is true from the first turn (spec §C, Gable #2295).
    """
    repo = Path(repo)
    repo.mkdir(parents=True, exist_ok=True)
    if (repo / ".git").exists():
        return False
    git(repo, "init", "-q", "-b", "main", git_bin=git_bin)
    git(repo, "config", "user.name", GIT_USER_NAME, git_bin=git_bin)
    git(repo, "config", "user.email", GIT_USER_EMAIL, git_bin=git_bin)
    return True


def has_commits(repo: str | os.PathLike, git_bin: str = "git") -> bool:
    return git(repo, "rev-parse", "--verify", "-q", "HEAD",
               check=False, git_bin=git_bin).returncode == 0


def porcelain(repo: str | os.PathLike, *, ignored: bool = False,
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


def dirty_paths(repo: str | os.PathLike, *, ignored: bool = False,
                git_bin: str = "git") -> list[str]:
    """Repo-relative FILE paths that exist on disk and are not what HEAD says.

    Deletions are excluded: there is no file to scan and none to quarantine.

    Directory entries are EXPANDED. `--ignored=matching` reports a directory
    when the ignore pattern named a directory (`secrets/` in .gitignore comes
    back as the single entry `secrets/`), and a scan that skipped it because it
    is not a regular file would be a hole exactly where the hardening was
    supposed to be. So every directory entry is walked and its files listed.

    The list is what the secret scan reads and what quarantine moves.
    """
    repo = Path(repo)
    paths: list[str] = []
    for xy, path in porcelain(repo, ignored=ignored, git_bin=git_bin):
        if not (xy == "!!" or xy.strip()):
            continue
        rel = path.rstrip("/")
        full = repo / rel
        if full.is_symlink() or full.is_file():
            paths.append(rel)
        elif full.is_dir():
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for name in filenames:
                    paths.append(os.path.relpath(
                        os.path.join(dirpath, name), repo))
    return sorted(set(paths))


def is_clean(repo: str | os.PathLike, git_bin: str = "git") -> bool:
    """The `no_changes` question, and deliberately WITHOUT ignored files: a
    turn that wrote only files its own .gitignore hides has still changed
    nothing this house will ever publish. (The SCAN uses the wider set.)"""
    return not porcelain(repo, ignored=False, git_bin=git_bin)


def commit_all(repo: str | os.PathLike, message: str,
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


def commit_files(repo: str | os.PathLike, sha: str,
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


def scan_for_secret(repo: str | os.PathLike,
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
        full = Path(repo) / rel
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


def quarantine(repo: str | os.PathLike, paths: list[str],
               dest: str | os.PathLike) -> list[str]:
    """Move every named path OUT of the repo into `dest`, relative paths kept.

    The quarantine root is 0700 and is never mounted into any seat: the brief
    tells the next turn to read /work first, so leaving a poisoned file in
    place would feed the injection straight back in (Claudette #2293).
    Returns the paths actually moved.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    moved = []
    for rel in paths:
        src = Path(repo) / rel
        if not (src.exists() or src.is_symlink()):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        moved.append(rel)
    return moved


def reset_tree(repo: str | os.PathLike, git_bin: str = "git") -> None:
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

def preview_argv(repo: str | os.PathLike, dest: str | os.PathLike,
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
    `--exclude .git`  unanchored, so it drops the repo's history AND any nested
                      .git a vendored copy dragged in.
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
            f"{str(repo).rstrip('/')}/", f"{str(dest).rstrip('/')}/"]


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


def copy_preview(repo: str | os.PathLike, preview_dir: str | os.PathLike,
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

    argv = preview_argv(repo, sibling, rsync_bin=rsync_bin)
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

def watch_for_scaffolded(repo: str | os.PathLike, marker_path: str | os.PathLike,
                         started_at: float, *, deadline: float | None = None,
                         interval: float = 0.5,
                         _clock=time.time) -> str | None:
    """Write the `scaffolded` marker on the first change under /work.

    Runs INSIDE the unit as a background process, because the broker runs as
    plink and must never watch (or own) anything under /srv/apps (Gable #2295,
    Claudette #2299). The broker reads the marker; it does not hold the watch.

    "First change" is §A's definition and not the obvious one: the repo is
    initialised BEFORE the container starts and turn 2 begins with a full tree,
    so "a path exists" would fire at t=0 every turn (Claudette #2284). What
    counts is a path OUTSIDE `.git/` whose mtime or ctime is at or after
    `started_at`.

    Returns the ISO timestamp written, or None if the deadline passed first.
    A poll, not inotify: 0.5s is invisible next to a model turn, and it needs
    no package in the image and no fd budget on a tree the turn is rewriting.
    """
    repo = Path(repo)
    marker_path = Path(marker_path)
    while True:
        if _changed_since(repo, started_at):
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


def _changed_since(repo: Path, started_at: float) -> bool:
    """Any ENTRY under the repo, outside `.git/`, created or modified at or
    after `started_at`. The repo root's own mtime does NOT count: `git init`
    creates `.git/` in it moments before the watch starts and would fire the
    marker at t=0 on turn 1 (measured at the keyboard's proving turn). A file
    created directly in the root registers through its own timestamps, so
    nothing is lost by ignoring the directory's."""
    root = Path(repo)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
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

def redact_spools(paths: list[str], patterns: list[tuple[str, bytes]]) -> bool:
    """Replace every encoding of the key in the spool files, in place.

    Returns True if anything was redacted. Spools are the runner's raw
    stdout/stderr; an authentication failure is the classic place a client
    library echoes a credential. Redacting keeps a later reader (a human at
    3am, the v2 scrollback) from meeting the value.
    """
    redacted = False
    for p in paths:
        if not p or not patterns:
            continue
        try:
            blob = Path(p).read_bytes()
        except OSError:
            continue
        out = blob
        for label, needle in patterns:
            if needle and needle in out:
                out = out.replace(needle, b"[REDACTED:" + label.encode() + b"]")
        if out != blob:
            tmp = Path(p + ".tmp")
            tmp.write_bytes(out)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
            redacted = True
    return redacted


def parse_usage_claude_code(spool_stdout: str) -> dict | None:
    """Usage from Claude Code's stream-json: the final `result` line carries
    `usage` (input/output/cache_creation/cache_read) and `total_cost_usd`.
    The ONE runner-specific parser the spec allows (§A); a different runner
    means a sibling function keyed by [apps].runner, not a change here.
    Absent, unreadable or unparsable → None, never a guess.
    """
    if not spool_stdout:
        return None
    try:
        lines = Path(spool_stdout).read_bytes().splitlines()
    except OSError:
        return None
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
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "total_cost_usd": obj.get("total_cost_usd"),
            "num_turns": obj.get("num_turns"),
            "duration_ms": obj.get("duration_ms"),
            "is_error": obj.get("is_error"),
        }
    return None


# ------------------------------------------------------------------- harvest

def harvest(repo: str | os.PathLike, exit_code: int,
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
    ended_at = ended_at or _now_iso()
    payload = {
        "session": int(session),
        "turn": int(turn),
        "app_id": app_id,
        "exit": int(exit_code),
        "halted": None,
        "no_changes": False,
        "files": [],
        "commit": None,
        "quarantine": None,
        "error": None,
        "started_at": started_at,
        "ended_at": ended_at,
        "model": model,
        "runner": RUNNER,
        "spool": {"stdout": spool_stdout, "stderr": spool_stderr},
        # Set again inside _branches; present HERE so an error record has the
        # same shape as a success record (Claudette #2336).
        "spool_redacted": False,
        "usage": None,
    }

    halted_reason = None
    if exit_code != 0:
        halted_reason = ("timeout"
                         if timed_out or exit_code in TIMEOUT_EXIT_CODES
                         else "error")

    patterns = secret_patterns(read_key(key_file)) if key_file else []
    try:
        return _branches(repo, payload, halted_reason, patterns, result_dir,
                         key_file, preview_dir, turn, quarantine_dir,
                         spool_stdout, spool_stderr, rsync_bin, git_bin)
    except Exception as exc:          # noqa: BLE001 — the record is the point
        text = f"{type(exc).__name__}: {exc}"
        for label, needle in patterns:  # an error string is still a string
            if needle:
                text = text.replace(needle.decode("latin-1"),
                                    f"[REDACTED:{label}]")
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
    # Usage lives in result.json, parsed HERE as the seat, so the spools can
    # stay 0600 seat-only and the broker (plink) never needs to open them.
    payload["usage"] = parse_usage_claude_code(spool_stdout)

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
        dest = Path(quarantine_dir) if quarantine_dir else Path(result_dir) / "quarantine"
        payload["halted"] = "secret"
        payload["quarantine"] = str(dest)
        payload["files"] = []
        moved = quarantine(repo, scan_paths, dest)
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
    copy_preview(repo, preview_dir, int(turn), rsync_bin=rsync_bin)
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
    created = ensure_repo(argv[0])
    print("created" if created else "present")
    return 0


def _cli_harvest(argv: list[str]) -> int:
    """`harvest <json-file>` — every argument in one JSON object, because there
    are thirteen of them and a positional bash call with thirteen slots is a
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
        spec["repo"], int(spec["exit_code"]), spec["result_dir"],
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
            "usage: apps_harvest.py watch <repo> <marker> <started-at-epoch> "
            "[deadline-epoch]\n"
            "       apps_harvest.py ensure-repo <repo>\n"
            "       apps_harvest.py harvest <spec.json>\n")
        return 64
    mode, rest = argv[1], argv[2:]
    if mode == "watch":
        return _cli_watch(rest)
    if mode == "ensure-repo":
        return _cli_ensure_repo(rest)
    if mode == "harvest":
        return _cli_harvest(rest)
    sys.stderr.write(f"apps_harvest.py: unknown mode {mode!r}\n")
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
