"""An audio file on the `$D418` DAC plays at its own speed and pitch however
far below its armed rate the sink drains (#675): `DrainFollower` measures the
drain off the sink's clock, and `AudioFileSource` resamples the track by it.
The analyzer is told the resampled rate, so its bands still read the track's
own frequencies."""

from __future__ import annotations

import tempfile
import unittest
import wave
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest import mock

import numpy as np

from c64cast.audio import audio_source
from c64cast.audio.audio_features import (
    AnalysisTap,
    AudioFeatureAnalyzer,
    AudioFeatureStream,
    band_edges,
    rescaled_band_edges,
)
from c64cast.audio.audio_source import (
    DRAIN_FOLLOW_MIN,
    DRAIN_FOLLOW_RETUNE_S,
    DRAIN_FOLLOW_STALL_S,
    DRAIN_FOLLOW_WARMUP_S,
    DRAIN_FOLLOW_WINDOW_S,
    DRAIN_PREDICT_SPAN_S,
    AudioFileSource,
    DrainFollower,
)
from c64cast.hw.api import Ultimate64API
from c64cast.hw.backend import (
    SYSTEM_MODE_CATEGORY,
    ULTIMATE_64_HALT_CYCLES_PER_BYTE,
    ULTIMATE_PROFILE,
)
from c64cast.hw.c64 import CLOCK_NTSC
from c64cast.video.video import ensure_pyav

STEP_S = 0.1
# An Ultimate 64 as refine_capabilities leaves it, and its halt, NTSC.
U64_PROFILE = replace(ULTIMATE_PROFILE, halt_cycles_per_byte=ULTIMATE_64_HALT_CYCLES_PER_BYTE)
HALT_S_PER_BYTE = 1.27 / CLOCK_NTSC
# The byte rate a generative halo scene writes in mcm, and the drain it
# predicts there: 1 - 48 KiB/s x 1.27 cycles/B / 1.0227 MHz.
HALO_BPS = 48 * 1024
HALO_DRAIN = 1.0 - HALO_BPS * HALT_S_PER_BYTE


def _feed(
    follower: DrainFollower,
    drain: float,
    seconds: float,
    *,
    start: tuple[float, float] = (0.0, 0.01),
    trust: object = 0,
) -> tuple[list[float], tuple[float, float]]:
    """Readings every STEP_S of a clock advancing at ``drain`` of the wall;
    returns the retunes and the last (wall, clock)."""
    now, clock = start
    retunes = []
    for _ in range(int(round(seconds / STEP_S))):
        now += STEP_S
        clock += drain * STEP_S
        got = follower.observe(now, clock, trust)
        if got is not None:
            retunes.append(got)
    return retunes, (now, clock)


