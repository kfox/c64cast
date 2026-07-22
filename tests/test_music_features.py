"""Tests for SidFeatureStream — the host-side SID music-feature stream that
drives reactive generative visuals. The feature math (onset spike/decay, level,
gate-edge + retrigger onsets, tempo proxy, beat-phase integration) is exercised
by driving `_process_tick` directly with hand-built register snapshots — no
thread, no real chip. A small start()/stop() smoke covers the real poll path."""

from __future__ import annotations

import time
import unittest

from c64cast.c64 import SID
from c64cast.modulation import MusicModulation
from c64cast.music_features import SidFeatureStream


def _make_sid(*, init=0x1000, play=0x1001, payload=(0x60, 0x60), load=0x1000) -> bytes:
    """Minimal runnable PSID v2 (RTS init/play) — INIT/PLAY no-op, so the
    feature math is driven entirely by the snapshots we feed _process_tick."""
    h = bytearray(124)
    h[0:4] = b"PSID"
    h[4:6] = (2).to_bytes(2, "big")
    h[6:8] = (0x7C).to_bytes(2, "big")
    h[8:10] = load.to_bytes(2, "big")
    h[10:12] = init.to_bytes(2, "big")
    h[12:14] = play.to_bytes(2, "big")
    h[14:16] = (1).to_bytes(2, "big")  # num_songs
    h[16:18] = (1).to_bytes(2, "big")  # start_song
    return bytes(h) + bytes(payload)


def _regs(*, gate: bool, freq: int = 0x2000, voice: int = 0, sustain: int = 0xF) -> bytes:
    """A 25-byte $D400-$D418 snapshot with one pulse voice (gated or not,
    sustain set so a held note has a non-zero envelope)."""
    b = bytearray(SID.N_VOICES * SID.BYTES_PER_VOICE + 4)
    base = voice * SID.BYTES_PER_VOICE
    b[base + SID.OFF_FREQ_LO] = freq & 0xFF
    b[base + SID.OFF_FREQ_HI] = (freq >> 8) & 0xFF
    b[base + SID.OFF_CONTROL] = SID.WAVE_PULSE | (SID.GATE if gate else 0)
    b[base + SID.OFF_SR] = (sustain & 0x0F) << 4
    return bytes(b)


class _PrimedStream(SidFeatureStream):
    """A SidFeatureStream prepped for direct ticking (no poll thread)."""

    @classmethod
    def primed(cls, sid: bytes, *, system: str = "NTSC") -> _PrimedStream:
        s = cls(sid, song=0, system=system)
        s._prepare()  # builds emulator + sets _poll_dt / _onset_decay, no thread
        return s


