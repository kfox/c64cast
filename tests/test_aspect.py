"""`video.aspect`: the screen aspect a source is fit to on each machine, and
the crop/fit/stretch primitives that fit it. Pure numpy/cv2."""

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from typing import cast
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
from _fakes import FakeAPI

from c64cast.app import scene_factory
from c64cast.app.config import ColorCfg, Config, SceneCfg
from c64cast.hw.backend import TEENSYROM_PROFILE, ULTIMATE_PROFILE, C64Backend, HardwareProfile
from c64cast.scenes import scenes
from c64cast.scenes.scenes import SlideshowScene, WebcamScene
from c64cast.video.aspect import (
    crop_for_mode,
    crop_to_aspect,
    fit_to_aspect,
    kept_aspect,
    pad_for_mode,
    pixel_aspect,
    screen_aspect,
)
from c64cast.video.palette import ColorFitAccumulator
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


class AspectModeTest(unittest.TestCase):
    def test_crop_keeps_the_screens_aspect(self):
        self.assertEqual(kept_aspect(1920, 1080, "crop", 4 / 3), 4 / 3)

    def test_fit_and_stretch_keep_the_whole_source(self):
        img = _solid(90, 160)
        for mode in ("fit", "stretch"):
            with self.subTest(mode=mode):
                self.assertIs(crop_for_mode(img, mode, 4 / 3), img)

    def test_hybrid_takes_sixteen_by_nine_into_four_by_three_to_fourteen_by_nine(self):
        self.assertAlmostEqual(kept_aspect(1920, 1080, "hybrid", 4 / 3), 14 / 9)

    def test_hybrid_crops_a_tall_source_vertically_to_the_mean(self):
        out = crop_for_mode(_solid(1600, 900), "hybrid", 4 / 3)
        self.assertEqual(out.shape[1], 900)
        self.assertAlmostEqual(out.shape[1] / out.shape[0], (9 / 16 + 4 / 3) / 2, places=2)

    def test_fit_and_hybrid_pad_to_the_screen(self):
        img = _solid(90, 160)
        for mode in ("fit", "hybrid"):
            with self.subTest(mode=mode):
                out = pad_for_mode(crop_for_mode(img, mode, 1.2), mode, 1.2)
                self.assertAlmostEqual(out.shape[1] / out.shape[0], 1.2, places=2)
                self.assertTrue((out[0] == 0).all())

    def test_crop_and_stretch_do_not_pad(self):
        img = _solid(90, 160)
        for mode in ("crop", "stretch"):
            with self.subTest(mode=mode):
                self.assertIs(pad_for_mode(img, mode, 1.2), img)


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

    def _shown(self, aspect_mode: str, color: ColorCfg) -> tuple[SlideshowScene, np.ndarray]:
        """A 16:9 gray ramp with no black in it, shown on an NTSC C64, and
        the picture loaded from it before any padding."""
        path = os.path.join(os.path.dirname(self.path), "ramp.png")
        row = np.linspace(60, 160, 640).astype(np.uint8)
        cv2.imwrite(path, np.repeat(np.tile(row, (360, 1))[:, :, None], 3, axis=2))
        mode = MagicMock(frame_target_size=(320, 200))
        scene = SlideshowScene(
            cast(C64Backend, FakeAPI()), mode, path, aspect_mode=aspect_mode, color=color
        )
        img = scene._load_slide(path)
        assert img is not None
        scene._show_slide(path, img)
        return scene, img

    def test_fit_pads_to_the_screen_aspect(self):
        img = self._shown("fit", ColorCfg())[0]._current_img
        assert img is not None
        self.assertAlmostEqual(img.shape[1] / img.shape[0], 1.2, places=2)
        self.assertTrue((img[0] == 0).all())

    def test_the_bars_stay_out_of_the_slides_color_fit(self):
        for aspect_mode in ("fit", "hybrid"):
            with self.subTest(aspect_mode=aspect_mode):
                scene, picture = self._shown(aspect_mode, ColorCfg(auto_fit=True))
                expected = ColorFitAccumulator(strength=1.0)
                expected.add(picture)
                fit = cast(MagicMock, scene.display_mode).set_color_fit.call_args.args[0]
                self.assertEqual(fit, expected.result())

    def test_the_bars_stay_out_of_the_slides_color_map(self):
        with patch.object(scenes, "ColorMapAccumulator") as accumulator:
            _, picture = self._shown("fit", ColorCfg(force_palette=True))
        [sampled] = accumulator.return_value.add.call_args.args
        self.assertIs(sampled, picture)

    def test_the_bars_stay_out_of_the_slides_hardware_palette(self):
        scene, picture = self._shown("fit", ColorCfg())
        scene._hw_palette = MagicMock()
        with patch.object(scenes, "_push_source_palette") as push:
            scene._push_slide_palette()
        [sampled] = push.call_args.args[2]
        self.assertIs(sampled, picture)


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

    def test_video_carries_its_aspect_mode(self):
        path = os.path.join(self.dir, "a.mp4")
        open(path, "wb").close()
        s = SceneCfg(type="video", file=path, aspect_mode="hybrid")
        scene = scene_factory.build_scene(s, self.cfg, self.api, None, None)
        self.assertEqual(scene.aspect_mode, "hybrid")

    def test_video_and_slideshow_fit_unless_told_otherwise(self):
        video = os.path.join(self.dir, "a.mp4")
        open(video, "wb").close()
        picture = os.path.join(self.dir, "a.png")
        cv2.imwrite(picture, _solid(4, 4))
        for scene_type, path in (("video", video), ("slideshow", picture)):
            with self.subTest(scene_type=scene_type):
                s = SceneCfg(type=scene_type, file=path)
                scene = scene_factory.build_scene(s, self.cfg, self.api, None, None)
                self.assertEqual(scene.aspect_mode, "fit")

    def test_the_live_camera_fallback(self):
        self.cfg.scenes = []
        built = scene_factory.scenes_from_config(self.cfg, self.api, None, self.camera)
        self.assertEqual([scene.pixel_aspect for scene in built], ["square"])


if __name__ == "__main__":
    unittest.main()
