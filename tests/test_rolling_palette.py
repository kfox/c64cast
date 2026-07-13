"""Rolling-window live force_palette driver: cut detection + worker publish."""

from __future__ import annotations

import time
import unittest

import numpy as np

from c64cast.palette import C64_PALETTE_BGR, ColorMap
from c64cast.rolling_palette import RollingForcePalette


def _solid(idx: int, h: int = 64, w: int = 64) -> np.ndarray:
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:] = C64_PALETTE_BGR[idx].astype(np.uint8)
    return img


def _two_color(a: int, b: int) -> np.ndarray:
    img = np.empty((64, 64, 3), dtype=np.uint8)
    img[:32] = C64_PALETTE_BGR[a].astype(np.uint8)
    img[32:] = C64_PALETTE_BGR[b].astype(np.uint8)
    return img


class DetectCutTest(unittest.TestCase):
    def _fp(self) -> RollingForcePalette:
        fp = RollingForcePalette(n_colors=2)
        self.addCleanup(fp.stop)
        return fp

    def test_first_frame_is_not_a_cut(self):
        fp = self._fp()
        self.assertFalse(fp._detect_cut(_solid(2)))

    def test_identical_frames_no_cut(self):
        fp = self._fp()
        fp._detect_cut(_solid(2))
        self.assertFalse(fp._detect_cut(_solid(2)))

    def test_very_different_frames_is_a_cut(self):
        fp = self._fp()
        fp._detect_cut(_solid(2))  # red
        self.assertTrue(fp._detect_cut(_solid(6)))  # blue → cut


class DriverPublishTest(unittest.TestCase):
    def test_worker_publishes_a_map(self):
        fp = RollingForcePalette(n_colors=2, sample_interval_s=0.03)
        self.addCleanup(fp.stop)
        fp.start()
        fp.submit_frame(_two_color(2, 6))
        # Give the worker a few cycles to sample + bake + publish.
        deadline = time.time() + 2.0
        cmap: ColorMap | None = None
        while time.time() < deadline:
            cmap = fp.poll_colormap()
            if cmap is not None:
                break
            fp.submit_frame(_two_color(2, 6))
            time.sleep(0.03)
        self.assertIsNotNone(cmap)
        assert cmap is not None
        self.assertEqual(set(cmap.indices), {2, 6})

    def test_poll_returns_map_only_once(self):
        fp = RollingForcePalette(n_colors=2, sample_interval_s=0.03)
        self.addCleanup(fp.stop)
        fp.start()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            fp.submit_frame(_two_color(2, 6))
            if fp.poll_colormap() is not None:
                break
            time.sleep(0.03)
        # Immediately after consuming a published map, the next poll is empty
        # (a stable, unchanged palette doesn't re-publish).
        self.assertIsNone(fp.poll_colormap())

    def test_stop_is_idempotent_and_joins(self):
        fp = RollingForcePalette(n_colors=2, sample_interval_s=0.03)
        fp.start()
        fp.stop()
        fp.stop()  # second stop is a no-op
        self.assertIsNone(fp._thread)


class SceneGatingTest(unittest.TestCase):
    """The scenes.py seam that starts/applies the rolling palette."""

    def test_maybe_start_gates_on_mode_and_color(self):
        from types import SimpleNamespace

        from c64cast.config import ColorCfg
        from c64cast.scenes import _maybe_start_rolling_palette

        scene = SimpleNamespace(name="live")
        applying_mode = SimpleNamespace(_force_palette=True, set_color_map=lambda c: None)
        plain_mode = SimpleNamespace(_force_palette=False)

        # No color, or a mode that doesn't apply force_palette → nothing starts.
        self.assertIsNone(_maybe_start_rolling_palette(scene, None, applying_mode))  # type: ignore[arg-type]
        self.assertIsNone(
            _maybe_start_rolling_palette(scene, ColorCfg(force_palette=True), plain_mode)  # type: ignore[arg-type]
        )

        # force_palette on + an applying mode → a started driver (stop it).
        fp = _maybe_start_rolling_palette(
            scene,  # type: ignore[arg-type]
            ColorCfg(force_palette=True, force_palette_colors=4),
            applying_mode,  # type: ignore[arg-type]
        )
        self.assertIsInstance(fp, RollingForcePalette)
        assert fp is not None
        self.addCleanup(fp.stop)

    def test_apply_installs_polled_map_and_is_none_safe(self):
        from types import SimpleNamespace

        from c64cast.scenes import _apply_rolling_palette

        installed: list[ColorMap] = []
        mode = SimpleNamespace(set_color_map=installed.append)
        frame = _solid(2)

        # None driver → no-op, no raise.
        _apply_rolling_palette(None, mode, frame)  # type: ignore[arg-type]
        self.assertEqual(installed, [])

        # A driver that yields a map → it gets installed; the frame is submitted.
        dummy = ColorMap(lut=np.zeros((2, 2, 2), dtype=np.uint8), shift=3, indices=(2, 6))
        submitted: list[np.ndarray] = []
        stub = SimpleNamespace(submit_frame=submitted.append, poll_colormap=lambda: dummy)
        _apply_rolling_palette(stub, mode, frame)  # type: ignore[arg-type]
        self.assertEqual(len(submitted), 1)
        self.assertIs(installed[0], dummy)


if __name__ == "__main__":
    unittest.main()
