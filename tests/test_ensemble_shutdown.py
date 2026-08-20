"""Tests for cli._run_playlists threading + teardown_stack ordering.

The cli module's per-stack lifecycle is exercised here with mocked stacks
so we don't have to bring up real APIs or playlists. End-to-end coverage
of real hardware lives outside the unittest suite (manual verification
against the U64 — see plan §4.2).

SystemStack carries typed fields (Ultimate64API, Playlist, ...) — we
stuff MagicMocks into them, so silence pyright's attribute-access
complaints file-wide rather than spraying ignores on every assertion."""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
from __future__ import annotations

import threading
import unittest
import unittest.mock
from unittest.mock import MagicMock

from _fakes import fake_system_stack

from c64cast.app import session
from c64cast.app.cli import _run_playlists, teardown_stack
from c64cast.app.ensemble import SystemStack


class RunPlaylistsTest(unittest.TestCase):
    def test_starts_one_thread_per_stack_and_joins(self):
        stop_event = threading.Event()
        stacks = [fake_system_stack("a"), fake_system_stack("b")]
        # playlist.run() returns immediately (no infinite loop here);
        # each thread exits and join() completes.
        for st in stacks:
            st.playlist.run.return_value = None
        _run_playlists(stacks, stop_event)
        for st in stacks:
            st.playlist.run.assert_called_once()
        # Sanity: no thread is left dangling.
        for t in threading.enumerate():
            self.assertFalse(t.name.startswith("playlist-"), f"playlist thread leaked: {t.name}")

    def test_stop_event_unblocks_blocking_playlists(self):
        # Playlists that block until stop_event is set should also join
        # cleanly when the event fires from outside.
        stop_event = threading.Event()
        stacks = [fake_system_stack("a"), fake_system_stack("b")]
        for st in stacks:
            st.playlist.run.side_effect = lambda: stop_event.wait()
        # Kick stop_event after a short delay so the main "join" loop
        # has a chance to enter join() on the first thread before exit.
        timer = threading.Timer(0.05, stop_event.set)
        timer.start()
        try:
            _run_playlists(stacks, stop_event)
        finally:
            timer.cancel()
        self.assertTrue(stop_event.is_set())
        for st in stacks:
            st.playlist.run.assert_called_once()

    def test_headless_join_polls_so_signals_can_be_delivered(self):
        # CPython 3.14 parks Thread.join() in _PyParkingLot_Park, which no
        # signal interrupts: the main thread never returns to the interpreter,
        # so Python never runs a signal handler. Measured on a hung run — two
        # SIGINTs produced no shutdown, no teardown and no final reset, and
        # SIGTERM was just as stuck. Only the preview path escaped it, because
        # pumping a window polls is_alive() anyway, which is exactly why Ctrl+C
        # looked intermittent. So the headless path must join with a timeout.
        stop_event = threading.Event()
        stacks = [fake_system_stack("a")]
        stacks[0].playlist.run.side_effect = lambda: stop_event.wait()
        timeouts: list[float | None] = []
        real_join = threading.Thread.join

        def recording_join(self, timeout=None):  # noqa: ANN001
            timeouts.append(timeout)
            return real_join(self, timeout)

        timer = threading.Timer(0.15, stop_event.set)
        timer.start()
        try:
            with unittest.mock.patch.object(threading.Thread, "join", recording_join):
                _run_playlists(stacks, stop_event)
        finally:
            timer.cancel()
        self.assertTrue(timeouts, "join was never called")
        self.assertNotIn(None, timeouts, "join blocked with no timeout; signals cannot be handled")


class JoinBoundedTest(unittest.TestCase):
    """session.join_bounded is the polling join every non-daemon join in the
    project now shares — pump_until_done's headless branch, join_playlists,
    and serve._Workers.join. Covering it once here is what lets those callers
    just trust it."""

    def test_polls_until_the_thread_finishes(self):
        event = threading.Event()
        t = threading.Thread(target=event.wait, name="winding-down")
        t.start()
        timer = threading.Timer(0.05, event.set)
        timer.start()
        try:
            self.assertTrue(session.join_bounded(t, 5.0, poll_s=0.02))
        finally:
            timer.cancel()
        self.assertFalse(t.is_alive())

    def test_reports_false_when_the_thread_outlives_the_timeout(self):
        event = threading.Event()
        t = threading.Thread(target=event.wait, name="stuck")
        t.start()
        try:
            with self.assertLogs("c64cast", level="ERROR") as cm:
                self.assertFalse(session.join_bounded(t, 0.05, poll_s=0.02))
            self.assertTrue(t.is_alive())
            self.assertTrue(any("stuck" in m and "0s" in m for m in cm.output))
        finally:
            event.set()
            t.join()


