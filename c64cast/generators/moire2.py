"""The `moire2` generative source: two concentric-ring distance fields whose
centers drift apart and together, summed into a moiré interference pattern."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("moire2")
class Moire2Source(GenerativeSource):
    """Two concentric-ring distance fields whose centers drift apart and
    together, summed into a classic moiré interference pattern (each field is
    `sin(distance-to-center * freq)`; xscreensaver's moire2.c gets the same
    beat pattern by XOR-compositing two arc bitmaps — this is the closed-form
    equivalent: a distance field instead of drawn arcs)."""

    LIVE_PARAMS = {"ring_freq": (10.0, 80.0), "drift_speed": (0.0, 2.0)}

    _DRIFT_FRAC = 0.22  # max center separation, as a fraction of width
    _VOICE_FREQ_GAIN = 0.03  # ring-freq nudge per Hz of the driving voice
    _HUE_DRIFT = 0.05  # base hue cycle rate (independent of the music)

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        ring_freq: float = 36.0,
        drift_speed: float = 0.35,
    ):
        super().__init__(width=width, height=height)
        self.ring_freq = float(ring_freq)
        self.drift_speed = float(drift_speed)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        cx, cy = self.width / 2.0, self.height / 2.0
        phase = t * self.drift_speed
        freq_a = freq_b = self.ring_freq
        if modulation is not None:
            # Tempo breathes the center separation; each ring tracks a
            # different voice's pitch so the two families drift apart in
            # frequency, not just in space.
            phase += modulation.beat_phase * 0.15
            freq_a = self.ring_freq + modulation.voice_freqs[0] * self._VOICE_FREQ_GAIN
            freq_b = self.ring_freq + modulation.voice_freqs[1] * self._VOICE_FREQ_GAIN
        sep = self.width * self._DRIFT_FRAC * math.sin(phase)
        ra = np.hypot(self._xs - (cx - sep), self._ys - cy)
        rb = np.hypot(self._xs - (cx + sep), self._ys - cy)
        field = np.sin(ra / freq_a * (2.0 * math.pi)) + np.sin(rb / freq_b * (2.0 * math.pi))
        hue = (field + 2.0) / 4.0 + t * self._HUE_DRIFT
        if modulation is None:
            return self._hsv_to_bgr(hue)
        hue = hue + self._reactive_hue_offset(modulation)
        return self._hsv_to_bgr(hue, val=self._reactive_value(modulation))
