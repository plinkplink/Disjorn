"""The two ways a change lands on main: the merge verb and the Tier-1 write wall."""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import tomllib
from typing import Any, Optional

import gates

from broker_common import (
    SUBPROCESS_TIMEOUTS, _SPEC_STEM_RE, SERVER_IDENTITY, MAX_SEQ, VerbError,
    _bad, ConfigError, _is_within, assert_dir_resident_unwritable, _check_int,
    _check_str, _reject_unknown,
)


# ---------------------------------------------------------------- the merge
# SPECS/2026-09-20-build-lane-v2-stage1-2b.md. `/merge <slug> [pass <seq>]`.
MERGE_VERB = "merge"
MERGE_REFUSED = "merge-refused"
# The closed set of refusal reasons; the wire carries one of these verbatim.
MERGE_REASONS = frozenset({
    "human", "branch-missing", "slug-mismatch", "busy", "moved", "gates",
    "tier", "pass-missing", "pass-invalid", "budget", "conflict", "push",
    "misconfigured"})
# The gate unit's own RuntimeMaxSec; the broker has to outwait the kill that works.
GATE_UNIT_RUNTIME_CAP_SEC = 1200
DEFAULT_GATE_TIMEOUT_SEC = 1320
DEFAULT_GATE_LOG_DIR = "/var/lib/disjorn-broker/gate-logs"
DEFAULT_MERGE_WORK_DIR = "/var/lib/disjorn-broker/merge-work"
# `[limits].daily_auto_apply_budget` in protected-paths.toml, when unreadable.
DEFAULT_AUTO_APPLY_BUDGET = 12
MERGE_IDENTITY_NAME = "disjorn-broker"
MERGE_IDENTITY_EMAIL = "broker@disjorn.local"
MAX_MERGE_PATHS = 500
MAX_SLUG_CHARS = 100
# A PASS cites the slug and says the word; both, or it is not a PASS.
_PASS_WORD_RE = re.compile(r"\bPASS\b")
# A verdict that also blocks is not a PASS, whatever else the post says.
_BLOCK_WORD_RE = re.compile(r"\bBLOCK\b")

# ------------------------------------------------ the fails-closed Tier-1 wall
# SPECS/2026-08-26-approval-object-and-resident-write-verbs.md item 2, building
# the tiers spec's "an unposted write fails closed".
#
# A seat writes its OWN Tier-0/1 surface by posting the exact content in
# #custodian and naming that post's seq. The seq is the only argument: the
# broker reads the post from the ledger, so no caller-supplied copy of the
# record, or token standing for one, is ever in the path.
#
# The guarantee is narrow: nothing reaches the surface without having been
# SHOWN in #custodian first. It is not an approval; no human sits in this path,
# and approval by another principal is the approval-* verbs' job.
WRITE_VERB = "apply-posted-write"
WRITE_RECORD_HEADER = "disjorn-write-record v1"
WRITE_RECORD_BEGIN = "--- content ---"
WRITE_RECORD_END = "--- end ---"
WRITE_RECORD_KEYS = ("path", "sha256")
# How long a posted record stays usable ([write_verbs].freshness_sec overrides).
DEFAULT_WRITE_FRESHNESS_SEC = 24 * 3600
# The whole file crosses this privileged daemon's address space, so it is
# bounded here as well as by the server's own message ceiling.
MAX_WRITE_CONTENT_BYTES = 256 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPO_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# --------------------------------------------------------------------------
# The posted record: parsing, path shape, and the tier map lookup.
#
# All three are pure functions of text, so every adversarial record in the test
# suite is exercised without a socket, a ledger or a filesystem.
# --------------------------------------------------------------------------

def check_repo_path(raw: Any) -> str:
    """A repo-relative target path, spelled the way a diff and the tier map
    spell it: `bots/fable/spine/05-bearings.md`.

    Refused outright: absolute paths, `.`/`..` components, empty components,
    backslashes, NUL, a leading dash, and any character outside
    [A-Za-z0-9._-]. The broker joins this onto a plink-owned root, so each of
    those is either an escape or an ambiguity, and an ambiguity here would be
    decided by whichever of the reader and the parser was wrong."""
    if not isinstance(raw, str) or not raw:
        raise _bad("the record's path is missing or empty")
    if len(raw) > 300:
        raise _bad("the record's path is too long (max 300 chars)")
    if raw.startswith("/") or raw.startswith("-") or "\\" in raw or "\0" in raw:
        raise _bad(f"the record's path must be repo-relative: {raw!r}")
    parts = raw.split("/")
    for part in parts:
        if not _REPO_PATH_COMPONENT_RE.match(part) or part in (".", ".."):
            raise _bad(f"the record's path has an unusable component {part!r} "
                       f"(allowed: letters, digits, '.', '_', '-')")
    return raw


def parse_write_record(text: Any) -> dict:
    """The posted record, parsed out of the #custodian message the broker read.

    THE POST IS THE RECORD — the whole message, not a block inside a longer
    one. A record that may be embedded in prose is a record whose boundaries
    this parser and a human reader can disagree about, and that disagreement
    would be invisible in the channel where the witnessing happens.

        disjorn-write-record v1
        path: bots/fable/spine/05-bearings.md
        sha256: <64 lowercase hex of the content between the fences>
        --- content ---
        <the exact bytes to be written>
        --- end ---

    THE END FENCE IS WHAT MAKES THE TRAILING NEWLINE EXPLICIT. Content is every
    line between the fences INCLUDING the newline that terminates the last one,
    so a transport that strips trailing whitespace cannot quietly change the
    file that gets written: it would change the sha, and the write would refuse.

    Exactly one of each fence, or the record is ambiguous and is refused —
    which is also what stops a fence line inside the content from truncating
    it. Returns {path, sha256, content}."""
    if not isinstance(text, str) or not text.strip():
        raise _bad("the cited post is empty")
    lines = text.splitlines(keepends=True)
    begins = [i for i, ln in enumerate(lines) if ln.strip() == WRITE_RECORD_BEGIN]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == WRITE_RECORD_END]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise _bad(f"the cited post is not a write record: it must carry "
                   f"exactly one {WRITE_RECORD_BEGIN!r} line and exactly one "
                   f"{WRITE_RECORD_END!r} line after it")

    head = [ln.strip() for ln in lines[:begins[0]]]
    head = [ln for ln in head if ln]
    if not head or head[0] != WRITE_RECORD_HEADER:
        raise _bad(f"the cited post does not begin with {WRITE_RECORD_HEADER!r}")
    fields: dict[str, str] = {}
    for line in head[1:]:
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if not sep or key not in WRITE_RECORD_KEYS:
            raise _bad(f"unusable header line in the record: {line[:80]!r} "
                       f"(expected {' and '.join(WRITE_RECORD_KEYS)})")
        if key in fields:
            raise _bad(f"the record names {key!r} twice")
        fields[key] = value.strip()
    missing = [k for k in WRITE_RECORD_KEYS if not fields.get(k)]
    if missing:
        raise _bad(f"the record does not name {', '.join(missing)}")

    for line in lines[ends[0] + 1:]:
        if line.strip():
            raise _bad("the record has text after its end fence; the post must "
                       "be the record and nothing else")

    content = "".join(lines[begins[0] + 1:ends[0]])
    raw = content.encode("utf-8")
    if len(raw) > MAX_WRITE_CONTENT_BYTES:
        raise _bad(f"the record's content is {len(raw)} bytes, over the "
                   f"{MAX_WRITE_CONTENT_BYTES}-byte ceiling")
    declared = fields["sha256"].lower()
    if not _SHA256_RE.match(declared):
        raise _bad("the record's sha256 must be 64 lowercase hex characters")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != declared:
        raise _bad(f"the record's sha256 does not match its own content "
                   f"(post says {declared}, content is {actual})")
    return {"path": check_repo_path(fields["path"]), "sha256": actual,
            "content": content}


