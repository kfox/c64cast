"""The appliance first-run setup window.

``[web].setup_wizard`` is for a pre-provisioned OS image booted with no
connection target and a token nobody has seen: the one deliberate exception to
SECURITY.md's "the web console has no 'off'". This is a middleware installed
*outside* the token gate (``Starlette.add_middleware`` prepends, and the stack
wraps in reverse, so the last one added is outermost), not a hole punched in
it, and it derives what to block from
:func:`c64cast.control.web_static.owned_segments` rather than a route list.

Once :data:`SETUP_PATH` answers ``pending: false`` the app is rebuilt without
this middleware (``serve.run_daemon``'s restart loop); nothing here supports a
mid-run toggle.

See docs/architecture/control.md#setup_gatepy--setup_apipy--the-appliance-first-run-setup-window.
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
            # TokenAuthMiddleware._deny: an unaccepted close is then a clean
            # handshake failure rather than a uvicorn warning.
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
