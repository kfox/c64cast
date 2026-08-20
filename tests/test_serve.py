"""Tests for the session supervisor — the state machine, no HTTP.

Every build and teardown is injected, which is the whole point of the seam:
the transitions run against fake sessions with no hardware, no sockets and no
sleeping. What is left over for the `hw-visual-verify` skill is everything the
fakes stand in for — a real backend reopened inside the settle window, a camera
reopened after `release()`, cv2 windows opened and closed across sessions in
one process, and reset-on-crash recovery against a machine that actually died
mid-show.

The cooldown tests inject `clock=` rather than patching a `FrozenClock` over
the module's `time`: `wait_for` reads a clock too, so freezing one clock for
both would freeze the test's own waiting alongside the cooldown it is trying to
observe.

Session and SystemStack carry typed fields we stuff MagicMocks into, so
silence pyright's attribute complaints file-wide rather than per assertion."""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
from __future__ import annotations

import argparse
import threading
import unittest
from unittest import mock

from _fakes import fake_system_stack, quiet_logging

from c64cast.app import config as cfgmod
from c64cast.app import serve, session
from c64cast.app.serve import SessionState

# Long enough that a loaded CI box doesn't fail on scheduling, short enough
# that a genuinely stuck transition fails the run instead of hanging it.
WAIT = 5.0


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


def _session(req: serve.StartRequest, *, threads: list[threading.Thread] | None = None):
    return session.Session(
        args=req.args,
        loaded=req.loaded,
        cfgs=req.cfgs,
        stacks=[fake_system_stack(n) for n in req.loaded.names],
        ensemble=None,
        stop_event=threading.Event(),
        profiler=mock.MagicMock(name="profiler"),
        threads=threads or [],
    )


def _blocking_threads(stop_event: threading.Event, n: int = 1) -> list[threading.Thread]:
    """Playlist stand-ins: alive until the session's stop_event is set, which
    is exactly the contract `join_playlists` relies on."""
    threads = [
        threading.Thread(target=stop_event.wait, name=f"fake-playlist-{i}", daemon=True)
        for i in range(n)
    ]
    for t in threads:
        t.start()
    return threads


def _finished_thread() -> threading.Thread:
    """A playlist that has already run out — what the reaper is looking for."""
    t = threading.Thread(target=lambda: None, name="fake-playlist-done", daemon=True)
    t.start()
    t.join()
    return t


class _Build:
    """Records its calls and hands back prepared sessions in order."""

    def __init__(self, *sessions, error: BaseException | None = None, publish: bool = True):
        self.sessions = list(sessions)
        self.error = error
        self.publish = publish
        self.calls: list[tuple[serve.StartRequest, int]] = []
        self.gate: threading.Event | None = None

    def __call__(self, req, generation, publish):
        self.calls.append((req, generation))
        if self.gate is not None:
            self.gate.wait(WAIT)
        sess = self.sessions.pop(0) if self.sessions else _session(req)
        if self.publish:
            publish(sess)
        if self.error is not None:
            raise self.error
        return sess


class _Teardown:
    def __init__(self):
        self.calls: list[object] = []

    def __call__(self, sess):
        self.calls.append(sess)


class _Recorder:
    def __init__(self):
        self.states: list[SessionState] = []
        self.snapshots: list[serve.SessionStatus] = []

    def __call__(self, status):
        self.states.append(status.state)
        self.snapshots.append(status)


class SupervisorTestCase(unittest.TestCase):
    def manager(self, **kwargs) -> serve.SessionManager:
        kwargs.setdefault("settle_s", 0.0)
        kwargs.setdefault("reap_period_s", 0.01)
        kwargs.setdefault("marker_path", self.marker)
        mgr = serve.SessionManager(**kwargs)
        self.addCleanup(mgr.close, timeout=WAIT)
        return mgr

    def setUp(self):
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.marker = Path(tmp.name) / "run.json"

    def assertReaches(
        self, mgr: serve.SessionManager, state: SessionState, *, generation: int | None = None
    ) -> None:
        self.assertTrue(
            mgr.wait_for(state, timeout=WAIT, generation=generation),
            f"still {mgr.state} (generation {mgr.generation}), wanted {state}",
        )


