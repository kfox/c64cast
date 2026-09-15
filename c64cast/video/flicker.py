"""Temporal color blending: the palette the eye sees when two C64 colors
alternate at the VIC field rate.

Alternating two hardware colors every video field fuses them into a shade the
VIC cannot produce (the Dragon Breed / Mayhem in Monsterland trick). This module
owns the *color* half of that: which pairs are eligible, what they look like
fused, and how to quantize a frame against the widened palette. The C64-side
alternation lives in `modes_irq.FLICKER_SWAP_IRQ_HANDLER`.

Eligibility is measured rather than modeled: `SCORED_FLICKER` is a blind
scoring run, `[color].flicker_tolerance` is a cut across it, and
`[color].flicker_max_luma_delta` is an advisory photosensitivity cap on top.

See docs/architecture/video-color.md#colorflicker_tolerance--temporal-color-blending.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property

import cv2
import numpy as np

from c64cast.video.palette import (
    C64_PALETTE_BGR,
    DISTANCE_WEIGHTS,
    color_display_name,
    on_palette_change,
    resolve_color,
)

# Rec.709 weights in OpenCV's BGR channel order, applied to linear-light
# values. Not palette.PALETTE_LUMA, which is Rec.601 on *encoded* sRGB.
_LUMA_WEIGHTS_BGR = np.array([0.0722, 0.7152, 0.2126], dtype=np.float32)

# Linear-luminance delta at which a pair modulates 12% of peak white at the
# field rate, against the 20% the ITU-R BT.1702 guidance is written around.
# Advisory: arming warns past it and proceeds.
FLASH_CRITERION_LUMA_DELTA = 0.12

WARN_LUMA_DELTA = 0.10

# Blind scoring run on an Ultimate 64: every pair the 0.12 clamp admits, rated
# by eye with positions shuffled, pools separated, and hidden solid negative
# controls. Keys are (lower index, higher index). Grown by
# scripts/diags/flicker_score_grid.py.
SCORED_FLICKER: dict[tuple[int, int], str] = {
    (6, 9): "none",
    (2, 4): "verymild",
    (2, 8): "verymild",
    (3, 15): "verymild",
    (4, 8): "verymild",
    (4, 12): "verymild",
    (8, 12): "verymild",
    (9, 11): "verymild",
    (0, 9): "mild",
    (0, 11): "mild",
    (4, 14): "mild",
    (6, 11): "mild",
    (8, 14): "mild",
    (12, 14): "mild",
    (0, 6): "moderate",
    (2, 9): "moderate",
    (2, 12): "moderate",
    (2, 14): "moderate",
    (4, 6): "moderate",
    (4, 11): "moderate",
    (5, 10): "moderate",
    (5, 15): "moderate",
    (7, 13): "moderate",
    (0, 2): "intense",
    (2, 6): "intense",
    (0, 4): "intense",
    (2, 11): "intense",
    (4, 9): "intense",
    (6, 8): "intense",
    (8, 9): "intense",
    (8, 11): "intense",
    (10, 12): "intense",
    (10, 14): "intense",
}

# Quietest first; FLICKER_TOLERANCES cuts across this order by index.
FLICKER_TIERS = ("none", "verymild", "mild", "moderate", "intense")

# `[color].flicker_tolerance` values, and the worst FLICKER_TIERS index each
# admits. Named apart from the tier names because one pair scored "none".
FLICKER_TOLERANCES: dict[str, int] = {
    "off": -1,
    "clean": 1,  # none + very mild
    "subtle": 2,  # + mild
    "visible": 3,  # + moderate
}
DEFAULT_TOLERANCE = "off"

# Minimum distance from every solid, in OpenCV 8-bit Lab units.
MIN_BLEND_LAB_GAIN = 4.0


def _srgb_to_linear(encoded: np.ndarray) -> np.ndarray:
    """sRGB 0..255 → linear light 0..1, elementwise."""
    c = encoded.astype(np.float32) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def _linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    """Linear light 0..1 → sRGB 0..255, the inverse of `_srgb_to_linear`."""
    c = np.clip(linear, 0.0, 1.0)
    enc = np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)
    return (enc * 255.0).astype(np.float32)


_PALETTE_LINEAR = _srgb_to_linear(C64_PALETTE_BGR)  # (16, 3)
_PALETTE_Y = _PALETTE_LINEAR @ _LUMA_WEIGHTS_BGR  # (16,) linear luminance


def pair_luma_delta(a: int, b: int) -> float:
    """Linear-luminance separation of a candidate flicker pair, 0.0 (identical
    brightness, fuses invisibly) to 1.0 (black against white).

    Absolute, not normalized by the pair's own brightness: two dark colors a
    given distance apart flicker no worse than two light ones the same distance
    apart, and every normalization tried — Michelson, Weber, a Ferry-Porter
    term — made the dark end worse rather than better.
    """
    return abs(float(_PALETTE_Y[a]) - float(_PALETTE_Y[b]))


def fuse(a: int, b: int) -> np.ndarray:
    """The BGR color the eye sees when palette indices `a` and `b` alternate."""
    return _linear_to_srgb(0.5 * (_PALETTE_LINEAR[a] + _PALETTE_LINEAR[b]))


def fuse_indices(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """`fuse` over arrays of palette indices, elementwise. Shape (..., 3).

    What the software mirror behind preview and recording paints with: fusing
    the two fields' cell colors once is equivalent to alternating them and far
    cheaper than rendering both pages, and it is the frame a viewer's eye
    actually integrates — so the mirror shows the blend with no flicker at all,
    which no capture of the real display can do."""
    return _linear_to_srgb(0.5 * (_PALETTE_LINEAR[a] + _PALETTE_LINEAR[b]))


def _to_lab(bgr: np.ndarray) -> np.ndarray:
    """(N, 3) float32 BGR 0..255 → (N, 3) float32 OpenCV 8-bit Lab."""
    u8 = np.clip(bgr, 0, 255).astype(np.uint8).reshape(-1, 1, 3)
    return cv2.cvtColor(u8, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)


_PALETTE_LAB = _to_lab(C64_PALETTE_BGR)


def pair_flicker_tier(a: int, b: int) -> str | None:
    """How much flicker this pair was scored at, or None if it was never scored."""
    return SCORED_FLICKER.get((a, b) if a <= b else (b, a))


def parse_scoring_pairs(specs: Sequence[str]) -> list[tuple[int, int]]:
    """`["Blue+Brown", "2+8"]` -> `[(6, 9), (2, 8)]`, for flicker_score_pairs.

    Takes the same "NAME+NAME" shape the arming log prints, so a pair can be
    copied out of a log and scored without translating it by hand.
    """
    out: list[tuple[int, int]] = []
    for spec in specs:
        halves = str(spec).split("+")
        if len(halves) != 2:
            raise ValueError(f"flicker_score_pairs entry must be 'A+B', got {spec!r}")
        a, b = (resolve_color(h.strip()) for h in halves)
        if a == b:
            raise ValueError(f"flicker_score_pairs entry pairs a color with itself: {spec!r}")
        pair = (a, b) if a < b else (b, a)
        if pair not in out:
            out.append(pair)
    return out


def blend_pairs(
    max_luma_delta: float, *, tolerance: str = DEFAULT_TOLERANCE
) -> list[tuple[int, int]]:
    """Eligible flicker pairs at this safety cap and tolerance, ordered by
    descending gain over the nearest solid.

    A pair qualifies when it modulates luminance gently enough to be safe, it
    was scored no worse than the tolerance allows, and its fused color lands
    far enough from all 16 solids to be worth a second screen page.
    """
    if tolerance not in FLICKER_TOLERANCES:
        raise ValueError(
            f"flicker tolerance must be one of {tuple(FLICKER_TOLERANCES)}, got {tolerance!r}"
        )
    worst = FLICKER_TOLERANCES[tolerance]
    if worst < 0:
        return []
    cap = float(max_luma_delta)
    scored: list[tuple[float, tuple[int, int]]] = []
    for a, b in itertools.combinations(range(16), 2):
        if pair_luma_delta(a, b) > cap:
            continue
        tier = SCORED_FLICKER.get((a, b))
        if tier is None or FLICKER_TIERS.index(tier) > worst:
            continue
        gain = float(np.min(np.linalg.norm(_PALETTE_LAB - _to_lab(fuse(a, b)[None, :]), axis=1)))
        if gain >= MIN_BLEND_LAB_GAIN:
            scored.append((gain, (a, b)))
    scored.sort(key=lambda s: -s[0])
    return [pair for _, pair in scored]


@dataclass(frozen=True)
class BlendTable:
    """A widened palette: the 16 solids followed by the eligible blends.

    `pairs[i]` is the (field A, field B) palette pair entry `i` renders as, so a
    solid is simply the pair `(c, c)` and nothing downstream needs a branch for
    it. Entry `i` for `i < 16` IS solid `i`, which lets a caller fall back to
    plain-palette behavior by clipping indices to 16.
    """

    pairs: np.ndarray  # (N, 2) uint8
    bgr: np.ndarray  # (N, 3) float32 — the fused color
    max_luma_delta: float
    tolerance: str = DEFAULT_TOLERANCE
    scoring: bool = False

    @property
    def size(self) -> int:
        return int(self.pairs.shape[0])

    @property
    def blend_count(self) -> int:
        """How many entries are true blends rather than solids."""
        return self.size - 16

    def field_pages(self, indices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Split extended indices into their field-A and field-B palette indices."""
        table = self.pairs[indices]
        return table[..., 0], table[..., 1]

    @cached_property
    def luma(self) -> np.ndarray:
        """(N,) Rec.601 luma of each entry's fused color, on encoded sRGB.

        Deliberately the same formula palette.PALETTE_LUMA uses rather than the
        linear-light Rec.709 one above: this orders a cell's colors dark→light
        for the luminance/contrast cell strategies, and those have to rank
        blends and solids on one scale.
        """
        return (self.bgr @ np.array([0.114, 0.587, 0.299], dtype=np.float32)).astype(np.float32)

    @cached_property
    def nearest_solid(self) -> np.ndarray:
        """(N,) index of the closest of the 16 solids to each entry's fused color.

        Identity over the first 16. What a slot that cannot alternate falls back
        to — mhires' c3 lives in color RAM at $D800, which is neither VIC-banked
        nor selected by $D018, so a blend picked there has to be demoted to a
        real color rather than dropped.
        """
        ent = _to_lab(self.bgr)
        sol = _to_lab(self.bgr[:16])
        return np.argmin(((ent[:, None, :] - sol[None, :, :]) ** 2).sum(axis=2), axis=1).astype(
            np.uint8
        )

    @cached_property
    def demotion_cost(self) -> np.ndarray:
        """(N,) Lab distance from each entry to the solid it would fall back to.

        Zero over the first 16. Ranks which of a cell's blends to give up when a
        slot that cannot alternate has to be filled from them: the cheapest is
        the blend that was closest to a real color anyway.
        """
        ent = _to_lab(self.bgr)
        return np.linalg.norm(ent - ent[self.nearest_solid], axis=1).astype(np.float32)

    def describe(self) -> list[str]:
        """Human-readable names of the blend entries, for logging."""
        return [
            f"{color_display_name(int(a))}+{color_display_name(int(b))}" for a, b in self.pairs[16:]
        ]


