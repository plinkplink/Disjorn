"""The apps serving gate: a second ASGI process that serves app files and nothing else.

Run by `deploy/disjorn-apps-gate.service` on 127.0.0.1:8402, fronted by
Tailscale Funnel on :10000 (spec `SPECS/2026-09-09-apps-serving-gate.md`, D1).
It is a separate origin from the house — scheme+host+port — which is what makes
the CSP, the gate cookie and the frame's localStorage actually separate.

**What this module may import is load-bearing.** Stdlib, starlette, and
`app.apps_grant`. Nothing else. A gate that could `import app.db` is a house
with a second front door; wall 5 of the parent spec ("the gate has no database
and no house credential") is enforced here by the absence of the import, and
by this file being the only place a reviewer has to look to check that.

What it does, in order, for every request:

1. Security headers on *every* response, errors included (D3). They are added
   by the outermost wrapper, so a 500 raised anywhere inside still carries the
   CSP that pins the app's egress.
2. Path grammar `/<app-id>/[preview/]<file>` (D4). The first segment names the
   app; a second segment of `preview` selects the preview root.
3. Entry: `?t=<grant>` verifies once, becomes a `Path=/<app>/` cookie, and
   redirects to the same URL without the token, so the grant leaves the address
   bar (and the referrer, and the user's history) immediately.
4. Everything else reads that cookie and checks it names *this* app and grants
   *this* root. A cookie scoped to one app can never ride to another's path,
   and the check is repeated here anyway: the path is the authority, the cookie
   is the claim.
5. Files come from `<APPS_WWW_ROOT>/<app>/<root>/`, resolved and prefix-checked
   so a symlink out of the tree is a 404 rather than a read.
6. `__disjorn.js` is generated from the cookie's `ctx` and injected into HTML
   by a `<script src>` tag — never inline, because the CSP the builder brief
   promises has no `'unsafe-inline'` (D5).

Config comes from the environment; the unit loads `server/.env` as its
EnvironmentFile, the same file the house reads, because the house mints with
the same secret.
"""

import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .apps_grant import APP_ID_RE, GrantError, verify

# The gate's cookie. Named nothing like the house's `__Host-disjorn_session`
# so that a confusion between them is a typo, not a near-miss.
COOKIE_NAME = "disjorn_gate"

# The generated context blob, and the tag that pulls it in. Relative, never
# leading-slash: the app is not served from the origin root (builder brief §3).
BLOB_FILENAME = "__disjorn.js"
BLOB_TAG = f'<script src="{BLOB_FILENAME}"></script>'

# Read whole files; house apps are static and small. The cap is a guard against
# something absurd landing in /srv/apps-www, not a streaming policy.
MAX_FILE_BYTES = 32 * 1024 * 1024

DEFAULT_WWW_ROOT = "/srv/apps-www"
INDEX_NAME = "index.html"

_HEAD_OPEN_RE = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)
_SCRIPT_OPEN_RE = re.compile(rb"<script\b", re.IGNORECASE)

# mimetypes' guesses vary by platform /etc/mime.types, and two of them matter
# enough to pin: `.js` must be `text/javascript` (the modern registration; an
# `application/javascript` here is harmless but inconsistent), and `.mjs` is
# missing on some boxes entirely.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}
FALLBACK_CONTENT_TYPE = "application/octet-stream"

CACHE_NO_STORE = "no-store"
CACHE_ASSET = "private, max-age=60"

# Serving methods, and everything else the route must still catch so that the
# 405 comes from this module and carries the security headers.
SERVING_METHODS = ("GET", "HEAD")
_ALL_METHODS = SERVING_METHODS + ("POST", "PUT", "PATCH", "DELETE", "OPTIONS")


class ConfigError(RuntimeError):
    """Config whose failure mode is silent. The gate refuses to start on it."""


