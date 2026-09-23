"""Approval object — the arming gate, the seat rule, and the derived decision.

Three claims are on trial and the rest is detail:

1. **It ships OFF, loudly.** Every endpoint, read and write alike, refuses with
   503 while `APPROVAL_ENABLED` is false, and the refusal names the setting. A
   disarmed surface and an empty one must not read alike, because the broker
   repeats that sentence to a resident verbatim.
2. **One state of record, reachable from either seat.** A resident answering
   through the broker's bot key and plink answering from the keyboard write the
   same row, with typed attribution either way — and a signed-in human can only
   ever answer as themselves.
3. **The decision is derived, never stored.** Deny outranks rework outranks
   pending; `closed_at` follows from the answers rather than from an endpoint,
   and a `rework` closes nothing.
"""

import httpx
import pytest

from app import db
from app.config import reset_settings_cache
from app.routers import approval, auth

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = auth.hash_password(PASSWORD)  # hash once — argon2 is slow
BOT_KEY = "bot-key-approval"

PRINCIPALS = ["plink", "res-claudette", "res-gable"]


@pytest.fixture
def armed(monkeypatch):
    """The witnessed config change, in test form. Both settings are pinned
    because server/.env is read too and would otherwise decide for us."""
    monkeypatch.setenv("APPROVAL_ENABLED", "true")
    monkeypatch.setenv("APPROVAL_PRINCIPALS", ",".join(PRINCIPALS))
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def disarmed(monkeypatch):
    monkeypatch.setenv("APPROVAL_ENABLED", "false")
    monkeypatch.setenv("APPROVAL_PRINCIPALS", ",".join(PRINCIPALS))
    reset_settings_cache()
    yield
    reset_settings_cache()


async def make_user(username: str, *, admin: bool = False) -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin) "
        "VALUES (?, ?, ?, ?)",
        (username, PASSWORD_HASH, username.capitalize(), 1 if admin else 0))
    return cur.lastrowid


async def make_bot(name: str = "broker", api_key: str = BOT_KEY) -> int:
    cur = await db.execute("INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
                           (name, auth.hash_api_key(api_key)))
    return cur.lastrowid


