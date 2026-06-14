"""PETSCII display-mode styles.

Each style maps a 25×40 BGR source image (post cv2.resize) to a pair of
(screen, color) uint8 buffers of length 1000. Styles are registered by
name; PETSCIIDisplayMode picks one at construction (from config) and can
rotate to the next via cycle_style() on a SHIFT press.

The styles deliberately span "informative" (default) → "abstract" (random
glyph, color-only) because the 40×25 grid loses too much detail for
strict photo-faithfulness anyway. The wild styles compensate by giving
the eye geometric texture or saturated color blocks instead.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from .c64 import SCREEN
from .palette import (
    C64_SPECTRUM_INDICES,
    HueCorrection,
    apply_hue_corrections,
    boost_saturation,
    quantize_distances,
    quantize_flat,
)

log = logging.getLogger(__name__)

# A "random" style at config-load time picks one of these at scene setup.
# Listed in cycle order; cycle_style() advances modulo this tuple. The
# "random" sentinel itself is NOT in the cycle list — it's a one-shot
# pick at startup, after which cycling proceeds through the concrete
# styles from wherever the random pick landed.
STYLE_NAMES = (
    "default",
    "halftone",
    "random_glyph",
    "letter_rain",
    "neon",
    "inverse_pop",
    "hatch",
    "color_only",
)

# Pseudonym for the "pick a concrete style at setup" sentinel.
RANDOM_STYLE = "random"


def validate_style(name: str) -> None:
    if name == RANDOM_STYLE:
        return
    if name not in STYLE_NAMES:
        raise ValueError(
            f"petscii style must be one of {(*STYLE_NAMES, RANDOM_STYLE)}, got {name!r}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _luma(img: np.ndarray) -> np.ndarray:
    """Per-cell grayscale luminance, flattened to (1000,) uint8."""
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).ravel()


def _ramp_to_chars(luma: np.ndarray, chars: np.ndarray) -> np.ndarray:
    """Map (1000,) uint8 luma values into a per-cell index into `chars`."""
    idx = np.minimum(
        (luma.astype(np.uint32) * len(chars)) // 256,
        len(chars) - 1,
    )
    return chars[idx]


def _shaped_flat(
    img: np.ndarray,
    channel_boost: np.ndarray,
    hue_corrections: tuple[HueCorrection, ...],
) -> np.ndarray:
    """Apply the global [color] shaping (hue corrections then per-channel
    boost) and return the (N, 3) float32 flat ready for quantization.

    Mirrors the bitmap modes' pre-quant stage so PETSCII color picks honor
    the same [color] config. The caller computes glyph/luma from the original
    image; only color selection sees the shaped pixels."""
    img = apply_hue_corrections(img, hue_corrections)
    return np.clip(img.reshape(-1, 3).astype(np.float32) * channel_boost, 0, 255)


def _quantize_to_spectrum(
    img: np.ndarray,
    channel_boost: np.ndarray,
    hue_corrections: tuple[HueCorrection, ...],
) -> np.ndarray:
    """Per-cell palette pick clamped to the 10 chromatic spectrum entries."""
    d = quantize_distances(_shaped_flat(img, channel_boost, hue_corrections))
    # Restrict argmin to the spectrum columns by setting all others to inf.
    mask = np.full(16, np.inf, dtype=np.float32)
    mask[C64_SPECTRUM_INDICES] = 0.0
    d += mask
    return np.argmin(d, axis=1).astype(np.uint8)


# ---------------------------------------------------------------------------
# Style base + concrete styles
# ---------------------------------------------------------------------------


class PetsciiStyle:
    """One style for PETSCIIDisplayMode. Subclasses implement compose().

    border + background are the palette indices the surrounding mode should
    poke to $D020/$D021 when this style becomes active. The mode owns the
    actual VIC writes; styles just declare their preferences.
    """

    name = "base"
    border: int = 0
    background: int = 0

    def reset(self) -> None:
        """Called when this style becomes active (initial setup or after a
        cycle). Override to re-seed per-cell glyph tables etc."""

    def compose(
        self,
        img_25x40: np.ndarray,
        channel_boost: np.ndarray,
        hue_corrections: tuple[HueCorrection, ...],
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (screen[1000], color[1000]) uint8 for the given image.

        channel_boost + hue_corrections are the global [color] shaping stage,
        applied to the per-cell color pick (not to glyph/luma selection)."""
        raise NotImplementedError


