"""The `metaballs` generative source: WLED "Metaballs" port: 3 moving "ball" centers blended into a classic inverse-distance metaball field."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("metaballs")
class MetaballsSource(GenerativeSource):
    """WLED "Metaballs" port: 3 moving "ball" centers blended into a classic
    inverse-distance metaball field. All 3 ball paths are closed-form
    functions of `t` in WLED's own source too — `beatsin8` is phase-linear in
    wall-clock time (no running accumulator), so ball 1 ports directly as a
    Lissajous sine pair; balls 2 & 3 use `perlin8` point samples, which this
    codebase has no primitive for, so they're replaced with a 2-term
    incommensurate-frequency sine "wander" (the same pure-trig
    organic-motion trick `hopalong`/`epicycle` already use elsewhere) — a
    documented simplification, not a literal noise port. Per frame: 3 scalar
    ball positions (a handful of scalar `sin()` calls) plus one vectorized
    distance field over the precomputed pixel grid."""

    LIVE_PARAMS = {"speed": (0.05, 5.0)}

    _W1X = 0.9
    _W1Y = 1.1
    _BALL2 = {"fx": (0.11, 0.178), "fy": (0.13, 0.210), "px": (0.0, 1.7), "py": (0.9, 2.4)}
    _BALL3 = {"fx": (0.17, 0.275), "fy": (0.19, 0.307), "px": (2.1, 0.4), "py": (1.2, 3.0)}
    _THRESHOLD = 60.0
    # WLED's raw `color/threshold` value maps cleanly to brightness on the
    # small (16-64px) matrices it targets, but decays too fast to read as
    # anything but a dim smudge at this generator's much larger 320x200 native
    # resolution — this gamma lifts the mid/low range for legibility after C64
    # quantization (background pixels, already ~0, stay ~0; it's a display
    # tone curve, not a change to the underlying distance-field math).
    _VALUE_GAMMA = 0.6

    def __init__(self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, speed: float = 1.0):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._amp = min(width, height) * 0.35

    def _wander(self, tt: float, spec: dict[str, tuple[float, float]]) -> tuple[float, float]:
        fx0, fx1 = spec["fx"]
        fy0, fy1 = spec["fy"]
        px0, px1 = spec["px"]
        py0, py1 = spec["py"]
        dx = 0.6 * math.sin(tt * fx0 + px0) + 0.4 * math.sin(tt * fx1 + px1)
        dy = 0.6 * math.sin(tt * fy0 + py0) + 0.4 * math.sin(tt * fy1 + py1)
        return self._cx + self._amp * dx, self._cy + self._amp * dy

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        tt = t * self.speed
        x1 = self._cx + self._amp * math.sin(tt * self._W1X)
        y1 = self._cy + self._amp * math.sin(tt * self._W1Y)
        x2, y2 = self._wander(tt, self._BALL2)
        x3, y3 = self._wander(tt, self._BALL3)
        d1 = np.hypot(self._xs - x1, self._ys - y1)
        d2 = np.hypot(self._xs - x2, self._ys - y2)
        d3 = np.hypot(self._xs - x3, self._ys - y3)
        dist = 2.0 * d1 + d2 + d3
        color = 1000.0 / np.maximum(dist, 1.0)
        in_range = color < self._THRESHOLD
        val = np.clip(color / self._THRESHOLD, 0.0, 1.0) ** self._VALUE_GAMMA
        hue = 0.55 - val * 0.55
        if modulation is not None:
            hue = np.mod(hue + self._reactive_hue_offset(modulation), 1.0)
            val = val * self._reactive_value(modulation)
        val = np.where(in_range, val, 0.0)
        # Per-pixel `val` (not the scalar `_hsv_to_bgr` accepts) needs the
        # manual HSV build Mandelbrot/Hopalong already use for the same reason.
        h, w = val.shape
        hsv = np.empty((h, w, 3), dtype=np.uint8)
        hsv[..., 0] = (np.mod(hue, 1.0) * 180.0).astype(np.uint8)
        hsv[..., 1] = 255
        hsv[..., 2] = np.clip(val * 255.0, 0.0, 255.0).astype(np.uint8)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
