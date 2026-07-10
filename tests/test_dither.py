"""Unit tests for c64cast/dither.py's spatial-dither primitives.

bayer_offset (ordered dither) and error_diffuse / error_diffuse_cells
(Floyd-Steinberg / Atkinson) are pure numpy — no C64Backend / hardware
involved — so they're tested directly against synthetic pixel arrays.
"""

from __future__ import annotations

import unittest

import numpy as np

from c64cast import dither


class BayerOffsetTest(unittest.TestCase):
    def test_shape_and_dtype(self):
        off = dither.bayer_offset(200, 160, 0.5)
        self.assertEqual(off.shape, (200, 160))
        self.assertEqual(off.dtype, np.float32)

    def test_tiling_repeats_every_8_pixels(self):
        # The 8x8 Bayer matrix tiles verbatim, so offset[y, x] must equal
        # offset[y + 8, x] and offset[y, x + 8] everywhere the tile fits.
        off = dither.bayer_offset(24, 24, 1.0)
        np.testing.assert_array_equal(off[0:8, 0:8], off[8:16, 0:8])
        np.testing.assert_array_equal(off[0:8, 0:8], off[0:8, 8:16])

    def test_strength_scales_linearly(self):
        off1 = dither.bayer_offset(8, 8, 1.0)
        off2 = dither.bayer_offset(8, 8, 2.0)
        np.testing.assert_allclose(off2, off1 * 2.0)

    def test_zero_strength_is_all_zero(self):
        off = dither.bayer_offset(16, 16, 0.0)
        np.testing.assert_array_equal(off, np.zeros((16, 16), dtype=np.float32))

    def test_mean_matches_bayer_permutation_average(self):
        # The 8x8 Bayer permutation covers 0..63 exactly once, so the
        # normalized (/64 - 0.5) tile's mean is (mean(0..63)/64 - 0.5) — a
        # small fixed negative offset (~-0.0078), scaled by strength*64.
        off = dither.bayer_offset(8, 8, 1.0)
        expected = (31.5 / 64.0 - 0.5) * 64.0
        self.assertAlmostEqual(float(off.mean()), expected, places=4)

    def test_odd_dimensions_dont_crash(self):
        # h/w need not be multiples of 8 (a downscaled cell grid rarely is).
        off = dither.bayer_offset(13, 5, 0.5)
        self.assertEqual(off.shape, (13, 5))


class ErrorDiffuseTest(unittest.TestCase):
    _CANDIDATES = np.array([[0, 0, 0], [255, 255, 255]], dtype=np.float32)  # black, white

    def test_unknown_method_raises(self):
        img = np.zeros((2, 2, 3), dtype=np.float32)
        with self.assertRaises(ValueError):
            dither.error_diffuse(img, self._CANDIDATES, "bogus")

    def test_codes_are_in_range(self):
        rng = np.random.default_rng(0)
        img = rng.uniform(0, 255, size=(9, 7, 3)).astype(np.float32)
        for method in ("floyd-steinberg", "atkinson"):
            with self.subTest(method=method):
                codes = dither.error_diffuse(img, self._CANDIDATES, method)
                self.assertEqual(codes.shape, (9, 7))
                self.assertEqual(codes.dtype, np.uint8)
                self.assertTrue(bool(((codes == 0) | (codes == 1)).all()))

    def test_solid_black_all_zero_solid_white_all_one(self):
        black = np.zeros((4, 4, 3), dtype=np.float32)
        white = np.full((4, 4, 3), 255.0, dtype=np.float32)
        for method in ("floyd-steinberg", "atkinson"):
            with self.subTest(method=method):
                codes_black = dither.error_diffuse(black, self._CANDIDATES, method)
                codes_white = dither.error_diffuse(white, self._CANDIDATES, method)
                np.testing.assert_array_equal(codes_black, np.zeros((4, 4), dtype=np.uint8))
                np.testing.assert_array_equal(codes_white, np.ones((4, 4), dtype=np.uint8))

    def test_mid_gray_dithers_a_mix_of_both_candidates(self):
        # A flat 50% gray field has no information a single nearest-candidate
        # pick could use, so error diffusion should spread the two candidates
        # roughly evenly rather than collapsing to one.
        gray = np.full((16, 16, 3), 127.5, dtype=np.float32)
        codes = dither.error_diffuse(gray, self._CANDIDATES, "floyd-steinberg")
        frac_white = float(codes.mean())
        self.assertTrue(0.3 < frac_white < 0.7, frac_white)

    def test_strength_zero_never_diffuses_error(self):
        # strength=0 means every pixel is judged independently (no error
        # carried to neighbors), so a flat field always resolves to the
        # single nearest candidate for every pixel.
        gray_below_mid = np.full((6, 6, 3), 100.0, dtype=np.float32)
        codes = dither.error_diffuse(gray_below_mid, self._CANDIDATES, "atkinson", strength=0.0)
        np.testing.assert_array_equal(codes, np.zeros((6, 6), dtype=np.uint8))


