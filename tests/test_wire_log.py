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

import annotationlib
import collections
import contextlib
import functools
import importlib
import inspect
import logging
import pkgutil
import re
import sys
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


# Returned when an annotation cannot be resolved at all. Distinct from None,
# which means "no annotation": a name imported under `if TYPE_CHECKING` is
# unresolvable and may still be the one that matters, so the two cases have to
# be told apart rather than both read as "not a factory".
_UNRESOLVED = object()

# Matched against an annotation this process cannot resolve, whose only
# remaining form is the source text. Word-bounded so `LogThrottleFactory` does
# not match. No looser than the resolved path, which accepts `LogThrottle`
# anywhere in the type's arguments.
_NAMES_THROTTLE = re.compile(r"\bLogThrottle\b")
_NAMES_LOGGER = re.compile(r"\bLogger\b")

# This package's own logger names, which is how much of the process-wide
# logger registry counts as its import scope. See [NoProcessWideThrottleTest].
_PACKAGE = c64cast.__name__
_PACKAGE_PREFIX = f"{_PACKAGE}."


def _in_package_scope(name: str) -> bool:
    return name == _PACKAGE or name.startswith(_PACKAGE_PREFIX)


def _annotations_of(target: object) -> dict[str, str]:
    """`target`'s annotations as the source text, never evaluated.

    Reading `target.__annotations__` is not safe here, and looked it. Under
    PEP 649 a module *without* `from __future__ import annotations` — 14 of
    them in this package — evaluates its annotations on that access, so an
    unquoted name imported under `if TYPE_CHECKING` raises `NameError` from
    `__annotate__`, which `getattr`'s default does not catch. That is the
    parameter deciding the whole function again, one round louder: an error
    taking the check down at discovery rather than a factory quietly dropped.
    `Format.STRING` answers with what was written and evaluates nothing — and
    so does the `annotation_format` that [ThrottleFactoryTest._arguments_for]
    hands `inspect.signature`, which resolves annotations of its own.
    """
    with contextlib.suppress(Exception):
        return dict(annotationlib.get_annotations(target, format=annotationlib.Format.STRING))
    return {}


def _annotated_target(
    value: Callable[..., object], *, unwrap: bool = True
) -> Callable[..., object]:
    """What carries `value`'s annotations.

    A `functools.partial` carries none of its own, so its `func` does. A
    callable *object* carries its class's rather than its `__call__`'s, which
    read as "no return annotation" and dropped a memoizing `__call__` factory
    out of discovery altogether — the very shape [ThrottleFactoryTest] is for.
    """
    target: Callable[..., object] = value.func if isinstance(value, functools.partial) else value
    # Both of these can raise, and this helper is called outside any guard, so
    # an escape here ends the whole check rather than dropping one factory —
    # the failure mode of the three rounds before this one. `unwrap` raises on
    # a `__wrapped__` cycle, and `getattr_static` is not fully static against a
    # metaclass `__getattr__`. Neither shape is in the tree; the guards are
    # here because "not in the tree today" is what the last three rounds
    # thought too.
    if unwrap:
        with contextlib.suppress(ValueError):
            target = inspect.unwrap(target)
    if inspect.isroutine(target) or isinstance(target, type):
        return target
    dunder_call: object = None
    with contextlib.suppress(Exception):
        dunder_call = inspect.getattr_static(type(target), "__call__", None)
    return dunder_call if inspect.isroutine(dunder_call) else target  # type: ignore[return-value]


