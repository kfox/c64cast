"""Aspect-handling helpers shared by the frame-bearing scenes.

`_crop_to_aspect` (fill), `_fit_to_aspect` (contain), and the `_apply_aspect`
dispatcher that selects between them + stretch for the slideshow `aspect_mode`
field. Pure numpy/cv2 — no hardware, no scene wiring needed.
"""

from __future__ import annotations

import unittest

import numpy as np

from c64cast.scenes import _C64_ASPECT, _apply_aspect, _crop_to_aspect, _fit_to_aspect


def _solid(h: int, w: int, value: int = 200) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


class CropToAspectTest(unittest.TestCase):
    def test_wide_source_trims_width(self):
        out = _crop_to_aspect(_solid(100, 320))  # ar 3.2 > 1.6
        self.assertEqual(out.shape[:2], (100, 160))

    def test_tall_source_trims_height(self):
        out = _crop_to_aspect(_solid(100, 100))  # ar 1.0 < 1.6
        self.assertEqual(out.shape[:2], (62, 100))

    def test_already_at_aspect_is_unchanged(self):
        img = _solid(200, 320)
        self.assertIs(_crop_to_aspect(img), img)


class FitToAspectTest(unittest.TestCase):
    def test_wide_source_pads_top_bottom(self):
        out = _fit_to_aspect(_solid(100, 320))  # ar 3.2 > 1.6 → letterbox
        self.assertEqual(out.shape[:2], (200, 320))
        # No source pixels lost: full source width preserved.
        self.assertAlmostEqual(out.shape[1] / out.shape[0], _C64_ASPECT, places=2)

    def test_tall_source_pads_left_right(self):
        out = _fit_to_aspect(_solid(100, 100))  # ar 1.0 < 1.6 → pillarbox
        self.assertEqual(out.shape[:2], (100, 160))
        self.assertAlmostEqual(out.shape[1] / out.shape[0], _C64_ASPECT, places=2)

    def test_pad_bars_are_black(self):
        out = _fit_to_aspect(_solid(100, 320, value=255))  # bright content
        # Top row is pad → black; a center row is content → bright.
        self.assertTrue((out[0] == 0).all())
        self.assertTrue((out[out.shape[0] // 2] == 255).all())

    def test_already_at_aspect_is_unchanged(self):
        img = _solid(200, 320)
        self.assertIs(_fit_to_aspect(img), img)


class ApplyAspectDispatchTest(unittest.TestCase):
    def test_crop_default_matches_crop_helper(self):
        img = _solid(100, 320)
        self.assertEqual(_apply_aspect(img).shape, _crop_to_aspect(img).shape)
        self.assertEqual(_apply_aspect(img, "crop").shape, _crop_to_aspect(img).shape)

    def test_fit_matches_fit_helper(self):
        img = _solid(100, 320)
        self.assertEqual(_apply_aspect(img, "fit").shape, _fit_to_aspect(img).shape)

    def test_stretch_is_identity(self):
        img = _solid(100, 100)
        self.assertIs(_apply_aspect(img, "stretch"), img)


if __name__ == "__main__":
    unittest.main()
