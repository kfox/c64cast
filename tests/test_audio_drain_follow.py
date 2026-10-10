"""An audio file on the `$D418` DAC plays at its own speed and pitch however
far below its armed rate the sink drains (#675): `DrainFollower` measures the
drain off the sink's clock, and `AudioFileSource` resamples the track by it."""

from __future__ import annotations

import tempfile
import unittest
import wave
from types import SimpleNamespace
from typing import cast
from unittest import mock

import numpy as np

from c64cast.audio import audio_source
from c64cast.audio.audio_source import (
    DRAIN_FOLLOW_MIN,
    DRAIN_FOLLOW_RETUNE_S,
    DRAIN_FOLLOW_STALL_S,
    DRAIN_FOLLOW_WARMUP_S,
    DRAIN_FOLLOW_WINDOW_S,
    AudioFileSource,
    DrainFollower,
)
from c64cast.video.video import ensure_pyav

STEP_S = 0.1


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

    def test_the_sampler_is_not_followed(self):
        sink = _DrainingSink(self.now, 0.9)
        sink.is_sampler = True
        src = AudioFileSource(cast("audio_source.AudioStreamer", sink), self.wav, reactive=False)
        self.assertIsNone(src._new_drain_follower())


if __name__ == "__main__":
    unittest.main()
