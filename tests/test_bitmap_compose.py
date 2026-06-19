"""Regression guard for the bitmap-mode compose/push split.

HiresDisplayMode + MultiHiresDisplayMode were refactored from a single
render() into compose() (build bitmap/screen/color buffers + a text surface)
+ push() (host-DMA or REU bank-swap upload), so overlays can fold text into
the buffers before they go to the U64. With no overlay attached, the
production render path (_render_with_overlays, which takes the compose+push
branch for these modes) must still produce the expected writes.

This asserts STRUCTURE (which regions get written, with what lengths) +
DETERMINISM (same frame → identical bytes) + a hand-computable absolute case,
rather than pinning machine-specific pixel hashes: the percell quantization's
per-pixel argmin over near-tied palette distances diverges by a few cells
across numpy/BLAS builds, so exact pixel bytes aren't portable across CI
runners — only their structure and per-machine determinism are.
"""

# FakeAPI is a structural stand-in (not a nominal C64Backend) — fake at the
# boundary, same pattern as test_overlays.py / test_bitmap_overlays.py.
# pyright: reportArgumentType=false
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _fakes import FakeAPI  # noqa: E402

from c64cast.modes import (  # noqa: E402
    FRAME_TRACKER_ADDR,
    FRAME_TRACKER_LEN,
    MHIRES_FRAME_TRACKER_LEN,
    REU_VIDEO_BITMAP_BASE,
    REU_VIDEO_BITMAP_COLOR_BASE,
    REU_VIDEO_BITMAP_SCREEN_BASE,
    HiresDisplayMode,
    MultiHiresDisplayMode,
)
from c64cast.scenes import Scene, _render_with_overlays  # noqa: E402

BITMAP_ADDR = 0x2000
SCREEN_ADDR = 0x0400
COLOR_ADDR = 0xD800


def _frame() -> np.ndarray:
    """Deterministic synthetic BGR gradient (240x320)."""
    h, w = 240, 320
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    b = xx / w * 255
    g = yy / h * 255
    r = (xx + yy) / (h + w) * 255
    return np.clip(np.stack([b, g, r], axis=-1), 0, 255).astype(np.uint8)


def _render(mode, api, frame=None) -> None:
    scene = cast(Scene, SimpleNamespace(effect=None))
    _render_with_overlays(mode, api, _frame() if frame is None else frame, [], 0.0, scene)


_HIRES = [("normal",), ("edges",), ("edges_inverted",)]
_MHIRES = [("percell",), ("cheap",), ("grayscale",)]


class BitmapStructureTest(unittest.TestCase):
    """compose+push writes exactly the regions each mode owns, full-length."""

    def test_hires_writes_bitmap_and_screen_only(self):
        for args in _HIRES:
            with self.subTest(style=args[0]):
                mode = HiresDisplayMode(*args)
                api = FakeAPI()
                mode.setup(api)
                _render(mode, api)
                self.assertEqual(len(api.regions[BITMAP_ADDR]), 8000)
                self.assertEqual(len(api.regions[SCREEN_ADDR]), 1000)
                # hires carries color in the screen nibble — no $D800 write.
                self.assertNotIn(COLOR_ADDR, api.regions)

    def test_mhires_writes_bitmap_screen_color(self):
        for args in _MHIRES:
            with self.subTest(palette_mode=args[0]):
                mode = MultiHiresDisplayMode(*args)
                api = FakeAPI()
                mode.setup(api)
                _render(mode, api)
                self.assertEqual(len(api.regions[BITMAP_ADDR]), 8000)
                self.assertEqual(len(api.regions[SCREEN_ADDR]), 1000)
                self.assertEqual(len(api.regions[COLOR_ADDR]), 1000)


class BitmapDeterminismTest(unittest.TestCase):
    """Same frame through two fresh instances → identical bytes (catches state
    leakage / non-determinism in the compose path). Portable: compares two
    runs on the same machine, not against a pinned hash."""

    def _regions(self, cls, args) -> dict[int, bytes]:
        api = FakeAPI()
        mode = cls(*args)
        mode.setup(api)
        _render(mode, api)
        return dict(api.regions)

    def test_hires_deterministic(self):
        for args in _HIRES:
            with self.subTest(style=args[0]):
                self.assertEqual(
                    self._regions(HiresDisplayMode, args),
                    self._regions(HiresDisplayMode, args),
                )

    def test_mhires_deterministic(self):
        for args in _MHIRES:
            with self.subTest(palette_mode=args[0]):
                self.assertEqual(
                    self._regions(MultiHiresDisplayMode, args),
                    self._regions(MultiHiresDisplayMode, args),
                )

    def test_repeated_frame_stable(self):
        # A second identical frame on the same instance must not drift (the
        # EMA/hysteresis state converges to the same output for a static frame).
        for cls, args in [(HiresDisplayMode, a) for a in _HIRES] + [
            (MultiHiresDisplayMode, a) for a in _MHIRES
        ]:
            with self.subTest(mode=cls.__name__, args=args):
                api = FakeAPI()
                mode = cls(*args)
                mode.setup(api)
                _render(mode, api)
                first = dict(api.regions)
                _render(mode, api)
                self.assertEqual(dict(api.regions), first)


class BitmapAbsoluteCaseTest(unittest.TestCase):
    """Hand-computable, environment-independent: a solid-black frame in
    hires-edges has no Canny edges, so the bitmap is empty and every cell's
    screen nibble is FG=1 / BG=0."""

    def test_hires_edges_black_frame_is_blank(self):
        mode = HiresDisplayMode("edges")
        api = FakeAPI()
        mode.setup(api)
        black = np.zeros((240, 320, 3), dtype=np.uint8)
        _render(mode, api, black)
        self.assertEqual(api.regions[BITMAP_ADDR], bytes(8000))  # no edges set
        self.assertEqual(api.regions[SCREEN_ADDR], bytes([0x10] * 1000))  # (1<<4)|0
        self.assertEqual(api.regs.get("D020"), (0, 0))


class BitmapREUStructureTest(unittest.TestCase):
    """REU bank-swap push stages the right REU regions + a frame tracker."""

    def test_hires_reu_staging(self):
        mode = HiresDisplayMode("normal", use_reu_staged=True)
        api = FakeAPI()
        mode.setup(api)
        _render(mode, api)
        staged = dict(api.socket_dma.reuwrites)
        self.assertEqual(len(staged[REU_VIDEO_BITMAP_BASE]), 8000)
        self.assertEqual(len(staged[REU_VIDEO_BITMAP_SCREEN_BASE]), 1000)
        self.assertNotIn(REU_VIDEO_BITMAP_COLOR_BASE, staged)  # hires has no color RAM
        self.assertEqual(len(api.mem_files[f"{FRAME_TRACKER_ADDR:04X}"]), FRAME_TRACKER_LEN)

    def test_mhires_reu_staging(self):
        mode = MultiHiresDisplayMode("percell", use_reu_staged=True)
        api = FakeAPI()
        mode.setup(api)
        _render(mode, api)
        staged = dict(api.socket_dma.reuwrites)
        self.assertEqual(len(staged[REU_VIDEO_BITMAP_BASE]), 8000)
        self.assertEqual(len(staged[REU_VIDEO_BITMAP_SCREEN_BASE]), 1000)
        self.assertEqual(len(staged[REU_VIDEO_BITMAP_COLOR_BASE]), 1000)
        self.assertEqual(len(api.mem_files[f"{FRAME_TRACKER_ADDR:04X}"]), MHIRES_FRAME_TRACKER_LEN)


if __name__ == "__main__":
    unittest.main()
