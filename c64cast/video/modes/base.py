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
    color_name,
    make_gray_penalty,
    parse_channel_boost,
    parse_hue_corrections,
)

ORDERED_DITHER_OFFSET_FNS = {"ordered": bayer_offset, "blue_noise": blue_noise_offset}


class ComposeBuffers(TypedDict):
    """The screen + color RAM buffers a char-mode display's ``compose()``
    produces and ``push()`` (plus overlay ``compose()``) consume. Each is a
    length-1000 uint8 numpy array, one byte per 40×25 cell.

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
    register write."""

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


class FlickerComposeBuffers(BitmapComposeBuffers):
    """Flicker blending ([color].flicker_tolerance) adds ``screen_b``: the second
    1000-byte screen matrix, alternated with ``screen`` at the VIC field rate so
    each cell's color pair fuses in the eye.

    The two pages differ only in their color nibbles; ``bitmap`` is shared, so
    a differing mask cannot make the flicker geometric and a frame costs one
    extra 1000-byte write rather than a second full frame."""

    screen_b: np.ndarray


class MHiresFlickerComposeBuffers(MHiresComposeBuffers, FlickerComposeBuffers):
    """MultiHires under flicker blending: ``color`` plus the second screen page
    (inherited from ``FlickerComposeBuffers`` rather than redeclared).

    Only the screen nibbles alternate, so only c1 and c2 can be blends. c3 lives
    in color RAM at $D800, which is neither VIC-banked nor selected by $D018 —
    one copy, both fields — and bg0 is the single $D021 register. Both stay real
    palette indices, which is why ``color`` has no B-page sibling here."""


# Fixed slot assignments in ascending luminance, so the bitmap stays a stable
# darkest-to-brightest LUT whatever the frame holds. MCM's FG is restricted to
# {0, 1} — color RAM bit 3 is the multicolor flag — so its bgs carry the
# mid-tones; mhires has no per-cell FG and drops pure white for a third
# mid-tone. See video-color.md#palette_mode--per-cell-slot-allocation.
GRAYSCALE_MHIRES_SLOTS = (0, 11, 12, 15)  # black, dark gray, gray, light gray
GRAYSCALE_MCM_BGS = (11, 12, 15)  # dark gray, gray, light gray

# EMA weight on the new frame's palette counts when picking the global color
# slots for cheap/vivid. Smooths over ≈4 frames.
PALETTE_PICK_EMA_ALPHA = 0.25

# Per-cell EMA weight for the percell mhires path. A 4×8 cell's 32-pixel
# histogram is an order of magnitude noisier than the global one, so this sits
# lower: ≈7-frame time constant, converging inside ≈120 ms.
PERCELL_PICK_EMA_ALPHA = 0.15

# Bitmap-code hysteresis for the percell path, in d² space (the units
# quantize_distances returns). A pixel keeps its previous code while it stays
# within this bonus of the frame's minimum-distance code.
PERCELL_CODE_HYSTERESIS_BONUS = 5000.0

# Per-pixel palette-index hysteresis for the percell path, same d² units.
# Calibrated against measured webcam sensor noise — see
# video-color.md#colormotion_smoothing--temporal-smoothing--after-images.
PERCELL_QUANT_HYSTERESIS_BONUS = 5000.0

# bg0 stickiness for the percell path: keep the current bg0 unless a
# challenger's smoothed count beats it by this relative margin. A vanished bg0
# has a smoothed count of ≈0, so it can never get stuck on an absent color.
BG0_HYSTERESIS_MARGIN = 0.25

# The strategies themselves are `pick_cell_colors`; the name list is
# CELL_STRATEGIES in palette.py, the source config.py validates against. See
# video-color.md#colorcell_strategy--which-3-colors-fill-a-cell.

# error-min bounds its trio search to each cell's top-K present colors by
# smoothed count: C(6,3) = 20 trios, evaluated across all 1000 cells at once.
ERROR_MIN_POOL_SIZE = 6

# Relative-margin hysteresis for the error-min trio pick: keep the previous
# frame's trio unless a challenger's summed error is at least this fraction
# lower. Unlike every other selection stage here, error-min scores against the
# frame's raw per-pixel distances rather than the EMA-smoothed cell_counts, so
# it gets no temporal stability for free. See
# video-color.md#colorflicker_tolerance--temporal-color-blending.
ERROR_MIN_HYSTERESIS_MARGIN = 0.25


