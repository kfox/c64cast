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
import contextlib
import os
import signal
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from _fakes import fake_system_stack, quiet_logging

from c64cast.app import config as cfgmod
from c64cast.app import paths, serve, session
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


def _fake_control_server(
    events: list[str],
    *,
    apps: list[Any] | None = None,
    built: threading.Event | None = None,
    bind_ok: bool = True,
):
    """A `control_plane.ControlServer` stand-in that never opens a socket.

    Built per test rather than shared, so nothing leaks between them. `start()`
    answers `bind_ok`, which is the real one's "uvicorn reported a bound
    socket" verdict — the whole point of `_serve_once` checking it — and, like
    the real one, leaves the server stopped when it says no."""

    class _FakeControlServer:
        def __init__(self, _host, _port, app=None, *, label=""):
            if apps is not None:
                apps.append(app)
            if built is not None:
                built.set()

        def start(self):
            events.append("server.start")
            if not bind_ok:
                self.stop()
            return bind_ok

        def stop(self):
            events.append("server.stop")

    return _FakeControlServer


def _fake_advertiser(events: list[str], *, built: list[tuple[Any, ...]] | None = None):
    """A `ConsoleMdnsAdvertiser` stand-in — the real one opens a multicast
    socket, and `test_console_mdns.py` covers its own behavior."""

    class _FakeAdvertiser:
        def __init__(self, host, port, *, pending):
            if built is not None:
                built.append((host, port, pending))

        def start(self):
            events.append("mdns.start")

        def stop(self):
            events.append("mdns.stop")

    return _FakeAdvertiser


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

    def awaitError(self, mgr: serve.SessionManager) -> str:
        """The diagnostic a worker parks in `last_error`, once it lands. There
        is no transition to wait on for an abandoned switch — the state does
        not change — so this polls the snapshot the console reads."""
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            error = mgr.status().last_error
            if error:
                return error
            time.sleep(0.01)
        self.fail(f"no last_error appeared (state {mgr.state})")


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

    def test_the_reaper_names_its_worker_apart_from_an_operator_stop(self):
        """`join_bounded` identifies a straggler only by thread name, and a
        wedged playlist and a hung teardown are different root causes."""
        req = _request("a")
        sess = _session(req, threads=[_finished_thread()])
        mgr = self.manager(build=_Build(sess), teardown=_Teardown())
        names: list[str] = []
        real_spawn = serve._Workers.spawn

        def recording_spawn(workers, name, fn):
            names.append(name)
            return real_spawn(workers, name, fn)

        with mock.patch.object(serve._Workers, "spawn", recording_spawn):
            with quiet_logging():
                mgr.start(req)
                self.assertReaches(mgr, SessionState.RUNNING)
                self.assertReaches(mgr, SessionState.IDLE)
        self.assertIn("session-reap-stop-1", names)
        self.assertNotIn("session-stop-1", names)

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

    def _stuck_teardown(self, mgr_kwargs: dict[str, Any]) -> tuple[serve.SessionManager, Any]:
        """A running generation-1 session whose teardown blocks until the
        returned gate is released — the window every abandoned-switch path
        lives in."""
        gate = threading.Event()
        mgr = self.manager(teardown=lambda _s: gate.wait(WAIT), **mgr_kwargs)
        # Registered after the manager, so it runs *before* the close() cleanup
        # the manager helper registered and that close never waits on a gate
        # nobody is going to release.
        self.addCleanup(gate.set)
        mgr.start(_request("a"))
        self.assertReaches(mgr, SessionState.RUNNING)
        return mgr, gate

    def test_a_switch_that_times_out_says_why_on_the_state_feed(self):
        rec = _Recorder()
        mgr, _gate = self._stuck_teardown({"build": _Build(), "on_transition": rec})
        with quiet_logging():
            self.assertEqual(mgr.switch(_request("b"), timeout=0.05), 2)
            error = self.awaitError(mgr)
        # The generation switch() promised never arrives, so the reason has to
        # ride the snapshot: it used to log and return with last_error null.
        self.assertIn("switch abandoned", error)
        self.assertEqual(mgr.generation, 1)
        self.assertEqual(rec.snapshots[-1].last_error, error)

    def test_a_stop_during_a_switch_cancels_the_pending_start(self):
        build = _Build()
        mgr, gate = self._stuck_teardown({"build": build})
        self.assertEqual(mgr.switch(_request("b")), 2)
        self.assertReaches(mgr, SessionState.STOPPING)
        with quiet_logging():
            # This answered False and was discarded, and the switch then
            # brought the hardware up anyway.
            self.assertTrue(mgr.stop())
            gate.set()
            self.assertReaches(mgr, SessionState.IDLE)
            error = self.awaitError(mgr)
        self.assertIn("switch abandoned", error)
        self.assertEqual(mgr.generation, 1)
        self.assertEqual(len(build.calls), 1)

    def test_close_during_a_switch_does_not_hand_the_hardware_to_a_new_session(self):
        build = _Build()
        mgr, gate = self._stuck_teardown({"build": build})
        mgr.switch(_request("b"))
        self.assertReaches(mgr, SessionState.STOPPING)
        # Released only once close() has set its terminal flag, which it does
        # before it blocks — the window this covers is the whole switch.
        timer = threading.Timer(0.15, gate.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with quiet_logging():
            mgr.close(timeout=WAIT)
        self.assertEqual(mgr.state, SessionState.IDLE)
        self.assertEqual(mgr.generation, 1)
        self.assertEqual(len(build.calls), 1)
        with self.assertRaises(serve.SupervisorBusy):
            mgr.start(_request("c"))


class SpawnFailureTest(SupervisorTestCase):
    """`starting` has exactly one way out — the start worker — so a spawn that
    raises used to wedge the supervisor there for the life of the process."""

    def test_a_worker_that_cannot_be_spawned_rolls_back_to_a_startable_state(self):
        mgr = self.manager(build=_Build(), teardown=_Teardown())
        with mock.patch.object(
            serve._Workers, "spawn", side_effect=RuntimeError("can't start new thread")
        ):
            with self.assertRaises(RuntimeError):
                mgr.start(_request("a"))
        self.assertEqual(mgr.state, SessionState.ERROR)
        self.assertEqual(mgr.generation, 0)
        self.assertIn("can't start new thread", mgr.status().last_error or "")
        # `error` is startable, so the host is still usable.
        mgr.start(_request("a"))
        self.assertReaches(mgr, SessionState.RUNNING)
        self.assertEqual(mgr.generation, 1)


class StopFailureTest(SupervisorTestCase):
    def test_a_shutdown_that_raises_is_reported_on_the_state_feed(self):
        req = _request("a")
        sess = _session(req)
        sess.threads = _blocking_threads(sess.stop_event)
        self.addCleanup(sess.stop_event.set)
        mgr = self.manager(build=_Build(sess), teardown=_Teardown())
        mgr.start(req)
        self.assertReaches(mgr, SessionState.RUNNING)
        with mock.patch.object(serve, "join_playlists", side_effect=OSError("link gone")):
            with self.assertLogs("c64cast", level="ERROR"):
                mgr.stop()
                self.assertReaches(mgr, SessionState.IDLE)
        # Settling with error=None left a failed teardown as invisible as an
        # abandoned switch was.
        self.assertIn("session shutdown failed", mgr.status().last_error or "")


class LastErrorRedactionTest(SupervisorTestCase):
    """`last_error` rides the same state frame as the log buffer and reaches a
    read-only viewer the same way, so it is redacted on the way in too."""

    def test_a_secret_in_a_build_failure_never_reaches_the_snapshot(self):
        boom = RuntimeError("refused by u64://box/?token=s3cr3t-abc")
        mgr = self.manager(build=_Build(error=boom), teardown=_Teardown())
        with quiet_logging():
            mgr.start(_request("a"))
            self.assertReaches(mgr, SessionState.ERROR)
        error = mgr.status().last_error or ""
        self.assertNotIn("s3cr3t-abc", error)
        self.assertIn("token=REDACTED", error)
        # Still diagnostic.
        self.assertIn("RuntimeError", error)


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

    def test_a_non_positive_limit_asks_for_nothing(self):
        """`rows[-0:]` is the whole list, so the guard that used to answer
        `limit=0` with every retained line inverted the parameter's meaning —
        a footgun for the first route that forwards a client's own number."""
        import logging

        buf = self._buffer("c64cast.test-buffer-limit", capacity=10)
        log = logging.getLogger("c64cast.test-buffer-limit")
        for i in range(3):
            log.info("line %d", i)
        self.assertEqual(buf.tail(limit=0), [])
        self.assertEqual(buf.tail(limit=-1), [])
        self.assertEqual(len(buf.tail(limit=3)), 3)

    def test_a_reader_survives_a_writer_appending_to_the_deque(self):
        """`since()` iterated the live deque at the Python level, which CPython
        answers with `RuntimeError: deque mutated during iteration` the moment
        `emit` appends (or evicts, at maxlen) from a build worker. The reader is
        the state feed's push loop, and it raised hardest exactly when the log
        was busiest — a failing build."""
        import logging

        buf = self._buffer("c64cast.test-buffer-race", capacity=64)
        log = logging.getLogger("c64cast.test-buffer-race")
        stop = threading.Event()
        failures: list[BaseException] = []

        def writer() -> None:
            while not stop.is_set():
                log.info("a line from a build worker")

        def reader() -> None:
            try:
                for _ in range(4000):
                    buf.since(0)
                    buf.tail(20)
            except BaseException as e:  # noqa: BLE001 - the whole point is what raised
                failures.append(e)

        hands = [threading.Thread(target=writer, daemon=True) for _ in range(2)]
        hands.append(threading.Thread(target=reader, daemon=True))
        for t in hands:
            t.start()
        hands[-1].join(timeout=WAIT)
        stop.set()
        for t in hands:
            t.join(timeout=WAIT)
        self.assertFalse(hands[-1].is_alive(), "the reader never finished")
        self.assertEqual(failures, [])

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


class WorkersJoinTest(unittest.TestCase):
    """_Workers.join is serve.py's other non-daemon join site (the supervisor's
    own start/teardown threads, not the playlist ones session.py owns) — same
    join_bounded helper, same reason: a single long join(timeout) parks
    uninterruptibly, so it has to poll instead."""

    def test_join_polls_so_signals_can_be_delivered(self):
        event = threading.Event()

        def wait_forever() -> None:
            event.wait()

        workers = serve._Workers()
        workers.spawn("worker-a", wait_forever)
        timeouts: list[float | None] = []
        real_join = threading.Thread.join

        def recording_join(self, timeout=None):  # noqa: ANN001
            timeouts.append(timeout)
            return real_join(self, timeout)

        timer = threading.Timer(0.05, event.set)
        timer.start()
        try:
            with mock.patch.object(threading.Thread, "join", recording_join):
                workers.join(timeout=5.0)
        finally:
            timer.cancel()
        self.assertTrue(timeouts, "join was never called")
        self.assertNotIn(None, timeouts, "join blocked with no timeout; signals cannot be handled")

    def test_a_worker_that_outlives_its_deadline_is_logged_and_abandoned(self):
        event = threading.Event()

        def wait_forever() -> None:
            event.wait()

        workers = serve._Workers()
        workers.spawn("worker-stuck", wait_forever)
        try:
            with self.assertLogs("c64cast", level="ERROR") as cm:
                workers.join(timeout=0.05)
            self.assertTrue(any("worker-stuck" in m for m in cm.output))
        finally:
            event.set()
            for t in workers.threads:
                t.join()


class RunDaemonTestCase(unittest.TestCase):
    """Shared isolation for every `run_daemon` test.

    Every path the run touches is redirected into a temporary directory: the
    data root carries the generated token, the read-only token and the setup
    markers, and `$C64CAST_SETTINGS` points at a missing file so the
    provisioning check `_setup_pending` makes cannot read the machine settings
    of the developer's own rig. The two token environment variables are
    cleared for the same reason — a real one in the shell would change both
    what `resolve_tokens` resolves and which source it reports."""

    def setUp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.settings = self.tmp / "settings.toml"
        patcher = mock.patch.dict(
            os.environ,
            {"C64CAST_DATA_DIR": tmp.name, "C64CAST_SETTINGS": str(self.settings)},
            clear=False,
        )
        patcher.start()
        # `patch.dict` restores the whole mapping on stop, so the pops below
        # are undone by the same cleanup.
        self.addCleanup(patcher.stop)
        for var in ("C64CAST_WEB_TOKEN", "C64CAST_WEB_VIEWER_TOKEN"):
            os.environ.pop(var, None)
        self.events: list[str] = []
        self.apps: list[Any] = []
        self.advertised: list[tuple[Any, ...]] = []
        self.app_built = threading.Event()

    def fakes(self, *, bind_ok: bool = True) -> contextlib.ExitStack:
        """`ControlServer` and the mDNS advertiser faked out, both recording
        into `self.events` so an ordering assertion has something to read."""
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch(
                "c64cast.control.control_plane.ControlServer",
                _fake_control_server(
                    self.events, apps=self.apps, built=self.app_built, bind_ok=bind_ok
                ),
            )
        )
        stack.enter_context(
            mock.patch(
                "c64cast.control.console_mdns.ConsoleMdnsAdvertiser",
                _fake_advertiser(self.events, built=self.advertised),
            )
        )
        return stack

    def recording_close(self) -> Any:
        """`SessionManager.close` labeled into `self.events`, which is the only
        way to pin it against `server.stop` — the ordering is stated in prose
        in three places and was asserted in none."""
        real_close = serve.SessionManager.close

        def close(mgr, **kwargs):
            self.events.append("manager.close")
            return real_close(mgr, **kwargs)

        return mock.patch.object(serve.SessionManager, "close", close)

    def refuse_load(self, _path):
        return self.fail("autostart is off; the config loader must not be called")

    def drive(
        self, web_cfg, *, load=None, poke=None, stop: bool = True, bind_ok: bool = True
    ) -> int:
        """Run `run_daemon` on a worker thread with its collaborators faked
        out, call `poke()` once its SIGTERM handler is installed, then deliver
        one stop signal and join.

        No real OS signal is ever raised: the handler `run_daemon` installs is
        captured by patching `signal.signal` and called directly. One signal,
        not two — the second one warns about the escape hatch, which is its own
        test's subject rather than every other test's console noise."""
        registered: dict[int, Callable[[int, object], None]] = {}
        installed = threading.Event()

        def recording_signal(signum, handler):
            registered[signum] = handler
            if signum == signal.SIGTERM:
                installed.set()
            return signal.SIG_DFL

        result: dict[str, int] = {}

        def run():
            result["code"] = serve.run_daemon(web_cfg, load or self.refuse_load)

        with self.fakes(bind_ok=bind_ok):
            with mock.patch.object(signal, "signal", side_effect=recording_signal):
                thread = threading.Thread(target=run, name="run-daemon", daemon=True)
                thread.start()
                try:
                    self.assertTrue(
                        installed.wait(timeout=WAIT), "no SIGTERM handler was installed"
                    )
                    if poke is not None:
                        poke()
                    if stop:
                        registered[signal.SIGTERM](signal.SIGTERM, None)
                finally:
                    thread.join(timeout=WAIT)
        self.assertFalse(thread.is_alive(), "run_daemon did not return")
        return result["code"]


