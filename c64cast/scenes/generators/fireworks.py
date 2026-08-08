"""The `fireworks` generative source: WLED "Fireworks" port — the flagship of WLED's shared particle-system engine, which also drives Volcano/Ballpit/Waterfall/Impact/Attractor/ Galaxy as different emitter/gravity presets on the same primitive; only the fireworks preset is ported here."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


@register("fireworks")
class FireworksSource(GenerativeSource):
    """WLED "Fireworks" port — the flagship of WLED's shared particle-system
    engine, which also drives Volcano/Ballpit/Waterfall/Impact/Attractor/
    Galaxy as different emitter/gravity presets on the same primitive; only
    the fireworks preset is ported here.

    A small fixed-size particle pool (preallocated numpy arrays — position /
    velocity / age / life / hue — updated with vectorized array ops, no
    per-particle Python loop) simulates: shells launch upward on a randomized
    schedule, arc under gravity, and explode into a burst of particles on a
    randomized fuse timer; particles then fall under gravity with velocity
    drag, fading out over their lifetime.

    Like `SoapSource`, this carries real incremental state (particle physics
    can't be cheaply replayed from an arbitrary `t` — position depends on the
    whole integration history) via the same tick-accumulator pattern: no
    advance in `t` -> no physics step -> the current frame is re-returned
    unchanged. Shell/particle spawn timing draws from a `numpy` Generator
    advanced once per step (not reseeded per call), so a given *real playback
    sequence* is reproducible run-to-run but — unlike the pure generators —
    not byte-identical for an arbitrary directly-requested `t` on a fresh
    instance; this is the deliberate tradeoff that comes with genuine particle
    state, documented rather than worked around.

    No synthetic per-particle trail is drawn (unlike halo/epicycle/
    colored_bursts' time-lag echoes) — pairing this scene with the existing
    `trails` FrameEffect gives the classic streak look for free, the same way
    `dna`/`metaballs` lean on `blur` rather than reinventing persistence
    per-generator."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.3, 3.0)}

    _STEP_S = 1.0 / 30.0
    _MAX_SHELLS = 6
    _MAX_PARTICLES = 260
    _BURST_SIZE = 45
    _GRAVITY_FRAC = 0.55  # px/s^2 of downward accel, as a fraction of height
    _LAUNCH_SPEED_FRAC = 0.9  # shell launch speed, as a fraction of height/s
    _DRAG = 0.985  # per-tick multiplicative velocity decay (particles only)
    _FUSE_TICKS_RANGE = (14, 26)  # ticks before a shell explodes
    _LAUNCH_INTERVAL_RANGE = (0.4, 1.1)  # seconds between shell launches at speed=1
    _PARTICLE_SPEED_FRAC = 0.35  # burst particle speed, as a fraction of height/s
    _LIFE_RANGE = (0.7, 1.3)  # seconds a burst particle survives
    _DOT_RADIUS_KERNEL = 2
    _ONSET_THRESHOLD = 0.55
    _LEVEL_INTERVAL_GAIN = 0.6  # loudness shortens the next launch interval

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
        self._rng = np.random.default_rng(0xF12E ^ 0x5040)
        self._gravity = height * self._GRAVITY_FRAC
        self._launch_speed = height * self._LAUNCH_SPEED_FRAC
        self._particle_speed = height * self._PARTICLE_SPEED_FRAC
        self._init_state()
        self._last_t = 0.0
        self._accum = 0.0

    def _init_state(self) -> None:
        n_s = self._MAX_SHELLS
        self._shell_alive = np.zeros(n_s, dtype=bool)
        self._shell_x = np.zeros(n_s, dtype=np.float32)
        self._shell_y = np.zeros(n_s, dtype=np.float32)
        self._shell_vy = np.zeros(n_s, dtype=np.float32)
        self._shell_hue = np.zeros(n_s, dtype=np.float32)
        self._shell_fuse = np.zeros(n_s, dtype=np.int32)
        self._shell_age = np.zeros(n_s, dtype=np.int32)

        n_p = self._MAX_PARTICLES
        self._p_alive = np.zeros(n_p, dtype=bool)
        self._p_x = np.zeros(n_p, dtype=np.float32)
        self._p_y = np.zeros(n_p, dtype=np.float32)
        self._p_vx = np.zeros(n_p, dtype=np.float32)
        self._p_vy = np.zeros(n_p, dtype=np.float32)
        self._p_age = np.zeros(n_p, dtype=np.float32)
        self._p_life = np.zeros(n_p, dtype=np.float32)
        self._p_hue = np.zeros(n_p, dtype=np.float32)
        self._next_launch_s = 0.0
        self._sim_t = 0.0

    def reset(self) -> None:
        self._init_state()
        self._last_t = 0.0
        self._accum = 0.0

    def _launch_shell(self) -> None:
        free = np.nonzero(~self._shell_alive)[0]
        if free.size == 0:
            return
        i = int(free[0])
        self._shell_alive[i] = True
        self._shell_x[i] = self._rng.uniform(0.2, 0.8) * self.width
        self._shell_y[i] = float(self.height - 1)
        self._shell_vy[i] = -self._launch_speed * self._rng.uniform(0.85, 1.15)
        self._shell_hue[i] = self._rng.uniform(0.0, 1.0)
        lo, hi = self._FUSE_TICKS_RANGE
        self._shell_fuse[i] = self._rng.integers(lo, hi + 1)
        self._shell_age[i] = 0

    def _explode(self, x: float, y: float, hue: float) -> None:
        free = np.nonzero(~self._p_alive)[0]
        k = min(self._BURST_SIZE, free.size)
        if k == 0:
            return
        idx = free[:k]
        angles = self._rng.uniform(0.0, 2.0 * math.pi, k)
        speeds = self._rng.uniform(0.4, 1.0, k) * self._particle_speed * self.scale
        self._p_alive[idx] = True
        self._p_x[idx] = x
        self._p_y[idx] = y
        self._p_vx[idx] = np.cos(angles) * speeds
        self._p_vy[idx] = np.sin(angles) * speeds
        self._p_age[idx] = 0.0
        lo, hi = self._LIFE_RANGE
        self._p_life[idx] = self._rng.uniform(lo, hi, k)
        self._p_hue[idx] = np.mod(hue + self._rng.uniform(-0.06, 0.06, k), 1.0)

    def _step(self) -> None:
        dt = self._STEP_S
        self._sim_t += dt
        if self._sim_t >= self._next_launch_s:
            self._launch_shell()
            lo, hi = self._LAUNCH_INTERVAL_RANGE
            self._next_launch_s = self._sim_t + self._rng.uniform(lo, hi)

        alive = self._shell_alive
        if alive.any():
            self._shell_vy[alive] += self._gravity * dt
            self._shell_y[alive] += self._shell_vy[alive] * dt
            self._shell_age[alive] += 1
            fuse_done = alive & (self._shell_age >= self._shell_fuse)
            offscreen = alive & (self._shell_y < 0.0)
            for i in np.nonzero(fuse_done | offscreen)[0]:
                if fuse_done[i]:
                    self._explode(
                        float(self._shell_x[i]), float(self._shell_y[i]), float(self._shell_hue[i])
                    )
                self._shell_alive[i] = False

        palive = self._p_alive
        if palive.any():
            self._p_vy[palive] += self._gravity * dt
            self._p_vx[palive] *= self._DRAG
            self._p_vy[palive] *= self._DRAG
            self._p_x[palive] += self._p_vx[palive] * dt
            self._p_y[palive] += self._p_vy[palive] * dt
            self._p_age[palive] += dt
            self._p_alive &= (self._p_age < self._p_life) & (self._p_y < self.height + 8)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        dt = t - self._last_t
        self._last_t = t
        if dt > 0.0:
            speed = max(self.speed, 0.0)
            level_gain = 0.0 if modulation is None else self._LEVEL_INTERVAL_GAIN * modulation.level
            self._accum += dt * speed
            while self._accum >= self._STEP_S:
                self._step()
                self._accum -= self._STEP_S
            if level_gain > 0.0:
                self._next_launch_s = max(
                    self._sim_t, self._next_launch_s - level_gain * self._STEP_S
                )
        # A strong transient bursts immediately regardless of whether a physics
        # tick fired this call — an onset is a discrete "the beat hit" reaction,
        # not something that should wait on the tick accumulator.
        if modulation is not None and modulation.onset > self._ONSET_THRESHOLD:
            self._explode(
                float(self._rng.uniform(0.2, 0.8) * self.width),
                float(self._rng.uniform(0.2, 0.6) * self.height),
                float(self._rng.uniform(0.0, 1.0)),
            )

        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        val = 1.0 if modulation is None else self._reactive_value(modulation)

        s_idx = np.nonzero(self._shell_alive)[0]
        p_idx = np.nonzero(self._p_alive)[0]
        if s_idx.size:
            sx = np.clip(self._shell_x[s_idx], 0, self.width - 1).astype(np.int32)
            sy = np.clip(self._shell_y[s_idx], 0, self.height - 1).astype(np.int32)
            scolors = self._hsv_to_bgr(self._shell_hue[s_idx][None, :], val=val)[0]
            frame[sy, sx] = scolors
        if p_idx.size:
            px = np.clip(self._p_x[p_idx], 0, self.width - 1).astype(np.int32)
            py = np.clip(self._p_y[p_idx], 0, self.height - 1).astype(np.int32)
            fade = np.clip(1.0 - self._p_age[p_idx] / self._p_life[p_idx], 0.0, 1.0)
            pcolors = self._hsv_to_bgr(self._p_hue[p_idx][None, :], val=val)[0]
            pcolors = (pcolors.astype(np.float32) * fade[:, None]).astype(np.uint8)
            frame[py, px] = pcolors
        if s_idx.size or p_idx.size:
            frame = cv2.dilate(
                frame, np.ones((self._DOT_RADIUS_KERNEL, self._DOT_RADIUS_KERNEL), np.uint8)
            )
        return frame
