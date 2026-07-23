"""Host-side unit tests for tempo.py — the Live-performance Phase 1 beat grid.

Everything here is offline and deterministic: the clock feed methods take an
explicit ``now`` timestamp, so tests drive synthetic evenly-spaced pulses / taps
and assert exact ``beat_phase`` / ``bpm`` / ``bar_phase`` — no real time, no
mido, no hardware. The mido bridge (``feed_message``) is exercised through a
tiny duck-typed message shim, and the midi_control integration (a clock stream
updating every playlist's grid, and the ``tempo_tap`` action) through the same
real-``mido``-guarded path the rest of test_midi_control uses.
"""

from __future__ import annotations

import threading
import unittest
from typing import Any
from unittest import mock

try:
    import mido as _mido

    mido: Any = _mido
    HAVE_MIDI = True
except ImportError:
    mido = None
    HAVE_MIDI = False

from c64cast import config as cfgmod
from c64cast.tempo import ClockModulationSource, TempoClock, build_tempo_clock


class _Msg:
    """Minimal duck-typed stand-in for a mido real-time message."""

    __slots__ = ("type", "pos")

    def __init__(self, mtype: str, pos: int = 0) -> None:
        self.type = mtype
        self.pos = pos


def _pulse_dt(bpm: float) -> float:
    """Seconds between 24-PPQN clock pulses at `bpm`."""
    return 60.0 / (bpm * TempoClock.PPQN)


class InternalTempoTest(unittest.TestCase):
    def test_internal_free_runs_at_static_bpm(self):
        c = TempoClock(bpm=120.0, source="internal", now=0.0)
        self.assertTrue(c.running)
        # 120 BPM = 2 beats/sec, integrated over wall clock (no external cap).
        self.assertAlmostEqual(c.beat_phase_at(1.0), 2.0, places=9)
        self.assertAlmostEqual(c.beat_phase_at(2.5), 5.0, places=9)

    def test_bar_phase_uses_beats_per_bar(self):
        c = TempoClock(bpm=120.0, beats_per_bar=4, source="internal", now=0.0)
        # beat_phase(4s) = 8 beats -> 2 bars of 4/4.
        self.assertAlmostEqual(c.bar_phase_at(4.0), 2.0, places=9)
        c3 = TempoClock(bpm=120.0, beats_per_bar=3, source="internal", now=0.0)
        self.assertAlmostEqual(c3.bar_phase_at(3.0), 2.0, places=9)  # 6 beats / 3

    def test_midi_source_idles_until_a_clock_byte(self):
        c = TempoClock(bpm=120.0, source="midi", now=0.0)
        self.assertFalse(c.running)
        # No advance while idle, regardless of elapsed time.
        self.assertEqual(c.beat_phase_at(10.0), 0.0)


class ExternalClockTest(unittest.TestCase):
    def test_beat_phase_advances_one_over_ppqn_per_pulse(self):
        c = TempoClock(bpm=120.0, source="midi", now=0.0)
        dt = _pulse_dt(120.0)
        c.start(0.0)
        # 25 pulses: the first anchors (no advance), the next 24 each add 1/24,
        # so exactly one beat elapses, read at the final pulse instant.
        for k in range(25):
            c.clock_pulse((k + 1) * dt)
        self.assertTrue(c.running)
        self.assertAlmostEqual(c.beat_phase_at(25 * dt), 1.0, places=9)

    def test_bpm_estimate_locks_to_a_steady_clock(self):
        c = TempoClock(bpm=90.0, source="midi", now=0.0)  # wrong starting bpm
        dt = _pulse_dt(174.0)
        for k in range(48):
            c.clock_pulse((k + 1) * dt)
        # Constant inter-pulse interval -> the EMA converges exactly to 174.
        self.assertAlmostEqual(c.bpm, 174.0, places=4)

    def test_extrapolation_capped_between_pulses(self):
        c = TempoClock(bpm=120.0, source="midi", now=0.0)
        dt = _pulse_dt(120.0)
        c.start(0.0)
        c.clock_pulse(dt)  # anchor
        c.clock_pulse(2 * dt)  # phase now 1/24
        base = c.beat_phase_at(2 * dt)
        # Reading far past the last pulse must not run away — capped at +1/24.
        self.assertLessEqual(c.beat_phase_at(2 * dt + 10.0) - base, 1.0 / TempoClock.PPQN + 1e-9)

    def test_free_running_clock_starts_without_a_start_byte(self):
        c = TempoClock(bpm=120.0, source="midi", now=0.0)
        self.assertFalse(c.running)
        c.clock_pulse(0.0)
        self.assertTrue(c.running)


