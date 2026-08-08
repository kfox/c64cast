"""Shared machinery for the DisplayMode hierarchy: the compose-buffer
TypedDicts, the cell-color pickers and palette-mode shaping helpers, the
live-tunable pick/hysteresis knobs, and the DisplayMode base class."""

from __future__ import annotations

import itertools
import logging
from typing import TypedDict

import numpy as np

from c64cast.hw.backend import C64Backend
from c64cast.scenes.text_surface import TextSurface
from c64cast.video.dither import bayer_offset, blue_noise_offset
from c64cast.video.palette import (
    CELL_STRATEGIES,
    DEFAULT_HUE_CORRECTIONS,
    GRAYSCALE_CHROMATIC_PENALTY,
    PALETTE_LUMA,
    ColorFit,
    ColorMap,
    HueCorrection,
    make_gray_penalty,
    parse_channel_boost,
    parse_hue_corrections,
)

# Both are pure additive (h, w) offsets with the same strength semantics
# (see dither.py) — dispatch table for the three compose() call sites below
# rather than duplicating the ordered/blue_noise branch three times.
ORDERED_DITHER_OFFSET_FNS = {"ordered": bayer_offset, "blue_noise": blue_noise_offset}


class ComposeBuffers(TypedDict):
    """The screen + color RAM buffers a char-mode display's ``compose()``
    produces and ``push()`` (plus overlay ``compose()``) consume. Each is a
    length-1000 uint8 numpy array, one byte per 40×25 cell. Named so the
    'screen'/'color' string keys stop being repeated as bare literals across
    the display modes and every PAINTS_INTO_BUFFERS overlay.

    ``text`` is the backend-neutral surface buffer-painting overlays write text
    into (see text_surface.TextSurface). Char modes wrap their screen/color
    arrays in a CharTextSurface; bitmap modes provide a glyph-folding surface.
    Every mode that hosts text overlays populates it."""

    screen: np.ndarray
    color: np.ndarray
    text: TextSurface


class MCMComposeBuffers(ComposeBuffers):
    """MCM adds `bg`: a 3-element array of bg0/bg1/bg2 palette indices that
    MCMDisplayMode.compose() hands to its own push() for the $D020-$D023
    register write. A separate type (rather than a NotRequired field on
    ComposeBuffers) so MCM's push can read buffers['bg'] without a
    possibly-missing-key warning, while other modes' buffers stay just
    screen+color."""

    bg: np.ndarray


class BitmapComposeBuffers(TypedDict):
    """The buffers a bitmap display's ``compose()`` produces and ``push()``
    consumes. ``bitmap`` is the 8000-byte VIC bitmap, ``screen`` the 1000-byte
    screen matrix (per-cell color nibbles), ``bg`` the global bg0/border
    palette index, ``text`` the glyph-folding surface overlays paint into.
    Overlay text is folded into ``bitmap``/``screen`` before push (so it rides
    the same host-DMA or REU bank-swap path as the frame)."""

    bitmap: np.ndarray
    screen: np.ndarray
    bg: int
    text: TextSurface


class MHiresComposeBuffers(BitmapComposeBuffers):
    """MultiHires adds ``color``: the 1000-byte color RAM (per-cell c3). The
    text surface reserves c1/c2 (screen nibbles) for an opaque text box, so it
    leaves color RAM to the frame."""

    color: np.ndarray


# grayscale palette_mode uses fixed slot assignments (no per-frame picking)
# in luminance order. Two reasons:
#   1. Slot 0..N maps to ascending luminance, so the bitmap stays a stable
#      "darkest-to-brightest" intensity LUT regardless of frame content.
#   2. Adaptive top-N picking flips the slot order whenever per-frame
#      counts shuffle (which they do constantly on a real webcam — the
#      low-count gray indices tie-break differently across frames). Each
#      reorder remaps every pixel to a different slot in the 8 KB bitmap,
#      busting the delta cache and forcing a full re-upload per frame.
# MCM has 3 bg slots; FG ∈ {0, 1} (color RAM bit 3 is the multicolor flag
# and the gray-axis entries below 8 are black and white), so FG covers the
# extremes and the bgs cover the mid-tones for full 5-level coverage.
# MHires has 4 global slots and no per-cell FG, so the slots include black
# plus the three mid/light grays — pure white (palette 1) is dropped in
# favor of better mid-tone resolution where webcam content lives.
GRAYSCALE_MHIRES_SLOTS = (0, 11, 12, 15)  # black, dark gray, gray, light gray
GRAYSCALE_MCM_BGS = (11, 12, 15)  # dark gray, gray, light gray