class DrainFollowerTest(unittest.TestCase):
    def test_follows_a_steady_drain_after_warmup_and_one_window(self):
        f = DrainFollower()
        early, last = _feed(f, 0.94, DRAIN_FOLLOW_WARMUP_S + 0.7 * DRAIN_FOLLOW_WINDOW_S)
        self.assertEqual(early, [])
        retunes, _ = _feed(f, 0.94, 0.2 * DRAIN_FOLLOW_WINDOW_S, start=last)
        self.assertEqual(len(retunes), 1)
        self.assertAlmostEqual(retunes[0], 0.94, places=3)
        self.assertAlmostEqual(f.scale, 0.94, places=3)

    def test_a_drain_inside_the_deadband_is_left_alone(self):
        f = DrainFollower(0.94)
        retunes, _ = _feed(f, 0.948, 20.0)
        self.assertEqual(retunes, [])

    def test_a_drain_just_past_the_deadband_is_followed(self):
        f = DrainFollower(0.94)
        retunes, _ = _feed(f, 0.952, DRAIN_FOLLOW_WARMUP_S + DRAIN_FOLLOW_WINDOW_S)
        self.assertEqual(len(retunes), 1)
        self.assertAlmostEqual(retunes[0], 0.952, places=3)

    def test_no_reading_before_the_consumer_starts_counts(self):
        f = DrainFollower()
        for i in range(200):
            self.assertIsNone(f.observe(i * STEP_S, 0.0, 0))
        # The warm-up runs from the clock's start, not the first reading.
        early, _ = _feed(f, 0.9, DRAIN_FOLLOW_WARMUP_S, start=(20.0, 0.01))
        self.assertEqual(early, [])

    def test_a_stalled_clock_restarts_the_window(self):
        # A link stall holds the clock: across it the drain reads far low,
        # and the window that holds it is not the drain.
        f = DrainFollower()
        _, (now, clock) = _feed(f, 1.0, DRAIN_FOLLOW_WARMUP_S + 0.5 * DRAIN_FOLLOW_WINDOW_S)
        now += 2.0 * DRAIN_FOLLOW_STALL_S + 1.0
        self.assertIsNone(f.observe(now, clock, 0))
        retunes, _ = _feed(f, 1.0, 0.7 * DRAIN_FOLLOW_WINDOW_S, start=(now, clock))
        self.assertEqual(retunes, [])
        self.assertEqual(f.scale, 1.0)

    def test_a_stall_read_at_the_put_timeout_restarts_the_window(self):
        # A push into a stalled sink returns after QUEUE_PUT_TIMEOUT_S, so the
        # readings across a stall come closer together than DRAIN_FOLLOW_STALL_S.
        from c64cast.audio.audio_handlers import QUEUE_PUT_TIMEOUT_S

        self.assertLess(QUEUE_PUT_TIMEOUT_S, DRAIN_FOLLOW_STALL_S)
        f = DrainFollower()
        _, (now, clock) = _feed(f, 1.0, DRAIN_FOLLOW_WARMUP_S + 0.5 * DRAIN_FOLLOW_WINDOW_S)
        for _ in range(5):
            now += QUEUE_PUT_TIMEOUT_S
            self.assertIsNone(f.observe(now, clock, 0))
        retunes, _ = _feed(f, 1.0, 0.7 * DRAIN_FOLLOW_WINDOW_S, start=(now, clock))
        self.assertEqual(retunes, [])
        self.assertEqual(f.scale, 1.0)

    def test_an_underrun_restarts_the_window(self):
        f = DrainFollower()
        _, last = _feed(f, 0.9, DRAIN_FOLLOW_WARMUP_S + 0.5 * DRAIN_FOLLOW_WINDOW_S)
        retunes, _ = _feed(f, 0.9, 0.7 * DRAIN_FOLLOW_WINDOW_S, start=last, trust=1)
        self.assertEqual(retunes, [])

    def test_a_window_after_an_underrun_waits_out_a_warmup(self):
        # The catch-up after an underrun reads as a drain that is not there.
        f = DrainFollower()
        _, last = _feed(f, 1.0, DRAIN_FOLLOW_WARMUP_S + 0.5 * DRAIN_FOLLOW_WINDOW_S)
        retunes, last = _feed(f, 0.9, DRAIN_FOLLOW_WARMUP_S + 0.5, start=last, trust=1)
        self.assertEqual(retunes, [])
        retunes, _ = _feed(f, 0.9, 0.75 * DRAIN_FOLLOW_WINDOW_S, start=last, trust=1)
        self.assertEqual(len(retunes), 1)

    def test_the_scale_is_bounded(self):
        f = DrainFollower()
        retunes, _ = _feed(f, 0.5, DRAIN_FOLLOW_WARMUP_S + DRAIN_FOLLOW_WINDOW_S)
        self.assertEqual(retunes, [DRAIN_FOLLOW_MIN])
        f = DrainFollower(0.9)
        retunes, _ = _feed(f, 1.2, DRAIN_FOLLOW_WARMUP_S + DRAIN_FOLLOW_WINDOW_S)
        self.assertEqual(retunes, [1.0])

    def test_retunes_are_spaced(self):
        f = DrainFollower()
        _, last = _feed(f, 0.9, DRAIN_FOLLOW_WARMUP_S + DRAIN_FOLLOW_WINDOW_S)
        self.assertAlmostEqual(f.scale, 0.9, places=3)
        retunes, _ = _feed(f, 0.8, 0.5 * DRAIN_FOLLOW_RETUNE_S, start=last)
        self.assertEqual(retunes, [])


def _feed_bytes(
    follower: DrainFollower, drain: float, seconds: float, byte_rate: float | None
) -> list[tuple[float, float]]:
    """`_feed` with the link writing ``byte_rate`` B/s; returns the retunes
    as (wall since the clock started, scale)."""
    now, clock = 0.0, 0.01
    retunes = []
    for _ in range(int(round(seconds / STEP_S))):
        now += STEP_S
        clock += drain * STEP_S
        halted = None if byte_rate is None else byte_rate * now * HALT_S_PER_BYTE
        got = follower.observe(now, clock, 0, halted)
        if got is not None:
            retunes.append((now, got))
    return retunes


