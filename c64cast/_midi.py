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
# leaves the flush at most 25% late and still runs the stop check ~240x a
# second against `PollThread`'s 1 s join.
_DRAIN_BUDGET_FRACTION = 0.25

# How long one drain pass may spend retiring messages, whatever the count bound
# would still permit. :data:`MAX_MSGS_PER_DRAIN` bounds the message *count*, but
# the wire chooses the *work* per message: one WARNING through the default
# terminal handler costs ~322 us of Rich rendering, so 64 of them is 20.6 ms
# inside a pass whose loop is otherwise sub-millisecond — which starves exactly
# the two things the count bound exists to protect. A pass always hands out at
# least one message, so a consumer slower than the whole budget still makes
# progress instead of spinning.
#
# This is the *default*, and it is sized for a consumer whose per-message cost
# is microseconds — `AsidScene._handle_sysex` decodes, pokes a shadow, and
# returns, so an ordinary pass never approaches it. It is emphatically NOT a
# universal budget: `MidiScene._handle_msg` issues blocking link writes inside
# the drain, and on an Ultimate one of those (5.222 ms) already exceeds this
# whole budget, so an ordinary note pass there would spend it on message one.
# A caller whose consumer is not cheap passes its own `budget_s` sized from
# what it actually costs — see `midi_scene._drain_budget_s`. The sizing lives
# with the caller and not here because reaching a `HardwareProfile` from this
# module would invert the layering, and the two callers' numbers differ.
MAX_DRAIN_WORK_S = _READER_FLUSH_PERIOD_S * _DRAIN_BUDGET_FRACTION


def poll_pending(
    port: Any,
    stop: threading.Event,
    *,
    limit: int | None = None,
    budget_s: float | None = None,
) -> Iterator[Any]:
    """Yield at most ``limit`` messages already waiting on ``port``, for at most
    ``budget_s`` of consumer work, stopping early once ``stop`` is set.

    The bounded, stop-aware stand-in for mido's ``iter_pending()`` in a reader
    loop. Both bounds are load-bearing rather than tuning knobs — see
    :data:`MAX_MSGS_PER_DRAIN` for the count and :data:`MAX_DRAIN_WORK_S` for
    the work. The clock is read between messages, i.e. after the consumer has
    processed the previous one, and never after a ``poll()`` that already took a
    message off the queue, so releasing the pass drops nothing.

    Both bounds accept ``None``, which is not "unbounded": it selects
    :data:`MAX_MSGS_PER_DRAIN` (64) and :data:`MAX_DRAIN_WORK_S` (4.167 ms)
    respectively. They read as parameter defaults would, except that the value
    is picked when the pass runs — see the comment below. A caller wanting no
    bound at all passes a large number, and one wanting none of a bound passes
    ``0``, which is honored rather than read as absent.

    The ``budget_s`` default is sized for a microsecond-per-message consumer; a
    caller whose consumer blocks on the link must pass its own or it will retire
    exactly one message a pass. :data:`MAX_DRAIN_WORK_S` says why the sizing
    belongs to the caller."""
    # Read here, not bound as the parameters' defaults, so that rebinding
    # either constant is not a silent no-op — this module's one injection idiom
    # is rebinding, as the next statement does for `_monotonic`. Full rationale
    # in docs/architecture/config.md under `_midi.py`. Note that rebinding
    # reaches only the callers that take the default (AsidScene): MidiScene's
    # reader always passes its own `budget_s`.
    if limit is None:
        limit = MAX_MSGS_PER_DRAIN
    if budget_s is None:
        budget_s = MAX_DRAIN_WORK_S

    deadline = _monotonic() + budget_s
    for retired in range(limit):
        if retired and _monotonic() >= deadline:
            return
        msg = port.poll()
        if msg is None or stop.is_set():
            return
        yield msg
