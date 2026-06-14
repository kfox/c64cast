"""Parallax background styles for the interstitial scene.

Each style implements ``render(t, top_rows, bottom_rows)`` and returns a
(chars[1000], colors[1000]) pair sized for the full 40×25 PETSCII screen.
Only the indices inside ``top_rows`` and ``bottom_rows`` are populated;
the caller fills the middle text strip and the chosen border/bg color
covers the rest.

All chars are C64 *screen codes* (what gets written to $0400), not PETSCII
codes. The two differ above 0x40; below that the encodings are identical.
The constants below use values that look right with the default uppercase
character set (the one in ROM when the chip starts up).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import TypeVar

import numpy as np

from .palette import C64_COLORS

# Screen codes for commonly-useful glyphs. (Screen code, not PETSCII.)
SC_SPACE = 0x20
SC_DOT = 0x2E
SC_STAR = 0x2A
SC_PLUS = 0x2B
SC_HYPHEN = 0x2D
SC_GT = 0x3E
SC_TILDE = 0x27  # apostrophe; the closest stock screen code to a wave
SC_FULL = 0xA0  # reverse-space: solid filled block
SC_HBLOCK_BOT = 0x64  # lower-half block
SC_HBLOCK_TOP = 0x77  # upper-half block (approx)
SC_AT = 0x00  # @
SC_W = 0x17
SC_V = 0x16
SC_O = 0x0F

# Vibrant palette indices for the "colorful but legible" requirement.
# Avoids muddy browns/dark grays for the prominent bar styles.
VIBRANT_COLORS = [
    C64_COLORS["yellow"],
    C64_COLORS["cyan"],
    C64_COLORS["light green"],
    C64_COLORS["light blue"],
    C64_COLORS["purple"],
    C64_COLORS["light red"],
    C64_COLORS["orange"],
]

DEPTH_COLORS = [
    C64_COLORS["light gray"],
    C64_COLORS["gray"],
    C64_COLORS["dark gray"],
]


# ---------------------------------------------------------------------------
# Base + registry
# ---------------------------------------------------------------------------


class Background:
    name = "base"

    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        self.np_rng = np.random.default_rng(seed)

    def render(
        self, t: float, top_rows: range, bottom_rows: range, bg_color: int = 0
    ) -> tuple[np.ndarray, np.ndarray]:
        """Populate top_rows and bottom_rows of a 40×25 screen.

        Returns (chars, colors), both uint8 arrays of length 1000.
        Indices outside top_rows + bottom_rows are filled with SC_SPACE/
        bg_color so the caller can overlay text without merging cells.
        """
        chars = np.full(1000, SC_SPACE, dtype=np.uint8)
        colors = np.full(1000, bg_color, dtype=np.uint8)
        self._fill(chars, colors, t, top_rows, bg_color)
        self._fill(chars, colors, t, bottom_rows, bg_color)
        return chars, colors

    def _fill(self, chars: np.ndarray, colors: np.ndarray, t: float, rows: range, bg_color: int):
        raise NotImplementedError


REGISTRY: dict[str, type[Background]] = {}

_BgT = TypeVar("_BgT", bound=Background)


def register(name: str) -> Callable[[type[_BgT]], type[_BgT]]:
    """Class decorator that registers a Background under a config name.

    Mirrors the overlay `@register` pattern so adding a new background is
    one decorator instead of remembering to extend a separate dict at the
    bottom of the file. Generic in the subclass so static analyzers see
    each decorated class's actual identity, not the base class."""

    def deco(cls: type[_BgT]) -> type[_BgT]:
        cls.name = name
        REGISTRY[name] = cls
        return cls

    return deco


# ---------------------------------------------------------------------------
# Starfield
# ---------------------------------------------------------------------------


