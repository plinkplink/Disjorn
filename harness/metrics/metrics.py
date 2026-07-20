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
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, Optional

DEFAULT_CONFIG_PATH = "/etc/disjorn-broker/broker.toml"
DEFAULT_WINDOW_DAYS = 7
TOP_REFERENCED = 10


# --------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------

def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _today_str(now: Optional[_dt.datetime] = None) -> str:
    return (now or _utc_now()).strftime("%Y-%m-%d")


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
    aggregates the stats WP-H8 consolidation also reads; it proposes nothing."""
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
        for rec in _iter_jsonl(Path(path)):
            total += 1
            day = str(rec.get("ts", ""))[:10]
            by_date[day] = by_date.get(day, 0) + 1
            q = rec.get("query")
            if isinstance(q, str):
                queries.add(q)
            ts = _parse_ts(str(rec.get("ts", "")))
            in_window = ts is not None and ts >= cutoff
            if in_window:
                window_recalls += 1
            for mid in (rec.get("returned_ids") or []):
                if not isinstance(mid, str):
                    continue
                returned.add(mid)
                if in_window:
                    ref_counts[mid] = ref_counts.get(mid, 0) + 1
        top = sorted(ref_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_REFERENCED]
        out[name] = {
            "total_recalls": total,
            "recalls_in_window": window_recalls,
            "by_date": by_date,
            "unique_queries": len(queries),
            "distinct_returned_ids": len(returned),
            "top_referenced": [[mid, n] for mid, n in top],
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


# --------------------------------------------------------------------------
# End-of-day #custodian line.
# --------------------------------------------------------------------------

def compose_daily_line(doc: dict, config: dict, date: str) -> str:
    """One compact message: per-resident action counts for `date`. Posted by
    the broker's own identity (not a resident), so no `[proposal from ...]`."""
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
    return f"[custodian daily {date}] action counts\n" + "\n".join(segments)


def post_daily_line(
    config: dict, doc: dict, *, date: str,
    transport: Optional[Callable[[dict, str], dict]] = None,
) -> dict:
    """Post the daily line to #custodian via the broker's posting identity.

    `transport` defaults to brokerd's `_sdk_transport` (the exact mechanism
    file-proposal uses); tests inject a stub so nothing hits the network."""
    body = compose_daily_line(doc, config, date)
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
    p_post.add_argument("--date", default=None, help="UTC date YYYY-MM-DD (default: today)")
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
        date = ns.date or _today_str()
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
