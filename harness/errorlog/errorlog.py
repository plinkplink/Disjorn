#!/usr/bin/env python3
"""disjorn house error log — the artifact Memory v2 left as "owner TBD".

WHAT THIS IS FOR. The v2 spec's trace auditor MUST log null and truncated
turns (`stop_reason=max_tokens`, adapter drops) and says they "also go to a
house error log". The canonical case is Claudette's 2026-07-28 14:47 reply:
she wrote a full answer, hit the token wall, and what reached the channel was
`No response generated.` — a silent hole where a turn had been. Nobody
noticed until she went looking. THAT is the failure this file exists to make
impossible: a turn that dies should leave a record somewhere a human reads.

OWNER (ruled 2026-08-03, plink). plink + the keyboard Claude Code harness, NOT
the residents. The original plan was resident-owned agent harnesses filing
their own faults, but residents are sealed in containers with no write path to
a house-level file and a long deploy chain to change anything — so a
resident-owned error log would have been an artifact nobody could actually
maintain. Keyboard-owned is the honest siting for now. The forward path is
unchanged and needs no rework: when a resident CAN write (phase 2's auditor,
or Claudette's adapter-side null-turn logging, her proposal seq 501), it
appends to its OWN file in its OWN volume and `collect` harvests it — same
shape as the sources below.

LOCATION. `/var/log/disjorn-errorlog/errors.jsonl`, plink-owned 0640. The
directory is created by the unit's `LogsDirectory=`, which is exactly how
`/var/log/disjorn-broker/audit.jsonl` gets its home — systemd owns the
directory's existence and ownership so no install step has to mkdir as root.
JSON lines, append-only.

PRIVACY RULE — load-bearing, read before adding a source. Claudette's adapter
log carries whole conversations on DEBUG lines. A collector that copied
matched lines verbatim would siphon chat content into a house-level file that
different people read. So a source declares a `redact` flag, and a redacted
source contributes ONLY the matched signature (e.g. `stop_reason=max_tokens`)
plus its file/line — never the surrounding text. `detail` is hard-capped at
DETAIL_MAX either way. When you add a source, decide its redact flag FIRST.

THREE ENTRY POINTS:

    errorlog.py record  --source S --kind K --detail D   # append one event
    errorlog.py collect                                  # harvest known sources
    errorlog.py tail    [--days N] [--kind K]            # read it back

`record` is the universal writer — the keyboard, a cron job, a future auditor,
or a human all append the same shape through it. `collect` is idempotent: it
watermarks each source by (inode, offset) and additionally de-dupes by
fingerprint, so re-running it never doubles an event.

Nothing here is privileged. It reads files plink can already read (both
residents' log dirs carry a `user:plink:r-x` ACL and world-readable logs) and
writes one file plink owns. No broker call, no socket, no /etc, no container.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Iterator, Optional

DEFAULT_LOG = "/var/log/disjorn-errorlog/errors.jsonl"
DEFAULT_STATE = "/var/lib/disjorn-errorlog/state.json"
DETAIL_MAX = 300
FINGERPRINT_LOOKBACK_DAYS = 30

# The taxonomy. Keep it small and stable — a kind is what you grep for at
# 3am, so a new one should earn its place rather than describe one incident.
KINDS = (
    "truncation",     # a turn hit the token wall; output lost or partial
    "null_turn",      # the adapter produced nothing where a reply belonged
    "refusal",        # a safety classifier declined the request
    "timeout",        # a session exceeded its wall clock
    "session_failed", # a session exited non-zero
    "model_drift",    # the model that ran was not the model pinned
    "transport",      # websocket/HTTP to the Disjorn server failed
    "crash",          # unhandled exception / traceback
    "other",
)


# --------------------------------------------------------------------------
# Sources. Each is a file plink can read plus the signatures worth recording.
#
# `redact=True` means: this file contains conversation content, so record the
# MATCH ONLY, never the line. See the privacy rule in the module docstring.
# --------------------------------------------------------------------------

SOURCES = (
    {
        "name": "gable-summon",
        "path": "/home/res-gable/resident-home/logs/gable.log",
        "subject": "res-gable",
        "redact": False,
        # Python logging: "YYYY-MM-DD HH:MM:SS,mmm logger LEVEL message"
        "ts_re": r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
        "patterns": (
            (r"model drift: pinned (\S+) but session ran (\S+)", "model_drift"),
            (r"summon session failed: session timed out after ([\d.]+)s", "timeout"),
            (r"summon session failed: session exit (\d+)", "session_failed"),
            (r"WS connect to \S+ failed", "transport"),
        ),
    },
    {
        "name": "claudette-adapter",
        "path": "/home/res-claudette/resident-home/logs/disjorn_bot.log",
        "subject": "res-claudette",
        "redact": True,   # DEBUG lines carry whole conversations
        "ts_re": None,    # her format ("LEVEL:logger:msg") carries no timestamp
        "patterns": (
            (r"stop_reason=max_tokens", "truncation"),
            # Ordered BEFORE null_turn: a refusal also produces an empty reply,
            # so without its own pattern it was collected as a bare null_turn
            # with the cause stripped off. Three of hers were logged that way
            # before this line existed (found 2026-08-04).
            (r"stop_reason=refusal", "refusal"),
            (r"REFUSAL stop_reason \(category=([^)]*)\)", "refusal"),
            (r"Final answer: No response generated\.", "null_turn"),
            (r"^OSError: .*Bad file descriptor", "crash"),
        ),
    },
)


# --------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------

def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value) -> Optional[_dt.datetime]:
    """Parse our own ISO8601-Z stamp. None/garbage -> None, never raises."""
    if not isinstance(value, str):
        return None
    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError:
        return None


def _clip(text: str, limit: int = DETAIL_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fingerprint(source: str, anchor: str, kind: str, detail: str) -> str:
    """Stable identity for an event, so a re-collect cannot double-append.

    `anchor` is the event's position in time or in a file, and it MUST be
    stable across runs — a fingerprint keyed on "now" changes every pass and
    silently disables the de-dupe backstop.

    For a timestamped source the anchor is the timestamp, deliberately: the
    same signature at two different times is two real events (a summon that
    times out twice is not one timeout). For a source with no timestamps the
    anchor is inode+line, which is stable across re-reads AND changes on
    rotation, so a rotated file's line 3 cannot collide with the old file's
    line 3."""
    raw = "\x1f".join((source, anchor, kind, detail))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_event(
    *,
    source: str,
    kind: str,
    detail: str,
    subject: Optional[str] = None,
    ts: Optional[str] = None,
    evidence: Optional[dict] = None,
    anchor: Optional[str] = None,
    stamp_now: bool = True,
) -> dict:
    """Build one event.

    `ts` is when the error HAPPENED. If the source cannot tell us — Claudette's
    adapter log carries no timestamps — `ts` is None and stays None. It is not
    back-filled with the collection time: an event stamped "now" reads as
    having just happened, and thirty of them stamped identically reads as an
    incident. `logged_at` already records when we saw it, so the honest field
    pair is "unknown when, known when-seen"."""
    if kind not in KINDS:
        kind = "other"
    if ts is None and stamp_now and anchor is None:
        # A directly-recorded event (CLI/`record`) happens as it is written,
        # so "now" is the true time, not a guess.
        ts = _iso(_utc_now())
    detail = _clip(detail)
    ev = {
        "ts": ts,
        "ts_known": ts is not None,
        "logged_at": _iso(_utc_now()),
        "source": source,
        "subject": subject,
        "kind": kind,
        "detail": detail,
        "evidence": evidence or {},
    }
    ev["fingerprint"] = fingerprint(source, anchor or ts or "", kind, detail)
    return ev


def iter_events(path: Path) -> Iterator[dict]:
    """Yield events from the house log. Missing file -> nothing; malformed
    lines are skipped, never fatal — same tolerance as the audit reader."""
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
    except FileNotFoundError:
        return


def append_events(path: Path, events: Iterable[dict]) -> int:
    """Append events, creating the file 0640. Returns how many were written.

    One open, one write, line-buffered flush — a concurrent reader either sees
    a whole line or none of it."""
    events = list(events)
    if not events:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with open(path, "a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    if not existed:
        try:
            os.chmod(path, 0o640)
        except OSError:
            pass
    return len(events)


# --------------------------------------------------------------------------
# State (per-source watermarks), so collect is idempotent and cheap.
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
            return obj if isinstance(obj, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    """Atomic replace — a killed collector must not leave a truncated state
    file, because an unreadable watermark silently re-reads a whole log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Collection.
