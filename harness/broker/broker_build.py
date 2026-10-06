"""start-build and the chat build: spec parsing, the unit, its outputs, reattachment."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Optional

from broker_common import (
    SUBPROCESS_TIMEOUTS, _SPEC_STEM_RE, BUILD_UNIT_PREFIX,
    BUILD_ACTIVE_STATES, SERVER_IDENTITY, BUILD_VERB, BUILD_REFUSED, MAX_SEQ,
    DEFAULT_WAKE_SESSION_CAP_SEC, DEFAULT_WAKE_GRACE_SEC, build_unit_name,
    hidden_from_bots, VerbError, _bad, ConfigError, _check_int, _check_str,
    _reject_unknown, _clean_field, parse_spec_status, _BUILD_CALLER_RE,
)


START_BUILD_DEFAULT_TIMEOUT = 3600
MAX_SPEC_BYTES = 64 * 1024
# BL-D2: the detached build's stdout/stderr go to temp FILES (bounded on disk),
# never to a pipe the privileged broker must drain into RAM.
MAX_BUILD_LOG_TAIL = 64 * 1024
# One JSON sidecar per in-flight build, written next to its output spool BEFORE the
# launch.
BUILD_SIDECAR_SUFFIX = ".build.json"
BUILD_SIDECAR_SCHEMA = 1
MAX_BUILD_TEXT_CHARS = 4000
BUILD_SLUG_WORDS = 5
# A slug stem must still fit _SPEC_STEM_RE after a uniqueness suffix.
MAX_BUILD_SLUG_STEM = 45
MAX_BUILD_SLUG_TRIES = 99
_SLUG_WORD_RE = re.compile(r"[a-z0-9]+")

# ---------------------------------------------------------------- publish lines
# SPECS/2026-08-13-build-publish-path.md item 3. The build session no longer pushes
# anything: after the container exits, run-build.sh harvests HOST-side (as
# res-<name>, where the gatehouse group actually exists) and prints one
# machine-readable line per entitled repo.
_PUBLISH_REPO_RE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_PUBLISH_LINE_RES = (
    ("published", re.compile(
        rf"^PUBLISHED[ \t]+({_PUBLISH_REPO_RE}\.git)[ \t]+([0-9a-fA-F]{{7,64}})"
        r"[ \t]*$")),
    ("failed", re.compile(
        rf"^PUBLISH-FAILED[ \t]+({_PUBLISH_REPO_RE}\.git)[ \t]+(\S.*)$")),
    ("no_commits", re.compile(
        rf"^NO-COMMITS[ \t]+({_PUBLISH_REPO_RE}\.git)[ \t]*$")),
    # The quarantine line names the repo WITHOUT .git (it is a workspace clone, not
    # a bare repo) and carries a path we only ever echo, never open.
    ("quarantined", re.compile(
        rf"^QUARANTINED[ \t]+({_PUBLISH_REPO_RE})[ \t]+(\S.*)$")),
)
# Bounds on what reaches the banner.
MAX_PUBLISH_LINES = 8
MAX_PUBLISH_ERR_CHARS = 200
MAX_QUARANTINE_PATH_CHARS = 160


# --------------------------------------------------------------------------
# start-build (WP-L4): spec parsing, slug/branch derivation, the build-session
# prompt, and #custodian narration. Pure functions — no I/O, no broker state — so
# the confirm gate, the slug rules, and every narration shape are unit-testable in
# isolation, exactly like the argv validators above.
# --------------------------------------------------------------------------

def _status_comment_text(text: str, cap: int = 300) -> str:
    """Make resident-influenced text safe INSIDE an HTML comment."""
    flat = " ".join(str(text).split())
    flat = re.sub(r"-{2,}", "-", flat).replace(">", "&gt;")
    return flat[:cap]


def build_outcome_class(publish: dict, unit_reason: "str | None") -> str:
    """'failed' or 'done', from the harvest lines — THE ladder, in this order:
      1. the unit itself failed (`unit_reason`)      -> failed
      2. ANY PUBLISH-FAILED line                     -> failed
      3. at least one PUBLISHED or NO-COMMITS line   -> done
      4. nothing at all                              -> failed (never assume
         success from silence).
    format_build_outcome narrates from it and spec_status_after_build stamps
    the spec from it: one ladder, so the banner and the file can never disagree
    about whether a build failed."""
    if unit_reason is not None or publish.get("failed"):
        return "failed"
    if publish.get("published") or publish.get("no_commits"):
        return "done"
    return "failed"


def spec_status_after_build(*, branch: str, publish: dict,
                            unit_reason: "str | None") -> tuple[str, str]:
    """(status token, comment) the spec should carry once its build is terminal."""
    published = publish.get("published", [])
    verdict = build_outcome_class(publish, unit_reason)
    if verdict == "failed":
        if unit_reason is not None:
            why = unit_reason
        elif publish.get("failed"):
            why = "publish failed: " + "; ".join(
                f"{repo}: {err}" for repo, err in publish["failed"])
        else:
            why = NO_HARVEST_REASON
        where = ""
        if published:
            where = " Published anyway: " + ", ".join(
                f"{repo} {sha}" for repo, sha in published) + "."
        return ("failed", f"build failed: {_status_comment_text(why)}.{where} "
                          "To allow another build, set this back to `confirmed` "
                          "(the confirm record above still stands).")
    if published:
        shas = ", ".join(f"{repo} {sha}" for repo, sha in published)
        return (f"built@{branch}",
                f"build published: {_status_comment_text(shas)} — on the branch "
                "for review, nothing merged. `board --mark-merged` advances "
                "this to `merged` once the merge lands.")
    return ("confirmed", "the build ran and produced no commits — no branch, "
                         "nothing to review; buildable again.")


def parse_confirm_record(text: str) -> dict:
    """`{confirmed_by, seq}` from the `## Confirm record` section."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == "## confirm record":
            start = i + 1
            break
    out: dict = {"confirmed_by": None, "seq": None}
    if start is None:
        return out
    for line in lines[start:]:
        if line.strip().startswith("## "):
            break  # next section
        # MATCH THE WORDS, NOT THE ASTERISKS.
        plain = re.sub(r"[*_`]", "", line)
        m = re.match(r"\s*-\s*Confirmed by\s*:\s*(.*)$", plain, re.I)
        if m:
            out["confirmed_by"] = _clean_field(m.group(1))
        m = re.match(r"\s*-\s*#custodian seq\s*:\s*(.*)$", plain, re.I)
        if m:
            raw = _clean_field(m.group(1))
            if raw is not None:
                digits = re.search(r"\d+", raw)
                out["seq"] = int(digits.group()) if digits else None
    return out


def build_identity_from_caller(caller: str) -> str:
    """Short build identity ("claudette") from a uid_map caller name
    ("res-claudette")."""
    m = _BUILD_CALLER_RE.match(caller or "")
    if not m:
        raise VerbError("internal",
                        f"cannot derive a build identity from caller {caller!r} "
                        "(expected res-<name>); refusing rather than guessing")
    return m.group(1)


