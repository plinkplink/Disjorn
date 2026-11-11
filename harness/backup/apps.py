"""The apps part of a snapshot: one git bundle and the served web roots per app, staged and verified."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

APP_ID = re.compile(r"^[a-z2-7]{12}$")
ROOTS = {"live": "live", "live_prev": "live.prev", "preview": "preview"}
LIVE_REF = "refs/disjorn/live"
CHILD_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": "/nonexistent",
             "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
GIT_HARDENING = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
CHILD_TIMEOUT = 120
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Skip(Exception):
    """This repo is not bundled; the message is the reason the manifest records."""


def say(msg: str) -> None:
    print(f"apps: {msg}", file=sys.stderr)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    """This process's environment holds the offsite credentials, so every child gets a built one."""
    return subprocess.run([shutil.which(argv[0]) or argv[0], *argv[1:]], env=CHILD_ENV, text=True,
                          stderr=subprocess.PIPE, **kw)


def git_as_owner(git_dir: Path, *args: str, stdout) -> subprocess.CompletedProcess:
    """The seat can write this repo's config: git reads it as the seat, never as root, and under a deadline."""
    owner = os.stat(git_dir).st_uid
    git = [shutil.which("git") or "git", *GIT_HARDENING, "-C", str(git_dir), *args]
    try:
        clean = ["env", "-i"]
        if os.geteuid() != owner:
            clean = ["runuser", "-u", pwd.getpwuid(owner).pw_name, "--", "env", "-i"]
        proc = run([*clean, *(f"{k}={v}" for k, v in CHILD_ENV.items()),
                    "timeout", "-k", "5", str(CHILD_TIMEOUT), *git],
                   stdout=stdout, cwd="/", timeout=CHILD_TIMEOUT + 30)
    except subprocess.TimeoutExpired as exc:
        raise Skip(f"git timed out after {CHILD_TIMEOUT}s") from exc
    except (OSError, KeyError) as exc:
        raise Skip("cannot run git as the git dir's owner") from exc
    if proc.returncode in (124, 137):
        raise Skip(f"git timed out after {CHILD_TIMEOUT}s")
    return proc


def must(git_dir: Path, proc: subprocess.CompletedProcess) -> str:
    """Git's own words go to the log; the manifest gets a fixed reason."""
    if proc.returncode:
        said = proc.stderr.strip().splitlines()
        say(f"{git_dir.name}: git said: {said[-1] if said else 'nothing'}")
        raise Skip("git could not bundle the repository")
    return proc.stdout


def bundle(git_dir: Path, dest: Path) -> dict:
    """The recorded commits are read back from the bundle, so they describe it even if a turn commits meanwhile."""
    if git_dir.is_symlink() or not os.path.isfile(git_dir / "HEAD"):
        raise Skip("git dir has no readable HEAD")
    refs = must(git_dir, git_as_owner(git_dir, "for-each-ref", "--count=1", "--format=%(refname)",
                                      stdout=subprocess.PIPE))
    if not refs.strip():
        say(f"{git_dir.name} has no commits, nothing to bundle")
        return {}
    with open(dest, "wb") as fh:
        must(git_dir, git_as_owner(git_dir, "bundle", "create", "-q", "-", "--all", stdout=fh))
    heads = must(git_dir, run(["git", "bundle", "list-heads", str(dest)], stdout=subprocess.PIPE))
    by_ref = {ref: sha for sha, ref in (line.split(" ", 1) for line in heads.splitlines())}
    return {"bundle_sha256": sha256(dest), "head": by_ref.get("HEAD"),
            "live_commit": by_ref.get(LIVE_REF)}


def copy_tree(src_fd: int, dst: Path) -> tuple[int, int]:
    """Regular files and directories only, each opened relative to its parent's fd: no symlink is followed."""
    os.mkdir(dst)
    files = size = 0
    for name in os.listdir(src_fd):
        mode = os.stat(name, dir_fd=src_fd, follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            fd = os.open(name, DIR_FLAGS, dir_fd=src_fd)
            try:
                more = copy_tree(fd, dst / name)
            finally:
                os.close(fd)
            files, size = files + more[0], size + more[1]
        elif stat.S_ISREG(mode):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_fd)
            with os.fdopen(fd, "rb") as src, open(dst / name, "wb") as out:
                shutil.copyfileobj(src, out)
                size += out.tell()
            files += 1
    return files, size


def stage_root(app_fd: int, name: str, dst: Path) -> dict | None:
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=app_fd)
    except FileNotFoundError:
        return None
    try:
        files, size = copy_tree(fd, dst)
    finally:
        os.close(fd)
    return {"files": files, "bytes": size}


def stage_app(dirs: dict, dest: Path) -> tuple[dict, list[str]]:
    """Each part fails alone: a repo that will not bundle still gets its web roots staged, and the reverse."""
    dest.mkdir()
    entry = {"bundle_sha256": None, "head": None, "live_commit": None, **dict.fromkeys(ROOTS)}
    reasons = []
    if "git" in dirs:
        try:
            entry.update(bundle(dirs["git"], dest / "repo.bundle"))
        except (Skip, OSError) as exc:
            reasons.append(str(exc) if isinstance(exc, Skip) else f"git dir: {exc.strerror}")
            (dest / "repo.bundle").unlink(missing_ok=True)
    if "www" in dirs:
        try:
            app_fd = os.open(dirs["www"], DIR_FLAGS)
        except OSError as exc:
            return entry, reasons + [f"web roots: {exc.strerror}"]
        try:
            for key, name in ROOTS.items():
                try:
                    entry[key] = stage_root(app_fd, name, dest / name)
                except (OSError, RecursionError) as exc:
                    reasons.append(f"{name}: {getattr(exc, 'strerror', 'nested too deep')}")
                    shutil.rmtree(dest / name, ignore_errors=True)
        finally:
            os.close(app_fd)
    return entry, reasons


