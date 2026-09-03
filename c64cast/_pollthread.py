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

`start()` and `stop()` are serialized against each other, because they are not
always called from the same thread: the session supervisor starts its reaper
from a build worker and stops it from whoever is shutting the host down.
Without that, a `stop()` landing between the moment `start()` publishes the
thread object and the moment it actually starts it joins a thread that has
never run, which `threading` rejects outright.

`stop()`'s join is bounded (`join_timeout`, default 0.5 s) so teardown never
hangs on a wedged target. If the worker is still alive when the join times
out, `stop()` keeps the thread reference instead of discarding it: `is_running()`
then keeps reporting True for as long as the abandoned worker actually runs,
and `start()`'s ordinary "already running" check refuses to spawn a
replacement — and therefore never calls `self._stop.clear()` — until it
finally exits. Discarding the reference on a timeout used to make both of
those lie, which let a `start()` after a timed-out `stop()` clear the one
shared stop `Event` out from under the still-running worker and resurrect it
alongside a second, freshly-started one.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Literal, overload

log = logging.getLogger(__name__)


class PollThread:
    @overload
    def __init__(
        self,
        target: Callable[[], None],
        *,
        name: str,
        period: float,
        run_first: bool = True,
        manual: Literal[False] = False,
        join_timeout: float = 0.5,
    ) -> None: ...

    @overload
    def __init__(
        self,
        target: Callable[[threading.Event], None],
        *,
        name: str,
        manual: Literal[True],
        run_first: bool = True,
        join_timeout: float = 0.5,
    ) -> None: ...

    def __init__(
        self,
        target: Callable[..., None],
        *,
        name: str,
        period: float | None = None,
        run_first: bool = True,
        manual: bool = False,
        join_timeout: float = 0.5,
    ) -> None:
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
        # Reentrant so a target that stops its own poller raises the
        # RuntimeError `Thread.join` gives for joining the current thread,
        # rather than deadlocking on the lock, where the cause would be much
        # harder to read.
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
                if self._thread.is_alive():
                    # Leave `self._thread` set rather than clearing it: a
                    # cleared reference would make `is_running()` lie (it
                    # reports False while this worker keeps running), and a
                    # later `start()` trusts that lie and calls
                    # `self._stop.clear()` — which this same worker reads
                    # dynamically, un-stopping it while a fresh thread also
                    # starts. Keeping the reference makes `start()`'s
                    # existing "already running" guard refuse the duplicate
                    # until this thread actually exits.
                    log.warning(
                        "PollThread %r did not stop within %.1fs; it is still "
                        "running and start() will refuse a replacement until it exits",
                        self._name,
                        self._join_timeout,
                    )
                    return
                self._thread = None

    def _run(self) -> None:
        if self._manual:
            self._call_target(self._target, self._stop)
            return
        assert self._period is not None
        if self._run_first:
            while not self._stop.is_set():
                if not self._call_target(self._target):
                    return
                self._stop.wait(self._period)
        else:
            while not self._stop.wait(self._period):
                if not self._call_target(self._target):
                    return

    def _call_target(self, target: Callable[..., None], *args: object) -> bool:
        """Run one target invocation. Returns False on an unhandled exception
        (the caller stops the loop rather than re-entering a target that just
        proved broken — the same "one bad call ends the loop" outcome as
        before, except the exception now reaches `log.exception` instead of
        bypassing all logging via `threading.excepthook` straight to stderr."""
        try:
            target(*args)
        except Exception:
            log.exception("PollThread %r target raised; stopping the loop", self._name)
            return False
        return True