def slug_from_spec_filename(filename: str) -> str:
    """`SPECS/YYYY-MM-DD-<name>.md` -> `YYYY-MM-DD-<name>` (branch = loop/<slug>)."""
    base = os.path.basename(filename)
    if base.endswith(".md"):
        base = base[:-3]
    m = _SPEC_STEM_RE.match(base)
    if not m:
        raise _bad(f"spec filename does not yield a valid slug: {base!r} "
                   "(expected SPECS/YYYY-MM-DD-<kebab-name>.md)")
    try:
        _dt.date.fromisoformat(m.group(1))
    except ValueError:
        raise _bad(f"spec filename date is not a real date: {base!r}") from None
    return base


def build_session_prompt(spec_text: str, *, slug: str, branch: str) -> str:
    """The committed spec plus a one-paragraph preamble, fed to the build session on
    STDIN."""
    return (
        f"Build exactly what the spec below describes.\n"
        f"Your branch `{branch}` is already created and checked out in every "
        f"clone under `~/work`. Your worktree and the rules you work under are "
        f"in your CLAUDE.md; this message is the spec and nothing else.\n"
        f"When you are finished OR you have stopped, print the final JSON "
        f"object your CLAUDE.md describes as the last thing on stdout.\n\n"
        f"--- SPEC ({slug}) ---\n{spec_text}"
    )


def strip_build_command(content: str) -> str:
    """`/build <what to do>` -> `<what to do>`; the word is stripped only when it
    leads."""
    text = (content or "").strip()
    parts = text.split(None, 1)
    if parts and parts[0].lower() == f"/{BUILD_VERB}":
        text = parts[1] if len(parts) > 1 else ""
    return text.strip()


def slug_from_build_text(text: str, today: str) -> str:
    """`YYYY-MM-DD-<up to five words>`, kebab, ASCII, bounded."""
    stem = "-".join(_SLUG_WORD_RE.findall(text.lower())[:BUILD_SLUG_WORDS])
    stem = stem[:MAX_BUILD_SLUG_STEM].strip("-")
    return f"{today}-{stem}" if stem else ""