class StartStopTest(SupervisorTestCase):
    def test_start_from_idle_records_starting_then_running(self):
        rec = _Recorder()
        mgr = self.manager(build=_Build(), teardown=_Teardown(), on_transition=rec)
        gen = mgr.start(_request("a"))
        self.assertReaches(mgr, SessionState.RUNNING)
        self.assertEqual(gen, 1)
        self.assertEqual(rec.states, [SessionState.STARTING, SessionState.RUNNING])
        status = mgr.status()
        self.assertEqual(status.generation, 1)
        self.assertEqual(status.systems, ("a",))
        self.assertEqual(status.config_path, "show.toml")
        self.assertIsNone(status.last_error)

    def test_idle_status_names_the_config_the_host_was_launched_with(self):
        # There is no other "host default" concept: before any start,
        # `config_path` (and the browser's `config_ref` built from it) is the
        # only way to say what `--config` named at launch.
        mgr = self.manager(build=_Build(), teardown=_Teardown(), launch_config_path="launch.toml")
        self.assertEqual(mgr.status().config_path, "launch.toml")

    def test_an_explicit_start_overrides_the_launch_default(self):
        mgr = self.manager(build=_Build(), teardown=_Teardown(), launch_config_path="launch.toml")
        mgr.start(_request("a", config_path="chosen.toml"))
        self.assertReaches(mgr, SessionState.RUNNING)
        self.assertEqual(mgr.status().config_path, "chosen.toml")

    def test_the_build_is_handed_the_request_and_the_generation(self):
        build = _Build()
        mgr = self.manager(build=build, teardown=_Teardown())
        req = _request("a", "b")
        mgr.start(req)
        self.assertReaches(mgr, SessionState.RUNNING)
        self.assertEqual(build.calls, [(req, 1)])
        self.assertEqual(mgr.status().systems, ("a", "b"))

    def test_start_while_running_is_refused(self):
        mgr = self.manager(build=_Build(), teardown=_Teardown())
        mgr.start(_request("a"))
        self.assertReaches(mgr, SessionState.RUNNING)
        with self.assertRaises(serve.SupervisorBusy) as cm:
            mgr.start(_request("a"))
        self.assertEqual(cm.exception.state, SessionState.RUNNING)
        # Refused, not silently swapped: the session that was running still is.
        self.assertEqual(mgr.generation, 1)

    def test_stop_tears_the_session_down_once_and_lands_idle(self):
        req = _request("a")
        sess = _session(req)
        sess.threads = _blocking_threads(sess.stop_event)
        down = _Teardown()
        rec = _Recorder()
        mgr = self.manager(build=_Build(sess), teardown=down, on_transition=rec)
        mgr.start(req)
        self.assertReaches(mgr, SessionState.RUNNING)
        with quiet_logging():
            self.assertTrue(mgr.stop())
            self.assertReaches(mgr, SessionState.IDLE)
        self.assertEqual(down.calls, [sess])
        self.assertTrue(sess.stop_event.is_set())
        self.assertEqual(
            rec.states,
            [
                SessionState.STARTING,
                SessionState.RUNNING,
                SessionState.STOPPING,
                SessionState.IDLE,
            ],
        )
        self.assertIsNone(mgr.session)

    def test_stop_from_idle_is_a_no_op(self):
        mgr = self.manager(build=_Build(), teardown=_Teardown())
        self.assertFalse(mgr.stop())
        self.assertEqual(mgr.state, SessionState.IDLE)

    def test_restart_yields_two_generations_and_two_teardowns(self):
        req = _request("a")
        first, second = _session(req), _session(req)
        down = _Teardown()
        mgr = self.manager(build=_Build(first, second), teardown=down)
        mgr.start(req)
        self.assertReaches(mgr, SessionState.RUNNING)
        mgr.stop()
        self.assertReaches(mgr, SessionState.IDLE)
        self.assertEqual(mgr.start(req), 2)
        self.assertReaches(mgr, SessionState.RUNNING)
        self.assertIs(mgr.session, second)
        mgr.stop()
        self.assertReaches(mgr, SessionState.IDLE)
        self.assertEqual(down.calls, [first, second])

    def test_a_stop_during_starting_cancels_the_run(self):
        # The build isn't interruptible (opening a backend blocks), so the
        # cancel has to be honored on the far side of it.
        build = _Build()
        build.gate = threading.Event()
        down = _Teardown()
        mgr = self.manager(build=build, teardown=down)
        mgr.start(_request("a"))
        self.assertReaches(mgr, SessionState.STARTING)
        self.assertTrue(mgr.stop())
        build.gate.set()
        with quiet_logging():
            self.assertReaches(mgr, SessionState.IDLE)
        self.assertEqual(len(down.calls), 1)
        self.assertIsNone(mgr.session)


