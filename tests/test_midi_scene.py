"""Host-side unit tests for MidiScene (c64cast/midi_scene.py).

These exercise the pure logic — note→frequency math, voice allocation /
stealing, CC + pitch-wheel mapping, the status-block paint, config
validation, and teardown — against the shared FakeAPI. No MIDI hardware
and no U64 are touched: `_open_port` is patched out, and real `mido.Message`
objects drive `_handle_msg` so message-attribute access is covered.

Real-hardware behavior (sound out of the SID, the PETSCII display on the
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
from c64cast.overlays import ascii_to_screen  # noqa: E402

# Control-register byte index within the 7-byte voice block written by
# _program_voice (freq_lo, freq_hi, pw_lo, pw_hi, CONTROL, ad, sr).
_CTRL_IDX = 4


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
        self.assertEqual(_note_to_sid_freq(69, "bogus"),
                         _note_to_sid_freq(69, "PAL"))


class VoiceAllocationTests(_MidiTestCase):
    def test_three_notes_use_three_distinct_voices(self):
        scene, _ = _make_scene()
        scene._note_on(60, 100)
        scene._note_on(64, 100)
        scene._note_on(67, 100)
        notes = [v.note for v in scene.voices]
        self.assertEqual(notes, [60, 64, 67])
        self.assertTrue(all(v.on for v in scene.voices))

    def test_fourth_note_steals_oldest_voice(self):
        scene, _ = _make_scene()
        for n in (60, 64, 67):
            scene._note_on(n, 100)
        scene._note_on(72, 100)
        # Voice 0 was gated first (smallest t_changed) → it gets stolen.
        self.assertEqual(scene.voices[0].note, 72)
        self.assertEqual([v.note for v in scene.voices[1:]], [64, 67])

    def test_repeated_note_reuses_same_voice(self):
        scene, _ = _make_scene()
        scene._note_on(60, 100)
        scene._note_on(60, 110)
        # Still only voice 0 is in use.
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


class ProgramVoiceTests(_MidiTestCase):
    def test_note_on_writes_full_voice_block(self):
        scene, api = _make_scene(waveform="sawtooth", pulse_width=2048,
                                 adsr=(0, 8, 12, 8))
        scene._note_on(60, 100)
        regs = api.regs["D400"]
        expected_freq = _note_to_sid_freq(60, "NTSC")
        self.assertEqual(regs, (
            expected_freq & 0xFF,
            (expected_freq >> 8) & 0xFF,
            2048 & 0xFF,
            (2048 >> 8) & 0x0F,
            SID.WAVE_SAWTOOTH | SID.GATE,
            (0 << 4) | 8,
            (12 << 4) | 8,
        ))

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
        scene, api = _make_scene()
        scene._control_change(7, 127)
        self.assertEqual(scene.master_volume, 15)
        self.assertEqual(api.memories["D418"], "0F")

    def test_cc1_modwheel_sweeps_pulse_width_on_all_voices(self):
        scene, api = _make_scene()
        scene._control_change(1, 127)
        self.assertEqual(scene.pulse_width, midi_scene._PW_MAX_AUDIBLE)
        pw = scene.pulse_width
        for base in ("D402", "D409", "D410"):  # base + OFF_PW_LO
            self.assertEqual(api.regs[base],
                             (pw & 0xFF, (pw >> 8) & 0x0F))

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
        self.assertEqual(api.regs["D415"],
                         (expected_fc & 0x07, (expected_fc >> 3) & 0xFF))

    def test_unmapped_cc_is_ignored(self):
        scene, api = _make_scene()
        before = dict(api.memories)
        scene._control_change(99, 64)
        self.assertEqual(api.memories, before)


class PitchWheelTests(_MidiTestCase):
    def test_pitch_bend_re_emits_frequency_for_gated_voices(self):
        scene, api = _make_scene()
        scene._note_on(60, 100)
        scene._pitchwheel(8192)  # full up = +2 semitones
        bent_freq = _note_to_sid_freq(62, "NTSC")
        self.assertEqual(api.regs["D400"],
                         (bent_freq & 0xFF, (bent_freq >> 8) & 0xFF))

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
        self.assertEqual(api.memories["D418"], "00")


class PaintTests(_MidiTestCase):
    def test_process_frame_paints_header_and_voice_row(self):
        scene, api = _make_scene(waveform="pulse")
        scene._note_on(60, 100)
        self.assertTrue(scene.process_frame(0.0))

        screen = api.regions[0x0400]
        self.assertEqual(len(screen), 1000)
        # Header starts at row 0.
        self.assertTrue(screen.startswith(ascii_to_screen("MIDI")))
        # Voice 1 row is row 10, col 2 → offset 402.
        v1 = ascii_to_screen("V1")
        self.assertEqual(screen[402:402 + len(v1)], v1)
        # Color RAM is also written.
        self.assertEqual(len(api.regions[0xD800]), 1000)

    def test_released_voice_clears_note_and_velocity(self):
        # After release the row must not keep showing the last note/vel —
        # it should read "--- off" so the display matches what's heard.
        scene, api = _make_scene()
        scene._note_on(60, 100)
        scene.process_frame(0.0)
        held = api.regions[0x0400]
        self.assertIn(ascii_to_screen("C-4"), held)
        self.assertIn(ascii_to_screen("vel 100"), held)

        scene._note_off(60)
        scene.process_frame(1.0)
        released = api.regions[0x0400]
        # Voice 1 row (row 10, col 2 → offset 402) now reads "V1  --- off".
        self.assertEqual(released[402:402 + len(ascii_to_screen("V1"))],
                         ascii_to_screen("V1"))
        self.assertIn(ascii_to_screen("---"), released)
        # The stale note name and velocity are gone.
        self.assertNotIn(ascii_to_screen("C-4"), released)
        self.assertNotIn(ascii_to_screen("vel 100"), released)

    def test_process_frame_is_skipped_when_not_dirty(self):
        scene, api = _make_scene()
        scene._note_on(60, 100)
        scene.process_frame(0.0)
        api.regions.clear()
        # No new MIDI activity → _dirty is False → no repaint.
        self.assertTrue(scene.process_frame(1.0))
        self.assertEqual(api.regions, {})


class ValidationTests(_MidiTestCase):
    def test_bad_waveform_rejected(self):
        with self.assertRaises(ValueError):
            _make_scene(waveform="square")

    def test_bad_adsr_rejected(self):
        with self.assertRaises(ValueError):
            _make_scene(adsr=(0, 8, 12))          # wrong length
        with self.assertRaises(ValueError):
            _make_scene(adsr=(0, 8, 12, 16))      # out of 0..15

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
        self.assertEqual(len(scene.voice_colors), SID.N_VOICES)


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
                op[0] == "write_regs" and op[1] == "D402" for op in scene.api.ops):
            time.sleep(0.005)
        scene._stop.set()
        t.join(timeout=1.0)

    def test_modwheel_flood_coalesced_to_few_writes(self):
        scene, api = _make_scene()
        # 256 mod-wheel messages (a fast sweep up and back down).
        batch = [mido.Message("control_change", control=1, value=v)
                 for v in list(range(128)) + list(range(127, -1, -1))]
        self._drain(scene, _ScriptedPort(batch))
        pw_writes = [op for op in api.ops
                     if op[0] == "write_regs" and op[1] == "D402"]
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
        voice_writes = [op for op in api.ops
                        if op[0] == "write_regs" and op[1] == "D400"]
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
            scene, "_open_port",
            side_effect=lambda: setattr(scene, "_midi_port", _FakePort()),
        ):
            scene.setup()
        try:
            # Global SID program: filter routing off, master vol + lp mode.
            self.assertEqual(api.memories["D417"], "00")
            self.assertEqual(api.memories["D418"], f"{(0x1 << 4) | 15:02X}")
            # Per-voice pre-program ran (pw + waveform, gate off) for all 3.
            for base in ("D402", "D409", "D410"):
                self.assertIn(base, api.regs)
            reader = scene._reader_thread
            assert reader is not None
            self.assertTrue(reader.is_alive())
        finally:
            scene.teardown()
        self.assertFalse(scene._reader_thread is not None
                         and scene._reader_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
