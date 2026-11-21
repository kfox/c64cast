"""`video.aspect`: the screen aspect a source is fit to on each machine, and
the crop/fit/stretch primitives that fit it. Pure numpy/cv2."""

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from typing import cast
from unittest.mock import MagicMock

import cv2
import numpy as np
from _fakes import FakeAPI

from c64cast.app import scene_factory
from c64cast.app.config import Config, SceneCfg
from c64cast.hw.backend import TEENSYROM_PROFILE, ULTIMATE_PROFILE, C64Backend, HardwareProfile
from c64cast.scenes.scenes import SlideshowScene, WebcamScene
from c64cast.video.aspect import (
    apply_aspect,
    crop_to_aspect,
    fit_to_aspect,
    pixel_aspect,
    screen_aspect,
)
from c64cast.video.video import WebcamSource

SQUARE = 320 / 200


def _solid(h: int, w: int, value: int = 200) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def _profile(base: HardwareProfile, system: str, *, ultimate_64: bool = False) -> HardwareProfile:
    return dataclasses.replace(base, system=system, supports_system_mode=ultimate_64)


class PixelAspectTest(unittest.TestCase):
    def test_composite_follows_the_video_standard(self):
        self.assertEqual(pixel_aspect("composite", _profile(TEENSYROM_PROFILE, "NTSC")), 0.75)
        self.assertAlmostEqual(
            pixel_aspect("composite", _profile(TEENSYROM_PROFILE, "PAL")), 0.9365, places=4
        )

    def test_hdmi_follows_the_video_standard(self):
        u64 = _profile(ULTIMATE_PROFILE, "NTSC", ultimate_64=True)
        self.assertAlmostEqual(pixel_aspect("hdmi", u64), 5 / 6)
        self.assertAlmostEqual(
            pixel_aspect("hdmi", dataclasses.replace(u64, system="PAL")), 15 / 16
        )

    def test_auto_is_hdmi_on_an_ultimate_64(self):
        u64 = _profile(ULTIMATE_PROFILE, "NTSC", ultimate_64=True)
        self.assertEqual(pixel_aspect("auto", u64), pixel_aspect("hdmi", u64))

    def test_auto_is_composite_on_a_real_c64(self):
        for base in (TEENSYROM_PROFILE, ULTIMATE_PROFILE):
            for system in ("NTSC", "PAL"):
                with self.subTest(backend=base.name, system=system):
                    machine = _profile(base, system)
                    self.assertEqual(
                        pixel_aspect("auto", machine), pixel_aspect("composite", machine)
                    )

    def test_square_is_one_on_both_standards(self):
        for system in ("NTSC", "PAL"):
            self.assertEqual(pixel_aspect("square", _profile(TEENSYROM_PROFILE, system)), 1.0)

    def test_an_unknown_choice_raises(self):
        with self.assertRaises(KeyError):
            pixel_aspect("wide", _profile(TEENSYROM_PROFILE, "NTSC"))


class ScreenAspectTest(unittest.TestCase):
    def test_ntsc_composite_screen_is_six_by_five(self):
        self.assertAlmostEqual(screen_aspect("composite", _profile(TEENSYROM_PROFILE, "NTSC")), 1.2)

    def test_ntsc_ultimate_64_hdmi_screen_is_four_by_three(self):
        u64 = _profile(ULTIMATE_PROFILE, "NTSC", ultimate_64=True)
        self.assertAlmostEqual(screen_aspect("auto", u64), 4 / 3)

    def test_square_screen_is_the_pixel_grid(self):
        self.assertEqual(screen_aspect("square", _profile(TEENSYROM_PROFILE, "PAL")), SQUARE)

    def test_a_pillarboxed_4_3_picture_is_cropped_inside_its_bars_on_ntsc(self):
        # A 1280x720 file carrying a 960x720 picture between black bars: at
        # 1.6 the crop kept 96 px of each bar and squeezed the picture.
        frame = _solid(720, 1280, value=0)
        frame[:, 160:1120] = 200
        aspect = screen_aspect("composite", _profile(TEENSYROM_PROFILE, "NTSC"))
        out = crop_to_aspect(frame, aspect)
        self.assertEqual(out.shape[:2], (720, 864))
        self.assertTrue((out == 200).all())


class CropToAspectTest(unittest.TestCase):
    def test_wide_source_trims_width(self):
        out = crop_to_aspect(_solid(100, 320), SQUARE)  # ar 3.2 > 1.6
        self.assertEqual(out.shape[:2], (100, 160))

    def test_tall_source_trims_height(self):
        out = crop_to_aspect(_solid(100, 100), SQUARE)  # ar 1.0 < 1.6
        self.assertEqual(out.shape[:2], (62, 100))

    def test_already_at_aspect_is_unchanged(self):
        img = _solid(200, 320)
        self.assertIs(crop_to_aspect(img, SQUARE), img)


