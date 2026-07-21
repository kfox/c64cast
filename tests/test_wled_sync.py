"""Tests for the WLED audio-sync broadcaster (bridge Mode 3).

Covers the pure packet builder against the documented WLED V2 wire format, the
log-spaced GEQ bin mapping, and the broadcaster's gate-edge peak derivation
(which gives WaveformScene — reporting onset == 0 — real note-attack flashes).
"""

from __future__ import annotations

import struct
import unittest
from typing import Any

from c64cast.modulation import MusicModulation
from c64cast.wled_sync import (
    WledAudioSyncBroadcaster,
    _freq_to_geq_bin,
    build_audio_sync_packet,
)

# The exact V2 struct from WLED audio_reactive.cpp (see wled_sync module docstring).
_FMT = "<6s2xffBx16B2xff"


def _unpack(packet: bytes) -> dict[str, Any]:
    hdr, raw, smth, peak, *rest = struct.unpack(_FMT, packet)
    return {
        "header": hdr,
        "sampleRaw": raw,
        "sampleSmth": smth,
        "samplePeak": peak,
        "fftResult": list(rest[:16]),
        "FFT_Magnitude": rest[16],
        "FFT_MajorPeak": rest[17],
    }


def _mod(
    level: float = 0.0,
    onset: float = 0.0,
    freqs: tuple[float, float, float] = (0.0, 0.0, 0.0),
    gates: tuple[bool, bool, bool] = (False, False, False),
) -> MusicModulation:
    return MusicModulation(
        level=level, onset=onset, beat_phase=0.0, bpm=0.0, voice_freqs=freqs, voice_gates=gates
    )


class PacketFormatTests(unittest.TestCase):
    def test_packet_is_44_bytes(self):
        self.assertEqual(len(build_audio_sync_packet(_mod(), False)), 44)
        self.assertEqual(struct.calcsize(_FMT), 44)

    def test_header_is_v2(self):
        got = _unpack(build_audio_sync_packet(_mod(), False))
        self.assertEqual(got["header"], b"00002\x00")

    def test_level_scales_to_volume(self):
        got = _unpack(build_audio_sync_packet(_mod(level=0.5), False))
        # level 0..1 → sampleRaw/sampleSmth 0..255, both fields equal.
        self.assertAlmostEqual(got["sampleRaw"], 127.5, places=3)
        self.assertAlmostEqual(got["sampleSmth"], 127.5, places=3)

    def test_level_clamped(self):
        # Out-of-range levels clamp to [0, 255].
        self.assertAlmostEqual(
            _unpack(build_audio_sync_packet(_mod(level=5.0), False))["sampleRaw"], 255.0
        )
        self.assertAlmostEqual(
            _unpack(build_audio_sync_packet(_mod(level=-1.0), False))["sampleRaw"], 0.0
        )

    def test_sample_peak_flag(self):
        self.assertEqual(_unpack(build_audio_sync_packet(_mod(), True))["samplePeak"], 1)
        self.assertEqual(_unpack(build_audio_sync_packet(_mod(), False))["samplePeak"], 0)

    def test_active_voices_light_geq_bins(self):
        mod = _mod(level=1.0, freqs=(440.0, 0.0, 880.0), gates=(True, False, True))
        got = _unpack(build_audio_sync_packet(mod, False))
        fft = got["fftResult"]
        b440, b880 = _freq_to_geq_bin(440.0), _freq_to_geq_bin(880.0)
        self.assertEqual(fft[b440], 255)
        self.assertEqual(fft[b880], 255)
        # No other bins lit.
        self.assertEqual(sum(1 for v in fft if v), 2)

    def test_gated_off_voice_does_not_light(self):
        # A voice with a nonzero freq but gate off contributes nothing.
        mod = _mod(level=1.0, freqs=(440.0, 0.0, 0.0), gates=(False, False, False))
        got = _unpack(build_audio_sync_packet(mod, False))
        self.assertEqual(sum(got["fftResult"]), 0)

    def test_major_peak_is_highest_active_voice(self):
        mod = _mod(level=1.0, freqs=(220.0, 660.0, 440.0), gates=(True, True, True))
        got = _unpack(build_audio_sync_packet(mod, False))
        self.assertAlmostEqual(got["FFT_MajorPeak"], 660.0)

    def test_silence_gives_zero_geq_and_min_peak(self):
        got = _unpack(build_audio_sync_packet(_mod(), False))
        self.assertEqual(sum(got["fftResult"]), 0)
        self.assertAlmostEqual(got["FFT_MajorPeak"], 1.0)  # WLED clamp floor

    def test_major_peak_clamped_to_range(self):
        mod = _mod(level=1.0, freqs=(50000.0, 0.0, 0.0), gates=(True, False, False))
        got = _unpack(build_audio_sync_packet(mod, False))
        self.assertLessEqual(got["FFT_MajorPeak"], 11025.0)