# --------------------------------------------------------------------------

def scan_source(src: dict, start_offset: int = 0) -> tuple[list[dict], int]:
    """Scan one source from start_offset. Returns (events, new_offset).

    Reads bytes, not lines, so the watermark survives a partial last line: we
    stop at the last complete newline and resume there next run."""
    path = Path(src["path"])
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(start_offset)
            chunk = fh.read()
            end_offset = fh.tell()
    except FileNotFoundError:
        return [], start_offset

    # Only consume through the last complete line.
    cut = chunk.rfind("\n")
    if cut == -1:
        return [], start_offset
    consumed = chunk[: cut + 1]
    new_offset = start_offset + len(consumed.encode("utf-8"))

    ts_re = re.compile(src["ts_re"]) if src.get("ts_re") else None
    events: list[dict] = []
    base_line = _count_lines_before(path, start_offset)
    try:
        inode = path.stat().st_ino
    except OSError:
        inode = 0

    for i, line in enumerate(consumed.splitlines(), start=1):
        for pattern, kind in src["patterns"]:
            m = re.search(pattern, line)
            if not m:
                continue
            ts = None
            if ts_re:
                tm = ts_re.match(line)
                if tm:
                    ts = tm.group(1).replace(" ", "T") + "Z"
            lineno = base_line + i
            if src.get("redact"):
                # Signature only. The line may be a whole conversation turn.
                detail = m.group(0)
                evidence = {"file": str(path), "line": lineno, "redacted": True}
            else:
                detail = line
                evidence = {"file": str(path), "line": lineno}
            events.append(
                make_event(
                    source=src["name"],
                    kind=kind,
                    detail=detail,
                    subject=src.get("subject"),
                    ts=ts,
                    evidence=evidence,
                    # Position anchor when the line carries no clock, so the
                    # fingerprint is stable across runs and distinct across
                    # rotations.
                    anchor=None if ts else f"ino{inode}:line{lineno}",
                    stamp_now=False,
                )
            )
            break  # one event per line; first pattern wins
    return events, new_offset


