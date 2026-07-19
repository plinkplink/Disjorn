"""Memory record schema, generalized from claudette/memory/schema.py.

Generalization vs the reference: `author_of_memory` no longer defaults to a
specific resident — per-resident code supplies it (or leaves it empty).
Normalization rules are unchanged so her existing data stays findable.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid
import json
import re

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


def normalize_tags(tags: list[str]) -> list[str]:
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
        if len(self.content) > CONTENT_HARD_CAP:
            self.content = self.content[:CONTENT_HARD_CAP].rstrip() + "…"

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

    def to_display(self) -> str:
        """Format for injection into a resident's context."""
        parts = [f"[{self.created_at[:10]}] about {self.subject}: {self.content}"]
        if self.tags:
            parts.append(f"(tags: {', '.join(self.tags)})")
        if self.confidence == "rumor":
            parts.append("(unconfirmed)")
        return " ".join(parts)
