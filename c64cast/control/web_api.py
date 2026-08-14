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

``/api/configs`` is the other half: browse, read and edit the host's configs, so
a show can be authored and then started without a shell. Two ways to save, and
the split matters: ``PUT`` takes the text a client composed (the raw editor),
while ``PATCH`` takes named field edits and lets the server compose the text
through the config dataclasses (the generated form). Every path goes through
:class:`~c64cast.app.config_store.ConfigStore`, which is also what turns the
``config`` a start request may name into something safe to hand the loader —
the jail is not repeated here, because a second copy of it is a second thing to
get wrong.

Like :mod:`perf_console` and :mod:`auth`, this module deliberately does **not**
``from __future__ import annotations``: the WebSocket route annotates its
parameter with a name imported inside :func:`register_web_routes`, and
stringized annotations would make FastAPI mis-read it as a query parameter and
skip the injection entirely.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from c64cast.app import introspect
from c64cast.app.config import ConfigError
from c64cast.app.config_store import (
    ConfigInvalid,
    ConfigNotFound,
    ConfigStore,
    ConfigStoreError,
    ConfigTooLarge,
    EditRejected,
    PathRejected,
)
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
#: start picks up an edit made since the host launched. Its argument is the
#: config path to run, or None for the one the host was launched with. It
#: raises ConfigError / SessionConfigError, which is what a 422 is built from.
RequestFactory = Callable[[str | None], StartRequest]
PlaylistRegistry = Callable[[], Mapping[str, Playlist]]

#: How a refusal from the config store reaches the caller. The store is
#: app-level and says nothing about HTTP; the mapping lives here so it doesn't
#: have to.
_STORE_STATUS: tuple[tuple[type[ConfigStoreError], int], ...] = (
    (ConfigInvalid, 422),
    (ConfigNotFound, 404),
    (ConfigTooLarge, 413),
    (PathRejected, 403),
    (EditRejected, 400),
)


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
    store: ConfigStore,
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

    def _make_request(path: str | None) -> StartRequest:
        """Load + validate, mapping either failure to a 422 the browser can
        render. `SessionConfigError` carries the CLI's exit code, which is the
        closest thing to a machine-readable reason the validators produce."""
        try:
            return request_factory(path)
        except SessionConfigError as e:
            raise HTTPException(
                422, f"config did not validate (exit code {e.exit_code}); see the log"
            ) from e
        except ConfigError as e:
            raise HTTPException(422, str(e)) from e

    def _busy(e: SupervisorBusy) -> HTTPException:
        return HTTPException(409, str(e))

    def _store_error(e: ConfigStoreError) -> HTTPException:
        for kind, status in _STORE_STATUS:
            if isinstance(e, kind):
                detail = getattr(e, "report", None) or str(e)
                return HTTPException(status, detail)
        return HTTPException(400, str(e))

    def _resolve_ref(ref: str | None) -> str | None:
        """A browser names a config by ref; the loader wants a filesystem path.
        This is the only crossing between the two, and the store is what makes
        it safe."""
        if not ref:
            return None
        try:
            return str(store.resolve(str(ref)))
        except ConfigStoreError as e:
            raise _store_error(e) from e

    async def _body(request: Request) -> Mapping[str, Any]:
        """The optional JSON body of a POST. Absent is not an error — a start
        with no body is a start of whatever the host was launched with."""
        raw = await request.body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise HTTPException(400, "request body is not JSON") from e
        if not isinstance(parsed, Mapping):
            raise HTTPException(400, "request body must be a JSON object")
        return parsed

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
    async def api_start(request: Request) -> dict[str, Any]:
        req = _make_request(_resolve_ref((await _body(request)).get("config")))
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
    async def api_switch(request: Request) -> dict[str, Any]:
        req = _make_request(_resolve_ref((await _body(request)).get("config")))
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

    # -- the config browser -------------------------------------------------

    @app.get("/api/configs")
    def api_configs() -> dict[str, Any]:
        return store.index()

    # Registered before the bare `{ref:path}` route so a POST can't be read as
    # a write to a file whose name happens to end in "/validate".
    @app.post("/api/configs/{ref:path}/validate")
    async def api_config_validate(ref: str, request: Request) -> dict[str, Any]:
        body = await _body(request)
        try:
            return store.validate_text(str(body.get("text", "")), ref)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    @app.get("/api/configs/{ref:path}")
    def api_config_read(ref: str) -> dict[str, Any]:
        try:
            return store.read(ref)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    @app.put("/api/configs/{ref:path}")
    async def api_config_write(ref: str, request: Request) -> dict[str, Any]:
        body = await _body(request)
        if "text" not in body:
            raise HTTPException(400, 'a config write needs a "text" key')
        try:
            return store.write(ref, str(body["text"]))
        except ConfigStoreError as e:
            raise _store_error(e) from e

    # The generated form's save. PUT replaces the file with the text a client
    # composed; PATCH names fields and lets the *server* compose the text, so a
    # form never has to know how a TOML is written — and two consoles editing
    # different fields of one config don't overwrite each other's sections.
    @app.patch("/api/configs/{ref:path}")
    async def api_config_patch(ref: str, request: Request) -> dict[str, Any]:
        body = await _body(request)
        edits = body.get("edits")
        if not isinstance(edits, list):
            raise HTTPException(400, 'a config patch needs an "edits" list')
        try:
            return store.patch(ref, edits)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    # -- the state feed -----------------------------------------------------

    def _apply_command(cmd: Mapping[str, Any]) -> bool:
        """Session commands over the socket, falling through to the
        performance engine for everything else — one inbound channel, so a
        console doesn't need a second connection to launch a clip."""
        action = cmd.get("session")
        if action is None:
            return bool(bridge.apply(cmd))
        try:
            ref = cmd.get("config")
            path = str(store.resolve(str(ref))) if ref else None
            if action == "start":
                manager.start(request_factory(path))
            elif action == "switch":
                manager.switch(request_factory(path))
            elif action == "stop":
                manager.stop()
            elif action == "reload":
                manager.reload()
            else:
                return False
        except (SupervisorBusy, SessionConfigError, ConfigError, ConfigStoreError) as e:
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
