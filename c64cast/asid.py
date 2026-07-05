"""ASID protocol decoder — ASID MIDI SysEx payloads → SID register updates.

The ASID protocol (spec: /Users/kfox/src/asid-protocol) streams SID register
writes frame-by-frame over MIDI SysEx; the receiving unit's SID chip
synthesizes the sound. Each message is ``F0 2D <cmd> <payload...> F7``; mido
hands us the bytes between ``F0`` and ``F7`` as ``msg.data``, i.e.
``(0x2D, cmd, *payload)``.

This module is a **pure** decoder — no mido, no hardware — so it's trivially
unit-testable: feed a byte sequence, assert the resulting register map. The
:class:`~c64cast.asid_scene.AsidScene` owns the MIDI port, the register shadow,
the DMA writes, and the oscilloscope.

Honored: ``0x4E`` register data (the workhorse — SID chip 0), the multi-SID
streams ``0x50``-``0x5F`` (SID2..SID17, same packed format → chips 1..16),
``0x4C``/``0x4D`` start/stop, ``0x4F`` character display, ``0x31`` speed
(PAL/NTSC + multiplier), and ``0x32`` SID type (per chip). OPL-FM (``0x60``) and
the ``0x30`` timing recipe are recognized but dropped (no OPL; the plain write
order suffices). Every register/type update carries a ``chip_index`` so the
scene can route it to the matching SID address (see :mod:`c64cast.asid_sidmap`
for the U64 address map). See docs/architecture.md for the rationale.

The ASID ``0x4E`` payload orders the three voice control registers last
(register IDs 22-27) so a frame can carry a *second* write to each control
register — the gate-off→gate-on "hard restart" trick players use to re-attack
an already-gated voice. We surface the first control value in
:attr:`AsidUpdate.control_first` so the scene can emit it before the coalesced
block write (which lands the second/final value), preserving the pulse a real
SID needs.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

# SysEx manufacturer id chosen by Elektron for ASID (45 = 0x2D).
ASID_MANUFACTURER_ID = 0x2D

# Commands (see spec/protocol.md).
CMD_TIMING = 0x30  # SID write order & timing recipe (dropped)
CMD_SPEED = 0x31  # PAL/NTSC + speed multiplier + frame delta
CMD_SID_TYPE = 0x32  # 6581 / 8580
CMD_START = 0x4C  # start playback
CMD_STOP = 0x4D  # stop playback
CMD_REG = 0x4E  # SID register data (the workhorse)
CMD_CHARS = 0x4F  # display characters
CMD_MULTI_SID_LO = 0x50  # SID2 .. SID17 register data (dropped)
CMD_MULTI_SID_HI = 0x5F
CMD_OPL = 0x60  # OPL-FM register data (dropped)

# ASID register ID (0-27) → SID register offset from $D400. IDs 0-21 are the
# 22 non-control registers in $D4xx order (skipping the control regs);
# IDs 22-24 are the *first* control write for voices 1/2/3 and 25-27 the
# *second* write to the same three control registers. See spec table.
# fmt: off
_ASID_REG_TO_OFFSET: tuple[int, ...] = (
    0x00, 0x01, 0x02, 0x03, 0x05, 0x06,  # voice 1: freq lo/hi, pw lo/hi, ad, sr
    0x07, 0x08, 0x09, 0x0A, 0x0C, 0x0D,  # voice 2
    0x0E, 0x0F, 0x10, 0x11, 0x13, 0x14,  # voice 3
    0x15, 0x16, 0x17, 0x18,              # filter cutoff lo/hi, res+enable, mode/vol
    0x04, 0x0B, 0x12,                    # voice 1/2/3 control — first write
    0x04, 0x0B, 0x12,                    # voice 1/2/3 control — second write
)
# fmt: on

# Voice control-register ID ranges within _ASID_REG_TO_OFFSET.
_CTRL_FIRST_BASE = 22  # ids 22,23,24 → voice 0,1,2 (first write)
_CTRL_SECOND_BASE = 25  # ids 25,26,27 → voice 0,1,2 (second write)


@dataclass
class AsidUpdate:
    """The result of decoding one ASID SysEx message.

    Empty/None fields mean "no change from this message". ``regs`` maps a SID
    register offset (0x00-0x18, i.e. ``addr - $D400``) to its new 8-bit value,
    so it doubles as an index into the scene's 25-byte register shadow.
    """

    command: int
    regs: dict[int, int] = field(default_factory=dict)
    # voice_idx → first control-register value, only when a differing second
    # write follows in the same frame (hard restart). Drives the two-phase emit.
    control_first: dict[int, int] = field(default_factory=dict)
    text: str | None = None  # 0x4F display characters
    playing: bool | None = None  # 0x4C start (True) / 0x4D stop (False)
    system: str | None = None  # "PAL" | "NTSC" (0x31)
    speed_multiplier: int | None = None  # 1..16 (0x31)
    frame_delta_us: int | None = None  # 0x31, 0 if unspecified
    chip_type: str | None = None  # "6581" | "8580" for chip `chip_index` (0x32)
    chip_index: int = 0  # SID chip this update targets: 0 = 0x4E, k = 0x50+(k-1); also 0x32
    dropped: bool = False  # command recognized but not applied (OPL/timing)


def decode(data: Sequence[int]) -> AsidUpdate | None:
    """Decode one ASID SysEx payload (``msg.data`` from mido, i.e. the bytes
    between ``F0`` and ``F7``, starting with the ``0x2D`` manufacturer id).

    Returns an :class:`AsidUpdate`, or ``None`` if this isn't an ASID message
    (wrong/absent manufacturer id) so the caller can ignore foreign SysEx.
    """
    if len(data) < 2 or data[0] != ASID_MANUFACTURER_ID:
        return None
    cmd = data[1]
    payload = data[2:]
    if cmd == CMD_REG:
        return _decode_registers(payload, chip_index=0)
    if CMD_MULTI_SID_LO <= cmd <= CMD_MULTI_SID_HI:
        # 0x50 = SID2 (chip 1) .. 0x5F = SID17 (chip 16). Same packed format
        # as 0x4E, just targeting a higher chip index.
        return _decode_registers(payload, chip_index=cmd - CMD_MULTI_SID_LO + 1)
    if cmd == CMD_START:
        return AsidUpdate(command=cmd, playing=True)
    if cmd == CMD_STOP:
        return AsidUpdate(command=cmd, playing=False)
    if cmd == CMD_CHARS:
        return AsidUpdate(command=cmd, text=_decode_chars(payload))
    if cmd == CMD_SPEED:
        return _decode_speed(payload)
    if cmd == CMD_SID_TYPE:
        return _decode_sid_type(payload)
    # Recognized-but-unsupported (OPL-FM, timing) and anything unknown: flag as
    # dropped so the scene can warn once and move on.
    return AsidUpdate(command=cmd, dropped=True)


def _iter_masked(mask4: Sequence[int], msb4: Sequence[int]) -> Iterator[tuple[int, int]]:
    """Yield ``(register_id, msb_bit)`` for each register the mask marks present,
    in ascending register-id order. Mask byte *i* bit *b* covers register id
    ``i*7 + b`` (7 registers per byte, bit 7 unused per MIDI's 7-bit rule); the
    MSB byte at the same position carries that register's 8th data bit."""
    for byte_idx in range(4):
        mbyte = mask4[byte_idx]
        sbyte = msb4[byte_idx]
        for bit in range(7):
            if mbyte & (1 << bit):
                yield byte_idx * 7 + bit, (sbyte >> bit) & 1


def _decode_registers(payload: Sequence[int], *, chip_index: int) -> AsidUpdate:
    update = AsidUpdate(command=CMD_REG, chip_index=chip_index)
    if len(payload) < 8:
        return update  # malformed / empty — no registers to apply
    mask4 = payload[0:4]
    msb4 = payload[4:8]
    reg_data = payload[8:]
    first_ctrl: dict[int, int] = {}
    second_ctrl: dict[int, int] = {}
    # register_data holds one byte per present register, in ascending id order —
    # so the mask-iteration index is the index into it.
    for di, (reg_id, msb) in enumerate(_iter_masked(mask4, msb4)):
        if di >= len(reg_data) or reg_id >= len(_ASID_REG_TO_OFFSET):
            break  # truncated stream — stop consuming
        value = (reg_data[di] & 0x7F) | (msb << 7)
        # Later ids overwrite earlier in the dict, so a control reg's second
        # write (ids 25-27) naturally wins as the final block-write value.
        update.regs[_ASID_REG_TO_OFFSET[reg_id]] = value
        if _CTRL_FIRST_BASE <= reg_id < _CTRL_SECOND_BASE:
            first_ctrl[reg_id - _CTRL_FIRST_BASE] = value
        elif _CTRL_SECOND_BASE <= reg_id < _CTRL_SECOND_BASE + 3:
            second_ctrl[reg_id - _CTRL_SECOND_BASE] = value
    # Hard restart: a voice with both a first and a *differing* second control
    # write in this frame. The first value (gate off / test) must reach the
    # chip before the final one, so surface it for the two-phase emit.
    for voice, fval in first_ctrl.items():
        sval = second_ctrl.get(voice)
        if sval is not None and sval != fval:
            update.control_first[voice] = fval
    return update


def _decode_chars(payload: Sequence[int]) -> str:
    """0x4F display characters → a sanitized ASCII string (non-printable → space)."""
    return "".join(chr(b & 0x7F) if 0x20 <= (b & 0x7F) < 0x7F else " " for b in payload).rstrip()


def _decode_speed(payload: Sequence[int]) -> AsidUpdate:
    update = AsidUpdate(command=CMD_SPEED)
    if not payload:
        return update
    data0 = payload[0]
    update.system = "NTSC" if (data0 & 0x01) else "PAL"
    update.speed_multiplier = ((data0 >> 1) & 0x0F) + 1  # bits 1-4 → 1..16
    if len(payload) >= 4:
        update.frame_delta_us = (
            (payload[1] & 0x7F) | ((payload[2] & 0x7F) << 7) | ((payload[3] & 0x03) << 14)
        )
    return update


def _decode_sid_type(payload: Sequence[int]) -> AsidUpdate:
    update = AsidUpdate(command=CMD_SID_TYPE)
    if len(payload) >= 2:
        # data0 = chip index (0 = SID1), data1 bit0 = 0:6581 / 1:8580.
        update.chip_index = payload[0]
        update.chip_type = "8580" if (payload[1] & 0x01) else "6581"
    return update