class RunDaemonSignalTest(RunDaemonTestCase):
    """run_daemon's own signal handling, mirroring cli._run_session's
    three-strike shape."""

    def test_a_second_signal_restores_the_default_disposition(self):
        registered: dict[int, Callable[[int, object], None]] = {}
        calls: list[tuple[int, object]] = []
        installed = threading.Event()

        def recording_signal(signum, handler):
            calls.append((signum, handler))
            registered[signum] = handler
            if signum == signal.SIGTERM:
                installed.set()
            return signal.SIG_DFL

        web_cfg = cfgmod.WebCfg(autostart=False)
        result: dict[str, int] = {}

        def run():
            result["code"] = serve.run_daemon(web_cfg, self.refuse_load)

        with self.fakes():
            with mock.patch.object(signal, "signal", side_effect=recording_signal):
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                try:
                    self.assertTrue(installed.wait(timeout=WAIT), "no SIGTERM handler")
                    handler = registered[signal.SIGTERM]
                    with self.assertLogs("c64cast", level="INFO") as cm:
                        handler(signal.SIGTERM, None)
                        handler(signal.SIGTERM, None)
                finally:
                    thread.join(timeout=WAIT)
        self.assertFalse(thread.is_alive(), "run_daemon did not return after the stop signal")
        self.assertEqual(result.get("code"), 0)
        self.assertTrue(
            any("again; next one exits immediately" in m for m in cm.output),
            "second signal did not warn about the escape hatch",
        )
        self.assertIn((signal.SIGTERM, signal.SIG_DFL), calls)


