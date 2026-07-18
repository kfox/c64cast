"""Shared hires bitmap text rasterizer.

Renders ASCII text into a VIC-II hires bitmap (320×200, 8×8 cells) by copying
glyph bytes from the C64 character ROM. Extracted from voice_scope.py so the
oscilloscope's text rows and the on-C64 MenuOverlay paint glyphs the same way
(voice_scope keeps its own row painter; it now sources `load_glyphs` /
`ascii_to_screen_code` from here).

A hires cell is 8 bytes (one byte per scanline); consecutive cells in a row are
consecutive 8-byte chunks, and screen-RAM color bytes for a row are consecutive,
so a run of cells at a column offset is a single contiguous `write_region` for
both the bitmap and the color row — which is what `paint_text_row` relies on."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import numpy as np

from .c64 import SCREEN

if TYPE_CHECKING:
    from .backend import C64Backend

log = logging.getLogger("c64cast.bitmap_text")

CELL_PX = 8  # one screen cell = 8 px square = 8 bitmap bytes
BITMAP_CELL_ROW_BYTES = SCREEN.BITMAP_W  # 320 bytes span one cell-row
SCREEN_W_CHARS = SCREEN.W_CHARS  # 40
COLOR_NIBBLE_MASK = 0x0F

# Uppercase charset ROM. Matches the [preview] charset_path default + big_text's
# loader so all consumers expect the same 2 KB artifact.
_CHARGEN_PATH = "assets/roms/characters.901225-01.bin"
_GLYPHS_CACHE: bytes | None = None


def load_glyphs() -> bytes:
    """Load the 2 KB uppercase charset. Cached process-wide.

    Falls back to framebuffer._builtin_charset() (a cv2-rendered ASCII font) if
    the ROM file is missing — keeps tests + minimal installs working, at the
    cost of glyphs that don't quite look C64-native."""
    global _GLYPHS_CACHE
    if _GLYPHS_CACHE is not None:
        return _GLYPHS_CACHE
    if os.path.exists(_CHARGEN_PATH):
        with open(_CHARGEN_PATH, "rb") as f:
            data = f.read(2048)
        if len(data) >= 2048:
            _GLYPHS_CACHE = data
            return _GLYPHS_CACHE
        log.warning("bitmap_text: charset %s shorter than 2KB; using builtin", _CHARGEN_PATH)
    from .framebuffer import _builtin_charset

    _GLYPHS_CACHE = _builtin_charset()
    return _GLYPHS_CACHE


def ascii_to_screen_code(ch: str) -> int:
    """Map a single ASCII character to its C64 screen code (uppercase set).

    Letters A-Z → screen codes 0x01-0x1A; @ → 0x00; everything else passes
    through as ord(ch) & 0xFF (digits + most punctuation are identical between
    ASCII and screen codes). Chars the charset can't represent fall back to
    space (0x20) so unknown bytes render as a blank cell instead of a graphics
    glyph that would look like noise.

    Underscore is special-cased to screen code 0x6F (the low horizontal-line
    graphics glyph) rather than the raw-mapping 0x1F (← arrow): the C64 charset
    has no ASCII underscore, and 0x6F is the closest visual match, so identifier
    text like ``auto_fit_strength`` reads correctly."""
    if ch == "_":
        return 0x6F  # low horizontal line ≈ underscore (charset has no true '_')
    c = ord(ch.upper())
    if 0x40 <= c <= 0x5F:
        return (c - 0x40) & 0x3F  # @, A-Z, [\]^_
    if 0x20 <= c <= 0x3F:
        return c  # space, digits, !"#... ?
    return 0x20  # unknown → blank


def glyphs_to_mask(glyphs: bytes, text: str) -> np.ndarray:
    """Rasterize `text` into an ``(8, 8*len(text))`` uint8 mask — 1 where a
    glyph pixel is set (foreground), 0 elsewhere — using the uppercase charset.

    Each C64 glyph is 8 bytes (one per scanline, bit 7 = leftmost pixel); this
    unpacks them into a pixel grid the host-side overlays can scale + composite
    into a BGR frame (so pre-quantization text uses the real C64 font instead of
    a Hershey vector font). Unknown chars become blank cells (see
    :func:`ascii_to_screen_code`). Empty text yields an ``(8, 0)`` array."""
    n = len(text)
    if n == 0:
        return np.zeros((CELL_PX, 0), dtype=np.uint8)
    rows = np.empty((n, CELL_PX), dtype=np.uint8)
    for i, ch in enumerate(text):
        sc = ascii_to_screen_code(ch)
        rows[i] = np.frombuffer(glyphs[sc * CELL_PX : (sc + 1) * CELL_PX], dtype=np.uint8)
    # (n, 8) bytes -> (n, 8 rows, 8 cols) bits -> (8 rows, n*8 cols)
    bits = np.unpackbits(rows, axis=1).reshape(n, CELL_PX, CELL_PX)
    return bits.transpose(1, 0, 2).reshape(CELL_PX, n * CELL_PX)


def paint_text_row(
    api: C64Backend,
    glyphs: bytes,
    *,
    cell_row: int,
    text: str,
    fg: int,
    bitmap_base: int,
    screen_base: int,
    bitmap_region_id: int,
    screen_region_id: int,
    col: int = 0,
    bg: int = 0,
) -> None:
    """Paint `text` into a hires bitmap at (cell_row, col), FG/BG colors into the
    matching screen-RAM cells. Writes only the `len(text)` cells (a partial-row
    panel), as two contiguous region writes so the delta cache absorbs unchanged
    columns on re-paint. `col + len(text)` must fit within the 40-cell row."""
    n = len(text)
    if n == 0:
        return
    assert col >= 0 and col + n <= SCREEN_W_CHARS, f"text overruns row: col={col} len={n}"
    bitmap_bytes = bytearray(n * CELL_PX)
    for i, ch in enumerate(text):
        sc = ascii_to_screen_code(ch)
        bitmap_bytes[i * CELL_PX : (i + 1) * CELL_PX] = glyphs[sc * CELL_PX : (sc + 1) * CELL_PX]
    bitmap_addr = bitmap_base + cell_row * BITMAP_CELL_ROW_BYTES + col * CELL_PX
    api.write_region(bitmap_addr, bytes(bitmap_bytes), region_id=bitmap_region_id)
    color_byte = ((fg & COLOR_NIBBLE_MASK) << 4) | (bg & COLOR_NIBBLE_MASK)
    screen_addr = screen_base + cell_row * SCREEN_W_CHARS + col
    api.write_region(screen_addr, bytes([color_byte] * n), region_id=screen_region_id)