@register("starfield")
class StarfieldBackground(Background):
    """Three layers of dot stars scrolling horizontally at different speeds.

    Stars are randomized once per layer at construction time, then each frame
    we recompute their column positions from `t * speed`. Different glyphs +
    colors per layer give the parallax depth cue."""

    LAYERS = (
        # (count, speed_cols_per_s, glyph, color)
        (12, 12.0, SC_DOT, DEPTH_COLORS[0]),  # near, fast, light
        (18, 6.0, SC_STAR, DEPTH_COLORS[1]),  # mid
        (28, 3.0, SC_PLUS, DEPTH_COLORS[2]),  # far, slow, dim
    )

    def __init__(self, seed: int | None = None):
        super().__init__(seed)
        self.layers = []
        for count, speed, glyph, color in self.LAYERS:
            xs = self.np_rng.uniform(0, 40, count).astype(np.float32)
            ys = self.np_rng.integers(0, 25, count).astype(np.int32)
            self.layers.append((xs, ys, float(speed), glyph, color))

    def _fill(self, chars, colors, t, rows, bg_color):
        if not rows:
            return
        row_set = set(rows)
        for xs, ys, speed, glyph, color in self.layers:
            shifted = (xs + speed * t) % 40.0
            cols = shifted.astype(np.int32)
            for x, y in zip(cols, ys, strict=False):
                if int(y) in row_set:
                    idx = int(y) * 40 + int(x)
                    chars[idx] = glyph
                    colors[idx] = color


# ---------------------------------------------------------------------------
# PETSCII bars
# ---------------------------------------------------------------------------


@register("petscii_bars")
class PetsciiBarsBackground(Background):
    """Each row is a strip of block chars whose phase scrolls at a per-row
    speed. Rows further from the text strip scroll faster — gives a
    depth-receding feel."""

    GLYPHS = (SC_FULL, SC_HBLOCK_TOP, SC_HBLOCK_BOT)

    def __init__(self, seed: int | None = None):
        super().__init__(seed)
        # Per-row offsets so adjacent rows don't all line up.
        self.row_phase = self.np_rng.uniform(0, 40, 25).astype(np.float32)

    def _fill(self, chars, colors, t, rows, bg_color):
        if not rows:
            return
        rows_list = list(rows)
        mid = (rows_list[0] + rows_list[-1]) / 2.0
        for y in rows_list:
            # Speed proportional to distance from the centre row of the strip
            # — far rows move faster.
            speed = 2.0 + 1.5 * abs(y - mid)
            phase = self.row_phase[y] + speed * t
            glyph = self.GLYPHS[y % len(self.GLYPHS)]
            color = VIBRANT_COLORS[y % len(VIBRANT_COLORS)]
            row_chars = np.full(40, SC_SPACE, dtype=np.uint8)
            # 50% duty: solid bars 4 cells wide every 8 cells.
            for x in range(40):
                p = (x + phase) % 8.0
                if p < 4.0:
                    row_chars[x] = glyph
            start = y * 40
            chars[start : start + 40] = row_chars
            colors[start : start + 40] = np.where(row_chars != SC_SPACE, color, bg_color).astype(
                np.uint8
            )


# ---------------------------------------------------------------------------
# Raster bars (copper-bar feel)
# ---------------------------------------------------------------------------


@register("raster_bars")
class RasterBarsBackground(Background):
    """Solid colored rows whose color index cycles with a vertical phase
    that drifts over time."""

    PALETTE = [
        C64_COLORS["red"],
        C64_COLORS["orange"],
        C64_COLORS["yellow"],
        C64_COLORS["light green"],
        C64_COLORS["cyan"],
        C64_COLORS["light blue"],
        C64_COLORS["purple"],
    ]

    def _fill(self, chars, colors, t, rows, bg_color):
        if not rows:
            return
        n = len(self.PALETTE)
        # Drift the palette upward at 6 rows/sec.
        offset = t * 6.0
        for y in rows:
            color = self.PALETTE[int(y + offset) % n]
            start = y * 40
            chars[start : start + 40] = SC_FULL
            colors[start : start + 40] = color


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


