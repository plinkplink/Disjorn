"""Privacy flag detection and bot-visibility filtering.

Every bot-facing path decides visibility here. `secret` and `off_the_record`
messages never reach a bot in any form: no content, tombstone or context.
Detection runs before insert and fan-out, so the flag exists first.
"""

import re
from typing import Any, Optional

BOT_HIDDEN_FLAGS = ("secret", "off_the_record")

_MESSAGE_EVENT_TYPES = ("message_create", "message_edit", "message_delete")

_SECRET_PHRASES = (
    "don't tell anyone",
    "just between us",
    "between you and me",
    "keep this secret",
    "keep this between us",
)
_OFF_THE_RECORD_PHRASES = ("off the record",)


def _phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """One case-insensitive, word-bounded alternation over a phrase list."""
    alts = []
    for phrase in phrases:
        pat = re.escape(phrase)
        pat = pat.replace("'", "['’]?")
        pat = pat.replace(r"\ ", r"\s+") if r"\ " in pat else pat.replace(" ", r"\s+")
        alts.append(pat)
    return re.compile(rf"(?<!\w)(?:{'|'.join(alts)})(?!\w)", re.IGNORECASE)


_SECRET_RE = _phrase_pattern(_SECRET_PHRASES)
_OFF_THE_RECORD_RE = _phrase_pattern(_OFF_THE_RECORD_PHRASES)


def detect_flags(content: str) -> dict[str, Any]:
    """Map message content to privacy flags. Callers merge; flags only accumulate."""
    flags: dict[str, Any] = {}
    if _SECRET_RE.search(content):
        flags["secret"] = True
    if _OFF_THE_RECORD_RE.search(content):
        flags["off_the_record"] = True
    return flags


def hidden_from_bots(privacy_flags: Optional[dict[str, Any]]) -> bool:
    """True if a message with these flags must never reach a bot."""
    if not privacy_flags:
        return False
    return any(bool(privacy_flags.get(f)) for f in BOT_HIDDEN_FLAGS)


def visible_to_bot(
    message: dict[str, Any], bot_id: int, *, channel_is_bot_member: bool
) -> bool:
    """May this bot see this payload? `bot_id` is unused: visibility is uniform."""
    if not channel_is_bot_member:
        return False
    return not hidden_from_bots(message.get("privacy_flags"))


def filter_event_for_bot(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Bus event to a bot-safe event, or None if bots must not see it.

    message_delete has no payload, so the caller must put the deleted
    message's flags under "privacy_flags". A hidden message's delete is hidden.
    """
    if event.get("type") not in _MESSAGE_EVENT_TYPES:
        return event
    message = event.get("message")
    flags = message.get("privacy_flags") if isinstance(message, dict) else event.get("privacy_flags")
    if hidden_from_bots(flags):
        return None
    return event
