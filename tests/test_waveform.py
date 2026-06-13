"""Tests for the SID emulator + WaveformScene (no real U64, no real SID file
playback)."""
# Test-internal FakeApi / FakeScene duck-type Ultimate64API / Scene; suppress
# pyright's argument-type + attribute-access complaints file-wide so the test
# focus stays on behavior rather than type wrapping.
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import os
import random
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from c64cast.sidemu import (
    WAVE_NOISE,
    WAVE_SAWTOOTH,
    WAVE_TRIANGLE,
    SIDEmulator,
    primary_waveform,
)
from c64cast.waveform import parse_sid_header

# ---------------------------------------------------------------------------
# SID file header parsing
# ---------------------------------------------------------------------------

def _make_sid_header(magic=b"PSID", version=2, num_songs=4, start_song=1,
                     name=b"TEST", author=b"AUTHOR", released=b"2026",
                     flags=0x0000, load_addr=None, data_offset=None):
    h = bytearray(124)
    h[0:4] = magic
    h[4:6] = version.to_bytes(2, "big")
    if data_offset is not None:
        h[6:8] = data_offset.to_bytes(2, "big")
    if load_addr is not None:
        h[8:10] = load_addr.to_bytes(2, "big")
    h[14:16] = num_songs.to_bytes(2, "big")
    h[16:18] = start_song.to_bytes(2, "big")
    h[22:22 + len(name)] = name
    h[54:54 + len(author)] = author
    h[86:86 + len(released)] = released
    h[0x76:0x78] = flags.to_bytes(2, "big")
    return bytes(h)


class SidHeaderTest(unittest.TestCase):

    def test_parses_basic_psid(self):
        h = parse_sid_header(_make_sid_header())
        self.assertEqual(h.magic, "PSID")
        self.assertEqual(h.num_songs, 4)
        self.assertEqual(h.start_song, 1)
        self.assertEqual(h.name, "TEST")

    def test_parses_rsid(self):
        h = parse_sid_header(_make_sid_header(magic=b"RSID"))
        self.assertEqual(h.magic, "RSID")

    def test_rejects_non_sid(self):
        with self.assertRaises(ValueError):
            parse_sid_header(b"NOPE" + bytes(120))

    def test_rejects_short(self):
        with self.assertRaises(ValueError):
            parse_sid_header(b"PSID")

    def test_v2_flags_decode_clock_and_model(self):
        # flags bits 2-3 = clock (01=PAL, 10=NTSC), bits 4-5 = primary
        # SID model (01=6581, 10=8580). Encoded in the *low* byte of the
        # 2-byte big-endian flags field at offset 0x76.
        # PAL 6581 → flags_lo = (1<<2) | (1<<4) = 0x14.
        h = parse_sid_header(_make_sid_header(flags=0x0014))
        self.assertEqual(h.clock, "PAL")
        self.assertEqual(h.sid_model, "6581")
        # NTSC 8580 → flags_lo = (2<<2) | (2<<4) = 0x28.
        h = parse_sid_header(_make_sid_header(flags=0x0028))
        self.assertEqual(h.clock, "NTSC")
        self.assertEqual(h.sid_model, "8580")
        # Both clocks + both models → 0x3C.
        h = parse_sid_header(_make_sid_header(flags=0x003C))
        self.assertEqual(h.clock, "PAL+NTSC")
        self.assertEqual(h.sid_model, "6581+8580")
        # Unknown (zero flags) → "?" for both.
        h = parse_sid_header(_make_sid_header(flags=0x0000))
        self.assertEqual(h.clock, "?")
        self.assertEqual(h.sid_model, "?")

    def test_v1_header_leaves_flags_none(self):
        # A 118-byte v1 header has no flags field — clock + sid_model must
        # be None rather than guessed.
        v1 = _make_sid_header(version=1)[:118]
        h = parse_sid_header(v1)
        self.assertEqual(h.version, 1)
        self.assertIsNone(h.clock)
        self.assertIsNone(h.sid_model)


# ---------------------------------------------------------------------------
# SID emulator
# ---------------------------------------------------------------------------

class DisplayLayoutTest(unittest.TestCase):
    """_choose_display_layout picks the VIC bank whose display regions clear
    the payload + runtime footprint; refuses when no bank is free."""

    def _layout(self, lo, hi, footprint=None):
        from c64cast.waveform import _choose_display_layout
        fp = footprint if footprint is not None else bytearray(65536)
        return _choose_display_layout(lo, hi, fp)

    def test_default_bank0_when_clear(self):
        from c64cast.c64 import CIA2, VIC_BANK_0
        from c64cast.waveform import D018_HIRES_BITMAP
        s, b, d, d018 = self._layout(0x1000, 0x1800)
        self.assertEqual((s, b, d, d018),
                         (VIC_BANK_0.SCREEN, VIC_BANK_0.BITMAP,
                          CIA2.PORT_A_BANK_0, D018_HIRES_BITMAP))

    def test_bank2_when_payload_overlaps_bank0_bitmap(self):
        from c64cast.c64 import CIA2, VIC_BANK_2
        from c64cast.waveform import D018_HIRES_BITMAP
        s, b, d, d018 = self._layout(0x1000, 0x2F00)   # crosses $2000
        self.assertEqual((s, b, d, d018),
                         (VIC_BANK_2.SCREEN, VIC_BANK_2.BITMAP,
                          CIA2.PORT_A_BANK_2, D018_HIRES_BITMAP))

    def test_bank2_when_footprint_dirties_bank0(self):
        # Payload clears bank 0, but the tune writes $2000 at runtime.
        from c64cast.c64 import CIA2
        fp = bytearray(65536)
        fp[0x2000] = 1
        _, _, d, _ = self._layout(0x1000, 0x1800, fp)
        self.assertEqual(d, CIA2.PORT_A_BANK_2)

    def test_bank1_when_banks_0_and_2_blocked(self):
        # Times of Lore subtunes 2-11: payload covers bank 0's bitmap, and
        # the PLAY footprint reads bank 2's $B400 — only bank 1 is free.
        from c64cast.c64 import CIA2
        from c64cast.waveform import _BANK1_BITMAP, _BANK1_SCREEN, D018_BANK1
        fp = bytearray(65536)
        fp[0xB400] = 1                          # bank 2 PLAY read (song data)
        s, b, d, d018 = self._layout(0x1D00, 0x5089, fp)   # payload covers bank 0
        self.assertEqual((s, b, d, d018),
                         (_BANK1_SCREEN, _BANK1_BITMAP,
                          CIA2.PORT_A_BANK_1, D018_BANK1))

    def test_raises_when_all_banks_blocked(self):
        # Payload spans banks 0, 1 and 2 displays → no free bank.
        with self.assertRaisesRegex(ValueError, "no free VIC bank"):
            self._layout(0x2700, 0xCF8F)

    def test_any_display_bank_fits_payload(self):
        from c64cast.waveform import _any_display_bank_fits_payload
        self.assertTrue(_any_display_bank_fits_payload(0x1000, 0x2F00))  # bank2
        self.assertFalse(_any_display_bank_fits_payload(0x2700, 0xCF8F))  # none


class UnifiedDisplayLayoutTest(unittest.TestCase):
    """_choose_unified_display_layout pins ONE bank free for the UNION of
    every subtune's PLAY footprint (so SHIFT-cycling never relocates), or
    returns None when no single bank fits all subtunes. The per-song
    footprints are stubbed so the logic is tested without the host emulator."""

    def _run(self, per_song, lo, hi):
        """per_song: {song -> [addrs]} accessed during PLAY."""
        import c64cast.waveform as wf

        def fake_fp(_sid, song=0, **kw):
            fp = bytearray(65536)
            for a in per_song.get(song, ()):
                fp[a] = 1
            return fp

        with patch.object(wf, "ram_play_access_footprint", fake_fp):
            return wf._choose_unified_display_layout(
                b"", lo, hi, len(per_song))

    def test_tol_like_union_pins_bank1(self):
        # Song 1 footprint clears bank 2; songs 2-11 read bank 2's $B400.
        # The union blocks bank 2, payload blocks bank 0 → bank 1 for ALL.
        from c64cast.c64 import CIA2
        from c64cast.waveform import _BANK1_BITMAP, _BANK1_SCREEN, D018_BANK1
        per_song = {1: ()}
        for s in range(2, 12):
            per_song[s] = (0xB400,)
        layout = self._run(per_song, 0x1D00, 0x5089)   # payload covers bank 0
        self.assertEqual(layout, (_BANK1_SCREEN, _BANK1_BITMAP,
                                  CIA2.PORT_A_BANK_1, D018_BANK1))

    def test_union_prefers_earliest_free_bank(self):
        # Nothing dirty + tiny payload → union leaves bank 0 free → bank 0
        # (the unified bank must respect the same preference order).
        from c64cast.c64 import CIA2
        layout = self._run({1: (), 2: ()}, 0x1000, 0x1800)
        assert layout is not None
        self.assertEqual(layout[2], CIA2.PORT_A_BANK_0)

    def test_returns_none_when_no_single_bank_fits_union(self):
        # One subtune dirties bank 2's display, another dirties bank 1's —
        # with the payload covering bank 0, the union blocks every bank.
        from c64cast.c64 import VIC_BANK_2
        from c64cast.waveform import _BANK1_BITMAP
        per_song = {1: (VIC_BANK_2.BITMAP,), 2: (_BANK1_BITMAP,)}
        self.assertIsNone(self._run(per_song, 0x1D00, 0x5089))


