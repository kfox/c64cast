"""Direct contract tests for c64cast._pollthread.PollThread.

Ten-plus modules run their worker threads through this one class, and
every lifecycle bug it could grow (stale stop event on restart, start
spawning a second thread, stop hanging on a stuck worker) would surface
as an unrelated-looking flake in a consumer's test. Everything here
waits on an observable (an event the worker sets, a semaphore, a join)
rather than sleeping — the same discipline the consumer tests follow.
"""

from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Callable
from unittest import mock

from _fakes import quiet_logging

from c64cast._pollthread import PollThread


class ConstructorContractTest(unittest.TestCase):
    """These deliberately construct with a bad flag/target combo — exactly
    what the `@overload`s on `__init__` now catch statically for a typed call
    site — to prove the runtime guard still refuses one built dynamically
    (e.g. `**kwargs`) or from an untyped caller. Both lines are therefore
    expected type errors, not just expected runtime ones."""

    def test_periodic_mode_requires_period(self):
        with self.assertRaises(ValueError):
            PollThread(lambda: None, name="t")  # pyright: ignore[reportCallIssue]

    def test_manual_mode_rejects_period(self):
        with self.assertRaises(ValueError):
            PollThread(lambda stop: None, name="t", manual=True, period=1.0)  # pyright: ignore[reportArgumentType]


class PeriodicModeTest(unittest.TestCase):
    def test_run_first_calls_target_before_first_period(self):
        # period is a minute: the only way `called` fires inside the wait
        # below is the run_first immediate call.
        called = threading.Event()
        poll = PollThread(called.set, name="t", period=60.0)
        poll.start()
        self.addCleanup(poll.stop)
        self.assertTrue(called.wait(2.0), "run_first=True must call the target immediately")

    def test_run_first_false_never_calls_before_a_period_elapses(self):
        # Deterministic negative: with run_first=False the target only runs
        # after stop.wait(period) returns False, which a 60 s period cannot
        # do inside this test — no sleep needed, any scheduling outcome
        # gives calls == 0.
        calls: list[int] = []
        poll = PollThread(lambda: calls.append(1), name="t", period=60.0, run_first=False)
        poll.start()
        poll.stop()
        self.assertEqual(calls, [])

    def test_run_first_false_calls_after_the_period(self):
        called = threading.Event()
        poll = PollThread(called.set, name="t", period=0.001, run_first=False)
        poll.start()
        self.addCleanup(poll.stop)
        self.assertTrue(called.wait(2.0))

    def test_target_is_called_repeatedly(self):
        third_call = threading.Event()
        calls = 0

        def target() -> None:
            nonlocal calls
            calls += 1
            if calls >= 3:
                third_call.set()

        poll = PollThread(target, name="t", period=0.001)
        poll.start()
        self.addCleanup(poll.stop)
        self.assertTrue(third_call.wait(2.0), "periodic mode must re-invoke the target")

    def test_stop_interrupts_a_long_period_wait(self):
        # After the immediate first call the loop parks in stop.wait(60);
        # stop() must unblock it, not ride out the period.
        called = threading.Event()
        poll = PollThread(called.set, name="t", period=60.0)
        poll.start()
        self.assertTrue(called.wait(2.0))
        t0 = time.monotonic()
        poll.stop()
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertFalse(poll.is_running())


class ManualModeTest(unittest.TestCase):
    def test_worker_receives_the_poll_stop_event(self):
        seen: dict[str, threading.Event] = {}
        started = threading.Event()

        def worker(stop: threading.Event) -> None:
            seen["stop"] = stop
            started.set()
            stop.wait()

        poll = PollThread(worker, name="w", manual=True)
        poll.start()
        self.assertTrue(started.wait(2.0))
        self.assertIs(seen["stop"], poll.stop_event)
        self.assertTrue(poll.is_running())
        poll.stop()
        self.assertFalse(poll.is_running())

    def test_worker_return_ends_the_thread_without_stop(self):
        # A manual worker owns its own loop; when it decides to exit, the
        # poll winds down on its own — no second lap, no zombie thread.
        done = threading.Event()
        poll = PollThread(lambda stop: done.set(), name="w", manual=True)
        poll.start()
        self.assertTrue(done.wait(2.0))
        deadline = time.monotonic() + 2.0
        while poll.is_running() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(poll.is_running())


