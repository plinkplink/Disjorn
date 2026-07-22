"""WP6 tests: upload + conversion, signed URLs, claim flow, WP4 payload
integration, avatars, picker (incl. traversal safety)."""

import io
import time

import pytest
from PIL import ExifTags, Image
from PIL.TiffImagePlugin import IFDRational

from app import db, events
from app.config import get_settings
from app.routers import auth, media

PASSWORD = "correct horse battery staple"
BOT_KEY = "bot-key-1"


# ---------------------------------------------------------------------------
# Helpers (same patterns as test_messages.py)
# ---------------------------------------------------------------------------

async def make_user(username: str, display_name: str | None = None) -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, auth.hash_password(PASSWORD), display_name or username.capitalize()),
    )
    return cur.lastrowid


async def make_bot(name: str = "claw", api_key: str = BOT_KEY) -> int:
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
        (name, auth.hash_api_key(api_key)),
    )
    return cur.lastrowid


async def login(client, username: str) -> None:
    r = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200


async def main_feed_id() -> int:
    row = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    assert row is not None
    return row["id"]


def capture_events() -> list[dict]:
    captured: list[dict] = []
    events.subscribe(captured.append)
    return captured


def png_bytes(size=(64, 64), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def jpeg_with_gps(size=(80, 60)) -> bytes:
    exif = Image.Exif()
    exif[ExifTags.IFD.GPSInfo] = {
        1: "N",
        2: (IFDRational(40, 1), IFDRational(41, 1), IFDRational(21, 1)),
        3: "E",
        4: (IFDRational(2, 1), IFDRational(0, 1), IFDRational(0, 1)),
    }
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 10)).save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


async def upload(client, filename: str, content: bytes, mime: str, message_id=None, **kw):
    data = {"message_id": str(message_id)} if message_id is not None else {}
    r = await client.post(
        "/upload", files=[("files", (filename, content, mime))], data=data, **kw
    )
    assert r.status_code == 200, r.text
    return r.json()


async def post_message(client, channel_id: int, content: str) -> dict:
    r = await client.post(f"/channels/{channel_id}/messages", json={"content": content})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Upload + conversion
# ---------------------------------------------------------------------------

async def test_upload_requires_auth(client):
    r = await client.post("/upload", files=[("files", ("a.png", png_bytes(), "image/png"))])
    assert r.status_code == 401


