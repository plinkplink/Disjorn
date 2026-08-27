#!/usr/bin/env python3
"""disjorn resident metrics producer + daily #custodian line (WP-H12).

The read-metrics broker verb (WP-H3) serves exactly one file: the JSON at
`[paths].metrics_json` in broker.toml. This module is the PRODUCER of that
file — the action/audit half of the resident dashboard. It aggregates,
read-only, from data that already exists:

  * per-resident broker action counts from the broker AUDIT log (every verb
    call, allowed and denied) — same file query-own-audit reads;
  * per-resident retrieval stats from each resident's house_memory retrieval
    log (the unified JSON-lines schema, parsed directly — no chromadb import,
    so this stays cheap and dependency-free);
  * optional spine entry counts (read-only markdown frontmatter);
  * optional tool-call counts from WP-H5's ~/.action-log (all tool calls in
    the container) and the WP-H5 budget.json caps, surfaced for legibility.

Everything is config-driven from broker.toml (the plink-owned file that lives
OUTSIDE both containers). Nothing here is privileged: it only reads files and
writes the one metrics JSON. It never touches the live service, a socket, or
/etc — paths come from the config you point it at.

Two entry points (a CLI a timer invokes; see INTEGRATION-NEEDS.md):

    metrics.py build       --config broker.toml   # aggregate -> metrics_json
    metrics.py post-daily  --config broker.toml   # end-of-day #custodian line

`post-daily` reuses the broker's OWN posting identity — the same transport
file-proposal uses inside brokerd — so the daily line is posted by the broker
bot, not by any resident.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, Optional

DEFAULT_CONFIG_PATH = "/etc/disjorn-broker/broker.toml"

# Which retrieval callers count as "referenced" (Memory v2 phase 1).
#
# DUPLICATED FROM house_memory.retrieval_log.HEAT_CALLERS, and duplicated on
# purpose: this module's whole point is that it parses the logs WITHOUT
# importing house_memory, which would drag in chromadb and ~90 packages for a
# job that only reads JSON lines. The duplication is fenced by
# test_heat_callers_matches_house_memory — if the two ever disagree, that test
# fails rather than the dashboard quietly reporting a different number from the
# walker. (A silent second copy of shared logic is what cost 75 memories their
# tags on 2026-08-04; this one is allowed to exist only because it is pinned.)
HEAT_CALLERS = frozenset({"service"})
DEFAULT_WINDOW_DAYS = 7
TOP_REFERENCED = 10


# --------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------

def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _today_str(now: Optional[_dt.datetime] = None) -> str:
    return (now or _utc_now()).strftime("%Y-%m-%d")


def _yesterday_str(now: Optional[_dt.datetime] = None) -> str:
    """The previous complete UTC day — what the daily digest reports.

    It used to report TODAY at 23:55, which left the last five minutes of every
    day in no digest at all: those events are stamped with today's date, but
    today's digest has already posted and tomorrow's reports tomorrow.

    Five minutes sounds like a rounding error and was not one. 12 of 103 audit
    events landed in it — 34x over-represented — because the traffic there was
    not random: it was Claudette reading her own audit 5-40 seconds after the
    digest posted, checking the number against her memory of what she did. The
    hole sat exactly over the resident auditing the ledger, so the ledger could
    not record that it had been checked. One of the twelve is #custodian seq
    599, a correction SHE FILED ABOUT THE DIGEST 27 seconds after it posted.

    Reporting the previous complete day removes the window entirely rather than
    shrinking it. Her post-digest audit now lands in the next digest, which is
    correct, because that is the day it happened on."""
    return ((now or _utc_now()) - _dt.timedelta(days=1)).strftime("%Y-%m-%d")


def _iter_jsonl(path: Path):
    """Yield parsed JSON objects from a JSON-lines file. Missing file -> no
    yields; malformed or non-object lines are skipped (never fatal) — the same
    tolerance house_memory.read_records and the broker audit reader use."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _blank_counts() -> dict:
    return {"total": 0, "allowed": 0, "denied": 0}


def _bump(bucket: dict, allowed: bool) -> None:
    bucket["total"] += 1
    if allowed:
        bucket["allowed"] += 1
    else:
        bucket["denied"] += 1


def _parse_ts(ts: str) -> Optional[_dt.datetime]:
    try:
        parsed = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


# --------------------------------------------------------------------------
# Config access.
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def resident_names(config: dict) -> list[str]:
    return sorted(config.get("residents", {}))


def daily_action_cap(config: dict, resident: str) -> Optional[int]:
    """The broker-side daily action budget for a resident, or None (off).

    `[budgets.<resident>].daily_action_cap` wins; else
    `[budgets].default_daily_action_cap`; else None (unlimited). Default OFF —
    instrument first, tune from observed data (AGENTHOOD Budget rule)."""
    budgets = config.get("budgets", {})
    per = budgets.get(resident)
    if isinstance(per, dict) and isinstance(per.get("daily_action_cap"), int):
        return per["daily_action_cap"]
    default = budgets.get("default_daily_action_cap")
    return default if isinstance(default, int) else None


# --------------------------------------------------------------------------
# Aggregators — each pure over its inputs, read-only.
# --------------------------------------------------------------------------

def aggregate_broker_actions(
    audit_path: Path, config: dict, *, now: Optional[_dt.datetime] = None
) -> dict:
    """Per-resident broker verb counts from the audit JSON-lines log.

    Returns {resident: {total, allowed, denied, by_date, by_verb, today,
    budget}} for every configured resident (zeros if silent) plus any other
    caller (e.g. `uid:<n>`) that appears in the log."""
    now = now or _utc_now()
    today = _today_str(now)
    out: dict[str, dict] = {}

    def _ensure(name: str) -> dict:
        if name not in out:
            out[name] = {**_blank_counts(), "by_date": {}, "by_verb": {}}
        return out[name]

    # Seed configured residents so a quiet resident still reports zeros.
    for name in resident_names(config):
        _ensure(name)

    for rec in _iter_jsonl(audit_path):
        name = rec.get("resident")
        if not isinstance(name, str):
            continue
        allowed = bool(rec.get("allowed"))
        verb = rec.get("verb") if isinstance(rec.get("verb"), str) else "(unknown)"
        day = str(rec.get("ts", ""))[:10]
        bucket = _ensure(name)
        _bump(bucket, allowed)
        _bump(bucket["by_date"].setdefault(day, _blank_counts()), allowed)
        _bump(bucket["by_verb"].setdefault(verb, _blank_counts()), allowed)

    # Attach today + budget.
    for name, bucket in out.items():
        today_counts = bucket["by_date"].get(today, _blank_counts())
        bucket["today"] = dict(today_counts)
        cap = daily_action_cap(config, name)
        used = today_counts["allowed"]
        bucket["budget"] = {
            "daily_action_cap": cap,
            "used_today": used,
            "remaining": (max(cap - used, 0) if cap is not None else None),
        }
    return out


def aggregate_retrieval(
    config: dict, *, window_days: int, now: Optional[_dt.datetime] = None
) -> dict:
    """Per-resident retrieval stats from each resident's house_memory
    retrieval log. Path is `[residents.<r>].retrieval_log`; residents without
    one (or with a missing file) are simply absent. Read-only — this only
    aggregates the stats WP-H8 consolidation also reads; it proposes nothing.

    `top_referenced` counts SERVICE reads only (Memory v2 phase 1). This is the
    third place the same defect turned up — after `reference_counts` and
    `_last_seen_map` — and Claudette predicted it from the other two: "worth
    one grep for any other field written on a retrieval path without a caller
    filter, because the shape clearly recurs" (#custodian seq 614). It is the
    dashboard the residents read, so a blended count here tells them a memory
    is hot when what is actually hot is their own attention on it.

    `by_caller` is published alongside so the blend stays inspectable rather
    than merely excluded — the ratio is a diagnostic she asked to keep."""
    now = now or _utc_now()
    cutoff = now - _dt.timedelta(days=window_days)
    residents = config.get("residents", {})
    out: dict[str, dict] = {}
    for name in sorted(residents):
        rcfg = residents[name] if isinstance(residents[name], dict) else {}
        path = rcfg.get("retrieval_log")
        if not path:
            continue
        total = 0
        window_recalls = 0
        by_date: dict[str, int] = {}
        queries: set[str] = set()
        returned: set[str] = set()
        ref_counts: dict[str, int] = {}
        by_caller: dict[str, int] = {}
        for rec in _iter_jsonl(Path(path)):
            total += 1
            caller = rec.get("caller") or "unattributed"
            day = str(rec.get("ts", ""))[:10]
            by_date[day] = by_date.get(day, 0) + 1
            q = rec.get("query")
            if isinstance(q, str):
                queries.add(q)
            ts = _parse_ts(str(rec.get("ts", "")))
            in_window = ts is not None and ts >= cutoff
            if in_window:
                window_recalls += 1
                by_caller[caller] = by_caller.get(caller, 0) + 1
            for mid in (rec.get("returned_ids") or []):
                if not isinstance(mid, str):
                    continue
                returned.add(mid)
                # HEAT_CALLERS, not "every read" — see the docstring.
                if in_window and caller in HEAT_CALLERS:
                    ref_counts[mid] = ref_counts.get(mid, 0) + 1
        top = sorted(ref_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_REFERENCED]
        out[name] = {
            "total_recalls": total,
            "recalls_in_window": window_recalls,
            "by_date": by_date,
            "unique_queries": len(queries),
            "distinct_returned_ids": len(returned),
            "top_referenced": [[mid, n] for mid, n in top],
            "by_caller": dict(sorted(by_caller.items())),
        }
    return out


