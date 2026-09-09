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

The rule is about a *wire*, not about ASID, so the gate is not ASID's alone:
``control/midi_control.py`` logged a full traceback per message it could not
handle — on a dispatch failure, a clock-feed failure, and a mapped action
failing against one system — inside an unbounded drain on a live-performance
control surface, and all three take :meth:`LogThrottle.exception` for exactly
the reason above. So it sits at the package root beside the other cross-cutting
utilities: it imports nothing from the package, and its consumers are in two
different subpackages. Under ``sid/`` — where the first two sites put it — the
``control/`` consumer was the tree's only import from ``control/`` into
``sid/`` — the same shape ``hw/backend.py`` twice refuses in as many words for
``hw/``, and it went unremarked here only because a throttle looks like a SID
detail.

Two design points worth not re-deriving:

* **The interval, not the level, is what bounds the cost.** Demoting a repeat to
  DEBUG is no bound at all under ``-vv``, where a DEBUG record costs the same
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

# Appended to the message of a record that stands for more than one occurrence.
# One spelling, so the two emitters can't drift and a log reader sees one shape.
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
        """The logger this site's records go to. Exposed so a test standing a
        throttle in for another one does not have to guess it from a module
        name — a logger whose name is not its module's would send the record
        somewhere the test's `assertLogs` is not watching, and the failure
        would read as the gate's."""
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

    def reset(self) -> None:
        """Forget the stream so far, so the next occurrence reports afresh."""
        with self._lock:
            self._since_report = 0
            self._reported_at = None
