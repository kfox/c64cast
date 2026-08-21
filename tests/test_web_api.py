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

from _fakes import MachineSettingsIsolation, fake_system_stack

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
from c64cast.app import config_store, console_library, media_store, serve, session
from c64cast.app.serve import SessionState
from c64cast.control import web_api
from c64cast.control.transport import LiveTuneTracker

TOKEN = "full-token-value"
VIEWER = "viewer-token-value"
AUTH = {"X-C64Cast-Token": TOKEN}
VIEWER_AUTH = {"X-C64Cast-Token": VIEWER}

# A config the loader accepts, so the browser routes exercise the real
# validate-before-write path rather than a patched one.
GIG_TOML = (
    '[audio]\nenabled = false\n\n[color]\ndither = "atkinson"\n\n'
    '[[scenes]]\ntype = "blank"\nduration_s = 5.0\n'
)
# Two scenes of a type that accepts `palette_mode` — a knob whose config home is
# the scene's own block, so a save-back has to reach one of these and not the
# other. (A `blank` scene has no display mode to tune, and the store refuses a
# field the scene's type doesn't declare.)
PAIR_TOML = (
    "[audio]\nenabled = false\n\n"
    '[[scenes]]\ntype = "generative"\nsource = "plasma"\ndisplay = "mhires"\nduration_s = 5.0\n\n'
    '[[scenes]]\ntype = "generative"\nsource = "plasma"\ndisplay = "mhires"\nduration_s = 5.0\n'
)
# Same pair, but scene 1 carries its own [scenes.color] override for a
# `_MODE_FIELD_TO_COLOR` field — the fixture for the scene-aware save-back
# tests, where a [color]-homed knob has to land in ONE scene's own block.
PAIR_TOML_ONE_OVERRIDES_COLOR = (
    "[audio]\nenabled = false\n\n"
    '[[scenes]]\ntype = "generative"\nsource = "plasma"\ndisplay = "mhires"\nduration_s = 5.0\n\n'
    '[[scenes]]\ntype = "generative"\nsource = "plasma"\ndisplay = "mhires"\nduration_s = 5.0\n'
    "  [scenes.color]\n  dither_strength = 0.1\n"
)
# Refused by `validate_configs`, not by the TOML parser — audio off so the
# audio check (which runs first) can't be what fails instead.
BAD_TOML = '[audio]\nenabled = false\n\n[color]\ndither = "nonsense"\n'

# Long enough for a loaded CI box, short enough that a stuck transition fails
# the run rather than hanging it.
WAIT = 5.0

# The store composes a saved config against the machine-settings layer, so a
# developer's own ~/.config/c64cast/settings.toml would otherwise decide what
# these writes put in the file.
_iso = MachineSettingsIsolation()


def setUpModule() -> None:
    _iso.start()


def tearDownModule() -> None:
    _iso.stop()


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


class _FakeScreenProfile:
    """Just enough profile for the screen routes' capability check."""

    name = "Ultimate 64"

    def __init__(self, streams: bool = True) -> None:
        self.supports_video_stream = streams


class _FakeReceiver:
    """A machine that streams one unchanging frame, so a route test can assert
    a picture came back without a socket or a real C64."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def latest(self) -> Any:
        import numpy as np

        from c64cast.hw.vic_stream import VicFrame

        return VicFrame(np.full((8, 16), 6, dtype=np.uint8), 1, 0.0)


class _FakeApi:
    stats = {"writes": 1}

    def __init__(self, streams: bool = True) -> None:
        self.profile = _FakeScreenProfile(streams)
        self.receiver = _FakeReceiver()

    def format_write_latency(self) -> str:
        return "lat 5ms"

    def open_video_stream(self) -> _FakeReceiver:
        return self.receiver


class _FakePlaylist:
    """JSON-serializable stand-in: the state feed carries the `/perf` payload,
    so a MagicMock playlist would only fail once it reached the encoder."""

    def __init__(self, config_path: str = "") -> None:
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
        # The real tracker — the save-back route reads and clears it, so a fake
        # would be testing the fake.
        self.live_tracker = LiveTuneTracker()
        self.config_path = config_path
        # The real Playlist always has one; a save-back brings it up to what it
        # just wrote, so None here is "this run was built without a Config".
        self.config: Any = None


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
        st.playlist = _FakePlaylist(config_path=req.config_path)
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
    can hold the supervisor in `starting` and drive the routes against it.

    `entered` fires as the build begins. A gated test needs it: the route
    answers as soon as the supervisor flips to `starting`, which is before the
    worker thread has reached the build at all, so `calls` is racing that
    thread until this is set."""

    def __init__(self) -> None:
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.calls = 0

    def __call__(self, req, generation, publish):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(WAIT)
        sess = _session(req)
        publish(sess)
        return sess


