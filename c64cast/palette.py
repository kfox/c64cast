"""C64 palette constants and color quantization."""

from __future__ import annotations

import difflib
import logging
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)

C64_COLORS = {
    "black": 0,
    "white": 1,
    "red": 2,
    "cyan": 3,
    "purple": 4,
    "green": 5,
    "blue": 6,
    "yellow": 7,
    "orange": 8,
    "brown": 9,
    "light red": 10,
    "dark gray": 11,
    "gray": 12,
    "light green": 13,
    "light blue": 14,
    "light gray": 15,
}

# Canonical Title-Case display names, index-aligned to C64_COLORS. Used for
# error messages, --describe/help, and the wizard. "gray" is preferred over
# "grey", and index 12 reads "Medium Gray" to disambiguate it from the dark
# (11) and light (15) grays.
C64_COLOR_NAMES: tuple[str, ...] = (
    "Black",
    "White",
    "Red",
    "Cyan",
    "Purple",
    "Green",
    "Blue",
    "Yellow",
    "Orange",
    "Brown",
    "Light Red",
    "Dark Gray",
    "Medium Gray",
    "Light Green",
    "Light Blue",
    "Light Gray",
)


def color_display_name(index: int) -> str:
    """The Title-Case display name for a C64 palette index (0..15)."""
    return C64_COLOR_NAMES[index & 0x0F]


# --- Fuzzy color-name resolution -------------------------------------------
# _COLOR_ALIASES maps many spellings/abbreviations to a palette index so config
# color knobs accept forgiving names (case-insensitive, "lgrn", "mgry", "blk",
# grey==gray, ...). Built once at import from a modifier x hue grammar plus the
# handful of modifier+hue combinations the C64 palette actually has.
_MODIFIER_ALIASES: dict[str, list[str]] = {
    "light": ["light", "lt", "l", "lite"],
    "dark": ["dark", "dk", "d"],
    "medium": ["medium", "med", "mid", "m"],
}
# hue name -> (base palette index, spelling aliases)
_HUE_ALIASES: dict[str, tuple[int, list[str]]] = {
    "black": (0, ["black", "blk", "bk"]),
    "white": (1, ["white", "wht", "wh"]),
    "red": (2, ["red", "rd"]),
    "cyan": (3, ["cyan", "cyn", "cy"]),
    "purple": (4, ["purple", "purp", "prpl", "pur", "violet", "magenta"]),
    "green": (5, ["green", "grn", "gn"]),
    "blue": (6, ["blue", "blu", "bl"]),
    "yellow": (7, ["yellow", "yel", "ylw", "yl"]),
    "orange": (8, ["orange", "org", "orng", "ora"]),
    "brown": (9, ["brown", "brn", "bwn"]),
    # bare "gray" == medium gray (index 12)
    "gray": (12, ["gray", "grey", "gry", "gy"]),
}
# (modifier, hue) -> index, for the combinations the palette actually contains.
_MODIFIER_COMBOS: dict[tuple[str, str], int] = {
    ("light", "red"): 10,
    ("dark", "gray"): 11,
    ("medium", "gray"): 12,
    ("light", "green"): 13,
    ("light", "blue"): 14,
    ("light", "gray"): 15,
}


def _build_color_aliases() -> dict[str, int]:
    aliases: dict[str, int] = {}
    # bare hues (no modifier)
    for _hue, (idx, spellings) in _HUE_ALIASES.items():
        for sp in spellings:
            aliases.setdefault(sp, idx)
    # modifier + hue combos, in both spaced ("med gry") and compact ("mgry") forms
    for (mod, hue), idx in _MODIFIER_COMBOS.items():
        _, hue_spellings = _HUE_ALIASES[hue]
        for m in _MODIFIER_ALIASES[mod]:
            for h in hue_spellings:
                aliases.setdefault(f"{m} {h}", idx)
                aliases.setdefault(f"{m}{h}", idx)
    return aliases


_COLOR_ALIASES: dict[str, int] = _build_color_aliases()


def _normalize_color_token(token: str) -> str:
    """Lower, unify separators to spaces, collapse whitespace, grey->gray."""
    norm = re.sub(r"[-_]+", " ", token.strip().lower())
    norm = re.sub(r"\s+", " ", norm)
    return norm.replace("grey", "gray")


def resolve_color(token: int | str, *, default: int | None = None) -> int:
    """Resolve a C64 color name or index to a palette index (0..15).

    Accepts an int (or int-valued string), a canonical name ("light blue"), or a
    fuzzy/abbreviated spelling ("lgrn", "mgry", "blk", "grey"). Matching is
    case-insensitive with a difflib fallback for near-misses. On no match,
    returns ``default`` if given, else raises ValueError listing the valid
    names. This is the single color-name resolver shared by every config knob.
    """
    if isinstance(token, bool):  # bool is an int subclass — reject explicitly
        raise ValueError(f"invalid C64 color: {token!r}")
    if isinstance(token, int):
        if 0 <= token <= 15:
            return token
        if default is not None:
            log.warning(
                "C64 palette index %d out of range 0..15 — using %s",
                token,
                color_display_name(default),
            )
            return default
        raise ValueError(f"C64 palette index must be 0..15, got {token}")

    norm = _normalize_color_token(token)
    if norm.isdigit():
        return resolve_color(int(norm), default=default)
    if norm in _COLOR_ALIASES:
        return _COLOR_ALIASES[norm]
    if norm in C64_COLORS:
        return C64_COLORS[norm]
    close = difflib.get_close_matches(norm, _COLOR_ALIASES.keys(), n=1, cutoff=0.6)
    if close:
        return _COLOR_ALIASES[close[0]]
    if default is not None:
        log.warning(
            "unknown C64 color %r — using %s (valid: %s)",
            token,
            color_display_name(default),
            ", ".join(C64_COLOR_NAMES),
        )
        return default
    raise ValueError(
        f"unknown C64 color {token!r}; valid colors are: " + ", ".join(C64_COLOR_NAMES)
    )


C64_PALETTE_BGR = np.array(
    [
        [0, 0, 0],
        [255, 255, 255],
        [0, 0, 136],
        [238, 255, 170],
        [204, 68, 204],
        [85, 204, 0],
        [170, 0, 0],
        [119, 238, 238],
        [85, 136, 221],
        [0, 68, 102],
        [119, 119, 255],
        [51, 51, 51],
        [119, 119, 119],
        [102, 255, 170],
        [255, 136, 0],
        [187, 187, 187],
    ],
    dtype=np.float32,
)

C64_SPECTRUM_INDICES = np.array([2, 8, 7, 5, 13, 3, 14, 6, 4, 10])

# Rec.601 luma of each palette entry (0..255), computed from the BGR palette
# (0.114·B + 0.587·G + 0.299·R). Used by the mhires per-cell luminance/contrast
# color-selection strategies (modes._pick_cell_colors) to order a cell's present
# colors dark→light.
PALETTE_LUMA = (C64_PALETTE_BGR @ np.array([0.114, 0.587, 0.299], dtype=np.float32)).astype(
    np.float32
)

