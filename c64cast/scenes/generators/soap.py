"""The `soap` generative source: WLED "Soap" port: a persistent color buffer smeared/advected each tick by a slowly-rotating noise-driven flow field — the classic swirling soap-film look."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register
from ._noise import periodic_value_noise

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


@register("soap")
class SoapSource(GenerativeSource):
    """WLED "Soap" port: a persistent color buffer smeared/advected each tick
    by a slowly-rotating noise-driven flow field — the classic swirling
    soap-film look. WLED derives its flow from `perlin8`; this codebase has no
    Perlin primitive, so (mirroring `metaballs`'/`hopalong`'s precedent of
    substituting an existing tool rather than adding one) it reuses the
    tileable value-noise helper `periodic_value_noise` already built for
    `FireSource`, sampled twice for independent x/y flow components.

    Unlike `GameOfLifeSource`, replaying this from scratch every frame
    is too expensive (a full-buffer `cv2.remap` per generation, not a handful
    of scalar ops), so this carries **real incremental state**: `render(t,
    ...)` tracks elapsed scene-clock time since the last call and advances a
    fixed-size-tick accumulator (the standard fixed-timestep-with-accumulator
    pattern — handles variable frame arrival / dropped frames gracefully,
    same spirit as the pure generators' "dropped frames harmless" guarantee,
    just via accumulation instead of recomputation). A call whose `t` doesn't
    advance (repeated or a backward jump) takes no step and re-returns the
    current buffer, so `render(t, None)` is still stable for a fixed,
    non-advancing `t` — the property the shared determinism test checks —
    even though (unlike the pure generators) jumping directly to an arbitrary
    `t` on a fresh instance does *not* reproduce the same frame as advancing
    there gradually; state is genuinely carried, not replayed. A small
    fraction of the original seed pattern is blended back in every step (an
    energy-injection term) so repeated bilinear remapping doesn't decay the
    buffer to a flat gray over a long-running scene."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.2, 3.0)}

    _STEP_S = 0.08
    _PHASE_STEP = 0.012  # radians the flow field's rotation advances per step
    _FLOW_FRAC = 0.05  # base flow displacement, as a fraction of min(width,height)
    _INJECT = 0.006  # per-step blend-back of the original seed pattern

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 1.0,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys
        rng = np.random.default_rng(0x50A9)
        seed_hue = periodic_value_noise(rng, height, width, octaves=[(3, 4, 1.0), (6, 8, 0.5)])
        self._seed_buf = self._hsv_to_bgr(seed_hue).astype(np.float32)
        flow_a = periodic_value_noise(rng, height, width, octaves=[(4, 5, 1.0), (9, 11, 0.5)])
        flow_b = periodic_value_noise(rng, height, width, octaves=[(5, 4, 1.0), (11, 9, 0.5)])
        self._flow_a = flow_a * 2.0 - 1.0
        self._flow_b = flow_b * 2.0 - 1.0
        self._flow_amp = min(width, height) * self._FLOW_FRAC
        self._buf = self._seed_buf.copy()
        self._phase = 0.0
        self._last_t = 0.0
        self._accum = 0.0

    def reset(self) -> None:
        self._buf = self._seed_buf.copy()
        self._phase = 0.0
        self._last_t = 0.0
        self._accum = 0.0

    def _step(self) -> None:
        self._phase += self._PHASE_STEP
        c, s = math.cos(self._phase), math.sin(self._phase)
        vx = self._flow_a * c + self._flow_b * s
        vy = -self._flow_a * s + self._flow_b * c
        amp = self._flow_amp * self.scale
        map_x = (self._xs + vx * amp).astype(np.float32)
        map_y = (self._ys + vy * amp).astype(np.float32)
        warped = cv2.remap(
            self._buf, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP
        )
        self._buf = warped * (1.0 - self._INJECT) + self._seed_buf * self._INJECT

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        dt = t - self._last_t
        self._last_t = t
        if dt > 0.0:
            self._accum += dt * max(self.speed, 0.0)
            while self._accum >= self._STEP_S:
                self._step()
                self._accum -= self._STEP_S
        gain = 1.0 if modulation is None else self._reactive_value(modulation) * 1.3
        frame = np.clip(self._buf * gain, 0.0, 255.0).astype(np.uint8)
        return frame
