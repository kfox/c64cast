"""Host-side SID register tracker driven by a pure-Python 6502 emulator.

Why this exists: the U64's FPGA SID is faithful to real hardware — SID
I/O is write-only and reads of $D400-$D418 return open-bus zeros. The
Socket DMA protocol has no read-mem opcode either, so there's no path
to recover live register state from the U64. WaveformScene needs a
25-byte snapshot of $D400-$D418 every frame to drive
[SIDEmulator.update_registers](sidemu.py) for its oscilloscope trace.

Fix: run the same SID file in parallel on a host-side 6502 emulator
([py65](https://github.com/mnaberez/py65), pure Python). Trap writes
to $D400-$D418 into a 25-byte shadow that `regs()` returns. Audio still
plays on the real U64 SID; the host emulator's would-be audio output
is discarded — only the register-write log matters.

The host emulator is loosely coupled to the U64: it has to be ticked at
the rate the tune's PLAY is *really* being called at, which is not the
video frame rate. The C64-side player chains PLAY onto the kernal's
CIA #1 Timer A jiffy IRQ, which the KERNAL programs to ~60 Hz on BOTH
standards ([C64Backend.sid_vsync_play_rate_hz](../hw/backend.py)), and a
CIA-timed multispeed tune runs PLAY at a multiple of that
([play_rate_hz]). WaveformScene reads the first, probes for the second,
and catches the emulator up to wall-clock each poll — assuming a nominal
50/60 Hz instead is what left the scope drifting progressively behind
the audio (a voice's trace staying flat for a beat).

Validation (RSID/load_addr/play_addr) is delegated to
[parse_psid_for_player](api.py) so SidHostEmu refuses the same SIDs
[Ultimate64API.run_sid_player](api.py) refuses — config errors surface
identically regardless of which path reports them first.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from py65.devices.mpu6502 import MPU

from c64cast.hw.api import parse_psid_for_player
from c64cast.hw.c64 import CIA1, CPU, RESERVED_IO_WINDOWS, ROM, SCREEN, SID, VIC_BANK_0

from .sidemu import SID_REG_COUNT

log = logging.getLogger(__name__)

# Sentinel return address pushed onto the 6502 stack before each
# JSR-equivalent into INIT/PLAY. The 6502 RTS pulls a word and adds 1,
# so pushing $FEFF yields PC=$FF00 after the final RTS — we step until
# we see that PC value and treat it as "the called routine returned".
# $FF00 itself reads as $60 (RTS) from the ROM-stub fill below, so a
# routine that reaches the sentinel one step late just returns again.
_SENTINEL_PUSH = 0xFEFF
_SENTINEL_PC = 0xFF00

# Safety net: any JSR into the ROM-mapped region (BASIC $A000-$BFFF,
# kernal $E000-$FFFF, etc.) lands on a byte we control. We fill the
# whole $A000-$FFFF range with $60 (RTS) so any such call returns
# immediately without infinite-looping the emulator.
_ROM_FILL_LO = 0xA000
_ROM_FILL_BYTE = 0x60

# Hardware-vector slots inside the ROM fill. Point IRQ/NMI/RESET at
# $A000 (which is $60 RTS) so a stray BRK doesn't enter an infinite
# BRK-loop through an unset $FFFE vector.
_VEC_NMI = 0xFFFA
_VEC_RESET = 0xFFFC
_VEC_IRQ = 0xFFFE
_RTS_TARGET = _ROM_FILL_LO  # any RTS-filled address works

# Per-call cycle caps, split by routine because their cost profiles differ.
#
# PLAY runs every frame, so its cap must stay tight: it bounds an infinite
# loop in a degenerate PLAY (e.g. a "wait for raster" spin) to ~50 k cycles
# (~4 ms of host CPU at ~11 M cyc/s) so the render thread isn't starved.
# Typical PLAY is ~2k-20k cycles, so 50 k is ~3x headroom.
#
# INIT runs only once per tune (scene setup, a SHIFT cycle, or a footprint
# pass), so its cap can be far more generous. Some INITs do heavy one-time
# work — Galway's Times of Lore copies a ~4 KB block per subtune (~65 k
# cycles), well past the old shared 50 k cap, which truncated the copy and
# left the host emu's SID state uninitialized: a flat scope, a false
# "silent" end-of-tune trip, and (in single-scene mode) a needless full
# reload. 2 M cycles (~170 ms one-time at ~11 M cyc/s) covers fat
# decompressors with room to spare while still bounding a degenerate
# raster-waiting INIT to a one-time stall.
_PLAY_CYCLE_CAP = 50_000
_INIT_CYCLE_CAP = 2_000_000

# Emulated cycles are not on their own a sound budget: py65 binds all 105
# undocumented opcodes to `inst_not_implemented` with a cycletime of 0, so a
# loop built out of them spends real host time and never advances
# `processorCycles`. A crafted .sid exploited exactly that — one measured
# tick_play() burned 7-21 s against a cap the comment above budgets at ~4 ms.
# _run_routine therefore bounds *interpreter steps* as well, and ends the pass
# at an unimplemented opcode rather than executing it (see
# _ILLEGAL_OPCODE_CYCLETIME). Every implemented 6502 opcode costs at least 2
# cycles, so a step budget equal to the cycle cap can never bind on a routine
# the cycle cap wouldn't have caught first — it exists so termination does not
# depend on the cycle accounting being honest.
#
# The per-step opcode test costs ~3% of tick_play (measured 0.126 → 0.130 ms
# for a 64-iteration table-copy PLAY), well inside the ~0.2 ms/pass the poll
# thread's catch-up budget already assumes.
_ILLEGAL_OPCODE_CYCLETIME = 0

# Wall clock is checked every this many interpreter steps inside a routine.
# The step cap alone bounds a routine's *steps*, not its seconds, and INIT's
# cap is 2 M of them. 4096 steps is ~4 ms of py65 at the ~1 M steps/s this
# interpreter manages, so the overshoot past a deadline is bounded at roughly
# one video frame while the check itself costs one modulo per step.
_WALL_CLOCK_CHECK_STEPS = 4096

# Wall-clock ceiling on a single INIT. _INIT_CYCLE_CAP bounds emulated cycles,
# which is not time: 2 M cycles of the most expensive legal 2-cycle opcode
# measured 0.68 s, and a routine built from opcodes py65 charges 0 cycles for
# spends time without advancing the cycle count at all. Every SidHostEmu runs
# INIT in its constructor, and a tune's analysis constructs up to
# 2 + _UNIFIED_LAYOUT_MAX_SONGS of them, so an unbounded INIT multiplied
# straight through the aggregate budget below. Analysis runs pass a
# HostEmuBudget and get the smaller of this and what is left of it.
_INIT_DEADLINE_S = 1.0

# Wall-clock ceiling on a single PLAY pass, applied only on the paths that
# *sample* one — footprint runs and the pre-flight, both of which already
# distrust a truncated pass. A pass there gets the smaller of this and what is
# left of the caller's HostEmuBudget.
#
# Holding a budget is not the test, and reading it as one is how the PLAY-rate
# probe came to be deadlined: it holds a budget too, and prices a pass rather
# than sampling it, so it deliberately runs its pass to completion — past
# `budget.deadline` if a nearly-spent budget let one start. Overrunning a
# budget by one bounded pass is the cheaper error; see [detect_play_rate_hz].
#
# The live render path deliberately gets no wall-clock cap. Truncating a PLAY
# there leaves the $D4xx shadow holding half a frame's writes — a visibly wrong
# scope — and a scheduler hiccup landing inside a pass would do it for a tune
# that is perfectly healthy. What bounds the render path instead is the poll
# period, sized against a measured pass cost; see [sustainable_poll_period_s].
#
# 50 ms is ~3x the most expensive *legal* PLAY measured (a pass that stays just
# inside _PLAY_CYCLE_CAP costs ~16 ms), so it binds on degenerate passes only.
_PLAY_DEADLINE_S = 0.05

# Default number of PLAY passes to run when profiling a tune's RAM write
# footprint. ~2000 ticks ≈ 33 s of tune time at 60 Hz; footprints observed to
# stabilize well before 1000 ticks. The footprint places the relocated
# C64-side player in RAM the tune never writes (see [ram_write_footprint] +
# api._find_free_layout).
FOOTPRINT_TICKS = 2000

# Wall-clock budget for ONE footprint run. A cheap PLAY finishes all 2000
# passes in ~0.5 s, but nothing about the per-pass cap bounds the run: a PLAY
# that legally burns just under _PLAY_CYCLE_CAP costs 2000 x 50 k emulated
# cycles, measured at ~12 s for a 172-byte crafted PSID.
FOOTPRINT_DEADLINE_S = 2.0

# Wall-clock budget for the WHOLE of one tune's analysis — every footprint run
# and every INIT it constructs, together. The per-run deadline above bounds one
# call; it does not bound the call count, and the counts are set by the file:
# WaveformScene.setup pays two footprint runs plus one per subtune (up to
# _UNIFIED_LAYOUT_MAX_SONGS), and a SHIFT press pays one per candidate (up to
# _MAX_CYCLE_CANDIDATES). A 306-byte PSID declaring 16 subtunes measured 43 s
# of blocked main thread that way, because each of the 18 runs drew a fresh
# 2 s. Callers thread one HostEmuBudget through the whole walk instead, so the
# multiplication is bounded once.
#
# 6 s leaves an ordinary 16-subtune tune (~0.25 s per run) and Galway's Times
# of Lore (11 subtunes) finishing well inside it, while a tune whose PLAY is
# expensive enough to need more gives up the parts that are optional — the
# unified display-bank pin, the later SHIFT candidates — rather than the show.
ANALYSIS_BUDGET_S = 6.0

# PLAY passes a rate probe may run before it settles for the video rate. A
# multispeed tune's CIA #1 Timer A latch is often written by the first PLAY
# rather than by INIT (Galway's Times of Lore does exactly that), so the true
# rate is unknowable until PLAY has run at least once. 64 passes is ~1 s of
# song at 60 Hz and a few ms on a throwaway emulator; the count bounds no
# duration, so the caller's HostEmuBudget bounds the seconds.
RATE_PROBE_TICKS = 64

# What one PLAY pass is assumed to cost when the probe never got to time one.
# A pass that stays just inside _PLAY_CYCLE_CAP measured ~16 ms, so this is the
# worst a *legal* pass can cost. It is a fail-safe default, not an inference: a
# spent budget says this tune's analysis was expensive (its INIT, its footprint
# runs), which is only correlated with an expensive PLAY. What decides the
# direction is that the two errors are not symmetric -- assuming free saturates
# a core silently, assuming expensive slows the wakeups visibly and says so.
#
# It is worth being explicit that this charge always bites: against the
# catch-up threads' 0.5 fraction it asks for a 32 ms period, above both the
# 16.7 ms NTSC and 20 ms PAL vsync periods, so an unmeasured tune polls at
# ~31 Hz and warns. That is the intended cost of not knowing, and it is not a
# state either caller can currently reach -- a probe's budget is 6 s and the
# most its two INITs can spend before the first pass is 2 (`_INIT_DEADLINE_S`
# caps each at 1), measured. The branch is a fail-safe for a state the code can
# express, not a live path.
UNMEASURED_PASS_COST_S = 0.016


class HostEmuBudget:
    """One wall-clock budget shared by every host-emulation run a single tune's
    analysis performs — footprint passes and the INITs between them alike.

    A budget is an absolute instant, not a duration, so passing the same object
    down a scan loop is what makes the loop's cost bounded once instead of once
    per call. Construct it *before* the first `SidHostEmu`, since construction
    runs INIT and INIT is the part no per-pass cap was ever bounding.

    `clock` is injected so the arithmetic is testable without sleeping.
    """

    __slots__ = ("deadline", "_clock")

    def __init__(
        self,
        seconds: float = ANALYSIS_BUDGET_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self.deadline = clock() + seconds

    def remaining(self) -> float:
        """Seconds left, negative once the budget is blown."""
        return self.deadline - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def deadline_for(self, seconds: float) -> float:
        """The instant one run may not pass: its own per-run cap or what is
        left of the shared budget, whichever comes first."""
        return min(self.deadline, self._clock() + seconds)

    def now(self) -> float:
        return self._clock()


# ---------------------------------------------------------------------------
# SID file structural helpers
#
# Pure (or footprint-only) helpers that parse / reason about a SID file's
# layout, shared by WaveformScene (the oscilloscope) and SidFileAudioSource
# (the composable audio building block). They live here — next to the
# footprint functions and parse_psid_for_player — rather than in waveform.py
# so that audio_source can use them without dragging in the oscilloscope
# renderer (numpy / voice_scope). waveform.py re-exports them for back-compat.
# ---------------------------------------------------------------------------


@dataclass
class SidHeader:
    magic: str
    version: int
    num_songs: int
    start_song: int
    name: str
    author: str
    released: str
    # Decoded PSID v2+ flags. None on v1 headers (no flags field).
    clock: str | None  # "PAL", "NTSC", "PAL+NTSC", "?" or None
    sid_model: (
        str | None
    )  # "6581", "8580", "6581+8580", "?" or None — chip 0 only, == sid_models[0]
    # $Dxxx base address of every SID chip the tune drives, chip 0 first
    # (always $D400). A single-SID tune yields (0xD400,); a 3SID tune yields
    # e.g. (0xD400, 0xD420, 0xD440). Parsed from the PSID v3/v4 second/third
    # SID-address bytes ($7A/$7B). See parse_sid_header.
    sid_addresses: tuple[int, ...] = (SID.BASE,)
    # Per-chip model, same length/order as sid_addresses (sid_models[0] ==
    # sid_model). An entry is None when that chip's model bits aren't
    # trustworthy — the header version/address-byte gating that makes the
    # chip's own address entry exist is exactly what makes its model bits
    # meaningful; see parse_sid_header.
    sid_models: tuple[str | None, ...] = (None,)


def _overlaps(lo: int, hi: int, region_lo: int, region_size: int) -> bool:
    """True when [lo, hi) overlaps the region [region_lo, region_lo+size)."""
    region_hi = region_lo + region_size
    return lo < region_hi and hi > region_lo


# PSID v2+ flags field (2 bytes, big-endian, at offset 0x76-0x77) layout:
# clock is bits 2-3 and sidModel1 is bits 4-5, both in the LOW byte (0x77).
# sidModel2 (bits 6-7) is *also* in the low byte, alongside model1 — only
# sidModel3 (bits 8-9, i.e. bits 0-1 of the HIGH byte 0x76) lives in the
# high byte. (A prior version of this comment claimed model2 lived in the
# high byte too — wrong; only model3 does.)
_CLOCK_TABLE = {0: "?", 1: "PAL", 2: "NTSC", 3: "PAL+NTSC"}
_MODEL_TABLE = {0: "?", 1: "6581", 2: "8580", 3: "6581+8580"}

# PSID extra-SID address bytes: secondSIDAddress at $7A (v3+), thirdSIDAddress
# at $7B (v4+). The byte encodes the middle nibble of a $Dxx0 base: address =
# $D000 | (byte << 4), so 0x42 → $D420, 0x50 → $D500, 0xE0 → $DE00. Zero means
# "no chip". The spec only permits even bytes resolving to the $D420-$D7E0 and
# $DE00-$DFE0 windows, and that is what we enforce — a byte outside them is
# malformed and degrades to single-SID.
#
# The check used to be `$D000 <= addr <= $DFF0`, which is what the arithmetic
# already guarantees for every byte 1..255: no byte was ever rejected, so a
# header field chose a DMA write target anywhere in the I/O page. $7A = 0xC0
# put a "SID" on CIA #1, and WaveformScene.teardown's 25-byte zero write there
# stops the jiffy IRQ and the keyboard scan until a physical reset. The bases
# also reach TrappedRam._addr_map, where a duplicate or overlapping window
# silently shadows a lower chip's registers (see _append_distinct_sid_base).
_SECOND_SID_ADDR = 0x7A
_THIRD_SID_ADDR = 0x7B
_EXTRA_SID_WINDOWS = ((0xD420, 0xD7E0), (0xDE00, 0xDFE0))

# I/O the spec's $DE00-$DFE0 "cartridge" window permits a chip on but c64cast
# drives itself, and so must not let a header aim register writes at
# (c64cast.hw.c64.RESERVED_IO_WINDOWS, shared with the multi-SID planner that
# realizes these same bases on the U64's UltiSID cores). WaveformScene.teardown
# writes 25 zero bytes at every declared base: over the REU's command registers
# ($DF00-$DF0A) that hits $DF02/$DF03, the running C64 destination pointer the
# audio ring's NMI handler reads back mid-transfer, pointing the DMA at $0000;
# over the Ultimate Audio sampler's page ($DF20-$DFFF) it walks a channel's
# control/volume/start/length file while that channel is playing the session's
# video audio. The rule is a window overlap against those devices' own address
# constants rather than a list of excluded literals, so it stays right if
# either moves.


def _decode_extra_sid_addr(byte: int) -> int | None:
    """Decode a PSID extra-SID address byte to a $Dxx0 base, or None when the
    byte is 0 (absent) or is not one the PSID spec permits: odd bytes and
    anything resolving outside the $D420-$D7E0 / $DE00-$DFE0 windows are
    refused, which also excludes chip 0's own $D400. A base whose 25-byte
    register window would reach hardware c64cast drives itself is refused too
    — see RESERVED_IO_WINDOWS."""
    if byte == 0 or byte & 1:
        return None
    addr = 0xD000 | (byte << 4)
    if addr == SID.BASE:
        return None
    if not any(lo <= addr <= hi for lo, hi in _EXTRA_SID_WINDOWS):
        return None
    if any(
        _overlaps(addr, addr + SID_REG_COUNT, lo, hi - lo + 1) for lo, hi in RESERVED_IO_WINDOWS
    ):
        return None
    return addr


def _append_distinct_sid_base(addresses: list[int], addr: int) -> bool:
    """Append `addr` to `addresses` unless its 25-byte register window touches
    one already accepted. Returns True when appended.

    TrappedRam builds its absolute-address → (bank, offset) map as a dict
    comprehension over the bases, so a later bank wins every colliding key: a
    duplicate base leaves the earlier chip's shadow permanently all-zero (its
    scope window stays flat while the audience hears the chip), and a partial
    overlap steals just the shared registers. Refusing the collision here keeps
    every chip's window distinct for every consumer, not just the trap."""
    if any(abs(addr - existing) < SID_REG_COUNT for existing in addresses):
        return False
    addresses.append(addr)
    return True


