"""The `hopalong` generative source: Hopalong chaotic point-map attractor, iterated for many parallel starting points at once (numpy-vectorized across the batch — each *step* is still sequential, the map depends on the previous point) into a density accumulator, color-mapped by (log-scaled) density."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


def _hopalong_step(
    x: np.ndarray, y: np.ndarray, a: float, b: float, c: float
) -> tuple[np.ndarray, np.ndarray]:
    """One iteration of Barry Martin's Hopalong map (the `sqrt` variant
    xscreensaver's hopalong.c defaults to): `x' = y - sign(x)*sqrt(|b*x-c|)`,
    `y' = a - x`."""
    nx = y - np.sign(x) * np.sqrt(np.abs(b * x - c))
    ny = a - x
    return nx, ny


@register("hopalong")
class HopalongSource(GenerativeSource):
    """Hopalong chaotic point-map attractor, iterated for many parallel
    starting points at once (numpy-vectorized across the batch — each *step*
    is still sequential, the map depends on the previous point) into a
    density accumulator, color-mapped by (log-scaled) density. `shape` is the
    map's `a` constant (named for what sweeping it does, since it is a live
    knob); a slow sinusoidal drift of it keeps the attractor breathing over
    time without needing a fundamentally different computation per frame; the
    batch is re-run from scratch every frame (cheap: a few hundred vector
    ops), so the shifting constant is reflected immediately.

    Reactive: `level` and a beat-locked term perturb `a`/`b` continuously
    (the attractor's shape swells with the music); a transient adds a
    temporary kick to `a` — one frame's worth of "the constants jump", not a
    lasting state change, matching the pure-in-`t` contract."""

    LIVE_PARAMS = {"shape": (-2.0, 2.0), "drift_speed": (0.0, 1.0)}

    _BATCH = 4000
    _WARMUP = 60
    _ITERS = 140
    _B = 1.0
    _C = 0.0
    _A_DRIFT = 0.5
    _LEVEL_GAIN = 0.35
    _ONSET_GAIN = 0.6
    _BEAT_B_GAIN = 0.01

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        shape: float = 1.1,
        drift_speed: float = 0.15,
    ):
        super().__init__(width=width, height=height)
        self.shape = float(shape)
        self.drift_speed = float(drift_speed)
        rng = np.random.default_rng(0x0A0F)
        self._x0 = rng.uniform(-0.5, 0.5, self._BATCH)
        self._y0 = rng.uniform(-0.5, 0.5, self._BATCH)

    def _density(self, a: float, b: float, c: float) -> np.ndarray:
        x, y = self._x0.copy(), self._y0.copy()
        xs = []
        ys = []
        for i in range(self._WARMUP + self._ITERS):
            x, y = _hopalong_step(x, y, a, b, c)
            if i >= self._WARMUP:
                xs.append(x)
                ys.append(y)
        px_f = np.concatenate(xs)
        py_f = np.concatenate(ys)
        xmin, xmax = px_f.min(), px_f.max()
        ymin, ymax = py_f.min(), py_f.max()
        px = ((px_f - xmin) / (xmax - xmin + 1e-9) * (self.width - 1)).astype(np.int64)
        py = ((py_f - ymin) / (ymax - ymin + 1e-9) * (self.height - 1)).astype(np.int64)
        flat = py * self.width + px
        counts = np.bincount(flat, minlength=self.width * self.height)
        density = counts.reshape(self.height, self.width).astype(np.float32)
        density = np.log1p(density)
        peak = density.max()
        if peak > 0.0:
            density /= peak
        return density

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        a = self.shape + self._A_DRIFT * math.sin(t * self.drift_speed)
        b = self._B
        if modulation is not None:
            a += self._LEVEL_GAIN * modulation.level + self._ONSET_GAIN * modulation.onset
            b += self._BEAT_B_GAIN * modulation.beat_phase
        density = self._density(a, b, self._C)
        hue = density * 0.7 + t * 0.04
        val = density
        if modulation is not None:
            hue = hue + self._reactive_hue_offset(modulation)
            val = np.clip(density + 0.4 * modulation.onset, 0.0, 1.0)
        h, w = density.shape
        hsv = np.empty((h, w, 3), dtype=np.uint8)
        hsv[..., 0] = (np.mod(hue, 1.0) * 180.0).astype(np.uint8)
        hsv[..., 1] = 255
        hsv[..., 2] = np.clip(val * 255.0, 0.0, 255.0).astype(np.uint8)
        frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        frame[density <= 1e-6] = 0
        return frame
