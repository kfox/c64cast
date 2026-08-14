"""Shared-token authentication for c64cast's HTTP surfaces.

Everything c64cast listens on has historically been unauthenticated (see
SECURITY.md). That is defensible for a LAN tool driving hardware on your desk,
but the control plane is the one surface people already want to reach from a
phone at a gig — and the web console being built on top of it will read and
write config files on the host, which is not defensible unauthenticated at all.

This module is the gate, shipped first on the control plane where it is small
and immediately useful.

**One pure-ASGI middleware, not a per-route dependency.** Two reasons a
``Depends`` can't do this job:

* ``BaseHTTPMiddleware`` (and any ``Depends``-based scheme) never sees
  ``websocket`` scopes, and it buffers response bodies — fatal for the binary
  preview stream the web console will add.
* A WebSocket rejected from *inside* the handler can only close **after**
  ``accept()``, which a browser reports as a normal disconnect. Closing before
  accept makes uvicorn answer the handshake with an HTTP ``403``, the one
  status a client can tell apart from "the server went away".

A route added later is therefore protected by default rather than by
remembering to decorate it — ``tests/test_control_auth.py`` iterates
``app.routes`` and asserts exactly that.

Token sources, in order: ``Authorization: Bearer`` → ``X-C64Cast-Token`` →
``?token=`` → the ``c64cast_token`` cookie. The last two exist because a browser
can set no headers on a WebSocket handshake or on a plain navigation; hitting
``/api/login?token=…`` once trades the token for an ``HttpOnly; SameSite=Strict``
cookie, after which the console's page loads and its WebSocket authenticate
themselves. ``?token=`` stays the curl/scripting escape hatch. The cookie is
written from the *configured* secret rather than from the caller's string (see
``_set_cookie``), so no request data reaches a response header even in a future
where the checks get reordered.

Roles: the token grants ``full``, the optional second token grants ``viewer``,
and a viewer may only issue read methods — which covers ``/pause``, ``/skip``,
``/reload`` and ``/perf/command`` with no per-route code. The **one hole the
middleware cannot plug** is the bidirectional ``/perf/ws``: it reads
``scope["c64cast_role"]`` itself and drops inbound command frames from viewers.

Like :mod:`perf_console`, this module deliberately does **not** ``from __future__
import annotations``: the login routes annotate their params with names imported
inside :func:`_register_login_routes`, and stringized annotations would make
FastAPI mis-read them as query params.
"""

import hmac
import html
import logging
from collections.abc import Iterable, MutableMapping
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs

log = logging.getLogger(__name__)

COOKIE_NAME = "c64cast_token"
TOKEN_HEADER = b"x-c64cast-token"
LOGIN_PATH = "/api/login"

# Paths reachable without a token. Only the login exchange itself: everything
# else — including the console page, which is useless without the state feed
# behind it — is gated.
PUBLIC_PATHS = frozenset({LOGIN_PATH})

# Methods a `viewer` token may use. Anything else is a write in this API.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_DEFAULT_NEXT = "/perf"

Scope = MutableMapping[str, Any]

