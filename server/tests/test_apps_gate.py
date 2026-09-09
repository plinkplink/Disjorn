"""The apps serving gate (app/gate.py) and the grants it verifies (app/apps_grant.py).

Every test here builds its own gate over a tmp `/srv/apps-www` stand-in, so
nothing in this file touches the house app, the database or the real config —
which is the point of the gate and is worth the tests proving by construction.

The TestClient's base_url is https so httpx keeps the gate's Secure cookie;
over http it would silently drop it and the entry tests would look like a
cookie bug instead of a scheme one.
"""

import json
import os
import time

import pytest
from starlette.testclient import TestClient

from app import gate
from app.apps_grant import GrantError, mint, opaque_user_id, verify

SECRET = b"gate-test-secret-at-least-32-bytes-long"
HOUSE = "https://house.example"
ORIGIN_BASE = "https://house.example:10000"

APP_A = "egnnhtz3ppwi"
APP_B = "aaaabbbbcccc"

CTX = {
    "version": 1,
    "user": {"id": "opaque123", "name": "Alice", "avatar_url": None},
    "shared_with": ["Bob"],
    "theme": {},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def www_root(tmp_path):
    """A minimal /srv/apps-www: app A live + preview, app B live."""
    root = tmp_path / "apps-www"
    (root / APP_A / "live").mkdir(parents=True)
    (root / APP_A / "preview").mkdir(parents=True)
    (root / APP_B / "live").mkdir(parents=True)

    (root / APP_A / "live" / "index.html").write_text(
        "<!doctype html><html><head><title>A</title></head><body>hi</body></html>"
    )
    (root / APP_A / "live" / "app.js").write_text("console.log('a');")
    (root / APP_A / "live" / "style.css").write_text("body{color:red}")
    (root / APP_A / "live" / ".secret").write_text("nope")
    (root / APP_A / "preview" / "index.html").write_text("<html><head></head><body>preview</body>")
    (root / APP_B / "live" / "index.html").write_text("<html><head></head><body>b</body></html>")
    (tmp_path / "outside.txt").write_text("private")
    return root


@pytest.fixture
def settings(www_root):
    return gate.GateSettings(
        secret=SECRET, www_root=www_root, house_origin=HOUSE, origin_base=ORIGIN_BASE
    )


@pytest.fixture
def client(settings):
    with TestClient(
        gate.build_app(settings), base_url="https://apps.example", follow_redirects=False
    ) as c:
        yield c


def grant(app_id=APP_A, roots=("live",), ctx=None, ttl_sec=3600):
    return mint(SECRET, app_id=app_id, roots=list(roots), ctx=ctx or CTX, ttl_sec=ttl_sec)


def enter(client, app_id=APP_A, token=None, path=None):
    """Do the `?t=` entry so the client holds the cookie, and return the 302."""
    return client.get(f"{path or f'/{app_id}/'}?t={token or grant(app_id)}")


CSP = (
    "default-src 'self'; connect-src 'self'; form-action 'self'; "
    f"base-uri 'self'; frame-ancestors {HOUSE}"
)


def assert_walls(response):
    """Every response, success or failure, carries all three (D3)."""
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------------------
# Grants (app/apps_grant.py)
# ---------------------------------------------------------------------------


def test_grant_round_trip():
    payload = verify(SECRET, grant(roots=("live", "preview")))
    assert payload["v"] == 1
    assert payload["app"] == APP_A
    assert payload["roots"] == ["live", "preview"]
    assert payload["ctx"] == CTX
    assert payload["exp"] > time.time()


def test_grant_tampered_signature_is_refused():
    body, sig = grant().split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    with pytest.raises(GrantError) as exc:
        verify(SECRET, f"{body}.{flipped}")
    assert exc.value.reason == "bad signature"


def test_grant_tampered_payload_is_refused():
    """Rewriting the payload to widen `roots` fails the signature, not the parser."""
    token = grant()
    payload = json.loads(_decode(token))
    payload["roots"] = ["live", "preview"]
    forged = _encode(json.dumps(payload).encode()) + "." + token.split(".")[1]
    with pytest.raises(GrantError) as exc:
        verify(SECRET, forged)
    assert exc.value.reason == "bad signature"


def test_grant_wrong_secret_is_refused():
    with pytest.raises(GrantError) as exc:
        verify(b"a-different-secret-of-at-least-32-bytes", grant())
    assert exc.value.reason == "bad signature"


def test_grant_expired():
    token = mint(SECRET, app_id=APP_A, roots=["live"], ctx=CTX, ttl_sec=60, now=time.time() - 3600)
    with pytest.raises(GrantError) as exc:
        verify(SECRET, token)
    assert exc.value.reason == "expired"


def test_grant_wrong_version():
    """A v2 token signed with the right secret still does not verify."""
    body = json.dumps(
        {"v": 2, "app": APP_A, "roots": ["live"], "exp": int(time.time()) + 60, "ctx": CTX},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with pytest.raises(GrantError) as exc:
        verify(SECRET, _sign_token(body))
    assert "version" in exc.value.reason


def test_grant_malformed_shapes():
    for token in ["", "nodot", "a.b.c", "!!!.???"]:
        with pytest.raises(GrantError):
            verify(SECRET, token)


def test_mint_refuses_bad_inputs():
    with pytest.raises(ValueError):
        mint(SECRET, app_id="TOO-SHORT", roots=["live"], ctx=CTX)
    with pytest.raises(ValueError):
        mint(SECRET, app_id=APP_A, roots=["staging"], ctx=CTX)
    with pytest.raises(ValueError):
        mint(SECRET, app_id=APP_A, roots=[], ctx=CTX)
    with pytest.raises(ValueError):
        mint(b"short", app_id=APP_A, roots=["live"], ctx=CTX)


def test_opaque_user_id_is_stable_and_per_app():
    a1 = opaque_user_id(SECRET, 7, APP_A)
    assert a1 == opaque_user_id(SECRET, 7, APP_A)
    assert a1 != opaque_user_id(SECRET, 7, APP_B)
    assert a1 != opaque_user_id(SECRET, 8, APP_A)
    assert len(a1) == 16
    assert a1 == a1.lower() and "=" not in a1
    assert set(a1) <= set("abcdefghijklmnopqrstuvwxyz234567")  # base32, lowercased


# ---------------------------------------------------------------------------
# Boot config
# ---------------------------------------------------------------------------


def test_short_secret_refuses_to_build(www_root):
    with pytest.raises(gate.ConfigError) as exc:
        gate.GateSettings(secret=b"too-short", www_root=www_root, house_origin=HOUSE)
    assert "32 bytes" in str(exc.value)


def test_empty_house_origins_refuses_to_build(www_root):
    with pytest.raises(gate.ConfigError):
        gate.GateSettings(secret=SECRET, www_root=www_root, house_origin="")
    with pytest.raises(gate.ConfigError):
        gate.load_settings({"APPS_GATE_SECRET": SECRET.decode(), "HOUSE_ORIGINS": "[]"})


def test_missing_secret_refuses_to_build():
    with pytest.raises(gate.ConfigError):
        gate.load_settings({"HOUSE_ORIGINS": json.dumps([HOUSE])})


def test_load_settings_reads_the_four_keys(www_root):
    loaded = gate.load_settings(
        {
            "APPS_GATE_SECRET": SECRET.decode(),
            "APPS_WWW_ROOT": str(www_root),
            "HOUSE_ORIGINS": json.dumps([HOUSE, "https://second"]),
            "APPS_ORIGIN_BASE": ORIGIN_BASE,
        }
    )
    assert loaded.secret == SECRET
    assert str(loaded.www_root) == str(www_root)
    assert loaded.house_origin == HOUSE  # first entry only
    assert loaded.origin_base == ORIGIN_BASE


# ---------------------------------------------------------------------------
# Entry, cookie and refusal (D3)
# ---------------------------------------------------------------------------


def test_entry_sets_the_cookie_and_strips_the_token(client):
    response = enter(client)
    assert response.status_code == 302
    assert response.headers["location"] == f"/{APP_A}/"
    assert_walls(response)

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{gate.COOKIE_NAME}=")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie or "SameSite=Lax" in cookie
    assert f"Path=/{APP_A}/" in cookie
    assert "Max-Age=" in cookie


def test_entry_keeps_other_query_params(client):
    response = client.get(f"/{APP_A}/?t={grant()}&mode=dark&x=1")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"/{APP_A}/?")
    assert "t=" not in location
    assert "mode=dark" in location and "x=1" in location


def test_entry_with_a_bad_token_is_403(client):
    response = client.get(f"/{APP_A}/?t=not-a-grant")
    assert response.status_code == 403
    assert_walls(response)
    assert response.cookies.get(gate.COOKIE_NAME) is None


def test_entry_with_another_apps_token_is_403(client):
    response = client.get(f"/{APP_A}/?t={grant(app_id=APP_B)}")
    assert response.status_code == 403
    assert_walls(response)


def test_no_cookie_is_403_with_the_walls(client):
    response = client.get(f"/{APP_A}/")
    assert response.status_code == 403
    assert_walls(response)
    body = response.text.strip()
    assert body.count("\n") == 0
    assert "house.example" in body  # names where to open it from


def test_403_body_without_origin_base(www_root):
    settings = gate.GateSettings(secret=SECRET, www_root=www_root, house_origin=HOUSE)
    with TestClient(gate.build_app(settings), base_url="https://apps.example") as c:
        assert c.get(f"/{APP_A}/").text.strip() == "Open this app from the house."


def test_cookie_for_app_a_does_not_open_app_b(client):
    enter(client)
    assert client.get(f"/{APP_A}/").status_code == 200
    # httpx scopes the cookie by Path, so B's path sends none at all; the
    # gate's app-id check is the second wall behind that and is asserted by
    # test_forged_cookie_path below.
    response = client.get(f"/{APP_B}/")
    assert response.status_code == 403
    assert_walls(response)


def test_a_cookie_carried_by_hand_to_another_app_is_403(client):
    """The path is the authority: even a valid grant, sent explicitly, is refused."""
    response = client.get(f"/{APP_B}/", headers={"cookie": f"{gate.COOKIE_NAME}={grant(APP_A)}"})
    assert response.status_code == 403
    assert_walls(response)


def test_expired_cookie_is_403(client):
    stale = mint(SECRET, app_id=APP_A, roots=["live"], ctx=CTX, ttl_sec=60, now=time.time() - 3600)
    response = client.get(f"/{APP_A}/", headers={"cookie": f"{gate.COOKIE_NAME}={stale}"})
    assert response.status_code == 403
    assert_walls(response)


# ---------------------------------------------------------------------------
# Roots (D4)
# ---------------------------------------------------------------------------


def test_viewer_grant_cannot_reach_preview(client):
    enter(client)  # roots = ["live"]
    response = client.get(f"/{APP_A}/preview/")
    assert response.status_code == 403
    assert_walls(response)


def test_viewer_grant_is_refused_at_the_preview_door_not_after_a_bounce(client):
    response = client.get(f"/{APP_A}/preview/?t={grant(roots=('live',))}")
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert_walls(response)


def test_owner_grant_reaches_both_roots(client):
    enter(client, token=grant(roots=("live", "preview")))
    assert "hi" in client.get(f"/{APP_A}/").text
    assert "preview" in client.get(f"/{APP_A}/preview/").text


def test_missing_live_root_is_404_not_live_yet(client, www_root):
    import shutil

    shutil.rmtree(www_root / APP_A / "live")
    enter(client, token=grant(roots=("live", "preview")))
    response = client.get(f"/{APP_A}/")
    assert response.status_code == 404
    assert response.text.strip() == "This app is not live yet."
    assert_walls(response)


def test_missing_preview_root_is_404_no_preview_yet(client, www_root):
    import shutil

    shutil.rmtree(www_root / APP_A / "preview")
    enter(client, token=grant(roots=("live", "preview")))
    response = client.get(f"/{APP_A}/preview/")
    assert response.status_code == 404
    assert response.text.strip() == "No preview yet."


def test_unknown_app_id_shape_is_404(client):
    for path in ["/", "/nope/", "/NOTANAPPID/", "/short/index.html"]:
        response = client.get(path)
        assert response.status_code == 404, path
        assert_walls(response)


# ---------------------------------------------------------------------------
# Files, traversal and dotfiles (D4)
# ---------------------------------------------------------------------------


def test_directory_serves_index_html(client):
    enter(client)
    assert client.get(f"/{APP_A}/").status_code == 200
    assert client.get(f"/{APP_A}/index.html").status_code == 200


def test_dotfile_is_404(client):
    enter(client)
    response = client.get(f"/{APP_A}/.secret")
    assert response.status_code == 404
    assert_walls(response)
    assert "nope" not in response.text


def test_encoded_traversal_is_404(client):
    enter(client)
    for path in [f"/{APP_A}/..%2f..%2foutside.txt", f"/{APP_A}/..%2Fpreview%2Findex.html"]:
        response = client.get(path)
        assert response.status_code == 404, path
        assert "private" not in response.text


def test_symlink_out_of_the_root_is_404(client, www_root, tmp_path):
    os.symlink(tmp_path / "outside.txt", www_root / APP_A / "live" / "escape.txt")
    enter(client)
    response = client.get(f"/{APP_A}/escape.txt")
    assert response.status_code == 404
    assert "private" not in response.text
    assert_walls(response)


def test_missing_file_is_404(client):
    enter(client)
    assert client.get(f"/{APP_A}/nothing-here.js").status_code == 404


def test_post_is_405_with_the_walls(client):
    response = client.post(f"/{APP_A}/")
    assert response.status_code == 405
    assert_walls(response)


def test_head_returns_headers_and_no_body(client):
    enter(client)
    response = client.head(f"/{APP_A}/app.js")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(len("console.log('a');"))
    assert_walls(response)


def test_cache_headers(client):
    enter(client)
    assert client.get(f"/{APP_A}/").headers["cache-control"] == "no-store"
    assert client.get(f"/{APP_A}/{gate.BLOB_FILENAME}").headers["cache-control"] == "no-store"
    assert client.get(f"/{APP_A}/app.js").headers["cache-control"] == "private, max-age=60"
    assert client.get(f"/{APP_A}/style.css").headers["cache-control"] == "private, max-age=60"


def test_content_types(client):
    enter(client)
    assert client.get(f"/{APP_A}/").headers["content-type"].startswith("text/html")
    assert client.get(f"/{APP_A}/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get(f"/{APP_A}/style.css").headers["content-type"].startswith("text/css")


def test_oversized_file_is_413(client, www_root, monkeypatch):
    monkeypatch.setattr(gate, "MAX_FILE_BYTES", 8)
    (www_root / APP_A / "live" / "big.bin").write_bytes(b"x" * 64)
    enter(client)
    response = client.get(f"/{APP_A}/big.bin")
    assert response.status_code == 413
    assert_walls(response)


# ---------------------------------------------------------------------------
# The context blob (D5)
# ---------------------------------------------------------------------------


def test_blob_carries_the_ctx_and_can_never_close_a_script(client):
    hostile = dict(CTX, shared_with=["</script><script>alert(1)</script>", "Bö"])
    enter(client, token=grant(ctx=hostile))
    response = client.get(f"/{APP_A}/{gate.BLOB_FILENAME}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert_walls(response)

    body = response.text
    assert body.startswith("window.__DISJORN__ = ")
    assert "</script" not in body
    assert "<" not in body
    # ...and it is still the real data once a browser parses it.
    parsed = json.loads(body[len("window.__DISJORN__ = ") : body.rstrip().rfind(";")])
    assert parsed == hostile


def test_blob_is_served_from_the_preview_root_too(client):
    enter(client, token=grant(roots=("live", "preview")))
    assert client.get(f"/{APP_A}/preview/{gate.BLOB_FILENAME}").status_code == 200


def test_blob_needs_a_grant_like_everything_else(client):
    assert client.get(f"/{APP_A}/{gate.BLOB_FILENAME}").status_code == 403


def test_script_tag_is_inserted_after_head(client):
    enter(client)
    body = client.get(f"/{APP_A}/").text
    assert gate.BLOB_TAG in body
    assert body.index(gate.BLOB_TAG) == body.lower().index("<head>") + len("<head>")
    assert 'src="__disjorn.js"' in body  # relative, no leading slash


def test_script_tag_goes_before_the_first_script_when_there_is_no_head(client, www_root):
    (www_root / APP_A / "live" / "index.html").write_text(
        "<!doctype html><body><script src='app.js'></script></body>"
    )
    enter(client)
    body = client.get(f"/{APP_A}/").text
    assert body.index(gate.BLOB_TAG) < body.index("<script src='app.js'>")


def test_script_tag_is_prepended_when_there_is_neither(client, www_root):
    (www_root / APP_A / "live" / "index.html").write_text("<p>bare</p>")
    enter(client)
    assert client.get(f"/{APP_A}/").text == gate.BLOB_TAG + "<p>bare</p>"


def test_head_tag_with_attributes_still_matches(client, www_root):
    (www_root / APP_A / "live" / "index.html").write_text(
        '<html><HEAD lang="en"><title>x</title></HEAD></html>'
    )
    enter(client)
    body = client.get(f"/{APP_A}/").text
    assert body.startswith('<html><HEAD lang="en">' + gate.BLOB_TAG)


def test_non_html_is_not_rewritten(client):
    enter(client)
    assert client.get(f"/{APP_A}/app.js").text == "console.log('a');"


# ---------------------------------------------------------------------------
# Wall 5: the gate has no database and no house credential
# ---------------------------------------------------------------------------


def test_gate_imports_nothing_from_the_house():
    """A gate that can reach the house DB is a house with a second front door.

    Asserted as a test and not only as a comment, because the way this wall
    falls is one convenient `from .config import get_settings` in a hurry.
    """
    import subprocess
    import sys

    pulled = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, importlib; importlib.import_module('app.gate'); "
            "print(' '.join(sorted(m for m in sys.modules if m.split('.')[0] in "
            "('app', 'fastapi', 'sqlite3', 'aiosqlite', 'pydantic_settings'))))",
        ],
        capture_output=True,
        text=True,
        check=True,
        # The server dir, wherever the suite was launched from.
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(gate.__file__))),
    ).stdout.split()
    assert set(pulled) == {"app", "app.gate", "app.apps_grant"}


def test_five_hundred_still_carries_the_walls(settings, monkeypatch):
    """Headers are stamped outside Starlette's own error handler, so a crash keeps them."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(gate, "_parse_path", boom)
    with TestClient(
        gate.build_app(settings), base_url="https://apps.example", raise_server_exceptions=False
    ) as c:
        response = c.get(f"/{APP_A}/")
    assert response.status_code == 500
    assert_walls(response)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(token: str) -> bytes:
    import base64

    body = token.split(".")[0]
    return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))


def _sign_token(body: bytes) -> str:
    import hashlib
    import hmac

    return f"{_encode(body)}.{_encode(hmac.new(SECRET, body, hashlib.sha256).digest())}"
