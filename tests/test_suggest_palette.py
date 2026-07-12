"""CLI `--suggest-palette` wrapper: file decode → ranking → formatting."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import cv2
import numpy as np

from c64cast.cli import _collect_lab_samples, run_suggest_palette
from c64cast.palette import C64_COLOR_NAMES, C64_PALETTE_BGR, suggest_palette


class SuggestPaletteCliTest(unittest.TestCase):
    def _write_palette_image(self, idx: int) -> str:
        """A solid image filled with C64 palette color ``idx`` (exact BGR), so
        the nearest-color decision is deterministic (→ that index)."""
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        img = np.empty((32, 32, 3), dtype=np.uint8)
        img[:] = C64_PALETTE_BGR[idx].astype(np.uint8)
        cv2.imwrite(path, img)
        return path

    def test_image_samples_rank_dominant_color_first(self):
        # A solid palette-Red (idx 2) image → the ranking must lead with idx 2.
        path = self._write_palette_image(2)
        samples = _collect_lab_samples(path)
        self.assertIsNotNone(samples)
        assert samples is not None
        ranked = suggest_palette(samples)
        self.assertEqual(ranked[0][0], 2)

    def test_run_prints_ranking_and_paste_line(self):
        path = self._write_palette_image(2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = run_suggest_palette(path)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("force_palette_colors", out)
        self.assertIn(C64_COLOR_NAMES[2], out)  # Red appears in the table

    def test_missing_file_returns_2(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = run_suggest_palette("/no/such/image.png")
        self.assertEqual(rc, 2)
        self.assertIn("not found", buf.getvalue())

    def test_unreadable_file_returns_2(self):
        # An existing but non-image file yields no samples → exit 2.
        fd, path = tempfile.mkstemp(suffix=".png")
        os.write(fd, b"not an image")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = run_suggest_palette(path)
        self.assertEqual(rc, 2)

    def test_lab_samples_shape(self):
        path = self._write_palette_image(5)  # palette Green
        samples = _collect_lab_samples(path)
        assert samples is not None
        self.assertEqual(samples.ndim, 2)
        self.assertEqual(samples.shape[1], 3)
        self.assertGreater(len(samples), 0)


if __name__ == "__main__":
    unittest.main()