# EMA weight on the new frame's palette counts when picking the global color
# slots for cheap/vivid modes. Raw per-frame counts shuffle constantly (a
# couple of pixels at a chromatic-vs-gray boundary is enough to swap which
# entries are in the top-4), and the slot ORDER coming out of argsort/diversity
# directly drives screen + color RAM + bg-register writes, producing a visible
# palette flash on every borderline reshuffle. The 0.25 weight smooths the
# counts over ~4 frames — fast enough to track real scene changes, slow enough
# to filter the borderline jitter. Picked slots are then sorted by palette
# index so the same SET always lands in the same slot ORDER regardless of
# count ranking, giving the bitmap delta cache something stable to hit.
PALETTE_PICK_EMA_ALPHA = 0.25

# Per-cell EMA weight for the percell mhires path. Each 4×8 cell has only
# 32 pixels, so its per-frame palette histogram is an order of magnitude
# noisier than the global one — a couple of pixels flipping at a
# chromatic-vs-gray boundary (webcam sensor noise on a flat region) was
# enough to swap which palette entry won the 3rd top-3 slot, which rewrites
# the cell's screen-RAM byte + color-RAM byte AND remaps every pixel in
# that cell's 8 bitmap bytes (the codes resolve against {bg0, c1, c2, c3}
# and the SET just changed). With 0.15 (≈7-frame time constant) the picks
# stay sticky until real content change dominates the noise, but still
# converge inside ~120 ms — fast enough that motion doesn't smear.
PERCELL_PICK_EMA_ALPHA = 0.15

# Bitmap-code hysteresis bonus for the percell path, in d² space (same
# units quantize_distances returns). Even with stable per-cell {bg0, c1,
# c2, c3}, pixels sitting at a chromatic boundary between two of the four
# candidates flip code every frame from sensor noise — the most-flickery
# cells in the long-capture profile had 80-90 % bitmap-byte transition
# rates with ZERO screen+color RAM changes, i.e. pure per-pixel code
# oscillation. A pixel "keeps" its previous code as long as it's within
# this bonus of the current frame's minimum-distance code. 5000 ≈ √5000
# ≈ 71 in L2 BGR space — strong enough to suppress webcam sensor noise
# on textured static subjects, weak enough that real color changes still
# flip the code on the next frame.
PERCELL_CODE_HYSTERESIS_BONUS = 5000.0

# Per-pixel palette-index hysteresis for the percell path. Each pixel's
# argmin over the 16-entry palette can flip frame-to-frame when sensor
# noise + downsample aliasing on a textured static subject shifts it
# across a chromatic boundary. The bitmap-code hysteresis below
# (PERCELL_CODE_HYSTERESIS_BONUS) only operates in the cell's 4-entry
# {bg0, c1, c2, c3} space *after* top-3 picks — so when the unstable
# argmin pushes the cell's histogram around, top-3 picks shift and the
# cand-changed gate disables the code hysteresis, defeating it.
#
# Stabilizing the per-pixel argmin upstream means per-cell histograms
# stay stable, top-3 picks stay stable, cand stays stable, and the code
# hysteresis stays armed — every layer benefits. Unlike input-frame EMA
# (which smears motion as the smoothed input chases the real one), this
# is a *decision* hysteresis: when a pixel's actual color shifts enough
# that the alternative palette entry is meaningfully better, the
# threshold is exceeded on a single frame and the new index wins
# immediately. No motion smear, no ghosting.
#
# 5000 in d² space suppresses up to ~10-LSB-per-channel sensor noise
# (which moves d² by ~3000 for a typical near-boundary pixel), while a
# 25-LSB real color change (d² shift ~22000) still releases on a single
# frame. Tuned upward from the initial 2000 because residual rug-style
# flicker (textured static subjects + ~8 LSB webcam noise) was still
# crossing the threshold; 5000 fully suppresses it without introducing
# any motion lag (since real motion exceeds 5000 trivially).
PERCELL_QUANT_HYSTERESIS_BONUS = 5000.0