def build_chat_prompt(text: str, *, slug: str, branch: str) -> str:
    """The chat request under the same preamble a spec build gets."""
    return (
        f"Build exactly what the request below describes.\n"
        f"Your branch `{branch}` is already created and checked out in every "
        f"clone under `~/work`. Your worktree and the rules you work under are "
        f"in your CLAUDE.md; this message is the whole request and there is NO "
        f"SPEC FILE to read.\n"
        f"When you are finished OR you have stopped, print the final JSON "
        f"object your CLAUDE.md describes as the last thing on stdout.\n\n"
        f"--- REQUEST ({slug}) ---\n{text}"
    )


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _json_object_from_text(text: str) -> "dict | None":
    """Pull a JSON object out of a chunk of model prose."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Fenced blocks: take the LAST one — the report is the closing artifact, and a
    # spec quoted earlier in the reply may itself contain a fence.
    fences = _FENCED_JSON_RE.findall(text)
    for block in reversed(fences):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    # Last resort: scan backwards for a balanced brace span.
    for start in range(len(text) - 1, -1, -1):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:end + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(data, dict) and data:
                        return data
                    break
    return None


def _parse_build_report(stdout: str) -> dict:
    """Best-effort structured report from the build session's stdout for the 'done'
    line."""
    text = stdout.strip()
    files = tests = diff = "n/a"
    data: Any = None
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
    if data is None and text:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                data = None
            break
    if isinstance(data, dict):
        inner = data
        for key in ("build_report", "report", "result", "reply"):
            v = data.get(key)
            if isinstance(v, dict):
                inner = v
                break
            if isinstance(v, str):
                parsed = _json_object_from_text(v)
                if isinstance(parsed, dict):
                    inner = parsed
                    break

        def _fmt(val: Any) -> str:
            if isinstance(val, list):
                return ", ".join(str(x) for x in val) or "none"
            return str(val) if val is not None else "n/a"

        files = _fmt(inner.get("files"))
        tests = _fmt(inner.get("tests"))
        diff = _fmt(inner.get("diff"))
    elif text:
        diff = text.replace("\n", " ")[:300]
    return {"files": files, "tests": tests, "diff": diff}


def _match_publish_line(line: str) -> "tuple[str, tuple[str, ...]] | None":
    """One line of wrapper stdout -> (kind, fields), or None if it is not a
    publish-protocol line."""
    for kind, rx in _PUBLISH_LINE_RES:
        m = rx.match(line)
        if m:
            return kind, m.groups()
    return None


def _parse_publish_lines(out: str) -> dict:
    """The wrapper's harvest report, extracted from a build's stdout."""
    found: dict = {"published": [], "failed": [], "no_commits": [],
                   "quarantined": []}
    for raw in (out or "").splitlines():
        hit = _match_publish_line(raw.rstrip("\r"))
        if hit is None:
            continue
        kind, groups = hit
        if kind == "published":
            entry: Any = (groups[0], groups[1].lower())
        elif kind == "failed":
            entry = (groups[0], groups[1].strip()[:MAX_PUBLISH_ERR_CHARS])
        elif kind == "no_commits":
            entry = groups[0]
        else:
            entry = (groups[0], groups[1].strip()[:MAX_QUARANTINE_PATH_CHARS])
        bucket = found[kind]
        if entry in bucket or len(bucket) >= MAX_PUBLISH_LINES:
            continue
        bucket.append(entry)
    return found


def _strip_publish_lines(out: str) -> str:
    """The same stdout with the wrapper's protocol lines removed — what the SESSION
    printed, which is what _parse_build_report must see."""
    return "\n".join(ln for ln in (out or "").splitlines()
                     if _match_publish_line(ln.rstrip("\r")) is None)


def _publish_reported(publish: dict) -> bool:
    """Did the harvest report a VERDICT for any repo?"""
    return any(publish.get(k) for k in ("published", "failed", "no_commits"))


def _quarantine_suffix(quarantined) -> str:
    """Quarantine notices, one line each, appended to WHATEVER banner results."""
    return "".join(
        f"\nquarantined: {repo} -> {path} — unharvested work from an earlier "
        f"run, preserved not deleted" for repo, path in quarantined)


def format_build_started(*, slug: str, branch: str, confirmed_by: str,
                         seq: int, eta_sec: int) -> str:
    """The 'started' state-transition line."""
    eta_min = max(1, eta_sec // 60)
    return (f"build started | {slug} -> {branch} | "
            f"confirmed by {confirmed_by} (#custodian seq {seq}) | "
            f"ETA <= {eta_min}m (guess) | no merge, no push — lands on the branch")


def format_build_done(*, slug: str, branch: str, files: str, tests: str,
                      diff: str, tier: str = "pending", published=(),
                      no_commits=(), quarantined=(), mirror: str = "") -> str:
    """The 'done' state-transition line."""
    if published:
        outcome = "published: " + ", ".join(f"{repo} {sha}"
                                            for repo, sha in published)
        closing = "in the gatehouse for review — nothing merged"
    elif no_commits:
        outcome = ("no commits produced — nothing published, no branch exists ("
                   + ", ".join(no_commits) + ")")
        closing = "nothing to review — nothing merged"
    else:                       # unreachable via format_build_outcome
        outcome = "published: none reported"
        closing = "nothing to review — nothing merged"
    return (f"build done | {slug} -> {branch} | tier {tier} | {outcome} | "
            f"files: {files} | tests: {tests} | diff: {diff} | {closing}"
            + _quarantine_suffix(quarantined) + mirror)


def format_seq_build_banner(*, tests: str, tier: str, diffstat: str,
                            next_line: str) -> str:
    """The four lines that close a build started from chat."""
    return (f"tests: {tests}\n"
            f"tier: {tier}\n"
            f"diffstat: {diffstat}\n"
            f"next: {next_line}")


def format_build_refused(*, slug: str, branch: str, reason: str) -> str:
    """A build that never started, because its seat could not have run the tests the
    spec asks for."""
    detail = " ".join(reason.split())[:400]
    return (f"build refused | {slug} -> {branch} | nothing ran, no slot spent | "
            f"{detail or 'the build seat failed its dependency preflight'}")


def format_build_failed(*, slug: str, branch: str, reason: str, published=(),
                        no_commits=(), quarantined=(), mirror: str = "") -> str:
    """The 'failed' state-transition line — LOUD."""
    if published:
        where = ("published anyway: "
                 + ", ".join(f"{repo} {sha}" for repo, sha in published))
    elif no_commits:
        where = "nothing published — no commits, no branch exists"
    else:
        where = "nothing published — no branch to review"
    return (f"BUILD FAILED | {slug} -> {branch} | {reason} | {where} | "
            f"a human should look" + _quarantine_suffix(quarantined) + mirror)


# The fail-closed clause.
NO_HARVEST_REASON = (
    "the wrapper printed no publish lines — the harvest never reported "
    "(killed at the cap, or a wrapper predating the publish contract): "
    "outcome unknown, nothing was published")


def format_mirror_note(branch: str, published, error: "str | None") -> str:
    """The one line that makes a PUBLISHED banner openable (spec item 5)."""
    if not published:
        return ""
    if error:
        return ("\nmirror: NOT refreshed (" + " ".join(error.split())[:200]
                + ") — the sha above is in the gatehouse but not yet readable "
                  "in /opt/disjorn; run refresh-mirror before reviewing")
    refs = ", ".join(f"gatehouse/{repo.removesuffix('.git')}/{branch}"
                     for repo, _sha in published)
    return f"\nmirror: refreshed — read it at {refs}"


def format_spec_status_note(stamp: dict) -> str:
    """One trailing line for a build banner saying what happened to the spec's
    Status line — moved (to what, in which commit) or NOT moved (and why)."""
    if not stamp:
        return ""
    if stamp.get("ok"):
        commit = stamp.get("commit") or "?"
        return f"\nspec status: {stamp.get('status')} (commit {commit})"
    return ("\nspec status: NOT updated — "
            + " ".join(str(stamp.get("why", "")).split())[:300]
            + " — fix the Status line by hand or a resident may rebuild")


def format_build_outcome(*, slug: str, branch: str, publish: dict,
                         report: "dict | None" = None,
                         unit_reason: "str | None" = None,
                         mirror: str = "") -> str:
    """THE decision: done or failed, from the wrapper's harvest lines."""
    report = report or {"files": "n/a", "tests": "n/a", "diff": "n/a"}
    published = publish.get("published", [])
    quarantined = publish.get("quarantined", [])
    no_commits = publish.get("no_commits", [])
    failed = publish.get("failed", [])
    common = {"published": published, "no_commits": no_commits,
              "quarantined": quarantined, "mirror": mirror}
    if build_outcome_class(publish, unit_reason) == "done":
        return format_build_done(slug=slug, branch=branch, files=report["files"],
                                 tests=report["tests"], diff=report["diff"],
                                 tier="pending", **common)
    if unit_reason is not None:
        return format_build_failed(slug=slug, branch=branch, reason=unit_reason,
                                   **common)
    if failed:
        errors = "; ".join(f"{repo}: {err}" for repo, err in failed)
        return format_build_failed(slug=slug, branch=branch,
                                   reason=f"publish failed: {errors}", **common)
    return format_build_failed(slug=slug, branch=branch,
                               reason=NO_HARVEST_REASON, **common)


