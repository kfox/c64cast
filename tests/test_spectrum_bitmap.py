"""Unit tests for the mhires spectrum overlay — no real U64, no real audio.

The overlay paints into the multicolor-bitmap compose buffers, so the
assertions here are about the MCBM encoding itself: which bitmap bytes carry
%11 (the color-RAM slot), which cells' color RAM carries the band color, and —
just as important — which bytes the overlay left alone so the video frame shows
through.
"""

# pyright: reportAttributeAccessIssue=false
from __future__ import annotations

import unittest
from typing import cast

import numpy as np

from c64cast.scenes.modulation import MusicModulation
from c64cast.scenes.overlays import build_overlay, known_overlays, validate_for_scene
from c64cast.scenes.overlays.spectrum_bitmap import (
    BAR_CELLS,
    BITMAP_H,
    CELL_PX,
    CELLS_PER_BAND,
    HW_COLS,
    HW_ROWS,
    BitmapSpectrumOverlay,
)
from c64cast.scenes.scenes import Scene

# A recognizable non-zero fill for the incoming frame, so "untouched" is
# provable rather than indistinguishable from a zeroed buffer.
_FRAME_BITMAP = 0x1B
_FRAME_COLOR = 0x07


def _make_buffers():
    """The four arrays MultiHiresDisplayMode.compose() hands overlays (minus
    the text surface, which this overlay never uses)."""
    return {
        "bitmap": np.full(HW_ROWS * HW_COLS * CELL_PX, _FRAME_BITMAP, dtype=np.uint8),
        "screen": np.full(HW_ROWS * HW_COLS, 0x42, dtype=np.uint8),
        "color": np.full(HW_ROWS * HW_COLS, _FRAME_COLOR, dtype=np.uint8),
        "bg": 0,
    }


class FakeScene:
    name = "fake"

    def __init__(self, features=None):
        self._features = features

    def features(self):
        return self._features


def _scene(features=None) -> Scene:
    return cast(Scene, FakeScene(features))


def _bands(values) -> MusicModulation:
    return MusicModulation(
        level=1.0,
        onset=0.0,
        beat_phase=0.0,
        bpm=0.0,
        voice_freqs=(0.0, 0.0, 0.0),
        voice_gates=(False, False, False),
        bands=tuple(values),
    )


def _one_loud_band(index: int, n: int = 8) -> MusicModulation:
    vals = [0.0] * n
    vals[index] = 1.0
    return _bands(vals)


class RegistrationTest(unittest.TestCase):
    def test_registered(self):
        self.assertIn("spectrum_bitmap", known_overlays())

    def test_builds_without_audio(self):
        ov = build_overlay({"type": "spectrum_bitmap"}, audio=None)
        self.assertIsNone(ov.audio)

    def test_only_valid_on_mhires(self):
        ov = build_overlay({"type": "spectrum_bitmap"}, audio=None)

        class _Mode:
            name = "mhires"
            is_bitmapped = True
            is_petscii_compatible = False
            is_bitmap_text_compatible = True

        validate_for_scene(ov, _Mode())  # no raise

        class _Hires(_Mode):
            name = "hires"

        with self.assertRaisesRegex(ValueError, "mhires"):
            validate_for_scene(ov, _Hires())

    def test_rejects_bad_params(self):
        with self.assertRaises(ValueError):
            BitmapSpectrumOverlay(placement="sideways")
        with self.assertRaises(ValueError):
            BitmapSpectrumOverlay(height_frac=0.0)
        with self.assertRaises(ValueError):
            BitmapSpectrumOverlay(height_frac=1.5)


