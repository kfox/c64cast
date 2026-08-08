"""Tileable 2D value noise, summed over octaves — the shared turbulence
primitive behind `fire` and `soap`."""

from __future__ import annotations

import cv2
import numpy as np


def periodic_value_noise(
    rng: np.random.Generator, rows: int, w: int, octaves: list[tuple[int, int, float]]
) -> np.ndarray:
    """Value noise of shape (rows, w), tileable in BOTH axes, summed over
    `octaves` of (vertical_cells, horizontal_cells, amplitude). Tileability
    comes from duplicating the first row/column of each octave's random grid
    before bilinear upsampling, so the upsampled endpoints match — a fire
    texture can then scroll past `rows` and wrap with no visible seam. Returns
    float32 normalized to [0, 1]."""
    acc = np.zeros((rows, w), dtype=np.float32)
    for cy, cx, amp in octaves:
        g = rng.random((cy, cx), dtype=np.float32)
        g = np.vstack([g, g[:1]])  # wrap row
        g = np.hstack([g, g[:, :1]])  # wrap col
        up = cv2.resize(g, (w, rows), interpolation=cv2.INTER_LINEAR)
        acc += amp * up
    lo, hi = float(acc.min()), float(acc.max())
    return (acc - lo) / (hi - lo + 1e-6)