async def login(client, username: str) -> None:
    r = await client.post("/auth/login",
                          json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text


def key(api_key: str = BOT_KEY) -> dict:
    return {"X-Api-Key": api_key}


@pytest.fixture
async def plink(app, house_origin):
    """plink's own signed-in session, on a client of its own: a cookie on the
    shared client would turn every later bot call into a plink call."""
    await make_user("plink", admin=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=house_origin,
                                 headers={"Origin": house_origin}) as c:
        await login(c, "plink")
        yield c


async def answer(client, plink, proposal_id: int, principal: str, action: str):
    """plink answers from his session; a resident answers through the relay."""
    if principal == "plink":
        return await act(plink, proposal_id, "plink", action, headers={})
    return await act(client, proposal_id, principal, action)


async def file_proposal(client, slug: str = "2026-08-26-approval-object",
                        **over) -> dict:
    body = {"slug": slug, "title": "Approval object",
            "text": "One record per proposal.", **over}
    r = await client.post("/approval/proposals", json=body, headers=key())
    assert r.status_code == 200, r.text
    return r.json()["proposal"]


async def act(client, proposal_id: int, principal: str, action: str,
              remarks=None, headers=None):
    body = {"principal": principal, "action": action}
    if remarks is not None:
        body["remarks"] = remarks
    return await client.post(f"/approval/proposals/{proposal_id}/act",
                             json=body,
                             headers=key() if headers is None else headers)


# ── it ships off ────────────────────────────────────────────────────────────

async def test_every_endpoint_refuses_while_the_surface_is_disarmed(
        client, app, disarmed):
    """Reads included. Off and empty must not read alike."""
    await make_bot()
    calls = [
        ("GET", "/approval/proposals", None),
        ("GET", "/approval/proposals/1", None),
        ("POST", "/approval/proposals",
         {"slug": "x", "title": "t", "text": "b"}),
        ("POST", "/approval/proposals/1/act",
         {"principal": "plink", "action": "approve"}),
    ]
    for method, path, body in calls:
        r = await client.request(method, path, json=body, headers=key())
        assert r.status_code == 503, f"{method} {path}: {r.text}"


async def test_the_refusal_names_the_setting_and_how_to_arm_it(
        client, app, disarmed):
    """The broker hands this sentence to a resident verbatim, so it has to be
    actionable on its own."""
    await make_bot()
    detail = (await client.get("/approval/proposals",
                               headers=key())).json()["detail"]
    assert "APPROVAL_ENABLED" in detail
    assert "false" in detail


async def test_an_armed_surface_with_nothing_filed_is_an_empty_list(
        client, app, armed):
    await make_bot()
    r = await client.get("/approval/proposals", headers=key())
    assert r.status_code == 200
    assert r.json() == {"proposals": [], "count": 0, "truncated": False}


# ── filing ──────────────────────────────────────────────────────────────────

async def test_filing_creates_one_pending_row_per_configured_principal(
        client, app, armed):
    """An unanswered principal is a pending row, never an absent one."""
    await make_bot()
    proposal = await file_proposal(client)
    assert [s["principal"] for s in proposal["states"]] == PRINCIPALS
    assert [s["state"] for s in proposal["states"]] == ["pending"] * 3
    assert all(s["acted_at"] is None and s["acted_by"] is None
               for s in proposal["states"])
    assert proposal["decision"] == "pending"
    assert proposal["closed_at"] is None


async def test_a_bot_filing_for_a_resident_keeps_both_identities(
        client, app, armed):
    bot_id = await make_bot()
    proposal = await file_proposal(client, author="Claudette")
    assert proposal["created_by"] == {"type": "bot", "id": bot_id,
                                      "label": "Claudette (via broker)"}


async def test_a_human_filing_is_attributed_to_the_human_not_the_author_field(
        client, app, armed):
    """`author` is a bot's attestation of who asked. A signed-in person is
    themselves, and a supplied name never replaces that."""
    user_id = await make_user("plink", admin=True)
    await login(client, "plink")
    r = await client.post("/approval/proposals",
                          json={"slug": "handled", "title": "t", "text": "b",
                                "author": "somebody-else"})
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["created_by"] == {"type": "user", "id": user_id,
                                                  "label": "Plink"}


async def test_a_non_admin_human_cannot_file(client, app, armed):
    await make_user("bystander")
    await login(client, "bystander")
    r = await client.post("/approval/proposals",
                          json={"slug": "nope", "title": "t", "text": "b"})
    assert r.status_code == 403


async def test_a_non_admin_human_may_still_read(client, app, armed):
    await make_bot()
    await file_proposal(client)
    await make_user("bystander")
    await login(client, "bystander")
    r = await client.get("/approval/proposals")
    assert r.status_code == 200
    assert r.json()["count"] == 1


async def test_a_duplicate_slug_is_refused_and_names_the_existing_id(
        client, app, armed):
    await make_bot()
    first = await file_proposal(client)
    r = await client.post("/approval/proposals",
                          json={"slug": first["slug"], "title": "again",
                                "text": "again"}, headers=key())
    assert r.status_code == 409
    assert str(first["id"]) in r.json()["detail"]


async def test_an_unknown_proposal_id_is_a_404(client, app, armed):
    await make_bot()
    assert (await client.get("/approval/proposals/999",
                             headers=key())).status_code == 404


# ── answering ───────────────────────────────────────────────────────────────

async def test_a_resident_answers_through_the_broker_with_typed_attribution(
        client, app, armed):
    """The broker stamps the resident principal from SO_PEERCRED; the server
    takes a named principal only from the configured relay."""
    bot_id = await make_bot()
    proposal = await file_proposal(client)
    r = await act(client, proposal["id"], "res-claudette", "approve",
                  remarks="Reads clean.")
    assert r.status_code == 200, r.text
    state = next(s for s in r.json()["proposal"]["states"]
                 if s["principal"] == "res-claudette")
    assert state["state"] == "approve"
    assert state["remarks"] == "Reads clean."
    assert state["acted_by"] == {"type": "bot", "id": bot_id,
                                 "label": "res-claudette (via broker)"}
    assert state["acted_at"]


async def test_a_human_cannot_answer_as_another_principal(client, app, armed):
    """The client modal is plink's seat, not a way to answer for a resident."""
    await make_bot()
    proposal = await file_proposal(client)
    await make_user("plink", admin=True)
    await login(client, "plink")
    r = await act(client, proposal["id"], "res-gable", "approve", headers={})
    assert r.status_code == 403
    assert "res-gable" in r.json()["detail"]
    row = await db.fetch_one(
        "SELECT state FROM approval_state WHERE proposal_id = ? AND "
        "principal = 'res-gable'", (proposal["id"],))
    assert row["state"] == "pending"


async def test_a_human_can_answer_as_themselves(client, app, armed):
    await make_bot()
    proposal = await file_proposal(client)
    user_id = await make_user("plink", admin=True)
    await login(client, "plink")
    r = await act(client, proposal["id"], "plink", "approve",
                  remarks="Ship it.", headers={})
    assert r.status_code == 200, r.text
    state = next(s for s in r.json()["proposal"]["states"]
                 if s["principal"] == "plink")
    assert state["state"] == "approve"
    assert state["acted_by"] == {"type": "user", "id": user_id,
                                 "label": "Plink"}


async def pending(proposal_id: int, principal: str) -> bool:
    row = await db.fetch_one(
        "SELECT state FROM approval_state WHERE proposal_id = ? AND "
        "principal = ?", (proposal_id, principal))
    return row["state"] == "pending"


async def test_a_bot_off_the_relay_list_cannot_answer(client, app, armed):
    """Any other bot key (a resident adapter's, the keyboard's) is refused,
    whatever principal it names."""
    await make_bot()
    await make_bot("Gable", "gable-own-key")
    proposal = await file_proposal(client)
    for principal in PRINCIPALS:
        r = await act(client, proposal["id"], principal, "approve",
                      headers=key("gable-own-key"))
        assert r.status_code == 403, (principal, r.text)
        assert "APPROVAL_RELAY_BOT_NAMES" in r.json()["detail"]
        assert await pending(proposal["id"], principal)


async def test_the_relay_cannot_answer_as_a_human_principal(client, app, armed):
    """A relayed answer is always a resident's; plink answers from his own
    session or not at all."""
    await make_bot()
    proposal = await file_proposal(client)
    r = await act(client, proposal["id"], "plink", "approve")
    assert r.status_code == 403
    assert "plink" in r.json()["detail"]
    assert await pending(proposal["id"], "plink")


async def test_the_relay_list_is_config(client, app, armed, monkeypatch):
    monkeypatch.setenv("APPROVAL_RELAY_BOT_NAMES", '["relay"]')
    reset_settings_cache()
    await make_bot()                      # "broker", now off the list
    await make_bot("relay", "relay-key")
    proposal = await file_proposal(client)
    r = await act(client, proposal["id"], "res-gable", "approve")
    assert r.status_code == 403
    r = await act(client, proposal["id"], "res-gable", "approve",
                  headers=key("relay-key"))
    assert r.status_code == 200, r.text


async def test_a_humans_principal_is_derived_not_named(client, app, armed, plink):
    """A signed-in human is their own principal; naming one is optional, and
    naming anyone else is refused."""
    await make_bot()
    proposal = await file_proposal(client)
    r = await plink.post(f"/approval/proposals/{proposal['id']}/act",
                         json={"action": "rework", "remarks": "Tighten it."})
    assert r.status_code == 200, r.text
    state = next(s for s in r.json()["proposal"]["states"]
                 if s["principal"] == "plink")
    assert state["state"] == "rework"


async def test_an_admin_who_is_not_a_principal_cannot_answer(client, app, armed):
    await make_bot()
    proposal = await file_proposal(client)
    await make_user("deputy", admin=True)
    await login(client, "deputy")
    r = await act(client, proposal["id"], "deputy", "approve", headers={})
    assert r.status_code == 403


async def test_a_person_account_named_like_a_resident_cannot_answer(
        client, app, armed):
    await make_bot()
    proposal = await file_proposal(client)
    await make_user("res-gable", admin=True)
    await login(client, "res-gable")
    r = await act(client, proposal["id"], "res-gable", "approve", headers={})
    assert r.status_code == 403


async def test_the_relay_must_name_the_resident(client, app, armed):
    await make_bot()
    proposal = await file_proposal(client)
    r = await client.post(f"/approval/proposals/{proposal['id']}/act",
                          json={"action": "approve"}, headers=key())
    assert r.status_code == 400


async def test_an_unknown_principal_is_refused_and_names_the_valid_set(
        client, app, armed):
    await make_bot()
    proposal = await file_proposal(client)
    r = await act(client, proposal["id"], "res-nobody", "approve")
    assert r.status_code == 400
    for name in PRINCIPALS:
        assert name in r.json()["detail"]


async def test_re_acting_replaces_that_principals_row_rather_than_appending(
        client, app, armed):
    """A principal may change its mind while the proposal is open."""
    await make_bot()
    proposal = await file_proposal(client)
    await act(client, proposal["id"], "res-gable", "rework", remarks="First.")
    first = await db.fetch_one(
        "SELECT acted_at FROM approval_state WHERE proposal_id = ? AND "
        "principal = 'res-gable'", (proposal["id"],))
    r = await act(client, proposal["id"], "res-gable", "approve",
                  remarks="Second.")
    assert r.status_code == 200, r.text
    rows = await db.fetch_all(
        "SELECT state, remarks, acted_at FROM approval_state WHERE "
        "proposal_id = ? AND principal = 'res-gable'", (proposal["id"],))
    assert len(rows) == 1
    assert rows[0]["state"] == "approve"
    assert rows[0]["remarks"] == "Second."
    assert rows[0]["acted_at"] >= first["acted_at"]
    assert len(r.json()["proposal"]["states"]) == 3


# ── the derived decision ────────────────────────────────────────────────────

async def test_every_principal_approving_closes_the_proposal_as_approved(
        client, app, armed, plink):
    await make_bot()
    proposal = await file_proposal(client)
    for principal in PRINCIPALS:
        r = await answer(client, plink, proposal["id"], principal, "approve")
        assert r.status_code == 200, r.text
    body = r.json()["proposal"]
    assert body["decision"] == "approved"
    assert body["closed_at"]


async def test_a_single_deny_closes_the_proposal_as_denied(
        client, app, armed, plink):
    """Denial outranks everything, and it does not wait for the others."""
    await make_bot()
    proposal = await file_proposal(client)
    await answer(client, plink, proposal["id"], "plink", "approve")
    body = (await act(client, proposal["id"], "res-gable",
                      "deny")).json()["proposal"]
    assert body["decision"] == "denied"
    assert body["closed_at"]


async def test_a_rework_does_not_close_the_proposal(client, app, armed):
    """Rework is a distinct answer, not a soft denial."""
    await make_bot()
    proposal = await file_proposal(client)
    body = (await act(client, proposal["id"], "res-claudette",
                      "rework", remarks="Split slice B out.")).json()["proposal"]
    assert body["decision"] == "rework"
    assert body["closed_at"] is None


async def test_deny_outranks_rework(client, app, armed, plink):
    await make_bot()
    proposal = await file_proposal(client)
    await act(client, proposal["id"], "res-claudette", "rework")
    body = (await answer(client, plink, proposal["id"], "plink",
                         "deny")).json()["proposal"]
    assert body["decision"] == "denied"


async def test_a_proposal_closed_while_an_answer_was_in_flight_stays_closed(
        client, app, armed, plink, monkeypatch):
    """An answer that read the proposal open before another answer closed it
    must not reopen or rewrite the decision."""
    await make_bot()
    proposal = await file_proposal(client)
    stale = await approval._require_proposal(proposal["id"])
    await answer(client, plink, proposal["id"], "plink", "deny")

    async def read_before_the_close(_proposal_id):
        return stale
    monkeypatch.setattr(approval, "_require_proposal", read_before_the_close)
    r = await act(client, proposal["id"], "res-gable", "approve")
    assert r.status_code == 409
    row = await db.fetch_one(
        "SELECT state FROM approval_state WHERE proposal_id = ? AND principal = ?",
        (proposal["id"], "res-gable"))
    assert row is None or row["state"] != "approve"


async def test_a_relayed_answer_is_labelled_with_its_resident(client, app, armed):
    await make_bot()
    proposal = await file_proposal(client)
    await act(client, proposal["id"], "res-gable", "rework")
    row = await db.fetch_one(
        "SELECT acted_by_label FROM approval_state "
        "WHERE proposal_id = ? AND principal = ?", (proposal["id"], "res-gable"))
    assert row["acted_by_label"] == "res-gable (via broker)"


async def test_answering_a_closed_proposal_is_refused(client, app, armed, plink):
    await make_bot()
    proposal = await file_proposal(client)
    await answer(client, plink, proposal["id"], "plink", "deny")
    r = await act(client, proposal["id"], "res-gable", "approve")
    assert r.status_code == 409


async def test_the_decision_is_derived_not_stored(client, app, armed):
    """No column anywhere holds it, so nothing can set it out of step."""
    await make_bot()
    await file_proposal(client)
    cols = await db.fetch_all("PRAGMA table_info(approval_proposal)")
    assert "decision" not in {c["name"] for c in cols}


# ── listing ─────────────────────────────────────────────────────────────────

async def test_the_list_is_most_recent_first_and_filters_on_open_or_closed(
        client, app, armed, plink):
    await make_bot()
    first = await file_proposal(client, slug="first")
    second = await file_proposal(client, slug="second")
    for principal in PRINCIPALS:
        await answer(client, plink, first["id"], principal, "approve")

    body = (await client.get("/approval/proposals", headers=key())).json()
    assert [p["slug"] for p in body["proposals"]] == ["second", "first"]
    assert body["count"] == 2 and body["truncated"] is False

    open_slugs = (await client.get("/approval/proposals?state=open",
                                   headers=key())).json()["proposals"]
    assert [p["slug"] for p in open_slugs] == ["second"]
    closed = (await client.get("/approval/proposals?state=closed",
                               headers=key())).json()["proposals"]
    assert [p["slug"] for p in closed] == ["first"]


async def test_the_list_declares_when_it_truncates(client, app, armed):
    await make_bot()
    for n in range(3):
        await file_proposal(client, slug=f"p-{n}")
    body = (await client.get("/approval/proposals?limit=2",
                             headers=key())).json()
    assert body["count"] == 2
    assert body["truncated"] is True
