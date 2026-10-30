"""flicker_tear_ab writes configs c64cast accepts, with blending where it means it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_a_clip_path_outside_the_bmp_or_with_del_is_written_as_itself(self) -> None:
        tool = _load("flicker_tear_ab")
        for clip in ("/clips/\U0001f3b5 track.mkv", "/clips/a\x7fb.mkv"):
            with self.subTest(clip=clip):
                cfg_path = Path(tempfile.mkdtemp()) / "phase.toml"
                tool.write_config(cfg_path, clip, flicker=True)
                (scene,) = cfgmod.load(str(cfg_path)).scenes
                self.assertEqual(scene.file, clip)


class AudioTest(unittest.TestCase):
    """Audio plays by default, as it does for a user, and `--no-audio` mutes."""

    def setUp(self) -> None:
        self.tool = _load("flicker_tear_ab")
        self.out = Path(tempfile.mkdtemp())

    def _audio_enabled(self, **kwargs: bool) -> bool:
        cfg_path = self.out / "phase.toml"
        self.tool.write_config(cfg_path, self.out / "ab.mkv", flicker=True, **kwargs)
        return cfgmod.load(str(cfg_path)).audio.enabled

    def test_a_phase_plays_audio_by_default(self) -> None:
        self.assertTrue(self._audio_enabled())

    def test_a_muted_phase_does_not(self) -> None:
        self.assertFalse(self._audio_enabled(audio=False))

    def _ffmpeg_inputs(self, *, audio: bool) -> list[str]:
        with mock.patch.object(self.tool.subprocess, "run") as run:
            self.tool.build_test_video(self.out / "ab.mkv", audio=audio)
        argvs = [call.args[0] for call in run.call_args_list]
        self.assertEqual(argvs[-1][-1], str(self.out / "ab.mkv"))
        return [arg for argv in argvs for arg in argv]

    def test_the_video_carries_a_tone_when_audio_plays(self) -> None:
        self.assertIn(
            f"sine=frequency={self.tool.TONE_HZ}:duration=25", self._ffmpeg_inputs(audio=True)
        )

    def test_the_muted_video_carries_none(self) -> None:
        self.assertNotIn("lavfi", self._ffmpeg_inputs(audio=False))


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