@dataclass(frozen=True)
class GateSettings:
    """Everything the gate is allowed to know.

    Note what is absent: no DB path, no house API key, no broker socket. If a
    field ever needs adding here, that is the moment to ask whether it belongs
    in a different process.
    """

    secret: bytes
    www_root: Path
    house_origin: str
    origin_base: str = ""

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ConfigError(
                "APPS_GATE_SECRET must be at least 32 bytes: a short secret makes "
                "every grant forgeable and nothing in the logs would say so. "
                'Generate one with: python3 -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if not self.house_origin:
            raise ConfigError(
                "HOUSE_ORIGINS must have at least one entry: its first entry is the "
                "CSP frame-ancestors value, and an empty one would let any site frame "
                "every app in the house."
            )


def load_settings(env: dict[str, str] | None = None) -> GateSettings:
    """Read config from the environment (systemd loads server/.env into it)."""
    env = os.environ if env is None else env

    secret = (env.get("APPS_GATE_SECRET") or "").encode("utf-8")
    if not secret:
        raise ConfigError(
            "APPS_GATE_SECRET is not set. The gate verifies house-minted grants "
            "with it and has no other way to tell a viewer from a stranger."
        )

    raw_origins = env.get("HOUSE_ORIGINS") or "[]"
    try:
        origins = json.loads(raw_origins)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"HOUSE_ORIGINS is not valid JSON: {exc}") from None
    if not isinstance(origins, list) or any(not isinstance(o, str) for o in origins):
        raise ConfigError('HOUSE_ORIGINS must be a JSON array of strings, e.g. ["https://host"]')

    return GateSettings(
        secret=secret,
        www_root=Path(env.get("APPS_WWW_ROOT") or DEFAULT_WWW_ROOT),
        house_origin=origins[0] if origins else "",
        origin_base=env.get("APPS_ORIGIN_BASE") or "",
    )


# ---------------------------------------------------------------------------
# Security headers — outermost, so even a 500 carries them
# ---------------------------------------------------------------------------


class SecurityHeaders:
    """Stamp the per-app CSP onto every response, including error responses.

    This wraps the whole Starlette app rather than sitting in its middleware
    stack, because Starlette's own 500 handler runs outside that stack: a
    response the app never got to write is exactly the one whose headers a
    reviewer will not be able to check by reading the endpoint.
    """

    def __init__(self, app: ASGIApp, headers: list[tuple[bytes, bytes]]) -> None:
        self.app = app
        self.headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    (k, v) for k, v in self.headers if k not in existing
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def security_headers(settings: GateSettings) -> list[tuple[bytes, bytes]]:
    csp = (
        "default-src 'self'; connect-src 'self'; form-action 'self'; "
        f"base-uri 'self'; frame-ancestors {settings.house_origin}"
    )
    return [
        (b"content-security-policy", csp.encode("latin-1")),
        (b"referrer-policy", b"no-referrer"),
        (b"x-content-type-options", b"nosniff"),
    ]


# ---------------------------------------------------------------------------
# The one endpoint
# ---------------------------------------------------------------------------


class _Refused(Exception):
    """Answer this request with `response` and stop. Keeps the happy path flat."""

    def __init__(self, response: Response) -> None:
        self.response = response


def build_app(settings: GateSettings) -> ASGIApp:
    """The gate as an ASGI app. Separate from `app` so tests can vary config."""

    async def serve(request: Request) -> Response:
        try:
            return _serve(settings, request)
        except _Refused as refusal:
            return refusal.response

    # One catch-all route that accepts every method, so the 405 is ours and
    # carries the CSP; Starlette's own method rejection would not go through
    # the endpoint at all.
    inner = Starlette(routes=[Route("/{path:path}", serve, methods=list(_ALL_METHODS))])
    return SecurityHeaders(inner, security_headers(settings))