def parse_sid_header(data: bytes) -> SidHeader:
    """Parse the PSID/RSID v1+ header. Validates magic, returns metadata.

    Reads the v2+ flags field at offset 0x76-0x77 (2 bytes, big-endian) to
    surface SID chip model(s) + PAL/NTSC clock. v1 headers (length 118)
    leave clock/sid_model/sid_models as None. On PSID v3/v4 headers, reads
    the second/third SID-address bytes ($7A/$7B) into `sid_addresses` (chip
    0 = $D400 first), and gates model2/model3 on those same address bytes
    being present — a chip's model is only trusted if the header actually
    declares that chip exists (mirrors the firmware's ConfigSIDs)."""
    if len(data) < 22:
        raise ValueError("SID file too short to contain a header")
    magic = data[:4]
    if magic not in (b"PSID", b"RSID"):
        raise ValueError(f"not a SID file (expected PSID/RSID magic, got {magic!r})")
    version = int.from_bytes(data[4:6], "big")
    clock: str | None = None
    model1: str | None = None
    model2: str | None = None
    model3: str | None = None
    if version >= 2 and len(data) >= 0x78:
        flags = (data[0x76] << 8) | data[0x77]
        clock = _CLOCK_TABLE[(flags >> 2) & 0x03]
        model1 = _MODEL_TABLE[(flags >> 4) & 0x03]
        if version >= 3:
            model2 = _MODEL_TABLE[(flags >> 6) & 0x03]
        if version >= 4:
            model3 = _MODEL_TABLE[(flags >> 8) & 0x03]
    # Extra SID chips (v3 adds a 2nd, v4 a 3rd). Chip 0 is always $D400.
    addresses = [SID.BASE]
    models = [model1]
    if version >= 3 and len(data) > _SECOND_SID_ADDR:
        second = _decode_extra_sid_addr(data[_SECOND_SID_ADDR])
        if second is not None and _append_distinct_sid_base(addresses, second):
            models.append(model2)
    if version >= 4 and len(data) > _THIRD_SID_ADDR and len(addresses) == 2:
        third = _decode_extra_sid_addr(data[_THIRD_SID_ADDR])
        if third is not None and _append_distinct_sid_base(addresses, third):
            models.append(model3)
    return SidHeader(
        magic=magic.decode("ascii"),
        version=version,
        num_songs=int.from_bytes(data[14:16], "big"),
        start_song=int.from_bytes(data[16:18], "big"),
        name=data[22:54].rstrip(b"\x00").decode("latin-1", "replace"),
        author=data[54:86].rstrip(b"\x00").decode("latin-1", "replace"),
        released=data[86:118].rstrip(b"\x00").decode("latin-1", "replace")
        if len(data) >= 118
        else "",
        clock=clock,
        sid_model=model1,
        sid_addresses=tuple(addresses),
        sid_models=tuple(models),
    )