def tier_for_path(tier_map: Any, seat: str, path: str) -> Optional[int]:
    """The tier this seat's own surface map gives a repo-relative path, or None.

    None is a refusal, never a default: a path no seat's map names has no tier
    on this surface, and the write verb applies Tier 0 and Tier 1 only. The
    `tier0` list is checked first; both apply alike, so the tier only labels the
    audit line.

    The map is `[tiers.<seat>]` in protected-paths.toml — beside the
    classifier's surface map, per the tiers spec's architecture note, and
    deliberately not in code. It is a DIFFERENT question from that file's
    [protected] list: [protected] tiers a DIFF at the merge gate, this tiers a
    seat writing its OWN live surface. The tiers spec puts personality and
    prompt-adjacent files at Tier 1 for exactly this path while they stay Tier
    2 for a merge, so consulting [protected] here would make the surface
    unreachable rather than safer. What keeps it narrow is the agreement it
    needs with [write_verbs.<seat>].repo_prefix in broker.toml: a tier-map row
    outside the seat's own root resolves to no host path at all."""
    if not isinstance(tier_map, dict):
        return None
    seat_map = tier_map.get(seat)
    if not isinstance(seat_map, dict):
        return None
    for tier, key in ((0, "tier0"), (1, "tier1")):
        entries = seat_map.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, str) or not entry:
                continue
            entry = entry.rstrip("/")
            if path == entry or path.startswith(entry + "/"):
                return tier
    return None


def _as_utc(value: Any) -> Optional[_dt.datetime]:
    """An ISO timestamp from the message DB or from git, as an aware UTC time."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=_dt.timezone.utc)


def merge_commit_message(*, slug: str, author: str, tier: int, channel_id: int,
                         seq: int, pass_seq: Optional[int] = None,
                         self_merge: bool = False) -> str:
    """The merge commit's text. `review-seq` goes AFTER `merge-seq` because the
    hook's last-trailer-wins rule is what records the review."""
    command = "/build" if self_merge else "/merge"
    lines = [f"merge: {slug} ({command} by {author}, tier {tier})", "",
             f"merge-seq: {channel_id}:{seq}"]
    if pass_seq is not None:
        lines.append(f"review-seq: {pass_seq}")
    return "\n".join(lines) + "\n"


def message_names_slug(content: str, slug: str) -> bool:
    """A `/merge` may only merge what its own message asks for: the slug, whole,
    after the command word."""
    text = (content or "").strip()
    parts = text.split(None, 1)
    if parts and parts[0].startswith("/"):
        text = parts[1] if len(parts) > 1 else ""
    return re.search(rf"(?<![0-9A-Za-z-]){re.escape(slug)}(?![0-9A-Za-z-])",
                     text) is not None


def format_merge_done(*, slug: str, sha: str, tier: int,
                      folded: bool = False) -> str:
    """The two lines a finished `/merge` posts to the room it was typed in."""
    return (f"merge: merged {slug} as {sha}"
            f"{' after folding main' if folded else ''} (tier {tier})\n"
            "next: deploy at the keyboard")


def format_merge_refused(*, slug: str, reason_text: str, next_line: str) -> str:
    """The same two lines for a merge that did not happen."""
    return (f"merge: refused {slug} — {' '.join(reason_text.split())[:400]}\n"
            f"next: {next_line}")


def merge_next_step(reason: str, *, slug: str, owner: Optional[str] = None,
                    gates_red: bool = False) -> str:
    """What the human does about a refusal, in their own hands."""
    if reason == "misconfigured":
        return "fix at the keyboard"
    if gates_red:
        return "fix the red gate, then /build again"
    if reason in ("moved", "conflict"):
        return "fold main into the branch, then /merge again"
    if reason in ("pass-missing", "pass-invalid") and owner:
        return f"PASS from {owner} in #custodian, then /merge {slug} pass <seq>"
    return "merge it at the keyboard"


def format_gate_tests_line(result: Any) -> str:
    """The gate run's own verdict: the launcher's exit code, never a self-report."""
    return ("pass" if result.exit_code == 0 else "fail") + f" — {result.summary}"


def format_tier_line(tier: Any, reasons: Any) -> str:
    said = [" ".join(str(r).split()) for r in list(reasons or [])[:2]]
    return (f"{tier} — " + "; ".join(said)) if said else str(tier)