class TransportTest(unittest.TestCase):
    def test_start_rewinds_to_zero(self):
        c = TempoClock(bpm=120.0, source="internal", now=0.0)
        self.assertGreater(c.beat_phase_at(1.0), 0.0)
        c.start(2.0)
        self.assertAlmostEqual(c.beat_phase_at(2.0), 0.0, places=9)

    def test_stop_freezes_then_continue_resumes(self):
        c = TempoClock(bpm=120.0, source="internal", now=0.0)
        c.stop(1.0)  # freeze at 2 beats
        self.assertFalse(c.running)
        frozen = c.beat_phase_at(1.0)
        self.assertAlmostEqual(frozen, 2.0, places=9)
        # Time passing while stopped must not advance the phase.
        self.assertAlmostEqual(c.beat_phase_at(100.0), 2.0, places=9)
        # Continue resumes from the frozen phase; under external clock the phase
        # then advances on the pulses (25 pulses = one beat, first anchors).
        c.continue_(5.0)
        self.assertTrue(c.running)
        dt = _pulse_dt(120.0)
        for k in range(25):
            c.clock_pulse(5.0 + (k + 1) * dt)
        self.assertAlmostEqual(c.beat_phase_at(5.0 + 25 * dt), 2.0 + 1.0, places=9)


class SongPositionTest(unittest.TestCase):
    def test_spp_sets_beat_phase_in_quarters(self):
        c = TempoClock(bpm=120.0, source="midi", now=0.0)
        # SPP is in sixteenths (MIDI beats); beat_phase is quarters = spp/4.
        c.song_position(16, now=3.0)  # one bar of 4/4
        self.assertAlmostEqual(c.beat_phase_at(3.0), 4.0, places=9)
        c.song_position(32, now=3.0)
        self.assertAlmostEqual(c.beat_phase_at(3.0), 8.0, places=9)
        c.song_position(6, now=3.0)  # 6 sixteenths -> 1.5 quarters
        self.assertAlmostEqual(c.beat_phase_at(3.0), 1.5, places=9)

    def test_spp_then_continue_resumes_from_seek(self):
        c = TempoClock(bpm=120.0, source="midi", now=0.0)
        c.song_position(16, now=0.0)  # seek to beat 4
        c.continue_(0.0)
        # Resumes at beat 4, then the clock pulses carry it forward one beat.
        dt = _pulse_dt(120.0)
        for k in range(25):
            c.clock_pulse((k + 1) * dt)
        self.assertAlmostEqual(c.beat_phase_at(25 * dt), 4.0 + 1.0, places=9)


class TapTempoTest(unittest.TestCase):
    def test_two_taps_set_bpm(self):
        c = TempoClock(bpm=60.0, source="internal", now=0.0)
        c.tap(0.0)
        c.tap(0.5)  # 0.5 s interval -> 120 BPM
        self.assertAlmostEqual(c.bpm, 120.0, places=6)

    def test_multiple_taps_average(self):
        c = TempoClock(bpm=60.0, source="internal", now=0.0)
        for t in (0.0, 0.5, 1.0, 1.5):
            c.tap(t)
        self.assertAlmostEqual(c.bpm, 120.0, places=6)

    def test_tap_reanchors_downbeat_to_a_whole_beat(self):
        c = TempoClock(bpm=120.0, source="internal", now=0.0)
        c.tap(0.7)  # single seed tap
        # The tapped instant becomes an integer beat boundary.
        phase = c.beat_phase_at(0.7)
        self.assertAlmostEqual(phase, round(phase), places=9)

    def test_absurd_tap_interval_ignored(self):
        c = TempoClock(bpm=100.0, source="internal", now=0.0)
        c.tap(0.0)
        c.tap(10.0)  # 6 BPM, below the floor + past the reset window -> no change
        self.assertAlmostEqual(c.bpm, 100.0, places=6)


