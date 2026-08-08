"""The `dna` generative source: WLED "DNA" port: two sine strands sweeping the full frame width, phase-shifted by half a cycle (`pi`, matching WLED's `i*4` vs `i*4+128` offset) so they wind around a shared center line like a double helix; color cycles per column + time."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("dna")
class DnaSource(GenerativeSource):
    """WLED "DNA" port: two sine strands sweeping the full frame width,
    phase-shifted by half a cycle (`pi`, matching WLED's `i*4` vs `i*4+128`
    offset) so they wind around a shared center line like a double helix;
    color cycles per column + time. WLED redraws every column on each render
    call — its softening comes entirely from `SEGMENT.blur`, not from state
    carried between frames — so this ports directly as a pure function of
    `t`: each column's y-position is a closed-form `sin`, sampled fresh every
    frame. Pair with the `blur` effect (see effect-trails.toml for the
    pattern) for WLED's own soft-edged look; unblurred it reads as a crisp
    oscilloscope-style double trace."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.3, 4.0)}

    _PERIOD_CYCLES = 3.0  # full sine cycles across the frame width at scale=1.0
    _AMP_FRAC = 0.38  # strand amplitude, as a fraction of height
    _LEVEL_AMP_GAIN = 0.3
    _BEAT_PHASE_GAIN = 0.6

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
        self._xs = np.arange(width, dtype=np.int32)
        self._xfrac = self._xs.astype(np.float32) / width
        self._cy = height / 2.0
        self._amp = height * self._AMP_FRAC

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        phase = t * self.speed * 2.0 * math.pi
        w = self._xfrac * self._PERIOD_CYCLES * self.scale * 2.0 * math.pi
        amp = self._amp
        hue_off = 0.0
        val = 1.0
        if modulation is not None:
            phase += modulation.beat_phase * self._BEAT_PHASE_GAIN
            amp *= 1.0 + self._LEVEL_AMP_GAIN * modulation.level
            hue_off = self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        y1 = self._cy + amp * np.sin(w + phase)
        y2 = self._cy + amp * np.sin(w + phase + math.pi)
        y1i = np.clip(y1, 0, self.height - 1).astype(np.int32)
        y2i = np.clip(y2, 0, self.height - 1).astype(np.int32)
        hue1 = self._xfrac * 0.6 + t * 0.05 + hue_off
        hue2 = self._xfrac * 0.6 + 0.5 + t * 0.05 + hue_off
        colors1 = self._hsv_to_bgr(hue1[None, :], val=val)[0]
        colors2 = self._hsv_to_bgr(hue2[None, :], val=val)[0]
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[y1i, self._xs] = colors1
        frame[y2i, self._xs] = colors2
        return cv2.dilate(frame, np.ones((3, 3), np.uint8))
