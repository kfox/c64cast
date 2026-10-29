"""The `[debug].frame_numbers` label on the live-frame scenes counts from 1 on
every entry, matching the timecode beside it."""

from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import MagicMock

import numpy as np

from c64cast.scenes.scenes import SourceScene, WebcamScene


def _frame() -> np.ndarray:
    return np.zeros((200, 320, 3), dtype=np.uint8)


class FrameNumbersRestartPerEntryTest(unittest.TestCase):
    def setUp(self):
        self.labels: list[str] = []
        patches = (
            mock.patch(
                "c64cast.scenes.scenes._annotate_frame_number",
                side_effect=lambda img, label: self.labels.append(label) or img,
            ),
            mock.patch("c64cast.scenes.scenes._render_with_overlays"),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _play_two_laps(self, scene: WebcamScene | SourceScene) -> None:
        scene.show_frame_numbers = True
        scene.duration_s = 60.0
        for frames in (3, 1):
            scene.setup()
            for _ in range(frames):
                scene.process_frame(scene.start_time)
            scene.teardown()

    def _webcam(self) -> WebcamScene:
        source = MagicMock()
        source.read.return_value = _frame()
        return WebcamScene(MagicMock(), None, MagicMock(), source, MagicMock(), "cam")

    def _source(self) -> SourceScene:
        source = MagicMock(finished=False)
        source.read.return_value = _frame()
        audio_source = MagicMock(finished=False, resets_display=False)
        audio_source.features.return_value = None
        return SourceScene(MagicMock(), None, MagicMock(), source, audio_source, "gen")

    def test_a_webcam_scene_counts_frames_from_one_on_its_second_lap(self):
        self._play_two_laps(self._webcam())
        self.assertTrue(self.labels[-1].endswith(" f1"), self.labels)

    def test_a_source_scene_counts_frames_from_one_on_its_second_lap(self):
        self._play_two_laps(self._source())
        self.assertTrue(self.labels[-1].endswith(" f1"), self.labels)


if __name__ == "__main__":
    unittest.main()
