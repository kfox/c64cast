"""Throttled logging for warnings a byte off the wire can trigger.

The rule on this path is **wire-triggered logging is O(1) per stream, not O(1)
per message**, and :class:`LogThrottle` is the one implementation of it. A free
function takes its stream's throttle as a required argument rather than
defaulting to one, because a default would be per *process*.

Two design points worth not re-deriving:

* **The interval, not the level, is what bounds the cost.** Demoting a repeat to
  DEBUG is no bound at all under ``-v``, where a DEBUG record costs the same
  322 us. So a repeat inside the window emits *nothing* and is counted; the
  count rides on the next record that does go out.
* **The level says whether the site is flooding.** A record that stands for a
  single occurrence is news and goes out at WARNING; a record that stands for
  several ("[and 900 more since the previous report]") is a flood you have
  already been told about, and goes out at DEBUG. That makes a lone recurrence
  an hour later visible again without a per-activation reset. The count is
  "since the previous report", not "in the last second": the interval bounds
  how *often* a record goes out, not how far back one reaches, so a site that
  fires twice an hour apart reports a span of an hour.

See docs/architecture/config.md#_wire_logpy--wire-triggered-logging-is-o1-per-stream.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

# Longest a repeating wire-triggered condition may go unreported: one record per
# site per second. `tests/test_wire_log.py` holds it to a 0.5-5.0 s band.
THROTTLE_INTERVAL_S = 1.0

_MORE_SUFFIX = " [and %d more since the previous report]"


class LogThrottle:
    """One wire-triggered warning site's report budget.

    :meth:`warn` (or :meth:`exception`) is called on **every** occurrence — the
    throttle, not the caller, decides whether a record is emitted, so a call
    site cannot forget the gate or spell it differently. Safe to share across
    threads: the counter moves under a lock.

    That lock is not free, and the number to hold in mind is the ratio to what
    it buys, not to what it replaces. Measured in this checkout's venv (CPython
    3.14.6, arm64 macOS, 300k iterations, best of 7): a suppressed
    ``LogThrottle.warn()`` costs ~206 ns against ~90 ns for the level-rejected
    ``log.warning()`` it stands in front of, so the gate is about **2x more
    expensive than the call it replaces** — of which ~82 ns is the uncontended
    lock itself. It is worth paying anyway, because the comparison that decides
    whether a site can afford this gate is not that one: it is 206 ns against
    the ~322 us record the gate suppresses, a factor of ~1,600. On the
    ``exception`` sites the margin is wider still, since what is suppressed
    there is an ERROR with a rendered traceback that no level check would have
    rejected.
    """

    def __init__(
        self,
        logger: logging.Logger,
        *,
        interval_s: float = THROTTLE_INTERVAL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = logger
        self._interval_s = interval_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._since_report = 0
        self._reported_at: float | None = None

    def _admit(self) -> int:
        """Count one occurrence and return how many occurrences the record that
        should now go out stands for — 0 when this occurrence is suppressed."""
        with self._lock:
            self._since_report += 1
            now = self._monotonic()
            previous = self._reported_at
            if previous is not None and now - previous < self._interval_s:
                return 0
            stands_for = self._since_report
            self._since_report = 0
            self._reported_at = now
            return stands_for

    @property
    def logger(self) -> logging.Logger:
        """The logger this site's records go to."""
        return self._log

    def warn(self, msg: str, *args: object) -> None:
        """Record one occurrence of this site's condition, emitting at most one
        log record per :data:`THROTTLE_INTERVAL_S`.

        ``msg``/``args`` are the ordinary lazy %-style logging pair, so the
        formatting a suppressed occurrence would have cost is never paid."""
        stands_for = self._admit()
        if not stands_for:
            return
        if stands_for == 1:
            self._log.warning(msg, *args)
            return
        self._log.debug(msg + _MORE_SUFFIX, *args, stands_for - 1)

    def exception(self, msg: str, *args: object) -> None:
        """:meth:`warn` for a site inside an ``except`` block: the emitted
        record carries the active exception's traceback.

        Same gate, one level up — a single occurrence goes out at ERROR (what
        ``log.exception`` does), a record standing for several at DEBUG with the
        count. The traceback rides on both, because it is the whole diagnostic
        value here and the *interval*, not the level, is what bounds the cost.
        Must be called from an active exception handler, exactly like
        ``log.exception``."""
        stands_for = self._admit()
        if not stands_for:
            return
        if stands_for == 1:
            self._log.exception(msg, *args)
            return
        self._log.debug(msg + _MORE_SUFFIX, *args, stands_for - 1, exc_info=True)
