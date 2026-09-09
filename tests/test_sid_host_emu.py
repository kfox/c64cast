"""Tests for the host-side SID register tracker.

Construction validation is delegated to parse_psid_for_player (shared
with run_sid_player and already covered there), so these tests focus on
the parts unique to SidHostEmu:
  * The 25-byte SID shadow reflects writes from a hand-rolled PLAY
    routine — proves the TrappedRam wrapper actually intercepts.
  * tick_play() is bounded by the cycle cap so a degenerate PLAY (e.g.
    one that spins waiting for a raster IRQ that never fires in the
    emulator) doesn't starve the render thread.
  * Validation errors surface at construction with the same messages
    parse_psid_for_player produces.
"""

from __future__ import annotations

import time
import unittest
from typing import cast
from unittest.mock import patch

from _fakes import FrozenClock, quiet_logging

from c64cast.hw.c64 import cpu_clock
from c64cast.sid.sid_host_emu import (
    SidHostEmu,
    TrappedRam,
    _append_distinct_sid_base,
    _decode_extra_sid_addr,
    detect_sid_addresses,
    parse_sid_header,
    ram_write_footprint,
)

# ---------------------------------------------------------------------------
# Synthetic-SID helper
# ---------------------------------------------------------------------------


def _make_synthetic_sid(
    *,
    init_code: bytes,
    play_code: bytes,
    load_addr: int = 0x0820,
    num_songs: int = 1,
    start_song: int = 1,
    magic: bytes = b"PSID",
) -> bytes:
    """Build a minimal PSID v2 file with INIT at load_addr and PLAY
    immediately after. Returns the full file bytes (124-byte header +
    payload)."""
    payload = init_code + play_code
    play_addr = load_addr + len(init_code)
    h = bytearray(124)
    h[0:4] = magic
    h[4:6] = (2).to_bytes(2, "big")
    h[6:8] = (124).to_bytes(2, "big")  # data_offset
    h[8:10] = load_addr.to_bytes(2, "big")
    h[10:12] = load_addr.to_bytes(2, "big")  # init = load (= RTS in our stubs)
    h[12:14] = play_addr.to_bytes(2, "big")
    h[14:16] = num_songs.to_bytes(2, "big")
    h[16:18] = start_song.to_bytes(2, "big")
    return bytes(h) + payload


# Tiny PLAY that writes recognizable bytes into 4 specific SID slots
# (V1 control, V2 control, V3 control, master volume) and RTSes. Easy
# to verify in the shadow.
_PLAY_WRITES = bytes(
    [
        0xA9,
        0xAA,  # LDA #$AA
        0x8D,
        0x04,
        0xD4,  # STA $D404 (V1 control)
        0xA9,
        0xBB,  # LDA #$BB
        0x8D,
        0x0B,
        0xD4,  # STA $D40B (V2 control)
        0xA9,
        0xCC,  # LDA #$CC
        0x8D,
        0x12,
        0xD4,  # STA $D412 (V3 control)
        0xA9,
        0x0F,  # LDA #$0F
        0x8D,
        0x18,
        0xD4,  # STA $D418 (volume)
        0x60,  # RTS
    ]
)

# INIT is a bare RTS — the host emulator JSRs into load_addr to run it.
_INIT_RTS = bytes([0x60])

# The clock play_rate_hz divides the CIA #1 Timer A latch into.
_NTSC_CLOCK_HZ = cpu_clock("NTSC")


def _init_set_timer_a(latch: int) -> bytes:
    """INIT that programs CIA #1 Timer A latch ($DC04/$DC05) — the mark of a
    CIA-timed (multispeed) tune — then RTSes."""
    lo, hi = latch & 0xFF, (latch >> 8) & 0xFF
    return bytes(
        [
            0xA9,
            lo,
            0x8D,
            0x04,
            0xDC,  # LDA #lo / STA $DC04
            0xA9,
            hi,
            0x8D,
            0x05,
            0xDC,  # LDA #hi / STA $DC05
            0x60,  # RTS
        ]
    )


# Degenerate PLAY: JMP to itself, forever. Used to verify the cycle cap.
# $0821: JMP $0821 (3 bytes). The cycle cap should kick in well before
# the host CPU notices.
_PLAY_INFINITE_LOOP = bytes([0x4C, 0x21, 0x08])

# The same shape for INIT, which is loaded at load_addr ($0820) itself.
_INIT_INFINITE_LOOP = bytes([0x4C, 0x20, 0x08])

# The attack the cycle cap alone does not stop: $02 is one of the 105 opcodes
# py65 leaves on `inst_not_implemented`, which charges 0 cycles. A field of
# them followed by a JMP back to the start spins forever on a budget that
# never advances — measured at 7-21 s per tick_play() before the step bound
# and the illegal-opcode refusal landed.
_PLAY_ILLEGAL_OPCODE_LOOP = bytes([0x02] * 64) + bytes([0x4C, 0x21, 0x08])


class SidHostEmuValidationTest(unittest.TestCase):
    """Validation is shared with run_sid_player via parse_psid_for_player;
    spot-check that it surfaces through SidHostEmu's __init__."""

    def test_rejects_rsid(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES, magic=b"RSID")
        with self.assertRaisesRegex(ValueError, "RSID"):
            SidHostEmu(sid)

    def test_rejects_load_addr_below_basic_stub(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES, load_addr=0x0801)
        with self.assertRaisesRegex(ValueError, "BASIC SYS stub"):
            SidHostEmu(sid)

    def test_rejects_song_out_of_range(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES, num_songs=3)
        with self.assertRaisesRegex(ValueError, "out of range"):
            SidHostEmu(sid, song=99)


