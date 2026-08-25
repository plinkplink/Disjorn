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
    "format_branches",
    "format_wake_done",
    "format_wake_failed",
    "format_wake_missed",
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


# --------------------------------------------------------------------- wake
# SPECS/2026-08-25-agentic-residents.md. Every wake ends in a #custodian post,
# and the poster is the WRAPPER, not the session: a banner is evidence only when
# the process that posts it is not the process it describes. So every field
# below is something the runner measured host-side — the exit status, the clock,
# the action-log delta, the gatehouse refs before and after — with exactly one
# labelled exception, the session's closing words on a wake that finished.


def _fmt_branches(branches) -> str:
    """The loop branches this wake moved, from the gatehouse refs, or an honest
    statement of what was not observed.

    A `wip:` head is quoted as-is: it is the session's own hand-over signal, and
    the point of quoting it is that a reader can see partial work without
    opening anything (`branches` entries carry `wip` already resolved by the
    watcher)."""
    if branches is None:
        return "branches: not observed (no gatehouse configured)"
    if not branches:
        return "branches: none moved"
    parts = []
    for b in branches:
        subject = " ".join((b.get("subject") or "").split())[:120]
        state = "wip" if b.get("wip") else ("new" if b.get("created") else "moved")
        parts.append(f"{b.get('repo')}:{b.get('ref')} @ {b.get('sha', '')[:12]} "
                     f"({state}) \"{subject}\"")
    return "branches: " + "; ".join(parts)


format_branches = _fmt_branches


def _fmt_clock(duration_sec: float, cap_sec) -> str:
    if cap_sec:
        return f"{duration_sec:.1f}s of {int(cap_sec)}s"
    return f"{duration_sec:.1f}s"


def _fmt_wake_model(model: Optional[str], verified: bool) -> str:
    """The substrate, named in the banner (spec decision 3: anything speaking
    under a resident's name stays on that resident's pinned model, and the
    banner says which). Unverified is marked, never smoothed over — the summon
    reply suffix learned that the hard way."""
    if not model:
        return "model unknown"
    return model if verified else f"{model} (pinned; actual unverified)"


def _fmt_wake_actions(action_count: Optional[int]) -> str:
    """Actions the WRAPPER counted (lines the container appended to the house
    action log while the session ran), not a number the session reported."""
    if action_count is None:
        return "actions n/a (no action log)"
    return f"{action_count} actions"


def format_wake_done(
    *,
    wake_id: str,
    resident: str,
    woken_by: str,
    duration_sec: float,
    cap_sec: Optional[int],
    action_count: Optional[int],
    model: Optional[str] = None,
    model_verified: bool = True,
    branches=None,
    account: str = "",
) -> str:
    """The result post for a wake whose session exited cleanly.

    ``account`` is the session's own closing words, and it is the only field
    here the wrapper did not measure — carried because a human reading
    #custodian wants to know what the session thought it did, labelled because
    the branch and the clock are what decide whether it did it."""
    line = (
        f"wake done | {wake_id} | {resident} woken by {woken_by} | "
        f"{_fmt_wake_model(model, model_verified)} | "
        f"{_fmt_clock(duration_sec, cap_sec)} | {_fmt_wake_actions(action_count)} | "
        f"{_fmt_branches(branches)}"
    )
    if account:
        line += "\nsession's own account (not the evidence above): " + account
    return line


def format_wake_failed(
    *,
    wake_id: str,
    resident: str,
    woken_by: str,
    reason: str,
    duration_sec: float,
    cap_sec: Optional[int],
    action_count: Optional[int],
    model: Optional[str] = None,
    model_verified: bool = True,
    branches=None,
) -> str:
    """The result post for a wake that was killed or died — LOUD, and posted
    from the same observations as the clean one.

    ``reason`` is the one field that differs by failure: the cap that fired, or
    the exit code/signal. Nothing the session produced appears here at all: a
    session we could not let finish is a session whose account of itself we
    cannot use, and the branch is what says how far it actually got."""
    return (
        f"WAKE FAILED | {wake_id} | {resident} woken by {woken_by} | "
        f"{' '.join(reason.split())[:300]} | "
        f"{_fmt_wake_model(model, model_verified)} | "
        f"{_fmt_clock(duration_sec, cap_sec)} | {_fmt_wake_actions(action_count)} | "
        f"{_fmt_branches(branches)} | a human should look"
    )


def format_wake_missed(*, wake_id: str, resident: str, woken_by: str,
                       requested_at: str) -> str:
    """A wake this runner found only after its whole window had passed — the
    daemon was down when it was asked for.

    It is posted late rather than dropped, and the session is NOT run: a wake is
    a human waiting, and starting one hours after the ask is worse than saying
    plainly that nobody was home."""
    return (
        f"WAKE MISSED | {wake_id} | {resident} woken by {woken_by} at "
        f"{requested_at} | the wake runner was not running inside this wake's "
        f"window, so no session ever started | nothing ran — wake again if it "
        f"still matters"
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