# Filename fallback for tunes whose header understates the SID count (or
# predates v3/v4): HVSC names multi-SID files "..._<N>SID.sid". Only ever used
# to *raise* the header count, never lower it. Clamped to 2-8.
_FILENAME_SID_COUNT_RE = re.compile(r"(?i)(\d)sid\.sid$")
# Stride between synthesized canonical chip bases when the count comes from the
# filename but the header gives no addresses ($D400, $D420, $D440, ...). Matches
# plan_sid_map's default UltiSID split stride and the most common HVSC layout.
_CANONICAL_SID_STRIDE = 0x20
_MAX_SIDS = 8
# How many canonical slots ($D400, $D420, ...) the filename fallback may walk
# looking for one the header hasn't already claimed. Generous enough that
# _MAX_SIDS chips always fit even when every header address collides.
_CANONICAL_SLOT_LIMIT = 2 * _MAX_SIDS


def detect_sid_addresses(path: str | None, data: bytes) -> tuple[int, ...]:
    """The $Dxxx base of every SID chip a tune drives, chip 0 ($D400) first.

    Authoritative source is the PSID v3/v4 header (`SidHeader.sid_addresses`). A
    filename ``_<N>SID.sid`` hint can *raise* the count above what the header
    declares (the ambiguous case of a v1/v2 header that can't declare extra
    chips) — the extra chips get canonical stride-$20 bases appended, since the
    filename carries no address info. Never lowers the header's count. The
    result has 1..8 entries; a plain single-SID tune yields ``($D400,)``."""
    try:
        addresses = list(parse_sid_header(data).sid_addresses)
    except ValueError:
        addresses = [SID.BASE]
    if path is not None:
        m = _FILENAME_SID_COUNT_RE.search(path)
        if m:
            want = min(int(m.group(1)), _MAX_SIDS)
            # Walk canonical slots rather than counting them: a header address
            # can already sit on one (a "_3SID" name over a v3 header
            # declaring $D440 used to synthesize a second $D440), and a base
            # repeated in the tuple silently kills the earlier chip's shadow.
            for slot in range(_CANONICAL_SLOT_LIMIT):
                if len(addresses) >= want:
                    break
                _append_distinct_sid_base(addresses, SID.BASE + slot * _CANONICAL_SID_STRIDE)
    return tuple(addresses[:_MAX_SIDS])


def _sid_payload_extent(sid_bytes: bytes) -> tuple[int, int]:
    """Return (load_addr, end_addr_exclusive) for the SID's payload bytes
    once loaded on the C64. Mirrors the load-address handling in
    `api.parse_psid_for_player` without re-running its full validation —
    used to refuse tunes whose payload would clobber a scene's display
    regions. Assumes the SID header has already been validated (magic +
    minimum length) by `parse_sid_header`."""
    data_offset = int.from_bytes(sid_bytes[6:8], "big")
    load_addr = int.from_bytes(sid_bytes[8:10], "big")
    payload = sid_bytes[data_offset:]
    if load_addr == 0 and len(payload) >= 2:
        load_addr = payload[0] | (payload[1] << 8)
        payload = payload[2:]
    return load_addr, load_addr + len(payload)


def _play_bank_for_footprints(
    write_fp: bytes | bytearray, access_fp: bytes | bytearray
) -> int | None:
    """Return the $01 CPU-port override the player should use around JSR play,
    or None to let api.run_sid_player's address-keyed heuristic decide.

    The heuristic banks on the play *address* page, but a tune can read its
    live song data from RAM under BASIC ROM ($A000-$BFFF) while its code sits
    below it — Galway's Times of Lore subtunes 2-11 copy per-song data to
    $B400 at INIT and read it back every PLAY. With the default $37 (BASIC
    mapped) PLAY reads ROM there instead of the data → silence. We return
    $36 (BASIC out) when PLAY reads an address under BASIC ROM that the tune
    also *wrote* — proof it's RAM data, not the ROM itself. A tune that reads
    BASIC ROM *as data* (e.g. Galway's Comic Bakery table) writes nothing
    there, so the intersection is empty and we keep $37."""
    # Pure-Python intersection over the BASIC-ROM window with an early exit
    # (avoids importing numpy into this otherwise-light module — see the
    # section header). The window is only 8 KB and a hit usually lands early.
    for addr in range(ROM.BASIC_LO, ROM.BASIC_HI):
        if write_fp[addr] and access_fp[addr]:
            return CPU.PORT_BASIC_OUT
    return None


def payload_overlaps_bank0_display(
    sid_bytes: bytes, *, is_bitmapped: bool
) -> tuple[int, int] | None:
    """Return the conflicting display region (lo, hi exclusive) when the SID
    payload would clobber a VIC bank-0 display, else None.

    A `SourceScene`'s display mode is hardwired to VIC bank 0 and — unlike
    WaveformScene — cannot relocate. Char modes (`is_bitmapped=False`) reserve
    only screen RAM at $0400; bitmap modes also reserve the hires bitmap at
    $2000. Color RAM ($D800) is I/O space, never main RAM, so a payload can't
    overlap it. The caller refuses a SID whose payload extent hits either
    region (most HVSC tunes load at $1000 with multi-KB payloads, so bitmap
    displays frequently conflict — char modes are the robust pairing)."""
    payload_lo, payload_hi = _sid_payload_extent(sid_bytes)
    regions = [(VIC_BANK_0.SCREEN, SCREEN.N_CELLS)]
    if is_bitmapped:
        regions.append((VIC_BANK_0.BITMAP, SCREEN.BITMAP_BYTES))
    for region_lo, region_size in regions:
        if _overlaps(payload_lo, payload_hi, region_lo, region_size):
            return region_lo, region_lo + region_size
    return None