DISTANCE_WEIGHTS = np.array([2.0, 4.0, 3.0], dtype=np.float32)
# Pre-quantization per-channel gain (BGR), the built-in default for the global
# [color].channel_boost shaping stage. The blue/green lift biases the palette
# match toward C64-friendly hues; red is left at 1.0 — A/B on real TRON frames
# showed the historical 0.9 red-cut only raised perceptual (Lab) error and
# starved warm colors (yellow/red/purple) with zero benefit to the blues it was
# meant to favor. Override per-config via [color].channel_boost.
CHANNEL_BOOST = np.array([1.3, 1.2, 1.0], dtype=np.float32)

# Squared weighted distance is computed via the (x-p)² expansion:
#     d²(x, p) = Σ w·(x² - 2xp + p²)
# Precomputing the weighted palette and per-palette norm avoids materializing
# the (N, 16, 3) broadcast tensor that the naive form requires.
_W = DISTANCE_WEIGHTS
_WPAL = (C64_PALETTE_BGR * _W).T  # (3, 16)
_PAL_NORMSQ = (C64_PALETTE_BGR**2) @ _W  # (16,)


def quantize_distances(flat_pixels: np.ndarray) -> np.ndarray:
    """Return per-pixel squared distance to each of the 16 palette colors.

    flat_pixels: (N, 3) float32. Returns (N, 16) float32.
    """
    px_normsq = (flat_pixels**2) @ _W  # (N,)
    cross = flat_pixels @ _WPAL  # (N, 16)
    return px_normsq[:, None] - 2.0 * cross + _PAL_NORMSQ[None, :]


def quantize_flat(flat_pixels: np.ndarray) -> np.ndarray:
    """Return nearest-palette index (int64) for each pixel. flat_pixels: (N, 3) float32."""
    return np.argmin(quantize_distances(flat_pixels), axis=1)


# ---------------------------------------------------------------------------
# Scene fade: per-palette-index dim toward black
# ---------------------------------------------------------------------------

_IDENTITY_FADE_LUT = np.arange(16, dtype=np.uint8)
_FADE_LUT_CACHE: dict[tuple[int, tuple[int, ...] | None], np.ndarray] = {}


def build_fade_lut(alpha: float, allowed: tuple[int, ...] | None = None) -> np.ndarray:
    """Return a length-16 uint8 LUT mapping each palette index to its dimmed
    counterpart at brightness ``alpha`` ∈ [0, 1].

    The C64 has no global brightness register and its 16 palette indices are not
    luminance-ordered, so a scene fade is expressed as a *palette remap*: for
    each color ``c``, the LUT entry is the palette index nearest (in the same
    weighted-BGR space the quantizer uses) to ``C64_PALETTE_BGR[c] * alpha``.
    ``alpha >= 1`` is the identity; ``alpha = 0`` maps every entry to black
    (index 0); black always maps to black. Applied to the color-bearing fields
    of a composed frame (color RAM / screen-RAM color nibbles / bg registers)
    while leaving the bitmap pixel-selectors untouched, this fades non-black
    pixels toward black uniformly across every compose-based display mode.

    ``allowed`` restricts the candidate output indices to a palette subset —
    multicolor char mode's per-cell foreground can only be palette 0..7 (color
    RAM bit 3 is the multicolor flag), so MCM passes ``allowed=(0..7)`` to keep
    the dimmed foreground in range. ``None`` allows all 16.

    Results are memoized on a 1/256-quantized alpha (and the allowed set) so a
    steady fade ramp reuses a handful of LUTs rather than recomputing the
    nearest-color search every frame.
    """
    if alpha >= 1.0 and allowed is None:
        return _IDENTITY_FADE_LUT
    key = 0 if alpha <= 0.0 else min(256, int(round(alpha * 256.0)))
    cache_key = (key, allowed)
    cached = _FADE_LUT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    scaled = C64_PALETTE_BGR * (key / 256.0)
    dist = quantize_distances(scaled)  # (16, 16)
    if allowed is None:
        lut = np.argmin(dist, axis=1).astype(np.uint8)
    else:
        cand = np.asarray(allowed, dtype=np.intp)
        lut = cand[np.argmin(dist[:, cand], axis=1)].astype(np.uint8)
    _FADE_LUT_CACHE[cache_key] = lut
    return lut


# ---------------------------------------------------------------------------
# Palette-mode helpers (used by MCM / MultiHires display modes)
# ---------------------------------------------------------------------------

# Indices of gray-axis palette entries. The C64 palette has 5 of these so
# any desaturated pixel has 5 close winners and rarely picks a chromatic
# neighbor — `make_gray_penalty()` adds a distance² bias that shifts the
# decision boundary in favor of the chromatic entry.
GRAY_INDICES = (0, 1, 11, 12, 15)  # black, white, dark gray, gray, light gray
PALE_INDICES = (3,)  # cyan — chromatic but very pale; over-selected on warm-gray skin
CHROMATIC_INDICES = tuple(i for i in range(16) if i not in GRAY_INDICES)

# Default penalties chosen by eye against typical webcam input. Units are
# squared distance in the weighted BGR space used by quantize_distances —
# 2500 is roughly "a chromatic palette entry wins if it is within ~50 BGR
# units of the pixel; otherwise gray can win".
DEFAULT_GRAY_PENALTY = 2500.0
DEFAULT_PALE_PENALTY = 625.0
# Large enough to dominate any real squared-distance score, so chromatic
# entries lose every argmin. Used by "grayscale" palette_mode.
GRAYSCALE_CHROMATIC_PENALTY = 1e10


def make_gray_penalty(
    gray_strength: float = DEFAULT_GRAY_PENALTY,
    pale_strength: float = DEFAULT_PALE_PENALTY,
    chromatic_strength: float = 0.0,
) -> np.ndarray:
    """Return a (16,) float32 penalty vector to ADD to per-pixel distances.

    Larger values mean the corresponding palette entry needs to be that
    much closer (in squared weighted distance) than a competitor before it
    wins the argmin. Set all three to 0.0 to disable.

    chromatic_strength (default 0) penalizes every non-gray-axis index. Set
    to a very large value (GRAYSCALE_CHROMATIC_PENALTY) to force the argmin
    to only pick gray-axis entries.
    """
    p = np.zeros(16, dtype=np.float32)
    for i in GRAY_INDICES:
        p[i] = gray_strength
    for i in PALE_INDICES:
        p[i] = pale_strength
    if chromatic_strength != 0.0:
        for i in CHROMATIC_INDICES:
            p[i] = chromatic_strength
    return p


_SAT_LUT_CACHE: dict[float, np.ndarray] = {}


