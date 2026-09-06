"""Tests for SidFeatureStream — the host-side SID music-feature stream that
drives reactive generative visuals. The feature math (onset spike/decay, level,
gate-edge + retrigger onsets, tempo proxy, beat-phase integration) is exercised
by driving `_process_tick` directly with hand-built register snapshots — no
thread, no real chip. A small start()/stop() smoke covers the real poll path."""

from __future__ import annotations

import itertools
import time
import unittest
from unittest.mock import MagicMock, patch

from _fakes import make_psid, quiet_logging

from c64cast.hw.c64 import SID
from c64cast.scenes.modulation import MusicModulation
from c64cast.scenes.music_features import HostEmuBudget, SidFeatureStream


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
        self.sid = make_psid()

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


class CatchupBoundTest(unittest.TestCase):
    """The poll thread advances the host emulator to wall clock each wakeup.
    _MAX_CATCHUP_TICKS bounds the pass COUNT, and the tune sets what a pass
    costs and the rate the batch is sized against — so a count alone let an
    expensive tune keep this thread permanently busy. Mirrors the same bound
    in WaveformScene._poll_regs; both go through
    sid_host_emu.run_catchup_passes."""

    def setUp(self):
        self.sid = make_psid()

    def test_catchup_stops_at_half_a_poll_period(self):
        s = _PrimedStream.primed(self.sid)
        s._host_emu = MagicMock()
        s._host_emu.regs.return_value = _regs(gate=False)
        s._host_emu.retriggers.return_value = (False, False, False)
        s._sid_start_time = 1000.0
        s._ticks_done = 0
        clock = itertools.count(0.0, 0.005)  # 5 ms of host time per reading
        with (
            patch("c64cast.scenes.music_features.time.time", return_value=1100.0),
            patch("c64cast.sid.sid_host_emu.time.monotonic", side_effect=clock),
            self.assertLogs("c64cast.scenes.music_features", level="WARNING") as logs,
        ):
            s._poll_loop()
        # Half of the poll period is well under 10 ms, so the second pass ends
        # the batch — far short of the 120 the count alone would have allowed.
        self.assertEqual(s._host_emu.tick_play.call_count, 2)
        self.assertEqual(s._ticks_done, 2)
        self.assertIn("can't keep up", "\n".join(logs.output))

    def test_lag_is_reported_once_not_per_wakeup(self):
        s = _PrimedStream.primed(self.sid)
        s._host_emu = MagicMock()
        s._host_emu.regs.return_value = _regs(gate=False)
        s._host_emu.retriggers.return_value = (False, False, False)
        s._sid_start_time = 1000.0
        s._ticks_done = 0
        with (
            patch("c64cast.scenes.music_features.time.time", return_value=1100.0),
            patch(
                "c64cast.sid.sid_host_emu.time.monotonic", side_effect=itertools.count(0.0, 0.005)
            ),
            self.assertLogs("c64cast.scenes.music_features", level="WARNING") as logs,
        ):
            for _ in range(4):
                s._poll_loop()
        self.assertEqual(len(logs.output), 1)

    def test_rate_probe_stops_when_its_budget_is_spent(self):
        # The probe runs up to _RATE_PROBE_TICKS passes on a throwaway
        # emulator; the count is not a time bound, so it gets a budget too.
        s = SidFeatureStream(self.sid, song=0, system="NTSC")
        with (
            patch(
                "c64cast.scenes.music_features.HostEmuBudget", lambda *a, **kw: HostEmuBudget(0.0)
            ),
            patch("c64cast.scenes.music_features.SidHostEmu") as cls,
        ):
            cls.return_value.play_rate_hz.return_value = 60.0
            self.assertAlmostEqual(s._detect_play_rate_hz(), 60.0)
        cls.return_value.tick_play.assert_not_called()


class StreamLifecycleTest(unittest.TestCase):
    def test_start_stop_smoke_produces_features(self):
        s = SidFeatureStream(make_psid(), song=0, system="NTSC")
        # The poll thread warns if a catch-up batch runs out of time; on a
        # loaded worker that is possible and incidental here. CatchupBoundTest
        # is where that warning is asserted.
        self.enterContext(quiet_logging())
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