# bg0 stickiness for the percell path. bg0 (the global %00 color, written to
# $D021) is picked each frame as argmax of the EMA-smoothed palette counts. On
# content where two colors are near-tied for most-populated — e.g. a mostly-
# black frame with a bright moment, or letterboxed/pillarboxed video whose bars
# quantize to black — the argmax flip-flops frame-to-frame, and since bg0 fills
# every %00 pixel (background + the bars) the whole field strobes a different
# color for a frame. That's a single instant $D021 change, not a write tear, so
# it's especially visible on a slow transport where the rest of the frame lags.
#
# Fix: make bg0 sticky. Keep the current bg0 unless a challenger's smoothed
# count beats it by this relative margin — so bg0 still tracks a *sustained*
# dominant-color change (a real blue scene eventually turns the bars blue) but
# stops flickering between near-equal dominants. If the old bg0 vanishes from the
# frame its smoothed count → ~0 and any challenger trivially clears the margin,
# so bg0 can never get stuck on an absent color.
BG0_HYSTERESIS_MARGIN = 0.25

# Per-cell color-selection strategies for the mhires percell path. Each 4×8 cell
# gets bg0 (global) plus 3 per-cell colors (c1/c2/c3); the strategy decides WHICH
# 3 of the cell's present colors fill those slots. See pick_cell_colors.
#   frequency — the 3 most-populated non-bg0 colors (default; temporally stable
#               via the existing EMA, since the histogram it ranks is smoothed).
#   luminance — the darkest, median, and brightest present color, so a cell's
#               full tonal span survives even when one tone dominates the count.
#   contrast  — darkest + brightest, then the present color farthest (in luma)
#               from both, maximizing tonal spread across the 3 slots.
#   error-min — the trio minimizing summed per-pixel quantization error against
#               {bg0, c1, c2, c3}. Best reconstruction, but evaluates C(K,3)
#               trios over the cell's top-K present colors (see
#               ERROR_MIN_POOL_SIZE) — costlier than the others.
# The strategy name list itself is CELL_STRATEGIES (imported from palette, the
# single source of truth config.py validates against).

# error-min considers only each cell's top-K present colors (by smoothed count),
# bounding the trio search to C(K,3) candidates evaluated across all 1000 cells
# at once. A 4×8 cell rarely holds more than this many meaningfully-populated
# colors after quantization, so top-6 is near-optimal while keeping the search
# vectorized and realtime-capable. C(6,3) = 20 trios.
ERROR_MIN_POOL_SIZE = 6


def validate_cell_strategy(strategy: str) -> None:
    if strategy not in CELL_STRATEGIES:
        raise ValueError(f"cell_strategy must be one of {CELL_STRATEGIES}, got {strategy!r}")


