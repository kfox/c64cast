"""The `colored_bursts` generative source: WLED "Colored Bursts" port: several lines burst from one common, slowly-orbiting point out to per-line endpoints that trace their own faster orbits — WLED's shared start point has no per-line phase offset, while the per-line `i*24`/`i*48+64` phase spread on the *other* endpoint is what fans the lines out into a burst."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


@register("colored_bursts")
class ColoredBurstsSource(GenerativeSource):
    """WLED "Colored Bursts" port: several lines burst from one common,
    slowly-orbiting point out to per-line endpoints that trace their own
    faster orbits — WLED's shared start point has no per-line phase offset,
    while the per-line `i*24`/`i*48+64` phase spread on the *other* endpoint
    is what fans the lines out into a burst. A short trailing-echo stack
    (the same pattern `halo`/`epicycle` use) stands in for WLED's own
    `fadeToBlackBy` accumulation, since this must stay a pure function of
    `t`; echoes are drawn oldest-first so the brightest (most recent)
    position always paints on top."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.3, 3.0)}

    _N_LINES = 6
    _N_ECHOES = 3
    _ECHO_LAG = 0.05
    _ECHO_DECAY = 0.45
    _ONSET_FLASH_GAIN = 90.0
    _LEVEL_GAIN = 0.4
    _BEAT_PHASE_GAIN = 0.4

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
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._amp = min(width, height) * 0.4
        n = self._N_LINES
        self._end_phase_x = np.arange(n, dtype=np.float64) * (2.0 * math.pi / 12.0)
        self._end_phase_y = np.arange(n, dtype=np.float64) * (2.0 * math.pi / 7.0) + 1.1
        self._colors = [
            self._hsv_to_bgr(np.full((1, 1), i / n, np.float32))[0, 0].tolist() for i in range(n)
        ]

    def _endpoints(
        self, tt: float, amp: float
    ) -> tuple[tuple[float, float], np.ndarray, np.ndarray]:
        ax = self._cx + amp * math.sin(tt * 0.9)
        ay = self._cy + amp * math.sin(tt * 0.7)
        bx = self._cx + amp * np.sin(tt * 1.6 + self._end_phase_x)
        by = self._cy + amp * np.sin(tt * 1.3 + self._end_phase_y)
        return (ax, ay), bx, by

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        tt = t * self.speed
        amp = self._amp * self.scale
        onset = 0.0
        if modulation is not None:
            tt += modulation.beat_phase * self._BEAT_PHASE_GAIN
            amp *= 1.0 + self._LEVEL_GAIN * modulation.level
            onset = modulation.onset
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        for e in reversed(range(self._N_ECHOES)):
            te = tt - e * self._ECHO_LAG
            fade = self._ECHO_DECAY**e
            (ax, ay), bx, by = self._endpoints(te, amp)
            p0 = (int(round(ax)), int(round(ay)))
            for j in range(self._N_LINES):
                p1 = (int(round(bx[j])), int(round(by[j])))
                color = tuple(int(c * fade) for c in self._colors[j])
                cv2.line(frame, p0, p1, color, 1, cv2.LINE_AA)
        if onset > 0.0:
            flash = int(self._ONSET_FLASH_GAIN * onset)
            frame = cv2.add(frame, np.full_like(frame, flash))
        return frame
