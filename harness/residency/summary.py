"""One-line #custodian session summaries (WP-H9 legibility).

Every summon — served or refused — leaves a single legible line in #custodian
so the house can see who summoned Gable, where, and what it cost. Plain text,
no emoji, greppable.
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "format_summary",
    "format_refusal_summary",
    "format_reply_suffix",
    "format_refusal_suffix",
    "format_chain_refusal_summary",
    "format_drift_alert",
    "format_gate_refusal_alert",
]


def _fmt_actions(action_count: Optional[int]) -> str:
    return f"{action_count} actions" if action_count is not None else "actions n/a"


def format_summary(
    *,
    summoner: str,
    where: str,
    action_count: Optional[int],
    duration_sec: float,
    ok: bool,
    model: Optional[str] = None,
) -> str:
    """One-line #custodian audit of a served summon.

    ``model`` (WP-L5) is the model this session ran under — appended so the
    audit trail records what actually served every summon. Omitted only for an
    unpinned deployment where no model is knowable.
    """
    status = "ok" if ok else "error"
    line = (
        f"summon | {summoner} in {where} | {status} | "
        f"{_fmt_actions(action_count)} | {duration_sec:.1f}s"
    )
    if model:
        line += f" | {model}"
    return line


def format_refusal_summary(*, summoner: str, where: str, cap: int) -> str:
    return (
        f"summon refused | {summoner} in {where} | "
        f"daily budget reached (cap {cap})"
    )


def format_chain_refusal_summary(
    *, summoner: str, where: str, reason: str
) -> str:
    """One-line #custodian audit of a bot-to-bot summon that was refused."""
    return f"summon refused | {summoner} in {where} | {reason}"


def format_refusal_suffix(bot_name: str, summoner: str, *, by: str) -> str:
    """Attribution on a refusal (Gable #1804 ruling 2: never silence).

    A refusal says who was asking, who is not answering, and WHO REFUSED —
    the broker's wall and this seat's own guards read the same from the
    channel otherwise, and they are unparked by different things.
    """
    return f"— {bot_name} · summoned by {summoner} · refused by {by}"


def format_reply_suffix(bot_name: str, model: Optional[str] = None, *,
                        verified: bool = True,
                        summoner: Optional[str] = None) -> str:
    """Identity suffix appended to a summon reply (WP-L5 VISIBLE).

    Every reply shows what's actually running — the platform-suffix idiom, so
    a silent model swap is visible in-channel, not just in the audit log.

    ``verified`` is False when the session did not report its model id (so
    ``model`` is the *pin*, not a confirmed fact). We must not stamp an
    unconfirmed pin as if it ran — that would invert the whole point of the
    suffix. Mark it explicitly instead.

    ``summoner`` names who spent this seat's budget (2026-08-24 guard 4). It
    matters most when the summoner is another bot: the reply is then the only
    place in the channel that says whose turn this was — which is why the
    suffix can now be built without a model at all, for the unpinned
    deployment that would otherwise carry no attribution.
    """
    line = f"— {bot_name}"
    if model:
        line += f" · {model}"
        if not verified:
            line += " (pinned; actual unverified)"
    if summoner:
        line += f" · summoned by {summoner}"
    return line


def format_drift_alert(*, expected: str, actual: str, summoner: str, where: str) -> str:
    """Loud #custodian alert on a pin/actual model mismatch (WP-L5 DRIFT).

    Fail-loud, never fail-over: the reply still goes out, but the house is told
    the session did NOT run the pinned model and a human should intervene.
    """
    return (
        f"MODEL DRIFT | summon by {summoner} in {where} | "
        f"pinned {expected} but session ran {actual} | "
        f"no fallback — a human should check #custodian and the pin"
    )


def format_gate_refusal_alert(
    *,
    expected: Optional[str],
    actual: Optional[str],
    stage: str,
    summoner: str,
    where: str,
    detail: str = "",
) -> str:
    """Loud #custodian alert when the PRE-ACT model gate killed a session (BL-G1).

    Distinct from format_drift_alert on purpose: drift means "the wrong model
    already answered and the answer shipped"; this means "the wrong model was
    caught before it answered and NOTHING it produced was posted." Reading the
    two the same way would lose exactly the distinction the gate exists to make.
    """
    saw = actual or "no model id"
    line = (
        f"MODEL GATE REFUSED | summon by {summoner} in {where} | "
        f"pinned {expected} but session came up on {saw} (at {stage}) | "
        f"session killed, nothing it produced was posted | "
        f"no fallback — a human should check the pin and the account"
    )
    if detail:
        line += f" | {detail}"
    return line
