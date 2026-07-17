"""Privacy (WP5): flag detection + bot-visibility filtering.

Single source of truth (BUILD-PLAN convention): every bot-facing code path —
REST reads (routers/messages.py), the WS event stream (app/ws.py), and context
injection — decides visibility through this module. Architecture §4.2 / §8.2:
messages flagged `secret` or `off_the_record` never reach a bot in any form
(no content, no tombstone, no context).

NL trigger detection is deliberately cheap: compiled word-boundary regexes at
module import, ASCII phrases with flexible whitespace and straight/curly
apostrophes. Runs server-side before insert/fan-out, so the flag exists before
any bot could see the message.
"""

import re
from typing import Any, Optional

# Flags that hide a message from bots entirely.
BOT_HIDDEN_FLAGS = ("secret", "off_the_record")

# Event types whose payload is (or refers to) a persisted message.
_MESSAGE_EVENT_TYPES = ("message_create", "message_edit", "message_delete")

_SECRET_PHRASES = (
    "don't tell anyone",   # matcher also covers "dont" / curly-apostrophe "don’t"
    "just between us",
    "between you and me",
    "keep this secret",
    "keep this between us",
)
_OFF_THE_RECORD_PHRASES = ("off the record",)


def _phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """One compiled alternation for a phrase list.

    - case-insensitive
    - word-boundary aware on both ends ("scoff the record" doesn't match)
    - spaces match any whitespace run
    - "'" matches straight/curly apostrophe or nothing ("dont tell anyone")
    """
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
    """NL trigger detection: map message content to privacy flags.

    Returns {} when nothing triggers; callers merge the result into
    caller-supplied flags (flags only ever accumulate — WP4 semantics).
    """
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
    """May this bot see this message payload?

    Pure/sync by design: the caller resolves channel membership (an async DB
    question) and passes it in; this module owns only the policy. `bot_id` is
    part of the signature for future per-bot policy; today visibility is
    uniform across bots.
    """
    if not channel_is_bot_member:
        return False
    return not hidden_from_bots(message.get("privacy_flags"))


def filter_event_for_bot(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Bus event -> bot-safe event, or None if bots must not see it at all.

    - Non-message events (typing_start, presence, ...) pass through untouched.
    - message_create / message_edit: hidden when the embedded message payload
      carries a bot-hidden flag.
    - message_delete: carries no payload, so the caller must enrich the event
      with the deleted message's flags under "privacy_flags" (soft delete keeps
      the row). A hidden message's delete is also hidden — bots that never saw
      the message get no tombstone for it.

    Membership scoping stays with the caller (visible_to_bot / channel fan-out);
    this function only enforces the flag policy.
    """
    if event.get("type") not in _MESSAGE_EVENT_TYPES:
        return event
    message = event.get("message")
    flags = message.get("privacy_flags") if isinstance(message, dict) else event.get("privacy_flags")
    if hidden_from_bots(flags):
        return None
    return event