class FeatureMathTest(unittest.TestCase):
    def setUp(self):
        self.sid = _make_sid()

    def test_features_none_before_prepare(self):
        s = SidFeatureStream(self.sid, song=0, system="NTSC")
        self.assertIsNone(s.features())

    def test_gate_on_edge_spikes_onset(self):
        s = _PrimedStream.primed(self.sid)
        s._process_tick(_regs(gate=False), (False, False, False))
        before = s.features()
        assert before is not None
        self.assertEqual(before.onset, 0.0)
        s._process_tick(_regs(gate=True), (False, False, False))
        after = s.features()
        assert after is not None
        self.assertEqual(after.onset, 1.0)
        self.assertTrue(after.voice_gates[0])

    def test_retrigger_spikes_onset_without_edge(self):
        # Gate stays high across the tick, but a retrigger flag (intra-tick
        # hard restart) still counts as an onset.
        s = _PrimedStream.primed(self.sid)
        s._process_tick(_regs(gate=True), (False, False, False))
        s._onset = 0.0  # clear the gate-on onset from the first tick
        s._process_tick(_regs(gate=True), (True, False, False))
        feat = s.features()
        assert feat is not None
        self.assertEqual(feat.onset, 1.0)

    def test_onset_decays_when_held(self):
        s = _PrimedStream.primed(self.sid)
        s._process_tick(_regs(gate=True), (False, False, False))
        peak = s.features()
        assert peak is not None and peak.onset == 1.0
        for _ in range(5):
            s._process_tick(_regs(gate=True), (False, False, False))
        feat = s.features()
        assert feat is not None
        self.assertLess(feat.onset, 1.0)
        self.assertGreater(feat.onset, 0.0)

    def test_level_tracks_envelope_and_freq_hz(self):
        s = _PrimedStream.primed(self.sid)
        # Hold a gated voice; the ADSR envelope climbs from 0 → level rises.
        for _ in range(30):
            s._process_tick(_regs(gate=True, freq=0x2000), (False, False, False))
        feat = s.features()
        assert feat is not None
        self.assertGreater(feat.level, 0.0)
        # 0x2000 * NTSC clock / 2^24 ≈ 499 Hz on voice 0.
        self.assertAlmostEqual(feat.voice_freqs[0], 0x2000 * 1022727 / (1 << 24), places=1)
        self.assertEqual(feat.voice_freqs[1], 0.0)

    def test_steady_onsets_estimate_tempo_and_advance_beat_phase(self):
        s = _PrimedStream.primed(self.sid)
        s._poll_dt = 1 / 60.0  # pin a known cadence
        # An onset every 30 ticks @ 60 Hz = 0.5 s IOI → 120 BPM.
        for _beat in range(8):
            for tick in range(30):
                gate = tick not in (0, 29)  # off at 29, on at 0 → a gate edge each beat
                s._process_tick(_regs(gate=gate), (False, False, False))
        feat = s.features()
        assert feat is not None
        self.assertAlmostEqual(feat.bpm, 120.0, delta=5.0)
        self.assertGreater(feat.beat_phase, 0.0)

    def test_beat_phase_frozen_without_tempo(self):
        # A single onset never establishes an IOI → bpm stays 0 → beat_phase
        # never advances (degrades to baseline drift in the generator).
        s = _PrimedStream.primed(self.sid)
        s._process_tick(_regs(gate=True), (False, False, False))
        for _ in range(20):
            s._process_tick(_regs(gate=True), (False, False, False))
        feat = s.features()
        assert feat is not None
        self.assertEqual(feat.bpm, 0.0)
        self.assertEqual(feat.beat_phase, 0.0)

    # Tempo estimation itself now lives in the shared modulation.TempoEstimator
    # (see tests/test_audio_features.py); these two keep guarding that
    # SidFeatureStream actually delegates to it and gets the old behavior.

    def test_simultaneous_onset_folds_into_one_beat(self):
        # Two onsets within MIN_IOI must not corrupt the beat reference: the
        # near-simultaneous second onset is folded into the current beat.
        s = _PrimedStream.primed(self.sid)
        s._poll_dt = 1 / 60.0
        s._tempo.note_onset(0.0)
        s._tempo.note_onset(0.01)  # < MIN_IOI_S
        self.assertEqual(s._tempo._last_onset_time, 0.0)  # reference unchanged
        self.assertIsNone(s._tempo._ioi_ema)

    def test_long_gap_reanchors_without_polluting_tempo(self):
        s = _PrimedStream.primed(self.sid)
        s._tempo.note_onset(0.0)
        s._tempo.note_onset(0.5)  # establishes 120 BPM
        self.assertAlmostEqual(s._tempo.bpm, 120.0, delta=1.0)
        s._tempo.note_onset(5.0)  # > MAX_IOI_S — re-anchor, don't fold in
        self.assertEqual(s._tempo._last_onset_time, 5.0)
        self.assertAlmostEqual(s._tempo.bpm, 120.0, delta=1.0)  # estimate unchanged


class StreamLifecycleTest(unittest.TestCase):
    def test_start_stop_smoke_produces_features(self):
        s = SidFeatureStream(_make_sid(), song=0, system="NTSC")
        s.start()
        try:
            # Give the poll thread a moment to run a few PLAY ticks.
            time.sleep(0.1)
            feat = s.features()
            self.assertIsInstance(feat, MusicModulation)
        finally:
            s.stop()
        # A second start after stop is allowed (rebuilds the thread).
        s.start()
        s.stop()


if __name__ == "__main__":
    unittest.main()
