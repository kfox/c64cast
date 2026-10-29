"""SlideshowScene's shuffle-and-walk picker, with cv2.imread faked so a
picture that does not decode needs no corrupt file on disk."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock
from unittest.mock import MagicMock

import numpy as np

from c64cast.scenes.scenes import SlideshowScene


class UndecodablePictureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.good = os.path.join(tmp.name, "good.png")
        self.bad = os.path.join(tmp.name, "bad.png")
        for path in (self.good, self.bad):
            open(path, "wb").close()
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


class LargePictureTest(unittest.TestCase):
    """A phone photo held at its decoded size made the display mode resize
    the same 12-megapixel still on every frame."""

    def test_a_large_picture_is_held_at_the_size_the_display_mode_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "photo.jpg")
            open(path, "wb").close()
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
