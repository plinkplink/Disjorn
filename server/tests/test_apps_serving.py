"""APPS stage 3 — the serving gate's house side: grants, live, share, remix.

SPECS/2026-09-09-apps-serving-gate.md D6–D9. Four claims carry this stage, and
they are what these tests are about rather than the endpoint list:

1. **The grant is the entitlement, not the URL.** Every mint is behind the same
   visibility question the rest of the registry asks, the preview root is
   minted for the owner alone, and the token says which app it opens — so the
   tests read the payload back rather than trusting the URL shape.
2. **The house never publishes anything itself.** `live`, `revert` and `remix`
   are three fixed-argv spawns of one host helper; the tests record the argv
   and drive the three outcomes (0 / 64 / anything else) from the fake.
3. **`live` moves one set of facts.** The tree is published FIRST; only then do
   the app's status, the stage stream, the modal's bar and the room's
   transcript move, and they move on the same code path the broker's turns use.
4. **An unconfigured gate is a working house.** No boot assertion, no broken
   tab: the four verbs that need an origin answer 503 with a sentence, and
   building an app still works end to end.

Fixtures and helpers come from test_apps.py — the same users, the same builder
seat, the same `broker` publisher — because this is the same feature two stages
later and a second set of fixtures would be a second thing to keep true.
"""

import base64
import json
import subprocess

import pytest

from app import db
from app.routers import apps

from tests.test_apps import (  # noqa: F401 — fixtures are used by name
    build_fixture,
    login,
    make_user,
    post_stage,
    seat_toml,
    settings_env,
)

