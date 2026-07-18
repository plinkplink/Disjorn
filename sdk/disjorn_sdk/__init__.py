"""disjorn_sdk — Python bot SDK for Disjorn.

Quickstart::

    from disjorn_sdk import DisjornClient, MessageCreate

    client = DisjornClient("http://localhost:8000", api_key="...")

    async def handle(event):
        if isinstance(event, MessageCreate) and event.context is not None:
            await client.send(event.channel_id, "hi!", emotion="happy")

    await client.run(handle)
"""

from .client import DisjornAuthError, DisjornClient, DisjornError
from .events import (
    ChannelCreate,
    Event,
    MessageCreate,
    MessageDelete,
    MessageEdit,
    Presence,
    Ready,
    TypingStart,
)

__version__ = "0.1.0"

__all__ = [
    "DisjornClient",
    "DisjornError",
    "DisjornAuthError",
    "Event",
    "Ready",
    "MessageCreate",
    "MessageEdit",
    "MessageDelete",
    "TypingStart",
    "Presence",
    "ChannelCreate",
    "__version__",
]
