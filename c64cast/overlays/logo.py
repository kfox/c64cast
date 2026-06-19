"""Logo overlay — paint a multi-line PETSCII art block in a screen corner
(or at an explicit row/col).

The art file is plain ASCII; each line becomes a row, each character
becomes a screen code via the standard ASCII → screen-code conversion.
Trailing whitespace is stripped per line, blank leading/trailing rows
are dropped, but interior spaces are preserved so multi-column art
aligns the way the author drew it. Lines wider than 40 chars are
truncated (with a warning) so weird files don't silently corrupt the
display.
"""

from __future__ import annotations

import logging
import os

import numpy as np

from ..c64 import SCREEN
from ..palette import C64_COLORS
from ..text_surface import corner_origin as _surface_corner_origin
from . import Overlay, ascii_to_screen, register
from .corner_text import VALID_CORNERS, corner_origin

log = logging.getLogger(__name__)

SCREEN_W = SCREEN.W_CHARS
SCREEN_H = SCREEN.H_CHARS


def _placeholder_art(missing_path: str) -> list[str]:
    """Friendly stand-in for a missing logo file.

    Renders a small bordered block that names the path the user needs
    to populate. Keeps the example config functional out of the box."""
    label = os.path.basename(missing_path) or "yours.txt"
    # Keep total width under 26 cols so the block fits in any corner.
    label = label[:24]
    inner_w = max(len(label), len("LOGO PLACEHOLDER"))
    top = "+" + "-" * (inner_w + 2) + "+"
    body1 = "| " + "LOGO PLACEHOLDER".ljust(inner_w) + " |"
    body2 = "| " + ("DROP " + label).ljust(inner_w) + " |"
    body3 = "| " + "IN assets/logos/".ljust(inner_w) + " |"
    return [top, body1, body2, body3, top]


def _load_art(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read().rstrip("\n")
    lines = raw.split("\n")
    # Trim blank rows top + bottom; preserve internal spacing.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ValueError("logo: file is empty after stripping blank rows")
    # Pad each line to the max width found, truncate cells past 40 cols.
    out = []
    for ln in lines:
        if len(ln) > SCREEN_W:
            log.warning("logo: line %d exceeds %d cols, truncating", len(out) + 1, SCREEN_W)
            ln = ln[:SCREEN_W]
        out.append(ln)
    width = max(len(ln) for ln in out)
    return [ln.ljust(width) for ln in out]


@register("logo")
class LogoOverlay(Overlay):
    REQUIRES_PETSCII = True
    # Art is just screen codes + a color, which the TextSurface folds into a
    # bitmap as readily as char RAM. On hires the 40-col layout maps 1:1; on
    # mhires the grid is 20 double-wide cols, so wide art clips — size the file
    # for the target mode (or use hires for full-width art).
    SUPPORTS_BITMAP_TEXT = True
    REQUIRES_AUDIO = False
    PAINTS_INTO_BUFFERS = True
    HELP = "Multi-line PETSCII art block loaded from a .txt file."
    PARAM_HELP = {
        "file": "Path to a .txt file of PETSCII art (one screen row per line).",
        "corner": "Corner to anchor the block (mutually exclusive with row/col).",
        "row": "Explicit top row (use with col instead of corner).",
        "col": "Explicit left column (use with row instead of corner).",
        "fg_color": "Art color (C64 color name).",
        "bg_color": "Background color, or 'none' to leave the scene showing through.",
    }

    def __init__(
        self,
        file: str,
        corner: str | None = None,
        row: int | None = None,
        col: int | None = None,
        fg_color: str = "white",
        bg_color: str = "black",
    ):
        # Missing file → render a friendly placeholder instead of crashing.
        # Lets the example config "just work" even before the user has
        # dropped their own art into assets/logos/.
        self._placeholder = not os.path.exists(file)
        if self._placeholder:
            log.warning("logo: file %r not found — using placeholder", file)
        if corner is None and (row is None or col is None):
            raise ValueError("logo: must specify either `corner` or both `row` + `col`")
        if corner is not None and corner not in VALID_CORNERS:
            raise ValueError(f"logo: corner must be one of {VALID_CORNERS}")
        self.file = file
        self.corner = corner
        self.row = row
        self.col = col
        self.fg = C64_COLORS.get(fg_color, C64_COLORS["white"])
        self.bg_name = bg_color
        self.bg = C64_COLORS.get(bg_color, C64_COLORS["black"])
        if self._placeholder:
            self.lines = _placeholder_art(file)
        else:
            self.lines = _load_art(file)
        # Limit total rows to the screen height.
        if len(self.lines) > SCREEN_H:
            log.warning("logo: %d rows exceeds %d; truncating", len(self.lines), SCREEN_H)
            self.lines = self.lines[:SCREEN_H]
        self._h = len(self.lines)
        self._w = max(len(ln) for ln in self.lines)
        if self.corner is not None:
            self._col, self._row = corner_origin(self.corner, self._w, self._h)
        else:
            assert self.row is not None and self.col is not None
            self._row = int(self.row)
            self._col = int(self.col)
        if self._row < 0 or self._row + self._h > SCREEN_H:
            raise ValueError(
                f"logo: rows {self._row}..{self._row + self._h - 1} don't fit in 0..{SCREEN_H - 1}"
            )
        if self._col < 0 or self._col + self._w > SCREEN_W:
            raise ValueError(
                f"logo: cols {self._col}..{self._col + self._w - 1} don't fit in 0..{SCREEN_W - 1}"
            )
        # Pre-encode once; logo is static so this never changes.
        self._encoded = [ascii_to_screen(ln) for ln in self.lines]

    def compose(self, buffers: dict, scene, t: float) -> None:
        surface = buffers["text"]
        # Recompute the corner anchor against the surface's actual grid (40-col
        # char/hires, 20-col mhires) so the block lands in the right corner on
        # every mode. Explicit row/col is used verbatim (clipped if off-grid).
        if self.corner is not None:
            col, row = _surface_corner_origin(
                self.corner, self._w, self._h, surface.cols, surface.rows
            )
        else:
            col, row = self._col, self._row
        for i, encoded in enumerate(self._encoded):
            row_chars = np.frombuffer(encoded, dtype=np.uint8)
            surface.paint_run(row + i, col, row_chars, self.fg, self.bg, draw_chars=True)
