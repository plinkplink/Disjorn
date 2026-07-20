"""The consolidation pass itself: read three inputs, emit proposals.

Inputs (all READ-ONLY):
  1. episodic store   — house_memory.MemoryStore, via `export_all()` (no embed)
  2. retrieval log    — house_memory.RetrievalLog, for reference counts
  3. markdown spine   — house_memory.Spine (read side)

Output: a `ConsolidationReport` (a batch of proposals). Nothing is written.

Reference-count keying (design decision — MEMORY-DESIGN left the mechanism
open; see INTEGRATION-NEEDS.md): the unified retrieval log records `returned_ids`.
Episodic memories are keyed by their uuid; spine entries are keyed by their
frontmatter `name`. `reference_counts()` is agnostic — it counts whatever
string ids were returned. So promotion evidence looks up episodic ids, and
eviction/compression evidence looks up spine entry names. This is why WP-H7's
spine retrieval-on-demand MUST log the spine entry name it served into the
same log's `returned_ids` — otherwise every spine entry reads as unreferenced.
Until then the age guard (`min_spine_age_days`) keeps young entries out of the
removal set, and — decisive — nothing is ever acted on without human review.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from house_memory import Memory, RetrievalLog, Spine, SpineEntry

from consolidation.config import ConsolidationConfig
from consolidation.embedders import NullEmbedder
from consolidation.model import (
    ConsolidationReport,
    Evidence,
    Proposal,
    ProposalKind,
)


def build_proposals(
    cfg: ConsolidationConfig,
    *,
    now: Optional[datetime] = None,
    store=None,
    spine: Optional[Spine] = None,
    log: Optional[RetrievalLog] = None,
) -> ConsolidationReport:
    """Run the consolidation pass. Inputs may be injected (tests); otherwise
    they are built read-only from `cfg`. NEVER mutates anything."""
    now = now or datetime.now(timezone.utc)

    if store is None:
        # imported lazily so tests that inject a store need no chromadb at all
        from house_memory import MemoryStore

        store = MemoryStore(
            data_dir=cfg.episodic_data_dir,
            collection_name=cfg.episodic_collection,
            embedder=NullEmbedder(),  # read-only: cannot embed, no network
        )
    if spine is None:
        spine = Spine(cfg.spine_dir)
    if log is None:
        log = RetrievalLog(cfg.retrieval_log_path, resident=cfg.resident)

    ref_counts = log.reference_counts(cfg.window_days, now=now)
    last_seen = _last_seen_map(log)

    spine_entries = spine.list_entries()
    spine_size = len(spine_entries)
    spine_bodies = [e.body.lower() for e in spine_entries]

    promotions = _promotion_proposals(
        cfg, store, ref_counts, last_seen, spine_bodies
    )
    evictions, compressions = _removal_proposals(
        cfg, spine_entries, ref_counts, last_seen, now
    )

    proposals = promotions + evictions + compressions

    report = ConsolidationReport(
        resident=cfg.resident,
        generated_at=now.isoformat(),
        window_days=cfg.window_days,
        spine_size=spine_size,
        soft_target=cfg.soft_target_spine_size,
        proposals=proposals,
    )

    _apply_soft_target_bias(cfg, report, promotions, evictions, compressions)
    return report


# ── promotions: episodic -> spine ────────────────────────────────────────────

def _promotion_proposals(
    cfg, store, ref_counts, last_seen, spine_bodies
) -> list[Proposal]:
    out: list[Proposal] = []
    for record in store.export_all():
        meta = record.get("metadata", {}) or {}
        if meta.get("superseded_by"):
            continue  # already retired; not a promotion candidate
        mem = Memory.from_chroma(record["id"], record["content"], meta)
        rc = ref_counts.get(mem.id, 0)
        if rc < cfg.promote_min_references:
            continue
        if _already_in_spine(mem.content, spine_bodies):
            continue  # the pattern is already spine; don't re-propose
        out.append(
            Proposal(
                kind=ProposalKind.PROMOTE,
                resident=cfg.resident,
                target=mem.id,
                subject=mem.subject,
                content=mem.content,
                evidence=Evidence(
                    reference_count=rc,
                    window_days=cfg.window_days,
                    last_referenced_at=last_seen.get(mem.id),
                ),
                rationale=(
                    f"episodic pattern retrieved {rc}x (>= promote threshold "
                    f"{cfg.promote_min_references}) — earning its way into the spine."
                ),
            )
        )
    # strongest evidence first (also the order the soft-target bias keeps)
    out.sort(key=lambda p: p.evidence.reference_count, reverse=True)
    if cfg.max_promotions is not None:
        out = out[: cfg.max_promotions]
    return out


# ── removals: evict / compress ───────────────────────────────────────────────

def _removal_proposals(
    cfg, spine_entries: list[SpineEntry], ref_counts, last_seen, now
) -> tuple[list[Proposal], list[Proposal]]:
    """Under-referenced spine entries become removal candidates. Constraint-
    shaped ones default to COMPRESS (anti-Chesterton's-fence); the rest EVICT.
    Constraint-shaped candidates sharing a `topic` are merged into one
    compression ('N variations of one idea -> one line')."""
    evictions: list[Proposal] = []
    compress_candidates: list[SpineEntry] = []

    for entry in spine_entries:
        if cfg.exclude_kernel and entry.kernel:
            continue  # kernel is the hardest rent; not auto-touched here
        rc = ref_counts.get(entry.name, 0)
        if rc > cfg.evict_max_references:
            continue  # still earning its keep
        if cfg.min_spine_age_days > 0 and _entry_age_days(entry, now) < cfg.min_spine_age_days:
            continue  # too young to judge unreferenced over the window

        if _is_constraint_shaped(entry, cfg):
            compress_candidates.append(entry)
        else:
            evictions.append(_evict_proposal(cfg, entry, rc, last_seen))

    compressions = _compress_proposals(cfg, compress_candidates, ref_counts, last_seen)
    return evictions, compressions


def _evict_proposal(cfg, entry, rc, last_seen) -> Proposal:
    return Proposal(
        kind=ProposalKind.EVICT,
        resident=cfg.resident,
        target=entry.name,
        subject=str(entry.meta.get("subject", entry.name)),
        content=entry.body,
        evidence=Evidence(
            reference_count=rc,
            window_days=cfg.window_days,
            last_referenced_at=last_seen.get(entry.name),
        ),
        rationale=(
            "spine entry has not earned its keep (unreferenced over the window) "
            "and is not constraint-shaped — a spine that never shrinks is a hoard."
        ),
    )


def _compress_proposals(cfg, candidates, ref_counts, last_seen) -> list[Proposal]:
    """Group constraint-shaped candidates by `topic` frontmatter; each group of
    2+ becomes one merge-compress, singletons a plain compress. The WHY is
    always kept — only tightened."""
    by_topic: dict[str, list[SpineEntry]] = {}
    singles: list[SpineEntry] = []
    for entry in candidates:
        topic = entry.meta.get("topic")
        if topic:
            by_topic.setdefault(str(topic), []).append(entry)
        else:
            singles.append(entry)

    out: list[Proposal] = []
    for topic, group in by_topic.items():
        if len(group) == 1:
            singles.append(group[0])
            continue
        names = [e.name for e in group]
        rc = max(ref_counts.get(n, 0) for n in names)
        last = _latest([last_seen.get(n) for n in names])
        merged_body = " / ".join(e.body.strip().splitlines()[0] if e.body.strip() else e.name for e in group)
        out.append(
            Proposal(
                kind=ProposalKind.COMPRESS,
                resident=cfg.resident,
                target=f"topic:{topic}",
                subject=topic,
                content=merged_body,
                evidence=Evidence(rc, cfg.window_days, last),
                rationale=(
                    f"{len(group)} constraint-shaped variations of one idea "
                    f"('{topic}'), all under-referenced — merge to one line. "
                    "Compress, don't evict: the constraint's WHY is load-bearing."
                ),
                constraint_shaped=True,
                members=names,
            )
        )
    for entry in singles:
        rc = ref_counts.get(entry.name, 0)
        out.append(
            Proposal(
                kind=ProposalKind.COMPRESS,
                resident=cfg.resident,
                target=entry.name,
                subject=str(entry.meta.get("subject", entry.name)),
                content=entry.body,
                evidence=Evidence(rc, cfg.window_days, last_seen.get(entry.name)),
                rationale=(
                    "under-referenced but constraint-shaped (lesson/why/promise): "
                    "defaults to compression, never eviction — evict the 'why' and "
                    "someone later removes the constraint it explained."
                ),
                constraint_shaped=True,
            )
        )
    return out


# ── soft-target bias ─────────────────────────────────────────────────────────

def _apply_soft_target_bias(cfg, report, promotions, evictions, compressions):
    """Over the soft target, propose >= as much reduction as addition. A bias
    on what gets SUGGESTED, never a wall on what may be approved. We hold the
    weakest-evidence promotions back so promotions <= reductions."""
    if not report.over_target:
        return
    reductions = len(evictions) + len(compressions)
    additions = len(promotions)
    if additions <= reductions:
        return
    keep = reductions
    kept_promotions = promotions[:keep]  # already sorted strongest-first
    suppressed = additions - keep
    report.proposals = kept_promotions + evictions + compressions
    report.bias_applied = True
    report.promotions_suppressed = suppressed


# ── helpers ──────────────────────────────────────────────────────────────────

def _already_in_spine(content: str, spine_bodies: list[str]) -> bool:
    needle = " ".join(content.split()).lower()
    if not needle:
        return False
    return any(needle in body for body in spine_bodies)


def _is_constraint_shaped(entry: SpineEntry, cfg) -> bool:
    """Constraint-shaped = a lesson / why / promise / rule. Detected from an
    explicit frontmatter `kind`/`shape`, a constraint tag, or a constraint
    keyword in the body. Default-to-compression hinges on this."""
    kind = str(entry.meta.get("kind", entry.meta.get("shape", ""))).lower()
    if kind in {"constraint", "lesson", "why", "promise", "rule", "boundary"}:
        return True
    tags = _entry_tags(entry)
    if any(t in cfg.constraint_tags for t in tags):
        return True
    body = entry.body.lower()
    if any(kw in body for kw in cfg.constraint_keywords):
        return True
    return False


def _entry_tags(entry: SpineEntry) -> list[str]:
    raw = entry.meta.get("tags", "")
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw).replace(",", " ").split()
    return [t.strip().lower() for t in items if t.strip()]


def _entry_age_days(entry: SpineEntry, now: datetime) -> float:
    """Age from a frontmatter `since`/`created` date if present, else the
    file's mtime. Younger-than-window entries are excluded from removal — you
    cannot call something unreferenced over a window it did not span."""
    for key in ("since", "created", "added"):
        val = entry.meta.get(key)
        if val:
            ts = _parse_iso(str(val))
            if ts is not None:
                return (now - ts).total_seconds() / 86400.0
    try:
        mtime = datetime.fromtimestamp(Path(entry.path).stat().st_mtime, tz=timezone.utc)
        return (now - mtime).total_seconds() / 86400.0
    except OSError:
        return float("inf")  # unknown age -> treat as old enough to consider


def _last_seen_map(log: RetrievalLog) -> dict[str, str]:
    """Most-recent return timestamp per id, across the WHOLE log (not just the
    window) — evidence enrichment ('last returned <date>')."""
    last: dict[str, str] = {}
    for rec in log.read():
        if not rec.ts:
            continue
        for mid in rec.returned_ids:
            prev = last.get(mid)
            if prev is None or rec.ts > prev:
                last[mid] = rec.ts
    return last


def _latest(values) -> Optional[str]:
    present = [v for v in values if v]
    return max(present) if present else None


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 date/datetime; naive values are treated as UTC."""
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
