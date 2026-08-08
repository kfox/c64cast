"""PETSCII spectrum-analyzer overlay.

Reads N log-spaced band magnitudes from the shared spectrum source
([_spectrum.py](_spectrum.py) — the scene's music features first, an FFT over
recent audio samples as a fallback) and paints colored "bars" into the 40×25
char grid. Each band occupies 5 columns. Vertical extent of each bar tracks
band energy.

Placement modes:
  bottom — bars rise from row 24 upward (classic VU meter look).
  center — bars expand from row 12 both up and down (mirrored).
  split  — bars descend from row 0 AND rise from row 24
           (meet in the middle when loud).
"""

from __future__ import annotations

import logging

import numpy as np

from c64cast.hw.c64 import SCREEN

from . import SC_FULL, Overlay, register
from ._spectrum import BAND_COLORS, N_BANDS, _SpectrumBands

log = logging.getLogger(__name__)

SCREEN_W = SCREEN.W_CHARS
SCREEN_H = SCREEN.H_CHARS

COLS_PER_BAND = SCREEN_W // N_BANDS  # 5


@register("spectrum_petscii")
class PetsciiSpectrumOverlay(_SpectrumBands, Overlay):
    REQUIRES_PETSCII = True
    WANTS_AUDIO = True
    PAINTS_INTO_BUFFERS = True
    HELP = "Audio spectrum rendered as vertical color bars in screen RAM."
    PARAM_HELP = {
        "placement": "Where the bars sit: 'bottom', 'center', or 'split'.",
        "height_rows": "Height of the bar strip in character rows.",
        "gain": "Multiplier applied to band magnitudes before bar height.",
    }

    def __init__(
        self, audio=None, placement: str = "center", height_rows: int = 12, gain: float = 1.0
    ):
        if placement not in ("bottom", "center", "split"):
            raise ValueError(
                f"spectrum_petscii: placement must be bottom|center|split, got {placement!r}"
            )
        if not (1 <= height_rows <= SCREEN_H):
            raise ValueError(f"spectrum_petscii: height_rows must be 1..{SCREEN_H}")
        self.audio = audio
        self.placement = placement
        self.height_rows = int(height_rows)
        self.gain = float(gain)
        self.n_bands = N_BANDS
        self._init_bands()
        # Strip rows we ever touch — used by compose to scope buffer writes.
        self._strip_rows = self._compute_strip_rows()

    def _compute_strip_rows(self) -> range:
        if self.placement == "bottom":
            top = SCREEN_H - self.height_rows
            return range(top, SCREEN_H)
        if self.placement == "center":
            half = self.height_rows
            top = max(0, (SCREEN_H // 2) - half)
            bot = min(SCREEN_H, (SCREEN_H // 2) + half)
            return range(top, bot)
        # split
        # Top strip 0..height_rows, bottom strip SCREEN_H-height_rows..SCREEN_H.
        # We expose a single range covering both for the write region;
        # placement of cells handled in render below.
        return range(0, SCREEN_H)

    def _bar_lengths(self, mags: np.ndarray) -> np.ndarray:
        """Map band magnitudes to integer bar lengths in [0, height_rows]."""
        # Heuristic mapping: clip at 1.0 after log compression, scale to rows.
        scaled = np.clip(mags, 0, 1.0)
        return (scaled * self.height_rows + 0.5).astype(np.int32)

    # ---- per-frame paint ----------------------------------------------------

    def compose(self, buffers: dict, scene, t: float) -> None:
        lengths = self._bar_lengths(self.bands_now(scene))

        screen = buffers["screen"]
        color = buffers["color"]
        # Paint bars on top of the scene's video without blanking the gaps —
        # quiet bands leave the underlying video visible between bars.
        for b in range(N_BANDS):
            ln = int(lengths[b])
            if ln <= 0:
                continue
            band_color = int(BAND_COLORS[b])
            x_start = b * COLS_PER_BAND
            x_end = x_start + COLS_PER_BAND
            self._paint_band(screen, color, x_start, x_end, ln, band_color)

    def _paint_band(self, chars, colors, x_start, x_end, ln, color):
        if self.placement == "bottom":
            top = SCREEN_H - ln
            bot = SCREEN_H
            self._fill_rect(chars, colors, x_start, x_end, top, bot, color)
        elif self.placement == "center":
            mid = SCREEN_H // 2
            half = max(1, ln // 2)
            self._fill_rect(chars, colors, x_start, x_end, mid - half, mid + half, color)
        elif self.placement == "split":
            # From top down to `ln`, AND from bottom up `ln` rows. When loud,
            # they meet in the middle. Each side gets half the magnitude.
            half = max(1, ln // 2)
            self._fill_rect(chars, colors, x_start, x_end, 0, half, color)
            self._fill_rect(chars, colors, x_start, x_end, SCREEN_H - half, SCREEN_H, color)

    @staticmethod
    def _fill_rect(chars, colors, x0, x1, y0, y1, color):
        for y in range(y0, y1):
            base = y * SCREEN_W
            chars[base + x0 : base + x1] = SC_FULL
            colors[base + x0 : base + x1] = color
