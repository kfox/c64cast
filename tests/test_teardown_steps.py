"""`run_teardown_steps` — the guarded step runner every scene teardown uses.

A scene's teardown steps are independent promises to the next scene, not a
transaction. This module pins the property those scenes rely on: a step that
raises does not starve the steps after it — for the runner itself, and for each
teardown built on it whose subject has no test module of its own (`scenes.py`'s
four, plus the two live audio sources under `SourceScene`).

The subjects that do have one are covered where they live: the three SID scenes
in `test_asid_scene.py`, `test_midi_scene.py` and `test_waveform.py`, and
`SidFileAudioSource` in `test_audio_source_sid.py`, because each has other
reasons to build its subject.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
import unittest
import wave
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

from c64cast._teardown import run_teardown_steps
from c64cast.audio.audio_source import AudioFileSource, MicAudioSource
from c64cast.scenes.scenes import (
    LauncherScene,
    SourceScene,
    VideoScene,
    WebcamScene,
)
from c64cast.video.rolling_palette import RollingForcePalette
from c64cast.video.video import ensure_pyav

if TYPE_CHECKING:
    from c64cast.audio.audio_features import AudioFeatureStream

log = logging.getLogger("c64cast.tests.teardown_steps")

_SCENES_LOG = "c64cast.scenes.scenes"
_SOURCES_LOG = "c64cast.audio.audio_source"


class _WedgedPalette:
    """A rolling force_palette whose `stop()` raises, as a real one can: it
    joins a worker that touches the DMA link."""

    def stop(self) -> None:
        raise RuntimeError("palette worker wedged")


def _wedged_palette() -> RollingForcePalette:
    return cast(RollingForcePalette, _WedgedPalette())


class _WedgedFeatures:
    """An `AudioFeatureStream` whose `stop()` raises the `RuntimeError` its
    `PollThread` gives for a join of the current thread."""

    def stop(self) -> None:
        raise RuntimeError("cannot join current thread")


def _wedged_features() -> AudioFeatureStream:
    return cast("AudioFeatureStream", _WedgedFeatures())


def _boom() -> None:
    raise RuntimeError("step failed")


class RunTeardownStepsTests(unittest.TestCase):
    def test_every_step_runs_in_order(self):
        ran: list[str] = []
        run_teardown_steps(
            log,
            "Scene",
            [("first", lambda: ran.append("first")), ("second", lambda: ran.append("second"))],
        )
        self.assertEqual(ran, ["first", "second"])

    def test_a_failing_step_does_not_starve_the_steps_after_it(self):
        ran: list[str] = []
        with self.assertLogs(log, level="ERROR"):
            run_teardown_steps(
                log,
                "Scene",
                [
                    ("silence", _boom),
                    ("display restore", lambda: ran.append("display restore")),
                    ("flush", lambda: ran.append("flush")),
                ],
            )
        self.assertEqual(ran, ["display restore", "flush"])

    def test_every_step_can_fail_without_stopping_the_run(self):
        with self.assertLogs(log, level="ERROR") as caught:
            run_teardown_steps(log, "Scene", [("a", _boom), ("b", _boom), ("c", _boom)])
        self.assertEqual(len(caught.records), 3)

    def test_the_failing_step_is_named_in_the_log(self):
        with self.assertLogs(log, level="ERROR") as caught:
            run_teardown_steps(log, "AsidScene", [("kernal IRQ restore", _boom)])
        self.assertIn("AsidScene", caught.output[0])
        self.assertIn("kernal IRQ restore", caught.output[0])
        self.assertIn("RuntimeError", caught.output[0])  # exc_info is attached

    def test_an_interrupt_is_not_swallowed(self):
        # Teardown runs on the shutdown path. Catching Exception (not
        # BaseException) is what keeps a KeyboardInterrupt from being logged as
        # a failed step and then discarded, which would hang the shutdown.
        def interrupt() -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            run_teardown_steps(log, "Scene", [("interrupted", interrupt)])


class SceneTeardownTests(unittest.TestCase):
    """The three `scenes.py` teardowns that sequenced independent guarantees.

    Each fails the step the old code put *first* and asserts the guarantee
    behind it still ran — which is the whole difference the runner makes, since
    `safe_teardown` swallows the raise and the failure is otherwise silent.
    """

    def test_a_failing_palette_stop_does_not_starve_the_webcam_audio_stop(self):
        audio = MagicMock()
        scene = WebcamScene(MagicMock(), audio, MagicMock(), MagicMock(), MagicMock(), "cam")
        scene._rolling_fp = _wedged_palette()
        with self.assertLogs(_SCENES_LOG, level="ERROR"):
            scene.teardown()
        self.assertTrue(audio.stop.called, "the next scene inherits a streaming audio pump")
        self.assertIsNone(scene._rolling_fp, "a dead worker is still referenced")

    def test_a_failing_palette_stop_does_not_starve_the_source_teardowns(self):
        source, audio_source = MagicMock(), MagicMock()
        scene = SourceScene(MagicMock(), MagicMock(), MagicMock(), source, audio_source, "gen")
        scene._rolling_fp = _wedged_palette()
        with self.assertLogs(_SCENES_LOG, level="ERROR"):
            scene.teardown()
        self.assertTrue(audio_source.teardown.called)
        self.assertTrue(source.teardown.called, "the capture handle leaks for the rest of the run")

    def test_a_failing_poll_stop_does_not_starve_the_launcher_reset(self):
        # The reset is mandatory for a `.crt` — `run_crt` leaves it active — and
        # `PollThread.stop` joins, which `_pollthread` documents as able to raise
        # RuntimeError on a target that stopped its own poller.
        with tempfile.TemporaryDirectory() as tmp:
            prg = os.path.join(tmp, "demo.prg")
            with open(prg, "wb") as f:
                f.write(b"\x01\x08")
            api = MagicMock()
            scene = LauncherScene(api, prg)
            scene._poll.stop = MagicMock(  # type: ignore[method-assign]
                side_effect=RuntimeError("cannot join current thread")
            )
            with self.assertLogs(_SCENES_LOG, level="ERROR"):
                scene.teardown()
        self.assertTrue(api.reset.called, "a launched .crt stays active into the next scene")

    def test_a_failing_border_restore_does_not_starve_the_video_guarantees(self):
        # The most-used scene type, and the first thing after the self-guarding
        # base teardown is a $D020 write over the link -- so it fails like any
        # other DMA op, and used to take the three guarantees behind it down.
        with tempfile.TemporaryDirectory() as tmp:
            clip = os.path.join(tmp, "clip.mp4")
            with open(clip, "wb") as f:
                f.write(b"\x00" * 16)
            audio = MagicMock()
            scene = VideoScene(MagicMock(), audio, MagicMock(), clip)
            source = MagicMock()
            scene.source = source
            scene._last_osd_shown = "12:00"
            scene.transport.set_record_border = MagicMock(  # type: ignore[method-assign]
                side_effect=RuntimeError("DMA link down")
            )
            with self.assertLogs(_SCENES_LOG, level="ERROR"):
                scene.teardown()
        self.assertTrue(source.close.called, "the PyAV handle leaks for the rest of the run")
        self.assertTrue(audio.stop.called, "the next scene inherits a streaming audio pump")
        self.assertIsNone(scene._last_osd_shown, "lap 2 suppresses its first OSD repaint")

    def test_the_av_lag_summary_reads_the_clock_before_the_audio_stops(self):
        """The summary's `clock/wall` gauge divides by a clock the audio stop
        zeroes.

        `AudioStreamer.stop()` clears its pushed-sample count and
        `UltimateAudioSampler.position_seconds` short-circuits to 0.0 once
        stopped, so a summary logged *after* the audio-stop step reports
        `clock/wall=0.0000` for every audible video scene. That reading is the
        only one at `-v` (the live line is DEBUG), and
        `scripts/diags/mhires_tempo_clock_ab.py` parses it as `clock_final`, so
        a constant zero silently breaks the tempo calibration instrument.
        """
        with tempfile.TemporaryDirectory() as tmp:
            clip = os.path.join(tmp, "clip.mp4")
            with open(clip, "wb") as f:
                f.write(b"\x00" * 16)
            audio = MagicMock()
            audio.sample_rate = 8000
            audio.position_seconds.return_value = 12.0
            # What the real streamer does on stop: the position collapses.
            audio.stop.side_effect = lambda: setattr(audio.position_seconds, "return_value", 0.0)
            scene = VideoScene(MagicMock(), audio, MagicMock(), clip)
            scene.wall_start_time = time.time() - 12.0
            scene._av_lag_count = 1
            scene._av_lag_min = 0.001
            scene._av_lag_max = 0.002
            scene._av_lag_sum = 0.001
            scene._av_buf_min = 3
            with self.assertLogs(_SCENES_LOG, level="INFO") as caught:
                scene.teardown()
        # Match the emitted prefix, not the step label: the runner's own
        # failure line is `teardown step 'A/V lag summary' failed`, so a filter
        # on the label alone stays green when the summary raises and the gauge
        # is gone entirely.
        summaries = [line for line in caught.output if "video A/V lag summary:" in line]
        self.assertEqual(len(summaries), 1, caught.output)
        gauge = re.search(r"clock/wall=([0-9.]+)", summaries[0])
        self.assertIsNotNone(gauge, summaries[0])
        assert gauge is not None  # for the type checker
        self.assertGreater(float(gauge.group(1)), 0.0, summaries[0])


class AudioSourceTeardownTests(unittest.TestCase):
    """The two live `audio_source.py` teardowns that sequenced independent
    guarantees.

    Both end in the audio stop, which is what keeps the next scene from
    inheriting a streaming pump — and both put a thread join in front of it.
    """

    def test_a_failing_feature_stop_does_not_starve_the_mic_audio_stop(self):
        # The raise is injected, not reproduced: `AudioFeatureStream.stop` is a
        # `PollThread.stop`, which raises only the join-current-thread
        # `RuntimeError` that `_pollthread` makes its lifecycle lock reentrant
        # to produce — and nothing tears a mic source down from inside the
        # analyzer's own tick. So this pins the runner's property at this site
        # rather than a live defect; the file and SID sources next to it are
        # reproductions.
        audio = MagicMock()
        source = MicAudioSource(audio, MagicMock())
        source._features = _wedged_features()
        with self.assertLogs(_SOURCES_LOG, level="ERROR"):
            source.teardown()
        self.assertTrue(audio.stop.called, "the next scene inherits a streaming audio pump")
        self.assertIsNone(source._features, "a dead analyzer is still referenced")

    def _file_source(self, audio, **kw) -> AudioFileSource:
        """An `AudioFileSource` over a throwaway wav, probed for real.

        The wav has to outlive construction: `setup()` re-resolves the spec and
        probes again, so a directory torn down in between fails the pick rather
        than the thread start this is aiming at.
        """
        tune = os.path.join(tempfile.mkdtemp(), "tune.wav")
        with wave.open(tune, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)
        return AudioFileSource(audio, tune, **kw)

    @unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
    def test_a_decode_thread_that_cannot_start_is_never_published(self):
        """A host out of threads must leave nothing for teardown to join.

        `Thread.join` raises `RuntimeError` on a thread that was never started,
        so publishing the thread before starting it put an unjoinable object
        where `teardown` reaches for one. Both backend orderings are pinned
        because they differ in what is already running when the start fails: on
        the DAC path `start_for_external_source()` has run, so a raise escaping
        teardown's first step would strand a live pump.
        """
        for is_sampler in (False, True):
            with self.subTest(sampler=is_sampler):
                audio = MagicMock(is_sampler=is_sampler)
                source = self._file_source(audio, reactive=False)
                with patch.object(
                    threading.Thread, "start", side_effect=RuntimeError("can't start new thread")
                ):
                    with self.assertRaises(RuntimeError):
                        source.setup()
                self.assertIsNone(source._thread, "teardown can reach an unstarted thread")
                source.teardown()  # no ERROR to catch: there is no join to fail
                self.assertTrue(audio.stop.called)

    @unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
    def test_a_scene_whose_audio_source_cannot_start_still_stops_the_pump(self):
        """The reachability half, driven rather than argued.

        `SourceScene.setup` catches an audio source that fails to start, logs,
        and flips `is_done` so the playlist advances and tears the scene down —
        which is the only way a half-set-up file source is ever torn down. The
        DAC ordering is the one that matters: `start_for_external_source()` runs
        before the decode thread, so the pump is live by the time the start
        fails, and stopping it is the scene's promise to whatever plays next.
        """
        audio = MagicMock(is_sampler=False)
        source = self._file_source(audio, reactive=False)
        scene = SourceScene(MagicMock(), audio, MagicMock(), MagicMock(), source, "tune")
        with patch.object(
            threading.Thread, "start", side_effect=RuntimeError("can't start new thread")
        ):
            with self.assertLogs(_SCENES_LOG, level="ERROR"):
                scene.setup()
        self.assertTrue(scene.is_done, "the playlist never advances, so teardown never runs")
        self.assertTrue(audio.start_for_external_source.called, "no pump was ever started")
        audio.stop.reset_mock()
        scene.teardown()
        self.assertTrue(audio.stop.called, "the next scene inherits a streaming audio pump")
