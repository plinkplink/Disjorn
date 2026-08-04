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

Absent inputs (deployment reality, and a safety property):
  * A resident may have NO on-disk spine at all — Claudette's spine is her
    system prompt, managed through her bot config, not a directory of markdown
    entries. `spine.dir` unset means "no spine": the run does the episodic
    promotion half and emits ZERO evict/compress proposals. That is enforced
    by an explicit short-circuit, not by "the loop happened to be empty", so
    "no spine dir" can never degrade into "empty spine, evict everything".
  * A spine dir that IS configured but missing on disk, or an episodic store
    dir that is missing, raises `MissingInputError`. Silently continuing would
    turn a stale path into either a phantom mass-eviction or (for the store)
    chromadb CREATING an empty collection in the resident's memory — a write.
    Read-only-by-construction means we refuse, loudly, instead.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from house_memory import (
    CALLER_CONSOLIDATION,
    HEAT_CALLERS,
    Memory,
    RetrievalLog,
    Spine,
    SpineEntry,
)

from consolidation.config import ConsolidationConfig
from consolidation.dedup import (
    DEFAULT_SPINE_CONTAINMENT,
    already_in_spine,
    cluster_records,
)
from consolidation.embedders import NullEmbedder
from consolidation.model import (
    ConsolidationReport,
    Evidence,
    Proposal,
    ProposalKind,
)


class MissingInputError(RuntimeError):
    """A CONFIGURED input path does not exist. Refuse the run rather than
    guess: a stale spine path must never read as 'empty spine', and a stale
    episodic path must never let chromadb create a fresh empty collection."""


# Sentinel so callers can inject `spine=None` meaning "this resident has no
# spine" and still be distinguished from "not injected, build it from cfg".
_UNSET = object()


def build_proposals(
    cfg: ConsolidationConfig,
    *,
    now: Optional[datetime] = None,
    store=None,
    spine=_UNSET,
    log: Optional[RetrievalLog] = None,
) -> ConsolidationReport:
    """Run the consolidation pass. Inputs may be injected (tests); otherwise
    they are built read-only from `cfg`. NEVER mutates anything."""
    with contextlib.ExitStack() as stack:
        if store is None:
            store = stack.enter_context(_read_only_store(cfg))
        return _build(cfg, now=now, store=store, spine=spine, log=log)


def _build(cfg, *, now, store, spine, log) -> ConsolidationReport:
    now = now or datetime.now(timezone.utc)

    if spine is _UNSET:
        spine = _open_spine(cfg)
    if log is None:
        # The walker declares itself. It only READS the log today, but if it
        # ever recalls through the store, those reads must not feed the heat
        # the walker is measuring — that circularity is the v1 defect.
        log = RetrievalLog(
            cfg.retrieval_log_path,
            resident=cfg.resident,
            default_caller=CALLER_CONSOLIDATION,
        )

    # Promotion no longer uses these — it pools counts per CLUSTER instead (see
    # _promotion_proposals). They stay for the removal half, which is keyed on
    # spine entry NAMES and has nothing to cluster.
    ref_counts = log.reference_counts(cfg.window_days, now=now)
    last_seen = _last_seen_map(log)

    # spine is None <=> this resident has no on-disk spine at all.
    spine_present = spine is not None
    spine_entries = spine.list_entries() if spine_present else []
    spine_size = len(spine_entries)
    spine_bodies = [e.body.lower() for e in spine_entries]

    promotions = _promotion_proposals(cfg, store, log, now, spine_bodies)
    rent_inactive_reason = (
        _rent_inactive_reason(cfg, now) if spine_present else None
    )
    if spine_present and rent_inactive_reason is None:
        # Rent is judged over its own (typically longer) window: a slow-
        # moving spine needs time to go stale. Reuse the promotion counts
        # only when the two windows coincide.
        rent_ref_counts = (
            ref_counts
            if cfg.rent_window() == cfg.window_days
            else log.reference_counts(cfg.rent_window(), now=now)
        )
        evictions, compressions = _removal_proposals(
            cfg, spine_entries, rent_ref_counts, last_seen, now
        )
    elif spine_present:
        # The no-data answer is SKIP, not evict (Claudette's question,
        # #custodian 2026-07-26): until spine reads are declared as logged
        # AND the declaration has aged a full window, zero-reference spine
        # entries mean "unmeasured", never "unreferenced".
        evictions, compressions = [], []
    else:
        # EXPLICIT short-circuit, not an incidentally-empty loop: with no
        # spine there is nothing whose rent could be assessed, so the run is
        # episodic-promotion only. "No spine dir" must never mean "evict all".
        evictions, compressions = [], []

    evictions, evictions_deferred = _apply_eviction_cap(cfg, evictions)

    proposals = promotions + evictions + compressions

    report = ConsolidationReport(
        resident=cfg.resident,
        generated_at=now.isoformat(),
        window_days=cfg.window_days,
        spine_size=spine_size,
        soft_target=cfg.soft_target_spine_size,
        proposals=proposals,
        spine_present=spine_present,
        evictions_deferred=evictions_deferred,
        rent_inactive_reason=rent_inactive_reason,
        rent_window_days=cfg.rent_window(),
    )

    _apply_soft_target_bias(cfg, report, promotions, evictions, compressions)
    return report