def _serve(settings: GateSettings, request: Request) -> Response:
    if request.method not in SERVING_METHODS:
        # 405 before anything else: a POST is never a serving request, whatever
        # path it names, and there is nothing here for it to reach.
        raise _Refused(
            _text(405, "Method not allowed.", headers={"allow": ", ".join(SERVING_METHODS)})
        )

    app_id, root, segments = _parse_path(request.url.path)

    token = request.query_params.get("t")
    if token is not None:
        return _enter(settings, request, app_id, root, token)

    payload = _payload_from_cookie(settings, request, app_id, root)
    body = _read(settings, app_id, root, segments, payload)
    return _respond(request, body)


# ---------------------------------------------------------------------------
# Path grammar (D4)
# ---------------------------------------------------------------------------


def _parse_path(path: str) -> tuple[str, str, list[str]]:
    """`/<app>/[preview/]<file...>` → (app id, root, file segments).

    Percent-encoding is already decoded by the server, so `..%2f` arrives here
    as a literal `..` segment and is refused by the dotfile rule below rather
    than by string-matching the encoded form.
    """
    parts = [p for p in path.split("/") if p != ""]
    if not parts or not APP_ID_RE.fullmatch(parts[0]):
        raise _Refused(_text(404, "Not found."))

    rest = parts[1:]
    if rest and rest[0] == "preview":
        return parts[0], "preview", rest[1:]
    return parts[0], "live", rest


# ---------------------------------------------------------------------------
# Entry and the cookie (D3)
# ---------------------------------------------------------------------------


def _enter(
    settings: GateSettings, request: Request, app_id: str, root: str, token: str
) -> Response:
    """Verify `?t=`, plant the cookie, and redirect the token out of the URL.

    The app id and the root are checked here as well as on the redirected
    request, so a grant that does not cover the door it is presented at is
    refused at that door rather than after a bounce.
    """
    try:
        payload = verify(settings.secret, token)
    except GrantError:
        raise _Refused(_forbidden(settings)) from None
    if payload["app"] != app_id or root not in payload["roots"]:
        raise _Refused(_forbidden(settings))

    params = parse_qsl(request.url.query, keep_blank_values=True)
    remaining = [(k, v) for k, v in params if k != "t"]
    target = request.url.path + (f"?{urlencode(remaining)}" if remaining else "")

    response = RedirectResponse(target, status_code=302)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max(0, payload["exp"] - int(time.time())),
        path=f"/{app_id}/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


def _payload_from_cookie(
    settings: GateSettings, request: Request, app_id: str, root: str
) -> dict:
    """The cookie is a claim; the path is the authority. Both must agree."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise _Refused(_forbidden(settings))
    try:
        payload = verify(settings.secret, token)
    except GrantError:
        raise _Refused(_forbidden(settings)) from None
    if payload["app"] != app_id or root not in payload["roots"]:
        raise _Refused(_forbidden(settings))
    return payload


def _forbidden(settings: GateSettings) -> Response:
    """One sentence, the same for every failure. See GrantError's docstring."""
    where = _host_of(settings.origin_base)
    line = (
        f"Open this app from the house at {where}." if where else "Open this app from the house."
    )
    return _text(403, line)


def _host_of(origin_base: str) -> str:
    if not origin_base:
        return ""
    parsed = urlsplit(origin_base if "//" in origin_base else f"//{origin_base}")
    return parsed.netloc or ""


# ---------------------------------------------------------------------------
# Files (D4) and the context blob (D5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Body:
    content: bytes
    content_type: str
    cache_control: str


