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
from . import Overlay, ascii_to_screen

log = logging.getLogger(__name__)

SCREEN_W = SCREEN.W_CHARS
SCREEN_H = SCREEN.H_CHARS

VALID_CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


def corner_origin(corner: str, width: int, height: int = 1) -> tuple[int, int]:
    """Top-left (col, row) of a `width`×`height` cell block in the given corner."""
    if corner not in VALID_CORNERS:
        raise ValueError(f"corner must be one of {VALID_CORNERS}, got {corner!r}")
    row = 0 if "top" in corner else SCREEN_H - height
    col = SCREEN_W - width if "right" in corner else 0
    return col, row


def paint_corner_string(
    buffers: dict, corner: str, lines: list[str], fg_color: str, bg_color: str
) -> None:
    """Paint multi-line text into the scene's screen/color buffers at a corner.

    bg_color == "none" only writes the color cells under the text (leaves
    the scene's underlying chars visible). Any other color also stamps a
    space-filled char block so the text reads cleanly.

    Mutates buffers["screen"] and buffers["color"] in place."""
    if not lines:
        return
    width = max(len(s) for s in lines)
    height = len(lines)
    col, row = corner_origin(corner, width, height)

    fg = C64_COLORS.get(fg_color, C64_COLORS["white"])
    paint_chars = bg_color != "none"

    screen = buffers["screen"]
    color = buffers["color"]

    for i, text in enumerate(lines):
        encoded = np.frombuffer(ascii_to_screen(text.ljust(width)), dtype=np.uint8)
        y = row + i
        if y < 0 or y >= SCREEN_H:
            continue
        base = y * SCREEN_W + col
        if paint_chars:
            screen[base : base + width] = encoded
        color[base : base + width] = fg


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
        paint_corner_string(buffers, self.corner, self._last_strings, self.fg_color, self.bg_color)
