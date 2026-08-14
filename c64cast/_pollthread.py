"""Background daemon thread with start/stop boilerplate.

Most sites use the simple form — a function called repeatedly on a fixed
cadence with a stop event handling shutdown:

    self._poll = PollThread(self._fetch, period=10.0, name="rss-poll")
    self._poll.start()    # in setup()
    self._poll.stop()     # in teardown()

For variable-cadence loops (e.g. exponential backoff) pass `manual=True`
and the target owns its own loop and pacing:

    self._poll = PollThread(self._worker, name="obs-status", manual=True)
    # worker signature: def _worker(stop: threading.Event) -> None

`start()` and `stop()` are serialised against each other, because they are not
always called from the same thread: the session supervisor starts its reaper
from a build worker and stops it from whoever is shutting the host down.
Without that, a `stop()` landing between the moment `start()` publishes the
thread object and the moment it actually starts it joins a thread that has
never run, which `threading` rejects outright.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class PollThread:
    def __init__(
        self,
        target: Callable,
        *,
        name: str,
        period: float | None = None,
        run_first: bool = True,
        manual: bool = False,
        join_timeout: float = 0.5,
    ):
        if manual and period is not None:
            raise ValueError("PollThread: period only meaningful when manual=False")
        if not manual and period is None:
            raise ValueError("PollThread: period required when manual=False")
        self._target = target
        self._name = name
        self._period = period
        self._run_first = run_first
        self._manual = manual
        self._join_timeout = join_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Reentrant so a target that stops its own poller deadlocks on the
        # join it was always going to (joining the current thread), rather
        # than on the lock, where the cause would be much harder to read.
        self._lifecycle = threading.RLock()

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lifecycle:
            if self.is_running():
                return
            self._stop.clear()
            thread = threading.Thread(target=self._run, daemon=True, name=self._name)
            # Started before it is published, so `stop()` can never reach a
            # thread object that has not run yet.
            thread.start()
            self._thread = thread

    def stop(self) -> None:
        with self._lifecycle:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=self._join_timeout)
                self._thread = None

    def _run(self) -> None:
        if self._manual:
            self._target(self._stop)
            return
        assert self._period is not None
        if self._run_first:
            while not self._stop.is_set():
                self._target()
                self._stop.wait(self._period)
        else:
            while not self._stop.wait(self._period):
                self._target()