def _parse_frontmatter_kernel(text: str) -> bool:
    """True if the .md file's simple `---` frontmatter has `kernel: true`.
    Mirrors house_memory.spine's key:value-only parse (no YAML dependency)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, val = line.partition(":")
            if key.strip() == "kernel":
                return val.strip().lower() == "true"
    return False


def aggregate_spine(config: dict) -> dict:
    """Optional per-resident spine entry counts. `[residents.<r>].spine_dir`;
    absent/missing dirs are skipped. Counts .md files and kernel entries."""
    residents = config.get("residents", {})
    out: dict[str, dict] = {}
    for name in sorted(residents):
        rcfg = residents[name] if isinstance(residents[name], dict) else {}
        spine_dir = rcfg.get("spine_dir")
        if not spine_dir:
            continue
        d = Path(spine_dir)
        if not d.is_dir():
            continue
        entries = 0
        kernel = 0
        for md in sorted(d.glob("*.md")):
            entries += 1
            try:
                if _parse_frontmatter_kernel(md.read_text(encoding="utf-8")):
                    kernel += 1
            except OSError:
                continue
        out[name] = {"entries": entries, "kernel_entries": kernel}
    return out


def _load_wp5_budget(path: Optional[str]) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for key in ("daily_action_cap", "wall_clock_cap_min"):
        if isinstance(data.get(key), int):
            out[key] = data[key]
    return out


def aggregate_tool_actions(
    config: dict, *, now: Optional[_dt.datetime] = None
) -> dict:
    """Optional per-resident tool-call counts from WP-H5's ~/.action-log (every
    tool call in the container, not just broker verbs). Path is
    `[residents.<r>].action_log`; residents without one are skipped. Also
    surfaces the WP-H5 budget.json caps (`[residents.<r>].budget_json`) so the
    daily-action-cap and wall-clock-cap H5 enforces are visible on the same
    dashboard the residents read (WP-H12 does not re-enforce them)."""
    now = now or _utc_now()
    today = _today_str(now)
    residents = config.get("residents", {})
    out: dict[str, dict] = {}
    for name in sorted(residents):
        rcfg = residents[name] if isinstance(residents[name], dict) else {}
        path = rcfg.get("action_log")
        budget_json = rcfg.get("budget_json")
        if not path and not budget_json:
            continue
        total = 0
        ok = 0
        by_date: dict[str, int] = {}
        sessions: set[str] = set()
        if path:
            for rec in _iter_jsonl(Path(path)):
                total += 1
                if rec.get("ok"):
                    ok += 1
                day = str(rec.get("ts", ""))[:10]
                by_date[day] = by_date.get(day, 0) + 1
                sid = rec.get("session_id")
                if isinstance(sid, str) and sid:
                    sessions.add(sid)
        entry = {
            "total": total,
            "ok": ok,
            "failed": total - ok,
            "by_date": by_date,
            "today": by_date.get(today, 0),
            "distinct_sessions": len(sessions),
        }
        wp5 = _load_wp5_budget(budget_json)
        if wp5:
            entry["wp5_budget"] = wp5
        out[name] = entry
    return out


def build_metrics(config: dict, *, window_days: int = DEFAULT_WINDOW_DAYS,
                  now: Optional[_dt.datetime] = None) -> dict:
    """The full metrics document read-metrics serves. Read-only over config."""
    now = now or _utc_now()
    audit_path = Path(config.get("broker", {}).get("audit_log", ""))
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": window_days,
        "broker_actions": {"by_resident": aggregate_broker_actions(audit_path, config, now=now)},
        "tool_actions": {"by_resident": aggregate_tool_actions(config, now=now)},
        "retrieval": {"by_resident": aggregate_retrieval(config, window_days=window_days, now=now)},
        "spine": {"by_resident": aggregate_spine(config)},
    }


def write_metrics(config: dict, doc: dict) -> str:
    """Atomically write the metrics document to `[paths].metrics_json`."""
    out_path = config.get("paths", {}).get("metrics_json")
    if not out_path:
        raise SystemExit("config error: [paths].metrics_json is not set")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=".metrics-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out_path


# ==========================================================================
# KEYBOARD-LANE GATE — the detector of record (Plan Room Phase 0).
# ==========================================================================
#
# The pre-receive hook (harness/gatehouse/hooks/pre-receive-main-review) is
# deliberately dumb: paths plus a trailer, a presence check on text. Everything
# it delegates lands HERE, and it has to actually exist here or `review-seq: 1`
# passes forever and the whole gate is a spelling test (seq 1428, G2).
#
# This half computes, for one day:
#
#   * the gate's OWN LIVENESS first (G3) — an empty drift block must be
#     distinguishable from a disarmed detector. Install is hand-made, and
#     committed-isn't-installed went four-for-four on 08-19/20.
#   * CITATION, defined once from PUSH TRUTH (G1/G1b): a commit is cited iff a
#     logged push covers it AND that push's trailer resolves (the seq exists,
#     lives in #custodian). Push boundaries come from the hook's log and are
#     never reconstructed from reachability — a five-commit push with one
#     trailer on the tip is ONE cited range, not one pass and four false
#     violations. And because coverage is per logged range, a later
#     trailer-bearing push cannot retroactively bless its ancestors: an uncited
#     push that landed only because the hook failed open stays uncited forever,
#     which is exactly the case this detector exists to catch.
#   * SELF-CITATION — a `review-seq` whose author is the person who pushed. The
#     comfortable failure mode, named so it cannot pass as review. (An
#     `override-seq` IS the pusher's own line by design; no check there.)
#   * COVERAGE CLASSES above the genesis floor (seq 2067). No covering log
#     line means the commit never met the hook, and that is ALL the log knows.
#     It does NOT mean the hook was down: commits made locally in the
#     canonical repo — the broker's own `## Status` stamps, keyboard commits —
#     never push, so they never could have a log line, and for two days in
#     August every one of them printed a flat assertion that the hook was
#     absent, two lines under the same digest's own hook MATCH. Nothing here
#     asserts a hook state it did not measure. Each commit above the floor
#     lands in exactly one class: `covered` (a logged push range holds it),
#     `local-stamp` (the actor left a positive record naming the sha at the
#     moment it committed), `local-keyboard` (no record, but the committer is
#     an identity this deployment declares local), or UNEXPLAINED. Only
#     unexplained is a finding; together with the fail-open count it is the
#     fact that exists nowhere else in the house.
#   * FLOOR MOTION — the floor this digest sees against the floor its own
#     PREVIOUS post reported. That baseline lives in the message store, outside
#     the git-dir, beyond the reach of a log delete or a repo re-create, so it
#     is the tell that survives when both in-log tamper tells die with the log.
#
# NOTHING HERE IS DERIVED-BUT-STORED (G5). The override count is recomputed
# from `main`'s trailers every time, so "counted forever" survives a database
# rebuild. The floor-motion baseline is read back out of the previous post. The
# push log is the one primary record — push boundaries and fail-open firings
# exist nowhere in git and cannot be derived after the fact, which makes it the
# same class as the broker audit log, not a cache.

GATE_GUARDED_PREFIXES = ("server/", "client/", "sdk/", "harness/")
HOOK_REPO_PATH = "harness/gatehouse/hooks/pre-receive-main-review"
DRIFT_HEADER = "GATE DRIFT"

# The push log's grammar, pinned on both sides: the hook writes it, this reads
# it, and harness/gatehouse/tests pins the writer's output against these shapes.
GENESIS_RE = re.compile(r"^GENESIS\s+(seeded|lazy)\s+(\S+)\s+(\S+)\s*$")
PUSH_RE = re.compile(r"^PUSH\s+(\S+)\s+(\S+)\.\.(\S+)\s+(\S+)\s+(\S+)\s*$")
TRAILER_VALUE_RE = re.compile(r"^(review-seq|override-seq):(\d+)$")
# The same trailer, as it appears in a commit message (the override count is
# derived from `main`'s history, never from the log).
TRAILER_LINE_RE = re.compile(
    r"^\s*(review-seq|override-seq)\s*:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE)

# THE LOCAL COVERAGE LOG — the push log's sibling (seq 2067). Written by the
# actors that commit into the canonical repo WITHOUT pushing, read here.
#
#   LOCAL <ts> <sha> <outcome>
#
# A local commit can never have a push-log line, because there was no push. The
# fix is not a cleverer inference, it is a POSITIVE RECORD from the one process
# that knows: this sha, this time, this word.
#
# A SEPARATE FILE, ON PURPOSE. The push log is the primary record of what the
# HOOK saw, and its genesis / truncation tells are how a deleted-and-recreated
# log gets caught. Putting a second writer inside the one file the whole
# detector's integrity rests on would buy tidiness with the thing that matters.
# Deleting THIS file costs nothing but explanations: every record it held
# degrades to local-keyboard or UNEXPLAINED, never to clean.
LOCAL_LOG_NAME = "disjorn-local-log"
LOCAL_RE = re.compile(r"^LOCAL\s+(\S+)\s+(\S+)\s+(\S+)\s*$")
LOCAL_STAMP = "local-stamp"        # the broker's own spec Status commits
LOCAL_KEYBOARD = "local-keyboard"  # a declared-local committer, no record
UNEXPLAINED = "unexplained"        # neither — the only class that is a finding

# How many named flags of one kind go on the line before it summarises. NO
# SILENT CAPS: whenever a list is cut, the line says how many were dropped.
FLAG_CAP = 10

# git identities that belong to a message-store author under another name.
# Overridable per-deployment in `[gate.author_aliases]`.
DEFAULT_AUTHOR_ALIASES = {"keyboard": ["plink"], "broker": ["disjorn-broker"]}

# Post-hoc classification asks about the SHAPE of a landed diff — which paths,
# how large — not about whether it was auto-appliable. classify() fails closed
# on missing gate results, so a digest that passed none would call every commit
# in history Tier 2 and the LANE VIOLATION flag would mean nothing.
CLASSIFY_GATES = {"tests": True, "typecheck": True, "build": True}


def gate_paths(config: dict) -> dict:
    """The `[gate]` block, with every path this module needs resolved.

    Absent config is NOT an error and NOT silence: `configured` goes False and
    the drift block says the detector is not wired up, which is the whole point
    of G3 — an empty block and a disarmed one must not read alike."""
    g = config.get("gate", {}) if isinstance(config.get("gate"), dict) else {}
    canonical = g.get("canonical_repo")
    deploy_tree = g.get("deploy_tree")
    hook_link = g.get("hook_link")
    push_log = g.get("push_log")
    local_log = g.get("local_log")
    if canonical:
        hook_link = hook_link or os.path.join(canonical, "hooks", "pre-receive")
        push_log = push_log or os.path.join(canonical, "hooks", "disjorn-push-log")
        local_log = local_log or os.path.join(canonical, "hooks", LOCAL_LOG_NAME)
    message_db = g.get("message_db")
    if not message_db and deploy_tree:
        message_db = os.path.join(deploy_tree, "server", "data", "disjorn.db")
    aliases = dict(DEFAULT_AUTHOR_ALIASES)
    for name, alts in (g.get("author_aliases") or {}).items():
        if isinstance(alts, list):
            aliases[str(name)] = [str(a) for a in alts]
    return {
        "configured": bool(canonical and g.get("mirror")),
        "canonical_repo": canonical,
        "hook_link": hook_link,
        "push_log": push_log,
        "local_log": local_log,
        # Git identities that have a shell on this box and commit straight into
        # the canonical repo. Substrings, matched against "<cn> <ce>|<an> <ae>",
        # case-insensitively. NO DEFAULT: a house's local uids are house facts,
        # and a built-in list would quietly explain away a commit on a
        # deployment that never made it. Empty means nothing classifies as
        # local-keyboard and local commits read as UNEXPLAINED — loud, and the
        # block says why it is loud.
        "local_committers": [str(x) for x in (g.get("local_committers") or [])
                             if str(x).strip()],
        "mirror": g.get("mirror"),
        "branch": g.get("mirror_branch", "main"),
        "deploy_tree": deploy_tree,
        "message_db": message_db,
        "custodian_channel_id": config.get("disjorn", {}).get("custodian_channel_id"),
        # The key behind the digest's own posts (_sdk_transport reads the same
        # path). previous_digest needs it to tell its own posts apart from
        # anyone else typing the drift header into the channel.
        "api_key_path": config.get("disjorn", {}).get("api_key_path"),
        "protected_paths": config.get("paths", {}).get("protected_paths"),
        "author_aliases": aliases,
    }


# -- read-only plumbing -----------------------------------------------------

def _git(repo: str, *args: str) -> Optional[str]:
    """`git -C repo …`, or None if it fails. Never raises: a drift block that
    dies on one unreadable repo reports nothing at all, which is the failure
    mode this whole build exists to prevent."""
    if not repo:
        return None
    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _read_bytes(path: Optional[str]) -> tuple[Optional[bytes], Optional[str]]:
    """Read a file, falling back to `sudo -n cat` for broker-owned paths.

    The push log lives in the canonical repo's git-dir, which belongs to the
    broker user; board.py reaches the same gatehouse through `sudo git` for the
    same reason. If BOTH fail the caller reports NO LOG — a permission problem
    is indistinguishable from a deletion from out here, and that is the correct
    direction to fail: a lost log degrades to more flags, never fewer."""
    if not path:
        return None, "no path configured"
    missing = False
    try:
        return Path(path).read_bytes(), None
    except FileNotFoundError as exc:
        first, missing = str(exc), True
    except OSError as exc:
        first = str(exc)
    if missing and os.access(os.path.dirname(path) or ".", os.R_OK):
        # We can see the directory and the file is not in it. That is an
        # answer, not a permission problem — do not go asking sudo.
        return None, first
    try:
        proc = subprocess.run(["sudo", "-n", "cat", path],
                              capture_output=True)
    except OSError:
        return None, first
    if proc.returncode == 0:
        return proc.stdout, None
    return None, first


def _resolve_link(path: Optional[str]) -> tuple[Optional[str], bool]:
    """(target, target_exists) for the installed hook symlink.

    Returns (None, False) when the link is missing — which, from a process that
    may not be able to traverse the broker's tree, also covers "cannot see it".
    ABSENT is the loud answer and the safe one."""
    if not path:
        return None, False
    if os.path.lexists(path):
        target = os.path.realpath(path)
        return target, os.path.exists(target)
    if os.access(os.path.dirname(path) or ".", os.R_OK):
        return None, False  # the hooks dir is readable and the link is not in it
    try:
        proc = subprocess.run(["sudo", "-n", "readlink", "-f", path],
                              capture_output=True, text=True)
    except OSError:
        return None, False
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, False
    target = proc.stdout.strip()
    probe = subprocess.run(["sudo", "-n", "test", "-e", target],
                           capture_output=True)
    return target, probe.returncode == 0


def _blob_sha(mirror: Optional[str], data: bytes) -> Optional[str]:
    """The git blob sha of some bytes, computed by the MIRROR's git so the hash
    algorithm matches the sha we compare it against. Falls back to sha1 (git's
    default object format) if the mirror is unavailable."""
    if mirror:
        try:
            proc = subprocess.run(
                ["git", "-C", mirror, "hash-object", "--no-filters",
                 "--stdin"], input=data, capture_output=True)
            if proc.returncode == 0:
                return proc.stdout.decode().strip()
        except OSError:
            pass
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def _short(sha: Optional[str], n: int = 8) -> str:
    return (sha or "?")[:n]


# -- the push log -----------------------------------------------------------

def parse_push_log(path: Optional[str]) -> dict:
    """Read the hook's append-only log. Never raises."""
    doc = {"path": path, "present": False, "error": None, "genesis": [],
           "first_is_genesis": False, "pushes": [], "malformed": 0}
    data, err = _read_bytes(path)
    if data is None:
        doc["error"] = err
        return doc
    doc["present"] = True
    first = True
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = GENESIS_RE.match(line)
        if m:
            doc["genesis"].append({"kind": m.group(1), "ts": m.group(2),
                                   "sha": m.group(3)})
            if first:
                doc["first_is_genesis"] = True
            first = False
            continue
        m = PUSH_RE.match(line)
        if m:
            doc["pushes"].append({
                "ts": m.group(1), "old": m.group(2), "new": m.group(3),
                "trailer": None if m.group(4) == "NONE" else m.group(4),
                "outcome": m.group(5),
            })
        else:
            doc["malformed"] += 1
        first = False
    return doc