class _Factory:
    """The request factory the routes hold: hands back a request, or raises
    the way a config that doesn't validate would. Records the path it was asked
    for, which is how the start-by-ref tests see what the store resolved."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0
        self.paths: list[str | None] = []

    def __call__(self, path: str | None = None) -> serve.StartRequest:
        self.calls += 1
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        return _request("a", config_path=path or "show.toml")


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class WebApiTestCase(unittest.TestCase):
    """Every case below drives the assembled app, so the skip lives here rather
    than on each subclass: `unittest` propagates it, and a class inserted
    between a decorator and the one it was meant for is exactly how this file
    once shipped an unguarded test."""

    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.marker = Path(tmp.name) / "run.json"
        self.root = Path(tmp.name).resolve() / "shows"
        self.root.mkdir()
        (self.root / "gig.toml").write_text(GIG_TOML, encoding="utf-8")
        # `include_examples=False`: this fixture is about the routes over one
        # configured root. `ExamplesRouteTest` below builds its own store with
        # the packaged examples root left in.
        self.store = config_store.ConfigStore([str(self.root)], include_examples=False)
        self.library = console_library.ConsoleLibrary(Path(tmp.name) / "console.json")
        # An explicit write table rather than the default four asset dirs:
        # several tests in this module `chdir` to a directory with no
        # `assets/` in it (on purpose — see SceneStructureRouteTest), and an
        # unset kind would otherwise fall back to a default resolved against
        # whatever the process cwd happens to be. `video` writes (and
        # browses) `self.root`, matching what `MediaBrowserTest` already
        # expects to find there; the rest are turned off outright so a test
        # picking another extension exercises the "not configured" refusal
        # rather than quietly finding a real directory.
        self.media = media_store.MediaStore(
            read_write={"video": str(self.root), "sid": "", "picture": "", "program": ""}
        )
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
        kwargs.setdefault("store", self.store)
        kwargs.setdefault("library", self.library)
        kwargs.setdefault("media", self.media)
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
            self.assertTrue(self.build.entered.wait(WAIT), "the build never started")
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

    def test_a_config_that_does_not_validate_names_the_reason_in_the_422(self):
        # SessionConfigError's detail is the same diagnostic validate_configs
        # already logged — carried here instead of the caller having to go
        # read the log for it.
        self.factory.error = session.SessionConfigError(3, "scene outro: no such file")
        with self.client() as c:
            r = c.post("/api/session/start", headers=AUTH)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"], "scene outro: no such file")

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

    def test_config_ref_is_none_when_idle(self):
        with self.client() as c:
            body = c.get("/api/session", headers=AUTH).json()
        self.assertIsNone(body["config_ref"])

    def test_config_ref_names_the_config_the_browser_should_preselect(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH, json={"config": "shows/gig.toml"})
            self.assertReaches(SessionState.RUNNING)
            body = c.get("/api/session", headers=AUTH).json()
        self.assertEqual(body["config_ref"], "shows/gig.toml")

    def test_config_ref_is_none_for_a_path_outside_any_root(self):
        # `_request`'s default `config_path="show.toml"` is a bare name under no
        # configured root — a quick-playback run looks the same way.
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            body = c.get("/api/session", headers=AUTH).json()
        self.assertIsNone(body["config_ref"])

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


class ConfigBrowserTest(WebApiTestCase):
    """The jail itself is tested in tests/test_config_store.py; what these add
    is that each of its refusals reaches the caller as a distinguishable status
    rather than a 500."""

    def test_the_listing_names_the_roots_and_their_configs(self):
        with self.client() as c:
            body = c.get("/api/configs", headers=AUTH).json()
        self.assertEqual([r["label"] for r in body["roots"]], ["shows"])
        self.assertEqual([f["path"] for f in body["files"]], ["shows/gig.toml"])

    def test_a_read_carries_the_text_and_the_form(self):
        with self.client() as c:
            r = c.get("/api/configs/shows/gig.toml", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], GIG_TOML)
        self.assertTrue(r.json()["form"]["sections"])

    def test_a_ref_that_leaves_its_root_is_forbidden(self):
        # The traversal is percent-encoded because an HTTP client collapses a
        # literal `..` in the URL before it is ever sent — the encoded form is
        # the one that actually reaches the route.
        refs = ("shows/%2e%2e/%2e%2e/etc/passwd.toml", "elsewhere/x.toml", "shows/notes.txt")
        with self.client() as c:
            for ref in refs:
                r = c.get(f"/api/configs/{ref}", headers=AUTH)
                self.assertEqual(r.status_code, 403, f"{ref} was not refused")

    def test_a_missing_config_is_a_404(self):
        with self.client() as c:
            self.assertEqual(c.get("/api/configs/shows/nope.toml", headers=AUTH).status_code, 404)

    def test_a_write_validates_first_and_replaces_the_file(self):
        text = GIG_TOML.replace("atkinson", "ordered")
        with self.client() as c:
            r = c.put("/api/configs/shows/gig.toml", headers=AUTH, json={"text": text})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), text)

    def test_a_write_that_does_not_validate_is_a_422_carrying_the_reason(self):
        with self.client() as c:
            r = c.put(
                "/api/configs/shows/gig.toml",
                headers=AUTH,
                json={"text": BAD_TOML},
            )
        self.assertEqual(r.status_code, 422)
        self.assertIn("dither", r.json()["detail"]["error"])
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)

    def test_validate_reports_without_touching_the_file(self):
        with self.client() as c:
            r = c.post(
                "/api/configs/shows/gig.toml/validate",
                headers=AUTH,
                json={"text": BAD_TOML},
            )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)

    def test_validate_with_no_text_checks_the_file_on_disk(self):
        # An absent "text" key used to validate an empty string and answer
        # about a config nobody submitted — this is the console's pre-flight
        # before a start, so it has to be the file that would actually run.
        with self.client() as c:
            r = c.post("/api/configs/shows/gig.toml/validate", headers=AUTH, json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["diagnostics"])
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)

    def test_a_viewer_may_read_but_not_write(self):
        with self.client() as c:
            self.assertEqual(c.get("/api/configs", headers=VIEWER_AUTH).status_code, 200)
            r = c.put("/api/configs/shows/gig.toml", headers=VIEWER_AUTH, json={"text": GIG_TOML})
        self.assertEqual(r.status_code, 403)

    def test_a_viewer_cannot_validate(self):
        with self.client() as c:
            r = c.post("/api/configs/shows/gig.toml/validate", headers=VIEWER_AUTH, json={})
        self.assertEqual(r.status_code, 403)


class MediaBrowserTest(WebApiTestCase):
    """`/api/media` — the jail and the listing semantics are covered by
    tests/test_media_store.py; what these add is the route's own mapping onto
    HTTP, same split as `ConfigBrowserTest`."""

    def setUp(self) -> None:
        super().setUp()
        (self.root / "clip.mp4").write_bytes(b"")

    def test_a_listing_names_the_kind_and_its_entries(self):
        with self.client() as c:
            body = c.get("/api/media", headers=AUTH, params={"kind": "video"}).json()
        self.assertEqual(body["kind"], "video")
        # The root here is configured by its absolute path (same as `self.store`
        # above), so a listed spec is that absolute path too — media_store.py's
        # specs are built from the root exactly as configured, with the
        # relative part always joined by "/" regardless of platform (unlike
        # `str(self.root / "clip.mp4")`, which normalizes to native separators).
        self.assertIn(f"{self.root}/clip.mp4", [e["spec"] for e in body["entries"]])

    def test_an_unknown_kind_is_a_400_not_a_500(self):
        with self.client() as c:
            r = c.get("/api/media", headers=AUTH, params={"kind": "subtitle"})
        self.assertEqual(r.status_code, 400)

    def test_a_viewer_may_browse_it(self):
        with self.client() as c:
            r = c.get("/api/media", headers=VIEWER_AUTH, params={"kind": "video"})
        self.assertEqual(r.status_code, 200)


class MediaUploadTest(WebApiTestCase):
    """`PUT /api/media/{name}` — the route's own mapping onto HTTP (status
    codes, who may call it); `destination`'s policy and `receive`'s streamed
    commit are covered by tests/test_media_store.py. `setUp`'s write table
    (see `WebApiTestCase.setUp`) writes `video` to `self.root` and turns
    `sid`/`picture`/`program` off outright, which is what makes an
    unconfigured-kind upload here a plain "not this host" refusal rather than
    a real directory it happened to find."""

    def test_a_put_writes_and_answers_the_spec(self):
        with self.client() as c:
            r = c.put("/api/media/clip2.mp4", headers=AUTH, content=b"hello")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["kind"], "video")
        self.assertEqual(body["name"], "clip2.mp4")
        self.assertEqual(body["bytes"], 5)
        self.assertFalse(body["renamed"])
        self.assertEqual(body["spec"], f"{self.root}/clip2.mp4")
        self.assertEqual((self.root / "clip2.mp4").read_bytes(), b"hello")

    def test_a_name_already_taken_is_renamed_not_overwritten(self):
        (self.root / "clip2.mp4").write_bytes(b"original")
        with self.client() as c:
            r = c.put("/api/media/clip2.mp4", headers=AUTH, content=b"new")
        body = r.json()
        self.assertEqual(body["name"], "clip2-2.mp4")
        self.assertTrue(body["renamed"])
        self.assertEqual((self.root / "clip2.mp4").read_bytes(), b"original")
        self.assertEqual((self.root / "clip2-2.mp4").read_bytes(), b"new")

    def test_a_viewer_may_not_upload(self):
        with self.client() as c:
            r = c.put("/api/media/clip2.mp4", headers=VIEWER_AUTH, content=b"hello")
        self.assertEqual(r.status_code, 403)
        self.assertFalse((self.root / "clip2.mp4").exists())

    def test_a_bad_extension_is_a_400(self):
        with self.client() as c:
            r = c.put("/api/media/notes.txt", headers=AUTH, content=b"hello")
        self.assertEqual(r.status_code, 400)

    def test_an_unconfigured_kind_is_a_403(self):
        with self.client() as c:
            r = c.put("/api/media/photo.png", headers=AUTH, content=b"hello")
        self.assertEqual(r.status_code, 403)

    def test_an_oversized_upload_is_a_413(self):
        with mock.patch.object(media_store, "MAX_UPLOAD_BYTES", 4):
            with self.client() as c:
                r = c.put("/api/media/big.mp4", headers=AUTH, content=b"way too big")
        self.assertEqual(r.status_code, 413)
        self.assertFalse((self.root / "big.mp4").exists())

    def test_a_body_that_ends_early_leaves_no_part_file_and_no_target(self):
        # The client side of a cancel: `request.stream()` raising is what
        # drives `MediaStore.receive`'s own `except BaseException` branch
        # (covered at the store level in tests/test_media_store.py), so this
        # is the route's half — nothing lands on disk either way.
        def cut_short():
            yield b"partial"
            raise RuntimeError("client vanished mid-upload")

        with self.client() as c:
            with self.assertRaises(RuntimeError):
                c.put("/api/media/cut.mp4", headers=AUTH, content=cut_short())
        self.assertFalse((self.root / "cut.mp4").exists())
        self.assertEqual(list(self.root.glob("*.part")), [])


class LibraryRouteTest(WebApiTestCase):
    """`/api/library*` — favorites and recents."""

    def test_library_starts_empty(self):
        with self.client() as c:
            body = c.get("/api/library", headers=AUTH).json()
        self.assertEqual(body, {"favorites": [], "recents": []})

    def test_favoriting_a_ref_is_reflected_back(self):
        with self.client() as c:
            r = c.post(
                "/api/library/favorites", headers=AUTH, json={"ref": "shows/gig.toml", "on": True}
            )
            self.assertEqual(r.json()["favorites"], ["shows/gig.toml"])
            body = c.get("/api/library", headers=AUTH).json()
        self.assertEqual(body["favorites"], ["shows/gig.toml"])

    def test_unfavoriting_removes_it(self):
        with self.client() as c:
            c.post(
                "/api/library/favorites", headers=AUTH, json={"ref": "shows/gig.toml", "on": True}
            )
            r = c.post(
                "/api/library/favorites", headers=AUTH, json={"ref": "shows/gig.toml", "on": False}
            )
        self.assertEqual(r.json()["favorites"], [])

    def test_a_favorite_needs_a_ref(self):
        with self.client() as c:
            r = c.post("/api/library/favorites", headers=AUTH, json={"on": True})
        self.assertEqual(r.status_code, 400)

    def test_starting_a_named_config_records_a_recent(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH, json={"config": "shows/gig.toml"})
            self.assertReaches(SessionState.RUNNING)
            body = c.get("/api/library", headers=AUTH).json()
        self.assertEqual([r["ref"] for r in body["recents"]], ["shows/gig.toml"])

    def test_starting_with_no_config_records_nothing(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING)
            body = c.get("/api/library", headers=AUTH).json()
        self.assertEqual(body["recents"], [])

    def test_switching_to_a_named_config_records_a_recent_too(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING, generation=1)
            c.post("/api/session/switch", headers=AUTH, json={"config": "shows/gig.toml"})
            self.assertReaches(SessionState.RUNNING, generation=2)
            body = c.get("/api/library", headers=AUTH).json()
        self.assertEqual([r["ref"] for r in body["recents"]], ["shows/gig.toml"])

    def test_a_viewer_may_read_the_library_but_not_favorite(self):
        with self.client() as c:
            self.assertEqual(c.get("/api/library", headers=VIEWER_AUTH).status_code, 200)
            r = c.post(
                "/api/library/favorites", headers=VIEWER_AUTH, json={"ref": "shows/gig.toml"}
            )
        self.assertEqual(r.status_code, 403)


class ConfigCreateDeleteRouteTest(WebApiTestCase):
    """`POST /api/configs` (create) and `DELETE /api/configs/{ref}`."""

    def test_a_blank_config_is_created(self):
        with self.client() as c:
            r = c.post("/api/configs", headers=AUTH, json={"path": "shows/new.toml"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue((self.root / "new.toml").exists())

    def test_creating_needs_a_path(self):
        with self.client() as c:
            r = c.post("/api/configs", headers=AUTH, json={})
        self.assertEqual(r.status_code, 400)

    def test_duplicating_an_existing_config(self):
        with self.client() as c:
            r = c.post(
                "/api/configs",
                headers=AUTH,
                json={"path": "shows/copy.toml", "copy_of": "shows/gig.toml"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.root / "copy.toml").read_text(encoding="utf-8"), GIG_TOML)

    def test_creating_over_an_existing_file_is_a_403(self):
        with self.client() as c:
            r = c.post("/api/configs", headers=AUTH, json={"path": "shows/gig.toml"})
        self.assertEqual(r.status_code, 403)

    def test_a_viewer_cannot_create(self):
        with self.client() as c:
            r = c.post("/api/configs", headers=VIEWER_AUTH, json={"path": "shows/new.toml"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse((self.root / "new.toml").exists())

    def test_delete_removes_the_file(self):
        with self.client() as c:
            r = c.delete("/api/configs/shows/gig.toml", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.root / "gig.toml").exists())

    def test_deleting_the_running_config_is_refused(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH, json={"config": "shows/gig.toml"})
            self.assertReaches(SessionState.RUNNING)
            r = c.delete("/api/configs/shows/gig.toml", headers=AUTH)
        self.assertEqual(r.status_code, 409)
        self.assertTrue((self.root / "gig.toml").exists())

    def test_deleting_a_stopped_configs_former_config_is_allowed(self):
        # status().config_path deliberately keeps naming the last-started
        # config after a stop (so the browser has something to preselect at
        # idle) — the delete route must not mistake that leftover pointer for
        # an active session and refuse a config nothing is using anymore.
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH, json={"config": "shows/gig.toml"})
            self.assertReaches(SessionState.RUNNING)
            c.post("/api/session/stop", headers=AUTH)
            self.assertReaches(SessionState.IDLE)
            still_named = c.get("/api/session", headers=AUTH).json()["config_path"]
            self.assertTrue(still_named.endswith("gig.toml"), still_named)
            r = c.delete("/api/configs/shows/gig.toml", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.root / "gig.toml").exists())

    def test_deleting_a_missing_config_is_a_404(self):
        with self.client() as c:
            r = c.delete("/api/configs/shows/nope.toml", headers=AUTH)
        self.assertEqual(r.status_code, 404)

    def test_a_viewer_cannot_delete(self):
        with self.client() as c:
            r = c.delete("/api/configs/shows/gig.toml", headers=VIEWER_AUTH)
        self.assertEqual(r.status_code, 403)
        self.assertTrue((self.root / "gig.toml").exists())


class ExamplesRouteTest(WebApiTestCase):
    """The packaged examples root, over HTTP: listed, readable, and refused on
    every write route the jail itself already refuses (tests/test_config_store.py
    covers the store's own logic; this is the route-level status mapping)."""

    def app(self, **kwargs) -> Any:
        kwargs.setdefault(
            "store", config_store.ConfigStore([str(self.root)], include_examples=True)
        )
        return super().app(**kwargs)

    def _example_ref(self, c) -> str:
        body = c.get("/api/configs", headers=AUTH).json()
        return next(f["path"] for f in body["files"] if f["root"] == "examples")

    def test_examples_are_listed_and_readable(self):
        with self.client() as c:
            ref = self._example_ref(c)
            r = c.get(f"/api/configs/{ref}", headers=AUTH)
        self.assertEqual(r.status_code, 200)

    def test_writing_to_an_example_is_forbidden(self):
        with self.client() as c:
            ref = self._example_ref(c)
            r = c.put(f"/api/configs/{ref}", headers=AUTH, json={"text": GIG_TOML})
        self.assertEqual(r.status_code, 403)

    def test_deleting_an_example_is_forbidden(self):
        with self.client() as c:
            ref = self._example_ref(c)
            r = c.delete(f"/api/configs/{ref}", headers=AUTH)
        self.assertEqual(r.status_code, 403)

    def test_creating_a_config_by_copying_an_example_works(self):
        # Some packaged examples need [audio].enabled for their own feature
        # (mic capture, a soundtrack) regardless of whether this host happens
        # to have the optional `mic` extra installed — irrelevant to a
        # verbatim copy, so stand in for it rather than picking an example
        # that avoids it.
        with mock.patch("c64cast.app.session.AUDIO_AVAILABLE", True):
            with self.client() as c:
                ref = self._example_ref(c)
                r = c.post(
                    "/api/configs",
                    headers=AUTH,
                    json={"path": "shows/from_example.toml", "copy_of": ref},
                )
        self.assertEqual(r.status_code, 200)
        self.assertTrue((self.root / "from_example.toml").exists())


class ConfigFormSaveTest(WebApiTestCase):
    """`PATCH` — the generated form's save. The edit semantics live in
    tests/test_config_store.py; these are the route's own answers."""

    def _patch(self, c, edits, *, headers=AUTH):
        return c.patch("/api/configs/shows/gig.toml", headers=headers, json={"edits": edits})

    def test_a_field_edit_is_composed_into_the_file_by_the_server(self):
        with self.client() as c:
            r = self._patch(c, [{"section": "color", "field": "dither", "value": "ordered"}])
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn('dither = "ordered"', body["text"])
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), body["text"])

    def test_an_edit_that_breaks_the_config_is_a_422_and_the_file_stands(self):
        with self.client() as c:
            r = self._patch(c, [{"section": "color", "field": "dither", "value": "nonsense"}])
        self.assertEqual(r.status_code, 422)
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)

    def test_an_edit_naming_something_that_is_not_a_field_is_a_400(self):
        with self.client() as c:
            r = self._patch(c, [{"section": "color", "field": "nope", "value": 1}])
        self.assertEqual(r.status_code, 400)

    def test_a_patch_with_no_edits_list_is_a_400(self):
        with self.client() as c:
            r = c.patch("/api/configs/shows/gig.toml", headers=AUTH, json={"text": GIG_TOML})
        self.assertEqual(r.status_code, 400)

    def test_a_ref_that_leaves_its_root_is_forbidden_here_too(self):
        with self.client() as c:
            r = c.patch(
                "/api/configs/shows/%2e%2e/%2e%2e/etc/passwd.toml",
                headers=AUTH,
                json={"edits": []},
            )
        self.assertEqual(r.status_code, 403)

    def test_a_viewer_cannot_save_the_form(self):
        with self.client() as c:
            r = self._patch(
                c,
                [{"section": "color", "field": "dither", "value": "ordered"}],
                headers=VIEWER_AUTH,
            )
        self.assertEqual(r.status_code, 403)
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)


