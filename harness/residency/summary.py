"""One-line #custodian session summaries (WP-H9 legibility).

Every summon — served or refused — leaves a single legible line in #custodian
so the house can see who summoned Gable, where, and what it cost. Plain text,
no emoji, greppable.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["format_summary", "format_refusal_summary"]


def _fmt_actions(action_count: Optional[int]) -> str:
    return f"{action_count} actions" if action_count is not None else "actions n/a"


def format_summary(
    *,
    summoner: str,
    where: str,
    action_count: Optional[int],
    duration_sec: float,
    ok: bool,
) -> str:
    status = "ok" if ok else "error"
    return (
        f"summon | {summoner} in {where} | {status} | "
        f"{_fmt_actions(action_count)} | {duration_sec:.1f}s"
    )


def format_refusal_summary(*, summoner: str, where: str, cap: int) -> str:
    return (
        f"summon refused | {summoner} in {where} | "
        f"daily budget reached (cap {cap})"
    )
