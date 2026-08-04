"""Native multicolor-bitmap spectrum-analyzer overlay.

The bitmap counterpart to `spectrum_petscii`. Where that one fills character
cells in a 40×25 grid, this paints directly into the mhires 160×200 multicolor
bitmap, so a bar's height is a *pixel* — 200 levels instead of 25.

Restricted to `mhires` (`COMPATIBLE_MODES`) because it is written against that
mode's exact buffer set: an 8000-byte 2bpp bitmap, a 1000-byte screen matrix
(c1 = high nibble, c2 = low nibble) and 1000 bytes of color RAM (c3), with %00
falling through to the global bg0. See modes.MHiresComposeBuffers.

**Which color slot a bar owns.** MCBM gives each 4×8 hardware cell four colors:
bg0 (global), c1 and c2 (the two screen nibbles) and c3 (color RAM). A bar wants
one solid color per band, and the frame underneath is already using all four. c3
is the slot to take: it is a whole byte per cell that nothing else in the cell
depends on, so setting a bar cell's pixels to %11 and writing the band color to
that cell's color RAM leaves the screen nibbles — and therefore the frame's own
c1/c2 pixels — completely alone. `MHiresTextSurface` makes the opposite choice
(it reserves c1+c2 for an opaque text box and leaves c3 to the frame) because
text needs two colors per cell; a bar needs one.

**Bar tops are sub-cell.** A bar's top edge lands wherever the energy puts it,
including mid-cell — that's the resolution this overlay exists for. The cost is
confined to the single 4×8 cell at each bar's tip: that cell's c3 becomes the
band color, so any *frame* pixel in the exposed part of that one cell that was
using the c3 slot is recolored. Frame pixels on bg0/c1/c2 are untouched, as is
every cell the bar doesn't reach. Snapping tops to the 8px cell boundary would
remove even that, at the price of throwing away the vertical resolution that is
the whole point — so it isn't the default.

Gaps are never blanked: only cells a bar actually fills are written, so the
video shows through between and above the bars.
"""

from __future__ import annotations

import logging

import numpy as np

from ..c64 import SCREEN
from . import Overlay, register
from ._spectrum import BAND_COLORS, N_BANDS, _SpectrumBands

log = logging.getLogger(__name__)

CELL_PX = 8  # scanlines per hardware cell
HW_COLS = SCREEN.W_CHARS  # 40 hardware cells across
HW_ROWS = SCREEN.H_CHARS  # 25
BITMAP_H = HW_ROWS * CELL_PX  # 200 scanlines

# All four pixels of a byte set to %11 — the color-RAM (c3) slot.
_C3_SOLID = np.uint8(0xFF)

CELLS_PER_BAND = HW_COLS // N_BANDS  # 5 hardware cells = 20 mhires px
# One cell of the band is left unpainted so neighboring bars don't fuse into a
# solid block along the bottom of the screen. 4 cells = 16 px of bar, 4 px gap.
GUTTER_CELLS = 1
BAR_CELLS = CELLS_PER_BAND - GUTTER_CELLS


