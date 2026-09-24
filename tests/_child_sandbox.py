"""Hold every child the test process waits on to the suite's own bound.

`tests/_child_process.py`'s :func:`~_child_process.run_bounded` bounds the
children a *test module* starts, and the AST sweep that makes it compulsory
reads `tests/` only. Production code under test starts children of its own,
under bounds chosen for a user at a terminal rather than for a test run — and
two of those in this tree are exactly `_timeout_sandbox._CAP_S`:

* `doctor._probe_uv_lock` runs a real `uv lock --check` under `timeout=60`,
  in 31 of `test_doctor`'s tests (#496);
* `scripts/lint_comments.py` runs `git diff --cached` and `git show` under
  `_DIFF_TIMEOUT_S = 60`, in 17 of `test_prose_gate`'s.

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
does. Both call sites above catch their own expiry —
`except (OSError, subprocess.TimeoutExpired)` in one, `except (OSError,
subprocess.SubprocessError)` in the other — so re-raising the `TimeoutExpired`
would be swallowed into a "could not check" diagnostic in `doctor` and into an
empty diff the prose gate then passes in `lint_comments`, and the test would go
green over a command that never returned. A distinct type is what clears those
two; `BaseException` also clears the `except Exception` that `doctor` and
`upgrade` degrade through elsewhere, and whichever one the next probe writes.
unittest's `testPartExecutor` catches with a bare `except`, so a BaseException
that is not `KeyboardInterrupt` is still recorded against the test that earned
it.

A caller that asked for *no more than* `BOUND_S` keeps its own
`TimeoutExpired`. That bound is the caller's own behavior — `upgrade._stop`'s
interrupt grace, which is `BOUND_S` exactly, and `run_bounded`'s `timeout=`,
both of which their tests grade — and converting it would grade something
else.

`Popen.communicate` and `Popen.wait` are the two hooks because every spelling
that waits reaches one of them: `run` and `check_output` through
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
import subprocess
from typing import Any

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

_armed = False


class ChildProcessHung(BaseException):
    """A child process outlived the suite's bound and was killed."""


def arm() -> None:
    """Install the clamp on `Popen.communicate` and `Popen.wait`. Idempotent."""
    global _armed
    if _armed:
        return

    def communicate(
        self: subprocess.Popen[Any], input: Any = None, timeout: float | None = None
    ) -> tuple[Any, Any]:
        bound = _child_process.BOUND_S
        if timeout is not None and timeout <= bound:
            return _ORIGINAL_COMMUNICATE(self, input, timeout)
        try:
            return _ORIGINAL_COMMUNICATE(self, input, bound)
        except subprocess.TimeoutExpired as expired:
            raise _hung(self, bound, timeout, expired) from expired

    def wait(self: subprocess.Popen[Any], timeout: float | None = None) -> int:
        bound = _child_process.BOUND_S
        if timeout is not None and timeout <= bound:
            return _ORIGINAL_WAIT(self, timeout)
        try:
            return _ORIGINAL_WAIT(self, bound)
        except subprocess.TimeoutExpired as expired:
            raise _hung(self, bound, timeout, expired) from expired

    subprocess.Popen.communicate = communicate  # type: ignore[method-assign]
    subprocess.Popen.wait = wait  # type: ignore[method-assign]
    _armed = True


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