class AudioDriveTest(unittest.TestCase):
    def test_audio_source_idles_until_a_tempo_locks(self):
        c = TempoClock(bpm=120.0, source="audio", now=0.0)
        self.assertFalse(c.running)
        # No advance while the analyzer hasn't locked a tempo.
        self.assertEqual(c.beat_phase_at(10.0), 0.0)

    def test_bpm_drives_the_grid_and_phase_integrates_at_that_rate(self):
        c = TempoClock(bpm=120.0, source="audio", now=0.0)
        c.audio_drive(140.0, now=0.0)
        self.assertTrue(c.running)
        self.assertAlmostEqual(c.bpm, 140.0, places=9)
        self.assertEqual(c.source, "audio")
        # 140 BPM = 7/3 beats/sec, integrated over wall clock (internal-style).
        self.assertAlmostEqual(c.beat_phase_at(3.0), 7.0, places=9)

    def test_phase_stays_continuous_across_a_tempo_change(self):
        c = TempoClock(bpm=120.0, source="audio", now=0.0)
        c.audio_drive(120.0, now=0.0)  # 2 beats/sec
        # At t=2 -> 4 beats; re-drive at a new tempo and the phase must not jump.
        before = c.beat_phase_at(2.0)
        self.assertAlmostEqual(before, 4.0, places=9)
        c.audio_drive(240.0, now=2.0)  # 4 beats/sec from here
        self.assertAlmostEqual(c.beat_phase_at(2.0), before, places=9)
        self.assertAlmostEqual(c.beat_phase_at(3.0), before + 4.0, places=9)

    def test_zero_bpm_freezes_the_grid_without_rewinding(self):
        c = TempoClock(bpm=120.0, source="audio", now=0.0)
        c.audio_drive(120.0, now=0.0)
        parked = c.beat_phase_at(2.0)
        c.audio_drive(0.0, now=2.0)  # analyzer lost the beat / went silent
        self.assertFalse(c.running)
        # Frozen where it was, no runaway and no rewind.
        self.assertAlmostEqual(c.beat_phase_at(2.0), parked, places=9)
        self.assertAlmostEqual(c.beat_phase_at(10.0), parked, places=9)

    def test_out_of_band_bpm_is_clamped(self):
        c = TempoClock(bpm=120.0, source="audio", now=0.0)
        c.audio_drive(10_000.0, now=0.0)
        self.assertLessEqual(c.bpm, 400.0)


class FeedMessageTest(unittest.TestCase):
    def test_routes_clock_messages_and_reports_consumption(self):
        c = TempoClock(bpm=120.0, source="midi", now=0.0)
        self.assertTrue(c.feed_message(_Msg("start"), now=0.0))
        self.assertTrue(c.running)
        self.assertTrue(c.feed_message(_Msg("clock"), now=0.0))
        self.assertTrue(c.feed_message(_Msg("songpos", pos=16), now=1.0))
        self.assertAlmostEqual(c.beat_phase_at(1.0), 4.0, places=9)
        self.assertTrue(c.feed_message(_Msg("stop"), now=1.0))
        self.assertFalse(c.running)

    def test_ignores_non_clock_messages(self):
        c = TempoClock(bpm=120.0, source="midi", now=0.0)
        self.assertFalse(c.feed_message(_Msg("note_on"), now=0.0))
        self.assertFalse(c.feed_message(_Msg("control_change"), now=0.0))


class ClockModulationSourceTest(unittest.TestCase):
    def test_stopped_clock_yields_zero_energy(self):
        c = TempoClock(bpm=120.0, source="midi", now=0.0)  # idle
        src = ClockModulationSource(c)
        m = src.features(now=5.0)
        self.assertEqual(m.level, 0.0)
        self.assertEqual(m.onset, 0.0)
        self.assertEqual(m.beat_phase, 0.0)
        self.assertEqual(m.voice_gates, (False, False, False))

    def test_onset_pulses_on_the_beat(self):
        c = TempoClock(bpm=120.0, source="internal", now=0.0)
        src = ClockModulationSource(c)
        # now=1.0 -> beat_phase=2.0 exactly (integer beat) -> onset peaks at 1.0.
        on_beat = src.features(now=1.0)
        self.assertAlmostEqual(on_beat.onset, 1.0, places=9)
        self.assertAlmostEqual(on_beat.beat_phase, 2.0, places=9)
        # A bit past the beat (frac beyond the decay window) -> onset back to 0.
        off_beat = src.features(now=1.0 + 0.5 * 0.5)  # +0.5 beat at 120bpm
        self.assertEqual(off_beat.onset, 0.0)

    def test_features_are_deterministic_for_a_given_now(self):
        c = TempoClock(bpm=137.0, source="internal", now=0.0)
        src = ClockModulationSource(c)
        a = src.features(now=3.3)
        b = src.features(now=3.3)
        self.assertEqual(
            (a.level, a.onset, a.beat_phase, a.bpm), (b.level, b.onset, b.beat_phase, b.bpm)
        )


