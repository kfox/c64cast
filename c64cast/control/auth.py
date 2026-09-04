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
``/reload`` and ``/perf/command`` with no per-route code. The method *is* the
authorization for every route where the verb tells the truth about the intent,
and :func:`require_full` is the seam for the ones where it doesn't: a ``GET``
that hands back host-authored text or a credential is a read to HTTP and an
administrative act to this system, and it has to say so itself. ``GET
/api/configs/{ref}`` is the reason that seam exists — its raw ``text`` is the
file verbatim, tokens and DMA password included, so a shared read-only link
would otherwise escalate to full control. The **other hole the middleware
cannot plug** is the bidirectional ``/perf/ws``: it reads the role off the
scope itself (:func:`is_viewer`) and drops inbound command frames from viewers.

The **third hole is not about credentials at all** — it is the request's origin,
and the middleware cannot close it because the mode it matters in is the one
where no middleware is installed. :func:`same_origin` is the shared check;
``perf_console.ConsoleFeed`` applies it to both console sockets before
``accept``, and ``POST /perf/command`` applies it too.

Every role comparison goes through :data:`SCOPE_ROLE_KEY` / :data:`ROLE_FULL` /
:data:`ROLE_VIEWER` and :func:`is_viewer` rather than a bare string, because
every consumer spells the check ``== "viewer"`` and a misspelling on either
side of that comparison evaluates False and *grants* write access — a fail-open
typo neither pyright nor mypy can see, since both sides are ``str``.