def pick_cell_colors(
    cell_counts: np.ndarray,
    d_cell: np.ndarray,
    bg0: int,
    strategy: str,
) -> np.ndarray:
    """Choose each cell's 3 non-bg0 color slots (c1/c2/c3) by `strategy`.

    `cell_counts` is the (1000, 16) smoothed per-cell palette histogram with the
    bg0 entry already masked to -1 (so bg0 is never picked). `d_cell` is the
    (1000, 32, 16) per-cell-pixel distance to all 16 palette entries (only the
    error-min strategy uses it). Returns a (1000, 3) int64 array of palette
    indices; any slot the cell can't fill from a genuinely-present color is set
    to `bg0` — the same poison-filler guard the frequency path has always used
    (a duplicate bg0 is harmless: the %00 code already reaches bg0, and it keeps
    the absent slots deterministic so present colors don't churn screen/color
    RAM frame-to-frame). The caller sorts the result by palette index for
    delta-cache stability.
    """
    if strategy == "frequency":
        top3 = np.argpartition(cell_counts, -3, axis=1)[:, -3:]
        absent = np.take_along_axis(cell_counts, top3, axis=1) <= 0.0
        return np.where(absent, bg0, top3)

    if strategy == "error-min":
        return _pick_cell_colors_error_min(cell_counts, d_cell, bg0)

    # luminance / contrast both order the cell's present colors dark→light and
    # pick the extremes; they differ only in the 3rd slot.
    rows = np.arange(cell_counts.shape[0])
    present = cell_counts > 0.0  # (1000, 16) bool; bg0 masked out via -1
    n = present.sum(axis=1)  # (1000,) present color count per cell
    # Sort present colors by luma; absent entries → +inf so they sort last and
    # never get gathered for a valid slot.
    luma_masked = np.where(present, PALETTE_LUMA[None, :], np.inf)
    order = np.argsort(luma_masked, axis=1)  # (1000, 16) ascending by luma
    darkest = order[:, 0]
    brightest = order[rows, np.clip(n - 1, 0, 15)]
    pick0 = np.where(n >= 1, darkest, bg0)
    pick1 = np.where(n >= 2, brightest, bg0)

    if strategy == "luminance":
        median = order[rows, np.clip(n // 2, 0, 15)]  # middle of the sorted span
        pick2 = np.where(n >= 3, median, bg0)
    else:  # contrast: farthest present color (in luma) from both extremes
        d_dark = np.abs(PALETTE_LUMA[None, :] - PALETTE_LUMA[darkest][:, None])
        d_bright = np.abs(PALETTE_LUMA[None, :] - PALETTE_LUMA[brightest][:, None])
        spread = np.minimum(d_dark, d_bright)  # (1000, 16)
        eligible = present.copy()
        eligible[rows, darkest] = False
        eligible[rows, brightest] = False
        spread = np.where(eligible, spread, -1.0)
        pick2 = np.where(n >= 3, spread.argmax(axis=1), bg0)

    return np.column_stack([pick0, pick1, pick2]).astype(np.int64)


def _pick_cell_colors_error_min(
    cell_counts: np.ndarray, d_cell: np.ndarray, bg0: int
) -> np.ndarray:
    """error-min strategy: for each cell pick the trio of present colors that
    minimizes the summed per-pixel quantization error against {bg0, c1, c2, c3}.

    Bounds the search to each cell's top-`ERROR_MIN_POOL_SIZE` present colors and
    evaluates every C(K, 3) trio across all cells at once (vectorized), so it
    stays realtime-capable while being near-optimal (optimal when a cell holds ≤K
    meaningfully-populated colors). Pool slots a cell can't fill are set to bg0,
    so a trio drawing on them simply re-uses bg0 (a no-op against the fixed bg0
    candidate) — which naturally handles cells with fewer than 3 present colors.
    """
    n_cells = cell_counts.shape[0]
    k = ERROR_MIN_POOL_SIZE
    # Top-K present colors per cell (poison-guarded to bg0), like frequency but K.
    poolk = np.argpartition(cell_counts, -k, axis=1)[:, -k:]  # (n, K)
    absent = np.take_along_axis(cell_counts, poolk, axis=1) <= 0.0
    poolk = np.where(absent, bg0, poolk)  # (n, K)
    # Per-cell-pixel distance to each pool color and to bg0.
    d_pool = np.take_along_axis(d_cell, poolk[:, None, :], axis=2)  # (n, 32, K)
    d_bg0 = d_cell[:, :, bg0]  # (n, 32)
    # Enumerate all C(K,3) position-trios once; evaluate each across every cell.
    trios = list(itertools.combinations(range(k), 3))  # T trios of pool positions
    best_err = np.full(n_cells, np.inf, dtype=np.float32)
    best_trio = np.zeros((n_cells, 3), dtype=np.intp)
    for i, j, m in trios:
        # Per-pixel min over {bg0, pool[i], pool[j], pool[m]}, summed over pixels.
        cand_min = np.minimum(
            d_bg0, np.minimum(d_pool[:, :, i], np.minimum(d_pool[:, :, j], d_pool[:, :, m]))
        )
        err = cand_min.sum(axis=1)  # (n,)
        better = err < best_err
        best_err = np.where(better, err, best_err)
        best_trio[better] = (i, j, m)
    return np.take_along_axis(poolk, best_trio, axis=1).astype(np.int64)  # (n, 3)


def ema_counts(mode, per_pixel: np.ndarray) -> np.ndarray:
    """EMA-smoothed (16,) palette counts. Mode must have `_smoothed_counts`."""
    counts = np.bincount(per_pixel, minlength=16).astype(np.float32)
    if mode._smoothed_counts is None:
        mode._smoothed_counts = counts
    else:
        a = PALETTE_PICK_EMA_ALPHA
        mode._smoothed_counts = mode._smoothed_counts * (1.0 - a) + counts * a
    return mode._smoothed_counts.astype(np.int64)


# Saturation multiplier applied (in HSV) before quantization in the palette-
# mapping modes. Pushes desaturated webcam input far enough away from the
# gray-axis palette entries that the gray-penalty bias actually flips the
# argmin to a chromatic neighbor. 1.0 = identity.
DEFAULT_SAT_FACTOR = 1.8

# palette_mode selects the VIC-II per-cell slot-allocation strategy ONLY.
# Color shaping (channel boost + hue corrections) is an orthogonal global stage
# configured in [color] and applied to every mode below — see
# resolve_color_shaping / DEFAULT_HUE_CORRECTIONS. percell leads the tuple so
# it's the default and the natural SHIFT-cycle starting point.
PALETTE_MODES = ("percell", "cheap", "vivid", "grayscale")


def validate_palette_mode(mode: str) -> None:
    if mode not in PALETTE_MODES:
        raise ValueError(f"palette_mode must be one of {PALETTE_MODES}, got {mode!r}")


def resolve_color_shaping(
    channel_boost: list[float] | None,
    hue_corrections: list[dict] | None,
    replace: bool,
) -> tuple[np.ndarray, tuple[HueCorrection, ...]]:
    """Build the global pre-quant color-shaping state from [color] config.

    Returns (channel_boost_bgr, hue_corrections). Applies to every chromatic
    display mode regardless of palette_mode — palette_mode picks slots, [color]
    shapes colors. `channel_boost` None/empty falls back to the built-in
    CHANNEL_BOOST. User hue bands EXTEND the built-in defaults unless `replace`
    is set — `replace` is honored even with no bands, the escape hatch for
    "no hue corrections at all".
    """
    boost = parse_channel_boost(channel_boost)
    user = parse_hue_corrections(hue_corrections or [])
    hue = user if replace else DEFAULT_HUE_CORRECTIONS + user
    return boost, hue


def advance_palette_cycle(
    palette_mode: str,
    force_palette: bool,
    has_color_map: bool,
) -> tuple[str, bool, str]:
    """Advance the SHIFT palette cycle by one stop.

    The cycle walks the four PALETTE_MODES, then — only when a forced-palette
    map is installed — a single `percell+forced` preset stop (forced palette
    pairs with percell; see palette.ColorMap). Returns (new_mode, new_force,
    label). The label is logged by the playlist."""
    states: list[tuple[str, bool]] = [(m, False) for m in PALETTE_MODES]
    if has_color_map:
        states.append(("percell", True))
    cur = (palette_mode, force_palette)
    idx = states.index(cur) if cur in states else -1
    new_mode, new_force = states[(idx + 1) % len(states)]
    label = f"palette_mode={new_mode}" + ("+forced" if new_force else "")
    return new_mode, new_force, label


def palette_mode_settings(mode: str) -> tuple[float, np.ndarray]:
    """Return (saturation_factor, gray_penalty_vector) for a palette mode."""
    if mode == "grayscale":
        # Boosting saturation on a frame that'll only quantize to gray-axis
        # is wasted work — leave it identity.
        return 1.0, make_gray_penalty(
            gray_strength=0.0,
            pale_strength=0.0,
            chromatic_strength=GRAYSCALE_CHROMATIC_PENALTY,
        )
    return DEFAULT_SAT_FACTOR, make_gray_penalty()


log = logging.getLogger(__name__)


def fade_nibbles(arr: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Remap both nibbles of a uint8 array through a 16-entry palette LUT.

    Bitmap modes pack two per-cell colors into one screen-RAM byte (hi nibble =
    fg/c1, lo nibble = bg/c2); the scene fade dims each color independently."""
    hi = lut[arr >> 4]
    lo = lut[arr & 0x0F]
    return ((hi << 4) | lo).astype(np.uint8)


class DisplayMode:
    name = "base"
    # True when the scene paints into the bitmap area ($2000). Overlays that
    # write character/color RAM ($0400/$D800) only make sense over char modes,
    # so they check this flag to refuse attachment to bitmap scenes.
    is_bitmapped = False
    # True for standard char modes (PETSCII screen codes + color RAM low
    # nibble = FG). Overlays that paint PETSCII glyphs check this flag
    # instead of matching `name == "petscii"`, so multiple compatible modes
    # (petscii, blank) can host the same overlays.
    is_petscii_compatible = False
    # True for bitmap modes (hires, mhires) that can render the PETSCII text
    # overlays (clock/marquee/…) by folding glyphs into the bitmap. Overlays
    # that paint text accept either is_petscii_compatible (char) OR this
    # (bitmap) — see overlays.validate_for_scene + text_surface.py.
    is_bitmap_text_compatible = False
    # Frame-rate ceiling the Playlist falls back to when the scene itself
    # doesn't override target_fps. None = "use the playlist default (60
    # NTSC / 50 PAL)". Bitmap modes can't sustain that over HTTP so they
    # cap at 30.
    default_target_fps: float | None = None
    # True if compose() + push() are implemented. When set, the scene's
    # render path can call compose() to get screen/color buffers, run
    # overlay composers that mutate those buffers, and then push() a single
    # set of writes to the U64. Single-pass composition is what prevents
    # overlay flicker — the scene's full-frame write would otherwise stomp
    # the overlay's separate writes (and vice versa) on the next frame.
    supports_compose = False

    # The (width, height) compose()/render() downscales an incoming source
    # frame to before quantizing — the *only* resolution this mode consumes
    # (≤ 320×200 for every C64 mode). The single source of truth for both the
    # compose resize AND the video decoder's downscale-during-decode plan
    # (video._plan_decode_size): a 4K source frame for a 320px result is pure
    # waste that blows the real-time decode budget, so AVFileSource reformats
    # to a small headroom multiple of this during the yuv→bgr swscale pass
    # instead of converting the full source frame. None = the mode renders no
    # source frame (BlankDisplayMode), so the decoder keeps the native size.
    frame_target_size: tuple[int, int] | None = None

    # Per-source adaptive color fit ([color].auto_fit). None = disabled (the
    # default for every mode); a scene that can pre-scan its source
    # (video / slideshow) installs one via set_color_fit. The chromatic
    # modes apply it as the first shaping step in compose()/render(); webcam
    # scenes never set it, so this stays None and the path is a no-op.
    _color_fit: ColorFit | None = None

    # Per-source forced-palette remap ([color].force_palette). None = disabled.
    # Installed by pre-scanning scenes via set_color_map; only the chromatic
    # quantizing modes (mcm, mhires) actually APPLY it — the base stores it so
    # other modes (petscii) accept the call as a no-op. `_force_palette` is the
    # active toggle (set from config at construction, flipped by SHIFT cycle);
    # the remap only runs when the toggle is on AND a map has been installed.
    _color_map: ColorMap | None = None
    _force_palette: bool = False

    # Scene fade (set/teardown transitions, driven by the Playlist). 1.0 = no
    # fade; < 1.0 dims the composed frame's color-bearing fields toward black
    # via a palette remap (see palette.build_fade_lut). `last_buffers` caches
    # the most recent full-brightness composed frame so the freeze+dim fade-out
    # can re-push it at decreasing alpha without re-composing. Only the
    # compose-based families (Char/Bitmap) implement apply_fade; the base is a
    # no-op so non-compose modes are unaffected.
    fade_alpha: float = 1.0
    last_buffers: ComposeBuffers | None = None

    # Persistent user brightness (WLED bridge Mode 1 `bri` slider). 1.0 = full
    # brightness; < 1.0 dims the composed frame the same way a fade does, but it
    # persists across frames (and is re-stamped onto each fresh scene's mode by
    # the Playlist) rather than ramping. It folds *multiplicatively* with the
    # transient `fade_alpha`, so a fade-out from a dimmed scene ramps down from
    # the dimmed level. See `_fade_lut_alpha`.
    user_dim: float = 1.0

    @property
    def _fade_lut_alpha(self) -> float:
        """Effective dimming alpha folded into the fade LUT: the transient scene
        fade (`fade_alpha`) times the persistent user brightness (`user_dim`).
        1.0 × 1.0 = identity (no dimming); either below 1.0 dims the frame."""
        return self.fade_alpha * self.user_dim

    def apply_fade(self, buffers: ComposeBuffers) -> ComposeBuffers:
        """Return `buffers` with color-bearing fields dimmed toward black at
        ``self._fade_lut_alpha`` (fade × user brightness). Never mutates the
        input (so the cached pristine buffers survive a multi-frame fade-out).
        Base: identity."""
        return buffers

    def repush_faded(self, api: C64Backend, alpha: float) -> None:
        """Re-push the last composed frame dimmed to ``alpha`` — the freeze+dim
        fade-out. No-op when nothing has been composed yet (e.g. a scene torn
        down before its first frame)."""
        if self.last_buffers is None:
            return
        saved = self.fade_alpha
        self.fade_alpha = alpha
        try:
            self.push(api, self.apply_fade(self.last_buffers))
        finally:
            self.fade_alpha = saved

    def set_color_fit(self, fit: ColorFit | None) -> None:
        """Install (or clear) the per-source adaptive color fit. Called by
        scenes that pre-scan their source; passing None clears a stale fit
        from a previous file."""
        self._color_fit = fit

    def set_color_map(self, cmap: ColorMap | None) -> None:
        """Install (or clear) the per-source forced-palette remap. Called by
        scenes that pre-scan their source; passing None clears a stale map
        from a previous file. No-op effect on modes that don't apply it."""
        self._color_map = cmap

    # --- Live performance: runtime-tunable parameters --------------------
    # Continuous scalars a MIDI knob / WLED slider can sweep, name -> (lo, hi);
    # and discrete choices, name -> allowed values (a CC/slider bucket-selects,
    # a note/pad cycles). midi_control / wled_device drive both through the
    # `mode.<name>` holder, the same seam they use for effect/source LIVE_PARAMS.
    # Empty on the base; the quantizing modes populate them. The choice tuples
    # are pinned to [color]'s metadata choices by tests/test_live_tune.py so they
    # can't drift from the config surface.
    LIVE_PARAMS: dict[str, tuple[float, float]] = {}
    LIVE_CHOICES: dict[str, tuple[str, ...]] = {}

    # [color].auto_fit_strength as a live knob. The pre-scanned ColorFit is
    # installed at FULL strength (the scenes' accumulators use strength=1.0) and
    # the mode lerps it toward identity by this factor at apply() time, so the
    # strength is tunable at runtime (and persistable) instead of frozen into the
    # fit. 1.0 = the full fit; 0.0 = identity (auto_fit off). Only the
    # color_fit-applying modes (mcm/mhires/petscii) read it via _fit_for_apply;
    # others leave it at the default and it does nothing.
    _auto_fit_strength: float = 1.0

    @property
    def auto_fit_strength(self) -> float:
        return self._auto_fit_strength

    @auto_fit_strength.setter
    def auto_fit_strength(self, value: float) -> None:
        self._auto_fit_strength = float(min(1.0, max(0.0, value)))

    def _fit_for_apply(self) -> ColorFit | None:
        """The installed ColorFit lerped by the live auto_fit_strength, or None
        when no fit is installed — the single seam the color_fit-applying modes
        call in compose() in place of reading `_color_fit` directly."""
        if self._color_fit is None:
            return None
        return self._color_fit.lerped(self._auto_fit_strength)

    def set_live_choice(self, api: C64Backend, name: str, value: str) -> str:
        """Apply a discrete LIVE_CHOICES value to the running mode; return a short
        OSD label. palette_mode needs the backend handle so it's special-cased;
        every other choice dispatches to its ``set_<name>`` setter. Empty label
        (a no-op) when the mode has no such setter."""
        if name == "palette_mode":
            setter = getattr(self, "set_palette_mode", None)
            return setter(api, value) if setter is not None else ""
        setter = getattr(self, "set_" + name, None)
        if setter is None:
            return ""
        label = setter(value)
        return label if isinstance(label, str) else f"{name}={value}"

    def get_live_choice(self, name: str) -> str | None:
        """The current value of a LIVE_CHOICES field (so a note/pad can cycle
        from it). None when this mode doesn't carry that field."""
        if name == "color_match":
            return "perceptual" if getattr(self, "_perceptual", False) else "rgb"
        if name == "palette_mode":
            return getattr(self, "palette_mode", None)
        if name == "dither_method":
            return getattr(self, "_dither_method", None)
        if name == "cell_strategy":
            return getattr(self, "_cell_strategy", None)
        return None

    def setup(self, api: C64Backend):
        # Anything that changes the meaning of the VIC memory map should
        # drop the dirty cache so we don't suppress a needed write.
        api.invalidate_cache()

    def teardown(self, api: C64Backend) -> None:
        """Reverse any per-mode state installed by setup() that survives
        a scene boundary. Default: no-op (most modes only write VIC
        registers + memory, which the next scene's setup overwrites).

        Modes that install a C64-side IRQ handler (currently:
        HiresDisplayMode with use_reu_staged) MUST override this to
        unhook $0314 before the next scene runs, or the next scene's
        IRQ-using code (e.g. an audio REU pump on a video that
        followed) vectors into the stale handler.

        Called by Scene.teardown before audio.stop() and any
        scene-specific teardown."""
        return None

    def compose(self, frame: np.ndarray) -> ComposeBuffers:
        """Build named numpy buffers from `frame`. Overlays mutate these
        before push() uploads them. Only implemented when supports_compose
        is True; default raises. Video-less modes (BlankDisplayMode) ignore
        the frame argument — scenes.py passes a placeholder when no frame
        is available."""
        raise NotImplementedError(f"{type(self).__name__} does not implement compose()")

    def push(self, api: C64Backend, buffers: ComposeBuffers) -> None:
        """Upload composed buffers via api.write_region. Only implemented
        when supports_compose is True; default raises."""
        raise NotImplementedError(f"{type(self).__name__} does not implement push()")

    def render(self, api: C64Backend, frame: np.ndarray):
        """Default render = compose + push for modes that support it.
        Modes without compose support override this directly."""
        if self.supports_compose:
            self.push(api, self.compose(frame))
            return
        raise NotImplementedError

    def cycle_style(self, api: C64Backend) -> str | None:
        """Rotate this display mode to its next visual style. Return the
        new style name, or None when the mode has no cyclable styles.

        Triggered by the SHIFT key (via the keyboard poller) and any
        future control-plane equivalent. Modes that implement this should
        invalidate the api delta cache so the next frame fully repaints
        with the new style — the cache is keyed by region, not by what's
        on screen, so a style change without invalidation can leave stale
        pixels for any region the new style happens to write the same
        bytes to. Default: no-op (return None)."""
        return None
