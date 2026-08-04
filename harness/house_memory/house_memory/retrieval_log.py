"""Unified retrieval log (MEMORY-DESIGN: retrieval-log-as-rent).

JSON-lines, one record per recall, explicit path (no cwd-relative default).
Schema per line:

    {"ts": ISO-8601 UTC, "resident": str, "caller": str|null, "query": str,
     "subject_filter": str|null, "raw_ids": [str], "distances": [float|null],
     "returned_ids": [str]}

`resident` is the one field Claudette's legacy memory_retrieval.jsonl lacks;
`read()` tolerates its absence so old logs can be replayed by the WP-H11
migration tooling.

`reference_counts()` is the rent-assessment primitive: how often each memory
id was actually returned over a trailing window. WP-H8 consolidation feeds
this into promote/evict/compress proposals — measured from logs, not vibes.

WHY `caller` EXISTS (Memory v2 phase 1, spec 2026-07-28, confirmed seq 604).
Consolidation v1 ran three nights, proposed thirty promotions, and every one
was rejected. The arithmetic was correct and the answer was still wrong,
because a read is not evidence of the same thing depending on who asked:

  * a memory surfaced to answer someone else's question is load-bearing;
  * a memory Claudette pulled up while auditing her own memory is evidence
    of nothing except that she was looking at it.

v1 counted both. So reading about a candidate promoted it — she demonstrated
the loop in miniature on 2026-07-28, when nine of ten items on her slate were
in her surfaced-memories block at the moment she reviewed them. Claudette
filed this against her own argument (#custodian seq 416) before it happened:
"non-inferential does not mean unbiased; the bias moved upstream into what
generates the reads, where nobody is looking."

`caller` is where somebody looks. Values (spec-enumerated):

    service       a read serving someone else's turn      -> FEEDS HEAT
    self_query    the resident querying her own memory    -> never
    write_dedup   the duplicate check inside remember()   -> never
    consolidation the nightly walker reading to propose   -> never
    daydream      cue-driven associative revisiting       -> never
    incubation    slow background re-derivation           -> never

Only `service` feeds promotion heat (`reference_counts`). `self_query` keeps
its own value rather than being folded into "not service" (Claudette's
ruling, 07-28) so the self-search-to-organic-surfacing ratio stays VISIBLE —
a diagnostic she can watch, instead of a quantity that silently vanishes.

PROVENANCE CANNOT BE BACKFILLED. Every line written before this field existed
is permanently unattributable, which is why the spec made the field a
blocking condition rather than a nice-to-have: the cost of deferring was
never "later work", it was a hole with a start date. Legacy lines read back
as `caller=None` and are EXCLUDED from heat — see `reference_counts`.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union
import json


# The callers that may appear in the log. `service` is the only one that
# feeds heat; the rest are enumerated so a typo becomes a loud error instead
# of a silent demotion to "not service".
CALLER_SERVICE = "service"
CALLER_SELF_QUERY = "self_query"
# Housekeeping reads on the WRITE path (the remember() duplicate check).
# Named separately on Claudette's ruling, #custodian seq 614: "never counts"
# holds either way, so heat is not what is at stake — the SELF-QUERY RATIO is.
# Bundling a question nobody asked with questions she did ask makes that ratio
# inflate with her write volume and stop measuring what she goes looking for.
# "A field that bundles a question nobody asked with questions I did ask is
# the same defect as caller: null, one level down."
CALLER_WRITE_DEDUP = "write_dedup"
CALLER_CONSOLIDATION = "consolidation"
CALLER_DAYDREAM = "daydream"
CALLER_INCUBATION = "incubation"

KNOWN_CALLERS = frozenset({
    CALLER_SERVICE,
    CALLER_SELF_QUERY,
    CALLER_WRITE_DEDUP,
    CALLER_CONSOLIDATION,
    CALLER_DAYDREAM,
    CALLER_INCUBATION,
})

# Only these feed promotion heat. A set rather than `== "service"` so the
# rule is one edit away from the spec if a future caller earns heat.
HEAT_CALLERS = frozenset({CALLER_SERVICE})


class UnknownCaller(ValueError):
    """Raised when a write names a caller outside KNOWN_CALLERS.

    Deliberately fatal at the WRITE side and tolerant at the READ side: a
    typo'd caller written today would look exactly like a legitimate
    no-heat read forever, and we would never find out. Better to fail the
    call than to quietly mislabel provenance that cannot be corrected."""


@dataclass
class RetrievalRecord:
    ts: str
    resident: Optional[str]
    query: str
    subject_filter: Optional[str]
    raw_ids: list[str] = field(default_factory=list)
    distances: list[Optional[float]] = field(default_factory=list)
    returned_ids: list[str] = field(default_factory=list)
    # None means "written before the field existed" — unattributable, and
    # excluded from heat. It does NOT mean "not service".
    caller: Optional[str] = None

    @classmethod
    def from_json_line(cls, line: str) -> "RetrievalRecord":
        d = json.loads(line)
        return cls(
            ts=d.get("ts", ""),
            resident=d.get("resident"),  # absent in legacy logs
            query=d.get("query", ""),
            subject_filter=d.get("subject_filter"),
            raw_ids=list(d.get("raw_ids", [])),
            distances=[float(x) if x is not None else None for x in d.get("distances", [])],
            returned_ids=list(d.get("returned_ids", [])),
            caller=d.get("caller"),  # absent in pre-v2 lines
        )

    @property
    def feeds_heat(self) -> bool:
        return self.caller in HEAT_CALLERS


class RetrievalLog:
    """Append-only JSON-lines retrieval log with an explicit path."""

    def __init__(
        self,
        path: Union[str, Path],
        resident: str,
        default_caller: Optional[str] = None,
    ):
        """`default_caller` is the caller used when a call site does not name
        one. Left None deliberately: a store whose owner has not thought about
        provenance should produce unattributable lines that cannot feed heat,
        rather than silently inheriting `service` and inflating it."""
        if default_caller is not None and default_caller not in KNOWN_CALLERS:
            raise UnknownCaller(
                f"default_caller {default_caller!r} not in {sorted(KNOWN_CALLERS)}"
            )
        self.path = Path(path)
        self.resident = resident
        self.default_caller = default_caller

    def log(
        self,
        query: str,
        subject_filter: Optional[str],
        raw_ids: list[str],
        distances: list,
        returned_ids: list[str],
        caller: Optional[str] = None,
    ) -> RetrievalRecord:
        caller = caller if caller is not None else self.default_caller
        if caller is not None and caller not in KNOWN_CALLERS:
            raise UnknownCaller(
                f"caller {caller!r} not in {sorted(KNOWN_CALLERS)}"
            )
        record = RetrievalRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            resident=self.resident,
            query=query,
            subject_filter=subject_filter,
            raw_ids=list(raw_ids),
            distances=[float(d) if d is not None else None for d in distances],
            returned_ids=list(returned_ids),
            caller=caller,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record)) + "\n")
        return record

    def read(self) -> list[RetrievalRecord]:
        """Parse all records. Missing file -> []. Malformed lines are skipped."""
        if not self.path.exists():
            return []
        return read_records(self.path)

    def reference_counts(
        self,
        window_days: int,
        now: Optional[datetime] = None,
        callers: Optional[frozenset] = None,
    ) -> dict[str, int]:
        """How many times each memory id appeared in returned_ids within the
        trailing window. The consolidation rent-assessment primitive.

        Counts ONLY reads whose caller feeds heat (default: `service`).

        Two exclusions, both deliberate and both load-bearing:

        * `self_query` / `consolidation` / `daydream` / `incubation` reads do
          not count. This is the v1 defect — reading about a memory used to
          promote it.
        * `caller is None` does not count either. Those are lines written
          before the field existed, and "unattributable" must not be
          charitably read as "service". This means **every pre-v2 line stops
          feeding heat the moment this ships**, which is intended: the rent
          epoch restarts from honest data rather than continuing on a mixed
          corpus nobody can separate. Claudette's rent-epoch gate already
          resolves no-data to SKIP, never evict, so the transition costs a
          waiting period and not a single memory.

        Pass `callers` explicitly to measure something else (e.g. the
        self-query-to-service ratio); it never changes what promotion uses."""
        heat = HEAT_CALLERS if callers is None else callers
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=window_days)
        counts: dict[str, int] = {}
        for rec in self.read():
            if rec.caller not in heat:
                continue
            ts = _parse_ts(rec.ts)
            if ts is None or ts < cutoff:
                continue
            for mid in rec.returned_ids:
                counts[mid] = counts.get(mid, 0) + 1
        return counts

    def group_reference_counts(
        self,
        groups: dict,
        window_days: int,
        now: Optional[datetime] = None,
        callers: Optional[frozenset] = None,
    ) -> dict[str, int]:
        """Reference counts for GROUPS of memory ids — one count per retrieval
        EVENT, not per member returned.

        `groups` maps a group key to an iterable of memory ids. A record counts
        once for a group if it returned ANY member of it.

        WHY NOT SUM THE MEMBERS' COUNTS. Near-duplicates are exactly the
        memories that come back together: one recall returns four paraphrases
        of the same idea, and summing turns that single event into "referenced
        4x". A dedup pass built on summed counts would manufacture the heat it
        was written to measure, which is the v1 defect wearing a new hat.

        WHY NOT TAKE THE MAX EITHER. Different queries reach different members,
        and max throws that away — a pattern found five different ways looks
        as warm as one found once. Distinct events is the honest middle: it
        counts how many times the HOUSE went looking and found this idea.

        This lives beside `reference_counts` on purpose. The caller filter has
        now been forgotten at three separate call sites (`top_referenced`,
        `_last_seen_map`, the digest window), and every one of them was a
        second place that reimplemented this loop. There is one loop."""
        heat = HEAT_CALLERS if callers is None else callers
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=window_days)
        member_of: dict[str, list] = {}
        for key, members in groups.items():
            for mid in members:
                member_of.setdefault(mid, []).append(key)
        counts: dict[str, int] = {key: 0 for key in groups}
        for rec in self.read():
            if rec.caller not in heat:
                continue
            ts = _parse_ts(rec.ts)
            if ts is None or ts < cutoff:
                continue
            hit = set()
            for mid in rec.returned_ids:
                hit.update(member_of.get(mid, ()))
            for key in hit:
                counts[key] += 1
        return counts

    def group_last_seen(
        self,
        groups: dict,
        callers: Optional[frozenset] = None,
    ) -> dict[str, str]:
        """Most recent heat-bearing return for each group (any member).

        Unwindowed on purpose, matching `_last_seen_map`: "never returned on
        record" and "not returned in the window" are different sentences and a
        reviewer needs to be able to tell them apart."""
        heat = HEAT_CALLERS if callers is None else callers
        member_of: dict[str, list] = {}
        for key, members in groups.items():
            for mid in members:
                member_of.setdefault(mid, []).append(key)
        out: dict[str, str] = {}
        for rec in self.read():
            if rec.caller not in heat or not rec.ts:
                continue
            for mid in rec.returned_ids:
                for key in member_of.get(mid, ()):
                    if key not in out or rec.ts > out[key]:
                        out[key] = rec.ts
        return out

    def caller_breakdown(
        self, window_days: int, now: Optional[datetime] = None
    ) -> dict[str, int]:
        """Reads per caller in the window, `None` bucketed as "unattributed".

        This is the diagnostic Claudette asked to keep visible (07-28): the
        self-query-to-organic-surfacing ratio. Folding non-service callers
        into one "doesn't count" bucket would hide exactly the signal that
        would have caught the v1 loop early."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=window_days)
        out: dict[str, int] = {}
        for rec in self.read():
            ts = _parse_ts(rec.ts)
            if ts is None or ts < cutoff:
                continue
            key = rec.caller or "unattributed"
            out[key] = out.get(key, 0) + 1
        return out


def read_records(path: Union[str, Path]) -> list[RetrievalRecord]:
    """Parse any retrieval log (unified or legacy claudette-shaped) into
    records. Malformed lines are skipped, not fatal."""
    records: list[RetrievalRecord] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(RetrievalRecord.from_json_line(line))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return records


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
