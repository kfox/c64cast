"""The hires per-cell foreground pick ([color].hires_cell_pick).

Asserts the PROPERTIES that make error-min the default rather than pinning
pixel bytes: that it beats the single-pixel "sample" read on reconstruction
error, that its hysteresis holds a static subject completely still, and that
the hysteresis is a decision threshold rather than a smoother (real change
still lands on one frame). Exact bytes aren't portable — the argmin over
near-tied palette distances diverges across numpy/BLAS builds, the same reason
test_bitmap_compose.py asserts structure instead.
"""

from __future__ import annotations

import unittest

import cv2
import numpy as np

from c64cast.video import modes as video_modes
from c64cast.video.modes import hires as hires_mod
from c64cast.video.modes.hires import HiresDisplayMode
from c64cast.video.palette import C64_PALETTE_BGR, HIRES_CELL_PICKS


def displayed(buffers) -> np.ndarray:
    """Reconstruct the 320×200 BGR image the VIC renders from a compose()."""
    bits = np.unpackbits(buffers["bitmap"].reshape(25, 40, 8), axis=2).reshape(25, 40, 8, 8)
    is_fg = bits.transpose(0, 2, 1, 3).reshape(200, 320).astype(bool)
    screen = buffers["screen"].reshape(25, 40)
    fg = np.repeat(np.repeat(C64_PALETTE_BGR[screen >> 4], 8, axis=0), 8, axis=1)
    bg = np.repeat(np.repeat(C64_PALETTE_BGR[screen & 0x0F], 8, axis=0), 8, axis=1)
    return np.where(is_fg[..., None], fg, bg).astype(np.float32)


def lab_error(src: np.ndarray, out: np.ndarray) -> float:
    def lab(a):
        u8 = np.clip(a, 0, 255).astype(np.uint8).reshape(-1, 1, 3)
        return cv2.cvtColor(u8, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)

    return float(np.linalg.norm(lab(src) - lab(out), axis=1).mean())


def textured_frame(seed: int = 4) -> np.ndarray:
    """A patchwork with high intra-cell variance.

    The pick strategies only diverge where a cell's own pixels disagree — the
    advantage tracks intra-cell standard deviation almost exactly, from a tie at
    sd≈1 (a smooth ramp, where the centre pixel already represents the cell) to
    ≈-32 % at sd≈73. Real frames sit high on that curve, so a flat or smoothly
    graded fixture would assert nothing.
    """
    rng = np.random.default_rng(seed)
    patches = rng.integers(0, 255, (10, 16, 3))
    return np.kron(patches, np.ones((20, 20, 1))).astype(np.uint8)