class TrappedRam:
    """64 KB RAM array that py65's MPU(memory=...) speaks to via plain
    `self.memory[addr]` / `self.memory[addr] = val` (verified in the
    py65 source: every read goes through MPU.ByteAt → self.memory[addr],
    every write is a direct subscript-store).

    Writes to any configured SID chip's registers ($D400-$D418, plus each
    extra chip's $Dxx0 bank on a multi-SID tune) land in both the RAM array AND
    that chip's 25-byte `sid_shadows[bank]` buffer, which the scene reads via
    SidHostEmu.regs(bank). Single-SID (the default `sid_bases=($D400,)`) shadows
    only $D400 — byte-identical to the prior $D400-only trap.

    `gate_low_banks[bank][v]` is set whenever a write clears chip `bank` voice
    v's gate bit.
    SID players retrigger a note with a "hard restart": gate off then on,
    often within a single PLAY call. The 25-byte shadow keeps only the
    final write, so such a retrigger would read as gate-still-high (no
    edge) and a plucked (sustain=0) voice would never re-attack — its
    scope strip going flat. SidHostEmu.retriggers() reads this to recover
    those intra-tick retriggers. Reset per tick by SidHostEmu.tick_play.

    When `track_footprint` is set, every write also marks `footprint[addr]`
    so a throwaway run can report which RAM the tune touches (used by
    [ram_write_footprint] to place the C64-side player in RAM the tune
    demonstrably never writes). `footprint` is None on the normal scope
    path so the hot write path costs only one `is not None` test.

    When `track_access` is set, every read AND write marks `access[addr]` —
    a stricter footprint that also catches RAM the tune merely *reads*. The
    display-bank choice needs this: a tune that copies per-song data into a
    VIC bank at INIT and reads it back during PLAY (e.g. Galway's Times of
    Lore at $B400) would be invisible to the write-only footprint, so the
    bitmap would clobber live song data. See [ram_play_access_footprint].
    `access` is None on the normal scope path (one `is not None` test).
    """

    __slots__ = (
        "ram",
        "sid_bases",
        "sid_shadows",
        "_addr_map",
        "footprint",
        "access",
        "gate_low_banks",
        "cia1_timer_a_written",
    )

    # Voice control-register offsets within a SID (gate bit lives here).
    _CONTROL_OFFSETS = frozenset(
        v * SID.BYTES_PER_VOICE + SID.OFF_CONTROL for v in range(SID.N_VOICES)
    )
    # CIA #1 Timer A latch bytes — a CIA-timed (multispeed) tune writes
    # these from INIT to set its PLAY call rate; see SidHostEmu.play_rate_hz.
    _CIA1_TIMER_A = frozenset((CIA1.TIMER_A_LO, CIA1.TIMER_A_HI))

    def __init__(
        self,
        track_footprint: bool = False,
        track_access: bool = False,
        sid_bases: tuple[int, ...] = (SID.BASE,),
    ) -> None:
        self.ram = bytearray(65536)
        # Fill ROM-mapped region with $60 (RTS) so any unexpected JSR
        # into BASIC/kernal space returns cleanly.
        for i in range(_ROM_FILL_LO, 0x10000):
            self.ram[i] = _ROM_FILL_BYTE
        # Point IRQ/NMI/RESET vectors at an RTS — defensive against BRK.
        for vec in (_VEC_NMI, _VEC_RESET, _VEC_IRQ):
            self.ram[vec] = _RTS_TARGET & 0xFF
            self.ram[vec + 1] = (_RTS_TARGET >> 8) & 0xFF
        # One 25-byte register shadow per SID chip the tune drives, plus an
        # absolute-address → (bank, offset) lookup so the write trap routes each
        # $Dxxx write to the right chip in one dict.get. Single-SID (the default)
        # is byte-identical to the old $D400-only path.
        self.sid_bases = sid_bases
        self.sid_shadows = [bytearray(SID_REG_COUNT) for _ in sid_bases]
        self._addr_map: dict[int, tuple[int, int]] = {
            base + off: (bank, off)
            for bank, base in enumerate(sid_bases)
            for off in range(SID_REG_COUNT)
        }
        # Per-(chip, voice) "gate cleared during this tick" flags (hard-restart
        # detection). Reset each tick by SidHostEmu.tick_play.
        self.gate_low_banks = [bytearray(SID.N_VOICES) for _ in sid_bases]
        # 64 KB write-footprint bitmap (1 = written at least once), or None
        # when footprint tracking is disabled (the normal scope path).
        self.footprint: bytearray | None = bytearray(65536) if track_footprint else None
        # 64 KB read+write access bitmap (1 = read or written at least once),
        # or None when access tracking is disabled. See [ram_play_access_footprint].
        self.access: bytearray | None = bytearray(65536) if track_access else None
        # Set once a tune writes CIA #1 Timer A — the signal that it's
        # CIA-timed (multispeed) rather than vsync. See play_rate_hz.
        self.cia1_timer_a_written = False

    def __getitem__(self, addr: int) -> int:
        # Wrap at 64 KB the way the real 6510's address bus does. py65's
        # MPU.WordAt(addr) reads addr+1 without masking, so a routine that
        # executes a 3-byte absolute-addressing opcode at $FFFE asks for
        # index $10000 — which a bytearray answers with IndexError, not a
        # byte. That escaped _run_routine, escaped the footprint helpers, and
        # unwound past the ValueError-only handlers in the SID pool pickers,
        # so one crafted 133-byte file ended the whole playlist.
        addr &= 0xFFFF
        if self.access is not None:
            self.access[addr] = 1
        return self.ram[addr]

    def __setitem__(self, addr: int, val: int) -> None:
        addr &= 0xFFFF
        self.ram[addr] = val
        if self.footprint is not None:
            self.footprint[addr] = 1
        if self.access is not None:
            self.access[addr] = 1
        if addr in self._CIA1_TIMER_A:
            self.cia1_timer_a_written = True
        hit = self._addr_map.get(addr)
        if hit is not None:
            bank, off = hit
            self.sid_shadows[bank][off] = val
            # A write that clears a voice's gate bit flags a (possibly
            # intra-tick) gate-low — recovered by retriggers() as a
            # hard-restart even when the shadow's final value is gate-high.
            if off in self._CONTROL_OFFSETS and not (val & SID.GATE):
                voice = (off - SID.OFF_CONTROL) // SID.BYTES_PER_VOICE
                self.gate_low_banks[bank][voice] = 1


