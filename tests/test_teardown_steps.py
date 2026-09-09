"""`run_teardown_steps` — the guarded step runner every scene teardown uses.

A scene's teardown steps are independent promises to the next scene, not a
transaction. This module pins the property those scenes rely on: a step that
raises does not starve the steps after it — for the runner itself, and for each
`scenes.py` teardown built on it whose subject has no test module of its own.

The three SID scenes are covered where they live (`test_asid_scene.py`,
`test_midi_scene.py`, `test_waveform.py`), because each has other reasons to
build a scene.
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from typing import cast
from unittest.mock import MagicMock

from c64cast.scenes.scenes import (
    LauncherScene,
    SourceScene,
    VideoScene,
    WebcamScene,
    run_teardown_steps,
)
from c64cast.video.rolling_palette import RollingForcePalette

log = logging.getLogger("c64cast.tests.teardown_steps")

_SCENES_LOG = "c64cast.scenes.scenes"


class _WedgedPalette:
    """A rolling force_palette whose `stop()` raises, as a real one can: it
    joins a worker that touches the DMA link."""

    def stop(self) -> None:
        raise RuntimeError("palette worker wedged")


def _wedged_palette() -> RollingForcePalette:
    return cast(RollingForcePalette, _WedgedPalette())


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
