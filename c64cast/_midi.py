"""Shared guarded mido import + MIDI input-port resolution.

mido (+ python-rtmidi) is the optional `midi` extra, so every MIDI consumer
(midi_scene, asid_scene, midi_control) needs the same try/except import
guard — one copy lives here. `mido` is typed as Any so Pyright doesn't flag
mido.* as attributes of None (and doesn't miss open_input/get_input_names
through stubs); `MIDI_AVAILABLE` is the runtime guard callers check before
touching it. `open_input_port` is the shared input-port resolver behind each
consumer's `_open_port`. Consumers re-import `mido` under their own module
name, so patching `<consumer>.mido` still works for code in that module —
but port *resolution* reads this module's `mido`, so tests faking ports
patch `c64cast._midi.mido`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

# The drain's work bound reads the clock through this name so a test can drive a
# pass without sleeping through one. Rebinding the module attribute is the only
# injection point: `poll_pending` is a free function with no object to hang a
# clock off, and the call sites that matter (`AsidScene._reader`,
# `MidiScene._reader`) pass no arguments of their own.
_monotonic = time.monotonic

# Typed as Any so Pyright doesn't flag every mido.XXX as accessing attributes
# of None — the MIDI_AVAILABLE flag is the runtime guard. Also sidesteps
# pyright not seeing mido.open_input / mido.get_input_names through stubs.
try:
    import mido as _mido

    mido: Any = _mido
    MIDI_AVAILABLE = True
except ImportError:
    mido = None
    MIDI_AVAILABLE = False


def open_input_port(spec: str | None, *, label: str) -> tuple[Any, str]:
    """Open a mido input port and return ``(port, name)``.

    A ``spec`` of None / "" / "default" opens the first available input;
    anything else is matched as a case-insensitive substring of the available
    port names, so users don't need to paste the exact rtmidi string. Raises
    RuntimeError (prefixed with ``label``, the caller's user-facing name)
    when the ``midi`` extra isn't installed, when no port is available, or
    when nothing matches.
    """
    if not MIDI_AVAILABLE:
        raise RuntimeError(
            f"{label}: MIDI support requires the 'midi' extra "
            "(uv sync --all-extras, or pip install 'c64cast[midi]')"
        )
    names = mido.get_input_names()
    if spec in (None, "", "default"):
        if not names:
            raise RuntimeError(f"{label}: no MIDI input ports available")
        match = names[0]
    else:
        match = next((n for n in names if spec.lower() in n.lower()), None)
        if match is None:
            raise RuntimeError(f"{label}: no MIDI input port matches {spec!r}; available: {names}")
    port = mido.open_input(match)
    log.info("%s: opened MIDI port %r", label, match)
    return port, match


# How many already-queued messages a reader pass may retire. mido's
# `iter_pending()` yields until the port queue is momentarily *empty*, and
# rtmidi's input queue has no size limit — so a backlog arriving faster than the
# reader retires it never returns, and whatever the reader does after the drain
# is never reached: the rate-bounded register flush that keeps the SID current,
# and the stop check that lets teardown's bounded join finish. 64 leaves ~7x
# headroom over the busiest legitimate stream we know of (a 16x multispeed
# 8-SID ASID frame is ~9 messages per 1 ms pass).
MAX_MSGS_PER_DRAIN = 64

# The coalescing flush both readers run *after* their drain — AsidScene and
# MidiScene each flush at 1/60 s — i.e. the deadline a drain pass must not eat.
_READER_FLUSH_PERIOD_S = 1.0 / 60.0

# The share of that period one pass may spend retiring messages. A quarter
# leaves the flush at most 25% late, and still runs the stop check ~240x a
# second against `PollThread`'s 1 s join, while giving an ordinary pass (the
# busiest legitimate stream is ~9 sub-millisecond messages) room it never uses.
_DRAIN_BUDGET_FRACTION = 0.25

# How long one drain pass may spend retiring messages, whatever the count bound
# would still permit. :data:`MAX_MSGS_PER_DRAIN` bounds the message *count*, but
# the wire chooses the *work* per message: one WARNING through the default
# terminal handler costs ~322 us of Rich rendering, so 64 of them is 20.6 ms
# inside a pass whose loop is otherwise sub-millisecond — which starves exactly
# the two things the count bound exists to protect. A pass always hands out at
# least one message, so a consumer slower than the whole budget still makes
# progress instead of spinning.
MAX_DRAIN_WORK_S = _READER_FLUSH_PERIOD_S * _DRAIN_BUDGET_FRACTION


def poll_pending(
    port: Any,
    stop: threading.Event,
    *,
    limit: int = MAX_MSGS_PER_DRAIN,
    budget_s: float = MAX_DRAIN_WORK_S,
) -> Iterator[Any]:
    """Yield at most ``limit`` messages already waiting on ``port``, for at most
    ``budget_s`` of consumer work, stopping early once ``stop`` is set.

    The bounded, stop-aware stand-in for mido's ``iter_pending()`` in a reader
    loop. Both bounds are load-bearing rather than tuning knobs — see
    :data:`MAX_MSGS_PER_DRAIN` for the count and :data:`MAX_DRAIN_WORK_S` for
    the work. The clock is read between messages, i.e. after the consumer has
    processed the previous one, and never after a ``poll()`` that already took a
    message off the queue, so releasing the pass drops nothing."""
    deadline = _monotonic() + budget_s
    for retired in range(limit):
        if retired and _monotonic() >= deadline:
            return
        msg = port.poll()
        if msg is None or stop.is_set():
            return
        yield msg
