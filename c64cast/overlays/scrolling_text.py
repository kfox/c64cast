"""Scrolling text overlay.

Reserves one row of the 40-col PETSCII screen and scrolls a sequence of
ScrollMessage entries through it. Direct screen-code writes (no CV2
rasterizing) — characters are stable at integer cell positions, so
flicker is bounded by the cell-shift rate.

Paints into the scene's composed screen+color buffers (compose-based
overlay), so the scene + this overlay produce one upload per frame with
no flicker."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

from ..c64 import SCREEN
from ..palette import C64_COLORS, resolve_color
from . import (
    SC_SPACE,
    Overlay,
    ascii_to_screen,
    register,
)

log = logging.getLogger(__name__)

SCREEN_W = SCREEN.W_CHARS
SCREEN_H = SCREEN.H_CHARS


@dataclass
class ScrollMessage:
    """One message in a scrolling sequence.

    style:
      "scroll"  — slides in from the right, off to the left.
      "static"  — appears centered for the full duration, no scrolling.
    """

    text: str
    color: str = "white"
    style: str = "scroll"
    pre_delay_s: float = 0.5
    pause_time_s: float = 2.0

    def __post_init__(self):
        if self.style not in ("scroll", "static"):
            raise ValueError(
                f"ScrollMessage: style must be 'scroll' or 'static', got {self.style!r}"
            )


@register("scrolling_text")
class ScrollingTextOverlay(Overlay):
    REQUIRES_PETSCII = True
    SUPPORTS_BITMAP_TEXT = True  # scroller folds into hires/mhires too
    REQUIRES_AUDIO = False
    PAINTS_INTO_BUFFERS = True
    HELP = "One scrolling row of messages (per-row scroller)."
    PARAM_HELP = {
        "messages": "List of message strings to cycle through.",
        "row": "Screen row (0..24) to scroll along.",
        "speed_cells_per_s": "Scroll speed in character cells per second.",
        "bg_color": "Background color (C64 color name).",
    }

    def __init__(
        self, messages: list, row: int = 24, speed_cells_per_s: float = 6.0, bg_color: str = "black"
    ):
        if not messages:
            raise ValueError("scrolling_text: messages must be non-empty")
        if not (0 <= row < SCREEN_H):
            raise ValueError(f"scrolling_text: row must be 0..{SCREEN_H - 1}")
        self.row = row
        self.speed = float(speed_cells_per_s)
        self.bg_color = resolve_color(bg_color, default=C64_COLORS["black"])
        # Accept either ScrollMessage instances or dicts (TOML inline tables).
        self.messages: list[ScrollMessage] = []
        for m in messages:
            if isinstance(m, ScrollMessage):
                self.messages.append(m)
            elif isinstance(m, dict):
                self.messages.append(ScrollMessage(**m))
            else:
                raise ValueError(f"scrolling_text: bad message {m!r}")
        # Pre-resolve color indices and pre-encode screen codes.
        self._encoded: list[tuple[bytes, int]] = []
        for m in self.messages:
            color_idx = resolve_color(m.color, default=C64_COLORS["white"])
            self._encoded.append((ascii_to_screen(m.text), color_idx))
        # Per-message durations: scroll-in + pause + scroll-out, padded by
        # pre_delay so consecutive messages don't crowd each other.
        self._durations = [self._duration_for(m) for m in self.messages]
        self.start_time = 0.0

    def _duration_for(self, m: ScrollMessage) -> float:
        if m.style == "static":
            return m.pre_delay_s + m.pause_time_s
        # text travels (text_len + SCREEN_W) cells total at self.speed
        travel_cells = len(m.text) + SCREEN_W
        scroll_t = travel_cells / max(self.speed, 0.1)
        return m.pre_delay_s + scroll_t

    def setup(self, api, scene):
        self.start_time = time.time()

    # ---- frame computation ---------------------------------------------------

    def _active_message(self, elapsed: float):
        """Walk the message list looking for which one is active now and how
        many seconds into it. Loops forever. Returns (msg, idx, local_t)."""
        total = sum(self._durations)
        if total <= 0:
            return self.messages[0], 0, 0.0
        t = elapsed % total
        acc = 0.0
        for i, d in enumerate(self._durations):
            if t < acc + d:
                return self.messages[i], i, t - acc
            acc += d
        return self.messages[-1], len(self.messages) - 1, 0.0

    def _row_for(self, msg: ScrollMessage, idx: int, local_t: float, width: int) -> np.ndarray:
        """Return a `width`-byte screen-code array for the message at local_t."""
        encoded, _ = self._encoded[idx]
        row = np.full(width, SC_SPACE, dtype=np.uint8)
        if local_t < msg.pre_delay_s:
            return row
        t = local_t - msg.pre_delay_s

        text_len = len(encoded)
        text = np.frombuffer(encoded, dtype=np.uint8)
        if msg.style == "static":
            x0 = (width - text_len) // 2
            self._paste(row, text, x0, width)
            return row

        # default: scroll right → left
        x = width - int(t * self.speed)
        self._paste(row, text, x, width)
        return row

    @staticmethod
    def _paste(row: np.ndarray, text: np.ndarray, x: int, width: int):
        """Write `text` into `row` starting at x, clipping to row bounds."""
        n = len(text)
        if x >= width or x + n <= 0:
            return
        src_start = max(0, -x)
        dst_start = max(0, x)
        copy_n = min(n - src_start, width - dst_start)
        if copy_n > 0:
            row[dst_start : dst_start + copy_n] = text[src_start : src_start + copy_n]

    def compose(self, buffers: dict, scene, t: float) -> None:
        surface = buffers["text"]
        width = surface.cols  # 40 char/hires, 20 mhires (double-wide)
        elapsed = t - self.start_time
        msg, idx, local_t = self._active_message(elapsed)
        row = self._row_for(msg, idx, local_t, width)
        _, color_idx = self._encoded[idx]
        # Color row: every non-space cell gets msg color, spaces get bg_color
        # so the row reads as a band of bg cells where the text isn't.
        colors = np.where(row != SC_SPACE, color_idx, self.bg_color).astype(np.int64)
        surface.paint_run(self.row, 0, row, colors, self.bg_color)
