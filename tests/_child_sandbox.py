"""Hold every child the test process waits on to the suite's own bound.

`tests/_child_process.py`'s :func:`~_child_process.run_bounded` bounds the
children a *test module* starts, and the AST sweep that makes it compulsory
reads `tests/` only. Production code under test starts children of its own,
under bounds chosen for a user at a terminal rather than for a test run — and
three of those in this tree are exactly `_timeout_sandbox._CAP_S`:

* `doctor._probe_uv_lock` runs a real `uv lock --check` under `timeout=60`, in
  31 of `test_doctor`'s tests (#496); an expiry becomes a `warn` diagnostic
  reading "could not check".
* `scripts/lint_comments.py` runs `git diff --cached` and `git show` under
  `_DIFF_TIMEOUT_S = 60`, in 12 of `test_prose_gate`'s; an expiry becomes an
  empty diff the gate then passes, or an unknown comment map it judges the
  line without.
* `scripts/check_venv_target.py` runs the project environment's interpreter
  under `_RESOLVE_TIMEOUT_S = 60`, in `test_venv_target`'s
  interpreter-isolation tests; an expiry becomes an environment it either
  waves through or reports as unrunnable.

A bound equal to the cap can never fire first: the per-test deadline starts
when the test starts and the child starts after it, so the cap always expires
sooner. A wedged command is therefore reported as `TestTimedOut: no progress
for 60s`, which names the test and not the command — the blindness
`run_bounded` exists to remove, arriving by a route it does not cover.

:func:`arm` closes that inside the test process: a wait longer than
:data:`_child_process.BOUND_S`, or one with no bound at all, is shortened to
`BOUND_S`, and a child that outlives it is killed and named. The production
numbers do not move — `--doctor` run by hand still gives `uv` its 60 seconds.

`ChildProcessHung` derives from `BaseException` for the reason `TestTimedOut`
does. Every site above catches the `TimeoutExpired` this replaces —
`except (OSError, subprocess.TimeoutExpired)` in `doctor`, `except (OSError,
subprocess.SubprocessError)` in the other two — and carries on into the
degraded answer its bullet names, so re-raising it would take the test green
over a command that never returned. A distinct type is what clears those
three; `BaseException` also clears the `except Exception` that `doctor` and
`upgrade` degrade through elsewhere, and whichever one the next probe writes.
unittest's `testPartExecutor` catches with a bare `except`, so a BaseException
that is not `KeyboardInterrupt` is still recorded against the test that earned
it.

A caller that asked for *no more than* `BOUND_S` keeps its own
`TimeoutExpired`. That bound is the caller's own behavior — `upgrade._stop`'s
interrupt grace, which is `BOUND_S` exactly, and `run_bounded`'s `timeout=`,
both of which their tests grade — and converting it would grade something
else.

A wait on a child the caller has already `kill()`ed is a reap, not the
command, and the clamp never cuts it below :data:`_REAP_S`. `subprocess.run`
answers its own expiry with `kill()` and then an unbounded `wait()`, which
reaches the clamp; held to `BOUND_S`, it turned the caller's `TimeoutExpired`
into a `ChildProcessHung` whenever the kernel took longer than `BOUND_S` to reap —
under the 0.3 s a test patches in, on a loaded macOS runner (#539). The kill is
recorded by wrapping `Popen.kill`, not inferred from timing. `terminate()` is
not recorded: a child may ignore SIGTERM, so the wait after one is still the
command's.

`Popen.communicate` and `Popen.wait` are the waiting hooks because every
spelling that waits reaches one of them: `run` and `check_output` through
`communicate`, `call` and `Popen.__exit__` through `wait`.

Blind spots worth knowing:

* A `Popen` nobody ever waits on is not seen here. It cannot hang the test
  either; what it can do is outlive the run, which is a different subject.
* `os.system`, `os.posix_spawn` and `multiprocessing` do not go through
  `subprocess` and are not reached.
* A test that patches `subprocess.run` or `subprocess.Popen` wholesale never
  reaches these wrappers, which is the wanted answer: its child is imaginary.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
import weakref
from collections.abc import Iterator
from typing import Any
from unittest import mock

import _child_process

#: Seconds a killed child gets to be reaped before the failure is raised. A
#: SIGKILL lands in microseconds; the wait is bounded at all so that a child
#: which somehow survives it cannot spend the per-test cap this module exists
#: to keep out of the report.
_REAP_S = 5.0

#: Captured before :func:`arm` replaces them, so :func:`_hung` can reap through
#: the real `wait` rather than recurse into the wrapper that called it.
_ORIGINAL_COMMUNICATE = subprocess.Popen.communicate
_ORIGINAL_WAIT = subprocess.Popen.wait
_ORIGINAL_KILL = subprocess.Popen.kill

#: Every `Popen` whose `kill()` has returned. Weak, so the record goes when the
#: `Popen` does.
_killed: weakref.WeakSet[subprocess.Popen[Any]] = weakref.WeakSet()

_armed = False


class ChildProcessHung(BaseException):
    """A child process outlived the suite's bound and was killed."""


