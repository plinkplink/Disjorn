"""Stored bytes are inert on the house origin: what each serving route answers for each file kind."""

import io

import pytest
from PIL import Image

from app import db
from app.config import get_settings
from app.routers import auth
from app.services import media_serve

PASSWORD = "correct horse battery staple"
HTML = b"<!doctype html><script>document.title = document.cookie</script>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.domain)</script></svg>'
XHTML = b'<html xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></html>'
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
MP3 = b"ID3\x03" + b"\x00" * 32
VARIANT_KEYS = ("orig_url", "url", "thumb_url")
ACTIVE_TYPES = ("html", "xml", "svg", "javascript", "ecmascript")


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


# (filename, bytes, type the uploader declares, type the server may answer with)
DOWNLOADS = [
    ("page.html", HTML, "text/html", media_serve.OCTET),
    ("pic.svg", SVG, "image/svg+xml", media_serve.OCTET),
    ("doc.xml", b"<?xml version='1.0'?><a/>", "application/xml", media_serve.OCTET),
    ("feed.xml", b"<?xml version='1.0'?><a/>", "text/xml", media_serve.OCTET),
    ("doc.xhtml", XHTML, "application/xhtml+xml", media_serve.OCTET),
    ("app.js", b"alert(1)", "application/javascript", media_serve.OCTET),
    ("shot.png", HTML, "text/html", media_serve.OCTET),
    ("fake.png", HTML, "image/png", media_serve.OCTET),
    ("clip.mp4", HTML, "video/mp4", media_serve.OCTET),
    ("blob", b"\x00\x01\x02", "application/octet-stream", media_serve.OCTET),
    ("blob", HTML, "", media_serve.OCTET),
    ("data.zzz", HTML, "application/x-unknown", media_serve.OCTET),
    ("notes.txt", b"hello", "text/plain", media_serve.OCTET),
    ("paper.pdf", b"%PDF-1.4\n%%EOF", "application/pdf", media_serve.PDF),
    ("paper.pdf", HTML, "application/pdf", media_serve.OCTET),
]


@pytest.fixture
async def alice(client):
    await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES ('alice', ?, 'Alice')",
        (auth.hash_password(PASSWORD),),
    )
    r = await client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200
    return client


async def stage(client, name: str, content: bytes, mime: str) -> dict:
    r = await client.post("/upload", files=[("files", (name, content, mime))])
    assert r.status_code == 200, r.text
    return r.json()["attachments"][0]


def assert_inert(r) -> None:
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    policy = [part.strip() for part in r.headers["content-security-policy"].split(";")]
    assert "default-src 'none'" in policy and "sandbox" in policy
    assert not any("script" in part or "allow-" in part for part in policy)


def assert_download(r, served: str) -> None:
    assert_inert(r)
    assert r.headers["content-type"] == served
    assert r.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"


@pytest.mark.parametrize("name,content,declared,served", DOWNLOADS)
async def test_a_file_that_is_not_inline_media_is_a_download_on_every_variant(
    alice, name, content, declared, served
):
    att = await stage(alice, name, content, declared)
    for key in VARIANT_KEYS:
        r = await alice.get(att[key])
        assert_download(r, served)
        assert r.content == content


@pytest.mark.parametrize("name,content,declared", [
    ("page.html", HTML, "text/html"),
    ("pic.svg", SVG, "image/svg+xml"),
])
async def test_no_query_or_header_makes_an_upload_answer_as_an_active_type(
    alice, name, content, declared
):
    att = await stage(alice, name, content, declared)
    extras = ("", "&type=text/html", "&content_type=text/html", "&inline=1", "&download=0",
              "&disposition=inline", "&response-content-type=text/html", f"&mime={declared}")
    for key in VARIANT_KEYS:
        for extra in extras:
            r = await alice.get(att[key] + extra, headers={"Accept": f"{declared},text/html"})
            assert_download(r, media_serve.OCTET)
            assert not any(word in r.headers["content-type"] for word in ACTIVE_TYPES)


async def test_an_image_is_inline_under_the_type_its_bytes_say(alice):
    for declared in ("image/png", "text/html", "image/svg+xml"):
        att = await stage(alice, "real.png", png() + HTML, declared)
        for key in ("orig_url", "url"):
            r = await alice.get(att[key])
            assert_inert(r)
            assert r.headers["content-type"] == "image/png"
            assert r.headers["content-disposition"] == "inline; filename*=UTF-8''real.png"
        thumb = await alice.get(att["thumb_url"])
        assert_inert(thumb)
        assert thumb.headers["content-type"] == "image/webp"
        assert "content-disposition" not in thumb.headers


async def test_an_image_needing_conversion_shows_its_variants_and_downloads_its_original(alice):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="TIFF")
    att = await stage(alice, "scan.tiff", buf.getvalue(), "image/tiff")
    assert att["width"] == 8
    for key in ("url", "thumb_url"):
        r = await alice.get(att[key])
        assert_inert(r)
        assert r.headers["content-type"] == "image/webp"
    assert_download(await alice.get(att["orig_url"]), media_serve.OCTET)


