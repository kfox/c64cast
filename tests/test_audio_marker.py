"""Tests for c64cast.audio.audio_marker — source-timeline alignment marker.

Two layers of guarantees:
  * Synthesis is byte-deterministic (same code → same bytes) so a marker
    encoded into a capture today still matches a freshly-synthesized
    reference tomorrow. Regression guard for accidental waveform drift.
  * `find_marker_in_capture` locks onto an embedded marker even when
    real captured audio is mixed in around it. End-to-end smoke for
    the cross-correlation path.
"""

from __future__ import annotations

import hashlib
import unittest

import numpy as np

from c64cast.audio.audio_marker import (
    DEFAULT_CAPTURE_RATE,
    DEFAULT_PLAYBACK_RATE,
    MARKER_DURATION_S,
    find_marker_in_capture,
    marker_duration_samples,
    synthesize_capture_reference,
    synthesize_marker,
    synthesize_marker_4bit,
)


class MarkerSynthesisTest(unittest.TestCase):
    def test_marker_4bit_length_matches_duration(self):
        # 100 ms at 8 kHz = 800 bytes (one 4-bit code per byte).
        n = marker_duration_samples(8000)
        self.assertEqual(n, int(MARKER_DURATION_S * 8000))
        self.assertEqual(len(synthesize_marker_4bit(8000)), n)

    def test_marker_4bit_values_in_range(self):
        # Encoded volume codes must fit the SID DAC nibble (0-15), or the
        # upload corrupts $D418.
        codes = np.frombuffer(synthesize_marker_4bit(), dtype=np.uint8)
        self.assertGreaterEqual(int(codes.min()), 0)
        self.assertLessEqual(int(codes.max()), 15)

    def test_marker_4bit_actually_chirps(self):
        # A sweep spans most of [0, 15]; a constant would not.
        codes = np.frombuffer(synthesize_marker_4bit(), dtype=np.uint8)
        self.assertGreater(int(codes.max()) - int(codes.min()), 10)

    def test_synthesis_deterministic(self):
        # Any RNG here would desync saved-capture-against-fresh-reference
        # correlation.
        self.assertEqual(synthesize_marker_4bit(), synthesize_marker_4bit())

    def test_waveform_is_pinned_to_a_golden_digest(self):
        # Determinism alone cannot see drift: a changed sweep is still equal to
        # itself. These digests are the bytes captures were recorded against;
        # a deliberate change to the chirp updates them and says so.
        golden = {
            8000: "6ecc501c9c35e49f0ea5096cc99fea831c46be1e9a7edd61b46f947d757d50a3",
            12000: "8cfd12eb75fa4d14902f4f19277d28bbde0b5d860d2aac1608f5a03d28753402",
            12032: "7980638bc06c0d45ed18460ada292877df93f599b6e2dd6194cbea6c1c0baf66",
        }
        for rate, digest in golden.items():
            with self.subTest(rate=rate):
                self.assertEqual(hashlib.sha256(synthesize_marker_4bit(rate)).hexdigest(), digest)

    def test_marker_goes_through_the_active_dac_curve(self):
        # Under a Mahoney curve the track is full $D418 bytes; a bare 0-15
        # code would play at the wrong level with no filter bits.
        curve = np.arange(256, dtype=np.uint8)[::-1].copy()
        codes = np.frombuffer(synthesize_marker(8000, curve), dtype=np.uint8)
        linear = np.frombuffer(synthesize_marker(8000), dtype=np.uint8)
        self.assertEqual(len(codes), len(linear))
        self.assertGreater(int(codes.max()), 200)
        self.assertGreater(int(codes.max()) - int(codes.min()), 200)
        # Amplitude index 128 + 128*x of the same chirp, looked up in the curve.
        self.assertEqual(int(codes[0]), int(curve[128]))

    def test_capture_reference_upsamples_by_integer_ratio(self):
        # 48 kHz capture / 8 kHz playback = 6x sample-and-hold.
        ref = synthesize_capture_reference()
        expected = marker_duration_samples(DEFAULT_PLAYBACK_RATE) * (
            DEFAULT_CAPTURE_RATE // DEFAULT_PLAYBACK_RATE
        )
        self.assertEqual(len(ref), expected)


def _held_capture(playback_rate: int, capture_rate: int) -> np.ndarray:
    """The marker as a capture device sees it: each 4-bit code held for the
    true ``capture_rate / playback_rate`` samples, ratio not an integer."""
    codes = np.frombuffer(synthesize_marker_4bit(playback_rate), dtype=np.uint8).astype(float) - 7.5
    n = int(round(len(codes) * capture_rate / playback_rate))
    idx = np.minimum((np.arange(n) * playback_rate) // capture_rate, len(codes) - 1)
    return codes[idx]


class FindMarkerTest(unittest.TestCase):
    """End-to-end: synth marker → embed in a longer signal at a known
    offset → run find_marker → assert it returns that offset (with
    tolerance for FFT correlation discretization)."""

    def test_find_clean_embed_at_zero(self):
        ref = synthesize_capture_reference().astype(np.int16)
        sig = np.zeros(DEFAULT_CAPTURE_RATE * 2, dtype=np.int16)
        sig[5000 : 5000 + len(ref)] = ref
        peak = find_marker_in_capture(sig)
        self.assertEqual(peak, 5000)

    def test_find_clean_embed_at_arbitrary_offset(self):
        ref = synthesize_capture_reference().astype(np.int16)
        sig = np.zeros(DEFAULT_CAPTURE_RATE * 3, dtype=np.int16)
        offset = 42_321
        sig[offset : offset + len(ref)] = ref
        self.assertEqual(find_marker_in_capture(sig), offset)

    def test_find_under_noise(self):
        # White noise at ~3x marker amplitude: uncorrelated with the chirp,
        # so the correlation peak should still resolve.
        rng = np.random.default_rng(42)
        ref = synthesize_capture_reference()
        sig = (rng.standard_normal(DEFAULT_CAPTURE_RATE * 2) * float(ref.max()) * 3.0).astype(
            np.float64
        )
        offset = 12_000
        sig[offset : offset + len(ref)] += ref
        peak = find_marker_in_capture(sig.astype(np.int16))
        # Allow ±5 samples of slack for FFT correlation discretization.
        self.assertLess(abs(peak - offset), 5)

    def test_find_at_the_ntsc_effective_rate(self):
        # 48000 / 12032 = 3.99: a floored hold factor shortens the reference by
        # a quarter and moved the peak ~850 samples (18 ms) late.
        held = _held_capture(12032, DEFAULT_CAPTURE_RATE)
        rng = np.random.default_rng(1)
        sig = rng.standard_normal(DEFAULT_CAPTURE_RATE * 3) * 2.0
        offset = 60_000
        sig[offset : offset + len(held)] += held
        peak = find_marker_in_capture(sig, DEFAULT_CAPTURE_RATE, 12032)
        self.assertLess(abs(peak - offset), 5)
        self.assertAlmostEqual(
            len(synthesize_capture_reference(DEFAULT_CAPTURE_RATE, 12032)), len(held), delta=1
        )

    def test_find_ignores_gain_and_dc_offset(self):
        held = _held_capture(DEFAULT_PLAYBACK_RATE, DEFAULT_CAPTURE_RATE)
        sig = np.zeros(DEFAULT_CAPTURE_RATE * 2)
        offset = 30_000
        sig[offset : offset + len(held)] = held
        sig = sig * 0.07 + 900.0
        self.assertEqual(find_marker_in_capture(sig), offset)


if __name__ == "__main__":
    unittest.main()
