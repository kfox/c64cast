"""Tests for the web console's `/api/*` routes and the host that serves them.

The supervisor's own state machine is covered by tests/test_serve.py; what
these add is the mapping from it onto HTTP — which transition is a 202, which
refusal is a 409, and that a config that can't run is refused *before* any
transition is claimed rather than twenty seconds into a build. The build and
teardown seams stay injected, so nothing here opens a socket to hardware or
sleeps waiting for one.

`EveryApiRouteIsProtectedTest` is the same payoff `test_control_auth.py` takes
on the control plane, extended to the routes that start and stop machines: it
walks the assembled app and asserts everything outside `PUBLIC_PATHS` refuses
an unauthenticated caller, so a route added to `web_api.py` later cannot ship
open by omission.

Not unit-testable here, and left to the `hw-visual-verify` skill: a browser's
own WebSocket handshake carrying the login cookie, preview windows opened and
closed across sessions on the main thread, and a start that actually reaches a
machine."""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
# pyright: reportOptionalCall=false
from __future__ import annotations

import argparse
import logging
import os
import threading
import unittest
import warnings
from pathlib import Path
from typing import Any
from unittest import mock

from _fakes import fake_system_stack

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from starlette.websockets import WebSocketDisconnect

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False
    TestClient = None  # type: ignore[misc,assignment]
    WebSocketDisconnect = Exception  # type: ignore[misc,assignment]

from c64cast.app import config as cfgmod
from c64cast.app import serve, session
from c64cast.app.serve import SessionState

TOKEN = "full-token-value"
VIEWER = "viewer-token-value"
AUTH = {"X-C64Cast-Token": TOKEN}
VIEWER_AUTH = {"X-C64Cast-Token": VIEWER}

# Long enough for a loaded CI box, short enough that a stuck transition fails
# the run rather than hanging it.
WAIT = 5.0


# --- fakes -----------------------------------------------------------------


class _FakeTempo:
    bpm = 120.0
    running = True
    source = "internal"
    beats_per_bar = 4

    def beat_phase_at(self, now: float | None = None) -> float:
        return 0.0

    def bar_phase_at(self, now: float | None = None) -> float:
        return 0.0


class _FakePerf:
    active_slot: int | None = None
    armed_slot: int | None = None
    armed_detail: tuple[int, str, float, float] | None = None

    def clips_info(self) -> list[dict[str, Any]]:
        return []

    def saved_look_slots(self) -> list[int]:
        return []


class _FakeScene:
    def __init__(self, name: str) -> None:
        self.name = name
        self.effects: list[Any] = []
        self.duration_s = 10.0


class _FakeApi:
    stats = {"writes": 1}

    def format_write_latency(self) -> str:
        return "lat 5ms"


class _FakePlaylist:
    """JSON-serialisable stand-in: the state feed carries the `/perf` payload,
    so a MagicMock playlist would only fail once it reached the encoder."""

    def __init__(self) -> None:
        self.tempo = _FakeTempo()
        self.performance = _FakePerf()
        self.current = _FakeScene("demo")
        self.scenes = [self.current]
        self.index = 0
        self.transitioning = False
        self.api = _FakeApi()
        self.pause_event = threading.Event()
        self.resume_event = threading.Event()
        self.skip_event = threading.Event()


def _request(*names: str, config_path: str = "show.toml") -> serve.StartRequest:
    cfgs = [cfgmod.Config() for _ in names]
    for cfg in cfgs:
        cfg.audio.enabled = False
    loaded = cfgmod.LoadResult(
        cfgs=cfgs,
        names=list(names),
        paths=[None] * len(names),
        is_ensemble=len(names) > 1,
        master_control=cfgs[0].control,
        master_midi_control=cfgs[0].midi_control,
    )
    return serve.request_from_configs(
        argparse.Namespace(overwrite=False), loaded, cfgs, config_path=config_path
    )


def _session(req: serve.StartRequest) -> session.Session:
    stacks = []
    for name in req.loaded.names:
        st = fake_system_stack(name)
        st.playlist = _FakePlaylist()
        stacks.append(st)
    return session.Session(
        args=req.args,
        loaded=req.loaded,
        cfgs=req.cfgs,
        stacks=stacks,
        ensemble=None,
        stop_event=threading.Event(),
        profiler=mock.MagicMock(name="profiler"),
    )


class _Build:
    """Publishes a fake session, optionally waiting on a gate first so a test
    can hold the supervisor in `starting` and drive the routes against it."""

    def __init__(self) -> None:
        self.gate: threading.Event | None = None
        self.calls = 0

    def __call__(self, req, generation, publish):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(WAIT)
        sess = _session(req)
        publish(sess)
        return sess


