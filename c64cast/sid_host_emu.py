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

The host emulator is loosely coupled to the U64 — both run at nominal
60 Hz (NTSC) / 50 Hz (PAL); small drift (one tick or so) is invisible
in an oscilloscope view.

Validation (RSID/load_addr/play_addr) is delegated to
[parse_psid_for_player](api.py) so SidHostEmu refuses the same SIDs
[Ultimate64API.run_sid_player](api.py) refuses — config errors surface
identically regardless of which path reports them first.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from py65.devices.mpu6502 import MPU

from .api import parse_psid_for_player
from .c64 import CIA1, CPU, ROM, SCREEN, SID, VIC_BANK_0
from .sidemu import SID_REG_COUNT

log = logging.getLogger(__name__)

# Sentinel return address pushed onto the 6502 stack before each
# JSR-equivalent into INIT/PLAY. The 6502 RTS pulls a word and adds 1,
# so pushing $FEFF yields PC=$FF00 after the final RTS — we step until
# we see that PC value and treat it as "the called routine returned".
# $FF00 itself reads as $60 (RTS) from the ROM-stub fill below, so even
# if the cap-check missed by one step the next step would just return
# again.
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

# Default number of PLAY passes to run when profiling a tune's RAM write
# footprint. ~2000 ticks ≈ 33 s of tune time at 60 Hz and runs in ~0.5 s of
# host CPU; footprints observed to stabilize well before 1000 ticks. The
# footprint places the relocated C64-side player in RAM the tune never writes
# (see [ram_write_footprint] + api._find_free_layout).
FOOTPRINT_TICKS = 2000


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
    sid_model: str | None  # "6581", "8580", "6581+8580", "?" or None
    # $Dxxx base address of every SID chip the tune drives, chip 0 first
    # (always $D400). A single-SID tune yields (0xD400,); a 3SID tune yields
    # e.g. (0xD400, 0xD420, 0xD440). Parsed from the PSID v3/v4 second/third
    # SID-address bytes ($7A/$7B). See parse_sid_header.
    sid_addresses: tuple[int, ...] = (SID.BASE,)


# PSID v2+ flags byte 1 (low-order) layout — clock at bits 2-3, primary
# SID model at bits 4-5. Higher-order model bits for 2nd/3rd SIDs live in
# byte 0 of the 2-byte flags field; the waveform UI only surfaces the
# primary chip + clock so we ignore the rest.
_CLOCK_TABLE = {0: "?", 1: "PAL", 2: "NTSC", 3: "PAL+NTSC"}
_MODEL_TABLE = {0: "?", 1: "6581", 2: "8580", 3: "6581+8580"}

# PSID extra-SID address bytes: secondSIDAddress at $7A (v3+), thirdSIDAddress
# at $7B (v4+). The byte encodes the middle nibble of a $Dxx0 base: address =
# $D000 | (byte << 4), so 0x42 → $D420, 0x50 → $D500, 0xE0 → $DE00. Zero means
# "no chip". The spec only permits even bytes in $42-$FE resolving to the
# $D420-$D7E0 and $DE00-$DFE0 windows; we accept any nonzero byte that lands in
# the $D000-$DFF0 I/O page and ignore the rest (a malformed byte → single SID).
_SECOND_SID_ADDR = 0x7A
_THIRD_SID_ADDR = 0x7B


def _decode_extra_sid_addr(byte: int) -> int | None:
    """Decode a PSID extra-SID address byte to a $Dxx0 base, or None if the
    byte is 0 (absent) or resolves outside the $D000-$DFF0 I/O page."""
    if byte == 0:
        return None
    addr = 0xD000 | (byte << 4)
    return addr if 0xD000 <= addr <= 0xDFF0 else None


def parse_sid_header(data: bytes) -> SidHeader:
    """Parse the PSID/RSID v1+ header. Validates magic, returns metadata.

    Reads the v2+ flags field at offset 0x76 (2 bytes, big-endian) to
    surface SID chip model + PAL/NTSC clock. v1 headers (length 118)
    leave both as None. On PSID v3/v4 headers, reads the second/third
    SID-address bytes ($7A/$7B) into `sid_addresses` (chip 0 = $D400 first)."""
    if len(data) < 22:
        raise ValueError("SID file too short to contain a header")
    magic = data[:4]
    if magic not in (b"PSID", b"RSID"):
        raise ValueError(f"not a SID file (expected PSID/RSID magic, got {magic!r})")
    version = int.from_bytes(data[4:6], "big")
    clock: str | None = None
    sid_model: str | None = None
    if version >= 2 and len(data) >= 0x78:
        # flags lives at 0x76 (16 bits big-endian); clock/model bits are in
        # the low byte (0x77).
        flags_lo = data[0x77]
        clock = _CLOCK_TABLE[(flags_lo >> 2) & 0x03]
        sid_model = _MODEL_TABLE[(flags_lo >> 4) & 0x03]
    # Extra SID chips (v3 adds a 2nd, v4 a 3rd). Chip 0 is always $D400.
    addresses = [SID.BASE]
    if version >= 3 and len(data) > _SECOND_SID_ADDR:
        second = _decode_extra_sid_addr(data[_SECOND_SID_ADDR])
        if second is not None:
            addresses.append(second)
    if version >= 4 and len(data) > _THIRD_SID_ADDR and len(addresses) == 2:
        third = _decode_extra_sid_addr(data[_THIRD_SID_ADDR])
        if third is not None:
            addresses.append(third)
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
        sid_model=sid_model,
        sid_addresses=tuple(addresses),
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
            while len(addresses) < want:
                addresses.append(SID.BASE + len(addresses) * _CANONICAL_SID_STRIDE)
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


