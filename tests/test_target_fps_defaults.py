"""Default target_fps for frame-pushing scenes (bitmap + digitized-audio caps).

Bitmap (hires/mhires) video / live-webcam / generative-mic scenes push a full
~9-10 KB frame per frame; each DMA write halts the C64 bus. When the 4-bit
$D418 digitized-audio DAC is *also* streaming, the combined halt load tears the
picture at the system rate, so those scenes default to 20 fps (both NTSC and
PAL); a bitmap scene without digitized audio defaults to half the system rate
(30 NTSC / 25 PAL). Char modes (petscii/blank) stay on the playlist system
default. Revisit when the firmware stops halting the bus on DMA writes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from typing import cast

from c64cast import config as cfgmod
from c64cast.config import _frame_push_default_fps
from c64cast.modes import DisplayMode

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeAPI  # noqa: E402


def _mode(is_bitmapped: bool) -> DisplayMode:
    """Minimal stand-in — the helper only reads `mode.is_bitmapped`."""
    return cast(DisplayMode, types.SimpleNamespace(is_bitmapped=is_bitmapped))


class FramePushDefaultFpsTest(unittest.TestCase):
    """The pure helper, exhaustively."""

    def test_char_mode_returns_none(self):
        # Char modes are cheap (1 KB delta-cached screen) → keep the system
        # default; the helper signals that with None.
        self.assertIsNone(_frame_push_default_fps(_mode(False), True, "NTSC"))
        self.assertIsNone(_frame_push_default_fps(_mode(False), False, "PAL"))

    def test_bitmap_with_digitized_audio_is_20_both_standards(self):
        self.assertEqual(_frame_push_default_fps(_mode(True), True, "NTSC"), 20.0)
        self.assertEqual(_frame_push_default_fps(_mode(True), True, "PAL"), 20.0)

    def test_bitmap_without_audio_is_half_system_rate(self):
        self.assertEqual(_frame_push_default_fps(_mode(True), False, "NTSC"), 30.0)
        self.assertEqual(_frame_push_default_fps(_mode(True), False, "PAL"), 25.0)

    def test_system_compare_is_case_insensitive(self):
        self.assertEqual(_frame_push_default_fps(_mode(True), False, "pal"), 25.0)
        self.assertEqual(_frame_push_default_fps(_mode(True), False, "ntsc"), 30.0)


class _BuildSceneFpsBase(unittest.TestCase):
    def setUp(self):
        from c64cast.api import Ultimate64API
        from c64cast.audio import AudioStreamer
        from c64cast.video import WebcamSource

        self.api = cast(Ultimate64API, FakeAPI())
        # The streamer is only stored on the scene here (setup() is never
        # called), so a bare sentinel is enough — matches the ensemble tests.
        self.audio = cast(AudioStreamer, object())
        self.source = cast(WebcamSource, object())

    def _cfg(self, system: str = "NTSC"):
        cfg = cfgmod.Config()
        cfg.ultimate64.system = system
        return cfg


class WebcamFpsDefaultTest(_BuildSceneFpsBase):
    def test_bitmap_with_audio_caps_at_20(self):
        # The default webcam display (hires_edges) is a bitmap mode.
        s = cfgmod.SceneCfg(type="webcam", display="hires_edges")
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, self.source)
        self.assertEqual(scene.target_fps, 20.0)

    def test_bitmap_with_audio_caps_at_20_on_pal(self):
        s = cfgmod.SceneCfg(type="webcam", display="mhires")
        scene = cfgmod.build_scene(s, self._cfg("PAL"), self.api, self.audio, self.source)
        self.assertEqual(scene.target_fps, 20.0)

    def test_bitmap_muted_falls_to_half_rate(self):
        # audio = None (global off) → no digitized DAC → half system rate.
        s = cfgmod.SceneCfg(type="webcam", display="hires")
        scene = cfgmod.build_scene(s, self._cfg(), self.api, None, self.source)
        self.assertEqual(scene.target_fps, 30.0)

    def test_bitmap_muted_half_rate_on_pal(self):
        s = cfgmod.SceneCfg(type="webcam", display="hires")
        scene = cfgmod.build_scene(s, self._cfg("PAL"), self.api, None, self.source)
        self.assertEqual(scene.target_fps, 25.0)

    def test_per_scene_audio_false_falls_to_half_rate(self):
        # `audio = false` opts a single bitmap scene out of the DAC even when
        # the global streamer is on → half rate, not 20.
        s = cfgmod.SceneCfg(type="webcam", display="mhires", audio=False)
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, self.source)
        self.assertEqual(scene.target_fps, 30.0)

    def test_char_mode_keeps_system_default(self):
        # petscii is a char mode — left on the playlist system default.
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, self.source)
        self.assertIsNone(scene.target_fps)

    def test_explicit_target_fps_wins(self):
        s = cfgmod.SceneCfg(type="webcam", display="hires_edges", target_fps=45.0)
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, self.source)
        self.assertEqual(scene.target_fps, 45.0)


class VideoFpsDefaultTest(_BuildSceneFpsBase):
    def setUp(self):
        super().setUp()
        fd, self.vid = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.vid)

    def test_bitmap_with_audio_caps_at_20(self):
        s = cfgmod.SceneCfg(type="video", display="mhires", file=self.vid)
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, None)
        self.assertEqual(scene.target_fps, 20.0)

    def test_bitmap_muted_falls_to_half_rate(self):
        s = cfgmod.SceneCfg(type="video", display="hires", file=self.vid, audio=False)
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, None)
        self.assertEqual(scene.target_fps, 30.0)

    def test_char_mode_keeps_system_default(self):
        s = cfgmod.SceneCfg(type="video", display="petscii", file=self.vid)
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, None)
        self.assertIsNone(scene.target_fps)


class GenerativeFpsDefaultTest(_BuildSceneFpsBase):
    def test_mic_source_bitmap_with_audio_caps_at_20(self):
        s = cfgmod.SceneCfg(
            type="generative", source="plasma", audio_source="mic", display="mhires"
        )
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, None)
        self.assertEqual(scene.target_fps, 20.0)

    def test_mic_source_bitmap_audio_off_falls_to_half_rate(self):
        # audio_source = mic is DAC-capable; with the global streamer off it
        # still gets the bitmap baseline (30/25), like a muted webcam.
        s = cfgmod.SceneCfg(type="generative", source="plasma", audio_source="mic", display="hires")
        scene = cfgmod.build_scene(s, self._cfg(), self.api, None, None)
        self.assertEqual(scene.target_fps, 30.0)

    def test_none_source_bitmap_keeps_system_default(self):
        # audio_source = none never drives the digitized DAC → not in scope.
        s = cfgmod.SceneCfg(
            type="generative", source="plasma", audio_source="none", display="mhires"
        )
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, None)
        self.assertIsNone(scene.target_fps)

    def test_mic_source_char_mode_keeps_system_default(self):
        s = cfgmod.SceneCfg(
            type="generative", source="plasma", audio_source="mic", display="petscii"
        )
        scene = cfgmod.build_scene(s, self._cfg(), self.api, self.audio, None)
        self.assertIsNone(scene.target_fps)


class InterleavedVideoFpsTest(_BuildSceneFpsBase):
    """Auto-interleaved videos are built directly (not via build_scene) with a
    bitmap hires_edges mode — they get the same cap."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        fd, _ = tempfile.mkstemp(suffix=".mp4", dir=self.tmpdir)
        os.close(fd)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _interleaved_videos(self, cfg, audio):
        import unittest.mock as mock

        from c64cast import video as videomod
        from c64cast.scenes import VideoScene

        cfg.playlist.interleave_videos = True
        cfg.playlist.videos_dir = self.tmpdir
        # Two non-video scenes so the playlist isn't in single-scene mode
        # (which skips interleaving entirely).
        cfg.scenes = [
            cfgmod.SceneCfg(type="blank", name="a"),
            cfgmod.SceneCfg(type="blank", name="b"),
        ]
        # Pretend PyAV is present so interleaving runs without the extra
        # (config imports _ensure_pyav from c64cast.video locally).
        with mock.patch.object(videomod, "_ensure_pyav", return_value=True):
            built = cfgmod.scenes_from_config(cfg, self.api, audio, None)
        return [s for s in built if isinstance(s, VideoScene)]

    def test_interleaved_video_with_audio_caps_at_20(self):
        videos = self._interleaved_videos(self._cfg(), self.audio)
        self.assertTrue(videos)
        for v in videos:
            self.assertEqual(v.target_fps, 20.0)

    def test_interleaved_video_muted_falls_to_half_rate(self):
        videos = self._interleaved_videos(self._cfg("PAL"), None)
        self.assertTrue(videos)
        for v in videos:
            self.assertEqual(v.target_fps, 25.0)


if __name__ == "__main__":
    unittest.main()
