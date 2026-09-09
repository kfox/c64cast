"""Buffered C64-side ASID ring player — cycle-accurate high-multispeed playback.

:class:`~c64cast.sid.asid_scene.AsidScene`'s default path coalesces incoming ASID
register frames into per-chip shadows and flushes one block write per chip at
≤60 Hz (host-driven socket DMA). That drops intermediate frames on multispeed
tunes (``0x31`` up to 16×) — arpeggios, fast vibrato, and gate-off→gate-on hard
restarts get mangled — and every flush is a bus-halting, wall-clock-jittered DMA
burst.

This module moves frame *consumption* onto the C64. The host serializes each
ASID frame into a compact fixed-size **slot** and REUWRITEs it (bus-clean, no
6510 halt) into a ring in REU SDRAM, ahead of a computed read head. A small 6502
player fired by CIA #1 Timer A at the ASID frame cadence pops one slot per tick
and applies its register writes to the SID(s) — honoring the ``0x30`` recipe's
write order + inter-write waits — decoupled from host-DMA jitter, no frames
dropped. It is the **producer-ahead-of-computed-read-head** pattern proven by
:mod:`c64cast.audio.sampler` (open-loop: the C64 crystal is exact, so the read head is
computed from wall-clock, never read back — no servo, no C64→host reads), with
an IRQ-driven ring consumer modeled on the REU audio pump in :mod:`c64cast.audio.audio`.

**U64-only** — it needs bus-clean ``reu_write`` (``profile.supports_reu``). On
TeensyROM / any no-REU backend :class:`AsidScene` keeps the coalesced path (and
never blanks the TR display). Because :class:`AsidScene` runs no ``$D418`` DAC /
NMI, the whole ``$C000`` RAM page and the REU are free for the player.

Two halves, mirroring :mod:`c64cast.audio.sampler`:

* **Pure builders** (unit-testable, no hardware): :func:`serialize_frame` /
  :func:`pack_slot` (the wire format), :func:`slot_size_for_chips`, and
  :func:`build_player` (the 6502 handler blob).
* :class:`AsidRingPlayer` — the scene-facing producer: a writer thread + REU
  ring, open-loop.

Wire format — each frame is one fixed-size slot (zero-padded), so DMA length +
stride + wrap are trivial::

    [n_ops]                 1 byte   (0 = "hold" tick: no writes, SID holds state)
    op × n_ops, each 4 bytes:
        [addr_lo][addr_hi]           absolute SID register address (baked host-side
                                     from the chip's $Dxxx base — the 6502 is
                                     chip-agnostic; multi-SID needs no base table)
        [value]                      register value
        [wait]                  0..255  delay units applied AFTER the write
                                        (one unit ≈ DELAY_CYCLES_PER_UNIT cycles)

A hard restart is two control-register ops (first value + a small wait, then the
final value). A multi-SID frame concatenates every active chip's ops into one
slot. ``SLOT_SIZE`` derives from the active chip count and the player is re-init'd
when it changes (piggybacks :meth:`AsidScene._reconfigure_chips`).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING

from c64cast._pollthread import PollThread
from c64cast._wire_log import LogThrottle
from c64cast.hw.c64 import (
    CIA1,
    KERNAL,
    REU,
    VECTORS,
    actual_rate_for_latch,
    cia1_latch_for_rate,
    cpu_clock,
    kernal_cia1_latch,
)

from .asid import _ASID_REG_TO_OFFSET

if TYPE_CHECKING:
    from c64cast.hw.backend import C64Backend

log = logging.getLogger("c64cast.sid.asid_player")

# `pack_slot` truncates on wire-driven input at the ASID frame rate (60-960 Hz),
# and there is a reachable state in which the condition is permanent rather than
# occasional, so its report is throttled instead of logged per frame. Module
# level because `pack_slot` is a free function; see :mod:`c64cast._wire_log`.
_truncated_slot_log = LogThrottle(log)

# --------------------------------------------------------------------------
# Memory map. AsidScene runs no DAC/NMI/pump, so $C000-$CFFF and the REU are
# entirely free. The literal addresses and their disjointness are pinned by
# MemoryMapTest in tests/test_asid_player.py — the symbolic assertions elsewhere
# move with the constant, so relocating HANDLER_ADDR onto LANDING_BUF (a handler
# the REU pull overwrites every tick) used to leave the whole suite green.
# --------------------------------------------------------------------------
HANDLER_ADDR = 0xC000  # player IRQ handler + inline delay subroutine
LANDING_BUF = 0xC400  # REU→RAM pull target (page-aligned; up to ~1 KB slot)
TRACKER_ADDR = 0xC800  # REU src tracker: LO/MI/HI at $C800-$C802
TICK_COUNTER_ADDR = 0xC803  # kernal-chain tick divider counter
NOPS_COUNTER_ADDR = 0xC804  # per-IRQ op-loop counter
# Zero-page indirect pointer used by the op-execute loop ($FB-$FE is the
# documented free-for-user ZP; the BASIC clear-loop + kernal IRQ tail don't
# touch it).
ZP_PTR = 0xFB

# REU ring offset. Clear of the $D418-DAC mic ring ($100000), the sampler ring
# ($200000), and the REU-staged video region ($E00000).
RING_BASE = 0x300000
# Number of fixed-size slots the ring holds. The ring is a jitter buffer, not
# latency (latency is the lead target below). 512 slots is ~8.5 s at single
# speed / ~0.5 s at a 960 Hz 16× multispeed — ample, and even at the 8-SID
# worst case (~912 B/slot) it is under 0.5 MB of REU, far below the video region.
RING_SLOTS = 512

# Op-execute delay: the on-C64 busy-wait loop costs ~5 cycles per unit (DEY 2 +
# BNE 3). serialize_frame converts a 0x30 recipe's wait_cycles into units by
# dividing by this. Coarse (±a few cycles), documented, far better than
# dropped/instant — see docs/caveats.md.
DELAY_CYCLES_PER_UNIT = 5
# Default wait between a voice's two hard-restart control writes when no 0x30
# recipe is carried — enough for the gate-off write to land before the gate-on
# (mirrors the coalesced path's two-phase emit). In delay units.
DEFAULT_HARD_RESTART_WAIT_UNITS = 2

# What one op costs the 6510 beyond its wait, counted off build_player's
# `oploop`: 40 cycles to unpack the op and store the value, 3 to branch past the
# delay, 13 to advance the slot pointer, 9 for the op counter and loop branch.
# An op that carries a nonzero wait pays WAITED_OP_EXTRA_CYCLES more (the BEQ
# falls through into JSR/TAY/RTS around the delay loop) plus
# DELAY_CYCLES_PER_UNIT per unit.
PER_OP_CYCLES = 65
WAITED_OP_EXTRA_CYCLES = 13

# The share of one consume period a slot's ops may spend. The rest is the
# handler's own overhead — the REU pull, the tracker advance + wrap test, the
# kernal IRQ entry/exit, and the $EA31 chain every Nth tick — and it is a coarse
# reserve, not a model: the point is to keep the 6510 out of the ASID handler,
# not to predict the last cycle. Without a budget the wait column is a wire-
# supplied amplifier: 28 ops each carrying the maximum 255-cycle wait cost 9324
# cycles per chip, over half a 60 Hz NTSC frame for ONE chip, and the read head
# is open-loop — the host keeps writing at the requested rate while the C64
# consumes at whatever it can manage, with no way to resynchronize.
FRAME_BUDGET_FRACTION = 0.8

# The most write-ops one chip's frame can carry: 22 non-control registers +
# 3 voices × 2 control writes (hard restart). Sets the slot size.
MAX_OPS_PER_CHIP = 28
OP_BYTES = 4  # addr_lo, addr_hi, value, wait
_SLOT_ALIGN = 16  # round slot size up to this so single-SID → 128 (plan pins it)

# Producer buffering depth / watermarks (in slots). Unlike the FPGA sampler
# (fed by a demuxer that races ahead of real time, so it can grow its lead), an
# ASID host streams in real time at exactly the consume cadence — the ring can
# never build lead beyond the startup prebuffer. So seed the prebuffer CLOSE to
# the lead target: that cushion is all the jitter headroom there is before a
# genuine producer stall pads a hold (SID holds its last state — no echo).
DEFAULT_LEAD_SLOTS_SECONDS = 0.30  # keep the write head this far ahead of read
DEFAULT_PREBUFFER_SECONDS = 0.30  # seed the full lead before arming (max cushion)
_QUEUE_MAX_SLOTS = 4096
# How long teardown waits for the arm lock before restoring the C64 anyway. The
# lock is only ever held across a bounded run of DMA writes, so overshooting it
# means the link is wedged — and a wedged link is exactly when the kernal
# restore matters most.
_TEARDOWN_LOCK_TIMEOUT_S = 2.0
# Cap one reu_write burst, so a 256-slot prebuffer or catch-up run at the 8-SID
# slot size doesn't become a quarter-megabyte single transfer. Mirrors the same
# cap in _prefill_holds. Well above the ~2.4 KB below which payload is free on
# the U64 DMA link (see CLAUDE.md), so widening a run up to here costs nothing.
_MAX_DMA_BURST_BYTES = 32 * 1024

# The band a wire-supplied 0x31 may steer the consume rate into.
#
# Ceiling: the ASID spec's speed multiplier is 4 bits against the video frame
# rate, so 16 x 60 Hz = 960 Hz is the fastest cadence the protocol can ask for;
# 1000 Hz leaves headroom for a host deriving the same 16x from frame_delta_us.
# Past it the numbers stop meaning anything: frame_delta_us = 1 asks for 1 MHz,
# and because cia1_latch_for_rate clamps the *latch* rather than the rate, that
# lands on latch 1 — a CIA IRQ every 2 cycles into a handler that needs hundreds,
# so the 6510 never leaves it (jiffy clock, SCNKEY and the kernal tail dead until
# a power cycle) while the read head advances ~511,000 slots/s and the writer
# floods the shared single-connection DMA socket forever (measured: 767
# reu_write/s against the ~200/s ceiling CLAUDE.md documents, starving the video
# render path that shares the socket).
#
# Floor: CIA #1 Timer A is a 16-bit down-counter, so the slowest cadence the
# hardware can realize is cpu_clock / 65536 — 15.6 Hz on NTSC, 15.0 Hz on PAL.
# Take the lower of the two: below it the latch saturates and the request means
# nothing. A malformed rate (NaN, or a delta the decoder aliased) clamps to the
# floor rather than the ceiling — slow is the safe direction here.
MAX_FRAME_RATE_HZ = 1000.0
MIN_FRAME_RATE_HZ = 15.0

# SID control-register offsets (voice 0/1/2), i.e. the ASID ids 22-27 targets.
_CONTROL_OFFSETS = (0x04, 0x0B, 0x12)
# Reverse of _ASID_REG_TO_OFFSET for the non-control ids (0-21): SID offset →
# ASID register id, so recipe ordering can key on either. Control offsets are
# handled separately (their ids split into first/second write).
_OFFSET_TO_NONCTRL_ID: dict[int, int] = {_ASID_REG_TO_OFFSET[rid]: rid for rid in range(22)}


# --------------------------------------------------------------------------
# Pure wire-format builders.
# --------------------------------------------------------------------------
def slot_size_for_chips(n_chips: int) -> int:
    """Fixed slot size (bytes) for a frame carrying ``n_chips`` chips' ops.

    ``1 + n_chips × MAX_OPS_PER_CHIP × OP_BYTES`` rounded up to _SLOT_ALIGN.
    Single-SID → 128; the 8-SID worst case → 912."""
    n = max(1, n_chips)
    raw = 1 + n * MAX_OPS_PER_CHIP * OP_BYTES
    return ((raw + _SLOT_ALIGN - 1) // _SLOT_ALIGN) * _SLOT_ALIGN


def _wait_units_for_cycles(wait_cycles: int) -> int:
    """Convert a 0x30 recipe wait (C64 cycles) to on-C64 delay-loop units."""
    if wait_cycles <= 0:
        return 0
    return min(255, round(wait_cycles / DELAY_CYCLES_PER_UNIT))


def _offset_for_recipe_id(recipe_id: int) -> int | None:
    """The SID register offset a wire-supplied ``0x30`` recipe id names, or None
    for an id the register table has no entry for.

    Recipe ids arrive as ``data0 & 0x3F`` (0-63) from an untrusted stream, while
    the table holds 28 entries — so the out-of-table half has to answer "no
    register", not raise."""
    if 0 <= recipe_id < len(_ASID_REG_TO_OFFSET):
        return _ASID_REG_TO_OFFSET[recipe_id]
    return None


