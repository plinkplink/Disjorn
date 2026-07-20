"""Summon detection (WP-H9).

Gable is summon-mostly: an expensive instantiation activated on demand, not a
participant in every conversation. This module decides, from a single
``MessageCreate`` event, whether that message summons him.

Three configurable signals, OR-ed together (all from config, never from the
message body):

1. **Mention context** — the server attaches a ``context`` block ONLY to a
   bot's copy of a message that @mentioned or name-matched it. Its presence is
   the mention signal; no client-side name parsing, no way for arbitrary chat
   to forge it.
2. **Trigger channels** — configured channels where every user message
   summons (e.g. a dedicated ask-Gable channel).
3. **Extra patterns** — configured regexes; any search-match on the content
   summons (e.g. a wake-word the house agrees on).

Non-user authors and backfilled history never summon: backfill is catch-up
state, not a live request, and bot messages (including Gable's own replies)
must never loop.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from disjorn_sdk import MessageCreate

if TYPE_CHECKING:  # pragma: no cover
    from config import SummonConfig

__all__ = ["SummonDetector"]


class SummonDetector:
    def __init__(self, config: "SummonConfig") -> None:
        self.config = config
        self._patterns = [re.compile(p) for p in config.extra_patterns]
        self._trigger_channels = set(config.trigger_channels)

    def is_summon(self, event: MessageCreate) -> bool:
        if not isinstance(event, MessageCreate):
            return False
        if event.backfilled:
            return False
        msg = event.message or {}
        if msg.get("author_type") != "user":
            return False

        # 1. Server-attested mention.
        if self.config.trigger_on_context and event.context is not None:
            return True
        # 2. Always-on trigger channel.
        if event.channel_id in self._trigger_channels:
            return True
        # 3. Configured wake patterns.
        content = msg.get("content") or ""
        return any(p.search(content) for p in self._patterns)

    @staticmethod
    def summoner_name(event: MessageCreate) -> str:
        author = (event.message or {}).get("author") or {}
        return author.get("name") or "someone"
