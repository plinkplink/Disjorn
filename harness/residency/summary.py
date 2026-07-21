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
    "format_drift_alert",
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


def format_reply_suffix(bot_name: str, model: str) -> str:
    """Identity suffix appended to a summon reply (WP-L5 VISIBLE).

    Every reply shows what's actually running — the platform-suffix idiom, so
    a silent model swap is visible in-channel, not just in the audit log.
    """
    return f"— {bot_name} · {model}"


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