def arm() -> None:
    """Install the clamp on `Popen.communicate`, `Popen.wait` and `Popen.kill`. Idempotent."""
    global _armed
    if _armed:
        return

    def communicate(
        self: subprocess.Popen[Any], input: Any = None, timeout: float | None = None
    ) -> tuple[Any, Any]:
        bound = _bound(self, timeout)
        if bound is None:
            return _ORIGINAL_COMMUNICATE(self, input, timeout)
        try:
            return _ORIGINAL_COMMUNICATE(self, input, bound)
        except subprocess.TimeoutExpired as expired:
            raise _hung(self, bound, timeout, expired) from expired

    def wait(self: subprocess.Popen[Any], timeout: float | None = None) -> int:
        bound = _bound(self, timeout)
        if bound is None:
            return _ORIGINAL_WAIT(self, timeout)
        try:
            return _ORIGINAL_WAIT(self, bound)
        except subprocess.TimeoutExpired as expired:
            raise _hung(self, bound, timeout, expired) from expired

    def kill(self: subprocess.Popen[Any]) -> None:
        _ORIGINAL_KILL(self)
        _killed.add(self)

    subprocess.Popen.communicate = communicate  # type: ignore[method-assign]
    subprocess.Popen.wait = wait  # type: ignore[method-assign]
    subprocess.Popen.kill = kill  # type: ignore[method-assign]
    _armed = True


def _bound(popen: subprocess.Popen[Any], requested: float | None) -> float | None:
    """The bound to wait under in place of `requested`, or None to keep the caller's."""
    bound = _child_process.BOUND_S
    if popen in _killed:
        bound = max(bound, _REAP_S)
    if requested is not None and requested <= bound:
        return None
    return bound


def _hung(
    popen: subprocess.Popen[Any],
    bound: float,
    requested: float | None,
    expired: subprocess.TimeoutExpired,
) -> ChildProcessHung:
    """Kill `popen`, then say what it was and how long it was given.

    Killed here rather than left to the caller: `subprocess.run` has a bare
    `except` that kills on the way past, but a `Popen` a test drives directly
    does not, and an orphan still holding a pipe is the next hang.

    Exposed to nobody — tests/test_child_sandbox.py drives the wrappers.
    """
    with contextlib.suppress(Exception):
        popen.kill()
    with contextlib.suppress(Exception):
        _ORIGINAL_WAIT(popen, _REAP_S)
    return ChildProcessHung(
        _child_process.hung_message(popen.args, bound, expired, note=_note(requested, bound))
    )


def _note(requested: float | None, bound: float) -> str:
    """Where the bound that expired came from, since it is not the caller's.

    The caller's own number is in the message because it is the thing to
    change if this child is legitimately slow, and because seeing "60s" next
    to a kill at 20 s is otherwise a contradiction the reader has to resolve.
    """
    asked = "no bound at all" if requested is None else f"{requested:g}s"
    return (
        f"\nthe caller allowed {asked}; the test suite waits no longer than "
        f"{bound:g}s, so a command that wedges is named here rather than "
        f"reported later as the per-test cap's 'no progress'"
    )


#: Seconds :func:`communicate_once_ready` waits for a child to signal. Its own
#: number rather than `BOUND_S`, which the tests using it patch down to a
#: fraction of a second.
_READY_S = 20.0


@contextlib.contextmanager
def communicate_once_ready(ready: str) -> Iterator[None]:
    """Start each `communicate` only once the file `ready` exists.

    For a test whose assertion is about what a killed child wrote. Its bound
    otherwise starts at the `communicate` call, while the child may not yet have
    started, and a short one can expire on a loaded machine before the child
    wrote anything. Held until the child creates `ready` after its writes, the
    bound covers only the wait the test is about, and the output is already in
    the pipe when it starts.

    A child that exits without creating `ready` releases the wait at once. One
    that never creates it fails the test after :data:`_READY_S`.
    """
    communicate = _ORIGINAL_COMMUNICATE

    def gated(popen: subprocess.Popen[Any], input: Any = None, timeout: float | None = None) -> Any:
        deadline = time.monotonic() + _READY_S
        while not os.path.exists(ready) and popen.poll() is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"the child never created {ready}: {popen.args!r}")
            time.sleep(0.01)
        return communicate(popen, input, timeout)

    with mock.patch(f"{__name__}._ORIGINAL_COMMUNICATE", gated):
        yield