class SceneStructureRouteTest(WebApiTestCase):
    """Adding and removing scenes. The semantics live in
    tests/test_config_store.py; these are the route's own answers, and the one
    thing only a route can get wrong — `/scenes` being swallowed by the
    catch-all `{ref:path}` that sits beside it.

    Runs from a directory with no `assets/` in it for the same reason its
    counterpart in tests/test_config_store.py does: a new video scene names no
    file, and the project's own populated `assets/videos` under the working
    directory is what makes this pass on a developer's machine and nowhere
    else."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root.parent)

    def test_a_scene_is_added_and_the_file_is_written(self):
        with self.client() as c:
            r = c.post("/api/configs/shows/gig.toml/scenes", headers=AUTH, json={"type": "video"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scene"]["added"], 1)
        self.assertIn('type = "video"', (self.root / "gig.toml").read_text(encoding="utf-8"))

    def test_a_scene_is_copied(self):
        with self.client() as c:
            r = c.post(
                "/api/configs/shows/gig.toml/scenes", headers=AUTH, json={"copy": 0, "after": 0}
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scene"]["copied_from"], 0)

    def test_a_scene_is_removed(self):
        with self.client() as c:
            c.post("/api/configs/shows/gig.toml/scenes", headers=AUTH, json={"type": "video"})
            r = c.delete("/api/configs/shows/gig.toml/scenes/0", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scene"]["removed"], 0)

    def test_removing_the_only_scene_is_a_400_not_a_broken_config(self):
        with self.client() as c:
            r = c.delete("/api/configs/shows/gig.toml/scenes/0", headers=AUTH)
        self.assertEqual(r.status_code, 400)
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)

    def test_a_scene_is_moved(self):
        with self.client() as c:
            c.post("/api/configs/shows/gig.toml/scenes", headers=AUTH, json={"type": "video"})
            r = c.patch("/api/configs/shows/gig.toml/scenes/1", headers=AUTH, json={"to": 0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scene"], {"moved": 1, "to": 0, "type": "video", "name": None})
        text = (self.root / "gig.toml").read_text(encoding="utf-8")
        self.assertLess(text.index('type = "video"'), text.index('type = "blank"'))

    def test_the_move_route_is_not_swallowed_by_the_field_patch_route(self):
        # `{ref:path}` is greedy: the move route has to be registered before
        # the bare field-patch PATCH, or this would be read as a request to
        # patch a file named "…/scenes/0" and 400 for a missing "edits" list.
        with self.client() as c:
            r = c.patch("/api/configs/shows/gig.toml/scenes/0", headers=AUTH, json={"to": 0})
        self.assertEqual(r.status_code, 200)
        self.assertIn("moved", r.json()["scene"])

    def test_a_move_with_no_to_index_is_a_400(self):
        with self.client() as c:
            r = c.patch("/api/configs/shows/gig.toml/scenes/0", headers=AUTH, json={})
        self.assertEqual(r.status_code, 400)

    def test_a_to_index_that_is_not_an_index_is_a_400(self):
        # `true` is an `int` in Python and would otherwise read as scene 1.
        with self.client() as c:
            r = c.patch("/api/configs/shows/gig.toml/scenes/0", headers=AUTH, json={"to": True})
        self.assertEqual(r.status_code, 400)

    def test_moving_an_out_of_range_index_is_a_400_not_a_broken_config(self):
        with self.client() as c:
            r = c.patch("/api/configs/shows/gig.toml/scenes/5", headers=AUTH, json={"to": 0})
        self.assertEqual(r.status_code, 400)
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)

    def test_a_copy_index_that_is_not_an_index_is_a_400(self):
        # `true` is an `int` in Python and would otherwise read as scene 1.
        with self.client() as c:
            r = c.post("/api/configs/shows/gig.toml/scenes", headers=AUTH, json={"copy": True})
        self.assertEqual(r.status_code, 400)

    def test_a_viewer_can_change_neither(self):
        with self.client() as c:
            add = c.post(
                "/api/configs/shows/gig.toml/scenes", headers=VIEWER_AUTH, json={"type": "video"}
            )
            drop = c.delete("/api/configs/shows/gig.toml/scenes/0", headers=VIEWER_AUTH)
            move = c.patch(
                "/api/configs/shows/gig.toml/scenes/0", headers=VIEWER_AUTH, json={"to": 0}
            )
        self.assertEqual(add.status_code, 403)
        self.assertEqual(drop.status_code, 403)
        self.assertEqual(move.status_code, 403)
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)


class ScreenRouteTest(WebApiTestCase):
    """The C64's screen. All three routes are GETs so the read-only role can
    watch; the stream's own lifetime is tested in tests/test_screen.py."""

    def _api(self) -> Any:
        sess = self.manager.session
        assert sess is not None
        return sess.stacks[0].playlist.api

    def _running(self, c) -> None:
        c.post("/api/session/start", headers=AUTH)
        self.assertReaches(SessionState.RUNNING)

    def test_nothing_running_answers_a_501_rather_than_a_blank_picture(self):
        with self.client() as c:
            r = c.get("/api/screen.png", headers=AUTH)
        self.assertEqual(r.status_code, 501)
        self.assertIn("nothing is running", r.json()["detail"])

    def test_availability_starts_nothing(self):
        with self.client() as c:
            self._running(c)
            r = c.get("/api/screen", headers=AUTH)
            self.assertEqual(r.json()["systems"], {"a": True})
            self.assertEqual(self._api().receiver.started, 0)

    def test_a_still_comes_back_as_a_png(self):
        with self.client() as c:
            self._running(c)
            r = c.get("/api/screen.png", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(r.content[:8], b"\x89PNG\r\n\x1a\n")
        # A still is a *now*; a cached one would show the screen from before
        # the change that was made to look at it.
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_a_machine_without_a_vic_of_its_own_says_which(self):
        with self.client() as c:
            self._running(c)
            self._api().profile.supports_video_stream = False
            r = c.get("/api/screen.png", headers=AUTH)
        self.assertEqual(r.status_code, 501)
        self.assertIn("no video stream of its own", r.json()["detail"])
        self.assertEqual(c.get("/api/screen", headers=AUTH).json()["systems"], {"a": False})

    def test_a_viewer_may_watch(self):
        # The point of a read-only link is seeing the show. A GET with a side
        # effect on the machine is the accepted trade — it changes nothing
        # about what the C64 is doing.
        with self.client() as c:
            self._running(c)
            r = c.get("/api/screen.png", headers=VIEWER_AUTH)
        self.assertEqual(r.status_code, 200)

    def test_the_off_switch_is_answered_rather_than_unrouted(self):
        # A console asking a host with the picture turned off should hear that,
        # not a 404 it would read as an older host.
        with self.client(screen_fps=0.0) as c:
            self._running(c)
            self.assertEqual(c.get("/api/screen", headers=AUTH).json(), {"systems": {}, "fps": 0.0})
            r = c.get("/api/screen.png", headers=AUTH)
        self.assertEqual(r.status_code, 501)
        self.assertIn("turned off", r.json()["detail"])

    def test_the_stream_route_refuses_before_it_opens_anything(self):
        # The 501 checks above go through the same helper the stream route
        # uses, and this is the one that proves the *stream* route consults it
        # rather than answering 200 and then failing inside the body — where a
        # browser would see a broken image and no reason.
        with self.client() as c:
            r = c.get("/api/screen/stream", headers=AUTH)
        self.assertEqual(r.status_code, 501)

    def test_the_streaming_adapter_stops_when_the_client_goes(self):
        """`_until_gone` is where a departed client is noticed, and it cannot be
        driven through TestClient — its transport never delivers the ASGI
        `http.disconnect` that a real server sends, so a route-level test of
        this would hang rather than fail. Driven directly instead."""
        import asyncio

        parts: list[bytes] = []
        closed: list[bool] = []

        def source():
            try:
                for i in range(1000):
                    yield f"part{i}".encode()
            finally:
                closed.append(True)

        class _Gone:
            def __init__(self) -> None:
                self.asked = 0

            async def is_disconnected(self) -> bool:
                self.asked += 1
                return self.asked > 3

        async def drive() -> None:
            request = _Gone()
            async for part in web_api._until_gone(source(), request):
                parts.append(part)

        asyncio.run(drive())
        # Three checks passed, three parts; the fourth check ended it — and the
        # generator was closed, which is what releases the machine's stream.
        self.assertEqual(parts, [b"part0", b"part1", b"part2"])
        self.assertEqual(closed, [True])

    def test_the_adapter_closes_the_generator_even_when_it_runs_out(self):
        import asyncio

        closed: list[bool] = []

        def source():
            try:
                yield b"only"
            finally:
                closed.append(True)

        class _Here:
            async def is_disconnected(self) -> bool:
                return False

        async def drive() -> list[bytes]:
            return [part async for part in web_api._until_gone(source(), _Here())]

        self.assertEqual(asyncio.run(drive()), [b"only"])
        self.assertEqual(closed, [True])


class ViewerLinkRouteTest(WebApiTestCase):
    """`POST /api/viewer-link` — the read-only link to hand somebody.

    A `POST` for a reason worth a test: the gate lets a viewer token through
    every `GET`, so a read-only guest could otherwise fetch the link that made
    them one."""

    def _cred(self):
        from c64cast.control.auth import ViewerCredential

        return ViewerCredential(VIEWER)

    def test_it_answers_an_origin_relative_login_path(self):
        with self.client(viewer_token=self._cred()) as c:
            r = c.post("/api/viewer-link", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["token"], VIEWER)
        self.assertFalse(body["minted"])
        # A path, not a URL: the host may be bound to 0.0.0.0 and cannot know
        # which of its addresses the browser asking actually reached it on.
        self.assertTrue(body["path"].startswith("/api/login?token="))
        self.assertNotIn("http", body["path"])

    def test_the_first_ask_mints_one(self):
        cred = self._cred_empty()
        with self.client(viewer_token=cred) as c:
            first = c.post("/api/viewer-link", headers=AUTH).json()
            second = c.post("/api/viewer-link", headers=AUTH).json()
        self.assertTrue(first["minted"])
        self.assertFalse(second["minted"])
        self.assertEqual(first["token"], second["token"])
        self.assertEqual(cred.token, first["token"])

    def _cred_empty(self):
        from c64cast.control.auth import ViewerCredential

        return ViewerCredential()

    def test_a_viewer_cannot_ask_for_it(self):
        with self.client(viewer_token=self._cred()) as c:
            r = c.post("/api/viewer-link", headers=VIEWER_AUTH)
        self.assertEqual(r.status_code, 403)

    def test_a_host_built_without_one_says_so_rather_than_pretending(self):
        with self.client(viewer_token=VIEWER) as c:
            r = c.post("/api/viewer-link", headers=AUTH)
        self.assertEqual(r.status_code, 501)


class LiveTuneSaveBackTest(WebApiTestCase):
    """`POST /api/session/live-tune` — the offer a daemon has no exit to make.

    A one-shot run prompts on the terminal at teardown; the host tears sessions
    down with `save_live_tune=False` and must, so this route is where a knob
    turned from a phone reaches the file it was tuned against."""

    def _running(self, c) -> Any:
        """Start the gig config and hand back its (fake) playlist, whose
        `config_path` is the absolute path the store resolved the ref to."""
        c.post("/api/session/start", headers=AUTH, json={"config": "shows/gig.toml"})
        self.assertReaches(SessionState.RUNNING)
        return self.manager.session.stacks[0].playlist

    def test_a_tuned_color_knob_lands_in_the_running_config(self):
        with self.client() as c:
            pl = self._running(c)
            pl.live_tracker.record("mode.dither_strength", 0.5, 0.8)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["saved"], ["mode.dither_strength"])
        self.assertIn("dither_strength = 0.8", (self.root / "gig.toml").read_text(encoding="utf-8"))
        # …and the offer is withdrawn, because it has been taken.
        self.assertFalse(pl.live_tracker.has_changes())

    def test_the_rest_of_the_file_survives_the_save(self):
        # The write is a PATCH of the file on disk, not a dump of the config the
        # run was built from — so a field edited in the console since the show
        # started is still there afterwards.
        with self.client() as c:
            pl = self._running(c)
            c.patch(
                "/api/configs/shows/gig.toml",
                headers=AUTH,
                json={"edits": [{"section": "color", "field": "dither", "value": "ordered"}]},
            )
            pl.live_tracker.record("mode.dither_strength", 0.5, 0.8)
            c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        text = (self.root / "gig.toml").read_text(encoding="utf-8")
        self.assertIn('dither = "ordered"', text)
        self.assertIn("dither_strength = 0.8", text)

    def test_the_running_config_is_brought_up_to_what_the_file_says(self):
        # The C64's own menu save dumps the run's Config wholesale, so a stale
        # one would let somebody at the machine revert this save by accident.
        with self.client() as c:
            pl = self._running(c)
            pl.config = cfgmod.Config()
            pl.live_tracker.record("mode.dither_strength", 0.5, 0.8)
            c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertAlmostEqual(pl.config.color.dither_strength, 0.8)

    def _running_two_scene(self, c) -> Any:
        """Start a config with two scenes whose type accepts `palette_mode`, so a
        per-scene save has a block to land in and a neighbor to leave alone."""
        (self.root / "pair.toml").write_text(PAIR_TOML, encoding="utf-8")
        c.post("/api/session/start", headers=AUTH, json={"config": "shows/pair.toml"})
        self.assertReaches(SessionState.RUNNING)
        return self.manager.session.stacks[0].playlist

    def test_a_tuned_palette_lands_in_the_scene_it_was_tuned_on(self):
        # palette_mode has no [color] home: the save has to reach one [[scenes]]
        # block and leave the other alone.
        with self.client() as c:
            pl = self._running_two_scene(c)
            pl.live_tracker.record("mode.palette_mode", "percell", "vivid", scene=1)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["saved"], ["mode.palette_mode"])
        loaded = cfgmod.load(str(self.root / "pair.toml"))
        self.assertEqual(loaded.scenes[1].palette_mode, "vivid")
        self.assertEqual(loaded.scenes[0].palette_mode, cfgmod.SceneCfg().palette_mode)
        self.assertFalse(pl.live_tracker.has_changes())

    def test_a_shared_knob_and_a_per_scene_one_save_together(self):
        # One patch, one backup, one refusal — the two homes are not two writes.
        with self.client() as c:
            pl = self._running_two_scene(c)
            pl.live_tracker.record("mode.dither_strength", 0.5, 0.8)
            pl.live_tracker.record("mode.palette_mode", "percell", "grayscale", scene=0)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 200)
        loaded = cfgmod.load(str(self.root / "pair.toml"))
        self.assertAlmostEqual(loaded.color.dither_strength, 0.8)
        self.assertEqual(loaded.scenes[0].palette_mode, "grayscale")

    def test_the_running_config_learns_the_scene_it_just_saved(self):
        # Same reason the [color] fields are re-stamped: the C64 menu's own save
        # dumps this object wholesale.
        with self.client() as c:
            pl = self._running_two_scene(c)
            pl.config = cfgmod.load(str(self.root / "pair.toml"))
            pl.live_tracker.record("mode.palette_mode", "percell", "vivid", scene=1)
            c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(pl.config.scenes[1].palette_mode, "vivid")

    def test_a_palette_tuned_on_a_scene_the_config_never_named_is_not_a_save(self):
        # A launched clip or an auto-inserted video is in the show and not in the
        # file, so the row has no block to be written to.
        with self.client() as c:
            pl = self._running_two_scene(c)
            pl.live_tracker.record("mode.palette_mode", "percell", "vivid", scene=None)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual((self.root / "pair.toml").read_text(encoding="utf-8"), PAIR_TOML)
        self.assertTrue(pl.live_tracker.has_changes())

    def test_a_palette_tuned_on_a_scene_that_is_gone_keeps_the_record(self):
        # The store refuses an index the file has no block for, and a refused
        # patch leaves the file and the offer exactly as they were.
        with self.client() as c:
            pl = self._running_two_scene(c)
            pl.live_tracker.record("mode.palette_mode", "percell", "vivid", scene=7)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual((self.root / "pair.toml").read_text(encoding="utf-8"), PAIR_TOML)
        self.assertTrue(pl.live_tracker.has_changes())

    def test_the_same_palette_on_two_scenes_is_two_saves_in_one_write(self):
        with self.client() as c:
            pl = self._running_two_scene(c)
            pl.live_tracker.record("mode.palette_mode", "percell", "vivid", scene=0)
            pl.live_tracker.record("mode.palette_mode", "percell", "cheap", scene=1)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 200)
        loaded = cfgmod.load(str(self.root / "pair.toml"))
        self.assertEqual(
            [s.palette_mode for s in loaded.scenes[:2]],
            ["vivid", "cheap"],
        )

    def test_a_runtime_only_change_is_not_a_save(self):
        with self.client() as c:
            pl = self._running(c)
            pl.live_tracker.record("source.scale", 1.0, 2.0)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("config field behind it", r.json()["detail"])
        self.assertTrue(pl.live_tracker.has_changes())

    def test_a_change_no_config_field_carries_is_left_in_the_record(self):
        with self.client() as c:
            pl = self._running(c)
            pl.live_tracker.record("mode.dither_strength", 0.5, 0.8)
            pl.live_tracker.record("source.scale", 1.0, 2.0)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.json()["kept_out"], ["source.scale"])
        self.assertEqual([row["target"] for row in pl.live_tracker.pending()], ["source.scale"])

    def test_a_save_that_would_break_the_config_keeps_the_record(self):
        with self.client() as c:
            pl = self._running(c)
            pl.live_tracker.record("mode.dither_method", "atkinson", "nonsense")
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)
        # Still offered: the file is untouched, so the Save button still means
        # what it said.
        self.assertTrue(pl.live_tracker.has_changes())

    def test_discard_drops_the_record_and_touches_nothing_else(self):
        with self.client() as c:
            pl = self._running(c)
            pl.live_tracker.record("mode.dither_strength", 0.5, 0.8)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "discard"})
        self.assertEqual(r.json()["discarded"], 1)
        self.assertFalse(pl.live_tracker.has_changes())
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)

    def test_nothing_running_is_a_409_not_a_crash(self):
        with self.client() as c:
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 409)

    def test_an_unknown_system_is_a_404(self):
        with self.client() as c:
            self._running(c)
            r = c.post(
                "/api/session/live-tune", headers=AUTH, json={"action": "save", "system": "nope"}
            )
        self.assertEqual(r.status_code, 404)

    def test_an_unknown_action_is_a_400(self):
        with self.client() as c:
            self._running(c)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "melt"})
        self.assertEqual(r.status_code, 400)

    def test_a_viewer_cannot_save_the_show_it_is_watching(self):
        with self.client() as c:
            pl = self._running(c)
            pl.live_tracker.record("mode.dither_strength", 0.5, 0.8)
            r = c.post("/api/session/live-tune", headers=VIEWER_AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual((self.root / "gig.toml").read_text(encoding="utf-8"), GIG_TOML)

    def _running_color_override(self, c) -> Any:
        """A pair like `_running_two_scene`, but scene 1 already overrides
        `dither_strength` in its own [scenes.color] block.

        `_Factory` (this suite's request factory) builds its request from a
        blank stub `Config`, not from the file on disk — so the fake playlist
        it produces needs its `live_tracker` swapped for one built against the
        real, loaded Config, the same way the real `Playlist.__init__` does,
        for the scene-aware routing under test to have anything to key on."""
        (self.root / "pair_override.toml").write_text(
            PAIR_TOML_ONE_OVERRIDES_COLOR, encoding="utf-8"
        )
        c.post("/api/session/start", headers=AUTH, json={"config": "shows/pair_override.toml"})
        self.assertReaches(SessionState.RUNNING)
        pl = self.manager.session.stacks[0].playlist
        pl.live_tracker = LiveTuneTracker(cfgmod.load(str(self.root / "pair_override.toml")))
        return pl

    def test_a_color_knob_tuned_on_an_overriding_scene_saves_into_its_block(self):
        with self.client() as c:
            pl = self._running_color_override(c)
            pl.live_tracker.record("mode.dither_strength", 0.1, 0.9, scene=1)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["saved"], ["mode.dither_strength"])
        loaded = cfgmod.load(str(self.root / "pair_override.toml"))
        self.assertAlmostEqual(loaded.scenes[1].color["dither_strength"], 0.9)
        self.assertEqual(loaded.color.dither_strength, cfgmod.ColorCfg().dither_strength)

    def test_the_same_knob_on_the_non_overriding_scene_still_saves_globally(self):
        with self.client() as c:
            pl = self._running_color_override(c)
            pl.live_tracker.record("mode.dither_strength", 0.5, 0.8, scene=0)
            r = c.post("/api/session/live-tune", headers=AUTH, json={"action": "save"})
        self.assertEqual(r.status_code, 200)
        loaded = cfgmod.load(str(self.root / "pair_override.toml"))
        self.assertAlmostEqual(loaded.color.dither_strength, 0.8)
        self.assertEqual(loaded.scenes[0].color, {})


