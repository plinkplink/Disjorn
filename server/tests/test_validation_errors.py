"""App-wide 422 shape (app/main.py: validation_error_body).

Regression for two coupled defects in FastAPI's default
RequestValidationError body, `{"detail": [ {loc, msg, type, input, url}, ...]}`:

  1. `detail` was a LIST. Every consumer in this repo (client api.ts, the SDK,
     curl) reads `detail` as a string because every HTTPException produces
     one, so a field-validation failure degraded to the bare status text
     "Unprocessable Entity" — pasting an over-length message told the user
     nothing about what was wrong.
  2. Each entry echoed `input`, i.e. the submitted value. On
     POST /channels/{id}/messages that is the user's message text, so the 422
     body (and any log holding it) quoted protected content back. House rule:
     refusals never echo protected content.

The echo checks below are the load-bearing ones: they post distinctive
sentinel strings and assert no fragment survives into the response.
"""

import pytest

from app import db
from app.main import validation_error_body
from app.routers import auth
from app.routers.messages import MAX_MESSAGE_CHARS

PASSWORD = "correct horse battery staple"

# Deliberately "sensitive-looking" and highly distinctive: if any of these
# words reaches a 422 body, the handler is echoing the submitted value.
SENTINEL = "codeword-tarragon-hunter2-safebehindthepainting"


async def make_user(username: str) -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, auth.hash_password(PASSWORD), username.capitalize()),
    )
    return cur.lastrowid


async def login(client, username: str) -> None:
    r = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200


async def main_feed_id() -> int:
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    assert row is not None
    return row["id"]


@pytest.fixture
async def alice(client):
    await make_user("alice")
    await login(client, "alice")
    return client


# ---------------------------------------------------------------------------
# The headline case: an over-length message
# ---------------------------------------------------------------------------