def serialize_frame(
    regs: dict[int, int],
    control_first: dict[int, int],
    base_addr: int,
    recipe: list[tuple[int, int]] | None = None,
) -> list[tuple[int, int, int]]:
    """Serialize one chip's frame into ``(abs_addr, value, wait_units)`` ops.

    ``regs`` maps SID register offset (0x00-0x18) → value for the registers this
    frame writes (as decoded by :func:`c64cast.sid.asid.decode`); the control regs'
    *final* value wins there. ``control_first`` maps voice → the differing first
    control write (a gate-off→gate-on hard restart); those voices emit two
    control ops (first, then final). ``base_addr`` is the chip's ``$Dxxx`` base,
    baked into every op's absolute address so the 6502 player stays chip-agnostic.

    Ordering: without a ``recipe`` (the common case), non-control registers first
    (ascending offset), then per voice its control write(s) — a hard restart's
    first write carries a small default wait before the final. With a ``0x30``
    recipe, the writes are ordered by the recipe's register sequence and each op
    takes the wait the recipe gave *its own* register id (recipe ids absent from
    the frame are skipped; any frame register the recipe omits is appended in
    default order).

    **A recipe can reorder registers against each other, never the two writes
    within one.** A voice's hard restart is a gate-off then a gate-on write to
    one register, and emitting them the other way round leaves the voice silent
    for the whole frame — so the pair is positioned as a unit and its internal
    order is the serializer's property, not a property of a well-formed
    recipe."""
    # Build the set of writes as (asid_reg_id, offset, value, default_wait_units).
    # A voice with a hard restart contributes two writes (ids 22-24 then 25-27).
    writes: list[tuple[int, int, int, int]] = []
    for offset in sorted(regs):
        if offset in _CONTROL_OFFSETS:
            continue
        rid = _OFFSET_TO_NONCTRL_ID.get(offset)
        if rid is None:
            continue
        writes.append((rid, offset, regs[offset] & 0xFF, 0))
    for voice, offset in enumerate(_CONTROL_OFFSETS):
        final = regs.get(offset)
        first = control_first.get(voice)
        if first is not None:
            # Hard restart: gate-off/first write, wait, then the final write.
            writes.append((22 + voice, offset, first & 0xFF, DEFAULT_HARD_RESTART_WAIT_UNITS))
            if final is not None:
                writes.append((25 + voice, offset, final & 0xFF, 0))
        elif final is not None:
            writes.append((25 + voice, offset, final & 0xFF, 0))

    if recipe:
        # The recipe positions *registers*, and the unit it can position is the
        # SID register offset — not the ASID register id. A voice's hard restart
        # is two writes to one offset under two ids (22+v gate-off, then 25+v
        # gate-on), and they mean the opposite thing in the opposite order, so
        # both travel together to the first position naming either of them.
        # Grouping by id instead let a recipe that named 25+v but not 22+v emit
        # the gate-on value at that position and the gate-off value in the tail
        # append below: the voice ended the frame gated OFF where the tune asked
        # for a re-attack, and ids 25-27 are the ordinary control ids, so that
        # was the default outcome for such a stream, not an exotic one.
        #
        # Waits are looked up per write id rather than taken from the recipe
        # entry that positioned the group, so a recipe naming both of a pair's
        # ids keeps both of its waits.
        by_offset: dict[int, list[tuple[int, int, int, int]]] = {}
        for w in writes:
            by_offset.setdefault(w[1], []).append(w)
        wait_units_by_id: dict[int, int] = {}
        for rid, wait_cycles in recipe:
            wait_units_by_id.setdefault(rid, _wait_units_for_cycles(wait_cycles))
        ordered: list[tuple[int, int, int]] = []
        seen: set[int] = set()
        for rid, _wait_cycles in recipe:
            # A register named twice in the write order — directly, or once per
            # control id of the same voice — writes once, at its first position.
            # Without this, op count tracks recipe length rather than the frame's
            # write count, and a spec-legal 28-pair recipe naming one id can push
            # a single chip past MAX_OPS_PER_CHIP — which pack_slot then
            # truncates, taking later chips with it.
            offset = _offset_for_recipe_id(rid)
            if offset is None or offset in seen:
                continue
            seen.add(offset)
            for w_rid, w_offset, value, default_wait in by_offset.get(offset, ()):
                ordered.append(
                    (base_addr + w_offset, value, wait_units_by_id.get(w_rid, default_wait))
                )
        ordered.extend(
            (base_addr + offset, value, dw)
            for (_rid, offset, value, dw) in writes
            if offset not in seen
        )
        return ordered

    return [(base_addr + offset, value, dw) for (_rid, offset, value, dw) in writes]


