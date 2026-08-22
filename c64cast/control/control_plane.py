"""HTTP control plane for runtime per-system pause / skip / reload actions.

One FastAPI app, one uvicorn server, regardless of how many systems are
in the ensemble. Endpoints take an optional `?system=NAME` query param:

  absent (1 system)   → today's un-wrapped response shape (back-compat)
  absent (N systems)  → wrapped { systems: { name: ... } } shape
  `?system=all`       → wrapped { systems: { name: ... } } shape
  `?system=NAME`      → unwrapped response for that one system
  `?system=UNKNOWN`   → 404 with the list of known names

POST endpoints (pause / resume / skip / reload) with no `?system=` and
multiple systems apply to every system. The convention reads as
"unscoped means cluster-wide, scoped means single-system."

Lives behind the `control` optional dep group (fastapi + uvicorn). The
server runs in a background thread so it doesn't block any render loop;
each system's Playlist + per-system reload closures are the shared state.

`build_app_for_registry` reads that state through providers called per
request, so one app can outlive the session it acts on (a host that starts
and stops shows under a server that keeps listening); `build_app` is the
one-shot CLI's fixed-map form of it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from c64cast._pollthread import PollThread
from c64cast.app.config import LOOPBACK_HOSTS
from c64cast.app.playlist import Playlist
from c64cast.scenes.scenes import Scene

if TYPE_CHECKING:
    from .auth import ViewerCredential

log = logging.getLogger(__name__)


SceneFactory = Callable[[], list[Scene]]
InterstitialFactory = Callable[[], Callable[[str], Scene]]

# Providers, not maps: the app outlives any one session. A long-lived host
# (`--serve`) starts and stops sessions under a server that keeps running, so
# the playlists a request acts on are whatever the *current* session owns —
# and there may be none. build_app's fixed-map form is these three closed over
# constants.
PlaylistRegistry = Callable[[], Mapping[str, Playlist]]
LoaderRegistry = Callable[[], Mapping[str, SceneFactory]]
InterstitialRegistry = Callable[[], Mapping[str, InterstitialFactory]]


class ControlServer:
    """Starts a uvicorn server bound to (host, port) on a background thread."""

    def __init__(self, host: str, port: int, app, *, label: str = "control plane"):
        try:
            import uvicorn
        except ImportError as e:
            raise RuntimeError(
                "control plane requires uvicorn: uv tool install --force 'c64cast[all]'"
            ) from e
        self.host = host
        self.port = port
        self.label = label
        self._cfg = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(self._cfg)
        # uvicorn has its own stop signal (should_exit, set in stop()), so the
        # target ignores the PollThread event — the poll supplies only the
        # daemon-thread start/join lifecycle.
        self._poll = PollThread(
            lambda stop: self._server.run(), name="control-plane", manual=True, join_timeout=2.0
        )

    def start(self) -> None:
        self._poll.start()
        log.info("%s: listening on http://%s:%d", self.label, self.host, self.port)

    def stop(self) -> None:
        self._server.should_exit = True
        self._poll.stop()


def _status_for(pl: Playlist) -> dict[str, Any]:
    cur = pl.current
    return {
        "current_scene": cur.name if cur else None,
        "current_index": pl.index,
        "n_scenes": len(pl.scenes),
        "paused": pl.pause_event.is_set(),
        "transitioning": pl.transitioning,
        "stats": pl.api.stats,
        "write_latency": pl.api.format_write_latency(),
    }


def _scenes_for(pl: Playlist) -> dict[str, Any]:
    # Shared with the web console's state feed, which offers a jump against the
    # same list — two answers about what is playing would be one too many.
    from .perf_console import scene_rows

    return {"scenes": scene_rows(pl)}


def build_app_for_registry(
    playlists: PlaylistRegistry,
    config_loaders: LoaderRegistry,
    interstitial_factories: InterstitialRegistry,
    *,
    token: str = "",
    viewer_token: str | ViewerCredential = "",
):
    """Build the FastAPI app over registry providers, consulted per request.

    An empty playlist registry means no session is running: every route
    answers `503` rather than the app being torn down and rebuilt around each
    session, which would drop the listening socket (and every connected
    console) on each show change.

    `token` gates the whole app (see `auth.install_auth`); empty leaves it
    open, which is the historical behavior. The gate is installed here rather
    than by the caller so an app built somewhere new can't ship unauthenticated
    by omission."""
    try:
        from fastapi import FastAPI, HTTPException, Query
    except ImportError as e:
        raise RuntimeError(
            "control plane requires fastapi: uv tool install --force 'c64cast[all]'"
        ) from e

    def _resolve(system: str | None) -> tuple[Mapping[str, Playlist], list[str]]:
        """Map the optional `?system=` query param to one or more system
        names. None / "all" → every system; a known name → just that one;
        unknown → 404 listing the valid names; no session at all → 503.

        Returns the registry snapshot alongside the names: a handler that
        called the provider a second time could pause one generation's
        playlists and report another's."""
        current = playlists()
        if not current:
            raise HTTPException(503, "no session running")
        names = list(current.keys())
        if system is None or system == "all":
            return current, names
        if system in current:
            return current, [system]
        raise HTTPException(404, f"unknown system {system!r}; known: {names}")

    app = FastAPI(title="c64cast", version="0.1.0")

    # GET endpoints unwrap the response when the caller named one system
    # (today's shape — single-system clients keep working unmodified).
    # Multi-system aggregate responses wrap in { systems: { name: ... } }.

    @app.get("/status")
    def status(system: str | None = Query(default=None)):
        current, targets = _resolve(system)
        if system is not None and system != "all":
            return _status_for(current[targets[0]])
        if len(targets) == 1 and system is None:
            return _status_for(current[targets[0]])
        return {"systems": {n: _status_for(current[n]) for n in targets}}

    @app.get("/scenes")
    def scenes(system: str | None = Query(default=None)):
        current, targets = _resolve(system)
        if system is not None and system != "all":
            return _scenes_for(current[targets[0]])
        if len(targets) == 1 and system is None:
            return _scenes_for(current[targets[0]])
        return {"systems": {n: _scenes_for(current[n]) for n in targets}}

    @app.post("/pause")
    def pause(system: str | None = Query(default=None)):
        current, targets = _resolve(system)
        for n in targets:
            current[n].pause_event.set()
        return {"ok": True, "paused": targets}

    @app.post("/resume")
    def resume(system: str | None = Query(default=None)):
        current, targets = _resolve(system)
        resumed: list[str] = []
        skipped: list[str] = []
        for n in targets:
            if current[n].pause_event.is_set():
                current[n].resume_event.set()
                resumed.append(n)
            else:
                skipped.append(n)
        if not resumed and len(targets) == 1:
            # Preserve the 409 today's single-system clients expect.
            raise HTTPException(409, "not currently paused")
        return {"ok": True, "resumed": resumed, "skipped_not_paused": skipped}

    @app.post("/skip")
    def skip(system: str | None = Query(default=None)):
        current, targets = _resolve(system)
        for n in targets:
            # skip_event matches the CTRL-key path so the run loop applies
            # it at a clean frame boundary, not racing process_frame.
            current[n].skip_event.set()
        return {"ok": True, "skipped": targets}

    @app.post("/reload")
    def reload(system: str | None = Query(default=None)):
        current, targets = _resolve(system)
        loaders = config_loaders()
        factories = interstitial_factories()
        reloaded: dict[str, int] = {}
        errors: dict[str, str] = {}
        for n in targets:
            # A system without a path-on-disk (e.g. defaults-only single-
            # system mode) has no reload loader. Surface that as a per-
            # system error rather than KeyErroring out.
            if n not in loaders:
                errors[n] = "no config file to reload from"
                continue
            try:
                new_scenes = loaders[n]()
                new_factory = factories[n]()
            except Exception as e:
                errors[n] = str(e)
                continue
            current[n].request_reload(new_scenes, new_factory)
            reloaded[n] = len(new_scenes)
        if errors and not reloaded:
            # Every requested reload failed — surface as a server error
            # so a single-system caller's existing 500-handling still works.
            raise HTTPException(500, f"reload failed: {errors}")
        return {"ok": True, "reloaded": reloaded, "errors": errors}

    # Live DJ/VJ Phase 5: the phone/web performance console rides the same server
    # (GET /perf page + /perf/state + /perf/command + /perf/ws), driving the same
    # performance engine the MIDI surface does. Always registered when the control
    # plane is up — the tempo readout + effect rack are useful even with no clip
    # grid configured. Kept in its own module (which, unlike this one, omits
    # `from __future__ import annotations`) so the WebSocket param injects.
    from .perf_console import PerfBridge, register_perf_routes

    register_perf_routes(app, PerfBridge(lambda: list(playlists().items())))

    # Last, so the middleware wraps every route above — including /perf/ws,
    # which a per-route dependency could only reject after accept().
    from .auth import install_auth

    install_auth(app, token=token, viewer_token=viewer_token)

    return app