def _read(
    settings: GateSettings, app_id: str, root: str, segments: list[str], payload: dict
) -> _Body:
    root_dir = settings.www_root / app_id / root
    if not root_dir.is_dir():
        # Distinct sentences: "no live yet" is the normal state of an app mid
        # build, and reads as progress rather than as breakage.
        raise _Refused(
            _text(404, "This app is not live yet." if root == "live" else "No preview yet.")
        )

    # A path ending in the blob name is always the generated blob, at any depth.
    # D5 names it "at either root"; serving it at every depth as well is a
    # deliberate widening, because the injected tag is relative and a nested
    # index.html would otherwise ask for a file that is not there. It also
    # means an app cannot ship a file by that name and shadow the real blob.
    if segments and segments[-1] == BLOB_FILENAME:
        return _blob(payload.get("ctx") or {})

    for segment in segments:
        # Refused before resolution, so `..` never reaches the filesystem and
        # a dotfile never leaks by being resolvable.
        if segment.startswith("."):
            raise _Refused(_text(404, "Not found."))

    target = root_dir.joinpath(*segments) if segments else root_dir
    real_root = os.path.realpath(root_dir)
    real = os.path.realpath(target)
    if os.path.isdir(real):
        real = os.path.realpath(os.path.join(real, INDEX_NAME))

    # Resolve first, then prefix-check: a symlink pointing out of the tree is
    # only visible after resolution, and it is a 404 like anything else.
    if real != real_root and not real.startswith(real_root + os.sep):
        raise _Refused(_text(404, "Not found."))
    if not os.path.isfile(real):
        raise _Refused(_text(404, "Not found."))
    if os.path.getsize(real) > MAX_FILE_BYTES:
        raise _Refused(_text(413, "That file is too large to serve."))

    content = Path(real).read_bytes()
    content_type = _content_type(real)
    if content_type.startswith("text/html"):
        content = _inject_blob_tag(content)
        return _Body(content, content_type, CACHE_NO_STORE)
    return _Body(content, content_type, CACHE_ASSET)


def _blob(ctx: dict) -> _Body:
    """`window.__DISJORN__ = {...}` as a same-origin script (D5).

    ensure_ascii keeps every non-ASCII character escaped, and `<` is escaped on
    top of that, so the text cannot contain `</script` however the blob was
    filled in. Inline delivery would need `'unsafe-inline'` in the CSP, which
    is exactly the thing the builder brief promises is absent.
    """
    encoded = json.dumps(ctx, ensure_ascii=True, sort_keys=True).replace("<", "\\u003c")
    body = f"window.__DISJORN__ = {encoded};\n".encode("ascii")
    return _Body(body, _CONTENT_TYPES[".js"], CACHE_NO_STORE)


def _inject_blob_tag(content: bytes) -> bytes:
    """Insert the blob tag after `<head>`, else before the first `<script`, else first.

    Done on bytes with byte-level regexes so a file with a broken encoding is
    still served (and still gets its blob) rather than failing to decode.
    """
    match = _HEAD_OPEN_RE.search(content)
    if match:
        return content[: match.end()] + BLOB_TAG.encode() + content[match.end() :]
    match = _SCRIPT_OPEN_RE.search(content)
    if match:
        return content[: match.start()] + BLOB_TAG.encode() + content[match.start() :]
    return BLOB_TAG.encode() + content


def _content_type(path: str) -> str:
    suffix = os.path.splitext(path)[1].lower()
    if suffix in _CONTENT_TYPES:
        return _CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or FALLBACK_CONTENT_TYPE


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _respond(request: Request, body: _Body) -> Response:
    headers = {"cache-control": body.cache_control}
    if request.method == "HEAD":
        # Same headers, same Content-Length, no body: a HEAD that reported a
        # different length than the GET would be worse than no HEAD at all.
        headers["content-length"] = str(len(body.content))
        return Response(b"", media_type=body.content_type, headers=headers)
    return Response(body.content, media_type=body.content_type, headers=headers)


def _text(status: int, line: str, headers: dict[str, str] | None = None) -> Response:
    return PlainTextResponse(
        line + "\n",
        status_code=status,
        headers={"cache-control": CACHE_NO_STORE, **(headers or {})},
    )


def __getattr__(name: str):
    """`app` is built on first access, not at import.

    uvicorn resolves `app.gate:app` by attribute lookup, so the unit still gets
    a configured app; tests import `build_app` without needing a real
    APPS_GATE_SECRET in the environment, and a config error surfaces at boot
    with its message instead of as an import traceback.
    """
    if name == "app":
        return build_app(load_settings())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
