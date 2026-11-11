#!/usr/bin/python3
"""apps_harvest: the host side of one apps-builder turn (APPS v1 stage 2, spec §E).

Installed root:root 0755; run-apps.sh calls it from the transient unit as res-appsbuilding. Never root, never podman.

The exit status decides the turn. Nonzero: commit "turn N (halted)", never touch the preview. Zero and clean:
no_changes. Zero and dirty: secret scan first; a hit quarantines the dirty files and commits nothing; otherwise
commit "turn N" and publish. result.json is written atomically, and a harvest that breaks partway is halted = error.

THE WALL (SPECS/2026-09-13-apps-gitdir-outside-the-mount.md): the git dir sits outside every mount. A git dir
the turn can write is host code execution (core.fsmonitor ran here). Every git call names both trees explicitly.

The preview is published by rename, never edited in place: rsync fills a fresh sibling, which then replaces the live root.
The secret scan includes ignored files, a deliberate deviation from the spec: a turn's own .gitignore could hide a key."""

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

# constants

# One author for every app repo: the seat, not a person.
GIT_USER_NAME = "apps-builder"
GIT_USER_EMAIL = "apps-builder@disjorn.local"

# Host-only git root: never mounted. test_apps_launch.py pins it equal to the launcher's GIT_ROOT.
GIT_ROOT = "/srv/apps-git"

# The one place a repository is named.
GIT_DIR_SUFFIX = ".git"

# Shorter keys are not scanned: a short needle matches honest files, so the scan says so instead.
MIN_SCANNABLE_KEY_LEN = 12

# 143 is SIGTERM from RuntimeMaxSec; 124 is GNU timeout. Either means the clock ended the turn.
TIMEOUT_EXIT_CODES = frozenset({124, 137, 143})
# A SIGTERM with the stop marker present is the user's stop; without it, the clock.
STOP_MARKER = "stop-requested"

RESULT_NAME = "result.json"
SCAFFOLDED_NAME = "scaffolded"

# Named here: the ledger and the usage parser key on it.
RUNNER = "claude-code"

# FLAG: is a builder-brief convention, anchored at the start of a line.
FLAG_PREFIX = "FLAG:"

# Summary and flag are bounded to 300 characters.
REPORT_LINE_MAX = 300
REPORT_WORD_BACKOFF = 40


def _now_iso() -> str:
    """UTC timestamp to the second, with an explicit offset."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _warn(msg: str) -> None:
    sys.stderr.write(f"apps-harvest: {msg}\n")
    sys.stderr.flush()


# git

class Repo(NamedTuple):
    """The two trees, always named together. git_dir is the host-only repository; work_tree is the turn's /work."""
    git_dir: Path
    work_tree: Path


def repo_at(git_dir: str | os.PathLike, work_tree: str | os.PathLike) -> Repo:
    """The one constructor for a Repo."""
    return Repo(Path(git_dir), Path(work_tree))


def repo_for(app_id: str, *, work_tree_root: str | os.PathLike,
             git_root: str | os.PathLike = GIT_ROOT) -> Repo:
    """The one place the layout <git-root>/<app-id>.git beside <apps-root>/<app-id> is spelled."""
    return repo_at(Path(git_root) / f"{app_id}{GIT_DIR_SUFFIX}",
                   Path(work_tree_root) / app_id)


def _git_env(repo: Repo, *, identity: bool) -> dict:
    """Environment for every git call. GIT_DIR and GIT_WORK_TREE sit beside the argv flags; GIT_CONFIG_GLOBAL=/dev/null stops seat-written config naming a program."""
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
    """git --git-dir=<G> --work-tree=<W> -c core.hooksPath=/dev/null <args>. core.hooksPath closes hooks only; the config file's location closes the rest."""
    return [git_bin, f"--git-dir={repo.git_dir}",
            f"--work-tree={repo.work_tree}",
            "-c", "core.hooksPath=/dev/null", *args]


def git(repo: Repo, *args: str, check: bool = True,
        git_bin: str = "git") -> subprocess.CompletedProcess:
    """Run git against the split repo with a scrubbed environment. cwd is the work tree, so relative pathspecs resolve there."""
    return subprocess.run(
        _git_argv(repo, args, git_bin),
        check=check, capture_output=True, text=True,
        cwd=str(repo.work_tree), env=_git_env(repo, identity=True),
    )


def git_bytes(repo: Repo, *args: str, git_bin: str = "git") -> bytes:
    """git output as raw bytes. The secret scan must not decode: errors='replace' would mangle a binary diff."""
    proc = subprocess.run(
        _git_argv(repo, args, git_bin),
        capture_output=True, cwd=str(repo.work_tree),
        env=_git_env(repo, identity=False),
    )
    return proc.stdout


