"""Integration tests: the PETSCII text overlays render on bitmap display
modes (hires + mhires) by folding glyphs into the composed bitmap before push.

The payoff for the compose/push split — the same clock/marquee/callsign/…
overlays that work on petscii/blank now work on hires/mhires unchanged. A
synthetic glyph table keeps the assertions ROM-independent (the C64 char ROM
is gitignored and absent in CI)."""

# FakeAPI is a structural stand-in (not a nominal C64Backend) and the bitmap
# compose() returns a TypedDict the overlays consume as a plain dict — same
# fake-at-the-boundary pattern as test_overlays.py.
# pyright: reportArgumentType=false, reportOptionalSubscript=false
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI  # noqa: E402

from c64cast import text_surface  # noqa: E402
from c64cast.modes import HiresDisplayMode, MultiHiresDisplayMode  # noqa: E402
from c64cast.overlays.callsign import CallsignOverlay  # noqa: E402
from c64cast.overlays.marquee import MarqueeOverlay  # noqa: E402
from c64cast.overlays.scrolling_text import ScrollingTextOverlay  # noqa: E402
from c64cast.text_surface import HiresTextSurface, MHiresTextSurface  # noqa: E402


def _frame() -> np.ndarray:
    h, w = 240, 320
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    return np.clip(
        np.stack([xx / w * 255, yy / h * 255, (xx + yy) / 560 * 255], -1), 0, 255
    ).astype(np.uint8)


def _synthetic_glyphs() -> np.ndarray:
    g = np.zeros((256, 8), dtype=np.uint8)
    for c in range(256):
        for s in range(8):
            g[c, s] = (c * 8 + s + 1) & 0xFF  # +1 so no glyph is all-zero
    return g


class HiresOverlayTest(unittest.TestCase):
    def test_callsign_folds_glyphs_into_bitmap(self):
        glyphs = _synthetic_glyphs()
        with patch.object(text_surface, "_glyph_table", lambda: glyphs):
            mode = HiresDisplayMode("normal")
            api = FakeAPI()
            mode.setup(api)
            buffers = mode.compose(_frame())
            self.assertIsInstance(buffers["text"], HiresTextSurface)
            ov = CallsignOverlay(text="ABC", corner="top-left", fg_color="white", bg_color="black")
            ov.setup(api, MagicMock())
            ov.compose(buffers, MagicMock(), 0.0)
        # screen nibble per cell = (FG<<4)|BG = (white=1 << 4)|black=0 = 0x10
        self.assertEqual(list(buffers["screen"][0:3]), [0x10, 0x10, 0x10])
        # glyph bytes for screen codes A=1, B=2, C=3 folded into the bitmap
        self.assertEqual(list(buffers["bitmap"][0:8]), list(glyphs[1]))
        self.assertEqual(list(buffers["bitmap"][8:16]), list(glyphs[2]))
        self.assertEqual(list(buffers["bitmap"][16:24]), list(glyphs[3]))
        # push uploads the folded bitmap + screen (host-DMA path)
        mode.push(api, buffers)
        self.assertIn(0x2000, api.regions)
        self.assertIn(0x0400, api.regions)

    def test_overlay_text_rides_reu_bank_swap(self):
        # The point of folding-before-push: text reaches the off-screen bank
        # via the same REU staging the frame uses (a post-hoc writer can't).
        glyphs = _synthetic_glyphs()
        with patch.object(text_surface, "_glyph_table", lambda: glyphs):
            mode = HiresDisplayMode("normal", use_reu_staged=True)
            api = FakeAPI()
            mode.setup(api)
            buffers = mode.compose(_frame())
            ov = CallsignOverlay(text="HI", corner="top-left")
            ov.setup(api, MagicMock())
            ov.compose(buffers, MagicMock(), 0.0)
            mode.push(api, buffers)
        # the staged bitmap REUWRITE carries the folded glyph bytes
        staged = dict(api.socket_dma.reuwrites)
        bitmap_staged = staged.get(14745600)  # REU_VIDEO_BITMAP_BASE
        self.assertIsNotNone(bitmap_staged)
        self.assertEqual(bitmap_staged[0:8], bytes(glyphs[ord("H") - 0x40]))  # 'H' code 8


class MhiresOverlayTest(unittest.TestCase):
    def test_marquee_double_wide(self):
        glyphs = _synthetic_glyphs()
        with patch.object(text_surface, "_glyph_table", lambda: glyphs):
            mode = MultiHiresDisplayMode("percell")
            api = FakeAPI()
            mode.setup(api)
            buffers = mode.compose(_frame())
            surf = buffers["text"]
            self.assertIsInstance(surf, MHiresTextSurface)
            self.assertEqual((surf.cols, surf.rows), (20, 25))
            ov = MarqueeOverlay(
                text="ABCDEFGHIJKLMNOPQRST", row=0, fg_color="yellow", bg_color="black"
            )
            ov.setup(api, MagicMock())
            ov.compose(buffers, MagicMock(), ov.start_time)  # offset 0 -> 'A' at col 0
        # text cell 0 ('A') -> two hw cells, screen nibble = (bg<<4)|fg = (0<<4)|7
        self.assertEqual(buffers["screen"][0], 0x07)
        self.assertEqual(buffers["screen"][1], 0x07)
        # color RAM (c3) cleared under the text box
        self.assertEqual(buffers["color"][0], 0)
        self.assertEqual(buffers["color"][1], 0)
        mode.push(api, buffers)
        self.assertIn(0x2000, api.regions)

    def test_double_height_grid(self):
        mode = MultiHiresDisplayMode("percell", text_double_height=True)
        api = FakeAPI()
        mode.setup(api)
        buffers = mode.compose(_frame())
        surf = buffers["text"]
        self.assertEqual((surf.cols, surf.rows), (20, 12))

    def test_scrolling_text_static_centered(self):
        glyphs = _synthetic_glyphs()
        with patch.object(text_surface, "_glyph_table", lambda: glyphs):
            mode = MultiHiresDisplayMode("cheap")
            api = FakeAPI()
            mode.setup(api)
            buffers = mode.compose(_frame())
            ov = ScrollingTextOverlay(
                messages=[{"text": "HI", "style": "static", "pre_delay_s": 0.0}], row=5
            )
            ov.setup(api, MagicMock())
            ov.compose(buffers, MagicMock(), ov.start_time)
        # "HI" centered in a 20-col grid -> text cols 9,10 -> hw cells 18..21 of row 5
        row_base = 5 * 40
        painted = buffers["screen"][row_base : row_base + 40]
        self.assertTrue((painted != 0).any(), "static centered text should paint some cells")


if __name__ == "__main__":
    unittest.main()
