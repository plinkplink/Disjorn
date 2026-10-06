"""Wake and bot-to-bot summon hops."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import tempfile
import threading
import time
from typing import Callable, Optional

from broker_common import (
    BOARD_SLUG_RE, DEFAULT_WAKE_SESSION_CAP_SEC, DEFAULT_WAKE_GRACE_SEC,
    VerbError, _bad, ConfigError, _check_int, _check_str, _reject_unknown,
    _clean_field, _BUILD_CALLER_RE,
)


# ---------------------------------------------------------------------- wake
# SPECS/2026-08-25-agentic-residents.md. A wake starts a headless work session in a
# resident's seat.
WAKE_VERB = "wake"
MAX_WAKE_TASK_CHARS = 4000
# One record per wake, in the plink-owned spool.
WAKE_SPOOL_SUFFIX = ".wake.json"
WAKE_SPOOL_SCHEMA = 1
# How long a record stays in the spool after its window closes.
WAKE_RETENTION_SEC = 7 * 86400
# Wakes per seat per UTC day, CAPPED BY DEFAULT — an unset cap is not "no policy",
# it is an unbounded number of 5400s account-billed sessions behind one button.
DEFAULT_DAILY_WAKE_CAP = 3
_WAKE_ID_RE = re.compile(r"^wake-\d{8}T\d{6}Z-[0-9a-f]{6}$")


# --------------------------------------------------------------------------
# Wake (SPECS/2026-08-25-agentic-residents.md).
# --------------------------------------------------------------------------


def new_wake_id(now: Optional[_dt.datetime] = None,
                entropy: Optional[str] = None) -> str:
    """`wake-20260825T142310Z-9f3a1c` — sortable, greppable, collision-safe."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    stamp = now.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"wake-{stamp}-{entropy or os.urandom(3).hex()}"


def format_session_time(seconds: float) -> str:
    """`4h10m` / `50m` — a day's wake wall clock, for a human reading a refusal."""
    total = max(0, int(seconds))
    hours, minutes = divmod(total // 60, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def format_wake_refusal(*, seat: str, count: int, cap: int,
                        spent_sec: float) -> str:
    """The wall a wake past the daily cap hits."""
    return (f"daily wake cap reached for {seat}: {count}/{cap} wakes, "
            f"{format_session_time(spent_sec)} of session time today. Next "
            f"wake is tomorrow (UTC), or a witnessed edit to "
            f"[wake].daily_wake_cap in broker.toml.")


def parse_review_owner(text: str) -> Optional[str]:
    """The `- **Review owner**: …` bullet's value, or None if the spec has no such
    bullet."""
    for line in text.splitlines():
        plain = re.sub(r"[*_`]", "", line)
        m = re.match(r"\s*-\s*Review owner\s*:\s*(.*)$", plain, re.I)
        if m:
            return _clean_field(m.group(1))
    return None


def review_owner_seat(raw: Optional[str],
                      known_seats: "set[str] | frozenset[str]") -> Optional[str]:
    """The SEAT a review-owner line names (`Claudette` -> `res-claudette`), or None
    when it names nobody this house runs as."""
    if not raw:
        return None
    m = re.match(r"[\s*_`]*([A-Za-z][A-Za-z0-9_-]{0,30})", raw)
    if not m:
        return None
    seat = f"res-{m.group(1).lower()}"
    return seat if seat in known_seats else None


# --------------------------------------------------------------------------
# Bot-to-bot summon hops (SPECS/2026-08-24-custodian-mention-summons.md).
# --------------------------------------------------------------------------

DEFAULT_HOP_CAP = 8
DEFAULT_DAILY_HOP_CAP = 24


def format_hop_refusal(*, work_item: str, count: int, cap: int,
                       daily: bool = False) -> str:
    """The refusal line, fixed format."""
    if daily:
        return (f"summon refused: {work_item} at {count}/{cap} bot hops today "
                f"— the daily ceiling, which clears at 00:00 UTC")
    return (f"summon refused: {work_item} at {count}/{cap} bot hops "
            f"— parked until a human posts on it")