class JoinPlaylistsTest(unittest.TestCase):
    def test_polls_so_signals_can_be_delivered(self):
        # Same measurement as pump_until_done: a single long join(timeout)
        # parks uninterruptibly, so join_playlists must poll too.
        running = threading.Event()
        stacks = [fake_system_stack("a")]
        t = threading.Thread(target=running.wait, name="playlist-a")
        t.start()
        stop_event = threading.Event()
        timeouts: list[float | None] = []
        real_join = threading.Thread.join

        def recording_join(self, timeout=None):  # noqa: ANN001
            timeouts.append(timeout)
            return real_join(self, timeout)

        timer = threading.Timer(0.05, running.set)
        timer.start()
        try:
            with unittest.mock.patch.object(threading.Thread, "join", recording_join):
                session.join_playlists([t], stacks, stop_event)
        finally:
            timer.cancel()
        self.assertTrue(stop_event.is_set())
        self.assertTrue(timeouts, "join was never called")
        self.assertNotIn(None, timeouts, "join blocked with no timeout; signals cannot be handled")

    def test_a_thread_that_never_exits_is_logged_and_abandoned(self):
        stacks = [fake_system_stack("a")]
        stop_event = threading.Event()
        t = threading.Thread(target=stop_event.wait, name="playlist-stuck")
        t.start()
        # Fast-forwards join_bounded's deadline past its 5s budget on the very
        # first check, so the thread reads as abandoned without a real wait.
        clock = iter([0.0])

        def fake_monotonic():
            return next(clock, 100.0)

        try:
            with unittest.mock.patch.object(session.time, "monotonic", side_effect=fake_monotonic):
                with self.assertLogs("c64cast", level="ERROR") as cm:
                    session.join_playlists([t], stacks, stop_event)
            self.assertTrue(any("playlist-stuck" in m and "5s" in m for m in cm.output))
        finally:
            stop_event.set()
            t.join()


class TeardownStackOrderTest(unittest.TestCase):
    def _record_order(self) -> tuple[SystemStack, list[str]]:
        order: list[str] = []
        st = fake_system_stack("only")
        st.preview_window = MagicMock()
        st.preview_window.close.side_effect = lambda: order.append("preview")
        st.recorder = MagicMock()
        st.recorder.stop.side_effect = lambda: order.append("recorder")
        st.audio = MagicMock()
        st.audio.close.side_effect = lambda: order.append("audio")
        st.source = MagicMock()
        st.source.release.side_effect = lambda: order.append("source")
        st.api.reset.side_effect = lambda: order.append("reset")
        st.api.close.side_effect = lambda: order.append("api_close")
        return st, order

    def test_teardown_order(self):
        # Preview/recording first (avoid rendering after API close);
        # audio before reset (NMI timer can't fire into a cleared buffer);
        # api.reset → api.close; camera release last.
        st, order = self._record_order()
        teardown_stack(st)
        self.assertEqual(order, ["preview", "recorder", "audio", "reset", "api_close", "source"])

    def test_one_failure_doesnt_strand_remaining_steps(self):
        st, order = self._record_order()
        st.audio.close.side_effect = lambda: (_ for _ in ()).throw(RuntimeError("audio gone weird"))
        with self.assertLogs("c64cast", level="ERROR"):
            teardown_stack(st)
        # The failing step is skipped; everything after it still runs.
        self.assertEqual(order, ["preview", "recorder", "reset", "api_close", "source"])

    def test_missing_optional_resources_skipped(self):
        # framebuffer / preview_window / recorder are all None by default.
        st = fake_system_stack("only")
        teardown_stack(st)
        st.api.reset.assert_called_once()
        st.api.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