class RunDaemonMdnsTest(RunDaemonTestCase):
    """`run_daemon` starts and stops a `ConsoleMdnsAdvertiser` alongside its
    `ControlServer`, once per loop iteration."""

    def test_advertiser_is_built_started_and_stopped_around_the_pump(self):
        code = self.drive(cfgmod.WebCfg(host="0.0.0.0", port=9999, autostart=False))
        self.assertEqual(code, 0)
        self.assertEqual(self.advertised, [("0.0.0.0", 9999, False)])
        self.assertEqual(
            [e for e in self.events if e.startswith("mdns")], ["mdns.start", "mdns.stop"]
        )


class RunDaemonShutdownOrderTest(RunDaemonTestCase):
    """The order run_daemon's docstring and control.md both assert, now pinned:
    the session comes down *before* the listener. It ran the other way round,
    so every connected console lost its socket first and then waited out a
    teardown it could no longer watch."""

    def test_the_session_comes_down_before_the_listener(self):
        with self.recording_close():
            code = self.drive(cfgmod.WebCfg(autostart=False))
        self.assertEqual(code, 0)
        self.assertIn("manager.close", self.events)
        self.assertLess(
            self.events.index("manager.close"),
            self.events.index("server.stop"),
            f"the listener was stopped before the session: {self.events}",
        )
        self.assertLess(self.events.index("manager.close"), self.events.index("mdns.stop"))


