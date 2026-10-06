"""Plan Room transport and the board, approval and backlog verbs."""

from __future__ import annotations

import datetime as _dt
import json
from typing import Optional

from broker_common import (
    MAX_REQUEST_BYTES, BOARD_SLUG_RE, VerbError, _bad, _check_int, _check_str,
    _reject_unknown,
)


# Plan Room (SPECS/2026-08-20-plan-room.md).
MAX_BOARD_CARDS = 200
MAX_BOARD_COMMENT_CHARS = 4000
MAX_BOARD_REASON_CHARS = 500
MAX_BOARD_SEARCH_CHARS = 200
PLANROOM_HTTP_TIMEOUT = 20
# The approval object (same spec, item 1). Three verbs over the server's
# /approval surface, as the broker's own bot identity — the board verbs' idiom,
# and for the board verbs' reason: one composer, one state of record.
MAX_APPROVAL_ROWS = 200
MAX_APPROVAL_REMARKS_CHARS = 4000
APPROVAL_ACTIONS = ("approve", "deny", "rework")
# The resident backlog verbs. The text cap is the server's `/backlog` cap and is
# not restated here: the server's refusal reaches the seat verbatim.
BACKLOG_STATUSES = ("open", "built", "rejected", "duplicate", "spec'd")
MAX_BACKLOG_LIST_ROWS = 50
BACKLOG_TEXT_CLIP = 300
BACKLOG_PAGE = 200
BACKLOG_DAILY_FILE_CAP = 10


def format_approval_line(proposal: dict) -> str:
    """One proposal, one line. brief's rule, inherited by the board verbs and
    kept here: never print a bare identifier — a row you have to go look up is
    a row that gets deferred."""
    states = proposal.get("states") or []
    answers = " ".join(
        f"{s.get('principal', '?')}={s.get('state', '?')}" for s in states)
    bits = [f"[{proposal.get('decision', '?')}]",
            f"#{proposal.get('id', '?')}",
            str(proposal.get("slug", "?")),
            str(proposal.get("title", "")).strip()]
    line = " ".join(b for b in bits if b)
    if answers:
        line += f" — {answers}"
    if proposal.get("closed_at"):
        line += f" (closed {proposal['closed_at']})"
    return line


# --------------------------------------------------------------------------
# Plan Room API transport (SPECS/2026-08-20-plan-room.md). Kept behind a callable so
# tests stub it, exactly like _sdk_transport above.
# --------------------------------------------------------------------------

def _api_label(path: str) -> str:
    """What to CALL the surface in a refusal."""
    if path.startswith("/apps"):
        return "apps API"
    if path.startswith("/approval"):
        return "approval API"
    if path.startswith("/backlog"):
        return "backlog API"
    return "plan room API"


def _planroom_http(disjorn_cfg: dict, method: str, path: str,
                   payload: Optional[dict] = None) -> dict:
    """One JSON call to the Disjorn server, as the broker's own bot identity."""
    import urllib.error
    import urllib.request

    base = str(disjorn_cfg.get("url") or "").rstrip("/")
    if not base:
        raise VerbError("internal", "no [disjorn].url configured")
    try:
        with open(disjorn_cfg["api_key_path"], "r", encoding="utf-8") as fh:
            api_key = fh.read().strip()
    except (KeyError, OSError) as exc:
        raise VerbError("internal", f"broker API key unreadable: {exc}") from None

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=PLANROOM_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8")).get("detail", ""))
        except Exception:  # noqa: BLE001 — a non-JSON error body is still a refusal
            pass
        # The server's refusal is carried through verbatim.
        raise VerbError("exec-failure",
                        detail or f"{_api_label(path)} returned {exc.code}",
                        status=exc.code) from None
    except Exception as exc:  # noqa: BLE001 — network, DNS, timeout, bad JSON
        raise VerbError("exec-failure",
                        f"{_api_label(path)} unreachable: {exc}") from None


def _urlq(value: str) -> str:
    import urllib.parse
    return urllib.parse.quote(value, safe="")


