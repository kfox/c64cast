"""Interstitial scene: a centered "UP NEXT" splash between scenes.

Renders two centered text lines ("UP NEXT:" then the upcoming scene
name, separated by a blank row) on top of an animated parallax
background (see backgrounds.py).
"""

from __future__ import annotations

import logging
import random
import time

import numpy as np

from .backend import C64Backend
from .backgrounds import build as build_background
from .c64 import RegionID
from .config import InterstitialCfg
from .overlays import ascii_to_screen
from .palette import C64_COLORS
from .scenes import Scene

log = logging.getLogger(__name__)

RAINBOW_COLORS = [
    C64_COLORS["yellow"],
    C64_COLORS["light red"],
    C64_COLORS["light green"],
    C64_COLORS["light blue"],
    C64_COLORS["cyan"],
    C64_COLORS["purple"],
    C64_COLORS["white"],
    C64_COLORS["orange"],
]

# Solid C64 palette colors that read well on a black background. Used when
# text_color is "random". Skips dark gray, brown, dark blue.
LEGIBLE_COLORS = [
    C64_COLORS["white"],
    C64_COLORS["yellow"],
    C64_COLORS["cyan"],
    C64_COLORS["light green"],
    C64_COLORS["light blue"],
    C64_COLORS["orange"],
    C64_COLORS["light red"],
    C64_COLORS["purple"],
    C64_COLORS["light gray"],
]

MAX_WIDTH = 40
MAX_HEIGHT = 25

LABEL = "UP NEXT:"


def _resolve_line_colors(text_color: str, n_lines: int) -> list[int]:
    """Pick a C64 color index for each text line."""
    if text_color == "rainbow":
        return [RAINBOW_COLORS[i % len(RAINBOW_COLORS)] for i in range(n_lines)]
    if text_color == "random":
        c = random.choice(LEGIBLE_COLORS)
        return [c] * n_lines
    if text_color not in C64_COLORS:
        log.warning("unknown text_color %r — using white", text_color)
        return [C64_COLORS["white"]] * n_lines
    return [C64_COLORS[text_color]] * n_lines


class InterstitialScene(Scene):
    """Centered two-line "UP NEXT: <scene>" splash."""

    def __init__(self, api: C64Backend, next_scene_name: str, cfg: InterstitialCfg | None = None):
        super().__init__(api, None, None, "Interstitial")  # type: ignore[arg-type]
        self.cfg = cfg or InterstitialCfg()
        self.next_scene_name = next_scene_name
        self.duration_s = self.cfg.duration_s
        self.start_time = 0.0
        # Set in setup().
        self.lines: list[str] = []
        self.line_rows: list[int] = []
        self.line_cols: list[int] = []
        self.line_colors: list[int] = []
        self.bg = None

    def setup(self):
        self.is_done = False
        self.start_time = time.time()
        log.info(
            "interstitial: UP NEXT %r (bg=%s color=%s, %.1fs)",
            self.next_scene_name,
            self.cfg.background,
            self.cfg.text_color,
            self.duration_s,
        )
        # Mode switch — drop the dirty cache or we may suppress a needed
        # frame-0 write that happens to look identical to the last scene.
        self.api.invalidate_cache()
        # Standard PETSCII char mode, black border/bg.
        self.api.write_memory("d018", "14")
        self.api.write_memory("d016", "08")
        self.api.write_memory("d011", "1b")
        self.api.write_regs("d020", 0x00, 0x00)

        name = self.next_scene_name.upper()[:MAX_WIDTH]
        self.lines = [LABEL, name]
        # Vertically center a 3-row block (label, blank, name).
        top = max(0, (MAX_HEIGHT - 3) // 2)
        self.line_rows = [top, top + 2]
        self.line_cols = [max(0, (MAX_WIDTH - len(text)) // 2) for text in self.lines]
        self.line_colors = _resolve_line_colors(self.cfg.text_color, len(self.lines))

        self.bg = build_background(self.cfg.background)

    def process_frame(self, current_time: float) -> bool:
        elapsed = current_time - self.start_time
        if elapsed >= self.duration_s:
            return False

        # The background fills the strips above and below the text block.
        # Pass the rows occupied by the text so it doesn't paint over them.
        text_top = self.line_rows[0]
        text_bot = self.line_rows[-1] + 1
        top_rows = range(0, text_top)
        bot_rows = range(text_bot, MAX_HEIGHT)

        assert self.bg is not None
        chars, colors = self.bg.render(elapsed, top_rows, bot_rows, bg_color=0)

        for text, row, col, color in zip(
            self.lines, self.line_rows, self.line_cols, self.line_colors, strict=True
        ):
            encoded = ascii_to_screen(text)
            base = row * MAX_WIDTH + col
            chars[base : base + len(encoded)] = np.frombuffer(encoded, dtype=np.uint8)
            colors[base : base + len(encoded)] = color

        self.api.write_region(0x0400, chars.tobytes(), region_id=RegionID.SCREEN)
        self.api.write_region(0xD800, colors.tobytes(), region_id=RegionID.COLOR)
        return True

    def teardown(self):
        # Nothing to release — no audio, no source.
        pass


def default_factory(api: C64Backend, cfg: InterstitialCfg | None = None):
    """Returns a callable that the Playlist uses to mint a fresh
    InterstitialScene with the next scene's name baked in."""
    cfg = cfg or InterstitialCfg()

    def make(next_scene_name: str) -> InterstitialScene:
        return InterstitialScene(api, next_scene_name, cfg)

    return make