def pack_slot(ops: list[tuple[int, int, int]], slot_size: int) -> bytes:
    """Pack concatenated ops (all active chips) into one fixed-size slot.

    ``[n_ops]`` then 4 bytes per op ``[addr_lo, addr_hi, value, wait]``,
    zero-padded to ``slot_size``. ``n_ops`` fits one byte (multi-SID tops out at
    8 × 28 = 224 ops < 256).

    Ops beyond what fits are dropped, and that is **loud**: ``slot_size`` is
    derived from the chip count on the assumption that 28 ops per chip is a hard
    ceiling, so a truncation means something upstream broke it. It used to be
    silent, which is how an over-long ``0x30`` recipe deleted a whole chip's
    frame (every op past the cut belongs to the later chips in the slot).

    Loud, but throttled: this runs once per ASID frame (60-960 Hz) on the MIDI
    reader thread, and the mismatch that trips it can persist for a whole scene,
    so the report is O(1) per stream — :mod:`c64cast._wire_log`."""
    max_ops = (slot_size - 1) // OP_BYTES
    if len(ops) > max_ops:
        _truncated_slot_log.warn(
            "asid_player: frame carries %d ops but the %d B slot holds %d; "
            "dropping %d — later chips in this slot lose their writes",
            len(ops),
            slot_size,
            max_ops,
            len(ops) - max_ops,
        )
        ops = ops[:max_ops]
    out = bytearray(slot_size)
    out[0] = len(ops) & 0xFF
    i = 1
    for addr, value, wait in ops:
        out[i] = addr & 0xFF
        out[i + 1] = (addr >> 8) & 0xFF
        out[i + 2] = value & 0xFF
        out[i + 3] = wait & 0xFF
        i += OP_BYTES
    return bytes(out)


def frame_cycle_cost(ops: list[tuple[int, int, int]]) -> int:
    """C64 cycles one frame's ops cost the 6510 inside the player's IRQ."""
    total = len(ops) * PER_OP_CYCLES
    for _addr, _value, wait in ops:
        if wait:
            total += WAITED_OP_EXTRA_CYCLES + wait * DELAY_CYCLES_PER_UNIT
    return total


def fit_frame_to_budget(
    ops: list[tuple[int, int, int]], budget_cycles: int
) -> list[tuple[int, int, int]]:
    """Scale a frame's inter-write waits down until it fits ``budget_cycles``.

    A frame longer than the consume period does not queue politely: the CIA
    fires again before the handler returns, so the 6510 never leaves it — the
    jiffy clock, ``SCNKEY`` and the whole kernal tail stop, and the open-loop
    read head keeps advancing against slots nobody applied. The ``0x30`` recipe
    supplies the waits from the wire, so they are the part that must give.

    Waits scale proportionally, to zero if that is what it takes — a hard
    restart's two writes still reach the SID in order, just an op apart instead
    of a programmed gap. The op cost itself is the frame's content (already
    bounded at ``MAX_OPS_PER_CHIP`` per chip) and is never dropped: a frame
    whose ops alone overrun the period comes back with every wait at zero, and
    the caller says so — deleting register writes would mangle the tune worse
    than a slow kernal chain, which teardown undoes anyway."""
    op_cycles = sum(PER_OP_CYCLES + (WAITED_OP_EXTRA_CYCLES if w else 0) for *_, w in ops)
    wait_cycles = sum(w for *_, w in ops) * DELAY_CYCLES_PER_UNIT
    if op_cycles + wait_cycles <= budget_cycles:
        return list(ops)
    allowance = budget_cycles - op_cycles
    if allowance <= 0 or wait_cycles <= 0:
        return [(addr, value, 0) for addr, value, _wait in ops]
    scale = allowance / wait_cycles
    return [(addr, value, int(wait * scale)) for addr, value, wait in ops]


def hold_slot(slot_size: int) -> bytes:
    """A "hold" slot (``n_ops == 0``): the player applies no writes this tick and
    the SID holds its last state — the graceful producer-underrun pad (no echo)."""
    return bytes(slot_size)


# --------------------------------------------------------------------------
# 6502 handler builder — a tiny label-based assembler keeps the many relative
# branches correct. The mnemonics live in comments, so BuildPlayerTest pins the
# exact output bytes against a committed golden blob: without it, flipping the
# op loop's `STA $0000` (0x8D) to `STX` (0x8E) — every SID write storing X
# instead of the value, i.e. total silence — left all 110 ASID tests green.
# --------------------------------------------------------------------------
class _Asm:
    """Minimal 6502 assembler: emit bytes, mark labels, resolve rel/abs refs."""

    def __init__(self, origin: int) -> None:
        self.origin = origin
        self.buf = bytearray()
        self._labels: dict[str, int] = {}
        self._rel: list[tuple[int, str]] = []  # (operand_pos, label)
        self._abs: list[tuple[int, str, int]] = []  # (operand_pos, label, addend)

    def label(self, name: str) -> None:
        self._labels[name] = self.origin + len(self.buf)

    def emit(self, *b: int) -> None:
        self.buf.extend(v & 0xFF for v in b)

    def branch(self, opcode: int, label: str) -> None:
        """A relative branch (opcode + signed 8-bit displacement to ``label``)."""
        self.emit(opcode, 0x00)
        self._rel.append((len(self.buf) - 1, label))

    def jsr(self, label: str) -> None:
        self.emit(0x20, 0x00, 0x00)
        self._abs.append((len(self.buf) - 2, label, 0))

    def sta_abs_label(self, label: str, addend: int) -> None:
        """STA <label + addend> (absolute) — used to self-modify an operand."""
        self.emit(0x8D, 0x00, 0x00)
        self._abs.append((len(self.buf) - 2, label, addend))

    def symbols(self) -> dict[str, int]:
        """Every label's absolute address. The cost model is derived from the
        emitted bytes, and a byte range is only findable by its labels."""
        return dict(self._labels)

    def resolve(self) -> bytes:
        for pos, label in self._rel:
            target = self._labels[label]
            src = self.origin + pos + 1  # address of the byte after the operand
            disp = target - src
            if not -128 <= disp <= 127:
                raise ValueError(f"branch to {label} out of range ({disp})")
            self.buf[pos] = disp & 0xFF
        for pos, label, addend in self._abs:
            addr = self._labels[label] + addend
            self.buf[pos] = addr & 0xFF
            self.buf[pos + 1] = (addr >> 8) & 0xFF
        return bytes(self.buf)