def validate_cell_strategy(strategy: str) -> None:
    if strategy not in CELL_STRATEGIES:
        raise ValueError(f"cell_strategy must be one of {CELL_STRATEGIES}, got {strategy!r}")


def pick_cell_colors(
    cell_counts: np.ndarray,
    d_cell: np.ndarray,
    bg0: int,
    strategy: str,
    luma: np.ndarray = PALETTE_LUMA,
    prev_trio: np.ndarray | None = None,
    error_min_margin: float = ERROR_MIN_HYSTERESIS_MARGIN,
) -> np.ndarray:
    """Choose each cell's 3 non-bg0 color slots (c1/c2/c3) by `strategy`.

    `cell_counts` is the (1000, N) smoothed per-cell palette histogram with the
    bg0 entry already masked to -1 (so bg0 is never picked). `d_cell` is the
    (1000, 32, N) per-cell-pixel distance to all N entries (only the error-min
    strategy uses it). Returns a (1000, 3) int64 array of palette indices; any
    slot the cell can't fill from a genuinely-present color is set to `bg0`,
    the poison-filler guard — a duplicate bg0 is harmless, since the %00 code
    already reaches it. The caller sorts the result by palette index for
    delta-cache stability.

    N is 16 for the plain palette and larger under flicker blending, where the
    trailing entries are color pairs rather than colors ([color].flicker_tolerance,
    video/flicker.py). `luma` orders that same entry space dark→light for the
    luminance/contrast strategies, so it has to be the blend table's own vector
    when one is in play — the default is the 16-entry palette.

    `prev_trio` (only the error-min strategy uses it) is the previous frame's
    (1000, 3) winning trio, for the ERROR_MIN_HYSTERESIS_MARGIN hysteresis;
    None disables it (first frame, or a caller that doesn't track it).
    """
    if strategy == "frequency":
        top3 = np.argpartition(cell_counts, -3, axis=1)[:, -3:]
        absent = np.take_along_axis(cell_counts, top3, axis=1) <= 0.0
        return np.where(absent, bg0, top3)

    if strategy == "error-min":
        return _pick_cell_colors_error_min(cell_counts, d_cell, bg0, prev_trio, error_min_margin)

    # luminance and contrast both order the cell's present colors dark→light
    # and pick the extremes; they differ only in the 3rd slot.
    rows = np.arange(cell_counts.shape[0])
    last = cell_counts.shape[1] - 1  # highest valid entry index
    present = cell_counts > 0.0  # (1000, N) bool; bg0 masked out via -1
    n = present.sum(axis=1)  # (1000,) present color count per cell
    # Absent entries → +inf so they sort last and are never gathered.
    luma_masked = np.where(present, luma[None, :], np.inf)
    order = np.argsort(luma_masked, axis=1)  # (1000, N) ascending by luma
    darkest = order[:, 0]
    brightest = order[rows, np.clip(n - 1, 0, last)]
    pick0 = np.where(n >= 1, darkest, bg0)
    pick1 = np.where(n >= 2, brightest, bg0)

    if strategy == "luminance":
        median = order[rows, np.clip(n // 2, 0, last)]  # middle of the sorted span
        pick2 = np.where(n >= 3, median, bg0)
    else:  # contrast: farthest present color (in luma) from both extremes
        d_dark = np.abs(luma[None, :] - luma[darkest][:, None])
        d_bright = np.abs(luma[None, :] - luma[brightest][:, None])
        spread = np.minimum(d_dark, d_bright)  # (1000, N)
        eligible = present.copy()
        eligible[rows, darkest] = False
        eligible[rows, brightest] = False
        spread = np.where(eligible, spread, -1.0)
        pick2 = np.where(n >= 3, spread.argmax(axis=1), bg0)

    return np.column_stack([pick0, pick1, pick2]).astype(np.int64)


def _pick_cell_colors_error_min(
    cell_counts: np.ndarray,
    d_cell: np.ndarray,
    bg0: int,
    prev_trio: np.ndarray | None = None,
    margin: float = ERROR_MIN_HYSTERESIS_MARGIN,
) -> np.ndarray:
    """error-min strategy: for each cell pick the trio of present colors that
    minimizes the summed per-pixel quantization error against {bg0, c1, c2, c3}.

    Bounds the search to each cell's top-`ERROR_MIN_POOL_SIZE` present colors and
    evaluates every C(K, 3) trio across all cells at once (vectorized), so it
    stays realtime-capable while being near-optimal (optimal when a cell holds ≤K
    meaningfully-populated colors). Pool slots a cell can't fill are set to bg0,
    so a trio drawing on them simply re-uses bg0 (a no-op against the fixed bg0
    candidate) — which naturally handles cells with fewer than 3 present colors.

    `prev_trio` + `margin` apply ERROR_MIN_HYSTERESIS_MARGIN's keep-unless-beaten
    rule: the pool search's winner only replaces the previous frame's trio when
    its summed error is at least `margin` lower, since scoring against this
    frame's raw d_cell (see that constant's comment) has no smoothing of its own.
    """
    n_cells = cell_counts.shape[0]
    k = ERROR_MIN_POOL_SIZE
    # Top-K present colors per cell, poison-guarded to bg0.
    poolk = np.argpartition(cell_counts, -k, axis=1)[:, -k:]  # (n, K)
    absent = np.take_along_axis(cell_counts, poolk, axis=1) <= 0.0
    poolk = np.where(absent, bg0, poolk)  # (n, K)
    d_pool = np.take_along_axis(d_cell, poolk[:, None, :], axis=2)  # (n, 32, K)
    d_bg0 = d_cell[:, :, bg0]  # (n, 32)
    # Every C(K,3) position-trio, each evaluated across all cells at once.
    trios = list(itertools.combinations(range(k), 3))  # T trios of pool positions
    best_err = np.full(n_cells, np.inf, dtype=np.float32)
    best_trio = np.zeros((n_cells, 3), dtype=np.intp)
    for i, j, m in trios:
        cand_min = np.minimum(
            d_bg0, np.minimum(d_pool[:, :, i], np.minimum(d_pool[:, :, j], d_pool[:, :, m]))
        )
        err = cand_min.sum(axis=1)  # (n,)
        better = err < best_err
        best_err = np.where(better, err, best_err)
        best_trio[better] = (i, j, m)
    challenger = np.take_along_axis(poolk, best_trio, axis=1).astype(np.int64)  # (n, 3)
    if prev_trio is None:
        return challenger

    # prev_trio scored against *this* frame's d_cell: the pool, and the trio's
    # own poolk positions, may have shifted since. Its entry indices still index
    # d_cell directly.
    d_prev = np.take_along_axis(d_cell, prev_trio[:, None, :], axis=2)  # (n, 32, 3)
    prev_min = np.minimum(
        d_bg0, np.minimum(d_prev[:, :, 0], np.minimum(d_prev[:, :, 1], d_prev[:, :, 2]))
    )
    prev_err = prev_min.sum(axis=1)  # (n,)
    keep = best_err >= prev_err * (1.0 - margin)
    return np.where(keep[:, None], prev_trio, challenger)


def ema_counts(mode, per_pixel: np.ndarray, n_entries: int = 16) -> np.ndarray:
    """EMA-smoothed (n_entries,) palette counts. Mode must have `_smoothed_counts`."""
    counts = np.bincount(per_pixel, minlength=n_entries).astype(np.float32)
    if mode._smoothed_counts is None:
        mode._smoothed_counts = counts
    else:
        a = PALETTE_PICK_EMA_ALPHA
        mode._smoothed_counts = mode._smoothed_counts * (1.0 - a) + counts * a
    return mode._smoothed_counts.astype(np.int64)


# HSV saturation multiplier applied before quantization in the palette-mapping
# modes, so the gray-penalty bias can flip a desaturated pixel's argmin to a
# chromatic neighbor. 1.0 = identity.
DEFAULT_SAT_FACTOR = 1.8

# percell leads the tuple: it is the default and the SHIFT-cycle start.
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
    # write $0400/$D800 check this flag to refuse attachment.
    is_bitmapped = False
    # True for standard char modes (PETSCII screen codes + color RAM low nibble
    # = FG). Overlays check this flag rather than matching `name == "petscii"`.
    is_petscii_compatible = False
    # True for bitmap modes that render PETSCII text overlays by folding glyphs
    # into the bitmap. Text overlays accept either this or
    # is_petscii_compatible — see overlays.validate_for_scene + text_surface.py.
    is_bitmap_text_compatible = False
    # Frame-rate ceiling the Playlist falls back to when the scene does not
    # override target_fps. None = the playlist default (60 NTSC / 50 PAL);
    # bitmap modes cannot sustain that over HTTP and cap at 30.
    default_target_fps: float | None = None
    # True if compose() + push() are implemented, which lets overlays mutate the
    # buffers between them so one set of writes carries scene and overlay
    # together. Two separate write passes stomp each other on alternate frames.
    supports_compose = False

    # The (width, height) compose()/render() downscales a source frame to before
    # quantizing — the only resolution this mode consumes. Also read by the
    # video decoder's downscale-during-decode plan (video._plan_decode_size),
    # which reformats to a small headroom multiple of this during the yuv→bgr
    # swscale pass. None = the mode renders no source frame (BlankDisplayMode),
    # so the decoder keeps the native size.
    frame_target_size: tuple[int, int] | None = None

    # Per-source adaptive color fit ([color].auto_fit), installed by a scene
    # that can pre-scan its source (video / slideshow). None = disabled.
    _color_fit: ColorFit | None = None

    # Per-source forced-palette remap ([color].force_palette). Only mcm and
    # mhires apply it; the base stores it so other modes accept set_color_map as
    # a no-op. The remap runs only when `_force_palette` is on AND a map is set.
    _color_map: ColorMap | None = None
    _force_palette: bool = False

    # Scene fade, driven by the Playlist. 1.0 = no fade. `last_buffers` caches
    # the most recent full-brightness composed frame, which is what lets the
    # freeze+dim fade-out re-push at decreasing alpha without re-composing.
    fade_alpha: float = 1.0
    last_buffers: ComposeBuffers | None = None

    # Persistent user brightness (WLED bridge Mode 1 `bri` slider). Folds
    # multiplicatively with the transient `fade_alpha` — see `_fade_lut_alpha`.
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

    # Continuous scalars a MIDI knob / WLED slider can sweep, name -> (lo, hi),
    # and discrete choices, name -> allowed values. The choice tuples are pinned
    # to [color]'s metadata choices by tests/test_live_tune.py.
    LIVE_PARAMS: dict[str, tuple[float, float]] = {}
    LIVE_CHOICES: dict[str, tuple[str, ...]] = {}

    # [color].auto_fit_strength as a live knob. The pre-scanned ColorFit is
    # installed at full strength and lerped toward identity by this factor at
    # apply() time, so the strength stays runtime-tunable. 0.0 = auto_fit off.
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

    #: Choice fields whose setter needs the backend handle to repaint a VIC
    #: register live, the same reason palette_mode does — border/background
    #: only ever hit $D020/$D021 at setup() otherwise (see BlankDisplayMode).
    _CHOICES_NEEDING_API: frozenset[str] = frozenset({"palette_mode", "border", "background"})

    def set_live_choice(self, api: C64Backend, name: str, value: str) -> str:
        """Apply a discrete LIVE_CHOICES value to the running mode; return a short
        OSD label. A handful of choices need the backend handle so they're
        special-cased; every other choice dispatches to its ``set_<name>``
        setter. Empty label (a no-op) when the mode has no such setter."""
        setter = getattr(self, "set_" + name, None)
        if setter is None:
            return ""
        label = setter(api, value) if name in self._CHOICES_NEEDING_API else setter(value)
        return label if isinstance(label, str) else f"{name}={value}"

    def get_live_choice(self, name: str) -> str | None:
        """The current value of a LIVE_CHOICES field. None when this mode
        doesn't carry that field.

        Two fields are not stored as their own choice string and so are read
        specially; every other one is the private attribute of the same name.
        That fallback rather than a case per field on purpose: a mode declares
        a live choice by adding one entry to ``LIVE_CHOICES``, and a getter
        that had to be extended in step was extended late — ``cell_pick`` was
        declared and never read here, which no caller noticed until a UI tried
        to *show* the current value rather than cycle past it."""
        if name == "color_match":
            return "perceptual" if getattr(self, "_perceptual", False) else "rgb"
        if name == "palette_mode":
            return getattr(self, "palette_mode", None)
        if name in ("border", "background"):
            index = getattr(self, name, None)
            return color_name(index) if isinstance(index, int) else None
        value = getattr(self, f"_{name}", None)
        return value if isinstance(value, str) else None

    def setup(self, api: C64Backend):
        # A change in what the VIC memory map means has to drop the dirty
        # cache, which is keyed by region and not by content.
        api.invalidate_cache()

    def teardown(self, api: C64Backend) -> None:
        """Reverse any per-mode state installed by setup() that survives
        a scene boundary. Default: no-op (most modes only write VIC
        registers + memory, which the next scene's setup overwrites).

        Modes that install a C64-side IRQ handler MUST override this to unhook
        $0314 before the next scene runs, or the next scene's IRQ-using code
        vectors into the stale handler.

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