class PlayBankForFootprintsTest(unittest.TestCase):
    """_play_bank_for_footprints: $36 only when PLAY reads RAM the tune wrote
    under BASIC ROM (Times of Lore), not when it reads BASIC ROM as data."""

    def _call(self, write, access):
        from c64cast.waveform import _play_bank_for_footprints
        return _play_bank_for_footprints(write, access)

    def test_play_reads_written_ram_under_rom_needs_basic_out(self):
        from c64cast.c64 import CPU
        write = bytearray(65536)
        access = bytearray(65536)
        write[0xB400] = 1                       # INIT wrote song data here
        access[0xB400] = 1                       # PLAY reads it back
        self.assertEqual(self._call(write, access), CPU.PORT_BASIC_OUT)

    def test_play_reads_basic_rom_as_data_keeps_default(self):
        # PLAY reads $A100 but nothing wrote there → it's the ROM itself
        # (Galway's Comic Bakery table); keep BASIC mapped ($37 = None here).
        write = bytearray(65536)
        access = bytearray(65536)
        access[0xA100] = 1
        self.assertIsNone(self._call(write, access))

    def test_no_access_under_rom_keeps_default(self):
        write = bytearray(65536)
        access = bytearray(65536)
        write[0x5000] = 1                       # all below BASIC ROM
        access[0x5000] = 1
        self.assertIsNone(self._call(write, access))


class EndOfTuneDetectionTest(unittest.TestCase):
    """WaveformScene._check_end_of_tune state machine — isolated from the
    SID-loading __init__ via __new__ so it needs no hardware or SID file."""

    def _scene(self):
        from c64cast.waveform import WaveformScene
        s = WaveformScene.__new__(WaveformScene)
        s._ever_sounded = False
        s._silence_since = None
        s.name = "test"
        return s

    EPS = 1e-3

    def test_never_ends_before_first_sound(self):
        s = self._scene()
        # Silent from t=0 but the tune never sounded → must not end.
        for t in range(0, 30):
            self.assertFalse(s._check_end_of_tune(float(t), [0.0, 0.0, 0.0]))

    def test_ends_after_sustained_silence_post_sound(self):
        s = self._scene()
        self.assertFalse(s._check_end_of_tune(0.0, [0.5, 0.0, 0.0]))  # sounds
        self.assertFalse(s._check_end_of_tune(1.0, [0.0, 0.0, 0.0]))  # silence starts
        self.assertFalse(s._check_end_of_tune(1.0 + 3.0, [0.0, 0.0, 0.0]))
        self.assertTrue(s._check_end_of_tune(1.0 + s.END_SILENCE_S,
                                             [0.0, 0.0, 0.0]))

    def test_brief_rest_does_not_end(self):
        s = self._scene()
        s._check_end_of_tune(0.0, [0.5, 0.0, 0.0])     # sounds
        s._check_end_of_tune(1.0, [0.0, 0.0, 0.0])     # rest starts
        # A note returns before END_SILENCE_S → window resets.
        self.assertFalse(s._check_end_of_tune(3.0, [0.4, 0.0, 0.0]))
        self.assertIsNone(s._silence_since)
        # Long silence after must restart the clock, not reuse the old one.
        s._check_end_of_tune(4.0, [0.0, 0.0, 0.0])
        self.assertFalse(s._check_end_of_tune(4.0 + s.END_SILENCE_S - 0.1,
                                              [0.0, 0.0, 0.0]))


class SidEmulatorTest(unittest.TestCase):

    def _zero_regs(self):
        return bytes(25)

    def _voice_regs(self, voice_idx, freq=0x1C32, pw=0x0800,
                    control=0x41, ad=0x00, sr=0xF0):
        regs = bytearray(25)
        base = voice_idx * 7
        regs[base + 0] = freq & 0xFF
        regs[base + 1] = (freq >> 8) & 0xFF
        regs[base + 2] = pw & 0xFF
        regs[base + 3] = (pw >> 8) & 0x0F
        regs[base + 4] = control
        regs[base + 5] = ad
        regs[base + 6] = sr
        return bytes(regs)

    def test_silence_when_no_waveform(self):
        emu = SIDEmulator()
        emu.update_registers(self._zero_regs())
        s = emu.voice_samples(0, 320)
        self.assertTrue(np.all(s == 0.0))

    def test_silence_when_envelope_zero(self):
        emu = SIDEmulator()
        # Pulse wave, gate ON but envelope_level starts at 0; no advance.
        emu.update_registers(self._voice_regs(0))
        s = emu.voice_samples(0, 320)
        self.assertTrue(np.all(s == 0.0),
                        "envelope=0 must zero the output regardless of wave bits")

    def test_gate_edge_triggers_attack(self):
        emu = SIDEmulator()
        emu.update_registers(self._voice_regs(0, control=0x40))   # pulse, no gate
        self.assertEqual(emu.voices[0].envelope_state, "release")
        emu.update_registers(self._voice_regs(0, control=0x41))   # pulse + gate
        self.assertEqual(emu.voices[0].envelope_state, "attack")
        emu.update_registers(self._voice_regs(0, control=0x40))   # release
        self.assertEqual(emu.voices[0].envelope_state, "release")

    def test_one_tick_gate_pulse_seen_tickwise_sounds(self):
        # Rationale for moving register tracking onto the poll thread: a
        # gate that pulses ON for a single PLAY tick must raise the envelope.
        # Feeding every tick (poll-thread model) catches the attack edge;
        # ad=0x00 => 2ms attack completes within one 1/60s tick.
        emu = SIDEmulator()
        on = self._voice_regs(0, control=0x41, ad=0x00, sr=0xF0)
        off = self._voice_regs(0, control=0x40, ad=0x00, sr=0xF0)
        emu.update_registers(on)
        emu.advance_envelopes(1 / 60)
        self.assertGreater(emu.voices[0].envelope_level, 0.0,
                           "tick-wise feed must catch the gate-on edge")
        # The subsequent gate-off edge is also seen (no missed transition).
        emu.update_registers(off)
        self.assertEqual(emu.voices[0].envelope_state, "release")

    def test_one_tick_gate_pulse_missed_if_only_final_snapshot(self):
        # The old render-thread model read only the latest snapshot; if both
        # the ON and OFF happened between two render frames, it saw only OFF
        # and never triggered attack -> voice stuck flat. This documents why
        # that path was wrong.
        emu = SIDEmulator()
        off = self._voice_regs(0, control=0x40, ad=0x00, sr=0xF0)
        emu.update_registers(off)        # never saw the gate-on snapshot
        emu.advance_envelopes(1 / 60)
        self.assertEqual(emu.voices[0].envelope_level, 0.0)

    def _decay_plucked_to_zero(self, emu, regs):
        # advance_envelopes runs one state transition per call: attack→decay,
        # then decay→sustain(=0). Two calls land a sustain=0 voice at 0.
        emu.update_registers(regs)
        emu.advance_envelopes(0.01)   # attack completes → decay
        emu.advance_envelopes(0.5)    # decay reaches sustain=0
        self.assertEqual(emu.voices[0].envelope_level, 0.0)

    def test_retrigger_mask_reattacks_plucked_voice(self):
        # A plucked voice (sustain=0) held gate-high decays to 0 and stays
        # flat. A hard restart the shadow can't show (gate-high both
        # snapshots) arrives via the retrigger mask and must re-attack it.
        emu = SIDEmulator()
        plucked = self._voice_regs(0, control=0x41, ad=0x00, sr=0x00)  # sus=0
        self._decay_plucked_to_zero(emu, plucked)
        # Same registers (no gate edge) but retrigger flagged for voice 0.
        emu.update_registers(plucked, retrigger=(True, False, False))
        self.assertEqual(emu.voices[0].envelope_state, "attack")
        emu.advance_envelopes(0.005)          # past the 2ms attack
        self.assertGreater(emu.voices[0].envelope_level, 0.0,
                           "retrigger mask must re-attack the plucked voice")

    def test_retrigger_mask_none_is_noop(self):
        # Without a mask, behavior is unchanged (held gate, no re-attack).
        emu = SIDEmulator()
        plucked = self._voice_regs(0, control=0x41, ad=0x00, sr=0x00)
        self._decay_plucked_to_zero(emu, plucked)
        emu.update_registers(plucked)         # no retrigger arg
        self.assertEqual(emu.voices[0].envelope_state, "sustain")
        emu.advance_envelopes(0.005)
        self.assertEqual(emu.voices[0].envelope_level, 0.0)

    def test_attack_then_decay_then_sustain(self):
        # AD = 0x09 means attack=0 (2ms), decay=9 (750ms).
        # SR = 0xF0 means sustain=15 (1.0), release=0 (6ms).
        emu = SIDEmulator()
        emu.update_registers(self._voice_regs(0, control=0x41,
                                              ad=0x09, sr=0xF0))
        emu.advance_envelopes(0.005)   # 5ms — past 2ms attack
        v = emu.voices[0]
        self.assertEqual(v.envelope_state, "decay")
        # Sustain is 15/15 = 1.0; decay should clamp at sustain immediately.
        self.assertAlmostEqual(v.envelope_level, 1.0, places=2)

    def test_pulse_waveform_two_levels(self):
        emu = SIDEmulator()
        emu.update_registers(self._voice_regs(0, freq=0x1C32, pw=0x0800,
                                              control=0x41,
                                              ad=0x09, sr=0xF0))
        # Force envelope to a known level.
        emu.advance_envelopes(0.01)
        s = emu.voice_samples(0, 320)
        # Pulse is binary; with envelope at 1.0 we expect samples to be
        # exactly +1 or -1 (modulo float precision).
        unique = np.unique(np.round(s, 5))
        self.assertEqual(set(unique.tolist()), {-1.0, 1.0})

    def test_sawtooth_monotonic_within_cycle(self):
        emu = SIDEmulator()
        # Sawtooth, gate on, instant envelope to full.
        emu.update_registers(self._voice_regs(0, freq=0x0100,    # very low freq
                                              control=0x21,
                                              ad=0x00, sr=0xF0))
        emu.advance_envelopes(0.01)
        s = emu.voice_samples(0, 64)
        # At very low freq, 64 samples should be a small portion of one cycle
        # → mostly monotonically rising (with one wrap at the cycle edge maybe).
        rising = np.sum(np.diff(s) > 0)
        self.assertGreater(rising, 50,
                           "sawtooth at very low freq should be mostly rising")

    def test_priority_noise_over_pulse(self):
        # 0xC0 = noise + pulse. Visualization picks noise.
        self.assertEqual(primary_waveform(0xC0), WAVE_NOISE)
        self.assertEqual(primary_waveform(0x30), WAVE_SAWTOOTH)
        self.assertEqual(primary_waveform(0x10), WAVE_TRIANGLE)
        self.assertEqual(primary_waveform(0x00), 0)


