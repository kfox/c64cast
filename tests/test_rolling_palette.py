"""Rolling-window live force_palette driver: cut detection + worker publish."""

from __future__ import annotations

import time
import unittest

import cv2
import numpy as np

from c64cast.video.palette import C64_PALETTE_BGR, ColorMap
from c64cast.video.rolling_palette import RollingForcePalette, _remap_drift


def _solid(idx: int, h: int = 64, w: int = 64) -> np.ndarray:
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:] = C64_PALETTE_BGR[idx].astype(np.uint8)
    return img


def _two_color(a: int, b: int) -> np.ndarray:
    img = np.empty((64, 64, 3), dtype=np.uint8)
    img[:32] = C64_PALETTE_BGR[a].astype(np.uint8)
    img[32:] = C64_PALETTE_BGR[b].astype(np.uint8)
    return img


def _hue_sweep(first_hue: int, last_hue: int) -> np.ndarray:
    """A 64x36 picture sweeping hue left to right and brightness top to bottom."""
    hsv = np.empty((36, 64, 3), dtype=np.uint8)
    hsv[..., 0] = np.linspace(first_hue, last_hue, 64).astype(np.uint8)[None, :]
    hsv[..., 1] = 200
    hsv[..., 2] = np.linspace(40, 250, 36).astype(np.uint8)[:, None]
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


class _OneFramePerCycle:
    """Stands in for the worker's stop event: each wait hands the worker the
    next frame and collects what the cycle before it published. The frame
    skips `submit_frame`, whose shot-cut check would read a few frames this
    far apart as cuts."""

    def __init__(self, fp: RollingForcePalette, frames: list[np.ndarray]):
        self._fp, self._frames = fp, iter(frames)
        self.installed: list[ColorMap] = []

    def wait(self, _timeout: float) -> bool:
        cmap = self._fp.poll_colormap()
        if cmap is not None:
            self.installed.append(cmap)
        frame = next(self._frames, None)
        if frame is None:
            return True
        self._fp._latest = frame
        return False


def _installs(fp: RollingForcePalette, frames: list[np.ndarray]) -> list[ColorMap]:
    stop = _OneFramePerCycle(fp, frames)
    fp._run(stop)  # type: ignore[arg-type]
    return stop.installed


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
        # A stable, unchanged palette does not re-publish, so the poll after a
        # consumed map is empty.
        self.assertIsNone(fp.poll_colormap())

    def test_stop_is_idempotent_and_joins(self):
        fp = RollingForcePalette(n_colors=2, sample_interval_s=0.03)
        fp.start()
        fp.stop()
        fp.stop()
        self.assertFalse(fp._poll.is_running())


class ReinstallPolicyTest(unittest.TestCase):
    """A baked map replaces the installed one only once it would repaint a
    meaningful share of the picture."""

    WARM, COOL = _hue_sweep(0, 30), _hue_sweep(90, 130)

    def _crossfade(self, cycles: int) -> list[np.ndarray]:
        return [
            cv2.addWeighted(self.WARM, 1 - t, self.COOL, t, 0) for t in np.linspace(0, 1, cycles)
        ]

    def test_a_gradual_change_reinstalls_when_every_candidate_is_in_use(self):
        for kwargs in ({"n_colors": 16}, {"indices": [0, 1, 2, 5, 6, 7]}):
            with self.subTest(**kwargs):
                fp = RollingForcePalette(**kwargs)  # type: ignore[arg-type]
                installs = _installs(fp, self._crossfade(30))
                self.assertGreater(len(installs), 1)

    def test_a_still_picture_installs_once(self):
        for kwargs in ({"n_colors": 16}, {"n_colors": 4}, {"indices": [0, 1, 2, 5, 6, 7]}):
            with self.subTest(**kwargs):
                fp = RollingForcePalette(**kwargs)  # type: ignore[arg-type]
                self.assertEqual(len(_installs(fp, [self.WARM] * 20)), 1)

    def test_small_steps_add_up_to_a_reinstall(self):
        frame = np.zeros((1, 100, 3), dtype=np.uint8)
        frame[0, :, 0] = (np.arange(100) % 32) << 3
        frame[0, :, 1] = (np.arange(100) // 32) << 3

        def tenths_repainted(k: int) -> ColorMap:
            lut = np.zeros((32, 32, 32), dtype=np.uint8)
            lut[frame[0, : 10 * k, 0] >> 3, frame[0, : 10 * k, 1] >> 3, 0] = 1
            return ColorMap(lut=lut, shift=3, indices=(0, 1))

        bakes = [tenths_repainted(k) for k in range(4)]

        class _Bakes:
            def add(self, _frame: np.ndarray) -> None:
                pass

            def result(self) -> ColorMap:
                return bakes.pop(0)

        fp = RollingForcePalette(n_colors=2)
        fp._acc = _Bakes()  # type: ignore[assignment]
        installs = _installs(fp, [frame] * 4)
        self.assertEqual(len(installs), 2)
        self.assertEqual(_remap_drift(installs[0], installs[1], frame), 0.2)

    def test_drift_is_the_share_of_pixels_repainted(self):
        frame = _two_color(2, 6)
        red = ColorMap(lut=np.full((32, 32, 32), 2, dtype=np.uint8), shift=3, indices=(2,))
        lut = np.full((32, 32, 32), 2, dtype=np.uint8)
        blue_bgr = C64_PALETTE_BGR[6].astype(np.uint8) >> 3
        lut[blue_bgr[0], blue_bgr[1], blue_bgr[2]] = 6
        split = ColorMap(lut=lut, shift=3, indices=(2, 6))
        self.assertEqual(_remap_drift(red, red, frame), 0.0)
        self.assertEqual(_remap_drift(red, split, frame), 0.5)


class SceneGatingTest(unittest.TestCase):
    """The scenes.py seam that starts/applies the rolling palette."""

    def test_maybe_start_gates_on_mode_and_color(self):
        from types import SimpleNamespace

        from c64cast.app.config import ColorCfg
        from c64cast.scenes.scenes import _maybe_start_rolling_palette

        scene = SimpleNamespace(name="live")
        applying_mode = SimpleNamespace(_force_palette=True, set_color_map=lambda c: None)
        plain_mode = SimpleNamespace(_force_palette=False)

        self.assertIsNone(_maybe_start_rolling_palette(scene, None, applying_mode))  # type: ignore[arg-type]
        self.assertIsNone(
            _maybe_start_rolling_palette(scene, ColorCfg(force_palette=True), plain_mode)  # type: ignore[arg-type]
        )

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

        from c64cast.scenes.scenes import _apply_rolling_palette

        installed: list[ColorMap] = []
        mode = SimpleNamespace(set_color_map=installed.append)
        frame = _solid(2)

        _apply_rolling_palette(None, mode, frame)  # type: ignore[arg-type]
        self.assertEqual(installed, [])

        dummy = ColorMap(lut=np.zeros((2, 2, 2), dtype=np.uint8), shift=3, indices=(2, 6))
        submitted: list[np.ndarray] = []
        stub = SimpleNamespace(submit_frame=submitted.append, poll_colormap=lambda: dummy)
        _apply_rolling_palette(stub, mode, frame)  # type: ignore[arg-type]
        self.assertEqual(len(submitted), 1)
        self.assertIs(installed[0], dummy)


if __name__ == "__main__":
    unittest.main()