def _overlaps(lo: int, hi: int, region_lo: int, region_size: int) -> bool:
    """True when [lo, hi) overlaps the region [region_lo, region_lo+size)."""
    region_hi = region_lo + region_size
    return lo < region_hi and hi > region_lo


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
        if self.access is not None:
            self.access[addr] = 1
        return self.ram[addr]

    def __setitem__(self, addr: int, val: int) -> None:
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
    ) -> None:
        self._parsed = parse_psid_for_player(sid_bytes, song=song)
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
        # Load the SID payload at its declared address.
        load = self._parsed.load_addr
        self._memory.ram[load : load + len(self._parsed.payload)] = self._parsed.payload
        # Run INIT once: A = song-1, X=Y=0; call init_addr; wait for
        # sentinel RTS or the cycle cap.
        self._run_routine(
            self._parsed.init_addr, a=(self._parsed.song_to_play - 1) & 0xFF, tag="init"
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

    def tick_play(self) -> None:
        """Run one PLAY pass. Re-entrant call into `play_addr`, same
        sentinel-RTS + cycle-cap discipline as INIT. The cycle cap
        bounds a degenerate PLAY (one that spins waiting for a raster
        or an IRQ that will never fire in this emulator) so the render
        thread isn't starved."""
        # Clear hard-restart flags (all chips) so retriggers() reflects only
        # this tick.
        for gl in self._memory.gate_low_banks:
            gl[:] = bytes(SID.N_VOICES)
        self._run_routine(self._parsed.play_addr, tag="play")

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

    def _run_routine(self, target: int, *, a: int = 0, tag: str) -> None:
        """JSR-equivalent: push a sentinel return address, set PC =
        target, step until PC == sentinel or we exceed the cycle cap.

        The push order matches the 6502's JSR: high byte first (higher
        stack slot), low byte second. py65's stPushWord handles this
        for us. RTS pulls back the same word and adds 1 — so pushing
        $FEFF leaves PC = $FF00 after the final RTS.
        """
        mpu = self._mpu
        mpu.sp = 0xFF
        mpu.stPushWord(_SENTINEL_PUSH)
        mpu.a = a
        mpu.x = 0
        mpu.y = 0
        mpu.pc = target & 0xFFFF
        mpu.processorCycles = 0
        # INIT is a one-time routine (setup / SHIFT cycle / footprint) and
        # may do heavy copies or depacking; PLAY runs every frame and must
        # stay tight. See _INIT_CYCLE_CAP / _PLAY_CYCLE_CAP.
        cap = _INIT_CYCLE_CAP if tag == "init" else _PLAY_CYCLE_CAP
        step = mpu.step
        sentinel = _SENTINEL_PC
        self.last_routine_capped = False
        while mpu.pc != sentinel:
            step()
            if mpu.processorCycles >= cap:
                log.debug(
                    "sid_host_emu: %s cycle cap (%d) reached at PC=$%04X — giving up this tick",
                    tag,
                    cap,
                    mpu.pc,
                )
                self.last_routine_capped = True
                return


def ram_write_footprint(sid_bytes: bytes, song: int = 0, ticks: int = FOOTPRINT_TICKS) -> bytearray:
    """Run a tune's INIT + `ticks` PLAY passes on a throwaway host emulator
    and return a 64 KB bitmap (1 = the tune wrote this RAM address at least
    once).

    Used to place the relocated C64-side SID player in RAM the tune
    demonstrably never touches: tunes like Beat_Dis use the page just past
    their payload as scratch, so the old "player goes right after the
    payload" heuristic put the player MC where PLAY would overwrite it
    (silent + crash to BASIC). See api._find_free_layout for the consumer.
    The player MC must survive INIT too, so this is the full INIT+PLAY
    *write* footprint; the *display*-bank choice uses a stricter
    read+write view — see [ram_play_access_footprint].

    The footprint is a sample over `ticks` passes, not a proof of total RAM
    usage — api._find_free_layout pairs it with a largest-hole preference to
    leave margin against patterns a short sample doesn't reach.
    """
    emu = SidHostEmu(sid_bytes, song=song, track_footprint=True)
    footprint = emu._memory.footprint
    assert footprint is not None  # track_footprint=True guarantees it
    for _ in range(ticks):
        emu.tick_play()
    return footprint


def ram_play_access_footprint(
    sid_bytes: bytes, song: int = 0, ticks: int = FOOTPRINT_TICKS
) -> bytearray:
    """Run a tune's INIT + `ticks` PLAY passes and return a 64 KB bitmap of
    every address the tune *read or wrote during PLAY* (1 = accessed).

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
    proof; _choose_display_layout pairs it with the payload extent.
    """
    emu = SidHostEmu(sid_bytes, song=song, track_access=True)
    access = emu._memory.access
    assert access is not None  # track_access=True guarantees it
    # INIT already ran in __init__; drop its accesses so only the PLAY
    # passes below are recorded (INIT-only scratch is paintable).
    access[:] = bytes(len(access))
    for _ in range(ticks):
        emu.tick_play()
    return access


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


def sid_play_preflight(sid_bytes: bytes, song: int = 0, ticks: int = PREFLIGHT_TICKS) -> bool:
    """Return True when a tune's PLAY completes within the host emulator's
    cycle cap on at least one of `ticks` passes; False when EVERY pass caps
    (a raster/IRQ-spinning tune that would dead-machine the C64-side player).

    Shared safety gate for WaveformScene._load_sid_file and
    SidFileAudioSource — see PREFLIGHT_TICKS. INIT already ran in __init__."""
    emu = SidHostEmu(sid_bytes, song=song)
    for _ in range(ticks):
        emu.tick_play()
        if not emu.last_routine_capped:
            return True
    return False
