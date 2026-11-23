"""scripts/diags/_offline_video.py decodes a clip the way playback does; the
offline mhires and cut-detection tools all read their frames through it."""

from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from c64cast.video.video import ensure_pyav

WIDE = (640, 360)


def _write_wide_clip(path: str, frames: int = 3) -> None:
    import av

    container = av.open(path, "w")
    try:
        stream = container.add_stream("mpeg4", rate=25)
        stream.width, stream.height = WIDE
        stream.pix_fmt = "yuv420p"
        img = np.zeros((WIDE[1], WIDE[0], 3), np.uint8)
        img[..., 0] = np.linspace(0, 255, WIDE[0], dtype=np.uint8)[None, :]
        for _ in range(frames):
            for packet in stream.encode(av.VideoFrame.from_ndarray(img, "rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class IterFramesTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "wide.mp4")
        _write_wide_clip(self.path)

    def test_a_clip_decodes_to_the_picture_playback_crops_to(self):
        from scripts.diags._offline_video import ASPECT, iter_frames

        ticks = list(iter_frames(self.path))
        self.assertTrue(ticks)
        h, w = ticks[0].frame.shape[:2]
        self.assertAlmostEqual(w / h, ASPECT, delta=0.02)


if __name__ == "__main__":
    unittest.main()