class GitDirIncomplete(RuntimeError):
    """A git dir that exists but is not a repository this house made. An incident for the keyboard, never an input."""


def _under(parent: str, child: str) -> bool:
    """True if child is parent or lies beneath it. Compares strings; both must already be real paths."""
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def gitdir_state(git_dir: str | os.PathLike,
                 git_root: str | os.PathLike = GIT_ROOT) -> str:
    """Returns 'absent' or 'repo'. Any other shape raises GitDirIncomplete.
    Host code writes the git dir, so a symlink, a file or a HEAD-less directory is an interrupted init. Never re-init over it."""
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
    """Create the split repo if absent; True if created. Runs before the container starts.
    End state: HEAD present, core.worktree unset, no .git entry in the work tree. --separate-git-dir is not used: it writes a .git pointer into the work tree."""
    repo.work_tree.mkdir(parents=True, exist_ok=True)
    if gitdir_state(repo.git_dir, git_root) == "repo":
        return False
    repo.git_dir.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main", git_bin=git_bin)
    # config --unset exits 5 when the key is absent; only the end state matters.
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
    """git status --porcelain=v1 -z -uall as [(xy, path)]. -z handles odd filenames; -uall lists files, not directories."""
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
        # Rename and copy entries are followed by their origin path: drop it.
        if xy and xy[0] in ("R", "C"):
            i += 1
        entries.append((xy, path))
        i += 1
    return entries


def dirty_paths(repo: Repo, *, ignored: bool = False,
                git_bin: str = "git") -> list[str]:
    """Repo-relative file paths that differ from HEAD. Deletions are excluded. Directories are walked: --ignored reports an ignored directory as one entry."""
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
    """The no_changes question. Deliberately without ignored files; the scan uses the wider set."""
    return not porcelain(repo, ignored=False, git_bin=git_bin)


def commit_all(repo: Repo, message: str,
               git_bin: str = "git") -> str | None:
    """git add -A and commit. Returns the sha, or None when there was nothing to commit."""
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
    """Paths a commit touched, for detail.files."""
    out = git(repo, "show", "--name-only", "--pretty=format:", sha,
              git_bin=git_bin).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


# secret scan

def secret_patterns(key: str | bytes | None) -> list[tuple[str, bytes]]:
    """Raw, standard-base64 and lowercase-hex needles for the key. A key under MIN_SCANNABLE_KEY_LEN yields none, and says so."""
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
    """The credential value from the seat's env file, read only for the scan. Matches podman: everything after the first '=' is literal, the last assignment wins."""
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
    """Search the turn's output for any needle: git diff, staged diff, and every dirty, untracked or ignored path. Returns {encoding, where} on the first hit."""
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
    """Move the named paths out of the work tree, keeping relative paths. The root is 0700 and never mounted, since the next turn reads /work first. Symlinks are renamed, never followed."""
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


# stray .git

# Stray .git entries get their own subdirectory.
STRAY_GIT_DEST = "stray-git"
STRAY_GIT_NAME = ".git"


def find_stray_git(work_tree: str | os.PathLike) -> list[str]:
    """Every entry named .git under the work tree. Status, add, ls-files and clean all walk past one. Lstat only: never follow, never walk a .git."""
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
    """Move every stray .git to quarantine. A class, not a case: no .git here has a legitimate author. The turn is not halted; the record names the paths."""
    paths = find_stray_git(work_tree)
    if not paths:
        return []
    moved = quarantine(work_tree, paths, Path(quarantine_dir) / STRAY_GIT_DEST)
    if moved:
        _warn(f"{len(moved)} stray .git entr(ies) moved out of the work tree: "
              + ", ".join(moved))
    return moved


def reset_tree(repo: Repo, git_bin: str = "git") -> None:
    """Back to HEAD. checkout needs a HEAD, so a first turn with none is cleaned with clean alone. -x removes ignored leftovers too."""
    if has_commits(repo, git_bin=git_bin):
        git(repo, "reset", "-q", git_bin=git_bin)       # unstage anything add'ed
        git(repo, "checkout", "--", ".", check=False, git_bin=git_bin)
    # -x: an ignored leftover would invite the next turn's read-first walk.
    git(repo, "clean", "-fdxq", check=False, git_bin=git_bin)


# preview

