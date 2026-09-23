"""Tests for the per-test timeout sandbox — the guard that interrupts a test
which has stopped making progress, so a hang is a named error instead of a
silently consumed CI job (see tests/_timeout_sandbox.py)."""

from __future__ import annotations

import io
import os
import threading
import time
import unittest
from typing import Any
from unittest.mock import patch

import _timeout_sandbox
from _timeout_sandbox import TestTimedOut, configured_cap, interrupt, report, thread_dump

#: Long enough to clear the watchdog's own `_TICK_S`, short enough that the
#: test driving a deliberate hang costs about a second. Patched onto the
#: module, which is process-wide: under `unittest_parallel --thread` that would
#: reach the neighbors, and nothing in this repository passes `--thread`.
_TEST_CAP_S = 0.6

#: What a deliberately hung thread falls back on if the guard never reaches
#: it, so a broken guard costs one slow test instead of the whole run.
_GIVE_UP_S = 10.0

#: A thread id no thread has, for driving the watchdog against a registry
#: entry without a real test behind it. `PyThreadState_SetAsyncExc` returns 0
#: for an id it does not know, so the injection lands nowhere.
_UNUSED_IDENT = 2**32 - 1


class _Spinner:
    """A thread spinning in interpretable bytecode, which is where an async
    exception can actually be delivered."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.started = threading.Event()
        self.raised: BaseException | None = None
        self.thread = threading.Thread(target=self._spin, name="spins-on-purpose")
        test.addCleanup(self.thread.join)
        self.thread.start()
        self.started.wait(timeout=_GIVE_UP_S)

    @property
    def ident(self) -> int:
        ident = self.thread.ident
        assert ident is not None, "the thread was started in __init__"
        return ident

    def _spin(self) -> None:
        self.started.set()
        deadline = time.monotonic() + _GIVE_UP_S
        try:
            while time.monotonic() < deadline:
                time.sleep(0.005)
        except BaseException as exc:  # noqa: BLE001 — what arrives is the subject
            self.raised = exc


class CapTest(unittest.TestCase):
    def test_the_default_cap_clears_the_slowest_legitimate_test(self):
        """The slowest test in this suite measures about a second, so the cap
        is not a number anything honest can reach — one a real test could trip
        would be turned off the first week."""
        self.assertGreater(configured_cap(), 30.0)

    def test_the_environment_can_lower_the_cap(self):
        with patch.dict(os.environ, {_timeout_sandbox._CAP_ENV: "2.5"}):
            self.assertEqual(configured_cap(), 2.5)

    def test_a_cap_that_will_not_parse_keeps_the_default(self):
        """Falling back to the default leaves the guard armed. Falling back to
        'off' would turn a typo into an unwatched run that looks identical."""
        with patch.dict(os.environ, {_timeout_sandbox._CAP_ENV: "soon"}):
            self.assertEqual(configured_cap(), _timeout_sandbox._CAP_S)

    def test_a_cap_that_is_not_finite_keeps_the_default(self):
        """`inf` and `nan` both survive `float()`, and a deadline built from
        either never compares as passed — which arms the guard and enforces
        nothing, the failure that is indistinguishable from success."""
        for raw in ("inf", "-inf", "nan"):
            with self.subTest(raw=raw), patch.dict(os.environ, {_timeout_sandbox._CAP_ENV: raw}):
                self.assertEqual(configured_cap(), _timeout_sandbox._CAP_S)

    def test_a_cap_of_zero_leaves_the_suite_unwatched(self):
        """The escape hatch for stepping through a test under a debugger: no
        watchdog, and `_armed` saying so rather than claiming a cap nothing is
        enforcing."""
        self.addCleanup(setattr, _timeout_sandbox, "_cap_s", _timeout_sandbox._cap_s)
        self.addCleanup(setattr, _timeout_sandbox, "_armed", _timeout_sandbox._armed)
        wrapped = unittest.TestCase.run
        _timeout_sandbox._armed = False
        with patch.dict(os.environ, {_timeout_sandbox._CAP_ENV: "0"}):
            _timeout_sandbox.arm()
        self.assertFalse(_timeout_sandbox._armed)
        self.assertIs(unittest.TestCase.run, wrapped, "nothing was wrapped a second time")


class InterruptTest(unittest.TestCase):
    def test_the_dump_carries_every_thread_by_name(self):
        """The stack of the test's own thread is already in the reported
        error; the reason to dump at all is the thread it was waiting on."""
        spinner = _Spinner(self)
        dump = thread_dump()
        interrupt(spinner.ident)
        self.assertIn("spins-on-purpose", dump)
        self.assertIn("MainThread", dump)
        self.assertIn("_spin", dump)

    def test_the_interrupted_thread_receives_the_timeout(self):
        spinner = _Spinner(self)
        interrupt(spinner.ident)
        spinner.thread.join(timeout=_GIVE_UP_S)
        self.assertIsInstance(spinner.raised, TestTimedOut)
        self.assertIn("no progress for", str(spinner.raised))

    def test_the_report_names_the_test_and_dumps_the_threads(self):
        spinner = _Spinner(self)
        written = io.StringIO()
        report("tests.test_thing.Case.test_one", written)
        interrupt(spinner.ident)
        self.assertIn("tests.test_thing.Case.test_one", written.getvalue())
        self.assertIn("spins-on-purpose", written.getvalue())

    def test_a_report_that_cannot_be_written_costs_nothing(self):
        """It runs after the injection that actually ends the hang, so a
        stream closed at interpreter shutdown should lose the evidence and
        not the interruption."""
        closed = io.StringIO()
        closed.close()
        report("tests.test_thing.Case.test_one", closed)


class WatchdogTest(unittest.TestCase):
    def test_the_watchdog_thread_is_running(self):
        """`_armed` is a claim about a thread. A `tick` that raised would
        leave the flag true with nothing behind it, which is why `tick`
        contains its own failures — this is the assertion that says the
        thread outlived the run so far."""
        alive = [t.name for t in threading.enumerate() if t.is_alive()]
        self.assertIn("per-test-timeout-watchdog", alive)

    def test_the_registry_lock_is_free_while_a_report_is_being_written(self):
        """`_lock` is taken by every test to register and to deregister, so a
        watchdog that held it across a stalled stderr write would stop every
        remaining test in the worker from starting — and `Pool.map` never
        completes a job whose worker never returns. One hung test would become
        a hung run, which is the whole thing this module exists to prevent."""
        writing = threading.Event()
        release = threading.Event()

        class Blocks(io.StringIO):
            def write(self, s: str) -> int:
                writing.set()
                release.wait(timeout=_GIVE_UP_S)
                return super().write(s)

        watched = _timeout_sandbox._Watched(self.id(), time.monotonic() - 1.0)
        with _timeout_sandbox._lock:
            _timeout_sandbox._running[_UNUSED_IDENT] = watched
        self.addCleanup(_timeout_sandbox._running.pop, _UNUSED_IDENT, None)

        with patch.object(_timeout_sandbox, "_DUMP_STREAM", Blocks()):
            one_pass = threading.Thread(target=_timeout_sandbox.tick, name="one-watchdog-pass")
            self.addCleanup(one_pass.join)
            self.addCleanup(release.set)
            one_pass.start()
            self.assertTrue(writing.wait(timeout=_GIVE_UP_S), "the pass never reached the report")
            free = _timeout_sandbox._lock.acquire(timeout=1.0)
            if free:
                _timeout_sandbox._lock.release()

        self.assertTrue(free, "the watchdog held the registry lock across the stderr write")


class ArmedTest(unittest.TestCase):
    """The guard is only worth anything if every test in the run is wearing
    it. Driving a deliberately hung TestCase through the real machinery is
    what says so — an assertion that `unittest.TestCase.run` is patched would
    pass against a patch that watches nothing."""

    def test_the_suite_runs_with_the_guard_installed(self):
        self.assertTrue(_timeout_sandbox._armed)

    def test_a_hung_test_is_reported_as_an_error_naming_it(self):
        outer = self
        result = unittest.TestResult()
        report = io.StringIO()

        class Hangs(unittest.TestCase):
            def runTest(self):
                deadline = time.monotonic() + _GIVE_UP_S
                while time.monotonic() < deadline:
                    time.sleep(0.005)
                outer.fail("the watchdog never interrupted this")

        # On a thread of its own, because a TestCase driven from inside a test
        # on the *same* thread is deliberately left to the outer test's
        # deadline, which is a minute away.
        case = Hangs()
        runner = threading.Thread(target=case.run, args=(result,), name="hangs-on-purpose")
        self.addCleanup(runner.join)
        with (
            patch.object(_timeout_sandbox, "_cap_s", _TEST_CAP_S),
            patch.object(_timeout_sandbox, "_DUMP_STREAM", report),
        ):
            runner.start()
            runner.join(timeout=_GIVE_UP_S + 5.0)
            ident = runner.ident

        self.assertFalse(runner.is_alive(), "the guard did not interrupt the hung test")
        self.assertEqual(result.failures, [], result.failures)
        self.assertEqual(len(result.errors), 1, result.errors)
        _case, trace = result.errors[0]
        self.assertIn("TestTimedOut", trace)
        self.assertIn("no progress for", trace)
        # Waited for rather than asserted outright: the watchdog writes the
        # report after releasing `_lock`, on purpose, so the interrupted thread
        # can deregister without queueing behind a stalled stderr. That also
        # means it can still be writing when the thread is already gone.
        deadline = time.monotonic() + _GIVE_UP_S
        while case.id() not in report.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(case.id(), report.getvalue())
        # An entry left behind is a later test injected into for a hang that
        # was never its own.
        self.assertNotIn(ident, _timeout_sandbox._running)

    def test_a_testcase_driven_from_inside_a_test_keeps_the_outer_deadline(self):
        ident = threading.get_ident()
        mine = _timeout_sandbox._running[ident]
        outer = self

        class Inner(unittest.TestCase):
            def runTest(self):
                outer.assertIs(_timeout_sandbox._running[ident], mine)

        result = unittest.TestResult()
        Inner().run(result)
        self.assertEqual(result.errors, [], result.errors)
        self.assertIs(_timeout_sandbox._running[ident], mine, "the outer test is still watched")

    def test_a_timeout_arriving_during_retirement_is_recorded_not_raised(self):
        """The third blind spot: a re-injection can land once
        `testPartExecutor` is off the stack. Out of `TestCase.run` it would end
        the worker's thread, and `Pool.map` never completes a job whose worker
        died — so the guard against hanging would itself be the hang. It has to
        be caught, charged to the test, and retirement retried, or the stale
        registry entry re-injects into every later test on that thread."""
        retired: list[int] = []
        real = _timeout_sandbox._retire

        def retire(ident: int, watched: Any) -> None:
            retired.append(ident)
            if len(retired) == 1:
                raise TestTimedOut
            real(ident, watched)

        class Ordinary(unittest.TestCase):
            def runTest(self):
                pass

        case = Ordinary()
        result = unittest.TestResult()
        escaped: list[BaseException | None] = []
        # On a thread of its own: a TestCase driven from inside a test on the
        # same thread is not the registration's owner and never retires.
        runner = threading.Thread(target=case.run, args=(result,), name="retires-on-purpose")
        self.addCleanup(runner.join)
        with (
            patch.object(_timeout_sandbox, "_retire", retire),
            patch.object(threading, "excepthook", lambda args: escaped.append(args.exc_value)),
        ):
            runner.start()
            runner.join(timeout=_GIVE_UP_S)
            ident = runner.ident

        self.assertEqual(escaped, [], "the timeout left TestCase.run and ended the thread")
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("TestTimedOut", result.errors[0][1])
        self.assertEqual(retired, [ident, ident], "retirement was not retried after the timeout")
        self.assertNotIn(ident, _timeout_sandbox._running)