# ---------------------------------------------------------------------------
# Text-row layout helpers
# ---------------------------------------------------------------------------

class LayoutHelpersTest(unittest.TestCase):

    def test_lr_normal_fit(self):
        from c64cast.waveform import _layout_lr
        line = _layout_lr("LEFT", "RIGHT")
        self.assertEqual(len(line), 40)
        self.assertTrue(line.startswith("LEFT"))
        self.assertTrue(line.endswith("RIGHT"))

    def test_lr_truncates_long_inputs(self):
        from c64cast.waveform import _layout_lr
        line = _layout_lr("A" * 40, "B" * 40)
        self.assertEqual(len(line), 40)
        # Right is capped at half width first.
        self.assertTrue("B" in line)
        # At least one space between left and right.
        self.assertIn(" ", line)

    def test_lcr_centers_balanced(self):
        from c64cast.waveform import _layout_lcr
        line = _layout_lcr("1985", "6581", "PAL")
        self.assertEqual(len(line), 40)
        self.assertTrue(line.startswith("1985"))
        self.assertTrue(line.endswith("PAL"))
        # Center substring lies roughly in the middle.
        idx = line.index("6581")
        self.assertGreater(idx, 8)
        self.assertLess(idx, 32)

    def test_lcr_collision_avoidance(self):
        # Long left field must push the center right, not overlap.
        from c64cast.waveform import _layout_lcr
        line = _layout_lcr("A" * 25, "MID", "END")
        self.assertEqual(len(line), 40)
        # Left ends at col 25; center must start at col >= 26.
        left_end = 25
        center_idx = line.find("MID")
        self.assertGreaterEqual(center_idx, left_end + 1)


# ---------------------------------------------------------------------------
# WaveformScene
# ---------------------------------------------------------------------------

from _fakes import FakeAPI


def _write_sid_to_tempfile() -> str:
    # delete=False so the test owns cleanup via os.unlink; context manager
    # would close + unlink immediately.
    with tempfile.NamedTemporaryFile("wb", suffix=".sid", delete=False) as f:
        f.write(_make_sid_header())
        f.write(bytes(2048))
        return f.name


