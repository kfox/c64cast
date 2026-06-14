"""Host-side unit tests for MidiScene (c64cast/midi_scene.py).

These exercise the pure logic — note→frequency math, voice allocation /
stealing, CC + pitch-wheel mapping, the SID register shadow + emulator feed,
the bitmap-oscilloscope info rows, config validation, and teardown — against
the shared FakeAPI. No MIDI hardware and no U64 are touched: `_open_port` is
patched out, and real `mido.Message` objects drive `_handle_msg` so
message-attribute access is covered.

Real-hardware behavior (sound out of the SID, the live oscilloscope on the
HDMI scaler) is covered separately by the Tier-2 smoke script, not here.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

# Typed as Any (mirroring midi_scene's own import guard) so pyright doesn't
# flag mido.Message access when the `midi` extra is absent (e.g. CI without
# it); HAVE_MIDI gates the tests at runtime.
try:
    import mido as _mido

    mido: Any = _mido
    HAVE_MIDI = True
except ImportError:
    mido = None
    HAVE_MIDI = False

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI  # noqa: E402

from c64cast import midi_scene  # noqa: E402
from c64cast.c64 import SID  # noqa: E402
from c64cast.midi_scene import MidiScene, _note_to_sid_freq  # noqa: E402
from c64cast.modes import DisplayMode  # noqa: E402
from c64cast.sidemu import primary_waveform  # noqa: E402

# Control-register byte index within the 7-byte voice block written by
# _program_voice (freq_lo, freq_hi, pw_lo, pw_hi, CONTROL, ad, sr).
_CTRL_IDX = 4

# Bitmap layout (bank 0): screen matrix at $0400, hires bitmap at $2000;
# 320 bytes per cell-row. The two info rows live at cell rows 22 (title) and
# 23 (meta); voice strips start at cell rows 0 / 7 / 14.
_SCREEN_BASE = 0x0400
_BITMAP_BASE = 0x2000
_TITLE_BITMAP = _BITMAP_BASE + 22 * 320
_TITLE_SCREEN = _SCREEN_BASE + 22 * 40
_META_BITMAP = _BITMAP_BASE + 23 * 320


def _bring_up_display(scene) -> None:
    """Run the bitmap bring-up pieces a full setup() would (VIC bank + charset
    + render buffers) without opening a MIDI port or starting threads."""
    scene._apply_vic_hires_bank()
    scene._alloc_scope_buffers()


def _make_scene(**kwargs) -> tuple[MidiScene, FakeAPI]:
    api = FakeAPI()
    scene = MidiScene(api, None, **kwargs)
    return scene, api


class _SpyMode:
    """Counts setup/teardown calls so we can assert MidiScene.teardown
    delegates to the base class (which calls display_mode.teardown) exactly
    once — a regression guard for the duplicated super().teardown() bug."""

    def __init__(self) -> None:
        self.setup_calls = 0
        self.teardown_calls = 0

    def setup(self, api) -> None:
        self.setup_calls += 1

    def teardown(self, api) -> None:
        self.teardown_calls += 1


class _FakePort:
    """Minimal mido-input stand-in: never yields a message, closes cleanly."""

    def __init__(self) -> None:
        self.closed = False

    def iter_pending(self):
        return iter(())

    def close(self) -> None:
        self.closed = True


class _ScriptedPort:
    """Yields one queued batch of messages on the first iter_pending()
    call (mirroring a controller flood arriving at once), then nothing."""

    def __init__(self, batch) -> None:
        self._batch = batch
        self._done = False
        self.closed = False

    def iter_pending(self):
        if self._done:
            return iter(())
        self._done = True
        return iter(self._batch)

    def close(self) -> None:
        self.closed = True


@unittest.skipUnless(HAVE_MIDI, "mido not installed (midi extra)")
class _MidiTestCase(unittest.TestCase):
    """Base for all MidiScene tests; skipped wholesale when mido is
    unavailable (the scene + these tests both require the midi extra)."""


class NoteFreqTests(_MidiTestCase):
    def test_a4_reference_pitch(self):
        # A-4 (MIDI 69) = 440 Hz; reg = 440 * 2^24 / clock.
        self.assertEqual(_note_to_sid_freq(69, "NTSC"), 7217)
        self.assertEqual(_note_to_sid_freq(69, "PAL"), 7492)

    def test_clamps_to_16_bit(self):
        # Very high notes saturate the 16-bit frequency register.
        self.assertEqual(_note_to_sid_freq(127, "NTSC"), 0xFFFF)
        # And never go negative.
        self.assertGreaterEqual(_note_to_sid_freq(0, "NTSC"), 0)

    def test_unknown_system_falls_back_to_pal_clock(self):
        # cpu_clock() treats anything that isn't "NTSC" as PAL.
        self.assertEqual(_note_to_sid_freq(69, "bogus"), _note_to_sid_freq(69, "PAL"))


class VoiceAllocationTests(_MidiTestCase):
    def test_three_notes_use_three_distinct_voices(self):
        scene, _ = _make_scene()
        scene._note_on(60, 100)
        scene._note_on(64, 100)
        scene._note_on(67, 100)
        notes = [v.note for v in scene.voices]
        self.assertEqual(notes, [60, 64, 67])
        self.assertTrue(all(v.on for v in scene.voices))

    def test_fourth_note_steals_newest_keeps_pad(self):
        scene, _ = _make_scene()
        for n in (60, 64, 67):
            scene._note_on(n, 100)  # v0=60, v1=64, v2=67
        scene._note_on(72, 100)
        # The most-recently-started voice (v2=67) is stolen; the older pad
        # (60, 64) survives. 67 is suspended (still held).
        self.assertEqual({v.note for v in scene.voices if v.on}, {60, 64, 72})
        self.assertEqual([v.note for v in scene.voices], [60, 64, 72])
        self.assertEqual(scene._held, [60, 64, 67, 72])

    def test_suspended_note_resurfaces_on_release(self):
        # Hold a 3-note chord, tap a 4th (steals the newest = 67), release the
        # 4th — the suspended 67 must come back (no voice left silent).
        scene, _ = _make_scene()
        for n in (60, 64, 67):
            scene._note_on(n, 100)
        scene._note_on(72, 100)  # suspends 67 (newest voice)
        self.assertEqual({v.note for v in scene.voices if v.on}, {60, 64, 72})
        scene._note_off(72)  # 67 resurfaces
        self.assertEqual({v.note for v in scene.voices if v.on}, {60, 64, 67})
        self.assertTrue(all(v.on for v in scene.voices))

    def test_pad_plus_legato_melody_keeps_pad_voices_stable(self):
        # Regression for the reported glitch: hold a low F + C (pad), then play
        # an overlapping (legato) high F→G line repeatedly. The two pad voices
        # must NOT move/restart — only the third voice cycles.
        scene, _ = _make_scene()
        scene._note_on(41, 100)  # low F  -> v0
        scene._note_on(48, 100)  # C      -> v1
        for _ in range(3):
            scene._note_on(65, 100)  # high F -> v2
            scene._note_on(67, 100)  # G overlaps F -> steals v2 (F)
            scene._note_off(65)  # F lifts (already suspended)
            scene._note_off(67)  # G lifts -> v2 idle
            # The pad never moved off v0/v1 and stayed gated the whole time.
            self.assertEqual(scene.voices[0].note, 41)
            self.assertEqual(scene.voices[1].note, 48)
            self.assertTrue(scene.voices[0].on and scene.voices[1].on)

    def test_hold_chord_play_melody_over_it(self):
        # Hold two notes, play single melody notes on top: the held pair stays,
        # the melody note takes the third voice and frees it on release.
        scene, _ = _make_scene()
        scene._note_on(48, 100)  # held bass
        scene._note_on(55, 100)  # held fifth
        for melody in (60, 62, 64):
            scene._note_on(melody, 100)
            self.assertEqual({v.note for v in scene.voices if v.on}, {48, 55, melody})
            scene._note_off(melody)
            self.assertEqual({v.note for v in scene.voices if v.on}, {48, 55})

    def test_releasing_suspended_note_is_silent_change(self):
        scene, api = _make_scene()
        for n in (60, 62, 64, 67):  # 67 steals v2(64); 64 suspended
            scene._note_on(n, 100)
        sounding = {v.note for v in scene.voices if v.on}
        self.assertNotIn(64, sounding)
        api.regs.clear()
        scene._note_off(64)  # release the suspended (silent) note
        self.assertEqual({v.note for v in scene.voices if v.on}, sounding)
        self.assertNotIn(64, scene._held)
        self.assertEqual(api.regs, {})  # no SID writes — it wasn't sounding

    def test_repeated_note_reuses_same_voice_and_retriggers(self):
        scene, _ = _make_scene()
        scene._note_on(60, 100)
        scene._note_on(60, 110)
        # Still only voice 0 in use; velocity updated (re-press re-triggers it).
        self.assertEqual(scene.voices[0].note, 60)
        self.assertEqual(scene.voices[0].velocity, 110)
        self.assertFalse(scene.voices[1].on)
        self.assertFalse(scene.voices[2].on)

    def test_note_off_ungates_and_clears_gate_bit(self):
        scene, api = _make_scene(waveform="pulse")
        scene._note_on(60, 100)
        ctrl_on = api.regs["D400"][_CTRL_IDX]
        self.assertEqual(ctrl_on, SID.WAVE_PULSE | SID.GATE)

        scene._note_off(60)
        self.assertFalse(scene.voices[0].on)
        ctrl_off = api.regs["D400"][_CTRL_IDX]
        self.assertEqual(ctrl_off, SID.WAVE_PULSE)  # gate bit cleared

    def test_note_off_for_unheld_note_is_noop(self):
        scene, _ = _make_scene()
        scene._note_on(60, 100)
        scene._note_off(72)  # never pressed
        self.assertTrue(scene.voices[0].on)


class HardRestartTests(_MidiTestCase):
    """The real SID re-attacks only on a gate 0→1 edge, so re-using an
    already-gated voice (re-press, steal, trill) must emit a gate-off first —
    otherwise the chip changes pitch but never re-triggers (silent note while
    the host-emulator waveform still moves)."""

    def _gate_off_op(self, scene, voice_idx):
        addr = f"{SID.voice_base(voice_idx) + SID.OFF_CONTROL:04X}"
        return ("write_memory", addr, f"{scene.waveform_bits:02X}")

    def test_re_press_emits_gate_off_edge_before_block(self):
        scene, api = _make_scene(waveform="pulse")
        scene._note_on(60, 100)  # fresh attack on idle v0 (gate 0→1)
        api.ops.clear()
        scene._note_on(60, 110)  # re-press same note → hard restart
        gate_off = self._gate_off_op(scene, 0)
        self.assertIn(gate_off, api.ops)
        block = next(i for i, o in enumerate(api.ops) if o[0] == "write_regs" and o[1] == "D400")
        self.assertLess(api.ops.index(gate_off), block)  # edge precedes block

    def test_steal_emits_gate_off_on_stolen_voice(self):
        scene, api = _make_scene(waveform="pulse")
        for n in (60, 64, 67):  # fills v0,v1,v2 (67 newest → v2)
            scene._note_on(n, 100)
        api.ops.clear()
        scene._note_on(72, 100)  # steals newest voice v2
        self.assertIn(self._gate_off_op(scene, 2), api.ops)

    def test_fresh_note_on_idle_voice_no_gate_off(self):
        scene, api = _make_scene(waveform="pulse")
        scene._note_on(60, 100)  # idle voice → single gate 0→1, no edge fix
        self.assertNotIn(self._gate_off_op(scene, 0), api.ops)


class ProgramVoiceTests(_MidiTestCase):
    def test_note_on_writes_full_voice_block(self):
        scene, api = _make_scene(waveform="sawtooth", pulse_width=2048, adsr=(0, 8, 12, 8))
        scene._note_on(60, 100)
        regs = api.regs["D400"]
        expected_freq = _note_to_sid_freq(60, "NTSC")
        self.assertEqual(
            regs,
            (
                expected_freq & 0xFF,
                (expected_freq >> 8) & 0xFF,
                2048 & 0xFF,
                (2048 >> 8) & 0x0F,
                SID.WAVE_SAWTOOTH | SID.GATE,
                (0 << 4) | 8,
                (12 << 4) | 8,
            ),
        )

    def test_voices_write_to_distinct_bases(self):
        scene, api = _make_scene()
        scene._note_on(60, 100)
        scene._note_on(64, 100)
        scene._note_on(67, 100)
        self.assertIn("D400", api.regs)  # voice 0
        self.assertIn("D407", api.regs)  # voice 1
        self.assertIn("D40E", api.regs)  # voice 2


class ControlChangeTests(_MidiTestCase):
    def test_cc7_sets_master_volume(self):
        scene, api = _make_scene()  # default filter_mode = lowpass (1)
        scene._control_change(7, 127)
        self.assertEqual(scene.master_volume, 15)
        # $D418 = (filter mode << 4) | volume — CC7 preserves the mode nibble.
        self.assertEqual(api.memories["D418"], f"{(0x1 << 4) | 15:02X}")

    def test_cc1_modwheel_sweeps_pulse_width_on_all_voices(self):
        scene, api = _make_scene()
        scene._control_change(1, 127)
        self.assertEqual(scene.pulse_width, midi_scene._PW_MAX_AUDIBLE)
        pw = scene.pulse_width
        for base in ("D402", "D409", "D410"):  # base + OFF_PW_LO
            self.assertEqual(api.regs[base], (pw & 0xFF, (pw >> 8) & 0x0F))

    def test_cc1_modwheel_to_zero_stays_audible(self):
        # Regression: wheel-to-zero used to set pulse width to 0, which is
        # a silent 0%-duty pulse. It must now floor to an audible window.
        scene, _ = _make_scene()
        scene._control_change(1, 0)
        self.assertEqual(scene.pulse_width, midi_scene._PW_MIN_AUDIBLE)
        self.assertGreater(scene.pulse_width, 0)

    def test_cc1_modwheel_midpoint_near_square(self):
        scene, _ = _make_scene()
        scene._control_change(1, 64)
        # Mid-wheel should land near a 50% duty cycle (~2048).
        self.assertAlmostEqual(scene.pulse_width, 2048, delta=128)

    def test_cc74_sets_filter_cutoff(self):
        scene, api = _make_scene()
        scene._control_change(74, 127)
        expected_fc = (127 << 4) & 0x07FF
        self.assertEqual(scene.filter_cutoff, expected_fc)
        self.assertEqual(api.regs["D415"], (expected_fc & 0x07, (expected_fc >> 3) & 0xFF))

    def test_unmapped_cc_is_ignored(self):
        scene, api = _make_scene()
        before = dict(api.memories)
        scene._control_change(99, 64)
        self.assertEqual(api.memories, before)

    def test_cc71_sets_resonance_and_routes_filter(self):
        scene, api = _make_scene()
        scene._control_change(midi_scene._CC_RESONANCE, 127)
        self.assertEqual(scene.filter_resonance, 15)
        # $D417 = (resonance << 4) | 0x07 (all 3 voices routed to the filter).
        self.assertEqual(api.memories["D417"], f"{(15 << 4) | 0x07:02X}")

    def test_cc73_72_75_set_adsr(self):
        scene, api = _make_scene(adsr=(0, 8, 12, 8))
        scene._control_change(midi_scene._CC_ATTACK, 127)  # attack → 15
        scene._control_change(midi_scene._CC_DECAY, 0)  # decay → 0
        scene._control_change(midi_scene._CC_RELEASE, 64)  # release → 8
        self.assertEqual(scene.adsr[0], 15)
        self.assertEqual(scene.adsr[1], 0)
        self.assertEqual(scene.adsr[3], 8)
        # AD byte for voice 0 = (attack << 4) | decay.
        self.assertEqual(api.regs["D405"][0], (15 << 4) | 0)


class VelocityTests(_MidiTestCase):
    def test_velocity_drives_sustain_nibble(self):
        # Velocity → sustain (loudness): SR high nibble = velocity >> 3.
        scene, api = _make_scene(adsr=(0, 8, 12, 8))
        scene._note_on(60, 40)  # 40 >> 3 = 5
        sr = api.regs["D400"][6]
        self.assertEqual((sr >> 4) & 0xF, 5)
        self.assertEqual(sr & 0xF, 8)  # release unchanged
        scene._note_on(64, 120)  # 120 >> 3 = 15 (voice 1)
        self.assertEqual((api.regs["D407"][6] >> 4) & 0xF, 15)

    def test_release_keeps_release_nibble(self):
        scene, api = _make_scene(adsr=(0, 8, 12, 9))
        scene._note_on(60, 100)
        scene._note_off(60)
        # note_off clears the gate; release nibble (9) stays.
        ctrl = api.regs["D400"][4]
        self.assertEqual(ctrl & SID.GATE, 0)
        self.assertEqual(api.regs["D400"][6] & 0xF, 9)


class WaveformCycleTests(_MidiTestCase):
    def test_shift_cycles_waveform(self):
        scene, _ = _make_scene(waveform="pulse")
        self.assertEqual(scene.cycle_style(scene.api), "waveform=sawtooth")
        self.assertEqual(scene.waveform, "sawtooth")
        self.assertEqual(scene.waveform_bits, SID.WAVE_SAWTOOTH)
        # wraps pulse→saw→tri→noise→pulse
        scene.cycle_style(scene.api)  # triangle
        scene.cycle_style(scene.api)  # noise
        label = scene.cycle_style(scene.api)
        self.assertEqual(scene.waveform, "pulse")
        self.assertEqual(label, "waveform=pulse")

    def test_cycle_reprograms_held_voice_with_new_waveform(self):
        scene, api = _make_scene(waveform="pulse")
        scene._note_on(60, 100)
        scene.cycle_style(scene.api)  # → sawtooth
        ctrl = api.regs["D400"][4]
        # Held voice keeps its gate but switches to the new waveform.
        self.assertEqual(ctrl, SID.WAVE_SAWTOOTH | SID.GATE)


class PitchWheelTests(_MidiTestCase):
    def test_pitch_bend_re_emits_frequency_for_gated_voices(self):
        scene, api = _make_scene()
        scene._note_on(60, 100)
        scene._pitchwheel(8192)  # full up = +2 semitones
        bent_freq = _note_to_sid_freq(62, "NTSC")
        self.assertEqual(api.regs["D400"], (bent_freq & 0xFF, (bent_freq >> 8) & 0xFF))

    def test_pitch_bend_skips_released_voices(self):
        scene, api = _make_scene()
        scene._note_on(60, 100)
        scene._note_off(60)
        api.regs.pop("D400", None)
        scene._pitchwheel(8192)
        # Voice 0 is released → no frequency re-emit.
        self.assertNotIn("D400", api.regs)


class HandleMsgDispatchTests(_MidiTestCase):
    def test_note_on_then_off_via_messages(self):
        scene, _ = _make_scene()
        scene._handle_msg(mido.Message("note_on", note=60, velocity=100))
        self.assertTrue(scene.voices[0].on)
        scene._handle_msg(mido.Message("note_off", note=60, velocity=0))
        self.assertFalse(scene.voices[0].on)

    def test_note_on_velocity_zero_is_treated_as_note_off(self):
        scene, _ = _make_scene()
        scene._handle_msg(mido.Message("note_on", note=60, velocity=100))
        scene._handle_msg(mido.Message("note_on", note=60, velocity=0))
        self.assertFalse(scene.voices[0].on)

    def test_control_change_message_routes(self):
        scene, api = _make_scene()
        scene._handle_msg(mido.Message("control_change", control=7, value=0))
        # vol 0 + lowpass mode (1) → high nibble 1, low nibble 0.
        self.assertEqual(api.memories["D418"], f"{(0x1 << 4) | 0:02X}")


class ShadowEmulatorTests(_MidiTestCase):
    """The 25-byte $D400-$D418 register shadow + the host SIDEmulator it
    feeds — the data source for the oscilloscope (no py65 host emu)."""

    def test_note_on_updates_shadow_and_gates_emulator_voice(self):
        scene, _ = _make_scene(waveform="pulse")
        scene._note_on(60, 100)
        # Shadow voice 0 control byte = pulse waveform + gate.
        ctrl = scene._sid_shadow[0 * SID.BYTES_PER_VOICE + SID.OFF_CONTROL]
        self.assertEqual(ctrl, SID.WAVE_PULSE | SID.GATE)
        # Emulator mirrors it: voice 0 gated, freq matches the note, and the
        # gate edge put it into the attack phase.
        v = scene.emulator.voices[0]
        self.assertTrue(v.gated())
        self.assertEqual(v.freq, _note_to_sid_freq(60, scene.system))
        self.assertEqual(v.envelope_state, "attack")

    def test_note_off_releases_emulator_voice(self):
        scene, _ = _make_scene()
        scene._note_on(60, 100)
        scene._note_off(60)
        ctrl = scene._sid_shadow[0 * SID.BYTES_PER_VOICE + SID.OFF_CONTROL]
        self.assertEqual(ctrl & SID.GATE, 0)
        self.assertEqual(scene.emulator.voices[0].envelope_state, "release")

    def test_retrigger_reattacks_already_gated_voice(self):
        # Re-gating a still-gated voice (re-trigger / voice steal) shows no
        # off→on edge to update_registers, so MidiScene must flag a hard
        # re-attack — otherwise a plucked voice would flatline after decay.
        scene, _ = _make_scene()
        scene._note_on(60, 100)
        v = scene.emulator.voices[0]
        # Force it down into release-ish state then re-trigger the SAME voice
        # with a new note while it is still gated.
        v.envelope_level = 0.0
        v.envelope_state = "sustain"
        scene._note_on(60, 110)  # reuses voice 0 (note already playing)
        self.assertEqual(v.envelope_state, "attack")
        self.assertEqual(v.envelope_level, 0.0)

    def test_envelope_ticker_advances_held_note(self):
        scene, _ = _make_scene(adsr=(8, 8, 12, 8))  # slow-ish attack
        scene._note_on(60, 100)
        v = scene.emulator.voices[0]
        start = v.envelope_level
        scene._tick_envelopes()
        scene._tick_envelopes()
        self.assertGreater(v.envelope_level, start)


class PaintTests(_MidiTestCase):
    def test_process_frame_writes_voice_strips_and_info_rows(self):
        scene, api = _make_scene(waveform="pulse", master_volume=15)
        _bring_up_display(scene)
        scene._note_on(60, 100)
        self.assertTrue(scene.process_frame(0.0))
        # All three voice bitmap strips were drawn (cell rows 0 / 7 / 14).
        for cell_row in (0, 7, 14):
            self.assertIn(_BITMAP_BASE + cell_row * 320, api.regions)
        # Both info rows painted: bitmap glyphs + screen-RAM FG nibble.
        self.assertIn(_TITLE_BITMAP, api.regions)
        self.assertIn(_TITLE_SCREEN, api.regions)
        self.assertIn(_META_BITMAP, api.regions)
        # Title = global state; controller row = live CC values (no per-voice
        # note text — the colored/gray strips convey activity).
        title = scene._build_title_line()
        self.assertIn("MIDI PULSE", title)
        self.assertIn("VOL 15", title)
        ctl = scene._build_controller_line()
        self.assertIn("PW", ctl)
        self.assertIn("CUT", ctl)

    def test_voice_strip_grays_when_idle_colors_when_sounding(self):
        # A sounding voice paints its color; once released + decayed it repaints
        # gray. Change-detected via _voice_sounding.
        from c64cast.palette import C64_COLORS

        scene, api = _make_scene(voice_colors=["light green", "cyan", "yellow"])
        _bring_up_display(scene)
        scene._voice_sounding = [False, False, False]
        scene._note_on(60, 100)
        api.regions.clear()
        scene.process_frame(0.0)
        green = C64_COLORS["light green"]
        self.assertEqual(api.regions[_SCREEN_BASE], bytes([green << 4]) * 280)
        self.assertTrue(scene._voice_sounding[0])
        # Force the voice fully idle (released + envelope decayed) and re-render.
        scene._note_off(60)
        scene.emulator.voices[0].envelope_level = 0.0
        api.regions.clear()
        scene.process_frame(1.0)
        gray = C64_COLORS[midi_scene._IDLE_GRAY]
        self.assertEqual(api.regions[_SCREEN_BASE], bytes([gray << 4]) * 280)
        self.assertFalse(scene._voice_sounding[0])

    def test_controller_line_reflects_cc_state(self):
        scene, _ = _make_scene()
        scene._control_change(midi_scene._CC_CUTOFF, 64)
        scene._control_change(midi_scene._CC_RESONANCE, 127)
        ctl = scene._build_controller_line()
        self.assertIn(f"RES {scene.filter_resonance:2d}", ctl)
        self.assertEqual(len(ctl), 40)  # _paint_text_row needs exactly 40

    def test_info_rows_repaint_only_when_dirty(self):
        # The scope strips redraw every frame, but the change-detected text
        # rows repaint only on note/CC events — keeps DMA low.
        scene, api = _make_scene()
        _bring_up_display(scene)
        scene._note_on(60, 100)
        scene.process_frame(0.0)
        api.regions.clear()
        scene.process_frame(1.0)  # _dirty now False
        # Info rows NOT rewritten...
        self.assertNotIn(_TITLE_BITMAP, api.regions)
        self.assertNotIn(_TITLE_SCREEN, api.regions)
        self.assertNotIn(_META_BITMAP, api.regions)
        # ...but the scope strips are redrawn.
        self.assertIn(_BITMAP_BASE, api.regions)

    def test_default_knobs_take_fast_render_path(self):
        scene, _ = _make_scene()
        self.assertEqual(scene._voice_render_modes, ["fast", "fast", "fast"])
        self.assertTrue(scene._fast_path)

    def test_per_waveform_color_repaints_on_transition(self):
        scene, api = _make_scene(color_mode="per_waveform", waveform="pulse")
        _bring_up_display(scene)
        scene._note_on(60, 100)
        api.regions.clear()
        scene.process_frame(0.0)
        # Voice 0 went from "off" (-1) to pulse → its color cells repaint.
        self.assertEqual(scene._last_voice_wave[0], primary_waveform(SID.WAVE_PULSE | SID.GATE))
        self.assertIn(_SCREEN_BASE, api.regions)  # voice-0 FG nibble block


class ValidationTests(_MidiTestCase):
    def test_bad_waveform_rejected(self):
        with self.assertRaises(ValueError):
            _make_scene(waveform="square")

    def test_bad_adsr_rejected(self):
        with self.assertRaises(ValueError):
            _make_scene(adsr=(0, 8, 12))  # wrong length
        with self.assertRaises(ValueError):
            _make_scene(adsr=(0, 8, 12, 16))  # out of 0..15

    def test_out_of_range_scalars_rejected(self):
        with self.assertRaises(ValueError):
            _make_scene(pulse_width=5000)
        with self.assertRaises(ValueError):
            _make_scene(filter_cutoff=3000)
        with self.assertRaises(ValueError):
            _make_scene(master_volume=16)

    def test_missing_midi_extra_raises(self):
        with mock.patch.object(midi_scene, "MIDI_AVAILABLE", False):
            with self.assertRaises(RuntimeError):
                _make_scene()

    def test_short_voice_colors_padded_to_three(self):
        scene, _ = _make_scene(voice_colors=["white"])
        self.assertEqual(len(scene.voice_color_names), SID.N_VOICES)

    def test_bad_scope_knobs_rejected(self):
        with self.assertRaises(ValueError):
            _make_scene(color_mode="rainbow")
        with self.assertRaises(ValueError):
            _make_scene(time_base="bogus")
        with self.assertRaises(ValueError):
            _make_scene(persistence="weird")
        with self.assertRaises(ValueError):
            _make_scene(scroll_columns=[1, 2])  # wrong length


class PortSelectionTests(_MidiTestCase):
    """_open_port name resolution (substring match / first-port / errors),
    exercised with mido patched so no real MIDI hardware is touched."""

    def _patch_mido(self, scene, names, opened):
        fake = mock.MagicMock()
        fake.get_input_names.return_value = names
        fake.open_input.side_effect = lambda n: opened.append(n) or _FakePort()
        return mock.patch.object(midi_scene, "mido", fake)

    def test_empty_port_picks_first(self):
        scene, _ = _make_scene(port="")
        opened: list[str] = []
        with self._patch_mido(scene, ["Port A", "Port B"], opened):
            scene._open_port()
        self.assertEqual(opened, ["Port A"])

    def test_no_ports_raises(self):
        scene, _ = _make_scene(port="")
        with self._patch_mido(scene, [], []):
            with self.assertRaises(RuntimeError):
                scene._open_port()

    def test_substring_match(self):
        scene, _ = _make_scene(port="keylab")
        opened: list[str] = []
        with self._patch_mido(scene, ["IAC Bus 1", "KeyLab mkII 49"], opened):
            scene._open_port()
        self.assertEqual(opened, ["KeyLab mkII 49"])

    def test_no_match_raises(self):
        scene, _ = _make_scene(port="nonexistent")
        with self._patch_mido(scene, ["IAC Bus 1"], []):
            with self.assertRaises(RuntimeError):
                scene._open_port()


class ReaderCoalescingTests(_MidiTestCase):
    """The reader must collapse a continuous-controller flood (wheel sweep)
    into a handful of SID writes instead of one-per-message, so normal
    controller use can't burst the DMA socket."""

    def _drain(self, scene, port) -> None:
        scene._midi_port = port
        scene._stop.clear()
        t = threading.Thread(target=scene._reader, daemon=True)
        t.start()
        # Wait until the batch has been drained + flushed at least once.
        deadline = time.time() + 1.0
        while time.time() < deadline and not any(
            op[0] == "write_regs" and op[1] == "D402" for op in scene.api.ops
        ):
            time.sleep(0.005)
        scene._stop.set()
        t.join(timeout=1.0)

    def test_modwheel_flood_coalesced_to_few_writes(self):
        scene, api = _make_scene()
        # 256 mod-wheel messages (a fast sweep up and back down).
        batch = [
            mido.Message("control_change", control=1, value=v)
            for v in list(range(128)) + list(range(127, -1, -1))
        ]
        self._drain(scene, _ScriptedPort(batch))
        pw_writes = [op for op in api.ops if op[0] == "write_regs" and op[1] == "D402"]
        # 256 messages must NOT become 256 writes.
        self.assertGreaterEqual(len(pw_writes), 1)
        self.assertLessEqual(len(pw_writes), 5)

    def test_note_floods_are_still_applied_individually(self):
        # Notes are discrete musical events — they are NOT coalesced.
        scene, api = _make_scene()
        batch = []
        for _ in range(8):
            batch.append(mido.Message("note_on", note=60, velocity=100))
            batch.append(mido.Message("note_off", note=60))
        scene._midi_port = _ScriptedPort(batch)
        scene._stop.clear()
        t = threading.Thread(target=scene._reader, daemon=True)
        t.start()
        time.sleep(0.1)
        scene._stop.set()
        t.join(timeout=1.0)
        voice_writes = [op for op in api.ops if op[0] == "write_regs" and op[1] == "D400"]
        # Each on/off reprograms voice 0 → ~16 writes, not collapsed to one.
        self.assertGreaterEqual(len(voice_writes), 16)


