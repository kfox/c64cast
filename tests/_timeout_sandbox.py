"""Fail the test that stops making progress, rather than let it eat the run.

`make test` is `unittest_parallel -s tests`, which exposes no timeout of its
own, and the CI jobs bound only the whole job. A test that stops making
progress therefore spends the job budget and identifies itself nowhere: no
dot, no test id, no stack. It was watched happening while reviewing #444 —
`_handle_pause`'s idle poll mutated from `stop_event.wait(0.1)` to
`time.sleep(0.1)` spun in silence until a wall limit killed the run.

:func:`arm` starts one daemon watchdog per process and wraps
`unittest.TestCase.run` to tell it which test is running and until when. A test
still running when its cap expires has every thread's stack written to stderr
under its own name, and `TestTimedOut` raised in the thread running it — so the
run reports that test as an error, with the stack of wherever it was, and
carries on into the next one.

Why not `faulthandler.dump_traceback_later(..., exit=True)`, which is the
obvious reading of #449: `unittest_parallel` hands its suites to
`multiprocessing.Pool.map`, and a `Pool` never completes the job whose worker
died. Measured here against Python 3.14 with a worker calling `os._exit`: the
parent waits forever. Killing the worker turns one hung test into a hung
*run*, which is the thing being fixed.

Why `TestTimedOut` derives from `BaseException` where `ThreadLeak` derives from
`AssertionError`: the call that hung is often inside an `except Exception`,
which would swallow an ordinary exception and go straight back to hanging.
unittest's `testPartExecutor` catches with a bare `except`, so a BaseException
that is not `KeyboardInterrupt` is still reported against the test that earned
it instead of aborting the run.

The stack dump goes to stderr rather than travelling with the exception
because `PyThreadState_SetAsyncExc` takes the exception *class* — CPython
raises `SystemError` for an instance — so there is nothing to attach a message
to. It is also the half that survives the first blind spot below.

Blind spots worth knowing:

* An async exception is delivered between bytecodes, so a test blocked in a
  call that never returns to the interpreter — `Thread.join()` with no
  timeout, a socket read, `lock.acquire()` — is not interrupted. The stderr
  dump still names it and prints every thread's stack, and the watchdog keeps
  re-injecting in case the call does return, but such a run still ends at the
  CI job's own timeout. `signal` would reach a blocked main thread and is not
  portable: `SIGALRM` does not exist on Windows, and the spelling that is
  portable, `_thread.interrupt_main()`, raises `KeyboardInterrupt` — which
  unittest reads as "abort the run" and `Playlist.run` catches on purpose.
* The watchdog is an ordinary Python thread, so a test spinning inside C with
  the GIL held is not seen at all.
* An interruption can arrive after the test body is over — the test ended
  within `_TICK_S` of its own cap, or a re-injection landed while `run()` was
  winding down. unittest's `testPartExecutor` is no longer on the stack there,
  so the wrapper catches `TestTimedOut` itself and records it against the test
  that earned it. What it must never do is let it out of `TestCase.run`: that
  ends the worker's thread, and `Pool.map` never completes a job whose worker
  died, so the guard against hanging would be the hang. The attribution can
  still be off by one — a test interrupted in its own teardown is named, one
  interrupted while the *next* one is starting is charged to whichever holds
  the registration. The cap is 60 s against a slowest legitimate test of
  ~1.1 s, so nothing in this suite is near that edge.
* :func:`tick` swallows its own failures, so a pass that goes wrong is a
  missed interruption and nothing else — no record of it reaches the run. The
  alternative is worse in the direction that matters: an exception out of the
  loop body ends the watchdog thread, and every test that worker runs
  afterwards is unwatched while `_armed` goes on saying otherwise.
* `unittest_parallel --thread` runs tests concurrently in one process, where
  one `_cap_s` and one registration per thread ident still hold but this
  module's own tests — which patch `_cap_s` process-wide — would interrupt
  their neighbors. Nothing in this repository passes `--thread`; `make test`
  and CI are both process mode.
"""

from __future__ import annotations

import contextlib
import ctypes
import math
import os
import sys
import threading
import time
import traceback
import unittest
from typing import Any, TextIO

#: Seconds one test may run before the watchdog calls it hung. The slowest
#: legitimate test in this suite measures ~1.1 s, so this is ~50x the real
#: ceiling — no honest test is near it even on a runner several times slower —
#: while staying a small fraction of the 10-20 minute CI job budgets, so a hang
#: is named with most of the run still ahead of it.
_CAP_S = 60.0

