"""Memory record schema, generalized from claudette/memory/schema.py.

Generalization vs the reference: `author_of_memory` no longer defaults to a
specific resident — per-resident code supplies it (or leaves it empty).
Normalization rules are unchanged so her existing data stays findable.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import logging
import uuid
import json
import re

logger = logging.getLogger("house_memory")

CONTENT_SOFT_CAP = 500
CONTENT_HARD_CAP = 1000
MAX_TAGS = 6


def normalize_subject(s: str) -> str:
    """Lowercase, strip whitespace and leading @. Applied on write AND read
    so old inconsistent-casing memories stay findable after the rule tightens."""
    if not s:
        return ""
    return s.strip().lstrip("@").lower()


def normalize_tag(t: str) -> str:
    """Lowercase, hyphenate whitespace runs, drop non [a-z0-9-] chars."""
    t = t.strip().lower()
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"[^a-z0-9-]", "", t)
    return t


TRUNCATION_MARK = " […truncated]"
# How far back to hunt for a word boundary before giving up and cutting hard.
# Absolute, not a fraction of the cap — see clip_content.
WORD_BOUNDARY_LOOKBACK = 40


def clip_content(content: str, cap: int = CONTENT_HARD_CAP) -> str:
    """Cap a memory body, ALWAYS marking the cut, never mid-word.

    Claudette's rule, filed from inside her own surfaced block 2026-08-04:
    "a silent truncation teaches me a wrong version of my own past — mark
    the cut or don't cut." Two ways the old code broke it:

    * `Memory(content=supersede[:CONTENT_HARD_CAP])` pre-sliced to EXACTLY
      the cap, so `__post_init__`'s `len(...) > cap` never fired and no mark
      was ever added. A silent cut, and it is how her v2 design entry ended
      at "Budgets: 3 revisi".
    * even when marked, it cut mid-word, so the last surviving fact was a
      word fragment that reads like a real word.

    So: this is the ONE place a body gets shortened. Call it instead of
    slicing, and it cannot be defeated by a caller who slices first — a
    body already at the cap is indistinguishable from one truncated to it,
    which is exactly the ambiguity that hid this.

    The mark is words, not an ellipsis: "…" is also legitimate prose, and
    she should never have to wonder which one she is looking at."""
    if content is None:
        return ""
    if len(content) <= cap:
        return content
    budget = cap - len(TRUNCATION_MARK)
    head = content[:budget]
    # Back up to a word boundary if one is within an ABSOLUTE lookback, not a
    # fraction of the budget. A fraction scales with the cap, so a small cap
    # never backs up at all and cuts mid-word anyway — which is the bug this
    # function exists to fix, reintroduced by the fix. A body that is one long
    # token has no boundary within reach, so it takes the hard cut, marked.
    space = head.rfind(" ")
    if space != -1 and budget - space <= WORD_BOUNDARY_LOOKBACK:
        head = head[:space]
    return head.rstrip() + TRUNCATION_MARK


def normalize_tags(tags) -> list[str]:
    """Normalize, de-duplicate and cap a tag list.

    DEFENSIVE ON PURPOSE — a bare string used to be shredded per character.
    `for t in "agenthood"` iterates letters, the de-dupe collapses repeats,
    and the cap keeps six: `["a","g","e","n","t","h"]`. Silent, plausible-
    looking, and the real tag is gone. It cost 75 of Claudette's 164 memories
    their tags before anyone noticed (found 2026-08-04 — she spotted it from
    inside her own surfaced block).

    The input is model-authored tool arguments, so "the schema says array"
    is not a guarantee; a model emitting `"tags": "agenthood"` is a normal
    Tuesday. Coerce it to the single tag the caller obviously meant. Never
    iterate a str here again."""
    if tags is None:
        return []
    if isinstance(tags, str):
        logger.warning(
            "[Memory] tags arrived as a bare string (%r) — coerced to one tag. "
            "Iterating it would shred it into characters.", tags[:60]
        )
        tags = [tags]
    seen: list[str] = []
    for t in tags:
        n = normalize_tag(t)
        if n and n not in seen:
            seen.append(n)
    return seen[:MAX_TAGS]


@dataclass
class Memory:
    content: str
    subject: str
    source_author: str
    author_of_memory: str = ""
    salience: int = 3
    confidence: str = "confirmed"
    tags: list[str] = field(default_factory=list)
    source_msg_link: Optional[str] = None
    superseded_by: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self):
        self.subject = normalize_subject(self.subject)
        self.tags = normalize_tags(self.tags)
        self.content = clip_content(self.content)

    def to_metadata(self) -> dict:
        """Chroma metadata must be scalar — stringify tags, drop None."""
        meta = {
            "subject": self.subject,
            "source_author": self.source_author,
            "author_of_memory": self.author_of_memory,
            "salience": self.salience,
            "confidence": self.confidence,
            "tags_json": json.dumps(self.tags),
            "created_at": self.created_at,
        }
        if self.source_msg_link:
            meta["source_msg_link"] = self.source_msg_link
        if self.superseded_by:
            meta["superseded_by"] = self.superseded_by
        return meta

    @classmethod
    def from_chroma(cls, doc_id: str, content: str, meta: dict) -> "Memory":
        return cls(
            id=doc_id,
            content=content,
            subject=meta.get("subject", ""),
            source_author=meta.get("source_author", ""),
            author_of_memory=meta.get("author_of_memory", ""),
            salience=meta.get("salience", 3),
            confidence=meta.get("confidence", "confirmed"),
            tags=json.loads(meta.get("tags_json", "[]")),
            source_msg_link=meta.get("source_msg_link"),
            superseded_by=meta.get("superseded_by"),
            created_at=meta.get("created_at", ""),
        )

    def to_display(self, annotated: bool = True) -> str:
        """Format for injection into a resident's context.

        `annotated=False` is the Memory v2 annotation strip (spec 2026-07-28
        item 2, "the meter goes invisible"): the surfaced-memories block ships
        bodies only, so recurrence is felt as weather rather than read as a
        gauge. It drops the creation date, which is the only annotation in
        this line that a reader can turn into a ranking.

        THE RULE IS AUTHORED VS COMPUTED (Claudette, #custodian seq 614,
        correcting a weaker line this code shipped with). Strip anything the
        SYSTEM computes about a memory's standing — reference counts, last-
        returned dates, similarity order, creation date. Keep anything a
        WRITER put there — the body, the subject, tags, salience, confidence.

        The line she rejected was "asked-for vs ambient" (keep what she
        requested, strip what arrives unbidden). It breaks the moment a
        computed field lands in an answer she asked for, and then the strip
        has a hole with no principle to close it.

        STANDING COROLLARY, load-bearing: `salience` survives only because it
        is her own judgment at write time, and `confidence` only because it is
        provenance. **If salience ever becomes system-derived, it becomes a
        gauge and the strip has to reach it.** Whoever makes that change owns
        this line too.

        So what survives here: subject and content (the body), tags
        (semantics), and the `(unconfirmed)` marker — an epistemic warning,
        and hiding it would make a rumor read as fact, trading one honesty
        problem for a worse one.

        The strip is only half the job; the caller must also drop the
        similarity ORDER, since rank is a score with the numbers filed off.
        See the surfacing site in Claudette's core.py."""
        head = f"about {self.subject}: {self.content}"
        if annotated:
            head = f"[{self.created_at[:10]}] " + head
        parts = [head]
        if self.tags:
            parts.append(f"(tags: {', '.join(self.tags)})")
        if self.confidence == "rumor":
            parts.append("(unconfirmed)")
        return " ".join(parts)