def format_board_line(card: dict) -> str:
    """One card, one line. brief's rule, inherited: NEVER PRINT A BARE IDENTIFIER —
    every row says what the thing is and where it lives, because an item you have to
    go look up is an item that gets deferred."""
    bits = [f"[{card.get('column', '?')}]", str(card.get("slug", "?"))]
    title = card.get("title")
    if title and title != card.get("slug"):
        bits.append(f"— {title}")
    tail = []
    if card.get("tier"):
        tail.append(str(card["tier"]))
    if card.get("review_owner"):
        tail.append(f"review {card['review_owner']}")
    if card.get("builder"):
        tail.append(f"builder {card['builder']}")
    if card.get("confirm_seq"):
        tail.append(f"seq {card['confirm_seq']}")
    if card.get("comment_count"):
        tail.append(f"{card['comment_count']} comment(s)")
    for flag in card.get("flags") or []:
        tail.append(f"!{flag}")
    if (card.get("deploy") or {}).get("badge"):
        tail.append(f"deploy {_deploy_words(card['deploy'])}")
    if card.get("blocked"):
        tail.append(f"BLOCKED: {card.get('blocked_reason') or 'no reason given'}")
    line = " ".join(bits)
    return f"{line}  ·  {' · '.join(tail)}" if tail else line


def _deploy_words(deploy: dict) -> str:
    badge = deploy.get("badge", "unknown")
    label = deploy.get("label")
    return f"{badge} ({label})" if label and label != badge else badge


def format_board_face(face: dict) -> str:
    """The board's own staleness, said out loud."""
    if face.get("available") is False:
        return f"UNAVAILABLE — {face.get('unavailable_reason', 'no reason given')}"
    head = str(face.get("mirror_head") or "?")[:12]
    out = (f"derived {face.get('derived_at', '?')} from mirror {head}; "
           f"deploy {_deploy_words(face.get('deploy') or {})}")
    for note in face.get("notes") or []:
        out += f"\nnote: {note}"
    return out