def genesis_state(log: dict) -> dict:
    """The floor and how it was born (G1c/G1d).

    state is one of `seeded`, `lazy`, `TRUNCATED`, `REPLACED`, `NO LOG`.

    A SEEDED floor puts everything below it out of scope by agreement — the
    gate starts where the log starts, so the first digest after install flags
    nothing historical and the detector doesn't cry wolf on its first breath.
    A LAZY floor does not: it was minted from whatever `main` looked like the
    first time the hook happened to fire, on the far side of exactly the window
    the uncovered flag exists to catch, so what is below it is UNVERIFIABLE
    rather than clean. The two must never read alike."""
    if not log.get("present"):
        return {"state": "NO LOG", "floor": None, "ts": None,
                "provenance": None,
                "detail": f"push log unreadable ({log.get('error')})"}
    gens = log.get("genesis") or []
    floor = gens[0]["sha"] if gens else None
    ts = gens[0]["ts"] if gens else None
    if len(gens) > 1:
        return {"state": "REPLACED", "floor": floor, "ts": ts,
                "provenance": gens[0]["kind"],
                "detail": f"{len(gens)} genesis lines — an append-only log with "
                          f"a second genesis line was deleted and recreated"}
    if not log.get("first_is_genesis"):
        return {"state": "TRUNCATED", "floor": floor, "ts": ts,
                "provenance": gens[0]["kind"] if gens else None,
                "detail": "the log's first line is not a genesis line — it was "
                          "truncated, not merely young"}
    return {"state": gens[0]["kind"], "floor": floor, "ts": ts,
            "provenance": gens[0]["kind"], "detail": ""}


