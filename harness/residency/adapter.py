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
* No summon is ever refused in silence (2026-08-24): allowlist, hop cap and
  budget refusals all land in the summoning channel, attributed, because a
  silent drop reads exactly like a lost mention and gets retried.

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
from detector import (
    MODE_BOT_CHAIN,
    SummonDetector,
    Trigger,
    demote_mentions,
    find_work_item,
)
from hops import HopArbiter
from launcher import ContainerLauncher
from prompt import assemble_prompt
from summary import (
    format_chain_refusal_summary,
    format_drift_alert,
    format_gate_refusal_alert,
    format_refusal_summary,
    format_refusal_suffix,
    format_reply_suffix,
    format_summary,
)

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
        hops: Optional[HopArbiter] = None,
    ) -> None:
        self.client = client
        self.config = config
        self.detector = SummonDetector(config.summon)
        self.launcher = launcher or ContainerLauncher(config.container)
        self.budget = budget or BudgetLedger(
            config.budget.state_path, config.budget.daily_session_cap
        )
        self.cursor = cursor or CursorStore(config.cursor.state_path)
        self.hops = hops or HopArbiter(config.hops)

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
            # Unpark first, and for EVERY user message: a human summon of
            # either bot on the work item counts as the human having looked,
            # so the reset must not be reachable only through the not-a-summon
            # branch.
            await self._maybe_unpark(event)
            trigger = self.detector.detect(event)
            if trigger is not None:
                await self._handle_summon(event, trigger)
            # Persist the cursor whether or not we acted, so a restart resumes
            # from the right place regardless of summon activity.
            self._persist_cursor()

    async def _maybe_unpark(self, event: MessageCreate) -> None:
        """A human posting on a parked work item is what resumes its chain.

        Only a human post does, and only ever the counter — never the day
        ceiling. The clock does not unpark anything: a chain parked at 23:59
        is still parked at 00:01, and stays parked until a human has looked
        (Claudette #1811).
        """
        msg = event.message or {}
        if msg.get("author_type") != "user" or event.backfilled:
            return
        if not (self.config.summon.bot_summon and self.hops.configured):
            return
        if not self.detector.mention_only(event.channel_id):
            return
        work_item = find_work_item(msg.get("content") or "")
        if not work_item:
            return
        who = self.detector.summoner_name(event)
        if await asyncio.to_thread(
            self.hops.unpark, work_item=work_item, by=who, seq=event.seq
        ):
            logger.info("hop counter unparked for %s by %s", work_item, who)

    # --------------------------------------------------------------- summon

    def _where(self, channel_id: int) -> str:
        name = self.config.summon.channel_names.get(channel_id)
        return name if name else f"channel {channel_id}"

    async def _refuse(self, event: MessageCreate, trigger: Trigger, *,
                      line: str, by: str, summary: str) -> None:
        """Post a refusal in-channel, attributed, and audit it in #custodian.

        Never a silent drop (Gable #1804 ruling 2, Claudette #1811): silence is
        indistinguishable from the detector having eaten the mention, so the
        summoner retries — which is the loop the refusal exists to stop.
        """
        channel_id = event.channel_id
        where = self._where(channel_id)
        logger.info("summon refused: %s in %s (%s)", trigger.summoner, where,
                    summary)
        suffix = format_refusal_suffix(
            self.config.summon.bot_name, trigger.summoner, by=by)
        await self._safe_send(channel_id, f"{line}\n\n{suffix}",
                              reply_to=(event.message or {}).get("id"))
        await self._safe_send(
            self.config.summon.custodian_channel_id,
            format_chain_refusal_summary(
                summoner=trigger.summoner, where=where, reason=summary),
        )

    async def _clear_the_hop_wall(self, event: MessageCreate,
                                  trigger: Trigger) -> bool:
        """Guards 1-3 for a bot-triggered summon. True = serve it.

        Two walls, in order. The allowlist is this seat's own config: a bot
        nobody named cannot spend this seat at all. The hop counter is the
        broker's, shared by both adapters, and it is what decides whether the
        chain may continue PAST depth 1 — a live work item under the cap, or
        rule 1 and a reply that cannot re-trigger anyone.
        """
        peers = {p.lower() for p in self.config.summon.peer_bots}
        if trigger.summoner.lower() not in peers:
            await self._refuse(
                event, trigger,
                line=(f"summon refused: {trigger.summoner} is not on this "
                      f"seat's bot allowlist — a human can summon me instead"),
                by=f"{self.config.summon.bot_name}'s adapter",
                summary=f"{trigger.summoner} is not an allowlisted bot")
            return False

        decision = await asyncio.to_thread(
            self.hops.spend, work_item=trigger.work_item,
            summoner=trigger.summoner)
        if not decision.allowed:
            await self._refuse(
                event, trigger,
                line=decision.refusal or (
                    f"summon refused: {decision.work_item or 'this chain'} — "
                    f"{decision.reason or 'the broker refused the hop'}"),
                by="the house broker",
                summary=f"{decision.reason} ({decision.count}/{decision.cap})")
            return False
        trigger.chain = decision.chain
        trigger.chain_cap = decision.cap
        if decision.chain:
            trigger.depth = decision.count
        return True

    async def _handle_summon(self, event: MessageCreate,
                             trigger: Trigger) -> None:
        msg = event.message or {}
        channel_id = event.channel_id
        trigger_id = msg.get("id")
        summoner = trigger.summoner
        where = self._where(channel_id)

        if not self.budget.can_spend():
            logger.info("summon over budget: %s in %s", summoner, where)
            await self._safe_send(
                channel_id,
                f"{self.config.text.refusal_line}\n\n"
                + format_refusal_suffix(self.config.summon.bot_name, summoner,
                                        by=f"{self.config.summon.bot_name}'s "
                                           "daily budget"),
                reply_to=trigger_id,
            )
            await self._safe_send(
                self.config.summon.custodian_channel_id,
                format_refusal_summary(
                    summoner=summoner, where=where,
                    cap=self.config.budget.daily_session_cap,
                ),
            )
            return

        # Budget first, hop wall second: a summon this seat cannot afford must
        # not spend a hop off the shared counter on its way to being refused.
        if trigger.mode == MODE_BOT_CHAIN:
            if not await self._clear_the_hop_wall(event, trigger):
                return

        count = self.budget.spend()
        logger.info(
            "summon %d/%d: %s in %s (model pin %s)",
            count, self.config.budget.daily_session_cap, summoner, where,
            self.config.container.model or "unpinned",
        )

        prompt = await self._assemble(event, trigger, where)

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

        # BL-G1 pre-act model gate: the session was killed before (or, for a
        # mid-session switch, without) its output being trusted. NOTHING it
        # produced reaches the channel — only the configured operator-facing
        # line — and #custodian is told loudly what was expected vs seen. This
        # path only exists when plink has set container.model_gate = "refuse";
        # the shipped default never reaches it.
        if getattr(result, "gate_abort", False):
            logger.error(
                "MODEL GATE REFUSED summon by %s in %s: pinned %s, saw %s (at %s)",
                summoner, where, result.gate_expected,
                result.gate_actual or "no model id", result.gate_stage,
            )
            await self._safe_send(
                channel_id, self.config.text.model_gate_line, reply_to=trigger_id
            )
            await self._safe_send(
                self.config.summon.custodian_channel_id,
                format_gate_refusal_alert(
                    expected=result.gate_expected, actual=result.gate_actual,
                    stage=result.gate_stage or "unknown",
                    summoner=summoner, where=where,
                ),
            )
            await self._safe_send(
                self.config.summon.custodian_channel_id,
                format_summary(
                    summoner=summoner, where=where,
                    action_count=result.action_count,
                    duration_sec=result.duration_sec, ok=False,
                    model=result.gate_actual,
                ),
            )
            return

        # WP-L5 model integrity: assert the actually-used model against the
        # pin where knowable. pin = config (never chat); actual = what the
        # session reported (best-effort, may be None). display = what's really
        # running, for the visible suffix + audit line.
        pin = self.config.container.model
        # A failed session can still have proven its model: the stream's init
        # event names the resolved id before the turn runs, so a timeout kill
        # leaves the identity known even though the reply is lost. Gating this
        # on result.ok discarded that proof and reported every failure as
        # "actual unverified" — the same words the drift alarm uses (see
        # #custodian 2026-07-26). Trust the gate's observation, not the
        # session's exit status. A gate ABORT is the one exception: there the
        # model is the refusal's subject and the session is disowned entirely.
        actual = None if result.gate_abort else result.model
        verified = actual is not None
        display_model = actual or pin
        drift = bool(pin and actual and actual != pin)
        if drift:
            logger.error(
                "model drift: pinned %s but session ran %s (summon by %s in %s)",
                pin, actual, summoner, where,
            )
        elif pin and not verified:
            # Fail-OPEN guard: with no reported model we cannot prove the pin
            # ran. Do NOT let this pass as a clean match — log it, and the
            # suffix below marks it unverified rather than advertising the pin.
            logger.warning(
                "model unverified: pinned %s but session reported no model id "
                "(summon by %s in %s)", pin, summoner, where,
            )

        reply = result.reply.strip() if result.ok else ""
        text = reply if reply else self.config.text.error_line
        text = self._end_the_chain(text, trigger)
        # An unpinned deployment has no identity line to hang the attribution
        # off; a bot summon needs one anyway, because the reply is then the
        # only thing in the channel that says whose turn this was.
        if display_model or trigger.summoner_type == "bot":
            text = f"{text}\n\n{format_reply_suffix(self.config.summon.bot_name, display_model, verified=verified, summoner=summoner)}"
        await self._safe_send(channel_id, text, reply_to=trigger_id)

        # Fail-loud, never fail-over: on drift the reply still went out above;
        # here the house gets a loud alert naming expected vs actual.
        if drift:
            await self._safe_send(
                self.config.summon.custodian_channel_id,
                format_drift_alert(
                    expected=pin, actual=actual, summoner=summoner, where=where,
                ),
            )

        await self._safe_send(
            self.config.summon.custodian_channel_id,
            format_summary(
                summoner=summoner, where=where,
                action_count=result.action_count,
                duration_sec=result.duration_sec, ok=result.ok,
                model=display_model,
            ),
        )

    def _end_the_chain(self, text: str, trigger: Trigger) -> str:
        """GUARD 1: a bot-triggered summon's reply does not re-trigger any bot.

        Hard and adapter-enforced, because this adapter is the only thing that
        can enforce it — it owns the reply. The exception is the work-loop
        provision: a chain the broker granted keeps its mentions, so the
        review -> revision -> fix round-trip can actually run.
        """
        if trigger.mode != MODE_BOT_CHAIN or trigger.chain:
            return text
        return demote_mentions(text, self.config.summon.peer_bots)

    async def _assemble(self, event: MessageCreate, trigger: Trigger,
                        where: str) -> str:
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
            backfill, event.message or {}, summoner=trigger.summoner,
            where=where, how=trigger.describe(),
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
