"""The `mandelbrot` generative source: Escape-time Mandelbrot zoom."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


@register("mandelbrot")
class MandelbrotSource(GenerativeSource):
    """Escape-time Mandelbrot zoom. `t` drives an exponential zoom into a
    fixed point of interest (`_CENTER`, a "seahorse valley" coordinate chosen
    so the starting view already frames the whole familiar Mandelbrot
    silhouette). float64 precision limits how far a zoom can go before
    per-pixel spacing collapses into noise, so the zoom **periodically resets**
    to the starting view rather than degrading — `render(t, None)` stays a
    well-defined pure function of `t` forever, the same determinism contract
    plasma/tunnel/fire already guarantee.

    Iteration count is intentionally fixed regardless of zoom depth: the
    output is quantized to a 16-color C64 grid, so resolving filament-level
    deep-zoom detail would be invisible anyway — fixing it bounds per-frame
    cost at any zoom depth instead of growing it toward the precision limit.
    """

    LIVE_PARAMS = {"zoom_speed": (0.02, 1.0), "cycle_speed": (0.0, 2.0)}

    _MAX_ITER = 100
    _CENTER = complex(-0.743643887037151, 0.13182590420533)  # seahorse valley
    _HALF_WIDTH = 1.75  # starting view half-width; frames the whole set
    _ZOOM_LIMIT = 1.0e13  # stays well inside float64's precision floor
    _HUE_SCALE = 3.0  # hue cycles per full pass through the smooth iteration count
    # The escape-time loop is the one generator whose per-frame cost scales
    # with pixel count rather than a couple of cheap elementwise ops, and the
    # eventual C64 quantization (16-color, 320x200 at best) throws away detail
    # far finer than this anyway — so compute at half resolution per axis
    # (1/4 the points) and let cv2.resize upscale the finished BGR frame
    # (never the HSV field — hue is circular, so linear-interpolating it
    # would blend the wrong way across the 0/179 wrap).
    _CALC_DIVISOR = 2

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        zoom_speed: float = 0.12,
        cycle_speed: float = 0.15,
    ):
        super().__init__(width=width, height=height)
        self.zoom_speed = float(zoom_speed)
        self.cycle_speed = float(cycle_speed)
        cw = max(1, width // self._CALC_DIVISOR)
        ch = max(1, height // self._CALC_DIVISOR)
        self._calc_size = (width, height)  # (w, h) for cv2.resize's dsize
        ys, xs = np.mgrid[0:ch, 0:cw].astype(np.float64)
        # Offsets from center in units of half the frame WIDTH (for both axes)
        # so the shorter height naturally narrows the view vertically instead
        # of stretching the fractal.
        self._px = (xs - cw / 2.0) / (cw / 2.0)
        self._py = (ys - ch / 2.0) / (cw / 2.0)

    def _scale(self, t: float) -> float:
        rate = max(abs(self.zoom_speed), 1e-3)
        period = math.log(self._ZOOM_LIMIT) / rate
        return math.exp(rate * (t % period))

    def _escape_frac(self, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Per-pixel smooth escape-time fraction in [0, 1) (hue-ready) plus an
        `escaped` mask — False means the point never escaped (inside the set),
        which the caller forces to black regardless of hue."""
        z = np.zeros_like(c)
        active = np.ones(c.shape, dtype=bool)
        escaped = np.zeros(c.shape, dtype=bool)
        smooth = np.zeros(c.shape, dtype=np.float64)
        for i in range(self._MAX_ITER):
            z[active] = z[active] * z[active] + c[active]
            mag = np.abs(z)
            newly = active & (mag > 2.0)
            if newly.any():
                # Smooth (continuous) iteration count — the standard
                # log-log correction that removes escape-time banding.
                smooth[newly] = i + 1 - np.log2(np.log2(mag[newly]))
                escaped[newly] = True
                active[newly] = False
            if not active.any():
                break
        frac = np.mod(smooth / self._MAX_ITER * self._HUE_SCALE, 1.0)
        return frac, escaped

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        half_width = self._HALF_WIDTH / self._scale(t)
        c = self._CENTER + (self._px + 1j * self._py) * half_width
        frac, escaped = self._escape_frac(c)
        if modulation is None:
            hue = frac + t * self.cycle_speed
            val: float | np.ndarray = 0.85
        else:
            hue = frac + t * self.cycle_speed + self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        h, w = frac.shape
        hsv = np.empty((h, w, 3), dtype=np.uint8)
        hsv[..., 0] = (np.mod(hue, 1.0) * 180.0).astype(np.uint8)
        hsv[..., 1] = 255
        val_field = np.where(escaped, val, 0.0)
        hsv[..., 2] = np.clip(val_field * 255.0, 0.0, 255.0).astype(np.uint8)
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        if (w, h) == self._calc_size:
            return bgr
        return cv2.resize(bgr, self._calc_size, interpolation=cv2.INTER_LINEAR)
