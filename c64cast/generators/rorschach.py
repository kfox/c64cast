"""The `rorschach` generative source: Mirrored-symmetric ink-blot: a precomputed 2D random walk (fixed seed → deterministic) cumulative-summed from Gaussian steps, progressively revealed as `t` advances and reflected across the vertical center line — xscreensaver's rorschach."""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("rorschach")
class RorschachSource(GenerativeSource):
    """Mirrored-symmetric ink-blot: a precomputed 2D random walk (fixed seed
    → deterministic) cumulative-summed from Gaussian steps, progressively
    revealed as `t` advances and reflected across the vertical center line —
    xscreensaver's rorschach.c animates the same way (draw a few more walk
    points each frame); this stays a pure function of `t` by redrawing
    however much of the (fixed) walk is "revealed" by `t` from scratch each
    frame, rather than accumulating pixels frame to frame. The reveal loops
    (grow, hold briefly at full bloom, reset) so playback never visibly ends.

    Reactive: `level` scales the whole blot larger (louder → bigger ink
    mass); a strong transient jumps the reveal forward — the "restart" flash
    xscreensaver's mirror-restart evokes, without discarding the walk."""

    LIVE_PARAMS = {"grow_speed": (0.0, 4.0)}

    _N_STEPS = 6000
    _STEP_SIZE = 2.2
    _PERIOD_S = 20.0  # seconds for one grow-then-recede cycle (triangle wave)
    _LEVEL_SCALE_GAIN = 0.5
    _ONSET_JUMP_FRAC = 0.15
    _HUE_DRIFT = 0.05

    def __init__(
        self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, grow_speed: float = 1.0
    ):
        super().__init__(width=width, height=height)
        self.grow_speed = float(grow_speed)
        rng = np.random.default_rng(0x707C)
        steps = rng.normal(0.0, self._STEP_SIZE, size=(self._N_STEPS, 2)).astype(np.float32)
        walk = np.cumsum(steps, axis=0)
        walk -= walk.mean(axis=0)
        span = float(np.abs(walk).max()) + 1e-6
        scale = min(width, height) * 0.42 / span
        self._walk = walk * scale  # (_N_STEPS, 2) offsets from center

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        # Triangle wave (grow then recede) rather than a sawtooth, so the
        # cycle loops with no visible pop back to empty.
        phase = (t * self.grow_speed / self._PERIOD_S) % 2.0
        frac = phase if phase <= 1.0 else 2.0 - phase
        scale = 1.0
        hue = t * self._HUE_DRIFT
        if modulation is not None:
            frac = min(1.0, frac + self._ONSET_JUMP_FRAC * modulation.onset)
            scale = 1.0 + self._LEVEL_SCALE_GAIN * modulation.level
            hue += self._reactive_hue_offset(modulation)
        n_reveal = max(2, int(frac * self._N_STEPS))
        pts = self._walk[:n_reveal] * scale
        cx, cy = self.width / 2.0, self.height / 2.0
        color = self._hsv_to_bgr(np.full((1, 1), hue % 1.0, np.float32))[0, 0].tolist()
        xs = pts[:, 0]
        ys = pts[:, 1]
        px = np.concatenate([cx + xs, cx - xs]).astype(np.int32)
        py = np.concatenate([cy + ys, cy + ys]).astype(np.int32)
        valid = (px >= 0) & (px < self.width) & (py >= 0) & (py < self.height)
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[py[valid], px[valid]] = color
        return cv2.dilate(frame, np.ones((3, 3), np.uint8))