@register("spectrum_bitmap")
class BitmapSpectrumOverlay(_SpectrumBands, Overlay):
    COMPATIBLE_MODES = ("mhires",)
    WANTS_AUDIO = True
    PAINTS_INTO_BUFFERS = True
    HELP = "Audio spectrum as pixel-resolution bars painted into the mhires bitmap."
    PARAM_HELP = {
        "placement": "Where the bars sit: 'bottom', 'center', or 'split'.",
        "height_frac": "Fraction of screen height a full-energy bar reaches.",
        "gain": "Multiplier applied to band magnitudes before bar height.",
    }

    def __init__(
        self,
        audio=None,
        placement: str = "bottom",
        height_frac: float = 0.5,
        gain: float = 1.0,
    ):
        if placement not in ("bottom", "center", "split"):
            raise ValueError(
                f"spectrum_bitmap: placement must be bottom|center|split, got {placement!r}"
            )
        if not (0.0 < height_frac <= 1.0):
            raise ValueError(f"spectrum_bitmap: height_frac must be in (0, 1], got {height_frac}")
        self.audio = audio
        self.placement = placement
        self.height_frac = float(height_frac)
        self.gain = float(gain)
        self.n_bands = N_BANDS
        self._init_bands()

    # ---- geometry -----------------------------------------------------------

    @property
    def height_px(self) -> int:
        """Scanlines a full-energy bar spans."""
        return max(1, int(BITMAP_H * self.height_frac))

    def _bar_heights(self, mags: np.ndarray) -> np.ndarray:
        """Band magnitudes → bar heights in scanlines, [0, height_px]."""
        return (np.clip(mags, 0.0, 1.0) * self.height_px + 0.5).astype(np.int32)

    def _spans(self, height: int) -> list[tuple[int, int]]:
        """The [y0, y1) scanline spans a bar of `height` px occupies, per the
        placement mode. `split` and `center` each get two halves, mirroring
        spectrum_petscii's behavior."""
        if height <= 0:
            return []
        if self.placement == "bottom":
            return [(BITMAP_H - height, BITMAP_H)]
        half = max(1, height // 2)
        if self.placement == "center":
            mid = BITMAP_H // 2
            return [(mid - half, mid + half)]
        # split — from the top edge down, and from the bottom edge up.
        return [(0, half), (BITMAP_H - half, BITMAP_H)]

    # ---- per-frame paint ----------------------------------------------------

    def compose(self, buffers: dict, scene, t: float) -> None:
        heights = self._bar_heights(self.bands_now(scene))
        if not heights.any():
            return  # silence — leave the frame byte-identical
        # Views, not copies: writes land in the arrays push() uploads.
        bitmap = buffers["bitmap"].reshape(HW_ROWS, HW_COLS, CELL_PX)
        color = buffers["color"].reshape(HW_ROWS, HW_COLS)
        for band in range(self.n_bands):
            height = int(heights[band])
            if height <= 0:
                continue
            x0 = band * CELLS_PER_BAND
            x1 = x0 + BAR_CELLS
            band_color = int(BAND_COLORS[band])
            for y0, y1 in self._spans(height):
                self._fill(bitmap, color, x0, x1, y0, y1, band_color)

    @staticmethod
    def _fill(
        bitmap: np.ndarray,
        color: np.ndarray,
        x0: int,
        x1: int,
        y0: int,
        y1: int,
        band_color: int,
    ) -> None:
        """Paint scanlines [y0, y1) of cell columns [x0, x1) solid `band_color`.

        Whole cell rows go in one slice; the partial cell row at each end gets
        only its covered scanlines, which is what makes bar tops sub-cell. Every
        touched cell's color RAM is claimed either way — c3 is per-cell, so a
        partially covered cell still hands its whole c3 slot to the bar."""
        y0 = max(0, y0)
        y1 = min(BITMAP_H, y1)
        if y1 <= y0:
            return
        first_row, last_row = y0 // CELL_PX, (y1 - 1) // CELL_PX
        if first_row == last_row:
            # One cell row, partially covered top and bottom.
            s0, s1 = y0 - first_row * CELL_PX, y1 - first_row * CELL_PX
            bitmap[first_row, x0:x1, s0:s1] = _C3_SOLID
            color[first_row, x0:x1] = band_color
            return
        # Leading partial row.
        s0 = y0 - first_row * CELL_PX
        if s0:
            bitmap[first_row, x0:x1, s0:] = _C3_SOLID
            color[first_row, x0:x1] = band_color
            first_row += 1
        # Trailing partial row.
        s1 = y1 - last_row * CELL_PX
        if s1 != CELL_PX:
            bitmap[last_row, x0:x1, :s1] = _C3_SOLID
            color[last_row, x0:x1] = band_color
            last_row -= 1
        # Fully covered rows in between.
        if first_row <= last_row:
            bitmap[first_row : last_row + 1, x0:x1, :] = _C3_SOLID
            color[first_row : last_row + 1, x0:x1] = band_color