class SidHostEmu:
    """Runs a SID file's INIT once and PLAY per `tick_play()` on a
    pure-Python 6502 (py65), trapping writes to $D400-$D418 into a
    25-byte shadow. The shadow is what WaveformScene's render thread
    consumes as a replacement for the broken-on-U64 SID read path.

    Audio still comes from the real SID on the U64 — this emulator's
    output (if any — most SID PLAYs write directly to $D4xx and produce
    no other side effects we'd hear) is discarded.

    Construction loads the payload into RAM and runs INIT. Each
    subsequent `tick_play()` runs one PLAY pass. `regs()` returns a
    snapshot of $D400-$D418 after the most recent INIT or PLAY.
    """

    def __init__(
        self,
        sid_bytes: bytes,
        song: int = 0,
        track_footprint: bool = False,
        track_access: bool = False,
        sid_bases: tuple[int, ...] | None = None,
        budget: HostEmuBudget | None = None,
    ) -> None:
        self._parsed = parse_psid_for_player(sid_bytes, song=song)
        # Every wall-clock instant this emulator compares — INIT's deadline,
        # a PLAY pass's — is read from THIS clock, which is the budget's own
        # when one was given. A deadline is only meaningful in the clock
        # domain that produced it: `budget.deadline_for` returns an instant on
        # the injected clock, and comparing that against `time.monotonic()`
        # made the two disagree about when the same budget expired. Production
        # was unaffected (the default clock IS time.monotonic), but every test
        # that injected a clock was measuring something the shipped code does
        # not do.
        self._now: Callable[[], float] = time.monotonic if budget is None else budget.now
        # SID chip bases to shadow. Default: the tune's own header addresses
        # (chip 0 = $D400). A caller (WaveformScene) may override to honor a
        # filename ``_NSID`` hint the header understates. Chip 0 always $D400.
        if sid_bases is None:
            sid_bases = parse_sid_header(sid_bytes).sid_addresses
        self.sid_bases: tuple[int, ...] = sid_bases
        self._memory = TrappedRam(
            track_footprint=track_footprint, track_access=track_access, sid_bases=sid_bases
        )
        self._mpu = MPU(memory=self._memory)
        # Set processor flags to a sane post-init state. I=1 (IRQs
        # disabled) matches what the real 6510 looks like immediately
        # after the kernal's SEI on reset; we don't model IRQs at all,
        # but it avoids any opcode looking for them.
        self._mpu.p = MPU.INTERRUPT | MPU.UNUSED
        self._mpu.sp = 0xFF
        # True when the most recent _run_routine bailed at the cycle cap
        # instead of returning to the sentinel RTS. A routine that caps
        # didn't complete — its $D4xx writes are partial/garbage. Callers
        # (WaveformScene._load_sid_file) reject tunes whose PLAY caps on
        # every tick: such a tune spins on a raster/IRQ this emulator never
        # provides, so the scope can't render it faithfully and the C64-side
        # player would hang/silence it too.
        self.last_routine_capped: bool = False
        # True once ANY routine on this emulator capped — the constructor's
        # INIT included, which is the one no later flag could ever report
        # because it ran before the caller held the object. `last_routine_capped`
        # answers "did the pass I just ran terminate", which is the PLAY
        # pre-flight's question; this answers "is anything sampled from this
        # emulator a prefix", which is FootprintSample.complete's. Reading the per-pass
        # flag for the second question let a truncated INIT — a fat
        # decompressor stopped at the 2 M-cycle cap, or at the shared budget —
        # report a full footprint, and everything after the cap was lost.
        self.any_routine_capped: bool = False
        # True once any routine on this emulator ended on an opcode py65 does
        # not implement. Distinct from `last_routine_capped` on purpose: the
        # tune runs fine on the real 6510 (LAX/SAX/SLO are a normal hand-rolled
        # player idiom), so this must NOT feed the PLAY pre-flight's "would
        # hang the machine" verdict. What it does mean is that every pass stopped at the
        # same instruction, so the RAM footprint sampled from this emulator is
        # a prefix of the truth and the placements built on it are not
        # trustworthy — see FootprintSample.complete.
        self.saw_undecodable_opcode: bool = False
        # One undocumented-opcode warning per emulator (see _run_routine).
        self._illegal_opcode_reported: bool = False
        # Load the SID payload at its declared address.
        load = self._parsed.load_addr
        self._memory.ram[load : load + len(self._parsed.payload)] = self._parsed.payload
        # Run INIT once: A = song-1, X=Y=0; call init_addr; wait for the
        # sentinel RTS, the cycle/step cap, or the wall clock. INIT is where an
        # unbounded run hides — it is the one routine whose cap is measured in
        # millions of cycles — so it gets an explicit deadline, tightened to
        # whatever is left of the caller's shared analysis budget.
        load_deadline = (
            budget.deadline_for(_INIT_DEADLINE_S)
            if budget is not None
            else self._now() + _INIT_DEADLINE_S
        )
        self._run_routine(
            self._parsed.init_addr,
            a=(self._parsed.song_to_play - 1) & 0xFF,
            cap=_INIT_CYCLE_CAP,
            tag="init",
            deadline=load_deadline,
        )

    @property
    def n_sids(self) -> int:
        """Number of SID chips this emulator shadows (>= 1)."""
        return len(self.sid_bases)

    def regs(self, bank: int = 0) -> bytes:
        """Return a 25-byte snapshot of chip `bank`'s $D4xx registers. Always
        exactly SID_REG_COUNT bytes; pre-INIT this is all zeros, post-INIT it
        reflects whatever the tune set during init, post-`tick_play` the last
        frame's writes. `bank` 0 is the primary $D400 chip (the only chip on a
        single-SID tune)."""
        return bytes(self._memory.sid_shadows[bank])

    def tick_play(self, deadline: float | None = None) -> None:
        """Run one PLAY pass. Re-entrant call into `play_addr`, same
        sentinel-RTS + budget discipline as INIT. The cycle/step caps bound a
        degenerate PLAY (one that spins waiting for a raster or an IRQ that
        will never fire in this emulator) so the render thread isn't starved.

        `deadline` is an optional absolute instant in this emulator's clock
        domain (see `_now`). The paths that *sample* a pass — footprint runs
        and the pre-flight — pass one, so no single pass outlasts the
        HostEmuBudget it is charged to; a truncated sample is one they already
        distrust. The paths that *price* or *render* a pass pass none: the live
        render path, because truncating there leaves a visibly wrong scope (see
        _PLAY_DEADLINE_S), and [detect_play_rate_hz], because a truncated pass
        priced as a whole one is a censored measurement."""
        # Clear hard-restart flags (all chips) so retriggers() reflects only
        # this tick.
        for gl in self._memory.gate_low_banks:
            gl[:] = bytes(SID.N_VOICES)
        self._run_routine(
            self._parsed.play_addr, cap=_PLAY_CYCLE_CAP, tag="play", deadline=deadline
        )

    def retriggers(self, bank: int = 0) -> tuple[bool, bool, bool]:
        """Per-voice hard-restart detection for chip `bank` on the most recent
        tick_play().

        A voice whose control register was written gate-low at some point
        during the tick but whose final shadow gate is high underwent a
        hard restart — the gate pulsed off→on within one PLAY call, a
        retrigger the 25-byte shadow alone collapses to gate-still-high.
        WaveformScene feeds this to SIDEmulator.update_registers so plucked
        (sustain=0) leads re-attack on every note instead of flatlining
        after their first decay. Voices that ended gate-low are ordinary
        note-offs handled by the shadow's gate edge, so they're excluded."""
        gl = self._memory.gate_low_banks[bank]
        shadow = self._memory.sid_shadows[bank]
        result = []
        for v in range(SID.N_VOICES):
            final_gate = bool(shadow[v * SID.BYTES_PER_VOICE + SID.OFF_CONTROL] & SID.GATE)
            result.append(bool(gl[v]) and final_gate)
        return (result[0], result[1], result[2])

    def play_rate_hz(self, video_hz: float, clock_hz: float) -> float:
        """Effective PLAY call rate for this tune, in Hz.

        Most PSIDs are vsync-timed: PLAY is called once per video frame
        (`video_hz` = 50 PAL / 60 NTSC). But a CIA-timed (multispeed) tune
        programs CIA #1 Timer A from its INIT; the real C64's kernal-IRQ
        chain then fires PLAY at `clock_hz / (latch + 1)` Hz — often well
        above the frame rate — so the song advances faster than once per
        frame. WaveformScene ticks the host emulator at this rate so the
        scope advances the song at the same wall-clock pace as the audience's
        audio; otherwise a 1.5x-multispeed tune's voices come in on screen
        ~1.5x later than you hear them (worst for late-entering voices).

        Call after INIT (the latch is set there). Falls back to `video_hz`
        for vsync tunes (no Timer A write) and for out-of-range latches
        (a transient/garbage value, or a rate that isn't a plausible
        multispeed multiple of the frame rate). `clock_hz` is the system
        clock of the machine actually playing the tune (the U64's), not the
        tune's PSID PAL/NTSC flag — the same latch yields a different rate
        on a PAL vs NTSC machine."""
        if not self._memory.cia1_timer_a_written:
            return video_hz
        latch = self._memory.ram[CIA1.TIMER_A_LO] | (self._memory.ram[CIA1.TIMER_A_HI] << 8)
        if latch <= 0:
            return video_hz
        rate = clock_hz / (latch + 1)
        # Accept only a plausible multispeed band around the frame rate;
        # a wild latch (e.g. a tune using Timer A for something else) keeps
        # the safe vsync default rather than racing the scope off the rails.
        if video_hz * 0.5 <= rate <= video_hz * 8.0:
            return rate
        return video_hz

    # ---- internals --------------------------------------------------

    def _run_routine(
        self, target: int, *, cap: int, tag: str, a: int = 0, deadline: float | None = None
    ) -> None:
        """JSR-equivalent: push a sentinel return address, set PC = target,
        step until PC == sentinel or the routine exhausts its budget.

        The push order matches the 6502's JSR: high byte first (higher
        stack slot), low byte second. py65's stPushWord handles this
        for us. RTS pulls back the same word and adds 1 — so pushing
        $FEFF leaves PC = $FF00 after the final RTS.

        `cap` is the emulated-cycle budget, passed explicitly by each caller
        (_INIT_CYCLE_CAP is 40x _PLAY_CYCLE_CAP, and it used to be selected by
        comparing `tag` — the log label — against "init", so a third caller
        with a descriptive tag would have silently drawn the tight PLAY
        budget). `deadline` is an optional absolute instant in THIS emulator's
        clock domain (`self._now`) — the budget's own clock when one was
        supplied, `time.monotonic` otherwise. Reading a different clock here
        than the one that produced the instant is not a rounding error: it
        makes the deadline meaningless.

        Four conditions end the routine early, and they do NOT all mean the
        same thing:

          * the cycle budget, the step budget (which is what actually bounds
            steps rather than trusting the cycle accounting — see
            _ILLEGAL_OPCODE_CYCLETIME), the wall-clock `deadline`, and an
            exception out of py65 itself all set `last_routine_capped` AND
            the sticky `any_routine_capped`. The first means "the pass just
            run does not terminate here", which is what the PLAY pre-flight
            refuses a tune on; the second means "something sampled from this
            emulator is a prefix", which is what FootprintSample.complete
            reports — including for the constructor's INIT, which no per-pass
            flag could ever report because it ran before the caller held the
            object.
          * an opcode py65 does not implement sets `saw_undecodable_opcode`
            and NOT `last_routine_capped`. Executing it would derail the
            instruction stream — py65 advances the PC by 2 regardless of the
            real instruction's length — so the pass still ends here, but the
            tune itself is fine: the undocumented opcodes are a normal 6510
            idiom and the real chip runs them. Refusing such a tune outright
            took a large share of HVSC off the air; distrusting the footprint
            it produces (FootprintSample.complete) is the proportionate answer.

        The cycle, step and wall-clock tests are re-guarded by the sentinel so
        a routine that completes on the very step that crosses a budget is
        reported as having returned normally, not as capped.
        """
        mpu = self._mpu
        mpu.sp = 0xFF
        mpu.stPushWord(_SENTINEL_PUSH)
        mpu.a = a
        mpu.x = 0
        mpu.y = 0
        mpu.pc = target & 0xFFFF
        mpu.processorCycles = 0
        step = mpu.step
        cycletime = mpu.cycletime
        ram = self._memory.ram
        sentinel = _SENTINEL_PC
        steps = 0
        self.last_routine_capped = False
        while mpu.pc != sentinel:
            if cycletime[ram[mpu.pc]] == _ILLEGAL_OPCODE_CYCLETIME:
                self._report_undecodable_opcode(tag, ram[mpu.pc], mpu.pc)
                return
            try:
                step()
            except Exception:
                # py65 is not written against hostile input and neither is a
                # .sid file trustworthy. Whatever it was, this routine did not
                # return — say so the way a blown budget does, so the
                # pre-flight refuses the tune instead of the exception unwinding
                # the footprint helpers and out of the scene's ValueError-only
                # handler, ending the whole playlist.
                log.warning(
                    "sid_host_emu: %s raised out of the 6502 interpreter at PC=$%04X "
                    "— treating the pass as non-terminating",
                    tag,
                    mpu.pc,
                    exc_info=True,
                )
                self._report_capped_routine()
                return
            steps += 1
            if mpu.pc == sentinel:
                return
            over_budget = mpu.processorCycles >= cap or steps >= cap
            if not over_budget and deadline is not None and steps % _WALL_CLOCK_CHECK_STEPS == 0:
                over_budget = self._now() >= deadline
            if over_budget:
                log.debug(
                    "sid_host_emu: %s budget reached at PC=$%04X (%d cycles, %d steps, "
                    "cap %d) — giving up this pass",
                    tag,
                    mpu.pc,
                    mpu.processorCycles,
                    steps,
                    cap,
                )
                self._report_capped_routine()
                return

    def _report_capped_routine(self) -> None:
        """Record that the pass just run did not terminate.

        Both flags move together and always through here: the per-pass verdict
        the pre-flight reads, and the sticky one a footprint's `complete` reads.
        Setting only the first is how a truncated INIT came back as a full
        footprint."""
        self.last_routine_capped = True
        self.any_routine_capped = True

    def _report_undecodable_opcode(self, tag: str, opcode: int, pc: int) -> None:
        """End the pass at an opcode py65 can't execute, and remember it.

        Warned once per emulator: every later pass stops at the same
        instruction, and the pre-flight alone runs 50 of them."""
        self.saw_undecodable_opcode = True
        if self._illegal_opcode_reported:
            return
        self._illegal_opcode_reported = True
        log.warning(
            "sid_host_emu: %s hit undocumented opcode $%02X at PC=$%04X — py65 can't "
            "execute it and would desync the instruction stream, so this pass ends "
            "here; the tune still plays on the real 6510, but its RAM footprint is "
            "only a prefix and placements derived from it are not trusted",
            tag,
            opcode,
            pc,
        )


