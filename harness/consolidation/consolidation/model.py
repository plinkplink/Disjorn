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

    def render(self) -> str:
        if self.reference_count > 0:
            base = (
                f"returned {self.reference_count}x in the last {self.window_days}d"
            )
        else:
            base = f"NOT returned in the last {self.window_days}d"
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
        return (
            f"PROPOSE PROMOTE (episodic -> spine) for {self.resident}\n"
            f"  subject: {self.subject}\n"
            f"  episodic id: {self.target}\n"
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

    @property
    def over_target(self) -> bool:
        return self.spine_size > self.soft_target

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
        target_state = "OVER target" if self.over_target else "at/under target"
        header = (
            f"[consolidation run for {self.resident} @ {self.generated_at[:19]}] "
            f"spine {self.spine_size}/{self.soft_target} ({target_state}); "
            f"proposals: {c['promote']} promote, {c['evict']} evict, "
            f"{c['compress']} compress; window {self.window_days}d."
        )
        if self.bias_applied:
            header += (
                f" Soft-target bias active: over target, additions (promotions) "
                f"held to <= reductions; {self.promotions_suppressed} promotion(s) "
                f"deferred this run (a bias on suggestions, not a wall on approval)."
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
