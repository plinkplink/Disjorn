"""Unified retrieval log (MEMORY-DESIGN: retrieval-log-as-rent).

JSON-lines, one record per recall, explicit path (no cwd-relative default).
Schema per line:

    {"ts": ISO-8601 UTC, "resident": str, "query": str,
     "subject_filter": str|null, "raw_ids": [str], "distances": [float|null],
     "returned_ids": [str]}

`resident` is the one field Claudette's legacy memory_retrieval.jsonl lacks;
`read()` tolerates its absence so old logs can be replayed by the WP-H11
migration tooling.

`reference_counts()` is the rent-assessment primitive: how often each memory
id was actually returned over a trailing window. WP-H8 consolidation feeds
this into promote/evict/compress proposals — measured from logs, not vibes.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union
import json


@dataclass
class RetrievalRecord:
    ts: str
    resident: Optional[str]
    query: str
    subject_filter: Optional[str]
    raw_ids: list[str] = field(default_factory=list)
    distances: list[Optional[float]] = field(default_factory=list)
    returned_ids: list[str] = field(default_factory=list)

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
        )


class RetrievalLog:
    """Append-only JSON-lines retrieval log with an explicit path."""

    def __init__(self, path: Union[str, Path], resident: str):
        self.path = Path(path)
        self.resident = resident

    def log(
        self,
        query: str,
        subject_filter: Optional[str],
        raw_ids: list[str],
        distances: list,
        returned_ids: list[str],
    ) -> RetrievalRecord:
        record = RetrievalRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            resident=self.resident,
            query=query,
            subject_filter=subject_filter,
            raw_ids=list(raw_ids),
            distances=[float(d) if d is not None else None for d in distances],
            returned_ids=list(returned_ids),
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
        self, window_days: int, now: Optional[datetime] = None
    ) -> dict[str, int]:
        """How many times each memory id appeared in returned_ids within the
        trailing window. The consolidation rent-assessment primitive."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=window_days)
        counts: dict[str, int] = {}
        for rec in self.read():
            ts = _parse_ts(rec.ts)
            if ts is None or ts < cutoff:
                continue
            for mid in rec.returned_ids:
                counts[mid] = counts.get(mid, 0) + 1
        return counts


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
