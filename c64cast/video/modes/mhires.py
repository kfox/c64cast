"""160x200 4-color VIC-II MCBM bitmap mode (the richest color pipeline)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import cast

import cv2
import numpy as np

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import CIA2, VIC, VIC_BANK_0, VIC_BANK_2, RegionID
from c64cast.scenes.text_surface import MHiresTextSurface
from c64cast.video.dither import DITHER_METHODS, error_diffuse_cells
from c64cast.video.flicker import (
    DEFAULT_TOLERANCE,
    FLICKER_TOLERANCES,
    BlendTable,
    blend_distances_for,
    build_blend_table,
    parse_scoring_pairs,
)
from c64cast.video.modes_irq import (
    BANK_SWAP_IRQ_HANDLER_ADDR,
    DD00_BANK_0,
    FRAME_TRACKER_ADDR,
    MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER,
    MHIRES_BANK_SWAP_IRQ_HANDLER,
    MHIRES_FRAME_TRACKER_LEN,
    REU_VIDEO_BITMAP_LEN,
    REU_VIDEO_BITMAP_SCREEN_LEN,
    install_bank_swap_irq,
    push_mhires_via_reu,
    uninstall_bank_swap_irq,
)
from c64cast.video.palette import (
    C64_PALETTE_BGR,
    CELL_STRATEGIES,
    COLOR_MATCH_MODES,
    PALETTE_LUMA,
    PERCEPTUAL_DIST_SCALE,
    apply_color_fit,
    apply_hue_corrections,
    boost_saturation,
    build_fade_lut,
    pick_diverse_top_n,
    quantize_distances,
    quantize_distances_for,
)

from . import base
from .base import (
    BG0_HYSTERESIS_MARGIN,
    GRAYSCALE_MHIRES_SLOTS,
    ORDERED_DITHER_OFFSET_FNS,
    PALETTE_MODES,
    MHiresComposeBuffers,
    MHiresFlickerComposeBuffers,
    advance_palette_cycle,
    ema_counts,
    palette_mode_settings,
    pick_cell_colors,
    resolve_color_shaping,
    validate_cell_strategy,
    validate_palette_mode,
)
from .bitmap import BitmapDisplayMode, engage_bitmap_mode

log = logging.getLogger(__name__)


def _solid_last(top3: np.ndarray, table: BlendTable) -> np.ndarray:
    """Reorder each cell's 3 picks so slot c3 holds a real palette color.

    c3 is written to color RAM at $D800, which both fields read — one copy, no
    $D018 to re-point — so a blend parked there would show only its first color
    and quietly mis-render the cell. c1 and c2 are the screen-byte nibbles and
    can hold anything.

    `top3` arrives sorted ascending, and the widened table lists all 16 solids
    before any pair, so a cell that picked a solid at all has it at position 0
    and the reorder is a rotation. A cell whose three picks are all pairs has to
    give one up: it loses the one with the smallest `demotion_cost`, i.e. the
    pair whose fused color was closest to a real one to begin with, which is the
    pair that was buying the least.
    """
    rows = np.arange(top3.shape[0])
    has_solid = top3[:, 0] < 16
    slot = np.where(has_solid, 0, table.demotion_cost[top3].argmin(axis=1))
    picked = top3[rows, slot]
    c3 = np.where(has_solid, picked, table.nearest_solid[picked])
    # The two survivors, re-sorted so an unchanged SET keeps an unchanged order
    # and the delta cache still skips the cell.
    keep = np.sort(
        np.stack([top3[rows, (slot + 1) % 3], top3[rows, (slot + 2) % 3]], axis=1), axis=1
    )
    return np.column_stack([keep, c3]).astype(np.int64)


class MultiHiresDisplayMode(BitmapDisplayMode):
    """160×200 4-color VIC-II MCBM bitmap.

    palette_mode (slot-allocation strategy only; color shaping is the global
    [color] stage applied to every mode):
      "percell" (default) — picks bg0 globally (most-populated palette
        index), then for every 4×8 cell picks its own top-3 non-bg colors
        by population. The hardware allows c1/c2/c3 to vary per cell via
        screen RAM + color RAM, so a frame can carry up to bg0 + 3×1000
        distinct colors instead of the global-4 the older modes assume.
        Webcam/video content gains substantially: cells that don't
        contain bg0 stop wasting one of their 4 slots on it, and cells in
        very different regions of the frame stop being forced to share a
        4-color set picked for the dominant subject.
      "cheap" — legacy global-4: HSV saturation boost + gray-penalty bias
        on the per-pixel argmin, top-4 palette slots picked by raw
        frequency. Cheap to compute but throws away most of MCBM's
        per-cell palette capacity.
      "vivid" — legacy global-4, same biases plus hue-diversity pick of
        the 4 globals (most-populated wins slot 0; subsequent slots prefer
        the most populated entry whose hue is far enough from already-
        chosen picks). Useful when a global mode is needed and the frame
        keeps collapsing to near-shades.
      "grayscale" — fixed 4-of-5 gray-axis slot assignment in luminance
        order (black, dark gray, gray, light gray; pure white is dropped
        for better mid-tone resolution). Adaptive picking from only 5 gray
        entries flipped the slot order on every frame whenever per-frame
        counts tie-broke differently, which remapped every pixel in the
        8 KB bitmap and forced a full re-upload — bytes/frame stayed at
        ~20 KB and the scene paced at ~13 fps. Fixing the slot order keeps
        the bitmap stable, lets the chunked delta-cache do its job, and
        restores the bitmap-mode 30 fps target.

    In cheap and vivid modes, palette indices that didn't win one of the
    4 global slots are LUT-mapped to the nearest of the 4 (in weighted BGR
    space). The previous code zero-defaulted them, which silently collapsed
    every "other" color to bg0 and bled large patches of background into
    the image. Per-cell skips the LUT entirely — every pixel resolves
    directly against its cell's own {bg0, c1, c2, c3}.

    use_reu_staged: opt into the REU bank-swap double-buffer pipeline.
      Each frame's bitmap + screen + color RAM are REUWRITE-staged
      (bus-clean) then dropped into the OFF-SCREEN VIC bank (bitmap +
      screen) and shared $D800 (color) via three REU→main DMAs triggered
      by a C64-side raster IRQ at vblank. The handler then writes the
      new bg0 to $D021 and swaps $DD00 to bring up the new bank.

      Cannot coexist with [audio].use_reu_pump on webcam scenes (both
      arm $0314); config.validate_scene_cfg rejects the combination at
      load time. The color RAM DMA writes to shared $D800 mid-handler,
      which produces a brief c3-mismatch window across the bank-swap
      tear line — bounded to one VIC cell row (~8 raster lines) and
      typically imperceptible on real content (color changes between
      consecutive frames are small).
    """

    name = "mhires"
    # 160 wide is the MCBM pixel grid (anamorphic — displayed stretched to
    # 320); height 200 exceeds width here, so the decode planner must honor
    # BOTH axes (see video._plan_decode_size).
    frame_target_size = (160, 200)
    # Live-tune surface (see DisplayMode.LIVE_PARAMS). mhires is the richest:
    # adaptive color fit, spatial dither, per-cell temporal smoothing, and the
    # full set of discrete choices (dither method, per-cell strategy, color
    # match, palette mode) are all live.
    LIVE_PARAMS = {
        "dither_strength": (0.0, 2.0),
        "motion_smoothing": (0.0, 1.0),
        "auto_fit_strength": (0.0, 1.0),
    }
    LIVE_CHOICES = {
        "dither_method": DITHER_METHODS,
        "cell_strategy": CELL_STRATEGIES,
        "color_match": COLOR_MATCH_MODES,
        "palette_mode": PALETTE_MODES,
    }

    # Derived from motion_smoothing (× _penalty_scale) by the property setter
    # below; declared here so the setter-only writes are visible as instance
    # attributes (compose() reads them).
    _motion_smoothing: float
    _ema_alpha: float
    _quant_hysteresis: float
    _code_hysteresis: float

    def __init__(
        self,
        palette_mode: str = "percell",
        *,
        use_reu_staged: bool = False,
        double_buffer: bool = False,
        audio_reu_pump_active: bool = False,
        hue_corrections: list[dict] | None = None,
        hue_corrections_replace: bool = False,
        channel_boost: list[float] | None = None,
        force_palette: bool = False,
        text_double_height: bool = False,
        dither_method: str = "none",
        dither_strength: float = 0.5,
        perceptual: bool = False,
        cell_strategy: str = "frequency",
        motion_smoothing: float = 1.0,
        auto_fit_strength: float = 1.0,
        flicker_tolerance: str = DEFAULT_TOLERANCE,
        flicker_max_luma_delta: float = 0.075,
        flicker_score_pairs: Sequence[str] | None = None,
    ):
        validate_palette_mode(palette_mode)
        validate_cell_strategy(cell_strategy)
        self._auto_fit_strength = float(min(1.0, max(0.0, auto_fit_strength)))
        # Text overlays render double-wide ("chunky") by default — an 8×8 glyph
        # spans 2 of the mode's 4px cells (20-col text grid). text_double_height
        # also stretches it to 16 px tall (12-row grid) for across-the-room
        # legibility. See text_surface.MHiresTextSurface.
        self.text_double_height = bool(text_double_height)
        # Forced-palette preset pairs with percell (see cycle_style); when config
        # opts in, start there regardless of the configured palette_mode.
        self._force_palette = bool(force_palette)
        if self._force_palette:
            palette_mode = "percell"
        self.palette_mode = palette_mode
        self._sat_factor, self._gray_penalty = palette_mode_settings(palette_mode)
        self._channel_boost, self._hue_corrections = resolve_color_shaping(
            channel_boost, hue_corrections, hue_corrections_replace
        )
        # Perceptual (CIE-Lab) nearest-palette matching ([color].color_match).
        # When on, compose() measures nearest-color in Lab (perceptually uniform)
        # instead of the brightness-weighted BGR metric; the channel_boost + gray
        # penalty shaping still applies (only the distance space changes). The
        # gray penalty and the percell code/quant hysteresis all live in d² space,
        # so scale them to the Lab metric's smaller magnitude to preserve the same
        # bias/flicker-suppression strength. See palette.quantize_distances_for.
        self._perceptual = bool(perceptual)
        # Flicker blend ([color].flicker_tolerance): hold two screen pages over
        # one bitmap and alternate them every field. None = off, and every blend
        # branch is keyed on that rather than a bool so the plain path keeps
        # running the 16-entry quantizer it always did.
        #
        # Only c1 and c2 can carry a pair. They are the two nibbles of the screen
        # byte, and the screen matrix is what $D018 re-points at each field; c3
        # lives in color RAM at $D800, which is neither VIC-banked nor reachable
        # from $D018, and bg0 is the single $D021 register the swap handler
        # writes once per committed frame. Blending bg0 was measured and dropped
        # rather than skipped: across the fixture set the frame's most-populated
        # entry was a solid every time — a solid wins the dominant slot precisely
        # because it owns the largest region of color space — so the handler
        # surgery to alternate $D021 would have bought a bit-identical picture.
        _blending = FLICKER_TOLERANCES.get(flicker_tolerance, -1) >= 0
        # Parsed here rather than at the call site so a malformed entry raises
        # where the mode is built, alongside the tolerance's own validation.
        _scored = parse_scoring_pairs(flicker_score_pairs) if flicker_score_pairs else None
        self._blend_table: BlendTable | None = (
            build_blend_table(
                flicker_max_luma_delta, tolerance=flicker_tolerance, score_pairs=_scored
            )
            if _blending
            else None
        )
        if self._blend_table is not None and cell_strategy != "error-min":
            # Blending forces the cell fit, the same measured reason hires
            # forces its own: a blend sits between two solids, so counting how
            # many pixels landed on each entry splits a cell's population across
            # neighbors and no slot wins on merit. Picking the trio by summed
            # reconstruction error instead turns a widened palette that measured
            # slightly WORSE than the 16 solids on two of seven fixtures into one
            # that improves or ties on all seven. The fit is what makes the
            # second screen page pay for itself.
            log.info(
                "mhires: flicker blend forces cell_strategy=error-min "
                "(a frequency pick fragments across the widened palette)"
            )
            cell_strategy = "error-min"
        if self._blend_table is not None and not self._perceptual:
            # Same finding as hires: a blend's fused color is a linear-light
            # average and its eligibility is a Lab gap, so fitting in
            # weighted-BGR optimizes a different space than the extra entries
            # live in, and the widened palette can measure worse than the 16
            # solids. Forced rather than refused — color_match's own default
            # already resolves here, so this only fires on an explicit "rgb".
            log.info("mhires: flicker blend forces color_match=perceptual (blends are Lab-defined)")
            self._perceptual = True
        self._penalty_scale = PERCEPTUAL_DIST_SCALE if self._perceptual else 1.0
        # Temporal-smoothing knob ([color].motion_smoothing, 0..1). The percell
        # path carries two flicker-suppression buffers that trade motion-tracking
        # for frame-to-frame stability: the per-cell color-count EMA and the
        # per-pixel/per-cell decision hysteresis. Both cause an after-image on
        # hard cuts (an outline from the previous shot lingering as the buffers
        # decay). `motion_smoothing` scales BOTH together — 1.0 = full (legacy)
        # smoothing (most stable, most ghost); 0.0 = none (tracks the source
        # exactly, but flickers on noisy content). HW A/B established that the
        # hysteresis is the dominant ghost source, the EMA the secondary one, so
        # a single dial over both is the right control. See docs/architecture.md.
        # Derives _ema_alpha + the two hysteresis bonuses from _penalty_scale
        # (set just above) — the `motion_smoothing` property setter, reused so the
        # live knob re-derives them identically. See its definition below.
        self.motion_smoothing = motion_smoothing
        self._dither_method = dither_method
        self._dither_strength = dither_strength
        # Per-cell 3-color selection strategy for the percell path (see
        # CELL_STRATEGIES / pick_cell_colors). Orthogonal to palette_mode
        # (which only decides percell-vs-global) and to dither (which decides
        # the per-pixel fill after the 3 colors are chosen).
        self._cell_strategy = cell_strategy
        # Per-palette pairwise distances (no penalty — this is for the
        # "snap unused indices to their nearest of the 4 winners" remap,
        # which is a pure color-space neighbor query, not a chromatic-
        # preference question. Match the active metric so the remap agrees
        # with the per-pixel picks.
        self._pal_pairwise = quantize_distances_for(
            C64_PALETTE_BGR, perceptual=self._perceptual
        )  # (16, 16)
        self._last_bg: int | None = None
        self._fixed_slots: tuple[int, ...] | None = None
        self._fixed_lut: np.ndarray | None = None
        self._apply_grayscale_fixed_slots()
        # EMA-smoothed counts for cheap/vivid/percell global picks; see
        # base.PALETTE_PICK_EMA_ALPHA.
        self._smoothed_counts: np.ndarray | None = None
        # EMA-smoothed per-cell counts for percell top-3 picks; see
        # base.PERCELL_PICK_EMA_ALPHA. Shape (1000, 16), float32.
        self._smoothed_cell_counts: np.ndarray | None = None
        # Per-pixel bitmap-code hysteresis state for the percell path: the
        # previous frame's cell candidate sets (1000, 4) and per-pixel codes
        # (1000, 32). The hysteresis only applies to cells whose cand is
        # bit-identical to last frame — when the cell's {bg0,c1,c2,c3}
        # changes, the codes (0..3) point at different palette entries and
        # the previous codes are meaningless, so we fall back to argmin.
        self._last_cand: np.ndarray | None = None
        self._last_codes: np.ndarray | None = None
        # Per-pixel previous-frame palette index for the percell path. See
        # base.PERCELL_QUANT_HYSTERESIS_BONUS. Shape (32000,) int64.
        self._last_quantized: np.ndarray | None = None
        # Previous frame's error-min trio (see base.ERROR_MIN_HYSTERESIS_MARGIN).
        # Only error-min consumes it; other strategies leave it unread. Shape
        # (1000, 3) int64.
        self._last_error_trio: np.ndarray | None = None
        # Sticky bg0 for the percell path (see BG0_HYSTERESIS_MARGIN). None =
        # no prior pick, so the first frame takes the raw argmax.
        self._bg0: int | None = None
        # Opt-in REU bank-swap pipeline. See MHIRES_BANK_SWAP_IRQ_HANDLER
        # and push_mhires_via_reu for the per-frame mechanics. When the
        # scene also opts into [audio].use_reu_pump, setup() installs the
        # merged dispatcher (MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER)
        # which JMPs to the audio pump at $C100 on non-raster IRQs.
        self.use_reu_staged = use_reu_staged
        # Host-DMA double-buffer (no-REU backends, e.g. TeensyROM): tear-free
        # bitmap+screen via off-screen-bank writes + a vblank $DD00 flip. Color
        # RAM ($D800) is shared/un-banked so the c3 slot still tears briefly;
        # mutually exclusive with use_reu_staged (resolve_double_buffer ensures).
        self.double_buffer = double_buffer
        self.audio_reu_pump_active = audio_reu_pump_active
        self._displayed_bank = 0

    def _apply_grayscale_fixed_slots(self) -> None:
        """Recompute the fixed 4-of-5 gray-axis slot assignment + LUT for
        grayscale mode, or clear both for cheap/vivid. Shared between
        __init__ and cycle_style so the slot state stays in lockstep with
        self.palette_mode."""
        if self.palette_mode == "grayscale":
            self._fixed_slots = GRAYSCALE_MHIRES_SLOTS
            self._fixed_lut = np.argmin(
                self._pal_pairwise[:, list(GRAYSCALE_MHIRES_SLOTS)], axis=1
            ).astype(np.uint8)
        else:
            self._fixed_slots = None
            self._fixed_lut = None

    def set_palette_mode(self, api, palette_mode: str, *, force_palette: bool | None = None) -> str:
        """Apply `palette_mode` (and optionally the forced-palette flag) to the
        running instance — shared by the SHIFT cycle and the on-C64 menu. Resets
        all per-frame EMA/hysteresis state and invalidates the delta cache so the
        next frame re-picks slots and fully repaints. Returns the SHIFT label."""
        validate_palette_mode(palette_mode)
        self.palette_mode = palette_mode
        if force_palette is not None:
            self._force_palette = force_palette
        self._sat_factor, self._gray_penalty = palette_mode_settings(palette_mode)
        self._apply_grayscale_fixed_slots()
        self._smoothed_counts = None
        self._smoothed_cell_counts = None
        self._last_cand = None
        self._last_codes = None
        self._last_quantized = None
        self._last_error_trio = None
        self._bg0 = None
        self._last_bg = None
        api.invalidate_cache()
        return f"palette_mode={palette_mode}" + ("+forced" if self._force_palette else "")

    def cycle_style(self, api):
        new_mode, new_force, _label = advance_palette_cycle(
            self.palette_mode, self._force_palette, self._color_map is not None
        )
        return self.set_palette_mode(api, new_mode, force_palette=new_force)

    # --- live-tune setters (see DisplayMode.LIVE_PARAMS / LIVE_CHOICES) ---
    @property
    def dither_strength(self) -> float:
        return self._dither_strength

    @dither_strength.setter
    def dither_strength(self, value: float) -> None:
        self._dither_strength = float(value)

    @property
    def motion_smoothing(self) -> float:
        return self._motion_smoothing

    @motion_smoothing.setter
    def motion_smoothing(self, value: float) -> None:
        # Re-derive the temporal flicker-suppression buffers from the dial: the
        # per-cell color-count EMA weight and the per-pixel/per-cell decision
        # hysteresis (in d² space, so × _penalty_scale). 1.0 = full (legacy)
        # smoothing; 0.0 = none. Identical math to __init__ (which calls this)
        # so the live knob and the config path stay in lockstep.
        #
        # base.ERROR_MIN_HYSTERESIS_MARGIN is deliberately NOT scaled by `s`
        # here, unlike the three above. Those trade motion-tracking speed for
        # stability — an EMA/hysteresis smooths across real frame-to-frame
        # change too, not just noise, hence "0.0 tracks the source exactly."
        # The error-min margin only breaks near-ties between two candidate
        # trios that already score almost identically; a genuinely better
        # trio's error improvement clears even a much larger margin on a
        # single frame (measured offline — see that constant's comment), so
        # there is no motion cost to scale away. Scaling it by `s` anyway left
        # it at ~0.06 under the config default (motion_smoothing 0.25), too
        # weak to suppress the near-tie flicker it exists for.
        s = min(1.0, max(0.0, float(value)))
        self._motion_smoothing = s
        self._ema_alpha = 1.0 - s * (1.0 - base.PERCELL_PICK_EMA_ALPHA)
        self._quant_hysteresis = base.PERCELL_QUANT_HYSTERESIS_BONUS * s * self._penalty_scale
        self._code_hysteresis = base.PERCELL_CODE_HYSTERESIS_BONUS * s * self._penalty_scale
        self._error_min_margin = base.ERROR_MIN_HYSTERESIS_MARGIN

    def set_dither_method(self, value: str) -> str:
        self._dither_method = value
        return f"dither_method={value}"

    def set_cell_strategy(self, value: str) -> str:
        """Live-swap the per-cell 3-color pick.

        Pinned while blending, for the reason __init__ gives: the widened
        palette fragments a frequency pick and needs the error fit."""
        validate_cell_strategy(value)
        if self._blend_table is not None:
            return "cell_strategy=error-min (pinned by flicker_tolerance)"
        self._cell_strategy = value
        return f"cell_strategy={value}"

    def set_color_match(self, value: str) -> str:
        """Live-swap the nearest-palette metric. Re-derives everything that lives
        in d² space and depends on it: the gray-penalty scale, the two percell
        hysteresis bonuses (via the motion_smoothing setter), and the per-palette
        pairwise-distance table used by the unused-index remap.

        Pinned while blending, for the reason __init__ gives: the widened palette
        is Lab-defined and measurably regresses under weighted-BGR."""
        if self._blend_table is not None:
            return "color_match=perceptual (pinned by flicker_tolerance)"
        self._perceptual = value == "perceptual"
        self._penalty_scale = PERCEPTUAL_DIST_SCALE if self._perceptual else 1.0
        # Re-derive the hysteresis at the new penalty scale (keeps motion_smoothing).
        self.motion_smoothing = self._motion_smoothing
        self._pal_pairwise = quantize_distances_for(C64_PALETTE_BGR, perceptual=self._perceptual)
        return f"color_match={value}"

    def setup(self, api):
        super().setup(api)
        # Single-buffer bring-up clears $2000+$0400 before the $D011 flip (engage
        # clean-field — see engage_bitmap_mode); the REU / host-DMA double-buffer
        # paths zero both VIC banks themselves below (clear=False). border ($D020)
        # AND bg0 ($D021) = black on EVERY path so the pre-first-frame screen is
        # solid black (a zeroed mhires bitmap is all-%00 → bg0). On the REU path
        # this is a deliberate belt-and-braces write: the swap-tracker IRQ only
        # writes $D021 on the first REAL swap (frame tracker's ready flag starts
        # zeroed — see install_bank_swap_irq), so without this the screen would
        # show whatever the previous scene left in $D021 (e.g. a stale blue) for
        # every frame between this setup() and that first swap. The IRQ still
        # owns $D021 from the first real swap onward; this just closes the gap.
        single_buffer = not self.use_reu_staged and not self.double_buffer
        engage_bitmap_mode(
            api,
            d011="3b",
            d018="18",
            d016="18",
            border=0x00,
            bg0=0x00,
            clear=single_buffer,
        )
        self._smoothed_cell_counts = None
        self._last_cand = None
        self._last_codes = None
        self._last_quantized = None
        self._last_error_trio = None
        self._bg0 = None
        if not self.use_reu_staged:
            # _last_bg tracks the host-written $D021 (single-buffer only — the
            # double-buffer path flips $D021 via the swap tracker instead).
            self._last_bg = 0
        if self._blend_table is not None:
            # Bank-swapping double-buffer with a second screen page per bank and
            # the field-alternating swap IRQ. self.double_buffer stays False for
            # this: the plain host-DMA path installs a swap handler with no $D018
            # phase toggle, and the two cannot share $0314. The handler itself is
            # the hires one unmodified — it already writes bg0 to $D021, which
            # hires ignores and mhires needs.
            self._setup_flicker_doublebuffer(api)
            self._log_flicker_arming(
                self._blend_table, blendable="c1 + c2 (c3 is $D800 and bg0 is $D021 — both solid)"
            )
            if self.palette_mode != "percell":
                log.warning(
                    "mhires: palette_mode=%r allocates 4 global slots and does not "
                    "blend — the pages are held identical and the picture is the "
                    "plain 16-color one. Blending needs the per-cell slot pick.",
                    self.palette_mode,
                )
        if self.double_buffer:
            # Host-DMA double-buffer: zero both banks + install the minimal
            # vblank swap IRQ (no REU). Bitmap+screen go tear-free; the shared
            # $D800 color RAM still tears briefly (the c3 slot) before each flip.
            self._setup_hostdma_doublebuffer(api)
            log.info(
                "mhires: host-DMA double-buffer armed (bank 0 ↔ bank 2, "
                "IRQ @ $%04X, tracker @ $%04X; bitmap+screen tear-free, "
                "color RAM (c3) tears briefly)",
                BANK_SWAP_IRQ_HANDLER_ADDR,
                FRAME_TRACKER_ADDR,
            )
        if self.use_reu_staged:
            self._last_bg = None
            # Zero both banks' bitmap + screen so the off-screen bank doesn't
            # show garbage on the first swap. Color RAM ($D800) isn't banked
            # — the first IRQ after install overwrites it from REU, so the
            # one-frame stale window (post-reset $D800 contents through
            # whatever the prior scene left there) is acceptable.
            zeros_bitmap = bytes(REU_VIDEO_BITMAP_LEN)
            zeros_screen = bytes(REU_VIDEO_BITMAP_SCREEN_LEN)
            api.write_memory_file(f"{VIC_BANK_0.BITMAP:04X}", zeros_bitmap)
            api.write_memory_file(f"{VIC_BANK_0.SCREEN:04X}", zeros_screen)
            api.write_memory_file(f"{VIC_BANK_2.BITMAP:04X}", zeros_bitmap)
            api.write_memory_file(f"{VIC_BANK_2.SCREEN:04X}", zeros_screen)
            api.write_memory(f"{CIA2.PORT_A:04X}", f"{DD00_BANK_0:02X}")
            self._displayed_bank = 0
            handler = (
                MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER
                if self.audio_reu_pump_active
                else MHIRES_BANK_SWAP_IRQ_HANDLER
            )
            install_bank_swap_irq(
                api, handler, MHIRES_FRAME_TRACKER_LEN, audio_pump_active=self.audio_reu_pump_active
            )
            log.info(
                "mhires: REU bank-swap pipeline armed "
                "(bank 0 ↔ bank 2, IRQ @ $%04X, tracker @ $%04X, "
                "color RAM via vblank DMA, audio_pump=%s, "
                "REC=%s)",
                BANK_SWAP_IRQ_HANDLER_ADDR,
                FRAME_TRACKER_ADDR,
                self.audio_reu_pump_active,
                "chunked-100B" if self.audio_reu_pump_active else "monolithic",
            )

    def teardown(self, api):
        if self.use_reu_staged or self.double_buffer or self._blend_table is not None:
            uninstall_bank_swap_irq(api)
            if self._blend_table is not None:
                # uninstall restores $DD00 but not $D018, and the flicker handler
                # may have left it on the $0C00 page. Nothing else re-asserts it
                # on the way into a char scene, which would then read its matrix
                # from the wrong offset. Safe only after uninstall — before it,
                # the next field's IRQ would put the page value straight back.
                api.write_memory(f"{VIC.D018_MEMORY:04X}", "14")
            api.invalidate_cache()

    def _entry_penalty(self, table: BlendTable) -> np.ndarray:
        """The gray penalty over the widened entry space, (N,).

        A blend's penalty is the mean of its two components': a pair of grays is
        as gray as either, and a gray paired with a chromatic sits half-way,
        which is what its fused color is. Derived from the same (16,) vector
        palette_mode_settings hands out rather than a second table, so a
        palette_mode swap moves both together."""
        return self._gray_penalty[table.pairs].mean(axis=1).astype(np.float32)

    def compose(self, frame) -> MHiresComposeBuffers:
        assert self.frame_target_size is not None
        img = cv2.resize(frame, self.frame_target_size, interpolation=cv2.INTER_AREA)
        forced = self._force_palette and self._color_map is not None
        # Blending needs the per-cell slot pick: the global-4 modes choose one
        # set for the frame, and grayscale's slots are fixed on purpose (see the
        # class docstring), so neither has a per-cell decision for a pair to win.
        # The forced-palette remap opts out for a different reason — it exists to
        # emit exactly the colors it was given, and a blend is not one of them.
        table = self._blend_table if self.palette_mode == "percell" and not forced else None
        if forced:
            # Forced-palette remap: emit exact C64 colors and skip the faithful
            # shaping stages + gray penalty (the remap already chose each color).
            assert self._color_map is not None
            flat = self._color_map.apply(img).reshape(-1, 3).astype(np.float32)
            d = quantize_distances(flat)
        else:
            fit = self._fit_for_apply()
            if fit is not None:
                img = apply_color_fit(img, fit)
            img = boost_saturation(img, self._sat_factor)
            # Global [color] shaping: hue-band corrections then per-channel boost.
            img = apply_hue_corrections(img, self._hue_corrections)
            flat = np.clip(img.reshape(-1, 3).astype(np.float32) * self._channel_boost, 0, 255)
            offset_fn = ORDERED_DITHER_OFFSET_FNS.get(self._dither_method)
            if offset_fn is not None:
                w, h = self.frame_target_size
                offset = offset_fn(h, w, self._dither_strength)
                flat = np.clip(flat + offset.reshape(-1, 1), 0, 255)
            # In-place gray-penalty add (scaled to the active metric) avoids a
            # second (N,16) float32 alloc.
            if table is None:
                d = quantize_distances_for(flat, perceptual=self._perceptual)
                d += self._gray_penalty * self._penalty_scale
            else:
                d = blend_distances_for(flat, table, perceptual=self._perceptual)
                d += self._entry_penalty(table) * self._penalty_scale

        if self.palette_mode == "percell":
            bitmap_ram, screen_ram, color_ram, bg0, screen_b = self._compose_percell(d, flat, table)
        else:
            bitmap_ram, screen_ram, color_ram, bg0 = self._compose_global(d)
            screen_b = None
        buffers: MHiresComposeBuffers = {
            "bitmap": bitmap_ram,
            "screen": screen_ram,
            "color": color_ram,
            "bg": bg0,
            "text": MHiresTextSurface(
                bitmap_ram, screen_ram, color_ram, double_height=self.text_double_height
            ),
        }
        if self._blend_table is None:
            return buffers
        # Armed but not blending this frame (a global palette_mode, or the
        # forced-palette remap) still owes push() a second page: the handler
        # alternates whatever is in the two pages regardless, so the B page has
        # to be a real copy rather than left holding the last blended frame.
        flicker = cast(MHiresFlickerComposeBuffers, buffers)
        flicker["screen_b"] = screen_ram if screen_b is None else screen_b
        return flicker

    def push(self, api: C64Backend, buffers: MHiresComposeBuffers) -> None:
        bg0 = buffers["bg"]
        bitmap_bytes = buffers["bitmap"].tobytes()
        screen_bytes = buffers["screen"].tobytes()
        color_bytes = buffers["color"].tobytes()
        if self._blend_table is not None:
            # Both pages plus the shared bitmap into the off-screen bank, then
            # color RAM into the SHARED $D800 (same brief c3 tear as the plain
            # host-DMA double-buffer — one copy serves both banks and both
            # fields), then arm. The field alternation is already free-running
            # against the displayed bank, so this stages a whole new pair set and
            # the next phase-0 vblank brings it up in one piece.
            (
                target,
                bm_addr,
                page_a,
                page_b,
                bm_id,
                page_a_id,
                page_b_id,
                dd00,
            ) = self._flicker_swap_target()
            page_b_bytes = cast(MHiresFlickerComposeBuffers, buffers)["screen_b"].tobytes()
            api.write_region(bm_addr, bitmap_bytes, region_id=bm_id)
            api.write_region(page_a, screen_bytes, region_id=page_a_id)
            api.write_region(page_b, page_b_bytes, region_id=page_b_id)
            api.write_region(0xD800, color_bytes, region_id=RegionID.COLOR)
            self._arm_flicker_swap(api, bg0, dd00)
            self._displayed_bank = target
            return
        if self.use_reu_staged:
            target_bank = 1 - self._displayed_bank
            push_mhires_via_reu(api, bitmap_bytes, screen_bytes, color_bytes, bg0, target_bank)
            self._displayed_bank = target_bank
            return
        if self.double_buffer:
            # Host-DMA double-buffer: bitmap+screen into the off-screen bank
            # (per-bank delta cache), then color RAM into the SHARED $D800 LAST
            # — written just before arming so its brief c3 tear on the still-
            # displayed bank is minimal — then arm the vblank swap. bg0 flips
            # via the tracker IRQ (atomic with $DD00), so no host $D021 write.
            target, bm_addr, scr_addr, bm_id, scr_id, dd00 = self._hostdma_swap_target()
            api.write_region(bm_addr, bitmap_bytes, region_id=bm_id)
            api.write_region(scr_addr, screen_bytes, region_id=scr_id)
            api.write_region(0xD800, color_bytes, region_id=RegionID.COLOR)
            self._arm_hostdma_swap(api, bg0, dd00)
            self._displayed_bank = target
            return
        if bg0 != self._last_bg:
            api.write_regs("d021", bg0)
            self._last_bg = bg0
        api.write_region(0x0400, screen_bytes, region_id=RegionID.SCREEN)
        api.write_region(0xD800, color_bytes, region_id=RegionID.COLOR)
        api.write_region(0x2000, bitmap_bytes, region_id=RegionID.BITMAP)

    def apply_fade(self, buffers: MHiresComposeBuffers) -> MHiresComposeBuffers:
        """MultiHires colors: screen byte packs c1 (hi nibble) + c2 (lo nibble),
        color RAM holds the per-cell c3, and `bg` is bg0 — all palette indices.
        The bitmap holds 2-bit selectors among {bg0, c1, c2, c3}, so dimming
        those four (via the parent's screen+bg fade plus c3 here) fades the
        whole frame; the bitmap is untouched."""
        out: MHiresComposeBuffers = super().apply_fade(buffers)  # type: ignore[assignment]
        lut = build_fade_lut(self._fade_lut_alpha)
        out["color"] = lut[buffers["color"]]
        return out

    def _compose_global(self, d: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
        """Legacy path: pick 4 global palette slots for the whole frame.
        Used by cheap/vivid/grayscale modes. Returns
        (bitmap_ram, screen_ram, color_ram, bg0)."""
        quantized = np.argmin(d, axis=1)
        if self._fixed_slots is not None:
            bg0, c1, c2, c3 = self._fixed_slots
            assert self._fixed_lut is not None
            lut = self._fixed_lut
        else:
            smoothed = ema_counts(self, quantized)
            if self.palette_mode == "vivid":
                picks = pick_diverse_top_n(smoothed, 4)
            else:
                picks = [int(x) for x in np.argsort(smoothed)[-4:]]
            # Sort by palette index so the slot order is determined by
            # the chosen SET, not by which entry happened to have the
            # highest smoothed count. Without this, even a stable SET
            # flips slot order whenever count rank shuffles, which
            # rewrites screen + color RAM + bg registers every frame and
            # shows up as a rapid palette flicker on the C64 output.
            bg0, c1, c2, c3 = sorted(picks)
            # Build a 16-entry LUT mapping every palette index to the
            # chosen slot (0..3) whose color is closest in weighted BGR
            # space. This remaps the ~12 unused palette indices to a
            # sensible neighbor instead of zero-defaulting them to bg0.
            # For the 4 chosen indices the argmin trivially returns their
            # own slot.
            chosen = [bg0, c1, c2, c3]
            lut = np.argmin(self._pal_pairwise[:, chosen], axis=1).astype(np.uint8)
        mapped = lut[quantized].reshape(200, 160)

        packed = (
            (mapped[:, 0::4] << 6)
            | (mapped[:, 1::4] << 4)
            | (mapped[:, 2::4] << 2)
            | mapped[:, 3::4]
        ).astype(np.uint8)
        bitmap_ram = packed.reshape(25, 8, 40).transpose(0, 2, 1).ravel()

        screen_ram = np.full(1000, (c1 << 4) | c2, dtype=np.uint8)
        color_ram = np.full(1000, c3, dtype=np.uint8)
        return bitmap_ram, screen_ram, color_ram, bg0

    def _compose_percell(
        self, d: np.ndarray, flat: np.ndarray, table: BlendTable | None = None
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, np.ndarray | None]:
        """Per-cell path: pick bg0 globally, then for each 4×8 cell pick
        its own top-3 non-bg0 colors by population. Each pixel resolves
        against its cell's local {bg0, c1, c2, c3} set, so screen RAM and
        color RAM both carry per-cell content instead of one repeated byte.
        Returns (bitmap_ram, screen_ram, color_ram, bg0, screen_b).

        `table` widens the entry space from the 16 colors to those plus the
        eligible blend pairs (flicker blending — see video/flicker.py). Every
        stage below is written against `d.shape[1]` entries rather than 16, so
        picking, smoothing, hysteresis and dithering are shared verbatim; only
        bg0's pick and the final slot split care which entries are pairs.
        `screen_b` is the second screen page, or None when not blending.

        Both the global bg0 pick and the per-cell top-3 picks go through
        EMA-smoothed counts (base.PALETTE_PICK_EMA_ALPHA for bg0,
        base.PERCELL_PICK_EMA_ALPHA for cells) so a few-pixel reshuffle from
        sensor noise can't flip which palette entries win a slot every
        frame. Without per-cell smoothing the unsmoothed top-3 flipped on
        ~7% of cells per frame even on static webcam content, rewriting
        screen + color RAM and remapping each affected cell's bitmap codes."""
        n_entries = d.shape[1]
        quantized = np.argmin(d, axis=1)  # (32000,) entry idx

        # Per-pixel decision hysteresis on the palette index: if the
        # previous frame's choice is within base.PERCELL_QUANT_HYSTERESIS_BONUS
        # of the new minimum distance, keep it. Stabilizes the per-pixel
        # argmin against sensor noise / sub-pixel-shake aliasing on
        # textured static subjects (striped rug, slatted blinds) WITHOUT
        # smearing motion: a real color change moves d² by far more than
        # the bonus, so the new index wins on a single frame.
        if self._last_quantized is not None and self._last_quantized.shape == quantized.shape:
            idx = np.arange(quantized.size)
            d_last = d[idx, self._last_quantized]
            d_min = d[idx, quantized]
            keep = (d_last - d_min) <= self._quant_hysteresis
            quantized = np.where(keep, self._last_quantized, quantized)
        self._last_quantized = quantized

        # bg0 = most-populated palette index across the frame, EMA-smoothed
        # so a few-pixel reshuffle at a chromatic-vs-gray boundary doesn't
        # flip bg0 (and with it, every cell's screen+color RAM byte). On top
        # of the EMA, apply relative hysteresis (BG0_HYSTERESIS_MARGIN): keep
        # the current bg0 unless a challenger's smoothed count beats it by the
        # margin, so near-tied dominants (mostly-black video + a bright moment,
        # or pillarbox bars) stop strobing $D021 every frame while a *sustained*
        # dominant shift still moves bg0.
        smoothed = ema_counts(self, quantized, n_entries)
        # bg0 is the $D021 register, one value for both fields, so it can only
        # be a real color however the counts fall. Costs nothing measurable: the
        # most-populated entry is a solid on real content anyway, because a solid
        # owns a larger region of color space than any pair squeezed between two
        # of them.
        cand = int(np.argmax(smoothed if table is None else smoothed[:16]))
        # Short-circuit keeps the margin index safe when there's no prior bg0.
        prev = self._bg0
        if (
            prev is None
            or cand == prev
            or smoothed[cand] > smoothed[prev] * (1.0 + BG0_HYSTERESIS_MARGIN)
        ):
            bg0 = cand
        else:
            bg0 = prev
        self._bg0 = bg0

        # Per-cell histogram: group into (1000, 32) cell-major layout.
        cells = quantized.reshape(25, 8, 40, 4).transpose(0, 2, 1, 3).reshape(1000, 32)
        d_cell = (
            d.reshape(25, 8, 40, 4, n_entries).transpose(0, 2, 1, 3, 4).reshape(1000, 32, n_entries)
        )

        cell_ids = np.repeat(np.arange(1000), 32)
        combined = cell_ids * n_entries + cells.ravel()
        cell_counts_raw = (
            np.bincount(combined, minlength=1000 * n_entries)
            .reshape(1000, n_entries)
            .astype(np.float32)
        )
        # EMA-smooth so a 1-2 pixel reshuffle from sensor noise on a flat
        # cell doesn't flip the 3rd top-3 slot every frame. The raw counts
        # are stored across all 16 entries (bg0 included) so a future bg0
        # change just remasks — the old-bg0's accumulated count stays valid
        # the moment it becomes pickable again.
        if self._smoothed_cell_counts is None:
            self._smoothed_cell_counts = cell_counts_raw
        else:
            a = self._ema_alpha  # scaled by [color].motion_smoothing (see __init__)
            self._smoothed_cell_counts = (
                self._smoothed_cell_counts * (1.0 - a) + cell_counts_raw * a
            )
        cell_counts = self._smoothed_cell_counts.copy()
        # Exclude bg0 from the per-cell pick — its slot is free via the %00
        # code, so wasting one of c1/c2/c3 on it would shrink the cell's
        # palette to 3.
        cell_counts[:, bg0] = -1.0
        # Top 3 candidate indices per cell. argpartition grabs the 3 highest
        # counts, but a cell with fewer than 3 genuinely-present non-bg0 colors
        # — very common, since most cells are mostly bg0 with 0-2 accents, and
        # a small forced palette ([0,4,6,14]) makes it the norm — leaves the
        # surplus slots holding ARBITRARY zero-count palette indices. Those
        # filler indices are poison: (a) they can be a color OUTSIDE the
        # forced palette (e.g. green=5 leaking into a black/purple/blue cast),
        # and (b) they shuffle frame-to-frame (argpartition tie order + EMA
        # jitter on the near-zero counts), which flips the sorted slot position
        # of the real colors and so rewrites screen/color RAM + bitmap codes
        # every frame on an otherwise-static cell.
        #
        # In steady state the garbage is never *selected* — present pixels
        # resolve to their own in-set color, so the filler slot stays unused
        # and invisible. But push() ships screen ($0400) / color ($D800) /
        # bitmap ($2000) as three NON-ATOMIC writes; on a slow transport
        # (TeensyROM serial, ~10 KB/frame ack-gated) the VIC can read a new
        # bitmap byte against a still-stale screen/color byte mid-frame and
        # briefly render the garbage filler — the green-square flicker (and,
        # on letterboxed video, the all-bg0 edge cells flashing = the
        # "flashing border"). On the U64's fast DMA the tear window is too
        # small to see, which is why it's TR-specific.
        #
        # Fix: replace any pick whose smoothed count is 0 (never present in
        # this cell) with bg0. screen/color RAM then only ever carries colors
        # genuinely present in the cell — so nothing outside the source's
        # color set can leak — and the absent slots become a deterministic
        # bg0, so present colors stop churning slots. bg0 in a filler slot is a
        # harmless duplicate: the %00 code already reaches bg0, and the
        # per-pixel argmin breaks ties to the real bg0 at slot 0.
        #
        # _cell_strategy decides WHICH 3 present colors fill c1/c2/c3 (frequency
        # / luminance / contrast / error-min — see pick_cell_colors). All keep
        # the absent→bg0 poison-filler guard above.
        top3 = pick_cell_colors(
            cell_counts,
            d_cell,
            bg0,
            self._cell_strategy,
            PALETTE_LUMA if table is None else table.luma,
            self._last_error_trio,
            self._error_min_margin,
        )
        # error-min's own temporal hysteresis (see base.ERROR_MIN_HYSTERESIS_MARGIN)
        # needs this frame's winning trio for next frame's comparison; harmless to
        # store unconditionally since other strategies never read it back.
        self._last_error_trio = top3
        # Sort by palette index for delta-cache stability (otherwise the slot
        # order would flip even when the chosen SET is identical).
        top3 = np.sort(top3, axis=1)
        if table is not None:
            top3 = _solid_last(top3, table)
        cand = np.column_stack([np.full(1000, bg0, dtype=np.int64), top3])  # (1000, 4)

        if self._dither_method in ("floyd-steinberg", "atkinson"):
            # Re-dither each cell's own 8×4 pixels against its resolved
            # candidate set {bg0, c1, c2, c3} — candidate SELECTION (cand,
            # above) stays on the EMA-smoothed histogram + hysteresis for
            # temporal stability; only the per-pixel fill dithers. No
            # cross-frame code hysteresis here: error diffusion recomputes
            # its own state from scratch each frame (see dither.py), so the
            # previous frame's codes aren't meaningful to blend in.
            pixels_cell = (
                flat.reshape(25, 8, 40, 4, 3).transpose(0, 2, 1, 3, 4).reshape(1000, 8, 4, 3)
            )
            cand_bgr = (C64_PALETTE_BGR if table is None else table.bgr)[cand]  # (1000, 4, 3)
            codes = error_diffuse_cells(
                pixels_cell, cand_bgr, self._dither_method, self._dither_strength
            )  # (1000, 8, 4) uint8, already in codes_rc's layout
            codes_rc = codes
            self._last_codes = codes.reshape(1000, 32)
            self._last_cand = cand
        else:
            # Per-cell-pixel distance to the 4 candidates (gather, not broadcast).
            d_cand = np.take_along_axis(
                d_cell, cand[:, None, :].repeat(32, axis=1), axis=2
            )  # (1000,32,4)
            codes = d_cand.argmin(axis=2).astype(np.uint8)  # 0..3

            # Per-pixel hysteresis: keep the previous frame's code when it's
            # within base.PERCELL_CODE_HYSTERESIS_BONUS of the new minimum distance,
            # but only for cells whose cand is bit-identical to last frame (a
            # change in any cand slot means the codes 0..3 no longer point at
            # the same palette entries they did last frame, so previous codes
            # are meaningless). Suppresses the per-pixel boundary flicker that
            # remains after the per-cell EMA stabilizes {bg0,c1,c2,c3}.
            if self._last_codes is not None and self._last_cand is not None:
                cell_unchanged = np.all(cand == self._last_cand, axis=1)  # (1000,) bool
                if cell_unchanged.any():
                    last = self._last_codes  # (1000, 32) uint8
                    d_last = np.take_along_axis(d_cand, last[..., None].astype(np.intp), axis=2)[
                        ..., 0
                    ]  # (1000, 32)
                    d_min = np.take_along_axis(d_cand, codes[..., None].astype(np.intp), axis=2)[
                        ..., 0
                    ]
                    keep = ((d_last - d_min) <= self._code_hysteresis) & cell_unchanged[:, None]
                    codes = np.where(keep, last, codes).astype(np.uint8)
            self._last_codes = codes
            self._last_cand = cand
            codes_rc = codes.reshape(1000, 8, 4)

        # Pack into bitmap layout: 8 rows × 4 px per cell → 8 bytes per cell.
        bitmap_ram = (
            (
                (codes_rc[..., 0] << 6)
                | (codes_rc[..., 1] << 4)
                | (codes_rc[..., 2] << 2)
                | codes_rc[..., 3]
            )
            .astype(np.uint8)
            .ravel()
        )

        # Screen RAM nibbles = (c1, c2) per cell; color RAM = c3 per cell.
        if table is None:
            screen_ram = ((top3[:, 0] << 4) | top3[:, 1]).astype(np.uint8)
            return bitmap_ram, screen_ram, top3[:, 2].astype(np.uint8), bg0, None
        # Blending: c1 and c2 split into the color each field shows, c3 is
        # already a real color (_solid_last put one there). A slot holding a
        # solid writes the same nibble to both pages and simply doesn't
        # alternate, so no branch is needed for the mixed case.
        c1_a, c1_b = table.field_pages(top3[:, 0])
        c2_a, c2_b = table.field_pages(top3[:, 1])
        screen_ram = ((c1_a << 4) | c2_a).astype(np.uint8)
        screen_b = ((c1_b << 4) | c2_b).astype(np.uint8)
        return bitmap_ram, screen_ram, top3[:, 2].astype(np.uint8), bg0, screen_b