def _saturation_lut(factor: float) -> np.ndarray:
    """Return a 256-entry uint8 LUT mapping s → clip(s * factor, 0, 255).

    Cached per-factor so each scene's fixed saturation multiplier builds
    the table once at startup, not every frame."""
    lut = _SAT_LUT_CACHE.get(factor)
    if lut is None:
        lut = np.clip(np.arange(256, dtype=np.float32) * factor, 0, 255).astype(np.uint8)
        _SAT_LUT_CACHE[factor] = lut
    return lut


def boost_saturation(img_bgr: np.ndarray, factor: float) -> np.ndarray:
    """Multiply HSV saturation by `factor` (clipped to 0..255) in a BGR
    uint8 image. Returns BGR uint8. Identity when factor == 1.0.

    Uses a precomputed 256-entry uint8 LUT applied via cv2.LUT on the S
    channel — single pass, no float promotion of the per-pixel saturation,
    no per-call allocation for the LUT itself."""
    if factor == 1.0:
        return img_bgr
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    hsv[:, :, 1] = cv2.LUT(hsv[:, :, 1], _saturation_lut(factor))
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


# ---------------------------------------------------------------------------
# Hue-targeted correction (the global [color] hue_corrections stage)
# ---------------------------------------------------------------------------
# The C64 has exactly one purple (index 4) and it is a bright, fully-saturated
# magenta. Real-world purples are dark, blue-leaning violets, so the weighted-
# BGR (brightness-dominated) quantizer sends them to gray/blue and never to
# purple. A pre-quantization HSV pass that, for pixels in the violet→magenta
# hue band, snaps the hue toward magenta and boosts saturation + value lets
# them reach C64 purple. The same mechanism can tune any hue band; the default
# table ships only the purple rescue (the one true gap in the 16-color set).


@dataclass(frozen=True)
class HueCorrection:
    """One hue-band correction applied before quantization.

    Angles are in degrees (0..360); cv2's HSV hue (0..179) is half that. A band
    with hue_hi_deg < hue_lo_deg wraps through 360°/0°. sat_thresh / val_thresh
    (0..1) gate the band so only sufficiently-saturated, bright pixels are
    touched (keeps near-gray/near-black out). sat_mult / val_mult scale S / V.
    hue_target_deg, if set, hard-snaps the hue of matched pixels toward that
    angle. name is for logging/debug only.
    """

    hue_lo_deg: float
    hue_hi_deg: float
    sat_thresh: float = 0.0
    val_thresh: float = 0.0
    sat_mult: float = 1.0
    val_mult: float = 1.0
    hue_target_deg: float | None = None
    name: str = ""


# The one C64 colour gap worth closing by default: dark blue-violets → purple.
DEFAULT_HUE_CORRECTIONS: tuple[HueCorrection, ...] = (
    HueCorrection(
        hue_lo_deg=240.0,
        hue_hi_deg=330.0,
        sat_thresh=40.0 / 255.0,
        val_thresh=30.0 / 255.0,
        sat_mult=2.2,
        val_mult=1.9,
        hue_target_deg=300.0,
        name="purple_rescue",
    ),
)


def apply_hue_corrections(
    img_bgr: np.ndarray,
    corrections: tuple[HueCorrection, ...] | list[HueCorrection],
) -> np.ndarray:
    """Apply hue-band corrections to a BGR uint8 image; return BGR uint8.

    Empty `corrections` is an identity no-op (no HSV roundtrip) — so non-c64
    display modes, which pass (), pay nothing and render byte-for-byte as
    before. Bands are applied in list order; in practice they are hue-disjoint.
    """
    if not corrections:
        return img_bgr
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    deg = h * 2.0  # cv2 hue 0..179 → 0..359
    for c in corrections:
        if c.hue_lo_deg <= c.hue_hi_deg:
            in_band = (deg >= c.hue_lo_deg) & (deg <= c.hue_hi_deg)
        else:  # wrap through 360°/0°
            in_band = (deg >= c.hue_lo_deg) | (deg <= c.hue_hi_deg)
        mask = in_band & (s >= c.sat_thresh * 255.0) & (v >= c.val_thresh * 255.0)
        if not mask.any():
            continue
        s[mask] = np.minimum(255.0, s[mask] * c.sat_mult)
        v[mask] = np.minimum(255.0, v[mask] * c.val_mult)
        if c.hue_target_deg is not None:
            h[mask] = c.hue_target_deg / 2.0  # degrees → cv2 hue
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def parse_hue_corrections(
    raw: list[dict] | tuple[dict, ...],
) -> tuple[HueCorrection, ...]:
    """Validate a list of [[color.hue_corrections]] tables → HueCorrection tuple.

    Raises ValueError with a field-pointed message on bad input, so a malformed
    table surfaces at config-load / --doctor time rather than mid-stream.
    """
    out: list[HueCorrection] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"color.hue_corrections[{i}] must be a table, got {entry!r}")
        for req in ("hue_lo_deg", "hue_hi_deg"):
            if req not in entry:
                raise ValueError(f"color.hue_corrections[{i}] is missing required key {req!r}")
        unknown = set(entry) - {
            "hue_lo_deg",
            "hue_hi_deg",
            "sat_thresh",
            "val_thresh",
            "sat_mult",
            "val_mult",
            "hue_target_deg",
            "name",
        }
        if unknown:
            raise ValueError(
                f"color.hue_corrections[{i}] has unknown key(s): {', '.join(sorted(unknown))}"
            )
        for k in ("hue_lo_deg", "hue_hi_deg", "hue_target_deg"):
            val = entry.get(k)
            if val is not None and not (0.0 <= float(val) <= 360.0):
                raise ValueError(f"color.hue_corrections[{i}].{k} must be in 0..360, got {val}")
        for k in ("sat_thresh", "val_thresh"):
            val = entry.get(k, 0.0)
            if not (0.0 <= float(val) <= 1.0):
                raise ValueError(f"color.hue_corrections[{i}].{k} must be in 0..1, got {val}")
        for k in ("sat_mult", "val_mult"):
            val = entry.get(k, 1.0)
            if float(val) <= 0.0:
                raise ValueError(f"color.hue_corrections[{i}].{k} must be > 0, got {val}")
        out.append(
            HueCorrection(
                hue_lo_deg=float(entry["hue_lo_deg"]),
                hue_hi_deg=float(entry["hue_hi_deg"]),
                sat_thresh=float(entry.get("sat_thresh", 0.0)),
                val_thresh=float(entry.get("val_thresh", 0.0)),
                sat_mult=float(entry.get("sat_mult", 1.0)),
                val_mult=float(entry.get("val_mult", 1.0)),
                hue_target_deg=(
                    None if entry.get("hue_target_deg") is None else float(entry["hue_target_deg"])
                ),
                name=str(entry.get("name", "")),
            )
        )
    return tuple(out)