class SidHostEmuRegsTest(unittest.TestCase):
    def test_shadow_is_25_bytes(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        self.assertEqual(len(emu.regs()), 25)

    def test_play_writes_land_in_shadow(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        # INIT was a bare RTS so the shadow is still zeros — proves the
        # baseline state isn't accidentally pre-populated.
        self.assertEqual(emu.regs(), bytes(25))

        emu.tick_play()
        shadow = emu.regs()
        # V1 ctl ($D404 → offset 4), V2 ctl ($D40B → 11), V3 ctl
        # ($D412 → 18), volume ($D418 → 24).
        self.assertEqual(shadow[4], 0xAA)
        self.assertEqual(shadow[11], 0xBB)
        self.assertEqual(shadow[18], 0xCC)
        self.assertEqual(shadow[24], 0x0F)

    def test_shadow_only_covers_d400_d418(self):
        # A STA to $D419 (one byte past the shadow window) must NOT be
        # written into the shadow. Tests TrappedRam's upper bound.
        play = bytes(
            [
                0xA9,
                0xEE,
                0x8D,
                0x19,
                0xD4,  # STA $D419
                0x60,
            ]
        )
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        emu = SidHostEmu(sid)
        emu.tick_play()
        # The shadow stays zeros — $D419 is outside the SID register file.
        self.assertEqual(emu.regs(), bytes(25))


class SidHostEmuCycleCapTest(unittest.TestCase):
    """A degenerate PLAY must give up and say so.

    `last_routine_capped` is the post-condition that matters: it is what
    `preflight_emu` reads to refuse a tune that would dead-machine the
    C64-side player. Asserting only "the call returned" leaves a partial fix
    green — which is how a budget that bounded cycles but not wall time
    survived."""

    # A degenerate PLAY is allowed a generous share of a test run, but not an
    # open-ended one: without a bound the failure mode is a CI timeout rather
    # than an assertion.
    _TICK_BUDGET_S = 5.0

    def test_infinite_play_returns_via_cycle_cap(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_INFINITE_LOOP)
        emu = SidHostEmu(sid)
        started = time.monotonic()
        emu.tick_play()
        self.assertTrue(emu.last_routine_capped, "a spinning PLAY must report itself capped")
        # Subsequent ticks must still terminate, and still report the cap.
        for _ in range(3):
            emu.tick_play()
            self.assertTrue(emu.last_routine_capped)
        self.assertLess(time.monotonic() - started, self._TICK_BUDGET_S)

    def test_illegal_opcode_ends_the_pass_without_condemning_the_tune(self):
        # py65 charges 0 cycles for undocumented opcodes and advances the PC
        # by 2 regardless of the real instruction length, so executing one
        # buys free host time AND derails the instruction stream (a wrong
        # $D4xx shadow and a wrong write footprint). The pass therefore ends
        # here — but NOT as `last_routine_capped`, which is preflight_emu's
        # "this tune would dead-machine the C64" verdict. LAX/SAX/SLO in PLAY
        # is a normal 6510 idiom the real chip runs, and gating the tune on
        # py65's instruction coverage refused a large share of HVSC.
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_ILLEGAL_OPCODE_LOOP)
        emu = SidHostEmu(sid)
        started = time.monotonic()
        with self.assertLogs("c64cast.sid.sid_host_emu", level="WARNING") as logs:
            emu.tick_play()
        self.assertFalse(emu.last_routine_capped, "an unrunnable opcode is not a hang")
        self.assertTrue(emu.saw_undecodable_opcode)
        self.assertLess(time.monotonic() - started, self._TICK_BUDGET_S)
        self.assertIn("undocumented opcode $02", "\n".join(logs.output))
        # The warning fires once per emulator, not once per pass — the
        # pre-flight alone runs 50 of them.
        for _ in range(3):
            emu.tick_play()
            self.assertFalse(emu.last_routine_capped)

    def test_illegal_opcode_tune_passes_the_play_preflight(self):
        # The regression this pins: `sid_play_preflight` is what
        # WaveformScene._build_host_emu and SidFileAudioSource._validate_candidate
        # refuse a file on, and a PLAY containing one LAX failed all 50 passes
        # — so a tune that played before stopped playing, with an error
        # blaming a raster spin that was not happening.
        from c64cast.sid.sid_host_emu import sid_play_preflight

        # LAX $10 (undocumented), then the ordinary SID writes and RTS.
        play = bytes([0xA7, 0x10]) + _PLAY_WRITES
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        with self.assertLogs("c64cast.sid.sid_host_emu", level="WARNING") as logs:
            self.assertIsNone(sid_play_preflight(sid))
        self.assertIn("undocumented opcode $A7", "\n".join(logs.output))

    def test_execution_at_the_top_of_memory_wraps_instead_of_raising(self):
        # py65's MPU.WordAt(addr) reads addr+1 without masking, so a routine
        # that lands a 3-byte absolute-addressing opcode at $FFFE asks the
        # 64 KB bytearray for index $10000. That IndexError is not a ValueError
        # and so escaped every "log it and try the next candidate" handler
        # between here and Playlist.run — six bytes of a crafted .sid ended
        # the whole show. The real 6510's address bus wraps; so does ours.
        play = bytes(
            [
                0xA9,
                0xAD,  # LDA #$AD          ($AD = LDA abs, a 3-byte opcode)
                0x8D,
                0xFE,
                0xFF,  # STA $FFFE
                0x4C,
                0xFE,
                0xFF,  # JMP $FFFE
            ]
        )
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        emu = SidHostEmu(sid)
        started = time.monotonic()
        # No warning: the address wrapped, so the interpreter ran the wrapped
        # instruction stream and this pass ended at its ordinary cycle cap.
        # Catching the IndexError further out would satisfy "didn't unwind"
        # while still refusing a tune the real 6510 executes fine.
        with self.assertNoLogs("c64cast.sid.sid_host_emu", level="WARNING"):
            emu.tick_play()
            # And the footprint helpers, which is where it reached the
            # playlist from: they must return a bitmap, not unwind.
            sample = ram_write_footprint(sid, ticks=3)
        self.assertLess(time.monotonic() - started, self._TICK_BUDGET_S)
        self.assertEqual(len(sample.ram), 65536)

    def test_trapped_ram_wraps_the_address_bus_at_64k(self):
        # The unit rule behind the test above, pinned where it lives.
        ram = TrappedRam()
        ram[0x0000] = 0x42
        self.assertEqual(ram[0x10000], 0x42, "a read past $FFFF wraps to $0000")
        ram[0x10001] = 0x99
        self.assertEqual(ram[0x0001], 0x99, "a write past $FFFF wraps too")

    def test_an_exception_out_of_py65_is_reported_as_a_non_terminating_pass(self):
        # Belt and braces for the wrap fix above: py65 is not written against
        # hostile input, and whatever else it may raise must come back as
        # "this routine did not return" — the verdict preflight_emu refuses a
        # tune on — rather than unwinding out of the scene.
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        with patch.object(emu._mpu, "step", side_effect=RuntimeError("boom")):
            with self.assertLogs("c64cast.sid.sid_host_emu", level="WARNING") as logs:
                emu.tick_play()
        self.assertTrue(emu.last_routine_capped)
        self.assertIn("raised out of the 6502 interpreter", "\n".join(logs.output))

    def test_init_is_bounded_by_wall_clock_not_only_by_emulated_cycles(self):
        # INIT's cycle cap is 2 M — the one routine whose budget is measured
        # in millions — and it runs in every constructor, so a tune's analysis
        # paid it up to 18 times with nothing bounding the seconds. Asserted
        # on the emulated-cycle count rather than a stopwatch: stopping at the
        # deadline leaves the cycle count orders of magnitude short of the cap.
        from c64cast.sid.sid_host_emu import _INIT_CYCLE_CAP

        sid = _make_synthetic_sid(init_code=_INIT_INFINITE_LOOP, play_code=_PLAY_WRITES)
        with patch("c64cast.sid.sid_host_emu._INIT_DEADLINE_S", 0.0):
            emu = SidHostEmu(sid)
        self.assertTrue(emu.last_routine_capped)
        self.assertLess(
            emu._mpu.processorCycles,
            _INIT_CYCLE_CAP // 10,
            "INIT must stop at the wall clock, long before its cycle cap",
        )

    def test_completing_play_is_not_reported_as_capped(self):
        # The budget test is re-guarded by the sentinel, so a routine that
        # returns on the very step that crosses a budget still counts as
        # having completed — a capped verdict means partial $D4xx state.
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        emu.tick_play()
        self.assertFalse(emu.last_routine_capped)


class RetriggerDetectionTest(unittest.TestCase):
    """retriggers() recovers hard restarts (gate off→on within one PLAY
    call) that the 25-byte shadow collapses to gate-still-high."""

    def test_intra_tick_gate_pulse_flags_retrigger(self):
        # PLAY writes V1 control gate-LOW ($40 pulse, gate=0) then gate-HIGH
        # ($41 pulse + gate) — a hard restart within one call. The shadow
        # ends at $41 (gate high), but retriggers() must flag voice 0.
        play = bytes(
            [
                0xA9,
                0x40,
                0x8D,
                0x04,
                0xD4,  # LDA #$40 / STA $D404 (gate low)
                0xA9,
                0x41,
                0x8D,
                0x04,
                0xD4,  # LDA #$41 / STA $D404 (gate high)
                0x60,  # RTS
            ]
        )
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        emu = SidHostEmu(sid)
        emu.tick_play()
        self.assertEqual(emu.regs()[4], 0x41)  # shadow ends gate-high
        self.assertEqual(emu.retriggers(), (True, False, False))

    def test_steady_gate_high_is_not_a_retrigger(self):
        # A voice written gate-high only (no intervening low) is an ordinary
        # held note, not a hard restart.
        play = bytes([0xA9, 0x41, 0x8D, 0x04, 0xD4, 0x60])  # STA $D404 = $41
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        emu = SidHostEmu(sid)
        emu.tick_play()
        self.assertEqual(emu.retriggers(), (False, False, False))

    def test_note_off_ending_gate_low_is_not_a_retrigger(self):
        # Gate written low and left low = a normal note-off (handled by the
        # shadow's gate edge), not a hard restart.
        play = bytes([0xA9, 0x40, 0x8D, 0x04, 0xD4, 0x60])  # STA $D404 = $40
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        emu = SidHostEmu(sid)
        emu.tick_play()
        self.assertEqual(emu.retriggers(), (False, False, False))

    def test_retrigger_flags_reset_each_tick(self):
        # A gate-low flag from a prior tick must not leak forward: tick_play
        # clears the flags, so a steady-gate PLAY reports no retrigger.
        play_steady = bytes([0xA9, 0x41, 0x8D, 0x04, 0xD4, 0x60])  # gate high
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play_steady)
        emu = SidHostEmu(sid)
        emu._memory.gate_low_banks[0][0] = 1  # poison as if a prior tick saw low
        emu.tick_play()
        self.assertEqual(emu.retriggers(), (False, False, False))


