"""Contracts the shared fakes in `_fakes.py` owe the tests that bind them.

A fake's properties are load-bearing for modules that never mention it, so the
ones a caller would silently rely on are pinned here rather than in whichever
consumer happened to need them first.
"""

from __future__ import annotations

import logging
import unittest

from _fakes import RestoresLogging, SleepDrivenClock, quiet_logging

from c64cast import _transport_log
from c64cast.app import cli_commands

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


# Spelled out rather than read from `cli_commands.HELD_BACK_LOGGERS`, the table
# `quiet_logging()` restores from, so a restore that skipped a name fails here.
_HELD_BACK = (
    "urllib3",
    "urllib3.connectionpool",
    "uvicorn",
    "uvicorn.asgi",
    "uvicorn.error",
    "uvicorn.access",
)


def _state(name: str) -> tuple[int, list[object]]:
    logger = logging.getLogger(name)
    return logger.level, list(logger.filters)


class QuietLoggingTest(RestoresLogging):
    """`cli.main()` under `quiet_logging()` runs `configure_logging`, and what
    that writes must not outlive the block."""

    def test_held_back_levels_are_restored(self):
        for name in _HELD_BACK:
            logging.getLogger(name).setLevel(logging.ERROR)
        with quiet_logging():
            cli_commands.configure_logging(2)
        self.assertEqual(
            {n: logging.getLogger(n).level for n in _HELD_BACK},
            dict.fromkeys(_HELD_BACK, logging.ERROR),
        )

    def test_a_transport_filter_it_attaches_is_taken_back_off(self):
        _transport_log.install(False)
        before = {n: _state(n) for n in _HELD_BACK}
        with quiet_logging():
            cli_commands.configure_logging(2)
        self.assertEqual({n: _state(n) for n in _HELD_BACK}, before)

    def test_a_transport_filter_it_strips_is_put_back(self):
        _transport_log.install(True)
        before = {n: _state(n) for n in _HELD_BACK}
        with quiet_logging():
            cli_commands.configure_logging(3)
        self.assertEqual({n: _state(n) for n in _HELD_BACK}, before)


if __name__ == "__main__":
    unittest.main()
