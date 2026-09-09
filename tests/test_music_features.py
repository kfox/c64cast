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
from c64cast.sid.sid_host_emu import UNMEASURED_PASS_COST_S, sustainable_poll_period_s


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

    def test_a_full_batch_that_used_its_whole_bound_still_warns(self):
        # One tick was due, it ran, and it outlasted the whole batch bound on
        # its own — invisible in the pass count, which is why the bound needs
        # to report it. Mirrors the same case in WaveformScene._poll_regs.
        s = _PrimedStream.primed(self.sid)
        s._host_emu = MagicMock()
        s._host_emu.regs.return_value = _regs(gate=False)
        s._host_emu.retriggers.return_value = (False, False, False)
        s._sid_start_time = 1000.0
        s._ticks_done = 0
        with (
            patch("c64cast.scenes.music_features.time.time", return_value=1000.0 + 1 / 60.0),
            patch("c64cast.sid.sid_host_emu.time.monotonic", side_effect=itertools.count(0.0, 0.5)),
            self.assertLogs("c64cast.scenes.music_features", level="WARNING") as logs,
        ):
            s._poll_loop()
        self.assertEqual(s._host_emu.tick_play.call_count, 1)
        self.assertEqual(s._ticks_done, 1, "the batch ran every pass it was asked for")
        self.assertIn("can't keep up", "\n".join(logs.output))

    def test_poll_period_is_stretched_when_one_pass_costs_more_than_the_rate_allows(self):
        # The wakeup period is floored so one measured PLAY pass fits inside
        # its allowed fraction; the per-tick song dt (which drives the onset
        # envelope decay) is not, or the features would track the thread
        # instead of the song.
        s = SidFeatureStream(self.sid, song=0, system="NTSC")
        with (
            patch.object(SidFeatureStream, "_detect_play_rate_hz", return_value=(60.0, 0.05)),
            patch("c64cast.scenes.music_features.PollThread") as poll_cls,
            self.assertLogs("c64cast.scenes.music_features", level="WARNING") as logs,
        ):
            s.start()
        self.assertAlmostEqual(s._poll_dt, 1.0 / 60.0)
        # A 50 ms pass may fill half a wakeup, so the wakeup becomes 100 ms.
        self.assertAlmostEqual(s._poll_period, 0.1)
        # ...and the thread has to actually wake at it.
        self.assertAlmostEqual(poll_cls.call_args.kwargs["period"], 0.1)
        self.assertIn("one PLAY pass costs", "\n".join(logs.output))

    def test_the_catchup_bound_is_sized_off_the_wakeup_period_not_the_tick_rate(self):
        # Mirrors WaveformScene: a stretched wakeup that the batch allowance
        # is not sized against leaves the allowance at its old, too-small
        # value. See sid_host_emu.sustainable_poll_period_s.
        s = _PrimedStream.primed(self.sid)
        s._host_emu = MagicMock()
        s._host_emu.regs.return_value = _regs(gate=False)
        s._host_emu.retriggers.return_value = (False, False, False)
        s._sid_start_time = 1000.0
        s._ticks_done = 0
        s._poll_period = 1.0
        with (
            patch("c64cast.scenes.music_features.time.time", return_value=1100.0),
            patch(
                "c64cast.sid.sid_host_emu.time.monotonic", side_effect=itertools.count(0.0, 0.005)
            ),
            self.assertLogs("c64cast.scenes.music_features", level="WARNING"),
        ):
            s._poll_loop()
        # Half of the 1/60 s tick dt is 8.3 ms — two passes, per the test
        # above. Half of the 1 s wakeup period is not.
        self.assertGreater(s._host_emu.tick_play.call_count, 2)

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
        # The probe runs up to RATE_PROBE_TICKS passes on a throwaway emulator;
        # the count is not a time bound, so it runs under the caller's budget —
        # the same one _prepare charges the persistent emulator's INIT to.
        s = SidFeatureStream(self.sid, song=0, system="NTSC")
        with patch("c64cast.scenes.music_features.SidHostEmu") as cls:
            cls.return_value.play_rate_hz.return_value = 60.0
            rate, pass_cost_s = s._detect_play_rate_hz(HostEmuBudget(0.0))
        self.assertAlmostEqual(rate, 60.0)
        cls.return_value.tick_play.assert_not_called()
        # Nothing ran, so nothing was measured — and that must not read as
        # "measured, and free". A budget already spent on this tune is evidence
        # the tune is expensive, which is the direction the sizing has to fail
        # in; see sustainable_poll_period_s.
        self.assertIsNone(pass_cost_s, "an unmeasured pass is not a free pass")
        self.assertGreater(
            sustainable_poll_period_s(1.0 / 400.0, pass_cost_s, 0.5),
            1.0 / 400.0,
            "an unmeasured pass must still floor the poll period",
        )

    def test_a_pass_too_quick_to_time_is_free_but_an_untimed_one_is_not(self):
        # The two readings that used to be one value. 0.0 back from a pass that
        # DID run means the host clock could not resolve it, and needs no
        # floor; None means no pass ran at all, and takes the worst-case charge.
        tick_dt_s = 1.0 / 400.0
        self.assertAlmostEqual(
            sustainable_poll_period_s(tick_dt_s, 0.0, 0.5), tick_dt_s, msg="measured as free"
        )
        self.assertAlmostEqual(
            sustainable_poll_period_s(tick_dt_s, None, 0.5),
            UNMEASURED_PASS_COST_S / 0.5,
            msg="never measured",
        )


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
