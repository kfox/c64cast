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
that reaches `subprocess` without a `timeout`, counting `timeout=None` as none
— which is why there is no second copy of this reasoning at a call site.

Not every child the suite starts, though, because the sweep reads `tests/`
only: production code under test starts its own, under bounds that are a
production choice and not this module's to rewrite. `tests/_child_sandbox.py`
holds those to :data:`BOUND_S` for the length of the test process — the
production numbers stay where they are — and raises past the caller's
`except` when one of them expires.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

#: Seconds a child process started under a test may run — one a test module
#: starts through :func:`run_bounded`, and, through `tests/_child_sandbox.py`,
#: one that production code under test starts for itself.
#:
#: A full run starts 192 children and the slowest measures 0.41 s (a `git
#: commit` against a scratch repository); the 31 real `uv lock --check` calls
#: `doctor._probe_uv_lock` makes come in under 0.1 s each. So this is ~48x the
#: real ceiling — no honest child is near it even on a runner several times
#: slower. The other end is `_timeout_sandbox._CAP_S`: staying at a third of it
#: means two expirations inside one test still report as stuck children rather
#: than as the per-test cap's vaguer "no progress", and the first of them
#: arrives 40 s before that cap would.
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
        raise AssertionError(hung_message(argv, timeout, expired)) from expired


def hung_message(
    argv: Any, bound: float, expired: subprocess.TimeoutExpired, *, note: str = ""
) -> str:
    """What a child that had to be killed was, and what it had written.

    Shared with `tests/_child_sandbox.py`, which kills children this module
    never started: two renderings of one failure would drift, and the tail of
    the stream is the part a reader has to be able to rely on finding.

    `note` goes between the command and that tail, for a caller whose bound is
    not the one the command was written against.
    """
    return (
        f"child process did not exit within {bound:g}s and was killed: "
        f"{_argv_text(argv)}{note}{_captured(expired)}"
    )


def _argv_text(argv: Any) -> str:
    """`argv` as one readable string, for every spelling `Popen` accepts.

    `list()` is wrong for the `shell=True` spelling, where `Popen.args` is a
    single string and listing it spells the command out one character per
    element — and it raises outright on the `Popen(Path(...))` spelling, which
    would replace the named hang with a `TypeError` from here.
    """
    if isinstance(argv, (str, bytes, os.PathLike)):
        return os.fsdecode(argv)
    return str(list(argv))


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
