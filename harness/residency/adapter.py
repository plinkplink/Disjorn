"""The Gable summon adapter daemon (WP-H9).

Connects to Disjorn as Gable (bot id 2) over the SDK's reconnecting WS event
stream, watches for summon triggers, and on each summon:

  1. checks the persisted daily budget (refuses politely if exhausted);
  2. assembles a session prompt from recent channel backfill + the trigger;
  3. spawns a headless Claude Code session in Gable's container via the
     configured launch command (run-resident.sh in prod, a stub in tests),
     keeping a typing indicator alive for the duration;
  4. posts the session's reply back to the channel;
  5. posts a one-line summary to #custodian for legibility.

Design invariants:

* Summon-mostly — nothing runs unless a message summons; each summon is one
  budgeted, audited session.
* Chat is data, never authorization — the argv, the budget cap, and every
  config field come from the plink-owned config file; a chat message is only
  ever the prompt handed to CC on stdin. See launcher.ContainerLauncher.
* Reconnect-from-seq handoff survives daemon restarts: the seq cursor is
  mirrored to disk and re-seeded at boot (cursor.CursorStore).
* One summon at a time — sessions are expensive; the daemon serves them
  sequentially, so the typing keepalive and subprocess share the loop without
  racing other summons.

The daemon depends only on a duck-typed client (the SDK's DisjornClient, or a
fake in tests): ``events()``, ``send()``, ``get_messages()``, ``typing()``,
``seed_seq()``, ``last_seen_seq``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from disjorn_sdk import MessageCreate, Ready

from budget import BudgetLedger
from config import AdapterConfig
from cursor import CursorStore
from detector import SummonDetector
from launcher import ContainerLauncher
from prompt import assemble_prompt
from summary import format_refusal_summary, format_summary

logger = logging.getLogger("disjorn.residency")

__all__ = ["SummonAdapter"]


class SummonAdapter:
    def __init__(
        self,
        client,
        config: AdapterConfig,
        *,
        launcher: Optional[ContainerLauncher] = None,
        budget: Optional[BudgetLedger] = None,
        cursor: Optional[CursorStore] = None,
    ) -> None:
        self.client = client
        self.config = config
        self.detector = SummonDetector(config.summon)
        self.launcher = launcher or ContainerLauncher(config.container)
        self.budget = budget or BudgetLedger(
            config.budget.state_path, config.budget.daily_session_cap
        )
        self.cursor = cursor or CursorStore(config.cursor.state_path)

    # --------------------------------------------------------------- run loop

    async def run(self) -> None:
        """Seed the cursor from disk, then consume events forever."""
        self._seed_cursor()
        try:
            async for event in self.client.events():
                try:
                    await self._dispatch(event)
                except Exception:  # noqa: BLE001 — one bad event never kills us
                    logger.exception("dispatch failed on %s", type(event).__name__)
        finally:
            self._persist_cursor()

    def _seed_cursor(self) -> None:
        saved = self.cursor.load()
        for channel_id, seq in saved.items():
            self.client.seed_seq(channel_id, seq)
        if saved:
            logger.info("re-seeded seq cursor for %d channel(s)", len(saved))

    def _persist_cursor(self) -> None:
        try:
            self.cursor.save(dict(self.client.last_seen_seq))
        except OSError:
            logger.warning("failed to persist seq cursor", exc_info=True)

    async def _dispatch(self, event) -> None:
        if isinstance(event, Ready):
            logger.info(
                "connected as bot %s (reconnected=%s)",
                getattr(event, "bot_id", "?"),
                getattr(event, "reconnected", False),
            )
            return
        if isinstance(event, MessageCreate):
            if self.detector.is_summon(event):
                await self._handle_summon(event)
            # Persist the cursor whether or not we acted, so a restart resumes
            # from the right place regardless of summon activity.
            self._persist_cursor()

    # --------------------------------------------------------------- summon

    def _where(self, channel_id: int) -> str:
        name = self.config.summon.channel_names.get(channel_id)
        return name if name else f"channel {channel_id}"

    async def _handle_summon(self, event: MessageCreate) -> None:
        msg = event.message or {}
        channel_id = event.channel_id
        trigger_id = msg.get("id")
        trigger_seq = event.seq
        summoner = self.detector.summoner_name(event)
        where = self._where(channel_id)

        if not self.budget.can_spend():
            logger.info("summon over budget: %s in %s", summoner, where)
            await self._safe_send(
                channel_id, self.config.text.refusal_line, reply_to=trigger_id
            )
            await self._safe_send(
                self.config.summon.custodian_channel_id,
                format_refusal_summary(
                    summoner=summoner, where=where,
                    cap=self.config.budget.daily_session_cap,
                ),
            )
            return

        count = self.budget.spend()
        logger.info(
            "summon %d/%d: %s in %s",
            count, self.config.budget.daily_session_cap, summoner, where,
        )

        prompt = await self._assemble(event, summoner, where)

        keepalive = asyncio.ensure_future(self._typing_keepalive(channel_id))
        try:
            result = await self.launcher.run(prompt)
        finally:
            keepalive.cancel()
            try:
                await keepalive
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        if not result.ok:
            # The polite channel line hides the cause on purpose; the log
            # must not — a silent 0.0s failure cost a debugging round.
            logger.warning("summon session failed: %s", result.error or "(no detail)")
        reply = result.reply.strip() if result.ok else ""
        text = reply if reply else self.config.text.error_line
        await self._safe_send(channel_id, text, reply_to=trigger_id)

        await self._safe_send(
            self.config.summon.custodian_channel_id,
            format_summary(
                summoner=summoner, where=where,
                action_count=result.action_count,
                duration_sec=result.duration_sec, ok=result.ok,
            ),
        )

    async def _assemble(self, event: MessageCreate, summoner: str, where: str) -> str:
        channel_id = event.channel_id
        trigger_seq = event.seq
        try:
            recent = await self.client.get_messages(
                channel_id,
                before_seq=trigger_seq,
                limit=self.config.backfill.count_for(channel_id),
            )
        except Exception:  # noqa: BLE001 — backfill is best-effort context
            logger.warning("backfill fetch failed for channel %s", channel_id,
                           exc_info=True)
            recent = []
        # before_seq mode returns newest-first; make it chronological.
        backfill = list(reversed(recent))
        return assemble_prompt(
            backfill, event.message or {}, summoner=summoner, where=where
        )

    # --------------------------------------------------------------- helpers

    async def _typing_keepalive(self, channel_id: int) -> None:
        """Emit typing immediately, then every interval, until cancelled."""
        interval = self.config.summon.typing_interval_sec
        while True:
            await self._safe_typing(channel_id)
            await asyncio.sleep(interval)

    async def _safe_typing(self, channel_id: int) -> None:
        try:
            await self.client.typing(channel_id)
        except Exception:  # noqa: BLE001 — no live WS / rate limit: keep going
            logger.debug("typing failed for channel %s", channel_id, exc_info=True)

    async def _safe_send(self, channel_id: int, content: str, *, reply_to=None) -> None:
        try:
            await self.client.send(channel_id, content, reply_to=reply_to)
        except Exception:  # noqa: BLE001 — a failed post never crashes the daemon
            logger.warning("send to channel %s failed", channel_id, exc_info=True)