def _count_lines_before(path: Path, offset: int) -> int:
    """Line number of the byte at `offset`, so evidence points at a real line
    even when we started mid-file."""
    if offset <= 0:
        return 0
    try:
        with open(path, "rb") as fh:
            return fh.read(offset).count(b"\n")
    except OSError:
        return 0


def collect(
    log_path: Path,
    state_path: Path,
    sources: Iterable[dict] = SOURCES,
    *,
    reset: bool = False,
) -> dict:
    """Harvest all sources into the house log. Idempotent."""
    state = {} if reset else load_state(state_path)
    known = _recent_fingerprints(log_path)
    written = 0
    per_source: dict[str, int] = {}

    for src in sources:
        name = src["name"]
        path = Path(src["path"])
        try:
            st = path.stat()
        except FileNotFoundError:
            per_source[name] = 0
            continue

        prev = state.get(name, {})
        offset = int(prev.get("offset", 0))
        # Rotation / truncation detection: a new inode or a shrunk file means
        # the offset we stored points into a different file. Start over rather
        # than silently skipping everything written since.
        if prev.get("inode") != st.st_ino or st.st_size < offset:
            offset = 0

        events, new_offset = scan_source(src, offset)
        fresh = [e for e in events if e["fingerprint"] not in known]
        for e in fresh:
            known.add(e["fingerprint"])
        n = append_events(log_path, fresh)
        written += n
        per_source[name] = n
        state[name] = {"inode": st.st_ino, "offset": new_offset}

    save_state(state_path, state)
    return {"written": written, "by_source": per_source}


