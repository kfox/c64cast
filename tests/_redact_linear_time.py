"""The linear-time check the redactor's tests share, and the shapes more
than one of them builds.

The checks live in `test_redact_linear_*.py`, apart from `test_redact.py`:
unittest_parallel hands a worker a whole module, and they take most of a
minute of CPU between them."""

from __future__ import annotations

import time
import unittest
from collections.abc import Callable

from c64cast._redact import redact_secrets, redact_source_line


def _hidden_value_ladder(levels: int, filler: int) -> str:
    """`levels` hidden values, each one separator level shallower than the
    last, then `filler` characters, then the encoded `&`s that end them
    deepest first, so each value runs on past the one before it."""
    return (
        "".join(
            f"token%{'25' * (levels + 1)}3Dpassword%{'25' * e}3Dx%{'25' * (levels + 1)}26"
            for e in range(levels - 1, -1, -1)
        )
        + "y" * filler
        + "".join(f"%{'25' * d}26" for d in range(levels, -1, -1))
    )


def _nested_escape(scale: int) -> str:
    """`%253` repeated in front of `%34`: each decoding assembles the next escape."""
    line = "%34"
    while len(line) < 32_000 * scale:
        line = "%253" + line
    return line


#: How many times longer the long input of a linear-time check is than its
#: short one. At 4, a linear pass on a loaded runner and a quadratic one
#: whose quadratic part was the linear part's size read the same ratio (9.1x
#: and 9.5x), so no limit told them apart.
_SCALE = 8

#: Measurements a linear-time check takes of each input before judging.
_TRIES = 3

#: Further measurements of each input a linear-time check takes, one round
#: at a time, before it fails a ratio over the limit.
_RETRIES = 4

#: How many times the length ratio the time ratio may reach. A linear pass
#: reads about 1x and a quadratic one about `_SCALE`x; at 2x a loaded
#: Windows runner failed a linear pass, whose long input read 2.3x slow in
#: every one of its measurements.
_ALLOWED_OVER_LINEAR = 3


#: CPU seconds a measurement runs `work` for before dividing by the runs. A
#: single call is not timed alone: Windows advances a thread's CPU clock once
#: per 15.6 ms tick, so a call of a few milliseconds reads as zero there.
_MEASURE_S = 0.1


def _cpu_seconds(work: Callable[[str], object], line: str) -> float:
    runs = 0
    started = time.thread_time()
    while True:
        work(line)
        runs += 1
        spent = time.thread_time() - started
        if spent >= _MEASURE_S:
            return spent / runs


def _redacts_both_ways(line: str) -> None:
    redact_secrets(line)
    redact_source_line([line], 1)


def _assert_linear_time(
    test: unittest.TestCase,
    make: Callable[[int], str],
    work: Callable[[str], object] = _redacts_both_ways,
) -> None:
    """Fail unless `work` takes time linear in the length of `make(scale)`.

    Compares `make(1)` against `make(_SCALE)`, each `_SCALE` times longer.
    A linear pass spends about `_SCALE` times as long on the long input and a
    quadratic one about `_SCALE` squared, so the check allows
    `_ALLOWED_OVER_LINEAR` times the length ratio. A wall-clock limit on one
    input fails whenever the machine is loaded; a ratio between two inputs
    measured on the same machine does not. The clock is this thread's CPU
    time, which stops while the scheduler runs something else, but not while
    a busy core runs it slower. Each input keeps its fastest measurement.
    The first `_TRIES` are all taken before the ratio is judged: deciding
    after each try would let one inflated measurement of the short input pass
    a quadratic regression. A ratio over the limit is re-measured up to
    `_RETRIES` more rounds before it fails, since a fastest time only moves
    toward the true cost: a slow spell on the long input passes, and a
    quadratic pass stays over the limit however often it is measured.
    """
    short, long = make(1), make(_SCALE)
    allowed = _ALLOWED_OVER_LINEAR * len(long) / len(short)
    fastest_short = fastest_long = float("inf")
    for tried in range(_TRIES + _RETRIES):
        fastest_short = min(fastest_short, _cpu_seconds(work, short))
        fastest_long = min(fastest_long, _cpu_seconds(work, long))
        if tried + 1 >= _TRIES and fastest_long <= allowed * fastest_short:
            return
    test.fail(
        f"{len(long) / len(short):.1f}x the input took "
        f"{fastest_long / fastest_short:.1f}x the time "
        f"({fastest_short * 1000:.1f} ms, then {fastest_long * 1000:.1f} ms)"
    )


def _linear_time_tests(
    shapes: dict[str, Callable[[int], str]],
    work: Callable[[str], object] = _redacts_both_ways,
) -> Callable[[type[unittest.TestCase]], type[unittest.TestCase]]:
    """Add a `test_<name>` to the decorated class for each of `shapes`.

    One test per shape rather than one test looping over them: each
    `_assert_linear_time` takes about a second of CPU, and the per-test cap
    applies to wall time, which a loaded machine stretches. Thirty shapes in
    one test took over eight seconds on an idle machine.
    """

    def add(cls: type[unittest.TestCase]) -> type[unittest.TestCase]:
        for name, make in shapes.items():

            def test(self: unittest.TestCase, make: Callable[[int], str] = make) -> None:
                _assert_linear_time(self, make, work)

            test.__name__ = f"test_{name}"
            test.__qualname__ = f"{cls.__qualname__}.{test.__name__}"
            if hasattr(cls, test.__name__):
                raise TypeError(f"{cls.__qualname__} already has {test.__name__}")
            setattr(cls, test.__name__, test)
        return cls

    return add