class MergeVerbs:

    # ------------------------------------------------------------------ merge
    # SPECS/2026-09-20-build-lane-v2-stage1-2b.md; the design is
    # harness/cc/MERGE-CONTRACT.md. The broker never merges anything without a
    # green gate run IT launched: a caller-supplied gate result has no schema
    # slot to arrive in.

    @staticmethod
    def _merge_refused(message: str, reason: str) -> VerbError:
        # The reason is on the wire, so it is a closed set or it is noise.
        assert reason in MERGE_REASONS, reason
        return VerbError(MERGE_REFUSED, message, reason=reason)

    def _gatehouse_or_refuse(self) -> str:
        repo = self._gatehouse_repo()
        if repo is None:
            raise self._merge_refused(
                "this broker has no gatehouse, so there is nothing to merge into",
                "branch-missing")
        return repo

    def _gate_argv_prefix(self) -> list[str]:
        """`[start_build].command` aimed at `gate` instead of `run`."""
        command = self.start_build.get("command", [])
        if (not isinstance(command, list) or not command
                or not all(isinstance(a, str) for a in command)):
            raise VerbError("internal",
                            "start_build.command must be a non-empty list of strings")
        return ["gate" if a == "run" else a for a in command]

    def _gate_branch(self, slug: str) -> Any:
        """One synchronous gate run over `loop/<slug>`."""
        try:
            return gates.run_gates(self._gate_argv_prefix(), self.build_seat,
                                   slug, timeout=self.gate_timeout,
                                   log_dir=self.gate_log_dir)
        except OSError as exc:
            raise self._merge_refused(
                f"the gate run could not start ({exc})", "gates") from None

    def _gate_and_classify(self, slug: str) -> tuple[Any, dict]:
        """The gates, then the classifier over the same range with their result.

        A red gate is NOT special-cased: the classifier answers Tier 2 on it
        and that is the answer used. A misconfigured gate is no answer."""
        repo = self._gatehouse_or_refuse()
        result = self._gate_branch(slug)
        if result.misconfigured:
            said = f" (the gates still said: {result.summary})" if result.summary else ""
            raise self._merge_refused(
                f"gates misconfigured: {result.misconfigured}{said}", "misconfigured")
        classification = self._classify(repo, f"main...loop/{slug}",
                                        gates.gates_json(result))
        return result, classification

    def _claim_gate_run(self, slug: str) -> bool:
        """False when this slug is already being gated somewhere else."""
        with self._gate_lock:
            if slug in self._gate_runs:
                return False
            self._gate_runs.add(slug)
            return True

    def _release_gate_run(self, slug: str) -> None:
        with self._gate_lock:
            self._gate_runs.discard(slug)

    def _main_is_ancestor(self, slug: str) -> str:
        """Main's sha, returned only once loop/<slug> already contains main —
        a merge of a branch that does not is a merge of a tree nobody gated."""
        repo = self._gatehouse_or_refuse()
        cp = self._git(repo, "rev-parse", "--verify", "--quiet",
                       "refs/heads/main")
        sha = (cp.stdout or "").strip()
        if cp.returncode != 0 or not sha:
            raise self._merge_refused(
                "the gatehouse has no main to merge into", "branch-missing")
        if self._git(repo, "merge-base", "--is-ancestor", "refs/heads/main",
                     f"refs/heads/loop/{slug}").returncode != 0:
            raise self._merge_refused(
                f"loop/{slug} is behind main; fold main into the branch first",
                "moved")
        return sha

    def _fold_main_into(self, slug: str) -> Optional[str]:
        """Main merged into loop/<slug> in the gatehouse: the branch's new
        tip, or None when the merge moved nothing. Called only from the check
        that runs BEFORE the gates."""
        repo = self._gatehouse_or_refuse()
        git = self._argv("spec_repo_git", ["git"])
        timeout = SUBPROCESS_TIMEOUTS["merge"]
        branch = f"loop/{slug}"
        # NOT under the merge lock: this writes the branch, never main. The
        # per-slug gate claim its callers hold is what serialises it.
        with self._merge_workspace(slug) as clone:
            def head() -> str:
                return (self._run([*git, "-C", clone, "rev-parse", "HEAD"],
                                  timeout).stdout or "").strip()

            cp = self._run([*git, "clone", "--quiet", "--no-tags", "--branch",
                            branch, repo, clone], timeout)
            if cp.returncode != 0:
                raise self._merge_refused(
                    f"{branch} is not in the gatehouse", "branch-missing")
            before = head()
            cp = self._run([*git, "-C", clone, "fetch", "--quiet", "origin",
                            "refs/heads/main"], timeout)
            if cp.returncode != 0:
                raise self._merge_refused(
                    "the gatehouse has no main to merge into",
                    "branch-missing")
            main_sha = (self._run(
                [*git, "-C", clone, "rev-parse", "FETCH_HEAD"],
                timeout).stdout or "").strip()
            cp = self._run(
                [*git, "-C", clone,
                 "-c", f"user.name={MERGE_IDENTITY_NAME}",
                 "-c", f"user.email={MERGE_IDENTITY_EMAIL}",
                 "merge", "--no-edit", "-m",
                 f"fold main into {branch} (broker, before the gates)",
                 "FETCH_HEAD"], timeout)
            if cp.returncode != 0:
                self._run([*git, "-C", clone, "merge", "--abort"], timeout)
                raise self._merge_refused(
                    f"{branch} conflicts with main; fold it at the keyboard",
                    "moved")
            tip = head()
            if tip == before:
                return None
            cp = self._run([*git, "-C", clone, "push", "--quiet", "origin",
                            f"HEAD:refs/heads/{branch}"], timeout)
            if cp.returncode != 0:
                raise self._merge_refused(
                    "the gatehouse refused the fold "
                    f"({(cp.stderr or cp.stdout).strip()[:300]})", "push")
        self._build_ledger_line({
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "kind": "fold", "slug": slug, "from": before, "to": tip,
            "main": main_sha})
        return tip

    def _main_before_gates(self, slug: str) -> tuple[str, str, Optional[str]]:
        """The first check: main's sha, the branch tip everything after this
        point is pinned to, and the tip a fold left behind when the branch did
        not already contain main. Runs under the caller's gate claim.

        THE SHA THAT IS GATED IS THE SHA THAT IS MERGED — the fold happens
        here, before the gates, the classifier and any PASS check, so all three
        see the folded tip and a PASS posted before the fold stops holding."""
        try:
            main_sha, folded = self._main_is_ancestor(slug), None
        except VerbError as exc:
            if exc.reason != "moved":
                raise
            folded = self._fold_main_into(slug)
            main_sha = self._main_is_ancestor(slug)
        repo = self._gatehouse_or_refuse()
        tip = self._branch_tip(slug)
        if (self._git(repo, "rev-list", "--count",
                      f"main..{tip}").stdout or "").strip() == "0":
            raise self._merge_refused(
                f"loop/{slug} has no commits of its own to merge",
                "branch-missing")
        return main_sha, tip, folded

    def _branch_tip(self, slug: str) -> str:
        repo = self._gatehouse_or_refuse()
        return (self._git(repo, "rev-parse", "--verify", "--quiet",
                          f"refs/heads/loop/{slug}").stdout or "").strip()

    def _assert_main_unmoved(self, slug: str, before: str) -> None:
        """Main where the gates saw it, or this push would land a tree that was
        never gated."""
        try:
            now = self._main_is_ancestor(slug)
        except VerbError as exc:
            if exc.reason != "moved":
                raise
            now = ""
        if now != before:
            raise self._merge_refused(
                "main moved while the gates ran; /merge again", "moved")

    def _assert_tip_unmoved(self, slug: str, tip: str) -> None:
        """The branch where the gates saw it: a commit pushed while they ran is
        a tree nobody gated, and merging it would say otherwise."""
        if self._branch_tip(slug) != tip:
            raise self._merge_refused(
                f"loop/{slug} moved while the gates ran; /merge again",
                "moved")

    def _changed_paths(self, repo: str, slug: str) -> list[str]:
        cp = self._git(repo, "diff", "--name-only", f"main...loop/{slug}")
        if cp.returncode != 0:
            raise self._merge_refused(
                f"loop/{slug} cannot be compared with main", "branch-missing")
        return [ln.strip() for ln in (cp.stdout or "").splitlines()
                if ln.strip()][:MAX_MERGE_PATHS]

    def _branch_tip_time(self, repo: str, slug: str) -> Optional[_dt.datetime]:
        cp = self._git(repo, "log", "-1", "--format=%cI",
                       f"refs/heads/loop/{slug}")
        return _as_utc(cp.stdout) if cp.returncode == 0 else None

    def _lane_owner(self, path: str) -> Optional[str]:
        """`[planroom].lane_owners`, prefix map, first match wins. There is no
        default map in code: an unmapped path has no reviewer, which is true."""
        owners = self.planroom.get("lane_owners")
        if not isinstance(owners, dict):
            return None
        low = path.lower()
        for prefix, owner in owners.items():
            if low.startswith(str(prefix).lower()):
                return str(owner)
        return None

    def _pass_message(self, pass_seq: int) -> dict:
        """The reviewer's post, which is only ever a bot's, only ever in
        #custodian."""
        channel = self.disjorn.get("custodian_channel_id")
        if not isinstance(channel, int) or channel < 1:
            raise self._merge_refused(
                "this broker has no #custodian to read a PASS from", "pass-invalid")
        try:
            row = self._message_row(
                channel, pass_seq,
                "m.author_type, m.content, m.deleted_at, m.created_at, b.name",
                "left join bots b on b.id = m.author_id")
        except VerbError as exc:
            raise self._merge_refused(exc.message, "pass-invalid") from None
        if row is None or row[2]:
            raise self._merge_refused(
                f"there is no message {pass_seq} in #custodian", "pass-invalid")
        author_type, content, _deleted, created_at, bot_name = row
        if author_type != "bot" or not bot_name:
            raise self._merge_refused(
                f"message {pass_seq} in #custodian is not a reviewer's post",
                "pass-invalid")
        return {"author": str(bot_name), "content": str(content or ""),
                "created_at": created_at}

    def _check_pass(self, *, pass_seq: int, slug: str,
                    folded: Optional[str] = None) -> str:
        """The four things that make a PASS hold: the right reviewer, after the
        tip, in #custodian, saying PASS for this slug."""
        repo = self._gatehouse_or_refuse()
        paths = self._changed_paths(repo, slug)
        tip_at = self._branch_tip_time(repo, slug)
        if not paths:
            raise self._merge_refused(
                f"loop/{slug} changes no files", "pass-invalid")
        owners: list[str] = []
        for path in paths:
            owner = self._lane_owner(path)
            if owner is None:
                raise self._merge_refused(
                    f"no lane owner for {path}; keyboard merge, or /merge {slug} "
                    "without a pass", "pass-invalid")
            if owner not in owners:
                owners.append(owner)
        message = self._pass_message(pass_seq)
        author = message["author"]
        if author.lower() not in {o.lower() for o in owners}:
            raise self._merge_refused(
                f"seq {pass_seq} is {author}'s, and this lane's reviewer is "
                f"{' or '.join(owners)}", "pass-invalid")
        posted = _as_utc(message["created_at"])
        if tip_at is not None and (posted is None or posted <= tip_at):
            if folded:
                raise self._merge_refused(
                    f"loop/{slug} was folded onto main as {folded[:7]}, so "
                    f"PASS {pass_seq} no longer names the gated tree; ask for "
                    "a fresh PASS", "pass-invalid")
            raise self._merge_refused(
                f"seq {pass_seq} was posted before the tip of loop/{slug}",
                "pass-invalid")
        text = message["content"]
        if not _PASS_WORD_RE.search(text) or slug not in text:
            raise self._merge_refused(
                f"seq {pass_seq} does not say PASS for {slug}", "pass-invalid")
        if _BLOCK_WORD_RE.search(text):
            raise self._merge_refused(
                f"seq {pass_seq} says BLOCK as well as PASS, so it is not a PASS",
                "pass-invalid")
        return author

    def _auto_apply_budget(self) -> int:
        """`[limits].daily_auto_apply_budget` from the classifier's own config."""
        try:
            with open(self._protected_paths(), "rb") as fh:
                cfg = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            return DEFAULT_AUTO_APPLY_BUDGET
        limits = cfg.get("limits")
        budget = limits.get("daily_auto_apply_budget") if isinstance(limits, dict) else None
        return (budget if isinstance(budget, int) and not isinstance(budget, bool)
                and budget >= 0 else DEFAULT_AUTO_APPLY_BUDGET)

    def _auto_merges_today(self) -> int:
        """Self-merges on the ledger today (UTC). A human `/merge` is not
        budgeted, so only the `self_merge` flag counts — never the tier."""
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        n = 0
        try:
            with open(self.build_ledger, "r", encoding="utf-8") as fh:
                for raw in fh:
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if (rec.get("kind") == MERGE_VERB
                            and rec.get("self_merge") is True
                            and str(rec.get("ts", ""))[:10] == today):
                        n += 1
        except OSError:
            return 0
        return n

    @contextlib.contextmanager
    def _merge_workspace(self, slug: str):
        """A throwaway clone tree, gone on every exit path, so a half-done
        merge is a directory nobody reads rather than a gatehouse nobody can
        build from."""
        try:
            os.makedirs(self.merge_work_dir, mode=0o700, exist_ok=True)
            work = tempfile.mkdtemp(prefix=f"{slug}-", dir=self.merge_work_dir)
        except OSError as exc:
            raise VerbError("exec-failure",
                            f"no merge workspace ({exc})") from None
        try:
            yield os.path.join(work, "repo")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _merge_branch(self, *, slug: str, author: str, tier: int,
                      channel_id: int, seq: int, pass_seq: Optional[int],
                      main_sha: str, tip_sha: str,
                      self_merge: bool = False) -> str:
        """Clone, merge, push. What is merged is `tip_sha` and nothing else:
        an unpinned FETCH_HEAD would merge whatever was pushed last."""
        repo = self._gatehouse_or_refuse()
        git = self._argv("spec_repo_git", ["git"])
        timeout = SUBPROCESS_TIMEOUTS["merge"]
        with self._merge_workspace(slug) as clone:
            cp = self._run([*git, "clone", "--quiet", "--no-tags", "--branch",
                            "main", repo, clone], timeout)
            if cp.returncode != 0:
                raise self._merge_refused(
                    "the gatehouse would not clone "
                    f"({(cp.stderr or cp.stdout).strip()[:200]})", "push")
            cp = self._run([*git, "-C", clone, "fetch", "--quiet", "origin",
                            f"refs/heads/loop/{slug}"], timeout)
            if cp.returncode != 0:
                raise self._merge_refused(
                    f"loop/{slug} is not in the gatehouse", "branch-missing")
            fetched = (self._run(
                [*git, "-C", clone, "rev-parse", "FETCH_HEAD"],
                timeout).stdout or "").strip()
            if fetched != tip_sha:
                raise self._merge_refused(
                    f"loop/{slug} moved while the gates ran; /merge again",
                    "moved")
            cp = self._run(
                [*git, "-C", clone,
                 "-c", f"user.name={MERGE_IDENTITY_NAME}",
                 "-c", f"user.email={MERGE_IDENTITY_EMAIL}",
                 "merge", "--no-ff", "--no-edit", "-m",
                 merge_commit_message(slug=slug, author=author, tier=tier,
                                      channel_id=channel_id, seq=seq,
                                      pass_seq=pass_seq,
                                      self_merge=self_merge),
                 tip_sha], timeout)
            if cp.returncode != 0:
                self._run([*git, "-C", clone, "merge", "--abort"], timeout)
                raise self._merge_refused(
                    f"loop/{slug} does not merge cleanly into main; nothing "
                    "moved", "conflict")
            cp = self._run([*git, "-C", clone, "rev-parse", "HEAD"], timeout)
            sha = (cp.stdout or "").strip()
            self._assert_main_unmoved(slug, main_sha)
            self._assert_tip_unmoved(slug, tip_sha)
            cp = self._run([*git, "-C", clone, "push", "--quiet", "origin",
                            "HEAD:refs/heads/main"], timeout)
            if cp.returncode != 0:
                raise self._merge_refused(
                    "the gatehouse refused the push "
                    f"({(cp.stderr or cp.stdout).strip()[:300]})", "push")
            return sha

    def _refresh_after_merge(self) -> str:
        """main moved, so the mirror and the board follow it."""
        timeout = SUBPROCESS_TIMEOUTS["refresh-mirror"]
        try:
            self._ff_mirror_main(timeout)
            self._fetch_gatehouse_into_mirror(timeout)
        except VerbError as exc:
            return f"; mirror NOT refreshed ({exc.message})"
        except Exception as exc:  # noqa: BLE001 — the merge already landed
            return f"; mirror NOT refreshed ({exc!r})"
        self._planroom_rebuild(MERGE_VERB)
        return ""

    def _merge_now(self, *, slug: str, author: str, tier: int, channel_id: int,
                   seq: int, pass_seq: Optional[int], self_merge: bool,
                   main_sha: str, tip_sha: str) -> tuple[str, str]:
        """Budget, merge, ledger, refresh — under one lock, so two merges can
        never both read the same pre-cap count or race each other onto main.
        Only a self-merge is budgeted, and the ledger says which kind this was."""
        with self._merge_lock:
            if self_merge:
                budget = self._auto_apply_budget()
                if self._auto_merges_today() >= budget:
                    raise self._merge_refused(
                        f"today's Tier 0 auto-merge budget ({budget}) is spent",
                        "budget")
            sha = self._merge_branch(slug=slug, author=author, tier=tier,
                                     channel_id=channel_id, seq=seq,
                                     pass_seq=pass_seq, main_sha=main_sha,
                                     tip_sha=tip_sha, self_merge=self_merge)
            self._build_ledger_line({
                "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "kind": MERGE_VERB, "seq": seq, "channel_id": channel_id,
                "author": author, "slug": slug, "tier": tier, "sha": sha,
                "pass_seq": pass_seq, "self_merge": self_merge})
        return sha, self._refresh_after_merge()

    def _verb_merge(self, caller: str, args: dict) -> tuple[dict, str, dict]:
        """Take a human's `/merge`: the message and its PASS are settled here;
        the gates and the merge run in a thread, like `/build`.

        NOTHING THAT WRITES THE BRANCH RUNS ON THE SOCKET THREAD: the caller
        is acknowledged first, and the claim is taken before the PASS is read
        so a second `/merge` is `busy` before the PASS check reads the branch."""
        _reject_unknown(args, {"seq", "channel_id", "slug", "pass_seq"})
        for key in ("seq", "channel_id", "slug"):
            if key not in args:
                raise _bad(f"missing required arg: {key}")
        seq = _check_int(args, "seq", 0, 1, MAX_SEQ)
        channel_id = _check_int(args, "channel_id", 0, 1, MAX_SEQ)
        slug = _check_str(args, "slug", required=True, max_len=MAX_SLUG_CHARS)
        assert slug is not None
        pass_seq = None
        if args.get("pass_seq") is not None:
            pass_seq = _check_int(args, "pass_seq", 0, 1, MAX_SEQ)
        if not self.build_cfg:
            raise self._merge_refused(
                "chat merges are not configured on this broker", "human")

        message = self._build_message(channel_id, seq, code=MERGE_REFUSED,
                                      reason="human", act="merge a branch")
        author = message["author"]
        if author not in self.build_humans:
            raise self._merge_refused(
                f"{author} is not on the broker's human list", "human")

        repo = self._gatehouse_or_refuse()
        if not _SPEC_STEM_RE.match(slug):
            raise self._merge_refused(f"{slug} is not a build slug",
                                      "branch-missing")
        if self._git(repo, "rev-parse", "--verify", "--quiet",
                     f"refs/heads/loop/{slug}").returncode != 0:
            raise self._merge_refused(
                f"there is no loop/{slug} in the gatehouse", "branch-missing")
        if not message_names_slug(message["content"], slug):
            raise self._merge_refused(
                f"message {seq} does not ask to merge {slug}", "slug-mismatch")
        if not self._claim_gate_run(slug):
            raise self._merge_refused(
                f"a gate run for {slug} is already in flight", "busy")
        if pass_seq is not None:
            try:
                self._check_pass(pass_seq=pass_seq, slug=slug)
            except BaseException:
                self._release_gate_run(slug)
                raise

        thread = threading.Thread(
            target=self._merge_in_background, args=(dict(args),),
            kwargs={"slug": slug, "author": author, "channel_id": channel_id,
                    "seq": seq, "pass_seq": pass_seq},
            daemon=True)
        self._merge_threads.append(thread)
        try:
            thread.start()
        except RuntimeError:
            self._release_gate_run(slug)
            raise
        return ({"started": True, "slug": slug, "branch": f"loop/{slug}"},
                f"merge {slug} started for {author} "
                f"(#{channel_id} seq {seq}); the gates are running",
                {"merge_started": True})

    def _merge_in_background(self, args: dict, *, slug: str, author: str,
                             channel_id: int, seq: int,
                             pass_seq: Optional[int]) -> None:
        """The merge, off the socket thread. Nothing above it would catch, so
        it must never raise and must always give the slug back."""
        try:
            self._merge_and_post(args, slug=slug, author=author,
                                 channel_id=channel_id, seq=seq,
                                 pass_seq=pass_seq)
        except Exception:  # noqa: BLE001 — even an unwritable audit log
            pass
        finally:
            self._release_gate_run(slug)

    def _merge_and_post(self, args: dict, *, slug: str, author: str,
                        channel_id: int, seq: int,
                        pass_seq: Optional[int]) -> None:
        """The fold, the gates, the tier, the PASS and the merge, then the one
        post the room is left with."""
        note: dict = {"owner": None, "gates_red": False, "folded": None}
        try:
            body = self._merge_gated(
                args, slug=slug, author=author, channel_id=channel_id,
                seq=seq, pass_seq=pass_seq, note=note)
        except VerbError as exc:
            reason = exc.reason if exc.reason in MERGE_REASONS else "gates"
            self._audit(SERVER_IDENTITY, MERGE_VERB, args, False,
                        f"denied: {exc.message} ({reason})",
                        extra=({"folded": note["folded"]}
                               if note["folded"] else None))
            body = format_merge_refused(
                slug=slug, reason_text=exc.message,
                next_line=merge_next_step(reason, slug=slug,
                                          owner=note["owner"],
                                          gates_red=note["gates_red"]))
        except Exception as exc:  # noqa: BLE001 — the room is owed an answer
            self._audit(SERVER_IDENTITY, MERGE_VERB, args, True,
                        f"error: internal: {exc!r}")
            body = format_merge_refused(
                slug=slug, reason_text=f"the merge broke ({exc!r})",
                next_line=merge_next_step("internal", slug=slug))
        self._post_to_channel(channel_id, body)

    def _merge_gated(self, args: dict, *, slug: str, author: str,
                     channel_id: int, seq: int, pass_seq: Optional[int],
                     note: dict) -> str:
        """Steps 3–7 of the merge; `note` carries what a refusal line needs."""
        main_sha, tip, folded = self._main_before_gates(slug)
        note["folded"] = folded
        result, classification = self._gate_and_classify(slug)
        note["gates_red"] = result.exit_code != 0
        tier = self._tier_of(classification)
        reviewer = None
        if tier == 2:
            note["owner"] = self._first_lane_owner(slug)
            if pass_seq is None:
                raise self._merge_refused(
                    f"loop/{slug} is Tier 2: it needs a reviewer's PASS in "
                    f"#custodian, then `/merge {slug} pass <seq>`",
                    "pass-missing")
            reviewer = self._check_pass(pass_seq=pass_seq, slug=slug,
                                        folded=folded)
        stamped = pass_seq if tier == 2 else None
        sha, mirror = self._merge_now(slug=slug, author=author, tier=tier,
                                      channel_id=channel_id, seq=seq,
                                      pass_seq=stamped, self_merge=False,
                                      main_sha=main_sha, tip_sha=tip)
        cited = f", PASS from {reviewer} (seq {pass_seq})" if reviewer else ""
        extra = {"merge_tier": tier, "merged_sha": sha}
        if folded is not None:
            extra["folded"] = folded
        self._audit(SERVER_IDENTITY, MERGE_VERB, args, True,
                    f"merged loop/{slug} into main as {sha} for {author} "
                    f"(tier {tier}{cited})" + mirror, extra=extra)
        return format_merge_done(slug=slug, sha=sha, tier=tier,
                                 folded=folded is not None)

    def _first_lane_owner(self, slug: str) -> Optional[str]:
        """Who the banner names on a Tier 2 build."""
        try:
            paths = self._changed_paths(self._gatehouse_or_refuse(), slug)
        except VerbError:
            return None
        for path in paths:
            owner = self._lane_owner(path)
            if owner is not None:
                return owner
        return None

    def _build_end_gates(self, *, slug: str, origin: dict) -> dict:
        """The build's own gate run, one per slug at a time: a `/merge` typed
        while it runs is refused rather than gating the branch twice."""
        if not self._claim_gate_run(slug):
            return {"tests": f"n/a — a gate run for {slug} is already in flight",
                    "tier": "n/a — nothing to classify",
                    "next": f"/merge {slug}"}
        try:
            return self._build_end_banner(slug=slug, origin=origin)
        finally:
            self._release_gate_run(slug)

    def _build_end_banner(self, *, slug: str, origin: dict) -> dict:
        """The gates, the tier and — for a green Tier 0 in budget — the merge
        this build's own `/build` seq authorizes. Never raises: it runs in the
        reaper, where an exception would eat the banner."""
        try:
            main_sha, tip, folded = self._main_before_gates(slug)
            result, classification = self._gate_and_classify(slug)
            tier = self._tier_of(classification)
        except VerbError as exc:
            if exc.reason == "moved":
                return {"tests": "n/a — nothing was gated",
                        "tier": "n/a — nothing to classify",
                        "next": f"{exc.message}, then /merge {slug}"}
            if exc.reason == "misconfigured":
                return {"tests": exc.message,
                        "tier": "n/a — nothing to classify",
                        "next": merge_next_step(exc.reason, slug=slug)}
            return {"tests": f"fail — {exc.message}",
                    "tier": "unknown — the gates did not run",
                    "next": "fix the red gate, then /build again"}
        except Exception as exc:  # noqa: BLE001 — a banner is legibility
            return {"tests": f"fail — {exc!r}",
                    "tier": "unknown — the gates did not run",
                    "next": "fix the red gate, then /build again"}
        out = {"tests": format_gate_tests_line(result),
               "tier": format_tier_line(tier, classification.get("reasons"))}
        if folded is not None:
            out["folded"] = folded
        if result.exit_code != 0:
            out["next"] = "fix the red gate, then /build again"
        elif tier == 0 and origin.get("seq"):
            # The self-merge's message named no slug: the broker minted it from
            # that message, so there is nothing to compare it against.
            try:
                sha, _note = self._merge_now(
                    slug=slug, author=str(origin.get("author") or "the broker"),
                    tier=0, channel_id=int(origin.get("channel_id") or 0),
                    seq=int(origin.get("seq") or 0), pass_seq=None,
                    self_merge=True, main_sha=main_sha, tip_sha=tip)
            except VerbError as exc:
                out["next"] = (f"Tier 0 budget spent today; /merge {slug}"
                               if exc.reason == "budget" else f"/merge {slug}")
            except Exception:  # noqa: BLE001 — a banner is legibility
                out["next"] = f"/merge {slug}"
            else:
                out["next"] = f"merged {sha}"
                out["merged_tier"] = 0
                out["merged_sha"] = sha
        elif tier == 2:
            owner = self._first_lane_owner(slug) or "a reviewer"
            out["next"] = (f"PASS from {owner} in #custodian, then "
                           f"/merge {slug} pass <seq>")
        else:
            out["next"] = f"/merge {slug}"
        return out