# 48 base64url characters — comfortably past the 32-byte floor, and shaped like
# what `secrets.token_urlsafe(48)` prints, which is what the .env.example says
# to generate.
GATE_SECRET = "x" * 48
ORIGIN = "https://house.example.ts.net:10000"
HELPER = ["/usr/local/lib/disjorn/fake-apps-launch"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gate_env(settings_env, tmp_path):
    """Configure the gate. Returns the www root so a test can plant a tree."""
    www = tmp_path / "apps-www"
    www.mkdir()

    def apply(**extra):
        settings_env(
            APPS_GATE_SECRET=GATE_SECRET,
            APPS_ORIGIN_BASE=ORIGIN,
            APPS_WWW_ROOT=str(www),
            APPS_LAUNCH_HELPER=HELPER,
            **extra,
        )

    apply.www = www  # type: ignore[attr-defined]
    return apply


class FakeHelper:
    """The host helper, faked at the one function that spawns it.

    Records argv (the point of the seam: what the house would have run as root
    is a test assertion, not a comment) and returns whatever exit code the test
    wants.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.returncode = 0
        self.stderr = ""

    def __call__(self, argv):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)

    @property
    def argv(self) -> list[str]:
        assert len(self.calls) == 1, self.calls
        return self.calls[0]


@pytest.fixture
def helper(monkeypatch):
    fake = FakeHelper()
    monkeypatch.setattr(apps, "_run_launch_helper", fake)
    return fake


async def make_live(app_id: str) -> None:
    """Mark an app live without going through the publish path.

    Used by the tests that are about something else (opening, sharing,
    remixing); `live` itself is tested through its endpoint.
    """
    await db.execute("UPDATE apps SET status = 'live' WHERE id = ?", (app_id,))


async def share_with(app_id: str, user_id: int) -> None:
    await db.execute(
        "INSERT INTO app_shares (app_id, user_id) VALUES (?, ?)", (app_id, user_id)
    )


def payload_of(url: str) -> dict:
    """The grant's payload, decoded. A URL is not evidence; the token is."""
    token = url.split("?t=", 1)[1]
    body = token.split(".", 1)[0]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


async def open_app(client, app_id: str, root: str | None = None):
    body = {} if root is None else {"root": root}
    return await client.post(f"/apps/{app_id}/open", json=body)


async def a_session(client, settings_env, seat_toml):
    """An app with an open build session, owned by alice, `broker` publishing."""
    return await build_fixture(client, settings_env, seat_toml)


# ---------------------------------------------------------------------------
# 1. Open — the grant is the entitlement
# ---------------------------------------------------------------------------

async def test_open_mints_for_the_entitled_and_for_nobody_else(
    client, app, gate_env, settings_env, seat_toml
):
    """Claim 1. Owner, shared-with and (once public) anyone; a stranger gets
    the refusal rather than a URL, and only the owner is granted `preview`."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    app_id = session["app"]["id"]
    bob = await make_user("bob", "Bob")
    await make_user("carol", "Carol")

    # Not live yet: entitled, but there is nothing published to point at.
    not_yet = await open_app(client, app_id)
    assert not_yet.status_code == 409
    assert "not live" in not_yet.json()["detail"]

    # The owner's preview needs no publish — it is the tree being built.
    preview = await open_app(client, app_id, "preview")
    assert preview.status_code == 200
    url = preview.json()["url"]
    assert url.startswith(f"{ORIGIN}/{app_id}/preview/?t=")
    body = payload_of(url)
    assert body["app"] == app_id and body["roots"] == ["live", "preview"]
    assert body["v"] == 1 and body["exp"] > 0
    assert body["ctx"]["version"] == 1
    assert body["ctx"]["user"]["name"] == "Alice"
    # The opaque id, never the account id.
    assert body["ctx"]["user"]["id"] != str(session["app"]["owner_user_id"])
    assert body["ctx"]["user"]["id"] == apps.apps_grant.opaque_user_id(
        GATE_SECRET.encode(), session["app"]["owner_user_id"], app_id
    )

    await make_live(app_id)
    await share_with(app_id, bob)

    owner_live = await open_app(client, app_id)
    assert owner_live.status_code == 200
    assert owner_live.json()["url"].startswith(f"{ORIGIN}/{app_id}/?t=")

    # Shared with: one root, and the blob names who else is in the ring.
    await login(client, "bob")
    shared = await open_app(client, app_id)
    assert shared.status_code == 200
    shared_body = payload_of(shared.json()["url"])
    assert shared_body["roots"] == ["live"]
    assert shared_body["ctx"]["shared_with"] == ["Bob"]
    assert (await open_app(client, app_id, "preview")).status_code == 403

    # A stranger: no URL, no preview, whatever the app's status.
    await login(client, "carol")
    assert (await open_app(client, app_id)).status_code == 403
    assert (await open_app(client, app_id, "preview")).status_code == 403

    # Public means public, and still only the live root.
    await db.execute("UPDATE apps SET visibility = 'public' WHERE id = ?", (app_id,))
    public = await open_app(client, app_id)
    assert public.status_code == 200
    assert payload_of(public.json()["url"])["roots"] == ["live"]
    assert (await open_app(client, app_id, "preview")).status_code == 403


async def test_the_verbs_that_need_an_origin_say_so_when_there_is_none(
    client, app, settings_env, seat_toml
):
    """Claim 4. The tab still builds; the four serving verbs answer 503 with a
    sentence. A too-short secret is the same answer as no secret — 'we have a
    secret' has to be a true sentence before anything is signed with it."""
    session = await a_session(client, settings_env, seat_toml)
    app_id = session["app"]["id"]
    await make_live(app_id)

    for values in (
        {},
        {"APPS_GATE_SECRET": "short", "APPS_ORIGIN_BASE": ORIGIN},
        {"APPS_GATE_SECRET": GATE_SECRET, "APPS_ORIGIN_BASE": ""},
    ):
        settings_env(**values)
        for r in (
            await open_app(client, app_id),
            await client.post(f"/apps/sessions/{session['id']}/live"),
            await client.post(f"/apps/{app_id}/share", json={"channel_id": 1}),
            await client.post(f"/apps/{app_id}/remix"),
        ):
            assert r.status_code == 503, r.text
            assert "not configured" in r.json()["detail"]

    # …and the tab is untouched: the registry still answers.
    assert (await client.get("/apps")).status_code == 200
    config = await client.get("/apps/config")
    assert config.json() == {"origin_base": "", "configured": False}


async def test_config_reports_the_origin_once_it_is_set(
    client, app, gate_env, settings_env, seat_toml
):
    await a_session(client, settings_env, seat_toml)
    gate_env()
    r = await client.get("/apps/config")
    assert r.json() == {"origin_base": ORIGIN, "configured": True}


# ---------------------------------------------------------------------------
# 2. Live — publish first, then move every fact at once
# ---------------------------------------------------------------------------

async def deployed_turn(client, session_id: int, files: list[str]) -> None:
    """One complete turn, the way the broker posts it."""
    await post_stage(client, session_id, "scoped", {"turn": 1})
    await post_stage(
        client, session_id, "files_written", {"turn": 1, "files": files, "tokens": 1200}
    )
    await post_stage(client, session_id, "deployed", {"turn": 1})
    await login(client, "alice")


async def test_live_publishes_the_tree_then_moves_status_bar_and_room(
    client, app, gate_env, settings_env, seat_toml, helper
):
    """Claim 3. One spawn with the fixed argv, then: status live, a `live`
    stage event on the ordinary stream, and one B9 line in the build room."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    sid, app_id = session["id"], session["app"]["id"]
    await deployed_turn(client, sid, ["index.html", "app.js"])

    r = await client.post(f"/apps/sessions/{sid}/live")
    assert r.status_code == 200, r.text
    assert helper.argv == [*HELPER, "publish", app_id]

    body = r.json()
    assert body["app"]["status"] == "live"
    assert body["stage"] == "live"
    assert [s["stage"] for s in body["stages"]][-1] == "live"
    assert body["stages"][-1]["detail"] == {"url": f"{ORIGIN}/{app_id}/"}

    row = await db.fetch_one("SELECT * FROM apps WHERE id = ?", (app_id,))
    assert row["status"] == "live"

    lines = [
        r["content"]
        for r in await db.fetch_all(
            "SELECT content FROM messages WHERE channel_id = ? ORDER BY seq",
            (session["channel_id"],),
        )
    ]
    assert lines[-1] == (
        f"**Untitled app** is live at {ORIGIN}/{app_id}/ — 1 turn, "
        "files: index.html, app.js"
    )


async def test_live_needs_an_idle_builder_a_deployed_turn_and_the_owner(
    client, app, gate_env, settings_env, seat_toml, helper
):
    """Nothing built yet, a turn in flight, and somebody else's session: three
    refusals, and none of them spawns anything."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    sid, app_id = session["id"], session["app"]["id"]

    nothing = await client.post(f"/apps/sessions/{sid}/live")
    assert nothing.status_code == 409
    assert "Nothing has been built" in nothing.json()["detail"]

    await deployed_turn(client, sid, ["index.html"])
    await post_stage(client, sid, "scoped", {"turn": 2})
    await login(client, "alice")
    running = await client.post(f"/apps/sessions/{sid}/live")
    assert running.status_code == 409
    assert "still working" in running.json()["detail"]

    await make_user("bob", "Bob")
    await login(client, "bob")
    assert (await client.post(f"/apps/sessions/{sid}/live")).status_code == 403

    assert helper.calls == []
    row = await db.fetch_one("SELECT status FROM apps WHERE id = ?", (app_id,))
    assert row["status"] == "draft"


async def test_a_refused_publish_is_the_helpers_sentence_and_a_failure_is_not(
    client, app, gate_env, settings_env, seat_toml, helper
):
    """64 is the user's business — it carries one sentence they can act on, and
    nothing about the app changed. Any other code is the house's, and its
    details stay in the log."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    sid, app_id = session["id"], session["app"]["id"]
    await deployed_turn(client, sid, ["index.html"])

    helper.returncode = 64
    helper.stderr = "There is nothing in this app's preview to publish.\n"
    refused = await client.post(f"/apps/sessions/{sid}/live")
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "There is nothing in this app's preview to publish."
    )

    helper.returncode = 3
    helper.stderr = "rsync: some path an admin should see\n"
    failed = await client.post(f"/apps/sessions/{sid}/live")
    assert failed.status_code == 502
    assert "rsync" not in failed.json()["detail"]

    row = await db.fetch_one("SELECT status FROM apps WHERE id = ?", (app_id,))
    assert row["status"] == "draft"
    events = await db.fetch_all(
        "SELECT stage FROM app_stage_events WHERE session_id = ? AND stage = 'live'",
        (sid,),
    )
    assert events == []


# ---------------------------------------------------------------------------
# 3. Revert
# ---------------------------------------------------------------------------

async def test_revert_swaps_the_trees_for_the_owner_of_a_live_app(
    client, app, gate_env, settings_env, seat_toml, helper
):
    """The house writes nothing: which tree is served is the filesystem's fact.
    A draft app has nothing to undo; a stranger is not asked."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    app_id = session["app"]["id"]

    draft = await client.post(f"/apps/{app_id}/revert")
    assert draft.status_code == 409 and helper.calls == []

    await make_live(app_id)
    r = await client.post(f"/apps/{app_id}/revert")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "status": "live"}
    assert helper.argv == [*HELPER, "revert", app_id]

    await make_user("bob", "Bob")
    await login(client, "bob")
    assert (await client.post(f"/apps/{app_id}/revert")).status_code == 403