def parse_channel_boost(
    raw: list[float] | tuple[float, ...] | None,
) -> np.ndarray:
    """Validate a [color].channel_boost value → (3,) float32 BGR gain.

    Accepts a length-3 list of positive numbers in OpenCV BGR order
    ([blue, green, red]). None or an empty list falls back to the built-in
    CHANNEL_BOOST default. Raises ValueError with a field-pointed message so a
    malformed value surfaces at config-load / --doctor time.
    """
    if not raw:
        return CHANNEL_BOOST
    if len(raw) != 3:
        raise ValueError(
            f"color.channel_boost must have exactly 3 entries "
            f"[blue, green, red], got {len(raw)}: {raw!r}"
        )
    for i, v in enumerate(raw):
        if float(v) <= 0.0:
            raise ValueError(f"color.channel_boost[{i}] must be > 0, got {v}")
    return np.array([float(v) for v in raw], dtype=np.float32)


# ---------------------------------------------------------------------------
# Adaptive per-source color fit (the [color].auto_fit stage)
# ---------------------------------------------------------------------------
# The static [color] stage above (channel_boost + hue_corrections) is the same
# nudge for every video. auto_fit is its per-source adaptive sibling: a scene
# pre-scans its source (video video / slideshow image), derives a contrast
# (levels) stretch + a gentle saturation lift that expands the content to FILL
# the C64 tonal + chroma range, and the display mode applies it as the first
# shaping step. Faithful (hue preserved) — it stretches what's there, it does
# not recolor. The guards below keep it do-no-harm on already-well-exposed
# sources: percentile black/white points reject outliers, a minimum span caps
# the gain, the saturation lift is floored at 1.0 (never desaturates) and
# capped, and a 0..1 strength dial lerps the whole transform toward identity.

_AUTO_FIT_BLACK_PCT = 1.0  # luma percentile mapped to black
_AUTO_FIT_WHITE_PCT = 99.0  # luma percentile mapped to white
# Smallest black→white span we'll stretch across. Enforcing a floor caps the
# contrast gain at 255/MIN_SPAN (~8x) so a near-flat frame doesn't blow its
# sensor/compression noise up to full contrast.
_AUTO_FIT_MIN_SPAN = 32.0
_AUTO_FIT_SAT_TARGET = 110.0  # target mean HSV S (0..255) the lift aims for
_AUTO_FIT_SAT_CAP = 1.6  # never multiply saturation by more than this
_AUTO_FIT_SCAN_WIDTH = 160  # downscale width for the cheap pre-scan


@dataclass(frozen=True)
class ColorFit:
    """A per-source contrast + saturation transform derived by pre-scan.

    `black`/`white` are the luma levels (0..255) mapped to 0/255 by the stretch;
    `sat_mult` is the HSV saturation multiplier. Applied by `apply_color_fit`.
    """

    black: float
    white: float
    sat_mult: float

    def is_identity(self) -> bool:
        """True when the fit would leave a frame essentially unchanged, so the
        scene can skip installing it (and `result()` can return None)."""
        return self.black <= 1.0 and self.white >= 254.0 and self.sat_mult <= 1.01


_CONTRAST_LUT_CACHE: dict[tuple[int, int], np.ndarray] = {}


def _contrast_lut(black: float, white: float) -> np.ndarray:
    """256-entry uint8 LUT mapping i → clip((i-black)*255/(white-black)).

    Cached per (black, white) so a video's fixed fit builds the table once, not
    per frame (mirrors `_saturation_lut`). Identical for all three channels, so
    a single cv2.LUT pass over a BGR image preserves hue (uniform scale+offset)."""
    key = (int(round(black)), int(round(white)))
    lut = _CONTRAST_LUT_CACHE.get(key)
    if lut is None:
        span = max(float(key[1] - key[0]), 1.0)
        lut = np.clip((np.arange(256, dtype=np.float32) - key[0]) * (255.0 / span), 0, 255).astype(
            np.uint8
        )
        _CONTRAST_LUT_CACHE[key] = lut
    return lut


def apply_color_fit(img_bgr: np.ndarray, fit: ColorFit) -> np.ndarray:
    """Apply a ColorFit to a BGR uint8 image; return BGR uint8.

    Contrast stretch (all channels, hue-preserving) then saturation lift. An
    identity fit is a no-op. Cheap: a cached LUT plus the boost_saturation LUT.
    """
    if fit.is_identity():
        return img_bgr
    out = cv2.LUT(img_bgr, _contrast_lut(fit.black, fit.white))
    return boost_saturation(out, fit.sat_mult)


class ColorFitAccumulator:
    """Accumulates luma + saturation stats over sampled source frames, then
    derives a `ColorFit`. One global histogram → one stable transform per
    source (no per-frame flicker). `strength` (0..1) scales the result toward
    identity for A/B and gentler looks."""

    def __init__(self, strength: float = 1.0):
        self._strength = float(np.clip(strength, 0.0, 1.0))
        self._hist = np.zeros(256, dtype=np.int64)
        self._sat_sum = 0.0
        self._sat_n = 0

    def add(self, img_bgr: np.ndarray) -> None:
        """Fold one frame/image into the running stats (downscaled for speed)."""
        h, w = img_bgr.shape[:2]
        if w > _AUTO_FIT_SCAN_WIDTH:
            new_h = max(1, h * _AUTO_FIT_SCAN_WIDTH // w)
            small = cv2.resize(img_bgr, (_AUTO_FIT_SCAN_WIDTH, new_h), interpolation=cv2.INTER_AREA)
        else:
            small = img_bgr
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        self._hist += np.bincount(gray.ravel(), minlength=256)
        s = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1]
        self._sat_sum += float(s.sum())
        self._sat_n += s.size

    def result(self) -> ColorFit | None:
        """Derive the ColorFit; None when there's nothing to do (no samples, or
        the transform reduces to identity — e.g. strength 0 or already full-range)."""
        total = int(self._hist.sum())
        if total == 0 or self._strength <= 0.0:
            return None
        cdf = np.cumsum(self._hist)
        black = float(np.searchsorted(cdf, total * _AUTO_FIT_BLACK_PCT / 100.0))
        white = float(np.searchsorted(cdf, total * _AUTO_FIT_WHITE_PCT / 100.0))
        white = min(255.0, black + max(white - black, _AUTO_FIT_MIN_SPAN))
        mean_s = (self._sat_sum / self._sat_n) if self._sat_n else 0.0
        sat_mult = (
            float(np.clip(_AUTO_FIT_SAT_TARGET / mean_s, 1.0, _AUTO_FIT_SAT_CAP))
            if mean_s > 1.0
            else 1.0
        )
        # Lerp toward identity by strength.
        st = self._strength
        black *= st
        white = 255.0 - (255.0 - white) * st
        sat_mult = 1.0 + (sat_mult - 1.0) * st
        fit = ColorFit(black=black, white=white, sat_mult=sat_mult)
        return None if fit.is_identity() else fit