class DefaultStyle(PetsciiStyle):
    """Original luma → 11-char ramp + nearest-palette color per cell.

    Faithful, low-key, "live PETSCII feed" look. The other styles are wilder."""

    name = "default"
    CHARS = np.array(
        [0x20, 0x2E, 0x3A, 0x2D, 0x3D, 0x2B, 0x2A, 0x23, 0x25, 0x40, 0xA0],
        dtype=np.uint8,
    )

    def compose(self, img, channel_boost, hue_corrections):
        screen = _ramp_to_chars(_luma(img), self.CHARS)
        flat = _shaped_flat(img, channel_boost, hue_corrections)
        color = quantize_flat(flat).astype(np.uint8)
        return screen, color


class HalftoneStyle(PetsciiStyle):
    """5-level block-coverage ramp. Chunky, high-contrast geometric look."""

    name = "halftone"
    # Picked by visual coverage: blank → bottom-quarter → bottom-half →
    # right-half → full. Exact char choices vary by ROM but all read as
    # increasingly-filled cells.
    CHARS = np.array(
        [0x20, 0x6C, 0x64, 0x61, 0xA0],
        dtype=np.uint8,
    )

    def compose(self, img, channel_boost, hue_corrections):
        screen = _ramp_to_chars(_luma(img), self.CHARS)
        flat = _shaped_flat(img, channel_boost, hue_corrections)
        color = quantize_flat(flat).astype(np.uint8)
        return screen, color


class RandomGlyphStyle(PetsciiStyle):
    """Each cell is a distinctive glyph; the cell→glyph mapping is fixed
    per scene (seeded RNG at reset) so glyphs don't strobe between frames,
    but color RAM still tracks the video. Reads as "alien text" — the
    grid feels alive but content is purely abstract.

    Curated source set: bullets, arrows, lines, partial blocks, geometric
    chars. Skip ASCII letters/digits (would read as garbled text)."""

    name = "random_glyph"
    GLYPHS = np.array(
        [
            0x51,  # bullet (Q in upper case)
            0x57,
            0x58,  # W X
            0x5F,  # back-arrow
            0x69,
            0x6A,  # graphics chars
            0x71,
            0x73,
            0x77,
            0x60,
            0x62,
            0x64,
            0xA0,  # full block
            0xE0,
            0xE1,
            0xE2,
            0xF0,
            0xF1,
            0xF2,
        ],
        dtype=np.uint8,
    )

    def __init__(self):
        self._screen: np.ndarray | None = None

    def reset(self):
        # Stable per-cell pick; same seed → same arrangement on cycle re-entry.
        rng = np.random.default_rng(seed=0xC64A1A5)
        self._screen = self.GLYPHS[rng.integers(0, len(self.GLYPHS), size=1000)]

    def compose(self, img, channel_boost, hue_corrections):
        if self._screen is None:
            self.reset()
        assert self._screen is not None
        flat = _shaped_flat(img, channel_boost, hue_corrections)
        color = quantize_flat(flat).astype(np.uint8)
        return self._screen, color


class LetterRainStyle(PetsciiStyle):
    """Luma → A-Z. Matrix-style cascade. Color is per-cell quantize."""

    name = "letter_rain"
    # Screen codes 0x01-0x1A are A-Z in the upper-case ROM.
    CHARS = np.arange(0x01, 0x1B, dtype=np.uint8)

    def compose(self, img, channel_boost, hue_corrections):
        screen = _ramp_to_chars(_luma(img), self.CHARS)
        flat = _shaped_flat(img, channel_boost, hue_corrections)
        color = quantize_flat(flat).astype(np.uint8)
        return screen, color


class NeonStyle(PetsciiStyle):
    """Default char ramp + chromatic-only color picker. No grays/whites in
    the FG; only the 10 chromatic palette entries — even on a desaturated
    frame. Reads as 80s-arcade saturated."""

    name = "neon"
    CHARS = DefaultStyle.CHARS
    background = 0  # black background so neon FG pops

    def compose(self, img, channel_boost, hue_corrections):
        screen = _ramp_to_chars(_luma(img), self.CHARS)
        color = _quantize_to_spectrum(img, channel_boost, hue_corrections)
        return screen, color


