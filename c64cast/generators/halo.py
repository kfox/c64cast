"""The `halo` generative source: several soft-edged halos drifting on
independent circular orbits, additively blended."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from ..modulation import MusicModulation


@register("halo")
class HaloSource(GenerativeSource):
    """Several soft-edged halos drifting on independent circular orbits,
    additively blended (bright where they overlap, no clear — matching
    xscreensaver's halo.c un-erased canvas). The "trail" halo.c gets by never
    clearing is faked here without carrying state across frames: each halo is
    drawn at a few trailing time-lags with decreasing brightness, all as a
    pure function of `t`.

    Reactive: `level` grows every halo's radius (louder → bigger blooms); a
    transient (`onset`) flashes in one extra halo centered on the frame,
    invisible at rest (its weight is scaled by `onset` directly)."""

    LIVE_PARAMS = {"drift_speed": (0.0, 2.0), "pulse_speed": (0.0, 3.0)}

    _N_HALOS = 4
    _N_ECHOES = 2
    _ECHO_LAG = 0.05  # seconds between trailing echoes
    _ECHO_DECAY = 0.4  # brightness multiplier per echo step back
    _PATH_FRAC = 0.42  # orbit radius, as a fraction of width/height
    _RADIUS_FRAC = 0.05  # halo radius, as a fraction of width
    _PULSE_FRAC = 0.015  # radius pulse amplitude, as a fraction of width
    _LEVEL_RADIUS_GAIN = 0.7  # extra radius fraction at full `level`
    _ONSET_HALO_RADIUS_FRAC = 0.22

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        drift_speed: float = 0.3,
        pulse_speed: float = 0.9,
    ):
        super().__init__(width=width, height=height)
        self.drift_speed = float(drift_speed)
        self.pulse_speed = float(pulse_speed)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys
        rng = np.random.default_rng(0x4A10)
        self._orbit_rate = rng.uniform(0.5, 1.3, self._N_HALOS)
        # Evenly spaced at t=0 (full-frame coverage from the first frame);
        # each halo's distinct orbit_rate then drifts them in and out of
        # alignment over time rather than clustering by luck of a random draw.
        self._orbit_phase = np.arange(self._N_HALOS) * (2.0 * math.pi / self._N_HALOS)
        self._pulse_rate = rng.uniform(0.6, 1.6, self._N_HALOS)
        self._pulse_phase = rng.uniform(0.0, 2.0 * math.pi, self._N_HALOS)
        hues = rng.uniform(0.0, 1.0, self._N_HALOS).astype(np.float32)
        self._colors = [
            self._hsv_to_bgr(np.full((1, 1), h, np.float32))[0, 0].astype(np.float32) for h in hues
        ]

    def _halo_center(self, i: int, t: float) -> tuple[float, float]:
        ang = self._orbit_phase[i] + t * self.drift_speed * self._orbit_rate[i]
        cx, cy = self.width / 2.0, self.height / 2.0
        rx = self.width * self._PATH_FRAC
        ry = self.height * self._PATH_FRAC
        return cx + rx * math.cos(ang), cy + ry * math.sin(ang)

    def _halo_radius(self, i: int, t: float, level_gain: float) -> float:
        base = self.width * self._RADIUS_FRAC
        pulse = (
            self.width
            * self._PULSE_FRAC
            * math.sin(t * self.pulse_speed * self._pulse_rate[i] + self._pulse_phase[i])
        )
        return (base + pulse) * (1.0 + level_gain)

    def _weight(self, cx: float, cy: float, r: float) -> np.ndarray:
        r = max(r, 1.0)
        d2 = (self._xs - cx) ** 2 + (self._ys - cy) ** 2
        return np.exp(-d2 / (2.0 * r * r))

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        level_gain = 0.0 if modulation is None else self._LEVEL_RADIUS_GAIN * modulation.level
        acc = np.zeros((self.height, self.width, 3), dtype=np.float32)
        for i in range(self._N_HALOS):
            r = self._halo_radius(i, t, level_gain)
            w = np.zeros((self.height, self.width), dtype=np.float32)
            for e in range(self._N_ECHOES):
                te = t - e * self._ECHO_LAG
                cx, cy = self._halo_center(i, te)
                w += (self._ECHO_DECAY**e) * self._weight(cx, cy, r)
            acc += w[..., None] * self._colors[i]
        if modulation is not None and modulation.onset > 0.0:
            cx, cy = self.width / 2.0, self.height / 2.0
            r = self.width * self._ONSET_HALO_RADIUS_FRAC
            flash = (modulation.onset * self._weight(cx, cy, r))[..., None]
            acc += flash * np.array([255.0, 255.0, 255.0], np.float32)
        return np.clip(acc, 0.0, 255.0).astype(np.uint8)
