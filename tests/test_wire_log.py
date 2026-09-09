"""The shared wire-triggered log throttle (c64cast/_wire_log.py).

The throttle exists because a byte on the ASID wire can otherwise buy an
unbounded amount of work inside a bounded reader: one WARNING through the
default terminal handler costs ~322 us, and a per-message warning lets the
sender pick the reader's throughput. So these tests pin the two properties that
bound the cost — at most one record per interval, whatever the occurrence rate,
and the interval read off the *injected* clock rather than the wall clock — plus
the level rule that keeps a lone recurrence visible.

`assertLogs` is used throughout, so nothing here may nest `quiet_logging()`.

The last two classes are a different kind of check: the rest of this module pins
what one throttle does, and [NoProcessWideThrottleTest] plus
[ThrottleFactoryTest] pin where a throttle may live and who may hand one out.
"""

from __future__ import annotations

import collections
import contextlib
import functools
import importlib
import inspect
import logging
import pkgutil
import types
import typing
import unittest
from collections.abc import Callable

import c64cast
from c64cast import _wire_log
from c64cast._wire_log import THROTTLE_INTERVAL_S, LogThrottle

# Handed to a factory that asks for one. Named so a record it emits — which
# `test_every_throttle_factory_answers_with_a_new_one` asserts cannot happen —
# would say where it came from.
_FACTORY_LOGGER = logging.getLogger("c64cast.tests.wire_log.factory")


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


def _package_modules():
    """Every module in the package, imported.

    All of them import cleanly with the package's hard dependencies alone, so a
    failure here is a real import failure rather than a missing extra — though
    no CI leg installs only the hard dependencies (`uv sync --frozen
    --no-default-groups --group dev --extra web`), so that is a property of the
    tree and not something the pipeline proves.
    """
    yield c64cast
    for found in pkgutil.walk_packages(c64cast.__path__, "c64cast."):
        if found.name.endswith("__main__"):
            continue  # a three-line entry point that runs the CLI on import
        yield importlib.import_module(found.name)


def _is_logger(hint: object) -> bool:
    return hint is logging.Logger or logging.Logger in typing.get_args(hint)


