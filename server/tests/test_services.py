"""WP8 tests: chibi resolve + serving, unfurl parse/cache/TTL, summarize
endpoint with mocked engines, STT 501 degradation + engine factory selection."""

import shutil
from pathlib import Path

import httpx
import pytest

from app import db
from app.config import reset_settings_cache
from app.routers import auth
from app.services import chibi
from app.services import stt as stt_service
from app.services import summarize as summarize_service
from app.services import unfurl as unfurl_service

PASSWORD = "correct horse battery staple"
FIXTURE_PACK = Path(__file__).parent / "fixtures" / "chibi_pack_sample"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

async def make_user(username: str = "alice") -> int:
    cur = await db.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, auth.hash_password(PASSWORD), username.capitalize()),
    )
    return cur.lastrowid


async def login(client, username: str = "alice") -> None:
    r = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200


@pytest.fixture(autouse=True)
def clear_service_caches():
    chibi.clear_cache()
    stt_service.reset_transcriber_cache()
    yield
    chibi.clear_cache()
    stt_service.reset_transcriber_cache()
    stt_service.ENGINES.pop("testfake", None)


@pytest.fixture
def pack_copy(tmp_path) -> Path:
    """Writable copy of the fixture pack (mtime/cache tests mutate it)."""
    dest = tmp_path / "sample_pack"
    shutil.copytree(FIXTURE_PACK, dest)
    return dest


CANNED_HTML = """<!doctype html><html><head>
<title>Fallback Title</title>
<meta property="og:title" content="OG Title">
<meta property="og:description" content="OG description here.">
<meta property="og:image" content="/img/thumb.png">
<meta name="twitter:title" content="Twitter Title">
</head><body><p>Hello world body text for extraction.</p>
<script>ignored()</script></body></html>"""


# ---------------------------------------------------------------------------
# Chibi: resolve
# ---------------------------------------------------------------------------

def test_resolve_exact(pack_copy):
    ref = chibi.resolve(str(pack_copy), "Smug")
    assert ref == "chibi:sample_pack/Happy_and_Confident/Smug.png"


def test_resolve_case_insensitive(pack_copy):
    assert chibi.resolve(str(pack_copy), "sMuG") == \
        "chibi:sample_pack/Happy_and_Confident/Smug.png"


def test_resolve_hyphen_space_normalization(pack_copy):
    expected = "chibi:sample_pack/Calm_and_Content/Heart-Eyes.png"
    assert chibi.resolve(str(pack_copy), "heart eyes") == expected
    assert chibi.resolve(str(pack_copy), "HEART-EYES") == expected
    assert chibi.resolve(str(pack_copy), "heart_eyes") == expected
    assert chibi.resolve(str(pack_copy), "crying laughing") == \
        "chibi:sample_pack/Happy_and_Confident/Crying-Laughing.png"


def test_resolve_missing_and_degenerate(pack_copy):
    assert chibi.resolve(str(pack_copy), "Nonexistent") is None
    assert chibi.resolve(None, "Smug") is None
    assert chibi.resolve("", "Smug") is None
    assert chibi.resolve(str(pack_copy), "") is None
    assert chibi.resolve("/nonexistent/path/pack", "Smug") is None
    assert chibi.resolve("no-such-named-pack", "Smug") is None


def test_index_cache_invalidates_on_mtime_change(pack_copy):
    assert chibi.resolve(str(pack_copy), "NewOne") is None
    # Add a PNG — the category dir mtime changes, index must rebuild.
    src = pack_copy / "Happy_and_Confident" / "Smug.png"
    shutil.copyfile(src, pack_copy / "Happy_and_Confident" / "New-One.png")
    assert chibi.resolve(str(pack_copy), "new one") == \
        "chibi:sample_pack/Happy_and_Confident/New-One.png"


def test_resolve_named_pack_seeds_default(app):
    """Bare pack name -> DATA_DIR lookup; claudette auto-seeded from assets_seed."""
    ref = chibi.resolve("claudette", "heart eyes")
    assert ref == "chibi:claudette/Calm_and_Content/Heart-Eyes.png"
    assert (chibi.packs_root() / "claudette" / "Emotions.txt").is_file()


def test_load_taxonomy(pack_copy):
    taxonomy = chibi.load_taxonomy(pack_copy)
    assert "Happy & Confident" in taxonomy
    assert "Smug" in taxonomy["Happy & Confident"]


# ---------------------------------------------------------------------------
# Chibi: serving endpoint (path-traversal safe)
# ---------------------------------------------------------------------------

