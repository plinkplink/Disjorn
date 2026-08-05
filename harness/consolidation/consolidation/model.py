"""Proposal / Evidence / report data model + their #custodian rendering.

A consolidation run produces a `ConsolidationReport`: a batch of `Proposal`s,
each carrying `Evidence` (reference counts from the retrieval logs). Rendering
is deliberately part of the model so the exact words a reviewer sees are
tested — in particular that eviction reads as a *supersession commit*, never a
deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ProposalKind(str, Enum):
    PROMOTE = "promote"   # episodic -> spine (an ADDITION to the spine)
    EVICT = "evict"       # spine entry -> supersession commit (a REDUCTION)
    COMPRESS = "compress" # spine entry(s) tightened/merged (a REDUCTION)


# Which kinds add to vs. reduce the spine — the soft-target bias math.
ADDITION_KINDS = frozenset({ProposalKind.PROMOTE})
REDUCTION_KINDS = frozenset({ProposalKind.EVICT, ProposalKind.COMPRESS})


@dataclass
class Evidence:
    """Rent evidence, straight from the retrieval logs. In EVERY proposal."""

    reference_count: int          # times returned within the trailing window
    window_days: int
    last_referenced_at: Optional[str] = None  # ISO ts of most recent return, if ever
    # >1 when this proposal stands for several near-identical memories that
    # were deduped into one idea. The reviewer is told, because a count pooled
    # over a merge they cannot see is a count they cannot audit.
    cluster_size: int = 1

    def render(self) -> str:
        if self.reference_count > 0:
            base = (
                f"returned {self.reference_count}x in the last {self.window_days}d"
            )
        else:
            base = f"NOT returned in the last {self.window_days}d"
        if self.cluster_size > 1:
            # Say the arithmetic out loud. "Retrieval events, not copies" is
            # the difference between a pooled count and an inflated one, and a
            # reviewer cannot check which one they are reading unless it is
            # stated on the line itself.
            base += (
                f" across {self.cluster_size} near-identical memories "
                f"(retrieval events, not copies returned)"
            )
        if self.last_referenced_at:
            base += f"; last returned {self.last_referenced_at[:10]}"
        else:
            base += "; never returned on record"
        return base


@dataclass
class Proposal:
    kind: ProposalKind
    resident: str
    # target: episodic memory id (promote) or spine entry name(s) (evict/compress)
    target: str
    subject: str
    content: str
    evidence: Evidence
    rationale: str
    constraint_shaped: bool = False
    # for compression that merges several spine entries into one line
    members: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Self-contained #custodian text for this single proposal."""
        if self.kind is ProposalKind.PROMOTE:
            return self._render_promote()
        if self.kind is ProposalKind.EVICT:
            return self._render_evict()
        return self._render_compress()

    # -- per-kind rendering --------------------------------------------------

    def _render_promote(self) -> str:
        # The deduped copies are named, not merely counted. A reviewer who
        # thinks the merge was wrong needs the ids to go look, and a resident
        # reading their own slate should be able to see which of their memories
        # got folded into which.
        also = ""
        if self.members:
            also = (
                f"  also covers ({len(self.members)} near-identical): "
                f"{', '.join(self.members)}\n"
            )
        return (
            f"PROPOSE PROMOTE (episodic -> spine) for {self.resident}\n"
            f"  subject: {self.subject}\n"
            f"  episodic id: {self.target}\n"
            f"{also}"
            f"  content: {_excerpt(self.content)}\n"
            f"  evidence: {self.evidence.render()}\n"
            f"  rationale: {self.rationale}\n"
            f"  action if approved: add a reviewed spine entry (git-committed, "
            f"witnessed)."
        )

    def _render_evict(self) -> str:
        # Eviction is a SUPERSESSION COMMIT, never a deletion. Reversible.
        return (
            f"PROPOSE EVICT (via supersession commit) for {self.resident}\n"
            f"  spine entry: {self.target}\n"
            f"  subject: {self.subject}\n"
            f"  body: {_excerpt(self.content)}\n"
            f"  evidence: {self.evidence.render()}\n"
            f"  rationale: {self.rationale}\n"
            f"  action if approved: supersede the entry with a git commit that "
            f"moves it to cold storage. Nothing is destroyed; the archive is git. "
            f"Re-promotion of this entry may be proposed later — reversible "
            f"forgetting is what makes the compression safe."
        )

    def _render_compress(self) -> str:
        member_line = ""
        if self.members:
            member_line = f"  merges entries: {', '.join(self.members)}\n"
        shape = " (constraint-shaped: the WHY is kept, only tightened)" if self.constraint_shaped else ""
        return (
            f"PROPOSE COMPRESS{shape} for {self.resident}\n"
            f"  spine entry: {self.target}\n"
            f"{member_line}"
            f"  subject: {self.subject}\n"
            f"  body: {_excerpt(self.content)}\n"
            f"  evidence: {self.evidence.render()}\n"
            f"  rationale: {self.rationale}\n"
            f"  action if approved: rewrite to one tighter line via git commit; "
            f"the original stays in git history (reversible)."
        )