class LifecycleTest(unittest.TestCase):
    def _blocking_poll(self) -> tuple[PollThread, threading.Semaphore]:
        run_started = threading.Semaphore(0)

        def worker(stop: threading.Event) -> None:
            run_started.release()
            stop.wait()

        return PollThread(worker, name="w", manual=True), run_started

    def test_stop_without_start_is_a_no_op(self):
        poll, _ = self._blocking_poll()
        poll.stop()
        self.assertFalse(poll.is_running())

    def test_start_while_running_does_not_spawn_a_second_thread(self):
        idents: list[int] = []
        run_started = threading.Semaphore(0)

        def worker(stop: threading.Event) -> None:
            idents.append(threading.get_ident())
            run_started.release()
            stop.wait()

        poll = PollThread(worker, name="w", manual=True)
        poll.start()
        self.assertTrue(run_started.acquire(timeout=2.0))
        poll.start()  # must be a no-op — already running
        poll.stop()
        self.assertEqual(len(idents), 1)

    def test_restart_after_stop_runs_the_worker_again(self):
        poll, run_started = self._blocking_poll()
        poll.start()
        self.assertTrue(run_started.acquire(timeout=2.0))
        poll.stop()

        poll.start()
        # The restart must present a CLEAR stop event to the new worker —
        # a stale set event from the previous stop() would make every
        # restarted loop exit on its first wait. start() clears it
        # synchronously before spawning, so no race in this read.
        self.assertFalse(poll.stop_event.is_set())
        self.assertTrue(run_started.acquire(timeout=2.0), "second start must run the worker again")
        poll.stop()

    def test_stop_returns_after_join_timeout_when_worker_hangs(self):
        # A worker that ignores its stop event must not hang teardown: stop()
        # gives up after join_timeout. It must not pretend the thread is gone,
        # though — see test_start_after_a_timed_out_stop_refuses_a_duplicate.
        hang = threading.Event()
        started = threading.Event()

        def stuck_worker(stop: threading.Event) -> None:
            started.set()
            hang.wait()

        poll = PollThread(stuck_worker, name="w", manual=True, join_timeout=0.05)
        self.addCleanup(hang.set)  # let the daemon thread die at test end
        poll.start()
        self.assertTrue(started.wait(2.0))
        t0 = time.monotonic()
        with self.assertLogs("c64cast._pollthread", level="WARNING"):
            poll.stop()
        self.assertLess(time.monotonic() - t0, 2.0, "stop() must not wait past join_timeout")
        self.assertTrue(
            poll.is_running(), "the worker really is still running; is_running() must say so"
        )

    def test_start_after_a_timed_out_stop_refuses_a_duplicate(self):
        # The sharp edge behind the module's top adverse-review finding: a
        # stop() that gives up on a hung worker used to clear self._thread,
        # so a later start() saw is_running() == False, called
        # self._stop.clear() — un-stopping the still-running abandoned
        # worker, which reads the same Event dynamically — and spawned a
        # second thread on top of it. start() must refuse instead.
        hang = threading.Event()
        started = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def stuck_worker(stop: threading.Event) -> None:
            nonlocal calls
            with calls_lock:
                calls += 1
            started.set()
            hang.wait()

        poll = PollThread(stuck_worker, name="w", manual=True, join_timeout=0.05)
        self.addCleanup(hang.set)
        poll.start()
        self.assertTrue(started.wait(2.0))
        started.clear()

        with self.assertLogs("c64cast._pollthread", level="WARNING"):
            poll.stop()
        self.assertTrue(poll.is_running())

        poll.start()  # must be a no-op: the old worker is still alive
        self.assertFalse(
            started.wait(0.2), "start() must not spawn a second worker over a live one"
        )
        with calls_lock:
            self.assertEqual(calls, 1, "the abandoned worker must not have been resurrected either")

        hang.set()  # let the abandoned worker finish
        deadline = time.monotonic() + 2.0
        while poll.is_running() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(poll.is_running(), "is_running() must self-correct once the worker exits")

        poll.start()  # now it is safe, and must actually run
        self.assertTrue(started.wait(2.0))
        with calls_lock:
            self.assertEqual(calls, 2)
        poll.stop()

    def test_a_raised_target_is_logged_not_lost_to_threading_excepthook(self):
        calls = 0

        def flaky() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")

        poll = PollThread(flaky, name="t", period=0.001)
        with self.assertLogs("c64cast._pollthread", level="ERROR") as logs:
            poll.start()
            deadline = time.monotonic() + 2.0
            while poll.is_running() and time.monotonic() < deadline:
                time.sleep(0.005)
        self.addCleanup(poll.stop)
        self.assertFalse(
            poll.is_running(), "the loop must stop rather than re-entering a broken target"
        )
        self.assertEqual(calls, 1, "a raised target must not be retried")
        self.assertIn("target raised", "".join(logs.output))

    def test_a_raised_manual_target_is_logged(self):
        def flaky(stop: threading.Event) -> None:
            raise RuntimeError("boom")

        poll = PollThread(flaky, name="w", manual=True)
        with self.assertLogs("c64cast._pollthread", level="ERROR") as logs:
            poll.start()
            deadline = time.monotonic() + 2.0
            while poll.is_running() and time.monotonic() < deadline:
                time.sleep(0.005)
        self.addCleanup(poll.stop)
        self.assertFalse(poll.is_running())
        self.assertIn("target raised", "".join(logs.output))