# Keyed by every input a table depends on, because a live-tuned tolerance or cap
# must land on the next frame rather than on the next palette swap.
_TABLE_CACHE: dict[tuple[float, str, tuple[tuple[int, int], ...] | None], BlendTable] = {}


def _rebuild_palette_tables() -> None:
    """Re-derive everything keyed to the palette after a host-palette swap."""
    global _PALETTE_LINEAR, _PALETTE_Y, _PALETTE_LAB
    _PALETTE_LINEAR = _srgb_to_linear(C64_PALETTE_BGR)
    _PALETTE_Y = _PALETTE_LINEAR @ _LUMA_WEIGHTS_BGR
    _PALETTE_LAB = _to_lab(C64_PALETTE_BGR)
    _TABLE_CACHE.clear()


on_palette_change(_rebuild_palette_tables)


def build_blend_table(
    max_luma_delta: float,
    *,
    tolerance: str = DEFAULT_TOLERANCE,
    score_pairs: Sequence[tuple[int, int]] | None = None,
) -> BlendTable:
    """The widened palette at this cap and tolerance. Cached per settings pair.

    `score_pairs` replaces the eligible set outright — no tier filter, no luma
    cap, no gain floor. Only `scripts/diags/flicker_score_grid.py` passes it,
    so the tool that produces the tiers is not bounded by them.
    """
    delta = round(float(max_luma_delta), 4)
    override = tuple(score_pairs) if score_pairs is not None else None
    key = (delta, tolerance, override)
    cached = _TABLE_CACHE.get(key)
    if cached is not None:
        return cached
    extra = list(override) if override is not None else blend_pairs(delta, tolerance=tolerance)
    pairs = np.array([(i, i) for i in range(16)] + extra, dtype=np.uint8)
    bgr = np.stack([fuse(int(a), int(b)) for a, b in pairs]).astype(np.float32)
    table = BlendTable(
        pairs=pairs,
        bgr=bgr,
        max_luma_delta=delta,
        tolerance=tolerance,
        scoring=override is not None,
    )
    _TABLE_CACHE[key] = table
    return table