class FootprintSample(NamedTuple):
    """A tune's 64 KB RAM footprint bitmap plus whether it can be trusted.

    `ram` is the bitmap (1 = the address was touched). `complete` is False
    when the run was cut short in ANY of the ways a run can be cut short — the
    shared budget ran out between passes, a routine hit its cycle or step cap
    or its own deadline, py65 raised, or a pass ended at an opcode py65 cannot
    execute — in which case the bitmap is a *prefix* of the tune's real
    behavior: every address it marks is genuine, but addresses the tune
    touches later are missing.

    The constructor's INIT counts, and is the worst of them: it runs before
    the caller holds the emulator, once, and everything past its cap is lost
    for every pass that follows. That is why the flag is computed from the
    emulator's sticky `any_routine_capped` rather than from what the tick loop
    happened to see.

    That distinction has to ride in the return value rather than only in a log
    line, because consumers place hardware on it: api._find_free_layout puts
    the relocated C64-side player in the largest hole the bitmap leaves, and
    _choose_display_layout picks the VIC bank from it. A missing late write
    reads as free RAM, the player MC goes there, and PLAY overwrites it —
    silence plus a crash to BASIC, which is the exact regression the footprint
    was added to prevent. The two consumers that place a whole tune's hardware
    reach these bitmaps only through [analyze_placement], which applies the
    trust decision for them; the per-subtune scans in waveform.py handle it
    themselves because their answer is "skip this subtune", not "widen".
    """

    ram: bytearray
    complete: bool


def _tick_until_budget(emu: SidHostEmu, ticks: int, budget: HostEmuBudget, what: str) -> bool:
    """Run up to `ticks` PLAY passes; stop early at FOOTPRINT_DEADLINE_S or at
    whatever is left of the shared `budget`, whichever comes first. Returns
    True only when the resulting bitmap is a complete sample.

    "Complete" is the AND of every way this emulator could have stopped short,
    not only the ways this loop can see: all `ticks` passes ran, no pass ended
    on an opcode py65 can't execute, and no routine on the emulator was ever
    cut off by a cycle/step cap, a deadline, or an exception — INIT included.
    INIT is the one that matters most, because it ran in the constructor and
    everything past its cap is lost for every pass that follows."""
    give_up_at = budget.deadline_for(FOOTPRINT_DEADLINE_S)
    for done in range(ticks):
        emu.tick_play(budget.deadline_for(_PLAY_DEADLINE_S))
        if done + 1 < ticks and budget.now() >= give_up_at:
            log.warning(
                "sid_host_emu: %s stopped after %d of %d PLAY passes (%.1fs left of the "
                "%.1fs analysis budget) — this tune's PLAY is far more expensive than "
                "usual; the footprint is a partial sample and won't be trusted for "
                "player placement",
                what,
                done + 1,
                ticks,
                max(budget.remaining(), 0.0),
                ANALYSIS_BUDGET_S,
            )
            return False
    return not (emu.saw_undecodable_opcode or emu.any_routine_capped)


class CatchupResult(NamedTuple):
    """What one catch-up batch managed. `passes` is how many PLAY passes ran;
    `overran` is True when the batch spent longer than the caller allowed it —
    which, since a batch always runs at least one pass, means one PLAY pass on
    its own costs more than the caller's whole time bound.

    `overran` rides in the return value because that case is invisible in
    `passes` alone: a batch asked for exactly one pass comes back "complete"
    while having blown the bound by any margin at all. A caller that sees it is
    running its poll thread at a duty cycle it did not choose."""

    passes: int
    overran: bool


def run_catchup_passes(
    emu: SidHostEmu, on_tick: Callable[[], None], *, ticks: int, seconds: float
) -> CatchupResult:
    """Run up to `ticks` PLAY passes, calling `on_tick` after each, and stop
    once `seconds` of host wall clock have gone by.

    Shared by the two threads that advance a host emulator to wall-clock —
    WaveformScene._poll_regs and SidFeatureStream._poll_loop — because both
    used to bound the burst by tick count alone, and the tune sets what a tick
    costs: a PLAY that stays legally inside _PLAY_CYCLE_CAP measured 15.8 ms,
    turning a 120-tick batch into 1.9 s on a thread whose period is 1/60 s.
    The tune sets the rate the batch is sized against as well (play_rate_hz
    honors a CIA #1 Timer A latch up to 8x the video rate, written from PLAY),
    so without a time bound the thread could never catch up and simply pegged a
    core for the scene's whole duration. A caller that gets back fewer passes
    than it asked for is running behind the audio and should say so once.

    One PLAY pass is indivisible here — the pass is not given a deadline of its
    own, because truncating it would leave the $D4xx shadow holding half a
    frame's writes and the scope would show that (see _PLAY_DEADLINE_S). So
    `seconds` cannot bound a batch below the cost of a single pass, and a tune
    that writes CIA #1 Timer A for 400 Hz sets both sides of that comparison:
    2.5 ms of poll period against a 10 ms pass ran the thread back to back for
    the scene's whole duration. What keeps the bound binding is the caller
    sizing its period against a measured pass cost — [sustainable_poll_period_s]
    — and `overran` is how this function says the sizing was wrong anyway."""
    stop_at = time.monotonic() + seconds
    for done in range(ticks):
        emu.tick_play()
        on_tick()
        if time.monotonic() < stop_at:
            continue
        # Out of time. Running out on the FIRST pass is the case no smaller
        # batch could have avoided — the bound did not bind, it was simply
        # smaller than one indivisible unit of work.
        return CatchupResult(done + 1, overran=done == 0)
    return CatchupResult(ticks, overran=False)


