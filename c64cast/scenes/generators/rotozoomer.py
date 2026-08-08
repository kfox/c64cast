"""The `rotozoomer` generative source: WLED "Rotozoomer" port: a static XOR bit-pattern texture (`(x*4) ^ (y*4)`, precomputed + colorized once) sampled through a rotating/zooming affine transform."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


@register("rotozoomer")
class RotozoomerSource(GenerativeSource):
    """WLED "Rotozoomer" port: a static XOR bit-pattern texture (`(x*4) ^
    (y*4)`, precomputed + colorized once) sampled through a rotating/zooming
    affine transform. WLED integrates its rotation angle once per render call
    (`angle -= 0.03 + (speed-128)*0.0002`), tied to WLED's own frame cadence
    rather than wall-clock time — incompatible with this codebase's
    pure-function-of-`t` contract, so the angle is redefined here as a closed
    form, `angle(t) = -speed * t`, exactly the same "phase advances linearly
    with `t`" pattern Plasma/Tunnel already use for their hue rotation. Also
    the first use of `cv2.warpAffine` in this codebase: `BORDER_WRAP` mirrors
    WLED's modulo-wrapped texture lookup. WLED's alternate Perlin-noise
    texture mode ("Alt") is not ported — a documented scope-narrowing, not an
    oversight."""

    LIVE_PARAMS = {"speed": (0.0, 4.0), "scale": (0.2, 4.0)}

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
        ys, xs = np.mgrid[0:height, 0:width].astype(np.uint16)
        pattern = ((xs * 4) ^ (ys * 4)) & 0xFF
        hue = pattern.astype(np.float32) / 255.0
        self._texture = self._hsv_to_bgr(hue)
        self._center = (width / 2.0, height / 2.0)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        angle_deg = math.degrees(-self.speed * t)
        matrix = cv2.getRotationMatrix2D(self._center, angle_deg, self.scale)
        frame = cv2.warpAffine(
            self._texture,
            matrix,
            (self.width, self.height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_WRAP,
        )
        if modulation is None:
            return frame
        gain = self._reactive_value(modulation)
        return np.clip(frame.astype(np.float32) * gain, 0.0, 255.0).astype(np.uint8)