class _Factory:
    """The request factory the routes hold: hands back a request, or raises
    the way a config that doesn't validate would."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    def __call__(self) -> serve.StartRequest:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _request("a")


class WebApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.marker = Path(tmp.name) / "run.json"
        self.build = _Build()
        self.factory = _Factory()
        self.log_buffer = serve.SessionLogBuffer(capacity=50)
        self.manager = serve.SessionManager(
            build=self.build,
            teardown=lambda sess: None,
            settle_s=0.0,
            reap_period_s=0.01,
            marker_path=self.marker,
            log_buffer=self.log_buffer,
        )
        self.addCleanup(self.manager.close, timeout=WAIT)

    def app(self, **kwargs) -> Any:
        kwargs.setdefault("token", TOKEN)
        kwargs.setdefault("viewer_token", VIEWER)
        return serve.build_daemon_app(
            self.manager, self.factory, log_buffer=self.log_buffer, **kwargs
        )

    def client(self, **kwargs) -> Any:
        return TestClient(self.app(**kwargs))

    def assertReaches(self, state: SessionState, *, generation: int | None = None) -> None:
        self.assertTrue(
            self.manager.wait_for(state, timeout=WAIT, generation=generation),
            f"still {self.manager.state} (generation {self.manager.generation}), wanted {state}",
        )


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class SessionLifecycleTest(WebApiTestCase):
    def test_start_is_accepted_and_returns_the_generation(self):
        with self.client() as c:
            r = c.post("/api/session/start", headers=AUTH)
            self.assertEqual(r.status_code, 202)
            self.assertEqual(r.json()["generation"], 1)
            self.assertReaches(SessionState.RUNNING)

    def test_start_while_starting_is_a_conflict_not_an_implicit_stop(self):
        self.build.gate = threading.Event()
        self.addCleanup(self.build.gate.set)
        with self.client() as c:
            self.assertEqual(c.post("/api/session/start", headers=AUTH).status_code, 202)
            r = c.post("/api/session/start", headers=AUTH)
            self.assertEqual(r.status_code, 409)
            self.assertIn("starting", r.json()["detail"])
        self.assertEqual(self.build.calls, 1)

    def test_a_config_that_does_not_validate_is_refused_before_any_transition(self):
        self.factory.error = session.SessionConfigError(5)
        with self.client() as c:
            r = c.post("/api/session/start", headers=AUTH)
            self.assertEqual(r.status_code, 422)
            self.assertIn("exit code 5", r.json()["detail"])
        self.assertEqual(self.manager.state, SessionState.IDLE)
        self.assertEqual(self.build.calls, 0)

    def test_a_broken_toml_is_refused_with_its_own_message(self):
        self.factory.error = cfgmod.ConfigError("Config file not found: nope.toml")
        with self.client() as c:
            r = c.post("/api/session/start", headers=AUTH)
        self.assertEqual(r.status_code, 422)
        self.assertIn("nope.toml", r.json()["detail"])

    def test_stop_from_idle_is_not_an_error(self):
        with self.client() as c:
            r = c.post("/api/session/stop", headers=AUTH)
        self.assertEqual(r.status_code, 202)
        self.assertFalse(r.json()["stopping"])

    def test_stop_brings_a_running_session_down(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            r = c.post("/api/session/stop", headers=AUTH)
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.json()["stopping"])
        self.assertReaches(SessionState.IDLE)

    def test_switch_replaces_the_running_show_with_the_next_generation(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING, generation=1)
            r = c.post("/api/session/switch", headers=AUTH)
            self.assertEqual(r.status_code, 202)
            self.assertEqual(r.json()["generation"], 2)
            self.assertReaches(SessionState.RUNNING, generation=2)
        self.assertEqual(self.build.calls, 2)

    def test_switch_on_a_config_that_does_not_validate_leaves_the_show_alone(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            self.factory.error = session.SessionConfigError(3)
            r = c.post("/api/session/switch", headers=AUTH)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.manager.state, SessionState.RUNNING)

    def test_reload_needs_a_running_session(self):
        with self.client() as c:
            self.assertEqual(c.post("/api/session/reload", headers=AUTH).status_code, 409)
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            self.assertEqual(c.post("/api/session/reload", headers=AUTH).status_code, 200)


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class SessionStatusTest(WebApiTestCase):
    def test_idle_status_shape(self):
        with self.client() as c:
            body = c.get("/api/session", headers=AUTH).json()
        self.assertEqual(body["state"], "idle")
        self.assertEqual(body["generation"], 0)
        self.assertEqual(body["systems"], [])
        self.assertIsNone(body["last_error"])
        self.assertEqual(body["role"], "full")
        self.assertIn("log", body)

    def test_running_status_names_its_systems(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            body = c.get("/api/session", headers=AUTH).json()
        self.assertEqual(body["state"], "running")
        self.assertEqual(body["systems"], ["a"])
        self.assertEqual(body["config_path"], "show.toml")

    def test_the_log_tail_carries_what_a_failed_start_wrote(self):
        self.log_buffer.install()
        self.addCleanup(self.log_buffer.uninstall)
        logger = logging.getLogger("c64cast")
        old_level, old_prop = logger.level, logger.propagate
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self.addCleanup(
            lambda: (logger.setLevel(old_level), setattr(logger, "propagate", old_prop))
        )
        logger.error("could not reach the machine")
        with self.client() as c:
            body = c.get("/api/session", headers=AUTH).json()
        messages = [row["message"] for row in body["log"]]
        self.assertIn("could not reach the machine", messages)
        self.assertEqual(body["log_seq"], self.log_buffer.seq)


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class IntrospectRouteTest(WebApiTestCase):
    def test_introspect_carries_the_metadata_the_schema_drops(self):
        with self.client() as c:
            body = c.get("/api/introspect", headers=AUTH).json()
        sections = {s["name"] for s in body["sections"]}
        self.assertIn("web", sections)
        self.assertIn("ultimate64", sections)
        # `apply` and `applies_to` are exactly why the console reads this
        # instead of the committed JSON Schema, which omits both.
        scene_fields = body["scene_types"][0]["fields"]
        self.assertTrue(all("apply" in f and "applies_to" in f for f in scene_fields))

    def test_a_required_overlay_param_survives_json(self):
        # `REQUIRED` is a sentinel object, not a value json can encode — an
        # overlay with a mandatory parameter is where that shows up.
        with self.client() as c:
            body = c.get("/api/introspect", headers=AUTH).json()
        required = [p for ov in body["overlays"] for p in ov["params"] if p["required"]]
        self.assertTrue(required, "no required overlay param found")
        self.assertTrue(all(p["default"] is None for p in required))


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class ControlPlaneUnderTheHostTest(WebApiTestCase):
    def test_control_routes_answer_503_until_a_session_exists(self):
        with self.client() as c:
            self.assertEqual(c.get("/status", headers=AUTH).status_code, 503)
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            r = c.get("/status", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["current_scene"], "demo")

    def test_skip_reaches_the_running_playlist(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            self.assertEqual(c.post("/skip", headers=AUTH).status_code, 200)
        sess = self.manager.session
        self.assertTrue(sess.stacks[0].playlist.skip_event.is_set())


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class StateFeedTest(WebApiTestCase):
    def test_the_feed_carries_the_perf_payload_plus_the_session(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            with c.websocket_connect("/api/ws", headers=AUTH) as ws:
                frame = ws.receive_json()
        self.assertEqual(frame["session"]["state"], "running")
        self.assertEqual(frame["session"]["systems"], ["a"])
        self.assertEqual(frame["role"], "full")
        self.assertEqual([s["name"] for s in frame["systems"]], ["a"])

    def test_log_lines_arrive_once_rather_than_as_a_resent_tail(self):
        self.log_buffer.install()
        self.addCleanup(self.log_buffer.uninstall)
        logger = logging.getLogger("c64cast")
        old_level, old_prop = logger.level, logger.propagate
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self.addCleanup(
            lambda: (logger.setLevel(old_level), setattr(logger, "propagate", old_prop))
        )
        with self.client() as c:
            with c.websocket_connect("/api/ws", headers=AUTH) as ws:
                ws.receive_json()  # backlog, if any
                logger.info("a line the console should see once")
                seen: list[str] = []
                for _ in range(3):
                    frame = ws.receive_json()
                    seen += [row["message"] for row in frame.get("log", [])]
        self.assertEqual(seen.count("a line the console should see once"), 1)

    def test_a_command_frame_drives_the_supervisor(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            with c.websocket_connect("/api/ws", headers=AUTH) as ws:
                ws.receive_json()
                ws.send_json({"session": "stop"})
                self.assertReaches(SessionState.IDLE)

    def test_a_viewer_may_watch_but_not_command(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            with c.websocket_connect("/api/ws", headers=VIEWER_AUTH) as ws:
                self.assertEqual(ws.receive_json()["role"], "viewer")
                ws.send_json({"session": "stop"})
                # Two more frames means the loop consumed the command frame:
                # the push only comes back round after the receive returns.
                ws.receive_json()
                ws.receive_json()
        self.assertEqual(self.manager.state, SessionState.RUNNING)


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class EveryApiRouteIsProtectedTest(WebApiTestCase):
    def test_no_token_no_api(self):
        from c64cast.control.auth import PUBLIC_PATHS

        app = self.app()
        with TestClient(app) as c:
            for route in app.routes:
                path = getattr(route, "path", "")
                if not path.startswith("/api/") or path in PUBLIC_PATHS:
                    continue
                methods = getattr(route, "methods", None)
                if methods is None:  # the WebSocket route
                    with self.assertRaises(WebSocketDisconnect, msg=f"{path} accepted a socket"):
                        with c.websocket_connect(path):
                            pass
                    continue
                for method in sorted(methods - {"HEAD", "OPTIONS"}):
                    r = c.request(method, path)
                    self.assertEqual(r.status_code, 401, f"{method} {path} was not gated")

    def test_a_viewer_cannot_start_a_show(self):
        with self.client() as c:
            self.assertEqual(c.post("/api/session/start", headers=VIEWER_AUTH).status_code, 403)
            self.assertEqual(c.get("/api/session", headers=VIEWER_AUTH).status_code, 200)
        self.assertEqual(self.build.calls, 0)


class TokenResolutionTest(unittest.TestCase):
    """No HTTP: the credential precedence itself."""

    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": str(self.tmp)}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("C64CAST_WEB_TOKEN", "C64CAST_WEB_VIEWER_TOKEN"):
            os.environ.pop(var, None)

    def test_the_env_var_wins_over_the_config(self):
        cfg = cfgmod.WebCfg(token="from-config", viewer_token="viewer-config")
        with mock.patch.dict(
            os.environ,
            {"C64CAST_WEB_TOKEN": "from-env", "C64CAST_WEB_VIEWER_TOKEN": "viewer-env"},
        ):
            self.assertEqual(serve.resolve_tokens(cfg), ("from-env", "viewer-env"))

    def test_a_token_file_is_read_and_stripped(self):
        path = self.tmp / "secret"
        path.write_text("  file-token \n", encoding="utf-8")
        cfg = cfgmod.WebCfg(token_file=str(path))
        self.assertEqual(serve.resolve_tokens(cfg)[0], "file-token")

    def test_an_unreadable_token_file_is_fatal_rather_than_silently_open(self):
        cfg = cfgmod.WebCfg(token_file=str(self.tmp / "missing"))
        with self.assertRaises(RuntimeError):
            serve.resolve_tokens(cfg)
        cfg = cfgmod.WebCfg(token_file=str(self.tmp / "empty"))
        (self.tmp / "empty").write_text("\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            serve.resolve_tokens(cfg)

    def test_an_unconfigured_host_generates_and_persists_a_token(self):
        from c64cast.app import paths

        first, viewer = serve.resolve_tokens(cfgmod.WebCfg())
        self.assertTrue(first)
        self.assertEqual(viewer, "")
        stored = paths.web_token_path()
        self.assertEqual(stored.read_text(encoding="utf-8").strip(), first)
        if os.name != "nt":
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        # Stable across restarts: a bookmarked console URL keeps working.
        self.assertEqual(serve.resolve_tokens(cfgmod.WebCfg())[0], first)


class RequestFactoryTest(unittest.TestCase):
    def test_the_factory_reloads_and_validates_on_every_call(self):
        loads = 0

        def load():
            nonlocal loads
            loads += 1
            req = _request("a")
            return req.loaded, req.cfgs

        args = argparse.Namespace(overwrite=False)
        factory = serve.make_request_factory(args, load, config_path="show.toml")
        with mock.patch.object(serve, "validate_configs") as validate:
            first = factory()
            factory()
        self.assertEqual(loads, 2)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(first.config_path, "show.toml")

    def test_a_validation_failure_reaches_the_caller(self):
        def load():
            req = _request("a")
            return req.loaded, req.cfgs

        factory = serve.make_request_factory(argparse.Namespace(overwrite=False), load)
        with mock.patch.object(
            serve, "validate_configs", side_effect=session.SessionConfigError(5)
        ):
            with self.assertRaises(session.SessionConfigError):
                factory()


if __name__ == "__main__":
    unittest.main()