def detect_play_rate_hz(
    probe: SidHostEmu,
    *,
    video_hz: float,
    clock_hz: float,
    budget: HostEmuBudget,
    ticks: int = RATE_PROBE_TICKS,
) -> tuple[float, float | None]:
    """Return ``(rate_hz, pass_cost_s)`` for the tune loaded into `probe`.

    `probe` must be a THROWAWAY emulator, because this runs PLAY passes on it:
    a caller that handed over its live one would lose the song position its
    wall-clock catch-up owns. Shared by `WaveformScene` and
    `SidFeatureStream`, which each carried a line-for-line copy of the loop.

    `pass_cost_s` is what one PLAY pass of this tune measured on this host —
    the number [sustainable_poll_period_s] floors the poll period against — or
    ``None`` when no pass was timed at all.

    A pass is timed *before* the rate is consulted, and that order is the whole
    point. Consulting the rate first meant a tune that programs Timer A from
    INIT — where the rate is known the moment INIT returns — left the loop
    having timed nothing and reported a cost of 0.0, which the sizing function
    read as "no measurement, don't clamp". The floor was therefore skipped on
    exactly the tunes it exists for: a 399.3 Hz CIA-timed tune measured here
    kept its 2.50 ms poll period against a pass it had never priced.
    `run_catchup_passes` runs its pass before consulting its clock for the same
    reason.

    The budget is consulted before each pass but does not bound the pass
    itself, and that is deliberate: the pass here has to cost what the pass on
    the render path will cost, and the render path's ``tick_play()`` gets no
    deadline. A deadlined probe pass is a *censored* measurement, and censored
    in the one direction that matters: whatever a pass really costs above
    ``_PLAY_DEADLINE_S``, it is priced at ``_PLAY_DEADLINE_S`` and no more.
    [sustainable_poll_period_s] then sizes the poll period off the truncation
    rather than off the pass, so the period it settles on can be shorter than
    the pass the render path really runs — the back-to-back GIL starvation the
    floor exists to prevent, reached through the floor. [describe_pass_cost]
    meanwhile names the truncation as a measurement, which is the whole thing
    the ``None`` reading was added to stop. No number is put on the overshoot
    on purpose: it is exactly the part a deadlined probe cannot see.

    What bounds one pass is the *step* cap in `_run_routine`, not the cycle cap
    and not a clock: a PLAY that spins on a raster forever measures 7.1 ms
    undeadlined, because 50 k steps is what binds. What bounds the loop is the
    ``budget.expired()`` check above — an expensive pass ends the probe after
    it, rather than being cut off inside it.

    ``None`` means only what it says: no pass was timed. That happens when the
    budget is already spent, or when `ticks` is non-positive; it is a distinct
    value rather than another 0.0 because the two readings must not be
    confused. A pass nobody timed is charged the worst a legal one can cost,
    while a pass measured at 0.0 really was that quick.
    """
    rate = probe.play_rate_hz(video_hz, clock_hz)
    passes = 0
    spent = 0.0
    for _ in range(ticks):
        if budget.expired():
            break  # too expensive to emulate; the vsync default stands
        started = time.monotonic()
        probe.tick_play()
        spent += time.monotonic() - started
        passes += 1
        rate = probe.play_rate_hz(video_hz, clock_hz)
        if abs(rate - video_hz) > 0.5:
            break  # multispeed Timer A latch seen — rate is known
    return float(rate), (spent / passes if passes else None)


def describe_pass_cost(pass_cost_s: float | None) -> str:
    """How a poll-period warning should name what the period was sized against.

    Both catch-up threads warn in the same shape and must not describe an
    assumed cost as a measured one — the whole point of the ``None`` reading is
    that nobody timed anything."""
    if pass_cost_s is None:
        return (
            "no PLAY pass was ever timed for this tune, so it is charged the "
            f"{UNMEASURED_PASS_COST_S * 1000.0:.1f} ms a legal pass can cost at worst"
        )
    return f"one PLAY pass costs {pass_cost_s * 1000.0:.1f} ms"


def sustainable_poll_period_s(
    tick_dt_s: float, pass_cost_s: float | None, fraction: float
) -> float:
    """The shortest poll period a catch-up thread may use: the tune's own PLAY
    period, floored so one measured PLAY pass fits inside `fraction` of it.

    The poll thread sleeps a fixed period *after* each wakeup, so its duty
    cycle is work / (work + period). Ticking at the tune's real rate is only
    affordable while a pass costs less than that rate allows; past there the
    thread runs continuously and — under the GIL — takes the render thread's
    time with it. Slowing the wakeups is the degradation that stays visible
    (the scope falls behind the audio, and the caller says so once) instead of
    the one that does not (a saturated core).

    `tick_dt_s` stays the per-PLAY-tick song time the caller advances envelopes
    by; only the wakeup period is stretched. Keeping those two the same number
    is what conflated "how fast the song advances" with "how often we wake".

    A `pass_cost_s` of ``None`` means the probe never timed a pass — because
    the tune's analysis budget was already gone, or because it was asked for no
    passes at all — so it is charged UNMEASURED_PASS_COST_S, the worst a legal
    pass can cost, rather than nothing. A measured 0.0 is different and stays
    free: a pass too quick for the host clock to resolve needs no floor. The
    two used to be one value, and the expensive reading was the one that got
    lost."""
    if fraction <= 0.0:
        return tick_dt_s
    if pass_cost_s is None:
        pass_cost_s = UNMEASURED_PASS_COST_S
    if pass_cost_s <= 0.0:
        return tick_dt_s
    return max(tick_dt_s, pass_cost_s / fraction)


def ram_write_footprint(
    sid_bytes: bytes,
    song: int = 0,
    ticks: int = FOOTPRINT_TICKS,
    budget: HostEmuBudget | None = None,
) -> FootprintSample:
    """Run a tune's INIT + `ticks` PLAY passes on a throwaway host emulator
    and return a 64 KB bitmap (1 = the tune wrote this RAM address at least
    once) plus whether the sample completed.

    Used to place the relocated C64-side SID player in RAM the tune
    demonstrably never touches: tunes like Beat_Dis use the page just past
    their payload as scratch, so the old "player goes right after the
    payload" heuristic put the player MC where PLAY would overwrite it
    (silent + crash to BASIC). See api._find_free_layout for the consumer.
    The player MC must survive INIT too, so this is the full INIT+PLAY
    *write* footprint; the *display*-bank choice uses a stricter
    read+write view — see [ram_play_access_footprint].

    Even a complete footprint is a sample over `ticks` passes, not a proof of
    total RAM usage — api._find_free_layout pairs it with a largest-hole
    preference to leave margin against patterns a short sample doesn't reach.
    Pass `budget` to charge this run against the tune's whole analysis rather
    than let it draw a fresh FOOTPRINT_DEADLINE_S; a caller that runs exactly
    one footprint can leave it None and get its own.
    """
    if budget is None:
        budget = HostEmuBudget()
    emu = SidHostEmu(sid_bytes, song=song, track_footprint=True, budget=budget)
    footprint = emu._memory.footprint
    assert footprint is not None  # track_footprint=True guarantees it
    complete = _tick_until_budget(emu, ticks, budget, "write footprint")
    return FootprintSample(footprint, complete)


def ram_play_access_footprint(
    sid_bytes: bytes,
    song: int = 0,
    ticks: int = FOOTPRINT_TICKS,
    budget: HostEmuBudget | None = None,
) -> FootprintSample:
    """Run a tune's INIT + `ticks` PLAY passes and return a 64 KB bitmap of
    every address the tune *read or wrote during PLAY* (1 = accessed), plus
    whether the sample completed.

    This is the right footprint for the *display*-bank choice. The waveform
    bitmap is painted once after INIT and refreshed every frame, so a region
    the tune only scratches at INIT is harmless — we paint over it and PLAY
    never touches it again. But a region PLAY *reads* every frame is live
    data we must not clobber: Galway's Times of Lore copies per-song data
    into VIC bank 2's $B400 at INIT and reads it back from there on every
    PLAY. A write-only footprint misses that read (the earlier `play_only`
    write footprint did, and the display then clobbered the song data → no
    audio, garbled screen). Tracking PLAY-phase reads as well as writes —
    and excluding the one-time INIT pass — catches exactly the regions that
    would fight a live bitmap. See WaveformScene.setup + _choose_display_layout.

    Like [ram_write_footprint] this is a sample over `ticks` passes, not a
    proof; _choose_display_layout pairs it with the payload extent. `budget`
    behaves the same way it does there.
    """
    if budget is None:
        budget = HostEmuBudget()
    emu = SidHostEmu(sid_bytes, song=song, track_access=True, budget=budget)
    access = emu._memory.access
    assert access is not None  # track_access=True guarantees it
    # INIT already ran in __init__; drop its accesses so only the PLAY
    # passes below are recorded (INIT-only scratch is paintable).
    access[:] = bytes(len(access))
    complete = _tick_until_budget(emu, ticks, budget, "PLAY access footprint")
    return FootprintSample(access, complete)


# PLAY pre-flight pass count. After loading a tune we run this many PLAY
# passes; if EVERY one bails at the host emulator's cycle cap (instead of
# returning normally in the usual ~1-2k cycles), the tune spins on a
# raster/IRQ this pure-Python 6502 never provides. Such a tune can't be
# rendered faithfully AND would hang the C64-side player — its `SEI; JSR
# init` sits with IRQs masked, so the kernal IRQ never fires, $028D stops
# updating, and the machine goes dead/silent (the Hollywood Poker Pro
# failure). 50 passes ≈ 1 s of PLAY @ 50 Hz — long enough to be unambiguous,
# short enough that a healthy tune adds only ~5 ms.
PREFLIGHT_TICKS = 50


