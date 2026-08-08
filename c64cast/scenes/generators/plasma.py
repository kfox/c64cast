"""The `plasma` generative source: Classic sine-sum plasma whose hue cycles over time."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


@register("plasma")
class PlasmaSource(GenerativeSource):
    """Classic sine-sum plasma whose hue cycles over time. The spatial field
    is precomputed once; per-frame work is one modulo + HSV→BGR convert."""

    LIVE_PARAMS = {"speed": (0.0, 2.0), "scale": (0.1, 4.0)}

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.35,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        field = (
            np.sin(xs / 16.0)
            + np.sin(ys / 8.0)
            + np.sin((xs + ys) / 16.0)
            + np.sin(np.sqrt((xs - width / 2.0) ** 2 + (ys - height / 2.0) ** 2) / 8.0)
        )
        # Normalize to ~[0,1] so `scale` maps to a predictable number of hue cycles.
        self._field = (field - field.min()) / (field.max() - field.min() + 1e-6)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        if modulation is None:
            hue = self._field * self.scale + t * self.speed
            return self._hsv_to_bgr(hue)
        # Reactive: beat_phase speeds the hue cycle with the tempo; an onset kicks
        # the hue and flashes the brightness. beat_phase is frozen while silent,
        # so this degrades smoothly to the baseline drift when nothing's playing.
        hue = self._field * self.scale + t * self.speed + self._reactive_hue_offset(modulation)
        return self._hsv_to_bgr(hue, val=self._reactive_value(modulation))
