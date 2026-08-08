"""The `tunnel` generative source: Infinite-zoom tunnel: hue is driven by per-pixel depth (1/radius) and angle, scrolled over time."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


@register("tunnel")
class TunnelSource(GenerativeSource):
    """Infinite-zoom tunnel: hue is driven by per-pixel depth (1/radius) and
    angle, scrolled over time. Depth + angle fields are precomputed once."""

    # `scale` multiplies the 0.05 depth coefficient (the ix live knob): higher
    # packs more concentric rings toward the mouth of the tunnel. 1.0 == the
    # historical fixed depth.
    LIVE_PARAMS = {"speed": (0.0, 2.0), "scale": (0.25, 4.0)}

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
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        dx = xs - width / 2.0
        dy = ys - height / 2.0
        r = np.sqrt(dx * dx + dy * dy) + 1e-3
        self._depth = (width * 0.5) / r  # large near center
        self._angle = np.arctan2(dy, dx) / (2.0 * np.pi)  # -0.5..0.5

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        depth_coeff = 0.05 * self.scale
        if modulation is None:
            hue = self._depth * depth_coeff + self._angle + t * self.speed
            return self._hsv_to_bgr(hue)
        # Reactive: same generic treatment as plasma (tempo cycles the colors,
        # onsets pulse). The depth-driven tunnel shape itself stays time-locked.
        offset = t * self.speed + self._reactive_hue_offset(modulation)
        hue = self._depth * depth_coeff + self._angle + offset
        return self._hsv_to_bgr(hue, val=self._reactive_value(modulation))