# ── input opening (read-only, fail loud on stale paths) ──────────────────────

@contextlib.contextmanager
def _read_only_store(cfg: ConsolidationConfig):
    """The episodic store, opened against a THROWAWAY SNAPSHOT — never the
    resident's live chroma dir.

    Why a snapshot and not just NullEmbedder: measured on the deployment host
    (2026-07-22), chromadb's `PersistentClient` rewrites parts of a store
    merely by OPENING it — `chroma.sqlite3` and the HNSW segment's
    `length.bin` both change content and every file's mtime moves. No
    consolidation code has run at that point, so `NullEmbedder` cannot prevent
    it. A job whose load-bearing property is "never writes the resident's
    memory" therefore must not open the live store at all. The snapshot also
    removes the hazard of two processes holding one sqlite store open while
    the resident is live.

    Two other guards live here:
      * a missing data_dir is a hard error — `get_or_create_collection` would
        otherwise CREATE an empty collection under a stale/typo'd path, i.e. a
        write into the resident's memory;
      * the store is still built with `NullEmbedder`, so even against the
        snapshot the pass cannot embed and cannot reach the network.
    """
    data_dir = Path(cfg.episodic_data_dir)
    if not data_dir.is_dir():
        raise MissingInputError(
            f"episodic store dir does not exist: {data_dir} "
            f"(resident {cfg.resident!r}). Refusing to run: chromadb would "
            f"CREATE it, and consolidation never writes. Fix "
            f"[episodic].data_dir in the resident's consolidation config."
        )
    # imported lazily so tests that inject a store need no chromadb at all
    from house_memory import MemoryStore

    workdir = Path(tempfile.mkdtemp(prefix="consolidation-ro-"))
    try:
        snapshot = workdir / "chroma_data"
        shutil.copytree(data_dir, snapshot)
        yield MemoryStore(
            data_dir=str(snapshot),
            collection_name=cfg.episodic_collection,
            embedder=NullEmbedder(),  # read-only: cannot embed, no network
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _open_spine(cfg: ConsolidationConfig) -> Optional[Spine]:
    """`None` when the resident has no on-disk spine (spine.dir unset) — a
    supported deployment shape (Claudette: her spine is her system prompt).
    A CONFIGURED-but-missing dir is a hard error, never a silent empty spine."""
    if not cfg.spine_dir:
        return None
    path = Path(cfg.spine_dir)
    if not path.is_dir():
        raise MissingInputError(
            f"spine dir configured but missing: {path} (resident "
            f"{cfg.resident!r}). Refusing to run: an absent spine dir must "
            f"never be read as an empty spine, or every entry would look "
            f"unreferenced. Either fix [spine].dir or unset it (leave it out) "
            f"to declare that this resident has no on-disk spine."
        )
    return Spine(path)


# ── promotions: episodic -> spine ────────────────────────────────────────────

def _promotion_proposals(
    cfg, store, log, now, spine_bodies
) -> list[Proposal]:
    """One proposal per IDEA, not one per copy of it.

    The order here is the fix, not an implementation detail: cluster first,
    THEN count. Counting first (what v1 did) tests every paraphrase against the
    threshold on its own, so a pattern the house went looking for eight times
    across four memories reads as four memories at 2 and is dropped whole. See
    consolidation/dedup.py for the three defects this covers and why memory-to-
    memory similarity is cosine while memory-to-spine is lexical.
    """
    records = [
        r for r in store.export_all()
        if not (r.get("metadata", {}) or {}).get("superseded_by")
    ]  # superseded memories are retired, not promotion candidates
    clusters = cluster_records(records, similarity=cfg.dedup_similarity)
    groups = {c.key: c.members for c in clusters}

    # Pooled over the cluster, counted once per retrieval EVENT. Both of these
    # apply the heat-caller filter inside house_memory rather than here, which
    # is the whole reason they live there — this house has now lost the filter
    # at three separate sites that each reimplemented the loop locally.
    ref_counts = log.group_reference_counts(groups, cfg.window_days, now=now)
    last_seen = log.group_last_seen(groups)

    by_id = {r["id"]: r for r in records}
    out: list[Proposal] = []
    for cluster in clusters:
        rc = ref_counts.get(cluster.key, 0)
        if rc < cfg.promote_min_references:
            continue
        if _already_in_spine(cluster.content, spine_bodies, cfg.spine_containment):
            continue  # the pattern is already spine; don't re-propose
        rep = by_id[cluster.representative]
        mem = Memory.from_chroma(
            rep["id"], rep["content"], rep.get("metadata", {}) or {}
        )
        if cluster.size > 1:
            rationale = (
                f"{cluster.size} near-identical episodic memories about "
                f"{mem.subject or 'this subject'} say one thing, retrieved {rc}x "
                f"across the window (>= promote threshold "
                f"{cfg.promote_min_references}) — one pattern, one proposal. "
                f"The count is retrieval EVENTS, not copies returned: near-"
                f"duplicates come back together, and counting each copy would "
                f"manufacture the heat this is measuring."
            )
        else:
            rationale = (
                f"episodic pattern retrieved {rc}x (>= promote threshold "
                f"{cfg.promote_min_references}) — earning its way into the spine."
            )
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
                    last_referenced_at=last_seen.get(cluster.key),
                    cluster_size=cluster.size,
                ),
                rationale=rationale,
                # The copies this proposal stands in for. Shown to the reviewer:
                # a merge they cannot see is a merge they cannot check.
                members=cluster.others,
            )
        )
    # strongest evidence first (also the order the soft-target bias keeps).
    # Ties break on id so two equally-warm clusters do not swap places between
    # runs and read as churn.
    out.sort(key=lambda p: (-p.evidence.reference_count, p.target))
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
            window_days=cfg.rent_window(),
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
                evidence=Evidence(rc, cfg.rent_window(), last),
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
                evidence=Evidence(rc, cfg.rent_window(), last_seen.get(entry.name)),
                rationale=(
                    "under-referenced but constraint-shaped (lesson/why/promise): "
                    "defaults to compression, never eviction — evict the 'why' and "
                    "someone later removes the constraint it explained."
                ),
                constraint_shaped=True,
            )
        )
    return out


