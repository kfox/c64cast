"""The metric math behind scripts/diags/mhires_video_eval.py and
video_cut_eval.py: the numbers those tools report are only as good as these."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

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
    def setUp(self):
        from c64cast.video import palette

        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))

    def test_the_render_shows_the_colors_an_ultimate_emits(self):
        from c64cast.video import palette
        from scripts.diags._offline_video import OfflineMhires

        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        renderer = OfflineMhires()
        rng = np.random.default_rng(1)
        renderer.show(rng.integers(0, 256, (400, 640, 3), dtype=np.uint8), 0.0)
        shown = {tuple(int(c) for c in px) for px in renderer.render().reshape(-1, 3)}
        u64 = {tuple(int(c) for c in px) for px in palette.U64_PALETTE_BGR}
        self.assertTrue(shown <= u64, shown - u64)

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


class LimitedFitTest(unittest.TestCase):
    def test_a_capped_gain_widens_the_range_about_its_center(self):
        from c64cast.video.palette import ColorFit
        from scripts.diags.mhires_video_eval import limited_fit

        fit = limited_fit(ColorFit(black=40.0, white=100.0, sat_mult=1.0), None, 2.0, 1.0)
        assert fit is not None
        self.assertAlmostEqual(fit.white - fit.black, 127.5)
        self.assertAlmostEqual((fit.black + fit.white) / 2, 70.0)

    def test_a_widened_range_stays_inside_the_luma_scale(self):
        from c64cast.video.palette import ColorFit
        from scripts.diags.mhires_video_eval import limited_fit

        fit = limited_fit(ColorFit(black=0.0, white=40.0, sat_mult=1.0), None, 2.0, 1.0)
        assert fit is not None
        self.assertEqual((fit.black, fit.white), (0.0, 127.5))

    def test_a_range_widened_near_white_stays_inside_the_luma_scale(self):
        from c64cast.video.palette import ColorFit
        from scripts.diags.mhires_video_eval import limited_fit

        fit = limited_fit(ColorFit(black=220.0, white=250.0, sat_mult=1.0), None, 2.0, 1.0)
        assert fit is not None
        self.assertEqual((fit.black, fit.white), (127.5, 255.0))

    def test_the_tighter_of_an_absolute_cap_and_a_rise_cap_wins(self):
        from c64cast.video.palette import ColorFit
        from scripts.diags.mhires_video_eval import limited_fit

        before = ColorFit(black=0.0, white=127.5, sat_mult=1.5)
        shot = ColorFit(black=100.0, white=140.0, sat_mult=2.0)
        by_absolute = limited_fit(shot, before, 3.0, 1.0, max_sat=1.2, max_rise=2.0)
        assert by_absolute is not None
        self.assertAlmostEqual(255.0 / (by_absolute.white - by_absolute.black), 3.0)
        self.assertAlmostEqual(by_absolute.sat_mult, 1.2)
        by_rise = limited_fit(shot, before, 5.0, 1.0, max_sat=1.8, max_rise=1.0)
        assert by_rise is not None
        self.assertAlmostEqual(255.0 / (by_rise.white - by_rise.black), 2.0)
        self.assertAlmostEqual(by_rise.sat_mult, 1.5)

    def test_saturation_is_capped_and_the_fit_is_blended_from_the_one_before(self):
        from c64cast.video.palette import ColorFit
        from scripts.diags.mhires_video_eval import limited_fit

        before = ColorFit(black=0.0, white=255.0, sat_mult=1.0)
        shot = ColorFit(black=20.0, white=235.0, sat_mult=1.6)
        fit = limited_fit(shot, before, None, 0.5, max_sat=1.2)
        self.assertEqual(fit, ColorFit(black=10.0, white=245.0, sat_mult=1.1))

    def test_a_rise_of_one_only_lets_a_cut_relax_the_fit(self):
        from c64cast.video.palette import ColorFit
        from scripts.diags.mhires_video_eval import limited_fit

        before = ColorFit(black=0.0, white=170.0, sat_mult=1.2)
        stronger = limited_fit(
            ColorFit(black=60.0, white=120.0, sat_mult=1.6), before, None, 1.0, max_rise=1.0
        )
        assert stronger is not None
        self.assertAlmostEqual(stronger.white - stronger.black, 170.0)
        self.assertAlmostEqual(stronger.sat_mult, 1.2)
        weaker = ColorFit(black=10.0, white=240.0, sat_mult=1.0)
        self.assertEqual(limited_fit(weaker, before, None, 1.0, max_rise=1.0), weaker)

    def test_no_fit_on_either_side_stays_no_fit(self):
        from scripts.diags.mhires_video_eval import limited_fit

        self.assertIsNone(limited_fit(None, None, 2.0, 0.5))

    def test_a_full_range_opening_still_anchors_the_rise_at_every_cut(self):
        import cv2

        from c64cast.video.palette import ColorFit
        from scripts.diags import mhires_video_eval as ev
        from scripts.diags._offline_video import OfflineMhires

        still = os.path.join(tempfile.mkdtemp(), "still.png")
        cv2.imwrite(still, np.tile(np.arange(256, dtype=np.uint8), (64, 1)))
        variant = ev.Variant(
            label="t",
            color={"auto_fit": False},
            interpolation=None,
            fps=None,
            sigma=1.0,
            still_delta=1.0,
            still_frames=3,
            ema_alpha=None,
            at_cut=["ahead"],
            post_cut=1,
            host_palette="pepto",
            fit_max_rise=2.0,
            fit_rise_from="opening",
        )
        shot_fits = {
            0.04: ColorFit(black=64.0, white=191.5, sat_mult=1.0),
            0.08: ColorFit(black=96.0, white=159.75, sat_mult=1.0),
        }
        installed: list[ColorFit | None] = []
        real_install = OfflineMhires.install_fit

        def record(renderer, fit):
            installed.append(fit)
            real_install(renderer, fit)

        with (
            patch.object(ev, "shot_fits", return_value=shot_fits),
            patch.object(OfflineMhires, "install_fit", record),
        ):
            ev.score_clip(ev.Clip(name="t", path=still), variant, list(shot_fits))
        gains = [255.0 / (f.white - f.black) for f in installed if f is not None]
        self.assertEqual(len(gains), 2)
        self.assertAlmostEqual(gains[1], 2.0)


class EvalToolTest(unittest.TestCase):
    def setUp(self):
        from c64cast.video import palette

        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))

    def _cuts_file(self, content: object) -> str:
        path = os.path.join(tempfile.mkdtemp(), "cuts.json")
        with open(path, "w") as fh:
            json.dump(content, fh)
        return path

    def test_cuts_load_from_the_cut_labels_file_and_from_plain_lists(self):
        from scripts.diags.mhires_video_eval import load_cuts

        labels = {"a": {"cuts": [1.5, 3.0], "flash": [2.0], "fade": [], "motion": []}}
        self.assertEqual(load_cuts(self._cuts_file(labels)), {"a": [1.5, 3.0]})
        self.assertEqual(load_cuts(self._cuts_file({"a": [1.5]})), {"a": [1.5]})

    def test_a_clip_with_no_frames_in_its_window_still_prints(self):
        from scripts.diags.mhires_video_eval import _fmt, summarize

        self.assertEqual(_fmt(summarize([], None, 30)), "frames=0")

    def test_ghost_ab_renders_without_an_online_fit_when_the_window_fit_is_identity(self):
        from scripts.diags.mhires_ema_ghost_ab import fixed_fit_renderer

        renderer = fixed_fit_renderer(None)
        self.assertIsNone(renderer._online)


if __name__ == "__main__":
    unittest.main()