# ---------------------------------------------------------------------------
# Forced-palette remap (the [color].force_palette "extreme" stage)
# ---------------------------------------------------------------------------
# The stages above (auto_fit, channel_boost, hue_corrections) are all FAITHFUL —
# they stretch/nudge the source but every pixel still maps to its nearest of the
# fixed 16 C64 colors. A source that clusters in one gamut region (TRON = black +
# dark blue) therefore leaves most of the 16 colors unused and renders nearly
# monochromatic. The forced-palette remap is a deliberate FALSE-COLOR pre-stage:
# it k-means the source into N clusters (in perceptual Lab space), assigns each
# cluster to a DISTINCT C64 color via an optimal (min total Lab error) bijection,
# and routes every pixel cluster→assigned color. Result: all N colors are used
# and the assignment is consistent across the whole source (no per-frame
# flicker). Opt-in only, never a default — it does not preserve the source's true
# colors. Once clusters are assigned to specific indices, the remap BYPASSES the
# faithful shaping stages (re-nudging hues would fight the assignment); it feeds
# the existing nearest-palette quantizer + per-cell slot-picker unchanged by
# emitting an image whose pixels are already exact C64 palette colors.
#
# Per-frame cost is a single LUT gather (see ColorMap.apply); the only real work
# (pre-scan + k-means + assignment + LUT bake) happens once per source.

_FORCE_PALETTE_BINS = 32  # per-axis BGR bins for the bake-once 3D LUT
_FORCE_PALETTE_SHIFT = 3  # 8 - log2(bins): BGR byte → bin index
_FORCE_PALETTE_SAMPLE_CAP = 60000  # max Lab pixels fed to k-means
_FORCE_PALETTE_PER_FRAME = 2000  # pixels sampled per added frame/image
_FORCE_PALETTE_SCAN_WIDTH = _AUTO_FIT_SCAN_WIDTH


def _hungarian(cost: np.ndarray) -> np.ndarray:
    """Min-cost perfect assignment on a SQUARE cost matrix.

    Returns an int array `col` where row i is assigned column col[i], minimizing
    the total cost. Classic O(n³) Kuhn-Munkres (potentials method) — no scipy in
    this project, and n ≤ 16 so the cubic cost is trivial. Runs once per source.
    """
    n = cost.shape[0]
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)  # p[j] = row matched to column j (1-indexed)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = -1
            for j in range(1, n + 1):
                if not used[j]:
                    cur = float(cost[i0 - 1, j - 1]) - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    col = np.zeros(n, dtype=np.int64)
    for j in range(1, n + 1):
        col[p[j] - 1] = j - 1
    return col


def _palette_lab() -> np.ndarray:
    """The 16 C64 colors in CIE-Lab (float32, (16, 3)) for perceptual distance."""
    bgr = C64_PALETTE_BGR.astype(np.uint8).reshape(16, 1, 3)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).reshape(16, 3).astype(np.float32)


_PALETTE_LAB = _palette_lab()

# Expansion-trick precompute for the Lab nearest-palette distance, mirroring
# the weighted-BGR path (_WPAL / _PAL_NORMSQ). d²(x, p) = |x|² - 2·x·p + |p|²
# lets a single (N, 3) @ (3, 16) matmul replace the (N, 16, 3) broadcast tensor.
_PAL_LAB_T = _PALETTE_LAB.T.copy()  # (3, 16)
_PAL_LAB_NORMSQ = (_PALETTE_LAB**2).sum(axis=1)  # (16,)

# ---------------------------------------------------------------------------
# Perceptual (CIE-Lab) nearest-palette matching — the [color].color_match
# "perceptual" path.
# ---------------------------------------------------------------------------
# The default quantizer (quantize_distances above) measures nearest-color in a
# weighted BGR space (DISTANCE_WEIGHTS [2, 4, 3]) that is brightness-dominated:
# a warm mid-gray skin pixel lands closer to a gray-axis entry than to orange/
# brown. CIE-Lab is perceptually near-uniform, so the nearest-Lab match picks
# the color the eye would actually call closest — the accuracy win of the
# perceptual path is a better hue decision among the candidate colors.
#
# The perceptual path swaps ONLY the distance space: the channel_boost + gray
# penalty shaping still applies. Those two aren't just weighted-BGR crutches —
# the gray penalty keeps a flat desaturated region (a pale sky) from fragmenting
# into gray under the accurate-but-drab Lab match, and channel_boost holds the
# C64-friendly hues; dropping them (an earlier revision) measurably regressed
# flat regions on real hardware. See modes.py.
#
# Distances are in OpenCV 8-bit Lab units (L, a, b each on a 0..255 scale), so a
# perceptual d² is ~1/3 the magnitude of a weighted-BGR d² for the same physical
# color gap (unweighted 3-channel vs weight-sum-9). Callers that add d²-space
# biases/thresholds under the Lab metric (modes.py's gray penalty + percell
# hysteresis) scale them by this factor to preserve their tuned strength.
PERCEPTUAL_DIST_SCALE = 1.0 / 3.0  # approx d²_lab / d²_weighted_bgr for equal gaps

COLOR_MATCH_MODES: tuple[str, ...] = ("rgb", "perceptual")

# Per-cell 3-color selection strategies for the mhires percell path (the
# [color].cell_strategy knob). The canonical list lives here (a lightweight,
# already-color-adjacent module) so config.py can validate against it without
# importing the heavy modes module; modes._pick_cell_colors implements them.
CELL_STRATEGIES: tuple[str, ...] = ("frequency", "luminance", "contrast", "error-min")


def _bgr_to_lab(flat_pixels: np.ndarray) -> np.ndarray:
    """Convert (N, 3) float32 BGR pixels (0..255) to (N, 3) float32 Lab.

    OpenCV's 8-bit Lab conversion needs uint8 input, so the float pixels are
    clipped + rounded first — sub-integer precision is irrelevant to a
    nearest-of-16 decision (the source pixels are uint8-derived anyway)."""
    u8 = np.clip(flat_pixels, 0, 255).astype(np.uint8).reshape(-1, 1, 3)
    return cv2.cvtColor(u8, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)


def quantize_distances_lab(flat_pixels: np.ndarray) -> np.ndarray:
    """Return per-pixel squared CIE-Lab distance to each of the 16 palette colors.

    flat_pixels: (N, 3) float32 BGR (0..255). Returns (N, 16) float32. The
    perceptual sibling of quantize_distances; downstream compose logic (argmin,
    bg/fg picks, dither candidate resolution) is metric-agnostic and works on
    this matrix unchanged."""
    lab = _bgr_to_lab(flat_pixels)
    px_normsq = (lab**2).sum(axis=1)  # (N,)
    cross = lab @ _PAL_LAB_T  # (N, 16)
    return px_normsq[:, None] - 2.0 * cross + _PAL_LAB_NORMSQ[None, :]


def quantize_distances_for(flat_pixels: np.ndarray, *, perceptual: bool) -> np.ndarray:
    """Dispatch to the Lab or weighted-BGR (N, 16) distance matrix by metric."""
    return quantize_distances_lab(flat_pixels) if perceptual else quantize_distances(flat_pixels)


def quantize_flat_for(flat_pixels: np.ndarray, *, perceptual: bool) -> np.ndarray:
    """Nearest-palette index per pixel in the selected metric. (N, 3) → (N,)."""
    return np.argmin(quantize_distances_for(flat_pixels, perceptual=perceptual), axis=1)