class NoProcessWideThrottleTest(unittest.TestCase):
    """No throttle is reachable from import scope.

    "O(1) per stream, not per message" was stated in three places and enforced
    in none. The two regressions it names were both a `LogThrottle` at a
    module's top level — per *process*, and indistinguishable from a per-stream
    one until a second stream exists — and both survived review. The tests that
    caught them are tests of the two factories, so they close those two
    instances and not the class.

    So this walks what importing the package produced: every module global,
    then containers (`deque` included) at any depth, dicts, classes, nested
    classes, and the `__dict__` *and* `__slots__` of any object a module holds.
    The walk stops only at a module, because a module's globals are the next
    module's roots and following them would be a walk of the whole interpreter.
    It deliberately does not ask who defined a holder's class: an earlier
    version descended only into classes this package defines, and a
    `types.SimpleNamespace`, a `threading.local` or a `deque` — all shapes a
    real author writes — hid a throttle from it.

    The depth bound is a backstop, not a policy. Nothing in the tree reaches
    even depth 10 today, so a truncation means the graph grew past what this
    check can see; truncations are collected and asserted empty rather than
    passing quietly.

    [ThrottleFactoryTest] is the other half, and it is a separate check rather
    than a wider walk because no walk of import scope can reach what it looks
    for. Between them, what stays out of reach is named there.
    """

    _MAX_DEPTH = 12
    _CONTAINERS = (list, tuple, set, frozenset, collections.deque)

    def _attributes_of(self, obj: object) -> dict[str, object]:
        """`obj`'s own stored attributes — `__dict__` plus every `__slots__`
        member along its MRO.

        Both halves are needed and the second was missing: `__dict__` is empty
        for a `__slots__` instance, so a slotted singleton read as holding
        nothing, and `__slots__` is already the idiom at five sites in this
        package. Only slot names are read through `getattr`, never arbitrary
        attribute names — a slot is plain storage, where a property would run
        code this check has no business running.
        """
        try:
            stored = dict(getattr(obj, "__dict__", None) or {})
        except Exception:  # noqa: BLE001 - a holder we cannot read shows us nothing
            stored = {}
        owner = obj if isinstance(obj, type) else type(obj)
        for klass in getattr(owner, "__mro__", ()):
            slots = getattr(klass, "__slots__", ()) or ()
            for slot in (slots,) if isinstance(slots, str) else slots:
                if not isinstance(slot, str):
                    continue
                with contextlib.suppress(AttributeError):  # declared, never assigned
                    stored.setdefault(slot, getattr(obj, slot))
        return stored

    def _reachable(self, root: object, label: str) -> tuple[list[str], list[str]]:
        """`(throttles, truncated)` reachable from `root` without calling
        anything.

        `seen` records the *shallowest* depth each object was reached at rather
        than merely that it was reached: keyed by id alone, an object first
        found deep enough to be truncated short-circuits the shallower path
        that would have explored it in full.
        """
        throttles: list[str] = []
        truncated: list[str] = []
        seen: dict[int, int] = {}

        def walk(obj: object, where: str, depth: int) -> None:
            if isinstance(obj, LogThrottle):
                throttles.append(where)
                return
            if depth >= self._MAX_DEPTH:
                truncated.append(where)
                return
            if seen.get(id(obj), self._MAX_DEPTH + 1) <= depth:
                return
            seen[id(obj)] = depth
            if isinstance(obj, self._CONTAINERS):
                for index, item in enumerate(obj):
                    walk(item, f"{where}[{index}]", depth + 1)
                return
            if isinstance(obj, dict):
                for key, item in obj.items():
                    walk(item, f"{where}[{key!r}]", depth + 1)
                return
            if isinstance(obj, types.ModuleType):
                return
            for name, item in self._attributes_of(obj).items():
                walk(item, f"{where}.{name}", depth + 1)

        walk(root, label, 0)
        return throttles, truncated

    def test_no_throttle_is_reachable_from_import_scope(self) -> None:
        throttles: list[str] = []
        truncated: list[str] = []
        for module in _package_modules():
            for name, value in vars(module).items():
                found, cut = self._reachable(value, f"{module.__name__}.{name}")
                throttles.extend(found)
                truncated.extend(cut)
        self.assertEqual(
            throttles,
            [],
            "a throttle importing the package produced is per process, so one "
            "stream's flood suppresses another stream's first report — build it "
            "per stream instead (see c64cast/_wire_log.py)",
        )
        self.assertEqual(
            truncated[:5],
            [],
            f"the object graph now runs deeper than {self._MAX_DEPTH}, so this "
            f"check stopped short of {len(truncated)} place(s) and cannot say no "
            f"throttle is parked there — raise _MAX_DEPTH",
        )