class FailedBuildTest(SupervisorTestCase):
    def test_a_failed_build_lands_in_error_with_the_hardware_released(self):
        rec = _Recorder()
        down = _Teardown()
        req = _request("a")
        sess = _session(req)
        # Published before it failed: the stacks are up, so the supervisor —
        # not the build — is what has to let go of them.
        build = _Build(sess, error=session.StackBuildError(1))
        mgr = self.manager(build=build, teardown=down, on_transition=rec)
        with quiet_logging():
            mgr.start(req)
            self.assertReaches(mgr, SessionState.ERROR)
        self.assertEqual(
            rec.states,
            [SessionState.STARTING, SessionState.STOPPING, SessionState.ERROR],
        )
        self.assertEqual(down.calls, [sess])
        self.assertIn("StackBuildError", mgr.status().last_error or "")

    def test_a_build_that_failed_before_publishing_still_tears_down(self):
        down = _Teardown()
        build = _Build(error=RuntimeError("no backend"), publish=False)
        mgr = self.manager(build=build, teardown=down)
        with quiet_logging():
            mgr.start(_request("a"))
            self.assertReaches(mgr, SessionState.ERROR)
        self.assertEqual(down.calls, [None])
        self.assertEqual(mgr.status().last_error, "RuntimeError: no backend")

    def test_error_is_not_sticky(self):
        build = _Build(error=RuntimeError("nope"), publish=False)
        mgr = self.manager(build=build, teardown=_Teardown())
        with quiet_logging():
            mgr.start(_request("a"))
            self.assertReaches(mgr, SessionState.ERROR)
        build.error = None
        self.assertEqual(mgr.start(_request("a")), 2)
        self.assertReaches(mgr, SessionState.RUNNING)
        # A clean start clears the previous failure rather than leaving the UI
        # showing an error next to a running show.
        self.assertIsNone(mgr.status().last_error)


class CooldownTest(SupervisorTestCase):
    """The settle window covers two separate hardware facts with one timer:
    the U64's DMA service refusing new connections for a few seconds after one
    closes, and AVFoundation refusing to reopen a camera straight after
    release()."""

    def test_a_start_waits_for_the_hardware_to_settle(self):
        now = [1000.0]
        mgr = self.manager(build=_Build(), teardown=_Teardown(), settle_s=5.0, clock=lambda: now[0])
        mgr.start(_request("a"))
        self.assertReaches(mgr, SessionState.RUNNING)
        mgr.stop()
        self.assertReaches(mgr, SessionState.IDLE)
        self.assertAlmostEqual(mgr.status().hardware_wait_s, 5.0)

        mgr.start(_request("a"))
        self.assertFalse(mgr.wait_for(SessionState.RUNNING, timeout=0.25))
        self.assertEqual(mgr.state, SessionState.STARTING)

        now[0] += 5.0
        self.assertReaches(mgr, SessionState.RUNNING)
        self.assertEqual(mgr.status().hardware_wait_s, 0.0)


class ReapTest(SupervisorTestCase):
    def test_playlists_that_end_by_themselves_drive_running_to_idle(self):
        req = _request("a")
        sess = _session(req, threads=[_finished_thread()])
        down = _Teardown()
        rec = _Recorder()
        mgr = self.manager(build=_Build(sess), teardown=down, on_transition=rec)
        with quiet_logging():
            mgr.start(req)
            # No stop() anywhere: a non-looping show ends on its own, and the
            # daemon has no join() to notice it the way the CLI does.
            self.assertReaches(mgr, SessionState.IDLE)
        self.assertEqual(down.calls, [sess])
        self.assertEqual(
            rec.states,
            [
                SessionState.STARTING,
                SessionState.RUNNING,
                SessionState.STOPPING,
                SessionState.IDLE,
            ],
        )

    def test_a_session_with_no_threads_is_not_reaped(self):
        req = _request("a")
        mgr = self.manager(build=_Build(_session(req)), teardown=_Teardown())
        mgr.start(req)
        self.assertReaches(mgr, SessionState.RUNNING)
        self.assertFalse(mgr.wait_for(SessionState.IDLE, timeout=0.2))
        self.assertEqual(mgr.state, SessionState.RUNNING)


