"""The `epicycle` generative source: Fourier epicycles: a chain of circles, each spinning around the tip of the previous, whose combined tip traces `sum_i r_i * exp(j*(w_i t + phi_i))` — a chain of rotations composes to the same vector sum regardless of framing, so this sums phasors directly rather than nesting rotations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import GEN_HEIGHT, GEN_WIDTH, GenerativeSource, register

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation


@register("epicycle")
class EpicycleSource(GenerativeSource):
    """Fourier epicycles: a chain of circles, each spinning around the tip of
    the previous, whose combined tip traces `sum_i r_i * exp(j*(w_i t +
    phi_i))` — a chain of rotations composes to the same vector sum regardless
    of framing, so this sums phasors directly rather than nesting rotations.
    Radii follow an odd-harmonic series (`r_i = r0/(2i+1)`, alternating spin
    direction) — the classic square-wave epicycle construction. Renders the
    current arm chain (circle + spoke per arm) plus a fading trail of the
    tip's recent path, drawn as several trailing echoes since `render` is a
    pure function of `t`, not stateful accumulation.

    Reactive: each of the first three arms' angular speed is retuned to track
    a SID voice's live pitch (`voice_freqs`) instead of its fixed harmonic, so
    the chain's shape visibly follows the tune; `level` scales every arm's
    radius (louder → bigger sweep); a transient briefly flashes the whole
    frame brighter."""

    LIVE_PARAMS = {"speed": (0.0, 2.0)}

    _N_ARMS = 5
    _N_TRAIL = 24
    _TRAIL_LAG = 0.04
    _FREQ_TO_W_GAIN = 0.015  # rad/s of arm speed per Hz of voice pitch
    _LEVEL_RADIUS_GAIN = 0.6
    _ONSET_FLASH_GAIN = 90.0  # max per-channel brightness add on a full onset

    # Radii taper geometrically (`r0 * _RADIUS_RATIO**i`) rather than the
    # stricter harmonic `1/(2i+1)` series: the harmonic decay makes every arm
    # past the first collapse into an illegible cluster at this arm count,
    # while a gentler taper keeps each ring visually distinct (a spirograph
    # look rather than a literal square-wave Fourier reconstruction).
    _RADIUS_RATIO = 0.55

    def __init__(self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, speed: float = 0.6):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        n = self._N_ARMS
        self._w = np.arange(1, n + 1, dtype=np.float64)  # 1, 2, 3, 4, 5
        self._sign = np.array([1.0 if i % 2 == 0 else -1.0 for i in range(n)])
        r0 = min(width, height) * 0.32
        self._radius = r0 * (self._RADIUS_RATIO ** np.arange(n))
        self._colors = [
            self._hsv_to_bgr(np.full((1, 1), i / n, np.float32))[0, 0].tolist() for i in range(n)
        ]

    def _chain(self, t: float, w: np.ndarray, radius_scale: float) -> tuple[np.ndarray, np.ndarray]:
        """Cumulative arm-tip positions (one per arm, chain order)."""
        angles = self.speed * t * w * self._sign
        r = self._radius * radius_scale
        dx = r * np.cos(angles)
        dy = r * np.sin(angles)
        cx = self.width / 2.0 + np.cumsum(dx)
        cy = self.height / 2.0 + np.cumsum(dy)
        return cx, cy

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        w = self._w
        radius_scale = 1.0
        if modulation is not None:
            w = self._w.copy()
            for i, freq in enumerate(modulation.voice_freqs):
                if i < len(w) and freq > 0.0:
                    w[i] = self._w[i] + freq * self._FREQ_TO_W_GAIN
            radius_scale = 1.0 + self._LEVEL_RADIUS_GAIN * modulation.level

        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        cx, cy = self._chain(t, w, radius_scale)
        px, py = self.width / 2.0, self.height / 2.0
        for i in range(self._N_ARMS):
            color = self._colors[i]
            r = max(int(round(self._radius[i] * radius_scale)), 1)
            p0 = (int(round(px)), int(round(py)))
            p1 = (int(round(cx[i])), int(round(cy[i])))
            cv2.circle(frame, p0, r, color, 1, cv2.LINE_AA)
            cv2.line(frame, p0, p1, color, 1, cv2.LINE_AA)
            px, py = cx[i], cy[i]
        for e in range(self._N_TRAIL):
            te = t - e * self._TRAIL_LAG
            tcx, tcy = self._chain(te, w, radius_scale)
            fade = 1.0 - e / self._N_TRAIL
            trail_color = (int(255 * fade),) * 3
            cv2.circle(
                frame, (int(round(tcx[-1])), int(round(tcy[-1]))), 2, trail_color, -1, cv2.LINE_AA
            )
        if modulation is not None and modulation.onset > 0.0:
            flash = int(self._ONSET_FLASH_GAIN * modulation.onset)
            frame = cv2.add(frame, np.full_like(frame, flash))
        return frame