class PaintTest(unittest.TestCase):
    def _paint(self, ov, features):
        buffers = _make_buffers()
        ov.compose(buffers, _scene(features), t=0.0)
        return buffers

    def test_silence_leaves_buffers_byte_identical(self):
        ov = BitmapSpectrumOverlay(placement="bottom")
        before = _make_buffers()
        after = self._paint(ov, _bands([0.0] * 8))
        for key in ("bitmap", "screen", "color"):
            np.testing.assert_array_equal(after[key], before[key], err_msg=key)

    def test_full_band_paints_a_full_height_bar(self):
        # height_frac = 1.0 → a full-energy bar spans the whole screen.
        ov = BitmapSpectrumOverlay(placement="bottom", height_frac=1.0)
        buf = self._paint(ov, _one_loud_band(0))
        bitmap = buf["bitmap"].reshape(HW_ROWS, HW_COLS, CELL_PX)
        color = buf["color"].reshape(HW_ROWS, HW_COLS)
        # Band 0 owns cell columns 0..BAR_CELLS, every row, all pixels %11.
        self.assertTrue((bitmap[:, 0:BAR_CELLS, :] == 0xFF).all())
        self.assertTrue((color[:, 0:BAR_CELLS] == int(_band_color(0))).all())

    def test_gutter_and_quiet_bands_are_untouched(self):
        ov = BitmapSpectrumOverlay(placement="bottom", height_frac=1.0)
        buf = self._paint(ov, _one_loud_band(0))
        bitmap = buf["bitmap"].reshape(HW_ROWS, HW_COLS, CELL_PX)
        color = buf["color"].reshape(HW_ROWS, HW_COLS)
        # The gutter cell at the right edge of band 0 keeps the frame.
        gutter = CELLS_PER_BAND - 1
        self.assertTrue((bitmap[:, gutter, :] == _FRAME_BITMAP).all())
        self.assertTrue((color[:, gutter] == _FRAME_COLOR).all())
        # Bands 1..7 are silent → their columns keep the frame entirely.
        self.assertTrue((bitmap[:, CELLS_PER_BAND:, :] == _FRAME_BITMAP).all())
        self.assertTrue((color[:, CELLS_PER_BAND:] == _FRAME_COLOR).all())

    def test_screen_nibbles_are_never_touched(self):
        # The whole point of owning c3: the frame's c1/c2 survive untouched.
        ov = BitmapSpectrumOverlay(placement="bottom", height_frac=1.0)
        buf = self._paint(ov, _bands([1.0] * 8))
        self.assertTrue((buf["screen"] == 0x42).all())

    def test_bar_rises_from_the_bottom(self):
        ov = BitmapSpectrumOverlay(placement="bottom", height_frac=0.5)
        buf = self._paint(ov, _one_loud_band(0))
        bitmap = buf["bitmap"].reshape(HW_ROWS, HW_COLS, CELL_PX)
        # Bottom scanline painted, top scanline not.
        self.assertEqual(bitmap[HW_ROWS - 1, 0, CELL_PX - 1], 0xFF)
        self.assertEqual(bitmap[0, 0, 0], _FRAME_BITMAP)

    def test_bar_top_lands_on_a_sub_cell_scanline(self):
        # The resolution claim: a height that isn't a multiple of 8 must leave
        # a partially-painted cell rather than snapping to the cell boundary.
        ov = BitmapSpectrumOverlay(placement="bottom", height_frac=1.0)
        # 0.51 * 200 = 102 px → top edge at scanline 98, i.e. 2 px into a cell.
        buf = self._paint(ov, _bands([0.51] + [0.0] * 7))
        bitmap = buf["bitmap"].reshape(HW_ROWS, HW_COLS, CELL_PX)
        top_px = BITMAP_H - int(0.51 * BITMAP_H + 0.5)
        row, scan = divmod(top_px, CELL_PX)
        self.assertNotEqual(scan, 0, "test needs a height that lands mid-cell")
        painted = bitmap[row, 0, :]
        self.assertTrue((painted[:scan] == _FRAME_BITMAP).all(), "above the top must survive")
        self.assertTrue((painted[scan:] == 0xFF).all(), "below the top must be bar")

    def test_placement_center_is_symmetric_about_the_middle(self):
        ov = BitmapSpectrumOverlay(placement="center", height_frac=0.5)
        buf = self._paint(ov, _one_loud_band(0))
        bitmap = buf["bitmap"].reshape(HW_ROWS, HW_COLS, CELL_PX)
        flat = bitmap[:, 0, :].reshape(-1)  # 200 scanlines of cell column 0
        painted = np.flatnonzero(flat == 0xFF)
        self.assertTrue(painted.size > 0)
        mid = BITMAP_H // 2
        self.assertAlmostEqual((painted[0] + painted[-1] + 1) / 2, mid, delta=1)

    def test_placement_split_paints_both_edges(self):
        ov = BitmapSpectrumOverlay(placement="split", height_frac=0.5)
        buf = self._paint(ov, _one_loud_band(0))
        bitmap = buf["bitmap"].reshape(HW_ROWS, HW_COLS, CELL_PX)
        flat = bitmap[:, 0, :].reshape(-1)
        self.assertEqual(flat[0], 0xFF, "split paints from the top edge down")
        self.assertEqual(flat[-1], 0xFF, "split paints from the bottom edge up")
        self.assertEqual(flat[BITMAP_H // 2], _FRAME_BITMAP, "and leaves the middle alone")

    def test_each_band_uses_its_own_color(self):
        ov = BitmapSpectrumOverlay(placement="bottom", height_frac=1.0)
        buf = self._paint(ov, _bands([1.0] * 8))
        color = buf["color"].reshape(HW_ROWS, HW_COLS)
        for band in range(8):
            x = band * CELLS_PER_BAND
            self.assertEqual(int(color[HW_ROWS - 1, x]), int(_band_color(band)), f"band {band}")

    def test_sid_voices_paint_without_any_bands(self):
        # The SID tier end-to-end through the bitmap overlay.
        feat = MusicModulation(
            level=0.9,
            onset=0.0,
            beat_phase=0.0,
            bpm=0.0,
            voice_freqs=(90.0, 900.0, 0.0),
            voice_gates=(True, True, False),
        )
        ov = BitmapSpectrumOverlay(placement="bottom")
        buf = self._paint(ov, feat)
        self.assertTrue((buf["bitmap"] == 0xFF).any(), "gated SID voices should paint bars")


def _band_color(band: int) -> int:
    from c64cast.scenes.overlays._spectrum import BAND_COLORS

    return int(BAND_COLORS[band])


if __name__ == "__main__":
    unittest.main()
