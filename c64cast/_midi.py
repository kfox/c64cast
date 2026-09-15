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

# The drain's work bound reads the clock through this name, so rebinding the
# module attribute lets a test drive a pass without sleeping through one.
_monotonic = time.monotonic

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


# How many already-queued messages a reader pass may retire — ~7x the busiest
# legitimate stream measured (a 16x multispeed 8-SID ASID frame is ~9 messages
# per 1 ms pass).
MAX_MSGS_PER_DRAIN = 64

# AsidScene and MidiScene each flush at 1/60 s after their drain — the deadline
# a drain pass must not eat.
_READER_FLUSH_PERIOD_S = 1.0 / 60.0

_DRAIN_BUDGET_FRACTION = 0.25

# The *default* work bound for a pass, sized for a consumer whose per-message
# cost is microseconds; a caller whose consumer is not cheap passes its own
# `budget_s` (see `midi_scene._drain_budget_s`). Why a count bound alone is not
# enough, and what a rebind of this constant does and does not reach:
# docs/architecture/config.md#_midipy--the-guarded-mido-import.
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
    the work. The *clock* is read between messages, i.e. after the consumer has
    processed the previous one, and never after a ``poll()`` that already took a
    message off the queue, so releasing on the budget drops nothing. Between
    messages means after the first, so **a pass never gates the message it
    opens with**, and a consumer slower than the whole budget still makes
    progress rather than spinning.

    The ``stop`` re-check is the one release that does drop, and deliberately:
    it sits *after* the ``poll()``, so a pass ending on ``stop`` discards the
    message it had just taken. That is the teardown path — `PollThread`'s
    bounded join is what is waiting on it, and both readers set ``stop`` only
    from their ``teardown`` — so losing one frame of register writes to a scene
    that is going away beats draining a port whose owner has stopped reading.

    `test_a_pass_entered_with_stop_already_set_drops_the_message_it_polls` is
    what pins that drop against a change to the ``stop`` release itself: hoist
    the check above the ``poll()`` and that test reddens on the drop — nothing
    handed out, one message taken — rather than on a count of what was handed
    out. `test_stops_mid_pass_once_the_stop_event_is_set` asserts the same pair
    two messages further into the pass, where the fake sets ``stop`` from inside
    the ``poll()`` that trips it, so its yielded count moves first under that
    same hoist. Its dequeued count still earns its place: with
    ``MAX_MSGS_PER_DRAIN = 2`` it reddens — 2 dequeued against the expected 3 —
    while the yielded count beside it passes, a two-message pass handing out
    exactly the two that assertion expects. That is the mutation that was run,
    not a property of every count-bound one: at 1 the yielded count moves too.
    The counts are of messages dequeued, not of ``poll()`` calls: an exhausted
    port answers ``None`` and takes nothing.

    Both bounds accept ``None``, which is not "unbounded": it selects
    :data:`MAX_MSGS_PER_DRAIN` (64) and :data:`MAX_DRAIN_WORK_S` (4.167 ms)
    respectively. They read as parameter defaults would, except that the value
    is picked when the pass runs — see the comment below. A caller wanting no
    bound at all passes a large number.

    A zero is honored rather than read as absent, but the two bounds do not
    answer it alike, and only ``limit=0`` hands out nothing whatever the port
    holds. ``budget_s=0`` yields *at most* one message: one when something is
    waiting **and** ``stop`` is clear, and none otherwise — on an idle port,
    where the ``poll()`` returns ``None`` before the yield, or under an
    already-set ``stop``, where the poll instead succeeds and the message it
    took is discarded, as the paragraph above says. It is one rather than none
    because a pass never gates the message it opens with, which that same
    paragraph states and
    `test_a_pass_always_hands_out_at_least_one_message` pins. That one message
    reaches the consumer, which on these two readers is a real SID register
    write or ASID frame, so a caller wanting a pass that cannot retire anything
    wants ``limit=0``.

    The ``budget_s`` default is sized for a microsecond-per-message consumer; a
    caller whose consumer blocks on the link must pass its own or a pass with
    traffic waiting will retire just the one message. :data:`MAX_DRAIN_WORK_S`
    says why the sizing belongs to the caller."""
    # Read in the body, not bound as the parameters' defaults, so that rebinding
    # either constant is not a silent no-op.
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