#: Overrides the cap, in seconds; 0 or less turns the watchdog off, which is
#: what stepping through a test under a debugger needs. A value that will not
#: parse keeps the default, because the direction to fail in is "still armed".
_CAP_ENV = "C64CAST_TEST_TIMEOUT_S"

#: How often the watchdog compares the clock against the running deadlines.
_TICK_S = 0.25

#: How long after one interruption before another is injected, for a test whose
#: own `except` swallowed the first or whose blocking call has since returned.
_RETRY_S = 5.0

#: Where the stack dump goes. `sys.__stderr__` rather than `sys.stderr`, so a
#: test that has redirected stderr still leaves the evidence somewhere the
#: reader will see it. This module's own test rebinds it, to keep the
#: deliberate timeout it drives out of the run's output.
_DUMP_STREAM: TextIO | None = sys.__stderr__

_lock = threading.Lock()
_running: dict[int, _Watched] = {}
_cap_s = 0.0
_armed = False

_set_async_exc = ctypes.pythonapi.PyThreadState_SetAsyncExc
_set_async_exc.argtypes = (ctypes.c_ulong, ctypes.py_object)
_set_async_exc.restype = ctypes.c_int


class TestTimedOut(BaseException):
    """A test ran past the per-test cap and was interrupted where it stood."""

    def __str__(self) -> str:
        return (
            f"no progress for {_cap_s:g}s, so the suite interrupted this test where it "
            f"stood rather than let it spend the whole job unnamed. The stack above is "
            f"where it was; every thread's stack went to stderr as the cap expired. If "
            f"the test is legitimately this slow, make it faster — see "
            f"tests/_timeout_sandbox.py before raising the cap."
        )


class _Watched:
    """One test the watchdog is holding to a deadline."""

    __slots__ = ("deadline", "fired", "test_id")

    def __init__(self, test_id: str, deadline: float) -> None:
        self.test_id = test_id
        self.deadline = deadline
        self.fired = False


def configured_cap() -> float:
    """The cap this process runs under, from :data:`_CAP_ENV` or the default.

    A value that will not parse keeps the default, and so does one that is not
    finite: `inf` and `nan` both survive `float()` and then compare against a
    deadline in a way that never fires, which arms the guard and enforces
    nothing — the failure that looks exactly like success. 0 is the spelling
    that turns it off, and `_armed` then says so.
    """
    raw = os.environ.get(_CAP_ENV)
    if raw is None:
        return _CAP_S
    try:
        value = float(raw)
    except ValueError:
        return _CAP_S
    return value if math.isfinite(value) else _CAP_S


def thread_dump() -> str:
    """Every thread's stack as text, labeled by thread name.

    The stack of the test's own thread is what the reported error already
    carries; what only this can show is the *other* threads, which is where a
    test waiting on a worker that never finishes actually stopped.
    """
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    blocks = []
    for ident, frame in sys._current_frames().items():
        label = names.get(ident, "unnamed")
        blocks.append(f"--- {label} ({ident}) ---\n" + "".join(traceback.format_stack(frame)))
    return "\n".join(blocks)


def interrupt(ident: int) -> None:
    """Raise `TestTimedOut` in the thread `ident`.

    Exposed for tests/test_timeout_sandbox.py, which drives it against a thread
    it owns rather than waiting out a real cap.
    """
    _set_async_exc(ctypes.c_ulong(ident), ctypes.py_object(TestTimedOut))


def report(test_id: str, stream: TextIO) -> None:
    """Write `test_id`'s hang and every thread's stack to `stream`.

    Allowed to fail without anyone hearing about it: it runs after the
    interruption that actually ends the hang, so a stream closed at
    interpreter shutdown or a frame `traceback` cannot format costs the
    evidence and nothing else.
    """
    with contextlib.suppress(Exception):
        print(
            f"\n{test_id} has made no progress for {_cap_s:g}s — interrupting it. "
            f"Every thread's stack at that moment:\n{thread_dump()}",
            file=stream,
            flush=True,
        )


def _clear_pending(ident: int) -> None:
    """Drop an injected exception that thread `ident` never received.

    An empty `py_object` is the NULL that `PyThreadState_SetAsyncExc` reads as
    "cancel". Without this, a test that finished on its own in the moment
    between the injection and its deregistration would hand the pending
    exception to whatever ran next on that thread.
    """
    _set_async_exc(ctypes.c_ulong(ident), ctypes.py_object())


