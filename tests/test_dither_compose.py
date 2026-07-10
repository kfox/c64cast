"""Integration tests: [color].dither wired into mhires/mcm/hires compose().

Complements tests/test_dither.py (which tests the dither.py primitives in
isolation) by driving each display mode's real compose() with dithering
enabled, checking it produces correctly-shaped, in-range buffers and that
"ordered"/"floyd-steinberg" actually change the quantized output relative to
"none" on a smooth gradient (where dithering has visible work to do).
"""

# FakeAPI is a duck-typed stub of Ultimate64API; silence pyright's
# argument-type complaints across the file (same pattern as
# test_bitmap_compose.py / test_mcm_mode.py).
# pyright: reportArgumentType=false
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from c64cast.modes import HiresDisplayMode, MCMDisplayMode, MultiHiresDisplayMode  # noqa: E402


def _gradient(h: int = 240, w: int = 320) -> np.ndarray:
    """A smooth BGR gradient — quantization bands visibly without dither,
    so it's a good probe for "did dithering actually change anything"."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    b = xx / w * 255
    g = yy / h * 255
    r = (xx + yy) / (h + w) * 255
    return np.clip(np.stack([b, g, r], axis=-1), 0, 255).astype(np.uint8)


class MultiHiresDitherTest(unittest.TestCase):
    def test_ordered_produces_valid_buffers(self):
        mode = MultiHiresDisplayMode("percell", dither_method="ordered", dither_strength=0.5)
        buffers = mode.compose(_gradient())
        self.assertEqual(len(buffers["bitmap"]), 8000)
        self.assertEqual(len(buffers["screen"]), 1000)
        self.assertEqual(len(buffers["color"]), 1000)
        self.assertTrue(0 <= buffers["bg"] <= 15)

    def test_floyd_steinberg_produces_valid_buffers(self):
        mode = MultiHiresDisplayMode(
            "percell", dither_method="floyd-steinberg", dither_strength=0.5
        )
        buffers = mode.compose(_gradient())
        self.assertEqual(len(buffers["bitmap"]), 8000)
        self.assertEqual(len(buffers["screen"]), 1000)
        self.assertEqual(len(buffers["color"]), 1000)

    def test_atkinson_produces_valid_buffers(self):
        mode = MultiHiresDisplayMode("percell", dither_method="atkinson", dither_strength=0.5)
        buffers = mode.compose(_gradient())
        self.assertEqual(len(buffers["bitmap"]), 8000)

    def test_ordered_changes_bitmap_vs_none_on_gradient(self):
        frame = _gradient()
        plain = MultiHiresDisplayMode("percell", dither_method="none").compose(frame)
        dithered = MultiHiresDisplayMode(
            "percell", dither_method="ordered", dither_strength=1.0
        ).compose(frame)
        self.assertFalse(np.array_equal(plain["bitmap"], dithered["bitmap"]))

    def test_floyd_steinberg_changes_bitmap_vs_none_on_gradient(self):
        frame = _gradient()
        plain = MultiHiresDisplayMode("percell", dither_method="none").compose(frame)
        dithered = MultiHiresDisplayMode(
            "percell", dither_method="floyd-steinberg", dither_strength=1.0
        ).compose(frame)
        self.assertFalse(np.array_equal(plain["bitmap"], dithered["bitmap"]))

    def test_global_palette_mode_still_gets_ordered_dither(self):
        # _compose_global (cheap/vivid/grayscale) has no per-cell candidate
        # structure for FS/Atkinson, but ordered perturbs `flat` upstream of
        # either compose path, so it applies there too.
        frame = _gradient()
        plain = MultiHiresDisplayMode("cheap", dither_method="none").compose(frame)
        dithered = MultiHiresDisplayMode(
            "cheap", dither_method="ordered", dither_strength=1.0
        ).compose(frame)
        self.assertFalse(np.array_equal(plain["bitmap"], dithered["bitmap"]))


class MCMDitherTest(unittest.TestCase):
    def test_ordered_produces_valid_buffers(self):
        mode = MCMDisplayMode(dither_method="ordered", dither_strength=0.5)
        buffers = mode.compose(_gradient())
        self.assertEqual(len(buffers["screen"]), 1000)
        self.assertEqual(len(buffers["color"]), 1000)
        self.assertTrue(bool(((buffers["color"] & 0x08) == 0x08).all()))  # multicolor bit set

    def test_floyd_steinberg_produces_valid_buffers(self):
        mode = MCMDisplayMode(dither_method="floyd-steinberg", dither_strength=0.5)
        buffers = mode.compose(_gradient())
        self.assertEqual(len(buffers["screen"]), 1000)
        self.assertEqual(len(buffers["color"]), 1000)
        self.assertTrue(bool(((buffers["color"] & 0x08) == 0x08).all()))

    def test_floyd_steinberg_changes_screen_vs_none_on_gradient(self):
        frame = _gradient()
        plain = MCMDisplayMode(dither_method="none").compose(frame)
        dithered = MCMDisplayMode(dither_method="floyd-steinberg", dither_strength=1.0).compose(
            frame
        )
        self.assertFalse(np.array_equal(plain["screen"], dithered["screen"]))


class HiresDitherTest(unittest.TestCase):
    def test_ordered_produces_valid_buffers(self):
        mode = HiresDisplayMode("normal", dither_method="ordered", dither_strength=0.5)
        buffers = mode.compose(_gradient())
        self.assertEqual(len(buffers["bitmap"]), 8000)
        self.assertEqual(len(buffers["screen"]), 1000)

    def test_floyd_steinberg_produces_valid_buffers(self):
        mode = HiresDisplayMode("normal", dither_method="floyd-steinberg", dither_strength=0.5)
        buffers = mode.compose(_gradient())
        self.assertEqual(len(buffers["bitmap"]), 8000)
        self.assertEqual(len(buffers["screen"]), 1000)

    def test_floyd_steinberg_changes_bitmap_vs_none_on_gradient(self):
        frame = _gradient()
        plain = HiresDisplayMode("normal", dither_method="none").compose(frame)
        dithered = HiresDisplayMode(
            "normal", dither_method="floyd-steinberg", dither_strength=1.0
        ).compose(frame)
        self.assertFalse(np.array_equal(plain["bitmap"], dithered["bitmap"]))

    def test_edges_style_ignores_dither_method(self):
        # Edge styles have no color quantization to dither; must not crash
        # and must behave identically regardless of dither_method.
        frame = _gradient()
        plain = HiresDisplayMode("edges", dither_method="none").compose(frame)
        forced = HiresDisplayMode(
            "edges", dither_method="floyd-steinberg", dither_strength=1.0
        ).compose(frame)
        np.testing.assert_array_equal(plain["bitmap"], forced["bitmap"])


if __name__ == "__main__":
    unittest.main()