Like :mod:`perf_console`, this module deliberately does **not** ``from __future__
import annotations``: the login routes annotate their params with names imported
inside :func:`_register_login_routes`, and stringized annotations would make
FastAPI mis-read them as query params.
"""

import hmac
import html
import json
import logging
import secrets
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

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

#: Where the middleware records the caller's role, and the one spelling of it.
#: A literal in each consumer fails *open* on a typo (see the module note), and
#: there are three modules reading this.
SCOPE_ROLE_KEY = "c64cast_role"
ROLE_FULL = "full"
ROLE_VIEWER = "viewer"

#: A generated token is 32 url-safe bytes; anything an operator sets by hand
#: only needs to be long enough that guessing it isn't the LAN's weak point.
#: One home for the policy: it used to be declared in `setup_api` and enforced
#: on that one route, which is a policy nobody owns.
MIN_TOKEN_LENGTH = 16

_DEFAULT_NEXT = "/perf"

#: Cap on a request body read through :func:`read_body`. `/api/login` and
#: `/api/setup` are both reachable with **no credential at all**, and
#: Starlette's `Request.json()` accumulates a body of any size — so an
#: unauthenticated caller who can reach the port could exhaust RAM on a 1-2 GB
#: appliance and take down a process that owns live hardware. Generous next to
#: any real body (a login is a few hundred bytes; a config PUT is capped at
#: `config_store.MAX_BYTES`, 1 MB, and JSON escaping can inflate that) and
#: small next to the host's memory.
MAX_BODY_BYTES = 8 << 20

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


class RoleRequired(Exception):
    """A ``viewer`` reached a route that needs the full token.

    Raised by :func:`require_full` and mapped to a ``403`` by the handler
    :func:`install_auth` registers, so a route declares the requirement without
    importing FastAPI and a test can assert it without standing up an app."""


class BodyTooLarge(Exception):
    """A request body past the cap — refused, not buffered (:func:`read_body`)."""


# What a refused body is told. The exception's own message names the cap and the
# size, which is operator diagnostics; the two routes that can raise this are
# reachable without a credential, so they log that detail and answer with this.
BODY_TOO_LARGE_ERROR = "request body too large"


class ViewerCredential:
    """The read-only token, which may not exist yet.

    Unlike the full token, this one is *not* settled at startup. A configured
    value is honored; otherwise nothing exists until somebody asks for a link
    to hand out, because a credential nobody asked for is one more thing that
    can leak. :meth:`issue` mints on first ask and persists through ``store``,
    so a link given to a guest keeps working across restarts the way a
    bookmarked full-token URL does.

    Shared by reference between the gate and the route that issues links,
    which is the whole point: a token minted mid-run has to start being
    accepted without rebuilding the app around the listening socket."""

    def __init__(self, token: str = "", *, store: Callable[[str], None] | None = None) -> None:
        self._token = token
        self._store = store

    def __bool__(self) -> bool:
        """Whether a read-only token exists *yet* — so the callers that used to
        test a plain string still read correctly."""
        return bool(self._token)

    @property
    def token(self) -> str:
        """The current token, or ``""`` when none has been issued."""
        return self._token

    def issue(self) -> tuple[str, bool]:
        """``(token, minted)`` — the existing token, or a fresh one."""
        if self._token:
            return self._token, False
        self._token = secrets.token_urlsafe(32)
        if self._store is not None:
            self._store(self._token)
        return self._token, True


def match_role(presented: str | None, token: str, viewer_token: str = "") -> str | None:
    """Return ``"full"``, ``"viewer"``, or ``None`` for a presented token.

    Constant-time per comparison; UTF-8 encoded first because
    :func:`hmac.compare_digest` rejects non-ASCII ``str`` with a ``TypeError``
    rather than a mismatch."""
    if not presented:
        return None
    candidate = presented.encode("utf-8")
    if token and hmac.compare_digest(candidate, token.encode("utf-8")):
        return ROLE_FULL
    if viewer_token and hmac.compare_digest(candidate, viewer_token.encode("utf-8")):
        return ROLE_VIEWER
    return None


def role_of(scope: Mapping[str, Any]) -> str | None:
    """The role the gate recorded, or ``None`` when it isn't gating at all
    (``[control]``'s legitimately-open mode — see :func:`install_auth`)."""
    return scope.get(SCOPE_ROLE_KEY)


def is_viewer(scope: Mapping[str, Any]) -> bool:
    """Whether this caller holds the read-only token.

    The one place the ``viewer`` comparison is spelled. ``None`` (no gate) is
    not a viewer, which is the historical open behavior and deliberate — but it
    is now one decision in one function rather than the same fail-open string
    comparison copied into three modules."""
    return role_of(scope) == ROLE_VIEWER


def same_origin(headers: Any) -> bool:
    """Whether a request's ``Origin`` agrees with the ``Host`` it reached.

    The third hole the middleware cannot plug, and the one it cannot even see:
    a **WebSocket handshake is exempt from CORS entirely**, and
    ``Request.json()`` never looks at ``Content-Type``, so a cross-site
    ``<form enctype="text/plain">`` POST is a CORS-simple request with no
    preflight to refuse. The unprompted default for the console is
    ``[control] token = ""`` on loopback, justified as "exposed to whoever
    already has a shell here" — but a browser the performer happens to visit
    is not that person, and in the open mode it could open
    ``ws://127.0.0.1:8765/perf/ws``, read every pushed state frame, and send
    command frames that drive the running show.

    No ``Origin`` header is allowed: that is a non-browser caller (``curl``,
    ``wscat``, a script), which is exactly the caller "whoever already has a
    shell here" describes. A *present* ``Origin`` whose ``host:port`` does not
    match the request's own ``Host`` is refused, which is what a browser on
    another origin sends and what a same-origin page never does. Compared on
    netloc alone, because ``Host`` carries no scheme.

    Takes the request's (or websocket's) headers rather than its scope, so the
    one function serves both an HTTP route and a handshake."""
    origin = headers.get("origin")
    if not origin:
        return True
    host = headers.get("host") or ""
    return urlsplit(origin).netloc.lower() == host.lower() != ""


def require_full(scope: Mapping[str, Any]) -> None:
    """Refuse a ``viewer`` on a route that needs the full token.

    The per-route half of the authorization model. The middleware's own check
    is method-shaped, which is right for every route whose verb matches its
    intent and blind to a ``GET`` that returns host-authored text or a
    credential — so a route in that second class calls this as its first
    statement. Raises :class:`RoleRequired`, not an ``HTTPException``: this has
    to stay sayable (and testable) with no FastAPI in scope, and
    :func:`install_auth` is what turns it into the 403."""
    if is_viewer(scope):
        raise RoleRequired("this needs the full token, not the read-only one")


def _cookie_token(header: str) -> str | None:
    """The ``c64cast_token`` morsel out of a raw ``Cookie`` header.

    Hand-parsed rather than handed to :class:`~http.cookies.SimpleCookie`,
    which is not usable for *extraction* from a header this app did not write:
    CPython's ``BaseCookie.__parse_string`` stages every morsel and, on the
    first segment its pattern rejects, executes a bare ``return`` — discarding
    the ones it had already collected, ours included, without raising. So
    ``load("bad cookie here; c64cast_token=SECRET")`` and
    ``load("c64cast_token=SECRET; bad cookie here")`` both yield ``{}``.
    Browser cookies are scoped by host and ignore the port, so any *other*
    service on the same box setting a cookie whose value carries a character
    outside the legal set made this console permanently unreachable in that
    browser: a 401, the login form, a fresh ``Set-Cookie`` that replaces our
    morsel and not the offending one, and a 401 again — a login loop with
    nothing logged. Splitting on ``;`` and then on the first ``=`` cannot be
    hidden by a sibling cookie we never set."""
    for part in header.split(";"):
        name, _, value = part.partition("=")
        if name.strip() == COOKIE_NAME:
            return value.strip().strip('"')
    return None


def _presented_token(scope: Scope) -> str | None:
    """Pull the token out of an ASGI scope, preferring the sources a script
    controls over the ones a browser is limited to."""
    headers: dict[bytes, bytes] = {}
    for raw_key, raw_val in scope.get("headers", []):
        headers.setdefault(bytes(raw_key).lower(), bytes(raw_val))

    auth = headers.get(b"authorization", b"").decode("latin-1")
    if auth[:7].lower() == "bearer ":
        # Fall through on an *empty* Bearer value rather than returning it: the
        # precedence order is deliberate for a wrong token, but `Authorization:
        # Bearer ` (what some proxies and client wrappers emit for an unset
        # credential) carries no claim at all, and returning "" here suppressed
        # a perfectly good cookie or `?token=` and answered 401.
        presented = auth[7:].strip()
        if presented:
            return presented

    header_token = headers.get(TOKEN_HEADER)
    if header_token:
        return header_token.decode("latin-1").strip()

    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    if query.get("token"):
        return query["token"][0]

    cookie_header = headers.get(b"cookie")
    if cookie_header:
        return _cookie_token(cookie_header.decode("latin-1"))
    return None


async def read_body(request: Any, *, max_bytes: int | None = None) -> bytes:
    """The request's body, refused past ``max_bytes`` rather than buffered.
    ``None`` means :data:`MAX_BODY_BYTES`, read at call time.

    Streamed rather than ``await request.body()``. ``Content-Length`` is only
    the caller's claim, so it is checked first as the cheap refusal and then the
    chunks are accumulated and abandoned the moment they pass the cap — which
    is the only check a chunked body cannot lie about. Lives here because the
    two routes that need it most (``/api/login``, ``/api/setup``) are the two
    this module lets through with no credential; ``web_api._body`` shares it so
    the store's own size limit is the second line of defense rather than the
    only one."""
    cap = MAX_BODY_BYTES if max_bytes is None else max_bytes
    claimed = request.headers.get("content-length")
    if claimed is not None and claimed.isdigit() and int(claimed) > cap:
        raise BodyTooLarge(f"request body is larger than {cap} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise BodyTooLarge(f"request body is larger than {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


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


class SameOriginMiddleware:
    """Pure-ASGI ``Origin`` gate over an exact set of paths.

    :func:`same_origin`'s docstring names the hole and why the token gate
    cannot plug it: "the mode it matters in is the one where no middleware is
    installed." This is that middleware — **always** installed, token or not,
    because a `token = ""` app is exactly the one a hostile page can drive.

    The routes it covers take only a query param and no body, so a cross-site
    ``<form method="post">`` aimed at one is a CORS-simple request with no
    preflight to refuse. `/perf/command` closes this itself, at a layer that
    can also refuse a WebSocket handshake before ``accept``; these four routes
    predate that work and had nothing.

    Pure ASGI rather than ``@app.middleware("http")`` for a reason worth
    keeping: Starlette's ``BaseHTTPMiddleware`` wraps ``receive``, which breaks
    a route that streams its request body to disk — `POST /api/media/{name}`
    does, and the "body that ends early leaves no .part file" test catches it.
    This never touches ``receive``, so nothing downstream can tell it is here.
    Headers come off the ASGI ``scope``, whose names the spec guarantees are
    lowercase, so the dict it builds is already the case-correct mapping
    :func:`same_origin` expects."""

    def __init__(self, app: Any, *, paths: Iterable[str]) -> None:
        self.app = app
        self._paths = frozenset(paths)

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path", "") not in self._paths:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", ())}
        if same_origin(headers):
            await self.app(scope, receive, send)
            return
        body = b"cross-origin request"
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class TokenAuthMiddleware:
    """Pure-ASGI shared-token gate over an entire app.

    Sets ``scope[SCOPE_ROLE_KEY]`` to :data:`ROLE_FULL` or :data:`ROLE_VIEWER`
    and hands off; denies with ``401`` (no/unknown token) or ``403`` (viewer
    attempting a write) without ever reaching the app. Non-``http``/
    ``websocket`` scopes (``lifespan``) pass straight through. A route needing
    more than "is this a write?" calls :func:`require_full` itself.

    ``viewer`` is read per request rather than copied at construction: the
    read-only token can be minted while the host is running, and the app —
    with its listening socket and every connected console — is built once."""

    def __init__(
        self,
        app: Any,
        *,
        token: str,
        viewer_token: str | ViewerCredential = "",
        public_paths: Iterable[str] = PUBLIC_PATHS,
    ) -> None:
        if not token:
            raise ValueError("TokenAuthMiddleware needs a non-empty token")
        self.app = app
        self._token = token
        self._viewer = (
            viewer_token
            if isinstance(viewer_token, ViewerCredential)
            else ViewerCredential(viewer_token)
        )
        # The floor lives in the class that owns the invariant, not in one
        # caller that happens to union it: a caller passing a set without
        # `/api/login` would otherwise get an app whose 401 serves a login form
        # that posts back to a route that can only 401 again.
        self._public = PUBLIC_PATHS | frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        role = match_role(_presented_token(scope), self._token, self._viewer.token)
        if role is None:
            if scope.get("path", "") in self._public:
                await self.app(scope, receive, send)
                return
            await self._deny(scope, receive, send, 401, "authentication required")
            return
        # A websocket handshake has no `method`; treat it as the read it is at
        # connect time and let the route drop inbound writes (see perf_ws).
        if role == ROLE_VIEWER and scope.get("method", "GET") not in READ_METHODS:
            await self._deny(scope, receive, send, 403, "read-only token")
            return

        scope[SCOPE_ROLE_KEY] = role
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
    leading slash falls back to the console.

    ``/\\host`` is rejected on the same grounds and for a reason worth stating:
    per the WHATWG URL spec's relative-slash state a special-scheme relative
    URL beginning ``/\\`` enters special-authority-ignore-slashes, so a browser
    resolves ``/\\evil.com`` against ``http://console/`` as
    ``http://evil.com/``. What saves it today is outside this function —
    Starlette's ``RedirectResponse`` percent-encodes the backslash because it
    isn't in ``quote``'s safe set — and a guarantee this docstring makes should
    not live in a third-party quoting table one non-redirect use site away from
    not holding."""
    if not target or not target.startswith("/") or target[1:2] in ("/", "\\"):
        return _DEFAULT_NEXT
    return target


def _register_login_routes(app: Any, *, token: str, viewer: ViewerCredential) -> None:
    """Register ``GET``/``POST`` ``/api/login`` — the token → cookie exchange.

    Real, non-stringized annotations (see the module note). ``GET`` redirects so
    a phone can be handed one URL with the token in it; ``POST`` answers JSON so
    a login form doesn't have to put the token in a URL that lands in history.

    ``viewer`` is read per request, not captured: a read-only token minted
    while the host runs has to be able to log in with the routes already up."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, RedirectResponse, Response

    def _set_cookie(response: Response, role: str) -> None:
        """Write the cookie from the **configured** secret the role names, not
        from the string the caller sent. The two are byte-equal by the time
        this runs — `match_role` is what decided the role — so this changes no
        behavior; it moves the guarantee from an equality check a few lines
        up into the statement that actually builds the header, where a later
        reordering can't step around it (CodeQL py/cookie-injection)."""
        response.set_cookie(
            COOKIE_NAME,
            token if role == ROLE_FULL else viewer.token,
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
        role = match_role(presented, token, viewer.token)
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
                body = json.loads(await read_body(request))
            except BodyTooLarge as e:
                # The detail (which cap, how big) goes to the operator's log,
                # not to an unauthenticated caller who has no use for it.
                log.debug("login body refused: %s", e)
                return JSONResponse({"ok": False, "error": BODY_TOO_LARGE_ERROR}, status_code=413)
            except Exception:
                body = None
            if isinstance(body, dict) and isinstance(body.get("token"), str):
                presented = body["token"]
        role = match_role(presented, token, viewer.token)
        if role is None:
            return _denied()
        ok = JSONResponse({"ok": True, "role": role})
        _set_cookie(ok, role)
        return ok


def _register_role_handler(app: Any) -> None:
    """Turn :class:`RoleRequired` into the ``403`` it means.

    Registered here rather than in :mod:`web_api` so the exception and the
    status it answers with are owned by one module: a route in any other
    module can call :func:`require_full` and get the same refusal, with the
    same wording, without knowing how it is rendered."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response

    def _refused(request: Request, exc: Exception) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    app.add_exception_handler(RoleRequired, _refused)


def install_auth(
    app: Any,
    *,
    token: str,
    viewer_token: str | ViewerCredential = "",
    public_paths: Iterable[str] = PUBLIC_PATHS,
) -> bool:
    """Gate ``app`` behind a shared token. Returns whether auth is on.

    A falsy ``token`` leaves the app wide open — the historical behavior, and
    the default. A viewer token alone can't gate anything (there would be no
    way to write at all), so it warns rather than silently half-enabling.

    A :class:`ViewerCredential` may be passed instead of a string, and then the
    gate and the login routes both follow it — which is what lets a host mint a
    read-only token for a guest without a restart. The
    "must differ from the full token" check runs against whatever it holds now;
    a minted one is 32 random bytes and cannot collide.

    ``public_paths`` widens the exact-match allowlist a caller genuinely needs
    reachable with no token at all — the appliance setup form
    (:mod:`c64cast.control.setup_api`) is the one user today, threaded through
    :func:`c64cast.control.control_plane.build_app_for_registry`. Defaults to
    :data:`PUBLIC_PATHS` (just the login exchange), never narrower than that —
    the floor is :class:`TokenAuthMiddleware`'s, which owns the invariant.

    A token shorter than :data:`MIN_TOKEN_LENGTH` is warned about rather than
    refused. Neither login route nor the middleware throttles attempts, so a
    short operator-set token is a console that falls to a few thousand
    unanswered requests — but refusing one outright would break runs that work
    today, which isn't a trade this can make for the user."""
    viewer = (
        viewer_token
        if isinstance(viewer_token, ViewerCredential)
        else ViewerCredential(viewer_token)
    )
    if not token:
        if viewer.token:
            log.warning("viewer_token is set but token is not — authentication stays OFF")
        return False
    if viewer.token and hmac.compare_digest(token.encode("utf-8"), viewer.token.encode("utf-8")):
        raise ValueError("viewer_token must differ from token")
    if len(token) < MIN_TOKEN_LENGTH:
        log.warning(
            "the configured token is %d characters; %d or more is the floor this "
            "project assumes, and nothing here throttles login attempts",
            len(token),
            MIN_TOKEN_LENGTH,
        )
    _register_login_routes(app, token=token, viewer=viewer)
    _register_role_handler(app)
    app.add_middleware(
        TokenAuthMiddleware, token=token, viewer_token=viewer, public_paths=public_paths
    )
    return True