# ---------------------------------------------------------------------------
# 4. Share — the card is a message, and the entitlement is a snapshot
# ---------------------------------------------------------------------------

async def make_channel(client, name: str, *, private: bool = True) -> int:
    r = await client.post(
        "/channels",
        json={"name": name, "visibility": "private" if private else "public"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_share_entitles_the_rooms_members_and_posts_the_card(
    client, app, gate_env, settings_env, seat_toml
):
    """Claim: `app_shares` is a snapshot of who was in the room at share time,
    and the card is an ordinary message by the sharing user."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    app_id = session["app"]["id"]
    await make_live(app_id)
    bob = await make_user("bob", "Bob")
    carol = await make_user("carol", "Carol")

    channel_id = await make_channel(client, "shed")
    await client.post(f"/channels/{channel_id}/invite", json={"user_id": bob})

    r = await client.post(f"/apps/{app_id}/share", json={"channel_id": channel_id})
    assert r.status_code == 200, r.text
    message_id = r.json()["message_id"]

    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (message_id,))
    assert row["channel_id"] == channel_id
    assert row["author_type"] == "user"
    assert row["content"] == (
        f"shared an app: **Untitled app** — {ORIGIN}/{app_id}/"
    )

    shared_ids = {
        s["user_id"]
        for s in await db.fetch_all(
            "SELECT user_id FROM app_shares WHERE app_id = ?", (app_id,)
        )
    }
    assert bob in shared_ids and carol not in shared_ids

    visibility = await db.fetch_one("SELECT visibility FROM apps WHERE id = ?", (app_id,))
    assert visibility["visibility"] == "shared"

    # Bob can now open it; Carol, who was not in the room, still cannot.
    await login(client, "bob")
    assert (await open_app(client, app_id)).status_code == 200
    await login(client, "carol")
    assert (await open_app(client, app_id)).status_code == 403


async def test_share_refuses_a_room_the_sharer_is_not_in_and_any_build_chat(
    client, app, gate_env, settings_env, seat_toml
):
    """The membership wall is the channel's, not a new one — and a build chat
    is never a room to share into: its members are fixed and the resident reads
    it as a transcript."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    app_id = session["app"]["id"]
    await make_live(app_id)
    bob = await make_user("bob", "Bob")

    # Bob's private channel; Alice is not in it.
    await login(client, "bob")
    bobs_room = await make_channel(client, "bobs-shed")
    await login(client, "alice")
    outside = await client.post(f"/apps/{app_id}/share", json={"channel_id": bobs_room})
    assert outside.status_code == 403

    build_chat = await client.post(
        f"/apps/{app_id}/share", json={"channel_id": session["channel_id"]}
    )
    assert build_chat.status_code == 403
    assert "build chat" in build_chat.json()["detail"]

    missing = await client.post(f"/apps/{app_id}/share", json={"channel_id": 9999})
    assert missing.status_code == 404

    # Nobody was entitled by a refused share — not even the owner of the room
    # Alice could not reach.
    assert (
        await db.fetch_all("SELECT 1 FROM app_shares WHERE app_id = ?", (app_id,))
    ) == []
    assert bob is not None


async def test_sharing_public_entitles_the_house_and_a_draft_cannot_be_shared(
    client, app, gate_env, settings_env, seat_toml
):
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    app_id = session["app"]["id"]
    channel_id = await make_channel(client, "shed")

    draft = await client.post(f"/apps/{app_id}/share", json={"channel_id": channel_id})
    assert draft.status_code == 409 and "live" in draft.json()["detail"]

    await make_live(app_id)
    r = await client.post(
        f"/apps/{app_id}/share",
        json={"channel_id": channel_id, "visibility": "public"},
    )
    assert r.status_code == 200 and r.json()["visibility"] == "public"

    await make_user("dave", "Dave")
    await login(client, "dave")
    assert (await open_app(client, app_id)).status_code == 200


# ---------------------------------------------------------------------------
# 5. Remix — a copy I own, with a session open on it
# ---------------------------------------------------------------------------

async def test_remix_clones_records_lineage_and_opens_the_chooser_s_session(
    client, app, gate_env, settings_env, seat_toml, helper
):
    """The response IS the chooser's, because a remix is a build session on a
    copy — same shape, same meter, same modal."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    parent_id = session["app"]["id"]
    await make_live(parent_id)
    await client.patch(f"/apps/{parent_id}", json={"name": "Tide clock"})
    bob = await make_user("bob", "Bob")
    await share_with(parent_id, bob)

    await login(client, "bob")
    r = await client.post(f"/apps/{parent_id}/remix")
    assert r.status_code == 200, r.text
    body = r.json()
    child_id = body["app"]["id"]

    assert helper.argv == [*HELPER, "remix", parent_id, child_id]
    assert body["app"]["parent_app_id"] == parent_id
    assert body["app"]["owner_user_id"] == bob
    assert body["app"]["name"] == "Tide clock (remix)"
    assert body["app"]["visibility"] == "private"
    assert body["app"]["status"] == "draft"
    assert body["app"]["on_menu"] is True
    assert body["channel_id"] and body["quota"]["used"] == 1

    child = await db.fetch_one("SELECT * FROM apps WHERE id = ?", (child_id,))
    assert child["repo_path"] == f"/srv/apps/{child_id}"
    assert child["builder_bot_id"] == session["builder"]["bot_id"]
    # Owning it IS the menu; a user_apps row would be the state
    # remove_from_menu's refusal says cannot exist.
    assert (
        await db.fetch_all("SELECT 1 FROM user_apps WHERE app_id = ?", (child_id,))
    ) == []


async def test_a_failed_clone_leaves_no_app_row_behind(
    client, app, gate_env, settings_env, seat_toml, helper
):
    """The row is written first so the clone has an id to write to; if the
    clone does not happen the row goes with it, or the chooser grows an app
    whose session opens on nothing."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    parent_id = session["app"]["id"]
    await make_live(parent_id)
    before = {r["id"] for r in await db.fetch_all("SELECT id FROM apps")}

    helper.returncode = 64
    helper.stderr = "That app is already being remixed.\n"
    refused = await client.post(f"/apps/{parent_id}/remix")
    assert refused.status_code == 409
    assert refused.json()["detail"] == "That app is already being remixed."

    helper.returncode = 9
    assert (await client.post(f"/apps/{parent_id}/remix")).status_code == 502

    after = {r["id"] for r in await db.fetch_all("SELECT id FROM apps")}
    assert after == before
    # …and no session was opened on an app that does not exist.
    assert len(await db.fetch_all("SELECT 1 FROM app_sessions")) == 1


async def test_remix_is_walled_by_entitlement_and_by_the_apps_status(
    client, app, gate_env, settings_env, seat_toml, helper
):
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    parent_id = session["app"]["id"]

    draft = await client.post(f"/apps/{parent_id}/remix")
    assert draft.status_code == 409 and "live" in draft.json()["detail"]

    await make_live(parent_id)
    await make_user("carol", "Carol")
    await login(client, "carol")
    assert (await client.post(f"/apps/{parent_id}/remix")).status_code == 403
    assert helper.calls == []


async def test_remix_spends_the_same_daily_meter_as_the_chooser(
    client, app, gate_env, settings_env, seat_toml, helper
):
    """And it is checked BEFORE the clone, so a refused remix leaves nothing on
    disk and nothing in the registry."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env(APPS_DAILY_SESSION_CAP=1)
    parent_id = session["app"]["id"]
    await make_live(parent_id)

    r = await client.post(f"/apps/{parent_id}/remix")
    assert r.status_code == 429
    assert helper.calls == []
    assert len(await db.fetch_all("SELECT 1 FROM apps")) == 1


# ---------------------------------------------------------------------------
# 6. The card
# ---------------------------------------------------------------------------

async def test_the_card_answers_the_entitled_and_denies_the_existence_to_others(
    client, app, gate_env, settings_env, seat_toml
):
    """404, not 403, for a viewer with no entitlement: the card is fetched from
    a message body by id, so 'forbidden' would confirm an app exists on an
    origin every app shares."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    app_id = session["app"]["id"]
    await make_user("carol", "Carol")

    draft = await client.get(f"/apps/{app_id}/card")
    assert draft.status_code == 200, draft.text
    card = draft.json()
    assert card["id"] == app_id and card["status"] == "draft"
    assert card["builder"]["bot_id"] == session["builder"]["bot_id"]
    assert card["owner"]["name"] == "Alice"
    assert card["can_remix"] is False and card["live_url"] is None
    assert card["image_url"] is None and card["has_previous_live"] is False

    await make_live(app_id)
    live = (await client.get(f"/apps/{app_id}/card")).json()
    assert live["can_remix"] is True
    assert live["live_url"] == f"{ORIGIN}/{app_id}/"

    await login(client, "carol")
    assert (await client.get(f"/apps/{app_id}/card")).status_code == 404
    assert (await client.get("/apps/zzzzzzzzzzzz/card")).status_code == 404


async def test_the_card_reads_has_previous_live_off_the_tree(
    client, app, gate_env, settings_env, seat_toml
):
    """Revert's button appears because a previous deploy EXISTS, which only the
    filesystem knows — the house records `live`, the helper rotates trees."""
    session = await a_session(client, settings_env, seat_toml)
    gate_env()
    app_id = session["app"]["id"]
    await make_live(app_id)
    assert (await client.get(f"/apps/{app_id}/card")).json()["has_previous_live"] is False

    (gate_env.www / app_id / "live.prev").mkdir(parents=True)
    assert (await client.get(f"/apps/{app_id}/card")).json()["has_previous_live"] is True
