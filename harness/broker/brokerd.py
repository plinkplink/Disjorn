#!/usr/bin/env python3
"""disjorn-broker — the privileged verb gateway for residents (WP-H3).

Residents (res-claudette, res-gable) live in rootless containers with no sudo
and a walled network. The ONLY way anything privileged happens on their behalf
is through this daemon: a unix-socket server whose caller identity comes from
SO_PEERCRED (kernel-asserted uid), never from anything the caller says.

Governance rules encoded here (AGENTHOOD.md / HARNESS-PLAN.md WP-H3):

* Kill switches: every verb is per-resident toggleable in verbs.toml, which is
  plink-owned and lives OUTSIDE both containers. Toggles default to OFF and
  verbs.toml is re-read on every request, so flipping a switch needs no broker
  restart.
* Chat is data, never authorization: nothing in a request body can widen what
  a caller may do. Identity = uid via SO_PEERCRED; permission = verbs.toml.
* No self-restart: there is deliberately NO `restart-self` verb, and no verb
  whose argv a caller can redirect at the broker or a resident's own process.
* No free-form shell, ever: every subprocess runs a fixed argv list
  (config-supplied list + individually validated scalar args appended by the
  handler). The shell-enabled subprocess mode is never used in this file.
* Total audit: every call — allowed, denied, or malformed — appends exactly
  one JSON line {ts, resident, verb, args, allowed, result_summary} to the
  audit log.

Config: /etc/disjorn-broker/broker.toml + verbs.toml (templates alongside this
file). Paths overridable for tests via DISJORN_BROKER_CONFIG /
DISJORN_BROKER_VERBS or --config/--verbs.

Runs as plink (not root) under systemd; the single privileged escape hatch is
the sudoers line in harness/keyboard/90-disjorn-broker.sudoers allowing exactly
`sudo -n systemctl restart disjorn`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import tomllib
from typing import Any, Callable, Optional

DEFAULT_CONFIG_PATH = "/etc/disjorn-broker/broker.toml"
DEFAULT_VERBS_PATH = "/etc/disjorn-broker/verbs.toml"
ENV_CONFIG = "DISJORN_BROKER_CONFIG"
ENV_VERBS = "DISJORN_BROKER_VERBS"

DEFAULT_SOCKET_PATH = "/run/disjorn-broker.sock"  # per HARNESS-PLAN; the
# shipped broker.toml template uses /run/disjorn-broker/broker.sock instead so
# the daemon can run unprivileged under systemd RuntimeDirectory=.

MAX_REQUEST_BYTES = 64 * 1024  # one request line; anything bigger is hostile
MAX_PROPOSAL_CHARS = 4000
MAX_LOG_LINES = 500
MAX_AUDIT_ENTRIES = 500
MAX_GREP_CHARS = 200
MAX_GATES_JSON = 8192
SUBPROCESS_TIMEOUTS = {  # seconds, per verb
    "restart-disjorn": 60,
    "run-server-tests": 900,
    "classify-diff": 120,
    "read-prod-logs": 30,
    "refresh-mirror": 120,
}

# start-build (WP-L4): the detached build is NOT a synchronous _run() call, so
# its wall-clock cap lives in config ([start_build].timeout_sec), not the dict
# above; this is only the fallback when config omits it. Longer than the 300s
# summon on purpose — a build is a whole feature, not a chat turn.
START_BUILD_DEFAULT_TIMEOUT = 3600
# Ratified default (BUILD-LOOP.md): builds are CAPPED by default (2/day), unlike
# the WP-H12 action budget which ships OFF. plink tunes at staging time.
DEFAULT_DAILY_BUILD_CAP = 2
MAX_SPEC_BYTES = 64 * 1024  # a spec is a short markdown doc; bigger is hostile

_RANGE_RE = re.compile(r"^[A-Za-z0-9._~^/{}-]{1,200}$")  # git rev / range; no
# whitespace, no leading dash (checked separately) — can never be read as a flag
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A spec slug also names the build branch (loop/<slug>) and rides argv as a
# positional, so it is held to a strict branch/argv-safe kebab charset — it can
# never be read as a flag or a path segment.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SPEC_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")  # SPECS/YYYY-MM-DD-<slug>.md


class VerbError(Exception):
    """A verb failed or a request was rejected. code -> PROTOCOL.md error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _bad(msg: str) -> VerbError:
    return VerbError("bad-args", msg)


# --------------------------------------------------------------------------
# Argument validation.  Every verb has an explicit schema; unknown keys are
# rejected; every value is type- and range-checked before a handler sees it.
# --------------------------------------------------------------------------

def _check_int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = args.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool):
        raise _bad(f"{key} must be an integer")
    if not lo <= v <= hi:
        raise _bad(f"{key} must be between {lo} and {hi}")
    return v


def _check_str(args: dict, key: str, *, required: bool = False,
               max_len: int = 1000) -> Optional[str]:
    v = args.get(key)
    if v is None:
        if required:
            raise _bad(f"missing required arg: {key}")
        return None
    if not isinstance(v, str):
        raise _bad(f"{key} must be a string")
    if not 1 <= len(v) <= max_len:
        raise _bad(f"{key} length must be 1..{max_len}")
    return v