# The 401 a *browser* gets when someone opens the console's address without
# having logged in. Plain text is the right answer for a fetch or a curl and
# the wrong one for the front door: the daemon prints a URL with the token in
# it at startup, and a phone that has lost its cookie has nowhere else to put
# that token back. Deliberately a `GET` form, which is the same exchange the
# startup URL performs — `POST /api/login` exists so a *scripted* login can
# keep the token out of a URL, and this page has no script at all. Inline
# everything: the bundle it would otherwise link to is itself behind this gate.
_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><title>c64cast</title>
<style>
 :root{color-scheme:dark light}
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:#121216;
      color:#ececf1;font:16px/1.5 system-ui,-apple-system,sans-serif}
 form{width:min(22rem,90vw);display:grid;gap:.75rem}
 h1{margin:0;font:600 1.25rem/1.2 ui-monospace,Menlo,monospace}
 p{margin:0;color:#9b9baa;font-size:.875rem}
 input,button{font:inherit;border-radius:.5rem;padding:.7rem .85rem;border:1px solid #33333c}
 input{background:#1b1b21;color:inherit}
 button{background:#7c70da;color:#12121a;border-color:transparent;font-weight:600}
</style></head><body>
<form method="get" action="/api/login">
 <h1>c64cast</h1>
 <p>This console needs its access token. The host prints one at startup.</p>
 <input type="password" name="token" placeholder="Access token"
        autocomplete="current-password" autofocus required>
 <input type="hidden" name="next" value="{next}">
 <button type="submit">Unlock</button>
</form></body></html>
"""


def match_role(presented: str | None, token: str, viewer_token: str = "") -> str | None:
    """Return ``"full"``, ``"viewer"``, or ``None`` for a presented token.

    Constant-time per comparison; UTF-8 encoded first because
    :func:`hmac.compare_digest` rejects non-ASCII ``str`` with a ``TypeError``
    rather than a mismatch."""
    if not presented:
        return None
    candidate = presented.encode("utf-8")
    if token and hmac.compare_digest(candidate, token.encode("utf-8")):
        return "full"
    if viewer_token and hmac.compare_digest(candidate, viewer_token.encode("utf-8")):
        return "viewer"
    return None


def _presented_token(scope: Scope) -> str | None:
    """Pull the token out of an ASGI scope, preferring the sources a script
    controls over the ones a browser is limited to."""
    headers: dict[bytes, bytes] = {}
    for raw_key, raw_val in scope.get("headers", []):
        headers.setdefault(bytes(raw_key).lower(), bytes(raw_val))

    auth = headers.get(b"authorization", b"").decode("latin-1")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()

    header_token = headers.get(TOKEN_HEADER)
    if header_token:
        return header_token.decode("latin-1").strip()

    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    if query.get("token"):
        return query["token"][0]

    cookie_header = headers.get(b"cookie")
    if cookie_header:
        jar = SimpleCookie()
        try:
            jar.load(cookie_header.decode("latin-1"))
        except Exception:
            return None
        morsel = jar.get(COOKIE_NAME)
        if morsel is not None:
            return morsel.value
    return None


def _wants_html(scope: Scope) -> bool:
    """Whether this looks like a browser navigating, rather than a fetch.

    ``Accept`` is the only signal available before the app is reached, and it
    is enough: a navigation asks for ``text/html`` first, while the console's
    own requests all ask for ``application/json``."""
    for raw_key, raw_val in scope.get("headers", []):
        if bytes(raw_key).lower() == b"accept":
            return b"text/html" in bytes(raw_val).lower()
    return False


def login_page(next_path: str = "") -> str:
    """The unauthenticated front door, pointed back at ``next_path``."""
    return _LOGIN_PAGE.replace("{next}", html.escape(_safe_next(next_path), quote=True))


class TokenAuthMiddleware:
    """Pure-ASGI shared-token gate over an entire app.

    Sets ``scope["c64cast_role"]`` to ``"full"`` or ``"viewer"`` and hands off;
    denies with ``401`` (no/unknown token) or ``403`` (viewer attempting a
    write) without ever reaching the app. Non-``http``/``websocket`` scopes
    (``lifespan``) pass straight through."""

    def __init__(
        self,
        app: Any,
        *,
        token: str,
        viewer_token: str = "",
        public_paths: Iterable[str] = PUBLIC_PATHS,
    ) -> None:
        if not token:
            raise ValueError("TokenAuthMiddleware needs a non-empty token")
        self.app = app
        self._token = token
        self._viewer_token = viewer_token
        self._public = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        role = match_role(_presented_token(scope), self._token, self._viewer_token)
        if role is None:
            if scope.get("path", "") in self._public:
                await self.app(scope, receive, send)
                return
            await self._deny(scope, receive, send, 401, "authentication required")
            return
        # A websocket handshake has no `method`; treat it as the read it is at
        # connect time and let the route drop inbound writes (see perf_ws).
        if role == "viewer" and scope.get("method", "GET") not in READ_METHODS:
            await self._deny(scope, receive, send, 403, "read-only token")
            return

        scope["c64cast_role"] = role
        await self.app(scope, receive, send)

    async def _deny(self, scope: Scope, receive: Any, send: Any, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            # Consume the `websocket.connect` the server has already queued,
            # then close without accepting: uvicorn turns that into a 403 on
            # the handshake.
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        html_page = status == 401 and _wants_html(scope)
        if html_page:
            body = login_page(scope.get("path", "")).encode("utf-8")
        else:
            body = detail.encode("utf-8")
        headers = [
            (
                b"content-type",
                b"text/html; charset=utf-8" if html_page else b"text/plain; charset=utf-8",
            ),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="c64cast"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _safe_next(target: str | None) -> str:
    """Constrain the login redirect to a path on this server. ``//host`` and
    ``https://host`` are both absolute to a browser, so anything but a single
    leading slash falls back to the console."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return _DEFAULT_NEXT
    return target


def _register_login_routes(app: Any, *, token: str, viewer_token: str) -> None:
    """Register ``GET``/``POST`` ``/api/login`` — the token → cookie exchange.

    Real, non-stringized annotations (see the module note). ``GET`` redirects so
    a phone can be handed one URL with the token in it; ``POST`` answers JSON so
    a login form doesn't have to put the token in a URL that lands in history."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, RedirectResponse, Response

    def _set_cookie(response: Response, role: str) -> None:
        """Write the cookie from the **configured** secret the role names, not
        from the string the caller sent. The two are byte-equal by the time
        this runs — `match_role` is what decided the role — so this changes no
        behaviour; it moves the guarantee from an equality check a few lines
        up into the statement that actually builds the header, where a later
        reordering can't step around it (CodeQL py/cookie-injection)."""
        response.set_cookie(
            COOKIE_NAME,
            token if role == "full" else viewer_token,
            httponly=True,
            samesite="strict",
            path="/",
            # No `secure`: the control plane speaks plain HTTP on a LAN, and a
            # Secure cookie would simply never be sent back.
        )

    def _denied() -> Response:
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)

    @app.get(LOGIN_PATH)
    def login_get(request: Request) -> Response:
        presented = request.query_params.get("token")
        role = match_role(presented, token, viewer_token)
        if role is None:
            return _denied()
        redirect = RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
        _set_cookie(redirect, role)
        return redirect

    @app.post(LOGIN_PATH)
    async def login_post(request: Request) -> Response:
        presented = request.query_params.get("token")
        if presented is None:
            try:
                body = await request.json()
            except Exception:
                body = None
            if isinstance(body, dict) and isinstance(body.get("token"), str):
                presented = body["token"]
        role = match_role(presented, token, viewer_token)
        if role is None:
            return _denied()
        ok = JSONResponse({"ok": True, "role": role})
        _set_cookie(ok, role)
        return ok


def install_auth(app: Any, *, token: str, viewer_token: str = "") -> bool:
    """Gate ``app`` behind a shared token. Returns whether auth is on.

    A falsy ``token`` leaves the app wide open — the historical behaviour, and
    the default. A viewer token alone can't gate anything (there would be no
    way to write at all), so it warns rather than silently half-enabling."""
    if not token:
        if viewer_token:
            log.warning("viewer_token is set but token is not — authentication stays OFF")
        return False
    if viewer_token and hmac.compare_digest(token.encode("utf-8"), viewer_token.encode("utf-8")):
        raise ValueError("viewer_token must differ from token")
    _register_login_routes(app, token=token, viewer_token=viewer_token)
    app.add_middleware(TokenAuthMiddleware, token=token, viewer_token=viewer_token)
    return True