# -- the local coverage log -------------------------------------------------

def parse_local_log(path: Optional[str]) -> dict:
    """Read the sibling coverage log the local committers write. Never raises.

    ABSENT IS NOT AN ERROR and is not a finding. Before anything wrote this
    file there were no records to read, and commits that predate it fall
    through to the committer rule (spec 2026-08-27, acceptance 5). What an
    absent file must never do is make an unexplained commit look explained —
    and it cannot: every rule here only ever ADDS an explanation.

    A record whose outcome word this reader does not know explains nothing.
    Unknown words are kept in the parse (so a newer writer's lines are visible
    rather than counted as garbage) and ignored by the classifier."""
    doc = {"path": path, "present": False, "error": None, "records": {},
           "malformed": 0}
    data, err = _read_bytes(path)
    if data is None:
        doc["error"] = err
        return doc
    doc["present"] = True
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = LOCAL_RE.match(line)
        if m:
            doc["records"][m.group(2)] = {"ts": m.group(1), "outcome": m.group(3)}
        else:
            doc["malformed"] += 1
    return doc


# -- liveness (G3) ----------------------------------------------------------

def hook_liveness(paths: dict) -> dict:
    """Is the detector armed? The installed path, the sha of the file the
    symlink ACTUALLY resolves to, and the mirror's sha for the same file.

    The comparison is against the DEPLOYED COPY (G4), never a working clone: a
    `git checkout` in a clone would silently disarm the gate, and this line is
    what would notice."""
    link = paths.get("hook_link")
    out = {"link": link, "target": None, "state": "ABSENT",
           "deployed_sha": None, "mirror_sha": None, "detail": ""}
    if not link:
        out["detail"] = "no [gate].canonical_repo / hook_link configured"
        return out
    target, exists = _resolve_link(link)
    out["target"] = target
    if not target or not exists:
        out["detail"] = ("the pre-receive symlink is missing or dangling — "
                         "the gate is NOT installed")
        return out
    data, err = _read_bytes(target)
    if data is None:
        out["state"] = "UNREADABLE"
        out["detail"] = f"cannot read the deployed hook: {err}"
        return out
    out["deployed_sha"] = _blob_sha(paths.get("mirror"), data)
    mirror_sha = _git(paths.get("mirror") or "", "rev-parse",
                      f"{paths.get('branch', 'main')}:{HOOK_REPO_PATH}")
    out["mirror_sha"] = mirror_sha.strip() if mirror_sha else None
    if out["mirror_sha"] is None:
        out["state"] = "UNKNOWN"
        out["detail"] = (f"the mirror has no {HOOK_REPO_PATH} to compare "
                         f"against")
    elif out["mirror_sha"] == out["deployed_sha"]:
        out["state"] = "MATCH"
    else:
        out["state"] = "MISMATCH"
        out["detail"] = ("the installed hook is NOT the committed one — "
                         "committed is not installed")
    return out


# -- the message store: resolving a cited seq (G2) --------------------------

def _open_db(path: Optional[str]):
    if not path or not os.path.exists(path):
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    db.row_factory = sqlite3.Row
    return db


def resolve_seq(db, seq: int, custodian_channel_id) -> dict:
    """Does this seq exist, does it live in #custodian, and who wrote it?

    `seq` is per-channel (server migration 001), so "resolves somewhere else"
    is a real and different answer from "does not resolve" — and both mean the
    citation does not hold."""
    out = {"seq": seq, "resolves": False, "in_custodian": False,
           "author": None, "detail": ""}
    if db is None:
        out["detail"] = "message store unreadable — citation NOT verified"
        return out
    try:
        rows = db.execute(
            "select channel_id, author_type, author_id from messages "
            "where seq=? and deleted_at is null", (seq,)).fetchall()
    except sqlite3.Error as exc:
        out["detail"] = f"message store query failed: {exc}"
        return out
    if not rows:
        out["detail"] = "no such seq in the message store"
        return out
    out["resolves"] = True
    hit = next((r for r in rows if r["channel_id"] == custodian_channel_id), None)
    if hit is None:
        out["detail"] = "the seq resolves, but not in #custodian"
        return out
    out["in_custodian"] = True
    out["author"] = _author_name(db, hit["author_type"], hit["author_id"])
    return out


def _author_name(db, author_type: str, author_id: int) -> str:
    # The table/column pair is chosen from a literal 2-tuple, never from input;
    # the only value that reaches the query as data is bound.
    table, column = ("bots", "name") if author_type == "bot" else ("users", "username")
    try:
        row = db.execute(f"select {column} as n from {table} where id=?",
                         (author_id,)).fetchone()
    except sqlite3.Error:
        row = None
    return row["n"] if row and row["n"] else f"{author_type}:{author_id}"


def identity_matches(author: Optional[str], git_identity: Optional[str],
                     aliases: dict) -> bool:
    """Is the #custodian author of a cited seq the same person as the committer
    of the commit that cited it?

    The push log records what the hook saw — timestamp, range, trailer, outcome
    — and not who pushed, so the committer identity on the pushed head is the
    proxy. It is a name match, deliberately crude and deliberately generous:
    this flag is a prompt to look, and a false SELF-CITED is cheap next to a
    self-review that reads as reviewed."""
    if not author or not git_identity:
        return False
    hay = git_identity.lower()
    tokens = [author.lower()] + [a.lower() for a in aliases.get(author, [])]
    return any(t and t in hay for t in tokens)


# -- coverage, citation, and what fell through ------------------------------

def _rev_list(mirror: str, *args: str) -> Optional[list]:
    out = _git(mirror, "rev-list", *args)
    if out is None:
        return None
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _commit_line(mirror: str, sha: str) -> str:
    out = _git(mirror, "log", "-1", "--format=%s", sha)
    return (out or "").strip() or "(subject unavailable)"


def _committer(mirror: str, sha: str) -> Optional[str]:
    out = _git(mirror, "log", "-1", "--format=%cn <%ce>|%an <%ae>", sha)
    return (out or "").strip() or None