def build_player_symbols(
    slot_size: int, tick_divider: int, *, ring_base: int = RING_BASE
) -> tuple[bytes, dict[str, int]]:
    """Assemble the CIA #1 Timer A IRQ player for a given slot size.

    Per IRQ: pull the next slot REU→landing-buffer (reload REU src from the
    main-RAM tracker, dst = landing buffer, len = ``slot_size``, trigger fetch;
    advance the tracker by ``slot_size`` and wrap at the ring end — never trust
    the REU read-back, exactly like the tracked audio pump), then execute the
    slot's ops (self-modify a ``STA`` target, write value, busy-wait ``wait``
    units), then chain ``$EA31`` every ``tick_divider``-th tick (keeping SCNKEY /
    jiffy ~60 Hz) and lean-exit the rest. A/X/Y are freely clobbered — the kernal
    ROM IRQ entry ($FF48) already saved them and the tail restores them.

    ``tick_divider`` must be 1..255 — it becomes an ``LDA #N`` immediate, and
    silently masking it to 8 bits is how a divider of 333 became 77 and a
    multiple of 256 became "chain once every 256 ticks".

    Returns the blob and its label addresses. The labels are not decoration:
    PER_OP_CYCLES and its two siblings describe *this* assembly, and nothing
    could check that while the only output was an opaque byte string — a NOP
    added to ``oploop`` and a regenerated golden blob left every constant
    green. ``tests/test_asid_player.py`` walks ``oploop``..``tail`` and
    ``dloop`` to re-derive them."""
    if not 1 <= tick_divider <= 255:
        raise ValueError(f"tick_divider must be 1..255, got {tick_divider}")
    ring_size = RING_SLOTS * slot_size
    ring_end = ring_base + ring_size
    b_lo, b_mi, b_hi = ring_base & 0xFF, (ring_base >> 8) & 0xFF, (ring_base >> 16) & 0xFF
    e_lo, e_mi, e_hi = ring_end & 0xFF, (ring_end >> 8) & 0xFF, (ring_end >> 16) & 0xFF
    s_lo, s_hi = slot_size & 0xFF, (slot_size >> 8) & 0xFF
    trk_lo, trk_hi = TRACKER_ADDR & 0xFF, (TRACKER_ADDR >> 8) & 0xFF
    buf_lo, buf_hi = LANDING_BUF & 0xFF, (LANDING_BUF >> 8) & 0xFF

    a = _Asm(HANDLER_ADDR)

    # --- pull next slot: REU ring → landing buffer -------------------------
    # src ← tracker (24-bit)
    a.emit(0xAD, trk_lo, trk_hi, 0x8D, 0x04, 0xDF)  # LDA trk_lo ; STA $DF04
    a.emit(0xAD, (trk_lo + 1) & 0xFF, trk_hi, 0x8D, 0x05, 0xDF)  # src_mi
    a.emit(0xAD, (trk_lo + 2) & 0xFF, trk_hi, 0x8D, 0x06, 0xDF)  # src_hi
    # dst = landing buffer
    a.emit(0xA9, buf_lo, 0x8D, 0x02, 0xDF)  # LDA #<buf ; STA $DF02
    a.emit(0xA9, buf_hi, 0x8D, 0x03, 0xDF)  # LDA #>buf ; STA $DF03
    # len = slot_size
    a.emit(0xA9, s_lo, 0x8D, 0x07, 0xDF)  # LDA #<slot ; STA $DF07
    a.emit(0xA9, s_hi, 0x8D, 0x08, 0xDF)  # LDA #>slot ; STA $DF08
    a.emit(0xA9, 0x00, 0x8D, 0x0A, 0xDF)  # LDA #0 ; STA $DF0A (both auto-inc)
    a.emit(0xA9, REU.CMD_FETCH_EXEC, 0x8D, 0x01, 0xDF)  # LDA #$91 ; STA $DF01

    # --- advance tracker by slot_size (24-bit) -----------------------------
    a.emit(0x18)  # CLC
    a.emit(0xAD, trk_lo, trk_hi, 0x69, s_lo, 0x8D, trk_lo, trk_hi)  # lo += <slot
    a.emit(0xAD, (trk_lo + 1) & 0xFF, trk_hi, 0x69, s_hi, 0x8D, (trk_lo + 1) & 0xFF, trk_hi)  # mi
    a.emit(0xAD, (trk_lo + 2) & 0xFF, trk_hi, 0x69, 0x00, 0x8D, (trk_lo + 2) & 0xFF, trk_hi)  # hi

    # --- wrap: tracker(24) >= ring_end(24) → reset to ring_base ------------
    a.emit(0xAD, (trk_lo + 2) & 0xFF, trk_hi, 0xC9, e_hi)  # LDA src_hi ; CMP #end_hi
    a.branch(0x90, "no_wrap")  # BCC → src_hi < end_hi
    a.branch(0xD0, "do_wrap")  # BNE → src_hi > end_hi
    a.emit(0xAD, (trk_lo + 1) & 0xFF, trk_hi, 0xC9, e_mi)  # LDA src_mi ; CMP #end_mi
    a.branch(0x90, "no_wrap")
    a.branch(0xD0, "do_wrap")
    a.emit(0xAD, trk_lo, trk_hi, 0xC9, e_lo)  # LDA src_lo ; CMP #end_lo
    a.branch(0x90, "no_wrap")
    a.label("do_wrap")
    a.emit(0xA9, b_lo, 0x8D, trk_lo, trk_hi)  # LDA #base_lo ; STA src_lo
    a.emit(0xA9, b_mi, 0x8D, (trk_lo + 1) & 0xFF, trk_hi)  # src_mi
    a.emit(0xA9, b_hi, 0x8D, (trk_lo + 2) & 0xFF, trk_hi)  # src_hi
    a.label("no_wrap")

    # --- execute the landing-buffer ops ------------------------------------
    # ZP ptr ← buf
    a.emit(0xA9, buf_lo, 0x85, ZP_PTR)  # LDA #<buf ; STA $FB
    a.emit(0xA9, buf_hi, 0x85, ZP_PTR + 1)  # LDA #>buf ; STA $FC
    a.emit(0xA0, 0x00, 0xB1, ZP_PTR)  # LDY #0 ; LDA ($FB),Y   (n_ops)
    a.branch(0xF0, "tail")  # BEQ tail (0 ops → hold tick)
    a.emit(0x8D, NOPS_COUNTER_ADDR & 0xFF, (NOPS_COUNTER_ADDR >> 8) & 0xFF)  # STA nops
    # ptr += 1 (skip n_ops byte)
    a.emit(0xE6, ZP_PTR)  # INC $FB
    a.branch(0xD0, "op0")  # BNE +2
    a.emit(0xE6, ZP_PTR + 1)  # INC $FC
    a.label("op0")

    a.label("oploop")
    a.emit(0xA0, 0x00, 0xB1, ZP_PTR)  # LDY #0 ; LDA ($FB),Y   (addr_lo)
    a.sta_abs_label("store", 1)  # STA store+1
    a.emit(0xA0, 0x01, 0xB1, ZP_PTR)  # LDY #1 ; LDA ($FB),Y   (addr_hi)
    a.sta_abs_label("store", 2)  # STA store+2
    a.emit(0xA0, 0x02, 0xB1, ZP_PTR)  # LDY #2 ; LDA ($FB),Y   (value)
    a.label("store")
    a.emit(0x8D, 0x00, 0x00)  # STA $0000  (operand self-modified above)
    a.emit(0xA0, 0x03, 0xB1, ZP_PTR)  # LDY #3 ; LDA ($FB),Y   (wait)
    a.branch(0xF0, "skipdelay")  # BEQ skipdelay (wait 0)
    a.jsr("delay")
    a.label("skipdelay")
    # ptr += 4
    a.emit(0xA5, ZP_PTR, 0x18, 0x69, OP_BYTES, 0x85, ZP_PTR)  # LDA $FB ; CLC ; ADC #4 ; STA $FB
    a.branch(0x90, "op_noinc")  # BCC +2
    a.emit(0xE6, ZP_PTR + 1)  # INC $FC
    a.label("op_noinc")
    a.emit(0xCE, NOPS_COUNTER_ADDR & 0xFF, (NOPS_COUNTER_ADDR >> 8) & 0xFF)  # DEC nops
    a.branch(0xD0, "oploop")  # BNE oploop

    # --- tick divider: chain $EA31 every Nth tick, lean-exit the rest ------
    a.label("tail")
    a.emit(0xCE, TICK_COUNTER_ADDR & 0xFF, (TICK_COUNTER_ADDR >> 8) & 0xFF)  # DEC tick
    a.branch(0xD0, "lean")  # BNE lean
    a.emit(0xA9, tick_divider)  # LDA #N
    a.emit(0x8D, TICK_COUNTER_ADDR & 0xFF, (TICK_COUNTER_ADDR >> 8) & 0xFF)  # STA tick
    a.emit(0x4C, KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)  # JMP $EA31
    a.label("lean")
    a.emit(0xAD, CIA1.ICR & 0xFF, (CIA1.ICR >> 8) & 0xFF)  # LDA $DC0D  (ack CIA #1)
    a.emit(0x4C, KERNAL.IRQ_RETURN & 0xFF, (KERNAL.IRQ_RETURN >> 8) & 0xFF)  # JMP $EA81

    # --- delay subroutine: A = units, ~5 cyc each --------------------------
    a.label("delay")
    a.emit(0xA8)  # TAY
    a.label("dloop")
    a.emit(0x88)  # DEY
    a.branch(0xD0, "dloop")  # BNE dloop
    a.emit(0x60)  # RTS

    return a.resolve(), a.symbols()


