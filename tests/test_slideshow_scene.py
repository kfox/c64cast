"""SlideshowScene's shuffle-and-walk picker, with cv2.imread faked so a
picture that does not decode needs no corrupt file on disk."""

from __future__ import annotations

import os
import struct
import tempfile
import threading
import unittest
from unittest import mock
from unittest.mock import MagicMock

import cv2
import numpy as np

from c64cast.scenes import scenes
from c64cast.scenes.scenes import SlideshowScene


def _write_png_header(path: str, width: int, height: int) -> None:
    """A PNG signature and IHDR declaring `width` x `height`, and nothing
    after it: enough to be sized, never enough to decode."""
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + ihdr)


class UndecodablePictureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.good = os.path.join(tmp.name, "good.png")
        self.bad = os.path.join(tmp.name, "bad.png")
        for path in (self.good, self.bad):
            _write_png_header(path, 4, 4)
        self.scene = SlideshowScene(MagicMock(), MagicMock(frame_target_size=(160, 200)), tmp.name)
        img = np.zeros((4, 4, 3), dtype=np.uint8)
        imread = mock.patch(
            "c64cast.scenes.scenes.cv2.imread",
            side_effect=lambda path, _flags: None if path == self.bad else img,
        )
        imread.start()
        self.addCleanup(imread.stop)

    def test_a_bag_that_ends_on_an_undecodable_picture_reshuffles_the_rest(self):
        self.scene._current_path = self.good
        self.scene._shuffle_bag = [self.bad]
        with self.assertLogs("c64cast.scenes.scenes", "INFO"):
            self.scene._advance_image()
        self.assertFalse(self.scene.is_done)
        self.assertEqual(self.scene._current_path, self.good)

    def test_a_pool_with_nothing_decodable_ends_the_scene(self):
        os.remove(self.good)
        self.scene._shuffle_bag = []
        with self.assertLogs("c64cast.scenes.scenes", "WARNING"):
            self.scene._advance_image()
        self.assertTrue(self.scene.is_done)

    def test_a_pool_with_nothing_decodable_records_why_it_ended(self):
        os.remove(self.good)
        with self.assertLogs("c64cast.scenes.scenes", "WARNING"):
            self.scene.setup()
        self.assertTrue(self.scene.is_done)
        self.assertIn("decode", self.scene.setup_failure or "")