def build_app(
    playlists: Mapping[str, Playlist],
    config_loaders: Mapping[str, SceneFactory],
    interstitial_factories: Mapping[str, InterstitialFactory],
    *,
    token: str = "",
    viewer_token: str | ViewerCredential = "",
):
    """Build the FastAPI app around one session's fixed maps — the one-shot
    CLI's shape, where the app and the session live and die together. Split
    from start_control_server so tests can drive it with a TestClient without
    binding a real socket."""
    if not playlists:
        raise ValueError("control plane needs at least one playlist")
    return build_app_for_registry(
        lambda: playlists,
        lambda: config_loaders,
        lambda: interstitial_factories,
        token=token,
        viewer_token=viewer_token,
    )


def start_control_server(
    host: str,
    port: int,
    playlists: Mapping[str, Playlist],
    config_loaders: Mapping[str, SceneFactory],
    interstitial_factories: Mapping[str, InterstitialFactory],
    token: str = "",
    viewer_token: str | ViewerCredential = "",
) -> ControlServer:
    """Build the FastAPI app + start a uvicorn server. Returns the server
    handle (caller calls `.stop()` at shutdown)."""
    if token:
        log.info("control plane: token authentication ON%s", " (+ viewer)" if viewer_token else "")
    elif host not in LOOPBACK_HOSTS:
        # Reached only when [control].allow_unauthenticated is set — otherwise
        # scene_factory.validate_control_cfg has already refused this
        # combination, before any hardware was opened. Kept as a warning here
        # so a caller reaching this entry point directly still gets told.
        log.warning(
            "control plane: bound to %s with no [control] token — "
            "anything that can reach the port can drive the run",
            host,
        )
    app = build_app(
        playlists, config_loaders, interstitial_factories, token=token, viewer_token=viewer_token
    )
    server = ControlServer(host, port, app)
    server.start()
    return server
