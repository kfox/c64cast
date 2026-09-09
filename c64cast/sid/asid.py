"""ASID protocol decoder — ASID MIDI SysEx payloads → SID register updates.

The ASID protocol (spec: https://github.com/thomasj/asid-protocol) streams SID register
writes frame-by-frame over MIDI SysEx; the receiving unit's SID chip
synthesizes the sound. Each message is ``F0 2D <cmd> <payload...> F7``; mido
hands us the bytes between ``F0`` and ``F7`` as ``msg.data``, i.e.
``(0x2D, cmd, *payload)``.

This module is a **pure** decoder — no mido, no hardware, and a decode's result
depends on nothing but its bytes — so it's trivially unit-testable: feed a byte
sequence, assert the resulting register map. (The one piece of module state,
``_overlong_recipe_log``, gates *how often* the over-cap warning is logged and
never what a decode returns; :mod:`c64cast._wire_log` says why it has to
exist.) The
:class:`~c64cast.sid.asid_scene.AsidScene` owns the MIDI port, the register shadow,
the DMA writes, and the oscilloscope.

Honored: ``0x4E`` register data (the workhorse — SID chip 0), the multi-SID
streams ``0x50``-``0x5F`` (SID2..SID17, same packed format → chips 1..16),
``0x4C``/``0x4D`` start/stop, ``0x4F`` character display, ``0x31`` speed
(PAL/NTSC + multiplier + buffering bit), ``0x32`` SID type (per chip), and the
``0x30`` timing recipe (per-register write order + inter-write wait cycles,
decoded into :attr:`AsidUpdate.timing_recipe`). OPL-FM (``0x60``) is recognized
but dropped (no OPL), as is **any unrecognized command byte** — the catch-all,
not just ``0x60``, is what a reader of this untrusted-input decoder most needs
to know. Every register/type update carries a ``chip_index`` so the
scene can route it to the matching SID address (see :mod:`c64cast.sid.asid_sidmap`
for the U64 address map). See docs/architecture.md for the rationale.

The ``0x30`` recipe used to be dropped: the coalesced flush path applies the
whole register image at once, so the plain write order sufficed. The buffered
C64-side ring player (see :mod:`c64cast.sid.asid_player`) *does* honor it — it
replays each frame's writes on the real SID in the recipe's order with the
recipe's inter-write waits — so the decoder now surfaces it.

The ASID ``0x4E`` payload orders the three voice control registers last
(register IDs 22-27) so a frame can carry a *second* write to each control
register — the gate-off→gate-on "hard restart" trick players use to re-attack
an already-gated voice. We surface the first control value in
:attr:`AsidUpdate.control_first` so the scene can emit it before the coalesced
block write (which lands the second/final value), preserving the pulse a real
SID needs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from c64cast._wire_log import LogThrottle

log = logging.getLogger("c64cast.sid.asid")

# The one warning this decoder can be made to emit is chosen by the sender, so
# it is throttled rather than logged per message — the smallest message that
# trips it is 62 bytes. Module-level because the decoder is a free function with
# no per-stream object to hang the gate off; see :mod:`c64cast._wire_log`.
_overlong_recipe_log = LogThrottle(log)

# SysEx manufacturer id chosen by Elektron for ASID (45 = 0x2D).
ASID_MANUFACTURER_ID = 0x2D

# Commands (see spec/protocol.md).
CMD_TIMING = 0x30  # SID write order & inter-write wait recipe
CMD_SPEED = 0x31  # PAL/NTSC + speed multiplier + frame delta + buffering bit
CMD_SID_TYPE = 0x32  # 6581 / 8580
CMD_START = 0x4C  # start playback
CMD_STOP = 0x4D  # stop playback
CMD_REG = 0x4E  # SID register data (the workhorse)
CMD_CHARS = 0x4F  # display characters
CMD_MULTI_SID_LO = 0x50  # SID2 .. SID17 register data (chips 1..16)
CMD_MULTI_SID_HI = 0x5F
CMD_OPL = 0x60  # OPL-FM register data (dropped)

# The highest chip index the protocol can name: 0x5F = SID17 = chip 16.
MAX_CHIP_INDEX = CMD_MULTI_SID_HI - CMD_MULTI_SID_LO + 1

# The 0x30 recipe is a SID write *order*, so it can be no longer than the
# register table and can name each ASID register id at most once. Neither bound
# is on the wire: SysEx has no length limit on a virtual/network MIDI port, and
# the recipe persists until the next 0x30 — so an uncapped decode lets one
# message amplify every later frame (measured: a 400 KB message decodes to
# 200,000 entries, and an ordinary 4-register frame then serializes to 200,004
# ops on the MIDI reader thread). Both bounds are load-bearing: a fully
# spec-legal 28-pair recipe that repeats one id still serializes past
# ``asid_player.MAX_OPS_PER_CHIP``, which silently truncates other chips away.
MAX_TIMING_RECIPE_PAIRS = 28

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
    frame_delta_us: int | None = None  # 0x31, None when the payload carries no delta
    buffering_requested: bool | None = None  # 0x31 data0 bit 6 (host asks the client to buffer)
    chip_type: str | None = None  # "6581" | "8580" for chip `chip_index` (0x32)
    chip_index: int = 0  # SID chip this update targets: 0 = 0x4E, k = 0x50+(k-1); also 0x32
    # 0x30 write-order/wait recipe: ordered (asid_reg_id, wait_cycles) pairs, one
    # per write-order position. wait_cycles (0..255) is the C64-cycle delay to
    # apply AFTER that register's write. Empty list = no recipe carried. At most
    # MAX_TIMING_RECIPE_PAIRS entries, each register id appearing at most once.
    timing_recipe: list[tuple[int, int]] = field(default_factory=list)
    dropped: bool = False  # not applied: OPL-FM, or an unrecognized command byte


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
    if cmd == CMD_TIMING:
        return _decode_timing(payload)
    # Recognized-but-unsupported (OPL-FM) and anything unknown: flag as dropped
    # so the scene can warn once and move on.
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
    update.buffering_requested = bool(data0 & 0x40)  # bit 6
    if len(payload) >= 4:
        update.frame_delta_us = (
            (payload[1] & 0x7F) | ((payload[2] & 0x7F) << 7) | ((payload[3] & 0x03) << 14)
        )
    return update


def _decode_timing(payload: Sequence[int]) -> AsidUpdate:
    """Decode a 0x30 recipe into ordered ``(asid_reg_id, wait_cycles)`` pairs.

    The payload is up to :data:`MAX_TIMING_RECIPE_PAIRS` two-byte pairs; pair *i*
    gives the ASID register id to write at write-order position *i* (``data0``
    bits 0-5) and the cycle delay to apply after it (``data0`` bit 6 = wait bit
    7, ``data1`` bits 0-6 = wait bits 0-6 → 0..255). Truncated/odd-length
    payloads decode the whole pairs present and stop (the caller falls back to
    the default order for the rest).

    Pairs past the cap are dropped with a throttled warning (the sender picks
    how often this fires, so the report is O(1) per stream — see
    :mod:`c64cast._wire_log`), and a register id repeated in the order keeps
    its first position — see :data:`MAX_TIMING_RECIPE_PAIRS` for why both bounds
    have to be enforced here, at the wire boundary."""
    update = AsidUpdate(command=CMD_TIMING)
    pairs = len(payload) // 2
    if pairs > MAX_TIMING_RECIPE_PAIRS:
        _overlong_recipe_log.warn(
            "asid: 0x30 timing recipe carries %d pairs; keeping the first %d "
            "(a SID write order can be no longer than the register table)",
            pairs,
            MAX_TIMING_RECIPE_PAIRS,
        )
    seen: set[int] = set()
    for i in range(0, min(len(payload), 2 * MAX_TIMING_RECIPE_PAIRS) - 1, 2):
        data0 = payload[i]
        data1 = payload[i + 1]
        reg_id = data0 & 0x3F
        if reg_id in seen:
            continue  # a write order names each register once; first wins
        seen.add(reg_id)
        wait = (((data0 >> 6) & 0x01) << 7) | (data1 & 0x7F)
        update.timing_recipe.append((reg_id, wait))
    return update


def _decode_sid_type(payload: Sequence[int]) -> AsidUpdate:
    update = AsidUpdate(command=CMD_SID_TYPE)
    if len(payload) >= 2:
        # data0 = chip index (0 = SID1), data1 bit0 = 0:6581 / 1:8580. mido hands
        # up a full 0..127 data byte; clamp it to the range the multi-SID
        # commands can produce so no consumer is handed an index this decoder
        # would never otherwise emit.
        update.chip_index = min(payload[0], MAX_CHIP_INDEX)
        update.chip_type = "8580" if (payload[1] & 0x01) else "6581"
    return update