class PlayRateTest(unittest.TestCase):
    """play_rate_hz: vsync tunes keep the video rate; CIA-timed tunes that
    program CIA #1 Timer A report clock/(latch+1) so the scope advances the
    song at the same pace the real chip plays it."""

    CLOCK = 1_022_727  # NTSC system clock

    def test_vsync_tune_keeps_video_rate(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        self.assertEqual(emu.play_rate_hz(60.0, self.CLOCK), 60.0)

    def test_cia_timed_tune_uses_timer_a_rate(self):
        # latch chosen so clock/(latch+1) ≈ 120 Hz (a 2x multispeed).
        latch = round(self.CLOCK / 120.0) - 1
        sid = _make_synthetic_sid(init_code=_init_set_timer_a(latch), play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        rate = emu.play_rate_hz(60.0, self.CLOCK)
        self.assertAlmostEqual(rate, self.CLOCK / (latch + 1), places=3)
        self.assertGreater(rate, 60.0)  # genuinely multispeed

    def test_out_of_range_latch_falls_back_to_video_rate(self):
        # A tiny latch implies an absurd >8x rate — reject, keep vsync.
        sid = _make_synthetic_sid(init_code=_init_set_timer_a(50), play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        self.assertEqual(emu.play_rate_hz(60.0, self.CLOCK), 60.0)


class RamWriteFootprintTest(unittest.TestCase):
    """ram_write_footprint marks the RAM a tune writes — used to place the
    relocated C64-side player off the tune's scratch (the Beat_Dis fix)."""

    def test_footprint_marks_scratch_writes(self):
        from c64cast.sid.sid_host_emu import ram_write_footprint

        # PLAY writes a byte to $5000 (scratch) + the SID registers, RTS.
        play = bytes(
            [
                0xA9,
                0x42,  # LDA #$42
                0x8D,
                0x00,
                0x50,  # STA $5000  (scratch)
                0x8D,
                0x04,
                0xD4,  # STA $D404  (a SID reg, for good measure)
                0x60,  # RTS
            ]
        )
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play, load_addr=0x1000)
        fp = ram_write_footprint(sid, ticks=10)
        self.assertTrue(fp.complete, "a cheap PLAY completes every requested pass")
        self.assertEqual(len(fp.ram), 65536)
        self.assertTrue(fp.ram[0x5000], "scratch write must be in the footprint")
        self.assertTrue(fp.ram[0xD404], "SID-reg write must be in the footprint")
        self.assertFalse(fp.ram[0x6000], "untouched RAM must stay clear")

    def test_play_access_footprint_catches_reads_excludes_init(self):
        from c64cast.sid.sid_host_emu import ram_play_access_footprint, ram_write_footprint

        # INIT writes a one-time block to $A000 (a display region) then RTS;
        # PLAY *reads* $B400 (live per-song data, à la Times of Lore) and
        # writes scratch at $5000. The access footprint drives the display-
        # bank choice: it must drop the INIT-only write (paintable), keep the
        # recurring PLAY write, AND — the key fix — catch the PLAY read that
        # the write-only footprint can't see.
        init = bytes(
            [
                0xA9,
                0x55,  # LDA #$55
                0x8D,
                0x00,
                0xA0,  # STA $A000  (one-time INIT scratch)
                0x60,  # RTS
            ]
        )
        play = bytes(
            [
                0xAD,
                0x00,
                0xB4,  # LDA $B400  (read live per-song data)
                0x8D,
                0x00,
                0x50,  # STA $5000  (recurring PLAY scratch)
                0x60,  # RTS
            ]
        )
        sid = _make_synthetic_sid(init_code=init, play_code=play, load_addr=0x1000)
        # The write-only footprint marks the INIT write but NOT the PLAY read.
        full = ram_write_footprint(sid, ticks=10).ram
        self.assertTrue(full[0xA000], "INIT write present in full footprint")
        self.assertFalse(full[0xB400], "write footprint can't see the read")

        access = ram_play_access_footprint(sid, ticks=10).ram
        self.assertFalse(access[0xA000], "INIT-only write must be excluded from access view")
        self.assertTrue(access[0x5000], "recurring PLAY write must be in the access view")
        self.assertTrue(access[0xB400], "PLAY read must be in the access view (the ToL fix)")

    def test_access_tracking_disabled_by_default(self):
        # The normal scope path constructs without access tracking.
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        self.assertIsNone(emu._memory.access)

    def test_footprint_disabled_by_default(self):
        # The normal scope path constructs without tracking → no footprint.
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        self.assertIsNone(emu._memory.footprint)

    def test_footprint_run_stops_at_its_wall_clock_budget(self):
        # The per-pass cycle cap bounds one PLAY, not 2000 of them: a tune
        # whose PLAY legally burns just under the cap costs ~12 s per
        # footprint run, and setup() pays two plus one per subtune. A zero
        # budget stands in for that tune — the run must stop early, say so,
        # return a usable partial bitmap, AND report itself incomplete so the
        # callers that place hardware on it can tell.
        from c64cast.sid.sid_host_emu import ram_write_footprint

        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        with patch("c64cast.sid.sid_host_emu.FOOTPRINT_DEADLINE_S", 0.0):
            with self.assertLogs("c64cast.sid.sid_host_emu", level="WARNING") as logs:
                fp = ram_write_footprint(sid, ticks=500)
        self.assertIn("stopped after 1 of 500 PLAY passes", "\n".join(logs.output))
        self.assertFalse(fp.complete, "a truncated sample must not read as a full one")
        self.assertEqual(len(fp.ram), 65536)
        self.assertTrue(fp.ram[0xD418], "the partial sample still records what PLAY did write")

    def test_one_budget_bounds_every_run_of_a_tunes_analysis(self):
        # The per-run deadline bounds one call, not the call count, and the
        # count is set by the file: setup() pays two runs plus one per
        # subtune, so 18 runs used to draw 18 fresh deadlines (a 306-byte
        # PSID declaring 16 subtunes measured 43 s of blocked main thread).
        # A shared budget is what makes the walk cost one budget in total.
        from c64cast.sid.sid_host_emu import HostEmuBudget, ram_write_footprint

        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        # A fake clock that advances 1 s per reading: deterministic, and no
        # test spends wall time proving a wall-clock rule.
        ticks = iter(range(10_000))
        budget = HostEmuBudget(2.0, clock=lambda: float(next(ticks)))
        with self.assertLogs("c64cast.sid.sid_host_emu", level="WARNING"):
            first = ram_write_footprint(sid, ticks=500, budget=budget)
            second = ram_write_footprint(sid, ticks=500, budget=budget)
        self.assertFalse(first.complete)
        self.assertFalse(second.complete, "the second run must inherit the spent budget")
        self.assertTrue(budget.expired())

    def test_budget_caps_a_run_at_whichever_deadline_comes_first(self):
        # deadline_for is the arithmetic the whole scheme rests on: a run gets
        # its own cap or what is left of the shared budget, whichever is
        # sooner. Asserted directly so no test has to sleep to prove it.
        from c64cast.sid.sid_host_emu import HostEmuBudget

        now = 100.0
        budget = HostEmuBudget(6.0, clock=lambda: now)
        self.assertEqual(budget.remaining(), 6.0)
        self.assertEqual(budget.deadline_for(2.0), 102.0, "the per-run cap binds first")
        self.assertEqual(budget.deadline_for(9.0), 106.0, "the shared budget binds first")
        self.assertFalse(budget.expired())
        now = 107.0
        self.assertTrue(budget.expired())
        self.assertEqual(budget.deadline_for(2.0), 106.0, "a spent budget grants no more time")

    def test_undocumented_opcode_marks_the_footprint_incomplete(self):
        # The pass stops at the opcode, so every pass stops at the same place
        # and the bitmap is a prefix of what the tune really touches. The tune
        # still plays (preflight accepts it), but api._find_free_layout must
        # not be handed a prefix as if it were the whole story.
        from c64cast.sid.sid_host_emu import ram_write_footprint

        play = bytes([0xA9, 0x0F, 0x8D, 0x18, 0xD4]) + _PLAY_ILLEGAL_OPCODE_LOOP
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        with self.assertLogs("c64cast.sid.sid_host_emu", level="WARNING"):
            fp = ram_write_footprint(sid, ticks=5)
        self.assertFalse(fp.complete, "a prefix footprint must not read as a complete one")
        self.assertTrue(fp.ram[0xD418], "what ran before the opcode is still recorded")


class HostEmuClockDomainTest(unittest.TestCase):
    """A deadline is only meaningful in the clock that produced it.

    `HostEmuBudget.deadline_for` returns an instant on the budget's own
    (injectable) clock; `_run_routine` used to compare it against
    `time.monotonic()`. With the default clock the two coincide, so nothing
    shipped wrong — but every test that injected a clock was measuring
    something the code does not do, and any test combining an injected clock
    with a routine that runs long enough to reach a wall-clock check would
    have silently proved nothing."""

    # Far enough in the future that time.monotonic() cannot reach it inside a
    # test run, so a routine bounded by the WRONG clock never stops at all.
    _FAKE_NOW = 1.0e9

    def test_init_stops_at_the_deadline_measured_on_the_budgets_own_clock(self):
        from c64cast.sid.sid_host_emu import _INIT_CYCLE_CAP, HostEmuBudget

        sid = _make_synthetic_sid(init_code=_INIT_INFINITE_LOOP, play_code=_PLAY_WRITES)
        # A budget already spent, on a clock whose instants are ~30 years past
        # anything time.monotonic() will report during this test.
        budget = HostEmuBudget(0.0, clock=lambda: self._FAKE_NOW)
        emu = SidHostEmu(sid, budget=budget)
        self.assertTrue(emu.last_routine_capped)
        # Reading time.monotonic() here would compare a small number against
        # _FAKE_NOW, never trip, and let INIT run to its 2 M-cycle cap.
        self.assertLess(
            emu._mpu.processorCycles,
            _INIT_CYCLE_CAP // 10,
            "INIT must stop at the deadline, not run on to the cycle cap",
        )

    def test_a_play_pass_stops_on_the_budgets_clock_too(self):
        from c64cast.sid.sid_host_emu import _PLAY_CYCLE_CAP, HostEmuBudget

        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_INFINITE_LOOP)
        budget = HostEmuBudget(0.0, clock=lambda: self._FAKE_NOW)
        emu = SidHostEmu(sid, budget=budget)
        emu.tick_play(budget.deadline_for(1.0))
        self.assertTrue(emu.last_routine_capped)
        # Without the deadline binding, this JMP-to-itself PLAY runs to its
        # full cycle cap; half of that is comfortably above the wall-clock
        # check granularity and comfortably below the cap.
        self.assertLess(
            emu._mpu.processorCycles,
            _PLAY_CYCLE_CAP // 2,
            "a PLAY pass given a deadline must read the same clock INIT does",
        )


class TruncatedRoutineMakesAFootprintIncompleteTest(unittest.TestCase):
    """`complete` has to mean "nothing about this sample was cut short", and a
    capped routine is the way a sample is cut short that the tick loop cannot
    see: INIT ran in the constructor, before the loop existed.

    Reading only the wall clock and the undecodable-opcode flag meant a tune
    whose INIT hit the 2 M-cycle cap — a fat decompressor, or one bounded by a
    shared budget — reported a full footprint made of the bytes it had written
    up to the cap, and api._find_free_layout put the relocated player MC in
    what the rest of INIT was about to fill."""

    def _init_that_writes_after_a_long_delay(self) -> bytes:
        # A ~390 k-cycle countdown loop, then STA $C000, then RTS. Under the
        # real INIT cycle cap it finishes and $C000 is marked; capped, it is
        # not, and the footprint that omits it looks like free RAM.
        return bytes(
            [
                0xA2,
                0xFF,  # LDX #$FF
                0xA0,
                0xFF,  # LDY #$FF        (outer)
                0x88,  # DEY             (inner)
                0xD0,
                0xFD,  # BNE inner
                0xCA,  # DEX
                0xD0,
                0xF8,  # BNE outer
                0xA9,
                0x42,  # LDA #$42
                0x8D,
                0x00,
                0xC0,  # STA $C000
                0x60,  # RTS
            ]
        )

    def test_a_full_init_marks_the_late_write_and_reports_complete(self):
        # The control: nothing is capped, so the footprint is the whole story.
        from c64cast.sid.sid_host_emu import ram_write_footprint

        sid = _make_synthetic_sid(
            init_code=self._init_that_writes_after_a_long_delay(), play_code=_PLAY_WRITES
        )
        fp = ram_write_footprint(sid, ticks=3)
        self.assertTrue(fp.ram[0xC000], "INIT ran to completion, so its last write is recorded")
        self.assertTrue(fp.complete)

    def test_a_capped_init_reports_the_footprint_incomplete(self):
        from c64cast.sid.sid_host_emu import _INIT_CYCLE_CAP, ram_write_footprint

        sid = _make_synthetic_sid(
            init_code=self._init_that_writes_after_a_long_delay(), play_code=_PLAY_WRITES
        )
        # Stand in for a decompressor that outruns the real 2 M-cycle cap.
        with patch("c64cast.sid.sid_host_emu._INIT_CYCLE_CAP", 5_000):
            fp = ram_write_footprint(sid, ticks=3)
        self.assertLess(5_000, _INIT_CYCLE_CAP, "the patch must be a tightening, not the default")
        self.assertFalse(fp.ram[0xC000], "the write past the cap is genuinely missing")
        self.assertFalse(fp.complete, "a footprint missing INIT's tail must say so")

    def test_a_capped_init_deadline_reports_the_footprint_incomplete(self):
        # Same fact by the other route INIT can be cut short: the wall clock.
        from c64cast.sid.sid_host_emu import ram_write_footprint

        sid = _make_synthetic_sid(
            init_code=self._init_that_writes_after_a_long_delay(), play_code=_PLAY_WRITES
        )
        with patch("c64cast.sid.sid_host_emu._INIT_DEADLINE_S", 0.0):
            fp = ram_write_footprint(sid, ticks=3)
        self.assertFalse(fp.ram[0xC000])
        self.assertFalse(fp.complete, "a footprint missing INIT's tail must say so")

    def test_a_capped_play_reports_the_footprint_incomplete(self):
        # And by the third: a PLAY that never returns. Every pass stops in the
        # same place, so the bitmap is a prefix of one PLAY.
        from c64cast.sid.sid_host_emu import ram_write_footprint

        play = bytes([0xA9, 0x0F, 0x8D, 0x18, 0xD4]) + bytes([0x4C, 0x26, 0x08])
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        fp = ram_write_footprint(sid, ticks=2)
        self.assertTrue(fp.ram[0xD418], "what ran before the spin is still recorded")
        self.assertFalse(fp.complete, "a PLAY that never returns is a prefix sample")


class AnalyzePlacementTest(unittest.TestCase):
    """analyze_placement is the only way the two whole-tune consumers reach a
    footprint, so the trust decision cannot be skipped at a call site. It was
    skipped at two of them: both logged the warning and then handed the prefix
    to api._find_free_layout and _choose_display_layout anyway."""

    # INIT writes $B400 (live song data under BASIC ROM, the Times of Lore
    # shape) and $A000 (one-time scratch a bitmap may paint over); PLAY reads
    # $B400 back and scratches $5000.
    _INIT = bytes(
        [
            0xA9,
            0x55,  # LDA #$55
            0x8D,
            0x00,
            0xB4,  # STA $B400
            0x8D,
            0x00,
            0xA0,  # STA $A000
            0x60,  # RTS
        ]
    )
    _PLAY = bytes(
        [
            0xAD,
            0x00,
            0xB4,  # LDA $B400
            0x8D,
            0x00,
            0x50,  # STA $5000
        ]
    )

    def _sid(self, *, truncated: bool) -> bytes:
        # $02 is an opcode py65 cannot execute: the pass ends there, so the
        # sample is a prefix — the commonest way a real HVSC tune produces one.
        tail = bytes([0x02]) if truncated else bytes([0x60])
        return _make_synthetic_sid(init_code=self._INIT, play_code=self._PLAY + tail)

    def test_a_trusted_sample_keeps_the_two_views_apart(self):
        from c64cast.sid.sid_host_emu import HostEmuBudget, analyze_placement

        placement = analyze_placement(
            self._sid(truncated=False), song=1, budget=HostEmuBudget(), what="unit test"
        )
        self.assertTrue(placement.trusted)
        self.assertTrue(placement.avoid[0xA000], "INIT scratch belongs in the player-avoid view")
        self.assertFalse(
            placement.display[0xA000],
            "INIT-only scratch is paintable, so the display view excludes it",
        )
        self.assertTrue(placement.display[0xB400], "PLAY reads $B400 every frame — it is live")
        self.assertEqual(placement.play_bank, 0x36, "PLAY reads RAM the tune wrote under BASIC")

    def test_a_prefix_sample_widens_both_views_and_drops_the_play_bank(self):
        from c64cast.sid.sid_host_emu import HostEmuBudget, analyze_placement

        with self.assertLogs("c64cast.sid.sid_host_emu", level="WARNING") as logs:
            placement = analyze_placement(
                self._sid(truncated=True), song=1, budget=HostEmuBudget(), what="unit test"
            )
        self.assertFalse(placement.trusted)
        self.assertIn("only partially footprinted", "\n".join(logs.output))
        # The display view is no longer allowed its INIT-scratch concession:
        # what was paintable on a complete sample is off-limits on a prefix.
        self.assertTrue(
            placement.display[0xA000],
            "an untrusted display view widens to everything the tune touched",
        )
        self.assertEqual(placement.avoid, placement.display, "both views become the union")
        self.assertIsNone(
            placement.play_bank, "a $01 bank derived from a prefix is a guess, not a finding"
        )
        # assertFalse on the identity, not assertIsNot: the latter's failure
        # message renders both 64 KB bitmaps into the test output.
        self.assertFalse(
            placement.avoid is placement.display,
            "equal is not identical: an in-place mark on one must not rewrite the other",
        )

    def test_the_widening_says_nothing_about_the_tail_an_opcode_cut_off(self):
        """The limit, pinned so it cannot be re-claimed as a safety property.

        Both footprint runs execute the same 6502 code for the same tick count,
        so an undocumented opcode truncates both at the identical instruction.
        The union therefore carries only the read-versus-write difference
        between two samples that stopped in the same place — it says nothing
        about the writes past the cut. Nothing downstream reliably makes up the
        difference either — api._choose_player_layout takes the fixed
        $C300/$C400 layout on a bare non-overlap check before it ever reaches
        _find_free_layout's largest-hole margin. See analyze_placement's
        docstring, which carries the whole argument and the measurements.
        """
        from c64cast.sid.sid_host_emu import (
            HostEmuBudget,
            analyze_placement,
            ram_play_access_footprint,
            ram_write_footprint,
        )

        # LDA #$AA / STA $2000 / LAX $3000 / STA $4000 / RTS: $4000 is written
        # on every real PLAY, and sits past an opcode py65 will not execute.
        play = bytes(
            [
                0xA9,
                0xAA,  # LDA #$AA
                0x8D,
                0x00,
                0x20,  # STA $2000  -- traced
                0xAF,
                0x00,
                0x30,  # LAX $3000  -- undocumented; the pass ends here
                0x8D,
                0x00,
                0x40,  # STA $4000  -- the untraced tail
                0x60,  # RTS
            ]
        )
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        # Incidental WARNING from each run; asserted in
        # TruncatedRoutineMakesAFootprintIncompleteTest.
        with quiet_logging():
            write = ram_write_footprint(sid, song=1)
            access = ram_play_access_footprint(sid, song=1)
        self.assertFalse(write.complete)
        self.assertFalse(access.complete)
        self.assertTrue(write.ram[0x2000], "the write before the opcode is traced")
        self.assertFalse(write.ram[0x4000], "the write after it is not, in either sample")
        self.assertFalse(access.ram[0x4000])

        # The measurement analyze_placement's docstring and
        # docs/architecture/sid.md both quote. $0820 is the load address and
        # INIT is one byte, so PLAY starts at $0821 and these five bytes are
        # the LDA and the STA that ran before the LAX -- fetched as reads,
        # which is the whole of what the union adds on this tune.
        differ = [a for a in range(0x10000) if bool(write.ram[a]) != bool(access.ram[a])]
        self.assertEqual(
            differ,
            [0x0821, 0x0822, 0x0823, 0x0824, 0x0825],
            "the union's contribution here is the traced prefix's own code bytes",
        )

        with self.assertLogs("c64cast.sid.sid_host_emu", level="WARNING"):
            placement = analyze_placement(sid, song=1, budget=HostEmuBudget(), what="unit test")
        self.assertFalse(placement.trusted)
        self.assertFalse(
            placement.avoid[0x4000],
            "the union cannot widen onto a write neither sample reached",
        )


class InitTruncationNoticeTest(unittest.TestCase):
    """A truncated INIT leaves the emulator the scene renders FROM holding a
    prefix of the tune's register state, and until this helper nothing above
    DEBUG said so: the footprint's `complete` flag covers placement and the
    pre-flight covers a non-terminating PLAY, and neither covers the render
    emulator."""

    def test_a_healthy_init_gets_no_notice(self):
        from c64cast.sid.sid_host_emu import init_truncation_notice

        emu = SidHostEmu(_make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES))
        self.assertIsNone(init_truncation_notice(emu))

    def test_an_init_out_of_wall_clock_says_so_and_says_whose_clock(self):
        # Naming "its bound" covered both bounds with one phrase, and they
        # call for different responses: a spent wall clock can be another
        # candidate's doing and re-running the tune alone may be clean, while
        # a cycle cap is the tune's own INIT and will reach it every time.
        from c64cast.sid.sid_host_emu import HostEmuBudget, init_truncation_notice

        sid = _make_synthetic_sid(init_code=_INIT_INFINITE_LOOP, play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid, budget=HostEmuBudget(0.0, clock=lambda: 1.0e9))
        notice = init_truncation_notice(emu)
        assert notice is not None
        self.assertIn("INIT did not run to completion", notice)
        self.assertIn("wall-clock deadline", notice)
        self.assertIn("what was left of the analysis budget", notice)
        self.assertIn("an earlier candidate can be what spent it", notice)
        self.assertIn("register state", notice)
        self.assertIn("PLAY rate", notice)

    def test_a_budget_less_run_does_not_blame_a_pool_walk(self):
        # The SHIFT cue path builds emulators with no budget on purpose, so a
        # cue is not charged to the walk's budget — and it surfaces this same
        # notice through _report_init_truncation. There is no shared budget on
        # that path and no pool walk, so naming one is a cause that cannot
        # exist. Narrow to reach (the 2 M-cycle cap normally wins first) and
        # unconditionally wrong when it does.
        from c64cast.sid import sid_host_emu

        sid = _make_synthetic_sid(init_code=_INIT_INFINITE_LOOP, play_code=_PLAY_WRITES)
        with patch.object(sid_host_emu, "_INIT_DEADLINE_S", 0.0):
            emu = SidHostEmu(sid)  # no budget=
        notice = sid_host_emu.init_truncation_notice(emu)
        assert notice is not None
        self.assertIn("wall-clock deadline", notice)
        self.assertIn("this run's own", notice)
        self.assertIn("nothing but this tune spent it", notice)
        self.assertNotIn("earlier candidate", notice)

    def test_a_fresh_budget_does_not_blame_a_pool_walk_either(self):
        # The predicate is which of the two instants `deadline_for` takes the
        # min of actually fired, not whether a budget was passed at all —
        # asking the second question gets this case wrong, and it is the common
        # one. A fresh HostEmuBudget has the whole analysis budget left, so the
        # per-run cap wins the min and nothing shared was spent.
        # SidFeatureStream builds exactly this: a private per-tune budget with
        # no pool walk anywhere on its path, and it surfaces the notice at
        # WARNING immediately.
        from c64cast.sid import sid_host_emu

        sid = _make_synthetic_sid(init_code=_INIT_INFINITE_LOOP, play_code=_PLAY_WRITES)
        budget = sid_host_emu.HostEmuBudget()
        self.assertGreater(budget.remaining(), sid_host_emu._INIT_DEADLINE_S)
        with patch.object(sid_host_emu, "_INIT_DEADLINE_S", 0.0):
            emu = SidHostEmu(sid, budget=budget)
        notice = sid_host_emu.init_truncation_notice(emu)
        assert notice is not None
        self.assertIn("this run's own", notice)
        self.assertNotIn("earlier candidate", notice)

    def test_neither_arm_claims_a_budget_was_or_was_not_shared(self):
        # A budget is threaded, never flagged: `analyze_placement` passes one
        # private budget through two footprint runs, and a candidate walk
        # passes one through candidates, with nothing to tell them apart. So
        # the wording says what follows *if* the budget is being shared, and
        # the other arm says which instant won rather than that no budget
        # exists — the same overclaim, in the other direction, as the predicate
        # this replaced.
        from c64cast.sid import sid_host_emu

        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        budget = sid_host_emu.HostEmuBudget()
        emu = SidHostEmu(sid, budget=budget)
        shared = emu._deadline_provenance(budget.deadline, 1.0)
        own = emu._deadline_provenance(budget.deadline + 1.0, 1.0)
        self.assertIn("if that budget is being shared", shared)
        self.assertNotIn("no shared budget", own)
        self.assertNotIn("not a shared budget", own)

    def test_the_cap_named_is_the_one_the_deadline_came_from(self):
        # `_run_routine` runs with two per-run caps — 1 s for INIT and 0.05 s
        # for a PLAY pass — so a message reading `_INIT_DEADLINE_S` quotes a
        # figure 20x wrong on the PLAY path. Latent only because the cause is
        # read solely for INIT today.
        from c64cast.sid import sid_host_emu

        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        self.assertIn("0.05s cap", emu._deadline_provenance(1.0, sid_host_emu._PLAY_DEADLINE_S))
        self.assertIn("1s cap", emu._deadline_provenance(1.0, sid_host_emu._INIT_DEADLINE_S))

    def test_a_play_pass_that_ends_on_its_own_cap_quotes_the_play_cap(self):
        # Through tick_play rather than the helper, because what has to be
        # right is the cap `tick_play` supplies when a caller names none —
        # asserting on the helper alone left that default free to be the INIT
        # one, which is the 20x-wrong figure.
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_INFINITE_LOOP)
        emu = SidHostEmu(sid)
        emu.tick_play(deadline=emu._now() - 1.0)
        self.assertTrue(emu.last_routine_capped)
        cause = emu._routine_end_cause
        assert cause is not None
        self.assertIn("0.05s cap", cause)
        self.assertNotIn("1s cap", cause)

    def test_the_cap_is_read_when_the_routine_runs_not_when_the_file_loads(self):
        # A default argument expression is evaluated at definition time, so
        # binding the constant there made patching it a silent no-op and the
        # message quoted whatever the value was at import. That is the same
        # false green the message itself is about.
        from c64cast.sid import sid_host_emu

        sid = _make_synthetic_sid(init_code=_INIT_INFINITE_LOOP, play_code=_PLAY_WRITES)
        with patch.object(sid_host_emu, "_INIT_DEADLINE_S", 0.25):
            emu = SidHostEmu(sid)
        assert emu.init_truncation is not None
        self.assertIn("0.25s cap", emu.init_truncation)

    def test_an_init_out_of_cycles_names_the_cap_instead(self):
        # The cap patched down rather than a 2 M-cycle INIT emulated for real:
        # which bound the code reports is the subject, and the cap's value is
        # not.
        from c64cast.sid import sid_host_emu

        sid = _make_synthetic_sid(init_code=_INIT_INFINITE_LOOP, play_code=_PLAY_WRITES)
        with patch.object(sid_host_emu, "_INIT_CYCLE_CAP", 200):
            emu = SidHostEmu(sid)
        notice = sid_host_emu.init_truncation_notice(emu)
        assert notice is not None
        self.assertIn("cycle/step cap", notice)
        self.assertIn("cap 200", notice)
        self.assertNotIn("wall-clock", notice)

    def test_an_init_stopped_at_an_undocumented_opcode_is_reported_too(self):
        # The most common way an INIT stops short, and the one a reading of
        # `any_routine_capped` was silent on: an undocumented opcode sets
        # `saw_undecodable_opcode` and NO capped flag, on purpose, because the
        # real 6510 executes it. The tune is fine; the register state sampled
        # from this emulator still is not.
        from c64cast.sid.sid_host_emu import init_truncation_notice

        # $0B (ANC #imm) is one of the 105 opcodes py65 leaves unimplemented.
        sid = _make_synthetic_sid(init_code=bytes([0x0B, 0x00, 0x60]), play_code=_PLAY_WRITES)
        with quiet_logging():
            emu = SidHostEmu(sid)
        self.assertFalse(emu.any_routine_capped, "an opcode ending sets no capped flag")
        self.assertTrue(emu.saw_undecodable_opcode)
        notice = init_truncation_notice(emu)
        assert notice is not None
        self.assertIn("undocumented opcode $0B", notice)

    def test_a_later_capped_pass_does_not_become_a_truncated_init(self):
        # `any_routine_capped` is sticky, so reading it stopped meaning "the
        # INIT" the moment the caller ticked. The verdict is frozen in the
        # constructor instead, which is what lets the two live callers report
        # after their pre-flight rather than before it.
        from c64cast.sid.sid_host_emu import init_truncation_notice

        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_INFINITE_LOOP)
        emu = SidHostEmu(sid)
        self.assertIsNone(init_truncation_notice(emu), "INIT itself was clean")
        emu.tick_play()
        self.assertTrue(emu.any_routine_capped, "the pass did cap")
        self.assertIsNone(
            init_truncation_notice(emu),
            "a capped PLAY pass is not a truncated INIT",
        )


