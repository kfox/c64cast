"""Tests for the parallax interstitial backgrounds — pure render functions.

Every Background subclass implements ``render(t, top_rows, bottom_rows,
bg_color) -> (chars[1000], colors[1000])`` and is meant to fill ONLY the
indices inside the two row strips, leaving the middle text band untouched so
the InterstitialScene can overlay its text. These tests pin that contract
(shape/dtype, strip-only painting, time variation, the empty-rows guard) for
every registered style plus the ``build`` factory — no U64, no rendering chip.
"""
from __future__ import annotations

import unittest

import numpy as np

from c64cast.backgrounds import (
    REGISTRY,
    SC_SPACE,
    Background,
    NoneBackground,
    build,
)

# The text band the InterstitialScene reserves (rows 11..13 for the default
# centered 3-row block). Backgrounds must never paint here.
TOP_ROWS = range(0, 11)
BOTTOM_ROWS = range(14, 25)
TEXT_ROWS = range(11, 14)


def _cells_in(rows: range) -> list[int]:
    return [r * 40 + c for r in rows for c in range(40)]


class RenderContractTest(unittest.TestCase):
    """Properties every registered background must satisfy."""

    def test_output_shape_and_dtype(self):
        for name, cls in REGISTRY.items():
            with self.subTest(background=name):
                bg = cls(seed=1)
                chars, colors = bg.render(0.0, TOP_ROWS, BOTTOM_ROWS, bg_color=0)
                self.assertEqual(chars.shape, (1000,))
                self.assertEqual(colors.shape, (1000,))
                self.assertEqual(chars.dtype, np.uint8)
                self.assertEqual(colors.dtype, np.uint8)

    def test_text_band_left_untouched(self):
        # The middle rows must stay at the render() defaults (space + bg_color)
        # so the scene can write text without merging cells.
        text_cells = _cells_in(TEXT_ROWS)
        for name, cls in REGISTRY.items():
            with self.subTest(background=name):
                bg = cls(seed=2)
                chars, colors = bg.render(1.5, TOP_ROWS, BOTTOM_ROWS, bg_color=7)
                self.assertTrue(
                    np.all(chars[text_cells] == SC_SPACE),
                    f"{name} painted glyphs into the text band",
                )
                self.assertTrue(
                    np.all(colors[text_cells] == 7),
                    f"{name} painted colors into the text band",
                )

    def test_paints_into_strips(self):
        # Every style except the deliberately-empty 'none' should actually
        # draw something into at least one of the two strips.
        strip_cells = _cells_in(TOP_ROWS) + _cells_in(BOTTOM_ROWS)
        for name, cls in REGISTRY.items():
            if name == "none":
                continue
            with self.subTest(background=name):
                bg = cls(seed=3)
                # Sample a few time points — some styles (e.g. starfield) only
                # have a sparse scatter that may miss a strip at t=0.
                painted = False
                for t in (0.0, 0.7, 1.3, 2.1, 3.5):
                    chars, _ = bg.render(t, TOP_ROWS, BOTTOM_ROWS, bg_color=0)
                    if np.any(chars[strip_cells] != SC_SPACE):
                        painted = True
                        break
                self.assertTrue(painted, f"{name} never painted into a strip")

    def test_empty_rows_guard(self):
        # When one strip is empty (text touches the screen edge) the style must
        # not raise and must leave the whole screen at defaults for that call.
        for name, cls in REGISTRY.items():
            with self.subTest(background=name):
                bg = cls(seed=4)
                chars, colors = bg.render(
                    1.0, range(0, 0), range(0, 0), bg_color=3)
                self.assertTrue(np.all(chars == SC_SPACE))
                self.assertTrue(np.all(colors == 3))

    def test_animates_over_time(self):
        # Every built-in style except 'none' has some time-driven motion. Some
        # animate glyphs (starfield), others only colors (raster_bars, checker),
        # and some have a coarse phase period — so compare the t=0 frame against
        # a spread of later samples and require motion in at least one.
        samples = (0.25, 0.5, 1.0, 1.7, 3.3)
        for name, cls in REGISTRY.items():
            if name == "none":
                continue
            with self.subTest(background=name):
                bg = cls(seed=5)
                a_ch, a_co = bg.render(0.0, TOP_ROWS, BOTTOM_ROWS, bg_color=0)
                moved = False
                for t in samples:
                    b_ch, b_co = bg.render(t, TOP_ROWS, BOTTOM_ROWS, bg_color=0)
                    if not (np.array_equal(a_ch, b_ch)
                            and np.array_equal(a_co, b_co)):
                        moved = True
                        break
                self.assertTrue(moved, f"{name} never animated across t=0→3.3")

    def test_bottom_only_strip(self):
        # Exercise the ground/bottom code paths in nature/city (which branch on
        # the strip midpoint) with a bottom-only strip.
        bottom = range(20, 25)
        for name, cls in REGISTRY.items():
            with self.subTest(background=name):
                bg = cls(seed=6)
                chars, _ = bg.render(1.0, range(0, 0), bottom, bg_color=0)
                # Text band + top must remain untouched.
                self.assertTrue(np.all(chars[_cells_in(range(0, 20))] == SC_SPACE))


class FactoryTest(unittest.TestCase):

    def test_build_named(self):
        for name, cls in REGISTRY.items():
            with self.subTest(background=name):
                bg = build(name, seed=0)
                self.assertIsInstance(bg, cls)
                self.assertEqual(bg.name, name)

    def test_build_unknown_raises(self):
        with self.assertRaises(ValueError) as ctx:
            build("does-not-exist")
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_build_random_excludes_none(self):
        # 'random' must never pick the boring empty style. Sample many times
        # with seeded RNG-independence (build() uses the module's random).
        for _ in range(50):
            bg = build("random")
            self.assertIn(bg.name, REGISTRY)
            self.assertNotEqual(bg.name, "none")

    def test_none_background_leaves_strips_empty(self):
        bg = NoneBackground(seed=0)
        chars, colors = bg.render(1.0, TOP_ROWS, BOTTOM_ROWS, bg_color=5)
        self.assertTrue(np.all(chars == SC_SPACE))
        self.assertTrue(np.all(colors == 5))


class BaseClassTest(unittest.TestCase):

    def test_base_fill_is_abstract(self):
        # The base Background._fill must be overridden; calling render on the
        # bare base raises NotImplementedError via _fill.
        base = Background(seed=0)
        with self.assertRaises(NotImplementedError):
            base.render(0.0, TOP_ROWS, BOTTOM_ROWS)


if __name__ == "__main__":
    unittest.main()