# ── rent-epoch gate ──────────────────────────────────────────────────────────

def _rent_inactive_reason(cfg, now) -> Optional[str]:
    """Why rent assessment (evict/compress) must not run, or None if it may.

    Zero references can mean two opposite things: "measured and unused" or
    "never measured". The arithmetic cannot tell them apart, so the boundary
    is DECLARED, never inferred (the seats rule, applied to telemetry):
    `spine_reads_logged_since` is set by the operator on the day spine reads
    start landing in the retrieval log (INTEGRATION-NEEDS §1), and rent stays
    off until that epoch has aged one full window. Fail closed on an
    unparseable date — a garbled epoch must not read as "measured"."""
    raw = cfg.spine_reads_logged_since
    if not raw:
        return (
            "spine-read logging epoch not declared "
            "(spine_reads_logged_since unset; INTEGRATION-NEEDS §1)"
        )
    epoch = _parse_iso(str(raw))
    if epoch is None:
        return f"spine_reads_logged_since unparseable: {raw!r}"
    covered = (now - epoch).total_seconds() / 86400.0
    if covered < cfg.rent_window():
        return (
            f"spine-read log covers only {covered:.1f}d of the "
            f"{cfg.rent_window()}d rent window (epoch {raw})"
        )
    return None


# ── eviction cap ─────────────────────────────────────────────────────────────

