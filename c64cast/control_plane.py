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
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from .playlist import Playlist
from .scenes import Scene

log = logging.getLogger(__name__)


SceneFactory = Callable[[], list[Scene]]
InterstitialFactory = Callable[[], Callable[[str], Scene]]


class ControlServer:
    """Starts a uvicorn server bound to (host, port) on a background thread."""

    def __init__(self, host: str, port: int, app):
        try:
            import uvicorn
        except ImportError as e:
            raise RuntimeError(
                "control plane requires uvicorn: pip install c64cast[control]"
            ) from e
        self.host = host
        self.port = port
        self._cfg = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(self._cfg)
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="control-plane")
        self._thread.start()
        log.info("control plane: listening on http://%s:%d", self.host, self.port)

    def stop(self):
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _status_for(pl: Playlist) -> dict[str, Any]:
    cur = pl.current
    return {
        "current_scene": cur.name if cur else None,
        "current_index": pl.index,
        "n_scenes": len(pl.scenes),
        "paused": pl.pause_event.is_set(),
        "transitioning": pl.transitioning,
        "u64_stats": pl.api.stats,
        "u64_dma_latency": pl.api.format_write_latency(),
    }


def _scenes_for(pl: Playlist) -> dict[str, Any]:
    import math

    return {
        "scenes": [
            {
                "index": i,
                "name": s.name,
                # VideoScene uses math.inf to mean "runs until the file
                # ends"; JSON can't carry inf, so surface that as None.
                "duration_s": (None if math.isinf(s.duration_s) else s.duration_s),
                "is_current": i == pl.index,
            }
            for i, s in enumerate(pl.scenes)
        ]
    }


def build_app(
    playlists: Mapping[str, Playlist],
    config_loaders: Mapping[str, SceneFactory],
    interstitial_factories: Mapping[str, InterstitialFactory],
):
    """Build the FastAPI app. Split from start_control_server so tests can
    drive it with a TestClient without binding a real socket."""
    try:
        from fastapi import FastAPI, HTTPException, Query
    except ImportError as e:
        raise RuntimeError("control plane requires fastapi: pip install c64cast[control]") from e

    if not playlists:
        raise ValueError("control plane needs at least one playlist")

    names = list(playlists.keys())

    def _resolve(system: str | None) -> list[str]:
        """Map the optional `?system=` query param to one or more system
        names. None / "all" → every system; a known name → just that one;
        unknown → 404 listing the valid names."""
        if system is None or system == "all":
            return names
        if system in playlists:
            return [system]
        raise HTTPException(404, f"unknown system {system!r}; known: {names}")

    app = FastAPI(title="c64cast", version="0.1.0")

    # GET endpoints unwrap the response when the caller named one system
    # (today's shape — single-system clients keep working unmodified).
    # Multi-system aggregate responses wrap in { systems: { name: ... } }.

    @app.get("/status")
    def status(system: str | None = Query(default=None)):
        targets = _resolve(system)
        if system is not None and system != "all":
            return _status_for(playlists[targets[0]])
        if len(targets) == 1 and system is None:
            return _status_for(playlists[targets[0]])
        return {"systems": {n: _status_for(playlists[n]) for n in targets}}

    @app.get("/scenes")
    def scenes(system: str | None = Query(default=None)):
        targets = _resolve(system)
        if system is not None and system != "all":
            return _scenes_for(playlists[targets[0]])
        if len(targets) == 1 and system is None:
            return _scenes_for(playlists[targets[0]])
        return {"systems": {n: _scenes_for(playlists[n]) for n in targets}}

    @app.post("/pause")
    def pause(system: str | None = Query(default=None)):
        targets = _resolve(system)
        for n in targets:
            playlists[n].pause_event.set()
        return {"ok": True, "paused": targets}

    @app.post("/resume")
    def resume(system: str | None = Query(default=None)):
        targets = _resolve(system)
        resumed: list[str] = []
        skipped: list[str] = []
        for n in targets:
            if playlists[n].pause_event.is_set():
                playlists[n].resume_event.set()
                resumed.append(n)
            else:
                skipped.append(n)
        if not resumed and len(targets) == 1:
            # Preserve the 409 today's single-system clients expect.
            raise HTTPException(409, "not currently paused")
        return {"ok": True, "resumed": resumed, "skipped_not_paused": skipped}

    @app.post("/skip")
    def skip(system: str | None = Query(default=None)):
        targets = _resolve(system)
        for n in targets:
            # skip_event matches the CTRL-key path so the run loop applies
            # it at a clean frame boundary, not racing process_frame.
            playlists[n].skip_event.set()
        return {"ok": True, "skipped": targets}

    @app.post("/reload")
    def reload(system: str | None = Query(default=None)):
        targets = _resolve(system)
        reloaded: dict[str, int] = {}
        errors: dict[str, str] = {}
        for n in targets:
            # A system without a path-on-disk (e.g. defaults-only single-
            # system mode) has no reload loader. Surface that as a per-
            # system error rather than KeyErroring out.
            if n not in config_loaders:
                errors[n] = "no config file to reload from"
                continue
            try:
                new_scenes = config_loaders[n]()
                new_factory = interstitial_factories[n]()
            except Exception as e:
                errors[n] = str(e)
                continue
            playlists[n].request_reload(new_scenes, new_factory)
            reloaded[n] = len(new_scenes)
        if errors and not reloaded:
            # Every requested reload failed — surface as a server error
            # so a single-system caller's existing 500-handling still works.
            raise HTTPException(500, f"reload failed: {errors}")
        return {"ok": True, "reloaded": reloaded, "errors": errors}

    return app


def start_control_server(
    host: str,
    port: int,
    playlists: Mapping[str, Playlist],
    config_loaders: Mapping[str, SceneFactory],
    interstitial_factories: Mapping[str, InterstitialFactory],
) -> ControlServer:
    """Build the FastAPI app + start a uvicorn server. Returns the server
    handle (caller calls `.stop()` at shutdown)."""
    app = build_app(playlists, config_loaders, interstitial_factories)
    server = ControlServer(host, port, app)
    server.start()
    return server