@dataclass(frozen=True)
class ColorMap:
    """A baked forced-palette remap: source BGR → a fixed set of distinct C64
    colors. `lut` is a (bins, bins, bins) uint8 array mapping a coarse-quantized
    BGR cell to a C64 palette index; `shift` turns a BGR byte into its bin index;
    `indices` is the distinct C64 indices the source was mapped onto (logging).
    Apply is a pure gather — cheap enough to run every frame."""

    lut: np.ndarray
    shift: int
    indices: tuple[int, ...]

    def apply(self, img_bgr: np.ndarray) -> np.ndarray:
        """Remap a BGR uint8 image to exact C64 palette colors (BGR uint8).

        Each pixel is binned, looked up in the LUT for its assigned palette
        index, and replaced with that palette color. Downstream
        quantize_distances then resolves it to the same index for free, so the
        per-cell slot-picker sees the forced color set with no other changes."""
        b = (img_bgr >> self.shift).astype(np.intp)
        idx = self.lut[b[..., 0], b[..., 1], b[..., 2]]
        return C64_PALETTE_BGR[idx].astype(np.uint8)


class ColorMapAccumulator:
    """Accumulates Lab pixel samples over sampled source frames/images, then
    derives a `ColorMap` by k-means + optimal palette assignment. One global
    sample set → one stable remap per source (no per-frame flicker).

    `n_colors` is the number of distinct C64 colors to spread across (clusters);
    `indices`, if given, is an explicit C64 index whitelist (its length sets the
    cluster count and the candidate set). Uniform `.add()` interface mirrors
    `ColorFitAccumulator` so a single source decode pass can feed both."""

    def __init__(self, n_colors: int = 16, indices: list[int] | None = None):
        if indices:
            self._candidates = [int(i) & 0x0F for i in indices]
            self._k = len(self._candidates)
        else:
            self._candidates = list(range(16))
            self._k = int(np.clip(n_colors, 2, 16))
        self._samples: list[np.ndarray] = []
        self._total = 0

    def lab_samples(self) -> np.ndarray:
        """The accumulated Lab pixel reservoir as one (N, 3) float32 array (empty
        when nothing was added). Exposed for `suggest_palette` (the "best
        faithful subset" discovery command), which wants the raw samples rather
        than the k-means/assignment result `result()` derives from them."""
        if not self._samples:
            return np.zeros((0, 3), dtype=np.float32)
        return np.concatenate(self._samples)

    def add(self, img_bgr: np.ndarray) -> None:
        """Fold one frame/image's pixels into the running Lab sample reservoir."""
        if self._total >= _FORCE_PALETTE_SAMPLE_CAP:
            return
        h, w = img_bgr.shape[:2]
        if w > _FORCE_PALETTE_SCAN_WIDTH:
            new_h = max(1, h * _FORCE_PALETTE_SCAN_WIDTH // w)
            small = cv2.resize(
                img_bgr, (_FORCE_PALETTE_SCAN_WIDTH, new_h), interpolation=cv2.INTER_AREA
            )
        else:
            small = img_bgr
        lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).reshape(-1, 3)
        n = lab.shape[0]
        if n > _FORCE_PALETTE_PER_FRAME:
            stride = max(1, n // _FORCE_PALETTE_PER_FRAME)
            lab = lab[::stride]
        self._samples.append(lab.astype(np.float32))
        self._total += lab.shape[0]

    def result(self) -> ColorMap | None:
        """Derive the ColorMap; None when there's nothing to cluster."""
        if not self._samples:
            return None
        samples = np.concatenate(self._samples)
        # k can't exceed the number of distinct samples (cv2.kmeans requires it).
        uniq = np.unique(samples, axis=0)
        k = int(min(self._k, uniq.shape[0]))
        if k < 1:
            return None
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
        # bestLabels=None is the documented "let OpenCV allocate" form; the cv2
        # stubs type it as a required MatLike, so suppress the false positive.
        _compactness, _labels, centers = cv2.kmeans(
            samples,
            k,
            None,  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
            criteria,
            3,
            cv2.KMEANS_PP_CENTERS,
        )
        centers = centers.astype(np.float32)  # (k, 3) Lab

        # Optimal cluster→distinct-C64-color bijection (min total Lab error).
        cand = self._candidates
        cand_lab = _PALETTE_LAB[cand]  # (C, 3)
        diff = centers[:, None, :] - cand_lab[None, :, :]  # (k, C, 3)
        cost = (diff * diff).sum(axis=2)  # (k, C)
        c = len(cand)
        if k < c:  # pad to square
            square = np.zeros((c, c), dtype=np.float32)
            square[:k] = cost
            assign_col = _hungarian(square)[:k]
        else:
            assign_col = _hungarian(cost)
        assigned = np.array([cand[j] for j in assign_col], dtype=np.uint8)  # (k,)

        lut = self._bake_lut(centers, assigned)
        used = tuple(int(x) for x in np.unique(assigned))
        return ColorMap(lut=lut, shift=_FORCE_PALETTE_SHIFT, indices=used)

    @staticmethod
    def _bake_lut(centers_lab: np.ndarray, assigned: np.ndarray) -> np.ndarray:
        """Bake the (bins³) BGR→palette-index LUT: each bin center → nearest
        cluster (in Lab) → that cluster's assigned C64 index."""
        bins = _FORCE_PALETTE_BINS
        step = 256 // bins
        grid = np.arange(bins, dtype=np.uint8) * step + step // 2
        bb, gg, rr = np.meshgrid(grid, grid, grid, indexing="ij")
        centers_bgr = np.stack([bb, gg, rr], axis=-1).reshape(-1, 1, 3)
        bin_lab = (
            cv2.cvtColor(centers_bgr.astype(np.uint8), cv2.COLOR_BGR2LAB)
            .reshape(-1, 3)
            .astype(np.float32)
        )
        diff = bin_lab[:, None, :] - centers_lab[None, :, :]  # (bins³, k, 3)
        d = (diff * diff).sum(axis=2)  # (bins³, k)
        nearest = d.argmin(axis=1)  # (bins³,)
        return assigned[nearest].reshape(bins, bins, bins)


# Rolling (live-source) force_palette tuning. The window holds ~this many
# per-frame Lab blocks; at the worker's ~1 Hz sampling that's a ~30 s memory,
# and 30 × _FORCE_PALETTE_PER_FRAME ≈ the one-shot 60k cap. ROLLING_HYSTERESIS
# is the fractional Lab-error improvement the OPTIMAL cluster→C64-index bijection
# must beat the previous one by before the palette re-assigns (stability bias,
# same idea as the mhires percell hysteresis).
_ROLLING_WINDOW_BLOCKS = 30
ROLLING_HYSTERESIS = 0.1


class RollingColorMapAccumulator:
    """Sliding-window sibling of `ColorMapAccumulator` for LIVE sources (webcam,
    wled sink, generative) that can't pre-scan a whole file up front.

    Keeps a bounded deque of recent per-frame Lab sample blocks and re-derives a
    `ColorMap` on demand, with two stability treatments so the forced palette
    adapts to changing content WITHOUT popping — the real problem with a live
    force_palette (CPU is a non-issue at ~1 Hz re-bakes on a worker thread):

      * **warm-start k-means** — seed each bake from the previous centers
        (`cv2.KMEANS_USE_INITIAL_LABELS`, initial labels = nearest previous
        center per sample) so cluster identity stays stable frame-to-frame
        instead of being re-randomized by `++` seeding;
      * **assignment hysteresis** — keep the previous cluster→C64-index bijection
        unless the optimal one improves total Lab error by more than
        `ROLLING_HYSTERESIS`, so a tiny center drift doesn't reshuffle which C64
        colors the source maps onto (mirrors the percell hysteresis philosophy).

    `add(img)` folds one frame (the deque evicts the oldest past the window, so
    the map tracks the last ~window frames). `clear()` drops the window + warm-
    start state — call on a detected shot cut so the new shot's palette is
    derived fresh (and the swap is hidden by the cut). `result()` bakes the
    current `ColorMap` (None until there's something to cluster). Not
    thread-safe: `RollingForcePalette` owns one on a single worker thread."""

    def __init__(
        self,
        n_colors: int = 16,
        indices: list[int] | None = None,
        window_blocks: int = _ROLLING_WINDOW_BLOCKS,
    ):
        if indices:
            self._candidates = [int(i) & 0x0F for i in indices]
            self._k = len(self._candidates)
        else:
            self._candidates = list(range(16))
            self._k = int(np.clip(n_colors, 2, 16))
        self._blocks: deque[np.ndarray] = deque(maxlen=max(1, window_blocks))
        self._prev_centers: np.ndarray | None = None  # (k, 3) warm-start seed
        self._prev_col: np.ndarray | None = None  # (k,) previous cluster→candidate-pos map

    def add(self, img_bgr: np.ndarray) -> None:
        """Fold one frame's pixels into the rolling Lab window (downscale → Lab →
        subsample to ≤ _FORCE_PALETTE_PER_FRAME points; the deque bounds total)."""
        h, w = img_bgr.shape[:2]
        if w > _FORCE_PALETTE_SCAN_WIDTH:
            new_h = max(1, h * _FORCE_PALETTE_SCAN_WIDTH // w)
            small = cv2.resize(
                img_bgr, (_FORCE_PALETTE_SCAN_WIDTH, new_h), interpolation=cv2.INTER_AREA
            )
        else:
            small = img_bgr
        lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).reshape(-1, 3)
        n = lab.shape[0]
        if n > _FORCE_PALETTE_PER_FRAME:
            stride = max(1, n // _FORCE_PALETTE_PER_FRAME)
            lab = lab[::stride]
        self._blocks.append(lab.astype(np.float32))

    def clear(self) -> None:
        """Drop the sample window + warm-start/hysteresis state (shot-cut reset)."""
        self._blocks.clear()
        self._prev_centers = None
        self._prev_col = None

    def _kmeans(self, samples: np.ndarray, k: int) -> np.ndarray:
        """k-means centers (k, 3) Lab — warm-started from the previous bake's
        centers when the cluster count matches, else `++`-seeded."""
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
        prev = self._prev_centers
        if prev is not None and prev.shape[0] == k:
            # Initial labels = nearest previous center per sample (expansion-trick
            # distance, no (N, k, 3) tensor). Warm-start keeps cluster i ≈ the
            # previous cluster i, which is what makes the bijection hysteresis
            # below meaningful across bakes.
            px_normsq = (samples**2).sum(axis=1)
            cross = samples @ prev.T  # (N, k)
            prev_normsq = (prev**2).sum(axis=1)
            d = px_normsq[:, None] - 2.0 * cross + prev_normsq[None, :]
            labels0 = d.argmin(axis=1).astype(np.int32).reshape(-1, 1)
            _c, _l, centers = cv2.kmeans(
                samples, k, labels0, criteria, 1, cv2.KMEANS_USE_INITIAL_LABELS
            )
        else:
            _c, _l, centers = cv2.kmeans(
                samples,
                k,
                None,  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
                criteria,
                3,
                cv2.KMEANS_PP_CENTERS,
            )
        return centers.astype(np.float32)

    def _bijection(self, centers: np.ndarray) -> np.ndarray:
        """Cluster→candidate-position assignment (k,), min-Lab-error Hungarian,
        but sticky: keep the previous bijection unless the optimal one beats it
        by more than ROLLING_HYSTERESIS in total Lab error."""
        cand_lab = _PALETTE_LAB[self._candidates]  # (C, 3)
        diff = centers[:, None, :] - cand_lab[None, :, :]  # (k, C, 3)
        cost = (diff * diff).sum(axis=2)  # (k, C)
        k, c = centers.shape[0], len(self._candidates)
        if k < c:  # pad to square
            square = np.zeros((c, c), dtype=np.float32)
            square[:k] = cost
            optimal = _hungarian(square)[:k]
        else:
            optimal = _hungarian(cost)
        prev = self._prev_col
        if prev is not None and prev.shape[0] == k:
            rows = np.arange(k)
            reuse_cost = float(cost[rows, prev].sum())
            opt_cost = float(cost[rows, optimal].sum())
            # Switch only if the optimal bijection improves by more than the
            # margin; otherwise the previous C64-color mapping stays (no pop).
            if opt_cost >= reuse_cost * (1.0 - ROLLING_HYSTERESIS):
                return prev
        return optimal

    def result(self) -> ColorMap | None:
        """Bake the current rolling `ColorMap` (None until there's a sample)."""
        if not self._blocks:
            return None
        samples = np.concatenate(self._blocks)
        uniq = np.unique(samples, axis=0)
        k = int(min(self._k, uniq.shape[0]))
        if k < 1:
            return None
        centers = self._kmeans(samples, k)
        assign_col = self._bijection(centers)
        self._prev_centers = centers
        self._prev_col = assign_col
        assigned = np.array([self._candidates[j] for j in assign_col], dtype=np.uint8)
        lut = ColorMapAccumulator._bake_lut(centers, assigned)
        used = tuple(int(x) for x in np.unique(assigned))
        return ColorMap(lut=lut, shift=_FORCE_PALETTE_SHIFT, indices=used)


def build_fixed_color_map(indices: Sequence[int]) -> ColorMap | None:
    """Build a forced-palette ``ColorMap`` from an explicit set of C64 indices.

    Unlike ``ColorMapAccumulator`` (which derives the color set by clustering a
    source pre-scan), this maps every BGR bin to the *nearest of the given
    indices* in Lab — no samples needed. Used by the WLED control surface (Mode
    1), where the color picker chooses the forced color set directly. Returns
    None for an empty index set. Duplicate/out-of-range indices are deduped and
    masked to 0..15 (preserving first-seen order for a stable `indices` tuple)."""
    seen: list[int] = []
    for i in indices:
        v = int(i) & 0x0F
        if v not in seen:
            seen.append(v)
    if not seen:
        return None
    centers_lab = _PALETTE_LAB[seen]  # (k, 3), each chosen index maps to itself
    assigned = np.array(seen, dtype=np.uint8)
    lut = ColorMapAccumulator._bake_lut(centers_lab, assigned)
    return ColorMap(lut=lut, shift=_FORCE_PALETTE_SHIFT, indices=tuple(sorted(seen)))


def suggest_palette(samples_lab: np.ndarray, max_k: int = 16) -> list[tuple[int, float]]:
    """Rank the 16 C64 colors by how well a *faithful* subset represents a
    source's colors (greedy facility-location / k-medoids over the fixed
    palette).

    Unlike ``ColorMapAccumulator`` — which k-means-clusters the source and
    spreads the clusters onto DISTINCT colors (a deliberate false-color remap) —
    this answers "which C64 colors would the source's pixels map *nearest* to,
    in order of value" WITHOUT remapping. Each greedy step adds the one unchosen
    C64 color that most reduces the total nearest-color Lab error over all
    samples, so the selection *order* is a value ranking: the top-k is the best
    k-color faithful subset, and its `(1 − 1/e)` approximation guarantee holds
    for the monotone-submodular coverage objective.

    ``samples_lab`` is an (N, 3) float32 array of CIE-Lab pixels (e.g.
    ``ColorMapAccumulator.lab_samples()``). Returns a list of
    ``(c64_index, mean_lab_error)`` in selection order, length ≤ ``max_k``; the
    mean error is the average per-sample distance to the nearest chosen color
    *after* adding that color (monotonically decreasing — its knee shows where
    extra colors stop helping). Empty input → empty list. Cost is O(16·k·N)
    (~16M flops at the 60k sample cap), a sub-100 ms one-shot analysis."""
    if samples_lab.size == 0:
        return []
    lab = samples_lab.astype(np.float32, copy=False).reshape(-1, 3)
    # (N, 16) Lab distance from every sample to every palette color (expansion
    # trick, same as quantize_distances_lab but the samples are already Lab).
    px_normsq = (lab**2).sum(axis=1)  # (N,)
    cross = lab @ _PAL_LAB_T  # (N, 16)
    dist = np.sqrt(np.maximum(px_normsq[:, None] - 2.0 * cross + _PAL_LAB_NORMSQ[None, :], 0.0))
    n = lab.shape[0]
    k = int(min(max_k, 16))
    nearest = np.full(n, np.inf, dtype=np.float32)  # current min dist per sample
    chosen: list[tuple[int, float]] = []
    remaining = list(range(16))
    for _ in range(k):
        best_idx = -1
        best_cost = np.inf
        best_nearest: np.ndarray | None = None
        for c in remaining:
            cand_nearest = np.minimum(nearest, dist[:, c])
            cost = float(cand_nearest.mean())
            if cost < best_cost:
                best_cost, best_idx, best_nearest = cost, c, cand_nearest
        if best_idx < 0 or best_nearest is None:
            break
        chosen.append((best_idx, best_cost))
        nearest = best_nearest
        remaining.remove(best_idx)
    return chosen


def nearest_palette_index(rgb: Sequence[int]) -> int:
    """Snap an ``[R, G, B]`` triple (0..255, e.g. a WLED color slot) to the
    perceptually nearest C64 palette index (CIE-Lab). Extra channels (a WLED
    ``W`` slot) are ignored; short/empty input falls back to black."""
    if rgb is None or len(rgb) < 3:
        return 0
    r, g, b = (int(rgb[0]) & 0xFF, int(rgb[1]) & 0xFF, int(rgb[2]) & 0xFF)
    bgr = np.array([[b, g, r]], dtype=np.float32)  # palette is BGR order
    return int(quantize_flat_for(bgr, perceptual=True)[0])


def _compute_palette_hues() -> np.ndarray:
    """Per-palette hue angle in degrees, NaN for gray-axis entries."""
    hues = np.full(16, np.nan, dtype=np.float32)
    for i in range(16):
        bgr = C64_PALETTE_BGR[i].astype(np.uint8).reshape(1, 1, 3)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[0, 0]
        # cv2 HSV: H in 0..179, S in 0..255. Low saturation = no meaningful hue.
        if hsv[1] >= 20:
            hues[i] = float(hsv[0]) * 2.0  # scale to 0..359
    return hues


_PALETTE_HUES_DEG = _compute_palette_hues()


def _hue_gap(a: float, b: float) -> float:
    """Shortest angular distance between two hues in degrees (0..180)."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def pick_diverse_top_n(counts: np.ndarray, n: int, min_hue_gap_deg: float = 45.0) -> list[int]:
    """Pick `n` palette indices favoring hue diversity AMONG populated entries.

    Diversity is a tie-breaker, not an override. An unpopulated palette
    index never wins over a populated one — picking a color that contributes
    zero pixels to the frame is strictly worse than picking even a slightly
    similar hue that actually appears.

    Always picks the most-populated index first (the frame's dominant color
    reproduces faithfully). For each subsequent slot:
      1. Among populated remaining entries (count > 0), prefer the most-
         populated chromatic one whose hue is at least `min_hue_gap_deg`
         away from every already-chosen chromatic pick.
      2. If no populated candidate qualifies, fall back to the most-
         populated remaining entry (chromatic or gray, no diversity check).
      3. Only after all populated entries are exhausted, fill the remaining
         slots from the unpopulated tail of the argsort order, so n slots
         always get filled even on a single-color frame.

    counts: (16,) int — typically np.bincount of nearest-palette indices.
    """
    counts = np.asarray(counts)
    order = [int(i) for i in np.argsort(counts)[::-1]]  # most → least
    populated = [i for i in order if counts[i] > 0]

    if not populated:
        # Degenerate (zero pixels). Just return the argsort order.
        return order[:n]

    chosen: list[int] = [populated.pop(0)]
    while len(chosen) < n:
        pick: int | None = None
        # 1: diversity search over populated chromatic entries.
        for cand in populated:
            cand_h = _PALETTE_HUES_DEG[cand]
            if np.isnan(cand_h):
                continue
            ok = True
            for c in chosen:
                c_h = _PALETTE_HUES_DEG[c]
                if np.isnan(c_h):
                    continue
                if _hue_gap(cand_h, c_h) < min_hue_gap_deg:
                    ok = False
                    break
            if ok:
                pick = cand
                break
        # 2: most-populated remaining (no diversity check).
        if pick is None and populated:
            pick = populated[0]
        # 3: dip into zero-count entries to reach n.
        if pick is None:
            tail = [i for i in order if i not in chosen]
            if not tail:
                break
            pick = tail[0]
        chosen.append(int(pick))
        if pick in populated:
            populated.remove(pick)
    return chosen
