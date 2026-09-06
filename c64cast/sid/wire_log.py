"""Throttled logging for warnings a byte off the wire can trigger.

An ASID host is untrusted input, and every warning on the decode/serialize path
costs the MIDI reader thread real time: ``configure_logging``'s default terminal
handler renders a record through Rich at ~322 us (vs 9.8 us for a plain
StreamHandler, 24.9 us for the redacting FileHandler, 14.5 us for
``SessionLogBuffer``). A warning that fires once per message therefore lets the
*sender* choose the reader's throughput — 64 records inside one
:func:`c64cast._midi.poll_pending` pass is 20.6 ms on a loop that is otherwise
sub-millisecond — and, because ``--log-file`` is a plain unrotated
``FileHandler``, it fills the disk at megabytes a second while taking the global
logging lock the render and audio threads also contend for.

So the rule on this path is: **wire-triggered logging is O(1) per stream, not
O(1) per message**, and :class:`LogThrottle` is the one implementation of it.
Each site owns an instance rather than sharing one — a shared instance would let
a noisy site swallow a quiet site's first report — but they all share this code,
because two hand-rolled copies of a gate is how the two drift and six is how the
seventh site forgets.

Two design points worth not re-deriving:

* **The interval, not the level, is what bounds the cost.** Demoting a repeat to
  DEBUG is no bound at all under ``-vv``, where a DEBUG record costs the same
  322 us. So a repeat inside the window emits *nothing* and is counted; the
  count rides on the next record that does go out.
* **The level says whether the site is flooding.** A record that stands for a
  single occurrence is news and goes out at WARNING; a record that stands for
  several ("and 900 more in the last second") is a flood you have already been
  told about, and goes out at DEBUG. That makes a lone recurrence an hour later
  visible again without a per-activation reset.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

# Longest a repeating wire-triggered condition may go unreported, and the whole
# cost budget this module is allowed to spend: one record per site per second.
# At the fastest frame rate the ASID protocol can express (960 Hz) that turns
# 960 records into one, and 322 us/s through the default terminal handler is
# 0.03% of the reader thread. Shorter buys nothing a human reading a log can
# use; longer starts to hide a condition that began after the previous report.
THROTTLE_INTERVAL_S = 1.0


class LogThrottle:
    """One wire-triggered warning site's report budget.

    :meth:`warn` is called on **every** occurrence — the throttle, not the
    caller, decides whether a record is emitted, so a call site cannot forget
    the gate or spell it differently. Safe to share across threads: the counter
    moves under a lock, which costs ~10x less than the level check on the
    ``log.warning`` call it replaces.
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

    def warn(self, msg: str, *args: object) -> None:
        """Record one occurrence of this site's condition, emitting at most one
        log record per :data:`THROTTLE_INTERVAL_S`.

        ``msg``/``args`` are the ordinary lazy %-style logging pair, so the
        formatting a suppressed occurrence would have cost is never paid."""
        with self._lock:
            self._since_report += 1
            now = self._monotonic()
            previous = self._reported_at
            if previous is not None and now - previous < self._interval_s:
                return
            stands_for = self._since_report
            self._since_report = 0
            self._reported_at = now

        if stands_for == 1:
            self._log.warning(msg, *args)
            return
        self._log.debug(msg + " [and %d more since the previous report]", *args, stands_for - 1)

    def reset(self) -> None:
        """Forget the stream so far, so the next occurrence reports afresh."""
        with self._lock:
            self._since_report = 0
            self._reported_at = None
