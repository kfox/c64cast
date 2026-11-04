"""The metric math behind scripts/diags/mhires_video_eval.py and
video_cut_eval.py: the numbers those tools report are only as good as these."""

from __future__ import annotations

import unittest

import numpy as np

from scripts.diags import _mhires_metrics as m
from scripts.diags._cut_detectors import AdaptiveRatio, FfmpegScene


def _flat(bgr: tuple[int, int, int]) -> np.ndarray:
    return np.tile(np.array(bgr, np.uint8), (200, 320, 1))


class OklabTest(unittest.TestCase):
    def test_white_and_black_land_on_the_published_lightness(self):
        self.assertAlmostEqual(float(m.oklab(_flat((255, 255, 255)))[0, 0, 0]), 1.0, places=3)
        np.testing.assert_allclose(m.oklab(_flat((0, 0, 0)))[0, 0], 0.0, atol=1e-6)

    def test_gray_has_no_chroma(self):
        lab = m.oklab(_flat((128, 128, 128)))[0, 0]
        np.testing.assert_allclose(lab[1:], 0.0, atol=1e-4)


class FidelityTest(unittest.TestCase):
    def test_identical_frames_score_zero_error_and_unit_ssim(self):
        img = np.random.default_rng(1).integers(0, 256, (200, 320, 3), dtype=np.uint8)
        lab = m.blurred_oklab(img)
        self.assertEqual(m.oklab_mse(lab, lab), 0.0)
        self.assertAlmostEqual(m.ssim(lab[..., 0], lab[..., 0]), 1.0, places=6)

    def test_error_of_two_flat_frames_is_their_squared_oklab_distance(self):
        a, b = _flat((0, 0, 0)), _flat((255, 255, 255))
        self.assertAlmostEqual(m.oklab_mse(m.blurred_oklab(a), m.blurred_oklab(b)), 1.0, places=3)

    def test_blur_forgives_a_fine_dither_that_averages_to_the_target(self):
        checker = np.indices((200, 320)).sum(axis=0) % 2
        dither = np.where(checker[..., None] == 1, 255, 0).astype(np.uint8).repeat(3, axis=2)
        mid_linear = round(255 * ((0.5 ** (1 / 2.4)) * 1.055 - 0.055))
        target = _flat((mid_linear, mid_linear, mid_linear))
        blurred = m.oklab_mse(m.blurred_oklab(target), m.blurred_oklab(dither))
        sharp = m.oklab_mse(m.oklab(target), m.oklab(dither))
        self.assertLess(blurred, sharp / 100)


class CellsTest(unittest.TestCase):
    def test_one_changed_pixel_marks_exactly_its_cell(self):
        a = _flat((10, 20, 30))
        b = a.copy()
        b[8 * 3 + 5, 8 * 7 + 2] = (0, 0, 0)
        changed = m.cells_changed(a, b)
        self.assertEqual(changed.shape, (25, 40))
        self.assertEqual(list(zip(*np.nonzero(changed), strict=True)), [(3, 7)])

    def test_cell_mean_delta_averages_over_the_cell(self):
        a = np.zeros((200, 320, 3), np.float32)
        b = a.copy()
        b[0:8, 0:4, 0] = 1.0
        self.assertAlmostEqual(float(m.cell_mean_delta(a, b)[0, 0]), 0.5)
        self.assertEqual(float(m.cell_mean_delta(a, b)[0, 1]), 0.0)


class PostCutWindowsTest(unittest.TestCase):
    def test_window_stops_at_the_next_cut_and_the_clip_end(self):
        self.assertEqual(
            m.post_cut_windows(100, [90, 10, 25], window=30),
            [range(10, 25), range(25, 55), range(90, 100)],
        )

    def test_cuts_outside_the_clip_and_repeats_are_dropped(self):
        self.assertEqual(m.post_cut_windows(50, [-1, 50, 5, 5], window=3), [range(5, 8)])


class MatchEventsTest(unittest.TestCase):
    def test_tolerance_and_one_to_one(self):
        match = m.match_events([10, 11, 30, 52], [10, 50, 70], tolerance=1)
        self.assertEqual(match.true_positives, [(10, 10)])
        self.assertEqual(sorted(match.false_positives), [11, 30, 52])
        self.assertEqual(sorted(match.false_negatives), [50, 70])
        self.assertAlmostEqual(match.precision, 0.25)
        self.assertAlmostEqual(match.recall, 1 / 3)

    def test_closest_pair_wins_a_contested_truth(self):
        match = m.match_events([9, 10], [10], tolerance=1)
        self.assertEqual(match.true_positives, [(10, 10)])
        self.assertEqual(match.false_positives, [9])

    def test_nothing_predicted_and_nothing_true(self):
        match = m.match_events([], [])
        self.assertEqual((match.precision, match.recall, match.f1), (0.0, 0.0, 0.0))


def _shots(lengths: list[int], colors: list[tuple[int, int, int]]) -> list[np.ndarray]:
    rng = np.random.default_rng(7)
    frames = []
    for n, color in zip(lengths, colors, strict=True):
        for _ in range(n):
            noise = rng.integers(-3, 4, (64, 96, 3))
            frames.append(np.clip(np.array(color) + noise, 0, 255).astype(np.uint8))
    return frames


def _run(detector, frames: list[np.ndarray]) -> list[int]:
    found: list[int] = []
    for frame in frames:
        found.extend(detector.push(frame))
    return found


class DetectorTest(unittest.TestCase):
    FRAMES = _shots([30, 30, 30], [(40, 40, 200), (230, 230, 230), (20, 20, 20)])

    def test_adaptive_ratio_reports_each_cut_at_its_first_frame(self):
        self.assertEqual(_run(AdaptiveRatio(), self.FRAMES), [30, 60])

    def test_adaptive_ratio_holds_a_minimum_shot_length(self):
        frames = _shots([30, 5, 30], [(40, 40, 200), (200, 60, 40), (40, 180, 40)])
        self.assertEqual(_run(AdaptiveRatio(min_shot=15), frames), [30])

    def test_ffmpeg_scene_fires_on_a_luma_jump_and_not_on_noise(self):
        self.assertEqual(_run(FfmpegScene(0.3), self.FRAMES), [30, 60])
        steady = _shots([60], [(90, 90, 90)])
        self.assertEqual(_run(FfmpegScene(0.3), steady), [])

    def test_ffmpeg_scene_is_blind_to_a_cut_that_keeps_luma(self):
        same_luma = _shots([30, 30], [(40, 40, 200), (200, 60, 70)])
        self.assertEqual(_run(FfmpegScene(0.3), same_luma), [])
        self.assertEqual(_run(AdaptiveRatio(), same_luma), [30])


class OfflineRendererTest(unittest.TestCase):
    def test_a_bank_swapping_push_shows_its_staged_background(self):
        """Flicker blending stages bg0 in the swap tracker for the C64's IRQ to
        copy into $D021; the offline framebuffer has no IRQ."""
        from c64cast.app.config import ColorCfg
        from scripts.diags._offline_video import OfflineMhires

        renderer = OfflineMhires(color=ColorCfg(flicker_tolerance="subtle"))
        gray = np.tile(np.array([200, 200, 200], np.uint8), (400, 640, 1))
        for i in range(3):
            renderer.show(gray, i / 25)
        staged = renderer.framebuffer.ram[0xC700] & 0x0F
        self.assertNotEqual(staged, 0)
        self.assertEqual(renderer.framebuffer.ram[0xD021] & 0x0F, staged)


if __name__ == "__main__":
    unittest.main()
