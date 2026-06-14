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

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=self._name)
        self._thread.start()

    def stop(self) -> None:
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
