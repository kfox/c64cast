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
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

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


def poll_pending(
    port: Any, stop: threading.Event, *, limit: int = MAX_MSGS_PER_DRAIN
) -> Iterator[Any]:
    """Yield at most ``limit`` messages already waiting on ``port``, stopping
    early once ``stop`` is set. The bounded, stop-aware stand-in for mido's
    ``iter_pending()`` in a reader loop — see :data:`MAX_MSGS_PER_DRAIN` for why
    the bound is load-bearing rather than a tuning knob."""
    for _ in range(limit):
        msg = port.poll()
        if msg is None or stop.is_set():
            return
        yield msg
