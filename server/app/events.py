"""Internal async pub/sub event bus.

Modules publish domain events as plain dicts; subscribers (e.g. the WS hub,
push notifier) receive every published event. Convention (BUILD-PLAN):

    {"type": "message_create" | "message_edit" | "message_delete"
             | "typing_start" | "presence",
     "channel_id": int | None,
     ...full materialized payload}

Subscribers may be sync or async callables taking a single dict. A failing
subscriber is logged and never breaks publishing or other subscribers.
"""

import inspect
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Subscriber = Callable[[dict[str, Any]], None | Awaitable[None]]

_subscribers: list[Subscriber] = []


def subscribe(fn: Subscriber) -> Subscriber:
    """Register a subscriber. Returns fn so it can be used as a decorator."""
    if fn not in _subscribers:
        _subscribers.append(fn)
    return fn


def unsubscribe(fn: Subscriber) -> None:
    """Remove a subscriber; no-op if not registered."""
    try:
        _subscribers.remove(fn)
    except ValueError:
        pass


async def publish(event: dict[str, Any]) -> None:
    """Deliver an event to all subscribers, in registration order."""
    for fn in list(_subscribers):
        try:
            result = fn(event)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — bus must never propagate subscriber errors
            logger.exception("event subscriber %r failed for event type %r", fn, event.get("type"))


def clear_subscribers() -> None:
    """Remove all subscribers — used by tests."""
    _subscribers.clear()
