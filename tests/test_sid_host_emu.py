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

from c64cast.sid_host_emu import SidHostEmu, detect_sid_addresses, parse_sid_header

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
    def test_infinite_play_returns_via_cycle_cap(self):
        # If the cap doesn't fire, this test will hang the suite —
        # which is itself the failure signal.
        sid = _make_synthetic_sid(init_code=_INIT_RTS, play_code=_PLAY_INFINITE_LOOP)
        emu = SidHostEmu(sid)
        emu.tick_play()  # must return, not hang
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
        from c64cast.sid_host_emu import ram_write_footprint

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
        full = ram_write_footprint(sid, ticks=10)
        self.assertTrue(full[0xA000], "INIT write present in full footprint")
        self.assertFalse(full[0xB400], "write footprint can't see the read")

        access = ram_play_access_footprint(sid, ticks=10)
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
