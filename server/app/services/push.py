"""Web Push delivery (WP7): pywebpush send + dead-subscription pruning.

Exports:
    send_push(subscription_row, payload)  — one push to one subscription row
        (a dict from the push_subscriptions table). VAPID keys + claims email
        come from config. WebPushException is caught here; a 404/410 response
        means the endpoint is gone, so the row is deleted (prune dead subs).
    notify_users(user_ids, payload)       — fan a payload out to every stored
        subscription of the given users.

Neither function ever raises into the caller — failures are logged. The actual
`webpush()` call is blocking (requests), so it runs in a thread via
asyncio.to_thread; tests monkeypatch `send_push` (or `webpush`) instead of
hitting the network.
"""

import asyncio
import inspect
import json
import logging
from typing import Any, Iterable

from pywebpush import WebPushException, webpush

from .. import db
from ..config import get_settings

logger = logging.getLogger(__name__)

_DEAD_SUBSCRIPTION_STATUSES = (404, 410)


async def send_push(subscription_row: dict[str, Any], payload: dict[str, Any]) -> None:
    """Send one push message to one push_subscriptions row. Never raises.

    On a 404/410 response the subscription is dead (browser unsubscribed or
    endpoint expired) — the row is deleted so we stop trying.
    """
    settings = get_settings()
    if not settings.VAPID_PRIVATE_KEY:
        logger.warning("push skipped: VAPID_PRIVATE_KEY not configured")
        return
    subscription_info = {
        "endpoint": subscription_row["endpoint"],
        "keys": json.loads(subscription_row["keys_json"] or "{}"),
    }
    try:
        await asyncio.to_thread(
            webpush,
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.VAPID_CLAIMS_EMAIL},
        )
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in _DEAD_SUBSCRIPTION_STATUSES:
            await db.execute(
                "DELETE FROM push_subscriptions WHERE id = ?",
                (subscription_row["id"],),
            )
            logger.info(
                "pruned dead push subscription id=%s user_id=%s (HTTP %s)",
                subscription_row["id"], subscription_row.get("user_id"), status,
            )
        else:
            logger.warning(
                "web push failed for subscription id=%s (HTTP %s): %s",
                subscription_row["id"], status, exc,
            )
    except Exception:  # noqa: BLE001 — push must never break the caller
        logger.exception(
            "web push errored for subscription id=%s", subscription_row.get("id")
        )


async def notify_users(user_ids: Iterable[int], payload: dict[str, Any]) -> None:
    """Send `payload` to every stored subscription of the given users.

    Deduplicates user ids; per-subscription failures are logged and never
    propagate (send_push handles WebPushException; anything else is caught
    here). Calls `send_push` through the module global so tests can
    monkeypatch it — sync replacements are fine (awaitable-checked).
    """
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    rows = await db.fetch_all(
        f"SELECT * FROM push_subscriptions WHERE user_id IN ({placeholders})", ids
    )
    for row in rows:
        try:
            result = send_push(row, payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — one bad sub never blocks the rest
            logger.exception(
                "notify_users: send failed for subscription id=%s", row.get("id")
            )
