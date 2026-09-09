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
import inspect
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

    Two checks, because a throttle can be process-wide in two ways and each one
    is invisible to the other's method. **Reachable from import scope** is the
    first: walk what importing the package produced, so a `LogThrottle` is found
    wherever it is parked — a module global, a container at any depth, a class
    attribute, a nested class's attribute, or an instance a module built at
    import time. That is a walk of objects rather than of source text, because
    the two regressions were `LogThrottle(log)` at a module's top level and the
    shapes that replace it read nothing like it: a factory call, a dict entry, a
    singleton's own field. A class attribute counts for the same reason a global
    does — it is shared by every instance, which is the same scope one name
    along — and so does that instance's field once the instance itself is a
    global.

    **A factory that answers with the same object twice** is the second, and no
    import-scope walk can see it: a memoized factory (`lru_cache`, or a
    `global` memo) holds nothing until first call and is then per process
    forever. It is also the shape this rule pushes an author toward, since the
    two sites it governs are free functions and "don't put it at module level"
    reads as "wrap it in a function". So every zero-argument callable in the
    package annotated `-> LogThrottle` is called twice and the two results must
    be distinct objects. `inspect.signature` follows `__wrapped__`, so a
    decorated factory is discovered rather than skipped.

    What still evades both: a throttle cached somewhere only a call can reach —
    on a class from inside a method, or in a closure cell. Neither has occurred
    and neither has an obvious cheap check, so this docstring says so rather
    than the test name implying otherwise.

    Every module in the package is imported. All of them import cleanly with the
    package's hard dependencies alone — which is what CI installs — so a failure
    here is a real import failure and not a missing extra.
    """

    # Deep enough for a container of containers or a singleton holding one, and
    # bounded so a cycle or an unexpectedly wide object cannot walk the heap.
    _MAX_DEPTH = 6

    def _modules(self):
        yield c64cast
        for found in pkgutil.walk_packages(c64cast.__path__, "c64cast."):
            if found.name.endswith("__main__"):
                continue  # a three-line entry point that runs the CLI on import
            yield importlib.import_module(found.name)

    def _throttles_reachable(self, root: object, label: str) -> list[str]:
        """Every `LogThrottle` reachable from `root` without calling anything.

        Recursion stops at anything that is not a container, a class, or an
        instance of a class this package defines — so an imported module or a
        stdlib object ends the walk rather than opening the rest of the heap.
        """
        found: list[str] = []
        seen: set[int] = set()

        def walk(obj: object, where: str, depth: int) -> None:
            if depth > self._MAX_DEPTH or id(obj) in seen:
                return
            seen.add(id(obj))
            if isinstance(obj, LogThrottle):
                found.append(where)
                return
            if isinstance(obj, (list, tuple, set, frozenset)):
                for index, item in enumerate(obj):
                    walk(item, f"{where}[{index}]", depth + 1)
                return
            if isinstance(obj, dict):
                for key, item in obj.items():
                    walk(item, f"{where}[{key!r}]", depth + 1)
                return
            owner = obj if isinstance(obj, type) else type(obj)
            if not getattr(owner, "__module__", "").startswith("c64cast"):
                return
            for name, item in getattr(obj, "__dict__", {}).items():
                walk(item, f"{where}.{name}", depth + 1)

        walk(root, label, 0)
        return found

    def test_no_throttle_is_reachable_from_import_scope(self) -> None:
        found: list[str] = []
        for module in self._modules():
            for name, value in vars(module).items():
                found.extend(self._throttles_reachable(value, f"{module.__name__}.{name}"))
        self.assertEqual(
            found,
            [],
            "a throttle importing the package produced is per process, so one "
            "stream's flood suppresses another stream's first report — build it "
            "per stream instead (see c64cast/_wire_log.py)",
        )

    def _throttle_factories(self):
        for module in self._modules():
            for name, value in vars(module).items():
                if isinstance(value, type) or not callable(value):
                    continue
                if getattr(value, "__module__", None) != module.__name__:
                    continue
                try:
                    signature = inspect.signature(value)
                except (TypeError, ValueError):
                    continue
                annotation = signature.return_annotation
                if annotation in ("LogThrottle", LogThrottle):
                    yield f"{module.__name__}.{name}", value, signature

    def test_every_throttle_factory_answers_with_a_new_one(self) -> None:
        factories = list(self._throttle_factories())
        self.assertTrue(
            factories,
            "no `-> LogThrottle` factory was discovered, so this test proves "
            "nothing — the discovery is what broke, not the rule",
        )
        for where, factory, signature in factories:
            with self.subTest(factory=where):
                self.assertFalse(
                    signature.parameters,
                    f"{where} takes arguments, so this test cannot call it — give "
                    f"it a zero-argument form or check its per-stream-ness by hand",
                )
                self.assertIsNot(
                    factory(),
                    factory(),
                    f"{where} answered twice with one object, so every stream "
                    f"that asks shares a report budget",
                )


if __name__ == "__main__":
    unittest.main()
