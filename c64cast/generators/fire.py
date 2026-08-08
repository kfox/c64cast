"""The `fire` generative source: an upward-scrolling turbulence texture masked
by a bottom-hot vertical gradient and color-mapped black→red→yellow→white."""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register
from ._noise import periodic_value_noise

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("fire")
class FireSource(GenerativeSource):
    """Rising fire: an upward-scrolling turbulence texture masked by a
    bottom-hot vertical gradient and color-mapped black→red→yellow→white
    (`cv2.COLORMAP_HOT` — a near-perfect match for the C64 palette). The
    turbulence is precomputed and *tileable*, so the scroll is a pure function
    of `t` (deterministic, dropped-frames-safe) rather than a stateful cellular
    sim — `render(t, None)` reproduces exactly.

    Reactive (the headline): `level` raises the flames (louder → taller/hotter),
    `onset` flares them on each transient. Both push more of the field toward
    the yellow/white end of COLORMAP_HOT, so the fire visibly leaps on the beat
    — the most legible music reaction after 16-color quantization."""

    # Scroll period (texture rows). The flames rise one full period per
    # period/scroll_speed seconds; a tall period keeps the motion organic.
    _PERIOD = 256
    # Reactive gains (None path uses gain=1, flare=0 — plain rising fire).
    _LEVEL_HEIGHT = 0.85  # extra heat gain at full level (taller, hotter flames)
    _ONSET_FLARE = 0.80  # extra heat gain on a full-strength transient

    # `intensity` scales the overall heat/flame height (the ix live knob),
    # applied on top of the reactive gain. 1.0 == the historical baseline.
    LIVE_PARAMS = {"scroll_speed": (0.0, 4.0), "intensity": (0.2, 2.0)}

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        scroll_speed: float = 1.1,
        intensity: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.scroll_speed = float(scroll_speed)
        self.intensity = float(intensity)
        rng = np.random.default_rng(0xF12E)
        self._turb = periodic_value_noise(
            rng,
            self._PERIOD,
            width,
            octaves=[(4, 3, 1.0), (8, 6, 0.6), (16, 12, 0.35), (32, 24, 0.2)],
        )
        # Bottom-hot vertical gradient: 0 at the top row, 1 at the bottom.
        # The 1.2 power pulls the flame tips down a touch so they taper.
        grad = np.linspace(0.0, 1.0, height, dtype=np.float32) ** 1.2
        self._grad = grad[:, None]  # (H, 1)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        off = int(t * self.scroll_speed * self._PERIOD) % self._PERIOD
        rows = (off + np.arange(self.height)) % self._PERIOD
        turb = self._turb[rows]  # (H, W), scrolled (wraps seamlessly)
        gain, flare = 1.0, 0.0
        if modulation is not None:
            gain = 1.0 + self._LEVEL_HEIGHT * modulation.level
            flare = self._ONSET_FLARE * modulation.onset
        heat = np.clip(turb * self._grad * gain * (1.0 + flare) * self.intensity, 0.0, 1.0)
        u8 = (heat * 255.0).astype(np.uint8)
        return cv2.applyColorMap(u8, cv2.COLORMAP_HOT)
