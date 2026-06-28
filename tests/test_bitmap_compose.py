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

from c64cast.c64 import VIC_BANK_0, VIC_BANK_2  # noqa: E402
from c64cast.modes import (  # noqa: E402
    BANK_SWAP_IRQ_HANDLER_ADDR,
    FRAME_TRACKER_ADDR,
    FRAME_TRACKER_LEN,
    HOSTDMA_SWAP_IRQ_HANDLER,
    HOSTDMA_TRACKER_LEN,
    HOSTDMA_TRACKER_OFF_READY,
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


class FrameTargetSizeTest(unittest.TestCase):
    """`frame_target_size` is the single source of truth for both the compose
    resize AND the video decoder's downscale plan, so it must equal the size
    compose actually downscales to. This guards against the two drifting (a
    decode plan keyed off a stale target would under/over-decode)."""

    def test_hires_target_matches_compose(self):
        mode = HiresDisplayMode("normal")
        self.assertEqual(mode.frame_target_size, (320, 200))
        buffers = mode.compose(_frame())
        # 320×200 hires → 8000-byte bitmap (25 rows × 40 cells × 8 bytes).
        self.assertEqual(len(buffers["bitmap"]), 8000)

    def test_mhires_target_is_anamorphic(self):
        # Width 160 < height 200: the planner must honor the taller axis.
        self.assertEqual(MultiHiresDisplayMode("percell").frame_target_size, (160, 200))

    def test_blank_has_no_target(self):
        from c64cast.modes import BlankDisplayMode

        self.assertIsNone(BlankDisplayMode().frame_target_size)


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


class BitmapEngageFlashTest(unittest.TestCase):
    """Single-buffer (non-REU) setup must clear BOTH the $2000 bitmap and screen
    RAM ($0400) BEFORE flipping $D011 into bitmap mode, so a bitmap scene doesn't
    flash a colour ghost of the prior scene on engage. In hires a zeroed bitmap
    selects each cell's BG nibble from $0400 — stale $0400 would render a 40×25
    ghost; zeroing it forces solid black until the first frame repaints."""

    def _ops_through_d011(self, mode) -> list[tuple]:
        api = FakeAPI()
        mode.setup(api)
        return api.ops

    def _assert_clears_before_bitmap_flip(self, ops):
        def idx(pred):
            return next(i for i, op in enumerate(ops) if pred(op))

        bitmap_zero = idx(
            lambda op: op[0] == "write_memory_file" and op[1] == "2000" and set(op[2]) == {0}
        )
        screen_zero = idx(
            lambda op: op[0] == "write_memory_file" and op[1] == "0400" and set(op[2]) == {0}
        )
        d011_flip = idx(lambda op: op[0] == "write_memory" and op[1] == "D011" and op[2] == "3b")
        self.assertLess(bitmap_zero, d011_flip, "bitmap zeroed after $D011 flip")
        self.assertLess(screen_zero, d011_flip, "screen RAM zeroed after $D011 flip")

    def test_hires_clears_bitmap_and_screen_before_flip(self):
        for args in _HIRES:
            with self.subTest(style=args[0]):
                self._assert_clears_before_bitmap_flip(
                    self._ops_through_d011(HiresDisplayMode(*args))
                )

    def test_mhires_clears_bitmap_and_screen_before_flip(self):
        for args in _MHIRES:
            with self.subTest(palette_mode=args[0]):
                self._assert_clears_before_bitmap_flip(
                    self._ops_through_d011(MultiHiresDisplayMode(*args))
                )


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


class PercellFillerSafetyTest(unittest.TestCase):
    """percell must never emit a screen/color-RAM color the cell doesn't
    actually contain. The per-cell top-3 picker grabs the 3 highest counts;
    when a cell has fewer than 3 distinct non-bg0 colors (the common case —
    mostly-bg0 cells, and the norm under a small forced palette) the surplus
    slots used to hold arbitrary ZERO-count palette indices. Those leaked an
    out-of-palette color (e.g. green into a black/purple/blue cast) that the
    VIC briefly rendered during the non-atomic screen/color/bitmap write tear
    on a slow transport (TeensyROM serial). The fix pads absent slots with
    bg0, so screen/color RAM only ever carries genuinely-present colors."""

    def _compose_from_targets(self, targets: np.ndarray):
        """Drive _compose_percell with a distance matrix whose per-pixel argmin
        is exactly `targets` (32000,), clean-margin one-hot — no quantization
        ambiguity, so the present set is exactly set(targets)."""
        mode = MultiHiresDisplayMode("percell")
        d = np.full((32000, 16), 1e6, dtype=np.float32)
        d[np.arange(32000), targets] = 0.0
        return mode._compose_percell(d)

    def test_no_color_outside_present_set(self):
        # Mostly black (index 0) with a few accent pixels from a 4-color cast
        # {0,4,6,14} — every other cell is all-black, the case that produced
        # garbage fillers. Spread the accents across distinct cells.
        targets = np.zeros(32000, dtype=np.int64)
        targets[10] = 4  # purple
        targets[8000] = 6  # blue
        targets[16000] = 14  # light blue
        targets[24000] = 4
        present = {0, 4, 6, 14}

        _bitmap, screen, color, bg0 = self._compose_from_targets(targets)
        self.assertEqual(bg0, 0)  # black dominates
        seen = (
            set(np.asarray(screen) >> 4) | set(np.asarray(screen) & 0x0F) | set(np.asarray(color))
        )
        self.assertTrue(
            seen <= present,
            f"screen/color RAM carried colors outside the present set: {sorted(seen - present)}",
        )

    def test_all_bg0_cell_is_solid(self):
        # A wholly-black frame: every cell must collapse to solid bg0 in both
        # screen nibbles and color RAM (this is the letterboxed-edge case that
        # flashed as a "border"). Pre-fix the fillers were random indices.
        _bitmap, screen, color, bg0 = self._compose_from_targets(np.zeros(32000, dtype=np.int64))
        self.assertEqual(bg0, 0)
        self.assertEqual(set(np.asarray(screen)), {0})
        self.assertEqual(set(np.asarray(color)), {0})


class PercellBg0HysteresisTest(unittest.TestCase):
    """bg0 (the %00 colour written to $D021) must not strobe when two colours
    are near-tied for most-populated — otherwise the background + the pillarbox
    bars flash a different colour every frame, very visible on the TR's slow,
    non-atomic transport. bg0 stays sticky until a *sustained* dominant shift
    clears the relative margin; see modes.BG0_HYSTERESIS_MARGIN."""

    def _bg0(self, mode: MultiHiresDisplayMode, targets: np.ndarray) -> int:
        """Run one percell frame whose per-pixel argmin is exactly `targets`
        and return the chosen bg0. Reuses `mode` so EMA + sticky bg0 persist."""
        d = np.full((32000, 16), 1e6, dtype=np.float32)
        d[np.arange(32000), targets] = 0.0
        return mode._compose_percell(d)[3]

    @staticmethod
    def _split(n_black: int, fill: int) -> np.ndarray:
        t = np.full(32000, fill, dtype=np.int64)
        t[:n_black] = 0
        return t

    def test_slight_majority_does_not_flip_bg0(self):
        mode = MultiHiresDisplayMode("percell")
        # Establish bg0 = black.
        self.assertEqual(self._bg0(mode, np.zeros(32000, dtype=np.int64)), 0)
        # Blue (6) now holds a slight, sustained majority (17000 vs 15000):
        # 17000/15000 ≈ 1.13 < 1 + BG0_HYSTERESIS_MARGIN (1.25), so bg0 must
        # stay black on every frame even after the EMA tips blue ahead, rather
        # than strobing $D021 the instant blue edges past.
        slight = self._split(15000, 6)
        for _ in range(15):
            self.assertEqual(self._bg0(mode, slight), 0)

    def test_sustained_dominant_change_flips_bg0(self):
        mode = MultiHiresDisplayMode("percell")
        self.assertEqual(self._bg0(mode, np.zeros(32000, dtype=np.int64)), 0)
        # An overwhelming, sustained colour change MUST still move bg0 (we damp
        # jitter, not real cuts): a few all-blue frames clear the margin.
        all_blue = np.full(32000, 6, dtype=np.int64)
        seen = [self._bg0(mode, all_blue) for _ in range(6)]
        self.assertEqual(seen[-1], 6, f"bg0 never tracked the sustained change: {seen}")


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


class BitmapHostDmaDoubleBufferTest(unittest.TestCase):
    """Host-DMA double-buffer (no-REU backends, e.g. TeensyROM): setup installs
    the minimal vblank swap IRQ + zeroes both VIC banks; push writes the
    OFF-screen bank (per-bank delta) + arms a 3-byte ready tracker with NO REU;
    teardown restores the kernal IRQ vector + bank 0."""

    def test_setup_installs_handler_and_zeroes_both_banks(self):
        mode = MultiHiresDisplayMode("percell", double_buffer=True)
        api = FakeAPI()
        mode.setup(api)
        # Minimal handler uploaded at $C500 and $0314 hooked to it.
        self.assertEqual(
            api.mem_files[f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}"], HOSTDMA_SWAP_IRQ_HANDLER
        )
        self.assertEqual(
            api.regs["0314"],
            (BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF, (BANK_SWAP_IRQ_HANDLER_ADDR >> 8) & 0xFF),
        )
        # Both banks' bitmap+screen pre-zeroed; VIC pinned to bank 0 ($DD00=$97).
        self.assertEqual(api.mem_files[f"{VIC_BANK_0.BITMAP:04X}"], bytes(8000))
        self.assertEqual(api.mem_files[f"{VIC_BANK_2.BITMAP:04X}"], bytes(8000))
        self.assertEqual(api.memories["DD00"], "97")

    def test_mhires_push_writes_offscreen_bank_and_arms_tracker(self):
        mode = MultiHiresDisplayMode("percell", double_buffer=True)
        api = FakeAPI()
        mode.setup(api)
        _render(mode, api)
        # First frame stages the OFF-screen bank (bank 2), leaving the visible
        # bank 0 untouched by the per-frame push.
        self.assertEqual(len(api.regions[VIC_BANK_2.BITMAP]), 8000)
        self.assertEqual(len(api.regions[VIC_BANK_2.SCREEN]), 1000)
        self.assertEqual(len(api.regions[COLOR_ADDR]), 1000)  # shared $D800
        self.assertNotIn(VIC_BANK_0.BITMAP, api.regions)
        # No REU on this path.
        self.assertEqual(dict(api.socket_dma.reuwrites), {})
        # 3-byte tracker armed (ready = 1); displayed bank toggled to bank 2.
        tracker = api.mem_files[f"{FRAME_TRACKER_ADDR:04X}"]
        self.assertEqual(len(tracker), HOSTDMA_TRACKER_LEN)
        self.assertEqual(tracker[HOSTDMA_TRACKER_OFF_READY], 1)
        self.assertEqual(mode._displayed_bank, 1)

    def test_hires_push_offscreen_bank_no_color_ram(self):
        mode = HiresDisplayMode("normal", double_buffer=True)
        api = FakeAPI()
        mode.setup(api)
        _render(mode, api)
        self.assertEqual(len(api.regions[VIC_BANK_2.BITMAP]), 8000)
        self.assertEqual(len(api.regions[VIC_BANK_2.SCREEN]), 1000)
        self.assertNotIn(COLOR_ADDR, api.regions)  # hires carries color in screen nibbles
        tracker = api.mem_files[f"{FRAME_TRACKER_ADDR:04X}"]
        self.assertEqual(len(tracker), HOSTDMA_TRACKER_LEN)
        self.assertEqual(tracker[HOSTDMA_TRACKER_OFF_READY], 1)

    def test_second_frame_targets_other_bank(self):
        # Alternation: frame 1 → bank 2, frame 2 → bank 0 (each bank diffs
        # against its own prior content via its own region id).
        mode = MultiHiresDisplayMode("percell", double_buffer=True)
        api = FakeAPI()
        mode.setup(api)
        _render(mode, api)
        self.assertEqual(mode._displayed_bank, 1)
        _render(mode, api)
        self.assertEqual(mode._displayed_bank, 0)
        self.assertIn(VIC_BANK_0.BITMAP, api.regions)  # bank 0 written on frame 2

    def test_teardown_restores_kernal_vector(self):
        mode = MultiHiresDisplayMode("percell", double_buffer=True)
        api = FakeAPI()
        mode.setup(api)
        _render(mode, api)
        mode.teardown(api)
        # $0314 → kernal $EA31 (lo=$31, hi=$EA); $DD00 → bank 0.
        self.assertEqual(api.regs["0314"], (0x31, 0xEA))
        self.assertEqual(api.memories["DD00"], "97")


if __name__ == "__main__":
    unittest.main()