class DrainPredictionTest(unittest.TestCase):
    def test_the_first_scale_is_the_byte_rate_s_prediction_after_one_span(self):
        follower = DrainFollower(predict=True)
        retunes = _feed_bytes(follower, 0.94, 2.0, HALO_BPS)
        self.assertEqual(len(retunes), 1)
        when, scale = retunes[0]
        self.assertAlmostEqual(scale, 0.9390, places=3)
        self.assertAlmostEqual(scale, HALO_DRAIN, places=4)
        self.assertLessEqual(when, DRAIN_PREDICT_SPAN_S + 2 * STEP_S)
        self.assertFalse(follower.measured)

    def test_the_window_then_trims_the_prediction(self):
        follower = DrainFollower(predict=True)
        retunes = _feed_bytes(follower, 0.90, 12.0, HALO_BPS)
        self.assertEqual([round(s, 3) for _, s in retunes], [round(HALO_DRAIN, 3), 0.9])
        self.assertTrue(follower.measured)

    def test_a_quiet_link_predicts_no_change(self):
        follower = DrainFollower(predict=True)
        self.assertEqual(_feed_bytes(follower, 1.0, 12.0, 2000.0), [])

    def test_no_halt_count_or_no_prediction_waits_for_the_window(self):
        for follower, byte_rate in (
            (DrainFollower(predict=True), None),
            (DrainFollower(), HALO_BPS),
        ):
            retunes = _feed_bytes(follower, 0.94, 12.0, byte_rate)
            self.assertGreaterEqual(retunes[0][0], DRAIN_FOLLOW_WARMUP_S)
            self.assertTrue(follower.measured)

    def test_it_predicts_once(self):
        follower = DrainFollower(predict=True)
        _feed_bytes(follower, 0.94, 2.0, HALO_BPS)
        follower.scale = 1.0
        self.assertEqual(_feed_bytes(follower, 0.94, 2.0, HALO_BPS), [])


class UltimateHaltFigureTest(unittest.TestCase):
    """Only a device read as an Ultimate 64 gets its halt figure. The II+ (no
    System Mode category) has none measured, and an unprobed or unreadable
    device predicts nothing rather than borrow it."""

    def setUp(self) -> None:
        patcher = mock.patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = Ultimate64API("http://example.invalid")
        get = mock.patch.object(self.api.session, "get").start()
        self.addCleanup(mock.patch.stopall)
        get.return_value.raise_for_status.return_value = None
        get.return_value.status_code = 200
        self.get = get

    def _refine(self, categories: list[str]) -> None:
        self.get.return_value.json.return_value = {"categories": categories, "errors": []}
        with self.assertLogs("c64cast.hw.api", "DEBUG"):
            self.api.refine_capabilities()

    def test_an_ultimate_64_gets_its_halt_figure(self):
        self._refine([SYSTEM_MODE_CATEGORY, "C64 and Cartridge Settings"])
        self.assertEqual(self.api.profile.halt_cycles_per_byte, 1.27)

    def test_an_ultimate_ii_plus_has_none(self):
        self.api.profile = U64_PROFILE
        self._refine(["Audio Output Settings", "C64 and Cartridge Settings"])
        self.assertEqual(self.api.profile.halt_cycles_per_byte, 0.0)

    def test_an_unprobed_or_unreadable_device_has_none(self):
        self.assertEqual(self.api.profile.halt_cycles_per_byte, 0.0)
        self.get.return_value.json.side_effect = ValueError("not json")
        with self.assertLogs("c64cast.hw.api", "DEBUG"):
            self.api.refine_capabilities()
        self.assertEqual(self.api.profile.halt_cycles_per_byte, 0.0)


class _DrainingSink:
    """A DAC sink that drains at ``drain`` of its effective rate: each push
    blocks for as long as the sink takes to play it, as a full queue does,
    and its clock counts every sample pushed as played."""

    is_sampler = False
    sample_rate = 8000
    effective_rate = 8000.0
    analysis_sink = None
    content_lag_seconds = 0.0

    def __init__(self, now: list[float], drain: float) -> None:
        self._now = now
        self.drain = drain
        self.pushed = 0

    def push_samples(self, arr: np.ndarray) -> int:
        self.pushed += int(arr.size)
        self._now[0] += arr.size / (self.effective_rate * self.drain)
        return int(arr.size)

    def end_input(self) -> None:
        pass

    def position_seconds(self) -> float:
        return self.pushed / self.effective_rate

    def stats(self) -> dict[str, int | float]:
        return {"full_underruns": 0, "partial_underruns": 0}


class _Api:
    delivery_epoch = 0


