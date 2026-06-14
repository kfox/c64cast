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

from py65.devices.mpu6502 import MPU

from .api import parse_psid_for_player
from .c64 import CIA1, SID
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


class TrappedRam:
    """64 KB RAM array that py65's MPU(memory=...) speaks to via plain
    `self.memory[addr]` / `self.memory[addr] = val` (verified in the
    py65 source: every read goes through MPU.ByteAt → self.memory[addr],
    every write is a direct subscript-store).

    Writes to $D400-$D418 land in both the RAM array AND a 25-byte
    `sid_shadow` buffer that the scene reads via SidHostEmu.regs().

    `gate_low_seen[v]` is set whenever a write clears voice v's gate bit.
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
        "sid_shadow",
        "footprint",
        "access",
        "gate_low_seen",
        "cia1_timer_a_written",
    )

    _SID_HI = SID.BASE + SID_REG_COUNT  # exclusive upper bound
    # Voice control-register offsets within $D400 (gate bit lives here).
    _CONTROL_OFFSETS = frozenset(
        v * SID.BYTES_PER_VOICE + SID.OFF_CONTROL for v in range(SID.N_VOICES)
    )
    # CIA #1 Timer A latch bytes — a CIA-timed (multispeed) tune writes
    # these from INIT to set its PLAY call rate; see SidHostEmu.play_rate_hz.
    _CIA1_TIMER_A = frozenset((CIA1.TIMER_A_LO, CIA1.TIMER_A_HI))

    def __init__(self, track_footprint: bool = False, track_access: bool = False) -> None:
        self.ram = bytearray(65536)
        # Fill ROM-mapped region with $60 (RTS) so any unexpected JSR
        # into BASIC/kernal space returns cleanly.
        for i in range(_ROM_FILL_LO, 0x10000):
            self.ram[i] = _ROM_FILL_BYTE
        # Point IRQ/NMI/RESET vectors at an RTS — defensive against BRK.
        for vec in (_VEC_NMI, _VEC_RESET, _VEC_IRQ):
            self.ram[vec] = _RTS_TARGET & 0xFF
            self.ram[vec + 1] = (_RTS_TARGET >> 8) & 0xFF
        self.sid_shadow = bytearray(SID_REG_COUNT)
        # Per-voice "gate cleared during this tick" flags (hard-restart
        # detection). Reset each tick by SidHostEmu.tick_play.
        self.gate_low_seen = bytearray(SID.N_VOICES)
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
        if SID.BASE <= addr < self._SID_HI:
            off = addr - SID.BASE
            self.sid_shadow[off] = val
            # A write that clears a voice's gate bit flags a (possibly
            # intra-tick) gate-low — recovered by retriggers() as a
            # hard-restart even when the shadow's final value is gate-high.
            if off in self._CONTROL_OFFSETS and not (val & SID.GATE):
                self.gate_low_seen[(off - SID.OFF_CONTROL) // SID.BYTES_PER_VOICE] = 1


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
    ) -> None:
        self._parsed = parse_psid_for_player(sid_bytes, song=song)
        self._memory = TrappedRam(track_footprint=track_footprint, track_access=track_access)
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

    def regs(self) -> bytes:
        """Return a 25-byte snapshot of $D400-$D418. Always exactly
        SID_REG_COUNT bytes; pre-INIT this is all zeros, post-INIT it
        reflects whatever the tune set during init, post-`tick_play`
        the last frame's writes."""
        return bytes(self._memory.sid_shadow)

    def tick_play(self) -> None:
        """Run one PLAY pass. Re-entrant call into `play_addr`, same
        sentinel-RTS + cycle-cap discipline as INIT. The cycle cap
        bounds a degenerate PLAY (one that spins waiting for a raster
        or an IRQ that will never fire in this emulator) so the render
        thread isn't starved."""
        # Clear hard-restart flags so retriggers() reflects only this tick.
        self._memory.gate_low_seen[:] = bytes(SID.N_VOICES)
        self._run_routine(self._parsed.play_addr, tag="play")

    def retriggers(self) -> tuple[bool, bool, bool]:
        """Per-voice hard-restart detection for the most recent tick_play().

        A voice whose control register was written gate-low at some point
        during the tick but whose final shadow gate is high underwent a
        hard restart — the gate pulsed off→on within one PLAY call, a
        retrigger the 25-byte shadow alone collapses to gate-still-high.
        WaveformScene feeds this to SIDEmulator.update_registers so plucked
        (sustain=0) leads re-attack on every note instead of flatlining
        after their first decay. Voices that ended gate-low are ordinary
        note-offs handled by the shadow's gate edge, so they're excluded."""
        gl = self._memory.gate_low_seen
        shadow = self._memory.sid_shadow
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