@register("checker")
class CheckerBackground(Background):
    """Marching diagonal checkerboard."""

    COLOR_A = C64_COLORS["light blue"]
    COLOR_B = C64_COLORS["blue"]

    def _fill(self, chars, colors, t, rows, bg_color):
        if not rows:
            return
        phase = int(t * 4.0)
        for y in rows:
            row_chars = np.full(40, SC_SPACE, dtype=np.uint8)
            row_cols = np.full(40, bg_color, dtype=np.uint8)
            for x in range(40):
                # 2-wide cells so the pattern is readable at 40 cols.
                tile = ((x + y * 2 + phase) // 2) & 1
                if tile:
                    row_chars[x] = SC_FULL
                    row_cols[x] = self.COLOR_A
                else:
                    row_chars[x] = SC_FULL
                    row_cols[x] = self.COLOR_B
            start = y * 40
            chars[start : start + 40] = row_chars
            colors[start : start + 40] = row_cols


# ---------------------------------------------------------------------------
# Nature
# ---------------------------------------------------------------------------


@register("nature")
class NatureBackground(Background):
    """Top: clouds drifting + birds flapping. Bottom: hills + lake waves.

    The visual split between "top behavior" and "bottom behavior" assumes
    top_rows is above the text and bottom_rows is below; the constructor
    samples once and each frame moves things along."""

    # Cloud glyphs sequence — three-cell fluffy mound.
    CLOUD = (SC_O, SC_AT, SC_O)
    BIRD_GLYPHS = (SC_V, SC_W)
    LAKE_GLYPH = SC_TILDE
    HILL_GLYPH = SC_HBLOCK_BOT
    HILL_PEAK = SC_FULL

    def __init__(self, seed: int | None = None):
        super().__init__(seed)
        self.clouds = [(self.rng.uniform(0, 40), self.rng.randint(0, 3)) for _ in range(5)]
        self.birds = [(self.rng.uniform(0, 40), self.rng.randint(0, 4)) for _ in range(3)]

    def _fill(self, chars, colors, t, rows, bg_color):
        if not rows:
            return
        rows_list = list(rows)
        # Decide if this strip is the "top" (sky) or "bottom" (ground).
        # We do not know which is which up-front, so heuristic on midpoint.
        mid = sum(rows_list) / len(rows_list)
        if mid < 12:
            self._fill_sky(chars, colors, t, rows_list, bg_color)
        else:
            self._fill_ground(chars, colors, t, rows_list, bg_color)

    def _fill_sky(self, chars, colors, t, rows, bg_color):
        row_set = set(rows)
        # Clouds drift left at 1 col/s.
        for x0, y0 in self.clouds:
            if y0 not in row_set:
                continue
            x = int((x0 - t * 1.0) % 40)
            for i, g in enumerate(self.CLOUD):
                xi = (x + i) % 40
                idx = y0 * 40 + xi
                chars[idx] = g
                colors[idx] = C64_COLORS["white"]
        # Birds flap (alternate V/W) and move right at 4 col/s.
        flap_phase = int(t * 6.0) & 1
        for x0, y0 in self.birds:
            if y0 not in row_set:
                continue
            x = int((x0 + t * 4.0) % 40)
            glyph = self.BIRD_GLYPHS[flap_phase]
            idx = y0 * 40 + x
            chars[idx] = glyph
            colors[idx] = C64_COLORS["dark gray"]

    def _fill_ground(self, chars, colors, t, rows, bg_color):
        # Bottom row(s) = lake with cycling colors.
        lake_y = rows[-1]
        hill_rows = [r for r in rows if r != lake_y]

        # Lake: alternating wave glyphs with phase shift, color cycles
        # between blue and light blue.
        lake_chars = np.full(40, self.LAKE_GLYPH, dtype=np.uint8)
        lake_colors = np.full(40, C64_COLORS["light blue"], dtype=np.uint8)
        phase = int(t * 8.0)
        for x in range(40):
            if (x + phase) % 4 < 2:
                lake_colors[x] = C64_COLORS["blue"]
        chars[lake_y * 40 : lake_y * 40 + 40] = lake_chars
        colors[lake_y * 40 : lake_y * 40 + 40] = lake_colors

        # Hills: pseudo-random silhouette using a sin sum, static (so they
        # look planted). Use lower-half block for slope, full block where
        # silhouette has cleared the bottom row by 2+.
        # Heights computed once across all 40 cols.
        xs = np.arange(40, dtype=np.float32)
        # Two sinusoids for varied skyline; output in [0, len(hill_rows)+1).
        h = (np.sin(xs * 0.4) + np.sin(xs * 0.21 + 1.2)) * 0.5 + 1.0
        h = np.clip((h * (len(hill_rows) + 0.5)).astype(np.int32), 0, len(hill_rows))
        # Bottom of hill_rows is closest to lake.
        for x in range(40):
            top_y = lake_y - h[x]
            for y in hill_rows:
                if y < top_y:
                    continue
                idx = y * 40 + x
                # Topmost hill row gets slope glyph; lower rows get full.
                chars[idx] = self.HILL_GLYPH if y == top_y else self.HILL_PEAK
                colors[idx] = C64_COLORS["green"]


# ---------------------------------------------------------------------------
# City
# ---------------------------------------------------------------------------


@register("city")
class CityBackground(Background):
    """Top: planes scrolling right, satellites blinking. Bottom: skyscraper
    silhouettes with lit windows."""

    PLANE = SC_HYPHEN
    PLANE_NOSE = SC_GT
    SAT_ON = SC_PLUS
    SAT_OFF = SC_SPACE
    WINDOW = SC_FULL
    SKY_BG = 0x20

    def __init__(self, seed: int | None = None):
        super().__init__(seed)
        self.planes = [(self.rng.uniform(0, 40), self.rng.randint(0, 4)) for _ in range(2)]
        self.satellites = [(self.rng.randint(0, 40), self.rng.randint(0, 3)) for _ in range(8)]
        # Skyscraper heights — pick once per construction.
        # Each "building" is 3-5 cols wide; heights are 3..8 rows.
        self.buildings = []
        x = 0
        while x < 40:
            w = self.rng.randint(3, 6)
            if x + w > 40:
                w = 40 - x
            h = self.rng.randint(3, 9)
            self.buildings.append((x, w, h))
            x += w

    def _fill(self, chars, colors, t, rows, bg_color):
        if not rows:
            return
        rows_list = list(rows)
        mid = sum(rows_list) / len(rows_list)
        if mid < 12:
            self._fill_sky(chars, colors, t, rows_list, bg_color)
        else:
            self._fill_ground(chars, colors, t, rows_list, bg_color)

    def _fill_sky(self, chars, colors, t, rows, bg_color):
        row_set = set(rows)
        # Planes: 3-cell sprite "-->" moving right.
        for x0, y0 in self.planes:
            if y0 not in row_set:
                continue
            x = int((x0 + t * 8.0) % 40)
            for i, g in enumerate((self.PLANE, self.PLANE, self.PLANE_NOSE)):
                xi = (x + i) % 40
                idx = y0 * 40 + xi
                chars[idx] = g
                colors[idx] = C64_COLORS["light gray"]
        # Satellites: stationary dots that blink in/out at 2 Hz with phase.
        blink = (int(t * 4.0)) & 1
        for i, (x, y) in enumerate(self.satellites):
            if y not in row_set:
                continue
            idx = y * 40 + x
            on = ((i + blink) & 1) == 0
            if on:
                chars[idx] = self.SAT_ON
                colors[idx] = C64_COLORS["yellow"]

    def _fill_ground(self, chars, colors, t, rows, bg_color):
        # Buildings: solid silhouettes, top h rows of each building are
        # rendered, lit-window pattern blinks every ~1 s.
        blink = int(t * 1.5) & 1
        bottom_y = rows[-1]
        for bx, bw, bh in self.buildings:
            top_y = bottom_y - bh + 1
            for y in rows:
                if y < top_y:
                    continue
                for x in range(bx, bx + bw):
                    idx = y * 40 + x
                    # Window pattern: every other col, every other row, with
                    # blink phase to vary.
                    if (x - bx) % 2 == 1 and (bottom_y - y) % 2 == 1:
                        lit = ((x + y + blink) & 1) == 0
                        if lit:
                            chars[idx] = self.WINDOW
                            colors[idx] = C64_COLORS["yellow"]
                        else:
                            chars[idx] = self.WINDOW
                            colors[idx] = C64_COLORS["dark gray"]
                    else:
                        chars[idx] = self.WINDOW
                        colors[idx] = C64_COLORS["dark gray"]


# ---------------------------------------------------------------------------
# None / blank
# ---------------------------------------------------------------------------


@register("none")
class NoneBackground(Background):
    def _fill(self, chars, colors, t, rows, bg_color):
        return  # leave the strip filled with space/bg from render()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build(name: str, seed: int | None = None) -> Background:
    if name == "random":
        # Exclude 'none' from random rotation — picking the boring one
        # randomly would feel like a bug.
        choices = [k for k in REGISTRY if k != "none"]
        name = random.choice(choices)
    if name not in REGISTRY:
        raise ValueError(
            f"unknown background {name!r} (want one of: {', '.join(REGISTRY)}, random)"
        )
    return REGISTRY[name](seed=seed)