class SwitchTest(SupervisorTestCase):
    def test_switch_stops_the_old_session_before_starting_the_new_one(self):
        req = _request("a")
        first, second = _session(req), _session(req)
        first.threads = _blocking_threads(first.stop_event)
        down = _Teardown()
        rec = _Recorder()
        mgr = self.manager(build=_Build(first, second), teardown=down, on_transition=rec)
        mgr.start(req)
        self.assertReaches(mgr, SessionState.RUNNING)
        with quiet_logging():
            self.assertEqual(mgr.switch(_request("b")), 2)
            self.assertReaches(mgr, SessionState.RUNNING, generation=2)
        self.assertEqual(mgr.generation, 2)
        self.assertIs(mgr.session, second)
        # The old session came all the way down before the new one came up.
        self.assertEqual(down.calls, [first])
        self.assertEqual(
            rec.states,
            [
                SessionState.STARTING,
                SessionState.RUNNING,
                SessionState.STOPPING,
                SessionState.IDLE,
                SessionState.STARTING,
                SessionState.RUNNING,
            ],
        )

    def test_switch_from_idle_just_starts(self):
        mgr = self.manager(build=_Build(), teardown=_Teardown())
        self.assertEqual(mgr.switch(_request("a")), 1)
        self.assertReaches(mgr, SessionState.RUNNING)

    def test_a_start_during_a_switch_is_refused(self):
        build = _Build()
        build.gate = threading.Event()
        mgr = self.manager(build=build, teardown=_Teardown())
        mgr.switch(_request("a"))
        self.assertReaches(mgr, SessionState.STARTING)
        with self.assertRaises(serve.SupervisorBusy):
            mgr.start(_request("b"))
        build.gate.set()
        self.assertReaches(mgr, SessionState.RUNNING)