class BuildTempoClockTest(unittest.TestCase):
    def test_builds_from_performance_cfg(self):
        cfg = cfgmod.PerformanceCfg(tempo_source="internal", bpm=90.0, beats_per_bar=3)
        c = build_tempo_clock(cfg)
        self.assertEqual(c.bpm, 90.0)
        self.assertEqual(c.beats_per_bar, 3)
        self.assertTrue(c.running)  # internal drives immediately

    def test_midi_source_cfg_idles(self):
        cfg = cfgmod.PerformanceCfg(tempo_source="midi", bpm=128.0)
        c = build_tempo_clock(cfg)
        self.assertFalse(c.running)

    def test_none_gives_a_sane_default(self):
        c = build_tempo_clock(None)
        self.assertEqual(c.beats_per_bar, 4)
        self.assertTrue(c.running)


def _fake_playlist_with_clock(name: str, *, scene_count: int = 4) -> Any:
    pl = mock.MagicMock(name=f"playlist-{name}")
    pl.name = name
    pl.scenes = [mock.MagicMock() for _ in range(scene_count)]
    pl.pause_event = threading.Event()
    pl.resume_event = threading.Event()
    pl.skip_event = threading.Event()
    pl.cycle_event = threading.Event()
    pl.current = mock.MagicMock()
    pl.tempo = TempoClock(bpm=120.0, source="midi", now=0.0)
    return pl


@unittest.skipUnless(HAVE_MIDI, "mido not installed")
class MidiControlIntegrationTest(unittest.TestCase):
    """The reader real-time fast path: a MIDI clock stream drives every
    playlist's TempoClock, and a mapped note fires tempo_tap — all off the DMA
    socket, on the reader thread."""

    def _listener(self, playlists, cc_map=None):
        from c64cast.midi_control import MidiControlListener

        return MidiControlListener(
            {pl.name: pl for pl in playlists},
            cc_map or [],
        )

    def test_clock_stream_starts_and_locks_every_playlist(self):
        a = _fake_playlist_with_clock("a")
        b = _fake_playlist_with_clock("b")
        lis = self._listener([a, b])
        lis._dispatch(mido.Message("start"))
        for _ in range(48):
            lis._dispatch(mido.Message("clock"))
        for pl in (a, b):
            self.assertTrue(pl.tempo.running)
            self.assertGreater(pl.tempo.beat_phase, 0.0)

    def test_songpos_message_seeks_the_grid(self):
        a = _fake_playlist_with_clock("a")
        lis = self._listener([a])
        lis._dispatch(mido.Message("songpos", pos=16))
        self.assertAlmostEqual(a.tempo.beat_phase, 4.0, places=6)

    def test_tempo_tap_action_taps_the_clock(self):
        a = _fake_playlist_with_clock("a")
        cc_map = [{"type": "note", "number": 48, "action": "tempo_tap"}]
        lis = self._listener([a], cc_map)
        # Two note-ons -> two taps -> a bpm gets set (exact value is timing
        # dependent, so just assert the grid is now running from the taps).
        lis._dispatch(mido.Message("note_on", note=48, velocity=100))
        lis._dispatch(mido.Message("note_on", note=48, velocity=100))
        self.assertTrue(a.tempo.running)

    def test_clock_bytes_are_not_treated_as_mappable_actions(self):
        # A clock byte must be consumed by the tempo fast path, never fall
        # through to the cc_map lookup (it carries no channel/number).
        a = _fake_playlist_with_clock("a")
        lis = self._listener([a])
        # No mapping, no crash, and the skip/pause events stay clear.
        lis._dispatch(mido.Message("clock"))
        self.assertFalse(a.skip_event.is_set())
        self.assertFalse(a.pause_event.is_set())


if __name__ == "__main__":
    unittest.main()