def _reject_unknown(args: dict, allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise _bad(f"unknown args: {sorted(unknown)}")


def _check_date(args: dict, key: str) -> str:
    v = _check_str(args, key, required=True, max_len=10)
    assert v is not None
    if not _DATE_RE.match(v):
        raise _bad(f"{key} must be YYYY-MM-DD")
    try:
        _dt.date.fromisoformat(v)
    except ValueError as exc:
        raise _bad(f"{key}: {exc}") from None
    return v


# --------------------------------------------------------------------------
# Default file-proposal transport: post to #custodian via the Disjorn SDK as
# the broker's own bot identity.  Kept behind a callable so tests stub it.
# --------------------------------------------------------------------------

def _sdk_transport(disjorn_cfg: dict, body: str) -> dict:
    """POST body to the configured custodian channel. Returns {seq, message_id}."""
    import asyncio

    from disjorn_sdk import DisjornClient  # deferred import: not needed in tests

    url = disjorn_cfg["url"]
    channel_id = int(disjorn_cfg["custodian_channel_id"])
    with open(disjorn_cfg["api_key_path"], "r", encoding="utf-8") as fh:
        api_key = fh.read().strip()

    async def _post() -> dict:
        client = DisjornClient(url, api_key=api_key)
        try:
            msg = await client.send(channel_id, body)
        finally:
            await client.aclose()
        return {"seq": msg.get("seq"), "message_id": msg.get("id")}

    return asyncio.run(_post())


# --------------------------------------------------------------------------
# start-build (WP-L4): spec parsing, slug/branch derivation, the build-session
# prompt, and #custodian narration. Pure functions — no I/O, no broker state —
# so the confirm gate, the slug rules, and every narration shape are unit-
# testable in isolation, exactly like the argv validators above.
# --------------------------------------------------------------------------

def _clean_field(value: str) -> Optional[str]:
    """A spec field value, or None if it is blank or still the TEMPLATE.md
    placeholder (angle-bracketed `<...>`). This is how "the confirm record is
    unfilled" is detected mechanically — a spec left with `<username>` in the
    box has no confirm record, whatever it looks like at a glance."""
    v = value.strip()
    if not v or v in {"-", "_"}:
        return None
    if v.startswith("<") and v.endswith(">"):
        return None
    return v


def parse_spec_status(text: str) -> Optional[str]:
    """The status token under `## Status` (e.g. 'confirmed'), lowercased, or
    None if the section is absent. Backticks and HTML comments are ignored —
    TEMPLATE.md writes the token as `` `confirmed` `` trailed by a comment."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == "## status":
            for follow in lines[i + 1:]:
                s = follow.strip()
                if not s or s.startswith("<!--"):
                    continue
                if s.startswith("#"):  # next heading, no value in the section
                    return None
                return s.strip("`").strip().lower()
            return None
    return None


def parse_confirm_record(text: str) -> dict:
    """`{confirmed_by, seq}` from the `## Confirm record` section. A field that
    is blank or still the `<...>` placeholder comes back None — mechanically,
    that IS "no confirm record". `seq` is the witnessing #custodian sequence as
    an int (or None). Chat is data: the broker verifies this record, it never
    trusts a caller's word that a build was confirmed."""
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
        m = re.match(r"\s*-\s*\*\*Confirmed by\*\*:\s*(.*)$", line)
        if m:
            out["confirmed_by"] = _clean_field(m.group(1))
        m = re.match(r"\s*-\s*\*\*#custodian seq\*\*:\s*(.*)$", line)
        if m:
            raw = _clean_field(m.group(1))
            if raw is not None:
                digits = re.search(r"\d+", raw)
                out["seq"] = int(digits.group()) if digits else None
    return out


def slug_from_spec_filename(filename: str) -> str:
    """`SPECS/YYYY-MM-DD-<slug>.md` -> `<slug>` (branch = loop/<slug>). The
    date prefix is stripped and the remainder must be a strict kebab slug —
    anything else is bad-args, because this string ends up as a git branch and
    an argv positional."""
    base = os.path.basename(filename)
    if base.endswith(".md"):
        base = base[:-3]
    slug = _SPEC_DATE_PREFIX_RE.sub("", base)
    if not _SLUG_RE.match(slug):
        raise _bad(f"spec filename does not yield a valid slug: {base!r} "
                   "(expected SPECS/YYYY-MM-DD-<kebab-slug>.md)")
    return slug


def build_session_prompt(spec_text: str, *, slug: str, branch: str) -> str:
    """The instruction preamble + the committed spec, fed to the build session
    on STDIN. ALL of it is data on stdin — argv stays config-only (launcher
    doctrine): only the mechanically-validated slug/branch and fixed broker
    text vary here, and the branch/no-merge/no-push rules are stated where the
    session actually reads them."""
    return (
        f"You are a Disjorn build session. Build exactly what the spec below "
        f"describes, on a NEW git branch named `{branch}` in your worktree.\n"
        f"Hard rules (non-negotiable):\n"
        f"- Do NOT merge. Do NOT push. Do NOT touch the production service.\n"
        f"- Everything you do lands on `{branch}` and waits there for a human.\n"
        f"- Narrate STATE TRANSITIONS ONLY to #custodian (channel 4): each\n"
        f"  checkpoint you choose to mark. No heartbeats, no timers — go quiet\n"
        f"  between transitions, and fail loud if you get stuck.\n"
        f"- When done, print a final JSON object on stdout with keys "
        f'"files" (paths touched), "tests" (what you ran + result), '
        f'"diff" (one-line summary), "branch".\n\n'
        f"--- SPEC ({slug}) ---\n{spec_text}"
    )


def _parse_build_report(stdout: str) -> dict:
    """Best-effort structured report from the build session's stdout for the
    'done' line. The session is asked to end with a JSON object
    {files, tests, diff, branch}; we surface those and degrade to 'n/a' (or a
    text tail) if it didn't. Tier is intentionally NOT computed here — see
    format_build_done: classify-diff is a separate verb, not coupled in."""
    text = stdout.strip()
    files = tests = diff = "n/a"
    data: Any = None
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
    if isinstance(data, dict):
        inner = data
        for key in ("build_report", "report", "result", "reply"):
            v = data.get(key)
            if isinstance(v, dict):
                inner = v
                break
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                except json.JSONDecodeError:
                    continue
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


def format_build_started(*, slug: str, branch: str, confirmed_by: str,
                         seq: int, eta_sec: int) -> str:
    """The 'started' state-transition line. Names the spec, the branch, who
    confirmed it + the witnessing seq, and an ETA GUESS (the wall-clock cap, a
    ceiling not a promise). Plain text, greppable, no emoji — same house idiom
    as the summon summaries."""
    eta_min = max(1, eta_sec // 60)
    return (f"build started | {slug} -> {branch} | "
            f"confirmed by {confirmed_by} (#custodian seq {seq}) | "
            f"ETA <= {eta_min}m (guess) | no merge, no push — lands on the branch")


def format_build_done(*, slug: str, branch: str, files: str, tests: str,
                      diff: str, tier: str = "pending") -> str:
    """The 'done' state-transition line: files touched, tests run/result, a
    one-line diff summary, the branch, and the advisory tier. Tier is 'pending'
    by default — the reaper does not invoke classify-diff (a separate verb,
    ships OFF); a human runs it on the branch. Nothing merged, ever."""
    return (f"build done | {slug} -> {branch} | tier {tier} | "
            f"files: {files} | tests: {tests} | diff: {diff} | "
            f"on the branch for review — nothing merged")


def format_build_failed(*, slug: str, branch: str, reason: str) -> str:
    """The 'failed' state-transition line — LOUD. A stalled build goes quiet
    then lands here (never a heartbeat); the branch keeps whatever exists and a
    human is told to look."""
    return (f"BUILD FAILED | {slug} -> {branch} | {reason} | "
            f"the branch holds what there is — a human should look")


# --------------------------------------------------------------------------
# The broker.
# --------------------------------------------------------------------------

class Broker:
    """Unix-socket verb broker. Construct with parsed broker.toml + a path to
    verbs.toml (re-read per request — that's the kill-switch property)."""

    def __init__(
        self,
        config: dict,
        verbs_path: str,
        *,
        transport: Optional[Callable[[dict, str], dict]] = None,
        build_spawn: Optional[Callable[[list[str]], Any]] = None,
    ) -> None:
        self.config = config
        self.verbs_path = verbs_path
        self.transport = transport or _sdk_transport
        # How a detached build session is launched. Injected in tests (mock the
        # exec); prod uses _default_build_spawn (a detached, un-waited Popen).
        self._build_spawn = build_spawn or self._default_build_spawn
        broker_cfg = config.get("broker", {})
        self.socket_path: str = broker_cfg.get("socket_path", DEFAULT_SOCKET_PATH)
        self.audit_path: str = broker_cfg["audit_log"]
        # uid map: TOML keys are strings; normalise to int -> resident name.
        self.uid_map: dict[int, str] = {
            int(uid): name for uid, name in config.get("uids", {}).items()
        }
        self.residents: dict[str, dict] = config.get("residents", {})
        self.commands: dict[str, Any] = config.get("commands", {})
        self.paths: dict[str, str] = config.get("paths", {})
        self.disjorn: dict[str, Any] = config.get("disjorn", {})
        # Daily per-resident action budget (WP-H12). Loaded at construction;
        # a cap change needs a broker restart (unlike verbs.toml kill switches,
        # which are re-read live). Default: no cap == OFF. Instrument first.
        self.budgets: dict[str, Any] = config.get("budgets", {})
        # start-build (WP-L4) config: the detached build-session launch contract
        # (command + session_argv + model pin), the SPECS/ dir the confirm gate
        # reads, the wall-clock cap, and the per-day build budget.
        self.start_build: dict[str, Any] = config.get("start_build", {})
        self._audit_lock = threading.Lock()
        # Build-budget lock (H13-D4): count-with-reservation is held under this,
        # so two concurrent start-builds can NEVER both slip past the cap — the
        # check-then-act race the red-team flagged is closed here.
        self._build_lock = threading.Lock()
        # Per-resident build reservations for the day: resident -> (utc_date,
        # count). Seeded lazily from the audit log per day, then authoritative
        # in memory (never re-read, so in-flight builds are never double-counted).
        self._builds: dict[str, tuple[Optional[str], int]] = {}
        # Detached build reaper threads, kept ONLY so tests can join them;
        # production never waits on a build — detachment is the whole point.
        self._build_threads: list[threading.Thread] = []
        self._listener: Optional[socket.socket] = None
        self._closed = False

        # The verb table.  Adding a verb here is a deliberate act; there is no
        # dynamic registration and — enforced by test — no "restart-self".
        self.verbs: dict[str, Callable[[str, dict], tuple[dict, str]]] = {
            "restart-disjorn": self._verb_restart_disjorn,
            "run-server-tests": self._verb_run_server_tests,
            "refresh-mirror": self._verb_refresh_mirror,
            "start-build": self._verb_start_build,
            "classify-diff": self._verb_classify_diff,
            "read-prod-logs": self._verb_read_prod_logs,
            "read-own-log": self._verb_read_own_log,
            "read-metrics": self._verb_read_metrics,
            "file-proposal": self._verb_file_proposal,
            "query-own-audit": self._verb_query_own_audit,
        }

    # ------------------------------------------------------------- audit

    def _audit(self, resident: str, verb: str, args: Any, allowed: bool,
               result_summary: str) -> None:
        rec = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "resident": resident,
            "verb": verb,
            "args": args,
            "allowed": allowed,
            "result_summary": result_summary[:500],
        }
        line = json.dumps(rec, default=str, ensure_ascii=False)
        with self._audit_lock:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # -------------------------------------------------------------- budget

    def _daily_action_cap(self, resident: str) -> Optional[int]:
        """Per-resident daily action cap from `[budgets]`, or None (off).
        `[budgets.<resident>].daily_action_cap` wins; else
        `[budgets].default_daily_action_cap`; else None."""
        per = self.budgets.get(resident)
        if isinstance(per, dict) and isinstance(per.get("daily_action_cap"), int):
            return per["daily_action_cap"]
        default = self.budgets.get("default_daily_action_cap")
        return default if isinstance(default, int) else None

    def _count_today_allowed(self, resident: str) -> int:
        """How many ALLOWED actions this resident has today (UTC), read from
        the audit log — the same source the metrics producer aggregates, so
        the count is authoritative and restart-proof. Denials never count."""
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        n = 0
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    if resident not in raw:  # safe prefilter: name is in the JSON
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if (rec.get("resident") == resident and rec.get("allowed") is True
                            and str(rec.get("ts", ""))[:10] == today):
                        n += 1
        except OSError:
            return 0
        return n

    # -------------------------------------------------------- build budget

    def _daily_build_cap(self, resident: str) -> Optional[int]:
        """Per-day build cap for a resident.
        `[start_build.per_resident.<r>].daily_build_cap` wins; else
        `[start_build].daily_build_cap`; else the ratified default of 2. Builds
        are capped by DEFAULT (BUILD-LOOP.md), unlike the WP-H12 action budget:
        the blast radius of an autonomous build is a whole branch of tokens."""
        per = self.start_build.get("per_resident")
        if isinstance(per, dict):
            r = per.get(resident)
            if isinstance(r, dict) and isinstance(r.get("daily_build_cap"), int):
                return r["daily_build_cap"]
        cap = self.start_build.get("daily_build_cap", DEFAULT_DAILY_BUILD_CAP)
        return cap if isinstance(cap, int) else DEFAULT_DAILY_BUILD_CAP

    def _count_builds_today(self, resident: str, today: str) -> int:
        """Allowed start-build audit lines for this resident today (UTC). Used
        ONLY to seed the in-memory reservation counter once per day; after
        seeding the counter is authoritative, so a build launched this process
        (already reserved in memory, not yet reflected here until dispatch
        writes its line) is never counted twice."""
        n = 0
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    if "start-build" not in raw or resident not in raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if (rec.get("resident") == resident
                            and rec.get("verb") == "start-build"
                            and rec.get("allowed") is True
                            and str(rec.get("ts", ""))[:10] == today):
                        n += 1
        except OSError:
            return 0
        return n

    def _reserve_build(self, resident: str) -> tuple[int, Optional[int]]:
        """Race-safe build-budget check + reservation (H13-D4: count-with-
        reservation under a lock, NEVER check-then-act on the audit file).
        Under one lock: seed the day's count from the audit log if unseen,
        refuse at/over the cap, else reserve a slot and return (used_after,
        cap). Because the lock spans count AND reserve, concurrent start-builds
        can never both pass a cap of N."""
        cap = self._daily_build_cap(resident)
        with self._build_lock:
            today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
            date, count = self._builds.get(resident, (None, 0))
            if date != today:  # first build this UTC day (or after restart)
                count = self._count_builds_today(resident, today)
            if cap is not None and count >= cap:
                self._builds[resident] = (today, count)
                raise VerbError("over-budget",
                                f"daily build budget of {cap} reached for {resident}")
            self._builds[resident] = (today, count + 1)
            return count + 1, cap

    def _release_build(self, resident: str) -> None:
        """Refund a reservation when the launch itself never started (a build
        that ran and then failed keeps its slot — it burned the attempt)."""
        with self._build_lock:
            date, count = self._builds.get(resident, (None, 0))
            if count > 0:
                self._builds[resident] = (date, count - 1)

    def join_builds(self, timeout: float = 5.0) -> None:
        """Join detached build reaper threads — TEST convenience only.
        Production never waits on a build (detachment is the whole point)."""
        for t in list(self._build_threads):
            t.join(timeout)

    # --------------------------------------------------------------- core

    def dispatch(self, uid: int, verb: Any, args: Any) -> dict:
        """Authorize + execute one request. Always writes exactly one audit line."""
        resident = self.uid_map.get(uid)
        caller = resident if resident is not None else f"uid:{uid}"

        if not isinstance(verb, str) or not isinstance(args, dict):
            self._audit(caller, str(verb)[:100], args, False, "denied: malformed request")
            return self._err("bad-args", "request must be {verb: str, args: object}")

        if resident is None:
            self._audit(caller, verb, args, False, "denied: unknown caller uid")
            return self._err("unknown-caller", f"uid {uid} is not a configured resident")

        if verb not in self.verbs:
            self._audit(caller, verb, args, False, "denied: unknown verb")
            return self._err("unknown-verb", f"no such verb: {verb}")

        # Kill switch: fresh read of verbs.toml on every request; missing file,
        # missing resident section or missing key all mean OFF (fail closed).
        try:
            with open(self.verbs_path, "rb") as fh:
                verbs_cfg = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            self._audit(caller, verb, args, False, "denied: verbs.toml unreadable")
            return self._err("internal", "verb configuration unavailable")
        if verbs_cfg.get(resident, {}).get(verb, False) is not True:
            self._audit(caller, verb, args, False, "denied: verb disabled for resident")
            return self._err("verb-disabled", f"{verb} is not enabled for {resident}")

        # Daily per-resident action budget (WP-H12). Default OFF: with no cap
        # configured this never denies. Counts today's ALLOWED actions for this
        # resident from the audit log, so the count survives a broker restart;
        # the (cap+1)-th action is denied and audited like any other denial.
        # Additive and permissive by default — instrument first, tune from
        # observed data (AGENTHOOD budget rule), never from imagined abuse.
        cap = self._daily_action_cap(resident)
        if cap is not None and self._count_today_allowed(resident) >= cap:
            self._audit(caller, verb, args, False,
                        f"denied: over daily action budget ({cap})")
            return self._err("over-budget",
                             f"daily action budget of {cap} reached for {resident}")

        try:
            result, summary = self.verbs[verb](resident, args)
        except VerbError as exc:
            # A denial (the verb never ran) audits allowed=False; an authorized
            # run that failed audits allowed=True. bad-args is a denial; so is a
            # handler-raised over-budget (e.g. the WP-L4 build budget, refused
            # before any launch) — neither reached execution.
            allowed = exc.code not in ("bad-args", "over-budget")
            self._audit(caller, verb, args, allowed,
                        f"{'error' if allowed else 'denied'}: {exc.message}")
            return self._err(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 — never crash the daemon on a verb
            self._audit(caller, verb, args, True, f"error: internal: {exc!r}")
            return self._err("internal", "internal broker error")

        self._audit(caller, verb, args, True, summary)
        return {"ok": True, "verb": verb, "result": result}

    @staticmethod
    def _err(code: str, message: str) -> dict:
        return {"ok": False, "error": {"code": code, "message": message}}

    # ---------------------------------------------------------- subprocess

    def _argv(self, key: str, default: list[str]) -> list[str]:
        argv = self.commands.get(key, default)
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise VerbError("internal", f"commands.{key} must be a list of strings")
        return list(argv)

    def _run(self, argv: list[str], timeout: int,
             cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        # Fixed argv list, shell NEVER involved.
        try:
            return subprocess.run(  # noqa: S603 — argv list, no shell
                argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            raise VerbError("exec-failure", f"command timed out after {timeout}s") from None
        except OSError as exc:
            raise VerbError("exec-failure", f"command failed to start: {exc}") from None

    # -------------------------------------------------------------- verbs
    # Each returns (result_dict, audit_summary).

    def _verb_restart_disjorn(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, set())
        # `sudo -n`: never prompts; works only because of the single sudoers
        # line installed by harness/keyboard/04-broker.sh.
        argv = self._argv("restart_disjorn",
                          ["sudo", "-n", "systemctl", "restart", "disjorn"])
        cp = self._run(argv, SUBPROCESS_TIMEOUTS["restart-disjorn"])
        out = (cp.stdout + cp.stderr).strip()[-2000:]
        return ({"exit_code": cp.returncode, "output": out},
                f"exit={cp.returncode}")

    def _verb_run_server_tests(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, set())
        argv = self._argv("run_server_tests", [
            "/home/plink/Disjorn/Disjorn/server/.venv/bin/python",
            "-m", "pytest", "tests", "-q",
        ])
        cwd = self.commands.get("run_server_tests_cwd",
                                "/home/plink/Disjorn/Disjorn/server")
        cp = self._run(argv, SUBPROCESS_TIMEOUTS["run-server-tests"], cwd=cwd)
        lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
        summary = lines[-1] if lines else "(no output)"
        return ({"exit_code": cp.returncode, "summary": summary},
                f"exit={cp.returncode}: {summary}"[:300])

    def _verb_refresh_mirror(self, resident: str, args: dict) -> tuple[dict, str]:
        """Fast-forward the shared read-only repo mirror to the canonical
        repo's main. The mirror is the ONLY view of the repo residents have
        (bind-mounted RO into each container), and nothing else ever fetches
        into it — host commits don't cross the wall until this runs. Zero
        caller args; every argv is fixed config, so a resident can refresh
        the mirror but can never aim git anywhere else. `--ff-only` on the
        update: a diverged mirror fails loudly and stays plink's to resolve."""
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
        head = _head()
        return ({"head": head, "before": before, "updated": head != before},
                f"mirror at {head}" + ("" if head == before else f" (was {before})"))

    # ------------------------------------------------------------ start-build

    def _specs_dir(self) -> str:
        d = self.start_build.get("specs_dir")
        if not d or not isinstance(d, str):
            raise VerbError("internal", "start_build.specs_dir is not configured")
        return d

    def _resolve_spec_path(self, spec: str) -> str:
        """Map caller input to a real spec file, CONFINED to the configured
        SPECS/ dir. realpath() resolves BOTH `..` traversal and symlink escape,
        then we require the resolved file to sit DIRECTLY in SPECS/ (the flat
        one-file-per-spec layout) and end in .md. A caller can never point the
        builder outside SPECS/ — not with `..`, not through a planted symlink,
        not with an absolute path. The path is caller input; the confinement is
        the broker's, verified mechanically."""
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
        """Read + validate the spec at `path`: status must be 'confirmed' and
        the confirm record must be filled (Confirmed by + #custodian seq).
        No confirm record -> refuse, fail-loud. Returns the fields the launch
        and narration need. The verbs.toml toggle authorizes the CLASS (this
        resident may build); THIS record selects the instance and the broker
        verifies it — chat is data, never authorization."""
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

    def _build_argv(self, slug: str) -> list[str]:
        """The detached build command — a PURE function of config + the
        validated slug. Mirrors the summon launcher's contract
        (launcher.build_argv):
            [*command, resident, slug, *session_argv, "--model", model]
        Only fixed config and the mechanically-validated kebab slug (branch/
        argv-safe) reach argv; the spec — the chat-derived design — rides on
        STDIN. The model pin is WP-L5's idiom: appended as `--model <id>`,
        forwarded by run-build.sh through the bash wrapper's "$@", with NO
        fallback (a blank pin is config drift and fails loud here, never
        silently rides the account default)."""
        command = self.start_build.get("command", [])
        if (not isinstance(command, list) or not command
                or not all(isinstance(a, str) for a in command)):
            raise VerbError("internal",
                            "start_build.command must be a non-empty list of strings")
        resident_arg = str(self.start_build.get("resident", "gable"))
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

    def _default_build_spawn(self, argv: list[str]) -> subprocess.Popen:
        """Launch the build DETACHED so it outlives this request.
        `start_new_session=True` puts it in its OWN session/process group — a
        broker signal to its own foreground group never reaches it — and the
        broker does NOT wait: a daemon reaper feeds the spec on stdin, holds the
        wall-clock cap, and narrates the terminal transition. Fixed argv, shell
        NEVER involved (same discipline as _run)."""
        return subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def _narrate(self, body: str) -> None:
        """Post a build state-transition line to #custodian via the broker's
        OWN bot identity — the same transport file-proposal uses. Best-effort:
        a posting failure never crashes a build (the audit line still lands).
        STATE TRANSITIONS ONLY — this is never called on a timer."""
        try:
            self.transport(self.disjorn, body)
        except Exception:  # noqa: BLE001 — narration is legibility, not control
            pass

    def _reap_build(self, proc: Any, spec_bytes: bytes, meta: dict,
                    timeout: int) -> None:
        """Detached-build lifecycle END (runs in a daemon thread; the request
        returned long ago). Feed the spec on stdin, wait up to the wall-clock
        cap, then narrate the terminal state transition — done or failed. No
        intermediate posts: a build that stalls goes quiet and fails loud at the
        cap (BUILD-LOOP: never timer-driven)."""
        slug, branch = meta["slug"], meta["branch"]
        try:
            out, err = proc.communicate(spec_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.communicate()
            except Exception:  # noqa: BLE001 — already reaping
                pass
            self._narrate(format_build_failed(
                slug=slug, branch=branch,
                reason=f"timed out after {timeout}s — killed"))
            return
        except Exception as exc:  # noqa: BLE001 — broken pipe etc. = a failure
            self._narrate(format_build_failed(
                slug=slug, branch=branch, reason=f"build error: {exc!r}"))
            return

        out_s = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out or "")
        err_s = err.decode("utf-8", "replace") if isinstance(err, (bytes, bytearray)) else str(err or "")
        rc = getattr(proc, "returncode", None)
        if rc is not None and rc != 0:
            self._narrate(format_build_failed(
                slug=slug, branch=branch,
                reason=f"exit {rc}: {(err_s or out_s).strip()[:400]}"))
            return
        report = _parse_build_report(out_s)
        self._narrate(format_build_done(
            slug=slug, branch=branch, files=report["files"],
            tests=report["tests"], diff=report["diff"], tier="pending"))

    def _verb_start_build(self, resident: str, args: dict) -> tuple[dict, str]:
        """Launch a DETACHED build of a CONFIRMED spec to `loop/<slug>` (WP-L4).

        The gate, in order and all mechanical (chat is data, never
        authorization — the verbs.toml toggle authorizes the CLASS, this
        resident may run builds; the confirm record in the file selects the
        INSTANCE and the broker verifies it, never trusts it):
          1. the spec path resolves inside SPECS/ (no `..`, no symlink escape);
          2. the spec's status is 'confirmed' with a real confirm record
             (Confirmed by + #custodian seq) — else refuse, fail-loud;
          3. the per-day build budget has a free slot (race-safe reservation).
        On accept it posts a 'started' line to #custodian, spawns the build
        detached (own session; outlives this request), and returns immediately.
        A daemon reaper feeds the spec on stdin, holds the wall-clock cap, and
        narrates done/failed. The build lands on the branch; NOTHING merges,
        pushes, or touches production."""
        _reject_unknown(args, {"spec"})
        spec_arg = _check_str(args, "spec", required=True, max_len=300)
        assert spec_arg is not None
        spec_path = self._resolve_spec_path(spec_arg)
        meta = self._read_confirmed_spec(spec_path)

        # Build the argv (pure config + validated slug) BEFORE reserving, so a
        # misconfiguration refuses without burning a budget slot.
        argv = self._build_argv(meta["slug"])
        timeout = int(self.start_build.get("timeout_sec", START_BUILD_DEFAULT_TIMEOUT))
        prompt = build_session_prompt(
            meta["text"], slug=meta["slug"], branch=meta["branch"])

        # Reserve the budget slot under the lock (H13-D4). A refusal here is
        # audited over-budget like any other denial.
        used, cap = self._reserve_build(resident)

        # 'started' — a state transition; best-effort (a failed post must never
        # sink a launched build, and is never a heartbeat).
        self._narrate(format_build_started(
            slug=meta["slug"], branch=meta["branch"],
            confirmed_by=meta["confirmed_by"], seq=meta["seq"], eta_sec=timeout))

        try:
            proc = self._build_spawn(argv)
        except OSError as exc:
            self._release_build(resident)  # never spawned; refund the slot
            self._narrate(format_build_failed(
                slug=meta["slug"], branch=meta["branch"],
                reason=f"launch failed: {exc}"))
            raise VerbError("exec-failure",
                            f"build failed to launch: {exc}") from None

        t = threading.Thread(
            target=self._reap_build,
            args=(proc, prompt.encode("utf-8"), meta, timeout),
            daemon=True)
        self._build_threads.append(t)
        t.start()

        result = {"started": True, "branch": meta["branch"], "slug": meta["slug"],
                  "pid": getattr(proc, "pid", None),
                  "confirmed_by": meta["confirmed_by"], "seq": meta["seq"]}
        budget_str = f"{used}/{cap}" if cap is not None else str(used)
        return (result,
                f"build {meta['slug']} -> {meta['branch']} launched "
                f"(budget {budget_str})")

    def _verb_classify_diff(self, resident: str, args: dict) -> tuple[dict, str]:
        """Contract with harness/classifier/classify_diff.py (WP-H4):
        argv: <classify_diff.py> --repo <abs path> --range <git range>
              --config <protected-paths.toml> --gates <json object>;
        stdout: one JSON object (the classification), exit 0. Anything else
        is exec-failure. --config comes from broker config, never from the
        caller — the classifier config is protected by placement."""
        _reject_unknown(args, {"repo", "range", "gates"})
        repo = _check_str(args, "repo", required=True, max_len=300)
        assert repo is not None
        if not repo.startswith("/") or "/../" in repo or repo.endswith("/.."):
            raise _bad("repo must be an absolute path without ..")
        rng = _check_str(args, "range", required=True, max_len=200)
        assert rng is not None
        if rng.startswith("-") or not _RANGE_RE.match(rng):
            raise _bad("range must be a plain git rev/range "
                       "(letters, digits, . _ ~ ^ / { } -, no leading dash)")
        # WP-H13 F3: the classifier splits A..B (or A...B) and hands each side
        # to git as a bare positional. A leading '-' on the WHOLE string is
        # rejected above, but the RIGHT side after the split can still start
        # with '-' (e.g. "main..--exit-code") and reach git as a flag. Reject
        # a leading dash on EITHER side of the split — no ref legitimately
        # starts with one.
        for _side in rng.replace("...", "..").split(".."):
            if _side.startswith("-"):
                raise _bad("neither side of the range may start with '-'")
        # Residents pass THEIR view of the filesystem; the broker runs
        # host-side where those paths don't exist. [residents.<r>.path_map]
        # translates container prefixes to host paths (longest prefix wins)
        # AND is the allowlist: a repo outside every mapped root is rejected,
        # so a resident can only ever point the classifier at repos
        # deliberately exposed to them.
        #
        # WP-H13 F2: absent map now FAILS CLOSED. It used to pass the caller's
        # repo through verbatim, so a resident configured without a map could
        # aim git at any host path the broker uid can read. A resident allowed
        # to classify must have an explicit map; no map = no classify.
        path_map = self.residents.get(resident, {}).get("path_map")
        if not path_map:
            raise _bad(f"no classify-diff path_map configured for {resident}; "
                       "classify-diff requires an explicit repo allowlist")
        best = max((p for p in path_map
                    if repo == p or repo.startswith(p.rstrip("/") + "/")),
                   key=len, default=None)
        if best is None:
            raise _bad(f"repo not under a mapped root for {resident}; "
                       f"available roots: {sorted(path_map)}")
        repo = path_map[best].rstrip("/") + repo[len(best.rstrip("/")):]
        gates = args.get("gates", {})
        if not isinstance(gates, dict):
            raise _bad("gates must be an object")
        gates_json = json.dumps(gates, ensure_ascii=False)
        if len(gates_json) > MAX_GATES_JSON:
            raise _bad(f"gates JSON exceeds {MAX_GATES_JSON} bytes")
        classifier = self.paths.get(
            "classifier",
            "/home/plink/Disjorn/Disjorn/harness/classifier/classify_diff.py")
        protected = self.paths.get(
            "protected_paths",
            "/home/plink/Disjorn/Disjorn/harness/classifier/protected-paths.toml")
        argv = self._argv("classify_diff", [sys.executable, classifier])
        argv += ["--repo", repo, "--range", rng,
                 "--config", protected, "--gates", gates_json]
        cp = self._run(argv, SUBPROCESS_TIMEOUTS["classify-diff"])
        if cp.returncode != 0:
            raise VerbError("exec-failure",
                            f"classifier exit {cp.returncode}: {cp.stderr.strip()[:500]}")
        try:
            classification = json.loads(cp.stdout)
        except json.JSONDecodeError:
            raise VerbError("exec-failure", "classifier emitted non-JSON output") from None
        tier = classification.get("tier") if isinstance(classification, dict) else None
        return ({"classification": classification}, f"classified: tier={tier}")

    def _verb_read_prod_logs(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, {"lines"})
        lines = _check_int(args, "lines", 100, 1, MAX_LOG_LINES)
        argv = self._argv("read_prod_logs",
                          ["journalctl", "-u", "disjorn", "--no-pager", "-o", "short-iso"])
        argv += ["-n", str(lines)]
        cp = self._run(argv, SUBPROCESS_TIMEOUTS["read-prod-logs"])
        if cp.returncode != 0:
            raise VerbError("exec-failure",
                            f"journalctl exit {cp.returncode}: {cp.stderr.strip()[:300]}")
        out = cp.stdout.splitlines()[-lines:]
        return ({"lines": out}, f"{len(out)} lines")

    def _verb_read_own_log(self, resident: str, args: dict) -> tuple[dict, str]:
        """Tail/grep of the CALLING resident's configured log file only. The
        path comes from broker.toml; a caller-supplied `path` is accepted only
        if it resolves to exactly that file (so `../` games are dead ends)."""
        _reject_unknown(args, {"lines", "grep", "path"})
        lines = _check_int(args, "lines", 100, 1, MAX_LOG_LINES)
        grep = _check_str(args, "grep", max_len=MAX_GREP_CHARS)
        cfg_path = self.residents.get(resident, {}).get("log_path")
        if not cfg_path:
            raise VerbError("internal", f"no log_path configured for {resident}")
        requested = _check_str(args, "path", max_len=500)
        if requested is not None and os.path.realpath(requested) != os.path.realpath(cfg_path):
            raise _bad("path may only be this resident's configured log file")
        try:
            with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.read().splitlines()
        except OSError as exc:
            raise VerbError("exec-failure", f"log not readable: {exc}") from None
        if grep is not None:  # plain substring match in-process — no shell, no regex
            all_lines = [ln for ln in all_lines if grep in ln]
        tail = all_lines[-lines:]
        return ({"lines": tail, "path": cfg_path},
                f"{len(tail)} lines" + (f" (grep={grep!r})" if grep else ""))

    def _verb_read_metrics(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, set())
        path = self.paths.get("metrics_json")
        if not path:
            raise VerbError("internal", "paths.metrics_json not configured")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                metrics = json.load(fh)
        except OSError as exc:
            raise VerbError("exec-failure", f"metrics not readable: {exc}") from None
        except json.JSONDecodeError:
            raise VerbError("exec-failure", "metrics file is not valid JSON") from None
        return ({"metrics": metrics}, "metrics read")

    def _verb_file_proposal(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, {"text"})
        text = _check_str(args, "text", required=True, max_len=MAX_PROPOSAL_CHARS)
        assert text is not None
        body = f"[proposal from {resident}] {text}"
        try:
            posted = self.transport(self.disjorn, body)
        except VerbError:
            raise
        except Exception as exc:  # noqa: BLE001 — transport errors -> clean failure
            raise VerbError("exec-failure", f"proposal post failed: {exc}") from None
        return ({"posted": True, **(posted or {})},
                f"proposal posted ({len(text)} chars)")

    def _verb_query_own_audit(self, resident: str, args: dict) -> tuple[dict, str]:
        """The calling resident's OWN audit lines for a date range. Filtering is
        by the broker-assigned resident name — never a caller-supplied value —
        so nobody can read anyone else's trail."""
        _reject_unknown(args, {"date_from", "date_to", "limit"})
        date_from = _check_date(args, "date_from")
        date_to = _check_date(args, "date_to")
        limit = _check_int(args, "limit", 100, 1, MAX_AUDIT_ENTRIES)
        entries: list[dict] = []
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("resident") != resident:
                        continue
                    day = str(rec.get("ts", ""))[:10]
                    if date_from <= day <= date_to:
                        entries.append(rec)
        except OSError as exc:
            raise VerbError("exec-failure", f"audit log not readable: {exc}") from None
        tail = entries[-limit:]  # most recent within range
        return ({"entries": tail, "count": len(tail),
                 "truncated": len(entries) > limit},
                f"{len(tail)} audit entries")

    # ------------------------------------------------------------- server

    def serve_forever(self) -> None:
        sock_dir = os.path.dirname(self.socket_path)
        if sock_dir and not os.path.isdir(sock_dir):
            os.makedirs(sock_dir, exist_ok=True)
        # Remove a stale socket left by an unclean shutdown (only if it IS a socket).
        try:
            if stat.S_ISSOCK(os.stat(self.socket_path).st_mode):
                os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.socket_path)
        # 0666 on the socket file: connecting is open to all local users because
        # AUTH is by SO_PEERCRED, not file permissions — unknown uids are denied
        # (and audited) inside dispatch().
        os.chmod(self.socket_path, 0o666)
        listener.listen(16)
        # A blocked accept() is not interrupted by close() on Linux, so poll
        # with a short timeout; shutdown() additionally pokes the socket.
        listener.settimeout(1.0)
        self._listener = listener
        while not self._closed:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break  # listener closed by shutdown()
            if self._closed:
                conn.close()
                break
            threading.Thread(target=self._handle_conn, args=(conn,),
                             daemon=True).start()

    def shutdown(self) -> None:
        self._closed = True
        # Wake a pending accept() immediately (best-effort).
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as poke:
                poke.settimeout(0.2)
                poke.connect(self.socket_path)
        except OSError:
            pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def _handle_conn(self, conn: socket.socket) -> None:
        """One connection = one request line = one response line."""
        try:
            conn.settimeout(30)
            # Kernel-asserted peer credentials: (pid, uid, gid).
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_REQUEST_BYTES:
                    # WP-H13 F1: audit this rejection like every other. The
                    # invariant (PROTOCOL.md, brokerd docstring) is that every
                    # request leaves exactly one line; the oversize path used
                    # to return silently, letting a resident spam hostile
                    # requests with no trace.
                    self._audit(f"uid:{uid}" if uid not in self.uid_map
                                else self.uid_map[uid],
                                "(oversize)", None, False, "denied: request too large")
                    self._send(conn, self._err("bad-args", "request too large"))
                    return
            line = buf.split(b"\n", 1)[0].strip()
            if not line:
                return  # connect-and-close probe; nothing to do or audit
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                self._audit(f"uid:{uid}" if uid not in self.uid_map
                            else self.uid_map[uid],
                            "(unparseable)", None, False, "denied: invalid JSON")
                self._send(conn, self._err("bad-args", "request is not valid JSON"))
                return
            if not isinstance(req, dict):
                req = {"verb": None, "args": None}
            resp = self.dispatch(uid, req.get("verb"), req.get("args", {}))
            self._send(conn, resp)
        except Exception:  # noqa: BLE001 — a bad client never kills the daemon
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send(conn: socket.socket, obj: dict) -> None:
        try:
            conn.sendall(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Disjorn privileged verb broker")
    parser.add_argument("--config", default=os.environ.get(ENV_CONFIG, DEFAULT_CONFIG_PATH))
    parser.add_argument("--verbs", default=os.environ.get(ENV_VERBS, DEFAULT_VERBS_PATH))
    ns = parser.parse_args(argv)

    config = load_config(ns.config)
    broker = Broker(config, ns.verbs)

    def _stop(signum: int, _frame: Any) -> None:
        print(f"disjorn-broker: signal {signum}, shutting down", file=sys.stderr)
        broker.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"disjorn-broker: listening on {broker.socket_path} "
          f"(config={ns.config}, verbs={ns.verbs})", file=sys.stderr)
    broker.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
