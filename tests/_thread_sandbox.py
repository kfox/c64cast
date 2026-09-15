"""Fail the test that leaves a background thread running.

A thread outlives every guard its test was wrapped in — `quiet_logging()` is
`logging.disable` and `assertLogs` swaps a handler, both scoped to a block the
thread survives — so whatever it logs afterwards reaches `logging.lastResort`
and prints inside an unrelated test, in whichever worker process drew it.

:func:`arm` wraps `unittest.TestCase.run` to register a cleanup *before* the
test's own, which puts it last on the LIFO cleanup stack — after `tearDown`,
after `quiet_logging()`'s restore, after anything the test registered itself.
Any thread alive there that was not alive when the test started fails that
test.

`ThreadLeak` derives from `AssertionError`, so it is reported as a plain test
failure rather than an error, and it carries the thread names: a `PollThread`
names itself at its construction site, while a bare `threading.Thread` reports
as `Thread-N` and the failing test is the only clue to its owner.

A stray gets `_GRACE_S` to finish first — the same bound `PollThread.stop()`
gives its own join — because a thread released by a cleanup that just ran is
still briefly alive. A thread that outlasts that is not winding down.

Two blind spots worth knowing:

* Threads started outside a test method — at module import, in `setUpClass` or
  `setUpModule` — are already in the snapshot the first test takes, so they are
  attributed to nobody. Their output leaks the same way.
* A test that fails for its own reason and *also* leaks reports both, which
  reads as two failures of one test.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any

# How long a stray may take to finish before it counts as a leak. Matches
# PollThread's default join_timeout: a loop told to stop gets one bounded wait,
# here as there.
_GRACE_S = 0.5

_armed = False


class ThreadLeak(AssertionError):
    """A test ended with a thread it started still running."""


def arm() -> None:
    """Install the per-test check. Idempotent."""
    global _armed
    if _armed:
        return
    _armed = True
    original = unittest.TestCase.run

    def run(self: unittest.TestCase, result: Any = None) -> Any:
        self.addCleanup(check_no_strays, set(threading.enumerate()))
        return original(self, result)

    unittest.TestCase.run = run  # type: ignore[method-assign]


def check_no_strays(before: set[threading.Thread]) -> None:
    """Raise `ThreadLeak` if any thread absent from `before` is still running.

    Exposed for tests/test_thread_sandbox.py, which drives it directly rather
    than leaking a thread to watch the armed hook catch it."""
    if not _strays(before):
        return

    deadline = time.monotonic() + _GRACE_S
    for thread in _strays(before):
        thread.join(max(0.0, deadline - time.monotonic()))

    names = sorted(thread.name for thread in _strays(before))
    if not names:
        return
    raise ThreadLeak(
        f"still running after the test: {', '.join(names)} — stop and join what "
        "the test started (the owning object's teardown, in addCleanup), or its "
        "log records land in a later test"
    )


def _strays(before: set[threading.Thread]) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t not in before and t.is_alive()]