class ConcurrentLifecycleTest(unittest.TestCase):
    """`start()` and `stop()` are not always called from the same thread.

    The session supervisor starts its reaper from a build worker and stops it
    from whoever is shutting the host down, which is what turned a latent
    ordering bug here into an intermittent CI failure — on two matrix legs out
    of twelve, since it needs the two calls to interleave."""

    def test_a_stop_racing_a_start_does_not_join_an_unstarted_thread(self):
        # The window was between publishing the thread object and starting it:
        # a `stop()` arriving there found a non-None `_thread` that had never
        # run, and `Thread.join` rejects that outright. Held open here rather
        # than hunted for, so the test fails deterministically without the fix.
        inside_start = threading.Event()
        release = threading.Event()
        original = threading.Thread.start

        def slow_start(thread: threading.Thread) -> None:
            if thread.name == "racy":
                inside_start.set()
                release.wait(2.0)
            original(thread)

        poll = PollThread(lambda: None, name="racy", period=60.0)
        self.addCleanup(poll.stop)

        with mock.patch.object(threading.Thread, "start", slow_start):
            starter = threading.Thread(target=poll.start, name="starter")
            starter.start()
            self.assertTrue(inside_start.wait(2.0))

            failure: list[BaseException] = []

            def stopper() -> None:
                try:
                    poll.stop()
                except BaseException as e:  # noqa: BLE001 - the assertion is the raise
                    failure.append(e)

            stopping = threading.Thread(target=stopper, name="stopper")
            stopping.start()
            release.set()
            starter.join(2.0)
            stopping.join(2.0)

        self.assertEqual(failure, [], f"stop() raced start(): {failure}")

    def test_hammering_start_and_stop_from_two_threads_stays_quiet(self):
        poll = PollThread(lambda: None, name="hammer", period=0.001, join_timeout=0.05)
        self.addCleanup(poll.stop)
        failures: list[BaseException] = []
        done = threading.Event()

        def churn(action: Callable[[], None]) -> None:
            try:
                while not done.is_set():
                    action()
            except BaseException as e:  # noqa: BLE001 - the assertion is the raise
                failures.append(e)

        threads = [
            threading.Thread(target=churn, args=(poll.start,)),
            threading.Thread(target=churn, args=(poll.stop,)),
        ]
        # A stop() landing on a scheduling hiccup could, in principle, outlive
        # its 0.05 s join and log a warning — incidental to what this test
        # asserts (no exception escapes the hammering), unlike the two tests
        # above that assert that exact message on purpose.
        with quiet_logging():
            for t in threads:
                t.start()
            # Bounded by iterations rather than by a clock: enough interleavings
            # to have caught the original bug, and no wall-time in the suite.
            for _ in range(2000):
                poll.is_running()
            done.set()
            for t in threads:
                t.join(2.0)
        self.assertEqual(failures, [], f"concurrent start/stop raised: {failures}")


if __name__ == "__main__":
    unittest.main()