def build_player(slot_size: int, tick_divider: int, *, ring_base: int = RING_BASE) -> bytes:
    """The assembled player for these parameters. See [build_player_symbols],
    which is the same assembly plus the label addresses the cost-model guard
    needs to read it back."""
    return build_player_symbols(slot_size, tick_divider, ring_base=ring_base)[0]


def clamp_frame_rate(frame_rate_hz: float) -> float:
    """Clamp a wire-derived ASID frame rate into the band the protocol and the
    CIA can express (:data:`MIN_FRAME_RATE_HZ`..:data:`MAX_FRAME_RATE_HZ`),
    warning when it has to. **This is the boundary** — every path that turns a
    ``0x31`` message into a consume rate goes through it, because the damage
    (C64 IRQ storm + a permanently flooded DMA socket) is done by the time the
    rate reaches the CIA latch."""
    if MIN_FRAME_RATE_HZ <= frame_rate_hz <= MAX_FRAME_RATE_HZ:
        return frame_rate_hz
    # Anything not inside the band — including NaN, which fails both
    # comparisons — clamps toward the floor unless it is definitely too fast.
    clamped = MAX_FRAME_RATE_HZ if frame_rate_hz > MAX_FRAME_RATE_HZ else MIN_FRAME_RATE_HZ
    log.warning(
        "asid_player: requested frame rate %s Hz is outside the %.0f-%.0f Hz band; using %.1f Hz",
        frame_rate_hz,
        MIN_FRAME_RATE_HZ,
        MAX_FRAME_RATE_HZ,
        clamped,
    )
    return clamped


def tick_divider_for_rate(rate_hz: float) -> int:
    """How many consume ticks per kernal-tail chain so SCNKEY/jiffy stay ~60 Hz.
    At single speed → 1 (chain every tick); at 960 Hz → 16.

    Clamped to 1..255: the value is emitted as an ``LDA #N`` immediate, so a
    divider above 255 would be truncated to a different number entirely and an
    exact multiple of 256 would emit 0 — "chain once every 256 ticks", with the
    jiffy clock and SCNKEY running far slower than this function promises."""
    return max(1, min(255, round(rate_hz / 60.0)))


def restore_kernal_irq(api: C64Backend, system: str) -> None:
    """Hand the C64's IRQ back to the kernal: ``$0314/$0315`` → ``$EA31`` and
    CIA #1 Timer A → this system's kernal latch.

    Idempotent, best-effort (never raises), and both halves matter on their own,
    which is why they live in one function rather than at each caller's
    discretion. Restoring only the vector leaves the jiffy clock, SCNKEY and
    every kernal timing service running at whatever cadence the ASID stream
    asked for — a spec-legal 960 Hz ``0x31`` burns a third of the machine's
    cycles in ``$EA31`` and runs the jiffy clock 16× fast for every scene that
    follows, until a power cycle. Restoring only the latch leaves the kernal IRQ
    vectored into a ring player nobody feeds."""
    try:
        api.write_regs(
            f"{VECTORS.IRQ:04X}", KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF
        )
        latch = kernal_cia1_latch(system)
        api.write_memory(f"{CIA1.TIMER_A_LO:04X}", f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}")
        api.flush()
    except Exception as e:  # best-effort; teardown must not raise
        log.debug("asid_player: kernal IRQ restore failed: %s", e)