class GeqBinTests(unittest.TestCase):
    def test_monotonic_and_clamped(self):
        self.assertEqual(_freq_to_geq_bin(10.0), 0)  # below low edge
        self.assertEqual(_freq_to_geq_bin(20000.0), 15)  # above high edge
        # Monotonic non-decreasing across the band.
        prev = -1
        for f in (40, 80, 160, 320, 640, 1280, 2560, 5120, 10000):
            b = _freq_to_geq_bin(float(f))
            self.assertGreaterEqual(b, prev)
            self.assertTrue(0 <= b <= 15)
            prev = b


class BroadcasterPeakTests(unittest.TestCase):
    """The broadcaster derives samplePeak itself so a source reporting onset==0
    (WaveformScene) still flashes on note attacks."""

    def _bc(self) -> WledAudioSyncBroadcaster:
        return WledAudioSyncBroadcaster(lambda: None)

    def test_rising_gate_triggers_peak(self):
        bc = self._bc()
        # First observation: gate rises from the initial all-off state → peak.
        self.assertTrue(bc._derive_peak(_mod(gates=(True, False, False))))
        # Held gate, no new edge → no peak (and onset is 0 for this source).
        self.assertFalse(bc._derive_peak(_mod(gates=(True, False, False))))
        # A different voice rising → peak again.
        self.assertTrue(bc._derive_peak(_mod(gates=(True, True, False))))

    def test_onset_triggers_peak_without_gate_edge(self):
        bc = self._bc()
        # Prime the gate state so no edge is present.
        bc._derive_peak(_mod(gates=(True, False, False)))
        # A reported transient alone (onset high) fires the peak.
        self.assertTrue(bc._derive_peak(_mod(onset=1.0, gates=(True, False, False))))

    def test_no_peak_when_quiet(self):
        bc = self._bc()
        self.assertFalse(bc._derive_peak(_mod()))
        self.assertFalse(bc._derive_peak(_mod()))


class _FakeApi:
    def __init__(self) -> None:
        self.stats = {"writes": 0, "skipped": 0, "errors": 0, "bytes": 0}

    def format_write_latency(self):
        return None


class _FakeScene:
    """A scene whose music features are settable (None = no SID)."""

    def __init__(self, feats: MusicModulation | None) -> None:
        self.name = "fake"
        self._feats = feats

    def features(self) -> MusicModulation | None:
        return self._feats


class TempoFallbackTest(unittest.TestCase):
    """[wled].broadcast_tempo_fallback: a non-SID scene falls back to the beat
    grid so WLED keeps pulsing on video/webcam/slideshow (Live DJ/VJ Phase 6)."""

    def _playlist(self, fallback: bool):
        from c64cast import config as cfgmod
        from c64cast.playlist import Playlist

        cfg = cfgmod.Config()
        cfg.wled.broadcast_tempo_fallback = fallback
        scene = _FakeScene(None)
        pl = Playlist(
            [scene],  # type: ignore[list-item]
            _FakeApi(),  # type: ignore[arg-type]
            target_fps=60.0,
            interstitial_factory=lambda name: scene,  # type: ignore[arg-type,return-value]
            config=cfg,
        )
        return pl

    def test_no_fallback_keeps_none_on_non_sid_scene(self):
        pl = self._playlist(fallback=False)
        pl.current = _FakeScene(None)  # type: ignore[assignment]
        self.assertIsNone(pl._active_features())

    def test_fallback_uses_clock_on_non_sid_scene(self):
        pl = self._playlist(fallback=True)
        pl.current = _FakeScene(None)  # type: ignore[assignment]
        # Internal grid free-runs (running=True), so the fallback fires.
        feats = pl._active_features()
        self.assertIsNotNone(feats)
        assert feats is not None
        self.assertGreater(feats.bpm, 0.0)

    def test_real_sid_features_win_over_fallback(self):
        pl = self._playlist(fallback=True)
        real = _mod(level=0.7)
        pl.current = _FakeScene(real)  # type: ignore[assignment]
        self.assertIs(pl._active_features(), real)

    def test_fallback_fills_between_scenes(self):
        pl = self._playlist(fallback=True)
        pl.current = None
        # No current scene, but the grid runs — WLED still gets a pulse.
        self.assertIsNotNone(pl._active_features())


if __name__ == "__main__":
    unittest.main()
