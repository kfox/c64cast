"""Buffered C64-side ASID ring player — cycle-accurate high-multispeed playback.

:class:`~c64cast.asid_scene.AsidScene`'s default path coalesces incoming ASID
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
:mod:`c64cast.sampler` (open-loop: the C64 crystal is exact, so the read head is
computed from wall-clock, never read back — no servo, no C64→host reads), with
an IRQ-driven ring consumer modeled on the REU audio pump in :mod:`c64cast.audio`.

**U64-only** — it needs bus-clean ``reu_write`` (``profile.supports_reu``). On
TeensyROM / any no-REU backend :class:`AsidScene` keeps the coalesced path (and
never blanks the TR display). Because :class:`AsidScene` runs no ``$D418`` DAC /
NMI, the whole ``$C000`` RAM page and the REU are free for the player.

Two halves, mirroring :mod:`c64cast.sampler`:

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

from .asid import _ASID_REG_TO_OFFSET
from .c64 import CIA1, KERNAL, REU, VECTORS, cpu_clock

if TYPE_CHECKING:
    from .backend import C64Backend

log = logging.getLogger("c64cast.asid_player")

# --------------------------------------------------------------------------
# Memory map. AsidScene runs no DAC/NMI/pump, so $C000-$CFFF and the REU are
# entirely free. Pinned by tests.
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

# The most write-ops one chip's frame can carry: 22 non-control registers +
# 3 voices × 2 control writes (hard restart). Sets the slot size.
MAX_OPS_PER_CHIP = 28
OP_BYTES = 4  # addr_lo, addr_hi, value, wait
_SLOT_ALIGN = 16  # round slot size up to this so single-SID → 128 (plan pins it)

# NTSC kernal default CIA #1 Timer A latch ($4025 → ~60.0 Hz), restored on
# teardown so the next kernal IRQ runs at the stock jiffy rate (see audio.py's
# identically-named constant; PAL differs but the timer keeps running and a
# reset clears it either way).
KERNAL_CIA1_TIMER_A_LATCH_NTSC = 0x4025

# Producer buffering depth / watermarks (in slots). Unlike the FPGA sampler
# (fed by a demuxer that races ahead of real time, so it can grow its lead), an
# ASID host streams in real time at exactly the consume cadence — the ring can
# never build lead beyond the startup prebuffer. So seed the prebuffer CLOSE to
# the lead target: that cushion is all the jitter headroom there is before a
# genuine producer stall pads a hold (SID holds its last state — no echo).
DEFAULT_LEAD_SLOTS_SECONDS = 0.30  # keep the write head this far ahead of read
DEFAULT_PREBUFFER_SECONDS = 0.30  # seed the full lead before arming (max cushion)
_QUEUE_MAX_SLOTS = 4096

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


def serialize_frame(
    regs: dict[int, int],
    control_first: dict[int, int],
    base_addr: int,
    recipe: list[tuple[int, int]] | None = None,
) -> list[tuple[int, int, int]]:
    """Serialize one chip's frame into ``(abs_addr, value, wait_units)`` ops.

    ``regs`` maps SID register offset (0x00-0x18) → value for the registers this
    frame writes (as decoded by :func:`c64cast.asid.decode`); the control regs'
    *final* value wins there. ``control_first`` maps voice → the differing first
    control write (a gate-off→gate-on hard restart); those voices emit two
    control ops (first, then final). ``base_addr`` is the chip's ``$Dxxx`` base,
    baked into every op's absolute address so the 6502 player stays chip-agnostic.

    Ordering: without a ``recipe`` (the common case), non-control registers first
    (ascending offset), then per voice its control write(s) — a hard restart's
    first write carries a small default wait before the final. With a ``0x30``
    recipe, the writes are ordered by the recipe's register-id sequence and each
    op takes that entry's wait (recipe ids absent from the frame are skipped; any
    frame register the recipe omits is appended in default order)."""
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
        # Order by the recipe's register-id sequence + take its waits. Group the
        # writes by reg_id (a voice's first/second write have distinct ids), then
        # walk the recipe. Frame registers the recipe omits keep default order,
        # appended after.
        by_id: dict[int, list[tuple[int, int, int, int]]] = {}
        for w in writes:
            by_id.setdefault(w[0], []).append(w)
        ordered: list[tuple[int, int, int]] = []
        seen: set[int] = set()
        for rid, wait_cycles in recipe:
            for _, offset, value, _dw in by_id.get(rid, ()):  # noqa: B007
                ordered.append((base_addr + offset, value, _wait_units_for_cycles(wait_cycles)))
            seen.add(rid)
        for rid, offset, value, dw in writes:
            if rid not in seen:
                ordered.append((base_addr + offset, value, dw))
        return ordered

    return [(base_addr + offset, value, dw) for (_rid, offset, value, dw) in writes]


def pack_slot(ops: list[tuple[int, int, int]], slot_size: int) -> bytes:
    """Pack concatenated ops (all active chips) into one fixed-size slot.

    ``[n_ops]`` then 4 bytes per op ``[addr_lo, addr_hi, value, wait]``,
    zero-padded to ``slot_size``. ``n_ops`` fits one byte (multi-SID tops out at
    8 × 28 = 224 ops < 256). Ops beyond what fits are dropped (should never
    happen: ``slot_size`` is derived from the chip count)."""
    max_ops = (slot_size - 1) // OP_BYTES
    if len(ops) > max_ops:
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


def hold_slot(slot_size: int) -> bytes:
    """A "hold" slot (``n_ops == 0``): the player applies no writes this tick and
    the SID holds its last state — the graceful producer-underrun pad (no echo)."""
    return bytes(slot_size)


# --------------------------------------------------------------------------
# 6502 handler builder — a tiny label-based assembler keeps the many relative
# branches correct; tests pin the exact output bytes.
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


def build_player(slot_size: int, tick_divider: int, *, ring_base: int = RING_BASE) -> bytes:
    """Assemble the CIA #1 Timer A IRQ player for a given slot size.

    Per IRQ: pull the next slot REU→landing-buffer (reload REU src from the
    main-RAM tracker, dst = landing buffer, len = ``slot_size``, trigger fetch;
    advance the tracker by ``slot_size`` and wrap at the ring end — never trust
    the REU read-back, exactly like the tracked audio pump), then execute the
    slot's ops (self-modify a ``STA`` target, write value, busy-wait ``wait``
    units), then chain ``$EA31`` every ``tick_divider``-th tick (keeping SCNKEY /
    jiffy ~60 Hz) and lean-exit the rest. A/X/Y are freely clobbered — the kernal
    ROM IRQ entry ($FF48) already saved them and the tail restores them."""
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
    a.emit(0xA9, tick_divider & 0xFF)  # LDA #N
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

    return a.resolve()


def cia1_latch_for_rate(rate_hz: float, system: str) -> int:
    """CIA #1 Timer A latch for a consume rate: ``round(cpu_clock/rate) - 1``,
    clamped to a valid 16-bit timer value (≥ 1)."""
    if rate_hz <= 0:
        raise ValueError(f"rate must be positive, got {rate_hz}")
    latch = round(cpu_clock(system) / rate_hz) - 1
    return max(1, min(latch, 0xFFFF))


def actual_rate_for_latch(latch: int, system: str) -> float:
    """The exact rate the CIA clocks a given latch: ``cpu_clock / (latch + 1)``.
    The read head is computed from this so it matches the hardware exactly."""
    return cpu_clock(system) / (latch + 1)


def tick_divider_for_rate(rate_hz: float) -> int:
    """How many consume ticks per kernal-tail chain so SCNKEY/jiffy stay ~60 Hz
    (≥ 1). At single speed → 1 (chain every tick); at 960 Hz → 16."""
    return max(1, round(rate_hz / 60.0))


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
        self._writer: threading.Thread | None = None
        self._running = False
        self._armed = False
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
        self._lead_min = -1
        self._lead_max = -1

    # ---- rate / read-head accounting --------------------------------------
    def _recompute_lead(self) -> None:
        lead = int(self._rate * self._lead_seconds)
        self._lead_target = max(1, min(lead, RING_SLOTS // 2))
        self._lead_panic = max(1, self._lead_target // 4)

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
        flowing, so the write head stays a full ``lead`` ahead."""
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

        self._running = True
        self._writer = threading.Thread(target=self._writer_loop, name="asid-ring", daemon=True)
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
        both call it). Returns True once armed."""
        with self._lock:
            if self._armed:
                return True
            if self._q.qsize() < self._prebuffer_target:
                return False
            n = 0
            while n < self._prebuffer_target:
                try:
                    slot = self._q.get_nowait()
                except queue.Empty:
                    break
                if len(slot) == self.slot_size:
                    self._write_slot_at(n, slot)
                    n += 1
            self.api.flush()
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

    def set_frame_rate(self, frame_rate_hz: float) -> None:
        """Retune the consume rate on a ``0x31`` change: reprogram CIA #1 and
        re-anchor the read head (freezing the current consumed estimate) so the
        absolute-slot alignment with ``_write_pos`` is preserved.

        Works **before arming too** — a ``0x31`` almost always arrives at stream
        start, before the prebuffer has filled, and if it were dropped the player
        would arm at the wrong (initial video-rate) cadence and silently decimate
        the tune to that rate. Pre-arm it just retunes the CIA latch + rebuilds
        the handler's tick divider so the correct rate takes effect the moment
        the vector swaps; post-arm it also re-anchors the running read head."""
        latch = cia1_latch_for_rate(frame_rate_hz, self.system)
        rate = actual_rate_for_latch(latch, self.system)
        divider = tick_divider_for_rate(rate)
        armed = self._armed
        with self._lock:
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
            # Safe to rebuild the handler in place — its vector isn't hooked yet —
            # so the tick divider matches the real rate before it starts running.
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
        chip-count change; briefly disarms and re-arms (rare, ~once per tune)."""
        n_chips = max(1, n_chips)
        if n_chips == self.n_chips:
            return
        rate = self._rate
        self._teardown_player()
        # Drain any queued slots sized for the old layout.
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self.n_chips = n_chips
        self.slot_size = slot_size_for_chips(n_chips)
        self._write_pos = 0
        self.start(rate)

    # ---- writer loop ------------------------------------------------------
    def _writer_loop(self) -> None:
        # Phase 1: wait for a real-frame prebuffer, then arm (start the read-head
        # clock + swap $0314). See start()/_try_arm for why we don't arm eagerly.
        while self._running and not self._armed:
            if not self._try_arm():
                time.sleep(0.005)
        # Phase 2: steady state — keep the write head a `lead` ahead of the read.
        while self._running:
            read_head = self._read_head()
            lead = self._write_pos - read_head
            self._lead_min = lead if self._lead_min < 0 else min(self._lead_min, lead)
            self._lead_max = max(self._lead_max, lead)
            deficit = self._lead_target - lead
            if deficit <= 0:
                time.sleep(0.002)
                continue
            # Gather up to `deficit` real slots without blocking. Drop any slot
            # whose size doesn't match the current layout — a chip-count reinit
            # can leave a straggler of the old size in flight (see reinit()).
            slots: list[bytes] = []
            for _ in range(deficit):
                try:
                    slot = self._q.get_nowait()
                except queue.Empty:
                    break
                if len(slot) == self.slot_size:
                    slots.append(slot)
            if slots:
                self._real_written += len(slots)
            else:
                # Producer momentarily empty. Only pad NEUTRAL (a hold) once the
                # lead has actually drained to the panic watermark — otherwise
                # just wait for the producer (no glitch). Holds make the SID hold
                # its last state (no echo).
                if lead > self._lead_panic:
                    try:
                        slots.append(self._q.get(timeout=0.02))
                        self._real_written += 1
                    except queue.Empty:
                        continue
                else:
                    slots.append(hold_slot(self.slot_size))
                    self._underrun_pads += 1
            self._write_slots(self._write_pos, slots)
            self._write_pos += len(slots)

    def _write_slot_at(self, slot_index: int, slot: bytes) -> None:
        pos = (slot_index % RING_SLOTS) * self.slot_size
        self.api.reu_write(self.ring_base + pos, slot)

    def _write_slots(self, start_index: int, slots: list[bytes]) -> None:
        """REUWRITE consecutive slots starting at absolute ``start_index``,
        splitting into runs that don't cross the ring wrap so each run is one
        contiguous transfer."""
        i = 0
        n = len(slots)
        while i < n:
            ring_slot = (start_index + i) % RING_SLOTS
            run = min(n - i, RING_SLOTS - ring_slot)
            payload = b"".join(slots[i : i + run])
            self.api.reu_write(self.ring_base + ring_slot * self.slot_size, payload)
            i += run

    # ---- shutdown ---------------------------------------------------------
    def _teardown_player(self) -> None:
        """Stop the writer + disarm the C64 IRQ (restore $0314 + CIA #1 latch).
        Idempotent; leaves the SID untouched (the scene silences it)."""
        self._running = False
        if self._writer is not None:
            self._writer.join(timeout=1.0)
            self._writer = None
        if not self._armed:
            return
        try:
            # Vector restore FIRST so the next kernal IRQ doesn't fire into a
            # handler we're dismantling, then CIA #1 latch back to the kernal
            # default so jiffy/SCNKEY resume at ~60 Hz.
            self.api.write_regs(
                f"{VECTORS.IRQ:04X}", KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF
            )
            latch = KERNAL_CIA1_TIMER_A_LATCH_NTSC
            self.api.write_memory(
                f"{CIA1.TIMER_A_LO:04X}", f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"
            )
            self.api.flush()
        except Exception as e:  # best-effort; teardown must not raise
            log.debug("asid_player: disarm write failed: %s", e)
        self._armed = False

    def stop(self) -> None:
        self._teardown_player()
        log.info(
            "asid_player: pushed=%d real_written=%d holds=%d dropped_full=%d",
            self._pushed,
            self._real_written,
            self._underrun_pads,
            self._dropped_full,
        )
        if self._lead_min >= 0:
            log.info(
                "asid_player: write-ahead lead min=%d max=%d slots (target=%d)",
                self._lead_min,
                self._lead_max,
                self._lead_target,
            )