class RunDaemonBindFailureTest(RunDaemonTestCase):
    """uvicorn binds on its own thread and `sys.exit(1)`s there when the port
    is taken — a `SystemExit` `PollThread` does not catch and
    `threading.excepthook` discards. The host used to print a login URL,
    autostart a show on real hardware, park forever with nothing listening,
    and then exit 0."""

    def test_a_listener_that_never_bound_is_a_nonzero_exit(self):
        # autostart on and a loader that fails the test if it is reached: a
        # host with no listener must not touch the hardware.
        code = self.drive(cfgmod.WebCfg(autostart=True), stop=False, bind_ok=False)
        self.assertEqual(code, 2)
        # No beacon either: nothing announces a console that isn't there.
        self.assertEqual(self.events, ["server.start", "server.stop"])
        self.assertEqual(self.advertised, [])


class SetupPendingTest(RunDaemonTestCase):
    """What `_setup_pending` requires before it opens an unauthenticated form
    on the network. The absence of one file under the *data* root used to be
    the only evidence, which cannot tell a first boot from a host that lost
    its data dir while staying fully configured."""

    def _provisioned(self) -> None:
        self.settings.write_text('[ultimate64]\nurl = "u64://192.168.2.64"\n', encoding="utf-8")

    def _reset_setup_asked(self) -> None:
        path = paths.setup_reopen_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"requested_at": 1}\n', encoding="utf-8")

    def test_a_first_boot_opens_the_window(self):
        self.assertTrue(serve._setup_pending(cfgmod.WebCfg(setup_wizard=True)))

    def test_the_wizard_switch_still_governs(self):
        self.assertFalse(serve._setup_pending(cfgmod.WebCfg(setup_wizard=False)))

    def test_a_completed_setup_closes_it(self):
        path = paths.setup_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        self.assertFalse(serve._setup_pending(cfgmod.WebCfg(setup_wizard=True)))

    def test_a_provisioned_host_that_lost_its_marker_stays_shut(self):
        self._provisioned()
        with self.assertLogs("c64cast", level="WARNING") as cm:
            self.assertFalse(serve._setup_pending(cfgmod.WebCfg(setup_wizard=True)))
        self.assertTrue(any("stays shut" in m for m in cm.output))

    def test_reset_setup_reopens_it_on_a_provisioned_host(self):
        self._provisioned()
        self._reset_setup_asked()
        with self.assertLogs("c64cast", level="WARNING") as cm:
            self.assertTrue(serve._setup_pending(cfgmod.WebCfg(setup_wizard=True)))
        self.assertTrue(any("--reset-setup" in m for m in cm.output))

    def test_completing_setup_spends_the_reopen_marker(self):
        self._reset_setup_asked()
        serve._clear_setup_reopen()
        self.assertFalse(paths.setup_reopen_path().exists())


