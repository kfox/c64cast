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

import unittest

from c64cast.sid_host_emu import SidHostEmu

# ---------------------------------------------------------------------------
# Synthetic-SID helper
# ---------------------------------------------------------------------------

def _make_synthetic_sid(*, init_code: bytes, play_code: bytes,
                        load_addr: int = 0x0820,
                        num_songs: int = 1, start_song: int = 1,
                        magic: bytes = b"PSID") -> bytes:
    """Build a minimal PSID v2 file with INIT at load_addr and PLAY
    immediately after. Returns the full file bytes (124-byte header +
    payload)."""
    payload = init_code + play_code
    play_addr = load_addr + len(init_code)
    h = bytearray(124)
    h[0:4] = magic
    h[4:6] = (2).to_bytes(2, "big")
    h[6:8] = (124).to_bytes(2, "big")    # data_offset
    h[8:10] = load_addr.to_bytes(2, "big")
    h[10:12] = load_addr.to_bytes(2, "big")  # init = load (= RTS in our stubs)
    h[12:14] = play_addr.to_bytes(2, "big")
    h[14:16] = num_songs.to_bytes(2, "big")
    h[16:18] = start_song.to_bytes(2, "big")
    return bytes(h) + payload


# Tiny PLAY that writes recognizable bytes into 4 specific SID slots
# (V1 control, V2 control, V3 control, master volume) and RTSes. Easy
# to verify in the shadow.
_PLAY_WRITES = bytes([
    0xA9, 0xAA,            # LDA #$AA
    0x8D, 0x04, 0xD4,      # STA $D404 (V1 control)
    0xA9, 0xBB,            # LDA #$BB
    0x8D, 0x0B, 0xD4,      # STA $D40B (V2 control)
    0xA9, 0xCC,            # LDA #$CC
    0x8D, 0x12, 0xD4,      # STA $D412 (V3 control)
    0xA9, 0x0F,            # LDA #$0F
    0x8D, 0x18, 0xD4,      # STA $D418 (volume)
    0x60,                  # RTS
])

# INIT is a bare RTS — the host emulator JSRs into load_addr to run it.
_INIT_RTS = bytes([0x60])


def _init_set_timer_a(latch: int) -> bytes:
    """INIT that programs CIA #1 Timer A latch ($DC04/$DC05) — the mark of a
    CIA-timed (multispeed) tune — then RTSes."""
    lo, hi = latch & 0xFF, (latch >> 8) & 0xFF
    return bytes([
        0xA9, lo, 0x8D, 0x04, 0xDC,    # LDA #lo / STA $DC04
        0xA9, hi, 0x8D, 0x05, 0xDC,    # LDA #hi / STA $DC05
        0x60,                          # RTS
    ])

# Degenerate PLAY: JMP to itself, forever. Used to verify the cycle cap.
# $0821: JMP $0821 (3 bytes). The cycle cap should kick in well before
# the host CPU notices.
_PLAY_INFINITE_LOOP = bytes([0x4C, 0x21, 0x08])


class SidHostEmuValidationTest(unittest.TestCase):
    """Validation is shared with run_sid_player via parse_psid_for_player;
    spot-check that it surfaces through SidHostEmu's __init__."""

    def test_rejects_rsid(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS,
                                  play_code=_PLAY_WRITES, magic=b"RSID")
        with self.assertRaisesRegex(ValueError, "RSID"):
            SidHostEmu(sid)

    def test_rejects_load_addr_below_basic_stub(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS,
                                  play_code=_PLAY_WRITES, load_addr=0x0801)
        with self.assertRaisesRegex(ValueError, "BASIC SYS stub"):
            SidHostEmu(sid)

    def test_rejects_song_out_of_range(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS,
                                  play_code=_PLAY_WRITES, num_songs=3)
        with self.assertRaisesRegex(ValueError, "out of range"):
            SidHostEmu(sid, song=99)