def _apply_eviction_cap(cfg, evictions: list[Proposal]) -> tuple[list[Proposal], int]:
    """At most `max_evictions` EVICT proposals per run (Claudette's floor,
    #custodian 2026-07-26: with spine reads unlogged, rent arithmetic could
    propose a bonfire on run one — "let it be timid once"). Weakest rent goes
    first: lowest reference count, then stalest last-reference (never-seen
    sorts stalest). Deferred candidates are counted for the report header and
    return on later runs; compressions are untouched — they never remove a
    WHY. `max_evictions < 0` means uncapped."""
    cap = cfg.max_evictions
    if cap < 0 or len(evictions) <= cap:
        return evictions, 0
    ranked = sorted(
        evictions,
        key=lambda p: (
            p.evidence.reference_count,
            p.evidence.last_referenced_at or "",
        ),
    )
    return ranked[:cap], len(evictions) - cap


# ── soft-target bias ─────────────────────────────────────────────────────────

def _apply_soft_target_bias(cfg, report, promotions, evictions, compressions):
    """Over the soft target, propose >= as much reduction as addition. A bias
    on what gets SUGGESTED, never a wall on what may be approved. We hold the
    weakest-evidence promotions back so promotions <= reductions."""
    if report.rent_inactive_reason is not None:
        # The epoch gate forbade reductions this run; demanding promotions
        # match them would silently zero every promotion. Bias waits for rent.
        return
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

def _already_in_spine(content: str, spine_bodies: list[str],
                      threshold: float = DEFAULT_SPINE_CONTAINMENT) -> bool:
    """Thin seam over dedup.already_in_spine, kept so the call site reads the
    same as it did. What changed is underneath: this used to ask whether the
    memory's whole text was a literal SUBSTRING of a spine body, which a
    compressed spine line essentially never satisfies — so promoted content was
    re-proposed on every run forever."""
    return already_in_spine(content, spine_bodies, threshold)


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


def _last_seen_map(log: RetrievalLog, callers=HEAT_CALLERS) -> dict[str, str]:
    """Most-recent return timestamp per id, across the WHOLE log (not just the
    window) — evidence enrichment ('last returned <date>').

    Counts only heat-bearing (`service`) reads, for the same reason
    `reference_counts` does — and this field carried the v1 defect just as
    hard as the counts did. On the 2026-07-28 slate every item read "last
    returned today", and it was true: she was reading them, right then, to
    review them. An entry cannot be kept warm by being looked at.

    Pre-v2 lines have `caller is None` and are excluded, so this map goes
    quiet until service reads accumulate. That is the honest reading — the
    rent-epoch gate already resolves no-data to SKIP, never evict."""
    last: dict[str, str] = {}
    for rec in log.read():
        if not rec.ts or rec.caller not in callers:
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