class BuildVerbs:

    # How often an ADOPTED build's unit is polled for its terminal state.
    BUILD_POLL_SEC = 5.0

    # ------------------------------------------------------- build config

    def _parse_build_humans(self) -> frozenset[str]:
        """`[build].humans` — the accounts whose `/build` the broker will act on.

        An EMPTY list is legal and means the verb refuses everything; a missing
        or malformed list is not, because a human list nobody can read is a
        surface that reads as armed while refusing every call."""
        humans = self.build_cfg.get("humans")
        if not isinstance(humans, list) or not all(
                isinstance(h, str) and h.strip() for h in humans):
            raise ConfigError(
                "[build] is configured but build.humans is missing or is not a "
                "list of usernames; refusing to start")
        for human in humans:
            if _BUILD_CALLER_RE.match(human.strip()):
                raise ConfigError(
                    f"build.humans names {human!r}, which is a resident seat: "
                    "only a person may start a build. Refusing to start.")
        return frozenset(h.strip() for h in humans)

    def _parse_build_seat(self) -> str:
        """`[build].seat` — the build identity, a resident name WITHOUT `res-`."""
        seat = self.build_cfg.get("seat")
        if not isinstance(seat, str) or not seat.strip():
            raise ConfigError(
                "[build] is configured but build.seat is missing; refusing to "
                "start (a build with no seat has nowhere to run)")
        seat = seat.strip()
        if _BUILD_CALLER_RE.match(seat):
            raise ConfigError(
                f"build.seat is {seat!r}: name the resident WITHOUT the `res-` "
                "prefix (e.g. `gable`); refusing to start")
        if f"res-{seat}" not in self.seat_names:
            raise ConfigError(
                f"build.seat names {seat!r}, and res-{seat} is not a resident "
                "of this house ([uids] / [residents]); refusing to start")
        return seat

    def _parse_build_ledger(self) -> str:
        """`[build].ledger` — where every accepted `/build` is snapshotted."""
        path = self.build_cfg.get("ledger")
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(
                "[build] is configured but build.ledger is missing; refusing "
                "to start (the ledger is the record that a human asked)")
        return path.strip()

    def _parse_build_int(self, key: str, default: int, *, minimum: int = 1,
                         stake: str = "") -> int:
        """A positive-integer `[build]` knob, at or above its floor."""
        value = self.build_cfg.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            floor = ("a positive integer" if minimum == 1
                     else f"an integer above {minimum - 1}")
            raise ConfigError(
                f"build.{key} must be {floor}; refusing to start"
                + (f" ({stake})" if stake else ""))
        return value

    def _parse_build_dir(self, key: str, default: str) -> str:
        """A `[build]` directory the broker writes into, never a resident."""
        value = self.build_cfg.get(key, default)
        if not isinstance(value, str) or not value.strip().startswith("/"):
            raise ConfigError(
                f"build.{key} must be an absolute path; refusing to start")
        return value.strip()

    def _wake_session_cap(self) -> int:
        cap = self.wake.get("session_cap_sec", DEFAULT_WAKE_SESSION_CAP_SEC)
        return cap if isinstance(cap, int) and cap > 0 else DEFAULT_WAKE_SESSION_CAP_SEC

    def _wake_grace(self) -> int:
        grace = self.wake.get("grace_sec", DEFAULT_WAKE_GRACE_SEC)
        return grace if isinstance(grace, int) and grace >= 0 else DEFAULT_WAKE_GRACE_SEC

    # ------------------------------------------------------------ start-build

    def _specs_dir(self) -> str:
        """The SPECS/ dir the confirm gate reads."""
        if self.specs_dir_real:
            return self.specs_dir_real
        d = self.start_build.get("specs_dir")
        if not d or not isinstance(d, str):
            raise VerbError("internal", "start_build.specs_dir is not configured")
        return d

    def _resolve_spec_path(self, spec: str) -> str:
        """Map caller input to a real spec file, CONFINED to the configured SPECS/
        dir. realpath() resolves BOTH `..` traversal and symlink escape, then we
        require the resolved file to sit DIRECTLY in SPECS/ (the flat
        one-file-per-spec layout) and end in .md."""
        if spec.startswith("-") or "\x00" in spec:
            raise _bad("spec must not start with '-' or contain NUL")
        specs_dir = self._specs_dir()
        candidate = spec if os.path.isabs(spec) else os.path.join(specs_dir, spec)
        real = os.path.realpath(candidate)
        real_specs = os.path.realpath(specs_dir)
        if os.path.dirname(real) != real_specs or not real.endswith(".md"):
            raise _bad("spec must be a .md file directly inside the SPECS/ directory")
        if not os.path.isfile(real):
            raise _bad("spec file does not exist")
        return real

    def _read_confirmed_spec(self, path: str) -> dict:
        """Read + validate the spec at `path`: status must be 'confirmed' and the
        confirm record must be filled (Confirmed by + #custodian seq)."""
        try:
            if os.path.getsize(path) > MAX_SPEC_BYTES:
                raise _bad(f"spec exceeds {MAX_SPEC_BYTES} bytes")
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            raise VerbError("exec-failure", f"spec not readable: {exc}") from None

        status = parse_spec_status(text)
        if status != "confirmed":
            raise _bad(f"spec status is {status!r}, not 'confirmed' — no build "
                       "starts without a confirmed spec")
        confirm = parse_confirm_record(text)
        if not confirm.get("confirmed_by") or confirm.get("seq") is None:
            raise _bad("spec has no confirm record (need 'Confirmed by' + "
                       "'#custodian seq') — the confirm record is the instance "
                       "selector the broker verifies mechanically")
        slug = slug_from_spec_filename(path)
        return {"text": text, "slug": slug, "branch": f"loop/{slug}",
                "confirmed_by": confirm["confirmed_by"], "seq": confirm["seq"]}

    def _build_argv(self, slug: str, build_resident: str) -> list[str]:
        """The detached build command — a PURE function of config + the validated
        slug."""
        command = self.start_build.get("command", [])
        if (not isinstance(command, list) or not command
                or not all(isinstance(a, str) for a in command)):
            raise VerbError("internal",
                            "start_build.command must be a non-empty list of strings")
        # BR-1: the identity is the CALLER's, passed in — see
        # build_identity_from_caller. [start_build].resident is dead config and
        # warned about at startup.
        resident_arg = build_resident
        session_argv = self.start_build.get("session_argv", [])
        if (not isinstance(session_argv, list)
                or not all(isinstance(a, str) for a in session_argv)):
            raise VerbError("internal",
                            "start_build.session_argv must be a list of strings")
        model = self.start_build.get("model")
        if not isinstance(model, str) or not model.strip():
            raise VerbError("internal",
                            "start_build.model must be a non-empty string "
                            "(WP-L5 pin; no fallback)")
        return [*command, resident_arg, slug, *session_argv, "--model", model.strip()]

    def _default_build_spawn(self, argv: list[str], *, stdout: Any,
                             stderr: Any) -> subprocess.Popen:
        """Launch the build DETACHED so it outlives this request."""
        return subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )

    # -------------------------------------------------- build output (BL-D2)

    def _build_log_dir(self) -> str:
        """Where the detached build's stdout/stderr files live."""
        d = self.config.get("broker", {}).get("build_log_dir")
        if isinstance(d, str) and d:
            return d
        base = os.path.dirname(self.audit_path)
        if base:
            candidate = os.path.join(base, "build-logs")
            try:
                os.makedirs(candidate, mode=0o700, exist_ok=True)
                return candidate
            except OSError:
                pass
        return tempfile.gettempdir()

    def _open_build_logs(self, slug: str) -> tuple[str, str, Any, Any]:
        """Create the two 0600 output files for one build and return (out_path,
        err_path, out_fh, err_fh). mkstemp() creates them with mode 0600 and O_EXCL,
        so no other local user can read a build's output and nothing can be
        pre-planted at the path."""
        d = self._build_log_dir()
        try:
            out_fd, out_path = tempfile.mkstemp(
                prefix=f"disjorn-build-{slug}.", suffix=".out", dir=d)
            try:
                err_fd, err_path = tempfile.mkstemp(
                    prefix=f"disjorn-build-{slug}.", suffix=".err", dir=d)
            except OSError:
                os.close(out_fd)
                os.unlink(out_path)
                raise
        except OSError as exc:
            raise VerbError("exec-failure",
                            f"cannot create build output file: {exc}") from None
        return out_path, err_path, os.fdopen(out_fd, "wb"), os.fdopen(err_fd, "wb")

    @staticmethod
    def _close_build_logs(*handles: Any) -> None:
        for fh in handles:
            try:
                fh.close()
            except Exception:  # noqa: BLE001 — closing twice is fine
                pass

    @staticmethod
    def _unlink_build_logs(*paths: str) -> None:
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def _read_build_tail(path: str, limit: int = MAX_BUILD_LOG_TAIL) -> str:
        """The last `limit` bytes of a build output file, decoded leniently."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - limit))
                data = fh.read(limit)
        except OSError:
            return ""
        return data.decode("utf-8", "replace")

    @staticmethod
    def _read_build_head(path: str, limit: int = MAX_BUILD_LOG_TAIL) -> str:
        """The FIRST `limit` bytes, truncated at the last complete line."""
        try:
            with open(path, "rb") as fh:
                data = fh.read(limit + 1)
        except OSError:
            return ""
        if len(data) <= limit:
            return data.decode("utf-8", "replace")
        text = data[:limit].decode("utf-8", "replace")
        return text[:text.rfind("\n") + 1]

    def _harvest_report(self, out_path: str, out_tail: str) -> dict:
        """The wrapper's publish lines for one build: parsed from the log's head AND
        tail, because the two ends carry different halves of the protocol
        (quarantine at provisioning time, verdicts after the container exits)."""
        return _parse_publish_lines(self._read_build_head(out_path) + "\n"
                                    + out_tail)

    # ------------------------------------------- transient-unit lifecycle (L4)

    def _start_build_argv(self, key: str, default: list[str]) -> list[str]:
        """A fixed argv list out of `[start_build]`, validated like `_argv`
        validates `[commands]`."""
        argv = self.start_build.get(key, default)
        if not isinstance(argv, list) or not argv or not all(
                isinstance(a, str) for a in argv):
            raise VerbError("internal",
                            f"start_build.{key} must be a non-empty list of strings")
        return list(argv)

    def _build_unit_state(self, slug: str) -> str:
        """systemd's word for what the build's unit is doing — `active`, `failed`,
        `inactive`, or `unknown` if we cannot ask."""
        try:
            argv = self._start_build_argv(
                "unit_state_command",
                ["systemctl", "show", "--property=ActiveState", "--value"])
            cp = self._run([*argv, build_unit_name(slug)], 30)
        except Exception:  # noqa: BLE001 — a state probe never breaks a reaper
            return "unknown"
        if cp.returncode != 0:
            return "unknown"
        return (cp.stdout or "").strip().lower() or "unknown"

    def _stop_build_unit(self, slug: str, build_resident: str) -> bool:
        """Ask systemd to stop a build's unit."""
        try:
            argv = self._start_build_argv(
                "stop_command",
                ["sudo", "-n", "/usr/local/lib/disjorn/disjorn-build-launch", "stop"])
            cp = self._run([*argv, build_resident, slug], 60)
            return cp.returncode == 0
        except Exception:  # noqa: BLE001 — never crash a reaper on cleanup
            return False

    def _sidecar_path(self, slug: str) -> str:
        return os.path.join(self._build_log_dir(), f"{slug}{BUILD_SIDECAR_SUFFIX}")

    def _write_build_sidecar(self, meta: dict, *, out_path: str, err_path: str,
                             timeout: int) -> str:
        """Persist everything a FUTURE broker process needs to finish this build's
        story: which unit, which branch, which spool files, and when the cap
        expires."""
        path = self._sidecar_path(meta["slug"])
        record = {
            "schema": BUILD_SIDECAR_SCHEMA,
            "slug": meta["slug"],
            "branch": meta["branch"],
            "unit": build_unit_name(meta["slug"]),
            # Since BR-1 these two agree by construction — `build_resident` is
            # DERIVED from `caller` (strip res-, launch helper re-derives uid/
            # home/config from it).
            "caller": meta.get("resident"),
            "build_resident": meta.get("build_resident", ""),
            "confirmed_by": meta.get("confirmed_by"),
            "seq": meta.get("seq"),
            # Present only for a build started from chat.
            "origin": meta.get("origin"),
            "out_path": out_path,
            "err_path": err_path,
            # NO pid, deliberately.
            "timeout_sec": timeout,
            "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "deadline": time.time() + timeout,
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return path

    def _remove_build_sidecar(self, slug: str) -> None:
        try:
            os.unlink(self._sidecar_path(slug))
        except OSError:
            pass

    def _narrate(self, body: str) -> None:
        """Post a build state-transition line to #custodian via the broker's OWN bot
        identity — the same transport file-proposal uses."""
        try:
            self.transport(self.disjorn, body)
        except Exception:  # noqa: BLE001 — narration is legibility, not control
            pass

    def _refresh_mirror_for_banner(self, branch: str, published) -> str:
        """SPEC ITEM 5 — refresh the mirror BEFORE the banner that names a sha."""
        if not published:
            return ""
        error = None
        try:
            if not self._gatehouse_fetch_argvs():
                # No gatehouse configured: there is no mirror claim to make, so the
                # banner makes none.
                return ""
            self._fetch_gatehouse_into_mirror(
                SUBPROCESS_TIMEOUTS["refresh-mirror"])
        except VerbError as exc:
            error = exc.message
        except Exception as exc:  # noqa: BLE001 — never crash a reaper
            error = repr(exc)
        return format_mirror_note(branch, published, error)

    def _narrate_build_outcome(self, **kwargs) -> None:
        """Every terminal build banner goes through here, so the mirror fetch cannot
        be forgotten on one of the five paths that post one."""
        origin = kwargs.pop("origin", None)
        if origin:
            # A build nobody wrote a spec for: no Status line to stamp, no
            # #custodian outcome post, and the banner goes where it was asked for.
            self._seq_build_outcome(origin=origin, **kwargs)
            return
        publish = kwargs.get("publish") or {}
        kwargs["mirror"] = self._refresh_mirror_for_banner(
            kwargs.get("branch", ""), publish.get("published", []))
        status, comment = spec_status_after_build(
            branch=kwargs.get("branch", ""), publish=publish,
            unit_reason=kwargs.get("unit_reason"))
        stamp = self._stamp_spec_status(kwargs.get("slug", ""), status, comment,
                                        expect=("building",))
        self._narrate(format_build_outcome(**kwargs)
                      + format_spec_status_note(stamp))
        # A build's terminal banner is the loudest column transition the house has —
        # `building` -> Review, or `building` -> back to Ready on a failure — and
        # the mirror was just refreshed two lines above.
        self._planroom_rebuild("build-outcome")

    # ------------------------------------------- reattachment after a restart

    def adopt_inflight_builds(self) -> list[str]:
        """Re-adopt builds that outlived the previous broker process, and sweep what
        did not survive."""
        adopted: list[str] = []
        keep: set[str] = set()
        try:
            log_dir = self._build_log_dir()
            entries = sorted(os.listdir(log_dir))
        except OSError:
            return adopted
        for name in entries:
            if not name.endswith(BUILD_SIDECAR_SUFFIX):
                continue
            path = os.path.join(log_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                slug = rec["slug"]
                build_unit_name(slug)          # re-validate: hostile until proven
                # The ticket must be named after the build it claims, or the slug
                # inside decides which files get deleted while the filename decides
                # nothing — a mismatch is not a build record.
                if name != f"{slug}{BUILD_SIDECAR_SUFFIX}":
                    raise ValueError("sidecar name does not match its slug")
            except Exception:  # noqa: BLE001 — an unreadable ticket is garbage
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            out_path = str(rec.get("out_path") or "")
            err_path = str(rec.get("err_path") or "")
            with self._build_lock:
                ours = slug in self._active_builds
            if ours:
                # A build THIS process already owns: its own reaper will finish the
                # story.
                keep.update({os.path.basename(p) for p in (out_path, err_path) if p})
                keep.add(name)
                continue
            state = self._build_unit_state(slug)
            if state in BUILD_ACTIVE_STATES:
                with self._build_lock:
                    self._active_builds.add(slug)
                keep.update({os.path.basename(p) for p in (out_path, err_path) if p})
                keep.add(name)
                adopted.append(slug)
                t = threading.Thread(target=self._reap_adopted_build,
                                     args=(rec,), daemon=True)
                self._build_threads.append(t)
                t.start()
            else:
                self._narrate_adopted_outcome(rec, state)
                self._unlink_build_logs(out_path, err_path)
                self._remove_build_sidecar(slug)
        # Janitor: spool files with no live ticket are orphans from a broker that
        # died mid-build.
        for name in entries:
            if name in keep or not name.startswith(BUILD_UNIT_PREFIX):
                continue
            if name.endswith(".out") or name.endswith(".err"):
                self._unlink_build_logs(os.path.join(log_dir, name))
        return adopted

    def _reap_adopted_build(self, rec: dict) -> None:
        """Watch an adopted build to its terminal state, then narrate + tidy."""
        slug = rec["slug"]
        try:
            deadline = float(rec.get("deadline") or 0.0)
            while not self._closed:
                state = self._build_unit_state(slug)
                if state not in BUILD_ACTIVE_STATES:
                    self._narrate_adopted_outcome(rec, state)
                    break
                if deadline and time.time() > deadline:
                    self._stop_build_unit(
                        slug, str(rec.get("build_resident") or ""))
                    out_path = str(rec.get("out_path") or "")
                    self._narrate_build_outcome(
                        slug=slug, branch=rec.get("branch", f"loop/{slug}"),
                        origin=rec.get("origin"),
                        publish=self._harvest_report(
                            out_path, self._read_build_tail(out_path)),
                        unit_reason=f"timed out after {rec.get('timeout_sec')}s "
                                    "— killed (build re-adopted after a broker "
                                    "restart)")
                    break
                time.sleep(self.BUILD_POLL_SEC)
            else:
                return           # shutting down: leave the ticket for next time
        except Exception:        # noqa: BLE001 — same rule: keep the ticket
            return
        self._unlink_build_logs(str(rec.get("out_path") or ""),
                                str(rec.get("err_path") or ""))
        self._remove_build_sidecar(slug)
        self._finish_build(slug)

    def _narrate_adopted_outcome(self, rec: dict, state: str) -> None:
        """The done/failed line for a build this process did not launch."""
        slug = rec["slug"]
        branch = rec.get("branch", f"loop/{slug}")
        out_path = str(rec.get("out_path") or "")
        out_s = self._read_build_tail(out_path)
        err_s = self._read_build_tail(str(rec.get("err_path") or ""))
        publish = self._harvest_report(out_path, out_s)
        session_out = _strip_publish_lines(out_s)
        report = _parse_build_report(session_out)
        note = (err_s or session_out).strip()[:400]
        unit_reason = None
        if state == "failed":
            unit_reason = (f"re-adopted after a broker restart; the unit ended in "
                           f"state {state} — outcome unknown"
                           + (f": {note}" if note else ""))
        elif not _publish_reported(publish):
            unit_reason = ("re-adopted after a broker restart and the wrapper "
                           f"printed no publish lines (unit state {state}) — "
                           "the harvest never reported: outcome unknown, nothing "
                           "was published" + (f": {note}" if note else ""))
        self._narrate_build_outcome(
            slug=slug, branch=branch, publish=publish, report=report,
            unit_reason=unit_reason, origin=rec.get("origin"))

    def _launch_build(self, resident: str, meta: dict, prompt: str, *,
                      announce: Callable[[int], None],
                      launch_failed: Callable[[Exception], None],
                      ) -> tuple[Any, int, Optional[int], int]:
        """Reserve, spool, spawn and reap ONE build.

        Both entrances — a confirmed spec file and a human's `/build` message —
        land here, so a build started from chat is the same build in every
        respect but what it narrates. Returns (proc, used, cap, timeout)."""
        # Build the argv (pure config + validated slug) BEFORE reserving, so a
        # misconfiguration refuses without burning a budget slot.
        argv = self._build_argv(meta["slug"], meta["build_resident"])
        timeout = int(self.start_build.get("timeout_sec", START_BUILD_DEFAULT_TIMEOUT))

        # Reserve the budget slot + claim the slug under the lock (H13-D4, BL-D4).
        used, cap = self._reserve_build(resident, meta["slug"])

        # BL-D2: the build's stdout/stderr land in 0600 temp FILES, never in pipes
        # this privileged process must drain.
        try:
            out_path, err_path, out_fh, err_fh = self._open_build_logs(meta["slug"])
        except BaseException:
            self._release_build(resident, meta["slug"])
            raise

        # The claim ticket for the transient unit, written BEFORE the launch so a
        # broker that dies mid-spawn still leaves a trail for the next process to
        # adopt (adopt_inflight_builds).
        meta["resident"] = resident
        try:
            self._write_build_sidecar(meta, out_path=out_path, err_path=err_path,
                                      timeout=timeout)
        except OSError as exc:
            self._release_build(resident, meta["slug"])
            self._close_build_logs(out_fh, err_fh)
            self._unlink_build_logs(out_path, err_path)
            raise VerbError("exec-failure",
                            f"cannot record the build: {exc}") from None

        announce(timeout)

        try:
            proc = self._build_spawn(argv, stdout=out_fh, stderr=err_fh)
        except OSError as exc:
            # Never spawned: refund the slot, drop the slug claim, delete the
            # (empty) output files.
            self._release_build(resident, meta["slug"])
            self._close_build_logs(out_fh, err_fh)
            self._unlink_build_logs(out_path, err_path)
            self._remove_build_sidecar(meta["slug"])
            launch_failed(exc)
            raise VerbError("exec-failure",
                            f"build failed to launch: {exc}") from None
        finally:
            # The child holds its own dups of these fds; the broker must not.
            self._close_build_logs(out_fh, err_fh)

        t = threading.Thread(
            target=self._reap_build,
            args=(proc, prompt.encode("utf-8"), meta, timeout, out_path, err_path),
            daemon=True)
        self._build_threads.append(t)
        t.start()
        return proc, used, cap, timeout

    def _verb_start_build(self, resident: str, args: dict) -> tuple[dict, str]:
        """Launch a DETACHED build of a CONFIRMED spec to `loop/<slug>` (WP-L4)."""
        _reject_unknown(args, {"spec"})
        spec_arg = _check_str(args, "spec", required=True, max_len=300)
        assert spec_arg is not None
        spec_path = self._resolve_spec_path(spec_arg)
        meta = self._read_confirmed_spec(spec_path)

        wake = self._active_wake(resident)
        if wake is not None:
            self._assert_woken_build_allowed(resident, wake, meta["text"])

        build_resident = build_identity_from_caller(resident)
        meta["build_resident"] = build_resident
        prompt = build_session_prompt(
            meta["text"], slug=meta["slug"], branch=meta["branch"])
        stamp: dict = {}

        def announce(timeout: int) -> None:
            stamp.update(self._stamp_spec_status(
                meta["slug"], "building",
                f"build running as {build_unit_name(meta['slug'])} -> "
                f"{meta['branch']}, launched by {build_resident} (confirmed by "
                f"{meta['confirmed_by']}, #custodian seq {meta['seq']}). Not "
                "buildable again until this line moves.",
                expect=("confirmed",)))
            # 'started' — a state transition; best-effort (a failed post must never
            # sink a launched build, and is never a heartbeat).
            self._narrate(format_build_started(
                slug=meta["slug"], branch=meta["branch"],
                confirmed_by=meta["confirmed_by"], seq=meta["seq"],
                eta_sec=timeout) + format_spec_status_note(stamp))

        def launch_failed(exc: Exception) -> None:
            unstamp = self._stamp_spec_status(
                meta["slug"], "confirmed",
                f"the launch failed before anything ran ({_status_comment_text(exc)}); "
                "no build happened, buildable again.",
                expect=("building",)) if stamp.get("ok") else {}
            self._narrate(format_build_failed(
                slug=meta["slug"], branch=meta["branch"],
                reason=f"launch failed: {exc}") + format_spec_status_note(unstamp))

        proc, used, cap, _timeout = self._launch_build(
            resident, meta, prompt, announce=announce,
            launch_failed=launch_failed)

        result = {"started": True, "branch": meta["branch"], "slug": meta["slug"],
                  "pid": getattr(proc, "pid", None),
                  # The transient unit the build runs in.
                  "unit": build_unit_name(meta["slug"]),
                  "confirmed_by": meta["confirmed_by"], "seq": meta["seq"],
                  # What happened to the spec's Status line (-> `building`).
                  # ok=False is NOT a refusal — the build runs regardless — but the
                  # caller should say so where a human will read it.
                  "spec_status": stamp}
        budget_str = f"{used}/{cap}" if cap is not None else str(used)
        # Third element = audit extras.
        return (result,
                f"build {meta['slug']} -> {meta['branch']} launched "
                f"(budget {budget_str})",
                {"build_started": True})

    # ---------------------------------------------------------- build (chat)
    # THE SECOND ENTRANCE to the same build. `start-build` reads a confirmed spec
    # file from a resident's hands; `build` reads a human's own message out of the
    # server DB and trusts nothing the caller says about it.

    def _message_row(self, channel_id: int, seq: int, columns: str,
                     join: str) -> Optional[tuple]:
        """One message row, read-only, or None. The DB is the only witness of
        what was asked for; nothing a caller says about it is used."""
        db_path = self._apps_message_db()
        if not db_path or not os.path.exists(db_path):
            raise VerbError(BUILD_REFUSED,
                            "the broker cannot read the message database, so "
                            "it cannot see what was asked for",
                            reason="human")
        try:
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                return db.execute(
                    f"select {columns} from messages m {join} "
                    "where m.channel_id = ? and m.seq = ?",
                    (channel_id, seq)).fetchone()
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise VerbError(BUILD_REFUSED,
                            f"the message database could not be read ({exc})",
                            reason="human") from None

    def _build_message(self, channel_id: int, seq: int, *,
                       code: str = BUILD_REFUSED,
                       reason: Optional[str] = None,
                       act: str = "start a build") -> dict:
        """The human's own `/build` or `/merge` message, read from the server DB."""
        try:
            row = self._message_row(
                channel_id, seq,
                "m.author_type, m.content, m.privacy_flags, m.deleted_at, "
                "u.username",
                "left join users u on u.id = m.author_id")
        except VerbError as exc:
            raise VerbError(code, exc.message, reason=reason) from None
        if row is None:
            raise VerbError(code,
                            f"there is no message {seq} in channel {channel_id}",
                            reason=reason)
        author_type, content, flags_json, deleted_at, username = row
        if deleted_at:
            raise VerbError(code, f"message {seq} was deleted", reason=reason)
        if author_type != "user":
            raise VerbError(code, f"only a person can {act}", reason=reason)
        try:
            flags = json.loads(flags_json or "{}")
        except ValueError:
            flags = {}
        if hidden_from_bots(flags):
            raise VerbError(code,
                            "that message is private, so no bot may act on it",
                            reason=reason)
        if not username:
            raise VerbError(code,
                            f"message {seq} names no account this server knows",
                            reason=reason)
        return {"author": str(username), "content": str(content or "")}

    def _assert_repo_session(self, session_id: int, author: str) -> None:
        """The caller names the session; the SERVER says whose it is.

        `session_id` arrives on the wire, so an open repo session belonging to
        the message's author is the only one a build may post its stages into."""
        try:
            view = self._apps_harness_view(session_id)
        except VerbError as exc:
            raise VerbError(BUILD_REFUSED,
                            f"build session {session_id} cannot be read "
                            f"({exc.message})") from None
        if not view.get("open"):
            raise VerbError(BUILD_REFUSED,
                            f"build session {session_id} has ended")
        if view.get("mode") != "repo":
            raise VerbError(BUILD_REFUSED,
                            f"session {session_id} is not a repo build")
        if view.get("owner_username") != author:
            raise VerbError(BUILD_REFUSED,
                            f"session {session_id} does not belong to {author}")

    def _unique_build_slug(self, slug: str) -> str:
        """The first `<slug>`, `<slug>-2`, … whose `loop/` branch the gatehouse does
        not already hold."""
        repo = self._gatehouse_repo()
        if repo is None:
            return slug
        candidate, n = slug, 1
        while self._git(repo, "rev-parse", "--verify", "--quiet",
                        f"refs/heads/loop/{candidate}").returncode == 0:
            n += 1
            if n > MAX_BUILD_SLUG_TRIES:
                raise VerbError(BUILD_REFUSED,
                                f"loop/{slug} and every suffix up to "
                                f"{MAX_BUILD_SLUG_TRIES} already exist")
            candidate = f"{slug}-{n}"
        return candidate

    def _gatehouse_repo(self) -> Optional[str]:
        """`[gate].canonical_repo`, or None when this broker has no gatehouse."""
        gate = self.config.get("gate")
        repo = gate.get("canonical_repo") if isinstance(gate, dict) else None
        if not isinstance(repo, str) or not repo or not os.path.isdir(repo):
            return None
        return repo

    def _gatehouse_diffstat(self, branch: str) -> str:
        """`git diff --shortstat main..<branch>` in the gatehouse."""
        repo = self._gatehouse_repo()
        if repo is None:
            return "no commits"
        try:
            cp = self._git(repo, "diff", "--shortstat", f"main..{branch}")
        except VerbError:
            return "no commits"
        if cp.returncode != 0:
            return "no commits"
        return " ".join((cp.stdout or "").split()) or "no commits"

    def _build_ledger_line(self, record: dict) -> None:
        """One JSON line per accepted `/build`, append-only."""
        try:
            parent = os.path.dirname(self.build_ledger)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            with open(self.build_ledger, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._audit("broker", BUILD_VERB, {"seq": record.get("seq")}, True,
                        f"build ledger unwritable: {exc}")

    def _post_to_channel(self, channel_id: int, body: str) -> None:
        """One post to a named channel, as the broker's own bot."""
        try:
            self.channel_transport(self.disjorn, int(channel_id), body)
        except Exception:  # noqa: BLE001 — a banner is legibility, not control
            pass

    def _seq_build_outcome(self, *, slug: str, branch: str, publish: dict,
                           origin: dict, report: Optional[dict] = None,
                           unit_reason: Optional[str] = None) -> None:
        """The end of a build started from chat: stage events into its session,
        then ONE banner in the channel the request came from."""
        session = int(origin.get("session_id") or 0)
        report = report or {"files": "n/a", "tests": "n/a", "diff": "n/a"}
        published = publish.get("published", [])
        # READ BEFORE THE MERGE: once loop/<slug> is in main, main..loop/<slug>
        # is empty and the diffstat would read "no commits".
        diffstat = self._gatehouse_diffstat(branch)
        # THE BANNER IS THE ONLY THING THE ROOM SEES for a repo build: no turn
        # line follows it, so a build that published nothing still says why here.
        banner = {"tests": "n/a — nothing was gated",
                  "tier": "n/a — nothing to classify",
                  "next": "no commits — /build again with more detail"}
        if build_outcome_class(publish, unit_reason) != "done":
            halted = " ".join((unit_reason or NO_HARVEST_REASON).split())[:300]
            banner["next"] = f"build halted — {halted}; /build again"
            self._apps_post_stage(session, "scoped",
                                  {"turn": 1, "halted": "error",
                                   "reason": unit_reason or NO_HARVEST_REASON})
        elif published:
            # The branch the room is watching is the platform repo's, whatever
            # else this seat was entitled to publish.
            sha = next((s for repo, s in published if repo.startswith("disjorn")),
                       published[0][1])
            banner = self._build_end_gates(slug=slug, origin=origin)
            if banner.get("folded"):
                # The diffstat above was read against the main the fold has
                # since put in the branch.
                diffstat += " (main folded in)"
            self._apps_post_stage(session, "files_written",
                                  {"turn": 1, "summary": report["files"]})
            detail = {"turn": 1, "branch": branch, "sha": sha}
            if banner.get("merged_sha"):
                detail["tier"] = banner["merged_tier"]
                detail["merged_sha"] = banner["merged_sha"]
            self._apps_post_stage(session, "deployed", detail)
        else:
            self._apps_post_stage(session, "files_written",
                                  {"turn": 1, "no_changes": True})
        self._post_to_channel(int(origin.get("channel_id") or 0),
                              format_seq_build_banner(
                                  tests=banner["tests"], tier=banner["tier"],
                                  diffstat=diffstat, next_line=banner["next"]))

    def _verb_build(self, caller: str, args: dict) -> tuple[dict, str, dict]:
        """Start a build from a human's `/build` message, named by its seq."""
        _reject_unknown(args, {"seq", "channel_id", "session_id"})
        for key in ("seq", "channel_id", "session_id"):
            if key not in args:
                raise _bad(f"missing required arg: {key}")
        seq = _check_int(args, "seq", 0, 1, MAX_SEQ)
        channel_id = _check_int(args, "channel_id", 0, 1, MAX_SEQ)
        session_id = _check_int(args, "session_id", 0, 1, MAX_SEQ)
        if not self.build_cfg:
            raise VerbError(BUILD_REFUSED,
                            "chat builds are not configured on this broker")

        message = self._build_message(channel_id, seq)
        author = message["author"]
        if author not in self.build_humans:
            raise VerbError(BUILD_REFUSED,
                            f"{author} is not on the broker's human list")
        text = strip_build_command(message["content"])
        if not text:
            raise VerbError(BUILD_REFUSED, "a build needs something to build")
        if len(text) > MAX_BUILD_TEXT_CHARS:
            raise VerbError(BUILD_REFUSED,
                            f"a build request must be at most "
                            f"{MAX_BUILD_TEXT_CHARS} characters")

        self._assert_repo_session(session_id, author)

        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        slug = slug_from_build_text(text, today)
        if not slug:
            raise VerbError(BUILD_REFUSED,
                            "a build request needs at least one word a branch "
                            "name can carry")
        slug = self._unique_build_slug(slug)
        branch = f"loop/{slug}"
        self._build_ledger_line({
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "seq": seq, "channel_id": channel_id, "author": author,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "slug": slug, "session_id": session_id})

        meta = {"slug": slug, "branch": branch, "build_resident": self.build_seat,
                "confirmed_by": author, "seq": seq,
                # Carried into the sidecar so a re-adopted build still knows
                # which room to answer in, and whose seq a self-merge cites.
                "origin": {"channel_id": channel_id, "session_id": session_id,
                           "seq": seq, "author": author}}
        prompt = build_chat_prompt(text, slug=slug, branch=branch)

        def announce(_timeout: int) -> None:
            self._apps_post_stage(session_id, "scoped",
                                  {"turn": 1,
                                   "model": self.start_build.get("model")})

        def launch_failed(exc: Exception) -> None:
            self._apps_post_stage(session_id, "scoped",
                                  {"turn": 1, "halted": "error",
                                   "reason": f"launch failed: {exc}"})

        proc, used, cap, _timeout = self._launch_build(
            SERVER_IDENTITY, meta, prompt, announce=announce,
            launch_failed=launch_failed)

        result = {"started": True, "slug": slug, "branch": branch,
                  "session_id": session_id, "pid": getattr(proc, "pid", None)}
        budget_str = f"{used}/{cap}" if cap is not None else str(used)
        return (result,
                f"build {slug} -> {branch} launched for {author} "
                f"(#{channel_id} seq {seq}, budget {budget_str})",
                {"build_started": True, "build_seat": f"res-{self.build_seat}"})