class PumpForeverTest(unittest.TestCase):
    """`pump_forever` is the main thread of every `--serve` run — HighGUI may
    only create and service a window there — so a regression in it hangs or
    busy-spins the host rather than failing a scene. Everything it touches is
    injectable, and nothing here needs cv2."""

    class _Window:
        def __init__(self, *, raises: bool = False):
            self.calls: list[str] = []
            self.is_open = False
            self._raises = raises

        def open(self):
            self.calls.append("open")
            self.is_open = True

        def pump(self):
            self.calls.append("pump")
            # The real pump blocks in cv2.waitKey; without a stand-in for that
            # this test's loop is a spin.
            time.sleep(0.005)
            if self._raises:
                raise RuntimeError("HighGUI said no")

        def close(self):
            self.calls.append("close")
            self.is_open = False

    def _manager(self, *windows) -> Any:
        stacks = [mock.MagicMock(preview_window=w) for w in windows]
        return mock.MagicMock(session=mock.MagicMock(generation=1, stacks=stacks))

    def _pumping(self, manager, shutdown, **kwargs) -> threading.Thread:
        thread = threading.Thread(
            target=serve.pump_forever,
            args=(manager, shutdown),
            kwargs={"poll_s": 0.01, **kwargs},
            name="pump-forever",
            daemon=True,
        )
        thread.start()
        self.addCleanup(shutdown.set)
        return thread

    def _await(self, predicate) -> None:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("the pump never got there")

    def test_windows_open_for_a_generation_and_close_when_it_goes(self):
        window = self._Window()
        manager = self._manager(window)
        shutdown = threading.Event()
        thread = self._pumping(manager, shutdown)
        self._await(lambda: "pump" in window.calls)
        manager.session = None
        self._await(lambda: "close" in window.calls)
        shutdown.set()
        thread.join(timeout=WAIT)
        self.assertFalse(thread.is_alive(), "the pump did not return on shutdown")
        self.assertEqual(window.calls[:2], ["open", "pump"])
        self.assertFalse(window.is_open)

    def test_a_window_that_raises_does_not_take_the_host_down(self):
        window = self._Window(raises=True)
        shutdown = threading.Event()
        with self.assertLogs("c64cast", level="ERROR") as cm:
            thread = self._pumping(self._manager(window), shutdown)
            self._await(lambda: window.calls.count("pump") >= 2)
            shutdown.set()
            thread.join(timeout=WAIT)
        self.assertFalse(thread.is_alive(), "a raising pump() took the host down")
        self.assertTrue(any("failed to draw" in m for m in cm.output))
        self.assertIn("close", window.calls)

    def test_a_session_with_no_preview_window_just_waits(self):
        manager = self._manager()
        shutdown = threading.Event()
        thread = self._pumping(manager, shutdown)
        shutdown.set()
        thread.join(timeout=WAIT)
        self.assertFalse(thread.is_alive())

    def test_a_restart_ends_the_pump_the_same_way_a_shutdown_does(self):
        window = self._Window()
        shutdown, restart = threading.Event(), threading.Event()
        thread = self._pumping(self._manager(window), shutdown, restart=restart)
        self._await(lambda: "pump" in window.calls)
        restart.set()
        thread.join(timeout=WAIT)
        self.assertFalse(thread.is_alive(), "the pump ignored the restart event")
        self.assertFalse(shutdown.is_set())
        self.assertIn("close", window.calls)