@dataclass
class ConsolidationReport:
    resident: str
    generated_at: str
    window_days: int
    spine_size: int
    soft_target: int
    proposals: list[Proposal] = field(default_factory=list)
    # soft-target bias bookkeeping (transparency for reviewers)
    bias_applied: bool = False
    promotions_suppressed: int = 0
    # eviction-cap bookkeeping (Claudette's floor): candidates over
    # max_evictions this run — deferred, not spared; they return next run.
    evictions_deferred: int = 0
    # non-None = spine exists but rent assessment is OFF (epoch gate): zero
    # references currently means "unmeasured", not "unreferenced", so no
    # evict/compress proposals were even computed. The reason is printed in
    # the header so a quiet run is legibly quiet-by-design.
    rent_inactive_reason: "str | None" = None
    # rent's own trailing window when it differs from window_days ("slow-
    # moving spine needs time to go stale"); None = same as window_days.
    rent_window_days: "int | None" = None
    # False = CONSOLIDATION HAS NO SPINE POINTER for this resident. It does NOT
    # mean the resident has no spine.
    #
    # The header used to say "spine: NONE on disk for this resident" and that
    # sentence was false on all ten proposals of the 2026-08-05 slate.
    # Claudette has had an on-disk spine since the 07-22/23 prompt->spine swap
    # — seven entries at /srv/disjorn-spine/claudette, mounted RO into her
    # container, read by her bot every session. What is unset is
    # `[spine].dir` in her consolidation config, deliberately, because spine
    # reads are not logged yet (INTEGRATION-NEEDS §1) and rent arithmetic over
    # unlogged reads scores every entry zero however load-bearing it is.
    #
    # A blindfold is not an absence, and the report must not describe one as
    # the other. Two consequences of it having done so: reviewers were told a
    # file they can `ls` does not exist, and walker gate 4's spine-containment
    # half was recorded green having never run — with no spine, `spine_bodies`
    # is empty and `already_in_spine` compares against nothing.
    spine_present: bool = True

    @property
    def over_target(self) -> bool:
        # No spine on disk => nothing to be over target with.
        return self.spine_present and self.spine_size > self.soft_target

    def counts(self) -> dict[str, int]:
        c = {k.value: 0 for k in ProposalKind}
        for p in self.proposals:
            c[p.kind.value] += 1
        return c

    def additions(self) -> int:
        return sum(1 for p in self.proposals if p.kind in ADDITION_KINDS)

    def reductions(self) -> int:
        return sum(1 for p in self.proposals if p.kind in REDUCTION_KINDS)

    def batch_header(self) -> str:
        c = self.counts()
        if self.spine_present and self.rent_inactive_reason:
            spine_clause = (
                f"spine {self.spine_size} entries, rent assessment INACTIVE "
                f"— {self.rent_inactive_reason} — zero reads mean unmeasured, "
                f"not unreferenced, so no evict/compress proposals this run"
            )
        elif self.spine_present:
            target_state = "OVER target" if self.over_target else "at/under target"
            spine_clause = f"spine {self.spine_size}/{self.soft_target} ({target_state})"
        else:
            spine_clause = (
                "spine: NOT CONNECTED to consolidation for this resident "
                "([spine].dir unset) — episodic-promotion only, no "
                "evict/compress proposals are possible this run. This says "
                "nothing about whether a spine exists on disk; it usually does"
            )
        header = (
            f"[consolidation run for {self.resident} @ {self.generated_at[:19]}] "
            f"{spine_clause}; "
            f"proposals: {c['promote']} promote, {c['evict']} evict, "
            f"{c['compress']} compress; window {self.window_days}d"
            + (
                f" (rent {self.rent_window_days}d)."
                if self.rent_window_days and self.rent_window_days != self.window_days
                else "."
            )
        )
        if self.bias_applied:
            header += (
                f" Soft-target bias active: over target, additions (promotions) "
                f"held to <= reductions; {self.promotions_suppressed} promotion(s) "
                f"deferred this run (a bias on suggestions, not a wall on approval)."
            )
        if self.evictions_deferred:
            header += (
                f" Eviction cap active: {self.evictions_deferred} eviction "
                f"candidate(s) over the per-run cap held back — deferred, not "
                f"spared; they return on later runs (a volume limit on "
                f"suggestions, not a wall on approval)."
            )
        return header

    def render_full(self) -> str:
        """Whole 'sleep, but out loud' report — used by --dry-run."""
        lines = [self.batch_header(), ""]
        if not self.proposals:
            lines.append("(no proposals this run — nothing crossed threshold.)")
        for i, p in enumerate(self.proposals, 1):
            lines.append(f"--- proposal {i}/{len(self.proposals)} ---")
            lines.append(p.render())
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _excerpt(text: str, cap: int = 500) -> str:
    text = " ".join(text.split())
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + "…"