def push_coverage(paths: dict, log: dict, db) -> dict:
    """Fold the push log into commit sets. THE definition of cited (G1/G1b).

    covered — every commit inside a logged push range that LANDED (passed or
      failed-open). A refused push never landed — its commits are not on
      `main` to need covering, and its range can never resolve in the mirror.
      A commit with no covering line never met the hook at all.
    cited — commits inside a logged range whose trailer RESOLVES per G2.
      A push that failed open with a real trailer is still a cited push. What
      cannot happen is the laundering case — a failed-open push with NO trailer
      contributes no citation, and no later push can reach back and bless it,
      because coverage is per logged range and never per reachability."""
    mirror = paths.get("mirror") or ""
    aliases = paths.get("author_aliases", {})
    custodian = paths.get("custodian_channel_id")
    covered: set = set()
    cited: set = set()
    citations: list = []
    unresolvable: list = []
    fail_open = 0
    seq_cache: dict = {}

    for push in log.get("pushes", []):
        if push["outcome"] == "failed-open":
            fail_open += 1
        if push["outcome"] == "refused":
            # A refused push never landed: nothing to cover, nothing to cite,
            # and its range can never resolve in the mirror. Rev-listing it
            # would misattribute every legitimate refusal — the hook doing
            # its one job — as permanent "history was rewritten" noise.
            continue
        old, new = push["old"], push["new"]
        if set(new) == {"0"}:
            continue  # a ref deletion covers nothing
        spec = [new] if set(old) == {"0"} else [f"{old}..{new}"]
        commits = _rev_list(mirror, *spec)
        if commits is None:
            unresolvable.append(push)
            continue
        covered.update(commits)

        trailer = push["trailer"]
        if not trailer:
            continue
        m = TRAILER_VALUE_RE.match(trailer)
        if not m:
            citations.append({"push": push, "trailer": trailer, "kind": None,
                              "seq": None, "holds": False, "self_cited": False,
                              "detail": "unparseable trailer in the push log"})
            continue
        kind, seq = m.group(1), int(m.group(2))
        if seq not in seq_cache:
            seq_cache[seq] = resolve_seq(db, seq, custodian)
        res = seq_cache[seq]
        holds = bool(res["in_custodian"])
        self_cited = False
        if holds and kind == "review-seq":
            # An override-seq IS the pusher's own line by design; only a review
            # can be self-cited.
            self_cited = identity_matches(res["author"], _committer(mirror, new),
                                          aliases)
        citations.append({"push": push, "trailer": trailer, "kind": kind,
                          "seq": seq, "holds": holds, "self_cited": self_cited,
                          "author": res.get("author"), "detail": res["detail"]})
        if holds:
            cited.update(commits)
    return {"covered": covered, "cited": cited, "citations": citations,
            "fail_open": fail_open, "unresolvable": unresolvable}


def is_local_committer(identity: Optional[str], patterns: list) -> bool:
    """Does this commit's identity belong to a uid that commits on this box?

    Deliberately crude, and deliberately WEAKER than a record: it is a fact
    about the deployment, not about the commit. It cannot prove a commit was
    made locally — nothing after the fact can, git does not record how a
    commit arrived — so it is only ever reached second, and only to say
    "this one has a mundane explanation available", never "this one is fine"."""
    if not identity or not patterns:
        return False
    hay = identity.lower()
    return any(p and p.lower() in hay for p in patterns)


def classify_coverage(mirror: str, above: list, covered: set, records: dict,
                      local_committers: list) -> dict:
    """Every commit above the floor into EXACTLY ONE class (seq 2067).

    ORDER IS THE ARGUMENT. Push truth first: a logged range that holds the sha
    is coverage, measured. Then the positive record, on its writer's
    authority. Then the committer rule, which is an availability of
    explanation and nothing more. What none of the three reaches is
    UNEXPLAINED — the word for `not measured`, which is the honest thing the
    old fixed string was not."""
    out = {"above": len(above), "covered": [], LOCAL_STAMP: [],
           LOCAL_KEYBOARD: [], UNEXPLAINED: []}
    for sha in above:
        if sha in covered:
            out["covered"].append(sha)
        elif (records.get(sha) or {}).get("outcome") == LOCAL_STAMP:
            out[LOCAL_STAMP].append(sha)
        elif is_local_committer(_committer(mirror, sha), local_committers):
            out[LOCAL_KEYBOARD].append(sha)
        else:
            out[UNEXPLAINED].append(sha)
    return out


def override_trailers(mirror: str, branch: str = "main") -> Optional[list]:
    """Every `override-seq` on `main`, DERIVED (G5) — never stored, never read
    from the push log. Computed from trailers in `main`'s history at digest
    time, so "counted forever" survives any database rebuild, the same
    cards-derive-from-artifacts rule the Plan Room spec is built on."""
    out = _git(mirror, "log", branch, "--format=%H%x1f%B%x1e")
    if out is None:
        return None
    seqs = []
    for record in out.split("\x1e"):
        if "\x1f" not in record:
            continue
        sha, _, body = record.strip("\n").partition("\x1f")
        for kind, seq in TRAILER_LINE_RE.findall(body):
            if kind.lower() == "override-seq":
                seqs.append({"commit": sha.strip(), "seq": int(seq)})
    return seqs


# -- classification of the uncited (G4 of item 4) ---------------------------

_CLASSIFIER = None


def _classifier():
    """The classifier module, imported from the tree — never re-implemented.
    Same reason board.py imports the broker's own parsers: two implementations
    of one rule disagree exactly when it matters."""
    global _CLASSIFIER
    if _CLASSIFIER is None:
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "classifier" / "classify_diff.py"
        spec = importlib.util.spec_from_file_location("classify_diff", path)
        mod = importlib.util.module_from_spec(spec)
        # REGISTERED BEFORE EXEC, and load-bearing: @dataclass resolves its own
        # module out of sys.modules while the class body runs, so a
        # module_from_spec that is not registered dies on classify_diff's very
        # first decorator. The failure surfaced as every commit reading
        # "UNCLASSIFIED" — a detector that had quietly stopped detecting.
        sys.modules.setdefault("classify_diff", mod)
        spec.loader.exec_module(mod)
        _CLASSIFIER = mod
    return _CLASSIFIER


def classify_commit(mirror: str, protected_paths: Optional[str],
                    sha: str) -> dict:
    """`classify_diff` over one landed commit. {tier, reasons, error}."""
    if not protected_paths:
        return {"tier": None, "error": "no [paths].protected_paths configured"}
    try:
        mod = _classifier()
        result = mod.classify(mirror, protected_paths,
                              range_spec=f"{sha}~1..{sha}",
                              gates=dict(CLASSIFY_GATES))
    except Exception as exc:  # a broken checkout, a root commit, a bad config
        return {"tier": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"tier": result.get("tier"), "reasons": result.get("reasons", []),
            "protected_hits": result.get("protected_hits", []), "error": None}


def guarded_hits_for(mirror: str, sha: str) -> list:
    """Which gated lanes a commit touched — the hook's own question, asked
    again after the fact so a LANE VIOLATION line can name the paths."""
    out = _git(mirror, "diff", "--name-only", f"{sha}~1", sha)
    if out is None:
        return []
    return sorted({p.strip() for p in out.splitlines()
                   if p.strip().startswith(GATE_GUARDED_PREFIXES)})


# -- deploy drift -----------------------------------------------------------

