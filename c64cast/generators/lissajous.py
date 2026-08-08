"""The `lissajous` generative source: WLED "Lissajous" port: a classic XY curve (`x = sin(theta*freq_x + phase)`, `y = cos(theta*2 + phase)`) sampled at a fixed number of points along its parametrization."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("lissajous")
class LissajousSource(GenerativeSource):
    """WLED "Lissajous" port: a classic XY curve (`x = sin(theta*freq_x +
    phase)`, `y = cos(theta*2 + phase)`) sampled at a fixed number of points
    along its parametrization. WLED's own version already redraws all 256
    points from scratch on every render call (a `fadeToBlackBy` trail is
    layered on top for a soft cometary look, but the curve itself is fully
    drawn each time, not accumulated) — so, unlike the halo/epicycle family,
    this needs no synthetic time-lag echo to look continuous: `render(t,
    None)` samples the whole curve fresh from a closed form every frame.
    WLED's independent X-frequency and rotation-speed sliders map to `scale`
    (curve shape) and `speed` (rotation rate)."""

    LIVE_PARAMS = {"speed": (0.0, 4.0), "scale": (0.2, 6.0)}

    _N_POINTS = 256
    _Y_FREQ = 2.0  # fixed y-axis frequency (WLED hardcodes `i*2` for the cos term)
    _HUE_CYCLES = 1.0  # hue cycles once per full curve sweep
    _LEVEL_GAIN = 0.25
    _BEAT_PHASE_GAIN = 0.3

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.6,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        self._theta = np.linspace(
            0.0, 2.0 * math.pi, self._N_POINTS, endpoint=False, dtype=np.float64
        )
        self._i_frac = np.linspace(0.0, 1.0, self._N_POINTS, endpoint=False, dtype=np.float32)
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._amp_x = (width / 2.0) * 0.92
        self._amp_y = (height / 2.0) * 0.92

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        phase = t * self.speed
        level_gain = 0.0
        hue_off = 0.0
        val = 1.0
        if modulation is not None:
            phase += modulation.beat_phase * self._BEAT_PHASE_GAIN
            level_gain = self._LEVEL_GAIN * modulation.level
            hue_off = self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        xs = self._cx + self._amp_x * (1.0 + level_gain) * np.sin(self._theta * self.scale + phase)
        ys = self._cy + self._amp_y * (1.0 + level_gain) * np.cos(
            self._theta * self._Y_FREQ + phase
        )
        px = np.clip(xs, 0, self.width - 1).astype(np.int32)
        py = np.clip(ys, 0, self.height - 1).astype(np.int32)
        hue = self._i_frac * self._HUE_CYCLES + t * 0.05 + hue_off
        colors = self._hsv_to_bgr(hue[None, :], val=val)[0]
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[py, px] = colors
        return cv2.dilate(frame, np.ones((2, 2), np.uint8))
