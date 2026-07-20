"""Shared helpers for the consolidation suite (uniquely named)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from house_memory import Memory

from consolidation.config import ConsolidationConfig

FIXED_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def add_memory(store, content: str, *, subject: str = "plink", mid: str, **kw) -> Memory:
    """Remember a memory with a fixed id so tests can reference it in logs."""
    kw.setdefault("source_author", "plink")
    kw.setdefault("author_of_memory", "claudette")
    mem = Memory(content=content, subject=subject, id=mid, **kw)
    store.remember(mem)
    return mem


def write_spine_entry(
    spine_dir: Path,
    filename: str,
    body: str,
    *,
    name: str | None = None,
    kernel: bool = False,
    **frontmatter,
) -> Path:
    """Write a spine .md file with simple key:value frontmatter."""
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    lines.append(f"kernel: {'true' if kernel else 'false'}")
    for k, v in frontmatter.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append(body)
    path = spine_dir / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def append_log(
    path: Path,
    *,
    returned_ids: list[str],
    ts: datetime | None = None,
    days_ago: float | None = None,
    resident: str = "claudette",
    query: str = "q",
    subject: str | None = None,
    raw_ids: list[str] | None = None,
    now: datetime = FIXED_NOW,
) -> None:
    """Append one retrieval-log line with an explicit timestamp."""
    if ts is None:
        delta = timedelta(days=days_ago or 0)
        ts = now - delta
    rec = {
        "ts": ts.isoformat(),
        "resident": resident,
        "query": query,
        "subject_filter": subject,
        "raw_ids": raw_ids if raw_ids is not None else list(returned_ids),
        "distances": [0.1] * len(returned_ids),
        "returned_ids": list(returned_ids),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def make_config(
    *,
    store,
    spine_dir: Path,
    log_path: Path,
    resident: str = "claudette",
    active: bool = True,
    broker_cli: str = "fake-broker",
    **overrides,
) -> ConsolidationConfig:
    """Build a config pointed at the synthetic fixtures. Defaults tuned for
    removal tests: min_spine_age_days=0 so fresh files aren't age-excluded."""
    kwargs: dict = dict(
        resident=resident,
        active=active,
        episodic_data_dir=str(store.data_dir),
        episodic_collection=store.collection_name,
        retrieval_log_path=str(log_path),
        spine_dir=str(spine_dir),
        soft_target_spine_size=60,
        window_days=30,
        promote_min_references=3,
        evict_max_references=0,
        min_spine_age_days=0,
        exclude_kernel=True,
        max_promotions=10,
        broker_cli=broker_cli,
    )
    kwargs.update(overrides)
    return ConsolidationConfig(**kwargs)