def deploy_state(config: Optional[dict] = None, *, mirror: Optional[str] = None,
                 deploy_tree: Optional[str] = None,
                 branch: str = "main") -> dict:
    """Prod's running tree against mirror head. THE tri-state, one computation.

    NAMED AND IMPORTABLE ON PURPOSE (seq 1428, P6): the Plan Room's tri-state
    badge is this same question, and it calls this rather than re-implementing
    it. Two implementations of "is prod current" would disagree on exactly the
    day it mattered.

    `state` is one of `in-sync`, `drift`, `unknown`. Since prod deploys from the
    mirror (plink, seq 1391) the hook already sits on the deploy path, so this
    is belt-and-braces — except for the case it uniquely catches: a DIRTY prod
    tree is code that is running and was never published, which is the
    ship-by-not-publishing incentive Claudette named at seq 1380."""
    if config is not None:
        p = gate_paths(config)
        mirror = mirror or p["mirror"]
        deploy_tree = deploy_tree or p["deploy_tree"]
        branch = branch or p["branch"]
    out = {"state": "unknown", "mirror_head": None, "deployed_head": None,
           "dirty": None, "ahead": None, "behind": None, "detail": ""}
    if not mirror or not deploy_tree:
        out["detail"] = "no [gate].mirror / [gate].deploy_tree configured"
        return out
    mirror_head = _git(mirror, "rev-parse", branch)
    deployed_head = _git(deploy_tree, "rev-parse", "HEAD")
    if mirror_head is None or deployed_head is None:
        out["detail"] = ("cannot read " + ("the mirror" if mirror_head is None
                                           else "prod's tree"))
        return out
    out["mirror_head"] = mirror_head.strip()
    out["deployed_head"] = deployed_head.strip()
    status = _git(deploy_tree, "status", "--porcelain")
    out["dirty"] = None if status is None else bool(status.strip())
    # Ask whichever repo can resolve BOTH commits. The mirror usually can (prod
    # deploys from it); prod cannot, the moment the mirror moves ahead — which
    # is precisely the case this line exists to describe.
    counts = (_git(mirror, "rev-list", "--left-right", "--count",
                   f"{out['mirror_head']}...{out['deployed_head']}")
              or _git(deploy_tree, "rev-list", "--left-right", "--count",
                      f"{out['mirror_head']}...{out['deployed_head']}"))
    if counts:
        parts = counts.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            out["behind"], out["ahead"] = int(parts[0]), int(parts[1])
    if out["mirror_head"] == out["deployed_head"] and out["dirty"] is False:
        out["state"] = "in-sync"
        out["detail"] = "prod runs mirror head, working tree clean"
        return out
    out["state"] = "drift"
    bits = []
    if out["mirror_head"] != out["deployed_head"]:
        bits.append(f"prod is at {_short(out['deployed_head'])}, mirror head is "
                    f"{_short(out['mirror_head'])}")
        if out["behind"] is not None:
            bits.append(f"prod is {out['behind']} behind and {out['ahead']} "
                        f"ahead of the mirror")
    if out["dirty"]:
        bits.append("prod's working tree is DIRTY — code is running that was "
                    "never published")
    elif out["dirty"] is None:
        bits.append("could not read prod's working tree state")
    out["detail"] = "; ".join(bits)
    return out


# -- the previous digest, read back out of the message store ----------------

FLOOR_LINE_RE = re.compile(r"^floor:\s+(\S+)", re.MULTILINE)
MIRROR_HEAD_LINE_RE = re.compile(r"^mirror head:\s+(\S+)", re.MULTILINE)
# The drift header as it opens a real block: at the start of a line, followed
# by the em-dash tail compose_drift_block writes. The same broker bot also
# posts build banners that mention the header MID-sentence ("the digest's GATE
# DRIFT block"); those are not digests and must never become the baseline.
DRIFT_BLOCK_RE = re.compile(rf"^{re.escape(DRIFT_HEADER)} — ", re.MULTILINE)
# Newest-first scan bound for the baseline query. The baseline is normally the
# first or second row; the cap only exists so a pathological channel cannot
# turn this read into a full-table walk.
BASELINE_SCAN_CAP = 50


