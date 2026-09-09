"""The shared wire-triggered log throttle (c64cast/_wire_log.py).

The throttle exists because a byte on the ASID wire can otherwise buy an
unbounded amount of work inside a bounded reader: one WARNING through the
default terminal handler costs ~322 us, and a per-message warning lets the
sender pick the reader's throughput. So these tests pin the two properties that
bound the cost — at most one record per interval, whatever the occurrence rate,
and the interval read off the *injected* clock rather than the wall clock — plus
the level rule that keeps a lone recurrence visible.

`assertLogs` is used throughout, so nothing here may nest `quiet_logging()`.

[NoProcessWideThrottleTest] is the other half: the rest of this module pins what
one throttle does, and that one pins where throttles may live.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import unittest

import c64cast
from c64cast import _wire_log
from c64cast._wire_log import THROTTLE_INTERVAL_S, LogThrottle


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


class LogThrottleExceptionTest(unittest.TestCase):
    """`exception()` is the same gate one level up, for a site inside an
    `except` block — `midi_control`'s readers, whose per-message record is a
    rendered traceback that no level check would have rejected."""

    def setUp(self) -> None:
        self.log = logging.getLogger("c64cast.tests.wire_log")

    def _throttle(self, step: float = 0.0) -> LogThrottle:
        return LogThrottle(self.log, monotonic=_StepClock(step))

    @staticmethod
    def _report(throttle: LogThrottle, msg: str, *args: object) -> None:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            throttle.exception(msg, *args)

    def test_the_first_occurrence_is_an_error_carrying_the_traceback(self):
        throttle = self._throttle()
        with self.assertLogs(self.log, "DEBUG") as caught:
            self._report(throttle, "dispatch failed for %r", "note_on")
        self.assertEqual(len(caught.records), 1)
        self.assertEqual(caught.records[0].levelname, "ERROR")
        self.assertEqual(caught.records[0].getMessage(), "dispatch failed for 'note_on'")
        self.assertIsNotNone(caught.records[0].exc_info)

    def test_a_flood_inside_the_window_costs_exactly_one_record(self):
        throttle = self._throttle()
        with self.assertLogs(self.log, "DEBUG") as caught:
            for _ in range(500):
                self._report(throttle, "dispatch failed")
        self.assertEqual(len(caught.records), 1)

    def test_a_repeat_report_keeps_the_traceback_and_carries_the_count(self):
        # The traceback rides on the repeat too: it is the whole diagnostic
        # value here, and the *interval*, not the level, is what bounds cost.
        throttle = self._throttle(step=THROTTLE_INTERVAL_S / 4)
        with self.assertLogs(self.log, "DEBUG") as caught:
            for _ in range(5):  # clock crosses the window every 4th read
                self._report(throttle, "dispatch failed")
        self.assertEqual([r.levelname for r in caught.records], ["ERROR", "DEBUG"])
        self.assertIn("and 3 more since the previous report", caught.records[1].getMessage())
        self.assertIsNotNone(caught.records[1].exc_info)

    def test_warn_and_exception_share_one_site_budget(self):
        # One site, one budget: the two emitters are the same gate, so a site
        # cannot double its report rate by alternating them.
        throttle = self._throttle()
        with self.assertLogs(self.log, "DEBUG") as caught:
            throttle.warn("first")
            self._report(throttle, "second")
        self.assertEqual([r.getMessage() for r in caught.records], ["first"])


class DocstringQuoteTest(unittest.TestCase):
    def test_the_module_docstring_quotes_the_text_the_code_actually_emits(self):
        # It once quoted "and 900 more in the last second", which the code has
        # never emitted and which is wrong in kind as well as wording: the gap
        # between two reports has no upper bound, so a count can span minutes.
        # Prose that quotes an emitted string drifts silently; this is the only
        # thing that notices.
        doc = _wire_log.__doc__ or ""
        self.assertIn(_wire_log._MORE_SUFFIX.strip() % 900, doc)


class ThrottleIntervalTest(unittest.TestCase):
    def test_the_interval_is_short_enough_to_read_and_long_enough_to_bound(self):
        # A sanity band, not a restatement: at 960 Hz this is a 960x reduction,
        # and a human tailing a log still sees the condition inside a second.
        self.assertGreaterEqual(THROTTLE_INTERVAL_S, 0.5)
        self.assertLessEqual(THROTTLE_INTERVAL_S, 5.0)


class NoProcessWideThrottleTest(unittest.TestCase):
    """No module reaches import time holding a throttle.

    "O(1) per stream, not per message" is stated in three places and was, until
    this test, enforced in none: the two regressions it names were both a
    `LogThrottle` at a module's top level, which is per *process*, and reads
    identically to a per-stream one until a second stream exists. Both survived
    review, and the tests that caught them are tests of the two factories — so
    they close those two instances and not the class. A seventh site can put the
    same instance back under a new name with the suite green.

    Import scope is the property, so the check is on imported objects rather
    than on source text: a `LogThrottle` built by a factory, or held inside a
    module-level list or dict, is the same process-wide instance whatever the
    call looks like. Every module in the package is imported (all of them
    import cleanly with the package's hard dependencies installed, so a failure
    here is a real import failure and not a missing extra), and its globals,
    one level into module-level containers, and its classes' attributes are all
    checked — a class attribute is shared by every instance, which is the same
    scope one name along.
    """

    def _modules(self):
        yield c64cast
        for found in pkgutil.walk_packages(c64cast.__path__, "c64cast."):
            if found.name.endswith("__main__"):
                continue  # a three-line entry point that runs the CLI on import
            yield importlib.import_module(found.name)

    def _throttles_in(self, holder: object, label: str):
        for name, value in vars(holder).items():
            where = f"{label}.{name}"
            if isinstance(value, LogThrottle):
                yield where
            elif isinstance(value, (list, tuple, set, frozenset)):
                yield from (where for item in value if isinstance(item, LogThrottle))
            elif isinstance(value, dict):
                yield from (where for item in value.values() if isinstance(item, LogThrottle))

    def test_no_throttle_lives_at_module_or_class_scope(self) -> None:
        found: list[str] = []
        for module in self._modules():
            found.extend(self._throttles_in(module, module.__name__))
            for name, value in vars(module).items():
                if isinstance(value, type) and value.__module__ == module.__name__:
                    found.extend(self._throttles_in(value, f"{module.__name__}.{name}"))
        self.assertEqual(
            found,
            [],
            "a throttle at import scope is per process, so one stream's flood "
            "suppresses another stream's first report — build it per stream "
            "instead (see c64cast/_wire_log.py)",
        )


if __name__ == "__main__":
    unittest.main()