class LifecycleTests(_MidiTestCase):
    def test_status_repaint_rate_is_capped(self):
        # The text status block is rate-capped below the 60 fps system
        # default so per-note screen pushes don't burst the DMA socket.
        scene, _ = _make_scene()
        fps = scene.target_fps
        assert fps is not None
        self.assertLessEqual(fps, 30.0)

    def test_teardown_delegates_to_base_exactly_once(self):
        # Regression for the duplicated super().teardown() call. The base
        # Scene.teardown is the only thing that touches display_mode, so
        # counting its teardown invocations proves the chain runs once.
        scene, api = _make_scene()
        spy = _SpyMode()
        scene.display_mode = cast(DisplayMode, spy)
        scene.teardown()
        self.assertEqual(spy.teardown_calls, 1)
        # SID is silenced on the way out so the next scene starts clean.
        self.assertIn("SILENCE", api.regs)

    def test_setup_programs_sid_and_starts_reader(self):
        scene, api = _make_scene(filter_mode="lowpass", master_volume=15)
        # Avoid touching real MIDI hardware: install a stub port.
        with mock.patch.object(
            scene,
            "_open_port",
            side_effect=lambda: setattr(scene, "_midi_port", _FakePort()),
        ):
            scene.setup()
        try:
            # Global SID program: all 3 voices routed to the filter (low 3
            # bits of $D417) so the cutoff/resonance CCs are audible; master
            # vol + lowpass mode in $D418.
            self.assertEqual(api.memories["D417"], "07")
            self.assertEqual(api.memories["D418"], f"{(0x1 << 4) | 15:02X}")
            # Per-voice pre-program ran (pw + waveform, gate off) for all 3.
            for base in ("D402", "D409", "D410"):
                self.assertIn(base, api.regs)
            reader = scene._reader_thread
            assert reader is not None
            self.assertTrue(reader.is_alive())
        finally:
            scene.teardown()
        self.assertFalse(scene._reader_thread is not None and scene._reader_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