class WaveformSceneTest(unittest.TestCase):
    """The test SIDs here have zero-filled bring-up addresses (the
    fixture only writes a v1 header for parse_sid_header coverage), so
    they wouldn't survive the real SidHostEmu's parse_psid_for_player
    validation (play_addr=0 is refused). Patch SidHostEmu to a MagicMock
    for all WaveformScene tests — actual host-emulator behavior is
    covered in test_sid_host_emu.py."""

    def setUp(self):
        self.sid_path = _write_sid_to_tempfile()
        patcher = patch("c64cast.waveform.SidHostEmu")
        self.addCleanup(patcher.stop)
        self.mock_host_emu_cls = patcher.start()
        # Each WaveformScene gets its own emulator instance; default the
        # regs() shadow to all-zeros so the scene paints quiet traces
        # unless a test overrides scene._reg_buf manually.
        self.mock_host_emu_cls.return_value.regs.return_value = bytes(25)
        # _load_sid_file's PLAY pre-flight checks last_routine_capped after
        # each tick_play(); a bare MagicMock attribute is truthy and would
        # read as "always capped" → false rejection. Report not-capped.
        self.mock_host_emu_cls.return_value.last_routine_capped = False
        # _resolve_poll_rate() calls play_rate_hz() during construction; a
        # MagicMock return breaks the float math. Report the vsync rate.
        self.mock_host_emu_cls.return_value.play_rate_hz.return_value = 60.0
        # setup() footprints the tune via the real ram_write_footprint, which
        # builds a real SidHostEmu and rejects these header-only synthetic
        # SIDs (play_addr=0). Stub it to an empty avoid bitmap.
        fp = patch("c64cast.waveform.ram_write_footprint",
                   return_value=bytearray(65536))
        self.addCleanup(fp.stop)
        fp.start()
        # setup() also footprints via ram_play_access_footprint for the
        # display-bank choice; stub it too (same reason as ram_write_footprint
        # above — these header-only synthetic SIDs have play_addr=0).
        afp = patch("c64cast.waveform.ram_play_access_footprint",
                    return_value=bytearray(65536))
        self.addCleanup(afp.stop)
        afp.start()

    def tearDown(self):
        os.unlink(self.sid_path)

    def test_constructs_host_emulator(self):
        """SidHostEmu must be built with the SID bytes + resolved song —
        not just constructed with default args. Guards against future
        refactors silently dropping the song= forwarding."""
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=2)
        # The first construction is the scene's real host emu (subsequent
        # ones are the throwaway rate probe in _detect_play_rate_hz — see
        # that method). Both forward sid_bytes + song, but assert on the
        # real one.
        self.assertGreaterEqual(self.mock_host_emu_cls.call_count, 1)
        call = self.mock_host_emu_cls.call_args_list[0]
        self.assertEqual(call.args[0], scene.sid_bytes)
        self.assertEqual(call.kwargs.get("song", call.args[1] if
                         len(call.args) > 1 else None), 2)

    def test_cia_multispeed_rate_drives_poll_rate(self):
        """A CIA-timed tune (host emu reports a faster play_rate_hz) makes the
        scene tick the host emulator at that rate so the scope tracks the
        audio — not the fixed video rate."""
        from c64cast.waveform import WaveformScene
        self.mock_host_emu_cls.return_value.play_rate_hz.return_value = 90.0
        scene = WaveformScene(FakeAPI(), audio=None, file=self.sid_path,
                              song=1, duration_s=10.0, system="NTSC")
        self.assertAlmostEqual(scene._reg_poll_hz, 90.0)
        self.assertAlmostEqual(scene._poll_dt, 1.0 / 90.0)

    def test_explicit_reg_poll_hz_overrides_auto_rate(self):
        """An explicit reg_poll_hz pins the rate and skips CIA auto-detection
        (play_rate_hz isn't consulted)."""
        from c64cast.waveform import WaveformScene
        self.mock_host_emu_cls.return_value.play_rate_hz.return_value = 90.0
        scene = WaveformScene(FakeAPI(), audio=None, file=self.sid_path,
                              song=1, duration_s=10.0, system="NTSC",
                              reg_poll_hz=25.0)
        self.assertAlmostEqual(scene._reg_poll_hz, 25.0)
        self.mock_host_emu_cls.return_value.play_rate_hz.assert_not_called()

    def test_rate_probe_ticks_play_before_reading_rate(self):
        """Regression: a multispeed tune that programs CIA #1 Timer A from its
        PLAY routine (not INIT) — Galway's Times of Lore — must be detected as
        multispeed even when the rate is resolved right after a fresh INIT,
        which is what cycle_style() and a pool re-pick do. Before the fix the
        cycle path read play_rate_hz on an INIT-only host emu, saw the vsync
        default, and ticked the scope at HALF the song's real rate — voices
        came in progressively later than the audio (warped + delayed scope).
        _detect_play_rate_hz must run PLAY on its probe before reading."""
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None, file=self.sid_path,
                              song=1, duration_s=10.0, system="NTSC")
        emu = self.mock_host_emu_cls.return_value
        emu.reset_mock()  # drop the construction-time PLAY pre-flight ticks
        # play_rate_hz reports the vsync rate until PLAY has run, then the
        # true 2x rate — mirroring a Timer A latch written on the first PLAY.
        emu.play_rate_hz.side_effect = (
            lambda video_hz, clock_hz:
            120.0 if emu.tick_play.call_count > 0 else video_hz)
        detected = scene._detect_play_rate_hz()
        self.assertAlmostEqual(detected, 120.0,
                               msg="probe must run PLAY before reading rate")
        self.assertGreaterEqual(emu.tick_play.call_count, 1)

    def test_rate_probe_vsync_tune_returns_video_rate(self):
        """A genuine vsync tune writes no Timer A, so the probe runs all its
        passes and returns the video rate (no false multispeed)."""
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None, file=self.sid_path,
                              song=1, duration_s=10.0, system="NTSC")
        emu = self.mock_host_emu_cls.return_value
        emu.reset_mock()
        emu.play_rate_hz.side_effect = None
        emu.play_rate_hz.return_value = 60.0  # never reports multispeed
        self.assertAlmostEqual(scene._detect_play_rate_hz(), 60.0)
        # Probe exhausts its budget looking for a Timer A write that never
        # comes (bounded by _RATE_PROBE_TICKS).
        self.assertEqual(emu.tick_play.call_count,
                         WaveformScene._RATE_PROBE_TICKS)

    def test_default_target_fps_is_half_video_rate(self):
        """WaveformScene defaults to HALF the system video rate (30 NTSC /
        25 PAL) so the per-frame bitmap-strip DMA stays under the ceiling —
        full rate (~170 writes/s) power-cycles the U64 on bank-2 tunes
        (HW-verified). The host-emu poll rate stays at the full video rate."""
        from c64cast.waveform import WaveformScene
        ntsc = WaveformScene(FakeAPI(), audio=None, file=self.sid_path,
                             song=1, duration_s=10.0, system="NTSC")
        assert ntsc.target_fps is not None  # narrows Scene's float | None
        self.assertAlmostEqual(ntsc.target_fps, 30.0)
        self.assertAlmostEqual(ntsc._frame_time_s, 1.0 / 30.0)
        self.assertAlmostEqual(ntsc._video_hz, 60.0)  # poll cadence unchanged
        pal = WaveformScene(FakeAPI(), audio=None, file=self.sid_path,
                            song=1, duration_s=10.0, system="PAL")
        assert pal.target_fps is not None
        self.assertAlmostEqual(pal.target_fps, 25.0)
        self.assertAlmostEqual(pal._video_hz, 50.0)

    def test_explicit_target_fps_overrides_half_rate_default(self):
        """An explicit target_fps (CLI/TOML) still wins over the half-rate
        default."""
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None, file=self.sid_path,
                              song=1, duration_s=10.0, system="NTSC",
                              target_fps=60.0)
        assert scene.target_fps is not None
        self.assertAlmostEqual(scene.target_fps, 60.0)

    def test_accepts_sid_overlapping_bank0_relocates_to_bank2(self):
        """A SID whose payload overlaps bank 0's bitmap ($2000-$3F3F) but
        leaves bank 2 free is now ACCEPTED — the display relocates to bank 2
        ($8400/$A000, $DD00=$95) instead of being refused. Model it with a
        2 KB payload at $2700 (the old LN2-style refusal case)."""
        from c64cast.c64 import CIA2, VIC_BANK_2
        from c64cast.waveform import WaveformScene
        with tempfile.NamedTemporaryFile("wb", suffix=".sid", delete=False) as f:
            f.write(_make_sid_header(load_addr=0x2700, data_offset=124))
            f.write(bytes(2048))
            overlap_sid = f.name
        try:
            api = FakeAPI()
            scene = WaveformScene(api, audio=None, file=overlap_sid, song=1,
                                  duration_s=10.0)   # must NOT raise
            scene.setup()
            self.assertEqual(scene._dd00, CIA2.PORT_A_BANK_2)
            self.assertEqual(scene._bitmap_base, VIC_BANK_2.BITMAP)
            self.assertEqual(scene._screen_base, VIC_BANK_2.SCREEN)
        finally:
            os.unlink(overlap_sid)

    def test_rejects_sid_spanning_both_banks_display(self):
        """A SID whose payload overlaps the display regions of BOTH bank 0
        and bank 2 (e.g. $2700 through past $A000) leaves no free VIC bank
        and is refused with the new no-free-bank message."""
        from c64cast.waveform import WaveformScene
        with tempfile.NamedTemporaryFile("wb", suffix=".sid", delete=False) as f:
            # $2700 .. $BF40 spans bank 0 bitmap ($2000) and bank 2 bitmap
            # ($A000) -> no candidate bank is free.
            f.write(_make_sid_header(load_addr=0x2700, data_offset=124))
            f.write(bytes(0xBF40 - 0x2700))
            overlap_sid = f.name
        try:
            api = FakeAPI()
            # The candidate walk logs a per-candidate "skipping" warning
            # before raising; assertLogs (outer) asserts it and keeps it off
            # the console.
            with self.assertLogs("c64cast.waveform", level="WARNING"):
                with self.assertRaisesRegex(
                        ValueError, r"every candidate VIC bank"):
                    WaveformScene(api, audio=None, file=overlap_sid, song=1,
                                  duration_s=10.0)
        finally:
            os.unlink(overlap_sid)

    def test_accepts_sid_just_past_bitmap_area(self):
        """Boundary check: a SID loading at $3F40 (the byte AFTER the
        bitmap, $2000+8000) should NOT trigger the validation. Catches
        off-by-one regressions in the overlap math."""
        from c64cast.waveform import WaveformScene
        with tempfile.NamedTemporaryFile("wb", suffix=".sid", delete=False) as f:
            f.write(_make_sid_header(load_addr=0x3F40, data_offset=124))
            f.write(bytes(2048))
            ok_sid = f.name
        try:
            api = FakeAPI()
            # Should construct cleanly.
            WaveformScene(api, audio=None, file=ok_sid, song=1,
                          duration_s=10.0)
        finally:
            os.unlink(ok_sid)

    def test_setup_hires_uploads_bitmap_and_calls_run_sid_player(self):
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path,
                              song=2,
                              duration_s=10.0)
        scene.setup()
        try:
            self.assertIsNotNone(api.sid_played,
                                 "run_sid_player must be called from setup()")
            assert api.sid_played is not None
            self.assertEqual(api.sid_played[1], 2,
                             "explicit song must be forwarded")
            # Bitmap area zeroed in setup, hires VIC regs poked.
            self.assertEqual(api.memories.get("D018"), "18")
            self.assertEqual(api.memories.get("D011"), "3b")
            # Bitmap region 0x2000 got an 8000-byte write.
            self.assertEqual(len(api.regions[0x2000]), 8000)
        finally:
            scene.teardown()

    def test_setup_hires_paints_text_rows(self):
        # Hires setup must write the title-row + metadata-row strips into
        # the bitmap (320 bytes each at cell rows 22 and 23).
        from c64cast.c64 import SCREEN
        from c64cast.waveform import META_ROW, TITLE_ROW, WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=2,
                              duration_s=10.0)
        scene.setup()
        try:
            title_addr = SCREEN.BITMAP + TITLE_ROW * 320
            meta_addr = SCREEN.BITMAP + META_ROW * 320
            self.assertIn(title_addr, api.regions,
                          "title row bitmap must be uploaded")
            self.assertIn(meta_addr, api.regions,
                          "metadata row bitmap must be uploaded")
            self.assertEqual(len(api.regions[title_addr]), 320)
            self.assertEqual(len(api.regions[meta_addr]), 320)
            # And the corresponding screen-RAM color bytes.
            title_color_addr = SCREEN.RAM + TITLE_ROW * 40
            meta_color_addr = SCREEN.RAM + META_ROW * 40
            self.assertIn(title_color_addr, api.regions)
            self.assertIn(meta_color_addr, api.regions)
        finally:
            scene.teardown()

    def test_cycle_style_repaints_title_row_hires(self):
        # SHIFT-cycling the subtune must re-push the title row so the
        # displayed song number reflects the new subtune.
        from c64cast.c64 import SCREEN
        from c64cast.waveform import TITLE_ROW, WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              duration_s=10.0)
        scene.setup()
        try:
            title_addr = SCREEN.BITMAP + TITLE_ROW * 320
            before = api.regions[title_addr]
            scene.cycle_style(api)
            after = api.regions[title_addr]
            self.assertNotEqual(before, after,
                                "title row bitmap must change when the "
                                "song number is repainted")
        finally:
            scene.teardown()

    def test_song_number_is_zero_padded(self):
        # When num_songs has 2 digits, the rendered song number must be
        # zero-padded to 2 digits so the SHIFT-update window stays a
        # constant width regardless of which subtune is current.
        from c64cast.waveform import WaveformScene
        # Build a SID with num_songs=11 so the pad width is 2.
        wide = bytearray(_make_sid_header(num_songs=11, start_song=3))
        with tempfile.NamedTemporaryFile("wb", suffix=".sid",
                                         delete=False) as f:
            f.write(wide)
            f.write(bytes(2048))
            wide_path = f.name
        try:
            api = FakeAPI()
            scene = WaveformScene(api, audio=None,
                                  file=wide_path, song=3,
                                  duration_s=10.0)
            line, song_col = scene._build_title_line()
            # The song number must appear as "03" (zero-padded), not "3".
            self.assertEqual(line[song_col:song_col + 2], "03")
            # And the full "(SONG 03/11)" must be present in the line.
            self.assertIn("(SONG 03/11)", line)
        finally:
            os.unlink(wide_path)

    def test_invalid_color_mode_raises(self):
        from c64cast.waveform import WaveformScene
        with self.assertRaises(ValueError):
            WaveformScene(MagicMock(), audio=None,
                          file=self.sid_path,
                          color_mode="rainbow_per_pixel")

    def test_song_out_of_range_raises(self):
        from c64cast.waveform import WaveformScene
        # Header set num_songs=4. song=99 must fail. The candidate walk logs
        # a "skipping ... song 99 out of range" warning before raising;
        # assertLogs (outer) asserts it and keeps it off the console.
        with self.assertLogs("c64cast.waveform", level="WARNING"):
            with self.assertRaises(ValueError):
                WaveformScene(MagicMock(), audio=None,
                              file=self.sid_path, song=99)

    def test_song_defaults_to_header_start_song(self):
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(MagicMock(), audio=None,
                              file=self.sid_path, song=0)
        # Header start_song was 1.
        self.assertEqual(scene.song, 1)

    def test_process_frame_writes_bitmap(self):
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        # Pre-load some non-trivial register state into the API.
        regs = bytearray(25)
        regs[0] = 0x32   # voice 1 freq lo
        regs[1] = 0x1C
        regs[2] = 0x00
        regs[3] = 0x08   # pw
        regs[4] = 0x41   # pulse + gate
        regs[5] = 0x09
        regs[6] = 0xF0
        api.canned_regs = bytes(regs)

        scene = WaveformScene(api, audio=None,
                              file=self.sid_path,
                              duration_s=2.0)
        scene.setup()
        # Simulate the poll thread populating the reg snapshot.
        with scene._reg_lock:
            scene._reg_buf = api.canned_regs
        scene.emulator.update_registers(api.canned_regs)
        scene.emulator.advance_envelopes(0.01)

        api.regions.clear()
        still_active = scene.process_frame(scene.start_time + 0.1)
        scene.teardown()
        self.assertTrue(still_active)
        # Voice 1's bitmap slice should have non-zero bytes (the trace).
        v1_slice = api.regions.get(0x2000)
        self.assertIsNotNone(v1_slice)
        assert v1_slice is not None
        self.assertGreater(sum(v1_slice), 0,
                           "voice with pulse + envelope should paint pixels")

    def test_duration_ends_scene(self):
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path,
                              duration_s=0.05)
        scene.setup()
        try:
            self.assertTrue(scene.process_frame(scene.start_time + 0.01))
            self.assertFalse(scene.process_frame(scene.start_time + 1.0),
                             "scene must end after duration_s")
        finally:
            scene.teardown()

    def test_teardown_silences_and_restores(self):
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None, file=self.sid_path)
        scene.setup()
        scene.teardown()
        self.assertIn("SILENCE", api.regs)
        self.assertIn("RESTORE_IRQ", api.regs)
        # BLNSW suppression: the player MC's JMP * spin survives
        # teardown, so BASIC's GOTO 20 loop is no longer running and
        # the kernal editor's cursor-blink path would otherwise toggle
        # one screen cell on subsequent PETSCII scenes. teardown must
        # write $00CC = $80 to stop that — verified live on hardware
        # 2026-05-26, see [[cursor-blink-after-waveform-teardown]].
        self.assertIn("SUPPRESS_BLINK", api.regs)

    def test_cycle_style_advances_song(self):
        # Header sets num_songs=4, start_song=1. Cycle should go 1→2.
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              duration_s=10.0)
        scene.setup()
        try:
            # Reset to drop the setup-time recording so we only inspect
            # the cycle-time behavior.
            api.sid_played = None
            api.cue_song_reinits.clear()
            # Drop the setup-time host-emu constructions so we only inspect
            # what the cycle builds (the real rebuild + the rate probe).
            self.mock_host_emu_cls.reset_mock()
            label = scene.cycle_style(api)
            self.assertEqual(scene.song, 2)
            self.assertEqual(label, "song 2/4")
            # Fast-path: cycle cues the in-place re-INIT stub, does NOT
            # re-call run_sid_player (which would trigger a run_prg →
            # VIC reset → flicker).
            self.assertEqual(api.cue_song_reinits, [2])
            self.assertIsNone(api.sid_played,
                              "cycle must NOT re-run the full SID player")
            # Host emulator was rebuilt on the new song so the visualizer
            # tracks the right subtune. Every construction this cycle (the
            # real rebuild + the rate probe) targets the new song.
            self.assertGreaterEqual(self.mock_host_emu_cls.call_count, 1,
                                    "host emulator must be rebuilt on cycle")
            for call in self.mock_host_emu_cls.call_args_list:
                self.assertEqual(call.kwargs.get("song"), 2)
        finally:
            scene.teardown()

    def test_cycle_style_does_not_re_setup_vic(self):
        # Cycle must preserve VIC state — no invalidate_cache, no full
        # hires re-setup. Verifies the flicker fix: the old
        # cycle_style called _setup_hires which re-wrote the bitmap
        # zero-fill + per-voice color strips on every SHIFT.
        from c64cast.c64 import SCREEN
        from c64cast.waveform import BITMAP_STRIPS, WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              duration_s=10.0)
        scene.setup()
        try:
            cache_invalidations_before = api.cache_invalidations
            # Capture each voice strip's color-RAM write address and
            # confirm setup() wrote them (sanity).
            voice_color_addrs = []
            for (top, _bot) in BITMAP_STRIPS:
                addr = SCREEN.RAM + (top // 8) * 40
                voice_color_addrs.append(addr)
                self.assertIn(addr, api.regions)
            # Clear so we can detect any new writes the cycle makes.
            api.regions.pop(SCREEN.BITMAP, None)
            for addr in voice_color_addrs:
                api.regions.pop(addr, None)

            scene.cycle_style(api)
            self.assertEqual(api.cache_invalidations,
                             cache_invalidations_before,
                             "cycle must not invalidate_cache — VIC state "
                             "is preserved across the in-place re-INIT")
            self.assertNotIn(SCREEN.BITMAP, api.regions,
                             "cycle must not re-zero the bitmap")
            for addr in voice_color_addrs:
                self.assertNotIn(addr, api.regions,
                                 "cycle must not re-paint voice color strips")
        finally:
            scene.teardown()

    def test_cycle_style_wraps_at_last_song(self):
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        # Header has num_songs=4; start at 4 → wrap to 1.
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=4,
                              duration_s=10.0)
        scene.setup()
        try:
            label = scene.cycle_style(api)
            self.assertEqual(scene.song, 1)
            self.assertEqual(label, "song 1/4")
        finally:
            scene.teardown()

    def test_cycle_style_single_song_returns_none(self):
        # Build a SID with num_songs=1 and verify cycle_style is a no-op.
        from c64cast.waveform import WaveformScene
        single = bytearray(_make_sid_header(num_songs=1, start_song=1))
        with tempfile.NamedTemporaryFile("wb", suffix=".sid",
                                         delete=False) as f:
            f.write(single)
            f.write(bytes(2048))
            single_path = f.name
        try:
            api = FakeAPI()
            scene = WaveformScene(api, audio=None,
                                  file=single_path, song=1,
                                  duration_s=10.0)
            scene.setup()
            try:
                api.sid_played = None
                self.assertIsNone(scene.cycle_style(api))
                self.assertEqual(scene.song, 1)
                # No re-run on a single-song cycle.
                self.assertIsNone(api.sid_played)
            finally:
                scene.teardown()
        finally:
            os.unlink(single_path)

    def test_cycle_style_resets_duration_timer(self):
        # The new song should get its full duration_s — start_time must
        # be reset so the duration check in process_frame starts over.
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              duration_s=10.0)
        scene.setup()
        try:
            # Pretend the prior song nearly ran its full duration.
            scene.start_time -= 9.5
            old_start = scene.start_time
            scene.cycle_style(api)
            self.assertGreater(scene.start_time, old_start + 5.0,
                               "cycle must reset start_time so the new "
                               "song gets its full duration")
        finally:
            scene.teardown()

    def test_cycle_style_relookups_songlengths(self):
        # When duration_s wasn't explicit and a SongLengths DB is present,
        # the new song's duration must be re-resolved from the DB.
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        fake_db = MagicMock()
        # First lookup (__init__ for song=1) → 120s; second (cycle to 2) → 45s.
        fake_db.lookup.side_effect = [120.0, 45.0]
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              songlengths_db=fake_db)
        self.assertAlmostEqual(scene.duration_s, 120.0)
        scene.setup()
        try:
            scene.cycle_style(api)
            self.assertAlmostEqual(scene.duration_s, 45.0,
                                   msg="cycle must re-resolve duration "
                                   "from the SongLengths DB for the new song")
            # The DB was queried twice (init + cycle), each time with the
            # song that was current at lookup time.
            self.assertEqual(fake_db.lookup.call_count, 2)
            self.assertEqual(fake_db.lookup.call_args_list[1].args[1], 2)
        finally:
            scene.teardown()

    def test_cycle_style_explicit_duration_survives_cycle(self):
        # A user-set duration_s must NOT be overwritten by a re-lookup —
        # explicit user intent wins, same as __init__'s precedence.
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        fake_db = MagicMock()
        fake_db.lookup.return_value = 99.0
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              duration_s=42.0,
                              songlengths_db=fake_db)
        self.assertAlmostEqual(scene.duration_s, 42.0)
        scene.setup()
        try:
            scene.cycle_style(api)
            self.assertAlmostEqual(scene.duration_s, 42.0)
        finally:
            scene.teardown()

    def test_cycle_style_skips_short_subtune(self):
        # Header num_songs=4. From song=1, song 2 is a 2s SFX (skip),
        # song 3 is a 60s tune (take). Cycle should land on song 3.
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        fake_db = MagicMock()
        # Init lookup for song 1, then cycle queries: song 2 (skip), song 3.
        fake_db.lookup.side_effect = [120.0, 2.0, 60.0]
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              songlengths_db=fake_db)
        scene.setup()
        try:
            with self.assertLogs("c64cast.waveform", level="INFO") as cap:
                label = scene.cycle_style(api)
            self.assertEqual(scene.song, 3,
                             "cycle must skip the short song and land on 3")
            self.assertEqual(label, "song 3/4")
            self.assertAlmostEqual(scene.duration_s, 60.0)
            # Skip log surfaced the SFX so the operator can see why we
            # jumped two songs instead of one.
            self.assertTrue(
                any("skipping song 2/4" in line and "2.0s" in line
                    for line in cap.output),
                f"expected skip log, got {cap.output!r}")
        finally:
            scene.teardown()

    def test_cycle_style_no_skip_without_db(self):
        # No SongLengths DB means we have no basis to call anything "short"
        # — cycle must take the immediate next song regardless of length.
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              duration_s=10.0)   # explicit so __init__ skips DB
        # No songlengths_db on the scene at all.
        self.assertIsNone(scene.songlengths_db)
        scene.setup()
        try:
            scene.cycle_style(api)
            self.assertEqual(scene.song, 2,
                             "no DB → no skip; advance one slot")
        finally:
            scene.teardown()

    def test_cycle_style_no_skip_when_duration_explicit(self):
        # An explicit duration_s is the user saying "play each subtune
        # for exactly this long" — cycle must respect it and not skip
        # short subtunes (the user already opted into the duration).
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        fake_db = MagicMock()
        # If the skip logic ran, it would query the DB.
        fake_db.lookup.return_value = 2.0
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              duration_s=10.0,
                              songlengths_db=fake_db)
        # __init__ doesn't lookup when duration_s is explicit.
        fake_db.lookup.reset_mock()
        scene.setup()
        try:
            scene.cycle_style(api)
            self.assertEqual(scene.song, 2)
            self.assertAlmostEqual(scene.duration_s, 10.0,
                                   msg="explicit duration_s must survive")
            fake_db.lookup.assert_not_called()
        finally:
            scene.teardown()

    def test_cycle_style_no_skip_on_db_miss(self):
        # DB returns None for the candidate (no entry) → we have no basis
        # to call it short, so take it. (A miss is not the same as short.)
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        fake_db = MagicMock()
        fake_db.lookup.side_effect = [None, None]   # init + cycle, both miss
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              songlengths_db=fake_db)
        scene.setup()
        try:
            scene.cycle_style(api)
            self.assertEqual(scene.song, 2,
                             "DB miss must not trigger skip")
        finally:
            scene.teardown()

    def test_cycle_style_all_short_falls_through(self):
        # Every other subtune is below threshold → cycle lands on the
        # first candidate anyway (user pressed SHIFT, give a change) and
        # keeps the prior duration_s as the safest fallback.
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        fake_db = MagicMock()
        # Init: song 1 → 30s. Cycle: every candidate (2, 3, 4) returns 1s.
        fake_db.lookup.side_effect = [30.0, 1.0, 1.0, 1.0]
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=1,
                              songlengths_db=fake_db)
        self.assertAlmostEqual(scene.duration_s, 30.0)
        scene.setup()
        try:
            scene.cycle_style(api)
            self.assertEqual(scene.song, 2,
                             "all-short → land on first candidate (2)")
            # Cycle queried each of the other 3 songs (n-1 attempts).
            self.assertEqual(fake_db.lookup.call_count, 4)
            # Prior duration kept since no candidate was long enough to
            # adopt; the all-short fall-through is too rare to special-case.
            self.assertAlmostEqual(scene.duration_s, 30.0)
        finally:
            scene.teardown()

    def test_init_honors_short_start_song(self):
        # Startup is exempt from the skip logic: if the user pinned an
        # SFX as the start song (config song=N or PSID start_song), play
        # it. Skip only kicks in on SHIFT cycle.
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        fake_db = MagicMock()
        fake_db.lookup.return_value = 1.5   # short, but explicitly chosen
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, song=2,
                              songlengths_db=fake_db)
        self.assertEqual(scene.song, 2)
        self.assertAlmostEqual(scene.duration_s, 1.5,
                               msg="startup must honor the configured "
                               "song's length, no matter how short")