# --------------------------------------------------------------------------
# Producer.
# --------------------------------------------------------------------------
class AsidRingPlayer:
    """Scene-facing producer: installs the 6502 player and streams serialized
    frame-slots into the REU ring ahead of a computed read head (open-loop).

    Lifecycle::

        player = AsidRingPlayer(api, system="NTSC", n_chips=1)
        player.start(frame_rate_hz)   # prefill holds + prebuffer + arm the IRQ
        player.push_frame(slot_bytes) # writer thread streams it into the ring
        player.set_frame_rate(hz)     # on a 0x31 change (retunes CIA + read head)
        player.reinit(n_chips)        # on a chip-count change (new slot size)
        player.stop()                 # disarm IRQ, restore CIA/$0314, join writer
        player.reset()                # back to a fresh layout for the next lap
    """

    def __init__(
        self,
        api: C64Backend,
        *,
        system: str = "NTSC",
        n_chips: int = 1,
        ring_base: int = RING_BASE,
        lead_seconds: float = DEFAULT_LEAD_SLOTS_SECONDS,
        prebuffer_seconds: float = DEFAULT_PREBUFFER_SECONDS,
    ) -> None:
        self.api = api
        self.system = system
        self.ring_base = ring_base
        self._lead_seconds = lead_seconds
        self._prebuffer_seconds = prebuffer_seconds

        self.n_chips = max(1, n_chips)
        self.slot_size = slot_size_for_chips(self.n_chips)

        self._q: queue.Queue[bytes] = queue.Queue(maxsize=_QUEUE_MAX_SLOTS)
        # ONE PollThread for the player's lifetime, restarted across reinit()
        # rather than replaced. Its "already running" guard lives on the object
        # (see _pollthread's module docstring), so a fresh object per start()
        # threw the guard away — and a writer still blocked in reu_write past
        # the join timeout would then race a second one over self._write_pos and
        # one REU ring. The loop exits on this thread's own stop event, so a
        # start() that does spawn a replacement can never un-stop the old one.
        self._writer = PollThread(
            self._writer_loop, name="asid-ring", manual=True, join_timeout=1.0
        )
        self._armed = False
        # Set the moment start() begins touching the C64 and cleared by the
        # teardown that puts it back. Distinct from _armed on purpose: start()
        # programs the CIA #1 latch immediately but hooks $0314 only once a
        # prebuffer arrives, so a stream that never sends a frame (one 0x31 and
        # no 0x4E at all) leaves the machine running the KERNAL IRQ at the
        # sender's rate with _armed still False. Teardown restores on this flag,
        # not on _armed, so the wire can never keep the CIA.
        self._installed = False
        self._lock = threading.Lock()  # guards rate/anchor accounting

        # Read-head accounting (cumulative consumed-slot estimate, drift-free).
        self._rate = 60.0
        self._rate_anchor = 0.0  # monotonic time the current rate took effect
        self._consumed_base = 0  # slots consumed before the last rate change
        self._write_pos = 0  # absolute slots written (monotone)
        self._lead_target = 1
        self._lead_panic = 1

        # Telemetry.
        self._underrun_pads = 0
        self._real_written = 0
        self._pushed = 0
        self._dropped_full = 0
        self._stale_slots = 0  # dropped by _take_slot: sized for a previous layout
        # None = never sampled. A plain -1 sentinel collided with a genuinely
        # negative lead — the pathological state this telemetry exists to catch
        # — so the minimum tracked the current value and stop()'s guard then
        # suppressed the whole line on exactly the run worth reading.
        self._lead_min: int | None = None
        self._lead_max: int | None = None

    # ---- rate / read-head accounting --------------------------------------
    def _recompute_lead(self) -> None:
        lead = int(self._rate * self._lead_seconds)
        self._lead_target = max(1, min(lead, RING_SLOTS // 2))
        self._lead_panic = max(1, self._lead_target // 4)

    def frame_cycle_budget(self) -> int:
        """C64 cycles one slot's ops may spend at the current consume rate.

        The rate is the one the CIA actually runs (post-latch-quantization), and
        the clock is the *machine's* — a ``0x31`` retunes which video standard
        the scene believes the tune targets, but not the crystal the timer counts
        — so this is what the 6510 really has between two ticks, less the
        handler's own reserve (:data:`FRAME_BUDGET_FRACTION`)."""
        return int(cpu_clock(self.system) * FRAME_BUDGET_FRACTION / max(self._rate, 1e-6))

    def _read_head(self) -> int:
        """Estimated slots consumed by the C64 so far (cumulative across rate
        changes). Absolute-slot space, matching ``_write_pos``."""
        if not self._armed:
            return 0
        return self._consumed_base + int((time.monotonic() - self._rate_anchor) * self._rate)

    # ---- bring-up ---------------------------------------------------------
    def start(self, frame_rate_hz: float) -> None:
        """Prefill the ring, install the player + program CIA #1 Timer A, then
        start the writer thread — but **arm lazily**: the read-head clock and the
        ``$0314`` swap only happen once a real-frame prebuffer has accumulated
        (see :meth:`_try_arm`). This is critical for a real-time producer: if we
        armed immediately, the computed read head would run away during the gap
        before the ASID host starts streaming, and real frames would land in
        already-consumed ring slots (heard as unbroken holds). Arming when the
        prebuffer is ready makes ``gate_time`` coincide with data actually
        flowing, so the write head stays a full ``lead`` ahead.

        **Refuses to bring up over a live writer.** A previous writer still
        blocked in ``reu_write`` past its join timeout would otherwise be
        stranded: ``PollThread.start()`` declines the duplicate, so it never
        clears the stop event, the abandoned worker exits on its next check and
        nothing spawns a replacement — the player would install, never arm, and
        stay silent for the whole run while claiming it was padding holds. Worse,
        that worker keeps REUWRITEing at the *old* slot size into a ring this
        install just re-described at the new one. Staying down is the honest and
        safe outcome; the next activation brings it up once the worker is gone."""
        if self._writer.is_running():
            log.warning(
                "asid_player: the previous writer thread has not exited (blocked on the "
                "DMA link); refusing to install a second player over it — the buffered "
                "path stays down for this activation"
            )
            return
        # From here on the C64's CIA #1 latch is ours, so teardown owes it a
        # restore even if one of the install writes below raises.
        self._installed = True
        frame_rate_hz = clamp_frame_rate(frame_rate_hz)
        self._latch = cia1_latch_for_rate(frame_rate_hz, self.system)
        self._rate = actual_rate_for_latch(self._latch, self.system)
        self._recompute_lead()
        # Prebuffer to the full lead so we start with maximum jitter cushion (a
        # real-time producer feeds at exactly the consume rate, so the lead can
        # never GROW past this — it's the only headroom before a stall pads).
        self._prebuffer_target = max(
            1, min(int(self._rate * self._prebuffer_seconds), self._lead_target)
        )
        self._divider = tick_divider_for_rate(self._rate)

        # Prefill the whole ring with hold slots so the first laps read silence,
        # not uninitialized REU.
        self._prefill_holds()

        # Upload the player, seed the tracker + counters + CIA latch. The vector
        # swap is deferred to _try_arm (the CIA keeps running the kernal tail at
        # the new latch until then — harmless).
        handler = build_player(self.slot_size, self._divider, ring_base=self.ring_base)
        self.api.write_memory_file(f"{HANDLER_ADDR:04X}", handler)
        self.api.write_memory(
            f"{TRACKER_ADDR:04X}",
            f"{self.ring_base & 0xFF:02X}"
            f"{(self.ring_base >> 8) & 0xFF:02X}"
            f"{(self.ring_base >> 16) & 0xFF:02X}",
        )
        # tick counter = 1: first IRQ DECs to 0, reloads N, chains; nops = 0.
        self.api.write_memory(f"{TICK_COUNTER_ADDR:04X}", "01")
        self.api.write_memory(f"{NOPS_COUNTER_ADDR:04X}", "00")
        # Program CIA #1 Timer A latch (kernal left it running in continuous mode).
        self.api.write_memory(
            f"{CIA1.TIMER_A_LO:04X}", f"{self._latch & 0xFF:02X}{(self._latch >> 8) & 0xFF:02X}"
        )
        self.api.flush()

        self._writer.start()
        log.info(
            "asid_player: installed — %d chip(s), slot %d B, %.1f Hz (latch %d, N=%d), "
            "ring %d slots @ $%06X, lead %d, prebuffer %d slots (arming on first data)",
            self.n_chips,
            self.slot_size,
            self._rate,
            self._latch,
            self._divider,
            RING_SLOTS,
            self.ring_base,
            self._lead_target,
            self._prebuffer_target,
        )
        # If frames are already queued (e.g. a test pre-seeded them), arm now;
        # otherwise the writer thread arms when the prebuffer fills.
        self._try_arm()

    def _try_arm(self) -> bool:
        """Arm once the queue holds a full prebuffer of real frames: drain them
        into ring slots 0.., anchor the read head at that instant, and swap
        ``$0314`` → the handler. Idempotent + thread-safe (start() and the writer
        both call it). Returns True once armed, and refuses to arm at all once a
        teardown has set the writer's stop event — the blocking DMA below is how
        this method outlives teardown's bounded join.

        The prebuffer goes out as **one** contiguous transfer rather than a write
        per slot: the whole method runs under the lock ``set_frame_rate`` needs
        on the MIDI reader thread, and at a spec-legal 16× the prebuffer pins at
        ``RING_SLOTS // 2`` = 256 slots — 256 blocking DMA writes is ~1.3 s of
        held lock, during which the reader is not draining ``iter_pending()``.
        Contiguous slots cost the same as one on this link (CLAUDE.md)."""
        with self._lock:
            if self._armed:
                return True
            if self._q.qsize() < self._prebuffer_target:
                return False
            stale_before = self._stale_slots
            slots: list[bytes] = []
            while len(slots) < self._prebuffer_target:
                slot = self._take_slot()
                if slot is None:
                    break
                slots.append(slot)
            mismatched = self._stale_slots - stale_before
            n = len(slots)
            if mismatched:
                # A chip-count reinit can leave stragglers packed at the old
                # slot size in flight from the reader thread.
                log.warning(
                    "asid_player: discarded %d prebuffer slot(s) sized for a previous "
                    "chip count; %d of %d realized",
                    mismatched,
                    n,
                    self._prebuffer_target,
                )
            if n < self._prebuffer_target:
                # Arming here would start the read-head clock against a ring the
                # prebuffer never filled — Symptom 1 with extra steps. The
                # discarded stragglers are gone, so the next call re-checks
                # against fresh, correctly-sized frames.
                return False
            self._write_slots(0, slots)
            self.api.flush()
            if self._writer.stop_event.is_set():
                # Teardown is in flight and its bounded join may already have
                # given up on us — the blocking DMA above is exactly how this
                # method outlives it. Hooking $0314 now would leave the C64
                # running the ASID IRQ into the next scene, against a ring
                # nobody feeds and at the CIA cadence this stream asked for,
                # with $C000 (where a later scene's DAC/NMI handler lands) as
                # the vector. The ring slots just written are inert: nothing
                # reads them unless the vector is hooked.
                log.debug("asid_player: arm abandoned — teardown in flight")
                return False
            self._write_pos = n
            self._real_written += n
            self._rate_anchor = time.monotonic()
            self._consumed_base = 0
            self._armed = True
            self.api.write_regs(
                f"{VECTORS.IRQ:04X}", HANDLER_ADDR & 0xFF, (HANDLER_ADDR >> 8) & 0xFF
            )
            self.api.flush()
        log.info("asid_player: armed — read head live, %d slots prebuffered", n)
        return True

    def _prefill_holds(self) -> None:
        hold = hold_slot(self.slot_size)
        # One reu_write per slice of slots (cap the burst); the whole ring is
        # holds, so a repeated block is fine.
        block = hold * max(1, (32 * 1024) // self.slot_size)
        total = RING_SLOTS * self.slot_size
        for off in range(0, total, len(block)):
            n = min(len(block), total - off)
            self.api.reu_write(self.ring_base + off, block[:n])
        self.api.flush()

    # ---- streaming --------------------------------------------------------
    def push_frame(self, slot_bytes: bytes) -> None:
        """Enqueue one serialized frame-slot. Never blocks the reader thread: the
        queue is large and, before arming, it fills to the prebuffer; after
        arming the writer keeps it drained. A full queue means the producer is
        outrunning the consume rate (can't happen with a matched cadence) — drop
        rather than stall the MIDI reader."""
        try:
            self._q.put_nowait(slot_bytes)
            self._pushed += 1
        except queue.Full:
            self._dropped_full += 1

    def _take_slot(self, timeout: float | None = None) -> bytes | None:
        """Pop the next queued slot that matches the current layout, or None
        when the queue runs dry (within ``timeout``, if given).

        **Every** consumer goes through here. A slot whose length doesn't match
        ``slot_size`` is a straggler packed by the reader thread for a previous
        chip count (see :meth:`reinit`), and ``_write_slots`` sizes its bursts by
        slot *count*: one oversized slot writes past its run and misaligns every
        slot after it in the ring. The 6502 player then reads ``n_ops`` from what
        was an op's wait byte and decodes ``[value][wait]`` pairs as absolute
        addresses — i.e. ``STA`` anywhere in the C64's 64K, including the handler
        at $C000 and the $0314 vector. Two of the three consumers used to filter
        and the third did not."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                if deadline is None:
                    slot = self._q.get_nowait()
                else:
                    slot = self._q.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if len(slot) == self.slot_size:
                return slot
            self._stale_slots += 1

    def _drain_queue(self) -> None:
        """Drop every queued slot — the layout they were packed for is gone."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def set_frame_rate(self, frame_rate_hz: float) -> None:
        """Retune the consume rate on a ``0x31`` change: reprogram CIA #1 and
        re-anchor the read head (freezing the current consumed estimate) so the
        absolute-slot alignment with ``_write_pos`` is preserved.

        Works **before arming too** — a ``0x31`` almost always arrives at stream
        start, before the prebuffer has filled, and if it were dropped the player
        would arm at the wrong (initial video-rate) cadence and silently decimate
        the tune to that rate. Pre-arm it just retunes the CIA latch + rebuilds
        the handler's tick divider so the correct rate takes effect the moment
        the vector swaps; post-arm it also re-anchors the running read head.

        The requested rate is clamped to the ASID band first — see
        :func:`clamp_frame_rate`. This is the boundary the scene's ``0x31``
        handling should route through."""
        frame_rate_hz = clamp_frame_rate(frame_rate_hz)
        latch = cia1_latch_for_rate(frame_rate_hz, self.system)
        rate = actual_rate_for_latch(latch, self.system)
        divider = tick_divider_for_rate(rate)
        with self._lock:
            # Read _armed under the lock _try_arm holds across the whole arm
            # sequence, so an arm in progress serializes ahead of this and the
            # post-arm re-anchor below actually runs.
            armed = self._armed
            if armed:
                self._consumed_base = self._read_head()
                self._rate_anchor = time.monotonic()
            self._latch = latch
            self._rate = rate
            self._divider = divider
            self._recompute_lead()
            if not armed:
                # The prebuffer target scales with the (now correct) lead.
                self._prebuffer_target = max(
                    1, min(int(rate * self._prebuffer_seconds), self._lead_target)
                )
        # Reprogram the CIA latch (takes effect at the vector swap if not armed).
        self.api.write_memory(
            f"{CIA1.TIMER_A_LO:04X}", f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"
        )
        if not armed:
            # The vector isn't hooked yet, so rebuild the handler in place — the
            # tick divider then matches the real rate before it starts running.
            # (`armed` came from the locked read above, so this branch can no
            # longer lose a race against an arm and rewrite a live handler.)
            self.api.write_memory_file(
                f"{HANDLER_ADDR:04X}",
                build_player(self.slot_size, divider, ring_base=self.ring_base),
            )
        self.api.flush()
        log.info(
            "asid_player: retuned to %.1f Hz (latch %d, N=%d, %s)",
            rate,
            latch,
            divider,
            "armed" if armed else "pre-arm",
        )

    def reinit(self, n_chips: int) -> None:
        """Re-install the player for a new chip count (new slot size). Called on a
        chip-count change; briefly disarms and re-arms (rare, ~once per tune).

        The layout only ever moves with **no writer alive**. ``_write_slots``
        derives its ring offsets from ``slot_size``, so assigning a new one while
        a writer is blocked mid-burst in ``reu_write`` put the rest of that
        burst's old-sized payloads at the new stride: slots land across slot
        boundaries, the 6502 reads ``n_ops`` from a mid-op byte and executes the
        stream shifted — arbitrary ``STA``s across the C64's 64K. A single
        ``0x5F`` SysEx reaches this method, so the precondition is checked, not
        assumed; failing it leaves the player down (teardown has already handed
        the machine back to the kernal) rather than half-reconfigured."""
        n_chips = max(1, n_chips)
        if n_chips == self.n_chips:
            return
        rate = self._rate
        self._teardown_player()
        if self._writer.is_running():
            log.warning(
                "asid_player: the writer thread is still blocked on the DMA link; "
                "refusing to re-init to %d chip(s) under it — the buffered path stays "
                "down until the scene is re-activated",
                n_chips,
            )
            return
        self._drain_queue()
        self._set_layout(n_chips)
        self.start(rate)

    def reset(self, n_chips: int = 1) -> None:
        """Put a stopped player back to a fresh ``n_chips`` layout for the next
        scene activation.

        Playlists reuse scene instances, so without this lap 2 starts on lap 1's
        chip count — which is what makes :meth:`reinit`'s ``n_chips ==
        self.n_chips`` guard let the ring *shrink* — and with lap 1's leftover
        frames still queued, so the new stream's prebuffer arms on the previous
        tune's registers."""
        if self._writer.is_running():
            log.warning(
                "asid_player: the writer thread from the previous activation is still "
                "blocked on the DMA link; keeping its %d-chip layout",
                self.n_chips,
            )
            return
        self._drain_queue()
        self._set_layout(n_chips)

    def _set_layout(self, n_chips: int) -> None:
        """Adopt a chip count and the ring geometry that follows from it. Only
        ever called with no writer alive (see :meth:`reinit`)."""
        self.n_chips = max(1, n_chips)
        self.slot_size = slot_size_for_chips(self.n_chips)
        self._write_pos = 0

    # ---- writer loop ------------------------------------------------------
    def _writer_loop(self, stop: threading.Event) -> None:
        # Phase 1: wait for a real-frame prebuffer, then arm (start the read-head
        # clock + swap $0314). See start()/_try_arm for why we don't arm eagerly.
        while not stop.is_set() and not self._armed:
            if not self._try_arm():
                time.sleep(0.005)
        # Phase 2: steady state — keep the write head a `lead` ahead of the read.
        while not stop.is_set():
            read_head = self._read_head()
            lead = self._write_pos - read_head
            self._lead_min = lead if self._lead_min is None else min(self._lead_min, lead)
            self._lead_max = lead if self._lead_max is None else max(self._lead_max, lead)
            deficit = self._lead_target - lead
            if deficit <= 0:
                time.sleep(0.002)
                continue
            # Gather up to `deficit` real slots without blocking (_take_slot
            # drops any straggler sized for a previous layout).
            slots: list[bytes] = []
            for _ in range(deficit):
                slot = self._take_slot()
                if slot is None:
                    break
                slots.append(slot)
            pad_seconds = 0.0
            if slots:
                self._real_written += len(slots)
            else:
                # Producer momentarily empty. Only pad NEUTRAL (a hold) once the
                # lead has actually drained to the panic watermark — otherwise
                # just wait for the producer (no glitch). Holds make the SID hold
                # its last state (no echo).
                if lead > self._lead_panic:
                    slot = self._take_slot(timeout=0.02)
                    if slot is None:
                        continue
                    slots.append(slot)
                    self._real_written += 1
                else:
                    # Pad a batch in one contiguous write, then sleep the time it
                    # buys. Nothing else paces this branch: a spec-legal 16×
                    # (960 Hz) stream that then goes quiet leaves the lead
                    # negative forever, and padding one slot per unpaced
                    # iteration ran at the link's maximum rate indefinitely
                    # (measured: 705 reu_write/s — the whole ~200/s DMA ceiling
                    # on real hardware, taken from the render path that shares
                    # the socket). Batched + paced, holds cost `rate / pads`
                    # writes per second, ~15/s at any rate in the band.
                    pads = min(deficit, max(1, self._lead_panic))
                    slots.extend([hold_slot(self.slot_size)] * pads)
                    self._underrun_pads += pads
                    pad_seconds = pads / self._rate
            self._write_slots(self._write_pos, slots)
            self._write_pos += len(slots)
            if pad_seconds:
                time.sleep(pad_seconds)

    def _write_slots(self, start_index: int, slots: list[bytes]) -> None:
        """REUWRITE consecutive slots starting at absolute ``start_index``,
        splitting into runs that don't cross the ring wrap (so each run is one
        contiguous transfer) and don't exceed ``_MAX_DMA_BURST_BYTES``.

        The stride is snapshotted once: ``self.slot_size`` is re-read nowhere in
        the loop, so a payload list and the stride it was built for can never
        disagree half way through a call. (:meth:`reinit` now refuses to move the
        layout while a writer is alive, which is the real guarantee; this keeps
        the function correct on its own terms rather than on that promise.)"""
        i = 0
        n = len(slots)
        slot_size = self.slot_size
        slots_per_burst = max(1, _MAX_DMA_BURST_BYTES // slot_size)
        while i < n:
            ring_slot = (start_index + i) % RING_SLOTS
            run = min(n - i, RING_SLOTS - ring_slot, slots_per_burst)
            payload = b"".join(slots[i : i + run])
            self.api.reu_write(self.ring_base + ring_slot * slot_size, payload)
            i += run

    # ---- shutdown ---------------------------------------------------------
    def _teardown_player(self) -> None:
        """Stop the writer + hand the C64's IRQ back to the kernal ($0314 + the
        CIA #1 latch). Idempotent; leaves the SID untouched (the scene silences
        it).

        Restores on ``_installed``, never on ``_armed``: ``start()`` programs the
        CIA the moment it runs, so a stream that sends a ``0x31`` and no frames
        never arms yet has already retuned the machine's jiffy IRQ. Both writes
        go out whenever the player touched the C64 at all — they are idempotent,
        cost two DMA ops, and the alternative is a wrong CIA latch surviving into
        every later scene."""
        # Stop the thread but KEEP the PollThread object: after a timed-out join
        # it deliberately holds its reference so a later start() refuses a
        # duplicate. Discarding it here is what let reinit() (reachable from one
        # 0x5F SysEx via _reconfigure_chips) run a second writer alongside an
        # abandoned one, racing self._write_pos over a single REU ring. The stop
        # event it sets is also what makes an in-flight _try_arm abandon its arm.
        self._writer.stop()
        if not self._claim_installed():
            return
        restore_kernal_irq(self.api, self.system)

    def _claim_installed(self) -> bool:
        """Clear the armed/installed state under the arm lock, reporting whether
        the C64 still owes a restore. False on a player that never installed.

        The lock is the one that :meth:`_try_arm` holds across its whole arm
        sequence, so the disarm cannot interleave with an arm and lose the race
        to write ``$0314``. It is taken with a bound because ``_try_arm`` holds
        it across blocking DMA: teardown must never hang on the link, so a
        timeout says so and restores anyway — a racing restore beats none."""
        acquired = self._lock.acquire(timeout=_TEARDOWN_LOCK_TIMEOUT_S)
        try:
            installed = self._installed
            self._installed = False
            self._armed = False
        finally:
            if acquired:
                self._lock.release()
        if not acquired:
            log.warning(
                "asid_player: the arm lock was still held after %.1f s (a DMA write is "
                "wedged); restoring the kernal IRQ anyway",
                _TEARDOWN_LOCK_TIMEOUT_S,
            )
        return installed

    def stop(self) -> None:
        self._teardown_player()
        log.info(
            "asid_player: pushed=%d real_written=%d holds=%d dropped_full=%d stale=%d",
            self._pushed,
            self._real_written,
            self._underrun_pads,
            self._dropped_full,
            self._stale_slots,
        )
        if self._lead_min is not None:
            log.info(
                "asid_player: write-ahead lead min=%d max=%d slots (target=%d)",
                self._lead_min,
                self._lead_max,
                self._lead_target,
            )
