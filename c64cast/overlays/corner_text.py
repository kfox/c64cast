"""Shared helpers for overlays that render short text strings in a screen
corner — clock, weather, callsign, countdown, network.

Each subclass implements ``compute_strings(t)``; the base handles corner
positioning, refresh throttling of the value source, and per-frame
composition into the scene's screen/color buffers. compute_strings() is
called at most every ``refresh_s`` seconds; the last result is composited
into the buffers every frame so the overlay survives the scene's
per-frame full-screen repaint.
"""

from __future__ import annotations

import logging

import numpy as np

from ..c64 import SCREEN
from ..palette import C64_COLORS
from ..text_surface import TextSurface
from ..text_surface import corner_origin as _surface_corner_origin
from . import Overlay, ascii_to_screen

log = logging.getLogger(__name__)

SCREEN_W = SCREEN.W_CHARS
SCREEN_H = SCREEN.H_CHARS

VALID_CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


def corner_origin(corner: str, width: int, height: int = 1) -> tuple[int, int]:
    """Top-left (col, row) of a `width`×`height` cell block in a 40×25 char
    grid (used by the char-only `logo` overlay). Mode-aware corner placement
    for the text overlays goes through text_surface.corner_origin."""
    if corner not in VALID_CORNERS:
        raise ValueError(f"corner must be one of {VALID_CORNERS}, got {corner!r}")
    row = 0 if "top" in corner else SCREEN_H - height
    col = SCREEN_W - width if "right" in corner else 0
    return col, row


def paint_corner_string(
    surface: TextSurface, corner: str, lines: list[str], fg_color: str, bg_color: str
) -> None:
    """Paint multi-line text into a TextSurface at a corner.

    Works on any display mode: the surface translates the cell-grid run into
    char screen codes or folded bitmap glyphs, and reports its own cols/rows
    (40×25 char/hires, 20×25 mhires) so corner placement adapts.

    bg_color == "none" leaves the underlying glyph (char modes recolor only;
    bitmap modes have no underlying glyph so they draw opaque on black). Any
    other color stamps an opaque text box."""
    if not lines:
        return
    width = max(len(s) for s in lines)
    height = len(lines)
    col, row = _surface_corner_origin(corner, width, height, surface.cols, surface.rows)

    fg = C64_COLORS.get(fg_color, C64_COLORS["white"])
    draw_chars = bg_color != "none"
    bg = C64_COLORS.get(bg_color, 0)  # 0 (black) when "none"

    for i, text in enumerate(lines):
        encoded = np.frombuffer(ascii_to_screen(text.ljust(width)), dtype=np.uint8)
        surface.paint_run(row + i, col, encoded, fg, bg, draw_chars=draw_chars)


class CornerTextOverlay(Overlay):
    """Base for overlays that render a few lines of text in a screen corner.

    Subclasses override:
      compute_strings(t) → Optional[list[str]]  — return None to skip update.
    """

    REQUIRES_PETSCII = True
    REQUIRES_AUDIO = False
    PAINTS_INTO_BUFFERS = True
    # Shared by clock/weather/callsign/countdown/network — the introspection
    # layer merges this with each subclass's own PARAM_HELP.
    PARAM_HELP = {
        "corner": "Screen corner to anchor the text (top-left/top-right/bottom-left/bottom-right).",
        "fg_color": "Text color (C64 color name).",
        "bg_color": "Cell background color, or 'none' to leave the scene showing through.",
        "refresh_s": "Seconds between value recomputes (the text is repainted every frame).",
    }

    def __init__(
        self,
        corner: str = "top-right",
        fg_color: str = "white",
        bg_color: str = "black",
        refresh_s: float = 1.0,
    ):
        if corner not in VALID_CORNERS:
            raise ValueError(f"corner must be one of {VALID_CORNERS}, got {corner!r}")
        self.corner = corner
        self.fg_color = fg_color
        self.bg_color = bg_color
        self.refresh_s = float(refresh_s)
        self._last_strings: list[str] = []
        self._last_compute_t = -1e9  # ensure first frame computes

    def compute_strings(self, t: float) -> list[str] | None:
        raise NotImplementedError

    def compose(self, buffers: dict, scene, t: float) -> None:
        # Throttle the (potentially expensive) compute_strings call, but
        # paint the cached strings into the buffers EVERY frame — the
        # scene rewrote the cells under us when it composed its own frame.
        if (t - self._last_compute_t) >= self.refresh_s:
            new_strings = self.compute_strings(t)
            if new_strings is not None:
                self._last_strings = new_strings
            self._last_compute_t = t
        if not self._last_strings:
            return
        paint_corner_string(
            buffers["text"], self.corner, self._last_strings, self.fg_color, self.bg_color
        )