class PlanroomVerbs:

    # ---------------------------------------------------------- plan room
    # Five verbs. Three read, two write, and the two writes touch BOARD-NATIVE STATE
    # ONLY: comments and the blocked flag + its reason. They are structurally unable
    # to touch derived state — not because anything here checks, but because derived
    # state has no write path anywhere in this house (P1).

    def _board_slug(self, args: dict) -> str:
        slug = _check_str(args, "slug", required=True, max_len=80)
        assert slug is not None
        if not BOARD_SLUG_RE.match(slug):
            raise _bad("slug must be a spec slug (YYYY-MM-DD-name), a "
                       "`backlog-<n>`, or a `keyboard-<sha>`")
        return slug

    def _board_get(self, path: str) -> dict:
        return self.planroom_api(self.disjorn, "GET", path)

    def _board_post(self, path: str, payload: dict) -> dict:
        return self.planroom_api(self.disjorn, "POST", path, payload)

    def _verb_board_list(self, resident: str, args: dict) -> tuple[dict, str]:
        """The board, ONE LINE PER CARD."""
        _reject_unknown(args, {"column", "lane", "owner", "blocked", "limit"})
        query: list[str] = []
        for key in ("column", "lane", "owner"):
            val = _check_str(args, key, max_len=100)
            if val is not None:
                query.append(f"{key}={_urlq(val)}")
        blocked = _check_str(args, "blocked", max_len=8)
        if blocked is not None:
            if blocked not in ("yes", "no"):
                raise _bad("blocked must be 'yes' or 'no'")
            query.append(f"blocked={'true' if blocked == 'yes' else 'false'}")
        limit = _check_int(args, "limit", 80, 1, MAX_BOARD_CARDS)
        qs = ("?" + "&".join(query)) if query else ""
        body = self._board_get("/planroom/board" + qs)
        cards = body.get("cards") or []
        lines = [format_board_line(c) for c in cards[:limit]]
        return ({"face": format_board_face(body.get("face") or {}),
                 "counts": body.get("counts") or {},
                 "cards": lines, "count": len(lines),
                 "truncated": len(cards) > limit},
                f"{len(lines)} of {len(cards)} cards")

    def _verb_board_card(self, resident: str, args: dict) -> tuple[dict, str]:
        """Everything on one card, comments included."""
        _reject_unknown(args, {"slug"})
        slug = self._board_slug(args)
        body = self._board_get(f"/planroom/cards/{slug}")
        comments = body.get("comments") or []
        return ({"face": format_board_face(body.get("face") or {}),
                 "card": body.get("card"), "comments": comments,
                 "note": body.get("note")},
                f"card {slug} ({len(comments)} comment(s))")

    def _verb_board_search(self, resident: str, args: dict) -> tuple[dict, str]:
        """Substring search across card text and comments, one line per hit."""
        _reject_unknown(args, {"text", "limit"})
        text = _check_str(args, "text", required=True,
                          max_len=MAX_BOARD_SEARCH_CHARS)
        assert text is not None
        limit = _check_int(args, "limit", 40, 1, MAX_BOARD_CARDS)
        body = self._board_get(
            f"/planroom/search?q={_urlq(text)}&limit={limit}")
        cards = body.get("cards") or []
        return ({"face": format_board_face(body.get("face") or {}),
                 "cards": [format_board_line(c) for c in cards],
                 "count": len(cards), "truncated": bool(body.get("truncated"))},
                f"{len(cards)} hits for {text!r}")

    def _verb_board_flag(self, resident: str, args: dict) -> tuple[dict, str]:
        """Block or unblock a card, with a reason."""
        _reject_unknown(args, {"slug", "action", "reason"})
        slug = self._board_slug(args)
        action = _check_str(args, "action", required=True, max_len=20)
        if action not in ("blocked", "unblock"):
            raise _bad("action must be 'blocked' or 'unblock'")
        reason = _check_str(args, "reason", max_len=MAX_BOARD_REASON_CHARS)
        blocked = action == "blocked"
        if blocked and not (reason or "").strip():
            raise _bad("blocking a card needs a reason — a card blocked for no "
                       "stated reason is one nobody can unblock")
        body = self._board_post(f"/planroom/cards/{slug}/flag",
                                {"blocked": blocked, "reason": reason,
                                 "author": resident})
        card = body.get("card") or {}
        return ({"slug": slug, "blocked": bool(card.get("blocked")),
                 "reason": card.get("blocked_reason"),
                 "column": card.get("column"),
                 "card": format_board_line(card) if card else None},
                f"{slug} {'blocked' if blocked else 'unblocked'}"
                + (f": {reason[:120]}" if blocked and reason else ""))

    def _verb_board_comment(self, resident: str, args: dict) -> tuple[dict, str]:
        """Add a comment to a card."""
        _reject_unknown(args, {"slug", "text"})
        slug = self._board_slug(args)
        text = _check_str(args, "text", required=True,
                          max_len=MAX_BOARD_COMMENT_CHARS)
        assert text is not None
        body = self._board_post(f"/planroom/cards/{slug}/comment",
                                {"text": text, "author": resident})
        comment = body.get("comment") or {}
        return ({"slug": slug, "comment": comment},
                f"comment on {slug} ({len(text)} chars)")

    # ------------------------------------------------- the approval object

    def _verb_approval_list(self, resident: str, args: dict) -> tuple[dict, str]:
        """Open proposals, ONE LINE EACH. Skim here, detail in approval-show."""
        _reject_unknown(args, {"state", "limit"})
        state = _check_str(args, "state", max_len=8)
        if state is not None and state not in ("open", "closed"):
            raise _bad("state must be 'open' or 'closed'")
        limit = _check_int(args, "limit", 50, 1, MAX_APPROVAL_ROWS)
        query = f"?limit={limit}" + (f"&state={_urlq(state)}" if state else "")
        body = self._board_get("/approval/proposals" + query)
        proposals = body.get("proposals") or []
        return ({"proposals": [format_approval_line(p) for p in proposals],
                 "count": len(proposals),
                 "truncated": bool(body.get("truncated"))},
                f"{len(proposals)} approval proposal(s)")

    def _verb_approval_show(self, resident: str, args: dict) -> tuple[dict, str]:
        """One proposal in full: text, every principal's state and remarks."""
        _reject_unknown(args, {"id"})
        proposal_id = _check_int(args, "id", 0, 1, 2 ** 53)
        body = self._board_get(f"/approval/proposals/{proposal_id}")
        proposal = body.get("proposal") or {}
        return ({"proposal": proposal,
                 "line": format_approval_line(proposal) if proposal else None},
                f"proposal #{proposal_id} ({proposal.get('decision', '?')})")

    def _verb_approval_act(self, resident: str, args: dict) -> tuple[dict, str]:
        """Approve, deny or rework a proposal, with remarks.

        THE PRINCIPAL IS STAMPED HERE, from the caller's SO_PEERCRED-derived
        seat name, never from `args`. It is the whole reason this is a verb
        rather than an API key handed to a resident: the server cannot tell
        which seat is behind the broker's bot identity, and a principal a
        caller could name is a principal any caller could answer as."""
        _reject_unknown(args, {"id", "action", "remarks"})
        proposal_id = _check_int(args, "id", 0, 1, 2 ** 53)
        action = _check_str(args, "action", required=True, max_len=10)
        if action not in APPROVAL_ACTIONS:
            raise _bad(f"action must be one of {', '.join(APPROVAL_ACTIONS)}")
        remarks = _check_str(args, "remarks", max_len=MAX_APPROVAL_REMARKS_CHARS)
        body = self._board_post(
            f"/approval/proposals/{proposal_id}/act",
            {"principal": resident, "action": action, "remarks": remarks})
        proposal = body.get("proposal") or {}
        return ({"proposal": proposal,
                 "line": format_approval_line(proposal) if proposal else None},
                f"{resident} {action} on proposal #{proposal_id} "
                f"-> {proposal.get('decision', '?')}")

    # ------------------------------------------------------------ backlog

    def _verb_backlog_list(self, resident: str, args: dict) -> tuple[dict, str]:
        """One status, newest first; `open` unless another is named."""
        _reject_unknown(args, {"status", "limit"})
        status = _check_str(args, "status", max_len=12) or "open"
        if status not in BACKLOG_STATUSES:
            raise _bad(f"status must be one of {', '.join(BACKLOG_STATUSES)}")
        limit = _check_int(args, "limit", 20, 1, MAX_BACKLOG_LIST_ROWS)
        matched: list[dict] = []
        from_id = 0
        while True:
            page = self.planroom_api(
                self.disjorn, "GET",
                f"/backlog?from_id={from_id}&limit={BACKLOG_PAGE}")
            if not isinstance(page, list):
                raise VerbError("exec-failure", "backlog API answered a non-list")
            matched += [r for r in page if r.get("status") == status]
            if len(page) < BACKLOG_PAGE:
                break
            from_id = int(page[-1]["id"]) + 1
        rows = []
        for r in matched[::-1][:limit]:
            text = str(r.get("text") or "")
            if len(text) > BACKLOG_TEXT_CLIP:
                text = text[:BACKLOG_TEXT_CLIP - 1] + "…"
            rows.append({"id": r.get("id"), "text": text,
                         "author": r.get("author"),
                         "created_at": r.get("created_at"),
                         "status": r.get("status"),
                         "spec_ref": r.get("spec_ref")})
        return ({"status": status, "rows": rows, "count": len(rows),
                 "truncated": len(matched) > limit},
                f"{len(rows)} {status} backlog row(s)")

    def _verb_backlog_file(self, resident: str, args: dict) -> tuple:
        """One `open` row whose author is the calling seat's peer identity,
        never a name from `args`."""
        _reject_unknown(args, {"text"})
        text = _check_str(args, "text", required=True, max_len=MAX_REQUEST_BYTES)
        self._reserve_backlog_file(resident)
        try:
            row = self.planroom_api(self.disjorn, "POST", "/backlog",
                                    {"text": text, "on_behalf_of": resident})
        except VerbError:
            self._release_backlog_file(resident)
            raise
        return ({"row": row},
                f"filed backlog #{row.get('id')} for {resident}",
                {"backlog_filed": True})

    def _count_backlog_filed(self, resident: str, today: str) -> int:
        n = 0
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    if "backlog_filed" not in raw or resident not in raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if (rec.get("resident") == resident
                            and rec.get("verb") == "backlog-file"
                            and rec.get("backlog_filed") is True
                            and str(rec.get("ts", ""))[:10] == today):
                        n += 1
        except OSError:
            return 0
        return n

    def _reserve_backlog_file(self, resident: str) -> None:
        """The audit log is the count after a restart, so the cap outlives one."""
        cap = BACKLOG_DAILY_FILE_CAP
        with self._backlog_lock:
            today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
            date, count = self._backlog_files.get(resident, (None, 0))
            if date != today:
                count = self._count_backlog_filed(resident, today)
            if count >= cap:
                self._backlog_files[resident] = (today, count)
                raise VerbError(
                    "over-budget",
                    f"{resident} has filed {count} backlog rows today, the "
                    f"daily cap of {cap}; nothing was filed. The count resets "
                    "at 00:00 UTC.")
            self._backlog_files[resident] = (today, count + 1)

    def _release_backlog_file(self, resident: str) -> None:
        with self._backlog_lock:
            date, count = self._backlog_files.get(resident, (None, 0))
            if count > 0:
                self._backlog_files[resident] = (date, count - 1)