class OversizedPictureTest(unittest.TestCase):
    """A full-role web console user can upload a few-megabyte PNG into a
    slideshow's directory that declares a raster OpenCV would decode to
    gigabytes on the render thread."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.huge = os.path.join(tmp.name, "huge.png")
        _write_png_header(self.huge, 30000, 30000)
        self.scene = SlideshowScene(MagicMock(), MagicMock(frame_target_size=(160, 200)), tmp.name)
        imread = mock.patch(
            "c64cast.scenes.scenes.cv2.imread", return_value=np.zeros((4, 4, 3), dtype=np.uint8)
        )
        self.imread = imread.start()
        self.addCleanup(imread.stop)

    def test_a_picture_past_the_pixel_cap_is_never_decoded(self):
        with self.assertLogs("c64cast.scenes.scenes", "WARNING") as logs:
            self.scene._advance_image()
        self.imread.assert_not_called()
        self.assertTrue(self.scene.is_done)
        self.assertIn("30000x30000", "\n".join(logs.output))

    def test_a_picture_at_the_pixel_cap_is_decoded(self):
        _write_png_header(self.huge, scenes.MAX_PICTURE_PIXELS // 4, 4)
        with self.assertLogs("c64cast.scenes.scenes", "INFO"):
            self.scene._advance_image()
        self.imread.assert_called_once()
        self.assertFalse(self.scene.is_done)

    def test_a_file_that_is_not_a_picture_is_never_decoded(self):
        with open(self.huge, "wb") as f:
            f.write(b"II*\x00" + bytes(64))
        with self.assertLogs("c64cast.scenes.scenes", "WARNING"):
            self.scene._advance_image()
        self.imread.assert_not_called()

    def test_a_picture_removed_after_the_pool_was_resolved_is_skipped(self):
        self.scene._shuffle_bag = [self.huge + ".gone"]
        with self.assertLogs("c64cast.scenes.scenes", "WARNING") as logs:
            self.scene._advance_image()
        self.assertIn("cannot read", "\n".join(logs.output))


class SlideOffTheFramePathTest(unittest.TestCase):
    """A slide advance used to re-resolve the whole file spec and decode the
    next picture at full size, both on the render thread."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            cv2.imwrite(os.path.join(tmp.name, name), np.zeros((3024, 4032, 3), dtype=np.uint8))
        display_mode = MagicMock(frame_target_size=(160, 200))
        self.scene = SlideshowScene(MagicMock(), display_mode, tmp.name)
        self.decodes: list[tuple[str, int]] = []
        real_imread = cv2.imread

        def imread(path, flags):
            self.decodes.append((threading.current_thread().name, flags))
            return real_imread(path, flags)

        patches = (
            mock.patch("c64cast.scenes.scenes.cv2.imread", side_effect=imread),
            mock.patch("c64cast.scenes.scenes._render_with_overlays"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _play_slides(self, slides: int) -> None:
        with self.assertLogs("c64cast.scenes.scenes", "INFO"):
            self.scene.setup()
            self.addCleanup(self.scene.teardown)
            for _ in range(slides - 1):
                due = self.scene._image_start + self.scene.image_duration_s
                self.assertTrue(self.scene.process_frame(due))

    def test_the_file_spec_is_resolved_once_per_setup(self):
        with mock.patch.object(
            SlideshowScene,
            "_resolve_candidates",
            autospec=True,
            wraps=SlideshowScene._resolve_candidates,
        ) as resolve:
            self._play_slides(5)
        self.assertEqual(resolve.call_count, 1)

    def test_slides_after_the_first_decode_off_the_render_thread(self):
        self._play_slides(4)
        render_thread = threading.current_thread().name
        self.assertEqual(self.decodes[0][0], render_thread)
        self.assertNotIn(render_thread, [thread for thread, _flags in self.decodes[1:]])

    def test_a_slide_decoded_ahead_goes_back_in_the_bag_at_teardown(self):
        self._play_slides(1)
        assert self.scene._upcoming is not None
        upcoming = self.scene._upcoming[0]
        self.scene.teardown()
        self.assertEqual(self.scene._shuffle_bag[0], upcoming)

    def test_a_large_jpeg_decodes_at_the_coarsest_scale_the_mode_can_use(self):
        self._play_slides(1)
        self.assertEqual(self.decodes[0][1], cv2.IMREAD_REDUCED_COLOR_4)
        assert self.scene._current_img is not None
        self.assertEqual(self.scene._current_img.shape, (400, 640, 3))


class ReducedDecodeTest(unittest.TestCase):
    def test_a_large_png_is_averaged_down_not_sampled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stripes = np.zeros((2560, 4096, 3), dtype=np.uint8)
        stripes[:, 3::8] = 255
        stripes[:, 4::8] = 255
        path = os.path.join(tmp.name, "stripes.png")
        cv2.imwrite(path, stripes)
        scene = SlideshowScene(MagicMock(), MagicMock(frame_target_size=(160, 200)), tmp.name)
        img = scene._load_slide(path)
        assert img is not None
        self.assertAlmostEqual(float(img.mean()), 255 * 2 / 8, delta=1.0)


class LargePictureTest(unittest.TestCase):
    """A phone photo held at its decoded size made the display mode resize
    the same 12-megapixel still on every frame."""

    def test_a_large_picture_is_held_at_the_size_the_display_mode_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "photo.jpg")
            _write_png_header(path, 4032, 3024)
            display_mode = MagicMock()
            display_mode.frame_target_size = (160, 200)
            scene = SlideshowScene(MagicMock(), display_mode, path)
            photo = np.zeros((3024, 4032, 3), dtype=np.uint8)
            with (
                mock.patch("c64cast.scenes.scenes.cv2.imread", return_value=photo),
                self.assertLogs("c64cast.scenes.scenes", "INFO"),
            ):
                scene._advance_image()
        assert scene._current_img is not None
        self.assertEqual(scene._current_img.shape, (400, 640, 3))


if __name__ == "__main__":
    unittest.main()
