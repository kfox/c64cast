"""Marquee — a single slow-scrolling line at the top of the screen.

Similar to scrolling_text but takes a single ``text`` string (not a
message list) and is intended for a continuous ticker. Subclasses (rss)
override `_current_text` to inject dynamic content.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from ..c64 import SCREEN
from ..palette import C64_COLORS, resolve_color
from . import SC_SPACE, Overlay, ascii_to_screen, register

log = logging.getLogger(__name__)

SCREEN_W = SCREEN.W_CHARS
SCREEN_H = SCREEN.H_CHARS


class MarqueeBase(Overlay):
    """Shared logic for marquee + rss. Subclass overrides _current_text()."""

    REQUIRES_PETSCII = True
    SUPPORTS_BITMAP_TEXT = True  # ticker folds into hires/mhires too
    REQUIRES_AUDIO = False
    PAINTS_INTO_BUFFERS = True
    SEPARATOR = "   *   "
    # Shared by marquee + rss (merged with each subclass's PARAM_HELP).
    PARAM_HELP = {
        "row": "Screen row (0..24) the ticker scrolls along.",
        "speed_cells_per_s": "Scroll speed in character cells per second.",
        "fg_color": "Text color (C64 color name).",
        "bg_color": "Background color (C64 color name).",
    }

    def __init__(
        self,
        row: int = 0,
        speed_cells_per_s: float = 3.0,
        fg_color: str = "yellow",
        bg_color: str = "black",
    ):
        if not (0 <= row < SCREEN_H):
            raise ValueError(f"row must be 0..{SCREEN_H - 1}")
        self.row = row
        self.speed = float(speed_cells_per_s)
        self.fg = resolve_color(fg_color, default=C64_COLORS["yellow"])
        self.bg = resolve_color(bg_color, default=C64_COLORS["black"])
        self.start_time = 0.0

    def _current_text(self) -> str:
        """Override to return what the ticker should show now."""
        raise NotImplementedError

    def setup(self, api, scene):
        self.start_time = time.time()

    def compose(self, buffers: dict, scene, t: float) -> None:
        surface = buffers["text"]
        width = surface.cols  # 40 char/hires, 20 mhires (double-wide)
        text = self._current_text() or " "
        # Loop the text seamlessly: repeat with a separator so there's never
        # a long blank gap.
        looped = text + self.SEPARATOR
        encoded = np.frombuffer(ascii_to_screen(looped), dtype=np.uint8)
        n = encoded.size
        if n == 0:
            return
        elapsed = t - self.start_time
        # Pixel-stable integer offset: each cell takes 1/speed seconds.
        offset = int(elapsed * self.speed) % n
        if offset + width <= n:
            row = encoded[offset : offset + width]
        else:
            tail = n - offset
            row = np.concatenate([encoded[offset:], encoded[: width - tail]])
        if row.size < width:
            pad = np.full(width - row.size, SC_SPACE, dtype=np.uint8)
            row = np.concatenate([row, pad])
        # Per-cell FG = text color on glyphs, bg color on the gaps (so the
        # ticker reads as a band). bg also fills the "off" pixels on bitmap.
        colors = np.where(row != SC_SPACE, self.fg, self.bg).astype(np.int64)
        surface.paint_run(self.row, 0, row, colors, self.bg)


@register("marquee")
class MarqueeOverlay(MarqueeBase):
    HELP = "Single-line continuous ticker scrolling one text string with a separator."
    PARAM_HELP = {"text": "The message to scroll continuously."}

    def __init__(
        self,
        text: str = "C64CAST",
        row: int = 0,
        speed_cells_per_s: float = 3.0,
        fg_color: str = "yellow",
        bg_color: str = "black",
    ):
        super().__init__(
            row=row, speed_cells_per_s=speed_cells_per_s, fg_color=fg_color, bg_color=bg_color
        )
        if not text:
            raise ValueError("marquee: text must be non-empty")
        self.text = str(text)

    def _current_text(self) -> str:
        return self.text