def _is_logger(hint: object) -> bool:
    """Whether `hint` asks for a logger, resolved or as written.

    A string arrives when the annotation could not be resolved — see
    [ThrottleFactoryTest._hints_of] — and refusing to read it there would skip
    exactly the factory whose *other* parameter is the unresolvable one.
    """
    if isinstance(hint, str):
        return bool(_NAMES_LOGGER.search(hint))
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
    It deliberately does not ask who defined a holder's class: an earlier
    version descended only into classes this package defines, and a
    `types.SimpleNamespace`, a `threading.local` or a `deque` — all shapes a
    real author writes — hid a throttle from it.

    A module stops it, because a module's globals are the next module's roots
    and following them would be a walk of the whole interpreter.

    `logging.Manager` is not stopped but is **narrowed**, and the difference
    matters because getting it wrong cost coverage. Every throttle holds a
    logger and every logger holds the manager, whose `loggerDict` is the
    *process* registry, so one hop off a c64cast logger reached every other
    library's loggers, handlers, formatters and filters — which is where the
    depth budget was going, at depth 8 of a bound of 12 through `urllib3`'s
    retry logger. Stopping at the manager outright fixed that and lost
    something: `loggerDict` holds *this package's own* loggers too, so a
    throttle parked on a `c64cast.*` logger that no module global holds — the
    walk found one at
    `c64cast.app.cli.log.manager.loggerDict['c64cast.probe.wire'].throttle`
    before the stop — became invisible. So the manager's children are its
    `c64cast`-named loggers and nothing else: this package's import scope, not
    the interpreter's. A logger outside that scope is narrowed the same way
    and for the same reason, which is what keeps the *root* logger's handlers
    out: `<module>.log.parent.handlers[0]` reaches whatever handler anything
    else in the process installed, and through a Rich one it runs to depth 12
    and fails the truncation assertion on entirely correct code.
    It keeps the whole cost win, since those loggers are
    already reachable at depth ≤ 2, and a dependency that nests one more
    object inside a handler no longer fails a test about this package's
    throttles.

    The depth bound is a backstop, not a policy, and the truncation rule is
    what keeps it from becoming one. A truncation is recorded only for an
    object the walk would otherwise have descended into — something with
    children it has not already explored by a shallower path — so a long chain
    of leaves and a re-reached object are not reported as places this check
    could not see. Both were: a 12-hop chain ending in a `str`, and an object
    fully explored at depth 2 and re-reached at depth 12, each reddened the
    suite with a message telling the author to raise `_MAX_DEPTH`, which only
    moves the wall.

    With both of those in place nothing the walk explores runs deeper than 5
    hops from a root, against a bound of 12 — so a truncation means the graph
    grew, not that the bound was always marginal. Truncations are collected
    and asserted empty rather than passing quietly.

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

    def _children_of(self, obj: object) -> list[tuple[str, object]]:
        """What `obj` holds, as `(path suffix, child)` — empty for anything the
        walk does not descend into.

        Emptiness is what the truncation rule reads, so leaves and stops
        answer the same way as an object that genuinely holds nothing. A
        scalar needs no case of its own: a `str` has no `__dict__` and no
        `__slots__`, so `_attributes_of` already answers `{}` for it.

        The two `logging` cases narrow rather than stop, by the same name
        rule — the class docstring says why, and why neither is a stop.
        """
        if isinstance(obj, types.ModuleType):
            return []
        if isinstance(obj, logging.Logger) and not _in_package_scope(obj.name):
            return []
        if isinstance(obj, logging.Manager):
            return [
                (f".loggerDict[{name!r}]", child)
                for name, child in obj.loggerDict.items()
                if _in_package_scope(name)
            ]
        if isinstance(obj, self._CONTAINERS):
            return [(f"[{index}]", item) for index, item in enumerate(obj)]
        if isinstance(obj, dict):
            return [(f"[{key!r}]", item) for key, item in obj.items()]
        return [(f".{name}", item) for name, item in self._attributes_of(obj).items()]

    def _reachable(self, roots: list[tuple[str, object]]) -> tuple[list[str], list[str]]:
        """`(throttles, truncated)` reachable from `roots` without calling
        anything.

        `seen` records the *shallowest* depth each object was reached at rather
        than merely that it was reached: keyed by id alone, an object first
        found deep enough to be truncated short-circuits the shallower path
        that would have explored it in full. It is consulted *before* the depth
        bound for the same reason — an object already explored in full is not a
        place this check could not see, whichever path re-reaches it.

        One memo across every root, not one per root. Both halves of that
        matter. Correctness: a truncation is only real if *no* path explored
        the object in full, and per-root memos made that answer depend on which
        module happened to be walked first. Cost: the roots share most of their
        graph, and re-walking it from each of the 6,769 of them was 505,195
        visits in 0.533 s against 39,368 in 0.044 s with one memo.

        A truncation still cannot be judged the moment it is hit — the
        shallower path may come later in the same walk — so each one is
        recorded with the id it stopped at and filtered at the end against
        what the memo finally knows.
        """
        throttles: list[str] = []
        stopped: list[tuple[int, str]] = []
        seen: dict[int, int] = {}

        def walk(obj: object, where: str, depth: int) -> None:
            if isinstance(obj, LogThrottle):
                throttles.append(where)
                return
            if seen.get(id(obj), self._MAX_DEPTH + 1) <= depth:
                return
            children = self._children_of(obj)
            if not children:
                return
            if depth >= self._MAX_DEPTH:
                stopped.append((id(obj), where))
                return
            seen[id(obj)] = depth
            for suffix, item in children:
                walk(item, f"{where}{suffix}", depth + 1)

        for label, root in roots:
            walk(root, label, 0)
        unexplored = self._MAX_DEPTH + 1
        truncated = [
            where for oid, where in stopped if seen.get(oid, unexplored) >= self._MAX_DEPTH
        ]
        return throttles, truncated

    def test_no_throttle_is_reachable_from_import_scope(self) -> None:
        # Materialized before the walk: `_package_modules` imports as it
        # yields, and an import can add a name to a module already being
        # iterated.
        roots = [
            (f"{module.__name__}.{name}", value)
            for module in _package_modules()
            for name, value in list(vars(module).items())
        ]
        throttles, truncated = self._reachable(roots)
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
    text that no literal comparison catches. The return annotation is resolved
    **on its own**, and an unresolvable one is read as source text rather than
    as a no: resolving a whole signature at once means one parameter typed with
    a name imported under `if TYPE_CHECKING` — the idiom in 64 modules here —
    raises `NameError` and silently deletes the factory from discovery.

    Discovery follows `__wrapped__` and `functools.partial`, and covers the
    classmethods and staticmethods of module-scope classes as well as
    module-level functions, so a decorated or partially-applied factory is
    checked rather than skipped. What is followed for the *hints* is not
    followed for the *call*: `inspect.signature` reads through `__wrapped__`,
    so a decorator that supplies the logger presents no parameters while its
    inner function is annotated as taking one. Both the wrapper's shape and the
    unwrapped one are tried, in that order, and only a factory that neither
    shape can call is skipped.

    **A factory that takes arguments is not a failure.** `LogThrottle(logger)`
    is the constructor, so `def new_x_log(logger: logging.Logger)` is the most
    natural per-stream factory there is, and reddening the suite on code that
    fully complies with the rule is how a guard gets worked around or deleted.
    A logger parameter is supplied; a required parameter this check cannot
    synthesize skips that factory by name, which reads as a skip in the run
    rather than as silence. So does a factory no argument shape can call, and
    a `-> LogThrottle | None` one that answers `None` — nothing was built, so
    there is no shared budget to report, and asserting on the two `None`s
    instead read as the failure this class exists to catch.

    What stays out of reach of both checks: a throttle only a call can produce
    where the call cannot be made from here — an instance method, which needs
    an instance this has no way to build; a factory whose arguments were
    skipped above; or a closure cell. One more, and it is a limit of the text
    fallback rather than of the walk: a return annotation that is an *alias*
    for `LogThrottle` imported under `if TYPE_CHECKING` (`-> WireThrottle`)
    resolves to nothing and matches no name, because a regex over source text
    cannot follow a rename. Named here rather than left for the test names to
    imply. The space of further shapes is unbounded, so this is where the
    mechanism stops and a reviewer's judgment takes over.
    """

    def _returns(self, value: Callable[..., object]) -> object:
        """The return annotation alone, resolved against its own module.

        A proxy carrying only that one annotation is what keeps a parameter
        from deciding the question: `typing.get_type_hints` resolves every
        annotation a function has, and raises for the whole function if any one
        of them names something that exists only under `if TYPE_CHECKING`.
        `globalns` is passed explicitly because the proxy is defined *here*, so
        the fallback would resolve the package's annotations against this test
        module's imports.
        """
        target = _annotated_target(value)
        written = _annotations_of(target).get("return")
        if written is None:
            return None

        def proxy() -> None: ...

        proxy.__annotations__ = {"return": written}
        module = sys.modules.get(getattr(target, "__module__", "") or "")
        try:
            return typing.get_type_hints(proxy, vars(module) if module else {})["return"]
        except Exception:  # noqa: BLE001 - fall back to the text, below
            return _UNRESOLVED if _NAMES_THROTTLE.search(written) else None

    def _is_factory(self, value: Callable[..., object]) -> bool:
        returns = self._returns(value)
        if returns is _UNRESOLVED:
            return True
        return returns is LogThrottle or LogThrottle in typing.get_args(returns)

    def _hints_of(self, func: Callable[..., object]) -> dict[str, object]:
        """Parameter hints, resolved where they can be and as written where
        they cannot — see [_is_logger] for the second half."""
        with contextlib.suppress(Exception):
            return dict(typing.get_type_hints(func))
        return dict(_annotations_of(func))

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
        """Each discovered factory once, keyed by the object that gets called.

        Keyed by the wrapper and not by `inspect.unwrap` of it, because a
        memoizing wrapper is a *different* factory from the function it wraps —
        `lru_cache(maxsize=None)(new_plain_log)` is per process forever, and
        collapsing the two skipped it as a duplicate of the compliant function
        underneath. The map holds the candidate rather than just its id: a
        classmethod is a fresh bound method on every `getattr`, and an id whose
        object has been freed is an id the next one can be handed.
        """
        seen: dict[int, Callable[..., object]] = {}
        for module in _package_modules():
            for where, candidate in self._candidates(module):
                if isinstance(candidate, type) or not callable(candidate):
                    continue
                if not self._is_factory(candidate):
                    continue
                if id(candidate) in seen:
                    continue
                seen[id(candidate)] = candidate
                yield where, candidate

    def _arguments_for(self, factory, *, follow_wrapped: bool):
        """`(positional, keywords)` to call `factory` with, or None when it
        asks for something this check cannot synthesize.

        A parameter with a default is left to it. Python does not allow a
        required positional to follow a defaulted one, so skipping the
        defaulted ones cannot misalign what is passed positionally.

        A `functools.partial` is read for its remaining parameters — which
        `inspect.signature` computes — but its hints come from `.func`, since a
        partial carries no annotations of its own and reading them off it
        skipped every partially-applied factory.
        """
        try:
            # `annotation_format` is not decoration: `inspect.signature`
            # resolves annotations too, and its default asks for values — so
            # under PEP 649 reading the signature of a factory in one of the
            # 14 modules without `from __future__ import annotations` raised
            # `NameError` for an unquoted `if TYPE_CHECKING` parameter, which
            # is the same defect as [_annotations_of]'s at a third site. This
            # only needs names, kinds and defaults.
            signature = inspect.signature(
                factory,
                follow_wrapped=follow_wrapped,
                # pyright is pinned to 3.11 stubs (pyproject), which predate
                # this 3.14 parameter; the runtime requires 3.14.
                annotation_format=annotationlib.Format.STRING,  # pyright: ignore[reportCallIssue]
            )
        except Exception:  # noqa: BLE001 - a signature we cannot read we do not call
            # Wider than the two documented raises on purpose: a callable whose
            # `__annotate__` refuses a non-VALUE format raises from inside
            # `signature` past a narrow guard, and this must skip a factory
            # rather than end the check.
            return None
        hints = self._hints_of(_annotated_target(factory, unwrap=follow_wrapped))
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

    def _call_shapes(self, factory):
        """The argument shapes to try, in order: as the factory is called, then
        as the function underneath it is annotated.

        One signature cannot answer both. `inspect.signature` follows
        `__wrapped__`, so a decorator that supplies the logger reads as taking
        one and is then called with one it does not accept; refusing to follow
        it reads a pass-through `(*args, **kwargs)` wrapper as taking none, and
        the inner function is the one that raises. Both are ordinary shapes, so
        both are tried.
        """
        shapes = []
        for follow_wrapped in (False, True):
            shape = self._arguments_for(factory, follow_wrapped=follow_wrapped)
            if shape is not None and shape not in shapes:
                shapes.append(shape)
        return shapes

    def test_every_throttle_factory_answers_with_a_new_one(self) -> None:
        factories = list(self._factories())
        self.assertTrue(
            factories,
            "no `-> LogThrottle` factory was discovered, so this test proves "
            "nothing — the discovery is what broke, not the rule",
        )
        exercised = 0
        for where, factory in factories:
            with self.subTest(factory=where):
                shapes = self._call_shapes(factory)
                if not shapes:
                    self.skipTest(f"{where} asks for arguments this check cannot synthesize")
                built = None
                refused: TypeError | None = None
                for positional, keywords in shapes:
                    # A factory that logs on construction would put records
                    # between the suite's dots, and a wire-log factory has no
                    # business logging anyway — so the purity is asserted, not
                    # suppressed.
                    try:
                        with self.assertNoLogs("c64cast", level="DEBUG"):
                            built = (
                                factory(*positional, **keywords),
                                factory(*positional, **keywords),
                            )
                    except TypeError as exc:  # noqa: PERF203 - one per shape tried
                        refused = exc
                    else:
                        break
                if built is None:
                    # Said as what happened, not as "cannot synthesize": a
                    # shape was built and the factory refused it, and reporting
                    # the two alike sent the reader after the argument
                    # synthesis for a TypeError raised in a factory's own body.
                    self.skipTest(f"{where} refused every shape this check built: {refused!r}")
                first, second = built
                if first is None and second is None:
                    self.skipTest(f"{where} answered None, so nothing was built to share")
                exercised += 1
                self.assertIsNot(
                    first,
                    second,
                    f"{where} answered twice with one object, so every stream "
                    f"that asks shares a report budget",
                )
        # The floor under every skip above. Each one is visible as an `s` and
        # each is a fact about this check rather than about the code — but a
        # change that makes *every* factory refuse the call, a `LogThrottle`
        # constructor signature among them, would otherwise turn the whole
        # guard from red into a quiet pass.
        self.assertTrue(
            exercised,
            f"all {len(factories)} discovered factories were skipped, so this test "
            "proves nothing — read the skip reasons rather than the green",
        )


if __name__ == "__main__":
    unittest.main()
