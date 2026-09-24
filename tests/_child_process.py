"""Start a child process under a bound, so one that never exits says so.

`subprocess.run` without `timeout` waits forever. On Windows it waits somewhere
especially unhelpful: `Popen.communicate` sets `endtime = None` when no timeout
was given, `_remaining_time(None)` returns None, and the reader-thread join
becomes `self.stdout_thread.join(None)` — an unbounded wait on a thread
draining a pipe the child never closes.

That is CI on PR #491, Windows/py3.14 only: `node --check` did not return,
`test_page_assets` sat in that join, and the run reported
`TestTimedOut: no progress for 60s` — `tests/_timeout_sandbox.py`'s per-test
cap, which names the test but not the cause, forty seconds later than the
cause was knowable. Before that cap landed (#486) the same hang spent the whole
CI job and named nothing at all.

:data:`BOUND_S` is what the bound buys back. Passing `timeout` makes `endtime`
finite, so the join is bounded, `_communicate` raises `TimeoutExpired`, and
`run`'s handler kills the child before re-raising — no orphan to inherit.

:func:`run_bounded` is how a *test module* starts a child, and
`tests/test_child_process.py` sweeps every module under `tests/` and fails one
that reaches `subprocess` without a `timeout` — which is why there is no second
copy of this reasoning at a call site.

Not every child the suite starts, though, because the sweep reads `tests/`
only. Production code under test starts its own: `doctor._probe_uv_lock` runs
a real `uv lock --check` in 31 of `test_doctor`'s tests, bounded at 60 s — the
same number as `_timeout_sandbox._CAP_S`, so a hung `uv` there still reports as
the cap rather than as a stuck child. That bound is a production choice for a
legitimately slow command and is not this module's to change.
"""

from __future__ import annotations

import subprocess
from typing import Any

#: Seconds a child process started by a test may run.
#:
#: A full run of the suite's subprocess-bearing modules starts 155 children and
#: the slowest measures 0.73 s (a `git` call against a scratch repository), so
#: this is ~27x the real ceiling — no honest child is near it even on a runner
#: several times slower. The other end is `_timeout_sandbox._CAP_S`: staying at
#: a third of it means two expirations inside one test still report as stuck
#: children rather than as the per-test cap's vaguer "no progress", and the
#: first of them arrives 40 s before that cap would.
#: tests/test_child_process.py pins both ends.
BOUND_S = 20.0


def run_bounded(
    argv: list[str] | tuple[str, ...],
    *,
    timeout: float = BOUND_S,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """`subprocess.run(argv, ...)`, bounded, failing the test if it expires.

    `timeout` overrides :data:`BOUND_S` for a child genuinely slower than it;
    every other keyword goes straight to `subprocess.run`.

    A child that outlives the bound is killed and the test fails naming the
    command, the bound and whatever the child managed to write. Escaping as
    `TimeoutExpired` would be enough to end the test, but its own message says
    only that a command timed out — the stream it died holding is usually the
    reason, and it is already in hand.
    """
    try:
        return subprocess.run(argv, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as expired:
        raise AssertionError(
            f"child process did not exit within {timeout:g}s and was killed: "
            f"{list(argv)}{_captured(expired)}"
        ) from expired


def _captured(expired: subprocess.TimeoutExpired) -> str:
    """What the killed child had written, as a suffix for the failure message.

    Empty when it wrote nothing or was not captured. `output` and `stderr` are
    `bytes` unless the caller asked for text, and the tail is the end worth
    having: a child that hung after printing usually hung on the last thing it
    said.
    """
    parts = []
    for label, raw in (("stdout", expired.output), ("stderr", expired.stderr)):
        if not raw:
            continue
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        parts.append(f"{label}: ...{text[-500:]}" if len(text) > 500 else f"{label}: {text}")
    return "\n" + "\n".join(parts) if parts else ""
