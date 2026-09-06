"""Origin wall: a cookie-authenticated write or /ws handshake must come from the house.

Apps v1 serves house-built, untrusted JS from `https://<house-host>:8443`.
Cookies ignore ports, so that JS shares this host's cookie jar and its fetches
and WebSocket handshakes to `:443` ride the session cookie. This is the wall
that stops them.

Keyed on the session cookie, never on the route: bots authenticate by
`X-Api-Key` or by the first WS frame and carry no cookie, so a route-wide check
would reject every bot. Without the cookie a request falls through untouched
and meets normal auth.

An absent Origin is a rejection, not a pass. This API is JSON-only via fetch,
which always sends Origin, and browsers send it on every cross-origin unsafe
request and on every WS handshake. The known omission case — same-origin form
POSTs from pre-2020 Chrome and older Safari — is asserted from spec knowledge,
not measured against house clients. If it ever bites, the fallback is
allow-absent with a WARN log, never a per-route exemption.
"""

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import get_settings
from .routers.auth import COOKIE_NAME

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Flat `detail` string, same shape as every HTTPException in the app.
REJECTED = "Cross-origin request rejected"


class OriginWall:
    """403s cookie-bearing unsafe requests and WS handshakes from a foreign Origin."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            conn = HTTPConnection(scope)
            if COOKIE_NAME in conn.cookies and self._guarded(scope):
                if conn.headers.get("origin") not in get_settings().HOUSE_ORIGINS:
                    await self._reject(scope, receive, send)
                    return
        await self.app(scope, receive, send)

    @staticmethod
    def _guarded(scope: Scope) -> bool:
        if scope["type"] == "websocket":
            return True
        return scope["method"].upper() not in SAFE_METHODS

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await JSONResponse({"detail": REJECTED}, status_code=403)(scope, receive, send)
            return
        # Both branches answer before accept, so the socket is never live and
        # the client sees a failed handshake rather than a close code.
        if "websocket.http.response" in (scope.get("extensions") or {}):
            await send(
                {
                    "type": "websocket.http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "websocket.http.response.body", "body": REJECTED.encode()})
        else:
            # A server without the denial-response extension turns a pre-accept
            # close into a 403 handshake itself; the code is never sent.
            await send({"type": "websocket.close", "code": 1008})