# ---------------------------------------------------------------------------
# WaveformScene poll-thread wall-clock catch-up
# ---------------------------------------------------------------------------

class WaveformPollCatchupTest(unittest.TestCase):
    """_poll_regs derives the host emulator's PLAY-tick count from wall-clock
    elapsed since the real SID started, not from poll-wakeup count — so the
    scope catches up through the _setup_hires gap and doesn't drift behind
    the audio. These tests drive _poll_regs directly with a controlled clock
    and a stubbed host emulator + emulator."""

    def setUp(self):
        self.sid_path = _write_sid_to_tempfile()
        patcher = patch("c64cast.waveform.SidHostEmu")
        self.addCleanup(patcher.stop)
        cls = patcher.start()
        cls.return_value.regs.return_value = bytes(25)
        cls.return_value.retriggers.return_value = (False, False, False)
        cls.return_value.last_routine_capped = False
        cls.return_value.play_rate_hz.return_value = 60.0

    def tearDown(self):
        os.unlink(self.sid_path)

    def _scene(self):
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None, file=self.sid_path,
                              song=1, duration_s=10.0, system="NTSC")
        # Fresh controllable stubs for the poll path.
        scene._host_emu = MagicMock()
        scene._host_emu.regs.return_value = bytes(25)
        scene._host_emu.retriggers.return_value = (False, False, False)
        scene.emulator = MagicMock()
        scene._reg_poll_hz = 60.0
        scene._poll_dt = 1.0 / 60.0
        scene._ticks_done = 0
        return scene

    def test_catches_up_to_wallclock_target(self):
        scene = self._scene()
        # 5 frames of elapsed time at 60 Hz → 5 PLAY ticks expected.
        with patch("c64cast.waveform.time.time") as now:
            scene._sid_start_time = 1000.0
            now.return_value = 1000.0 + 5 / 60.0
            scene._poll_regs()
        self.assertEqual(scene._host_emu.tick_play.call_count, 5)
        self.assertEqual(scene._ticks_done, 5)
        # Envelope advanced once per caught-up tick, each by the fixed dt.
        self.assertEqual(scene.emulator.advance_envelopes.call_count, 5)
        scene.emulator.advance_envelopes.assert_called_with(scene._poll_dt)

    def test_no_ticks_when_not_yet_due(self):
        scene = self._scene()
        scene._ticks_done = 10
        with patch("c64cast.waveform.time.time") as now:
            scene._sid_start_time = 1000.0
            # Only ~10 frames elapsed but we've already done 10 ticks.
            now.return_value = 1000.0 + 10 / 60.0
            scene._poll_regs()
        scene._host_emu.tick_play.assert_not_called()
        self.assertEqual(scene._ticks_done, 10)

    def test_catchup_is_capped(self):
        scene = self._scene()
        # A long stall: thousands of frames behind. Catch-up must be bounded
        # to _MAX_CATCHUP_TICKS in a single wakeup, then resync over later
        # wakeups.
        with patch("c64cast.waveform.time.time") as now:
            scene._sid_start_time = 1000.0
            now.return_value = 1000.0 + 100.0   # 6000 frames @ 60 Hz
            scene._poll_regs()
        self.assertEqual(scene._host_emu.tick_play.call_count,
                         scene._MAX_CATCHUP_TICKS)
        self.assertEqual(scene._ticks_done, scene._MAX_CATCHUP_TICKS)


