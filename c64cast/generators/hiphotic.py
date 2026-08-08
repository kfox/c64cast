"""The `hiphotic` generative source: WLED "Hiphotic" port, a nested trig
interference pattern."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("hiphotic")
class HiphoticSource(GenerativeSource):
    """WLED "Hiphotic" port: nested trig interference
    (`sin(cos(x...) + sin(y...) + a)`), reimplemented in continuous float
    instead of WLED's 8-bit sin8/cos8 lookup tables. Unlike Plasma, the
    `t`-driven phase sits *inside* the inner cos/sin terms rather than being
    added on at the end, so the combined field can't be precomputed once and
    modulo'd per frame the way Plasma's can — only the raw `xs`/`ys` pixel
    grids are cached; the rest is recomputed every `render()` call. WLED
    exposes independent X-scale/Y-scale sliders; those collapse here into one
    `scale` LIVE_PARAM (a deliberate simplification)."""

    LIVE_PARAMS = {"speed": (0.1, 8.0), "scale": (0.1, 4.0)}

    # Tuned by eye at 320x200; scale=1.0 ~= WLED's default band density.
    _BASE_FREQ = 0.02

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 1.5,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        k = self.scale * self._BASE_FREQ
        a = t * self.speed
        inner_x = np.cos(self._xs * k + a / 3.0)
        inner_y = np.sin(self._ys * k + a / 4.0)
        hue = (np.sin(inner_x + inner_y + a) + 1.0) * 0.5
        if modulation is None:
            return self._hsv_to_bgr(hue)
        hue = hue + self._reactive_hue_offset(modulation)
        return self._hsv_to_bgr(hue, val=self._reactive_value(modulation))