class PreflightBudgetTest(unittest.TestCase):
    """The pre-flight is an INIT plus 50 PLAY passes and the tune prices both.
    Bounding it in passes alone left ~1.1 s per candidate, re-paid for every
    candidate of a pool walk."""

    def test_the_budget_stops_the_pass_loop_and_says_which_refusal_it_is(self):
        from c64cast.sid.sid_host_emu import (
            PREFLIGHT_TICKS,
            HostEmuBudget,
            SidHostEmu,
            play_preflight_failure,
        )

        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        # A clock that advances 1 s per reading spends a 3 s budget within a
        # few passes — deterministically, without the test sleeping.
        ticks = iter(range(10_000))
        budget = HostEmuBudget(3.0, clock=lambda: float(next(ticks)))
        emu = SidHostEmu(sid, budget=budget)
        # This PLAY returns, so a healthy pre-flight accepts on pass 1. Force
        # every pass to look non-terminating so the loop runs to a bound.
        with patch.object(SidHostEmu, "tick_play", autospec=True) as tick:

            def _capped(self_, deadline=None):
                self_.last_routine_capped = True

            tick.side_effect = _capped
            refusal = play_preflight_failure(emu, PREFLIGHT_TICKS, budget)
        assert refusal is not None
        self.assertIn("could not be pre-flighted", refusal)
        self.assertLess(
            tick.call_count,
            PREFLIGHT_TICKS,
            "the budget must stop the loop well before the pass count does",
        )

    def test_without_a_budget_the_verdict_is_still_about_the_tune(self):
        from c64cast.sid.sid_host_emu import PREFLIGHT_TICKS, SidHostEmu, play_preflight_failure

        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_INFINITE_LOOP)
        emu = SidHostEmu(sid)
        refusal = play_preflight_failure(emu, PREFLIGHT_TICKS)
        assert refusal is not None
        self.assertIn("spins on a raster/IRQ", refusal)


