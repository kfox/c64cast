"""Tests for the video-setup progress bar: per-mode bar styles, the painted
geometry (addresses, spans, incremental extension), and the weighted-segment
progress model including its null object. No hardware; FakeAPI records the
writes."""

# FakeAPI duck-types C64Backend; suppress pyright's argument-type complaints
# file-wide so the test focus stays on behavior (same convention as
# test_dac_calibration.py).
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest

from _fakes import FakeAPI

from c64cast.scenes import setup_progress as sp
from c64cast.video.modes.blank import BlankDisplayMode
from c64cast.video.modes.hires import HiresDisplayMode
from c64cast.video.modes.mcm import MCMDisplayMode
from c64cast.video.modes.mhires import MultiHiresDisplayMode
from c64cast.video.modes.petscii import PETSCIIDisplayMode

_ROW_SCREEN = 0x0400 + sp.BAR_ROW * 40
_ROW_COLOR = 0xD800 + sp.BAR_ROW * 40
_ROW_BITMAP = 0x2000 + sp.BAR_ROW * 320


def _writes(api: FakeAPI) -> list[tuple[int, bytes]]:
    return [(int(addr, 16), data) for addr, data in api.writes]


class BarStyleForTest(unittest.TestCase):
    def test_char_modes_get_the_diagonal_glyph(self):
        for mode in (PETSCIIDisplayMode(), BlankDisplayMode()):
            style = sp.bar_style_for(mode)
            assert style is not None
            self.assertEqual((style.kind, style.fill_code), ("char", 0x4E))
            self.assertEqual(style.color_byte, 1)

    def test_mcm_gets_the_crumb_code_and_multicolor_white(self):
        style = sp.bar_style_for(MCMDisplayMode())
        assert style is not None
        self.assertEqual((style.kind, style.fill_code), ("char", 0xC3))
        self.assertEqual(style.color_byte, 0x09)

    def test_bitmap_modes_get_stripes_with_mode_matched_nibbles(self):
        hires = sp.bar_style_for(HiresDisplayMode())
        mhires = sp.bar_style_for(MultiHiresDisplayMode())
        assert hires is not None and mhires is not None
        self.assertEqual((hires.kind, hires.color_byte), ("bitmap", 0x10))
        self.assertEqual((mhires.kind, mhires.color_byte), ("bitmap", 0x11))

    def test_no_mode_means_no_bar(self):
        self.assertIsNone(sp.bar_style_for(None))
        self.assertIsNone(sp.make_setup_bar(FakeAPI(), None))


class CharBarTest(unittest.TestCase):
    def _bar(self, api):
        return sp.SetupProgressBar(api, sp.bar_style_for(PETSCIIDisplayMode()))

    def test_half_fills_twenty_cells_white(self):
        api = FakeAPI()
        self._bar(api).show(0.5)
        self.assertEqual(_writes(api)[0], (_ROW_SCREEN, bytes([0x4E]) * 20))
        self.assertEqual(_writes(api)[1], (_ROW_COLOR, bytes([1]) * 20))

    def test_growth_writes_only_the_new_span(self):
        api = FakeAPI()
        bar = self._bar(api)
        bar.show(0.5)
        bar.show(0.75)
        self.assertEqual(_writes(api)[2], (_ROW_SCREEN + 20, bytes([0x4E]) * 10))
        self.assertEqual(_writes(api)[3], (_ROW_COLOR + 20, bytes([1]) * 10))

    def test_repeat_and_shrink_are_no_ops(self):
        api = FakeAPI()
        bar = self._bar(api)
        bar.show(0.5)
        before = len(api.writes)
        bar.show(0.5)
        bar.show(0.3)
        self.assertEqual(len(api.writes), before)

    def test_fraction_is_clamped(self):
        api = FakeAPI()
        bar = self._bar(api)
        bar.show(7.0)
        self.assertEqual(_writes(api)[0], (_ROW_SCREEN, bytes([0x4E]) * 40))
        bar.show(-1.0)  # already full; must not write or raise
        self.assertEqual(len(api.writes), 2)


class BitmapBarTest(unittest.TestCase):
    def test_stripes_land_in_the_bank0_bitmap_row(self):
        api = FakeAPI()
        bar = sp.SetupProgressBar(api, sp.bar_style_for(HiresDisplayMode()))
        bar.show(0.5)
        addr, data = _writes(api)[0]
        self.assertEqual(addr, _ROW_BITMAP)
        self.assertEqual(data, bytes((0x88, 0x11, 0x22, 0x44) * 2) * 20)
        self.assertEqual(_writes(api)[1], (_ROW_SCREEN, bytes([0x10]) * 20))

    def test_stripe_cell_is_a_45_degree_diagonal(self):
        cell = bytes((0x88, 0x11, 0x22, 0x44) * 2)
        for y, row_byte in enumerate(cell):
            for x in range(8):
                lit = bool(row_byte & (0x80 >> x))
                self.assertEqual(lit, (x + y) % 4 == 0, f"x={x} y={y}")

    def test_mhires_lights_both_screen_nibbles(self):
        api = FakeAPI()
        bar = sp.SetupProgressBar(api, sp.bar_style_for(MultiHiresDisplayMode()))
        bar.show(1.0)
        self.assertEqual(_writes(api)[1], (_ROW_SCREEN, bytes([0x11]) * 40))


class SegmentedProgressTest(unittest.TestCase):
    def _model(self, segments):
        seen: list[float] = []
        return sp.SegmentedProgress(segments, seen.append), seen

    def test_weights_average_into_the_overall_fraction(self):
        model, seen = self._model([("open", 1.0), ("upload", 3.0)])
        model.complete("open")
        self.assertAlmostEqual(seen[-1], 0.25)
        reporter = model.reporter("upload")
        assert reporter is not None
        reporter(0.5)
        self.assertAlmostEqual(seen[-1], 0.25 + 0.5 * 0.75)

    def test_per_segment_fraction_never_regresses(self):
        model, seen = self._model([("upload", 1.0)])
        reporter = model.reporter("upload")
        assert reporter is not None
        reporter(0.8)
        reporter(0.2)
        self.assertAlmostEqual(seen[-1], 0.8)

    def test_unknown_segments_are_inert(self):
        model, seen = self._model([("open", 1.0)])
        self.assertIsNone(model.reporter("prescan"))
        model.complete("prescan")
        self.assertEqual(seen, [])

    def test_finish_forces_full(self):
        model, seen = self._model([("open", 1.0), ("upload", 3.0)])
        model.finish()
        self.assertEqual(seen[-1], 1.0)

    def test_off_is_fully_inert(self):
        model = sp.SegmentedProgress.off()
        self.assertIsNone(model.reporter("open"))
        model.complete("open")
        model.finish()


if __name__ == "__main__":
    unittest.main()