def _weighted_distances(flat_pixels: np.ndarray, table: BlendTable) -> np.ndarray:
    """(N, 3) BGR → (N, size) squared weighted-BGR distance to the widened palette.

    Same expansion trick as palette.quantize_distances: d²(x, p) expands to
    |x|² - 2·x·p + |p|², so one (N, 3) @ (3, size) matmul replaces the
    (N, size, 3) broadcast tensor.
    """
    wpal = (table.bgr * DISTANCE_WEIGHTS).T  # (3, size)
    pal_normsq = (table.bgr**2) @ DISTANCE_WEIGHTS  # (size,)
    px_normsq = (flat_pixels**2) @ DISTANCE_WEIGHTS  # (N,)
    return px_normsq[:, None] - 2.0 * (flat_pixels @ wpal) + pal_normsq[None, :]


def _lab_distances(flat_pixels: np.ndarray, table: BlendTable) -> np.ndarray:
    """(N, 3) BGR → (N, size) squared CIE-Lab distance to the widened palette."""
    lab = _to_lab(flat_pixels)
    pal_lab = _to_lab(table.bgr)
    px_normsq = (lab**2).sum(axis=1)
    pal_normsq = (pal_lab**2).sum(axis=1)
    return px_normsq[:, None] - 2.0 * (lab @ pal_lab.T) + pal_normsq[None, :]


def blend_distances_for(
    flat_pixels: np.ndarray, table: BlendTable, *, perceptual: bool
) -> np.ndarray:
    """(N, size) distance matrix in the selected metric — the widened-palette
    sibling of palette.quantize_distances_for, with the same dispatch."""
    return (
        _lab_distances(flat_pixels, table)
        if perceptual
        else _weighted_distances(flat_pixels, table)
    )


def quantize_flat_blend(
    flat_pixels: np.ndarray, table: BlendTable, *, perceptual: bool
) -> np.ndarray:
    """Nearest widened-palette index per pixel. (N, 3) → (N,)."""
    return np.argmin(blend_distances_for(flat_pixels, table, perceptual=perceptual), axis=1)
