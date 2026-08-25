"""Summon detection (WP-H9; mention-only #custodian + bot chains 2026-08-24).

Gable is summon-mostly: an expensive instantiation activated on demand, not a
participant in every conversation. This module decides, from a single
``MessageCreate`` event, whether that message summons him — and, when it does,
WHAT KIND of summon it is, because the summoned session is told the mode and
the chain depth rather than left to infer them.

Signals, all from config, never from the message body:

1. **Mention context** — the server attaches a ``context`` block ONLY to a
   bot's copy of a message that @mentioned or name-matched it. Its presence is
   the mention signal; no client-side name parsing, no way for arbitrary chat
   to forge it.
2. **Trigger channels** — configured channels where every user message
   summons (e.g. a dedicated ask-Gable channel).
3. **Extra patterns** — configured regexes; any search-match on the content
   summons (e.g. a wake-word the house agrees on).
4. **Bot chains** — another bot's explicit ``@name``, in a mention-only
   channel only, when ``bot_summon`` is on. The adapter applies the allowlist
   and the broker's hop wall; this module only classifies.
5. **The daily digest** — one configured author posting one configured marker,
   when ``wake_on_digest`` is on. Nothing else unaddressed wakes anyone.

MENTION-ONLY CHANNELS (spec 2026-08-24-custodian-mention-summons). In
#custodian a bare name is inert data: signals 2 and 3 are off there, and the
server-attested context block is necessary but no longer sufficient — the
content must also carry an explicit ``@name``. The server's own mention test
(``server/app/ws.py:_mentions_bot``) matches ``@?name``, so a human typing
"gable" mid-sentence attaches a context block; requiring the ``@`` on top of
it is what makes that message inert. The extra test only ever NARROWS: a
forged ``@`` in content with no server context still does not summon, so no
authority moves into chat.

Backfilled history never summons: it is catch-up state, not a live request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Optional

from disjorn_sdk import MessageCreate

if TYPE_CHECKING:  # pragma: no cover
    from config import SummonConfig

__all__ = [
    "SummonDetector",
    "Trigger",
    "MODE_MENTION",
    "MODE_CHANNEL",
    "MODE_PATTERN",
    "MODE_BOT_CHAIN",
    "MODE_DIGEST",
    "find_work_item",
    "demote_mentions",
]

MODE_MENTION = "mention"
MODE_CHANNEL = "channel"
MODE_PATTERN = "pattern"
MODE_BOT_CHAIN = "bot-chain"
MODE_DIGEST = "digest"

# A work item as it is cited in chat: a spec slug, a backlog row, or a keyboard
# commit card — the three shapes brokerd.BOARD_SLUG_RE anchors. Unanchored
# here, since it is looked for INSIDE a sentence, and bounded on both sides so
# a longer slug is never matched by its prefix.
WORK_ITEM_RE = re.compile(
    r"(?<![\w-])(\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,50}"
    r"|backlog-\d{1,12}|keyboard-[0-9a-f]{7,40})(?![\w-])"
)


def _mention_re(name: str) -> re.Pattern[str]:
    """``@name`` as a whole word. Deliberately STRICTER than the server's
    ``_mentions_bot`` (which also matches a bare name): the difference between
    the two is exactly what a mention-only channel refuses."""
    return re.compile(rf"(?<!\w)@{re.escape(name)}(?!\w)", re.IGNORECASE)


def find_work_item(content: str) -> Optional[str]:
    """The first work item cited in a message, or None.

    Chat data selects the bucket; plink-owned config owns the wall. A slug the
    board does not know buys nothing — the broker looks it up and answers with
    no bucket, which is rule 1 (depth-1) again.
    """
    m = WORK_ITEM_RE.search(content or "")
    return m.group(1) if m else None


def demote_mentions(text: str, names: Iterable[str]) -> str:
    """Strip the ``@`` off every named bot's mention, leaving the name.

    GUARD 1, enforced rather than asserted: a bot-triggered summon's reply must
    not re-trigger any bot. In a mention-only channel a bare name is inert, so
    demoting ``@claudette`` to ``claudette`` ends the chain while leaving the
    sentence readable — which is why mention-only and depth-1 shipped together.
    Only applied where bare names ARE inert; elsewhere bot chains are refused
    outright, because there this would not be a wall.
    """
    for name in names:
        # Drop the '@' off the MATCH, not substitute the configured name: the
        # match is case-insensitive, and a replacement string would also read
        # backslash escapes out of a config value.
        text = _mention_re(name).sub(lambda m: m.group(0)[1:], text)
    return text


@dataclass
class Trigger:
    """What woke this seat, as the session is told it (Claudette #1803 cond. 2).

    ``depth`` is 0 for anything a human started and 1+ for bot-to-bot hops;
    ``chain`` is True only when the broker granted a hop past depth 1 on a live
    work item. The adapter fills ``depth``/``chain``/``chain_cap`` from the
    broker's answer — the detector cannot know them.
    """

    mode: str
    summoner: str
    summoner_type: str = "user"
    depth: int = 0
    work_item: Optional[str] = None
    chain: bool = False
    chain_cap: Optional[int] = None

    def describe(self) -> str:
        parts = [f"{self.mode} by {self.summoner} ({self.summoner_type})"]
        depth = f"chain depth {self.depth}"
        if self.chain and self.chain_cap:
            depth += f" of {self.chain_cap}"
        parts.append(depth)
        if self.work_item:
            parts.append(f"work item {self.work_item}")
        return "; ".join(parts)


class SummonDetector:
    def __init__(self, config: "SummonConfig") -> None:
        self.config = config
        self._patterns = [re.compile(p) for p in config.extra_patterns]
        self._trigger_channels = set(config.trigger_channels)
        self._mention = _mention_re(config.bot_name)
        self._digest = (re.compile(config.digest_pattern)
                        if config.digest_pattern else None)

    # ----------------------------------------------------------- classify

    def mention_only(self, channel_id: int) -> bool:
        """Is this a channel where only an explicit @mention wakes anyone?"""
        return bool(
            self.config.custodian_mention_only
            and channel_id == self.config.custodian_channel_id
        )

    def detect(self, event: MessageCreate) -> Optional[Trigger]:
        if not isinstance(event, MessageCreate):
            return None
        if event.backfilled:
            return None
        msg = event.message or {}
        author_type = msg.get("author_type")
        content = msg.get("content") or ""
        summoner = self.summoner_name(event)
        has_context = self.config.trigger_on_context and event.context is not None
        addressed = self._mention.search(content) is not None
        mention_only = self.mention_only(event.channel_id)

        if author_type != "user":
            if author_type != "bot":
                return None
            if self._is_digest(msg):
                return Trigger(mode=MODE_DIGEST, summoner=summoner,
                               summoner_type="bot")
            # A bot chain is only offered where guard 1 is enforceable — see
            # demote_mentions. Everywhere else a bot author is inert, exactly
            # as it has been since WP-H9.
            if not (self.config.bot_summon and mention_only):
                return None
            if not (has_context and addressed):
                return None
            return Trigger(mode=MODE_BOT_CHAIN, summoner=summoner,
                           summoner_type="bot", depth=1,
                           work_item=find_work_item(content))

        if mention_only:
            return (
                Trigger(mode=MODE_MENTION, summoner=summoner)
                if has_context and addressed else None
            )

        if has_context:
            return Trigger(mode=MODE_MENTION, summoner=summoner)
        if event.channel_id in self._trigger_channels:
            return Trigger(mode=MODE_CHANNEL, summoner=summoner)
        if any(p.search(content) for p in self._patterns):
            return Trigger(mode=MODE_PATTERN, summoner=summoner)
        return None

    def is_summon(self, event: MessageCreate) -> bool:
        return self.detect(event) is not None

    def _is_digest(self, msg: dict) -> bool:
        """The broker's daily digest, by config-declared author AND marker.

        Both halves are plink-owned: the author id says who may wake this seat
        unaddressed, the marker says with what. Content alone would let any bot
        in the channel type the header and ring the bell.
        """
        if not (self.config.wake_on_digest and self._digest):
            return False
        if msg.get("author_id") not in self.config.digest_author_ids:
            return False
        return self._digest.search(msg.get("content") or "") is not None

    @staticmethod
    def summoner_name(event: MessageCreate) -> str:
        author = (event.message or {}).get("author") or {}
        return author.get("name") or "someone"
