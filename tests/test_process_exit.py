"""Tests for cli.ensure_exit / cli.run — the process-exit backstop.

`main()` itself is exercised end-to-end by test_cli_parser.py and friends;
these cover only the guarantee layered on top of it, so a test never has to
call the real `os._exit` (which would kill the worker process running the
suite). See the docstring on `ensure_exit` for why the backstop exists —
`threading._shutdown()` re-joins any non-daemon thread untimed and with no
signal delivery, after `main()` has already returned its exit code.

`ensure_exit` takes its thread list via the injected `lingering` callable
rather than reading `threading.enumerate()` directly, precisely so these
tests don't have to reason about every other test's threads in a shared,
parallelized test process — only `LingeringThreadsTest` looks at the real
one, and it only asserts membership, never that the process is otherwise
quiet."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from c64cast.app.cli import _lingering_threads, ensure_exit, run


class EnsureExitTest(unittest.TestCase):
    def test_returns_the_code_untouched_when_nothing_lingers(self):
        hard_exit = mock.Mock(name="hard_exit")
        code = ensure_exit(3, grace_s=0.05, lingering=lambda: [], hard_exit=hard_exit)
        self.assertEqual(code, 3)
        hard_exit.assert_not_called()

    def test_a_thread_that_finishes_within_the_grace_period_needs_no_force(self):
        event = threading.Event()
        t = threading.Thread(target=event.wait, name="winding-down", daemon=False)
        t.start()
        timer = threading.Timer(0.05, event.set)
        timer.start()
        try:
            hard_exit = mock.Mock(name="hard_exit")
            code = ensure_exit(
                0,
                grace_s=1.0,
                lingering=lambda: [t] if t.is_alive() else [],
                hard_exit=hard_exit,
            )
        finally:
            timer.cancel()
            t.join()
        self.assertEqual(code, 0)
        hard_exit.assert_not_called()

    def test_a_thread_that_outlives_the_grace_period_forces_the_exit(self):
        event = threading.Event()
        t = threading.Thread(target=event.wait, name="stuck-thread", daemon=False)
        t.start()
        try:
            hard_exit = mock.Mock(name="hard_exit")
            with self.assertLogs("c64cast", level="ERROR") as cm:
                code = ensure_exit(7, grace_s=0.05, lingering=lambda: [t], hard_exit=hard_exit)
            self.assertEqual(code, 7)
            hard_exit.assert_called_once_with(7)
            self.assertTrue(any("stuck-thread" in m for m in cm.output))
        finally:
            event.set()
            t.join()


class LingeringThreadsTest(unittest.TestCase):
    """The real thread-lister: only membership is asserted, never that the
    whole process is otherwise quiet — a shared, parallelized test process
    can have other tests' threads alive at the same time."""

    def test_an_alive_non_daemon_thread_is_lingering(self):
        event = threading.Event()
        t = threading.Thread(target=event.wait, name="a-lingerer", daemon=False)
        t.start()
        try:
            self.assertIn(t, _lingering_threads())
        finally:
            event.set()
            t.join()

    def test_a_daemon_thread_never_counts(self):
        event = threading.Event()
        t = threading.Thread(target=event.wait, name="a-daemon", daemon=True)
        t.start()
        try:
            self.assertNotIn(t, _lingering_threads())
        finally:
            event.set()
            t.join()

    def test_a_finished_thread_never_counts(self):
        t = threading.Thread(target=lambda: None, name="already-done", daemon=False)
        t.start()
        t.join()
        self.assertNotIn(t, _lingering_threads())

    def test_the_main_thread_never_counts(self):
        self.assertNotIn(threading.main_thread(), _lingering_threads())


class RunTest(unittest.TestCase):
    def test_run_passes_mains_code_through_ensure_exit(self):
        with mock.patch("c64cast.app.cli.main", return_value=42) as main_mock:
            with mock.patch("c64cast.app.cli.ensure_exit", return_value=42) as ensure_mock:
                self.assertEqual(run(["--version"]), 42)
        main_mock.assert_called_once_with(["--version"])
        ensure_mock.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
