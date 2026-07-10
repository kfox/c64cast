"""Tests for the mhires per-cell color-selection strategies.

Covers the pure picker (modes._pick_cell_colors) semantics for each strategy,
the key invariant that error-min never loses to frequency in reconstruction
error, and the end-to-end wiring through MultiHiresDisplayMode.compose().
"""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from c64cast.modes import (  # noqa: E402
    ERROR_MIN_POOL_SIZE,
    MultiHiresDisplayMode,
    _pick_cell_colors,
)
from c64cast.palette import PALETTE_LUMA  # noqa: E402


def _cell_counts_from_present(present: dict[int, float], bg0: int) -> np.ndarray:
    """A (1, 16) smoothed-count row with `present` colors populated and bg0
    masked to -1 (the state _compose_percell hands the picker)."""
    counts = np.zeros((1, 16), dtype=np.float32)
    for idx, cnt in present.items():
        counts[0, idx] = cnt
    counts[0, bg0] = -1.0
    return counts


def _cell_error(picks: np.ndarray, d_cell: np.ndarray, bg0: int) -> np.ndarray:
    """Per-cell summed reconstruction error for a candidate set {bg0}+picks:
    Σ_pixels min over the 4 candidates of the pixel's palette distance."""
    cand = np.concatenate([np.full((picks.shape[0], 1), bg0), picks], axis=1)  # (n, 4)
    d_cand = np.take_along_axis(d_cell, cand[:, None, :], axis=2)  # (n, 32, 4)
    return d_cand.min(axis=2).sum(axis=1)  # (n,)


class PickCellColorsSemanticsTest(unittest.TestCase):
    """The luminance/contrast strategies order a cell's present colors by luma
    and pick documented extremes; frequency picks the highest counts."""

    def setUp(self):
        # Dummy d_cell — only error-min reads it.
        self.d_cell = np.zeros((1, 32, 16), dtype=np.float32)

    def test_frequency_picks_the_three_highest_counts(self):
        counts = _cell_counts_from_present({6: 10, 12: 8, 1: 6, 9: 1}, bg0=0)
        picks = _pick_cell_colors(counts, self.d_cell, 0, "frequency")
        self.assertEqual(set(picks[0].tolist()), {6, 12, 1})

    def test_luminance_picks_darkest_median_brightest(self):
        # luma: 6=19.4 (dark), 11=51, 9=70.4 (median of 5), 4=124.2, 1=255 (bright)
        counts = _cell_counts_from_present({6: 5, 11: 5, 9: 5, 4: 5, 1: 5}, bg0=0)
        picks = _pick_cell_colors(counts, self.d_cell, 0, "luminance")
        # darkest=6, brightest=1, median (index 2 of the 5 sorted by luma)=9
        self.assertEqual(set(picks[0].tolist()), {6, 9, 1})

    def test_contrast_picks_extremes_plus_farthest(self):
        counts = _cell_counts_from_present({6: 5, 11: 5, 9: 5, 4: 5, 1: 5}, bg0=0)
        picks = _pick_cell_colors(counts, self.d_cell, 0, "contrast")
        # extremes 6 & 1; farthest-from-both in luma among {11,9,4} is 4 (124.2)
        self.assertEqual(set(picks[0].tolist()), {6, 4, 1})

    def test_luminance_and_contrast_can_differ(self):
        counts = _cell_counts_from_present({6: 5, 11: 5, 9: 5, 4: 5, 1: 5}, bg0=0)
        lum = _pick_cell_colors(counts, self.d_cell, 0, "luminance")
        con = _pick_cell_colors(counts, self.d_cell, 0, "contrast")
        self.assertNotEqual(set(lum[0].tolist()), set(con[0].tolist()))

    def test_absent_slots_fall_back_to_bg0(self):
        # Only one present non-bg0 color → other two slots poison-guarded to bg0.
        # error-min needs a d_cell where color 5 genuinely reduces error, else
        # (all-equal distances) any trio is optimal and 5 need not be chosen.
        d_cell = np.full((1, 32, 16), 200.0, dtype=np.float32)
        d_cell[:, :, 5] = 0.0  # every pixel is exactly color 5
        for strat in ("frequency", "luminance", "contrast", "error-min"):
            with self.subTest(strat=strat):
                counts = _cell_counts_from_present({5: 7}, bg0=0)
                picks = _pick_cell_colors(counts, d_cell, 0, strat)
                vals = picks[0].tolist()
                self.assertIn(5, vals)
                self.assertEqual(sum(v == 0 for v in vals), 2)  # two bg0 fillers

    def test_ordering_matches_palette_luma_constant(self):
        # Guards the darkest/brightest identification against a PALETTE_LUMA
        # regression: the min/max-luma present colors must be the picks.
        counts = _cell_counts_from_present({3: 5, 6: 5, 10: 5}, bg0=0)
        picks = set(_pick_cell_colors(counts, self.d_cell, 0, "contrast")[0].tolist())
        idxs = [3, 6, 10]
        darkest = min(idxs, key=lambda k: float(PALETTE_LUMA[k]))
        brightest = max(idxs, key=lambda k: float(PALETTE_LUMA[k]))
        self.assertIn(darkest, picks)
        self.assertIn(brightest, picks)