async def test_serve_chibi_ok(client):
    await make_user()
    await login(client)
    r = await client.get("/chibi/claudette/Happy_and_Confident/Smug.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_serve_chibi_requires_auth(client):
    r = await client.get("/chibi/claudette/Happy_and_Confident/Smug.png")
    assert r.status_code == 401


async def test_serve_chibi_unknown_404(client):
    await make_user()
    await login(client)
    r = await client.get("/chibi/claudette/Happy_and_Confident/Nope.png")
    assert r.status_code == 404


async def test_serve_chibi_traversal_rejected(client, tmp_path, app):
    # Unit level: safe_file must reject every traversal shape outright.
    assert chibi.safe_file("..", "Anger", "Angry.png") is None
    assert chibi.safe_file("claudette", "..", "Emotions.txt") is None
    assert chibi.safe_file("claudette", "Happy_and_Confident", "../Emotions.txt") is None
    assert chibi.safe_file("claudette", "Happy_and_Confident", "..") is None
    assert chibi.safe_file("claudette", ".hidden", "x.png") is None
    assert chibi.safe_file("claudette", "Happy_and_Confident", "Smug.txt") is None

    # Endpoint level: encoded traversal never escapes the pack root.
    await make_user()
    await login(client)
    secret = chibi.packs_root().parent / "secret.png"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"\x89PNG top secret")
    r = await client.get("/chibi/claudette/%2e%2e/secret.png")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Unfurl
# ---------------------------------------------------------------------------

def test_parse_meta_og_tags():
    meta = unfurl_service.parse_meta(CANNED_HTML, "https://example.com/post/1")
    assert meta["title"] == "OG Title"  # og: beats twitter: and <title>
    assert meta["description"] == "OG description here."
    assert meta["image_url"] == "https://example.com/img/thumb.png"  # urljoined


def test_parse_meta_garbage_never_raises():
    meta = unfurl_service.parse_meta("<<<%%% not html &&& <meta", "https://x.example")
    assert meta == {"title": None, "description": None, "image_url": None}


async def test_unfurl_endpoint_and_cache(client, monkeypatch):
    await make_user()
    await login(client)
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        return url, CANNED_HTML

    monkeypatch.setattr(unfurl_service, "fetch_head", fake_fetch)
    r = await client.get("/unfurl", params={"url": "https://example.com/a"})
    assert r.status_code == 200
    assert r.json() == {
        "url": "https://example.com/a",
        "title": "OG Title",
        "description": "OG description here.",
        "image_url": "https://example.com/img/thumb.png",
    }
    assert len(calls) == 1

    # Cache hit: a second request must not refetch.
    r2 = await client.get("/unfurl", params={"url": "https://example.com/a"})
    assert r2.status_code == 200
    assert r2.json()["title"] == "OG Title"
    assert len(calls) == 1

    # TTL expiry: age the row past 7 days -> refetch happens.
    await db.execute(
        "UPDATE unfurl_cache SET fetched_at = ? WHERE url = ?",
        ("2020-01-01T00:00:00.000Z", "https://example.com/a"),
    )
    r3 = await client.get("/unfurl", params={"url": "https://example.com/a"})
    assert r3.status_code == 200
    assert len(calls) == 2
    row = await db.fetch_one(
        "SELECT fetched_at FROM unfurl_cache WHERE url = ?", ("https://example.com/a",)
    )
    assert row["fetched_at"] > "2020-01-01"


async def test_unfurl_failure_returns_minimal_and_skips_cache(client, monkeypatch):
    await make_user()
    await login(client)

    async def broken_fetch(url):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(unfurl_service, "fetch_head", broken_fetch)
    r = await client.get("/unfurl", params={"url": "https://down.example/x"})
    assert r.status_code == 200
    assert r.json() == {
        "url": "https://down.example/x",
        "title": None,
        "description": None,
        "image_url": None,
    }
    assert await db.fetch_one(
        "SELECT * FROM unfurl_cache WHERE url = ?", ("https://down.example/x",)
    ) is None


async def test_unfurl_rejects_non_http_schemes(client):
    await make_user()
    await login(client)
    r = await client.get("/unfurl", params={"url": "file:///etc/passwd"})
    assert r.status_code == 200
    assert r.json()["title"] is None


# ---------------------------------------------------------------------------
# Summarize
# ---------------------------------------------------------------------------

class FakeSummarizer:
    def __init__(self):
        self.calls = []

    async def summarize(self, text, url):
        self.calls.append((text, url))
        return "A tidy three-sentence summary."


