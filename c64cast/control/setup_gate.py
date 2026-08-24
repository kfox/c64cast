"""The appliance first-run setup window.

``[web].setup_wizard`` is for a pre-provisioned OS image: the box boots with no
connection target and a token nobody has seen yet, and the *only* way to reach
it is a browser on the LAN. Something has to be reachable before any
credential exists, and per SECURITY.md that has never before been true of this
surface — "it has no 'off'". This module is the one deliberate, narrow
exception, and the point of putting it in its own module is that the exposure
is bounded by construction rather than by an allowlist someone has to keep
correct.

**A middleware *outside* the token gate, not a hole punched in it.**
:func:`c64cast.control.auth.install_auth` runs inside
:func:`c64cast.control.control_plane.build_app_for_registry`, so
``TokenAuthMiddleware`` already wraps the app by the time
:func:`c64cast.app.serve.build_daemon_app` gets control back.
``Starlette.add_middleware`` inserts at index 0 of ``user_middleware`` and the
stack is built by wrapping the router in *reverse* of that list, which makes
the most-recently-added middleware the **outermost** one — installing this
gate afterward means it sees every request *before* the token check does, so
it can let the setup surface through with no token at all rather than trying
to carve an exemption out of ``TokenAuthMiddleware`` itself (whose
``public_paths`` matches only exact strings, and would need to grow a copy of
every setup route by hand).

**No hardcoded route list either.** :func:`install_setup_gate` is called after
every other route — including :func:`c64cast.control.web_static.mount_web_app`
— is registered, so :func:`c64cast.control.web_static.owned_segments` already
knows every top-level path segment the app answers. Blocking "any owned
segment except the console shell's own assets and the setup API" costs nothing
to keep correct as routes are added elsewhere; the alternative, a list of
``/status``, ``/scenes``, ``/perf``, … maintained here, is exactly the kind of
copy that silently stops covering a new route.

Once :data:`SETUP_PATH` answers ``pending: false`` the whole app is rebuilt
without this middleware (see ``serve.run_daemon``'s restart loop) rather than
this gate switching itself off mid-run — a `[web].token` chosen during setup
has to reach ``TokenAuthMiddleware``'s constructor, which takes a plain string,
not a live credential, so there is no "mid-run" state worth supporting here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

#: The one path this gate always lets through unauthenticated while pending —
#: the setup form's own API. Matched exactly, like `auth.PUBLIC_PATHS`.
SETUP_PATH = "/api/setup"

#: Where the console shell puts the setup form in the address bar. A *client*
#: route: no server route claims the segment, so the gate passes it and the
#: shell's catch-all answers it — which is what makes reloading the form work.
#: Named here rather than in the shell alone because `TokenAuthMiddleware`
#: needs it in `public_paths` (see `serve.build_daemon_app`), and a second copy
#: of the string over there is one that can drift.
SETUP_PAGE_PATH = "/setup"

#: Path segments the console shell needs regardless of setup state: the static
#: bundle. Everything else `owned_segments` reports is a real API/control
#: route and stays blocked until setup completes.
_ALWAYS_ALLOWED_SEGMENTS = frozenset({"assets"})


class SetupGateMiddleware:
    """Pure-ASGI: while setup is pending, only the console shell, its static
    assets, and :data:`SETUP_PATH` are reachable. Everything else answers
    ``503`` (or, for a WebSocket, closes with code 1013 — "try again later")
    rather than reaching the app at all, so no hardware, config, or media route
    is ever exercised through the window.

    Mirrors :class:`c64cast.control.auth.TokenAuthMiddleware`'s shape (plain
    ``__init__``/``__call__``, non-``http``/``websocket`` scopes passed
    straight through) rather than ``BaseHTTPMiddleware``, for the same reason:
    a WebSocket scope has to be inspectable and closeable before ``accept()``."""

    def __init__(self, app: Any, *, reserved: frozenset[str]) -> None:
        self.app = app
        self._reserved = reserved - _ALWAYS_ALLOWED_SEGMENTS

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        segment = path.lstrip("/").split("/", 1)[0]
        if path == SETUP_PATH or segment not in self._reserved:
            await self.app(scope, receive, send)
            return
        await self._deny(scope, receive, send)

    async def _deny(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            # Consume the queued `websocket.connect` before closing, same as
            # TokenAuthMiddleware._deny — closing unaccepted turns into a
            # clean handshake failure rather than uvicorn logging a warning.
            await receive()
            await send({"type": "websocket.close", "code": 1013})
            return
        body = json.dumps({"ok": False, "setup_required": True, "setup_path": SETUP_PATH}).encode(
            "utf-8"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def install_setup_gate(app: Any) -> None:
    """Wrap ``app`` in :class:`SetupGateMiddleware`.

    Call this **last** — after every other route, including
    ``mount_web_app`` — so :func:`c64cast.control.web_static.owned_segments`
    sees the complete route table. Only called when setup is actually
    pending; the caller decides that (``serve.run_daemon``'s restart loop
    rebuilds the app with this omitted once ``setup.json`` exists), so this
    function itself carries no "is it pending" check of its own."""
    from .web_static import owned_segments

    app.add_middleware(SetupGateMiddleware, reserved=owned_segments(app))
    log.info("web console: setup pending — only the setup form is reachable")
