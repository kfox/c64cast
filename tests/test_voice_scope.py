"""Direct tests for the shared 3-voice oscilloscope core
(c64cast.sid.voice_scope) — the layout helpers and the render primitives
that turn a trace into C64 hires bitmap bytes. Three scenes (waveform /
midi / asid) ride on this renderer, but until now it was only exercised
indirectly through whichever scene test happened to touch a given path.

VoiceScopeRenderer is a mixin with a documented attribute contract, so a
bare instance plus exactly the attributes a method reads is the intended
harness (mirrors ScopeGainTest in test_waveform.py).
"""

# FakeAPI duck-types C64Backend (the mixin's contract type), so silence
# pyright's attribute-access complaints file-wide — same convention as
# test_waveform.py / test_playlist.py.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np
from _fakes import FakeAPI

from c64cast.hw.c64 import RegionID
from c64cast.scenes.bitmap_text import ascii_to_screen_code
from c64cast.sid.sidemu import ACCUMULATOR_RANGE, WAVE_TRIANGLE
from c64cast.sid.voice_scope import (
    BITMAP_H,
    BITMAP_W,
    META_ROW,
    SCREEN_W_CHARS,
    TIME_BASE_AUTO,
    TIME_BASE_WALLCLOCK,
    TITLE_ROW,
    VoiceScopeRenderer,
    _compute_window_slices,
    _layout_lcr,
    _layout_lr,
    _mirror_glyph_h,
)

CELL = 8


class MirrorGlyphTest(unittest.TestCase):
    """_mirror_glyph_h synthesizes the right-arrow glyph the C64 charset has
    no cell for by bit-reversing each row of the ROM's left-arrow."""

    def test_each_row_is_bit_reversed(self):
        glyph = bytes([0b10000000, 0b00000001, 0b11001010, 0, 0, 0, 0, 0b11111111])
        mirrored = _mirror_glyph_h(glyph)
        self.assertEqual(mirrored[0], 0b00000001)
        self.assertEqual(mirrored[1], 0b10000000)
        self.assertEqual(mirrored[2], 0b01010011)
        self.assertEqual(mirrored[7], 0b11111111)

    def test_mirroring_twice_is_identity(self):
        glyph = bytes(range(8))
        self.assertEqual(_mirror_glyph_h(_mirror_glyph_h(glyph)), glyph)


class LayoutLrTest(unittest.TestCase):
    """_layout_lr: left/right justified fields with the gap filled — always
    exactly the requested width, always ≥1 space of separation."""

    def test_pads_gap_between_fields(self):
        line = _layout_lr("TITLE", "AUTHOR", width=20)
        self.assertEqual(len(line), 20)
        self.assertEqual(line, "TITLE" + " " * 9 + "AUTHOR")

    def test_overflow_caps_right_at_half_then_truncates_left(self):
        line = _layout_lr("L" * 30, "R" * 30, width=20)
        self.assertEqual(len(line), 20)
        # Right capped at width//2 - 1 = 9; left gets the rest minus the gap.
        self.assertTrue(line.endswith("R" * 9))
        self.assertTrue(line.startswith("L" * 10))
        self.assertIn(" ", line)

    def test_empty_fields(self):
        self.assertEqual(_layout_lr("", "", width=10), " " * 10)