class FitToAspectTest(unittest.TestCase):
    def test_wide_source_pads_top_bottom(self):
        out = fit_to_aspect(_solid(100, 320), SQUARE)  # ar 3.2 > 1.6 → letterbox
        self.assertEqual(out.shape[:2], (200, 320))
        self.assertAlmostEqual(out.shape[1] / out.shape[0], SQUARE, places=2)

    def test_tall_source_pads_left_right(self):
        out = fit_to_aspect(_solid(100, 100), SQUARE)  # ar 1.0 < 1.6 → pillarbox
        self.assertEqual(out.shape[:2], (100, 160))
        self.assertAlmostEqual(out.shape[1] / out.shape[0], SQUARE, places=2)

    def test_pad_bars_are_black(self):
        out = fit_to_aspect(_solid(100, 320, value=255), SQUARE)
        self.assertTrue((out[0] == 0).all())
        self.assertTrue((out[out.shape[0] // 2] == 255).all())

    def test_already_at_aspect_is_unchanged(self):
        img = _solid(200, 320)
        self.assertIs(fit_to_aspect(img, SQUARE), img)


class ApplyAspectDispatchTest(unittest.TestCase):
    def test_crop_matches_crop_helper(self):
        img = _solid(100, 320)
        self.assertEqual(apply_aspect(img, "crop", 1.2).shape, crop_to_aspect(img, 1.2).shape)

    def test_fit_matches_fit_helper(self):
        img = _solid(100, 320)
        self.assertEqual(apply_aspect(img, "fit", 1.2).shape, fit_to_aspect(img, 1.2).shape)

    def test_stretch_is_identity(self):
        img = _solid(100, 100)
        self.assertIs(apply_aspect(img, "stretch", 1.2), img)


def _pillarboxed(height: int = 720, width: int = 1280, picture_width: int = 960) -> np.ndarray:
    """A 16:9 frame carrying a 4:3 picture between black bars."""
    frame = _solid(height, width, value=0)
    left = (width - picture_width) // 2
    frame[:, left : left + picture_width] = 200
    return frame


class SlideshowFitAspectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "frame.png")
        cv2.imwrite(self.path, _pillarboxed())

    def _slide(
        self, api: FakeAPI, *, pixel_aspect: str = "auto", aspect_mode: str = "crop"
    ) -> np.ndarray:
        mode = MagicMock(frame_target_size=(320, 200))
        scene = SlideshowScene(
            cast(C64Backend, api),
            mode,
            self.path,
            aspect_mode=aspect_mode,
            pixel_aspect=pixel_aspect,
        )
        img = scene._load_slide(self.path)
        assert img is not None
        return img

    def test_an_ntsc_c64_crops_the_picture_to_six_by_five_inside_its_bars(self):
        img = self._slide(FakeAPI())
        self.assertAlmostEqual(img.shape[1] / img.shape[0], 1.2, places=2)
        self.assertTrue((img == 200).all())

    def test_an_ntsc_ultimate_64_crops_to_four_by_three(self):
        img = self._slide(FakeAPI.ultimate())
        self.assertAlmostEqual(img.shape[1] / img.shape[0], 4 / 3, places=2)

    def test_square_keeps_the_old_crop(self):
        img = self._slide(FakeAPI(), pixel_aspect="square")
        self.assertAlmostEqual(img.shape[1] / img.shape[0], SQUARE, places=2)

    def test_fit_pads_to_the_screen_aspect(self):
        img = self._slide(FakeAPI(), aspect_mode="fit")
        self.assertAlmostEqual(img.shape[1] / img.shape[0], 1.2, places=2)
        self.assertTrue((img[0] == 0).all())


class WebcamFitAspectTest(unittest.TestCase):
    def test_an_ntsc_c64_crops_the_camera_frame_to_six_by_five(self):
        source = MagicMock()
        source.read.return_value = _pillarboxed()
        api = cast(C64Backend, FakeAPI())
        scene = WebcamScene(api, None, MagicMock(), source, MagicMock(), "cam")
        img = scene._read_frame()
        assert img is not None
        self.assertEqual(img.shape[:2], (720, 864))


class SceneFactoryPixelAspectTest(unittest.TestCase):
    """Every scene that fits a source picture carries `[hardware].pixel_aspect`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.cfg = Config()
        self.cfg.hardware.pixel_aspect = "square"
        self.api = cast(C64Backend, FakeAPI())
        self.camera = cast(WebcamSource, object())

    def test_webcam(self):
        s = SceneCfg(type="webcam", display="petscii")
        scene = scene_factory.build_scene(s, self.cfg, self.api, None, self.camera)
        self.assertEqual(scene.pixel_aspect, "square")

    def test_slideshow(self):
        path = os.path.join(self.dir, "a.png")
        cv2.imwrite(path, _solid(4, 4))
        s = SceneCfg(type="slideshow", file=path)
        scene = scene_factory.build_scene(s, self.cfg, self.api, None, None)
        self.assertEqual(scene.pixel_aspect, "square")

    def test_video(self):
        path = os.path.join(self.dir, "a.mp4")
        open(path, "wb").close()
        s = SceneCfg(type="video", file=path)
        scene = scene_factory.build_scene(s, self.cfg, self.api, None, None)
        self.assertEqual(scene.pixel_aspect, "square")

    def test_the_live_camera_fallback(self):
        self.cfg.scenes = []
        built = scene_factory.scenes_from_config(self.cfg, self.api, None, self.camera)
        self.assertEqual([scene.pixel_aspect for scene in built], ["square"])


if __name__ == "__main__":
    unittest.main()