class InversePopStyle(PetsciiStyle):
    """Every cell is space-or-block by luma threshold; FG limited to a
    curated 4-color pop-art palette."""

    name = "inverse_pop"
    # Pop-art-y high-contrast 4-color set: white, light red, cyan, yellow.
    POP_PALETTE_INDICES = np.array([1, 10, 3, 7], dtype=np.uint8)
    background = 0  # black background; FG colors do all the heavy lifting

    def compose(self, img, channel_boost, hue_corrections):
        luma = _luma(img)
        # Threshold at 128 → all-or-nothing fill.
        screen = np.where(luma >= 128, SCREEN.SC_FULL_BLOCK, SCREEN.SC_SPACE).astype(np.uint8)
        # Quantize the boosted color, then map to nearest of the 4 pop
        # picks using a precomputed pairwise distance table.
        boosted = boost_saturation(img, 1.8)
        flat = _shaped_flat(boosted, channel_boost, hue_corrections)
        pix_idx = quantize_flat(flat).astype(np.int64)
        # 16-entry LUT: each palette index → its closest pop_palette entry.
        # Cached once at class load; cheap to recompute per-instance if needed.
        if not hasattr(InversePopStyle, "_LUT"):
            from .palette import C64_PALETTE_BGR

            pairwise = quantize_distances(C64_PALETTE_BGR)  # (16, 16)
            InversePopStyle._LUT = np.argmin(  # type: ignore[attr-defined]
                pairwise[:, self.POP_PALETTE_INDICES], axis=1
            ).astype(np.uint8)
        slot = InversePopStyle._LUT[pix_idx]  # 0..3
        color = self.POP_PALETTE_INDICES[slot]
        return screen, color


class HatchStyle(PetsciiStyle):
    """Luma → 5-level cross-hatch shading. Sketchy, line-art look."""

    name = "hatch"
    # blank → "/" → "\" → "X" → full. Picked from the upper-ROM graphics
    # block; some readers may render these slightly differently but the
    # progression-of-density reads correctly.
    CHARS = np.array(
        [0x20, 0x4E, 0x4D, 0x58, 0xA0],
        dtype=np.uint8,
    )

    def compose(self, img, channel_boost, hue_corrections):
        screen = _ramp_to_chars(_luma(img), self.CHARS)
        flat = _shaped_flat(img, channel_boost, hue_corrections)
        color = quantize_flat(flat).astype(np.uint8)
        return screen, color


class ColorOnlyStyle(PetsciiStyle):
    """Every cell = SC_FULL_BLOCK; the image lives entirely in color RAM.

    Pure 40×25 color blocks — the screen becomes an abstract Mondrian of
    the source frame's palette distribution."""

    name = "color_only"
    background = 0

    def compose(self, img, channel_boost, hue_corrections):
        screen = np.full(1000, SCREEN.SC_FULL_BLOCK, dtype=np.uint8)
        flat = _shaped_flat(img, channel_boost, hue_corrections)
        color = quantize_flat(flat).astype(np.uint8)
        return screen, color


# ---------------------------------------------------------------------------
# Registry + factory
# ---------------------------------------------------------------------------

_STYLE_REGISTRY: dict[str, type[PetsciiStyle]] = {
    "default": DefaultStyle,
    "halftone": HalftoneStyle,
    "random_glyph": RandomGlyphStyle,
    "letter_rain": LetterRainStyle,
    "neon": NeonStyle,
    "inverse_pop": InversePopStyle,
    "hatch": HatchStyle,
    "color_only": ColorOnlyStyle,
}


def make_style(name: str) -> PetsciiStyle:
    """Construct a fresh PetsciiStyle instance. `name` must already be in
    STYLE_NAMES (validate with validate_style first; `random` should be
    resolved to a concrete name before calling this)."""
    cls = _STYLE_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"unknown petscii style {name!r} (known: {', '.join(sorted(_STYLE_REGISTRY))})"
        )
    style = cls()
    style.reset()
    return style


def pick_random_style_name() -> str:
    """Pick a concrete style name uniformly at random from STYLE_NAMES.
    Called by PETSCIIDisplayMode at setup() when configured with the
    'random' sentinel — afterwards cycle_style() proceeds from there."""
    # Drop into numpy's default RNG so the per-process pick is stable
    # enough for testing if anyone seeds the global, while still being
    # genuinely-random in production.
    return str(np.random.default_rng().choice(STYLE_NAMES))
