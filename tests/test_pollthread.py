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

from c64cast._pollthread import PollThread


class ConstructorContractTest(unittest.TestCase):
    def test_periodic_mode_requires_period(self):
        with self.assertRaises(ValueError):
            PollThread(lambda: None, name="t")

    def test_manual_mode_rejects_period(self):
        with self.assertRaises(ValueError):
            PollThread(lambda stop: None, name="t", manual=True, period=1.0)


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
        # gives up after join_timeout and detaches the thread.
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
        poll.stop()
        self.assertLess(time.monotonic() - t0, 2.0, "stop() must not wait past join_timeout")
        self.assertFalse(poll.is_running())


if __name__ == "__main__":
    unittest.main()