class CatchupBoundTest(unittest.TestCase):
    """`run_catchup_passes` runs a PLAY pass before it consults the clock,
    because a pass is indivisible — truncating one leaves the $D4xx shadow
    holding half a frame's writes. So `seconds` cannot bound a batch below the
    cost of one pass, and a tune that programs CIA #1 Timer A for 400 Hz sets
    both sides of that comparison: 1.25 ms of allowance against a 10 ms pass
    ran the poll thread back to back for the scene's whole duration."""

    def _emu(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        return SidHostEmu(sid)

    def test_a_batch_that_fits_reports_no_overrun(self):
        from c64cast.sid.sid_host_emu import run_catchup_passes

        result = run_catchup_passes(self._emu(), lambda: None, ticks=3, seconds=60.0)
        self.assertEqual(result.passes, 3)
        self.assertFalse(result.overran)

    def test_one_pass_outlasting_the_whole_bound_is_reported(self):
        # The case a pass count cannot express: every pass asked for ran, and
        # the bound was still blown — by the first pass, on its own.
        from c64cast.sid.sid_host_emu import run_catchup_passes

        result = run_catchup_passes(self._emu(), lambda: None, ticks=1, seconds=0.0)
        self.assertEqual(result.passes, 1, "a batch always makes progress")
        self.assertTrue(result.overran, "one indivisible pass outlasting the bound must be loud")

    def test_a_short_batch_is_still_a_short_batch(self):
        from c64cast.sid.sid_host_emu import run_catchup_passes

        result = run_catchup_passes(self._emu(), lambda: None, ticks=9, seconds=0.0)
        self.assertEqual(result.passes, 1)
        self.assertTrue(result.overran)


class SustainablePollPeriodTest(unittest.TestCase):
    """The bound that makes `run_catchup_passes`' bound bind: stretch the poll
    period until one measured PLAY pass fits inside its allowed fraction."""

    def test_a_cheap_pass_leaves_the_tunes_own_period_alone(self):
        from c64cast.sid.sid_host_emu import sustainable_poll_period_s

        # 60 Hz song, a 1 ms pass, half a period allowed: 0.001/0.5 = 0.002 s,
        # under the 1/60 s period, so nothing is stretched.
        self.assertAlmostEqual(sustainable_poll_period_s(1.0 / 60.0, 0.001, 0.5), 1.0 / 60.0)

    def test_an_expensive_pass_stretches_the_period_to_fit(self):
        from c64cast.sid.sid_host_emu import sustainable_poll_period_s

        # The 400 Hz multispeed case: a 2.5 ms period against a 10 ms pass.
        # Half a period must hold a whole pass, so the period becomes 20 ms.
        self.assertAlmostEqual(sustainable_poll_period_s(0.0025, 0.010, 0.5), 0.020)

    def test_a_pass_too_quick_for_the_clock_needs_no_floor(self):
        from c64cast.sid.sid_host_emu import sustainable_poll_period_s

        # 0.0 comes back from a pass that DID run and was faster than the host
        # clock could resolve. Nothing to stretch for.
        self.assertAlmostEqual(sustainable_poll_period_s(0.004, 0.0, 0.5), 0.004)

    def test_a_pass_that_was_never_timed_is_charged_the_worst_legal_one(self):
        from c64cast.sid.sid_host_emu import (
            UNMEASURED_PASS_COST_S,
            sustainable_poll_period_s,
        )

        # None is not 0.0, and this is the whole reason the two are different
        # values. The only way to reach None is a budget already spent on this
        # tune, which is evidence of an expensive tune, not a free one — so it
        # is charged the worst a legal pass can cost rather than nothing.
        self.assertAlmostEqual(
            sustainable_poll_period_s(0.0025, None, 0.5), UNMEASURED_PASS_COST_S / 0.5
        )


class _SlowProbe:
    """A PLAY pass of a known cost, which truncates at a deadline exactly as
    `SidHostEmu.tick_play` does. A MagicMock cannot stand in here: its pass is
    free, so it prices the same whether or not the caller deadlines it — which
    is the very difference under test."""

    def __init__(self, clock: FrozenClock, cost_s: float) -> None:
        self._clock = clock
        self._cost_s = cost_s
        self.deadlines: list[float | None] = []

    def play_rate_hz(self, video_hz: float, clock_hz: float) -> float:
        return video_hz

    def tick_play(self, deadline: float | None = None) -> None:
        self.deadlines.append(deadline)
        spend = self._cost_s
        if deadline is not None:
            spend = min(spend, max(0.0, deadline - self._clock.monotonic()))
        self._clock.advance(spend)


class DetectPlayRateTest(unittest.TestCase):
    """The shared PLAY-rate probe. It prices a pass before it reads the rate,
    because the tunes whose rate is known earliest are the ones whose cost
    matters most."""

    def _probe(self, init_code):
        from c64cast.sid.sid_host_emu import HostEmuBudget, SidHostEmu

        sid = _make_synthetic_sid(init_code=init_code, play_code=_PLAY_WRITES)
        budget = HostEmuBudget()
        return SidHostEmu(sid, budget=budget), budget

    def test_a_tune_timed_by_init_is_still_priced(self):
        """Regression. A tune that programs CIA #1 Timer A from INIT has its
        rate known the moment INIT returns, so the old loop broke out before
        timing anything and reported a cost of 0.0 — which the sizing function
        read as free. The floor was therefore skipped on exactly the tunes it
        was written for: a 399.3 Hz tune kept its 2.5 ms poll period against a
        pass nobody had priced."""
        from c64cast.sid.sid_host_emu import detect_play_rate_hz

        probe, budget = self._probe(_init_set_timer_a(0x0A00))
        rate, pass_cost_s = detect_play_rate_hz(
            probe, video_hz=60.0, clock_hz=_NTSC_CLOCK_HZ, budget=budget
        )
        self.assertGreater(rate, 300.0, "an INIT-programmed Timer A is multispeed")
        self.assertIsNotNone(pass_cost_s, "the rate being known early is not a reason to skip")

    def test_a_vsync_tune_runs_every_pass_and_keeps_the_video_rate(self):
        from c64cast.sid.sid_host_emu import RATE_PROBE_TICKS, detect_play_rate_hz

        probe, budget = self._probe(_INIT_RTS)
        with patch.object(probe, "tick_play", wraps=probe.tick_play) as ticked:
            rate, pass_cost_s = detect_play_rate_hz(
                probe, video_hz=60.0, clock_hz=_NTSC_CLOCK_HZ, budget=budget
            )
        self.assertAlmostEqual(rate, 60.0)
        self.assertEqual(ticked.call_count, RATE_PROBE_TICKS, "no Timer A ever appears")
        self.assertIsNotNone(pass_cost_s)

    def test_a_pass_is_priced_at_what_it_costs_not_at_a_deadline(self):
        """Regression: the probe pass must carry no wall-clock deadline.

        A deadlined pass is a censored measurement — truncated at
        `_PLAY_DEADLINE_S` it prices a 120 ms pass at 50 ms, and the render
        path, which passes no deadline of its own, then runs a pass longer than
        the whole poll period the floor sized from it. `_SlowProbe` truncates
        the way `tick_play` really does, so restoring the deadline turns this
        red at 50 ms rather than leaving it green against a free mock."""
        from c64cast.sid import sid_host_emu
        from c64cast.sid.sid_host_emu import HostEmuBudget, SidHostEmu, detect_play_rate_hz

        clock = FrozenClock(0.0, "monotonic")
        probe = _SlowProbe(clock, cost_s=0.12)
        with patch.object(sid_host_emu, "time", clock):
            _rate, pass_cost_s = detect_play_rate_hz(
                cast(SidHostEmu, probe),
                video_hz=60.0,
                clock_hz=_NTSC_CLOCK_HZ,
                budget=HostEmuBudget(6.0, clock=clock.monotonic),
                ticks=4,
            )
        assert pass_cost_s is not None
        self.assertAlmostEqual(pass_cost_s, 0.12, msg="the full pass, not the deadline")
        self.assertEqual(probe.deadlines, [None] * 4, "the probe pass is not deadlined")

    def test_a_spent_budget_times_nothing_and_says_so(self):
        from c64cast.sid.sid_host_emu import HostEmuBudget, detect_play_rate_hz

        probe, _ = self._probe(_INIT_RTS)
        with patch.object(probe, "tick_play", wraps=probe.tick_play) as ticked:
            _rate, pass_cost_s = detect_play_rate_hz(
                probe, video_hz=60.0, clock_hz=_NTSC_CLOCK_HZ, budget=HostEmuBudget(0.0)
            )
        ticked.assert_not_called()
        self.assertIsNone(pass_cost_s, "nothing ran, so nothing was measured")


def _sid_with_extra_addrs(
    *,
    version: int,
    second: int = 0,
    third: int = 0,
    flags: int = 0,
    play_code: bytes = _PLAY_WRITES,
) -> bytes:
    """A synthetic PSID whose header declares the given version, second/third
    SID-address bytes (offsets $7A/$7B), and 16-bit flags word (offset
    $76-$77, big-endian) — clock at bits 2-3, sidModel1 at bits 4-5,
    sidModel2 at bits 6-7 (same low byte as model1), sidModel3 at bits 8-9
    (bits 0-1 of the high byte)."""
    sid = bytearray(_make_synthetic_sid(init_code=_INIT_RTS, play_code=play_code))
    sid[4:6] = version.to_bytes(2, "big")
    sid[0x76] = (flags >> 8) & 0xFF
    sid[0x77] = flags & 0xFF
    sid[0x7A] = second
    sid[0x7B] = third
    return bytes(sid)


class HeaderSidAddressTest(unittest.TestCase):
    """parse_sid_header.sid_addresses: PSID v3/v4 second/third-SID addresses."""

    def test_v4_three_sids(self):
        # 0x42 → $D420, 0x44 → $D440 (address = $D000 | byte<<4).
        h = parse_sid_header(_sid_with_extra_addrs(version=4, second=0x42, third=0x44))
        self.assertEqual(h.sid_addresses, (0xD400, 0xD420, 0xD440))

    def test_v3_second_only_ignores_third(self):
        # v3 has a second-SID field but no third — the $7B byte is ignored.
        h = parse_sid_header(_sid_with_extra_addrs(version=3, second=0x50, third=0x44))
        self.assertEqual(h.sid_addresses, (0xD400, 0xD500))

    def test_v2_has_no_extra_addresses(self):
        h = parse_sid_header(_sid_with_extra_addrs(version=2, second=0x42, third=0x44))
        self.assertEqual(h.sid_addresses, (0xD400,))

    def test_third_ignored_when_second_absent(self):
        # A third address with no second collapses to single-SID (can't have a
        # 3rd chip without a 2nd).
        h = parse_sid_header(_sid_with_extra_addrs(version=4, second=0x00, third=0x44))
        self.assertEqual(h.sid_addresses, (0xD400,))


class HeaderSidModelsTest(unittest.TestCase):
    """parse_sid_header.sid_models: per-chip model bits, gated on the same
    version + address-byte conditions as sid_addresses (SID Player
    Autoconfig)."""

    # model1=8580(2) at bits 4-5, model2=6581(1) at bits 6-7 (same low byte),
    # model3=6581+8580(3) at bits 0-1 of the high byte.
    _FLAGS_M1_8580_M2_6581_M3_BOTH = (3 << 8) | (1 << 6) | (2 << 4)

    def test_v4_three_sids_all_models_decoded(self):
        h = parse_sid_header(
            _sid_with_extra_addrs(
                version=4,
                second=0x42,
                third=0x44,
                flags=self._FLAGS_M1_8580_M2_6581_M3_BOTH,
            )
        )
        self.assertEqual(h.sid_models, ("8580", "6581", "6581+8580"))
        self.assertEqual(h.sid_model, "8580")
        self.assertEqual(h.sid_models[0], h.sid_model)

    def test_v3_second_address_zero_leaves_model2_absent(self):
        # version >= 3 but the 2nd-SID address byte is 0 (no chip declared) —
        # model2 must not be trusted even though the flag bits are set.
        h = parse_sid_header(
            _sid_with_extra_addrs(version=3, second=0x00, flags=self._FLAGS_M1_8580_M2_6581_M3_BOTH)
        )
        self.assertEqual(h.sid_addresses, (0xD400,))
        self.assertEqual(h.sid_models, ("8580",))

    def test_v3_second_present_model2_decoded_model3_gated_by_version(self):
        h = parse_sid_header(
            _sid_with_extra_addrs(version=3, second=0x42, flags=self._FLAGS_M1_8580_M2_6581_M3_BOTH)
        )
        # v3 has no third-SID field at all, so only two chips/models exist.
        self.assertEqual(h.sid_addresses, (0xD400, 0xD420))
        self.assertEqual(h.sid_models, ("8580", "6581"))

    def test_v4_third_address_zero_leaves_model3_absent(self):
        h = parse_sid_header(
            _sid_with_extra_addrs(
                version=4, second=0x42, third=0x00, flags=self._FLAGS_M1_8580_M2_6581_M3_BOTH
            )
        )
        self.assertEqual(h.sid_addresses, (0xD400, 0xD420))
        self.assertEqual(h.sid_models, ("8580", "6581"))

    def test_v1_header_has_no_model(self):
        sid = bytearray(_make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES))
        sid[4:6] = (1).to_bytes(2, "big")  # v1: no flags field at all
        h = parse_sid_header(bytes(sid))
        self.assertIsNone(h.sid_model)
        self.assertEqual(h.sid_models, (None,))

    def test_v2_single_sid_model1_only(self):
        h = parse_sid_header(_sid_with_extra_addrs(version=2, flags=2 << 4))  # model1=8580
        self.assertEqual(h.sid_addresses, (0xD400,))
        self.assertEqual(h.sid_models, ("8580",))


