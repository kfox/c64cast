"""The `drift` generative source: WLED "Drift" port: a rotating spiral trail — for radii `i` stepping outward from center, a point at angle `t*(maxDim-i)` traces a full spiral arm every frame."""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("drift")
class DriftSource(GenerativeSource):
    """WLED "Drift" port: a rotating spiral trail — for radii `i` stepping
    outward from center, a point at angle `t*(maxDim-i)` traces a full
    spiral arm every frame. Like `lissajous`, WLED already redraws the whole
    arm (`i` from 1 to maxDim) on every render call, so this ports as a pure
    function of `t` with no synthetic echo needed. Always draws both the
    `(sin,cos)` point AND its `(cos,sin)` mirror — WLED gates the mirror
    behind a "Twin" checkbox this codebase has no per-scene boolean toggle
    for, so it's always-on here, a deliberate simplification that gives a
    fuller, more symmetric rose by default."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.3, 2.0)}

    _STEP = 0.25
    _HUE_SCALE = 0.08
    _HUE_DRIFT = 0.05
    _LEVEL_GAIN = 0.3

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.5,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._max_dim = min(width, height) / 2.0
        self._i = np.arange(1.0, self._max_dim, self._STEP, dtype=np.float64)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        radius_scale = self.scale
        hue_off = 0.0
        val = 1.0
        if modulation is not None:
            radius_scale *= 1.0 + self._LEVEL_GAIN * modulation.level
            hue_off = self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        i = self._i
        angle = t * self.speed * (self._max_dim - i)
        r = i * radius_scale
        s = np.sin(angle)
        c = np.cos(angle)
        x1 = np.clip(self._cx + r * s, 0, self.width - 1).astype(np.int32)
        y1 = np.clip(self._cy + r * c, 0, self.height - 1).astype(np.int32)
        x2 = np.clip(self._cx + r * c, 0, self.width - 1).astype(np.int32)
        y2 = np.clip(self._cy + r * s, 0, self.height - 1).astype(np.int32)
        hue = np.mod(i * self._HUE_SCALE + t * self._HUE_DRIFT + hue_off, 1.0).astype(np.float32)
        colors = self._hsv_to_bgr(hue[None, :], val=val)[0]
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[y1, x1] = colors
        frame[y2, x2] = colors
        return cv2.dilate(frame, np.ones((2, 2), np.uint8))