@pytest.mark.parametrize("name,content,declared", [
    ("clip.mp4", MP4, "video/mp4"),
    ("clip.mp4", MP4, "Video/MP4; codecs=avc1"),
    ("song.mp3", MP3, "audio/mpeg"),
])
async def test_audio_and_video_play_inline_when_the_bytes_agree_with_the_type(
    alice, name, content, declared
):
    att = await stage(alice, name, content, declared)
    r = await alice.get(att["orig_url"])
    assert_inert(r)
    assert r.headers["content-type"] == declared.split(";")[0].lower()
    assert r.headers["content-disposition"].startswith("inline; ")
    assert "media-src 'self'" in r.headers["content-security-policy"]


async def test_audio_bytes_under_another_containers_type_are_a_download(alice):
    att = await stage(alice, "song.mp3", MP3, "video/mp4")
    assert_download(await alice.get(att["orig_url"]), media_serve.OCTET)


async def test_a_filename_cannot_break_out_of_the_disposition_header(alice):
    att = await stage(alice, "page.html", HTML, "text/html")
    hostile = 'r\u00e9 "x";\r\nSet-Cookie: a=b\\.html'
    await db.execute(
        "UPDATE attachments SET original_filename = ? WHERE id = ?", (hostile, att["id"])
    )
    r = await alice.get(att["orig_url"])
    assert r.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''r%C3%A9%20%22x%22%3B%0D%0ASet-Cookie%3A%20a%3Db%5C.html"
    )
    assert "set-cookie" not in r.headers


async def test_a_signed_url_still_verifies_and_a_tampered_one_does_not(alice):
    att = await stage(alice, "page.html", HTML, "text/html")
    assert (await alice.get(att["orig_url"])).status_code == 200
    assert (await alice.get(att["orig_url"].replace("v=orig", "v=display"))).status_code == 403
    assert (await alice.get(att["orig_url"][:-1] + "0x")).status_code == 403


async def test_a_picker_file_that_is_not_a_raster_image_is_a_download(alice):
    assert (await alice.get("/picker", params={"tab": "image"})).status_code == 200
    folder = get_settings().data_dir / "assets" / "picker" / "images"
    (folder / "dropped.svg").write_bytes(SVG)
    (folder / "dropped.html").write_bytes(HTML)
    (folder / "lying.png").write_bytes(HTML)
    for name in ("dropped.svg", "dropped.html", "lying.png"):
        assert_download(await alice.get(f"/picker/file/image/{name}"), media_serve.OCTET)
    r = await alice.get("/picker/file/image/placeholder-red.png")
    assert_inert(r)
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline; ")


async def test_avatars_and_chibis_answer_as_inert_images(alice):
    r = await alice.post("/me/avatar", files=[("file", ("me.png", png(), "image/png"))])
    assert r.status_code == 200, r.text
    avatar = await alice.get(r.json()["url"])
    assert_inert(avatar)
    assert avatar.headers["content-type"] == "image/webp"

    bot = await db.execute(
        "INSERT INTO bots (name, api_key_hash, avatar_path) VALUES ('claw', 'x', ?)",
        (r.json()["avatar_path"],),
    )
    bot_avatar = await alice.get(f"/bots/{bot.lastrowid}/avatar")
    assert_inert(bot_avatar)
    assert bot_avatar.headers["content-type"] == "image/webp"

    chibi = await alice.get("/chibi/claudette/Happy_and_Confident/Smug.png")
    assert_inert(chibi)
    assert chibi.headers["content-type"] == "image/png"


@pytest.mark.parametrize("head,declared,decoded,expected", [
    (b"\x89PNG\r\n\x1a\n", "text/html", True, ("image/png", True)),
    (b"\x89PNG\r\n\x1a\n", "image/png", False, (media_serve.OCTET, False)),
    (b"\xff\xd8\xff\xe0", "", True, ("image/jpeg", True)),
    (b"GIF89a", "", True, ("image/gif", True)),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "", True, ("image/webp", True)),
    (b"\x00\x00\x00\x1cftypavif", "", True, ("image/avif", True)),
    (b"\x00\x00\x00\x1cftypheic", "image/heic", True, (media_serve.OCTET, False)),
    (b"<svg xmlns=", "image/svg+xml", True, (media_serve.OCTET, False)),
    (b"\x00\x00\x00\x18ftypqt  ", "video/quicktime", False, ("video/quicktime", True)),
    (b"\x1a\x45\xdf\xa3", "video/webm", False, ("video/webm", True)),
    (b"OggS", "audio/ogg", False, ("audio/ogg", True)),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav", False, ("audio/wav", True)),
    (b"fLaC", "audio/flac", False, ("audio/flac", True)),
    (b"\xff\xfb\x90\x00", "audio/mpeg", False, ("audio/mpeg", True)),
    (b"OggS", "text/html", False, (media_serve.OCTET, False)),
    (b"OggS", "video/x-anything", False, (media_serve.OCTET, False)),
    (b"", "video/mp4", False, (media_serve.OCTET, False)),
    (b"%PDF-1.7", "text/html", False, (media_serve.PDF, False)),
])
def test_the_served_type_comes_from_the_bytes(tmp_path, head, declared, decoded, expected):
    path = tmp_path / "f"
    path.write_bytes(head)
    assert media_serve.served_type(path, declared, decoded) == expected