class DetectSidAddressesTest(unittest.TestCase):
    """detect_sid_addresses: header authority + filename _NSID fallback."""

    def test_header_is_authoritative(self):
        sid = _sid_with_extra_addrs(version=4, second=0x42, third=0x44)
        self.assertEqual(detect_sid_addresses(None, sid), (0xD400, 0xD420, 0xD440))

    def test_filename_raises_count_with_canonical_fillers(self):
        # v2 header (single SID) but the filename says 3SID → synthesize
        # canonical stride-$20 bases for the chips the header can't describe.
        sid = _sid_with_extra_addrs(version=2)
        self.assertEqual(
            detect_sid_addresses("tunes/Great_Song_3SID.sid", sid),
            (0xD400, 0xD420, 0xD440),
        )

    def test_header_beats_smaller_filename_hint(self):
        # A 2SID header must not be lowered by a "_1SID" filename.
        sid = _sid_with_extra_addrs(version=3, second=0x50)
        self.assertEqual(detect_sid_addresses("x_1SID.sid", sid), (0xD400, 0xD500))

    def test_plain_single_sid(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_WRITES)
        self.assertEqual(detect_sid_addresses("tune.sid", sid), (0xD400,))

    def test_filename_hint_skips_a_slot_the_header_already_claims(self):
        # A "_3SID" name over a header declaring $D440 used to synthesize a
        # second $D440: TrappedRam keys its address map by absolute address,
        # so the later bank won every colliding key and the earlier chip's
        # shadow stayed all-zero — a scope window permanently flat while the
        # audience hears the chip.
        sid = _sid_with_extra_addrs(version=3, second=0x44)
        addresses = detect_sid_addresses("tunes/Song_3SID.sid", sid)
        self.assertEqual(len(set(addresses)), len(addresses), "no base may repeat")
        self.assertEqual(addresses, (0xD400, 0xD440, 0xD420))