class StartByRefTest(WebApiTestCase):
    def test_a_start_with_no_body_runs_what_the_host_was_launched_with(self):
        with self.client() as c:
            self.assertEqual(c.post("/api/session/start", headers=AUTH).status_code, 202)
        self.assertEqual(self.factory.paths, [None])

    def test_a_named_config_reaches_the_factory_as_an_absolute_path(self):
        with self.client() as c:
            r = c.post("/api/session/start", headers=AUTH, json={"config": "shows/gig.toml"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.factory.paths, [str(self.root / "gig.toml")])
        self.assertReaches(SessionState.RUNNING)
        self.assertEqual(self.manager.status().config_path, str(self.root / "gig.toml"))

    def test_a_config_outside_the_roots_never_reaches_the_factory(self):
        with self.client() as c:
            r = c.post("/api/session/start", headers=AUTH, json={"config": "/etc/passwd.toml"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.factory.paths, [])
        self.assertEqual(self.manager.state, SessionState.IDLE)

    def test_switch_takes_a_ref_too(self):
        with self.client() as c:
            c.post("/api/session/start", headers=AUTH)
            self.assertReaches(SessionState.RUNNING, generation=1)
            r = c.post("/api/session/switch", headers=AUTH, json={"config": "shows/gig.toml"})
        self.assertEqual(r.status_code, 202)
        self.assertReaches(SessionState.RUNNING, generation=2)
        self.assertEqual(self.factory.paths, [None, str(self.root / "gig.toml")])

    def test_a_body_that_is_not_json_is_a_400(self):
        with self.client() as c:
            r = c.post("/api/session/start", headers=AUTH, content=b"{nope")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.build.calls, 0)


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

    def test_the_console_itself_is_gated(self):
        # The app shell and its assets sit behind the same token as the API
        # they talk to. A browser reaches them through /api/login, which is why
        # nothing here is in PUBLIC_PATHS.
        with TestClient(self.app()) as c:
            for path in ("/", "/assets/app.js", "/some/client/route"):
                with self.subTest(path=path):
                    self.assertEqual(c.get(path).status_code, 401)

    def test_the_console_is_served_once_authenticated(self):
        with TestClient(self.app()) as c:
            r = c.get("/", headers=AUTH)
            self.assertEqual(r.status_code, 200)
            self.assertIn("/assets/app.js", r.text)

    def test_a_viewer_may_load_the_console(self):
        # Read-only is a role inside the console, not a different console.
        with TestClient(self.app()) as c:
            self.assertEqual(c.get("/", headers=VIEWER_AUTH).status_code, 200)

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
            token, viewer = serve.resolve_tokens(cfg)
        self.assertEqual(token, "from-env")
        self.assertEqual(viewer.token, "viewer-env")

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
        # The read-only one is *not* generated alongside it: nobody asked for a
        # second credential, and one that exists unasked is one more to leak.
        self.assertEqual(viewer.token, "")
        self.assertFalse(viewer)
        self.assertFalse(paths.web_viewer_token_path().exists())
        stored = paths.web_token_path()
        self.assertEqual(stored.read_text(encoding="utf-8").strip(), first)
        if os.name != "nt":
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        # Stable across restarts: a bookmarked console URL keeps working.
        self.assertEqual(serve.resolve_tokens(cfgmod.WebCfg())[0], first)


class RequestFactoryTest(unittest.TestCase):
    def test_the_factory_reloads_and_validates_on_every_call(self):
        loads = 0

        def load(path):
            nonlocal loads
            loads += 1
            req = _request("a")
            return argparse.Namespace(overwrite=False, config=path), req.loaded, req.cfgs

        factory = serve.make_request_factory(load, config_path="show.toml")
        with mock.patch.object(serve, "validate_configs") as validate:
            first = factory(None)
            factory(None)
        self.assertEqual(loads, 2)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(first.config_path, "show.toml")

    def test_a_named_path_reaches_the_loader_and_the_status(self):
        seen: list[str | None] = []

        def load(path):
            seen.append(path)
            req = _request("a")
            return argparse.Namespace(overwrite=False, config=path), req.loaded, req.cfgs

        factory = serve.make_request_factory(load, config_path="launch.toml")
        with mock.patch.object(serve, "validate_configs"):
            req = factory("/shows/other.toml")
        self.assertEqual(seen, ["/shows/other.toml"])
        self.assertEqual(req.config_path, "/shows/other.toml")
        self.assertEqual(req.args.config, "/shows/other.toml")

    def test_a_validation_failure_reaches_the_caller(self):
        def load(path):
            req = _request("a")
            return argparse.Namespace(overwrite=False), req.loaded, req.cfgs

        factory = serve.make_request_factory(load)
        with mock.patch.object(
            serve, "validate_configs", side_effect=session.SessionConfigError(5)
        ):
            with self.assertRaises(session.SessionConfigError):
                factory(None)


if __name__ == "__main__":
    unittest.main()