async def test_summarize_endpoint(client, monkeypatch):
    await make_user()
    await login(client)
    fake = FakeSummarizer()

    async def fake_fetch_page(url):
        return CANNED_HTML

    monkeypatch.setattr(summarize_service, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(summarize_service, "get_summarizer", lambda: fake)
    r = await client.post("/summarize", json={"url": "https://example.com/article"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "url": "https://example.com/article",
        "summary": "A tidy three-sentence summary.",
    }
    text, url = fake.calls[0]
    assert url == "https://example.com/article"
    assert "Hello world" in text
    assert "ignored()" not in text  # script content stripped by extraction


async def test_summarize_fetch_failure_502(client, monkeypatch):
    await make_user()
    await login(client)

    async def broken(url):
        raise summarize_service.PageFetchError("connection refused")

    monkeypatch.setattr(summarize_service, "fetch_page", broken)
    r = await client.post("/summarize", json={"url": "https://down.example/x"})
    assert r.status_code == 502
    assert "connection refused" in r.json()["detail"]


async def test_summarize_ollama_unreachable_503(client, monkeypatch):
    await make_user()
    await login(client)

    async def fake_fetch_page(url):
        return CANNED_HTML

    monkeypatch.setattr(summarize_service, "fetch_page", fake_fetch_page)
    # Real OllamaSummarizer pointed at a dead port -> httpx.ConnectError -> 503.
    monkeypatch.setattr(
        summarize_service,
        "get_summarizer",
        lambda: summarize_service.OllamaSummarizer(
            base_url="http://127.0.0.1:9", model="test"
        ),
    )
    r = await client.post("/summarize", json={"url": "https://example.com/a"})
    assert r.status_code == 503
    assert "unreachable" in r.json()["detail"].lower()


async def test_summarize_no_engine_503(client, monkeypatch):
    await make_user()
    await login(client)

    async def fake_fetch_page(url):
        return CANNED_HTML

    monkeypatch.setattr(summarize_service, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(summarize_service, "get_summarizer", lambda: None)
    r = await client.post("/summarize", json={"url": "https://example.com/a"})
    assert r.status_code == 503


def test_get_summarizer_engine_selection(monkeypatch, tmp_db_path):
    monkeypatch.setenv("SUMMARIZE_ENGINE", "ollama")
    reset_settings_cache()
    assert isinstance(summarize_service.get_summarizer(), summarize_service.OllamaSummarizer)
    monkeypatch.setenv("SUMMARIZE_ENGINE", "no-such-engine")
    reset_settings_cache()
    assert summarize_service.get_summarizer() is None


def test_extract_text_fallback_strips_tags():
    text = summarize_service.extract_text(
        "<html><body><script>var x=1;</script><p>Visible words.</p></body></html>"
    )
    assert "Visible words." in text
    assert "var x=1" not in text


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------

class FakeTranscriber:
    def __init__(self):
        self.paths = []

    async def transcribe(self, audio_path):
        self.paths.append(audio_path)
        return "hello disjorn"


async def test_stt_501_when_engine_unavailable(client):
    """faster-whisper is an optional extra and not installed in this venv."""
    assert stt_service._make_faster_whisper() is None  # precondition
    await make_user()
    await login(client)
    r = await client.post(
        "/stt", files={"audio": ("clip.webm", b"\x1aE\xdf\xa3fake", "audio/webm")}
    )
    assert r.status_code == 501
    assert "faster_whisper" in r.json()["detail"]


async def test_stt_transcribes_with_registered_engine(client, monkeypatch):
    await make_user()
    await login(client)
    fake = FakeTranscriber()
    stt_service.ENGINES["testfake"] = lambda: fake
    monkeypatch.setenv("STT_ENGINE", "testfake")
    reset_settings_cache()
    stt_service.reset_transcriber_cache()

    r = await client.post(
        "/stt", files={"audio": ("clip.webm", b"fake-webm-bytes", "audio/webm")}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "hello disjorn"}
    # Temp file lived under DATA_DIR/tmp and was cleaned up afterwards.
    assert len(fake.paths) == 1
    tmp_file = Path(fake.paths[0])
    assert tmp_file.parent.name == "tmp"
    assert not tmp_file.exists()


async def test_bot_message_emotion_resolves_to_emote_ref(client):
    """Integration: messages.py calls services.chibi.resolve(pack, emotion)."""
    cur = await db.execute(
        "INSERT INTO bots (name, api_key_hash, chibi_pack) VALUES (?, ?, ?)",
        ("claw", auth.hash_api_key("bot-key-svc"), "claudette"),
    )
    bot_id = cur.lastrowid
    feed = await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'")
    await db.execute(
        """INSERT OR IGNORE INTO channel_members (channel_id, member_type, member_id)
           VALUES (?, 'bot', ?)""",
        (feed["id"], bot_id),
    )
    r = await client.post(
        f"/channels/{feed['id']}/messages",
        json={"content": "hehe", "emotion": "smug"},
        headers={"X-Api-Key": "bot-key-svc"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["emote_refs"] == ["chibi:claudette/Happy_and_Confident/Smug.png"]


async def test_stt_requires_auth(client):
    r = await client.post(
        "/stt", files={"audio": ("clip.webm", b"x", "audio/webm")}
    )
    assert r.status_code == 401


def test_get_transcriber_factory_selection(monkeypatch, tmp_db_path):
    # Unknown engine -> None.
    monkeypatch.setenv("STT_ENGINE", "no-such-engine")
    reset_settings_cache()
    stt_service.reset_transcriber_cache()
    assert stt_service.get_transcriber() is None

    # Default engine present in the registry but backend not installed -> None.
    monkeypatch.setenv("STT_ENGINE", "faster_whisper")
    reset_settings_cache()
    stt_service.reset_transcriber_cache()
    assert stt_service.get_transcriber() is None

    # Registered fake engine -> selected and cached (same instance back).
    stt_service.ENGINES["testfake"] = lambda: FakeTranscriber()
    monkeypatch.setenv("STT_ENGINE", "testfake")
    reset_settings_cache()
    stt_service.reset_transcriber_cache()
    first = stt_service.get_transcriber()
    assert isinstance(first, FakeTranscriber)
    assert stt_service.get_transcriber() is first
