"""Contracts the shared fakes in `_fakes.py` owe the tests that bind them.

A fake's properties are load-bearing for modules that never mention it, so the
ones a caller would silently rely on are pinned here rather than in whichever
consumer happened to need them first.
"""

from __future__ import annotations

import unittest

from _fakes import SleepDrivenClock

START = 1000.0


def _readings(clock: SleepDrivenClock) -> tuple[float, float, float]:
    return clock.time(), clock.monotonic(), clock.perf_counter()


class SleepDrivenClockTest(unittest.TestCase):
    def test_every_clock_name_reads_one_timeline(self):
        clock = SleepDrivenClock()
        self.assertEqual(_readings(clock), (START,) * 3)
        clock.sleep(0.25)
        self.assertEqual(_readings(clock), (START + 0.25,) * 3)

    def test_it_starts_away_from_the_never_happened_sentinel(self):
        self.assertNotEqual(SleepDrivenClock().time(), 0.0)
        self.assertEqual(SleepDrivenClock(start=5.0).time(), 5.0)

    def test_a_negative_sleep_raises_instead_of_rewinding(self):
        clock = SleepDrivenClock()
        with self.assertRaises(ValueError):
            clock.sleep(-1.0)
        self.assertEqual(clock.time(), START)

    def test_a_zero_length_sleep_is_accepted_like_the_real_module(self):
        clock = SleepDrivenClock()
        clock.sleep(0)
        # `-0.0 < 0` is False, and the real `time.sleep(-0.0)` also returns.
        clock.sleep(-0.0)
        self.assertEqual(clock.time(), START)

    def test_an_unpinned_name_cannot_reach_the_host_clock(self):
        clock = SleepDrivenClock()
        for name in ("process_time", "time_ns", "clock_gettime"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(clock, name)


if __name__ == "__main__":
    unittest.main()
