"""Tests for the background-thread sandbox — the guard that fails a test which
leaves a thread running, because such a thread logs into whatever test runs
next (see tests/_thread_sandbox.py)."""

from __future__ import annotations

import threading
import unittest

import _thread_sandbox
from _thread_sandbox import ThreadLeak, check_no_strays


class _Released:
    """A thread that runs until the test that made it lets it go."""

    def __init__(self, test: unittest.TestCase, name: str) -> None:
        self.release = threading.Event()
        self.thread = threading.Thread(target=self.release.wait, name=name, daemon=True)
        test.addCleanup(self.thread.join)
        test.addCleanup(self.release.set)
        self.thread.start()


class CheckTest(unittest.TestCase):
    def test_a_thread_started_after_the_snapshot_and_still_running_is_a_leak(self):
        before = set(threading.enumerate())
        _Released(self, "a-leaked-loop")
        with self.assertRaises(ThreadLeak) as caught:
            check_no_strays(before)
        self.assertIn("a-leaked-loop", str(caught.exception))

    def test_a_thread_already_running_at_the_snapshot_is_not_this_test_s(self):
        held = _Released(self, "somebody-elses-loop")
        check_no_strays(set(threading.enumerate()))
        self.assertTrue(held.thread.is_alive(), "still running, and still not a leak")

    def test_a_thread_winding_down_inside_the_grace_is_not_a_leak(self):
        before = set(threading.enumerate())
        done = threading.Event()
        thread = threading.Thread(target=done.wait, name="winding-down", daemon=True)
        thread.start()
        done.set()  # released before the check, but not yet finished
        check_no_strays(before)
        self.assertFalse(thread.is_alive())


class ArmedTest(unittest.TestCase):
    """The guard is only worth anything if every test in the run is wearing it.
    Driving a leaking TestCase through the real machinery is what says so — an
    assertion about `unittest.TestCase.run` being patched would pass against a
    patch that checks nothing."""

    def test_the_suite_runs_with_the_guard_installed(self):
        self.assertTrue(_thread_sandbox._armed)

    def test_a_test_that_stops_its_thread_in_its_own_cleanup_passes(self):
        """The order the whole design rests on: the check goes on the cleanup
        stack before the test's own, so LIFO leaves it last — after `tearDown`,
        after `quiet_logging()`'s restore, after everything the test registered.
        Checked any earlier, a test that tidies up correctly is failed anyway."""

        class Tidy(unittest.TestCase):
            def runTest(self):
                _Released(self, "stopped-by-its-own-cleanup")

        result = unittest.TestResult()
        Tidy().run(result)

        self.assertEqual(result.failures, [], result.failures)
        self.assertEqual(result.errors, [], result.errors)

    def test_a_leaking_test_is_reported_as_a_failure_naming_the_thread(self):
        outer = self

        class Leaky(unittest.TestCase):
            def runTest(self):
                _Released(outer, "leaked-from-an-inner-test")

        result = unittest.TestResult()
        Leaky().run(result)

        self.assertEqual(len(result.failures), 1, result.failures)
        _test, trace = result.failures[0]
        self.assertIn("ThreadLeak", trace)
        self.assertIn("leaked-from-an-inner-test", trace)