class _LinkApi:
    """A link whose running byte count ``written()`` reads, on an Ultimate
    64 profile."""

    delivery_epoch = 0
    profile = U64_PROFILE

    def __init__(self, written: Callable[[], int]) -> None:
        self._written = written

    @property
    def stats(self) -> dict[str, int]:
        return {"bytes": self._written()}


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class AudioFileSourceDrainTest(unittest.TestCase):
    SECONDS = 20.0

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wav = f"{tmp.name}/tone.wav"
        rate = 8000
        t = np.arange(int(self.SECONDS * rate)) / rate
        pcm = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
        with wave.open(self.wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
        self.now = [1000.0]
        patcher = mock.patch.object(
            audio_source, "time", SimpleNamespace(monotonic=lambda: self.now[0])
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _play(self, src: AudioFileSource, sink: _DrainingSink) -> float:
        """Decode the whole track into ``sink``; returns the wall it took."""
        start = self.now[0]
        with self.assertLogs("c64cast.audio.audio_source", "INFO") as logs:
            src._decode_loop()
        self.logs = logs.output
        return self.now[0] - start

    def test_a_slow_drain_plays_the_track_in_real_time(self):
        sink = _DrainingSink(self.now, 0.9)
        src = AudioFileSource(cast("audio_source.AudioStreamer", sink), self.wav, reactive=False)
        wall = self._play(src, sink)
        self.assertAlmostEqual(src._drain_scale, 0.9, places=2)
        self.assertTrue(any("drains at 0.900" in line for line in self.logs), self.logs)
        # Unfollowed, the 20 s track took 20 / 0.9 = 22.2 s. Followed after
        # the warm-up and a window, only those first seconds play slow.
        lead = DRAIN_FOLLOW_WARMUP_S + DRAIN_FOLLOW_WINDOW_S
        self.assertLess(wall, self.SECONDS + lead * (1 / 0.9 - 1) + 0.5)

        # The next activation starts from the drain followed: real time
        # from its first sample.
        sink.pushed = 0
        wall = self._play(src, sink)
        self.assertAlmostEqual(wall, self.SECONDS, delta=0.1)

    def test_a_full_drain_resamples_nothing(self):
        sink = _DrainingSink(self.now, 1.0)
        src = AudioFileSource(cast("audio_source.AudioStreamer", sink), self.wav, reactive=False)
        wall = self._play(src, sink)
        self.assertEqual(src._drain_scale, 1.0)
        self.assertAlmostEqual(wall, self.SECONDS, delta=0.1)

    def test_a_lost_write_restarts_the_window(self):
        sink = _DrainingSink(self.now, 0.9)
        api = _Api()
        sink.api = api  # type: ignore[attr-defined]
        src = AudioFileSource(cast("audio_source.AudioStreamer", sink), self.wav, reactive=False)
        follower = src._new_drain_follower()
        assert follower is not None
        for _ in range(int((DRAIN_FOLLOW_WARMUP_S + DRAIN_FOLLOW_WINDOW_S) / STEP_S)):
            self.now[0] += STEP_S
            sink.pushed += int(0.9 * STEP_S * sink.effective_rate)
            api.delivery_epoch += 1
            self.assertIsNone(src._observe_drain(follower, 8000))
        self.assertEqual(follower.scale, 1.0)

    def test_the_analyzer_is_told_each_rate_the_track_is_resampled_to(self):
        sink = _DrainingSink(self.now, 0.9)
        src = AudioFileSource(cast("audio_source.AudioStreamer", sink), self.wav, reactive=False)
        features = mock.Mock()
        src._features = features
        self._play(src, sink)
        self.assertEqual(
            features.set_content_rate.call_args_list, [mock.call(8000), mock.call(7200)]
        )

        features.reset_mock()
        sink.pushed = 0
        self._play(src, sink)
        self.assertEqual(features.set_content_rate.call_args_list, [mock.call(7200)])

    def test_the_link_s_writes_set_the_first_scale_within_a_span(self):
        sink = _DrainingSink(self.now, 0.9)
        start = self.now[0]
        # A link writing what halts the CPU for 10 % of its cycles.
        rate = 0.1 / HALT_S_PER_BYTE
        reads: list[float] = []

        def written() -> int:
            reads.append(self.now[0] - start)
            return int((self.now[0] - start) * rate)

        sink.api = _LinkApi(written)  # type: ignore[attr-defined]
        src = AudioFileSource(cast("audio_source.AudioStreamer", sink), self.wav, reactive=False)
        wall = self._play(src, sink)
        self.assertIn("predict the DAC drains at 0.900", self.logs[0])
        # Only the first span plays slow, not the warm-up and a window.
        self.assertLess(wall, self.SECONDS + DRAIN_PREDICT_SPAN_S * (1 / 0.9 - 1) + 0.2)
        self.assertTrue(src._drain_measured)
        # The link's counters are read only until the prediction.
        self.assertLess(max(reads), DRAIN_PREDICT_SPAN_S * (1 / 0.9) + 0.2)

        follower = src._new_drain_follower()
        assert follower is not None
        self.assertTrue(follower._predicted)

    def test_an_unmeasured_link_predicts_nothing(self):
        sink = _DrainingSink(self.now, 1.0)
        sink.api = _LinkApi(lambda: 1000)  # type: ignore[attr-defined]
        src = AudioFileSource(cast("audio_source.AudioStreamer", sink), self.wav, reactive=False)
        self.assertAlmostEqual(src._halted_s() or 0.0, 1000 * 1.27 / CLOCK_NTSC)
        sink.api.profile = replace(U64_PROFILE, halt_cycles_per_byte=0.0)  # type: ignore[attr-defined]
        self.assertIsNone(src._halted_s())
        follower = src._new_drain_follower()
        assert follower is not None
        self.assertTrue(follower._predicted)

    def test_the_sampler_is_not_followed(self):
        sink = _DrainingSink(self.now, 0.9)
        sink.is_sampler = True
        src = AudioFileSource(cast("audio_source.AudioStreamer", sink), self.wav, reactive=False)
        self.assertIsNone(src._new_drain_follower())


REF_RATE = 12000.0
FFT = 1024
BANDS = 8


def _loudest_band(analyzer: AudioFeatureAnalyzer, tone_hz: float, sampled_at: float) -> int:
    t = np.arange(FFT) / sampled_at
    window = (0.05 * np.sin(2 * np.pi * tone_hz * t)).astype(np.float32)
    return int(np.argmax(analyzer._update_bands(window)))


class AnalyzerContentRateTest(unittest.TestCase):
    """A track followed at 0.94 reaches the tap at 0.94 of the sink's rate.
    A tone two bins under a band's top edge at the sink's rate sits 6 % higher
    in the resampled window's bins, past that edge."""

    SCALE = 0.94

    def setUp(self) -> None:
        edges = band_edges(BANDS, FFT)
        self.band = BANDS - 2
        self.tone_hz = (edges[self.band + 1] - 2) * REF_RATE / FFT

    def test_a_tone_reads_in_its_own_band_at_the_rate_it_was_resampled_to(self):
        analyzer = AudioFeatureAnalyzer(REF_RATE, n_bands=BANDS, fft_size=FFT)
        self.assertEqual(_loudest_band(analyzer, self.tone_hz, REF_RATE), self.band)
        followed = REF_RATE * self.SCALE
        self.assertEqual(_loudest_band(analyzer, self.tone_hz, followed), self.band + 1)
        analyzer.set_content_rate(followed)
        self.assertEqual(_loudest_band(analyzer, self.tone_hz, followed), self.band)

    def test_rescaled_bands_still_tile_dc_to_nyquist(self):
        for bands, ratio in ((BANDS, 1 / 0.8), (BANDS, 0.8), (FFT // 2 - 1, 1.25)):
            edges = rescaled_band_edges(bands, FFT, ratio)
            self.assertEqual(len(edges), bands + 1)
            self.assertGreaterEqual(edges[0], 1)
            self.assertEqual(edges[-1], FFT // 2)
            self.assertTrue(np.all(np.diff(edges) >= 1), (bands, ratio))

    def test_a_rate_change_waits_for_the_heard_window_to_reach_it(self):
        tap = AnalysisTap(size=8 * FFT)
        played = [0.0]
        stream = AudioFeatureStream(
            tap, REF_RATE, n_bands=BANDS, fft_size=FFT, play_position=lambda: played[0]
        )
        tap.push(np.zeros(4 * FFT, dtype=np.float32))
        stream.set_content_rate(REF_RATE * self.SCALE)
        tap.push(np.zeros(2 * FFT, dtype=np.float32))
        analyzer = stream._analyzer

        played[0] = 4 * FFT
        stream._process_tick()
        np.testing.assert_array_equal(analyzer._edges, band_edges(BANDS, FFT))

        played[0] = 4 * FFT + 1
        stream._process_tick()
        np.testing.assert_array_equal(
            analyzer._edges, rescaled_band_edges(BANDS, FFT, 1 / self.SCALE)
        )


if __name__ == "__main__":
    unittest.main()