async def test_upload_png_creates_row_and_variants(client):
    await make_user("alice")
    await login(client, "alice")
    # 3000px wide -> exceeds DISPLAY_MAX, so a re-encoded display copy must exist.
    body = await upload(client, "big photo.png", png_bytes((3000, 1500)), "image/png")

    (att,) = body["attachments"]
    assert body["message"] is None  # staged upload, no message linked
    assert att["message_id"] is None
    assert att["original_filename"] == "big photo.png"
    assert att["mime_type"] == "image/png"
    assert (att["width"], att["height"]) == (3000, 1500)
    assert att["has_preview"] is True
    for key in ("url", "thumb_url", "orig_url"):
        assert att[key].startswith(f"/media/{att['id']}?")

    row = await db.fetch_one("SELECT * FROM attachments WHERE id = ?", (att["id"],))
    assert row["message_id"] is None
    assert row["uploader_type"] == "user"
    data_dir = get_settings().data_dir
    assert (data_dir / row["file_path"]).is_file()
    assert row["display_path"] and (data_dir / row["display_path"]).is_file()
    assert row["thumb_path"] and (data_dir / row["thumb_path"]).is_file()

    # Display variant is a downscaled WebP within limits.
    r = await client.get(att["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(r.content)) as im:
        assert max(im.size) <= 2048
    # Thumb within 400.
    r = await client.get(att["thumb_url"])
    assert r.status_code == 200
    with Image.open(io.BytesIO(r.content)) as im:
        assert max(im.size) <= 400


async def test_small_web_friendly_image_serves_original_as_display(client):
    await make_user("alice")
    await login(client, "alice")
    body = await upload(client, "small.png", png_bytes((64, 48)), "image/png")
    (att,) = body["attachments"]
    row = await db.fetch_one("SELECT * FROM attachments WHERE id = ?", (att["id"],))
    assert row["display_path"] is None  # no re-encode needed
    assert row["thumb_path"] is not None  # thumb always generated for images
    # display falls back to the original file + mime
    r = await client.get(att["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


async def test_gps_jpeg_is_reencoded_and_stripped(client):
    await make_user("alice")
    await login(client, "alice")
    body = await upload(client, "geo.jpg", jpeg_with_gps(), "image/jpeg")
    (att,) = body["attachments"]
    row = await db.fetch_one("SELECT * FROM attachments WHERE id = ?", (att["id"],))
    # GPS present -> serve-original shortcut must NOT apply even though small JPEG.
    assert row["display_path"] is not None
    r = await client.get(att["url"])
    assert r.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(r.content)) as im:
        assert not im.getexif().get_ifd(ExifTags.IFD.GPSInfo)  # GPS stripped
    # Original (signed orig variant) still carries its metadata.
    r = await client.get(att["orig_url"])
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.getexif().get_ifd(ExifTags.IFD.GPSInfo)


async def test_non_image_upload_stored_without_preview(client):
    await make_user("alice")
    await login(client, "alice")
    body = await upload(client, "notes.txt", b"hello disjorn", "text/plain")
    (att,) = body["attachments"]
    assert att["width"] is None and att["has_preview"] is False
    # display variant falls back to serving the original file
    r = await client.get(att["url"])
    assert r.status_code == 200
    assert r.content == b"hello disjorn"
    assert r.headers["content-type"].startswith("text/plain")


async def test_heic_upload_converted(client):
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    try:
        Image.new("RGB", (120, 90), (90, 40, 160)).save(buf, format="HEIF")
    except Exception:
        pytest.skip("HEIF encoder unavailable")

    await make_user("alice")
    await login(client, "alice")
    body = await upload(client, "phone.heic", buf.getvalue(), "image/heic")
    (att,) = body["attachments"]
    assert (att["width"], att["height"]) == (120, 90)
    row = await db.fetch_one("SELECT * FROM attachments WHERE id = ?", (att["id"],))
    assert row["display_path"] is not None  # HEIC is never web-friendly -> re-encoded
    r = await client.get(att["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"


# ---------------------------------------------------------------------------
# Signed URLs
# ---------------------------------------------------------------------------

async def test_signed_url_roundtrip_tamper_and_expiry(client):
    await make_user("alice")
    await login(client, "alice")
    body = await upload(client, "a.png", png_bytes(), "image/png")
    (att,) = body["attachments"]
    att_id = att["id"]

    # Good signature -> 200
    assert (await client.get(att["url"])).status_code == 200

    # Tampered signature -> 403
    assert (await client.get(att["url"][:-4] + "beef")).status_code == 403

    # Signature for one variant doesn't authorize another -> 403
    tampered = att["url"].replace("v=display", "v=orig")
    assert (await client.get(tampered)).status_code == 403

    # Expired (correctly signed, exp in the past) -> 403
    exp = int(time.time()) - 10
    sig = media._signature(att_id, "display", exp)
    r = await client.get(f"/media/{att_id}?v=display&exp={exp}&sig={sig}")
    assert r.status_code == 403

    # Valid signature but unknown attachment -> 404
    url = media.sign_media_url(999999)
    assert (await client.get(url)).status_code == 404

    # Unknown variant -> 400
    exp = int(time.time()) + 60
    r = await client.get(f"/media/{att_id}?v=nope&exp={exp}&sig=x")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Claim flow + WP4 integration
# ---------------------------------------------------------------------------

async def test_claim_flow_links_and_publishes_refreshed_payload(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()

    body = await upload(client, "pic.png", png_bytes((900, 600)), "image/png")
    (att,) = body["attachments"]
    msg = await post_message(client, main, "check this out")
    assert msg["attachments"] == []

    captured = capture_events()
    r = await client.post(
        "/attachments/claim",
        json={"attachment_ids": [att["id"]], "message_id": msg["id"]},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert [a["id"] for a in payload["attachments"]] == [att["id"]]

    # message_edit published with the refreshed full payload
    assert len(captured) == 1
    event = captured[0]
    assert event["type"] == "message_edit" and event["channel_id"] == main
    assert [a["id"] for a in event["message"]["attachments"]] == [att["id"]]

    # WP4 integration: history now materializes attachments with signed URLs
    r = await client.get(f"/channels/{main}/messages")
    listed = next(m for m in r.json() if m["id"] == msg["id"])
    (la,) = listed["attachments"]
    assert la["id"] == att["id"]
    assert la["url"].startswith(f"/media/{att['id']}?v=display")
    assert (la["width"], la["height"]) == (900, 600)
    assert (await client.get(la["url"])).status_code == 200

    # Re-claim is idempotent, still 200
    r = await client.post(
        "/attachments/claim",
        json={"attachment_ids": [att["id"]], "message_id": msg["id"]},
    )
    assert r.status_code == 200


async def test_claim_authorization_rules(client):
    await make_user("alice")
    await make_user("bob")
    main = await main_feed_id()

    await login(client, "alice")
    alice_att = (await upload(client, "a.png", png_bytes(), "image/png"))["attachments"][0]
    alice_msg = await post_message(client, main, "alice message")

    await login(client, "bob")
    bob_msg = await post_message(client, main, "bob message")

    # Bob can't claim onto Alice's message
    r = await client.post(
        "/attachments/claim",
        json={"attachment_ids": [alice_att["id"]], "message_id": alice_msg["id"]},
    )
    assert r.status_code == 403
    # Bob can't claim Alice's staged upload onto his own message either
    r = await client.post(
        "/attachments/claim",
        json={"attachment_ids": [alice_att["id"]], "message_id": bob_msg["id"]},
    )
    assert r.status_code == 403
    # Unknown attachment -> 404
    r = await client.post(
        "/attachments/claim", json={"attachment_ids": [12345], "message_id": bob_msg["id"]}
    )
    assert r.status_code == 404

    # Once linked, nobody can re-claim it to a different message (409)
    await login(client, "alice")
    r = await client.post(
        "/attachments/claim",
        json={"attachment_ids": [alice_att["id"]], "message_id": alice_msg["id"]},
    )
    assert r.status_code == 200
    other = await post_message(client, main, "another")
    r = await client.post(
        "/attachments/claim",
        json={"attachment_ids": [alice_att["id"]], "message_id": other["id"]},
    )
    assert r.status_code == 409


async def test_upload_with_message_id_links_immediately(client):
    await make_user("alice")
    await login(client, "alice")
    main = await main_feed_id()
    msg = await post_message(client, main, "photo incoming")

    captured = capture_events()
    body = await upload(
        client, "p.png", png_bytes((500, 400)), "image/png", message_id=msg["id"]
    )
    (att,) = body["attachments"]
    assert att["message_id"] == msg["id"]
    assert body["message"] is not None
    assert [a["id"] for a in body["message"]["attachments"]] == [att["id"]]
    assert any(
        e["type"] == "message_edit" and e["message"]["id"] == msg["id"] for e in captured
    )


async def test_upload_with_someone_elses_message_id_forbidden(client):
    await make_user("alice")
    await make_user("bob")
    main = await main_feed_id()
    await login(client, "alice")
    msg = await post_message(client, main, "mine")

    await login(client, "bob")
    r = await client.post(
        "/upload",
        files=[("files", ("x.png", png_bytes(), "image/png"))],
        data={"message_id": str(msg["id"])},
    )
    assert r.status_code == 403
    # Unknown message -> 404
    r = await client.post(
        "/upload",
        files=[("files", ("x.png", png_bytes(), "image/png"))],
        data={"message_id": "99999"},
    )
    assert r.status_code == 404


async def test_bot_can_upload_and_claim(client):
    await make_user("alice")
    bot_id = await make_bot()
    main = await main_feed_id()
    await db.execute(
        "INSERT OR IGNORE INTO channel_members (channel_id, member_type, member_id) VALUES (?, 'bot', ?)",
        (main, bot_id),
    )
    client.cookies.clear()
    headers = {"X-Api-Key": BOT_KEY}

    body = await upload(client, "bot.png", png_bytes(), "image/png", headers=headers)
    (att,) = body["attachments"]
    r = await client.post(
        f"/channels/{main}/messages", json={"content": "beep"}, headers=headers
    )
    assert r.status_code == 200
    msg = r.json()
    r = await client.post(
        "/attachments/claim",
        json={"attachment_ids": [att["id"]], "message_id": msg["id"]},
        headers=headers,
    )
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["attachments"]] == [att["id"]]


# ---------------------------------------------------------------------------
# Avatars
# ---------------------------------------------------------------------------

async def test_avatar_upload_and_fetch(client):
    uid = await make_user("alice")
    await login(client, "alice")

    r = await client.post(
        "/me/avatar", files=[("file", ("me.png", png_bytes((800, 600)), "image/png"))]
    )
    assert r.status_code == 200, r.text
    first_url = r.json()["url"]
    assert first_url.startswith(f"/avatars/{uid}?v=")
    row = await db.fetch_one("SELECT avatar_path FROM users WHERE id = ?", (uid,))
    assert row["avatar_path"] == f"avatars/user_{uid}.webp"

    # The versioned URL is surfaced wherever the user shape is, and it moves
    # when the avatar does — the path alone can't be a cache key (it never
    # changes), which is why a re-upload used to stay invisible for max-age.
    assert (await client.get("/me")).json()["avatar_url"] == first_url
    r = await client.post(
        "/me/avatar", files=[("file", ("me2.png", png_bytes((64, 64)), "image/png"))]
    )
    assert r.status_code == 200
    assert r.json()["url"] != first_url
    assert (await client.get("/me")).json()["avatar_url"] == r.json()["url"]

    r = await client.get(f"/avatars/{uid}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(r.content)) as im:
        assert max(im.size) <= 256

    # Non-image avatar -> 400; user without avatar -> 404
    r = await client.post(
        "/me/avatar", files=[("file", ("x.txt", b"not an image", "text/plain"))]
    )
    assert r.status_code == 400
    other = await make_user("bob")
    assert (await client.get(f"/avatars/{other}")).status_code == 404


# ---------------------------------------------------------------------------
# Picker
# ---------------------------------------------------------------------------

async def test_picker_listing_and_fetch(client):
    await make_user("alice")
    await login(client, "alice")

    for tab in ("image", "gif"):
        r = await client.get("/picker", params={"tab": tab})
        assert r.status_code == 200
        items = r.json()
        assert items, f"picker tab {tab} should be seeded with placeholders"
        first = items[0]
        assert first["url"] == f"/picker/file/{tab}/{first['name']}"
        rf = await client.get(first["url"])
        assert rf.status_code == 200
        assert rf.headers["content-type"].startswith("image/")

    assert (await client.get("/picker", params={"tab": "nope"})).status_code == 400
    # listing requires a user session
    client.cookies.clear()
    assert (await client.get("/picker", params={"tab": "image"})).status_code == 401


async def test_picker_path_traversal_blocked(client):
    await make_user("alice")
    await login(client, "alice")
    await client.get("/picker", params={"tab": "image"})  # ensure seeded

    # Encoded traversal survives httpx/starlette normalization into the path param.
    r = await client.get("/picker/file/image/..%2F..%2F..%2Fdisjorn.db")
    assert r.status_code in (400, 404)
    r = await client.get("/picker/file/image/%2e%2e")
    assert r.status_code in (400, 404)
    r = await client.get("/picker/file/image/.hidden")
    assert r.status_code == 400
    r = await client.get("/picker/file/nope/whatever.png")
    assert r.status_code == 404
    r = await client.get("/picker/file/image/does-not-exist.png")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DEFERRED: orig_url / thumb_url in message payloads
# ---------------------------------------------------------------------------

async def test_message_payload_carries_all_three_variants(client):
    # Regression: message_payload() signed only the display variant, so the
    # client image modal could only ever link the re-encoded WebP — the
    # preserved original was unreachable from history even though /upload
    # already returned an orig_url.
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()

    msg = await post_message(client, ch, "look at this")
    body = await upload(client, "geo.jpg", jpeg_with_gps(), "image/jpeg", message_id=msg["id"])
    att_id = body["attachments"][0]["id"]

    for payload in (
        body["message"],                                   # publish path
        (await client.get(f"/channels/{ch}/messages", params={"from_seq": 0})).json()[0],
    ):
        (att,) = payload["attachments"]
        assert att["id"] == att_id
        assert att["url"].startswith(f"/media/{att_id}?v=display")
        assert att["thumb_url"].startswith(f"/media/{att_id}?v=thumb")
        assert att["orig_url"].startswith(f"/media/{att_id}?v=orig")

    # Each URL is independently signed and actually serves its own variant:
    # display is the stripped WebP, orig is the untouched source.
    (att,) = body["message"]["attachments"]
    r = await client.get(att["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    r = await client.get(att["thumb_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    r = await client.get(att["orig_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.getexif().get_ifd(ExifTags.IFD.GPSInfo)  # the preserved original


async def test_payload_variant_urls_are_bus_visible(client):
    """WS/bot consumers see the same three variants (one payload builder)."""
    await make_user("alice")
    await login(client, "alice")
    ch = await main_feed_id()
    captured = capture_events()

    msg = await post_message(client, ch, "with a file")
    await upload(client, "s.png", png_bytes((64, 48)), "image/png", message_id=msg["id"])

    edits = [e for e in captured if e["type"] == "message_edit"]
    (att,) = edits[-1]["message"]["attachments"]
    assert {"url", "thumb_url", "orig_url"} <= set(att)
    assert all(att[k] for k in ("url", "thumb_url", "orig_url"))