def preview_argv(work_tree: str | os.PathLike, dest: str | os.PathLike,
                 rsync_bin: str = "rsync") -> list[str]:
    """The exact rsync argv, checkable without rsync. dest is the fresh sibling, never the live root.
    --no-links drops symlinks as a class. --no-D drops devices. --chmod sets modes. --exclude .git and /.* keep any .git and root dotfiles (.env is where a model writes a key). No --delete: a live root must not be half-updated."""
    return [rsync_bin, "-a", "--no-links", "--no-D", "--chmod=D0755,F0644",
            "--exclude", ".git", "--exclude", "/.*",
            f"{str(work_tree).rstrip('/')}/", f"{str(dest).rstrip('/')}/"]


def _remove(path: Path) -> None:
    """Remove a leftover of ours, whatever its shape. A symlink is unlinked, never followed."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


STAGING_NAME = ".staging"


def staging_root(preview_dir: str | os.PathLike) -> Path:
    """<www-root>/.staging/<app-id>/, where the sibling and the superseded root live during a publish. Not beside preview/, which would be served."""
    preview_dir = Path(preview_dir)
    app_dir = preview_dir.parent
    return app_dir.parent / STAGING_NAME / app_dir.name


def preview_sibling(preview_dir: str | os.PathLike, turn: int) -> Path:
    """preview.tmp.<turn> under the staging root. Created, filled and renamed by this process alone."""
    preview_dir = Path(preview_dir)
    return staging_root(preview_dir) / f"{preview_dir.name}.tmp.{int(turn)}"


def preview_old(preview_dir: str | os.PathLike, turn: int) -> Path:
    """Where the superseded live root goes between the two renames, and until it is removed."""
    preview_dir = Path(preview_dir)
    return staging_root(preview_dir) / f"{preview_dir.name}.old.{int(turn)}"


def copy_preview(work_tree: str | os.PathLike, preview_dir: str | os.PathLike,
                 turn: int, rsync_bin: str = "rsync") -> None:
    """Publish by rename: rsync fills a fresh sibling; the live root moves aside, the sibling takes its name, and the old root is removed.
    If the second rename fails the old root is restored. A failed rsync removes the sibling and raises; the live root is untouched."""
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


# watcher

def watch_for_scaffolded(work_tree: str | os.PathLike,
                         marker_path: str | os.PathLike,
                         started_at: float, *, deadline: float | None = None,
                         interval: float = 0.5,
                         _clock=time.time) -> str | None:
    """Write the scaffolded marker on the first change under the work tree. Runs inside the unit; the broker reads the marker. A poll, not inotify."""
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
    """True if any entry under the work tree changed at or after started_at. The root's own mtime does not count; .git does."""
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


# result

def write_result_atomic(result_dir: str | os.PathLike, payload: dict) -> str:
    """Write result.json whole or not at all: tmp file, then rename within the same directory. Mode 0644: the broker reads it and does not own it."""
    result_dir = Path(result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    final = result_dir / RESULT_NAME
    tmp = result_dir / (RESULT_NAME + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o644)                # the broker reads it and does not own it
    os.replace(tmp, final)
    return str(final)


# spools

def redact_text(blob, patterns: list[tuple[str, bytes]]):
    """Replace every encoding of the key with a labelled mark, in bytes or str. Returns the type it was given."""
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
    """Redact the key from the runner's raw stdout and stderr, in place, before the result line is parsed. Returns True if anything changed."""
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
    """The final stream-json result line, as (usage, closing text). Both come from one object, so they cannot disagree. Absent or unparsable gives (None, None)."""
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
    """Runner text made safe for plain-text readers: control characters out, the key redacted, then the 300-character bound."""
    clean = "".join(c for c in text if c >= " " and c != "\x7f").strip()
    line = redact_text(clean, patterns)
    if len(line) <= REPORT_LINE_MAX:
        return line
    head = line[:REPORT_LINE_MAX]
    space = head.rfind(" ", REPORT_LINE_MAX - 1 - REPORT_WORD_BACKOFF)
    head = head[:space].rstrip() if space > 0 else head[:REPORT_LINE_MAX - 1]
    return head + "\u2026"


def runner_report(text: str | None,
                  patterns: list[tuple[str, bytes]]) -> tuple[str | None, str | None]:
    """(summary, flag) from the closing text. flag is the first FLAG: line, prefix removed. summary is the last non-empty line that is not a FLAG line."""
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
        # Non-empty is judged after cleaning.
        summary = _one_line(ln, patterns) or None
        if summary is not None:
            break
    return summary, flag


# harvest

def _skeleton(exit_code: int, *, session: int, turn: int, app_id: str,
              started_at: str, ended_at: str | None, model: str,
              spool_stdout: str, spool_stderr: str) -> dict:
    """Every key result.json has, with its nothing-happened value. One builder for both writers."""
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
        # Always present: no strays and predates-the-sweep must differ.
        "stray_git": [],
        "error": None,
        "started_at": started_at,
        "ended_at": ended_at or _now_iso(),
        "model": model,
        "runner": RUNNER,
        "spool": {"stdout": spool_stdout, "stderr": spool_stderr},
        # Present here too, so error and success records match.
        "spool_redacted": False,
        "usage": None,
        # Always present, null when the runner said nothing.
        "summary": None,
        "flag": None,
    }