class CellPickTest(unittest.TestCase):
    def test_vocabulary_matches_the_config_choices(self):
        # config.py validates against palette.HIRES_CELL_PICKS without importing
        # the modes tree; this is the pin that keeps the two in step.
        for pick in HIRES_CELL_PICKS:
            HiresDisplayMode("normal", cell_pick=pick)

    def test_rejects_an_unknown_pick(self):
        with self.assertRaises(ValueError):
            HiresDisplayMode("normal", cell_pick="nearest")

    def test_error_min_beats_sample(self):
        src = textured_frame()
        err = {
            pick: lab_error(src, displayed(HiresDisplayMode("normal", cell_pick=pick).compose(src)))
            for pick in HIRES_CELL_PICKS
        }
        self.assertLess(err["error-min"], err["sample"] * 0.95)

    def test_error_min_is_the_default(self):
        self.assertEqual(HiresDisplayMode("normal")._cell_pick, "error-min")

    def test_both_picks_produce_well_formed_buffers(self):
        src = textured_frame()
        for pick in HIRES_CELL_PICKS:
            with self.subTest(pick=pick):
                b = HiresDisplayMode("normal", cell_pick=pick).compose(src)
                self.assertEqual(b["bitmap"].shape, (8000,))
                self.assertEqual(b["screen"].shape, (1000,))
                self.assertEqual(b["screen"].dtype, np.uint8)
                self.assertEqual(b["bitmap"].dtype, np.uint8)

    def test_hysteresis_holds_a_noisy_static_subject_still(self):
        """The property the default rests on: a static subject under sensor
        noise must stop rewriting screen bytes entirely."""
        rng = np.random.default_rng(9)
        base = textured_frame().astype(np.float32)
        mode = HiresDisplayMode("normal", cell_pick="error-min")
        prev = None
        changed = []
        for _ in range(6):
            noisy = np.clip(base + rng.normal(0, 6, base.shape), 0, 255).astype(np.uint8)
            screen = mode.compose(noisy)["screen"]
            if prev is not None:
                changed.append(int((screen != prev).sum()))
            prev = screen.copy()
        self.assertEqual(max(changed), 0, f"static subject churned: {changed}")

    def test_hysteresis_releases_on_a_real_change_in_one_frame(self):
        """A decision hysteresis, not a smoother — no motion lag."""
        mode = HiresDisplayMode("normal", cell_pick="error-min")
        warm = textured_frame()
        mode.compose(warm)
        cold = textured_frame(seed=17)  # unrelated content: every cell changes
        after_one = mode.compose(cold)["screen"].copy()
        after_two = mode.compose(cold)["screen"]
        np.testing.assert_array_equal(
            after_one, after_two, "pick had not settled after a single frame"
        )

    def test_live_swap_clears_the_previous_pick(self):
        mode = HiresDisplayMode("normal", cell_pick="error-min")
        mode.compose(textured_frame())
        self.assertIsNotNone(mode._last_fg)
        self.assertEqual(mode.set_cell_pick("sample"), "cell_pick=sample")
        self.assertIsNone(mode._last_fg)

    def test_cell_pick_is_a_live_choice(self):
        self.assertEqual(HiresDisplayMode.LIVE_CHOICES["cell_pick"], HIRES_CELL_PICKS)

    def test_edges_styles_ignore_the_pick(self):
        """Fixed 2-colour styles pick no colour, so the knob must be inert."""
        src = textured_frame()
        for style in ("edges", "edges_inverted"):
            with self.subTest(style=style):
                a = HiresDisplayMode(style, cell_pick="error-min").compose(src)
                b = HiresDisplayMode(style, cell_pick="sample").compose(src)
                np.testing.assert_array_equal(a["screen"], b["screen"])
                np.testing.assert_array_equal(a["bitmap"], b["bitmap"])

    def test_every_dither_method_still_composes_under_error_min(self):
        src = textured_frame()
        for dither in ("none", "ordered", "blue_noise", "floyd-steinberg", "atkinson"):
            with self.subTest(dither=dither):
                b = HiresDisplayMode("normal", cell_pick="error-min", dither_method=dither).compose(
                    src
                )
                self.assertEqual(b["bitmap"].shape, (8000,))
                self.assertEqual(b["screen"].shape, (1000,))

    def test_hysteresis_bonus_scales_with_the_lab_metric(self):
        """Lab d² runs ~1/3 the magnitude of weighted-BGR d² for the same gap,
        so a shared threshold has to be rescaled or it means something different
        under each metric — the convention base.py's percell bonuses follow."""
        self.assertGreater(hires_mod.HIRES_CELL_HYSTERESIS_BONUS, 0.0)
        src = textured_frame()
        for perceptual in (False, True):
            with self.subTest(perceptual=perceptual):
                mode = HiresDisplayMode("normal", cell_pick="error-min", perceptual=perceptual)
                mode.compose(src)
                self.assertIsNotNone(mode._last_fg)

    def test_exported_from_the_modes_package(self):
        self.assertIs(video_modes.HiresDisplayMode, HiresDisplayMode)


if __name__ == "__main__":
    unittest.main()