def _recent_fingerprints(log_path: Path) -> set:
    """Fingerprints already recorded, within the lookback. The watermark is
    the primary guard; this is the backstop for a lost/reset state file."""
    cutoff = _utc_now() - _dt.timedelta(days=FINGERPRINT_LOOKBACK_DAYS)
    out = set()
    for ev in iter_events(log_path):
        fp = ev.get("fingerprint")
        if not fp:
            continue
        when = _parse_ts(ev.get("logged_at")) or _parse_ts(ev.get("ts"))
        if when is None or when >= cutoff:
            out.add(fp)
    return out


# --------------------------------------------------------------------------
# Read-back.
# --------------------------------------------------------------------------

def tail(
    log_path: Path,
    *,
    days: Optional[int] = None,
    kind: Optional[str] = None,
    subject: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    cutoff = None
    if days is not None:
        cutoff = _utc_now() - _dt.timedelta(days=days)
    out = []
    for ev in iter_events(log_path):
        if kind and ev.get("kind") != kind:
            continue
        if subject and ev.get("subject") != subject:
            continue
        if cutoff is not None:
            # Fall back to logged_at when the source had no clock, so a
            # timestamp-less event is windowed by when we saw it rather than
            # being silently dropped from every --days query.
            when = _parse_ts(ev.get("ts")) or _parse_ts(ev.get("logged_at"))
            if when is not None and when < cutoff:
                continue
        out.append(ev)
    return out[-limit:]


def format_event(ev: dict) -> str:
    # An unknown time prints as "seen <logged_at>" rather than a blank or a
    # fabricated clock, so a reader can never mistake it for when it happened.
    if ev.get("ts"):
        when = ev["ts"]
    else:
        when = "seen " + (ev.get("logged_at") or "?")
    return "{when:<26} {kind:<15} {subject:<15} {detail}".format(
        when=when,
        kind=ev.get("kind", "?"),
        subject=ev.get("subject") or "-",
        detail=ev.get("detail", ""),
    )


# --------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Disjorn house error log.")
    p.add_argument("--log", default=DEFAULT_LOG, help="house error log path")
    p.add_argument("--state", default=DEFAULT_STATE, help="collector state path")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="append one event")
    r.add_argument("--source", required=True)
    r.add_argument("--kind", required=True, choices=KINDS)
    r.add_argument("--detail", required=True)
    r.add_argument("--subject", default=None)
    r.add_argument("--ts", default=None, help="ISO8601 Z; default now")

    c = sub.add_parser("collect", help="harvest known sources")
    c.add_argument("--reset", action="store_true", help="ignore watermarks")
    c.add_argument("--quiet", action="store_true")

    t = sub.add_parser("tail", help="read the log back")
    t.add_argument("--days", type=int, default=None)
    t.add_argument("--kind", default=None, choices=KINDS)
    t.add_argument("--subject", default=None)
    t.add_argument("--limit", type=int, default=50)
    t.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    log_path = Path(args.log)

    if args.cmd == "record":
        ev = make_event(
            source=args.source,
            kind=args.kind,
            detail=args.detail,
            subject=args.subject,
            ts=args.ts,
        )
        append_events(log_path, [ev])
        print(ev["fingerprint"])
        return 0

    if args.cmd == "collect":
        res = collect(log_path, Path(args.state), reset=args.reset)
        if not args.quiet:
            bits = ", ".join(f"{k}={v}" for k, v in sorted(res["by_source"].items()))
            print(f"wrote {res['written']} event(s) [{bits}]")
        return 0

    if args.cmd == "tail":
        rows = tail(
            log_path,
            days=args.days,
            kind=args.kind,
            subject=args.subject,
            limit=args.limit,
        )
        if args.json:
            for ev in rows:
                print(json.dumps(ev, sort_keys=True))
        else:
            if not rows:
                print("(no events)")
            for ev in rows:
                print(format_event(ev))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
