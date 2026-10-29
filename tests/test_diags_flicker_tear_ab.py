"""flicker_tear_ab writes configs c64cast accepts, with blending where it means it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from _fakes import MachineSettingsIsolation
from test_diags_nmi_latch import _load

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory

_settings_isolation = MachineSettingsIsolation()


def setUpModule() -> None:
    _settings_isolation.start()


def tearDownModule() -> None:
    _settings_isolation.stop()


class PhaseConfigTest(unittest.TestCase):
    def _resolved_tolerance(self, *, flicker: bool) -> str:
        tool = _load("flicker_tear_ab")
        out = Path(tempfile.mkdtemp())
        cfg_path = out / "phase.toml"
        tool.write_config(cfg_path, out / "ab.mkv", flicker=flicker)
        cfg = cfgmod.load(str(cfg_path))
        for validate in scene_factory.PER_SYSTEM_VALIDATORS:
            validate(cfg)
        (scene,) = cfg.scenes
        assert scene.display is not None
        return scene_factory.resolve_flicker_tolerance(
            cfgmod.scene_color(cfg, scene).flicker_tolerance, scene.display
        )

    def test_the_flicker_phase_arms_blending(self) -> None:
        self.assertNotEqual(self._resolved_tolerance(flicker=True), "off")

    def test_the_plain_phase_leaves_it_off(self) -> None:
        self.assertEqual(self._resolved_tolerance(flicker=False), "off")

    def test_a_windows_clip_path_is_written_as_itself(self) -> None:
        tool = _load("flicker_tear_ab")
        cfg_path = Path(tempfile.mkdtemp()) / "phase.toml"
        clip = r"C:\Users\runner\AppData\Local\Temp\ab.mkv"
        tool.write_config(cfg_path, clip, flicker=True)
        (scene,) = cfgmod.load(str(cfg_path)).scenes
        self.assertEqual(scene.file, clip)


def _captured(picture: np.ndarray) -> np.ndarray:
    """A C64 picture as the capture device returns it: scaled up inside a
    black border."""
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[60:660, 160:1120] = cv2.resize(picture, (960, 600), interpolation=cv2.INTER_NEAREST)
    return frame


class PositionContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load("flicker_tear_ab")

    def _states(self, picture: np.ndarray) -> list[str]:
        return self.tool._position_row_states(_captured(picture))

    def test_frames_two_apart_differ(self) -> None:
        a, b = self.tool.position_state(1), self.tool.position_state(3)
        self.assertFalse(np.array_equal(a, b))

    def test_a_whole_frame_is_not_torn(self) -> None:
        frames = [_captured(self.tool.position_state(i)) for i in range(4)]
        pct, torn, blank, _ = self.tool.score(frames, self.tool._position_row_states)
        self.assertEqual((pct, torn, blank), (0.0, [], 0))
        self.assertEqual(
            {s for s in self._states(self.tool.position_state(2)) if s != "blank"}, {"2"}
        )

    def test_frames_two_apart_in_one_picture_read_as_two_apart(self) -> None:
        picture = self.tool.position_state(1).copy()
        picture[:96] = self.tool.position_state(3)[:96]
        frames = [_captured(picture)]
        _, torn, _, _ = self.tool.score(frames, self.tool._position_row_states)
        self.assertEqual(torn, [0])
        self.assertEqual(self.tool.seam_kind(self._states(picture)), "two-apart")

    def test_a_ragged_seam_between_frames_two_apart_reads_as_two_apart(self) -> None:
        # What a write caught by the beam leaves: rows lighting both bands.
        picture = self.tool.position_state(1).copy()
        picture[:96] = self.tool.position_state(3)[:96]
        picture[96:120] |= self.tool.position_state(3)[96:120]
        self.assertEqual(self.tool.seam_kind(self._states(picture)), "two-apart")

    def test_consecutive_frames_in_one_picture_read_as_adjacent(self) -> None:
        picture = self.tool.position_state(1).copy()
        picture[:96] = self.tool.position_state(2)[:96]
        self.assertEqual(self.tool.seam_kind(self._states(picture)), "adjacent")

    def test_a_cleared_band_reads_as_partial(self) -> None:
        picture = self.tool.position_state(1).copy()
        picture[:96] = self.tool.position_state(1)[:96] * 0
        picture[:96, :8] = self.tool.position_state(1)[:96, :8]
        picture[:96, -8:] = self.tool.position_state(1)[:96, -8:]
        self.assertEqual(self.tool.seam_kind(self._states(picture)), "partial")


if __name__ == "__main__":
    unittest.main()