class ExtraSidAddressValidationTest(unittest.TestCase):
    """_decode_extra_sid_addr enforces the PSID spec's windows.

    The guard used to read `0xD000 <= addr <= 0xDFF0`, which the arithmetic
    already guarantees for every byte 1..255 — so no byte was ever rejected
    and a header field chose a DMA write target anywhere in the I/O page."""

    def test_spec_legal_bytes_decode(self):
        self.assertEqual(_decode_extra_sid_addr(0x42), 0xD420)
        self.assertEqual(_decode_extra_sid_addr(0x50), 0xD500)
        self.assertEqual(_decode_extra_sid_addr(0x7E), 0xD7E0)
        self.assertEqual(_decode_extra_sid_addr(0xE0), 0xDE00)
        # $EE -> $DEE0 is the top of the cartridge window that survives
        # RESERVED_IO_WINDOWS; $F0 and up are c64cast's own REU and sampler.
        self.assertEqual(_decode_extra_sid_addr(0xEE), 0xDEE0)

    def test_absent_and_malformed_bytes_degrade_to_single_sid(self):
        rejected = {
            0x00: "absent",
            0x01: "odd, and $D010 is VIC sprite-coordinate space",
            0x43: "odd",
            0x40: "chip 0's own $D400",
            0x02: "$D020, the VIC border color",
            0xC0: "$DC00, CIA #1 — teardown's zero write kills the jiffy IRQ",
            0xD0: "$DD00, CIA #2 — forces the VIC bank and pulls the serial lines",
            0x80: "$D800, between the two legal windows",
            0xF0: "$DF00, the REU's own command registers",
            0xF2: "$DF20, Ultimate Audio channel 0 — the sampler plays video audio there",
            0xFE: "$DFE0, Ultimate Audio channel 6",
        }
        for byte, why in rejected.items():
            with self.subTest(byte=byte, why=why):
                self.assertIsNone(_decode_extra_sid_addr(byte))

    def test_no_byte_escapes_the_legal_windows(self):
        for byte in range(1, 256):
            addr = _decode_extra_sid_addr(byte)
            if addr is None:
                continue
            with self.subTest(byte=byte):
                self.assertTrue(
                    0xD420 <= addr <= 0xD7E0 or 0xDE00 <= addr <= 0xDFE0,
                    f"byte ${byte:02X} decoded to ${addr:04X}, outside the PSID windows",
                )

    def test_no_accepted_base_reaches_hardware_c64cast_drives(self):
        # The PSID spec's $DE00-$DFE0 window is "cartridge I/O", but c64cast
        # drives that cartridge itself: the REU's command registers at
        # $DF00-$DF0A (the audio ring's NMI handler reads $DF02/$DF03 back as
        # its running C64 destination pointer) and the Ultimate Audio sampler's
        # seven channel register files filling $DF20-$DFFF. WaveformScene's
        # teardown zero-writes 25 bytes at every declared base, so either one
        # is a live device walked by a header field. Brute-forced rather than
        # spot-checked, and the rule is derived from the devices' own address
        # constants instead of a list of excluded magic numbers.
        from c64cast.hw.c64 import RESERVED_IO_WINDOWS
        from c64cast.sid.sidemu import SID_REG_COUNT

        for byte in range(256):
            addr = _decode_extra_sid_addr(byte)
            if addr is None:
                continue
            for low, high in RESERVED_IO_WINDOWS:
                with self.subTest(byte=byte, window=(hex(low), hex(high))):
                    self.assertFalse(
                        addr <= high and addr + SID_REG_COUNT > low,
                        f"byte ${byte:02X} decoded to ${addr:04X}, whose register window "
                        f"covers ${low:04X}-${high:04X}",
                    )

    def test_reu_page_byte_degrades_to_single_sid(self):
        # $F0 -> $DF00 is the spec-legal byte that lands on the REU.
        self.assertIsNone(_decode_extra_sid_addr(0xF0))
        h = parse_sid_header(_sid_with_extra_addrs(version=3, second=0xF0))
        self.assertEqual(h.sid_addresses, (0xD400,))

    def test_sampler_page_bytes_degrade_to_single_sid(self):
        # The same shape one page up: $F2..$FE decode into $DF20-$DFFF, which
        # the U64's "Map Ultimate Audio $DF20-DFFF" switch hands to the FPGA
        # sampler c64cast streams video audio through. These are also the bytes
        # that fed the planner the $DF20/$DF60 pair it aligned down onto $DF00
        # (tests/test_asid_sidmap.py ReservedIoTest).
        for byte in range(0xF2, 0x100, 2):
            with self.subTest(byte=byte):
                self.assertIsNone(_decode_extra_sid_addr(byte))
        h = parse_sid_header(_sid_with_extra_addrs(version=4, second=0xF2, third=0xF6))
        self.assertEqual(h.sid_addresses, (0xD400,))

    def test_hostile_header_byte_no_longer_declares_a_chip_on_cia1(self):
        # WaveformScene.teardown writes 25 zero bytes at every non-$D400 base
        # it was handed; $DC00 is CIA #1's port/DDR/timer/ICR file, and the
        # machine is dead until a physical reset.
        h = parse_sid_header(_sid_with_extra_addrs(version=3, second=0xC0))
        self.assertEqual(h.sid_addresses, (0xD400,))
        self.assertEqual(h.sid_models, (h.sid_model,))

    def test_duplicate_header_addresses_collapse_to_one_chip(self):
        h = parse_sid_header(_sid_with_extra_addrs(version=4, second=0x42, third=0x42))
        self.assertEqual(h.sid_addresses, (0xD400, 0xD420))

    def test_overlapping_bases_are_refused_not_just_duplicates(self):
        # $D420 and $D430 are 16 bytes apart, so their 25-byte register
        # windows overlap and the later bank steals $D430-$D438 from the
        # earlier one — including its $D418 master-volume shadow. Equality is
        # not the whole rule, so the guard tests the window, not the address.
        addresses = [0xD400, 0xD420]
        self.assertFalse(_append_distinct_sid_base(addresses, 0xD430))
        self.assertFalse(_append_distinct_sid_base(addresses, 0xD420))
        self.assertTrue(_append_distinct_sid_base(addresses, 0xD440))
        self.assertEqual(addresses, [0xD400, 0xD420, 0xD440])


