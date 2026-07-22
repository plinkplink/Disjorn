"""consolidation — witnessed memory consolidation for the Disjorn harness (WP-H8).

"Sleep, but out loud." A scheduled, per-resident pass that reads the fast
(episodic) layer, the retrieval log, and the markdown spine, and emits
REVIEWED PROPOSALS to #custodian — never a silent write.

Load-bearing invariants (MEMORY-DESIGN.md, AGENTHOOD.md):

- **proposes-never-acts**: this job NEVER mutates a store, a spine file, or a
  retrieval log. Its only output is proposals. `NullEmbedder` guarantees it
  cannot even embed (no network).
- **bidirectional**: promote (episodic->spine), evict, compress. The spine
  pays rent; sleep composts as well as files.
- **evidence in every proposal**: reference counts from the retrieval logs,
  "measured from logs, not vibes."
- **soft-target bias**: over the spine's soft target, propose >= as much
  reduction as addition. A bias on suggestions, never a wall on approval.
- **constraint-shaped entries default to compression, never eviction**
  (anti-Chesterton's-fence).
- **eviction = supersession commit, not deletion** (reversible forgetting).
- **absent inputs fail loud, never fail open**: a resident with no on-disk
  spine runs episodic-promotion only (explicit short-circuit); a configured
  input path that is missing raises `MissingInputError` rather than being read
  as an empty spine or letting chromadb create a fresh store.
"""

from consolidation.config import ConsolidationConfig, load_config
from consolidation.embedders import NullEmbedder
from consolidation.model import (
    ConsolidationReport,
    Evidence,
    Proposal,
    ProposalKind,
)
from consolidation.analyze import MissingInputError, build_proposals
from consolidation.poster import PostOutcome, post_report

__all__ = [
    "MissingInputError",
    "ConsolidationConfig",
    "load_config",
    "NullEmbedder",
    "ConsolidationReport",
    "Evidence",
    "Proposal",
    "ProposalKind",
    "build_proposals",
    "PostOutcome",
    "post_report",
]
