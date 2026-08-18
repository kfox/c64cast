"""Temporal colour blending: the palette the eye sees when two C64 colours
alternate at the VIC field rate.

Alternating two hardware colours every video field fuses them into a shade the
VIC cannot produce (the Dragon Breed / Mayhem in Monsterland trick). This module
owns the *colour* half of that: which pairs are eligible, what they look like
fused, and how to quantize a frame against the widened palette. The C64-side
alternation lives in `modes_irq.FLICKER_SWAP_IRQ_HANDLER`.

Two questions decide the eligible set, and they are answered by different rules.

**ΔY is the safety cap, and only that.** `[color].flicker_max_luma_delta` bounds
the *absolute* difference in linear luminance — not a contrast ratio, and not the
8-bit `PALETTE_LUMA` delta. A ratio was the first attempt
and it fails in the one place it matters. Michelson divides by the pair's mean
luminance, so it is maximally wrong where the eye is least sensitive: black
against anything scores 1.0 by construction, which categorically refused
Black+Blue, Black+Brown and Black+Dark Gray (all under 0.07 ΔY, all of which
fuse cleanly), while admitting Cyan+Yellow at 0.26 ΔY on an Ultimate 64 — as
violent a flicker as anything on the test chart. The 8-bit delta is wrong for a
different reason: it is Rec.601 on gamma-encoded values, so it misreads the
dark end in the opposite direction.

What the cap is *for* is photosensitivity, and that justification stands by
itself: a blended area alternates at 25 Hz (PAL) / 30 Hz (NTSC), inside the
ITU-R BT.1702 risk band, where the hazard is governed by luminance modulation
depth. `MAX_ALLOWED_LUMA_DELTA` keeps every admitted pair well under the
20%-of-peak-white flash criterion.

What the cap is **not** is a predictor of whether a pair fuses, and a lower
setting should not be read as "less flicker". All 21 pairs admitted at 0.075
were scored by eye on a 1702, and ΔY correlates with the verdicts at r=+0.33:
Black+Dark Gray, at ΔY 0.0685 near the top of the admitted range, reads as
almost nothing, while Medium Gray+Light Blue at 0.0002 — the smallest separation
the palette can offer — visibly flickers. No pairwise distance did better. The
strongest single term tried was ΔY·√(meanY) at r=+0.47, Δchroma reached +0.18,
and the best three-term fit an adjusted R² of 0.25 over n=21.

**Which colours are in the pair predicts it far better than how far apart they
are.** Every pair containing Red, Purple, Orange or Light Red scored high, on
flat patches and in motion alike, and excluding all of them is what made a real
clip settle down — same clip, same threshold, 21 pairs down to 7.
`[color].flicker_exclude_warm` is that rule and it defaults to on.
`WARM_FLICKER_COLORS` is an explicit set of four observed colours rather than a
hue band, because Brown is every bit as warm by name and stays: it read as solid
against both Blue and Black. Whether the four fail to fuse or are a
composite-NTSC chroma artifact is untested, and that is the distinction which
decides whether the rule ought to depend on the display and not just on the
palette.

ΔY is measured against the *active* palette, so this follows `host_palette`:
which two colours fuse is a statement about the light a particular machine
emits, not a property of "the C64 palette". The two shipped tables disagree
about half the time — 18 eligible pairs against the VIC-II rendering, 21 on an
Ultimate 64, agreeing on 10 — which is why the tables here are rebuilt on a
palette swap rather than computed once at import.

**Fused colour is the linear-light average, not the sRGB average.** The eye
integrates emitted light over the two fields, so the mix has to happen after
sRGB decode. Averaging the encoded values instead makes every blend read too
dark, worst on the high-contrast pairs where the gamma curve is steepest.

At the 0.075 default, 21 of the 120 mixed pairs clear the cap on an Ultimate 64
and every one lands >=4 Lab from all 16 solids; the warm exclusion takes that to
7, a 23-colour palette that gains a blue-charcoal ramp rather than crowding the
greys. Turning the exclusion off restores the 37-colour set, which is worth more
of the gamut and visibly less steady.
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

# Palette indices that no pair may contain while `flicker_exclude_warm` is on:
# Red, Purple, Orange, Light Red. Not a hue band — Brown is as warm by name and
# is deliberately absent, having read as solid against both Blue and Black — and
# not a threshold that was merely set too high, since pairs of larger ΔY built
# from the rest of the palette sat still while every one of these four moved.
WARM_FLICKER_COLORS = frozenset({2, 4, 8, 10})

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


def blend_pairs(max_luma_delta: float, *, exclude_warm: bool = True) -> list[tuple[int, int]]:
    """Eligible flicker pairs at `max_luma_delta`, ordered by descending gain.

    A pair qualifies when it modulates gently enough to be safe, contains no
    colour observed to flicker whatever its partner (unless `exclude_warm` is
    off), and lands far enough from all 16 solids to be worth a second screen
    page.
    """
    cap = min(float(max_luma_delta), MAX_ALLOWED_LUMA_DELTA)
    scored: list[tuple[float, tuple[int, int]]] = []
    for a, b in itertools.combinations(range(16), 2):
        if pair_luma_delta(a, b) > cap:
            continue
        if exclude_warm and (a in WARM_FLICKER_COLORS or b in WARM_FLICKER_COLORS):
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
    exclude_warm: bool = True

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
_TABLE_CACHE: dict[tuple[float, bool], BlendTable] = {}


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


def build_blend_table(max_luma_delta: float, *, exclude_warm: bool = True) -> BlendTable:
    """The widened palette at `max_luma_delta`. Cached per settings pair."""
    delta = round(float(max_luma_delta), 4)
    key = (delta, bool(exclude_warm))
    cached = _TABLE_CACHE.get(key)
    if cached is not None:
        return cached
    extra = blend_pairs(delta, exclude_warm=exclude_warm)
    pairs = np.array([(i, i) for i in range(16)] + extra, dtype=np.uint8)
    bgr = np.stack([fuse(int(a), int(b)) for a, b in pairs]).astype(np.float32)
    table = BlendTable(pairs=pairs, bgr=bgr, max_luma_delta=delta, exclude_warm=bool(exclude_warm))
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