class MultiBankTrapTest(unittest.TestCase):
    """TrappedRam/SidHostEmu shadow every configured SID chip's register bank."""

    # PLAY writes a distinct byte to V1-control of three chips: $D404/$D424/$D444.
    _PLAY_THREE_CHIPS = bytes(
        [
            0xA9,
            0x11,
            0x8D,
            0x04,
            0xD4,  # STA $D404 = $11 (chip 0)
            0xA9,
            0x22,
            0x8D,
            0x24,
            0xD4,  # STA $D424 = $22 (chip 1)
            0xA9,
            0x33,
            0x8D,
            0x44,
            0xD4,  # STA $D444 = $33 (chip 2)
            0x60,
        ]
    )

    def test_each_bank_captured(self):
        sid = _sid_with_extra_addrs(
            version=4, second=0x42, third=0x44, play_code=self._PLAY_THREE_CHIPS
        )
        emu = SidHostEmu(sid, sid_bases=(0xD400, 0xD420, 0xD440))
        emu.tick_play()
        self.assertEqual(emu.n_sids, 3)
        self.assertEqual(emu.regs(0)[4], 0x11)
        self.assertEqual(emu.regs(1)[4], 0x22)
        self.assertEqual(emu.regs(2)[4], 0x33)

    def test_single_sid_ignores_other_banks(self):
        # Default single-SID trap shadows only $D400; writes to $D424 land in
        # RAM but not the shadow (byte-identical to the pre-multi-SID path).
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=self._PLAY_THREE_CHIPS)
        emu = SidHostEmu(sid)
        emu.tick_play()
        self.assertEqual(emu.n_sids, 1)
        self.assertEqual(emu.regs(0)[4], 0x11)
        self.assertEqual(emu._memory.ram[0xD424], 0x22)  # reached RAM
        self.assertEqual(len(emu._memory.sid_shadows), 1)  # but not shadowed


if __name__ == "__main__":
    unittest.main()