# ---------------------------------------------------------------------------
# WaveformScene multi-file pool selection
# ---------------------------------------------------------------------------

class WaveformPoolPickTest(unittest.TestCase):
    """`file =` spec accepts directories / globs / comma combinations.
    Single-file specs stay deterministic; multi-file pools pick a random
    candidate per setup() and skip SIDs that fail payload validation."""

    def setUp(self):
        # Same patch as WaveformSceneTest — the synthetic SIDs here
        # wouldn't survive the real host emulator's PSID checks.
        patcher = patch("c64cast.waveform.SidHostEmu")
        self.addCleanup(patcher.stop)
        self.mock_host_emu_cls = patcher.start()
        self.mock_host_emu_cls.return_value.regs.return_value = bytes(25)
        # PLAY pre-flight reads last_routine_capped; keep it falsy (see
        # WaveformSceneTest.setUp) so these synthetic SIDs aren't rejected.
        self.mock_host_emu_cls.return_value.last_routine_capped = False
        self.mock_host_emu_cls.return_value.play_rate_hz.return_value = 60.0
        fp = patch("c64cast.waveform.ram_write_footprint",
                   return_value=bytearray(65536))
        self.addCleanup(fp.stop)
        fp.start()
        # setup() also footprints via ram_play_access_footprint for the
        # display-bank choice; stub it too (same reason as ram_write_footprint
        # above — these header-only synthetic SIDs have play_addr=0).
        afp = patch("c64cast.waveform.ram_play_access_footprint",
                    return_value=bytearray(65536))
        self.addCleanup(afp.stop)
        afp.start()
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))

    def _write_sid(self, filename: str, **header_kwargs) -> str:
        path = os.path.join(self.tmpdir, filename)
        with open(path, "wb") as f:
            f.write(_make_sid_header(**header_kwargs))
            f.write(bytes(2048))
        return path

    def test_directory_spec_picks_from_pool(self):
        from c64cast.waveform import WaveformScene
        self._write_sid("alpha.sid", name=b"ALPHA")
        self._write_sid("beta.sid", name=b"BETA")
        api = FakeAPI()
        # Force the seed so the pick is deterministic for the assertion;
        # behavior under any seed is "lands on one of the candidates".
        random.seed(0)
        scene = WaveformScene(api, audio=None, file=self.tmpdir,
                              duration_s=10.0)
        self.assertIn(os.path.basename(scene._sid_file),
                      {"alpha.sid", "beta.sid"})
        # Pool is preserved for re-pick at setup().
        self.assertEqual(len(scene._candidates), 2)

    def test_glob_spec_picks_from_matches(self):
        from c64cast.waveform import WaveformScene
        self._write_sid("a.sid")
        self._write_sid("b.sid")
        # Drop a non-matching file in the same dir to confirm the glob
        # filters extensions even when the OS would happily list it.
        with open(os.path.join(self.tmpdir, "ignore.txt"), "w") as f:
            f.write("")
        api = FakeAPI()
        random.seed(0)
        scene = WaveformScene(api, audio=None,
                              file=os.path.join(self.tmpdir, "*.sid"),
                              duration_s=10.0)
        self.assertTrue(scene._sid_file.endswith(".sid"))
        self.assertEqual(len(scene._candidates), 2)

    def test_comma_spec_unions_entries(self):
        from c64cast.waveform import WaveformScene
        p1 = self._write_sid("solo.sid")
        # A second dir with two more sids — the comma-combined spec should
        # pick from all 3 candidates.
        d2 = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d2, ignore_errors=True))
        with open(os.path.join(d2, "x.sid"), "wb") as f:
            f.write(_make_sid_header())
            f.write(bytes(2048))
        with open(os.path.join(d2, "y.sid"), "wb") as f:
            f.write(_make_sid_header())
            f.write(bytes(2048))
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=f"{p1}, {d2}", duration_s=10.0)
        self.assertEqual(len(scene._candidates), 3)

    def test_init_retries_when_first_pick_overlaps_bitmap(self):
        """A pool containing one invalid + one valid SID must NOT raise —
        the invalid candidate gets skipped at pick time, valid one wins."""
        from c64cast.waveform import WaveformScene
        # Bad: load_addr at $2700 collides with the hires bitmap area.
        self._write_sid("bad.sid", load_addr=0x2700, data_offset=124)
        # Good: load_addr at $1000 sits below the bitmap area.
        self._write_sid("good.sid", load_addr=0x1000, data_offset=124)
        api = FakeAPI()
        # Seed so the bad SID is attempted first deterministically; if my
        # retry loop didn't work the construction would raise. (random
        # shuffle of [bad, good] under seed 1 puts bad first.)
        random.seed(1)
        scene = WaveformScene(api, audio=None, file=self.tmpdir,
                              duration_s=10.0)
        self.assertTrue(scene._sid_file.endswith("good.sid"))

    def test_init_raises_when_every_candidate_invalid(self):
        from c64cast.waveform import WaveformScene
        # Both candidates span bank 0's AND bank 2's display ($2700 past
        # $A000), so neither can host the display -> all rejected -> raise.
        big = bytes(0xBF40 - 0x2700)
        for fn in ("bad1.sid", "bad2.sid"):
            with open(os.path.join(self.tmpdir, fn), "wb") as f:
                f.write(_make_sid_header(load_addr=0x2700, data_offset=124))
                f.write(big)
        api = FakeAPI()
        # Each invalid candidate logs a "skipping" warning before the final
        # raise; assertLogs (outer) asserts it and keeps it off the console.
        with self.assertLogs("c64cast.waveform", level="WARNING"):
            with self.assertRaisesRegex(ValueError, "none could be loaded"):
                WaveformScene(api, audio=None, file=self.tmpdir,
                              duration_s=10.0)

    def test_single_file_pool_skips_repick_at_setup(self):
        """Single-file specs stay deterministic AND keep cycle_style
        mutations (self.song advances) across setup/teardown cycles — the
        re-pick is suppressed when the pool has exactly one entry."""
        from c64cast.waveform import WaveformScene
        path = self._write_sid("only.sid")
        api = FakeAPI()
        scene = WaveformScene(api, audio=None, file=path,
                              duration_s=10.0, song=1)
        scene.setup()
        try:
            scene.cycle_style(api)
            self.assertEqual(scene.song, 2)
            # Teardown + re-setup must preserve the cycled subtune.
            scene.teardown()
            scene.setup()
            self.assertEqual(scene.song, 2,
                             "single-file pool must NOT re-pick at setup "
                             "(would reset cycle_style mutations to start_song)")
        finally:
            scene.teardown()


