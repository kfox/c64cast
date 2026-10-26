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
        self.scene = SlideshowScene(MagicMock(), MagicMock(), tmp.name)
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


if __name__ == "__main__":
    unittest.main()
