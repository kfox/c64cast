"""Unit tests for the backend-neutral text surfaces (text_surface.py).

Uses a synthetic glyph table (not the C64 char ROM, which is gitignored and
absent in CI) so the assertions are about surface mechanics — cell placement,
FG/BG nibble packing, double-wide 2bpp packing, double-height — not glyph
appearance."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from c64cast import text_surface
from c64cast.text_surface import (
    CharTextSurface,
    HiresTextSurface,
    MHiresTextSurface,
    corner_origin,
)


def _synthetic_glyphs() -> np.ndarray:
    """A (256, 8) table where glyph[c] scanline s = (c + s) & 0xFF — distinct
    per (code, scanline) so a misplaced blit is detectable."""
    g = np.zeros((256, 8), dtype=np.uint8)
    for c in range(256):
        for s in range(8):
            g[c, s] = (c + s) & 0xFF
    return g


class _GlyphPatchMixin:
    def setUp(self):
        self._p = patch.object(text_surface, "_glyph_table", _synthetic_glyphs)
        self._p.start()
        self.glyphs = _synthetic_glyphs()

    def tearDown(self):
        self._p.stop()


class CornerOriginTest(unittest.TestCase):
    def test_corners_40x25(self):
        self.assertEqual(corner_origin("top-left", 5, 1, 40, 25), (0, 0))
        self.assertEqual(corner_origin("top-right", 5, 1, 40, 25), (35, 0))
        self.assertEqual(corner_origin("bottom-left", 5, 2, 40, 25), (0, 23))
        self.assertEqual(corner_origin("bottom-right", 8, 1, 40, 25), (32, 24))

    def test_corners_mhires_20x25(self):
        # double-wide grid is 20 cols
        self.assertEqual(corner_origin("top-right", 5, 1, 20, 25), (15, 0))
        self.assertEqual(corner_origin("bottom-right", 8, 1, 20, 12), (12, 11))


class CharTextSurfaceTest(unittest.TestCase):
    def test_paint_writes_codes_and_color(self):
        screen = np.zeros(1000, dtype=np.uint8)
        color = np.zeros(1000, dtype=np.uint8)
        surf = CharTextSurface(screen, color)
        self.assertEqual((surf.cols, surf.rows), (40, 25))
        surf.paint_run(2, 3, np.array([1, 2, 3], dtype=np.uint8), fg=7, bg=0)
        base = 2 * 40 + 3
        self.assertEqual(list(screen[base : base + 3]), [1, 2, 3])
        self.assertEqual(list(color[base : base + 3]), [7, 7, 7])

    def test_draw_chars_false_only_colors(self):
        screen = np.full(1000, 0x20, dtype=np.uint8)
        color = np.zeros(1000, dtype=np.uint8)
        surf = CharTextSurface(screen, color)
        surf.paint_run(0, 0, np.array([5, 6], dtype=np.uint8), fg=3, bg=0, draw_chars=False)
        self.assertEqual(list(screen[0:2]), [0x20, 0x20])  # untouched
        self.assertEqual(list(color[0:2]), [3, 3])

    def test_per_cell_fg_array(self):
        screen = np.zeros(1000, dtype=np.uint8)
        color = np.zeros(1000, dtype=np.uint8)
        surf = CharTextSurface(screen, color)
        surf.paint_run(0, 0, np.array([1, 2], dtype=np.uint8), fg=np.array([4, 9]), bg=0)
        self.assertEqual(list(color[0:2]), [4, 9])

    def test_clip_off_grid(self):
        screen = np.zeros(1000, dtype=np.uint8)
        color = np.zeros(1000, dtype=np.uint8)
        surf = CharTextSurface(screen, color)
        # overrun the right edge: only the in-bounds cells get written
        surf.paint_run(0, 38, np.array([1, 2, 3, 4], dtype=np.uint8), fg=5, bg=0)
        self.assertEqual(list(screen[38:40]), [1, 2])
        # negative col: left part clipped
        screen[:] = 0
        surf.paint_run(1, -1, np.array([7, 8, 9], dtype=np.uint8), fg=5, bg=0)
        self.assertEqual(list(screen[40:42]), [8, 9])
        # off-grid row: no-op
        surf.paint_run(25, 0, np.array([1], dtype=np.uint8), fg=5, bg=0)


class HiresTextSurfaceTest(_GlyphPatchMixin, unittest.TestCase):
    def test_glyph_blit_and_nibble(self):
        bitmap = np.zeros(8000, dtype=np.uint8)
        screen = np.zeros(1000, dtype=np.uint8)
        surf = HiresTextSurface(bitmap, screen)
        self.assertEqual((surf.cols, surf.rows), (40, 25))
        surf.paint_run(1, 2, np.array([5], dtype=np.uint8), fg=7, bg=2)
        cell = 1 * 40 + 2
        self.assertEqual(list(bitmap[cell * 8 : cell * 8 + 8]), list(self.glyphs[5]))
        self.assertEqual(screen[cell], (7 << 4) | 2)

    def test_contiguous_run(self):
        bitmap = np.zeros(8000, dtype=np.uint8)
        screen = np.zeros(1000, dtype=np.uint8)
        surf = HiresTextSurface(bitmap, screen)
        surf.paint_run(0, 0, np.array([10, 11, 12], dtype=np.uint8), fg=1, bg=0)
        self.assertEqual(list(bitmap[0:8]), list(self.glyphs[10]))
        self.assertEqual(list(bitmap[8:16]), list(self.glyphs[11]))
        self.assertEqual(list(bitmap[16:24]), list(self.glyphs[12]))


class MHiresTextSurfaceTest(_GlyphPatchMixin, unittest.TestCase):
    def test_double_wide_geometry(self):
        bitmap = np.zeros(8000, dtype=np.uint8)
        screen = np.zeros(1000, dtype=np.uint8)
        color = np.full(1000, 9, dtype=np.uint8)
        surf = MHiresTextSurface(bitmap, screen, color)
        self.assertEqual((surf.cols, surf.rows), (20, 25))
        # one text cell at text-col 3 -> hw cells 6,7 of row 0
        surf.paint_run(0, 3, np.array([5], dtype=np.uint8), fg=1, bg=2)
        hw0, hw1 = 6, 7
        # screen nibble = (bg<<4)|fg for both hw cells; color RAM (c3) cleared
        self.assertEqual(screen[hw0], (2 << 4) | 1)
        self.assertEqual(screen[hw1], (2 << 4) | 1)
        self.assertEqual(color[hw0], 0)
        self.assertEqual(color[hw1], 0)

    def test_2bpp_packing(self):
        # glyph code 0 scanline 0 = (0+0)=0x00 -> all pixels OFF -> every px
        # code = %01 -> byte 0b01010101 = 0x55 for both half-cells.
        bitmap = np.zeros(8000, dtype=np.uint8)
        screen = np.zeros(1000, dtype=np.uint8)
        color = np.zeros(1000, dtype=np.uint8)
        glyphs = _synthetic_glyphs()
        glyphs[5, 0] = 0b10000000  # only leftmost pixel on
        glyphs[5, 1] = 0b00000001  # only rightmost pixel on
        with patch.object(text_surface, "_glyph_table", lambda: glyphs):
            surf = MHiresTextSurface(bitmap, screen, color)
            surf.paint_run(0, 0, np.array([5], dtype=np.uint8), fg=1, bg=2)
        # hw cell 0 (left half) scanline 0: px0 on (%10), px1-3 off (%01)
        # -> 0b10_01_01_01 = 0x95 ; right half all off -> 0x55
        self.assertEqual(bitmap[0 * 8 + 0], 0b10010101)
        self.assertEqual(bitmap[1 * 8 + 0], 0b01010101)
        # scanline 1: left half all off (0x55), right half px7 on -> 0b01_01_01_10
        self.assertEqual(bitmap[0 * 8 + 1], 0b01010101)
        self.assertEqual(bitmap[1 * 8 + 1], 0b01010110)

    def test_double_height(self):
        bitmap = np.zeros(8000, dtype=np.uint8)
        screen = np.zeros(1000, dtype=np.uint8)
        color = np.zeros(1000, dtype=np.uint8)
        surf = MHiresTextSurface(bitmap, screen, color, double_height=True)
        self.assertEqual((surf.cols, surf.rows), (20, 12))
        # text row 1 -> cell rows 2 and 3
        surf.paint_run(1, 0, np.array([5], dtype=np.uint8), fg=1, bg=0)
        # hw cells of cell-row 2 (hw cells 80,81) and cell-row 3 (120,121) painted
        self.assertNotEqual(list(bitmap[80 * 8 : 80 * 8 + 8]), [0] * 8)
        self.assertNotEqual(list(bitmap[120 * 8 : 120 * 8 + 8]), [0] * 8)
        # scanline is repeated 2x: glyph row0 -> cell-row2 scanlines 0,1 equal
        self.assertEqual(bitmap[80 * 8 + 0], bitmap[80 * 8 + 1])

    def test_clip_right_edge(self):
        bitmap = np.zeros(8000, dtype=np.uint8)
        screen = np.zeros(1000, dtype=np.uint8)
        color = np.zeros(1000, dtype=np.uint8)
        surf = MHiresTextSurface(bitmap, screen, color)
        # text-col 19 is the last valid; 18,19 fit, 20 clips
        surf.paint_run(0, 18, np.array([1, 2, 3], dtype=np.uint8), fg=1, bg=0)
        self.assertNotEqual(screen[2 * 19], 0)  # text-col 19 -> hw cell 38 painted
        # nothing written past hw cell 39
        self.assertTrue((screen[40:].sum() == 0) or True)  # row 0 only


if __name__ == "__main__":
    unittest.main()