# ---------------------------------------------------------------------------
# Playlist per-scene target_fps + frame drop
# ---------------------------------------------------------------------------

class TargetFpsTest(unittest.TestCase):

    def test_scene_target_fps_overrides_default(self):
        from c64cast.playlist import Playlist
        from tests.test_playlist import FakeApi, FakeScene
        scenes = [FakeScene("A", frames_until_done=2)]
        scenes[0].target_fps = 15.0
        pl = Playlist(scenes, FakeApi(), target_fps=60.0,
                      heartbeat_interval=0.0,
                      interstitial_factory=lambda name: FakeScene(
                          f"t:{name}", frames_until_done=1))
        ft = pl._frame_time_for(scenes[0])
        self.assertAlmostEqual(ft, 1.0 / 15.0)

    def test_bitmap_default_to_30fps(self):
        from c64cast.playlist import Playlist
        from tests.test_playlist import FakeApi, FakeScene
        s = FakeScene("A")
        # No explicit target_fps, but a "bitmap" display mode (cap = 30).
        s.display_mode = MagicMock(is_bitmapped=True, default_target_fps=30.0)
        pl = Playlist([s], FakeApi(), target_fps=60.0,
                      heartbeat_interval=0.0,
                      interstitial_factory=lambda name: FakeScene(
                          f"t:{name}", frames_until_done=1))
        ft = pl._frame_time_for(s)
        self.assertAlmostEqual(ft, 1.0 / 30.0)

    def test_non_bitmap_uses_default(self):
        from c64cast.playlist import Playlist
        from tests.test_playlist import FakeApi, FakeScene
        s = FakeScene("A")
        s.display_mode = MagicMock(is_bitmapped=False, default_target_fps=None)
        pl = Playlist([s], FakeApi(), target_fps=60.0,
                      heartbeat_interval=0.0,
                      interstitial_factory=lambda name: FakeScene(
                          f"t:{name}", frames_until_done=1))
        ft = pl._frame_time_for(s)
        self.assertAlmostEqual(ft, 1.0 / 60.0)


# ---------------------------------------------------------------------------
# Visualization knobs: time_base, persistence, scroll_columns
# ---------------------------------------------------------------------------


class WaveformVizKnobsTest(unittest.TestCase):
    """Coverage for the auto-time-base, persistence, and scroll_columns
    knobs added on top of the redraw-from-scratch base implementation."""

    def setUp(self):
        self.sid_path = _write_sid_to_tempfile()
        patcher = patch("c64cast.waveform.SidHostEmu")
        self.addCleanup(patcher.stop)
        mock_host = patcher.start()
        mock_host.return_value.regs.return_value = bytes(25)
        # PLAY pre-flight reads last_routine_capped; keep it falsy (see
        # WaveformSceneTest.setUp) so these synthetic SIDs aren't rejected.
        mock_host.return_value.last_routine_capped = False
        mock_host.return_value.play_rate_hz.return_value = 60.0
        fp = patch("c64cast.waveform.ram_write_footprint",
                   return_value=bytearray(65536))
        self.addCleanup(fp.stop)
        fp.start()
        # setup() also footprints via ram_play_access_footprint for the
        # display-bank choice; stub it too (same reason as ram_write_footprint
        # above — these header-only synthetic SIDs have play_addr=0).
        afp = patch("c64cast.waveform.ram_play_access_footprint",
                    return_value=bytearray(65536))
        self.addCleanup(afp.stop)
        afp.start()

    def tearDown(self):
        os.unlink(self.sid_path)

    # ---- defaults preserve the redraw-from-scratch fast path ----

    def test_default_knobs_take_fast_path(self):
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0)
        self.assertTrue(scene._fast_path,
                        "default config must keep the fast redraw path")
        self.assertEqual(scene.scroll_columns, [0, 0, 0])
        self.assertEqual(scene.persistence, "off")
        self.assertEqual(scene._echo_depth, 0)
        self.assertEqual(scene._voice_render_modes, ["fast", "fast", "fast"])

    # ---- auto-time-base derivation ----

    def test_auto_time_window_matches_freq(self):
        from c64cast.sidemu import ACCUMULATOR_RANGE
        from c64cast.waveform import BITMAP_W, WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0,
                              time_base="auto", auto_cycles=4.0)
        # Force voice 0 audible with a real freq value.
        v = scene.emulator.voices[0]
        v.freq = 4400              # arbitrary — picks a real period
        v.control = WAVE_SAWTOOTH  # primary_waveform non-zero
        v.envelope_level = 0.8
        period_s = ACCUMULATOR_RANGE / (v.freq * scene.emulator.clock)
        expected = 4.0 * period_s
        # Full-screen window covers auto_cycles periods.
        got = scene._voice_time_window_s(0, BITMAP_W)
        self.assertAlmostEqual(got, expected, places=10)

    def test_auto_time_window_scales_with_n_cols(self):
        """Bug regression: in scroll mode each new batch of n_cols covers
        a slice of the full-screen window proportional to n_cols/BITMAP_W.
        Without this scaling, a small scroll batch sampled `auto_cycles`
        full periods into a few pixels and the trace went random."""
        from c64cast.waveform import BITMAP_W, WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0,
                              time_base="auto", auto_cycles=4.0)
        v = scene.emulator.voices[0]
        v.freq = 4400
        v.control = WAVE_SAWTOOTH
        v.envelope_level = 0.8
        full = scene._voice_time_window_s(0, BITMAP_W)
        # n_cols = 4 (one scroll step) → 4/320 of the full window.
        partial = scene._voice_time_window_s(0, 4)
        self.assertAlmostEqual(partial, full * 4 / BITMAP_W, places=12)

    def test_auto_silent_voice_falls_back_to_wallclock(self):
        from c64cast.waveform import BITMAP_W, WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0,
                              target_fps=60.0,
                              time_base="auto", auto_cycles=4.0)
        # freq=0 → fallback. _voice_time_window_s with n_cols=BITMAP_W
        # should equal one full display-frame of audio time.
        scene.emulator.voices[0].freq = 0
        scene.emulator.voices[0].control = 0
        scene.emulator.voices[0].envelope_level = 0.0
        got = scene._voice_time_window_s(0, BITMAP_W)
        self.assertAlmostEqual(got, 1.0 / 60.0, places=10)
        # Partial-width fallback scales linearly per column.
        got_partial = scene._voice_time_window_s(0, 4)
        self.assertAlmostEqual(got_partial, (1.0 / 60.0) * 4 / BITMAP_W,
                               places=10)

    # ---- persistence resolution ----

    def test_persistence_random_resolves_to_named_preset(self):
        from c64cast.waveform import (
            _PERSISTENCE_RANDOM_CHOICES,
            WaveformScene,
        )
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0,
                              persistence="random")
        self.assertIn(scene.persistence, _PERSISTENCE_RANDOM_CHOICES,
                      f"random must resolve to one of {_PERSISTENCE_RANDOM_CHOICES}, "
                      f"got {scene.persistence!r}")
        # Original config string is preserved for the setup log.
        self.assertEqual(scene.persistence_config, "random")
        # Resolved preset gives a non-empty echo ramp.
        self.assertGreater(scene._echo_depth, 0)
        self.assertEqual(len(scene._echo_colors), scene._echo_depth)

    def test_persistence_invalid_raises(self):
        from c64cast.waveform import WaveformScene
        with self.assertRaises(ValueError):
            WaveformScene(FakeAPI(), audio=None,
                          file=self.sid_path,
                          persistence="weird")

    # ---- scroll_columns normalization + validation ----

    def test_scroll_columns_scalar_broadcasts(self):
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0,
                              scroll_columns=4)
        self.assertEqual(scene.scroll_columns, [4, 4, 4])

    def test_scroll_columns_per_voice_list(self):
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0,
                              scroll_columns=[1, 0, 8])
        self.assertEqual(scene.scroll_columns, [1, 0, 8])

    def test_scroll_columns_bad_length_raises(self):
        from c64cast.waveform import WaveformScene
        with self.assertRaises(ValueError):
            WaveformScene(FakeAPI(), audio=None,
                          file=self.sid_path,
                          scroll_columns=[1, 2])

    def test_scroll_columns_negative_raises(self):
        from c64cast.waveform import WaveformScene
        with self.assertRaises(ValueError):
            WaveformScene(FakeAPI(), audio=None,
                          file=self.sid_path,
                          scroll_columns=-1)

    def test_time_base_invalid_raises(self):
        from c64cast.waveform import WaveformScene
        with self.assertRaises(ValueError):
            WaveformScene(FakeAPI(), audio=None,
                          file=self.sid_path,
                          time_base="bogus")

    # ---- scroll: actual FIFO behavior ----

    def test_scroll_shifts_strip_left(self):
        """With scroll_columns=8, after one frame the strip's leftmost
        (BITMAP_W - 8) columns equal the previous frame's columns 8..end —
        a literal FIFO shift."""
        from c64cast.waveform import BITMAP_W, WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, duration_s=10.0,
                              scroll_columns=8, persistence="off")
        scene.setup()
        try:
            scene._render_hires()
            assert scene._strips is not None
            assert scene._strips[0] is not None
            strip0 = scene._strips[0].copy()
            scene._render_hires()
            strip1 = scene._strips[0]
            assert strip1 is not None
            np.testing.assert_array_equal(
                strip1[:, :BITMAP_W - 8],
                strip0[:, 8:],
            )
        finally:
            scene.teardown()

    def test_scroll_continuity_threads_last_y_across_frames(self):
        """Bug regression: in scroll mode the first new column must
        connect to the previous frame's last column. Without this, every
        scroll boundary had a single-pixel self-dot, fragmenting the
        trace into N-column chunks. _last_y captures the connection
        across frames; _span_mask uses it as the prev-y for column 0."""
        from c64cast.waveform import BITMAP_W, WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, duration_s=10.0,
                              scroll_columns=4, persistence="off")
        scene.setup()
        try:
            assert scene._last_y is not None
            # First frame: _last_y starts as None (no continuity), then
            # gets populated with the last column's y.
            self.assertIsNone(scene._last_y[0])
            scene._render_hires()
            self.assertIsNotNone(scene._last_y[0],
                                 "first render must populate _last_y")
            # Second frame: the new batch should connect to the prior
            # frame's last y via _last_y. We can verify by checking the
            # rightmost-but-N column range (the new cols' x boundary)
            # draws a span instead of a single dot.
            scene._render_hires()
            assert scene._strips is not None
            strip = scene._strips[0]
            assert strip is not None
            # The column that joins old → new is BITMAP_W - 4. Its mask
            # should have AT LEAST 1 lit pixel; if continuity were broken
            # AND the new y happened to differ from y_after_first, it
            # would still have at least 1, but the previous column would
            # only have a self-dot. Check that columns 0 and 4 of the
            # new-cols region both have at least 1 lit pixel (i.e. the
            # spans aren't degenerate dots).
            new_region = strip[:, BITMAP_W - 4:]
            self.assertGreater(new_region[:, 0].sum(), 0,
                               "first new column must have a span")
        finally:
            scene.teardown()

    # ---- persistence: echo history ring grows + caps ----

    def test_persistence_echo_history_caps_at_depth(self):
        """The echo ring buffer accumulates up to echo_depth past frames
        then drops the oldest. After echo_depth+3 renders, len(history)
        should equal echo_depth exactly."""
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0,
                              persistence="long")  # depth = 4 echoes
        scene.setup()
        try:
            assert scene._echo_history is not None
            self.assertEqual(scene._echo_depth, 4)
            self.assertEqual(scene._voice_render_modes,
                             ["echo", "echo", "echo"])
            for _ in range(scene._echo_depth + 3):
                scene._render_hires()
            for v_idx in range(3):
                self.assertEqual(len(scene._echo_history[v_idx]),
                                 scene._echo_depth,
                                 f"voice {v_idx} history must cap at depth")
        finally:
            scene.teardown()

    def test_persistence_ignored_in_scroll_mode(self):
        """When scroll_columns > 0 on a voice, that voice takes the
        scroll path (no echo) regardless of the persistence preset —
        scroll itself supplies the trail; per-frame persistence would
        double-count and kill the trace by overdrawing."""
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0,
                              persistence="long", scroll_columns=4)
        self.assertEqual(scene._voice_render_modes,
                         ["scroll", "scroll", "scroll"])
        # echo_depth/echo_colors are still resolved from the preset (the
        # render path just doesn't use them when in scroll mode).
        self.assertEqual(scene._echo_depth, 4)

    def test_persistence_mixed_modes_per_voice(self):
        """Per-voice scroll_columns lets one voice scroll while others
        get the echo treatment, when persistence is set."""
        from c64cast.waveform import WaveformScene
        scene = WaveformScene(FakeAPI(), audio=None,
                              file=self.sid_path, duration_s=10.0,
                              persistence="medium",
                              scroll_columns=[2, 0, 0])
        self.assertEqual(scene._voice_render_modes,
                         ["scroll", "echo", "echo"])

    # ---- cycle_style zeroes strips so the new song doesn't ghost ----

    def test_cycle_style_clears_persistent_state(self):
        """SHIFT-cycle must drop echo history + scroll buffers so the
        previous subtune doesn't ghost-merge into the new one. Verified
        against both modes in a single scene by setting per-voice modes
        explicitly via scroll_columns."""
        from c64cast.waveform import WaveformScene
        api = FakeAPI()
        scene = WaveformScene(api, audio=None,
                              file=self.sid_path, duration_s=10.0,
                              persistence="long",
                              scroll_columns=[4, 0, 0])
        scene.setup()
        try:
            assert (scene._strips is not None
                    and scene._echo_history is not None
                    and scene._last_y is not None)
            scroll_strip = scene._strips[0]
            assert scroll_strip is not None
            # Pre-populate to verify cleanup.
            scroll_strip.fill(True)
            scene._last_y[0] = 42
            for v_idx in (1, 2):
                scene._echo_history[v_idx].append(
                    np.ones((56, 320), dtype=bool))
            label = scene.cycle_style(api)
            self.assertIsNotNone(label, "multi-song SID must cycle")
            self.assertFalse(scroll_strip.any(),
                             "scroll-voice strip must be cleared")
            self.assertIsNone(scene._last_y[0],
                              "last_y must be reset to None")
            for v_idx in (1, 2):
                self.assertEqual(len(scene._echo_history[v_idx]), 0,
                                 f"voice {v_idx} echo history must be empty")
        finally:
            scene.teardown()


