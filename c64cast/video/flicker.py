"""Temporal colour blending: the palette the eye sees when two C64 colours
alternate at the VIC field rate.

Alternating two hardware colours every video field fuses them into a shade the
VIC cannot produce (the Dragon Breed / Mayhem in Monsterland trick). This module
owns the *colour* half of that: which pairs are eligible, what they look like
fused, and how to quantize a frame against the widened palette. The C64-side
alternation lives in `modes_irq.FLICKER_SWAP_IRQ_HANDLER`.

Two gates decide the eligible set, and they answer different questions.

**ΔY is the safety cap, and only that.** `[color].flicker_max_luma_delta` bounds
the *absolute* difference in linear luminance — not a contrast ratio, and not the
8-bit `PALETTE_LUMA` delta. A ratio was the first attempt and it fails in the one
place it matters. Michelson divides by the pair's mean luminance, so it is
maximally wrong where the eye is least sensitive: black against anything scores
1.0 by construction, which categorically refused Black+Blue, Black+Brown and
Black+Dark Gray (all under 0.07 ΔY, all of which fuse cleanly), while admitting
Cyan+Yellow at 0.26 ΔY on an Ultimate 64 — as violent a flicker as anything on
the test chart. The 8-bit delta is wrong for a different reason: it is Rec.601 on
gamma-encoded values, so it misreads the dark end in the opposite direction.

What the cap is *for* is photosensitivity, and that justification stands by
itself: a blended area alternates at 25 Hz (PAL) / 30 Hz (NTSC), inside the
ITU-R BT.1702 risk band, where the hazard is governed by luminance modulation
depth. `MAX_ALLOWED_LUMA_DELTA` keeps every admitted pair well under the
20%-of-peak-white flash criterion.

What the cap is **not** is a predictor of whether a pair fuses — and neither is
anything else derived from the two colours. Every pair the hard clamp admits was
scored by eye, blind, and against those verdicts ΔY reaches r=+0.26, Δchroma
+0.04, mean luminance −0.04, and the best multi-term fit an adjusted R² of 0.18
over n=33. A warmth axis was tried and removed: fitted to an earlier, smaller run
in which Red, Purple, Orange and Light Red all scored high, it reached r=+0.32 on
the blind run — no better than the ΔY rule it replaced — while excluding five of
the eight steadiest pairs. Warm *solids* do not flicker (all seven hidden solid
controls scored none, Red and Orange among them) and warm+warm pairs fuse well;
what the earlier run had actually picked up was warm against neutral.

**So eligibility is measured rather than modelled.** `SCORED_FLICKER` is that
blind run, one tier per pair, and `[color].flicker_tolerance` is a cut across it.
A pair with no entry is never admitted at any tolerance. On the Ultimate 64
table that costs nothing — the scored set is exactly what the hard clamp allows
— but the VIC-II rendering shifts luminances enough to bring five unscored pairs
under the clamp, Cyan+Yellow among them, which the U64 run never had to judge
because ΔY refused it there. `scripts/diags/flicker_score_grid.py` is how the
table grows.

The tiers are one observer, one sitting, who placed the mild/moderate and
moderate/intense boundaries at ±1. `"clean"` is the cut that does not rest on
either boundary.

**Tiers travel across palettes; ΔY does not.** ΔY is measured against the
*active* palette, so which pairs are even candidates follows `host_palette` —
what fuses is a statement about the light one machine emits, not a property of
"the C64 palette", which is why the tables here are rebuilt on a palette swap
rather than computed once at import. The scored tiers are then applied to
whatever palette is active, which is an extrapolation: they were collected on an
Ultimate 64, and a custom `host_palette` far from either shipped table
invalidates them.

**Fused colour is the linear-light average, not the sRGB average.** The eye
integrates emitted light over the two fields, so the mix has to happen after
sRGB decode. Averaging the encoded values instead makes every blend read too
dark, worst on the high-contrast pairs where the gamma curve is steepest.

Note that the safety cap binds before the tolerance does: at the 0.075 default
on an Ultimate 64, `"clean"` admits 5 of its 8 pairs, the other 3 sitting between
0.075 and the 0.12 clamp. Widening the cap to reach them is a photosensitivity
decision, not a quality one.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import cv2
import numpy as np

from c64cast.video.palette import (
    C64_PALETTE_BGR,
    DISTANCE_WEIGHTS,
    color_display_name,
    on_palette_change,
)

# Rec.709 luminance weights in OpenCV's BGR channel order, applied to
# linear-light values. Distinct from palette.PALETTE_LUMA, which is Rec.601 on
# *encoded* sRGB — fine for ordering a cell's colours dark→light, wrong for
# deciding whether two colours will visibly flicker against each other.
_LUMA_WEIGHTS_BGR = np.array([0.0722, 0.7152, 0.2126], dtype=np.float32)

# Linear-luminance delta above which a pair is refused outright, whatever the
# config asks for. Set from the flash criterion and not from anything observed
# to fuse: a pair here modulates 12% of peak white at the field rate, and the
# ceiling exists so no config can walk the modulation depth up to the 20% the
# photosensitivity guidance is written around.
MAX_ALLOWED_LUMA_DELTA = 0.12

# Past here the modulation depth is close enough to that criterion that the
# arming path says so rather than letting it through silently.
WARN_LUMA_DELTA = 0.10

# The blind scoring run: every pair the hard clamp admits on an Ultimate 64,
# rated by eye with positions shuffled, pools separated, and hidden solid
# negative controls. Keys are (lower index, higher index). This is data, not a
# rule — an earlier fitted metric is why it exists; see the module docstring.
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

# The scale the run was scored on, quietest first.
FLICKER_TIERS = ("none", "verymild", "mild", "moderate", "intense")

# `[color].flicker_tolerance` values, and the worst tier each one admits.
# Named apart from the tiers because one pair scored "none", which a tolerance
# called "none" would have to exclude and include at the same time.
FLICKER_TOLERANCES: dict[str, int] = {
    "off": -1,
    "clean": 1,  # none + very mild
    "subtle": 2,  # + mild
    "visible": 3,  # + moderate
    "strobe": 4,  # + intense
}
DEFAULT_TOLERANCE = "off"

# Below this the pair fuses to something a solid colour already covers, so it
# costs a page write and buys nothing. In OpenCV 8-bit Lab units.
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

    Absolute, not normalized by the pair's own brightness: two dark colours a
    given distance apart flicker no worse than two light ones the same distance
    apart, and every normalization tried — Michelson, Weber, a Ferry-Porter
    term — made the dark end worse rather than better.
    """
    return abs(float(_PALETTE_Y[a]) - float(_PALETTE_Y[b]))