class ErrorDiffuseCellsTest(unittest.TestCase):
    def test_unknown_method_raises(self):
        pixels = np.zeros((1, 2, 2, 3), dtype=np.float32)
        cand = np.zeros((1, 2, 3), dtype=np.float32)
        with self.assertRaises(ValueError):
            dither.error_diffuse_cells(pixels, cand, "bogus")

    def test_shape_and_code_bounds(self):
        rng = np.random.default_rng(1)
        n, h, w, k = 50, 8, 4, 4
        pixels = rng.uniform(0, 255, size=(n, h, w, 3)).astype(np.float32)
        # Distinct per-cell candidate sets (mirrors mhires' per-cell {bg0,c1,c2,c3}).
        cand = rng.uniform(0, 255, size=(n, k, 3)).astype(np.float32)
        for method in ("floyd-steinberg", "atkinson"):
            with self.subTest(method=method):
                codes = dither.error_diffuse_cells(pixels, cand, method)
                self.assertEqual(codes.shape, (n, h, w))
                self.assertEqual(codes.dtype, np.uint8)
                self.assertTrue(bool((codes < k).all()))

    def test_matches_error_diffuse_for_a_single_cell(self):
        # error_diffuse_cells batched over N=1 cell must reduce to the same
        # result as the single-image error_diffuse primitive (same math,
        # different looping structure).
        rng = np.random.default_rng(2)
        img = rng.uniform(0, 255, size=(6, 5, 3)).astype(np.float32)
        cand = np.array([[10, 20, 30], [200, 210, 220], [100, 90, 80]], dtype=np.float32)
        for method in ("floyd-steinberg", "atkinson"):
            with self.subTest(method=method):
                single = dither.error_diffuse(img, cand, method)
                batched = dither.error_diffuse_cells(img[None], cand[None], method)[0]
                np.testing.assert_array_equal(single, batched)

    def test_no_diffusion_across_cell_boundary(self):
        # Two cells with identical pixel content but DIFFERENT candidate sets
        # must dither independently — a bug that let error leak across the
        # batch (cell) axis would make cell 1's result depend on cell 0's.
        content = np.full((2, 3, 3, 3), 128.0, dtype=np.float32)
        cand_a = np.array([[0, 0, 0], [255, 255, 255]], dtype=np.float32)
        cand_b = np.array([[100, 100, 100], [150, 150, 150]], dtype=np.float32)
        cand = np.stack([cand_a, cand_b])
        codes = dither.error_diffuse_cells(content, cand, "floyd-steinberg")
        # Re-run cell 0 alone; must match regardless of what's batched alongside it.
        solo = dither.error_diffuse_cells(content[:1], cand[:1], "floyd-steinberg")
        np.testing.assert_array_equal(codes[0], solo[0])


if __name__ == "__main__":
    unittest.main()