async def test_over_length_message_gives_an_actionable_string_detail(alice):
    ch = await main_feed_id()
    body = SENTINEL * (MAX_MESSAGE_CHARS // len(SENTINEL) + 2)
    assert len(body) > MAX_MESSAGE_CHARS

    r = await alice.post(f"/channels/{ch}/messages", json={"content": body})
    assert r.status_code == 422
    payload = r.json()

    detail = payload["detail"]
    assert isinstance(detail, str), "detail must stay a string for api.ts / the SDK"
    assert "content" in detail                       # names the offending field
    assert str(MAX_MESSAGE_CHARS) in detail          # names the constraint
    assert detail == (
        "Invalid request: body.content must be at most "
        f"{MAX_MESSAGE_CHARS} characters"
    )
    assert await db.fetch_all("SELECT * FROM messages") == []  # still fails closed


async def test_422_never_echoes_the_submitted_value(alice):
    """The one that would have caught the leak: no fragment of the rejected
    message text may appear in the response, in any field."""
    ch = await main_feed_id()
    body = SENTINEL * (MAX_MESSAGE_CHARS // len(SENTINEL) + 2)

    r = await alice.post(f"/channels/{ch}/messages", json={"content": body})
    assert r.status_code == 422
    for fragment in ("codeword", "tarragon", "hunter2", "safebehindthepainting"):
        assert fragment not in r.text, f"422 body echoed {fragment!r}"
    # No internals either: no traceback, no pydantic docs link, no `input` key.
    assert "Traceback" not in r.text
    assert "errors.pydantic.dev" not in r.text
    assert all("input" not in item for item in r.json()["errors"])


async def test_422_does_not_echo_caller_chosen_dict_keys(alice):
    """Validation locations can include a key the caller chose (this API has
    `dict[str, str]` fields). Only server-declared path segments are echoed,
    so a key carrying content cannot ride out inside `loc` either."""
    r = await alice.post(
        "/push/subscribe",
        json={"endpoint": "https://push.example/x", "keys": {SENTINEL: 5}},
    )
    assert r.status_code == 422
    assert "tarragon" not in r.text and "hunter2" not in r.text
    assert r.json()["detail"] == (
        "Invalid request: body.keys.<key> must be a valid string"
    )


# ---------------------------------------------------------------------------
# Shape holds across every validation surface
# ---------------------------------------------------------------------------

async def test_detail_is_a_string_for_body_query_and_path_errors(alice):
    ch = await main_feed_id()
    cases = [
        await alice.post(f"/channels/{ch}/messages", json={}),                    # missing
        await alice.get("/backlog", params={"limit": 5000}),                      # query bound
        await alice.get("/channels/not-an-int/messages"),                         # path type
        await alice.patch("/me", json={"status": "invisible"}),                   # literal
        await alice.post(
            f"/channels/{ch}/messages",
            content=b"{not json",
            headers={"content-type": "application/json"},
        ),                                                                        # bad JSON
    ]
    for r in cases:
        assert r.status_code == 422, r.text
        assert isinstance(r.json()["detail"], str)
        assert r.json()["detail"].startswith("Invalid request:")
        assert r.json()["errors"], "machine-readable errors list must survive"


async def test_errors_list_is_machine_readable(alice):
    r = await alice.get("/backlog", params={"limit": 5000, "from_id": -1})
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert {e["field"] for e in errors} == {"query.limit", "query.from_id"}
    assert {e["type"] for e in errors} == {"less_than_equal", "greater_than_equal"}
    assert all(set(e) == {"field", "type", "message"} for e in errors)


async def test_http_exception_detail_shape_is_unchanged(alice):
    """The handler must not disturb the ordinary HTTPException contract that
    every other refusal in the codebase uses."""
    r = await alice.get("/channels/999999/members")
    assert r.status_code == 404
    assert r.json() == {"detail": "Channel not found"}


# ---------------------------------------------------------------------------
# Unit: the formatter itself
# ---------------------------------------------------------------------------

def test_formatter_covers_the_constraint_vocabulary():
    cases = [
        ({"type": "missing", "loc": ["body", "content"]}, "body.content is required"),
        (
            {"type": "string_too_long", "loc": ["body", "content"],
             "ctx": {"max_length": 16000}},
            "body.content must be at most 16000 characters",
        ),
        (
            {"type": "string_too_short", "loc": ["body", "name"],
             "ctx": {"min_length": 1}},
            "body.name must be at least 1 character",
        ),
        (
            {"type": "too_short", "loc": ["body", "attachment_ids"],
             "ctx": {"min_length": 1}},
            "body.attachment_ids must have at least 1 item",
        ),
        (
            {"type": "greater_than_equal", "loc": ["query", "from_id"], "ctx": {"ge": 0}},
            "query.from_id must be greater than or equal to 0",
        ),
        (
            {"type": "less_than_equal", "loc": ["query", "limit"], "ctx": {"le": 200}},
            "query.limit must be less than or equal to 200",
        ),
        ({"type": "int_parsing", "loc": ["path", "channel_id"]},
         "path.channel_id must be a valid integer"),
        ({"type": "bool_type", "loc": ["body", "flag"]}, "body.flag must be a valid boolean"),
        ({"type": "extra_forbidden", "loc": ["body", "nope"]},
         "body.nope is not a recognized field"),
        ({"type": "json_invalid", "loc": ["body", 17]}, "body is not valid JSON"),
        (
            {"type": "value_error", "loc": ["body"],
             "ctx": {"error": ValueError("privacy_flags exceeds 4000 serialized characters")}},
            "body privacy_flags exceeds 4000 serialized characters",
        ),
        # Unknown pydantic type: degrades to the machine slug, never to input.
        ({"type": "some_future_error", "loc": ["body", "x"]},
         "body.x failed validation (some_future_error)"),
    ]
    for error, expected in cases:
        assert validation_error_body([error])["detail"] == f"Invalid request: {expected}"


def test_formatter_sanitizes_field_paths_and_bounds_the_message():
    # A caller-chosen mapping key never reaches the response, whatever it says.
    for key in ("sneaky key: value/1", "plain_identifier", SENTINEL):
        body = validation_error_body([{"type": "string_type", "loc": ["body", "keys", key]}])
        assert body["errors"][0]["field"] == "body.keys.<key>"

    # Server-declared segments are still charset-restricted and truncated.
    long_key = "z" * 500
    body = validation_error_body([{"type": "missing", "loc": ["body", long_key]}])
    assert len(body["errors"][0]["field"]) < 60
    assert (
        validation_error_body([{"type": "missing", "loc": ["header", "x api key"]}])[
            "errors"
        ][0]["field"]
        == "header.x?api?key"
    )

    # List indices read naturally.
    body = validation_error_body(
        [{"type": "int_parsing", "loc": ["body", "attachment_ids", 2]}]
    )
    assert body["errors"][0]["field"] == "body.attachment_ids[2]"

    # Many errors are capped, with an honest count of the remainder.
    many = [{"type": "missing", "loc": ["body", f"f{i}"]} for i in range(9)]
    body = validation_error_body(many)
    assert len(body["errors"]) == 5
    assert body["detail"].endswith("; (+4 more validation errors)")
    assert "f8" not in body["detail"]

    assert validation_error_body([]) == {"detail": "Invalid request.", "errors": []}