def stage(git_root: Path, www_root: Path, out: Path) -> dict:
    out.mkdir(parents=True)
    found: dict[str, dict] = {}
    apps, warnings, lost_roots = {}, {}, {}
    for kind, root, suffix in (("git", git_root, ".git"), ("www", www_root, "")):
        try:
            names = os.listdir(root)
        except OSError as exc:
            say(f"RED {root}: {exc.strerror}; no app is covered from it")
            lost_roots[root.name] = exc.strerror
            continue
        for name in names:
            app = name.removesuffix(suffix)
            if name.endswith(suffix) and APP_ID.match(app):
                found.setdefault(app, {})[kind] = root / name
    for app, dirs in sorted(found.items()):
        entry, reasons = stage_app(dirs, out / app)
        if reasons and not all(os.path.lexists(p) for p in dirs.values()):
            say(f"{app} vanished during the backup (deleted?), skipped")
            shutil.rmtree(out / app, ignore_errors=True)
            continue
        if reasons:
            warnings[app] = "; ".join(reasons)
            say(f"WARNING {app}: {warnings[app]}")
        elif "git" not in dirs:
            say(f"{app} has web roots and no git dir; only the web roots are staged")
        apps[app] = entry
    return {"apps": apps, "warnings": warnings, "lost_roots": lost_roots}


def told(facts: dict | None) -> str:
    return f"{facts['files']} files, {facts['bytes']} bytes" if facts else "nothing"


def count(root: Path) -> dict | None:
    if not root.is_dir() or root.is_symlink():
        return None
    files = size = 0
    for here, _dirs, names in os.walk(root):
        for name in names:
            st = os.lstat(os.path.join(here, name))
            if stat.S_ISREG(st.st_mode):
                files, size = files + 1, size + st.st_size
    return {"files": files, "bytes": size}


def check_bundle(path: Path, want: dict, scratch: Path) -> str | None:
    def git(*args: str) -> subprocess.CompletedProcess:
        return run(["git", *args], stdout=subprocess.PIPE)

    if not path.is_file():
        return "no bundle in the snapshot"
    if git("-C", str(scratch / "verify.git"), "bundle", "verify", "-q", str(path)).returncode:
        return "bundle verify failed"
    if sha256(path) != want["bundle_sha256"]:
        return "bundle sha256 differs from manifest"
    clone = scratch / f"{path.parent.name}.git"
    if git("clone", "-q", "--mirror", str(path), str(clone)).returncode:
        return "clone from the bundle failed"
    for name, ref in (("head", "HEAD"), ("live_commit", LIVE_REF)):
        have = git("-C", str(clone), "rev-parse", "-q", "--verify", ref + "^{commit}").stdout.strip()
        if want[name] and have != want[name]:
            return f"{ref} is {have[:12] or 'absent'} in the clone, manifest says {want[name][:12]}"
    return None


def verify(stage_dir: Path, manifest: dict, scratch: Path) -> tuple[list[str], str]:
    if "apps" not in manifest:
        return ["apps: the manifest has no apps section (snapshot predates apps coverage)"], ""
    scratch.mkdir(parents=True)
    run(["git", "init", "-q", "--bare", str(scratch / "verify.git")], check=True)
    fails, bundles, live = [], 0, 0
    for name, why in sorted(manifest["apps"]["lost_roots"].items()):
        fails.append(f"apps: {name} could not be read at snapshot time ({why}); no app is covered from it")
    for app, want in sorted(manifest["apps"]["apps"].items()):
        if not APP_ID.match(app):
            fails.append("apps: the manifest names something that is not an app id")
            continue
        if want["bundle_sha256"]:
            error = check_bundle(stage_dir / app / "repo.bundle", want, scratch)
            if error:
                fails.append(f"app {app}: {error}")
            else:
                bundles += 1
        for key, name in ROOTS.items():
            if want[key] is None:
                continue
            have = count(stage_dir / app / name)
            if have != want[key]:
                fails.append(f"app {app}: {name} holds {told(have)}, manifest says {told(want[key])}")
            elif key == "live":
                live += 1
    summary = f"apps: {bundles} bundles verified, {live} live roots matched"
    warnings = manifest["apps"]["warnings"]
    if warnings:
        summary += "; APPS COVERAGE PARTIAL at snapshot time: " + ", ".join(
            f"{name} ({why})" for name, why in sorted(warnings.items()))
    return fails, summary


def main(argv: list[str]) -> int:
    global CHILD_TIMEOUT
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage")
    s.add_argument("--git-root", type=Path, required=True)
    s.add_argument("--www-root", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--timeout", type=int, default=CHILD_TIMEOUT)
    v = sub.add_parser("verify")
    v.add_argument("--stage", type=Path, required=True)
    v.add_argument("--manifest", type=Path, required=True)
    v.add_argument("--scratch", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "stage":
        CHILD_TIMEOUT = a.timeout
        facts = stage(a.git_root, a.www_root, a.out)
        (a.out / "apps.json").write_text(json.dumps(facts, sort_keys=True) + "\n")
        return 0
    fails, summary = verify(a.stage, json.loads(a.manifest.read_text()), a.scratch)
    print("\n".join(fails) if fails else summary)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