class LayoutLcrTest(unittest.TestCase):
    """_layout_lcr: three fields with the center geometrically placed and
    nudged off-center rather than colliding."""

    def test_center_is_geometrically_placed(self):
        line = _layout_lcr("L", "CC", "R", width=20)
        self.assertEqual(len(line), 20)
        self.assertEqual(line[0], "L")
        self.assertEqual(line[-1], "R")
        center_start = line.index("CC")
        self.assertAlmostEqual(center_start, 20 // 2 - 1, delta=1)

    def test_long_left_nudges_center_right(self):
        line = _layout_lcr("L" * 10, "CC", "R", width=20)
        self.assertEqual(len(line), 20)
        # Center must not overlap the left field; ≥1 gap after it.
        self.assertEqual(line.index("CC"), 11)

    def test_total_overflow_still_returns_exact_width(self):
        line = _layout_lcr("L" * 30, "C" * 30, "R" * 30, width=40)
        self.assertEqual(len(line), 40)


class ComputeWindowSlicesTest(unittest.TestCase):
    """The multi-chip split windows: cell-aligned, remainder to the earliest
    windows, n=1 the identity."""

    def test_single_window_is_identity(self):
        self.assertEqual(_compute_window_slices(1), [(0, BITMAP_W)])
        self.assertEqual(_compute_window_slices(0), [(0, BITMAP_W)])

    def test_two_windows_split_evenly(self):
        self.assertEqual(_compute_window_slices(2), [(0, 160), (160, 160)])

    def test_three_windows_give_remainder_to_the_first(self):
        # 40 cells / 3 = 13 rem 1 → 14 + 13 + 13 cells.
        self.assertEqual(_compute_window_slices(3), [(0, 112), (112, 104), (216, 104)])

    def test_every_window_is_cell_aligned_and_covers_the_strip(self):
        for n in range(1, 8):
            slices = _compute_window_slices(n)
            self.assertEqual(sum(w for _, w in slices), BITMAP_W, f"n={n}")
            for x_off, w in slices:
                self.assertEqual(x_off % CELL, 0, f"n={n}")
                self.assertEqual(w % CELL, 0, f"n={n}")


def _bare_renderer(**attrs) -> VoiceScopeRenderer:
    """A VoiceScopeRenderer with exactly the contract attributes the method
    under test reads (the mixin has no __init__ of its own)."""
    r = VoiceScopeRenderer()
    for name, value in attrs.items():
        setattr(r, name, value)
    return r


class SpanMaskTest(unittest.TestCase):
    """_span_mask fills the vertical span between adjacent columns so a
    sharp jump doesn't fragment the trace into isolated dots."""

    def _renderer(self):
        return _bare_renderer(_rows_col=np.arange(BITMAP_H, dtype=np.int32)[:, None])

    def test_flat_trace_lights_one_pixel_per_column(self):
        r = self._renderer()
        ys = np.full(4, 10, dtype=np.int32)
        mask = r._span_mask(ys, top=0, bot=56, prev_y=None)
        self.assertEqual(mask.shape, (56, 4))
        self.assertTrue((mask.sum(axis=0) == 1).all())
        self.assertTrue(mask[10].all())

    def test_jump_fills_the_vertical_span(self):
        r = self._renderer()
        ys = np.array([10, 20], dtype=np.int32)
        mask = r._span_mask(ys, top=0, bot=56, prev_y=None)
        # Column 1 spans rows 10..20 inclusive — no gap at the jump.
        self.assertTrue(mask[10:21, 1].all())
        self.assertFalse(mask[9, 1])
        self.assertFalse(mask[21, 1])

    def test_prev_y_connects_the_first_column(self):
        # Scroll mode passes the previous frame's last y so the trace stays
        # continuous across the scroll boundary.
        r = self._renderer()
        ys = np.array([30], dtype=np.int32)
        mask = r._span_mask(ys, top=0, bot=56, prev_y=25)
        self.assertTrue(mask[25:31, 0].all())

    def test_strip_offset_is_subtracted(self):
        # A voice-2 strip (top=56) maps absolute y 60 to strip row 4.
        r = self._renderer()
        ys = np.full(2, 60, dtype=np.int32)
        mask = r._span_mask(ys, top=56, bot=112, prev_y=None)
        self.assertTrue(mask[4].all())
        self.assertEqual(mask.sum(), 2)


class WriteBitmapStripTest(unittest.TestCase):
    """_write_bitmap_strip packs a bool mask into the C64 hires layout:
    cell-row-major, then cell-column, then row-within-cell — the exact
    bytes the VIC fetches."""

    BASE = 0x2000

    def _renderer(self):
        return _bare_renderer(api=FakeAPI(), _bitmap_base=self.BASE)

    def test_empty_mask_writes_zeros_to_the_voice_region(self):
        r = self._renderer()
        mask = np.zeros((56, BITMAP_W), dtype=bool)
        r._write_bitmap_strip(1, 56, 112, mask)
        # Voice 1's strip starts at cell row 7 → base + 7*320.
        addr = self.BASE + 7 * BITMAP_W
        self.assertEqual(r.api.regions[addr], bytes(7 * BITMAP_W))
        op = r.api.ops[-1]
        self.assertEqual(op[3], RegionID.WAVE_BITMAP + 1)

    def test_single_pixel_lands_at_the_hires_byte(self):
        r = self._renderer()
        mask = np.zeros((56, BITMAP_W), dtype=bool)
        mask[9, 105] = True  # strip row 9 (cell row 1, row-in-cell 1), px 105
        r._write_bitmap_strip(0, 0, 56, mask)
        strip = r.api.regions[self.BASE]
        # Byte index: (cell_row * 40 + cell_col) * 8 + row_in_cell.
        idx = (1 * SCREEN_W_CHARS + 105 // CELL) * CELL + 9 % CELL
        expected = 1 << (7 - 105 % CELL)
        self.assertEqual(strip[idx], expected)
        self.assertEqual(sum(strip), expected)  # nothing else lit


class VoiceTimeWindowTest(unittest.TestCase):
    """_voice_time_window_s: per-column audio time under each time base,
    with silent voices falling back to wallclock."""

    def _renderer(self, *, time_base, voice=None, auto_cycles=4):
        emu = SimpleNamespace(
            voices=[voice if voice is not None else SimpleNamespace()],
            clock=1_000_000,
        )
        return _bare_renderer(
            time_base=time_base,
            auto_cycles=auto_cycles,
            _frame_time_s=1 / 30.0,
            emulator=emu,
        )

    def test_wallclock_full_width_is_one_display_frame(self):
        r = self._renderer(time_base=TIME_BASE_WALLCLOCK)
        self.assertAlmostEqual(r._voice_time_window_s(0, BITMAP_W), 1 / 30.0)
        self.assertAlmostEqual(r._voice_time_window_s(0, BITMAP_W // 2), 1 / 60.0)

    def test_auto_spans_auto_cycles_of_the_voice_period(self):
        voice = SimpleNamespace(freq=0x2000, control=WAVE_TRIANGLE, envelope_level=1.0)
        r = self._renderer(time_base=TIME_BASE_AUTO, voice=voice, auto_cycles=4)
        period_s = ACCUMULATOR_RANGE / (0x2000 * 1_000_000)
        self.assertAlmostEqual(r._voice_time_window_s(0, BITMAP_W), 4 * period_s)

    def test_auto_falls_back_to_wallclock_for_a_silent_voice(self):
        voice = SimpleNamespace(freq=0x2000, control=WAVE_TRIANGLE, envelope_level=0.0)
        r = self._renderer(time_base=TIME_BASE_AUTO, voice=voice)
        self.assertAlmostEqual(r._voice_time_window_s(0, BITMAP_W), 1 / 30.0)


class PaintInfoRowsTest(unittest.TestCase):
    """_paint_info_rows renders the two subclass-supplied 40-char lines into
    the title/meta cell rows with the shared colors and region IDs."""

    class _Host(VoiceScopeRenderer):
        def __init__(self):
            self.api = FakeAPI()
            self._bitmap_base = 0x2000
            self._screen_base = 0x0400
            # Synthetic charset: the glyph for screen code c is bytes([c])*8,
            # which makes the expected bitmap bytes trivially computable.
            self._glyphs = bytes(c for c in range(256) for _ in range(CELL))

        def _build_title_line(self):
            return "T" * SCREEN_W_CHARS

        def _build_meta_line(self):
            return "m" * SCREEN_W_CHARS

    def test_rows_land_in_their_cells_with_their_regions(self):
        host = self._Host()
        host._paint_info_rows()

        sc_title = ascii_to_screen_code("T")
        title_addr = 0x2000 + TITLE_ROW * BITMAP_W
        self.assertEqual(host.api.regions[title_addr], bytes([sc_title]) * BITMAP_W)
        sc_meta = ascii_to_screen_code("m")
        meta_addr = 0x2000 + META_ROW * BITMAP_W
        self.assertEqual(host.api.regions[meta_addr], bytes([sc_meta]) * BITMAP_W)

        region_ids = [op[3] for op in host.api.ops if op[0] == "write_region"]
        self.assertEqual(
            region_ids,
            [
                RegionID.WAVE_TITLE_BITMAP,
                RegionID.WAVE_TITLE_SCREEN,
                RegionID.WAVE_META_BITMAP,
                RegionID.WAVE_META_SCREEN,
            ],
        )

    def test_colors_ride_the_fg_nibble(self):
        # Title white (1 → $10), meta light gray (15 → $F0), BG black.
        host = self._Host()
        host._paint_info_rows()
        title_screen = host.api.regions[0x0400 + TITLE_ROW * SCREEN_W_CHARS]
        self.assertEqual(title_screen, bytes([0x10]) * SCREEN_W_CHARS)
        meta_screen = host.api.regions[0x0400 + META_ROW * SCREEN_W_CHARS]
        self.assertEqual(meta_screen, bytes([0xF0]) * SCREEN_W_CHARS)

    def test_base_hooks_are_abstract(self):
        r = VoiceScopeRenderer()
        with self.assertRaises(NotImplementedError):
            r._build_title_line()
        with self.assertRaises(NotImplementedError):
            r._build_meta_line()


if __name__ == "__main__":
    unittest.main()