class SidHostEmuRegsTest(unittest.TestCase):

    def test_shadow_is_25_bytes(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS,
                                  play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        self.assertEqual(len(emu.regs()), 25)

    def test_play_writes_land_in_shadow(self):
        sid = _make_synthetic_sid(init_code=_INIT_RTS,
                                  play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        # INIT was a bare RTS so the shadow is still zeros — proves the
        # baseline state isn't accidentally pre-populated.
        self.assertEqual(emu.regs(), bytes(25))

        emu.tick_play()
        shadow = emu.regs()
        # V1 ctl ($D404 → offset 4), V2 ctl ($D40B → 11), V3 ctl
        # ($D412 → 18), volume ($D418 → 24).
        self.assertEqual(shadow[4],  0xAA)
        self.assertEqual(shadow[11], 0xBB)
        self.assertEqual(shadow[18], 0xCC)
        self.assertEqual(shadow[24], 0x0F)

    def test_shadow_only_covers_d400_d418(self):
        # A STA to $D419 (one byte past the shadow window) must NOT be
        # written into the shadow. Tests TrappedRam's upper bound.
        play = bytes([
            0xA9, 0xEE,
            0x8D, 0x19, 0xD4,   # STA $D419
            0x60,
        ])
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        emu = SidHostEmu(sid)
        emu.tick_play()
        # The shadow stays zeros — $D419 is outside the SID register file.
        self.assertEqual(emu.regs(), bytes(25))


class SidHostEmuCycleCapTest(unittest.TestCase):

    def test_infinite_play_returns_via_cycle_cap(self):
        # If the cap doesn't fire, this test will hang the suite —
        # which is itself the failure signal.
        sid = _make_synthetic_sid(init_code=_INIT_RTS,
                                  play_code=_PLAY_INFINITE_LOOP)
        emu = SidHostEmu(sid)
        emu.tick_play()   # must return, not hang
        # Subsequent ticks must still terminate.
        for _ in range(3):
            emu.tick_play()


class RetriggerDetectionTest(unittest.TestCase):
    """retriggers() recovers hard restarts (gate off→on within one PLAY
    call) that the 25-byte shadow collapses to gate-still-high."""

    def test_intra_tick_gate_pulse_flags_retrigger(self):
        # PLAY writes V1 control gate-LOW ($40 pulse, gate=0) then gate-HIGH
        # ($41 pulse + gate) — a hard restart within one call. The shadow
        # ends at $41 (gate high), but retriggers() must flag voice 0.
        play = bytes([
            0xA9, 0x40, 0x8D, 0x04, 0xD4,   # LDA #$40 / STA $D404 (gate low)
            0xA9, 0x41, 0x8D, 0x04, 0xD4,   # LDA #$41 / STA $D404 (gate high)
            0x60,                           # RTS
        ])
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play)
        emu = SidHostEmu(sid)
        emu.tick_play()
        self.assertEqual(emu.regs()[4], 0x41)          # shadow ends gate-high
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
        emu._memory.gate_low_seen[0] = 1   # poison as if a prior tick saw low
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
        sid = _make_synthetic_sid(init_code=_init_set_timer_a(latch),
                                  play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        rate = emu.play_rate_hz(60.0, self.CLOCK)
        self.assertAlmostEqual(rate, self.CLOCK / (latch + 1), places=3)
        self.assertGreater(rate, 60.0)   # genuinely multispeed

    def test_out_of_range_latch_falls_back_to_video_rate(self):
        # A tiny latch implies an absurd >8x rate — reject, keep vsync.
        sid = _make_synthetic_sid(init_code=_init_set_timer_a(50),
                                  play_code=_PLAY_WRITES)
        emu = SidHostEmu(sid)
        self.assertEqual(emu.play_rate_hz(60.0, self.CLOCK), 60.0)


class RamWriteFootprintTest(unittest.TestCase):
    """ram_write_footprint marks the RAM a tune writes — used to place the
    relocated C64-side player off the tune's scratch (the Beat_Dis fix)."""

    def test_footprint_marks_scratch_writes(self):
        from c64cast.sid_host_emu import ram_write_footprint
        # PLAY writes a byte to $5000 (scratch) + the SID registers, RTS.
        play = bytes([
            0xA9, 0x42,            # LDA #$42
            0x8D, 0x00, 0x50,      # STA $5000  (scratch)
            0x8D, 0x04, 0xD4,      # STA $D404  (a SID reg, for good measure)
            0x60,                  # RTS
        ])
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=play,
                                  load_addr=0x1000)
        fp = ram_write_footprint(sid, ticks=10)
        self.assertEqual(len(fp), 65536)
        self.assertTrue(fp[0x5000], "scratch write must be in the footprint")
        self.assertTrue(fp[0xD404], "SID-reg write must be in the footprint")
        self.assertFalse(fp[0x6000], "untouched RAM must stay clear")

    def test_play_access_footprint_catches_reads_excludes_init(self):
        from c64cast.sid_host_emu import ram_play_access_footprint, ram_write_footprint
        # INIT writes a one-time block to $A000 (a display region) then RTS;
        # PLAY *reads* $B400 (live per-song data, à la Times of Lore) and
        # writes scratch at $5000. The access footprint drives the display-
        # bank choice: it must drop the INIT-only write (paintable), keep the
        # recurring PLAY write, AND — the key fix — catch the PLAY read that
        # the write-only footprint can't see.
        init = bytes([
            0xA9, 0x55,            # LDA #$55
            0x8D, 0x00, 0xA0,      # STA $A000  (one-time INIT scratch)
            0x60,                  # RTS
        ])
        play = bytes([
            0xAD, 0x00, 0xB4,      # LDA $B400  (read live per-song data)
            0x8D, 0x00, 0x50,      # STA $5000  (recurring PLAY scratch)
            0x60,                  # RTS
        ])
        sid = _make_synthetic_sid(init_code=init, play_code=play,
                                  load_addr=0x1000)
        # The write-only footprint marks the INIT write but NOT the PLAY read.
        full = ram_write_footprint(sid, ticks=10)
        self.assertTrue(full[0xA000], "INIT write present in full footprint")
        self.assertFalse(full[0xB400], "write footprint can't see the read")

        access = ram_play_access_footprint(sid, ticks=10)
        self.assertFalse(access[0xA000],
                         "INIT-only write must be excluded from access view")
        self.assertTrue(access[0x5000],
                        "recurring PLAY write must be in the access view")
        self.assertTrue(access[0xB400],
                        "PLAY read must be in the access view (the ToL fix)")

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


if __name__ == "__main__":
    unittest.main()