class WriteWall:

    # ------------------------------------------------ write-verb config

    def _parse_write_seats(self) -> dict[str, dict]:
        """`[write_verbs.<seat>]` — one section per seat that may write its own
        Tier-0/1 surface. Three keys, all mandatory, all checked here:

          author       the seat's #custodian identity, which check (a) compares
                       the post's author against. An absent key is fatal rather
                       than permissive: a seat with no author could never
                       satisfy that check, so the section would read as a grant
                       while refusing every call.
          root         the host directory the seat's surface lives in, asserted
                       resident-UNWRITABLE for the reason start_build.specs_dir
                       is. A seat that can already write the target does not
                       need a record to write it, and the wall would be scenery.
          repo_prefix  what that root is called in a diff, so the tier map, the
                       classifier and the posted record all spell one path.

        TWO PLINK-OWNED FILES MUST AGREE before anything is written. The tier
        map ([tiers.<seat>] in protected-paths.toml) says which paths are Tier
        0/1 for a seat; this section says where that seat's repo actually lives.
        A tier-map row naming a path outside the seat's own repo_prefix resolves
        to no host path, so neither file can widen the surface on its own."""
        seats: dict[str, dict] = {}
        for name, section in self.write_verbs.items():
            if not isinstance(section, dict):
                continue  # a scalar here is a top-level knob, not a seat
            if name not in self.seat_names:
                raise ConfigError(
                    f"[write_verbs.{name}] is not a resident of this house "
                    f"([uids] / [residents]); refusing to start")
            author = section.get("author")
            if not isinstance(author, str) or not author.strip():
                raise ConfigError(
                    f"[write_verbs.{name}].author is missing: the record check "
                    f"is a comparison against the seat's #custodian identity, "
                    f"and without one no record can ever authorize a write. "
                    f"Refusing to start.")
            prefix = section.get("repo_prefix")
            if not isinstance(prefix, str) or not prefix.strip():
                raise ConfigError(
                    f"[write_verbs.{name}].repo_prefix is missing: without it "
                    f"the tier map's repo-relative rows cannot be resolved to "
                    f"a host path. Refusing to start.")
            prefix = prefix.strip().strip("/")
            try:
                check_repo_path(prefix)
            except VerbError as exc:
                raise ConfigError(
                    f"[write_verbs.{name}].repo_prefix is not a repo-relative "
                    f"path: {exc.message}. Refusing to start.") from None
            root = section.get("root")
            if not isinstance(root, str) or not root:
                raise ConfigError(
                    f"[write_verbs.{name}].root is missing; refusing to start")
            real = assert_dir_resident_unwritable(
                root,
                label=f"write_verbs.{name}.root",
                remedy=("Point it at the canonical copy plink owns (the same "
                        "tree [residents.<seat>].spine_dir reads from), never "
                        "at the seat's own home volume."),
                stake=("A seat that can write this directory directly does not "
                       "need a posted record to change it, so the wall would "
                       "be a promise again."),
                uid_map=self.uid_map, residents=self.residents)
            seats[name] = {"author": author.strip(), "repo_prefix": prefix,
                           "root": real}
        if not seats:
            raise ConfigError(
                "[write_verbs] is configured but names no seat sections "
                "([write_verbs.res-<name>]); refusing to start rather than "
                "come up with a write surface that grants nothing and looks "
                "armed")
        if not self.paths.get("protected_paths"):
            raise ConfigError(
                "[write_verbs] is configured but [paths].protected_paths is "
                "not: the tier map lives beside the classifier's surface map, "
                "and a write verb with no tier map has nothing to authorize "
                "against. Refusing to start.")
        if not self._write_message_db():
            raise ConfigError(
                "[write_verbs] is configured but no message store is: set "
                "[write_verbs].message_db (or [gate].message_db / "
                "[gate].deploy_tree). The broker reads the cited seq from the "
                "ledger itself; with no ledger there is nothing to read. "
                "Refusing to start.")
        return seats

    def _parse_write_consumed(self) -> str:
        """`[write_verbs].consumed_ledger`: the consumed-set, one JSON line per
        spent seq. Mandatory, in a resident-unwritable directory, because a seat
        that could truncate it could spend one record twice."""
        path = self.write_verbs.get("consumed_ledger")
        if not isinstance(path, str) or not path.strip().startswith("/"):
            raise ConfigError(
                "[write_verbs] is configured but write_verbs.consumed_ledger is "
                "missing or not an absolute path; refusing to start (without "
                "it no record can be marked spent)")
        path = path.strip()
        parent = assert_dir_resident_unwritable(
            os.path.dirname(path),
            label="write_verbs.consumed_ledger",
            remedy=("Keep it in the broker's own state dir, e.g. "
                    "/var/lib/disjorn-broker/write-consumed.jsonl."),
            stake=("A seat that can truncate the consumed-set can spend one "
                   "record twice."),
            uid_map=self.uid_map, residents=self.residents)
        return os.path.join(parent, os.path.basename(path))

    def _write_message_db(self) -> Optional[str]:
        """Where the #custodian ledger lives. `[write_verbs].message_db` wins,
        else the gate detector's `[gate].message_db`, else the conventional
        path under `[gate].deploy_tree` — the same resolution metrics.py does,
        so the two readers of this ledger can never be aimed at different
        files."""
        explicit = self.write_verbs.get("message_db")
        if isinstance(explicit, str) and explicit:
            return explicit
        gate = self.config.get("gate", {})
        gate = gate if isinstance(gate, dict) else {}
        configured = gate.get("message_db")
        if isinstance(configured, str) and configured:
            return configured
        deploy_tree = gate.get("deploy_tree")
        if isinstance(deploy_tree, str) and deploy_tree:
            return os.path.join(deploy_tree, "server", "data", "disjorn.db")
        return None

    def _write_freshness(self) -> int:
        window = self.write_verbs.get("freshness_sec", DEFAULT_WRITE_FRESHNESS_SEC)
        if isinstance(window, int) and not isinstance(window, bool) and window > 0:
            return window
        return DEFAULT_WRITE_FRESHNESS_SEC

    # --------------------------------------- the fails-closed Tier-1 wall

    def _write_seat(self, resident: str) -> dict:
        """The calling seat's write config, or a refusal. An unconfigured seat
        is refused with `verb-disabled` rather than `bad-args`: nothing about
        the request is wrong, the surface simply is not armed for that seat."""
        seat = self.write_seats.get(resident)
        if seat is None:
            raise VerbError(
                "verb-disabled",
                f"no write surface is configured for {resident}: "
                f"[write_verbs.{resident}] is absent from broker.toml")
        return seat

    def _read_ledger_post(self, seq: int) -> dict:
        """The cited #custodian post, read from the message store itself.

        The one place this verb's authority comes from. A caller-supplied copy
        of the record is not a design option, so nothing here reads `args`
        beyond the integer that selects the row. `seq` is per-channel (server
        migration 001), so "resolves somewhere else" is a real and different
        answer from "does not resolve" — and both refuse."""
        path = self._write_message_db()
        channel_id = self.disjorn.get("custodian_channel_id")
        if not path or not os.path.exists(path):
            raise VerbError("exec-failure",
                            "the message store is unreadable, so the cited "
                            "record cannot be verified")
        if not isinstance(channel_id, int):
            raise VerbError("internal",
                            "[disjorn].custodian_channel_id is not configured")
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise VerbError("exec-failure",
                            f"the message store cannot be opened: {exc}") from None
        try:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "select channel_id, author_type, author_id, content, created_at, "
                "edited_at, deleted_at from messages where seq=?",
                (seq,)).fetchall()
            hit = next((r for r in rows if r["channel_id"] == channel_id), None)
            if hit is None:
                raise _bad(f"seq {seq} resolves, but not in #custodian"
                           if rows else
                           f"#custodian seq {seq} does not resolve")
            # The record is what was SHOWN. An edit can rewrite content and sha
            # together, so any edited post is refused, as is a deleted one.
            if hit["deleted_at"]:
                raise VerbError("bad-args",
                                f"#custodian seq {seq} was deleted; a deleted "
                                f"post is not a record", reason="post-deleted")
            if hit["edited_at"]:
                raise VerbError("bad-args",
                                f"#custodian seq {seq} was edited after it was "
                                f"posted; post the record again, unedited",
                                reason="post-edited")
            if hit["author_type"] != "bot":
                raise VerbError("bad-args",
                                f"#custodian seq {seq} was posted by a person "
                                f"account, and only a seat's bot identity can "
                                f"post its record", reason="not-a-bot-post")
            author = self._ledger_author(db, hit["author_id"])
        except sqlite3.Error as exc:
            raise VerbError("exec-failure",
                            f"the message store query failed: {exc}") from None
        finally:
            db.close()
        return {"author": author, "content": hit["content"],
                "created_at": hit["created_at"]}

    @staticmethod
    def _ledger_author(db: Any, author_id: int) -> str:
        """`bots.name` for a bot-authored post; never `users.username`."""
        row = db.execute("select name as n from bots where id=?",
                         (author_id,)).fetchone()
        return row["n"] if row and row["n"] else f"bot:{author_id}"

    def _load_tier_map(self) -> dict:
        """`[tiers]` from protected-paths.toml, re-read on EVERY call.

        Live like verbs.toml and unlike the budgets, because this file is
        authorization: plink narrowing a seat's surface must bite on the next
        write, not after a broker restart. Unreadable means refused."""
        path = self.paths.get("protected_paths")
        try:
            with open(path, "rb") as fh:  # type: ignore[arg-type]
                cfg = tomllib.load(fh)
        except (TypeError, OSError, tomllib.TOMLDecodeError) as exc:
            raise VerbError("internal",
                            f"the tier map is unreadable ({exc}); refusing the "
                            f"write") from None
        tiers = cfg.get("tiers")
        return tiers if isinstance(tiers, dict) else {}

    def _consumed_seqs(self) -> set[int]:
        """Every seq the consumed-set records as spent. A complete line that
        does not parse refuses the write, since it could be hiding a spent seq;
        a torn final line (no newline) is skipped, because its fsync never
        returned and so nothing was written on it."""
        seqs: set[int] = set()
        try:
            with open(self.write_consumed, "r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.endswith("\n"):
                        break
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line).get("seq")
                    except (ValueError, AttributeError):
                        value = None
                    if not isinstance(value, int) or isinstance(value, bool):
                        raise VerbError(
                            "internal",
                            f"the consumed-set {self.write_consumed} has a line "
                            f"that does not parse; refusing the write")
                    seqs.add(value)
        except FileNotFoundError:
            return seqs
        except (OSError, UnicodeDecodeError) as exc:
            raise VerbError("internal",
                            f"the consumed-set is unreadable ({exc}); refusing "
                            f"the write") from None
        return seqs

    def _mark_consumed(self, entry: dict) -> None:
        """Append one spent seq and fsync it before the target is touched."""
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            fd = os.open(self.write_consumed,
                         os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                size = os.fstat(fd).st_size
                if size:
                    keep = os.pread(fd, size, 0).rfind(b"\n") + 1
                    if keep != size:
                        os.ftruncate(fd, keep)
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            if not size:
                dfd = os.open(os.path.dirname(self.write_consumed), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
        except OSError as exc:
            raise VerbError("internal",
                            f"the consumed-set cannot be written ({exc}); "
                            f"refusing the write") from None

    def _resolve_write_target(self, seat: dict, repo_path: str) -> str:
        """repo-relative path -> the host file this verb may write, or refuse.

        Containment is checked on the REALPATH of the parent directory, so a
        symlink planted anywhere in the chain cannot aim the write out of the
        seat's root. The parent must already exist: a verb that creates
        directories can scaffold a whole tree from one record, and the record
        only ever describes one file."""
        prefix = seat["repo_prefix"]
        if repo_path != prefix and not repo_path.startswith(prefix + "/"):
            raise _bad(f"{repo_path} is outside this seat's surface "
                       f"({prefix}/…)")
        relative = repo_path[len(prefix):].lstrip("/")
        if not relative:
            raise _bad("the record names a directory, not a file")
        target = os.path.join(seat["root"], relative)
        parent = os.path.realpath(os.path.dirname(target))
        if not _is_within(parent, seat["root"]):
            raise _bad(f"{repo_path} resolves outside this seat's root")
        if not os.path.isdir(parent):
            raise _bad(f"the directory for {repo_path} does not exist; this "
                       f"verb writes one named file and creates no directories")
        if os.path.islink(target) or (os.path.exists(target)
                                      and not os.path.isfile(target)):
            raise _bad(f"{repo_path} is not a regular file on disk")
        return os.path.join(parent, os.path.basename(target))

    @staticmethod
    def _apply_write(target: str, content: str) -> int:
        """Write the exact bytes, atomically. An existing file keeps its mode:
        this verb changes a file's contents and never its permissions."""
        raw = content.encode("utf-8")
        try:
            mode = stat.S_IMODE(os.stat(target).st_mode)
        except OSError:
            mode = 0o644
        directory = os.path.dirname(target)
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".write-record-")
        except OSError as exc:
            raise VerbError("exec-failure",
                            f"the write failed: {exc}") from None
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:  # pragma: no cover — the replace already moved it
                pass
            raise VerbError("exec-failure",
                            f"the write failed: {exc}") from None
        return len(raw)

    def _verb_apply_posted_write(self, resident: str,
                                 args: dict) -> tuple[dict, str, dict]:
        """Apply a write the caller already posted in #custodian. THE WALL.

        The argument is a #custodian seq and nothing more. Everything else —
        the target path, the content, the hash it must match — is read out of
        the post by this daemon, so there is no claim a caller can make that
        the broker takes on trust.

        Applies only if ALL of:
          (a) the post's author is the requesting seat's own identity;
          (b) the post names the target path and the sha256 of the exact
              content about to be written, and that path is Tier 0 or Tier 1 on
              the seat's own surface map;
          (c) the post is younger than the freshness window;
          (d) the seq is unconsumed — one record authorizes exactly one write.
        Any check failing refuses, and dispatch() audits the refusal like every
        other denial.

        CONSUME BEFORE WRITE (rev 2). The consumed mark is appended before the
        target is touched, so a crash in between leaves a spent record and an
        unapplied write; a retry against that seq is refused like any consumed
        seq and the caller posts a fresh record. Fail toward the wasted record,
        never toward a free replay.

        WHAT THIS IS NOT: an approval. Check (a) means a seat authorizes its
        own Tier-0/1 write by having posted it. Nothing here asks anyone
        whether the change is a good idea."""
        _reject_unknown(args, {"seq"})
        seq = _check_int(args, "seq", 0, 1, 2 ** 53)
        seat = self._write_seat(resident)

        post = self._read_ledger_post(seq)
        if post["author"] != seat["author"]:
            raise _bad(f"#custodian seq {seq} was posted by "
                       f"{post['author']!r}, not by {seat['author']!r}: a seat "
                       f"can only apply a record it posted itself")
        window = self._write_freshness()
        age = self._post_age_seconds(post["created_at"])
        if age is None:
            raise _bad(f"#custodian seq {seq} has an unreadable timestamp "
                       f"({post['created_at']!r}), so its freshness cannot be "
                       f"established")
        if age > window:
            raise _bad(f"#custodian seq {seq} is {int(age)}s old, past the "
                       f"{window}s freshness window; post the record again")

        record = parse_write_record(post["content"])
        tier = tier_for_path(self._load_tier_map(), resident, record["path"])
        if tier is None:
            raise _bad(f"{record['path']} is not on {resident}'s Tier-0/1 "
                       f"surface map; nothing above Tier 1 is written here")
        target = self._resolve_write_target(seat, record["path"])

        # (d) and the consume mark are ONE step under the lock — a check that
        # released before marking would let two concurrent calls both spend the
        # same record.
        with self._write_lock:
            if seq in self._consumed_seqs():
                raise _bad(f"#custodian seq {seq} is already consumed: one "
                           f"record authorizes exactly one write. Post a fresh "
                           f"record.")
            self._mark_consumed({
                "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "seq": seq, "seat": resident, "path": record["path"],
                "sha256": record["sha256"]})
            self._audit(resident, WRITE_VERB, {"seq": seq}, True,
                        f"consumed seq {seq} for {record['path']} "
                        f"(tier {tier}) — writing next",
                        extra={"consumed_seq": seq})
        written = self._apply_write(target, record["content"])
        return ({"applied": True, "seq": seq, "path": record["path"],
                 "tier": tier, "sha256": record["sha256"], "bytes": written},
                f"applied seq {seq} to {record['path']} "
                f"(tier {tier}, {written} bytes)",
                {"write_applied": True})

    @staticmethod
    def _post_age_seconds(created_at: Any) -> Optional[float]:
        """Age of a ledger timestamp in seconds, or None if it cannot be read.

        The server writes `2026-08-26T12:34:56.789Z`; `fromisoformat` refuses a
        `Z` suffix before 3.11's relaxation, so it is normalised rather than
        trusted to parse. A naive timestamp is read as UTC, which is what the
        server writes; None (not 0) is the answer when it cannot be read at
        all, because an unreadable timestamp must refuse, never pass."""
        if not isinstance(created_at, str) or not created_at:
            return None
        text = created_at.strip().replace("Z", "+00:00")
        try:
            when = _dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        return (_dt.datetime.now(_dt.timezone.utc) - when).total_seconds()
