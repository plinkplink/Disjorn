"""Which bots a message addresses with an explicit `@`, resolved once when it is written."""

import re

from . import db
from .config import get_settings

GROUP = "bots"

# The client draws no mention chip inside code, and what is not drawn as a mention summons nobody.
_CODE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")


def _at(name: str, text: str) -> bool:
    return re.search(rf"(?<!\w)@{re.escape(name)}(?!\w)", text, re.IGNORECASE) is not None


async def mentioned_bot_ids(channel_id: int, author_type: str, content: str) -> list[int]:
    """The channel's member bots named `@name`, and every answering one when a person writes `@bots`.

    A bot author never reaches the group branch: that is the wall against bots summoning each other.
    """
    text = _CODE_RE.sub(" ", content)
    if "@" not in text:
        return []
    bots = await db.fetch_all(
        """SELECT b.id, b.name FROM channel_members m JOIN bots b ON b.id = m.member_id
           WHERE m.channel_id = ? AND m.member_type = 'bot' ORDER BY b.id""",
        (channel_id,),
    )
    everyone = author_type == "user" and _at(GROUP, text)
    # These accounts post and never answer; `@bots` listing them would name a call nobody takes.
    skipped = {n.lower() for n in get_settings().GROUP_MENTION_SKIP_BOT_NAMES}
    return [
        b["id"]
        for b in bots
        if _at(b["name"], text) or (everyone and b["name"].lower() not in skipped)
    ]