def _digest_author_id(paths: dict, db) -> Optional[int]:
    """The bot id behind the broker's posting key ([disjorn].api_key_path) —
    the identity every digest post carries. None when the key or its bot row
    cannot be read; the caller reports that loudly rather than widening the
    query, because a baseline query with no author filter lets anyone who can
    post in the channel write the floor-motion baseline.

    The lookup is pinned to the server's key scheme (routers/auth.py,
    hash_api_key: sha256 hexdigest over the raw key). If that scheme ever
    changes, this resolves to None and the BASELINE UNAVAILABLE line is the
    tell — a detector fault, never a silent no-baseline."""
    key_path = paths.get("api_key_path")
    if not key_path:
        return None
    try:
        with open(key_path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    except OSError:
        return None
    if not key:
        return None
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    try:
        row = db.execute("select id from bots where api_key_hash=?",
                         (key_hash,)).fetchone()
    except sqlite3.Error:
        return None
    return int(row["id"]) if row else None


def previous_digest(paths: dict, db=None) -> Optional[dict]:
    """The floor and mirror head THIS digest reported last time.

    The baseline for the floor-motion check lives HERE — in the message store,
    outside the git-dir — and not in the log, because the whole point of the
    check is to survive the log being deleted and lazily re-born. It adds no
    storage anywhere (G5): it is read back out of a post that already exists,
    through the message-store access G2 already grants.

    Three rules decide what counts as the baseline (review findings, seqs
    1470/1471), and all three are needed:
      * only posts by the digest's OWN identity — the bot behind the broker's
        posting key — are considered. The channel is writable by everyone,
        and a baseline anyone can write is the G1d hole reopened one layer
        out. If that identity cannot be resolved, the return is an ``error``
        marker, never a widened query and never a quiet no-baseline.
      * the header must OPEN a line, in block form. The same broker bot posts
        build banners that mention the header mid-sentence; a banner is not a
        digest.
      * the post must carry a parseable ``floor:`` line. A drift block
        without one (DETECTOR NOT CONFIGURED, a mangled post) is skipped and
        the scan continues to the next-older candidate — no-baseline rather
        than motion, and an older true baseline still beats none: floors
        never legitimately move, so age does not stale it."""
    own = db is None
    db = db or _open_db(paths.get("message_db"))
    if db is None:
        return None
    try:
        author = _digest_author_id(paths, db)
        if author is None:
            return {"seq": None, "floor": None, "mirror_head": None,
                    "error": "could not resolve the digest's own posting "
                             "identity (api_key_path -> bots row)"}
        rows = db.execute(
            "select seq, content from messages where channel_id=? "
            "and deleted_at is null and author_type='bot' and author_id=? "
            "and content like ? order by seq desc limit ?",
            (paths.get("custodian_channel_id"), author,
             f"%{DRIFT_HEADER}%", BASELINE_SCAN_CAP)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if own:
            db.close()
    for row in rows:
        content = row["content"]
        if not DRIFT_BLOCK_RE.search(content):
            continue  # our own banner quoting the header mid-sentence
        floor = FLOOR_LINE_RE.search(content)
        if floor is None:
            continue  # a block with no floor line is no baseline, not motion
        head = MIRROR_HEAD_LINE_RE.search(content)
        return {"seq": row["seq"], "floor": floor.group(1),
                "mirror_head": head.group(1) if head else None}
    return None


# -- the drift block --------------------------------------------------------

def gate_drift(config: dict, *, date: str, now: Optional[_dt.datetime] = None,
               previous: Optional[dict] = None) -> dict:
    """Everything the drift block reports, as data. Never raises."""
    paths = gate_paths(config)
    drift = {"date": date, "configured": paths["configured"], "paths": paths}
    if not paths["configured"]:
        return drift

    log = parse_push_log(paths["push_log"])
    genesis = genesis_state(log)
    drift["log"] = log
    drift["genesis"] = genesis
    drift["liveness"] = hook_liveness(paths)

    db = _open_db(paths["message_db"])
    try:
        if previous is None:
            previous = previous_digest(paths, db)
        if previous and previous.get("error"):
            # Identity failure is a detector fault and renders as one — it
            # must never look like the benign first-digest state, or a broken
            # key file silently retires the floor-motion check forever.
            drift["baseline_error"] = previous["error"]
            previous = None
        drift["previous"] = previous
        drift["floor_moved"] = bool(
            previous and (previous.get("floor") or "NONE")
            != (genesis.get("floor") or "NONE"))

        mirror, branch = paths["mirror"], paths["branch"]
        head = _git(mirror, "rev-parse", branch)
        drift["mirror_head"] = head.strip() if head else None

        # -- what the hook saw, and what it missed
        if log.get("present"):
            cov = push_coverage(paths, log, db)
        else:
            # STRICT FALLBACK (G1b): no reachability inference, ever. With no
            # log there are no push boundaries, so citation degrades to
            # per-commit trailer presence — more flags, never fewer.
            cov = {"covered": set(), "cited": set(), "citations": [],
                   "fail_open": None, "unresolvable": [],
                   "strict_fallback": True}
        drift["fail_open"] = cov["fail_open"]
        drift["citations"] = cov["citations"]
        drift["self_cited"] = [c for c in cov["citations"] if c["self_cited"]]
        drift["broken_citations"] = [c for c in cov["citations"] if not c["holds"]]
        drift["unresolvable_ranges"] = len(cov["unresolvable"])
        drift["strict_fallback"] = cov.get("strict_fallback", False)

        # -- the window: since the previous digest's mirror head
        base = previous.get("mirror_head") if previous else None
        window = None
        drift["window_source"] = "— the mirror is unreadable"
        if drift["mirror_head"]:
            if base:
                window = _rev_list(mirror, f"{base}..{drift['mirror_head']}")
                drift["window_source"] = (f"since the previous digest "
                                          f"(seq {previous['seq']})")
            if window is None:
                # No baseline (the first digest after install), or a baseline
                # the mirror no longer has. Fall back to the reported day.
                window = _rev_list(mirror, branch, f"--since={date}T00:00:00Z",
                                   f"--until={date}T23:59:59Z")
                drift["window_source"] = (f"on {date} UTC — no previous digest "
                                          f"to measure from")
        window = window or []
        drift["window"] = window

        if drift["strict_fallback"]:
            uncited = [c for c in window
                       if not _commit_has_trailer(mirror, c)]
        else:
            uncited = [c for c in window if c not in cov["cited"]]
        drift["uncited"] = uncited
        drift["classified"] = [
            {"sha": c, "subject": _commit_line(mirror, c),
             "hits": guarded_hits_for(mirror, c),
             **classify_commit(mirror, paths["protected_paths"], c)}
            for c in uncited
        ]
        drift["violations"] = [c for c in drift["classified"] if c["tier"] == 2]

        # -- uncovered: above the floor, no covering log line at all
        #
        # A floor of all zeros is a lazy floor minted when `main` itself was
        # created: there is nothing below it, and everything is above it.
        floor = genesis.get("floor")
        effective_floor = None if (floor and set(floor) == {"0"}) else floor
        above = None
        if drift["mirror_head"]:
            above = (_rev_list(mirror, f"{effective_floor}..{drift['mirror_head']}")
                     if effective_floor else _rev_list(mirror, drift["mirror_head"]))
        drift["floor_resolves"] = above is not None
        above = above or []
        drift["above_floor"] = len(above)
        drift["uncovered"] = ([] if not log.get("present")
                              else [c for c in above if c not in cov["covered"]])
        # -- and what each of those actually is (seq 2067). `uncovered` stays
        # exactly what it always was — no covering push line, the raw fact —
        # and the classes sit beside it. A commit is reported by its class;
        # only UNEXPLAINED is a finding.
        local = parse_local_log(paths["local_log"])
        drift["local_log"] = local
        drift["coverage"] = (
            classify_coverage(mirror, above, cov["covered"], local["records"],
                              paths["local_committers"])
            if log.get("present") else None)
        # Below a LAZY floor is not out of scope, it is UNVERIFIABLE (G1d).
        drift["unverifiable"] = 0
        if genesis.get("state") == "lazy" and effective_floor:
            below = _rev_list(mirror, effective_floor)
            drift["unverifiable"] = len(below or [])

        overrides = override_trailers(mirror, branch)
        drift["overrides"] = overrides
        drift["deploy"] = deploy_state(mirror=mirror,
                                       deploy_tree=paths["deploy_tree"],
                                       branch=branch)
    finally:
        if db is not None:
            db.close()
    return drift


def _commit_has_trailer(mirror: str, sha: str) -> bool:
    body = _git(mirror, "log", "-1", "--format=%B", sha)
    return bool(body) and bool(TRAILER_LINE_RE.search(body))


def _named(items: list, render) -> list:
    """Render up to FLAG_CAP items and SAY SO when the list was cut. A silent
    truncation reads as 'covered everything' when it didn't."""
    lines = [render(i) for i in items[:FLAG_CAP]]
    if len(items) > FLAG_CAP:
        lines.append(f"    …and {len(items) - FLAG_CAP} more, not named here")
    return lines


def compose_drift_block(drift: dict, *, verbose: bool = False) -> str:
    """The drift block, opening with the detector's own liveness (G3).

    `verbose` names the commits in the INFORMATIONAL coverage classes on a day
    that is not alarming. The daily post never passes it: a clean day's
    coverage is one line, because a dozen known-benign rows every morning is
    precisely how the one row that matters stops being read."""
    L = [f"{DRIFT_HEADER} — keyboard lane, {drift['date']} UTC"]
    if not drift.get("configured"):
        L.append("DETECTOR NOT CONFIGURED: broker.toml has no [gate] block "
                 "with canonical_repo + mirror. The gate may or may not be "
                 "installed; from here nothing can be said. This is not an "
                 "empty drift block.")
        return "\n".join(L)

    # 1. the hook itself
    live = drift["liveness"]
    if live["state"] == "ABSENT":
        L.append(f"hook: ABSENT at {live['link']} — {live['detail']}")
    elif live["state"] in ("UNREADABLE", "UNKNOWN"):
        L.append(f"hook: {live['state']} at {live['link']} — {live['detail']}")
    else:
        L.append(f"hook: {live['link']} -> {live['target']}, "
                 f"sha {_short(live['deployed_sha'])} vs mirror "
                 f"{_short(live['mirror_sha'])} ({live['state']})"
                 + (f" — {live['detail']}" if live["detail"] else ""))

    # 2. the log's genesis
    g = drift["genesis"]
    if g["state"] == "seeded":
        L.append(f"push log: genesis seeded, floor {_short(g['floor'])} "
                 f"at {g['ts']}")
    elif g["state"] == "lazy":
        L.append(f"push log: genesis LAZY (warning), floor "
                 f"{_short(g['floor'])} at {g['ts']} — floor minted at first "
                 f"push; commits before {_short(g['floor'])} unverifiable")
    else:
        L.append(f"push log: {g['state']} — {g['detail']}")

    # 3. the floor, against the previous digest's own report
    prev = drift.get("previous")
    floor_txt = g["floor"] or "NONE"
    if drift.get("floor_moved"):
        L.append(f"floor: {floor_txt}")
        L.append(f"  FLOOR MOVED: the previous digest (seq {prev['seq']}) "
                 f"reported {prev.get('floor') or 'NONE'}. Floors do not move. "
                 f"The log was replaced, whatever the log itself claims.")
    elif prev:
        L.append(f"floor: {floor_txt} — unchanged since the previous digest "
                 f"(seq {prev['seq']})")
    elif drift.get("baseline_error"):
        L.append(f"floor: {floor_txt}")
        L.append(f"  BASELINE UNAVAILABLE: {drift['baseline_error']} — floor "
                 f"motion UNCHECKED this digest. This is a detector fault, "
                 f"not a first run.")
    else:
        L.append(f"floor: {floor_txt} — no baseline yet (first digest since "
                 f"install); this is the floor every later digest checks "
                 f"against")

    # 4. the drift itself
    # `floor:` and `mirror head:` carry FULL shas, and not for tidiness: the
    # next digest parses both back out of this post — the floor as its
    # motion baseline, the head as the start of its window.
    L.append(f"mirror head: {drift.get('mirror_head') or 'UNREADABLE'}")
    window, uncited = drift.get("window", []), drift.get("uncited", [])
    L.append(f"commits on main {drift.get('window_source', '')}: "
             f"{len(window)} ({len(uncited)} uncited)")
    if drift.get("strict_fallback"):
        L.append("  NOTE: no push log, so citation fell back to strict "
                 "per-commit trailer presence — no reachability inference. "
                 "Multi-commit pushes will read as uncited.")
    for v in _named(drift.get("violations", []),
                    lambda c: f"  LANE VIOLATION: {_short(c['sha'])} "
                              f"{c['subject']} — Tier 2, uncited"
                              + (f" ({', '.join(c['hits'][:4])})" if c["hits"] else "")):
        L.append(v)
    for c in drift.get("classified", []):
        if c["tier"] is None and c.get("error"):
            L.append(f"  UNCLASSIFIED: {_short(c['sha'])} {c['subject']} — "
                     f"{c['error']}")
    for c in drift.get("self_cited", []):
        L.append(f"  SELF-CITED: {c['trailer']} on "
                 f"{_short(c['push']['new'])} was posted by {c['author']}, who "
                 f"pushed it. A review you wrote yourself is not a review.")
    for c in drift.get("broken_citations", []):
        L.append(f"  CITATION DOES NOT RESOLVE: {c['trailer']} on "
                 f"{_short(c['push']['new'])} — {c['detail']}. That range "
                 f"counts as UNCITED.")

    # 5. the two facts that exist nowhere else
    fo = drift.get("fail_open")
    L.append(f"fail-open pushes in the log: "
             f"{'UNKNOWN (no log)' if fo is None else fo}")
    mir = drift["paths"]["mirror"]
    if not drift.get("log", {}).get("present"):
        L.append("coverage above floor: UNKNOWN — the push log is unreadable, "
                 "so the one record of what the hook saw is gone")
    elif not drift.get("floor_resolves", True):
        L.append(f"coverage above floor: UNKNOWN — the floor "
                 f"{_short(g['floor'])} does not resolve in the mirror, so "
                 f"'above the floor' cannot be computed")
    else:
        cls = drift.get("coverage") or {}
        unexplained = cls.get(UNEXPLAINED, [])
        L.append(f"coverage above floor: {cls.get('above', 0)} commits — "
                 f"covered {len(cls.get('covered', []))}, "
                 f"{LOCAL_STAMP} {len(cls.get(LOCAL_STAMP, []))}, "
                 f"{LOCAL_KEYBOARD} {len(cls.get(LOCAL_KEYBOARD, []))}, "
                 f"{UNEXPLAINED} {len(unexplained)}")
        # The finding. It says what was measured — no push-log line, no record,
        # no declared-local committer — and stops there. It does NOT say the
        # hook was down: that is the liveness line's job, three lines up, and
        # for two days this line contradicted it (seq 2067).
        for line in _named(unexplained,
                           lambda c: f"  UNEXPLAINED: {_short(c)} "
                                     f"{_commit_line(mir, c)} — on main above "
                                     f"the floor with no push-log line, no "
                                     f"{LOCAL_STAMP} record, and a committer "
                                     f"this deployment does not declare local. "
                                     f"How it arrived is not measured here."):
            L.append(line)
        if unexplained and not drift["paths"].get("local_committers"):
            L.append("  NOTE: [gate].local_committers is empty, so nothing can "
                     "classify as local-keyboard and every local commit reads "
                     "as unexplained. Name this deployment's local identities "
                     "or this count never falls to zero.")
        # The informational classes name their commits only when the section is
        # already alarming, or on demand.
        if verbose or unexplained:
            for name in (LOCAL_STAMP, LOCAL_KEYBOARD):
                for line in _named(cls.get(name, []),
                                   lambda c, name=name: f"  {name}: {_short(c)}"
                                                        f" {_commit_line(mir, c)}"):
                    L.append(line)
        if (drift.get("local_log") or {}).get("malformed"):
            L.append(f"  {drift['local_log']['malformed']} unparseable line(s) "
                     f"in the local coverage log — they explain nothing, and "
                     f"whatever they were meant to cover fell to another class")
    if drift.get("unverifiable"):
        L.append(f"  UNVERIFIABLE: {drift['unverifiable']} commits below the "
                 f"LAZY floor {_short(g['floor'])} — the window between install "
                 f"and the hook's first firing cannot be ruled out. Not clean, "
                 f"just unknown.")
    if drift.get("unresolvable_ranges"):
        L.append(f"  {drift['unresolvable_ranges']} logged push range(s) do not "
                 f"resolve in the mirror — history was rewritten, or the mirror "
                 f"is behind")

    # 6. overrides, counted forever, derived from main's trailers
    ov = drift.get("overrides")
    if ov is None:
        L.append("overrides to date: UNKNOWN (cannot read the mirror's history)")
    else:
        seqs = ", ".join(str(o["seq"]) for o in ov[:FLAG_CAP])
        more = f" +{len(ov) - FLAG_CAP} more" if len(ov) > FLAG_CAP else ""
        L.append(f"overrides to date: {len(ov)}"
                 + (f" (override-seq {seqs}{more})" if ov else ""))

    # 7. deploy drift
    d = drift.get("deploy", {})
    L.append(f"deploy: {d.get('state', 'unknown')}"
             + (f" — {d['detail']}" if d.get("detail") else ""))
    return "\n".join(L)


# --------------------------------------------------------------------------
# End-of-day #custodian line.
# --------------------------------------------------------------------------

def compose_daily_line(doc: dict, config: dict, date: str,
                       drift: Optional[dict] = None) -> str:
    """One compact message: per-resident action counts for `date`, plus the
    keyboard-lane GATE DRIFT block when one was computed. Posted by the broker's
    own identity (not a resident), so no `[proposal from ...]`.

    `drift` is passed IN rather than computed here on purpose: composing stays a
    pure function of its inputs (this module's whole shape), and the drift
    block's git + message-store reads stay in one place, `post_daily_line`.
    The floor-motion check reads THIS message back next time, so the block's
    `floor:` and `mirror head:` lines are load-bearing text, not decoration."""
    broker_by = doc.get("broker_actions", {}).get("by_resident", {})
    tool_by = doc.get("tool_actions", {}).get("by_resident", {})
    segments = []
    for name in resident_names(config):
        b = broker_by.get(name, {})
        day = b.get("by_date", {}).get(date, {"total": 0, "allowed": 0, "denied": 0})
        cap = daily_action_cap(config, name)
        budget_str = f", budget {day['allowed']}/{cap}" if cap is not None else ""
        seg = f"{name}: {day['total']} broker verbs ({day['denied']} denied){budget_str}"
        t = tool_by.get(name)
        if t is not None:
            day_tool = t.get("by_date", {}).get(date, 0)
            seg += f", {day_tool} tool calls"
        segments.append(seg)
    # The bounds are ON the line. "Daily" was a claim of completeness the old
    # 23:55 run could not honour, and a reader cannot tell a complete day from
    # a truncated one unless the line says which it is.
    #
    # UTC is stated because it is NOT the reader's day. The box runs EDT, so
    # this window is 20:00-20:00 local, and the busiest hour in the whole audit
    # log (03:00 UTC = 23:00 EDT) belongs to the local evening BEFORE the date
    # in this header. Every other log in the house is UTC and splitting the
    # digest off would create a reconciliation problem worse than the
    # confusion; so it stays UTC and says so.
    body = (
        f"[custodian daily {date} UTC — complete day, 00:00:00–23:59:59] "
        f"action counts\n" + "\n".join(segments)
    )
    if drift is not None:
        body += "\n\n" + compose_drift_block(drift)
    return body


def post_daily_line(
    config: dict, doc: dict, *, date: str,
    transport: Optional[Callable[[dict, str], dict]] = None,
    drift: Optional[dict] = None,
) -> dict:
    """Post the daily line to #custodian via the broker's posting identity.

    `transport` defaults to brokerd's `_sdk_transport` (the exact mechanism
    file-proposal uses); tests inject a stub so nothing hits the network.

    The GATE DRIFT block is computed here unless one is passed in. It is not
    optional and it is not skippable on error: `gate_drift` never raises, and
    an unconfigured or unreachable detector produces a block that SAYS SO. An
    empty drift block and a disarmed detector must not read alike (G3)."""
    if drift is None:
        drift = gate_drift(config, date=date)
    body = compose_daily_line(doc, config, date, drift=drift)
    if transport is None:
        transport = _default_transport()
    return transport(config.get("disjorn", {}), body)


def _default_transport() -> Callable[[dict, str], dict]:
    # Import lazily and reuse the broker's own SDK poster — one posting
    # identity, one code path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "broker"))
    from brokerd import _sdk_transport  # noqa: E402
    return _sdk_transport


# --------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Disjorn resident metrics producer (WP-H12)")
    parser.add_argument("--config", default=os.environ.get("DISJORN_BROKER_CONFIG", DEFAULT_CONFIG_PATH),
                        help="path to broker.toml (source of all paths/budgets)")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help=f"trailing window for retrieval reference counts (default {DEFAULT_WINDOW_DAYS})")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="aggregate and (re)write the metrics JSON file")
    p_post = sub.add_parser("post-daily", help="post the end-of-day #custodian action-count line")
    p_post.add_argument("--date", default=None,
                        help="UTC date YYYY-MM-DD (default: YESTERDAY — the "
                             "previous complete day; see _yesterday_str)")
    p_post.add_argument("--no-rebuild", action="store_true",
                        help="post from the existing metrics file instead of rebuilding")
    ns = parser.parse_args(argv)

    config = load_config(ns.config)

    if ns.cmd == "build":
        doc = build_metrics(config, window_days=ns.window_days)
        path = write_metrics(config, doc)
        print(f"metrics written: {path}", file=sys.stderr)
        return 0

    if ns.cmd == "post-daily":
        date = ns.date or _yesterday_str()
        if ns.no_rebuild:
            out_path = config.get("paths", {}).get("metrics_json", "")
            doc = json.loads(Path(out_path).read_text(encoding="utf-8"))
        else:
            doc = build_metrics(config, window_days=ns.window_days)
            write_metrics(config, doc)
        result = post_daily_line(config, doc, date=date)
        print(f"posted daily line for {date}: {result}", file=sys.stderr)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
