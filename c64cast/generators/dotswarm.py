"""The `dotswarm` generative source: A WLED "beatsin dot swarm" port covering the shared shape of several kin effects — Black Hole, Frizzles, Sindots, Squared Swirl, Drift Rose — which all boil down to the same primitive: a handful of points, each independently orbiting via a bounded sine (`beatsin8` in WLED) at its own frequency, color-cycled and blended."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("dotswarm")
class DotSwarmSource(GenerativeSource):
    """A WLED "beatsin dot swarm" port covering the shared shape of several
    kin effects — Black Hole, Frizzles, Sindots, Squared Swirl, Drift Rose —
    which all boil down to the same primitive: a handful of points, each
    independently orbiting via a bounded sine (`beatsin8` in WLED) at its own
    frequency, color-cycled and blended. Rather than port each as its own
    near-identical generator, this ports the shared primitive ONCE with a
    fixed, varied per-dot frequency assortment (echoing the spread across all
    of them) plus a fixed white center dot (Black Hole's signature). A short
    trailing-echo stack fakes WLED's `fadeToBlackBy` persistence, the same
    pattern `halo`/`epicycle` use; echoes are drawn oldest-first so the
    brightest (most recent) position always paints on top."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.2, 2.0)}

    _N_DOTS = 12
    _N_ECHOES = 4
    _ECHO_LAG = 0.045
    _ECHO_DECAY = 0.5
    _ORBIT_FRAC = 0.42  # max orbit reach, as a fraction of min(width,height)
    _DOT_RADIUS = 2
    _LEVEL_GAIN = 0.25
    _BEAT_PHASE_GAIN = 0.3
    _ONSET_FLASH_GAIN = 60.0

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.7,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._orbit = min(width, height) * self._ORBIT_FRAC
        rng = np.random.default_rng(0xD07A)
        n = self._N_DOTS
        # Varied, deliberately non-harmonic per-dot frequencies (mirrors the
        # spread of distinct beatsin8 rates each WLED kin effect hand-picks).
        self._fx = rng.uniform(0.4, 2.6, n)
        self._fy = rng.uniform(0.4, 2.6, n)
        self._px = rng.uniform(0.0, 2.0 * math.pi, n)
        self._py = rng.uniform(0.0, 2.0 * math.pi, n)
        self._reach = rng.uniform(0.35, 1.0, n)
        hues = (np.arange(n) / n).astype(np.float32)
        self._colors = [
            self._hsv_to_bgr(np.full((1, 1), h, np.float32))[0, 0].tolist() for h in hues
        ]

    def _positions(self, tt: float, orbit: float) -> tuple[np.ndarray, np.ndarray]:
        x = self._cx + orbit * self._reach * np.sin(tt * self._fx + self._px)
        y = self._cy + orbit * self._reach * np.sin(tt * self._fy + self._py)
        return x, y

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        tt = t * self.speed
        orbit = self._orbit * self.scale
        gain = 1.0
        onset = 0.0
        if modulation is not None:
            tt += modulation.beat_phase * self._BEAT_PHASE_GAIN
            orbit *= 1.0 + self._LEVEL_GAIN * modulation.level
            gain = self._reactive_value(modulation) * 1.3
            onset = modulation.onset
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        for e in reversed(range(self._N_ECHOES)):
            te = tt - e * self._ECHO_LAG
            fade = self._ECHO_DECAY**e
            xs, ys = self._positions(te, orbit)
            for j in range(self._N_DOTS):
                color = tuple(int(c * fade) for c in self._colors[j])
                cv2.circle(
                    frame,
                    (int(round(xs[j])), int(round(ys[j]))),
                    self._DOT_RADIUS,
                    color,
                    -1,
                    cv2.LINE_AA,
                )
        cx, cy = int(round(self._cx)), int(round(self._cy))
        cv2.circle(frame, (cx, cy), self._DOT_RADIUS, (255, 255, 255), -1, cv2.LINE_AA)
        if gain != 1.0:
            frame = np.clip(frame.astype(np.float32) * gain, 0.0, 255.0).astype(np.uint8)
        if onset > 0.0:
            frame = cv2.add(frame, np.full_like(frame, int(self._ONSET_FLASH_GAIN * onset)))
        return frame