class RunDaemonSetupWizardTest(RunDaemonTestCase):
    """`[web].setup_wizard`'s restart loop: the first app built serves the
    setup form and blocks everything else; completing it rebuilds a second,
    ordinary app with neither the form nor the gate. The built *app* itself is
    inspected via TestClient rather than the server ever really listening.

    The one write that has nothing to do with the loop — the machine-settings
    overlay, which `test_setup_api.py` covers on its own — is mocked out
    rather than performed: a test that reaches `~/.config` is a test that
    edits the machine it runs on."""

    def setUp(self):
        try:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from fastapi.testclient import TestClient  # noqa: F401
        except (ImportError, RuntimeError):
            self.skipTest("fastapi.testclient (httpx) not installed")

        super().setUp()
        writes = mock.patch("c64cast.control.setup_api._write_connection")
        self.write_connection = writes.start()
        self.addCleanup(writes.stop)

    def test_completing_setup_restarts_into_the_normal_console(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from fastapi.testclient import TestClient

        def poke():
            self.assertTrue(self.app_built.wait(timeout=WAIT), "first app never built")
            self.app_built.clear()
            first_client = TestClient(self.apps[0])
            self.assertTrue(first_client.get("/api/setup").json()["pending"])
            self.assertEqual(first_client.get("/status").status_code, 503)
            # The form is a screen of the ordinary console bundle, so the
            # shell, its assets and the address it puts itself at all have to
            # load with no token — the gate lets them by, and `shell_paths()`
            # is what exempts them from the token check one layer in.
            for path in ("/", "/assets/app.js", "/assets/app.css", "/setup"):
                with self.subTest(path=path):
                    self.assertEqual(first_client.get(path).status_code, 200)

            resp = first_client.post("/api/setup", json={"connection": "u64://192.168.2.64"})
            self.assertEqual(resp.status_code, 200)
            self.assertIn("/api/login?token=", resp.json()["login_url"])
            self.assertEqual(self.write_connection.call_count, 1)

            self.assertTrue(self.app_built.wait(timeout=WAIT), "restart never rebuilt the app")
            self.assertEqual(len(self.apps), 2)
            second_client = TestClient(self.apps[1])
            # setup_api was never registered on this app, but the token gate
            # (which now wraps /api/setup too — the public_paths exemption is
            # per-app-build, not per-route) answers before the catch-all can
            # report a 404.
            self.assertEqual(second_client.get("/api/setup").status_code, 401)
            self.assertEqual(second_client.get("/status").status_code, 401)

        with self.recording_close():
            code = self.drive(cfgmod.WebCfg(setup_wizard=True, autostart=False), poke=poke)
        self.assertEqual(code, 0)
        # The restart replaces the listener and leaves the session alone: only
        # the shutdown path tears the machine down, and only once.
        restarted = self.events.index("server.start", self.events.index("server.stop"))
        self.assertNotIn("manager.close", self.events[:restarted])


if __name__ == "__main__":
    unittest.main()
