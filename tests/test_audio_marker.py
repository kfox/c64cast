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

import unittest

import numpy as np

from c64cast.audio.audio_marker import (
    DEFAULT_CAPTURE_RATE,
    DEFAULT_PLAYBACK_RATE,
    MARKER_DURATION_S,
    find_marker_in_capture,
    marker_duration_samples,
    synthesize_capture_reference,
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

    def test_capture_reference_upsamples_by_integer_ratio(self):
        # 48 kHz capture / 8 kHz playback = 6x sample-and-hold.
        ref = synthesize_capture_reference()
        expected = marker_duration_samples(DEFAULT_PLAYBACK_RATE) * (
            DEFAULT_CAPTURE_RATE // DEFAULT_PLAYBACK_RATE
        )
        self.assertEqual(len(ref), expected)


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


if __name__ == "__main__":
    unittest.main()
