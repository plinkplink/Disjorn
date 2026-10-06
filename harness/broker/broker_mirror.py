"""The gatehouse fetch, the mirror refresh and spec Status stamping."""

from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import tempfile
from typing import Optional

from broker_common import (
    SUBPROCESS_TIMEOUTS, VerbError, _reject_unknown, parse_spec_status,
)


def replace_spec_status(text: str, new_status: str, comment: str) -> Optional[str]:
    """Rewrite the `## Status` token in a spec to `new_status`, followed by ONE HTML
    comment line saying who moved it and why."""
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.strip().lower() != "## status":
            continue
        for j in range(i + 1, len(lines)):
            st = lines[j].strip()
            if not st or st.startswith("<!--"):
                continue
            if st.startswith("#"):
                return None
            lines[j] = f"{new_status}\n<!-- {comment} -->\n"
            return "".join(lines)
        return None
    return None


class MirrorVerbs:

    # ------------------------------------------------- the gatehouse fetch
    # SPECS/2026-08-14-file-vision.md item 1. `refresh-mirror` used to move `main`
    # and nothing else, so the mirror could tell a resident what production runs and
    # could not show them a single branch anyone was being asked to review. Every
    # branch now lands under refs/gatehouse/<repo>/*.
    _GATEHOUSE_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    def _gatehouse_fetch_argvs(self) -> list[tuple[str, list[str]]]:
        """One fixed argv per entitled gatehouse repo."""
        base_dir = self.commands.get("refresh_mirror_gatehouse_dir")
        repos = self.commands.get("refresh_mirror_gatehouse_repos")
        if not base_dir or not repos:
            return []                     # not configured = today's behaviour
        if not isinstance(base_dir, str) or not isinstance(repos, list):
            raise VerbError("internal",
                            "commands.refresh_mirror_gatehouse_dir must be a "
                            "string and _repos a list of strings")
        fetch = self._argv("refresh_mirror_gatehouse_fetch",
                           ["git", "-C", "/srv/disjorn-ro", "fetch", "--prune"])
        out: list[tuple[str, list[str]]] = []
        for repo in repos:
            if not isinstance(repo, str) or not self._GATEHOUSE_REPO_RE.match(repo):
                raise VerbError("internal",
                                f"commands.refresh_mirror_gatehouse_repos holds "
                                f"{repo!r}, which is not a plain repo name")
            out.append((repo, [*fetch, f"{base_dir}/{repo}.git",
                               f"+refs/heads/*:refs/gatehouse/{repo}/*"]))
        return out

    def _gatehouse_count_argv(self, repo: str) -> list[str]:
        """Fixed argv listing the refs the mirror HOLDS for `repo`."""
        if not isinstance(repo, str) or not self._GATEHOUSE_REPO_RE.match(repo):
            raise VerbError("internal",
                            f"gatehouse repo {repo!r} is not a plain repo name")
        base = self._argv("refresh_mirror_gatehouse_count",
                          ["git", "-C", "/srv/disjorn-ro", "for-each-ref",
                           "--format=%(refname)"])
        return [*base, f"refs/gatehouse/{repo}/"]

    def _gatehouse_present(self, repo: str, timeout: int) -> Optional[int]:
        """How many refs the mirror holds for `repo` — an INVENTORY, next to
        `arrived`'s DELTA."""
        try:
            cp = self._run(self._gatehouse_count_argv(repo), timeout)
        except VerbError:
            return None
        if cp.returncode != 0:
            return None
        return sum(1 for line in cp.stdout.splitlines() if line.strip())

    @staticmethod
    def _parse_fetch_refs(output: str) -> tuple[list[str], list[str]]:
        """(arrived, vanished) ref names out of `git fetch --prune` chatter."""
        arrived, vanished = [], []
        for line in output.splitlines():
            ref = line.strip().rsplit(" ", 1)[-1].strip()
            if not ref:
                continue
            if "[deleted]" in line:
                vanished.append(ref)
            elif "[new branch]" in line or "[new ref]" in line:
                arrived.append(ref)
        return arrived, vanished

    def _fetch_gatehouse_into_mirror(self, timeout: int) -> list[dict]:
        """Run every gatehouse fetch; return one record per repo."""
        records = []
        for repo, argv in self._gatehouse_fetch_argvs():
            cp = self._run(argv, timeout)
            if cp.returncode != 0:
                raise VerbError(
                    "exec-failure",
                    f"gatehouse fetch for {repo} exit {cp.returncode}: "
                    f"{(cp.stderr or cp.stdout).strip()[:500]}")
            arrived, vanished = self._parse_fetch_refs(cp.stderr + cp.stdout)
            records.append({"repo": repo, "arrived": arrived,
                            "vanished": vanished,
                            "present": self._gatehouse_present(repo, timeout)})
        return records

    def _verb_refresh_mirror(self, resident: str, args: dict) -> tuple[dict, str]:
        """Fast-forward the shared read-only repo mirror to the canonical repo's
        main, THEN re-fetch every entitled gatehouse repo's branches into
        refs/gatehouse/<repo>/*."""
        _reject_unknown(args, set())
        timeout = SUBPROCESS_TIMEOUTS["refresh-mirror"]
        head_argv = self._argv("refresh_mirror_head", [
            "git", "-C", "/srv/disjorn-ro", "rev-parse", "--short", "HEAD"])

        def _head() -> str:
            cp = self._run(head_argv, timeout)
            if cp.returncode != 0:
                raise VerbError("exec-failure",
                                f"rev-parse exit {cp.returncode}: "
                                f"{cp.stderr.strip()[:300]}")
            return cp.stdout.strip()

        before = _head()
        self._ff_mirror_main(timeout)
        gatehouse = self._fetch_gatehouse_into_mirror(timeout)
        # The mirror has just moved, so every card derived from it may have moved
        # with it.
        planroom = ({"rebuilt": False, "reason": "disabled by config"}
                    if not self.planroom.get("rebuild_on_refresh", True)
                    else self._planroom_rebuild("refresh-mirror"))
        head = _head()
        summary = f"mirror at {head}" + ("" if head == before
                                         else f" (was {before})")
        # No news stays no line.
        empty = ", ".join(rec["repo"] for rec in gatehouse
                          if rec["present"] == 0)
        if empty:
            summary = f"{summary}; gatehouse EMPTY for {empty}"
        moved = "; ".join(
            f"{rec['repo']}: +{len(rec['arrived'])} new, "
            f"-{len(rec['vanished'])} harvested or deleted"
            for rec in gatehouse if rec["arrived"] or rec["vanished"])
        if moved:
            summary = f"{summary}; gatehouse {moved}"
        if planroom.get("transitions"):
            summary = f"{summary}; plan room {planroom['transitions']} move(s)"
        elif planroom.get("rebuilt") is False and planroom.get("reason") \
                not in ("no [planroom].index configured", "disabled by config"):
            summary = f"{summary}; PLAN ROOM REBUILD FAILED: {planroom['reason']}"
        return ({"head": head, "before": before, "updated": head != before,
                 "gatehouse": gatehouse, "planroom": planroom}, summary[:300])

    def _ff_mirror_main(self, timeout: int) -> None:
        """Fetch origin into the read-only mirror and fast-forward it to origin/main
        — the two fixed argvs `refresh-mirror` has always run, factored so the
        spec-status stamp can use the SAME refresh (never a second implementation of
        "the mirror is fresh")."""
        for key, default in (
            ("refresh_mirror_fetch",
             ["git", "-C", "/srv/disjorn-ro", "fetch", "origin"]),
            ("refresh_mirror_update",
             ["git", "-C", "/srv/disjorn-ro", "merge", "--ff-only", "origin/main"]),
        ):
            cp = self._run(self._argv(key, default), timeout)
            if cp.returncode != 0:
                raise VerbError("exec-failure",
                                f"{key} exit {cp.returncode}: "
                                f"{(cp.stderr or cp.stdout).strip()[:500]}")

    # ------------------------------------------------- spec Status stamping

    def _spec_repo(self) -> Optional[tuple[str, str, str]]:
        """(repo path, branch, SPECS subdir) of the CANONICAL repo whose SPECS/ the
        mirror follows, from `[start_build].spec_repo` (+ `spec_repo_branch`,
        default main; `spec_repo_subdir`, default SPECS)."""
        repo = self.start_build.get("spec_repo")
        if not isinstance(repo, str) or not repo:
            return None
        branch = self.start_build.get("spec_repo_branch", "main")
        subdir = self.start_build.get("spec_repo_subdir", "SPECS")
        if (not isinstance(branch, str) or not branch
                or not isinstance(subdir, str) or not subdir):
            return None
        return repo, branch, subdir.strip("/")

    def _git(self, repo: str, *args: str, stdin: Optional[str] = None,
             env: Optional[dict] = None) -> subprocess.CompletedProcess:
        """One git command against the canonical repo, fixed argv, no shell."""
        argv = [*self._argv("spec_repo_git", ["git"]), "-C", repo, *args]
        full_env = None
        if env:
            full_env = dict(os.environ)
            full_env.update(env)
        try:
            return subprocess.run(  # noqa: S603 — argv list, no shell
                argv, capture_output=True, text=True, input=stdin,
                timeout=SUBPROCESS_TIMEOUTS["spec-status"], env=full_env)
        except subprocess.TimeoutExpired:
            raise VerbError("exec-failure", "git timed out") from None
        except OSError as exc:
            raise VerbError("exec-failure", f"git failed to start: {exc}") from None

    def _git_ok(self, repo: str, *args: str, **kw) -> str:
        cp = self._git(repo, *args, **kw)
        if cp.returncode != 0:
            raise VerbError("exec-failure",
                            f"git {args[0]} exit {cp.returncode}: "
                            f"{(cp.stderr or cp.stdout).strip()[:300]}")
        return cp.stdout

    # -- the local coverage record ------------------------------------------
    # THE PUSH LOG'S SIBLING (spec, confirmed). A stamp commit is made with git
    # plumbing straight onto the canonical repo's branch: no push, so it never meets
    # the pre-receive hook, so it can never have a push-log line.

    LOCAL_LOG_NAME = "disjorn-local-log"
    LOCAL_STAMP = "local-stamp"

    def _local_coverage_log(self) -> Optional[str]:
        """Where the record goes: beside the push log, `[gate].local_log`,
        defaulting to <[gate].canonical_repo>/hooks/disjorn-local-log."""
        gate = self.config.get("gate")
        if not isinstance(gate, dict):
            return None
        path = gate.get("local_log")
        if isinstance(path, str) and path:
            return path
        canonical = gate.get("canonical_repo")
        if isinstance(canonical, str) and canonical:
            return os.path.join(canonical, "hooks", self.LOCAL_LOG_NAME)
        return None

    def _record_local_commit(self, sha: str,
                             outcome: str = LOCAL_STAMP) -> str:
        """Append `LOCAL <ts> <sha> <outcome>`."""
        path = self._local_coverage_log()
        if not path:
            return ("no [gate].local_log or [gate].canonical_repo is "
                    f"configured, so no coverage record names {sha[:7]}; the "
                    "digest will have to guess what put it on the branch")
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, f"LOCAL {ts} {sha} {outcome}\n".encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            return (f"coverage record NOT written to {path}: {exc}; the next "
                    f"digest will report {sha[:7]} as unexplained")
        return ""

    def _stamp_spec_status(self, slug: str, new_status: str, comment: str, *,
                           expect: tuple[str, ...]) -> dict:
        """Move a spec's `## Status` line in the CANONICAL repo and commit it, then
        fast-forward the read-only mirror so residents (and this broker's own
        confirm gate) read the new word at once."""
        cfg = self._spec_repo()
        if cfg is None:
            return {"ok": False, "status": new_status, "commit": None,
                    "why": "start_build.spec_repo is not configured, so the "
                           "broker cannot move Status lines"}
        repo, branch, subdir = cfg
        relpath = f"{subdir}/{slug}.md"
        ref = f"refs/heads/{branch}"
        try:
            old_sha = self._git_ok(repo, "rev-parse", "--verify", "--quiet",
                                   ref).strip()
            text = self._git_ok(repo, "show", f"{old_sha}:{relpath}")
            have = parse_spec_status(text)
            if have not in expect:
                return {"ok": False, "status": new_status, "commit": None,
                        "why": f"{relpath} on {branch} says {have!r}, expected "
                               f"one of {sorted(expect)} — left as is"}
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
            new_text = replace_spec_status(
                text, new_status,
                f"set by the broker on {stamp} (start-build, {slug}): {comment}")
            if new_text is None:
                return {"ok": False, "status": new_status, "commit": None,
                        "why": f"{relpath} has no parseable ## Status line"}
            blob = self._git_ok(repo, "hash-object", "-w", "--stdin",
                                stdin=new_text).strip()
            fd, index = tempfile.mkstemp(prefix="disjorn-broker-index.")
            os.close(fd)
            os.unlink(index)          # git wants a path, not an empty file
            env = {"GIT_INDEX_FILE": index}
            try:
                self._git_ok(repo, "read-tree", old_sha, env=env)
                self._git_ok(repo, "update-index", "--add", "--cacheinfo",
                             f"100644,{blob},{relpath}", env=env)
                tree = self._git_ok(repo, "write-tree", env=env).strip()
            finally:
                try:
                    os.unlink(index)
                except OSError:
                    pass
            msg = (f"{slug}: Status -> {new_status}\n\nStamped by the broker "
                   f"(start-build). {comment}\n")
            ident = {"GIT_AUTHOR_NAME": "disjorn-broker",
                     "GIT_AUTHOR_EMAIL": "broker@disjorn.local",
                     "GIT_COMMITTER_NAME": "disjorn-broker",
                     "GIT_COMMITTER_EMAIL": "broker@disjorn.local"}
            commit = self._git_ok(repo, "commit-tree", tree, "-p", old_sha,
                                  "-m", msg, env=ident).strip()
            self._git_ok(repo, "update-ref", "-m", f"broker: {slug} -> {new_status}",
                         ref, commit, old_sha)
        except VerbError as exc:
            return {"ok": False, "status": new_status, "commit": None,
                    "why": exc.message}
        except Exception as exc:  # noqa: BLE001 — a stamp must never sink a build
            return {"ok": False, "status": new_status, "commit": None,
                    "why": repr(exc)}
        result = {"ok": True, "status": new_status, "commit": commit[:7],
                  "why": ""}
        notes: list[str] = []
        note = self._record_local_commit(commit)
        if note:
            notes.append(note)
        # Courtesy sync of the keyboard's worktree, only when it is provably safe:
        # HEAD is this branch and the file has no local edits.
        try:
            head = self._git(repo, "symbolic-ref", "--quiet", "HEAD").stdout.strip()
            if head == ref:
                # "Clean" = worktree AND index still equal the commit we just moved
                # past (old_sha), not HEAD — HEAD is already the new commit, against
                # which an untouched checkout looks modified.
                dirty = (self._git(repo, "diff", "--quiet", old_sha, "--",
                                   relpath).returncode != 0
                         or self._git(repo, "diff", "--quiet", "--cached",
                                      old_sha, "--", relpath).returncode != 0)
                if dirty:
                    notes.append(f"{relpath} has local edits in the working "
                                 "tree; the commit landed on the branch but "
                                 "the worktree was not touched")
                else:
                    self._git_ok(repo, "checkout", "HEAD", "--", relpath)
        except Exception as exc:  # noqa: BLE001 — courtesy only
            notes.append(f"worktree not synced: {exc!r}")
        # Carry the word to the mirror the gate and the residents read.
        try:
            self._ff_mirror_main(SUBPROCESS_TIMEOUTS["refresh-mirror"])
        except VerbError as exc:
            notes.append(f"mirror NOT refreshed: {exc.message}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"mirror NOT refreshed: {exc!r}")
        result["why"] = "; ".join(notes)
        return result
