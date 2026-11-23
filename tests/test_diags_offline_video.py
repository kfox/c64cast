"""scripts/diags/_offline_video.py decodes a clip the way playback does; the
offline mhires and cut-detection tools all read their frames through it."""

from __future__ import annotations

import os
import tempfile
import unittest

import cv2
import numpy as np

from c64cast.video.video import ensure_pyav

WIDE = (640, 360)


def _wide_image() -> np.ndarray:
    img = np.zeros((WIDE[1], WIDE[0], 3), np.uint8)
    img[..., 0] = np.linspace(0, 255, WIDE[0], dtype=np.uint8)[None, :]
    img[..., 1] = 160
    return img


def _write_wide_clip(path: str, frames: int = 3) -> None:
    import av

    container = av.open(path, "w")
    try:
        stream = container.add_stream("mpeg4", rate=25)
        stream.width, stream.height = WIDE
        stream.pix_fmt = "yuv420p"
        for _ in range(frames):
            frame = av.VideoFrame.from_ndarray(_wide_image()[..., ::-1].copy(), "rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


def _aspect(img: np.ndarray) -> float:
    h, w = img.shape[:2]
    return w / h


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class IterFramesTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "wide.mp4")
        _write_wide_clip(self.path)

    def test_crop_trims_each_frame_to_the_screen(self):
        from scripts.diags._offline_video import ASPECT, iter_frames

        ticks = list(iter_frames(self.path, aspect_mode="crop"))
        self.assertTrue(ticks)
        self.assertAlmostEqual(_aspect(ticks[0].frame), ASPECT, delta=0.02)

    def test_an_interpolation_override_trims_the_same_way(self):
        from scripts.diags._offline_video import ASPECT, iter_frames

        ticks = list(iter_frames(self.path, interpolation="AREA", aspect_mode="crop"))
        self.assertTrue(ticks)
        self.assertAlmostEqual(_aspect(ticks[0].frame), ASPECT, delta=0.02)

    def test_frames_keep_the_whole_picture_as_the_scene_default_does(self):
        from scripts.diags._offline_video import iter_frames

        ticks = list(iter_frames(self.path, reference=True))
        self.assertAlmostEqual(_aspect(ticks[0].frame), WIDE[0] / WIDE[1], delta=0.02)
        reference = ticks[0].reference
        assert reference is not None
        self.assertEqual(reference.shape[:2], (200, 320))
        self.assertEqual(int(reference[0].max()), 0)
        self.assertGreater(int(reference[100].max()), 0)


class OfflineMhiresFramingTest(unittest.TestCase):
    def setUp(self):
        from c64cast.video import palette

        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))

    def test_a_wide_picture_is_letterboxed_and_its_mask_leaves_out_the_bars(self):
        from scripts.diags._offline_video import OfflineMhires

        renderer = OfflineMhires()
        renderer.show(_wide_image(), 0.0)
        mask = renderer.picture_mask
        self.assertEqual(mask.shape, (200, 320))
        self.assertFalse(mask[0].any())
        self.assertFalse(mask[-1].any())
        self.assertTrue(mask[100].all())
        self.assertEqual(int(renderer.render()[0].max()), 0)

    def test_a_cropped_picture_fills_the_screen(self):
        from scripts.diags._offline_video import OfflineMhires, picture

        renderer = OfflineMhires(aspect_mode="crop")
        renderer.show(picture(_wide_image(), "crop"), 0.0)
        self.assertTrue(renderer.picture_mask.all())


class MaskedMetricsTest(unittest.TestCase):
    def test_a_mask_scores_only_the_pixels_it_marks(self):
        from scripts.diags import _mhires_metrics as m

        a = np.zeros((200, 320, 3), np.uint8)
        b = a.copy()
        b[:20] = 255
        mask = np.zeros((200, 320), bool)
        mask[40:160] = True
        self.assertGreater(m.oklab_mse(m.oklab(a), m.oklab(b)), 0.0)
        self.assertEqual(m.oklab_mse(m.oklab(a), m.oklab(b), mask), 0.0)
        self.assertLess(m.ssim(m.oklab(a)[..., 0], m.oklab(b)[..., 0]), 1.0)
        self.assertAlmostEqual(m.ssim(m.oklab(a)[..., 0], m.oklab(b)[..., 0], mask), 1.0)

    def test_cells_touched_marks_every_cell_holding_a_masked_pixel(self):
        from scripts.diags import _mhires_metrics as m

        mask = np.zeros((200, 320), bool)
        mask[12, 3] = True
        touched = m.cells_touched(mask)
        self.assertEqual(int(touched.sum()), 1)
        self.assertTrue(touched[1, 0])

    def test_still_cells_count_only_the_picture(self):
        from c64cast.video import palette
        from scripts.diags import mhires_video_eval as ev

        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))
        still = os.path.join(tempfile.mkdtemp(), "wide.png")
        cv2.imwrite(still, _wide_image())
        variant = ev.Variant(
            label="t",
            color={"auto_fit": False},
            interpolation=None,
            fps=None,
            sigma=1.0,
            still_delta=1.0,
            still_frames=2,
            ema_alpha=None,
            at_cut=[],
            post_cut=1,
            host_palette="pepto",
        )
        (row,) = ev.score_clip(ev.Clip(name="t", path=still), variant, [])["rows"]
        self.assertLess(row.picture_cells, ev.m.CELL_ROWS * ev.m.CELL_COLS)
        self.assertEqual(row.still_cells, row.picture_cells)


if __name__ == "__main__":
    unittest.main()