class HopLedger:
    """The per-work-item hop counter, persisted and restart-proof."""

    def __init__(self, path: str, *, hop_cap: int = DEFAULT_HOP_CAP,
                 daily_hop_cap: int = DEFAULT_DAILY_HOP_CAP,
                 today_fn: Optional[Callable[[], str]] = None) -> None:
        self.path = path
        self.hop_cap = hop_cap
        self.daily_hop_cap = daily_hop_cap
        self._today_fn = today_fn or (
            lambda: _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"))
        self._lock = threading.Lock()

    # ------------------------------------------------------------- state io

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: dict) -> None:
        parent = os.path.dirname(self.path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _record(self, state: dict, work_item: str) -> dict:
        rec = state.get(work_item)
        if not isinstance(rec, dict):
            rec = {}
        today = self._today_fn()
        if rec.get("day") != today:
            rec["day"] = today
            rec["day_hops"] = 0
        rec.setdefault("hops", 0)
        rec.setdefault("unpark_seq", 0)
        state[work_item] = rec
        return rec

    # -------------------------------------------------------------- public

    def spend(self, work_item: str) -> dict:
        """Charge one hop."""
        with self._lock:
            state = self._load()
            rec = self._record(state, work_item)
            hops, day_hops = int(rec["hops"]), int(rec["day_hops"])
            if day_hops >= self.daily_hop_cap:
                return {"allowed": False, "reason": "daily-ceiling",
                        "count": day_hops, "cap": self.daily_hop_cap,
                        "refusal": format_hop_refusal(
                            work_item=work_item, count=day_hops,
                            cap=self.daily_hop_cap, daily=True)}
            if hops >= self.hop_cap:
                return {"allowed": False, "reason": "parked",
                        "count": hops, "cap": self.hop_cap,
                        "refusal": format_hop_refusal(
                            work_item=work_item, count=hops, cap=self.hop_cap)}
            rec["hops"] = hops + 1
            rec["day_hops"] = day_hops + 1
            self._save(state)
            return {"allowed": True, "reason": "hop", "count": rec["hops"],
                    "cap": self.hop_cap, "day_count": rec["day_hops"],
                    "day_cap": self.daily_hop_cap}

    def unpark(self, work_item: str, seq: Optional[int] = None) -> dict:
        """A human posted on this work item: the chain resumes at 0/cap."""
        with self._lock:
            state = self._load()
            rec = self._record(state, work_item)
            if seq is not None and seq <= int(rec["unpark_seq"]):
                return {"reset": False, "count": int(rec["hops"]),
                        "cap": self.hop_cap}
            rec["hops"] = 0
            if seq is not None:
                rec["unpark_seq"] = int(seq)
            self._save(state)
            return {"reset": True, "count": 0, "cap": self.hop_cap,
                    "day_count": int(rec["day_hops"]),
                    "day_cap": self.daily_hop_cap}


class WakeVerbs:

    # -------------------------------------------------------- wake config

    def _parse_wake_callers(self) -> frozenset[str]:
        """`[wake].callers` — the identities that may wake a seat."""
        callers = self.wake.get("callers")
        if not isinstance(callers, list) or not callers or not all(
                isinstance(c, str) and c for c in callers):
            raise ConfigError(
                "[wake] is configured but wake.callers is missing or is not a "
                "non-empty list of identity names; refusing to start")
        mapped = {v for v in self.uid_map.values() if isinstance(v, str)}
        for caller in callers:
            if _BUILD_CALLER_RE.match(caller):
                raise ConfigError(
                    f"wake.callers names the resident seat {caller!r}: a seat "
                    "may never wake anyone (nothing self-wakes). Refusing to "
                    "start.")
            if caller not in mapped:
                raise ConfigError(
                    f"wake.callers names {caller!r}, which has no uid in "
                    "[uids]: it could never be authenticated at the socket, so "
                    "the wake surface would read as armed while refusing every "
                    "call. Add the uid or drop the name.")
        return frozenset(callers)

    def _parse_wake_seats(self) -> frozenset[str]:
        """`[wake].residents` — the seats that may BE woken."""
        seats = self.wake.get("residents")
        if not isinstance(seats, list) or not seats or not all(
                isinstance(s, str) and s for s in seats):
            raise ConfigError(
                "[wake] is configured but wake.residents is missing or is not "
                "a non-empty list of seat names; refusing to start")
        for seat in seats:
            if seat not in self.seat_names:
                raise ConfigError(
                    f"wake.residents names {seat!r}, which is not a resident "
                    "of this house ([uids] / [residents]); refusing to start")
        return frozenset(seats)

    # ---------------------------------------------------------------- wake

    def _check_wake_identity(self, resident: str, verb: str) -> Optional[str]:
        """None if this identity may attempt this verb, else the refusal text."""
        is_waker = resident in self.wake_callers
        if verb == WAKE_VERB and not is_waker:
            return (f"{resident} may not wake anyone — the wake caller is "
                    "authenticated by uid at the socket, and no seat is one")
        if is_waker and verb != WAKE_VERB:
            return (f"{resident} is a wake caller and may call only "
                    f"{WAKE_VERB!r}")
        return None

    def _wake_spool_dir(self) -> str:
        """The spool realpath VERIFIED resident-unwritable at construction — never
        the raw config string, for the reason _specs_dir prefers its verified path:
        the directory written must be the directory proven."""
        if not self.wake_spool_real:
            raise VerbError("internal", "wake.spool_dir is not configured")
        return self.wake_spool_real

    def _write_wake_record(self, record: dict) -> str:
        """One 0644 JSON record per wake, written atomically."""
        path = os.path.join(self._wake_spool_dir(),
                            record["wake_id"] + WAKE_SPOOL_SUFFIX)
        fd, tmp = tempfile.mkstemp(dir=self._wake_spool_dir(),
                                   prefix=".wake-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False)
                fh.write("\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    def _read_wake_records(self) -> list[dict]:
        """Every parseable record in the spool."""
        try:
            names = sorted(os.listdir(self._wake_spool_dir()))
        except (OSError, VerbError):
            return []
        out: list[dict] = []
        for name in names:
            if not name.endswith(WAKE_SPOOL_SUFFIX):
                continue
            try:
                with open(os.path.join(self._wake_spool_dir(), name), "r",
                          encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict) and _WAKE_ID_RE.match(str(rec.get("wake_id"))):
                out.append(rec)
        return out

    @staticmethod
    def _wake_requested_epoch(record: dict) -> Optional[float]:
        """When the wake was asked for."""
        try:
            started = _dt.datetime.fromisoformat(str(record.get("requested_at")))
        except (TypeError, ValueError):
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=_dt.timezone.utc)
        return started.timestamp()

    @classmethod
    def _wake_window_ends(cls, record: dict) -> Optional[float]:
        """When this wake stops being in flight: requested_at + the session cap +
        the grace margin."""
        started = cls._wake_requested_epoch(record)
        if started is None:
            return None
        cap = record.get("session_cap_sec")
        grace = record.get("grace_sec")
        cap = cap if isinstance(cap, int) and cap > 0 else DEFAULT_WAKE_SESSION_CAP_SEC
        grace = grace if isinstance(grace, int) and grace >= 0 else DEFAULT_WAKE_GRACE_SEC
        return started + cap + grace

    def _prune_wake_spool(self, now: Optional[float] = None) -> int:
        """Delete records past the RETENTION horizon (not past their window)."""
        now = now if now is not None else time.time()
        removed = 0
        for rec in self._read_wake_records():
            started = self._wake_requested_epoch(rec)
            if started is not None and started + WAKE_RETENTION_SEC > now:
                continue
            try:
                os.unlink(os.path.join(self._wake_spool_dir(),
                                       str(rec["wake_id"]) + WAKE_SPOOL_SUFFIX))
                removed += 1
            except OSError:
                pass
        return removed

    def _daily_wake_cap(self) -> int:
        """`[wake].daily_wake_cap`, else DEFAULT_DAILY_WAKE_CAP."""
        cap = self.wake.get("daily_wake_cap", DEFAULT_DAILY_WAKE_CAP)
        if isinstance(cap, bool) or not isinstance(cap, int):
            return DEFAULT_DAILY_WAKE_CAP
        return cap

    def _wake_spend_today(self, seat: str, now: float) -> tuple[int, float]:
        """(wakes, session seconds) recorded for this seat so far today (UTC)."""
        count = 0
        spent = 0.0
        today = _dt.datetime.fromtimestamp(
            now, _dt.timezone.utc).strftime("%Y-%m-%d")
        for rec in self._read_wake_records():
            if rec.get("resident") != seat:
                continue
            started = self._wake_requested_epoch(rec)
            if started is None:
                continue
            if _dt.datetime.fromtimestamp(
                    started, _dt.timezone.utc).strftime("%Y-%m-%d") != today:
                continue
            granted = rec.get("session_cap_sec")
            if isinstance(granted, bool) or not isinstance(granted, int):
                granted = 0
            count += 1
            spent += max(0.0, min(float(granted), now - started))
        return count, spent

    def _active_wake(self, resident: str) -> Optional[dict]:
        """The most recent wake still in flight for this seat, if any."""
        if not self.wake:
            return None
        now = time.time()
        live = [r for r in self._read_wake_records()
                if r.get("resident") == resident
                and (self._wake_window_ends(r) or 0) > now]
        if not live:
            return None
        return max(live, key=lambda r: str(r.get("requested_at", "")))

    def _assert_woken_build_allowed(self, resident: str, wake: dict,
                                    text: str) -> None:
        """The no-self-review rule, one level down (spec)."""
        raw = parse_review_owner(text)
        if raw is None:
            raise _bad(
                f"woken session ({wake.get('wake_id')}): this spec states no "
                "review owner, so it cannot be shown that the review does not "
                "land in the building seat's own queue — a woken build needs a "
                "'Review owner' line")
        owner_seat = review_owner_seat(raw, self.seat_names)
        if owner_seat is None:
            return
        if owner_seat == resident:
            raise _bad(
                f"woken session ({wake.get('wake_id')}): this spec's review "
                f"owner is {raw!r}, which is this seat — a seat may not build "
                "what it would then review")
        woken_by = str(wake.get("woken_by") or "")
        if woken_by in self.seat_names and owner_seat == woken_by:
            raise _bad(
                f"woken session ({wake.get('wake_id')}): this spec's review "
                f"owner is {raw!r}, which is the seat that woke this one — the "
                "review would land in the waker's queue")

    def _verb_wake(self, caller: str, args: dict) -> tuple[dict, str, dict]:
        """Wake a seat with a task (SPECS/2026-08-25-agentic-residents.md)."""
        _reject_unknown(args, {"resident", "task"})
        seat = _check_str(args, "resident", required=True, max_len=64)
        task = _check_str(args, "task", required=True,
                          max_len=MAX_WAKE_TASK_CHARS)
        assert seat is not None and task is not None
        if seat not in self.wake_seats:
            raise _bad(f"{seat!r} is not a wakeable seat "
                       f"({', '.join(sorted(self.wake_seats)) or 'none'} are)")
        if not task.strip():
            raise _bad("task must not be empty — a wake names the work")

        now = _dt.datetime.now(_dt.timezone.utc)
        cap = self._daily_wake_cap()
        record = {
            "schema": WAKE_SPOOL_SCHEMA,
            "wake_id": new_wake_id(now),
            "resident": seat,
            "woken_by": caller,
            "requested_at": now.isoformat(),
            "session_cap_sec": self._wake_session_cap(),
            "grace_sec": self._wake_grace(),
            "task": task,
        }
        with self._wake_lock:
            count, spent = self._wake_spend_today(seat, now.timestamp())
            if count >= cap:
                raise VerbError("over-budget", format_wake_refusal(
                    seat=seat, count=count, cap=cap, spent_sec=spent))
            try:
                self._write_wake_record(record)
            except OSError as exc:
                raise VerbError("exec-failure",
                                f"cannot record the wake: {exc}") from None
        self._prune_wake_spool()

        return (
            {"wake_id": record["wake_id"], "resident": seat,
             "session_cap_sec": record["session_cap_sec"],
             "grace_sec": record["grace_sec"],
             "requested_at": record["requested_at"]},
            f"wake {record['wake_id']} recorded for {seat} by {caller} "
            f"(cap {record['session_cap_sec']}s)",
            # A FACT field, like start-build's `build_started`: the wake id is what
            # ties this line to the action log's start/end pair and to the
            # #custodian post the seat's runner makes.
            {"wake_id": record["wake_id"]},
        )

    # ------------------------------------------------------- summon hops

    def _work_item_bucket(self, work_item: Optional[str]) -> Optional[str]:
        """The work item this chain spends against, or None for no bucket."""
        if not work_item or self.hops is None:
            return None
        if not BOARD_SLUG_RE.match(work_item):
            return None
        try:
            body = self._board_get(f"/planroom/cards/{work_item}")
        except VerbError:
            return None
        card = body.get("card") or {}
        return work_item if card.get("column") == "Review" else None

    def _unpark_refusal(self, work_item: str, seq: int) -> Optional[str]:
        """Why the cited post cannot unpark `work_item`, or None if it can.
        The broker reads the post itself: the reporting adapter runs as the
        same res-* uid as the model it gates, so its word is no evidence."""
        channel = self.disjorn.get("custodian_channel_id")
        if not isinstance(channel, int) or channel < 1:
            return "this broker has no #custodian to read the post from"
        try:
            post = self._build_message(channel, seq, act="unpark a hop chain")
        except VerbError as exc:
            return exc.message
        cited = re.search(rf"(?<![\w-]){re.escape(work_item)}(?![\w-])",
                          post["content"])
        if cited is None:
            return f"message {seq} does not cite {work_item}"
        return None

    def _verb_summon_hop(self, resident: str, args: dict) -> tuple[dict, str]:
        """The bot-to-bot hop wall, for the summon adapters."""
        _reject_unknown(args, {"action", "work_item", "summoner", "seq"})
        action = _check_str(args, "action", required=True, max_len=10)
        if action not in ("spend", "unpark"):
            raise _bad("action must be 'spend' or 'unpark'")
        work_item = _check_str(args, "work_item", max_len=80)
        summoner = _check_str(args, "summoner", max_len=100) or "someone"
        seq = args.get("seq")
        if seq is not None:
            seq = _check_int(args, "seq", 0, 0, 2**53)

        if action == "unpark":
            if not work_item:
                raise _bad("unpark needs a work_item")
            if self.hops is None:
                return ({"reset": False, "reason": "no-wall"},
                        "no hop wall configured")
            if seq is None:
                raise _bad("unpark needs the seq of the human post")
            refused = self._unpark_refusal(work_item, seq)
            if refused:
                return ({"work_item": work_item, "reset": False,
                         "reason": "not-a-human-post", "refusal": refused},
                        f"unpark of {work_item} refused: {refused}")
            out = self.hops.unpark(work_item, seq)
            verb = ("unparked by" if out["reset"]
                    else "already unparked, reported again by")
            return ({"work_item": work_item, **out},
                    f"{work_item} {verb} {summoner}")

        bucket = self._work_item_bucket(work_item)
        if bucket is None:
            return ({"allowed": True, "chain": False, "work_item": work_item,
                     "reason": "no-bucket"},
                    f"depth-1 only for {summoner} (no live work item)")
        out = self.hops.spend(bucket)
        result = {"chain": bool(out["allowed"]), "work_item": bucket, **out}
        summary = (f"hop {out['count']}/{out['cap']} on {bucket} for {summoner}"
                   if out["allowed"] else
                   f"REFUSED {out['reason']} {out['count']}/{out['cap']} on {bucket}")
        return (result, summary)