class ThrottleFactoryTest(unittest.TestCase):
    """Every discoverable `-> LogThrottle` factory answers with a new object.

    No walk of import scope can see this one: a memoized factory holds nothing
    until its first call and is then per process forever. It is also the shape
    the rule pushes an author toward, since both governed sites are free
    functions and "don't put it at module level" reads as "wrap it in a
    function".

    Return types are resolved with `typing.get_type_hints` rather than matched
    as written, because the package uses `from __future__ import annotations`:
    `-> _wire_log.LogThrottle` and `-> LogThrottle | None` arrive as source
    text that no literal comparison catches. Discovery follows `__wrapped__`
    and `functools.partial`, and covers the classmethods and staticmethods of
    module-scope classes as well as module-level functions, so a decorated or
    partially-applied factory is checked rather than skipped.

    **A factory that takes arguments is not a failure.** `LogThrottle(logger)`
    is the constructor, so `def new_x_log(logger: logging.Logger)` is the most
    natural per-stream factory there is, and reddening the suite on code that
    fully complies with the rule is how a guard gets worked around or deleted.
    A logger parameter is supplied; a required parameter this check cannot
    synthesize skips that factory by name, which reads as a skip in the run
    rather than as silence.

    What stays out of reach of both checks: a throttle only a call can produce
    where the call cannot be made from here — an instance method, which needs
    an instance this has no way to build; a factory whose arguments were
    skipped above; or a closure cell. Named here rather than left for the test
    names to imply. The space of further shapes is unbounded, so this is where
    the mechanism stops and a reviewer's judgment takes over.
    """

    def _returns(self, value: Callable[..., object]) -> object:
        target: Callable[..., object] = (
            value.func if isinstance(value, functools.partial) else value
        )
        try:
            return typing.get_type_hints(inspect.unwrap(target)).get("return")
        except Exception:  # noqa: BLE001 - an unresolvable hint names no factory here
            return None

    def _is_factory(self, value: Callable[..., object]) -> bool:
        returns = self._returns(value)
        return returns is LogThrottle or LogThrottle in typing.get_args(returns)

    def _candidates(self, module):
        """Module-level callables, plus the classmethods and staticmethods of
        the classes this module defines — the two places a factory can sit and
        still be callable without building an instance."""
        for name, value in list(vars(module).items()):
            where = f"{module.__name__}.{name}"
            if not isinstance(value, type):
                yield where, value
                continue
            if getattr(value, "__module__", "") != module.__name__:
                continue
            for attr, raw in list(vars(value).items()):
                if isinstance(raw, (classmethod, staticmethod)):
                    yield f"{where}.{attr}", getattr(value, attr)

    def _factories(self):
        seen: set[int] = set()
        for module in _package_modules():
            for where, candidate in self._candidates(module):
                if isinstance(candidate, type) or not callable(candidate):
                    continue
                if not self._is_factory(candidate):
                    continue
                key = id(inspect.unwrap(candidate))
                if key in seen:
                    continue
                seen.add(key)
                yield where, candidate

    def _arguments_for(self, factory):
        """`(positional, keywords)` to call `factory` with, or None when it
        asks for something this check cannot synthesize.

        A parameter with a default is left to it. Python does not allow a
        required positional to follow a defaulted one, so skipping the
        defaulted ones cannot misalign what is passed positionally.
        """
        try:
            signature = inspect.signature(factory)
            hints = typing.get_type_hints(inspect.unwrap(factory))
        except Exception:  # noqa: BLE001 - a callable we cannot introspect we do not call
            return None
        positional: list[object] = []
        keywords: dict[str, object] = {}
        for parameter in signature.parameters.values():
            if parameter.default is not inspect.Parameter.empty:
                continue
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                continue
            if not _is_logger(hints.get(parameter.name)):
                return None
            if parameter.kind is parameter.KEYWORD_ONLY:
                keywords[parameter.name] = _FACTORY_LOGGER
            else:
                positional.append(_FACTORY_LOGGER)
        return positional, keywords

    def test_every_throttle_factory_answers_with_a_new_one(self) -> None:
        factories = list(self._factories())
        self.assertTrue(
            factories,
            "no `-> LogThrottle` factory was discovered, so this test proves "
            "nothing — the discovery is what broke, not the rule",
        )
        for where, factory in factories:
            with self.subTest(factory=where):
                arguments = self._arguments_for(factory)
                if arguments is None:
                    self.skipTest(f"{where} asks for arguments this check cannot synthesize")
                positional, keywords = arguments
                # A factory that logs on construction would put records between
                # the suite's dots, and a wire-log factory has no business
                # logging anyway — so the purity is asserted, not suppressed.
                with self.assertNoLogs("c64cast", level="DEBUG"):
                    first = factory(*positional, **keywords)
                    second = factory(*positional, **keywords)
                self.assertIsNot(
                    first,
                    second,
                    f"{where} answered twice with one object, so every stream "
                    f"that asks shares a report budget",
                )


if __name__ == "__main__":
    unittest.main()
