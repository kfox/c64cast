"""ASID *host* / broadcaster — emit ASID MIDI SysEx so external ASID clients
play in sync with what c64cast drives on its own SID + HDMI screen.

This is the reverse of :class:`~c64cast.asid_scene.AsidScene` (the ASID
*client*). Given a per-frame 25-byte ``$D400-$D418`` register image — which
:class:`~c64cast.waveform.WaveformScene` already reconstructs host-side via
:class:`~c64cast.sid_host_emu.SidHostEmu` — this class packs it with the pure
encoder in :mod:`c64cast.asid` and ships it out a MIDI **output** port at the
tune's PLAY rate. Any ASID client (TherapSID, SidStation, USBSID-Pico, DeepSID,
another c64cast running the ``asid`` scene) then plays the same tune.

**Delta encoding.** The ASID ``0x4E`` mask format is built for partial updates,
so :meth:`send_frame` diffs each chip's image against the last one sent and
emits only the changed registers (empty → no message for that chip). A fresh
:meth:`start` resets the per-chip baseline so the next frame is a *full* image —
that's how a client joining mid-stream (or a new subtune) gets a complete state.

**Hard restart.** The host emulator collapses a within-tick gate off→on pulse
into a boolean per-voice ``retrigger`` flag (the 25-byte shadow keeps only the
final value). :meth:`send_frame` reconstructs the pulse for the client: a
retriggered voice force-includes its control register with ``control_first`` =
the final control value with the gate bit cleared, which the encoder emits on
the ASID first-write id (22-24) ahead of the final value (25-27).

Multi-SID falls out for free: pass one image per chip and each extra chip is
emitted as ``0x50``-``0x5F``.

Requires the ``midi`` extra (``pip install c64cast[midi]``) — ASID rides the
same MIDI transport. Constructing a broadcaster without mido raises a clear
``RuntimeError``; the pure :func:`frame_messages` helper needs no mido and is
used both here and by the unit tests.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from . import asid
from .c64 import SID

log = logging.getLogger(__name__)

# Typed as Any so Pyright doesn't flag mido.* as attributes of None — the
# MIDI_AVAILABLE flag is the runtime guard. Mirrors asid_scene.py / midi_scene.py.
try:
    import mido as _mido

    mido: Any = _mido
    MIDI_AVAILABLE = True
except ImportError:
    mido = None
    MIDI_AVAILABLE = False

# 25-byte $D400-$D418 register image length.
_SID_REG_COUNT = SID.N_VOICES * SID.BYTES_PER_VOICE + 4  # 3*7 + 4 = 25
# Voice control-register offsets within a SID (gate bit lives here).
_CONTROL_OFFSETS = tuple(v * SID.BYTES_PER_VOICE + SID.OFF_CONTROL for v in range(SID.N_VOICES))


def frame_messages(
    images: Sequence[Sequence[int]],
    last_images: Sequence[Sequence[int] | None],
    retrigger: Sequence[Sequence[bool]] | None = None,
) -> list[list[int]]:
    """Build the ASID register SysEx payload(s) for one frame — pure, no mido.

    ``images[chip]`` is the chip's current 25-byte ``$D400-$D418`` image;
    ``last_images[chip]`` is the previously-sent image (or ``None`` for a full
    frame). Returns one ``0x4E``/``0x50+`` payload per chip whose image changed
    (or has a retriggered voice), in chip order; unchanged chips are skipped.

    A voice flagged in ``retrigger[chip]`` force-includes its control register
    (even when the shadow value is unchanged) with a synthesized ``control_first``
    (final value, gate cleared) so the receiver replays the hard-restart pulse.
    """
    messages: list[list[int]] = []
    for chip, image in enumerate(images):
        last = last_images[chip] if chip < len(last_images) else None
        rt = retrigger[chip] if retrigger is not None and chip < len(retrigger) else None
        regs: dict[int, int] = {}
        control_first: dict[int, int] = {}
        for offset in range(min(len(image), _SID_REG_COUNT)):
            value = image[offset] & 0xFF
            if last is None or offset >= len(last) or value != (last[offset] & 0xFF):
                regs[offset] = value
        # Hard restart: a retriggered voice must re-send its control register
        # with a gate-off first write, regardless of whether the final value
        # changed (a same-value re-attack would otherwise be a no-op delta).
        if rt is not None:
            for voice, fired in enumerate(rt):
                if not fired:
                    continue
                offset = voice * SID.BYTES_PER_VOICE + SID.OFF_CONTROL
                if offset < len(image):
                    final = image[offset] & 0xFF
                    regs[offset] = final
                    control_first[voice] = final & ~SID.GATE
        if regs:
            messages.append(
                asid.encode_registers(regs, chip_index=chip, control_first=control_first or None)
            )
    return messages


class AsidBroadcaster:
    """Emit an ASID MIDI stream out an output port (see the module docstring).

    Lifecycle: construct → :meth:`start` (opens the port, sends 0x4C + 0x31 +
    0x32 + optional 0x4F) → :meth:`send_frame` per PLAY tick → :meth:`stop`
    (sends 0x4D, closes the port). All sends are best-effort: a port error is
    logged once and disables further output rather than crashing the caller's
    render/poll thread.
    """

    def __init__(self, port_name: str | None = None, *, system: str = "PAL") -> None:
        if not MIDI_AVAILABLE:
            raise RuntimeError(
                "AsidBroadcaster requires mido + python-rtmidi (pip install c64cast[midi])"
            )
        self.port_name = port_name
        self.system = system
        self._port: Any = None
        self._disabled = False
        # Per-chip last-sent image for delta encoding; None = send a full frame.
        self._last_images: list[bytearray | None] = []

    # ---- MIDI plumbing -------------------------------------------------------
    def _open_port(self) -> None:
        """Open the output port by exact/substring match (mirrors
        AsidScene._open_port, but for output). Falls back to the first available
        port when the name is unset/``"default"``."""
        assert mido is not None
        names = mido.get_output_names()
        if self.port_name in (None, "", "default"):
            if not names:
                raise RuntimeError("AsidBroadcaster: no MIDI output ports available")
            self._port = mido.open_output(names[0])
            log.info("AsidBroadcaster: opened MIDI output port %r", names[0])
            return
        assert self.port_name is not None
        match = next((n for n in names if self.port_name.lower() in n.lower()), None)
        if match is None:
            raise RuntimeError(
                f"AsidBroadcaster: no MIDI output port matches {self.port_name!r}; "
                f"available: {names}"
            )
        self._port = mido.open_output(match)
        log.info("AsidBroadcaster: opened MIDI output port %r", match)

    def _send(self, payload: list[int]) -> None:
        """Send one ASID SysEx payload (best-effort)."""
        if self._disabled or self._port is None:
            return
        try:
            self._port.send(mido.Message("sysex", data=payload))
        except Exception:
            self._disabled = True
            log.warning("AsidBroadcaster: MIDI send failed — disabling broadcast", exc_info=True)

    # ---- lifecycle -----------------------------------------------------------
    def start(
        self,
        *,
        frame_rate_hz: float | None = None,
        chip_types: Sequence[str | None] | None = None,
        text: str | None = None,
    ) -> None:
        """Open the port (if needed) and announce a (re)start: 0x4C, then 0x31
        speed (from ``frame_rate_hz``), a 0x32 per known chip type, and an
        optional 0x4F now-playing string. Resets the delta baseline so the next
        :meth:`send_frame` is a full image (client-join / new-subtune safe)."""
        if self._port is None and not self._disabled:
            self._open_port()
        self._last_images = []
        self._send(asid.encode_start())
        if frame_rate_hz and frame_rate_hz > 0:
            self._send(
                asid.encode_speed(self.system, frame_delta_us=round(1_000_000 / frame_rate_hz))
            )
        if chip_types:
            for chip, ctype in enumerate(chip_types):
                if ctype:
                    self._send(asid.encode_sid_type(chip, ctype))
        if text:
            self._send(asid.encode_chars(text[:32]))

    def set_frame_rate(self, frame_rate_hz: float) -> None:
        """Re-announce the PLAY cadence via a 0x31 message."""
        if frame_rate_hz > 0:
            self._send(
                asid.encode_speed(self.system, frame_delta_us=round(1_000_000 / frame_rate_hz))
            )

    def send_text(self, text: str) -> None:
        """Send a 0x4F now-playing string."""
        self._send(asid.encode_chars(text[:32]))

    def send_frame(
        self,
        images: Sequence[Sequence[int]],
        *,
        retrigger: Sequence[Sequence[bool]] | None = None,
    ) -> None:
        """Encode + send this frame's register deltas (one 0x4E/0x50+ per changed
        chip). Grows the per-chip baseline as chips appear."""
        if self._disabled or self._port is None:
            return
        while len(self._last_images) < len(images):
            self._last_images.append(None)
        for payload in frame_messages(images, self._last_images, retrigger):
            self._send(payload)
        # Update the baseline for every chip we saw (whether or not it changed).
        for chip, image in enumerate(images):
            self._last_images[chip] = bytearray(bytes(image[:_SID_REG_COUNT]))

    def stop(self) -> None:
        """Send 0x4D stop and close the port (best-effort, idempotent)."""
        self._send(asid.encode_stop())
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                log.debug("AsidBroadcaster: port close failed", exc_info=True)
            self._port = None
