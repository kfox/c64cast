"""The web console's HTTP + WebSocket API (`--serve`).

The control plane answers questions about a session that already exists —
pause it, skip a scene, reload its config. These routes are the layer above:
they *create* and destroy sessions, so the server can outlive any show and a
browser can drive a machine that is currently doing nothing at all. They are
registered onto the same FastAPI app as `/status` and `/perf`, behind the same
token gate, because the whole point of a host console is one address.

Three shapes of answer, and which one a route gives is the design:

* **202 Accepted** for ``start`` / ``stop`` / ``switch``. Building a session
  blocks for many seconds on hardware; the supervisor claims the transition and
  returns, and the caller watches ``/api/ws`` for what happened. A route that
  waited would hold a request open across a machine reset.
* **409 Conflict** when the supervisor is busy — mapped from
  :class:`~c64cast.app.serve.SupervisorBusy`, never from a state check here.
  Reading the state and then acting on it is a race with the reap poller; the
  supervisor's own lock is the only place that decision can be made.
* **422** when the config doesn't validate. This is the payoff for
  :func:`~c64cast.app.session.validate_configs` being pure and hardware-free:
  the request that would have failed twenty seconds into a build fails
  immediately instead, with the supervisor still idle.

The state feed is the ``/perf`` payload with a ``session`` key added, so a
console that already renders the performance surface gains the supervisor for
free. Log lines ride along by sequence number rather than as a re-sent tail:
the push cadence is ~3/sec and re-sending 500 lines each time would dwarf
everything else on the socket.

Like :mod:`perf_console` and :mod:`auth`, this module deliberately does **not**
``from __future__ import annotations``: the WebSocket route annotates its
parameter with a name imported inside :func:`register_web_routes`, and
stringized annotations would make FastAPI mis-read it as a query parameter and
skip the injection entirely.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from c64cast.app import introspect
from c64cast.app.config import ConfigError
from c64cast.app.playlist import Playlist
from c64cast.app.serve import SessionManager, SessionStatus, StartRequest, SupervisorBusy
from c64cast.app.session import SessionConfigError

log = logging.getLogger(__name__)

#: How often the state feed pushes. Matches the `/perf` console's cadence —
#: the same beat grid is in the payload and the same local extrapolation runs
#: against it.
_PUSH_INTERVAL_S = 0.35

#: Lines of session log a fresh connection is handed before it starts
#: following along by sequence number.
_LOG_BACKLOG = 200

#: A request factory re-reads the config from disk and validates it, so a
#: start picks up an edit made since the host launched. It raises
#: ConfigError / SessionConfigError, which is what a 422 is built from.
RequestFactory = Callable[[], StartRequest]
PlaylistRegistry = Callable[[], Mapping[str, Playlist]]


def _status_payload(status: SessionStatus, log_buffer: Any) -> dict[str, Any]:
    out = status.as_dict()
    out["log_seq"] = log_buffer.seq if log_buffer is not None else 0
    return out


def register_web_routes(
    app: Any,
    *,
    manager: SessionManager,
    request_factory: RequestFactory,
    playlists: PlaylistRegistry,
    log_buffer: Any = None,
) -> None:
    """Register the ``/api/*`` routes on an existing FastAPI ``app``.

    Called by :func:`c64cast.app.serve.run_daemon` after the control-plane
    routes and the auth middleware are already on the app, so everything here
    is gated by the same token — a route added to this module can't ship
    unauthenticated by omission."""
    from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect

    from .perf_console import PerfBridge

    bridge = PerfBridge(lambda: list(playlists().items()))

    # Built once: ~150 KB of JSON assembled by walking every config dataclass,
    # every scene type and every overlay. It describes the code, not the run,
    # so it cannot change while the process is up.
    introspection: dict[str, Any] = {}

    def _make_request() -> StartRequest:
        """Load + validate, mapping either failure to a 422 the browser can
        render. `SessionConfigError` carries the CLI's exit code, which is the
        closest thing to a machine-readable reason the validators produce."""
        try:
            return request_factory()
        except SessionConfigError as e:
            raise HTTPException(
                422, f"config did not validate (exit code {e.exit_code}); see the log"
            ) from e
        except ConfigError as e:
            raise HTTPException(422, str(e)) from e

    def _busy(e: SupervisorBusy) -> HTTPException:
        return HTTPException(409, str(e))

    def _session_state(scope: Mapping[str, Any]) -> dict[str, Any]:
        state = _status_payload(manager.status(), log_buffer)
        state["role"] = scope.get("c64cast_role")
        return state

    @app.get("/api/introspect")
    def api_introspect() -> dict[str, Any]:
        nonlocal introspection
        if not introspection:
            introspection = introspect.as_dict()
        return introspection

    @app.get("/api/session")
    def api_session(request: Request) -> dict[str, Any]:
        state = _session_state(request.scope)
        if log_buffer is not None:
            state["log"] = log_buffer.tail(_LOG_BACKLOG)
        return state

    @app.post("/api/session/start", status_code=202)
    def api_start() -> dict[str, Any]:
        req = _make_request()
        try:
            generation = manager.start(req)
        except SupervisorBusy as e:
            raise _busy(e) from e
        return {"ok": True, "generation": generation, "state": str(manager.state)}

    @app.post("/api/session/stop", status_code=202)
    def api_stop() -> dict[str, Any]:
        # Not an error from idle: a console that stops twice, or stops a show
        # that just ended by itself, has got what it asked for.
        return {"ok": True, "stopping": manager.stop(), "state": str(manager.state)}

    @app.post("/api/session/switch", status_code=202)
    def api_switch() -> dict[str, Any]:
        req = _make_request()
        try:
            generation = manager.switch(req)
        except SupervisorBusy as e:
            raise _busy(e) from e
        return {"ok": True, "generation": generation, "state": str(manager.state)}

    @app.post("/api/session/reload")
    def api_reload() -> dict[str, Any]:
        try:
            manager.reload()
        except SupervisorBusy as e:
            raise _busy(e) from e
        return {"ok": True, "generation": manager.generation}

    def _apply_command(cmd: Mapping[str, Any]) -> bool:
        """Session commands over the socket, falling through to the
        performance engine for everything else — one inbound channel, so a
        console doesn't need a second connection to launch a clip."""
        action = cmd.get("session")
        if action is None:
            return bool(bridge.apply(cmd))
        try:
            if action == "start":
                manager.start(request_factory())
            elif action == "switch":
                manager.switch(request_factory())
            elif action == "stop":
                manager.stop()
            elif action == "reload":
                manager.reload()
            else:
                return False
        except (SupervisorBusy, SessionConfigError, ConfigError) as e:
            # The socket has no status code; the refusal shows up as the state
            # simply not changing, so say why in the log the console renders.
            log.warning("web console: %s refused: %s", action, e)
            return False
        return True

    @app.websocket("/api/ws")
    async def api_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        # The one gap the auth middleware can't cover: a socket is a single
        # `GET` handshake, so inbound command frames are dropped here (see
        # perf_console.perf_ws, which has the same hole for the same reason).
        read_only = websocket.scope.get("c64cast_role") == "viewer"
        sent_seq = 0 if log_buffer is None else max(0, log_buffer.seq - _LOG_BACKLOG)
        try:
            while True:
                frame = bridge.state()
                frame["role"] = websocket.scope.get("c64cast_role")
                frame["session"] = _status_payload(manager.status(), log_buffer)
                if log_buffer is not None:
                    lines = log_buffer.since(sent_seq)
                    if lines:
                        sent_seq = lines[-1]["seq"]
                    frame["log"] = lines
                await websocket.send_json(frame)
                try:
                    msg = await asyncio.wait_for(websocket.receive_json(), timeout=_PUSH_INTERVAL_S)
                except TimeoutError:
                    continue
                if isinstance(msg, Mapping) and not read_only:
                    _apply_command(msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("web console: websocket closed", exc_info=True)