class RunMarkerTest(SupervisorTestCase):
    """The marker is the daemon's only way to tell "the last run ended" from
    "the last run died with the machine mid-show"."""

    def test_the_marker_is_written_while_running_and_cleared_on_the_way_down(self):
        req = _request("a", config_path="/tmp/show.toml")
        mgr = self.manager(build=_Build(), teardown=_Teardown())
        mgr.start(req)
        self.assertReaches(mgr, SessionState.RUNNING)
        self.assertTrue(self.marker.exists())
        import json

        payload = json.loads(self.marker.read_text())
        self.assertEqual(payload["generation"], 1)
        self.assertEqual(payload["config_path"], "/tmp/show.toml")
        mgr.stop()
        self.assertReaches(mgr, SessionState.IDLE)
        self.assertFalse(self.marker.exists())

    def test_a_marker_left_behind_resets_the_machine_before_the_next_start(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text('{"pid": 1, "generation": 7}\n')
        order: list[str] = []
        build = _Build()

        def recording_build(req, gen, publish):
            order.append("build")
            return build(req, gen, publish)

        mgr = self.manager(
            build=recording_build,
            teardown=_Teardown(),
            safe_state=lambda _req: order.append("safe_state"),
        )
        with quiet_logging():
            mgr.start(_request("a"))
            self.assertReaches(mgr, SessionState.RUNNING)
        self.assertEqual(order, ["safe_state", "build"])

    def test_the_build_settles_after_a_recovery_touched_the_hardware(self):
        # safe_state opens and closes a backend, which arms the same window a
        # teardown does — handing it straight to the build is the socket-reuse
        # case the cooldown exists for.
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("{}\n")
        now = [1000.0]
        build = _Build()
        mgr = self.manager(
            build=build,
            teardown=_Teardown(),
            safe_state=lambda _req: None,
            settle_s=5.0,
            clock=lambda: now[0],
        )
        with quiet_logging():
            mgr.start(_request("a"))
            self.assertFalse(mgr.wait_for(SessionState.RUNNING, timeout=0.25))
            self.assertEqual(build.calls, [])
            now[0] += 5.0
            self.assertReaches(mgr, SessionState.RUNNING)

    def test_a_failing_recovery_does_not_block_the_start(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("{}\n")

        def boom(_req):
            raise RuntimeError("machine unreachable")

        mgr = self.manager(build=_Build(), teardown=_Teardown(), safe_state=boom)
        with quiet_logging():
            mgr.start(_request("a"))
            self.assertReaches(mgr, SessionState.RUNNING)


class ReloadTest(SupervisorTestCase):
    def test_reload_reaches_the_running_session(self):
        req = _request("a")
        sess = _session(req)
        mgr = self.manager(build=_Build(sess), teardown=_Teardown())
        mgr.start(req)
        self.assertReaches(mgr, SessionState.RUNNING)
        with mock.patch.object(serve, "reload_all") as reload_all:
            mgr.reload()
        reload_all.assert_called_once_with(sess)

    def test_reload_with_nothing_running_is_refused(self):
        mgr = self.manager(build=_Build(), teardown=_Teardown())
        with self.assertRaises(serve.SupervisorBusy):
            mgr.reload()


class SafeStateTest(unittest.TestCase):
    """default_safe_state is the smallest bring-up that can still reset — no
    probe, no char ROM, no provisioning, any of which could fail and abandon
    the one thing that has to happen."""

    def test_every_named_system_is_reset_and_closed(self):
        req = _request("a", "b")
        apis = [mock.MagicMock(name="api-a"), mock.MagicMock(name="api-b")]
        with mock.patch("c64cast.hw.backend.make_backend", side_effect=apis) as make:
            with quiet_logging():
                serve.default_safe_state(req)
        self.assertEqual(make.call_count, 2)
        for api in apis:
            api.reset.assert_called_once_with()
            api.close.assert_called_once_with()

    def test_a_backend_that_will_not_open_does_not_stop_the_others(self):
        req = _request("a", "b")
        good = mock.MagicMock(name="api-b")
        with mock.patch("c64cast.hw.backend.make_backend", side_effect=[OSError("no route"), good]):
            with quiet_logging():
                serve.default_safe_state(req)
        good.reset.assert_called_once_with()

    def test_a_reset_that_raises_still_closes_the_backend(self):
        req = _request("a")
        api = mock.MagicMock(name="api-a")
        api.reset.side_effect = OSError("write failed")
        with mock.patch("c64cast.hw.backend.make_backend", return_value=api):
            with quiet_logging():
                serve.default_safe_state(req)
        api.close.assert_called_once_with()


class SessionLogBufferTest(unittest.TestCase):
    def _buffer(self, name: str, capacity: int = 500) -> serve.SessionLogBuffer:
        """A buffer on its own logger, at a level that lets INFO through — the
        daemon's own logger is set by `configure_logging`, but a throwaway one
        inherits the root's WARNING."""
        import logging

        buf = serve.SessionLogBuffer(capacity=capacity)
        logger = logging.getLogger(name)
        level, propagate = logger.level, logger.propagate
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        buf.install(name)
        self.addCleanup(setattr, logger, "propagate", propagate)
        self.addCleanup(logger.setLevel, level)
        self.addCleanup(buf.uninstall, name)
        return buf

    def test_records_are_tagged_with_the_generation_that_produced_them(self):
        import logging

        buf = self._buffer("c64cast.test-buffer", capacity=10)
        log = logging.getLogger("c64cast.test-buffer")
        buf.generation = 1
        log.error("first run failed")
        buf.generation = 2
        log.info("second run up")
        self.assertEqual([r["generation"] for r in buf.tail()], [1, 2])
        self.assertEqual([r["message"] for r in buf.tail(generation=1)], ["first run failed"])
        self.assertEqual(buf.tail(limit=1)[0]["level"], "INFO")

    def test_the_buffer_is_bounded(self):
        import logging

        buf = self._buffer("c64cast.test-buffer-cap", capacity=3)
        log = logging.getLogger("c64cast.test-buffer-cap")
        for i in range(10):
            log.info("line %d", i)
        self.assertEqual([r["message"] for r in buf.tail()], ["line 7", "line 8", "line 9"])

    def test_a_token_never_reaches_the_buffer(self):
        """The buffer is served to every client on the state feed, read-only
        viewers included, so the login URL's token must not survive into it —
        a viewer handed the admin token could stop the show."""
        import logging

        buf = self._buffer("c64cast.test-buffer-secret")
        log = logging.getLogger("c64cast.test-buffer-secret")
        log.info("web console: open http://127.0.0.1:8123/api/login?token=s3cr3t-abc&next=/")
        message = buf.tail(limit=1)[0]["message"]
        self.assertNotIn("s3cr3t-abc", message)
        self.assertIn("token=REDACTED", message)
        # Still diagnostic: the reader can tell which URL was printed.
        self.assertIn("127.0.0.1:8123", message)
        self.assertIn("next=/", message)

    def test_the_supervisor_tags_the_buffer_with_each_new_generation(self):
        buf = serve.SessionLogBuffer()
        mgr = serve.SessionManager(
            build=_Build(), teardown=_Teardown(), settle_s=0.0, log_buffer=buf
        )
        self.addCleanup(mgr.close, timeout=WAIT)
        mgr.start(_request("a"))
        self.assertTrue(mgr.wait_for(SessionState.RUNNING, timeout=WAIT))
        self.assertEqual(buf.generation, 1)


class StatusShapeTest(SupervisorTestCase):
    def test_the_status_dict_is_json_ready(self):
        import json

        mgr = self.manager(build=_Build(), teardown=_Teardown())
        mgr.start(_request("a"))
        self.assertReaches(mgr, SessionState.RUNNING)
        payload = json.loads(json.dumps(mgr.status().as_dict()))
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["systems"], ["a"])
        self.assertEqual(payload["generation"], 1)


if __name__ == "__main__":
    unittest.main()
