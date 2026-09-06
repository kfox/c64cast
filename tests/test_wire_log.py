"""The shared wire-triggered log throttle (c64cast/sid/wire_log.py).

The throttle exists because a byte on the ASID wire can otherwise buy an
unbounded amount of work inside a bounded reader: one WARNING through the
default terminal handler costs ~322 us, and a per-message warning lets the
sender pick the reader's throughput. So these tests pin the two properties that
bound the cost — at most one record per interval, whatever the occurrence rate,
and the interval read off the *injected* clock rather than the wall clock — plus
the level rule that keeps a lone recurrence visible.

`assertLogs` is used throughout, so nothing here may nest `quiet_logging()`.
"""

from __future__ import annotations

import logging
import unittest

from c64cast.sid.wire_log import THROTTLE_INTERVAL_S, LogThrottle


class _StepClock:
    """A monotonic stand-in that advances by `step` on every read, so a test can
    cross the throttle's window without sleeping through it."""

    def __init__(self, step: float, start: float = 1000.0) -> None:
        self.step = step
        self.now = start

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class LogThrottleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.log = logging.getLogger("c64cast.tests.wire_log")

    def _throttle(self, step: float = 0.0) -> LogThrottle:
        return LogThrottle(self.log, monotonic=_StepClock(step))

    def test_the_first_occurrence_is_a_warning_worded_exactly_as_written(self):
        throttle = self._throttle()
        with self.assertLogs(self.log, "DEBUG") as caught:
            throttle.warn("asid: %d pairs", 400)
        self.assertEqual(len(caught.records), 1)
        self.assertEqual(caught.records[0].levelname, "WARNING")
        self.assertEqual(caught.records[0].getMessage(), "asid: 400 pairs")

    def test_a_flood_inside_the_window_costs_exactly_one_record(self):
        # The property the whole class exists for: O(1) per stream, not per
        # message. 960 is the fastest ASID frame rate the protocol can express.
        throttle = self._throttle()
        with self.assertLogs(self.log, "DEBUG") as caught:
            for _ in range(960):
                throttle.warn("truncated %d ops", 7)
        self.assertEqual(len(caught.records), 1)
        self.assertEqual(caught.records[0].levelname, "WARNING")

    def test_a_repeat_report_lands_at_debug_and_carries_the_count_it_stands_for(self):
        throttle = self._throttle(step=THROTTLE_INTERVAL_S / 4)
        with self.assertLogs(self.log, "DEBUG") as caught:
            for _ in range(9):  # clock crosses the window every 4th read
                throttle.warn("truncated %d ops", 7)
        levels = [r.levelname for r in caught.records]
        self.assertEqual(levels, ["WARNING", "DEBUG", "DEBUG"])
        self.assertIn("and 3 more since the previous report", caught.records[1].getMessage())
        self.assertIn("truncated 7 ops", caught.records[1].getMessage())

    def test_a_lone_recurrence_after_a_quiet_gap_is_a_warning_again(self):
        # The level says whether the site is *flooding*: a record standing for a
        # single occurrence is news even when it isn't the first one, so a
        # second scene activation doesn't have to reset anything to be heard.
        throttle = self._throttle(step=THROTTLE_INTERVAL_S * 10)
        with self.assertLogs(self.log, "DEBUG") as caught:
            throttle.warn("lonely")
            throttle.warn("lonely")
        self.assertEqual([r.levelname for r in caught.records], ["WARNING", "WARNING"])

    def test_the_window_is_read_off_the_injected_clock_not_the_wall_clock(self):
        # Trap shape 5: a throttle that consulted time.monotonic() directly would
        # emit one record here (the loop takes microseconds), so this fails the
        # moment the injected clock stops being the one the gate reads.
        throttle = self._throttle(step=THROTTLE_INTERVAL_S * 2)
        with self.assertLogs(self.log, "DEBUG") as caught:
            for _ in range(6):
                throttle.warn("every read crosses the window")
        self.assertEqual(len(caught.records), 6)

    def test_reset_makes_the_next_occurrence_report_afresh(self):
        throttle = self._throttle()
        with self.assertLogs(self.log, "DEBUG") as caught:
            throttle.warn("first")
            throttle.warn("suppressed")
            throttle.reset()
            throttle.warn("first again")
        self.assertEqual([r.getMessage() for r in caught.records], ["first", "first again"])

    def test_two_sites_do_not_share_a_budget(self):
        # Why each site owns an instance rather than sharing one: a site the wire
        # can flood must not be able to swallow another site's first report.
        noisy = self._throttle()
        quiet = self._throttle()
        with self.assertLogs(self.log, "DEBUG") as caught:
            for _ in range(100):
                noisy.warn("noisy")
            quiet.warn("quiet")
        self.assertEqual([r.getMessage() for r in caught.records], ["noisy", "quiet"])


class ThrottleIntervalTest(unittest.TestCase):
    def test_the_interval_is_short_enough_to_read_and_long_enough_to_bound(self):
        # A sanity band, not a restatement: at 960 Hz this is a 960x reduction,
        # and a human tailing a log still sees the condition inside a second.
        self.assertGreaterEqual(THROTTLE_INTERVAL_S, 0.5)
        self.assertLessEqual(THROTTLE_INTERVAL_S, 5.0)


if __name__ == "__main__":
    unittest.main()