def _fire_overdue(now: float) -> list[tuple[str, TextIO]]:
    """Interrupt every test past its deadline; return the reports still to
    write, which is at most one per test and only on the first interruption.

    The injection is under the lock because the lock is what orders it against
    the registry: without it a thread could be removed from `_running`, and
    another test start on the same ident, between the deadline comparison and
    the `interrupt`. The lock is *not* what stops an undelivered exception
    reaching the next test — `PyThreadState_SetAsyncExc` only marks the thread
    state, and delivery happens at that thread's next bytecode, arbitrarily
    later. Cancelling it is :func:`_retire`'s `_clear_pending`, which is
    therefore not redundant with anything here.

    Writing the report is deliberately not under the lock: `_lock` is taken by
    every test to register and to deregister, so a watchdog that held it while
    writing to a stalled stderr would stop every remaining test in the worker
    from starting. That worker then never returns its job and `Pool.map` never
    completes — one hung test becoming a hung run, which is the outcome this
    module exists to avoid.
    """
    pending: list[tuple[str, TextIO]] = []
    with _lock:
        for ident, watched in list(_running.items()):
            if now < watched.deadline:
                continue
            first, watched.fired = not watched.fired, True
            watched.deadline = now + _RETRY_S
            interrupt(ident)
            # Only the first: the stacks were dumped when the cap expired and
            # repeating them every `_RETRY_S` would bury that copy.
            if first and _DUMP_STREAM is not None:
                pending.append((watched.test_id, _DUMP_STREAM))
    return pending


def tick() -> None:
    """One watchdog pass: interrupt every test now past its deadline.

    Swallows its own failure rather than letting it out, because what is on
    the other side is the `while` in :func:`_watch`: an exception here ends
    the watchdog thread, and every test that worker runs afterwards is
    unwatched while `_armed` goes on saying otherwise. One missed
    interruption is the smaller loss, and the next pass tries again a
    quarter-second later.

    Exposed for tests/test_timeout_sandbox.py, which drives one pass rather
    than waiting on the thread.
    """
    try:
        for test_id, stream in _fire_overdue(time.monotonic()):
            report(test_id, stream)
    except Exception:
        return


def _watch() -> None:
    while True:
        time.sleep(_TICK_S)
        tick()


def _retire(ident: int, watched: _Watched) -> None:
    """Take `ident` off the watchdog's list and drop any injection it never
    received.

    Cleared before taking the lock as well as under it. The watchdog holds
    `_lock` while it injects, so a thread parked on that lock is precisely
    where a pending exception gets delivered; clearing first keeps the
    ordinary case away from that edge, and the clear under the lock — after
    the entry is gone, so nothing can inject again — is the one that is final.

    Idempotent, because the wrapper calls it a second time on the path where
    an injection landed in here and unwound it before the entry came out.
    """
    if watched.fired:
        _clear_pending(ident)
    with _lock:
        _running.pop(ident, None)
        if watched.fired:
            _clear_pending(ident)


def arm() -> None:
    """Install the per-test cap and start the watchdog. Idempotent.

    A cap of 0 or less leaves the suite unwatched and `_armed` False, which is
    what tests/test_timeout_sandbox.py asserts against to know the suite it is
    running in really is wearing this.
    """
    global _armed, _cap_s
    if _armed:
        return
    _cap_s = configured_cap()
    if _cap_s <= 0.0:
        return
    _armed = True
    original = unittest.TestCase.run

    def run(self: unittest.TestCase, result: Any = None) -> Any:
        ident = threading.get_ident()
        watched = _Watched(self.id(), time.monotonic() + _cap_s)
        with _lock:
            # A TestCase driven from inside another test — how the sandbox
            # tests exercise the armed machinery — is part of the outer test's
            # work, and the outer deadline is the one that means anything.
            # Registering the inner one would displace that deadline and then
            # remove it, leaving the outer test unwatched for the rest of its
            # run.
            owned = ident not in _running
            if owned:
                _running[ident] = watched
        if not owned:
            return original(self, result)
        try:
            try:
                return original(self, result)
            finally:
                _retire(ident, watched)
        except TestTimedOut:
            # A re-injection that arrived once `testPartExecutor` was no longer
            # on the stack — in `run()`'s own tail, in one of the sibling
            # sandbox wrappers nested inside this one, or in `_retire` itself.
            # Letting it out of `TestCase.run` would end the worker's thread,
            # and `Pool.map` never completes a job whose worker died: the whole
            # run hangs on the guard against hanging. Recorded against the test
            # that earned it instead, and retired a second time because the
            # path through `_retire` may not have reached the removal.
            _retire(ident, watched)
            if result is not None:
                result.addError(self, sys.exc_info())
            return result

    unittest.TestCase.run = run  # type: ignore[method-assign]
    threading.Thread(target=_watch, name="per-test-timeout-watchdog", daemon=True).start()
