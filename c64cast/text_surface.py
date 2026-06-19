"""Backend-neutral text surface for buffer-painting overlays.

A ``TextSurface`` is the cell-grid an overlay paints short text runs into.
The scene's ``compose()`` builds one over its frame buffers and stashes it as
``buffers["text"]``; overlays call ``paint_run`` and read ``cols``/``rows``,
oblivious to whether the underlying display is a character mode or a bitmap.

Three implementations:

* ``CharTextSurface`` — wraps a char mode's 40×25 screen-code + color-nibble
  arrays. ``paint_run`` writes screen codes + FG nibbles exactly as the
  overlays did before this abstraction existed, so char-mode output stays
  byte-identical.
* ``HiresTextSurface`` — folds glyphs straight into a 320×200 hires bitmap
  (one glyph per 8×8 cell) + the per-cell FG/BG screen nibble. 40×25 grid.
* ``MHiresTextSurface`` — folds double-wide ("chunky") glyphs into a 160×200
  multicolor bitmap. A standard 8×8 glyph spans 2 cells wide (8 mhires px), so
  the grid is 20 columns; each text cell reserves two of its four MCBM color
  slots (c1 = text bg, c2 = text fg) for an opaque, bg0-independent box. With
  ``double_height`` the glyph also spans two cell rows (16 px tall, 12-row
  grid) for read-across-the-room legibility.

The bitmap surfaces fold text into the *in-memory* buffer arrays before the
mode's ``push()`` uploads them, so overlay text rides the same path as the
frame — including the REU bank-swap double-buffer, which a post-hoc direct
writer can't reach.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from . import bitmap_text
from .c64 import SCREEN

_NIBBLE = 0x0F
CELL_PX = bitmap_text.CELL_PX  # 8
_HW_COLS = SCREEN.W_CHARS  # 40 hardware cells per row (both hires and mhires)
_HW_ROWS = SCREEN.H_CHARS  # 25


def _glyph_table() -> np.ndarray:
    """The 2 KB charset ROM as a (256, 8) uint8 table indexed by screen code.
    Cached process-wide by bitmap_text.load_glyphs()."""
    return np.frombuffer(bitmap_text.load_glyphs(), dtype=np.uint8).reshape(256, CELL_PX)


def corner_origin(corner: str, width: int, height: int, cols: int, rows: int) -> tuple[int, int]:
    """Top-left (col, row) of a ``width``×``height`` block in ``corner`` of a
    ``cols``×``rows`` grid. ``corner`` is one of top/bottom × left/right."""
    row = 0 if "top" in corner else rows - height
    col = cols - width if "right" in corner else 0
    return col, row


class TextSurface(Protocol):
    """Cell grid an overlay paints into. ``cols``/``rows`` are the usable text
    dimensions for the current display mode (40×25 char/hires, 20×25 or 20×12
    mhires)."""

    cols: int
    rows: int

    def paint_run(
        self,
        row: int,
        col: int,
        codes: np.ndarray,
        fg: int | np.ndarray,
        bg: int,
        *,
        draw_chars: bool = True,
    ) -> None:
        """Paint a horizontal run of cells at (row, col).

        ``codes`` is a 1-D array of C64 screen codes (one per cell). ``fg`` is
        the FG color index for glyph "on" pixels — a scalar or a per-cell
        array. ``bg`` is the cell background ("off" pixels). ``draw_chars``
        False means "recolor only, leave the underlying glyph" (honored by the
        char surface; bitmap surfaces have no underlying glyph and always
        rasterize). Out-of-grid cells are clipped."""
        ...


def _clip_run(
    row: int, col: int, codes: np.ndarray, fg: np.ndarray, cols: int, rows: int
) -> tuple[int, np.ndarray, np.ndarray] | None:
    """Clip a run to the [0, cols) × [0, rows) grid. Returns (col, codes, fg)
    trimmed to the visible span, or None if entirely off-grid."""
    if row < 0 or row >= rows:
        return None
    n = codes.shape[0]
    if n == 0 or col >= cols or col + n <= 0:
        return None
    left = max(0, -col)
    right = min(n, cols - col)
    if right <= left:
        return None
    return max(0, col), codes[left:right], fg[left:right]


def _as_fg_array(fg: int | np.ndarray, n: int) -> np.ndarray:
    return np.broadcast_to(np.asarray(fg, dtype=np.int64), (n,))


class CharTextSurface:
    """Text surface over a char mode's screen-code + color-nibble buffers.

    Writes match the pre-abstraction overlay behavior exactly: screen codes
    into the screen buffer, FG nibbles into the color buffer (the cell BG is
    the global $D021, so ``bg`` is unused here)."""

    cols = _HW_COLS
    rows = _HW_ROWS

    def __init__(self, screen: np.ndarray, color: np.ndarray) -> None:
        self._screen = screen
        self._color = color

    def paint_run(
        self,
        row: int,
        col: int,
        codes: np.ndarray,
        fg: int | np.ndarray,
        bg: int,
        *,
        draw_chars: bool = True,
    ) -> None:
        codes = np.asarray(codes, dtype=np.uint8)
        clipped = _clip_run(row, col, codes, _as_fg_array(fg, codes.shape[0]), self.cols, self.rows)
        if clipped is None:
            return
        c0, codes, fg_arr = clipped
        base = row * self.cols + c0
        n = codes.shape[0]
        if draw_chars:
            self._screen[base : base + n] = codes
        self._color[base : base + n] = fg_arr & _NIBBLE


class HiresTextSurface:
    """Text surface that folds glyphs into a 320×200 hires bitmap + per-cell
    FG/BG screen nibble. One 8×8 glyph per cell; 40×25 grid."""

    cols = _HW_COLS
    rows = _HW_ROWS

    def __init__(self, bitmap: np.ndarray, screen: np.ndarray) -> None:
        self._bitmap = bitmap  # flat (8000,) uint8, cell (r,c) at (r*40+c)*8
        self._screen = screen  # flat (1000,) uint8
        self._glyphs = _glyph_table()

    def paint_run(
        self,
        row: int,
        col: int,
        codes: np.ndarray,
        fg: int | np.ndarray,
        bg: int,
        *,
        draw_chars: bool = True,
    ) -> None:
        codes = np.asarray(codes, dtype=np.uint8)
        clipped = _clip_run(row, col, codes, _as_fg_array(fg, codes.shape[0]), self.cols, self.rows)
        if clipped is None:
            return
        c0, codes, fg_arr = clipped
        n = codes.shape[0]
        cell0 = row * self.cols + c0
        # Glyph bit pattern straight into the bitmap; cells in a row are
        # contiguous 8-byte chunks, so one slice assignment covers the run.
        block = self._glyphs[codes]  # (n, 8)
        self._bitmap[cell0 * CELL_PX : (cell0 + n) * CELL_PX] = block.reshape(-1)
        # Screen nibble: high = FG (on pixels), low = BG (off pixels).
        self._screen[cell0 : cell0 + n] = ((fg_arr & _NIBBLE) << 4) | (bg & _NIBBLE)


class MHiresTextSurface:
    """Text surface that folds double-wide glyphs into a 160×200 multicolor
    bitmap. Each glyph spans 2 hardware cells horizontally (20-col grid); the
    cell reserves c1 = text bg, c2 = text fg (opaque box, independent of bg0).
    ``double_height`` stretches the glyph to 16 px tall (2 cell rows, 12-row
    grid)."""

    def __init__(
        self,
        bitmap: np.ndarray,
        screen: np.ndarray,
        color: np.ndarray,
        *,
        double_height: bool = False,
    ) -> None:
        self._bitmap = bitmap  # flat (8000,) uint8
        self._screen = screen  # flat (1000,) uint8
        self._color = color  # flat (1000,) uint8
        self._glyphs = _glyph_table()
        self.double_height = double_height
        self.cols = _HW_COLS // 2  # 20 double-wide columns
        self.rows = _HW_ROWS // 2 if double_height else _HW_ROWS

    def paint_run(
        self,
        row: int,
        col: int,
        codes: np.ndarray,
        fg: int | np.ndarray,
        bg: int,
        *,
        draw_chars: bool = True,
    ) -> None:
        codes = np.asarray(codes, dtype=np.uint8)
        clipped = _clip_run(row, col, codes, _as_fg_array(fg, codes.shape[0]), self.cols, self.rows)
        if clipped is None:
            return
        c0, codes, fg_arr = clipped
        n = codes.shape[0]

        # Expand each glyph to per-pixel 2bpp codes: on -> %10 (c2 = fg),
        # off -> %01 (c1 = bg). bits[i, s, p]: glyph i, scanline s, pixel p
        # (p=0 is the leftmost/MSB). codes2 in {1, 2}.
        bits = np.unpackbits(self._glyphs[codes], axis=1).reshape(n, CELL_PX, CELL_PX)
        if self.double_height:
            bits = np.repeat(bits, 2, axis=1)  # (n, 16, 8) — glyph stretched 2x tall
        codes2 = bits.astype(np.uint8) + 1  # off->1, on->2
        n_scan = codes2.shape[1]  # 8 or 16

        # Pack 4 pixels -> one MCBM byte. Left cell = px 0..3, right = px 4..7.
        def _pack(px: np.ndarray) -> np.ndarray:  # px: (n, n_scan, 4) -> (n, n_scan)
            return (px[..., 0] << 6) | (px[..., 1] << 4) | (px[..., 2] << 2) | px[..., 3]

        left = _pack(codes2[..., 0:4])  # (n, n_scan)
        right = _pack(codes2[..., 4:8])
        cell_rows = n_scan // CELL_PX  # 1 normal, 2 double-height

        screen_byte = ((bg & _NIBBLE) << 4) | 0  # low nibble (c2=fg) filled per-cell below
        for cr in range(cell_rows):
            cell_row = row * (2 if self.double_height else 1) + cr
            if cell_row >= _HW_ROWS:
                break
            # 8 scanlines of this cell row, for left+right of every text cell.
            lcell = left[:, cr * CELL_PX : (cr + 1) * CELL_PX]  # (n, 8)
            rcell = right[:, cr * CELL_PX : (cr + 1) * CELL_PX]
            # Interleave left/right into hardware-cell order [l0, r0, l1, r1...].
            interleaved = np.stack([lcell, rcell], axis=1).reshape(2 * n, CELL_PX)  # (2n, 8)
            hw0 = cell_row * _HW_COLS + 2 * c0
            self._bitmap[hw0 * CELL_PX : (hw0 + 2 * n) * CELL_PX] = interleaved.reshape(-1)
            # screen nibble per hw cell: high = c1 (bg), low = c2 (fg); both
            # hardware cells of a text cell share its fg. color RAM (c3) unused.
            fg2 = np.repeat(fg_arr & _NIBBLE, 2).astype(np.uint8)  # (2n,)
            self._screen[hw0 : hw0 + 2 * n] = np.uint8(screen_byte) | fg2
            self._color[hw0 : hw0 + 2 * n] = 0