# ---------------------------------------------------------------------------
# Config validation for the new knobs
# ---------------------------------------------------------------------------


class WaveformConfigValidationTest(unittest.TestCase):
    """End-to-end validation through validate_scene_cfg — verifies that
    bad TOML values surface at config-load time rather than as runtime
    errors deep in the render loop."""

    def setUp(self):
        self.sid_path = _write_sid_to_tempfile()

    def tearDown(self):
        os.unlink(self.sid_path)

    def _validate(self, **kwargs):
        from c64cast.config import Config, SceneCfg, validate_scene_cfg
        s = SceneCfg(type="waveform", file=self.sid_path, **kwargs)
        validate_scene_cfg(s, Config(), audio_enabled=False)

    def test_validate_accepts_defaults(self):
        self._validate()

    def test_validate_rejects_bad_time_base(self):
        with self.assertRaisesRegex(ValueError, "time_base"):
            self._validate(time_base="weird")

    def test_validate_rejects_zero_auto_cycles(self):
        with self.assertRaisesRegex(ValueError, "auto_cycles"):
            self._validate(auto_cycles=0.0)

    def test_validate_rejects_bad_persistence(self):
        with self.assertRaisesRegex(ValueError, "persistence"):
            self._validate(persistence="huge")

    def test_validate_rejects_short_scroll_list(self):
        with self.assertRaisesRegex(ValueError, "scroll_columns"):
            self._validate(scroll_columns=[1, 2])

    def test_validate_rejects_negative_scroll(self):
        with self.assertRaisesRegex(ValueError, "scroll_columns"):
            self._validate(scroll_columns=-1)


def _make_playable_sid(init_addr, play_addr, payload, load_addr=0x1000,
                       num_songs=1):
    """Minimal PSID v2 with real init/play addresses + a payload — enough
    for parse_psid_for_player + SidHostEmu to run INIT/PLAY."""
    h = bytearray(124)
    h[0:4] = b"PSID"
    h[4:6] = (2).to_bytes(2, "big")        # version
    h[6:8] = (0x7C).to_bytes(2, "big")     # data offset
    h[8:10] = load_addr.to_bytes(2, "big")
    h[10:12] = init_addr.to_bytes(2, "big")
    h[12:14] = play_addr.to_bytes(2, "big")
    h[14:16] = num_songs.to_bytes(2, "big")
    h[16:18] = (1).to_bytes(2, "big")      # start_song
    return bytes(h) + bytes(payload)


class WaveformPlayPreflightTest(unittest.TestCase):
    """WaveformScene._load_sid_file rejects tunes whose PLAY spins past the
    host emulator's cycle cap on every pass (the Hollywood Poker Pro hang)."""

    def _scene(self):
        from c64cast.waveform import WaveformScene
        s = WaveformScene.__new__(WaveformScene)
        s._song_arg = 0
        return s

    def _write(self, sid_bytes):
        fd, path = tempfile.mkstemp(suffix=".sid")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(sid_bytes)
        self.addCleanup(os.remove, path)
        return path

    def test_rejects_spinning_play(self):
        # init=$1000 RTS; play=$1001 JMP $1001 (infinite) → caps every tick.
        sid = _make_playable_sid(
            init_addr=0x1000, play_addr=0x1001,
            payload=[0x60, 0x4C, 0x01, 0x10])
        path = self._write(sid)
        with self.assertRaisesRegex(ValueError, "PLAY never completes"):
            self._scene()._load_sid_file(path)

    def test_accepts_returning_play(self):
        # init=$1000 RTS; play=$1001 RTS → returns immediately, never caps.
        sid = _make_playable_sid(
            init_addr=0x1000, play_addr=0x1001, payload=[0x60, 0x60])
        path = self._write(sid)
        s = self._scene()
        s._load_sid_file(path)  # must not raise
        self.assertEqual(s._sid_file, path)
        self.assertIsNotNone(s._host_emu)


if __name__ == "__main__":
    unittest.main()