class ErrorMinInvariantTest(unittest.TestCase):
    """error-min evaluates every C(K,3) trio over each cell's top-K colors; the
    frequency top-3 is always one of those trios, so error-min's reconstruction
    error can never exceed frequency's on the same cell."""

    def test_error_min_never_worse_than_frequency(self):
        rng = np.random.default_rng(1234)
        n = 500
        cell_counts = rng.random((n, 16), dtype=np.float32) * 20.0
        d_cell = rng.random((n, 32, 16), dtype=np.float32) * 1000.0
        bg0 = 0
        cc = cell_counts.copy()
        cc[:, bg0] = -1.0
        freq = _pick_cell_colors(cc, d_cell, bg0, "frequency")
        emin = _pick_cell_colors(cc, d_cell, bg0, "error-min")
        freq_err = _cell_error(freq, d_cell, bg0)
        emin_err = _cell_error(emin, d_cell, bg0)
        # Allow a hair of fp slack; error-min must be ≤ frequency everywhere.
        self.assertTrue(np.all(emin_err <= freq_err + 1e-3))
        # And strictly better on at least some cells (the whole point).
        self.assertTrue(np.any(emin_err < freq_err - 1.0))

    def test_pool_size_covers_frequency_top3(self):
        self.assertGreaterEqual(ERROR_MIN_POOL_SIZE, 3)


def _gradient(h: int = 200, w: int = 160) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    b = xx / w * 255
    g = yy / h * 255
    r = (xx + yy) / (h + w) * 255
    return np.clip(np.stack([b, g, r], axis=-1), 0, 255).astype(np.uint8)


class ComposeIntegrationTest(unittest.TestCase):
    """Each strategy drives MultiHiresDisplayMode.compose() to valid buffers,
    and the non-default strategies actually change the output vs frequency."""

    def test_all_strategies_produce_valid_buffers(self):
        frame = _gradient()
        for strat in ("frequency", "luminance", "contrast", "error-min"):
            with self.subTest(strat=strat):
                mode = MultiHiresDisplayMode("percell", cell_strategy=strat)
                b = mode.compose(frame)
                self.assertEqual(len(b["bitmap"]), 8000)
                self.assertEqual(len(b["screen"]), 1000)
                self.assertEqual(len(b["color"]), 1000)
                self.assertTrue(0 <= int(b["bg"]) <= 15)
                self.assertTrue((b["screen"] <= 0xFF).all())
                self.assertTrue((b["color"] < 16).all())

    def test_strategies_differ_from_frequency_on_busy_content(self):
        # A smooth gradient gives each 4×8 cell ≤3 colors, where every strategy
        # picks the same set. High-entropy content puts >3 colors in many cells,
        # so tonal-extreme strategies diverge from the frequency ranking.
        rng = np.random.default_rng(7)
        frame = rng.integers(0, 256, (200, 160, 3), dtype=np.uint8)
        base = MultiHiresDisplayMode("percell", cell_strategy="frequency").compose(frame)
        for strat in ("luminance", "contrast", "error-min"):
            with self.subTest(strat=strat):
                other = MultiHiresDisplayMode("percell", cell_strategy=strat).compose(frame)
                differs = (base["screen"] != other["screen"]).any() or (
                    base["color"] != other["color"]
                ).any()
                self.assertTrue(differs)

    def test_invalid_strategy_rejected(self):
        with self.assertRaisesRegex(ValueError, "cell_strategy"):
            MultiHiresDisplayMode("percell", cell_strategy="median")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