def play_preflight_failure(
    emu: SidHostEmu, ticks: int = PREFLIGHT_TICKS, budget: HostEmuBudget | None = None
) -> str | None:
    """Return None when `emu`'s PLAY completes within its budget on at least
    one of `ticks` passes; otherwise a one-line reason the caller can put in
    front of a user.

    The reason is returned rather than a bare False because the two ways to
    fail are not the same fact and the caller's message has to say which. EVERY
    pass bailing means a raster/IRQ-spinning tune that would dead-machine the
    C64-side player. Running out of `budget` before any pass terminated means
    we never found out — a different sentence entirely, and reporting it as the
    first is the "error message blaming a raster spin that was not happening"
    this gate has already been wrong about once. Both refuse the tune: an
    un-pre-flighted PLAY that does hang leaves a machine needing a physical
    reset, while refusing costs one scene while the playlist advances.

    The healthy verdict reads `last_routine_capped` and nothing else, and that
    is the whole classifier: a pass that ends any other way — including one
    that stops at an undocumented opcode — counts as terminating, so the tune
    is accepted. It has to be that way round. LAX/SAX/SLO in PLAY is a normal
    hand-rolled-player idiom that the U64's real 6510 executes; gating on "py65
    could run every instruction" refused a large share of HVSC at scene setup.
    What such a tune loses is the trust placed in its RAM footprint, not the
    ability to play — see FootprintSample.

    The rule lives here so its two callers can't drift: `sid_play_preflight`
    builds a throwaway emulator for SidFileAudioSource, and WaveformScene runs
    it against the live `_host_emu` it is about to render from. Both leave the
    emulator advanced by however many passes it took to reach the verdict.

    `budget` bounds the passes in seconds as well as in count. Without it the
    only bound was the per-pass cycle/step cap, and 50 of those measured 0.6 s
    per candidate — re-paid for every candidate in a pool walk."""
    for done in range(ticks):
        if budget is not None and budget.expired():
            return (
                f"PLAY could not be pre-flighted within the {ANALYSIS_BUDGET_S:.0f}s host "
                f"analysis budget — {done} of {ticks} passes ran and none of them "
                f"returned, so whether it would hang the C64-side player is unknown"
            )
        emu.tick_play(None if budget is None else budget.deadline_for(_PLAY_DEADLINE_S))
        if not emu.last_routine_capped:
            return None
    return (
        f"PLAY never completes within the host emulator's budget over {ticks} passes "
        f"— the tune spins on a raster/IRQ the player environment doesn't provide; "
        f"it would hang the C64-side player (silent + unresponsive)"
    )


def sid_play_preflight(
    sid_bytes: bytes,
    song: int = 0,
    ticks: int = PREFLIGHT_TICKS,
    budget: HostEmuBudget | None = None,
) -> str | None:
    """Construct-and-check wrapper around [play_preflight_failure] for callers
    that don't already hold an emulator (SidFileAudioSource) — see
    PREFLIGHT_TICKS. Returns None when the tune passes, else the reason.
    INIT already ran in __init__, under `budget` when one is given."""
    return play_preflight_failure(SidHostEmu(sid_bytes, song=song, budget=budget), ticks, budget)


class PlacementFootprints(NamedTuple):
    """Everything one tune's analysis licenses a caller to place hardware from.

    `avoid` is the RAM the relocated player MC must clear; `display` is the
    view the VIC bank is chosen from; `play_bank` is the $01 value to use
    around JSR play, or None to let run_sid_player's address heuristic decide.

    The point of the type is that there is no way to reach a raw bitmap
    without the trust decision having already been made. `complete` used to
    ride back on two separate FootprintSample values that three call sites
    each had to remember to consult, and two of them did not: they logged the
    warning and then handed the prefix to api._find_free_layout and
    _choose_display_layout anyway. `trusted` is reported for the log line, not
    for a decision the caller still has to make."""

    avoid: bytearray
    display: bytearray
    play_bank: int | None
    trusted: bool


def _union_of(first: bytes | bytearray, second: bytes | bytearray) -> bytearray:
    """Elementwise OR of two 64 KB bitmaps. Pure Python on purpose — see the
    note on _play_bank_for_footprints; it runs once, on the untrusted path."""
    return bytearray(a | b for a, b in zip(first, second, strict=True))


def analyze_placement(
    sid_bytes: bytes, *, song: int, budget: HostEmuBudget, what: str
) -> PlacementFootprints:
    """Footprint one tune both ways and return the placement inputs, already
    made safe for a sample that came back a prefix.

    A prefix has to make a placement MORE conservative, never less. Every
    address a truncated run marks is genuine; what is missing is the tail, and
    a missing late write reads as free RAM — the player MC goes there, PLAY
    overwrites it, and the tune is silent with a crash to BASIC, which is the
    regression the footprint was added to prevent. So on an untrusted sample:

      * both views widen to the union of everything the tune was observed to
        touch at all, INIT writes included. Normally the display view excludes
        INIT-only scratch because the bitmap is painted after INIT and may
        cover it; giving that concession up uses observed data rather than
        guesswork.
      * `play_bank` is dropped to None. Deriving $36 (BASIC out) from a prefix
        means reading an intersection that the missing tail could have created
        or destroyed either way; None is the pre-existing, correct-by-default
        address heuristic.

    **The widening does not close the class, and this is the load-bearing
    caveat.** It is near-inert for the cause that is common. Both runs execute
    the same 6502 code with the same tick count, so an undocumented opcode
    truncates BOTH at the identical instruction: the union adds only the
    read-versus-write difference between two samples that stopped in the same
    place. Measured on a PSID whose PLAY is
    ``STA $2000 / LAX $3000 / STA $4000 / RTS`` — both samples incomplete, the
    two differing at **5** addresses, and ``avoid[$4000] == 0`` for a write
    PLAY makes on every frame. A tune with a LAX in PLAY can still get the
    player MC placed in RAM its untraced tail writes, which is the exact
    regression the footprint exists to prevent. What the widening genuinely
    buys is the *nondeterministic* truncation cause — the wall-clock deadline,
    where the two runs can stop at different points and the union really does
    carry information neither sample has alone.

    So the trust flag is not what protects the placement in the common case,
    and neither — reliably — is anything else. api._find_free_layout does
    supply a real margin: it excludes the payload extent and prefers the
    LARGEST free hole, which is the same margin that stands between a
    finite-but-complete sample and an unreached write pattern (a trusted sample
    is a sample too). But it is on the *relocation* path only.
    api._choose_player_layout tries the fixed historical $C300/$C400 layout
    first and reaches _find_free_layout only when _layout_fits rejects it — and
    _layout_fits consults this bitmap and nothing else, with no hole preference
    of any kind. A tune truncated by a LAX in PLAY whose untraced tail writes
    $C300-$C3FF therefore gets the default layout accepted and the player MC
    put exactly where PLAY overwrites it. The display side is the same shape:
    WaveformScene._choose_display_layout gets the payload extent, not a
    largest-hole preference.

    And if the widened bitmaps leave no room at all,
    the callers' existing ValueError paths abort the scene and the playlist
    advances — the fail-closed end, reached by the code that already handles
    "no free VIC bank" rather than a second refusal written beside it.

    Refusing every untrusted tune outright is the alternative, and choosing it
    is a product decision rather than a correctness one: an undocumented opcode
    anywhere in PLAY makes a sample a prefix, that is a normal hand-rolled-player
    idiom, and refusing on it takes a large share of HVSC off the air — see
    play_preflight_failure, which had to be talked out of the same gate.
    """
    write_sample = ram_write_footprint(sid_bytes, song=song, budget=budget)
    access_sample = ram_play_access_footprint(sid_bytes, song=song, budget=budget)
    trusted = write_sample.complete and access_sample.complete
    if trusted:
        return PlacementFootprints(
            avoid=write_sample.ram,
            display=access_sample.ram,
            play_bank=_play_bank_for_footprints(write_sample.ram, access_sample.ram),
            trusted=True,
        )
    union = _union_of(write_sample.ram, access_sample.ram)
    log.warning(
        "sid_host_emu: %s was only partially footprinted (%.1fs analysis budget) — "
        "the player's RAM slot and the display bank are being placed from the union "
        "of everything this tune was seen to touch, and its PLAY $01 bank falls back "
        "to the address heuristic; the scene aborts if that leaves nothing free",
        what,
        ANALYSIS_BUDGET_S,
    )
    # Two bitmaps, not one shared object: the type advertises `avoid` and
    # `display` as independent views and both callers mark reservations into
    # them. They copy before mutating today, so nothing breaks — but a shared
    # bytearray means the next in-place mark on one silently rewrites the other.
    return PlacementFootprints(avoid=union, display=bytearray(union), play_bank=None, trusted=False)
