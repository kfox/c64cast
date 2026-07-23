"""Tests for the audio-input feature stream — the analyzer that turns live
audio into the same `MusicModulation` the SID host-emulator produces.

The feature math (level follower, band split, spectral-flux onsets, tempo) is
driven with synthetic signals straight through `AudioFeatureAnalyzer.update` —
no thread, no hardware, no audio device. `AudioFeatureStream._process_tick` is
exercised over a hand-filled tap, mirroring how tests/test_music_features.py
drives `SidFeatureStream._process_tick`.
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from c64cast.audio_features import (
    FFT_SIZE,
    AnalysisTap,
    AudioFeatureAnalyzer,
    AudioFeatureStream,
    band_edges,
)
from c64cast.modulation import MusicModulation, TempoEstimator

SR = 44100.0
POLL_HZ = 60.0
DT = 1.0 / POLL_HZ


def _sine(freq: float, n: int = FFT_SIZE, amp: float = 0.5, phase: float = 0.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / SR + phase
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _silence(n: int = FFT_SIZE) -> np.ndarray:
    return np.zeros(n, dtype=np.float32)


def _noise(n: int = FFT_SIZE, amp: float = 0.5, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) * amp).astype(np.float32)


def _run(analyzer: AudioFeatureAnalyzer, blocks, *, start: float = 0.0) -> float:
    """Feed successive blocks at the nominal poll rate; return the final time."""
    now = start
    for block in blocks:
        analyzer.update(block, now)
        now += DT
    return now


class LevelTest(unittest.TestCase):
    def test_silence_reads_zero_level_and_no_onsets(self):
        a = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        _run(a, [_silence() for _ in range(240)])  # 4 s of nothing
        m = a.snapshot()
        self.assertEqual(m.level, 0.0)
        self.assertEqual(m.onset, 0.0)
        # The adaptive threshold must not manufacture a tempo out of silence.
        self.assertEqual(m.bpm, 0.0)
        self.assertEqual(m.beat_phase, 0.0)

    def test_steady_tone_normalizes_to_full_scale(self):
        # A quiet source still reaches full scale: the rolling peak decays
        # toward its floor, so `level` is relative loudness, not absolute.
        a = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        _run(a, [_sine(440.0, amp=0.05) for _ in range(120)])
        self.assertAlmostEqual(a.snapshot().level, 1.0, places=2)

    def test_level_releases_after_the_signal_stops(self):
        a = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        now = _run(a, [_sine(440.0) for _ in range(120)])
        loud = a.snapshot().level
        _run(a, [_silence() for _ in range(120)], start=now)
        self.assertGreater(loud, 0.9)
        self.assertLess(a.snapshot().level, 0.05)


class BandTest(unittest.TestCase):
    def test_band_edges_are_monotone_and_in_range(self):
        edges = band_edges(8, FFT_SIZE)
        self.assertEqual(edges.size, 9)
        self.assertTrue(np.all(np.diff(edges) >= 0))
        self.assertGreaterEqual(int(edges[0]), 1)  # DC skipped
        self.assertLessEqual(int(edges[-1]), FFT_SIZE // 2)

    def test_sweep_moves_the_peak_band_upward(self):
        # A sine sweep must walk the peak band index low→high, monotonically.
        peaks = []
        for freq in (120.0, 400.0, 1200.0, 4000.0, 12000.0):
            a = AudioFeatureAnalyzer(SR, nominal_dt=DT)
            _run(a, [_sine(freq, phase=i * FFT_SIZE / SR) for i in range(30)])
            peaks.append(int(np.argmax(a.snapshot().bands)))
        self.assertEqual(peaks, sorted(peaks), f"peak band not monotone: {peaks}")
        self.assertLess(peaks[0], peaks[-1])

    def test_bass_and_treble_folds_separate_low_from_high(self):
        low = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        _run(low, [_sine(80.0, phase=i * FFT_SIZE / SR) for i in range(30)])
        high = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        _run(high, [_sine(11000.0, phase=i * FFT_SIZE / SR) for i in range(30)])
        self.assertGreater(low.snapshot().bass, low.snapshot().treble)
        self.assertGreater(high.snapshot().treble, high.snapshot().bass)

    def test_band_count_is_configurable(self):
        a = AudioFeatureAnalyzer(SR, n_bands=16, nominal_dt=DT)
        _run(a, [_sine(440.0) for _ in range(5)])
        self.assertEqual(len(a.snapshot().bands), 16)

    def test_bands_are_clipped_to_unit_range(self):
        a = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        _run(a, [_noise(amp=4.0) for _ in range(30)])  # deliberately over-driven
        bands = a.snapshot().bands
        self.assertTrue(all(0.0 <= b <= 1.0 for b in bands))


class OnsetTest(unittest.TestCase):
    def test_step_from_silence_latches_then_decays_on_tau(self):
        a = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        now = _run(a, [_silence() for _ in range(30)])
        # One loud block right after silence — a maximal spectral flux.
        a.update(_noise(amp=0.9), now)
        now += DT
        self.assertEqual(a.snapshot().onset, 1.0)
        # Decay follows exp(-dt/0.18) per block, same τ as the SID path.
        n_decay = 6
        _run(a, [_silence() for _ in range(n_decay)], start=now)
        expected = math.exp(-(n_decay * DT) / AudioFeatureAnalyzer._ONSET_TAU_S)
        self.assertAlmostEqual(a.snapshot().onset, expected, places=3)

    def test_steady_tone_does_not_retrigger(self):
        # Continuous unchanging content has ~no positive flux after the initial
        # attack, so it must not read as a stream of transients.
        a = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        blocks = [_sine(440.0, phase=i * FFT_SIZE / SR) for i in range(180)]
        _run(a, blocks)
        self.assertLess(a.snapshot().onset, 0.5)

    def test_sensitivity_raises_the_onset_count(self):
        def count_onsets(sensitivity: float) -> int:
            a = AudioFeatureAnalyzer(SR, onset_sensitivity=sensitivity, nominal_dt=DT)
            hits, now = 0, 0.0
            for i in range(240):
                # A soft click every 20 blocks over a quiet noise bed.
                block = _noise(amp=0.02, seed=i)
                if i % 20 == 0:
                    block = block + _noise(amp=0.12, seed=1000 + i)
                a.update(block.astype(np.float32), now)
                now += DT
                if a.snapshot().onset >= 1.0:
                    hits += 1
            return hits

        # A high sensitivity picks the quiet bed apart; a low one keeps to the
        # deliberate clicks (12 of them over the 240 blocks).
        self.assertGreater(count_onsets(4.0), count_onsets(0.25))
        self.assertLessEqual(count_onsets(0.25), 12)


class TempoTest(unittest.TestCase):
    def test_click_train_converges_on_the_true_bpm(self):
        # 120 BPM = a click every 0.5 s = every 30 blocks at 60 Hz.
        a = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        now = 0.0
        for i in range(60 * 12):  # 12 s
            block = _silence() if i % 30 else _noise(amp=0.8, seed=i)
            a.update(block, now)
            now += DT
        m = a.snapshot()
        self.assertAlmostEqual(m.bpm, 120.0, delta=8.0)
        self.assertGreater(m.beat_phase, 0.0)

    def test_beat_phase_is_monotone_across_a_jittery_estimate(self):
        a = AudioFeatureAnalyzer(SR, nominal_dt=DT)
        now, phases, gap = 0.0, [], 30
        for i in range(60 * 12):
            # Wander the click spacing so the BPM estimate keeps moving; the
            # phase integral must still never step backward.
            block = _silence()
            if i % gap == 0:
                block = _noise(amp=0.8, seed=i)
                gap = 24 + (i // 30) % 14
            a.update(block, now)
            now += DT
            phases.append(a.snapshot().beat_phase)
        self.assertTrue(all(b >= x for x, b in zip(phases, phases[1:], strict=False)))
        self.assertGreater(phases[-1], 0.0)


class TempoEstimatorTest(unittest.TestCase):
    """The shared estimator, lifted out of SidFeatureStream so both producers
    report tempo identically (tests/test_music_features.py guards the SID side
    still delegating here)."""

    def test_first_onset_only_anchors(self):
        te = TempoEstimator()
        te.note_onset(0.0)
        self.assertEqual(te.bpm, 0.0)

    def test_two_onsets_establish_bpm(self):
        te = TempoEstimator()
        te.note_onset(0.0)
        te.note_onset(0.5)
        self.assertAlmostEqual(te.bpm, 120.0, delta=1.0)

    def test_near_simultaneous_onsets_fold_into_one_beat(self):
        te = TempoEstimator()
        te.note_onset(0.0)
        te.note_onset(0.01)  # < MIN_IOI_S
        self.assertEqual(te._last_onset_time, 0.0)
        self.assertIsNone(te._ioi_ema)

    def test_long_gap_reanchors_without_polluting(self):
        te = TempoEstimator()
        te.note_onset(0.0)
        te.note_onset(0.5)
        te.note_onset(5.0)  # > MAX_IOI_S
        self.assertEqual(te._last_onset_time, 5.0)
        self.assertAlmostEqual(te.bpm, 120.0, delta=1.0)

    def test_bpm_is_clamped_to_the_plausible_band(self):
        fast = TempoEstimator()
        fast.note_onset(0.0)
        fast.note_onset(0.11)  # ~545 BPM raw
        self.assertLessEqual(fast.bpm, TempoEstimator.BPM_MAX)
        slow = TempoEstimator()
        slow.note_onset(0.0)
        slow.note_onset(1.4)  # ~43 BPM raw
        self.assertGreaterEqual(slow.bpm, TempoEstimator.BPM_MIN)

    def test_phase_frozen_until_tempo_known(self):
        te = TempoEstimator()
        te.advance(1.0)
        self.assertEqual(te.beat_phase, 0.0)
        te.note_onset(0.0)
        te.note_onset(0.5)  # 120 BPM = 2 beats/s
        te.advance(1.0)
        self.assertAlmostEqual(te.beat_phase, 2.0, places=3)

    def test_reset_clears_everything(self):
        te = TempoEstimator()
        te.note_onset(0.0)
        te.note_onset(0.5)
        te.advance(1.0)
        te.reset()
        self.assertEqual(te.bpm, 0.0)
        self.assertEqual(te.beat_phase, 0.0)
        self.assertIsNone(te._ioi_ema)
        self.assertIsNone(te._last_onset_time)


class AnalysisTapTest(unittest.TestCase):
    def test_recent_returns_the_newest_samples_oldest_first(self):
        tap = AnalysisTap(size=8)
        tap.push(np.arange(5, dtype=np.float32))
        np.testing.assert_array_equal(tap.recent(3), np.array([2, 3, 4], dtype=np.float32))

    def test_ring_wraps_without_reordering(self):
        tap = AnalysisTap(size=8)
        tap.push(np.arange(6, dtype=np.float32))
        tap.push(np.arange(6, 12, dtype=np.float32))  # wraps
        np.testing.assert_array_equal(tap.recent(8), np.arange(4, 12, dtype=np.float32))

    def test_block_larger_than_the_ring_keeps_the_tail(self):
        tap = AnalysisTap(size=4)
        tap.push(np.arange(10, dtype=np.float32))
        np.testing.assert_array_equal(tap.recent(4), np.array([6, 7, 8, 9], dtype=np.float32))

    def test_head_reads_as_zeros_before_enough_audio(self):
        tap = AnalysisTap(size=8)
        tap.push(np.ones(2, dtype=np.float32))
        np.testing.assert_array_equal(tap.recent(4), np.array([0, 0, 1, 1], dtype=np.float32))

    def test_empty_push_is_a_no_op(self):
        tap = AnalysisTap(size=4)
        tap.push(np.zeros(0, dtype=np.float32))
        np.testing.assert_array_equal(tap.recent(4), np.zeros(4, dtype=np.float32))


class StreamTest(unittest.TestCase):
    def test_features_none_before_first_tick(self):
        stream = AudioFeatureStream(AnalysisTap(), SR, poll_hz=POLL_HZ)
        self.assertIsNone(stream.features())

    def test_tick_over_a_filled_tap_produces_features(self):
        tap = AnalysisTap()
        stream = AudioFeatureStream(tap, SR, poll_hz=POLL_HZ)
        for i in range(10):
            tap.push(_sine(440.0, phase=i * FFT_SIZE / SR))
            stream._process_tick()
        m = stream.features()
        assert m is not None
        self.assertGreater(m.level, 0.0)
        self.assertEqual(len(m.bands), 8)

    def test_start_stop_smoke(self):
        tap = AnalysisTap()
        tap.push(_sine(440.0))
        stream = AudioFeatureStream(tap, SR, poll_hz=POLL_HZ)
        stream.start()
        try:
            for _ in range(200):
                if stream.features() is not None:
                    break
                import time

                time.sleep(0.01)
            self.assertIsNotNone(stream.features())
        finally:
            stream.stop()
        # Stopping twice is harmless (teardown may run after an early abort).
        stream.stop()

    def test_restart_resets_the_analyzer(self):
        tap = AnalysisTap()
        stream = AudioFeatureStream(tap, SR, poll_hz=POLL_HZ)
        for i in range(10):
            tap.push(_sine(440.0, phase=i * FFT_SIZE / SR))
            stream._process_tick()
        self.assertIsNotNone(stream.features())
        stream.start()
        try:
            self.assertIsNotNone(stream.features())  # thread ticks immediately
        finally:
            stream.stop()


class ModulationCompatTest(unittest.TestCase):
    """`bands` is defaulted, so every pre-existing producer/consumer of
    MusicModulation is untouched by its arrival."""

    def _sid_style(self) -> MusicModulation:
        return MusicModulation(
            level=0.5,
            onset=0.25,
            beat_phase=1.5,
            bpm=120.0,
            voice_freqs=(440.0, 0.0, 0.0),
            voice_gates=(True, False, False),
        )

    def test_constructs_without_bands(self):
        m = self._sid_style()
        self.assertEqual(m.bands, ())
        self.assertEqual((m.bass, m.mid, m.treble), (0.0, 0.0, 0.0))

    def test_generator_helpers_ignore_empty_bands(self):
        # The SID path's reactive look must be bit-for-bit what it was before
        # the spectral terms existed: both fold to exactly 0.0.
        from c64cast.generators import GenerativeSource as G

        m = self._sid_style()
        self.assertEqual(
            G._reactive_hue_offset(m),
            m.beat_phase * G._BEAT_HUE_GAIN + m.onset * G._ONSET_HUE_KICK,
        )
        self.assertEqual(
            G._reactive_value(m),
            G._V_REST + G._ONSET_FLASH * m.onset + G._LEVEL_GAIN * m.level,
        )

    def test_generator_helpers_use_bands_when_present(self):
        from c64cast.generators import GenerativeSource as G

        base = self._sid_style()
        bassy = MusicModulation(**{**vars(base), "bands": (1.0, 1.0, 0.0, 0.0, 0.0, 0.0)})
        trebly = MusicModulation(**{**vars(base), "bands": (0.0, 0.0, 0.0, 0.0, 1.0, 1.0)})
        self.assertGreater(G._reactive_value(bassy), G._reactive_value(base))
        self.assertGreater(G._reactive_hue_offset(trebly), G._reactive_hue_offset(base))

    def test_band_folds_handle_awkward_counts(self):
        base = vars(self._sid_style())
        one = MusicModulation(**{**base, "bands": (0.7,)})
        self.assertEqual((one.bass, one.mid, one.treble), (0.7, 0.7, 0.7))
        two = MusicModulation(**{**base, "bands": (0.0, 1.0)})
        self.assertEqual(two.bass, 0.0)
        self.assertEqual(two.treble, 1.0)

    def test_wled_packet_builds_from_a_banded_snapshot(self):
        from c64cast.wled_sync import build_audio_sync_packet

        m = MusicModulation(
            level=0.5,
            onset=0.25,
            beat_phase=1.5,
            bpm=120.0,
            voice_freqs=(0.0, 0.0, 0.0),
            voice_gates=(False, False, False),
            bands=tuple([0.5] * 8),
        )
        self.assertEqual(len(build_audio_sync_packet(m, sample_peak=True)), 44)


if __name__ == "__main__":
    unittest.main()