def fuse(a: int, b: int) -> np.ndarray:
    """The BGR colour the eye sees when palette indices `a` and `b` alternate."""
    return _linear_to_srgb(0.5 * (_PALETTE_LINEAR[a] + _PALETTE_LINEAR[b]))


def fuse_indices(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """`fuse` over arrays of palette indices, elementwise. Shape (..., 3).

    What the software mirror behind preview and recording paints with: fusing
    the two fields' cell colours once is equivalent to alternating them and far
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


def blend_pairs(
    max_luma_delta: float, *, tolerance: str = DEFAULT_TOLERANCE
) -> list[tuple[int, int]]:
    """Eligible flicker pairs at this safety cap and tolerance, ordered by
    descending gain over the nearest solid.

    A pair qualifies when it modulates luminance gently enough to be safe, it
    was scored no worse than the tolerance allows, and its fused colour lands
    far enough from all 16 solids to be worth a second screen page.
    """
    worst = FLICKER_TOLERANCES.get(tolerance, -1)
    if worst < 0:
        return []
    cap = min(float(max_luma_delta), MAX_ALLOWED_LUMA_DELTA)
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
    plain-palette behaviour by clipping indices to 16.
    """

    pairs: np.ndarray  # (N, 2) uint8
    bgr: np.ndarray  # (N, 3) float32 — the fused colour
    max_luma_delta: float
    tolerance: str = DEFAULT_TOLERANCE

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

    def describe(self) -> list[str]:
        """Human-readable names of the blend entries, for logging."""
        return [
            f"{color_display_name(int(a))}+{color_display_name(int(b))}" for a, b in self.pairs[16:]
        ]


# Table construction costs a few hundred Lab conversions, and a live-tuned
# max_luma_delta would otherwise rebuild it every frame.
_TABLE_CACHE: dict[tuple[float, str], BlendTable] = {}


def _rebuild_palette_tables() -> None:
    """Re-derive everything keyed to the palette after a host-palette swap.

    Which pairs fuse is a statement about the luminances the display actually
    emits, so a machine with a different table has a different eligible set —
    not a rescaled one. Stale tables here would silently admit pairs that
    flicker on that machine, which is the one failure this module exists to
    prevent, hence the registration rather than a lazily-checked cache key.
    """
    global _PALETTE_LINEAR, _PALETTE_Y, _PALETTE_LAB
    _PALETTE_LINEAR = _srgb_to_linear(C64_PALETTE_BGR)
    _PALETTE_Y = _PALETTE_LINEAR @ _LUMA_WEIGHTS_BGR
    _PALETTE_LAB = _to_lab(C64_PALETTE_BGR)
    _TABLE_CACHE.clear()


on_palette_change(_rebuild_palette_tables)


def build_blend_table(max_luma_delta: float, *, tolerance: str = DEFAULT_TOLERANCE) -> BlendTable:
    """The widened palette at this cap and tolerance. Cached per settings pair."""
    delta = round(float(max_luma_delta), 4)
    key = (delta, tolerance)
    cached = _TABLE_CACHE.get(key)
    if cached is not None:
        return cached
    extra = blend_pairs(delta, tolerance=tolerance)
    pairs = np.array([(i, i) for i in range(16)] + extra, dtype=np.uint8)
    bgr = np.stack([fuse(int(a), int(b)) for a, b in pairs]).astype(np.float32)
    table = BlendTable(pairs=pairs, bgr=bgr, max_luma_delta=delta, tolerance=tolerance)
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