def refuse(result_dir: str | os.PathLike, reason: str, *, session: int,
           turn: int, app_id: str, started_at: str, model: str = "",
           spool_stdout: str = "", spool_stderr: str = "",
           exit_code: int = 1) -> dict:
    """The record for a turn refused before the container started. Nothing is committed, scanned or published; halted = error, with the reason."""
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
    """The section E branch order, then result.json. A failed harvest is a result too: the error is recorded with any commit made. The preview is untouched on error."""
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
        # The error goes through the same needle loop as the spools.
        text = redact_text(f"{type(exc).__name__}: {exc}", patterns)
        payload["halted"] = payload["halted"] or "error"
        payload["error"] = text
        _warn(f"harvest FAILED partway — recording a halted turn: {text}")
        return _finish(result_dir, payload)


def _branches(repo, payload, halted_reason, patterns, result_dir, key_file,
              preview_dir, turn, quarantine_dir, spool_stdout, spool_stderr,
              rsync_bin, git_bin) -> dict:
    """The branch order proper. Anything here may raise; harvest() turns a raise into a record."""
    if key_file and not patterns:
        _warn("no scannable credential — the turn's output is being published "
              "WITHOUT a secret scan; check the drop file")

    # Spools are redacted in place before anything else reads them.
    payload["spool_redacted"] = redact_spools(
        [spool_stdout, spool_stderr], patterns)
    # Parsed here after redaction: the broker never opens a spool.
    payload["usage"], report_text = parse_result_claude_code(spool_stdout)
    payload["summary"], payload["flag"] = runner_report(report_text, patterns)

    # Strays go before the scan: a .git entry hides from every porcelain.
    quar = Path(quarantine_dir) if quarantine_dir else Path(result_dir) / "quarantine"
    payload["stray_git"] = sweep_stray_git(repo.work_tree, quar)

    # The scan includes ignored files, so a turn's .gitignore cannot hide a key.
    scan_paths = dirty_paths(repo, ignored=True, git_bin=git_bin)
    found = scan_for_secret(repo, patterns, paths=scan_paths, git_bin=git_bin) \
        if scan_paths else None
    if found:
        # The headline is set on the hit; a later failure adds an error, never a downgrade.
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
            # Halted with nothing to keep: the preview is untouched.
            payload["halted"] = halted_reason
            return _finish(result_dir, payload)
        # no_changes is terminal: it must end the bar's wait.
        payload["no_changes"] = True
        return _finish(result_dir, payload)

    if halted_reason:
        # Commit the halted work so the next turn inherits it. Never touch the preview.
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


# CLI

def _cli_watch(argv: list[str]) -> int:
    repo, marker, started_at = argv[0], argv[1], float(argv[2])
    deadline = float(argv[3]) if len(argv) > 3 and argv[3] else None
    stamp = watch_for_scaffolded(repo, marker, started_at, deadline=deadline)
    if stamp:
        print(stamp)
        return 0
    return 1


def _cli_ensure_repo(argv: list[str]) -> int:
    """ensure-repo <git-dir> <work-tree> [git-root]. Exit 2 on GitDirIncomplete, with the reason on stderr."""
    git_root = argv[2] if len(argv) > 2 else GIT_ROOT
    try:
        created = ensure_repo(repo_at(argv[0], argv[1]), git_root)
    except GitDirIncomplete as exc:
        _warn(str(exc))
        return 2
    print("created" if created else "present")
    return 0


def _cli_refuse(argv: list[str]) -> int:
    """refuse <spec.json> <reason>. Takes the same spec as harvest, so the fields are written once."""
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
    """harvest <spec.json>. One JSON object, because the harvest takes fifteen arguments."""
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    try:
        payload = _harvest_from_spec(spec)
    except Exception as exc:          # noqa: BLE001 — last resort, loud
        # Only the write of result.json itself reaches here. A unit that ends without it is a halt.
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
