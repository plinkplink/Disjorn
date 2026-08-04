"""Dedup — the fourth walker re-activation gate.

WHY THE WALKER IS OFF. Consolidation v1 ran three nights, emitted 30 promote
proposals, and had 0 approved. Three of the four causes are fixed (the `caller`
field so self-reads stop feeding heat, the annotation strip so the meter goes
invisible, the spec confirmed). This is the fourth, and it is the one a
reviewer feels directly: the slate was full of the same idea said four ways,
and each copy arrived as its own proposal with its own evidence line.

Three distinct defects live under the one word "dedup".

1. NO CLUSTERING. `_promotion_proposals` walked every memory and emitted one
   proposal per memory over threshold. Four paraphrases of one pattern became
   four proposals. A reviewer reading that slate is not reading a summary of
   what the resident learned; they are reading the store with a filter on it.

2. COUNTING BEFORE CLUSTERING — the subtler one, and it loses real signal
   rather than merely adding noise. A pattern split across four memories, each
   returned twice, is four memories at 2 references against a threshold of 3.
   Every one falls below the bar and the pattern is dropped entirely, despite
   the house having gone looking for it eight times. The order has to be
   cluster-then-count, which is why the pooling primitive lives on the
   retrieval log (`group_reference_counts`) and counts distinct retrieval
   EVENTS — see that docstring for why neither sum nor max is honest.

3. THE SPINE WAS EFFECTIVELY INVISIBLE. `_already_in_spine` asked whether the
   memory's entire normalized text appeared as a literal substring of a spine
   body. That is nearly never true — a spine line is a compression of the
   memory, not a superset of it — so content already promoted kept being
   re-proposed forever. Substring is not a similarity measure; it is an
   accident that occasionally resembles one.

WHY MEMORY-TO-MEMORY USES COSINE AND SPINE-TO-MEMORY DOES NOT. Episodic
memories already carry their vectors: `export_all()` returns stored embeddings
verbatim, so cosine between two memories is free and exact. Spine entries are
markdown with no vectors, and embedding them would need the network — which
would break the property `NullEmbedder` exists to enforce ("consolidation makes
no network calls" is a tripwire, not a claim). So the spine comparison is
lexical: shingle containment, deterministic, offline, and enormously better
than substring. Two different measures because the two sides genuinely differ
in what is available, not because one of them was easier.

SUBJECT IS A HARD WALL. Clustering only ever happens within a single
normalized subject. "plink prefers the terse version" and "gable prefers the
terse version" sit very close in embedding space, and merging them would emit
one proposal attributing one resident's fact to another — a dedup pass that
invents a false memory is worse than no dedup pass. Subject is read BEFORE any
similarity is computed, so this cannot be forgotten downstream.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Cosine over Voyage embeddings. 0.92 is deliberately tight: a false merge
# silently destroys a distinct memory's chance at the spine and the reviewer
# cannot see what was swallowed, while a false split merely leaves two
# proposals where one would do — visible, annoying, harmless. Asymmetric costs,
# so the threshold sits on the safe side of the asymmetry.
DEFAULT_SIMILARITY = 0.92

# Word-shingle containment for "is this already in the spine". 0.60 of the
# memory's shingles appearing in one spine body means the spine already says
# most of this. Looser than the cosine gate because the failure is benign in
# the other direction: wrongly skipping a promotion leaves the memory episodic
# and it will be proposed again the moment the spine line drifts.
DEFAULT_SPINE_CONTAINMENT = 0.60

SHINGLE_SIZE = 5

_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Cluster:
    """One idea, however many times the resident wrote it down."""

    key: str                     # == representative id; stable across runs
    subject: str
    representative: str          # the member whose text stands for the cluster
    content: str                 # the representative's body
    members: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def others(self) -> list[str]:
        """Members the representative stands in for, in stable order."""
        return [m for m in self.members if m != self.representative]


def cosine(a: Optional[Iterable[float]], b: Optional[Iterable[float]]) -> float:
    """Plain cosine. Returns 0.0 when either vector is missing or degenerate —
    'we cannot tell' resolves to 'not similar', so an absent embedding can only
    ever cause a false SPLIT, never a false merge."""
    if a is None or b is None:
        return 0.0
    av, bv = list(a), list(b)
    if not av or len(av) != len(bv):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(av, bv):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def cluster_records(
    records: list[dict],
    similarity: float = DEFAULT_SIMILARITY,
) -> list[Cluster]:
    """Group `export_all()`-shaped records into clusters of one idea.

    Records are `{"id", "content", "embedding", "metadata"}`. Grouping is
    per-subject and uses LEADER clustering, not single-link: each record joins
    the first existing cluster whose REPRESENTATIVE it is similar enough to, or
    starts its own.

    Leader, specifically, because single-link chains. With A~B and B~C but
    A≁C, single-link merges all three, and a long enough chain of small steps
    swallows genuinely different memories one hop at a time. Every member of a
    leader cluster is within `similarity` of the same representative, so the
    cluster has a stated meaning: "these all say what the representative says."

    Order is deterministic — longest body first, ties broken by id — so the
    same store always produces the same clusters, and the longest member (the
    most complete statement of the pattern) becomes the representative rather
    than whichever copy chroma happened to return first.
    """
    by_subject: dict[str, list[dict]] = {}
    for rec in records:
        meta = rec.get("metadata", {}) or {}
        subject = str(meta.get("subject", "") or "")
        by_subject.setdefault(subject, []).append(rec)

    clusters: list[Cluster] = []
    for subject in sorted(by_subject):
        ordered = sorted(
            by_subject[subject],
            key=lambda r: (-len(r.get("content") or ""), r["id"]),
        )
        leaders: list[tuple[Cluster, Optional[list]]] = []
        for rec in ordered:
            emb = rec.get("embedding")
            placed = False
            if emb is not None:
                for cluster, leader_emb in leaders:
                    if cosine(emb, leader_emb) >= similarity:
                        cluster.members.append(rec["id"])
                        placed = True
                        break
            if not placed:
                cluster = Cluster(
                    key=rec["id"],
                    subject=subject,
                    representative=rec["id"],
                    content=rec.get("content") or "",
                    members=[rec["id"]],
                )
                leaders.append((cluster, emb))
                clusters.append(cluster)
    return clusters


# ── spine containment ────────────────────────────────────────────────────────

def _shingles(text: str, size: int = SHINGLE_SIZE) -> set:
    """Word n-grams over normalized tokens. Punctuation and casing are dropped
    so a spine line that re-punctuates the memory still matches."""
    words = _WORD.findall(text.lower())
    if not words:
        return set()
    if len(words) <= size:
        return {tuple(words)}
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def containment(needle: str, haystack: str, size: int = SHINGLE_SIZE) -> float:
    """Fraction of `needle`'s shingles present in `haystack`.

    Containment, not Jaccard, and the asymmetry is the point: a spine entry is
    normally much SHORTER than the memory that earned it, and Jaccard punishes
    that length difference exactly when the answer should be yes. The question
    is "does the spine already say this", not "are these the same size".
    """
    ns = _shingles(needle, size)
    if not ns:
        return 0.0
    hs = _shingles(haystack, size)
    if not hs:
        return 0.0
    return len(ns & hs) / len(ns)


def overlap(a: str, b: str, size: int = SHINGLE_SIZE) -> float:
    """Containment measured in whichever direction is meaningful — equivalently
    |A ∩ B| / min(|A|, |B|).

    BOTH DIRECTIONS HAPPEN, which is why this is not a single containment call:

      * a spine line is usually a COMPRESSION of the memory that earned it, so
        the spine text sits inside the longer memory;
      * but a spine entry can also be a paragraph that absorbed a one-line
        memory whole, so the memory sits inside the longer spine body — that
        is the shape the original substring check was written for.

    Taking the containment of the shorter inside the longer answers "do these
    two say the same thing" without letting the length gap decide, which is the
    trap Jaccard falls into here.
    """
    sa, sb = _shingles(a, size), _shingles(b, size)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def already_in_spine(
    content: str,
    spine_bodies: list[str],
    threshold: float = DEFAULT_SPINE_CONTAINMENT,
) -> bool:
    """Is this pattern already carried by some spine entry?

    Substring stays as a fast path — when it hits it is certainly true — and
    shingle overlap catches everything it missed, which was almost everything.
    """
    needle = " ".join(content.split()).lower()
    if not needle:
        return False
    for body in spine_bodies:
        if needle in body:
            return True
    return any(overlap(content, body) >= threshold for body in spine_bodies)
