"""VIC-II display mode renderers.

Each mode owns the conversion from an OpenCV BGR frame to the byte layout
the VIC-II expects, plus the register pokes needed to put the chip into
that mode. All renderers go through C64Backend.write_region so the
delta-upload cache can skip unchanged bytes.
"""

from __future__ import annotations

import itertools
import logging
from typing import TypedDict

import cv2
import numpy as np

from .audio import REU_PUMP_BODY_SUBROUTINE_ADDR
from .backend import C64Backend
from .c64 import (
    CIA1,
    CIA2,
    KERNAL,
    RASTER_VBLANK_LINE,
    REU,
    SCREEN,
    VECTORS,
    VIC_BANK_0,
    VIC_BANK_2,
    RegionID,
)
from .dither import bayer_offset, blue_noise_offset, error_diffuse_cells
from .palette import (
    C64_PALETTE_BGR,
    CELL_STRATEGIES,
    DEFAULT_HUE_CORRECTIONS,
    GRAYSCALE_CHROMATIC_PENALTY,
    PALETTE_LUMA,
    PERCEPTUAL_DIST_SCALE,
    ColorFit,
    ColorMap,
    HueCorrection,
    apply_color_fit,
    apply_hue_corrections,
    boost_saturation,
    build_fade_lut,
    make_gray_penalty,
    parse_channel_boost,
    parse_hue_corrections,
    pick_diverse_top_n,
    quantize_distances,
    quantize_distances_for,
    quantize_flat_for,
)
from .petscii_styles import (
    RANDOM_STYLE,
    STYLE_NAMES,
    make_style,
    pick_random_style_name,
    validate_style,
)
from .text_surface import (
    CharTextSurface,
    HiresTextSurface,
    MHiresTextSurface,
    TextSurface,
)

# Both are pure additive (h, w) offsets with the same strength semantics
# (see dither.py) — dispatch table for the three compose() call sites below
# rather than duplicating the ordered/blue_noise branch three times.
_ORDERED_DITHER_OFFSET_FNS = {"ordered": bayer_offset, "blue_noise": blue_noise_offset}


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
# on textured static subjects, weak enough that real colour changes still
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
# Stabilising the per-pixel argmin upstream means per-cell histograms
# stay stable, top-3 picks stay stable, cand stays stable, and the code
# hysteresis stays armed — every layer benefits. Unlike input-frame EMA
# (which smears motion as the smoothed input chases the real one), this
# is a *decision* hysteresis: when a pixel's actual colour shifts enough
# that the alternative palette entry is meaningfully better, the
# threshold is exceeded on a single frame and the new index wins
# immediately. No motion smear, no ghosting.
#
# 5000 in d² space suppresses up to ~10-LSB-per-channel sensor noise
# (which moves d² by ~3000 for a typical near-boundary pixel), while a
# 25-LSB real colour change (d² shift ~22000) still releases on a single
# frame. Tuned upward from the initial 2000 because residual rug-style
# flicker (textured static subjects + ~8 LSB webcam noise) was still
# crossing the threshold; 5000 fully suppresses it without introducing
# any motion lag (since real motion exceeds 5000 trivially).
PERCELL_QUANT_HYSTERESIS_BONUS = 5000.0

# bg0 stickiness for the percell path. bg0 (the global %00 colour, written to
# $D021) is picked each frame as argmax of the EMA-smoothed palette counts. On
# content where two colours are near-tied for most-populated — e.g. a mostly-
# black frame with a bright moment, or letterboxed/pillarboxed video whose bars
# quantize to black — the argmax flip-flops frame-to-frame, and since bg0 fills
# every %00 pixel (background + the bars) the whole field strobes a different
# colour for a frame. That's a single instant $D021 change, not a write tear, so
# it's especially visible on a slow transport where the rest of the frame lags.
#
# Fix: make bg0 sticky. Keep the current bg0 unless a challenger's smoothed
# count beats it by this relative margin — so bg0 still tracks a *sustained*
# dominant-colour change (a real blue scene eventually turns the bars blue) but
# stops flickering between near-equal dominants. If the old bg0 vanishes from the
# frame its smoothed count → ~0 and any challenger trivially clears the margin,
# so bg0 can never get stuck on an absent colour.
BG0_HYSTERESIS_MARGIN = 0.25

# Per-cell color-selection strategies for the mhires percell path. Each 4×8 cell
# gets bg0 (global) plus 3 per-cell colors (c1/c2/c3); the strategy decides WHICH
# 3 of the cell's present colors fill those slots. See _pick_cell_colors.
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


def _validate_cell_strategy(strategy: str) -> None:
    if strategy not in CELL_STRATEGIES:
        raise ValueError(f"cell_strategy must be one of {CELL_STRATEGIES}, got {strategy!r}")


def _pick_cell_colors(
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


def _ema_counts(mode, per_pixel: np.ndarray) -> np.ndarray:
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
# _resolve_color_shaping / DEFAULT_HUE_CORRECTIONS. percell leads the tuple so
# it's the default and the natural SHIFT-cycle starting point.
PALETTE_MODES = ("percell", "cheap", "vivid", "grayscale")


# --- REU-staged video pipeline (experimental, opt-in) --------------------
# Selected char-mode display modes can render their per-frame screen RAM by
# first pushing the 1000 bytes to REU SRAM via socket DMA opcode 0xFF07
# (REUWRITE — bus-clean, no SID perturbation), then triggering a REU→main
# DMA on the C64 to drop the screen bytes into VIC's screen-RAM area in one
# shot. Color RAM ($D800) is never banked and stays on the regular
# DMAWRITE path. The opt-in flag flows from `[video].use_reu_staged` in
# TOML to the display mode constructor.
#
# Slice 1 (current): single-buffer — REU→main writes into the
# currently-displayed $0400. The screen RAM gets stomped during the
# transfer, but the visible artifact is one frame's worth at most. No bank
# swap yet (the bank-swap bytes are defined in c64.VIC_BANK_0 / VIC_BANK_2
# / CIA2.PORT_A_BANK_* and ready for a future slice that pairs the REU
# trigger with a $DD00 swap via a C64-side handler).
#
# Coexistence with REU audio: this path drives the REU controller's REC
# registers from the host, while the REU audio pump (audio.start_for_reu_staged)
# drives them from a kernal-IRQ handler on the C64. They share one set of
# registers — if both are active, REU writes will interleave unpredictably
# and audio will glitch or stop. Mutual exclusion is enforced at scene
# setup; the resulting useful pairing today is REU video + host-DMA audio
# (e.g. mic on a webcam scene) or REU video + no audio.
REU_VIDEO_SCREEN_BASE = 0xE00000  # 14 MB in — way past any REU audio region
REU_VIDEO_SCREEN_LEN = SCREEN.N_CELLS  # 1000 bytes of PETSCII screen codes

# --- REU-staged bitmap pipeline (double-buffer, bank-swap) ---------------
# HiresDisplayMode opt-in path. Each frame is REUWRITE-staged into REU
# SRAM (bus-clean), then a pair of REU→main DMAs drop the bitmap + screen
# into the OFF-SCREEN VIC bank's addresses while the on-screen bank keeps
# being rendered (no visible tearing during the transfer). A C64-side
# raster IRQ at line $F8 reads a pending-bank byte in main RAM and, when
# set, writes the new $DD00 value to flip which bank VIC fetches from —
# a 1-cycle, vblank-aligned swap. The host alternates target_bank between
# 0 (bank 0 @ $2000/$0400) and 1 (bank 2 @ $A000/$8400) each frame.
#
# Memory map (both banks always reserved while this path is active):
#   Bank 0: bitmap $2000-$3F3F, screen $0400-$07E7
#   Bank 2: bitmap $A000-$BF3F, screen $8400-$87E7
#   Bank 1 unchanged: audio ring at $4000-$5FFF
#   Color RAM at $D800 unused by hires (color encoded in screen RAM nibbles).
#
# REU staging layout (reused each frame; the DMA dest changes per target_bank):
#   $E10000-$E11F3F  bitmap staging (8000 bytes)
#   $E12000-$E123E7  screen staging (1000 bytes)
#
# Coexistence: shares the REC controller with the REU audio pump. Mutex
# is enforced at validate_scene_cfg — REU video on a hires scene cannot
# coexist with REU audio (mic on webcam OR video pre-encode), because
# both arm IRQ handlers via $0314.
REU_VIDEO_BITMAP_BASE = 0xE10000
REU_VIDEO_BITMAP_LEN = SCREEN.BITMAP_BYTES  # 8000 bytes
REU_VIDEO_BITMAP_SCREEN_BASE = 0xE12000  # 1000-byte screen for hires
REU_VIDEO_BITMAP_SCREEN_LEN = SCREEN.N_CELLS
# MultiHires adds per-cell color RAM ($D800) on top of bitmap+screen. Color
# RAM isn't VIC-banked — one shared SRAM is read by VIC regardless of which
# bank is currently displayed — so the IRQ handler triggers a third REU→main
# DMA into $D800 right before the bank swap. The DMA is fast enough (~1000
# cycles ≈ 16 raster lines) that the c3-mismatch window across the bank
# swap is bounded to one VIC cell row at most; on stationary content it's
# imperceptible, on motion content it's a 1-row band of "wrong c3" at the
# tear line that the eye reads as part of the bank-swap location anyway.
REU_VIDEO_BITMAP_COLOR_BASE = 0xE13000  # 1000-byte color RAM staging
REU_VIDEO_BITMAP_COLOR_LEN = SCREEN.N_CELLS

# C64-side bank-swap raster IRQ handler. Lives at $C500 (audio.py owns
# $C000-$C2FF for NMI DAC + REU pump handlers; api.py uses $C300/$C400
# for the SID player + re-INIT stub; big_text.py uses $C000 — but
# big_text is only valid on `blank`/`mcm` scenes, and HiresDisplayMode
# is a bitmap mode, so they never coexist). The frame tracker at
# $C700-$C70F holds everything the IRQ needs per frame, packed
# contiguously so the host can stage a frame in one DMAWRITE.
BANK_SWAP_IRQ_HANDLER_ADDR = 0xC500
FRAME_TRACKER_ADDR = 0xC700

# Frame tracker layout (16 bytes at $C700-$C70F). The host packs this
# in a single 16-byte DMAWRITE per frame — the wire FIFO guarantees
# either all-new or all-old contents on the C64 side, so the IRQ never
# sees half-updated regs paired with a fresh ready flag.
#
#   $C700-$C706 : bitmap REU regs ($DF02-$DF08 pre-staged values, 7 bytes)
#                 c64_lo, c64_hi, reu_lo, reu_mi, reu_hi, len_lo, len_hi
#   $C707-$C70D : screen REU regs (same layout, 7 bytes)
#   $C70E       : pending bank value ($97 = bank 0, $95 = bank 2)
#   $C70F       : ready flag (1 = frame staged, 0 = no new frame)
#
# IRQ handler clears $C70F after committing; host sets $C70F = 1 (last
# byte of the DMAWRITE blob) to arm. A skipped IRQ (ready=0) just chains
# straight to kernal — costs ~13 cycles, negligible.
FRAME_TRACKER_LEN = 16
TRACKER_OFF_BITMAP_REGS = 0  # 7 bytes
TRACKER_OFF_SCREEN_REGS = 7  # 7 bytes
TRACKER_OFF_BANK_VALUE = 14  # 1 byte
TRACKER_OFF_READY_FLAG = 15  # 1 byte

# C64-side raster IRQ handler. On every IRQ at line 248 (vblank):
#   * AND $D019 with $01 — isolate raster source bit. If 0, chain.
#   * Ack raster IRQ ($D019 = $01, write-1-to-clear).
#   * Read $C70F ready flag. If 0, chain (no new frame to swap in).
#   * Copy $C700-$C706 → $DF02-$DF08 (bitmap REU regs).
#   * Trigger bitmap DMA ($DF01 = $91). CPU halts ~8000 cycles while
#     REU→main copies into the off-screen bitmap addr. VIC continues
#     fetching the visible bank (bank 0 or bank 2, whichever was last
#     swapped to). NMI is blocked during the halt — same as host-DMAWRITE.
#   * Copy $C707-$C70D → $DF02-$DF08 (screen REU regs).
#   * Trigger screen DMA ($DF01 = $91). CPU halts ~1000 cycles.
#   * Load $C70E (bank value) and store to $DD00 — 1-cycle swap that
#     flips VIC to the just-painted bank during vblank (tear-free).
#   * Clear $C70F so the next IRQ skips until the host stages a new frame.
#   * Chain to kernal $EA31 for SCNKEY / UDTIM / cursor blink.
#
# Why have the IRQ trigger the DMA instead of the host? Doing it host-
# side adds Python-paced jitter to a sequence that's otherwise deterministic
# (kernal IRQ fires on a clockwork CIA #1 timer). The earlier reu_irq_pump
# experiment ([u64_reu_socket_dma.md] Phase 2 v2) found that deterministic
# C64-side IRQ-paced REU DMAs sounded perceptually cleaner than jittery
# host-paced ones, even when measured sideband power was the same or higher.
# Moving the trigger here also collapses 6 host socket round-trips per
# frame (2× setup + 2× trigger + pending flag) into 1 (the 16-byte tracker
# DMAWRITE), and eliminates host-induced mid-frame bus halts entirely.
#
# A/X/Y survive: kernal $FF48 saved A/X/Y before vectoring through
# $0314; our handler uses A + X, both of which $EA81 restores via PLA.
# Same convention as big_text.py's raster IRQ ([overlays/big_text.py:104])
# and audio.py's REU pump ([audio.py REU_IRQ_HANDLER]).
#
# Offsets must be exact: BEQ at offset 5 (+51 → 58), BEQ at offset 13
# (+43 → 58), BPL at offsets 24 + 40 (-9 → 17 + 33). The assert below
# catches length drift; if you edit the bytes, recompute all four branches.
BANK_SWAP_IRQ_HANDLER = bytes(
    [
        0xAD,
        0x19,
        0xD0,  # 0  LDA $D019         ; VIC IRQ status
        0x29,
        0x01,  # 3  AND #$01          ; raster bit
        0xF0,
        0x33,  # 5  BEQ +51 → 58      ; not raster → chain
        0x8D,
        0x19,
        0xD0,  # 7  STA $D019         ; ack raster
        0xAD,
        0x0F,
        0xC7,  # 10 LDA $C70F         ; ready flag
        0xF0,
        0x2B,  # 13 BEQ +43 → 58      ; no frame staged → chain
        0xA2,
        0x06,  # 15 LDX #$06
        0xBD,
        0x00,
        0xC7,  # 17 LDA $C700,X       ; copy bitmap regs
        0x9D,
        0x02,
        0xDF,  # 20 STA $DF02,X
        0xCA,  # 23 DEX
        0x10,
        0xF7,  # 24 BPL -9 → 17       ; loop over 7 bytes
        0xA9,
        0x91,  # 26 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 28 STA $DF01         ; trigger bitmap DMA (~8000 cyc halt)
        0xA2,
        0x06,  # 31 LDX #$06
        0xBD,
        0x07,
        0xC7,  # 33 LDA $C707,X       ; copy screen regs
        0x9D,
        0x02,
        0xDF,  # 36 STA $DF02,X
        0xCA,  # 39 DEX
        0x10,
        0xF7,  # 40 BPL -9 → 33       ; loop
        0xA9,
        0x91,  # 42 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 44 STA $DF01         ; trigger screen DMA (~1000 cyc halt)
        0xAD,
        0x0E,
        0xC7,  # 47 LDA $C70E         ; bank value
        0x8D,
        0x00,
        0xDD,  # 50 STA $DD00         ; swap (1 cycle)
        0xA9,
        0x00,  # 53 LDA #$00
        0x8D,
        0x0F,
        0xC7,  # 55 STA $C70F         ; clear ready flag
        0x4C,
        0x31,
        0xEA,  # 58 JMP $EA31         ; chain to kernal
    ]
)
assert len(BANK_SWAP_IRQ_HANDLER) == 61, (
    "BANK_SWAP_IRQ_HANDLER length changed — the 4 branch offsets (+51 and "
    "+43 forward, -9 twice for the loops) must be recomputed before "
    "changing. See the offsets in the byte-comment column."
)


# --- MultiHires bank-swap IRQ handler --------------------------------------
# Extends the hires handler: same bitmap + screen REU→main DMAs, but adds a
# third DMA into shared $D800 color RAM and a $D021 bg0 register write
# before the bank swap. The DMA order matters — see the long comment block
# on MHIRES_FRAME_TRACKER below for why color goes BEFORE the swap, not
# before bitmap/screen (TL;DR: color RAM is read by VIC regardless of bank,
# so updating it before the swap minimizes the bitmap-vs-color mismatch
# window during the visible frame).
#
# Tracker layout extends to 24 bytes at $C700-$C717:
#   $C700-$C706 : bitmap REU regs    ($DF02-$DF08 staged values)
#   $C707-$C70D : screen REU regs
#   $C70E-$C714 : color REU regs (NEW; dest = $D800, len = 1000)
#   $C715       : bg0 value to write to $D021 (NEW)
#   $C716       : pending bank value ($97 = bank 0, $95 = bank 2)
#   $C717       : ready flag (1 = frame staged) — moved from hires's $C70F
#
# The hires and mhires handlers share BANK_SWAP_IRQ_HANDLER_ADDR ($C500)
# and FRAME_TRACKER_ADDR ($C700) because they're mutually exclusive (a
# scene only has one display mode at a time). Each install function writes
# its own handler bytes + tracker length.
#
# Offsets must be exact: BEQ at offset 5 (+73 → 80), BEQ at offset 13
# (+65 → 80), BPL at offsets 24, 40, 56 (all -9 to their respective loop
# starts at offsets 17, 33, 49). The assert below catches length drift;
# if you edit the bytes, recompute all five branches.
MHIRES_BANK_SWAP_IRQ_HANDLER = bytes(
    [
        0xAD,
        0x19,
        0xD0,  # 0  LDA $D019         ; VIC IRQ status
        0x29,
        0x01,  # 3  AND #$01          ; raster bit
        0xF0,
        0x49,  # 5  BEQ +73 → 80      ; not raster → chain
        0x8D,
        0x19,
        0xD0,  # 7  STA $D019         ; ack raster
        0xAD,
        0x17,
        0xC7,  # 10 LDA $C717         ; ready flag
        0xF0,
        0x41,  # 13 BEQ +65 → 80      ; no frame staged → chain
        0xA2,
        0x06,  # 15 LDX #$06
        0xBD,
        0x00,
        0xC7,  # 17 LDA $C700,X       ; copy bitmap regs
        0x9D,
        0x02,
        0xDF,  # 20 STA $DF02,X
        0xCA,  # 23 DEX
        0x10,
        0xF7,  # 24 BPL -9 → 17       ; loop over 7 bytes
        0xA9,
        0x91,  # 26 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 28 STA $DF01         ; trigger bitmap DMA (~8000 cyc halt)
        0xA2,
        0x06,  # 31 LDX #$06
        0xBD,
        0x07,
        0xC7,  # 33 LDA $C707,X       ; copy screen regs
        0x9D,
        0x02,
        0xDF,  # 36 STA $DF02,X
        0xCA,  # 39 DEX
        0x10,
        0xF7,  # 40 BPL -9 → 33       ; loop
        0xA9,
        0x91,  # 42 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 44 STA $DF01         ; trigger screen DMA (~1000 cyc halt)
        0xA2,
        0x06,  # 47 LDX #$06
        0xBD,
        0x0E,
        0xC7,  # 49 LDA $C70E,X       ; copy color regs (NEW)
        0x9D,
        0x02,
        0xDF,  # 52 STA $DF02,X
        0xCA,  # 55 DEX
        0x10,
        0xF7,  # 56 BPL -9 → 49       ; loop
        0xA9,
        0x91,  # 58 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 60 STA $DF01         ; trigger color DMA (~1000 cyc halt)
        0xAD,
        0x15,
        0xC7,  # 63 LDA $C715         ; bg0 value (NEW)
        0x8D,
        0x21,
        0xD0,  # 66 STA $D021         ; set bg0 ($D021)
        0xAD,
        0x16,
        0xC7,  # 69 LDA $C716         ; bank value
        0x8D,
        0x00,
        0xDD,  # 72 STA $DD00         ; swap (1 cycle)
        0xA9,
        0x00,  # 75 LDA #$00
        0x8D,
        0x17,
        0xC7,  # 77 STA $C717         ; clear ready flag
        0x4C,
        0x31,
        0xEA,  # 80 JMP $EA31         ; chain to kernal
    ]
)
assert len(MHIRES_BANK_SWAP_IRQ_HANDLER) == 83, (
    "MHIRES_BANK_SWAP_IRQ_HANDLER length changed — the 5 branch offsets "
    "(+73 and +65 forward, -9 three times for the loops) must be "
    "recomputed before changing. See the offsets in the byte-comment column."
)

# MultiHires tracker (24 bytes at $C700). Layout pairs 1:1 with the handler's
# hardcoded offsets above. The host packs this as a single 24-byte DMAWRITE
# per frame — the socket FIFO guarantees the C64 sees either all-new or
# all-old contents, so the IRQ can't catch ready=1 paired with stale regs.
MHIRES_FRAME_TRACKER_LEN = 24
MHIRES_TRACKER_OFF_BITMAP_REGS = 0  # 7 bytes
MHIRES_TRACKER_OFF_SCREEN_REGS = 7  # 7 bytes
MHIRES_TRACKER_OFF_COLOR_REGS = 14  # 7 bytes
MHIRES_TRACKER_OFF_BG0 = 21  # 1 byte
MHIRES_TRACKER_OFF_BANK_VALUE = 22  # 1 byte
MHIRES_TRACKER_OFF_READY_FLAG = 23  # 1 byte

# --- Merged dispatcher: bank-swap + audio REU pump fall-through ----------
# Today the bank-swap handler at $C500 chains to $EA31 on non-raster IRQs
# (i.e. CIA #1 jiffy). When the scene ALSO opted into REU audio, the
# audio pump handler at $C100 (37 B video / 102 B mic) wants every
# CIA #1 IRQ to run its REU→ring drain. The two handlers can't both own
# $0314 — historically `validate_scene_cfg` rejected the combination.
#
# The merge lifts that restriction by appending `JMP $C100` to the bank-
# swap handler and retargeting its first BEQ ("not raster → chain") to
# fall through to that JMP instead of to the chain-to-kernal. The 6502
# can't preempt IRQ handlers (I flag), so audio + bank-swap serialize
# naturally — each fully completes its REC ($DF02-$DF08) use before
# returning. The audio handler at $C100 stays byte-for-byte identical
# (audio.py owns its bytes; this side only routes execution there).
AUDIO_HANDLER_INSTALL_ADDR = 0xC100  # where audio.py uploads its REU pump
AUDIO_HANDLER_STUB = bytes([0x4C, 0x31, 0xEA])  # JMP $EA31


def _make_merged_handler(base: bytes, audio_jmp_target: int = AUDIO_HANDLER_INSTALL_ADDR) -> bytes:
    """Derive a merged dispatcher from a base bank-swap handler.

    The dispatcher replaces the base handler's trailing `JMP $EA31`
    (chain-to-kernal) with a JMP $EA31 chain followed by a JMP $C100
    audio handler fallthrough target for the non-raster path. The
    first BEQ at offset 5 (non-raster → audio) is retargeted from the
    chain to the audio JMP.

    Layout (offsets relative to base — extension replaces base[-3:]):
        body = base[:-3]
        extension at body_len:
            JMP $EA31         ; +0..2   ; chain to kernal (raster path)
            JMP $C100         ; +3..5   ; audio handler entry (non-raster)

    Empirical history (2026-05-27, Cam Link envelope FFT on day-in-life
    mhires REU bank-swap + REU audio pump):
      * A prior variant inserted an LDA $DC0D / AND #$01 / BNE check
        between the chain and the audio fallthrough — intended to
        recover CIA #1 IRQs that the kernal's $DC0D-read might ACK
        on chain-back. Folded envelope showed 35 % peak-to-peak
        excursion at 30 Hz and 12 % AM depth at 60 Hz.
      * Stripping the check (this form) drops 60 Hz depth ~84 % and
        30 Hz excursion to ~25 %. The kernal-ACK loss is small enough
        in practice (bank-swap halt ~2 ms; only a fraction of CIA #1
        wraps land inside it) that the pump still matches NMI over
        the audio ring's 1-sec buffer.
      * C (REU audio without bank-swap) sits at 1.2 % excursion;
        bank-swap REC DMAs themselves still drive the residual 25 %
        in D and are not addressable without splitting the per-frame
        REC into smaller pieces.
    """
    body = bytes(base[:-3])
    extension = bytes(
        [
            0x4C,
            0x31,
            0xEA,  # +0  JMP $EA31 (chain)
            0x4C,
            audio_jmp_target & 0xFF,  # +3  JMP $C100 (audio fallthrough)
            (audio_jmp_target >> 8) & 0xFF,
        ]
    )
    merged = bytearray(body + extension)
    audio_jmp_offset = len(body) + 3
    new_displacement = audio_jmp_offset - 7
    if not 0 <= new_displacement < 128:
        raise ValueError(
            f"merged handler displacement {new_displacement} out of "
            f"single-byte BEQ range for base handler of {len(base)} bytes"
        )
    merged[6] = new_displacement
    return bytes(merged)


# Pre-built merged dispatchers. Hires base = 61 → merged = 61 - 3 + 13 = 71 B.
# Mhires base = 83 → merged = 83 - 3 + 13 = 93 B. These are installed at
# $C500 in place of the base handlers when the scene combines REU video
# bank-swap with REU audio pump.
BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER = _make_merged_handler(BANK_SWAP_IRQ_HANDLER)
MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER = _make_merged_handler(MHIRES_BANK_SWAP_IRQ_HANDLER)
assert len(BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER) == 64
assert len(MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER) == 86


# --- Chunked mhires merged dispatcher ------------------------------------
# The plain merged dispatcher above triggers one large REC DMA per family
# (bitmap = 8000 bytes ≈ 8 ms halt, screen = 1000 ≈ 1 ms, color = 1000 ≈
# 1 ms). NMI fires at 8 kHz = every 125 cycles (≈ 125 µs at 1 MHz NTSC).
# CIA #2 is edge-triggered through the NMI line: when the bus halt covers
# multiple NMI underflows, the ICR bit latches once and the rest collapse
# into the same edge — losing every NMI past the first per halt.
#
# Empirically (2026-05-27 Cam Link D-vs-C diagnosis): the plain mhires
# merged dispatcher loses ~30 % of NMI events per frame, slowing 8 kHz
# playback to ~5 600 Hz effective. The music's BPM drops to ~70 % and
# the slow drift creates the "echo / time-stretch" the user reported.
#
# Fix: split each REC into 100-byte chunks. 100 bytes × 1 cyc/byte =
# 100 µs halt per chunk, comfortably under the 125 µs NMI period — so
# every NMI underflow lands either between chunks or in the active code
# right after a halt, and is serviced before the next underflow can
# collapse onto it. Bitmap: 80 chunks; screen + color: 10 chunks each.
# After each chunk DMA, only the LENGTH register decrements to 0; the
# src/dst registers auto-increment and stay valid across chunks, so the
# per-chunk inner body is just "reload length, retrigger" + the standard
# DEC/BNE counter.
#
# CIA #1 (audio pump) loss is partially addressed by per-family pump
# JSR calls (3 per bank-swap). After each family's chunk loop ends, the
# handler reads $DC0D / AND #$01 / BEQ skip / JSR $C180 — picking up any
# CIA #1 underflow that latched into the ICR during the family's halt
# time. Per-CHUNK pump checks would be ideal but break the bitmap's REC
# auto-increment (pump_body overwrites $DF02..$DF06 with the audio
# REU/main addresses, so the next bitmap chunk would re-trigger a
# 100-byte transfer from audio → audio rather than the next bitmap
# slice; the per-family check is safe because each family begins with
# its own copy-from-tracker loop that re-sets REC).
#
# Capture rate per ~33 ms bank-swap cycle: bitmap (10.7 ms halt, ~1.07
# underflows, 1 latched) + screen (1.34 ms, ~0.13) + color (1.34 ms,
# ~0.13) + inter-bank-swap gap (19.6 ms, 1.96 normal CIA #1 dispatches
# via the audio fallthrough). Total ≈ 3.22 of 3.30 underflows captured
# (~97 % vs. 67 % baseline). Residual ~3 % loss is below the host
# audio sample queue's hysteresis and not audibly distinguishable from
# the C baseline (REU audio alone with no bank-swap).
#
# Wall-time cost: each chunk adds ~17 cycles of inner-loop overhead
# (length reload + 5-cyc DEC zp + 3-cyc BNE) on top of the 100-cycle
# halt, plus ~50 µs NMI service per chunk on average. Total bank-swap
# wall ≈ 18 ms (vs. ~10 ms for the monolithic merged variant). On NTSC
# (16.6 ms frame) this means bank-swap straddles the frame boundary —
# but the host already produces mhires frames at ~30 fps (per-cell
# quantization is the bottleneck), so the effective display rate is
# unchanged.
#
# Zero-page: the chunk counter lives at $FB (the canonical 4-byte
# user-free block $FB-$FE). c64cast uses no other zero-page slots.
BANK_SWAP_CHUNK_SIZE = 100  # bytes per chunked REC DMA
_BITMAP_CHUNKS = 8000 // BANK_SWAP_CHUNK_SIZE  # 80
_SCREEN_CHUNKS = 1000 // BANK_SWAP_CHUNK_SIZE  # 10
_COLOR_CHUNKS = 1000 // BANK_SWAP_CHUNK_SIZE  # 10
_CHUNK_COUNTER_ZP = 0xFB  # zero-page chunk counter

# The dispatcher is too large for the original 1-byte BEQ displacement
# trick (the audio fallthrough sits ~170 bytes deep). The first two
# branches are inverted to BNE-skip-then-JMP form so they can reach
# any offset in the handler. The rest of the branches stay within
# single-byte range (chunk loops + copy loops are all ≤ 19 bytes;
# pump-check BEQ is +3).
#
# Byte layout (offsets relative to $C500 install address):
#   0-20    Header: raster vs audio dispatch + ready-flag gate
#   21-64   Bitmap family: copy loop (11) + counter init (4) + chunk loop
#           (19) + end-of-family pump check (10) = 44 B
#   65-108  Screen family: same shape
#   109-152 Color family: same shape
#   153-169 Tail: bg0, $DD00 bank swap, clear ready flag
#   170-172 chain: JMP $EA31
#   173-175 audio_fallthrough: JMP $C100
#
# Branch displacements (all verified by the assertion below):
#   offset 5   BNE +3 → 10     (skip JMP audio)
#   offset 7   JMP $C5AD       (audio_fallthrough = $C500 + 173)
#   offset 16  BNE +3 → 21     (skip JMP chain)
#   offset 18  JMP $C5AA       (chain = $C500 + 170)
#   offset 30  BPL -9 → 23     (bitmap copy loop body)
#   offset 53  BNE -19 → 36    (bitmap chunk loop body)
#   offset 60  BEQ +3 → 65     (bitmap end-of-family pump check)
#   offset 74  BPL -9 → 67     (screen copy loop body)
#   offset 97  BNE -19 → 80    (screen chunk loop body)
#   offset 104 BEQ +3 → 109    (screen end-of-family pump check)
#   offset 118 BPL -9 → 111    (color copy loop body)
#   offset 141 BNE -19 → 124   (color chunk loop body)
#   offset 148 BEQ +3 → 153    (color end-of-family pump check)
_PUMP_BODY_LO = 0x80  # REU_PUMP_BODY_SUBROUTINE_ADDR low byte ($C180 & $FF)
_PUMP_BODY_HI = 0xC1  # REU_PUMP_BODY_SUBROUTINE_ADDR high byte ($C180 >> 8)
MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER = bytes(
    [
        # --- Header: dispatch raster vs audio ---
        0xAD,
        0x19,
        0xD0,  # 0   LDA $D019
        0x29,
        0x01,  # 3   AND #$01
        0xD0,
        0x03,  # 5   BNE +3 → 10
        0x4C,
        0xAD,
        0xC5,  # 7   JMP $C5AD (audio fallthrough)
        0x8D,
        0x19,
        0xD0,  # 10  STA $D019 (ack raster)
        0xAD,
        0x17,
        0xC7,  # 13  LDA $C717 (ready flag)
        0xD0,
        0x03,  # 16  BNE +3 → 21
        0x4C,
        0xAA,
        0xC5,  # 18  JMP $C5AA (chain to kernal)
        # --- BITMAP family: 80 chunks × 100 bytes = 8000 bytes ---
        # Copy 5 bytes ($DF02..$DF06 = main lo/hi + REU lo/mi/hi). Length
        # ($DF07/$DF08) is set per-chunk, NOT here.
        0xA2,
        0x04,  # 21  LDX #$04
        0xBD,
        0x00,
        0xC7,  # 23  LDA $C700,X
        0x9D,
        0x02,
        0xDF,  # 26  STA $DF02,X
        0xCA,  # 29  DEX
        0x10,
        0xF7,  # 30  BPL -9 → 23
        0xA9,
        _BITMAP_CHUNKS,  # 32  LDA #80
        0x85,
        _CHUNK_COUNTER_ZP,  # 34  STA $FB
        0xA9,
        BANK_SWAP_CHUNK_SIZE,  # 36  LDA #100 (chunk lo)
        0x8D,
        0x07,
        0xDF,  # 38  STA $DF07
        0xA9,
        0x00,  # 41  LDA #$00 (chunk hi)
        0x8D,
        0x08,
        0xDF,  # 43  STA $DF08
        0xA9,
        0x91,  # 46  LDA #$91 (REU exec REU→C64)
        0x8D,
        0x01,
        0xDF,  # 48  STA $DF01 (trigger ~100 cyc halt)
        0xC6,
        _CHUNK_COUNTER_ZP,  # 51  DEC $FB
        0xD0,
        0xED,  # 53  BNE -19 → 36
        # End-of-bitmap pump check: ack CIA #1 if pending, run pump body.
        # JSR clobbers $DF02..$DF06 — safe because the next family's copy
        # loop re-loads them from the frame tracker.
        0xAD,
        0x0D,
        0xDC,  # 55  LDA $DC0D (ack CIA #1 ICR)
        0x29,
        0x01,  # 58  AND #$01 (timer A bit)
        0xF0,
        0x03,  # 60  BEQ +3 → 65 (skip JSR)
        0x20,
        _PUMP_BODY_LO,
        _PUMP_BODY_HI,  # 62  JSR $C180 (pump body)
        # --- SCREEN family: 10 chunks × 100 bytes = 1000 bytes ---
        0xA2,
        0x04,  # 65  LDX #$04
        0xBD,
        0x07,
        0xC7,  # 67  LDA $C707,X
        0x9D,
        0x02,
        0xDF,  # 70  STA $DF02,X
        0xCA,  # 73  DEX
        0x10,
        0xF7,  # 74  BPL -9 → 67
        0xA9,
        _SCREEN_CHUNKS,  # 76  LDA #10
        0x85,
        _CHUNK_COUNTER_ZP,  # 78  STA $FB
        0xA9,
        BANK_SWAP_CHUNK_SIZE,  # 80  LDA #100
        0x8D,
        0x07,
        0xDF,  # 82  STA $DF07
        0xA9,
        0x00,  # 85  LDA #$00
        0x8D,
        0x08,
        0xDF,  # 87  STA $DF08
        0xA9,
        0x91,  # 90  LDA #$91
        0x8D,
        0x01,
        0xDF,  # 92  STA $DF01 (trigger)
        0xC6,
        _CHUNK_COUNTER_ZP,  # 95  DEC $FB
        0xD0,
        0xED,  # 97  BNE -19 → 80
        # End-of-screen pump check.
        0xAD,
        0x0D,
        0xDC,  # 99  LDA $DC0D
        0x29,
        0x01,  # 102 AND #$01
        0xF0,
        0x03,  # 104 BEQ +3 → 109
        0x20,
        _PUMP_BODY_LO,
        _PUMP_BODY_HI,  # 106 JSR $C180
        # --- COLOR family: 10 chunks × 100 bytes = 1000 bytes ---
        0xA2,
        0x04,  # 109 LDX #$04
        0xBD,
        0x0E,
        0xC7,  # 111 LDA $C70E,X
        0x9D,
        0x02,
        0xDF,  # 114 STA $DF02,X
        0xCA,  # 117 DEX
        0x10,
        0xF7,  # 118 BPL -9 → 111
        0xA9,
        _COLOR_CHUNKS,  # 120 LDA #10
        0x85,
        _CHUNK_COUNTER_ZP,  # 122 STA $FB
        0xA9,
        BANK_SWAP_CHUNK_SIZE,  # 124 LDA #100
        0x8D,
        0x07,
        0xDF,  # 126 STA $DF07
        0xA9,
        0x00,  # 129 LDA #$00
        0x8D,
        0x08,
        0xDF,  # 131 STA $DF08
        0xA9,
        0x91,  # 134 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 136 STA $DF01 (trigger)
        0xC6,
        _CHUNK_COUNTER_ZP,  # 139 DEC $FB
        0xD0,
        0xED,  # 141 BNE -19 → 124
        # End-of-color pump check.
        0xAD,
        0x0D,
        0xDC,  # 143 LDA $DC0D
        0x29,
        0x01,  # 146 AND #$01
        0xF0,
        0x03,  # 148 BEQ +3 → 153
        0x20,
        _PUMP_BODY_LO,
        _PUMP_BODY_HI,  # 150 JSR $C180
        # --- TAIL: bg0, bank swap, clear ready ---
        0xAD,
        0x15,
        0xC7,  # 153 LDA $C715 (bg0)
        0x8D,
        0x21,
        0xD0,  # 156 STA $D021
        0xAD,
        0x16,
        0xC7,  # 159 LDA $C716 (bank value)
        0x8D,
        0x00,
        0xDD,  # 162 STA $DD00 (swap VIC bank)
        0xA9,
        0x00,  # 165 LDA #$00
        0x8D,
        0x17,
        0xC7,  # 167 STA $C717 (clear ready flag)
        # --- EXIT PATHS ---
        0x4C,
        0x31,
        0xEA,  # 170 JMP $EA31 (chain to kernal)
        0x4C,
        AUDIO_HANDLER_INSTALL_ADDR & 0xFF,
        (AUDIO_HANDLER_INSTALL_ADDR >> 8) & 0xFF,  # 173 JMP $C100
    ]
)
assert len(MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER) == 176, (
    "MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER length changed — the "
    "JMP targets at offsets 7 ($C500+173) and 18 ($C500+170), the BPL "
    "offsets in the 3 copy loops, the BNE offsets in the 3 chunk loops, "
    "and the BEQ +3 offsets in the 3 end-of-family pump checks must all "
    "be recomputed before changing. See the offset comments in the byte "
    "column."
)
# Sanity-check the cross-module address coupling between the chunked
# dispatcher (constructed here from raw bytes) and the pump body
# subroutine address (imported from audio.py at the top of the module).
# If audio.py ever relocates REU_PUMP_BODY_SUBROUTINE_ADDR away from
# $C180, the JSR operands inside the dispatcher above must move with it.
assert REU_PUMP_BODY_SUBROUTINE_ADDR == 0xC180
assert _PUMP_BODY_LO == (REU_PUMP_BODY_SUBROUTINE_ADDR & 0xFF)
assert _PUMP_BODY_HI == ((REU_PUMP_BODY_SUBROUTINE_ADDR >> 8) & 0xFF)


# --- Host-DMA double-buffer swap IRQ handler (no-REU backends, e.g. TeensyROM) -
# The minimal sibling of the REU bank-swap handlers above. On a backend whose bus
# DMA is too slow to rewrite a full bitmap frame in the VISIBLE bank without
# tearing (TeensyROM serial/TCP both ~106 KiB/s — the bus, not the link, is the
# wall), the host writes each frame's bitmap+screen straight into the OFF-screen
# VIC bank over the normal host-DMA write_region path, then arms this IRQ to flip
# $DD00 at vblank. The visible bank is never touched mid-display, so every shown
# frame is whole — tear-free at the same frame rate.
#
# Unlike the REU handlers, this does NO in-IRQ DMA — it just writes $D021 (bg0)
# and flips $DD00 from a tiny 3-byte tracker. So the swap lands cleanly inside
# vblank with no past-vblank overrun → no shimmer, and text overlays folded into
# the bitmap render crisply (which the REU path can't claim). NMI audio lives on
# the $FFFA vector, independent of this $0314 raster IRQ, so they coexist; the
# handler chains to kernal $EA31 so SCNKEY keeps $028D live for the key pollers.
#
# Compact tracker at $C700 (reuses FRAME_TRACKER_ADDR — never live alongside the
# REU tracker, since a scene has exactly one display mode):
#   $C700 : bg0 value to write to $D021
#   $C701 : pending bank value ($97 = bank 0, $95 = bank 2)
#   $C702 : ready flag (1 = frame staged) — host arms, handler clears
#
# A/X/Y survive: kernal $FF48 saved them before vectoring through $0314, and we
# only touch A (restored by $EA81's PLA). Offsets must be exact: both BEQs target
# the JMP $EA31 chain at offset 32. The assert below catches length drift.
HOSTDMA_TRACKER_OFF_BG0 = 0  # $C700
HOSTDMA_TRACKER_OFF_BANK = 1  # $C701
HOSTDMA_TRACKER_OFF_READY = 2  # $C702
HOSTDMA_TRACKER_LEN = 3

HOSTDMA_SWAP_IRQ_HANDLER = bytes(
    [
        0xAD,
        0x19,
        0xD0,  # 0  LDA $D019         ; VIC IRQ status
        0x29,
        0x01,  # 3  AND #$01          ; raster bit
        0xF0,
        0x19,  # 5  BEQ +25 → 32      ; not raster → chain
        0x8D,
        0x19,
        0xD0,  # 7  STA $D019         ; ack raster (A = $01)
        0xAD,
        0x02,
        0xC7,  # 10 LDA $C702         ; ready flag
        0xF0,
        0x11,  # 13 BEQ +17 → 32      ; no new frame → chain
        0xAD,
        0x00,
        0xC7,  # 15 LDA $C700         ; bg0
        0x8D,
        0x21,
        0xD0,  # 18 STA $D021         ; set bg0
        0xAD,
        0x01,
        0xC7,  # 21 LDA $C701         ; pending bank value
        0x8D,
        0x00,
        0xDD,  # 24 STA $DD00         ; swap bank (tear-free at vblank)
        0xA9,
        0x00,  # 27 LDA #$00
        0x8D,
        0x02,
        0xC7,  # 29 STA $C702         ; clear ready flag
        0x4C,
        0x31,
        0xEA,  # 32 JMP $EA31         ; chain to kernal
    ]
)
assert len(HOSTDMA_SWAP_IRQ_HANDLER) == 35, (
    "HOSTDMA_SWAP_IRQ_HANDLER length changed — the two BEQ offsets (+25 and "
    "+17, both targeting the JMP $EA31 chain at offset 32) must be recomputed "
    "before changing. See the offsets in the byte-comment column."
)


# CIA #2 PORT_A bank-select values (also defined in c64.CIA2 but pulled
# here so the per-frame push has them as Python ints, not strings — fewer
# allocations on the hot path).
_DD00_BANK_0 = CIA2.PORT_A_BANK_0  # $97
_DD00_BANK_2 = CIA2.PORT_A_BANK_2  # $95

# CIA #1 ICR control words for raster-IRQ bring-up / teardown.
# CIA1_ICR_DISABLE_TIMER_A clears bit 0 of the ICR; CIA1_ICR_ENABLE_TIMER_A
# re-arms it (high bit = 1 = set bits, plus bit 0 = timer A IRQ source).
# Mirrors the audio.py CIA #2 disable/enable pattern but on CIA #1.
_CIA1_ICR_DISABLE_TIMER_A = 0x7F
_CIA1_ICR_ENABLE_TIMER_A = 0x81


def _install_bank_swap_irq(
    api: C64Backend,
    handler_bytes: bytes = BANK_SWAP_IRQ_HANDLER,
    tracker_len: int = FRAME_TRACKER_LEN,
    *,
    audio_pump_active: bool = False,
) -> None:
    """Bring up the bank-swap raster IRQ.

    `handler_bytes` and `tracker_len` default to the hires-flavour 61-byte
    handler + 16-byte tracker. MultiHires passes its own (83-byte handler,
    24-byte tracker). Both flavours live at the same addresses
    (BANK_SWAP_IRQ_HANDLER_ADDR, FRAME_TRACKER_ADDR) because the two
    display modes are mutually exclusive.

    `audio_pump_active`: True when the scene also opted into REU audio
    (`use_reu_pump = true`). In that case `handler_bytes` is expected to
    be a merged dispatcher (BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER or the
    mhires equivalent) whose non-raster branch JMPs to $C100 where the
    audio pump handler lives. We pre-upload a 3-byte JMP $EA31 stub at
    $C100 BEFORE hooking $0314 so the gap between this install completing
    (CIA #1 IRQ re-enabled at the end) and audio.start_for_reu_staged
    populating the real handler bytes is covered by a safe fall-through
    instead of a JMP into uninitialized RAM.

    Order matters: with both raster and CIA #1 sources masked, hook $0314,
    program the raster compare line, ack any pending raster IRQ, then
    enable raster + re-enable CIA #1. If we left CIA #1 enabled while
    swinging $0314, a stray jiffy IRQ could vector through our
    half-installed handler. Same sequence as
    [overlays/big_text.py:_install_raster_irq]."""
    if audio_pump_active:
        # Critical ordering: stub MUST be in place by the time CIA #1 is
        # re-enabled at step 6 below. Easiest correct ordering is to
        # upload it before any other write — that way ANY IRQ source firing
        # during the install sees a safe $C100, even if some future edit
        # changes the install order.
        api.write_memory_file(f"{AUDIO_HANDLER_INSTALL_ADDR:04X}", AUDIO_HANDLER_STUB)
    api.write_memory_file(f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}", handler_bytes)
    # Zero the frame tracker — ready flag (last byte) = 0 means the first
    # IRQ after install skips the DMA path until the host stages a real
    # frame.
    api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", bytes(tracker_len))
    # 1) Mask CIA #1 (jiffy IRQ would otherwise vector through $0314 mid-install).
    api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_DISABLE_TIMER_A:02X}")
    # 2) Disable VIC IRQ sources (raster + sprite collisions + light pen).
    api.write_memory("D01A", "00")
    # 3) Hook $0314/$0315 → our handler. write_regs packs both bytes into
    #    one DMA so the vector is never half-updated on the wire.
    api.write_regs(
        f"{VECTORS.IRQ:04X}",
        BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF,
        (BANK_SWAP_IRQ_HANDLER_ADDR >> 8) & 0xFF,
    )
    # 4) Program the raster compare register. RASTER_VBLANK_LINE = 248
    #    sits at the top of VBLANK on both PAL and NTSC — VIC isn't
    #    rendering visible pixels, so the bank swap + per-frame REU DMAs
    #    happen entirely outside the rendered area. $D011 bit 7 is the
    #    raster MSB; we leave it 0 (lines 0-255 only).
    api.write_memory("D012", f"{RASTER_VBLANK_LINE:02X}")
    # 5) Ack any latent raster flag, then enable raster IRQ source.
    api.write_memory("D019", "01")
    api.write_memory("D01A", "01")
    # 6) Re-enable CIA #1 jiffy IRQ — kernal keyboard scan etc.
    api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_ENABLE_TIMER_A:02X}")


def _uninstall_bank_swap_irq(api: C64Backend) -> None:
    """Tear down the bank-swap raster IRQ. Mirror of _install_bank_swap_irq
    in reverse, plus restore $DD00 = bank 0 so the next scene's setup
    sees the kernal-default VIC bank. Best-effort: any failure logs and
    swallows so teardown doesn't abort a multi-scene transition."""
    try:
        # 1) Mask CIA #1 + disable VIC IRQ first so no IRQ source can fire
        #    into the about-to-be-unhooked handler.
        api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_DISABLE_TIMER_A:02X}")
        api.write_memory("D01A", "00")
        # 2) Restore $0314/$0315 → kernal $EA31.
        api.write_regs(
            f"{VECTORS.IRQ:04X}", KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF
        )
        # 3) Ack any pending raster IRQ flag so the next $D019 read is clean.
        api.write_memory("D019", "01")
        # 4) Restore VIC bank to 0 (kernal default) so the next scene
        #    paints into the addresses it expects.
        api.write_memory(f"{CIA2.PORT_A:04X}", f"{_DD00_BANK_0:02X}")
        # 5) Re-enable CIA #1 jiffy IRQ — keyboard scan must keep running
        #    for the C= / CTRL / SHIFT poller.
        api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_ENABLE_TIMER_A:02X}")
    except Exception as e:
        log.debug("bank-swap IRQ teardown: %s", e)


def _push_bitmap_via_reu(
    api: C64Backend, bitmap_bytes: bytes, screen_bytes: bytes, target_bank: int
) -> None:
    """REUWRITE bitmap + screen into REU staging, then DMAWRITE a 16-byte
    frame tracker to $C700-$C70F. The C64-side raster IRQ at vblank
    reads the tracker, triggers the two REU→main DMAs into the
    off-screen bank, and flips $DD00 — all without any further host
    involvement.

    target_bank: 0 = bank 0 (dest $2000 + $0400, $DD00 = $97),
                 1 = bank 2 (dest $A000 + $8400, $DD00 = $95).

    Per-frame host work: 2 REUWRITEs (bus-clean) + 1 DMAWRITE (16 bytes,
    halts C64 bus for ~16 cycles — negligible vs the ~9000 cycles the
    REU→main DMAs themselves consume). The big halts happen at vblank
    on a deterministic 60-Hz schedule (kernal IRQ tick) rather than at
    Python-jittered wall-clock instants — see u64_reu_socket_dma.md
    Phase 2 v2 for the perceptual argument."""
    if target_bank == 0:
        bitmap_dest = VIC_BANK_0.BITMAP
        screen_dest = VIC_BANK_0.SCREEN
        pending_value = _DD00_BANK_0
    else:
        bitmap_dest = VIC_BANK_2.BITMAP
        screen_dest = VIC_BANK_2.SCREEN
        pending_value = _DD00_BANK_2
    # 1. Stage bitmap + screen into REU SRAM (bus-clean — no C64 halt).
    api.reu_write(REU_VIDEO_BITMAP_BASE, bitmap_bytes)
    api.reu_write(REU_VIDEO_BITMAP_SCREEN_BASE, screen_bytes)
    # 2. Pack the 16-byte frame tracker. Order matches the IRQ handler's
    #    layout exactly; ready flag = 1 is the LAST byte, so even if the
    #    IRQ fired mid-write (it can't — the DMAWRITE arrives atomically
    #    on the C64 side after the FIFO drain) the regs would always be
    #    consistent before ready flips.
    tracker = bytes(
        [
            bitmap_dest & 0xFF,
            (bitmap_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_BASE & 0xFF,
            (REU_VIDEO_BITMAP_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_LEN & 0xFF,
            (REU_VIDEO_BITMAP_LEN >> 8) & 0xFF,
            screen_dest & 0xFF,
            (screen_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_SCREEN_BASE & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_SCREEN_LEN & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_LEN >> 8) & 0xFF,
            pending_value,
            0x01,  # ready flag
        ]
    )
    api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)


def _push_mhires_via_reu(
    api: C64Backend,
    bitmap_bytes: bytes,
    screen_bytes: bytes,
    color_bytes: bytes,
    bg0: int,
    target_bank: int,
) -> None:
    """MultiHires bank-swap push. Extends _push_bitmap_via_reu with a third
    REUWRITE for the 1000-byte color RAM, plus a bg0 byte in the tracker
    that the IRQ writes to $D021.

    target_bank: 0 = bank 0 (dest $2000 + $0400, $DD00 = $97),
                 1 = bank 2 (dest $A000 + $8400, $DD00 = $95).

    Per-frame host work: 3 REUWRITEs (bus-clean) + 1 DMAWRITE (24 bytes,
    halts C64 bus ~24 cycles — negligible). The big halts (bitmap ~8000,
    screen ~1000, color ~1000 = ~10000 cycles total) happen on the C64
    side, triggered by the kernal IRQ at vblank. The color DMA's
    write-to-shared-$D800 means a brief c3-mismatch window across the
    bank-swap line — see MHIRES_BANK_SWAP_IRQ_HANDLER for the timing
    analysis."""
    if target_bank == 0:
        bitmap_dest = VIC_BANK_0.BITMAP
        screen_dest = VIC_BANK_0.SCREEN
        pending_value = _DD00_BANK_0
    else:
        bitmap_dest = VIC_BANK_2.BITMAP
        screen_dest = VIC_BANK_2.SCREEN
        pending_value = _DD00_BANK_2
    color_dest = SCREEN.COLOR_RAM  # $D800 — not banked, single shared SRAM
    # 1. Stage bitmap + screen + color into REU SRAM (all bus-clean — no
    #    C64 halts; ARM-side memcpy into FPGA SRAM).
    api.reu_write(REU_VIDEO_BITMAP_BASE, bitmap_bytes)
    api.reu_write(REU_VIDEO_BITMAP_SCREEN_BASE, screen_bytes)
    api.reu_write(REU_VIDEO_BITMAP_COLOR_BASE, color_bytes)
    # 2. Pack the 24-byte frame tracker. Order matches the IRQ handler's
    #    layout exactly; ready flag = 1 is the LAST byte, so the IRQ
    #    handler can rely on the regs being consistent whenever it sees
    #    ready=1.
    tracker = bytes(
        [
            # bitmap regs: $DF02..$DF08 packed [c64_lo, c64_hi, reu_lo, reu_mi,
            # reu_hi, len_lo, len_hi]
            bitmap_dest & 0xFF,
            (bitmap_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_BASE & 0xFF,
            (REU_VIDEO_BITMAP_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_LEN & 0xFF,
            (REU_VIDEO_BITMAP_LEN >> 8) & 0xFF,
            # screen regs
            screen_dest & 0xFF,
            (screen_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_SCREEN_BASE & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_SCREEN_LEN & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_LEN >> 8) & 0xFF,
            # color regs
            color_dest & 0xFF,
            (color_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_COLOR_BASE & 0xFF,
            (REU_VIDEO_BITMAP_COLOR_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_COLOR_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_COLOR_LEN & 0xFF,
            (REU_VIDEO_BITMAP_COLOR_LEN >> 8) & 0xFF,
            # bg0, bank value, ready flag
            bg0 & 0xFF,
            pending_value,
            0x01,
        ]
    )
    api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)


def _validate_palette_mode(mode: str) -> None:
    if mode not in PALETTE_MODES:
        raise ValueError(f"palette_mode must be one of {PALETTE_MODES}, got {mode!r}")


def _push_screen_via_reu(api: C64Backend, screen_bytes: bytes, dest_addr: int) -> None:
    """REUWRITE the screen bytes to REU, then trigger a REU→main DMA into
    `dest_addr` (the screen RAM location for the current VIC bank — $0400
    for bank 0, $8400 for bank 2). Used by the REU-staged char-mode push.
    Each frame is a one-shot transfer (no auto-increment across triggers),
    so the REU source offset stays pinned at REU_VIDEO_SCREEN_BASE — the
    REUWRITE in step 1 overwrites the staging area each frame."""
    # 1. Stage the new screen into REU SRAM (clean — no C64 bus halt).
    api.reu_write(REU_VIDEO_SCREEN_BASE, screen_bytes)
    # 2. Configure REU source (REU_VIDEO_SCREEN_BASE, 24-bit), dest
    # (dest_addr, 16-bit), length (1000 bytes), addr-control (auto-inc
    # both — default 0). write_regs packs contiguous register writes into
    # one DMA command, so REU regs go in 3 commands instead of 7.
    api.write_regs(f"{REU.C64_ADDR_LO:04X}", dest_addr & 0xFF, (dest_addr >> 8) & 0xFF)
    api.write_regs(
        f"{REU.REU_ADDR_LO:04X}",
        REU_VIDEO_SCREEN_BASE & 0xFF,
        (REU_VIDEO_SCREEN_BASE >> 8) & 0xFF,
        (REU_VIDEO_SCREEN_BASE >> 16) & 0xFF,
    )
    api.write_regs(
        f"{REU.LENGTH_LO:04X}", REU_VIDEO_SCREEN_LEN & 0xFF, (REU_VIDEO_SCREEN_LEN >> 8) & 0xFF
    )
    # 3. Trigger. The CPU halts for ~1000 cycles (1 byte/cycle) while the
    # REU→main DMA copies the staged frame into screen RAM. This is the
    # only bus-halt event in the REU-staged char push (REUWRITE in step 1
    # is bus-clean; color RAM uses the regular delta cache).
    api.write_memory(f"{REU.COMMAND:04X}", f"{REU.CMD_FETCH_EXEC:02X}")


def _resolve_color_shaping(
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


def _advance_palette_cycle(
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


def _palette_mode_settings(mode: str) -> tuple[float, np.ndarray]:
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


def _fade_nibbles(arr: np.ndarray, lut: np.ndarray) -> np.ndarray:
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
    # via a palette remap (see palette.build_fade_lut). `_last_buffers` caches
    # the most recent full-brightness composed frame so the freeze+dim fade-out
    # can re-push it at decreasing alpha without re-composing. Only the
    # compose-based families (Char/Bitmap) implement apply_fade; the base is a
    # no-op so non-compose modes are unaffected.
    fade_alpha: float = 1.0
    _last_buffers: ComposeBuffers | None = None

    def apply_fade(self, buffers: ComposeBuffers) -> ComposeBuffers:
        """Return `buffers` with color-bearing fields dimmed toward black at
        ``self.fade_alpha``. Never mutates the input (so the cached pristine
        buffers survive a multi-frame fade-out). Base: identity."""
        return buffers

    def repush_faded(self, api: C64Backend, alpha: float) -> None:
        """Re-push the last composed frame dimmed to ``alpha`` — the freeze+dim
        fade-out. No-op when nothing has been composed yet (e.g. a scene torn
        down before its first frame)."""
        if self._last_buffers is None:
            return
        saved = self.fade_alpha
        self.fade_alpha = alpha
        try:
            self.push(api, self.apply_fade(self._last_buffers))
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


class CharDisplayMode(DisplayMode):
    """Mid-base for text-mode renderers (PETSCII, MCM).

    Writes go to screen RAM ($0400) and color RAM ($D800). MCM reinterprets
    color RAM bit 3 as "multicolor mode for this cell", so PETSCII-glyph
    overlays only render correctly in the standard PETSCII subclass — the
    validator gates them via REQUIRES_PETSCII against the display mode's
    `name`, not the broader is_bitmapped flag. Default frame budget defers
    to the playlist default — char modes are cheap enough to hit 50/60.

    Char modes implement compose()/push() so overlays can paint into the
    same 1000-byte screen + color buffers the scene built — one combined
    upload per frame, no flicker from scene/overlay write interleaving."""

    is_bitmapped = False
    default_target_fps = None  # follow the playlist's NTSC/PAL default
    supports_compose = True

    def apply_fade(self, buffers: ComposeBuffers) -> ComposeBuffers:
        """Char modes carry per-cell foreground color in the `color` buffer
        (color RAM low nibble); screen RAM holds glyph codes, not colors. Dim
        the foreground; black cells (color 0) stay black. MCM overrides to also
        dim its shared bg registers and constrain the multicolor foreground."""
        out: ComposeBuffers = dict(buffers)  # type: ignore[assignment]
        lut = build_fade_lut(self.fade_alpha)
        out["color"] = lut[buffers["color"]]
        return out


def engage_bitmap_mode(
    api: C64Backend,
    *,
    d011: str,
    d018: str,
    d016: str,
    bitmap_base: int = VIC_BANK_0.BITMAP,
    screen_base: int = SCREEN.RAM,
    dd00: int | None = None,
    border: int | None = None,
    bg0: int | None = None,
    clear: bool = True,
    clear_region_ids: tuple[int, int] | None = None,
) -> None:
    """Canonical hires/mhires VIC bitmap-mode bring-up — the single place the
    "clear-then-flip" engage invariant lives. Used by both ``BitmapDisplayMode``
    (Hires/MultiHires single-buffer ``setup``) and ``VoiceScopeRenderer``
    (waveform/midi scope ``_apply_vic_hires_bank``) so the ordering and the VIC
    register set can't drift between them.

    **The invariant:** zero the bitmap (``$2000``) AND screen RAM (``$0400``)
    BEFORE writing ``$D011`` bitmap-on, so the window between the mode flip and
    the first composed frame shows a clean black field — not uninitialized-RAM
    garbage and not a colour ghost of the prior scene. A zeroed hires bitmap
    makes every pixel select its cell's BACKGROUND colour, and in HIRES that
    background is the LOW nibble of the cell's screen-RAM byte (NOT ``$D021``) —
    so leaving stale ``$0400`` (e.g. the previous scene's PETSCII codes / colour
    grid) paints a 40×25 colour ghost on engage. Zeroing ``$0400`` too forces
    every cell's background to black. (In mhires/MCBM ``%00`` reads ``$D021``,
    set here via ``bg0``, so the screen clear is belt-and-braces there.) ``$D011``
    is written LAST so the configured sub-bank pointers + colours are already in
    place when bitmap mode reveals them.

    Parameters thread the legitimate per-caller differences (so this stays one
    primitive, not a fork):

    * ``d011`` / ``d018`` / ``d016`` — the VIC register values (hex strings).
      Hires uses ``d016="08"`` (no multicolor); mhires ``d016="18"``.
    * ``bitmap_base`` / ``screen_base`` — the bitmap + screen-matrix addresses.
      Default to VIC bank 0 ($2000/$0400); the scope relocates these to bank 2.
    * ``dd00`` — CIA2 ``$DD00`` VIC-bank select, written FIRST so the clear lands
      in the bank VIC will fetch from. ``None`` leaves the bank as-is (kernal
      default bank 0 — the display modes never relocate).
    * ``border`` / ``bg0`` — ``$D020`` / ``$D021``; written as separate pokes
      (callers that read back ``$D021`` independently rely on the standalone
      register). ``None`` leaves that register untouched (hires sets neither in
      setup — it manages the border per-frame; the REU mhires path leaves
      ``$D021`` to its swap-tracker IRQ).
    * ``clear`` — do the ``$2000`` + ``$0400`` zeroing. ``True`` for every
      single-buffer path (the engage clean-field). The REU / host-DMA
      double-buffer paths pass ``False`` because they zero both VIC *banks*
      themselves; they still want the register pokes from here.
    * ``clear_region_ids`` — ``(bitmap_region_id, screen_region_id)`` ⇒ clear via
      the delta-cached ``write_region`` path (the scope, which relocates the VIC
      bank and reuses the IDs as its spacer-row baseline). ``None`` ⇒ clear via
      ``write_memory_file`` (the display modes' one-time bulk clear, which
      bypasses the delta cache the first ``push`` rebuilds)."""
    # 1. VIC bank select — before the clear so it lands in the fetched bank.
    if dd00 is not None:
        api.write_memory(f"{CIA2.PORT_A:04X}", f"{dd00:02X}")
    # 2. Clear bitmap + screen matrix while $2000 is still OFF-screen (text
    #    mode), so the $D011 flip in step 4 reveals a clean black field.
    if clear:
        if clear_region_ids is None:
            api.write_memory_file(f"{bitmap_base:04X}", bytes(SCREEN.BITMAP_BYTES))
            api.write_memory_file(f"{screen_base:04X}", bytes(SCREEN.N_CELLS))
        else:
            bitmap_region_id, screen_region_id = clear_region_ids
            api.write_region(bitmap_base, bytes(SCREEN.BITMAP_BYTES), region_id=bitmap_region_id)
            api.write_region(screen_base, bytes(SCREEN.N_CELLS), region_id=screen_region_id)
    # 3. Configure the sub-bank pointers ($D018/$D016) + background colours.
    api.write_memory("d018", d018)
    api.write_memory("d016", d016)
    if border is not None:
        api.write_regs("d020", border)
    if bg0 is not None:
        api.write_regs("d021", bg0)
    # 4. Flip $D011 into bitmap mode LAST — now the clean field is revealed.
    api.write_memory("d011", d011)


class BitmapDisplayMode(DisplayMode):
    """Mid-base for bitmap renderers (Hires, MultiHires).

    Inherits default_target_fps = None so bitmap scenes follow the playlist's
    system rate (60 fps NTSC / 50 fps PAL). The old cap of 30 fps was
    conservative sizing for the HTTP transport; socket DMA handles full-frame
    bitmap uploads at 60 fps comfortably within the ~200 writes/sec ceiling.

    Bitmap modes implement compose()/push() (supports_compose = True) so text
    overlays can fold glyphs into the bitmap before push — including down the
    REU bank-swap path, which a post-hoc direct writer can't reach. compose()
    returns BitmapComposeBuffers ({bitmap, screen, bg, text}); MultiHires adds
    color. The text surface (text_surface.py) folds glyphs into the in-memory
    bitmap/screen(/color) arrays, so push() uploads one combined frame."""

    is_bitmapped = True
    supports_compose = True
    # Bitmap modes can host the text overlays (clock/marquee/…) that paint
    # PETSCII screen codes — see text_surface.HiresTextSurface / MHiresTextSurface.
    is_bitmap_text_compatible = True
    # Which VIC bank is currently displayed under double-buffering (REU staging
    # or host-DMA): 0 ⇒ bank 0 on screen / paint bank 2 next, 1 ⇒ bank 2 on
    # screen / paint bank 0 next. Subclasses reset it in __init__/setup.
    _displayed_bank: int = 0

    # The clear-then-flip engage bring-up lives in the module-level
    # `engage_bitmap_mode` (above) so it's shared with VoiceScopeRenderer.

    # --- Host-DMA double-buffer (no-REU backends, e.g. TeensyROM) -----------
    # Shared by Hires + MultiHires. The host writes bitmap+screen into the
    # OFF-screen VIC bank over the normal host-DMA write_region path, then arms
    # HOSTDMA_SWAP_IRQ_HANDLER (installed in setup) to flip $DD00 at vblank — so
    # the visible bank is never written mid-display (tear-free) without needing
    # an REU. See the handler block near the top of this module. Subclasses own
    # self._displayed_bank (0 ⇒ off-screen is bank 2, 1 ⇒ off-screen is bank 0).
    def _hostdma_swap_target(self) -> tuple[int, int, int, int, int, int]:
        """Resolve the current off-screen bank to
        (target_bank, bitmap_addr, screen_addr, bitmap_region, screen_region,
        dd00_value). The caller toggles self._displayed_bank after the writes."""
        if 1 - self._displayed_bank == 0:
            return (
                0,
                VIC_BANK_0.BITMAP,
                VIC_BANK_0.SCREEN,
                RegionID.BITMAP,
                RegionID.SCREEN,
                _DD00_BANK_0,
            )
        return (
            1,
            VIC_BANK_2.BITMAP,
            VIC_BANK_2.SCREEN,
            RegionID.BITMAP_BANK2,
            RegionID.SCREEN_BANK2,
            _DD00_BANK_2,
        )

    def _arm_hostdma_swap(self, api: C64Backend, bg0: int, dd00_value: int) -> None:
        """Write the 3-byte swap tracker [bg0, bank, ready=1] as one ACK-gated
        segment. By the time it returns the off-screen bank is fully staged, so
        the next vblank IRQ flips $DD00 to a complete frame (and sets $D021 from
        bg0 atomically with the swap — for hires $D021 is unused, harmless)."""
        tracker = bytes([bg0 & 0x0F, dd00_value & 0xFF, 0x01])
        api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)

    def _setup_hostdma_doublebuffer(self, api: C64Backend) -> None:
        """Zero both VIC banks' bitmap+screen, pin bank 0, and install the
        minimal vblank swap IRQ. Mirrors the REU setup minus the REU staging —
        the caller has already set $D011/$D018/$D016 and the initial bg0/border.
        audio_pump_active is always False: NMI audio is on the $FFFA vector,
        independent of this $0314 raster IRQ."""
        zeros_bitmap = bytes(REU_VIDEO_BITMAP_LEN)
        zeros_screen = bytes(REU_VIDEO_BITMAP_SCREEN_LEN)
        api.write_memory_file(f"{VIC_BANK_0.BITMAP:04X}", zeros_bitmap)
        api.write_memory_file(f"{VIC_BANK_0.SCREEN:04X}", zeros_screen)
        api.write_memory_file(f"{VIC_BANK_2.BITMAP:04X}", zeros_bitmap)
        api.write_memory_file(f"{VIC_BANK_2.SCREEN:04X}", zeros_screen)
        api.write_memory(f"{CIA2.PORT_A:04X}", f"{_DD00_BANK_0:02X}")
        self._displayed_bank = 0
        _install_bank_swap_irq(
            api, HOSTDMA_SWAP_IRQ_HANDLER, HOSTDMA_TRACKER_LEN, audio_pump_active=False
        )

    def apply_fade(self, buffers: BitmapComposeBuffers) -> BitmapComposeBuffers:
        """Hires per-cell colors are packed into the screen byte (hi nibble =
        fg, lo nibble = bg) plus the global bg/border scalar; the bitmap is a
        per-pixel fg/bg selector, so it's left untouched. Dim both nibbles and
        the bg. MultiHires overrides to also dim its per-cell color RAM (c3)."""
        out: BitmapComposeBuffers = dict(buffers)  # type: ignore[assignment]
        lut = build_fade_lut(self.fade_alpha)
        out["screen"] = _fade_nibbles(buffers["screen"], lut)
        out["bg"] = int(lut[buffers["bg"]])
        return out


class PETSCIIDisplayMode(CharDisplayMode):
    """40×25 character mode. Luma → glyph, hue → color RAM.

    The glyph + color policies live in petscii_styles.PetsciiStyle
    subclasses; `style` picks one at construction. SHIFT cycles to the
    next style in STYLE_NAMES (no-op on cycle out of an unknown name).

    Special sentinel `style = "random"` picks a concrete style at the
    first setup() and then cycles from there (so subsequent SHIFT presses
    have predictable next-style behavior, not another random pick).
    """

    name = "petscii"
    is_petscii_compatible = True
    frame_target_size = (40, 25)

    def __init__(
        self,
        style: str = "default",
        *,
        use_reu_staged: bool = False,
        hue_corrections: list[dict] | None = None,
        hue_corrections_replace: bool = False,
        channel_boost: list[float] | None = None,
        perceptual: bool = False,
    ):
        validate_style(style)
        # Perceptual (CIE-Lab) nearest-palette matching ([color].color_match).
        # Threaded into each style's per-cell color pick; styles decide their
        # own glyph/luma independently of the color metric.
        self._perceptual = bool(perceptual)
        self._configured_style = style  # may be "random" sentinel
        # Resolve "random" lazily at setup() so each scene instance
        # (including single-scene loops via teardown+setup) picks fresh.
        self._style_name = style if style != RANDOM_STYLE else pick_random_style_name()
        self._style = make_style(self._style_name)
        # Global [color] shaping passed through to whichever style is active —
        # styles run their own per-cell quantization but share this pre-quant
        # stage (channel boost + hue corrections) with the bitmap modes.
        self._channel_boost, self._hue_corrections = _resolve_color_shaping(
            channel_boost, hue_corrections, hue_corrections_replace
        )
        # Opt-in REU-staged screen RAM push. See _push_screen_via_reu and
        # the REU_VIDEO_SCREEN_BASE block above for details + caveats.
        # Color RAM stays on the DMAWRITE delta path regardless.
        self.use_reu_staged = use_reu_staged

    def setup(self, api):
        super().setup(api)
        api.write_memory("d018", "14")
        api.write_memory("d016", "08")
        api.write_memory("d011", "1b")
        # Each style declares its own border + background; push them now
        # so we don't carry the previous scene's choices into the first
        # frame. Bordr + bg are contiguous at $D020-$D021.
        api.write_regs("d020", self._style.border, self._style.background)

    def set_style(self, api, name: str) -> str:
        """Switch to PETSCII style `name` in place. Shared by the SHIFT cycle
        and the on-C64 menu: repaints border/bg and invalidates the delta cache
        so the next frame fully redraws with the new style. `name` must be a
        concrete STYLE_NAMES entry (not the 'random' sentinel)."""
        self._style_name = name
        self._style = make_style(name)
        api.write_regs("d020", self._style.border, self._style.background)
        api.invalidate_cache()
        return f"style={name}"

    def cycle_style(self, api):
        idx = STYLE_NAMES.index(self._style_name)
        new_name = STYLE_NAMES[(idx + 1) % len(STYLE_NAMES)]
        return self.set_style(api, new_name)

    @property
    def style(self) -> str:
        """Currently-active concrete style name (never the 'random' sentinel)."""
        return self._style_name

    def compose(self, frame) -> ComposeBuffers:
        assert self.frame_target_size is not None
        img = cv2.resize(frame, self.frame_target_size, interpolation=cv2.INTER_AREA)
        if self._color_fit is not None:
            img = apply_color_fit(img, self._color_fit)
        screen, color = self._style.compose(
            img, self._channel_boost, self._hue_corrections, self._perceptual
        )
        return {"screen": screen, "color": color, "text": CharTextSurface(screen, color)}

    def push(self, api: C64Backend, buffers: ComposeBuffers) -> None:
        screen_bytes = buffers["screen"].tobytes()
        if self.use_reu_staged:
            _push_screen_via_reu(api, screen_bytes, SCREEN.RAM)
        else:
            api.write_region(SCREEN.RAM, screen_bytes, region_id=RegionID.SCREEN)
        api.write_region(SCREEN.COLOR_RAM, buffers["color"].tobytes(), region_id=RegionID.COLOR)


class BlankDisplayMode(CharDisplayMode):
    """Standard PETSCII char mode with no video input.

    Paints the whole screen as SC_SPACE, leaving overlays to provide all
    the visible content. Useful as a clean canvas for big-text title cards
    where a webcam feed would just compete with the text. Configurable
    border + background palette indices.
    """

    name = "blank"
    is_petscii_compatible = True

    def __init__(self, border: int = 0, background: int = 0, *, use_reu_staged: bool = False):
        self.border = int(border) & 0x0F
        self.background = int(background) & 0x0F
        # Opt-in REU-staged screen RAM push. Blank scenes are typically
        # static (overlays paint over a near-constant background), so the
        # delta cache makes the default path almost zero-traffic — REU
        # staging is mostly useful here for testing the pipeline or when
        # a busy overlay (big_text, scrolling spectrum) forces frequent
        # full-screen rewrites.
        self.use_reu_staged = use_reu_staged

    def setup(self, api):
        super().setup(api)
        api.write_memory("d018", "14")
        api.write_memory("d016", "08")
        api.write_memory("d011", "1b")
        api.write_regs("d020", self.border, self.background)

    def compose(self, frame=None) -> ComposeBuffers:
        # frame ignored — blank mode has no video input. Pass through so
        # the scene's `_render_with_overlays(None, t)` path still works.
        screen = np.full(1000, 0x20, dtype=np.uint8)  # SC_SPACE
        # Color RAM is the FG color of every cell. Default to background
        # so SC_SPACE renders invisibly until an overlay paints over it.
        color = np.full(1000, self.background, dtype=np.uint8)
        return {"screen": screen, "color": color, "text": CharTextSurface(screen, color)}

    def push(self, api: C64Backend, buffers: ComposeBuffers) -> None:
        screen_bytes = buffers["screen"].tobytes()
        if self.use_reu_staged:
            _push_screen_via_reu(api, screen_bytes, SCREEN.RAM)
        else:
            api.write_region(SCREEN.RAM, screen_bytes, region_id=RegionID.SCREEN)
        api.write_region(SCREEN.COLOR_RAM, buffers["color"].tobytes(), region_id=RegionID.COLOR)


class MCMDisplayMode(CharDisplayMode):
    """80×50 multicolor character mode using an uploaded 2×2-pixel charset.

    palette_mode (slot-allocation strategy only; color shaping is the global
    [color] stage applied to every mode):
      "cheap" — HSV saturation boost + gray-penalty bias on the per-pixel
        argmin. Fixes the typical "everything turns gray or pale cyan" failure
        mode of unbiased nearest-palette quantization without changing how the
        three global background colors are chosen.
      "vivid" — same biases, plus the 3 global backgrounds are picked by
        hue-diversity rather than raw frequency. The frame's single most
        populated palette entry always wins slot 0 (so a webcam pointed at a
        red sweater still gets red); the remaining slots prefer the most
        populated *with a hue gap* from the already-chosen chromatic picks.
      "grayscale" — fixed bg slots (dark gray / gray / light gray) in
        luminance order; FG resolves to {black, white}. Yields full 5-level
        gray coverage per screen while keeping the bg assignment stable
        across frames so the delta cache hits on every screen RAM write.
      "percell" (default) — MCM already picks the fg color per cell (1 of 8) so
        the per-cell c1/c2/c3 trick mhires uses doesn't apply here; MCM treats
        percell as "cheap". Accepted so the playlist-default palette_mode value
        works on every display mode.
    """

    name = "mcm"
    frame_target_size = (80, 50)

    def __init__(
        self,
        palette_mode: str = "percell",
        hue_corrections: list[dict] | None = None,
        hue_corrections_replace: bool = False,
        channel_boost: list[float] | None = None,
        force_palette: bool = False,
        dither_method: str = "none",
        dither_strength: float = 0.5,
        perceptual: bool = False,
    ):
        _validate_palette_mode(palette_mode)
        # The forced-palette preset pairs with percell (see cycle_style); when
        # config opts in, start in that state regardless of the configured
        # palette_mode (which still seeds the non-forced cycle stops).
        self._force_palette = bool(force_palette)
        if self._force_palette:
            palette_mode = "percell"
        self.palette_mode = palette_mode
        self._sat_factor, self._gray_penalty = _palette_mode_settings(palette_mode)
        self._channel_boost, self._hue_corrections = _resolve_color_shaping(
            channel_boost, hue_corrections, hue_corrections_replace
        )
        # Perceptual (CIE-Lab) nearest-palette matching ([color].color_match).
        # When on, compose() measures nearest-color in Lab (perceptually uniform)
        # instead of the brightness-weighted BGR metric. The channel_boost + gray
        # penalty shaping still applies (they keep flat desaturated regions from
        # fragmenting to gray and hold C64-friendly hues); only the distance
        # space changes. The penalty is in d² units, so it's scaled to the Lab
        # metric's smaller magnitude. See palette.quantize_distances_for.
        self._perceptual = bool(perceptual)
        self._penalty_scale = PERCEPTUAL_DIST_SCALE if self._perceptual else 1.0
        self._dither_method = dither_method
        self._dither_strength = dither_strength
        self._last_bg: np.ndarray | None = None
        # grayscale uses a fixed bg slot assignment so the per-cell screen
        # nibbles don't shuffle frame-to-frame — see GRAYSCALE_* comment up top.
        self._fixed_bg: np.ndarray | None = (
            np.array(GRAYSCALE_MCM_BGS, dtype=np.int64) if palette_mode == "grayscale" else None
        )
        # EMA-smoothed counts for cheap/vivid picks; see PALETTE_PICK_EMA_ALPHA.
        self._smoothed_counts: np.ndarray | None = None

    def set_palette_mode(self, api, palette_mode: str, *, force_palette: bool | None = None) -> str:
        """Apply `palette_mode` (and optionally the forced-palette flag) to the
        running instance — shared by the SHIFT cycle and the on-C64 menu. Resets
        the EMA + last-bg state and invalidates the delta cache so the next frame
        re-picks slots and fully repaints. Returns the same label the SHIFT
        cycle logs."""
        _validate_palette_mode(palette_mode)
        self.palette_mode = palette_mode
        if force_palette is not None:
            self._force_palette = force_palette
        self._sat_factor, self._gray_penalty = _palette_mode_settings(palette_mode)
        self._fixed_bg = (
            np.array(GRAYSCALE_MCM_BGS, dtype=np.int64) if palette_mode == "grayscale" else None
        )
        # Reset EMA + last-bg so the new mode's slot picks don't blend with
        # the previous mode's accumulated counts and so border/bg get
        # re-pushed on the next frame.
        self._smoothed_counts = None
        self._last_bg = None
        api.invalidate_cache()
        return f"palette_mode={palette_mode}" + ("+forced" if self._force_palette else "")

    def cycle_style(self, api):
        new_mode, new_force, _label = _advance_palette_cycle(
            self.palette_mode, self._force_palette, self._color_map is not None
        )
        return self.set_palette_mode(api, new_mode, force_palette=new_force)

    def setup(self, api):
        super().setup(api)
        # Re-upload the charset on every setup(), not just the first. The
        # charset lives at $3000, which falls inside the $2000-$3F3F bitmap
        # area that hires/mhires scenes write to. In a looping multi-scene
        # playlist this MCMDisplayMode instance is reused across loops, so an
        # intervening bitmap scene clobbers $3000 between two appearances of
        # this scene — a one-time upload would then leave stale bitmap bytes
        # as the character set (visible as a corrupted charset). It's a single
        # 2 KB write at scene-entry time, so re-uploading is cheap.
        charset = bytearray(2048)
        for i in range(256):
            tl, tr, bl, br = (i >> 6) & 3, (i >> 4) & 3, (i >> 2) & 3, i & 3
            row_top = (tl << 6) | (tl << 4) | (tr << 2) | tr
            row_bot = (bl << 6) | (bl << 4) | (br << 2) | br
            charset[i * 8 : i * 8 + 4] = [row_top] * 4
            charset[i * 8 + 4 : i * 8 + 8] = [row_bot] * 4
        api.write_memory_file("3000", bytes(charset))
        api.write_memory("d018", "1c")
        api.write_memory("d016", "18")
        api.write_memory("d011", "1b")
        self._last_bg = None  # force re-push of bg on first frame after setup

    def compose(self, frame) -> MCMComposeBuffers:
        assert self.frame_target_size is not None
        img = cv2.resize(frame, self.frame_target_size, interpolation=cv2.INTER_AREA)
        if self._force_palette and self._color_map is not None:
            # Forced-palette remap: emit exact C64 colors and skip the faithful
            # shaping stages + gray penalty (the remap already chose each color).
            flat = self._color_map.apply(img).reshape(-1, 3).astype(np.float32)
            all_d = quantize_distances(flat)  # (4000, 16)
        else:
            if self._color_fit is not None:
                img = apply_color_fit(img, self._color_fit)
            img = boost_saturation(img, self._sat_factor)
            # Global [color] shaping: hue-band corrections then per-channel boost.
            img = apply_hue_corrections(img, self._hue_corrections)
            flat = np.clip(img.reshape(-1, 3).astype(np.float32) * self._channel_boost, 0, 255)
            offset_fn = _ORDERED_DITHER_OFFSET_FNS.get(self._dither_method)
            if offset_fn is not None:
                w, h = self.frame_target_size
                offset = offset_fn(h, w, self._dither_strength)
                flat = np.clip(flat + offset.reshape(-1, 1), 0, 255)
            # Single distance matrix (with gray penalty, scaled to the active
            # metric) shared across all downstream decisions — per-pixel argmin,
            # the bg picker, and the per-cell fg search all need to agree on which
            # palette entry "wins" for a given pixel, so apply the bias once at
            # the top. In-place add avoids a second ~256 KB allocation each frame.
            all_d = quantize_distances_for(flat, perceptual=self._perceptual)  # (4000, 16)
            all_d += self._gray_penalty * self._penalty_scale
        per_pixel = np.argmin(all_d, axis=1)
        if self._fixed_bg is not None:
            bg = self._fixed_bg
        else:
            smoothed = _ema_counts(self, per_pixel)
            if self.palette_mode == "vivid":
                picks = pick_diverse_top_n(smoothed, 3)
            else:
                picks = [int(x) for x in np.argsort(smoothed)[-3:]]
            bg = np.array(sorted(picks), dtype=np.int64)  # (3,)

        # Group per-pixel distances into 1000 cells of 4 pixels each.
        d_grid = (
            all_d.reshape(50, 80, 16)
            .reshape(25, 2, 40, 2, 16)
            .transpose(0, 2, 1, 3, 4)
            .reshape(1000, 4, 16)
        )

        bg_d = d_grid[:, :, bg]  # (1000, 4, 3)
        fg_d = d_grid[:, :, :8]  # (1000, 4, 8)

        # For each (cell, pixel, fg_candidate), pick the best of {bg0,bg1,bg2,fg}.
        # The best-bg choice is fg-independent — collapse it first to skip the
        # (1000, 4, 8, 4) tensor the naive concat+argmin would build.
        bg_argmin = bg_d.argmin(axis=2)  # (1000, 4)  -> 0/1/2
        bg_min = bg_d.min(axis=2)[:, :, None]  # (1000, 4, 1)
        minv = np.minimum(fg_d, bg_min)  # (1000, 4, 8)
        err_per_fg = minv.sum(axis=1)  # (1000, 8)
        best_fg = err_per_fg.argmin(axis=1)  # (1000,)

        force_palette_active = self._force_palette and self._color_map is not None
        if self._dither_method in ("floyd-steinberg", "atkinson") and not force_palette_active:
            # Re-dither each cell's own 2×2 pixels against its resolved
            # candidate set {bg0, bg1, bg2, fg} — candidate SELECTION (bg,
            # best_fg above) stays on the EMA-smoothed histogram for temporal
            # stability; only the per-pixel fill dithers. Candidate order
            # matches the fa code convention (0/1/2 = bg slot, 3 = fg), so the
            # returned code IS fa directly.
            pixels_cell = (
                flat.reshape(50, 80, 3)
                .reshape(25, 2, 40, 2, 3)
                .transpose(0, 2, 1, 3, 4)
                .reshape(1000, 2, 2, 3)
            )
            cand_bgr = np.concatenate(
                [
                    np.broadcast_to(C64_PALETTE_BGR[bg], (1000, 3, 3)),
                    C64_PALETTE_BGR[best_fg][:, None, :],
                ],
                axis=1,
            )  # (1000, 4, 3)
            fa = error_diffuse_cells(
                pixels_cell, cand_bgr, self._dither_method, self._dither_strength
            ).reshape(1000, 4)
        else:
            idx = np.arange(1000)
            fg_wins = fg_d[idx, :, best_fg] < bg_min[:, :, 0]  # (1000, 4)
            fa = np.where(fg_wins, 3, bg_argmin).astype(np.int64)  # (1000, 4)

        screen = ((fa[:, 0] << 6) | (fa[:, 1] << 4) | (fa[:, 2] << 2) | fa[:, 3]).astype(np.uint8)
        color = (best_fg + 8).astype(np.uint8)  # high bit = multicolor

        # text surface present for the buffers contract; MCM rejects PETSCII
        # text overlays (color-RAM bit 3 = multicolor), so nothing paints it.
        return {"screen": screen, "color": color, "bg": bg, "text": CharTextSurface(screen, color)}

    def push(self, api: C64Backend, buffers: MCMComposeBuffers) -> None:
        bg = buffers["bg"]
        if self._last_bg is None or not np.array_equal(bg, self._last_bg):
            # D020-D023 are contiguous: border, bg0, bg1, bg2.
            api.write_regs("d020", int(bg[0]), int(bg[0]), int(bg[1]), int(bg[2]))
            self._last_bg = bg.copy()
        api.write_region(0x0400, buffers["screen"].tobytes(), region_id=RegionID.SCREEN)
        api.write_region(0xD800, buffers["color"].tobytes(), region_id=RegionID.COLOR)

    def apply_fade(self, buffers: MCMComposeBuffers) -> MCMComposeBuffers:
        """MCM colors live in three places: the shared bg0/bg1/bg2 registers
        (`bg`, any palette index) and the per-cell multicolor foreground stored
        in color RAM as ``fg | 8`` with ``fg`` ∈ 0..7. Dim all four; the screen
        buffer is 2-bit selectors among them, so it's left untouched. The
        foreground uses a 0..7-constrained LUT so the dimmed value stays a legal
        multicolor color (and the bit-3 flag is preserved)."""
        alpha = self.fade_alpha
        lut = build_fade_lut(alpha)
        fg_lut = build_fade_lut(alpha, allowed=tuple(range(8)))
        out: MCMComposeBuffers = dict(buffers)  # type: ignore[assignment]
        color = buffers["color"]
        out["color"] = (fg_lut[color & 0x07] | 0x08).astype(np.uint8)
        out["bg"] = lut[buffers["bg"]]
        return out


HIRES_STYLES = ("normal", "edges", "edges_inverted")


def _validate_hires_style(style: str) -> None:
    if style not in HIRES_STYLES:
        raise ValueError(f"hires style must be one of {HIRES_STYLES}, got {style!r}")


class HiresDisplayMode(BitmapDisplayMode):
    """320×200 bitmap.

    style:
      "normal"          — luma-quantized: per-cell sampled fg + dominant bg.
      "edges"           — Canny edges in white on black.
      "edges_inverted"  — Canny edges in black on white (negative print).

    use_reu_staged: opt into the REU bank-swap double-buffer pipeline.
      Each frame's bitmap + screen are REUWRITE-staged into REU SRAM
      (bus-clean) then dropped into the OFF-SCREEN VIC bank via two
      REU→main DMAs while VIC keeps rendering the on-screen bank. A
      C64-side raster IRQ at vblank flips $DD00 to bring up the new
      bank tear-free. See _push_bitmap_via_reu / _install_bank_swap_irq
      and the REU_VIDEO_BITMAP_* constants up top.

      Cannot coexist with [audio].use_reu_pump (both share REC + $0314)
      — validate_scene_cfg enforces this at load time. Color RAM isn't
      used by hires (color is in screen RAM nibbles), so the shared-
      $D800 mid-frame-mismatch problem the other display modes would
      have doesn't apply.
    """

    name = "hires"
    frame_target_size = (320, 200)

    def __init__(
        self,
        style: str = "normal",
        *,
        use_reu_staged: bool = False,
        double_buffer: bool = False,
        audio_reu_pump_active: bool = False,
        dither_method: str = "none",
        dither_strength: float = 0.5,
        perceptual: bool = False,
    ):
        _validate_hires_style(style)
        self.style = style
        # Perceptual (CIE-Lab) nearest-palette matching ([color].color_match).
        # Only the "normal" style quantizes color (bg + per-cell fg samples); the
        # edges styles are fixed 2-color, so this is a no-op there.
        self._perceptual = bool(perceptual)
        self._dither_method = dither_method
        self._dither_strength = dither_strength
        self._last_bg: int | None = None
        self.use_reu_staged = use_reu_staged
        # Host-DMA double-buffer (no-REU backends, e.g. TeensyROM): tear-free
        # via off-screen-bank writes + a vblank $DD00 flip, no REU. Mutually
        # exclusive with use_reu_staged (resolve_double_buffer guarantees it).
        self.double_buffer = double_buffer
        # When the scene also opted into REU audio (`[audio].use_reu_pump`),
        # the bank-swap dispatcher at $C500 needs to fall through to the
        # audio pump handler at $C100 on non-raster (CIA #1 jiffy) IRQs.
        # Picks BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER in setup() and pre-seeds
        # $C100 with a safe JMP $EA31 stub so the install window can't
        # vector into uninitialized RAM.
        self.audio_reu_pump_active = audio_reu_pump_active
        # Double-buffer tracker: which VIC bank is currently displayed.
        # 0 = bank 0 (paint into bank 2 next), 1 = bank 2 (paint into
        # bank 0 next). Only meaningful when use_reu_staged is True;
        # reset in setup().
        self._displayed_bank = 0

    def setup(self, api):
        super().setup(api)
        # Single-buffer bring-up clears $2000+$0400 before the $D011 flip
        # (engage clean-field — see engage_bitmap_mode); the REU / host-DMA
        # double-buffer paths zero both VIC banks themselves below, so they pass
        # clear=False and only take the register pokes. Hires manages the border
        # per-frame, so no border/bg0 here.
        single_buffer = not self.use_reu_staged and not self.double_buffer
        engage_bitmap_mode(api, d011="3b", d018="18", d016="08", clear=single_buffer)
        self._last_bg = None
        if self.double_buffer:
            # Host-DMA double-buffer: zero both banks + install the minimal
            # vblank swap IRQ (no REU). See _setup_hostdma_doublebuffer.
            self._setup_hostdma_doublebuffer(api)
            log.info(
                "hires: host-DMA double-buffer armed (bank 0 ↔ bank 2, "
                "IRQ @ $%04X, tracker @ $%04X)",
                BANK_SWAP_IRQ_HANDLER_ADDR,
                FRAME_TRACKER_ADDR,
            )
        if self.use_reu_staged:
            # Zero both banks' bitmap + screen so the off-screen bank
            # doesn't show garbage on the first swap. Single full-region
            # writes — these aren't on the per-frame path.
            zeros_bitmap = bytes(REU_VIDEO_BITMAP_LEN)
            zeros_screen = bytes(REU_VIDEO_BITMAP_SCREEN_LEN)
            api.write_memory_file(f"{VIC_BANK_0.BITMAP:04X}", zeros_bitmap)
            api.write_memory_file(f"{VIC_BANK_0.SCREEN:04X}", zeros_screen)
            api.write_memory_file(f"{VIC_BANK_2.BITMAP:04X}", zeros_bitmap)
            api.write_memory_file(f"{VIC_BANK_2.SCREEN:04X}", zeros_screen)
            # Pin VIC bank to 0 (kernal default; the reset path leaves
            # CIA #2 PORT_A at this value already, but be explicit so a
            # scene-to-scene transition into REU-staged hires from a
            # non-default bank still starts from a known state).
            api.write_memory(f"{CIA2.PORT_A:04X}", f"{_DD00_BANK_0:02X}")
            self._displayed_bank = 0
            handler = (
                BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER
                if self.audio_reu_pump_active
                else BANK_SWAP_IRQ_HANDLER
            )
            _install_bank_swap_irq(api, handler, audio_pump_active=self.audio_reu_pump_active)
            log.info(
                "hires: REU bank-swap pipeline armed "
                "(bank 0 ↔ bank 2, IRQ @ $%04X, tracker @ $%04X, "
                "audio_pump=%s)",
                BANK_SWAP_IRQ_HANDLER_ADDR,
                FRAME_TRACKER_ADDR,
                self.audio_reu_pump_active,
            )

    def teardown(self, api):
        if self.use_reu_staged or self.double_buffer:
            _uninstall_bank_swap_irq(api)
            api.invalidate_cache()

    def cycle_style(self, api):
        idx = HIRES_STYLES.index(self.style)
        new_style = HIRES_STYLES[(idx + 1) % len(HIRES_STYLES)]
        self.style = new_style
        self._last_bg = None
        api.invalidate_cache()
        return f"style={new_style}"

    def compose(self, frame) -> BitmapComposeBuffers:
        assert self.frame_target_size is not None
        img = cv2.resize(frame, self.frame_target_size, interpolation=cv2.INTER_AREA)

        if self.style == "normal":
            flat = np.clip(img.reshape(-1, 3).astype(np.float32), 0, 255)
            offset_fn = _ORDERED_DITHER_OFFSET_FNS.get(self._dither_method)
            if offset_fn is not None:
                offset = offset_fn(200, 320, self._dither_strength)
                flat = np.clip(flat + offset.reshape(-1, 1), 0, 255)
            quantized = quantize_flat_for(flat, perceptual=self._perceptual).reshape(200, 320)
            counts = np.bincount(quantized.ravel(), minlength=16)
            bg = int(counts.argmax())
            sample_fg = quantized[4::8, 4::8]  # one sample per 8×8 cell
            if self._dither_method in ("floyd-steinberg", "atkinson"):
                # Re-dither each 8×8 cell's own pixels against its 2-color set
                # {bg, cell fg} — the fg PICK stays the cheap single-pixel
                # sample above (temporal stability / cost), only the per-pixel
                # fill dithers.
                pixels_cell = (
                    flat.reshape(200, 320, 3)
                    .reshape(25, 8, 40, 8, 3)
                    .transpose(0, 2, 1, 3, 4)
                    .reshape(1000, 8, 8, 3)
                )
                cand_bgr = np.stack(
                    [
                        np.broadcast_to(C64_PALETTE_BGR[bg], (1000, 3)),
                        C64_PALETTE_BGR[sample_fg.ravel()],
                    ],
                    axis=1,
                )  # (1000, 2, 3)
                codes = error_diffuse_cells(
                    pixels_cell, cand_bgr, self._dither_method, self._dither_strength
                )
                is_fg = codes.reshape(25, 40, 8, 8).transpose(0, 2, 1, 3).reshape(200, 320) == 1
            else:
                is_fg = quantized != bg
            fg_const: int | None = None
        else:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 75, 150)
            is_fg = edges > 128
            quantized = None
            # edges_inverted: black edges on white background. Swap which
            # palette index plays bg vs fg — VIC packs both into one byte
            # per cell so the bit-pattern stays identical.
            if self.style == "edges_inverted":
                bg, fg_const = 1, 0
            else:
                bg, fg_const = 0, 1

        # Bit-pack into VIC bitmap layout: 25 rows × 40 cells × 8 bytes.
        packed = np.packbits(is_fg.astype(np.uint8), axis=1)  # (200, 40)
        bitmap_ram = packed.reshape(25, 8, 40).transpose(0, 2, 1).reshape(-1)

        if fg_const is not None:
            screen_ram = np.full(1000, (fg_const << 4) | bg, dtype=np.uint8)
        else:
            assert quantized is not None
            sample_fg = quantized[4::8, 4::8]  # one sample per 8×8 cell
            screen_ram = ((sample_fg << 4) | bg).astype(np.uint8).ravel()

        return {
            "bitmap": bitmap_ram,
            "screen": screen_ram,
            "bg": bg,
            "text": HiresTextSurface(bitmap_ram, screen_ram),
        }

    def push(self, api: C64Backend, buffers: BitmapComposeBuffers) -> None:
        bg = buffers["bg"]
        # $D020 (border) is a single global register — write it from the host
        # on both paths (the REU bank-swap IRQ only manages the banked bitmap +
        # screen, not the border).
        if bg != self._last_bg:
            api.write_regs("d020", bg, bg)
            self._last_bg = bg
        bitmap_bytes = buffers["bitmap"].tobytes()
        screen_bytes = buffers["screen"].tobytes()
        if self.use_reu_staged:
            # Drop into the off-screen bank, then cue a vblank swap.
            target_bank = 1 - self._displayed_bank
            _push_bitmap_via_reu(api, bitmap_bytes, screen_bytes, target_bank)
            self._displayed_bank = target_bank
            return
        if self.double_buffer:
            # Host-DMA: write bitmap+screen into the off-screen bank, then arm
            # the vblank swap. Hires has no color RAM, so the swap is fully
            # tear-free (bg passed as the tracker's bg0 → $D021, unused in hires).
            target, bm_addr, scr_addr, bm_id, scr_id, dd00 = self._hostdma_swap_target()
            api.write_region(bm_addr, bitmap_bytes, region_id=bm_id)
            api.write_region(scr_addr, screen_bytes, region_id=scr_id)
            self._arm_hostdma_swap(api, bg, dd00)
            self._displayed_bank = target
            return
        api.write_region(0x2000, bitmap_bytes, region_id=RegionID.BITMAP)
        api.write_region(0x0400, screen_bytes, region_id=RegionID.SCREEN)


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
    ):
        _validate_palette_mode(palette_mode)
        _validate_cell_strategy(cell_strategy)
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
        self._sat_factor, self._gray_penalty = _palette_mode_settings(palette_mode)
        self._channel_boost, self._hue_corrections = _resolve_color_shaping(
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
        self._penalty_scale = PERCEPTUAL_DIST_SCALE if self._perceptual else 1.0
        self._quant_hysteresis = PERCELL_QUANT_HYSTERESIS_BONUS * self._penalty_scale
        self._code_hysteresis = PERCELL_CODE_HYSTERESIS_BONUS * self._penalty_scale
        self._dither_method = dither_method
        self._dither_strength = dither_strength
        # Per-cell 3-color selection strategy for the percell path (see
        # CELL_STRATEGIES / _pick_cell_colors). Orthogonal to palette_mode
        # (which only decides percell-vs-global) and to dither (which decides
        # the per-pixel fill after the 3 colors are chosen).
        self._cell_strategy = cell_strategy
        # Per-palette pairwise distances (no penalty — this is for the
        # "snap unused indices to their nearest of the 4 winners" remap,
        # which is a pure color-space neighbour query, not a chromatic-
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
        # PALETTE_PICK_EMA_ALPHA.
        self._smoothed_counts: np.ndarray | None = None
        # EMA-smoothed per-cell counts for percell top-3 picks; see
        # PERCELL_PICK_EMA_ALPHA. Shape (1000, 16), float32.
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
        # PERCELL_QUANT_HYSTERESIS_BONUS. Shape (32000,) int64.
        self._last_quantized: np.ndarray | None = None
        # Sticky bg0 for the percell path (see BG0_HYSTERESIS_MARGIN). None =
        # no prior pick, so the first frame takes the raw argmax.
        self._bg0: int | None = None
        # Opt-in REU bank-swap pipeline. See MHIRES_BANK_SWAP_IRQ_HANDLER
        # and _push_mhires_via_reu for the per-frame mechanics. When the
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
        _validate_palette_mode(palette_mode)
        self.palette_mode = palette_mode
        if force_palette is not None:
            self._force_palette = force_palette
        self._sat_factor, self._gray_penalty = _palette_mode_settings(palette_mode)
        self._apply_grayscale_fixed_slots()
        self._smoothed_counts = None
        self._smoothed_cell_counts = None
        self._last_cand = None
        self._last_codes = None
        self._last_quantized = None
        self._bg0 = None
        self._last_bg = None
        api.invalidate_cache()
        return f"palette_mode={palette_mode}" + ("+forced" if self._force_palette else "")

    def cycle_style(self, api):
        new_mode, new_force, _label = _advance_palette_cycle(
            self.palette_mode, self._force_palette, self._color_map is not None
        )
        return self.set_palette_mode(api, new_mode, force_palette=new_force)

    def setup(self, api):
        super().setup(api)
        # Single-buffer bring-up clears $2000+$0400 before the $D011 flip (engage
        # clean-field — see engage_bitmap_mode); the REU / host-DMA double-buffer
        # paths zero both VIC banks themselves below (clear=False). border ($D020)
        # = black on every path. bg0 ($D021) = black on the non-REU paths so the
        # pre-first-frame screen is solid black (a zeroed mhires bitmap is all-%00
        # → bg0); the REU path leaves $D021 to its swap-tracker IRQ.
        single_buffer = not self.use_reu_staged and not self.double_buffer
        engage_bitmap_mode(
            api,
            d011="3b",
            d018="18",
            d016="18",
            border=0x00,
            bg0=(None if self.use_reu_staged else 0x00),
            clear=single_buffer,
        )
        self._smoothed_cell_counts = None
        self._last_cand = None
        self._last_codes = None
        self._last_quantized = None
        self._bg0 = None
        if not self.use_reu_staged:
            # _last_bg tracks the host-written $D021 (single-buffer only — the
            # double-buffer path flips $D021 via the swap tracker instead).
            self._last_bg = 0
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
            api.write_memory(f"{CIA2.PORT_A:04X}", f"{_DD00_BANK_0:02X}")
            self._displayed_bank = 0
            handler = (
                MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER
                if self.audio_reu_pump_active
                else MHIRES_BANK_SWAP_IRQ_HANDLER
            )
            _install_bank_swap_irq(
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
        if self.use_reu_staged or self.double_buffer:
            _uninstall_bank_swap_irq(api)
            api.invalidate_cache()

    def compose(self, frame) -> MHiresComposeBuffers:
        assert self.frame_target_size is not None
        img = cv2.resize(frame, self.frame_target_size, interpolation=cv2.INTER_AREA)
        if self._force_palette and self._color_map is not None:
            # Forced-palette remap: emit exact C64 colors and skip the faithful
            # shaping stages + gray penalty (the remap already chose each color).
            flat = self._color_map.apply(img).reshape(-1, 3).astype(np.float32)
            d = quantize_distances(flat)
        else:
            if self._color_fit is not None:
                img = apply_color_fit(img, self._color_fit)
            img = boost_saturation(img, self._sat_factor)
            # Global [color] shaping: hue-band corrections then per-channel boost.
            img = apply_hue_corrections(img, self._hue_corrections)
            flat = np.clip(img.reshape(-1, 3).astype(np.float32) * self._channel_boost, 0, 255)
            offset_fn = _ORDERED_DITHER_OFFSET_FNS.get(self._dither_method)
            if offset_fn is not None:
                w, h = self.frame_target_size
                offset = offset_fn(h, w, self._dither_strength)
                flat = np.clip(flat + offset.reshape(-1, 1), 0, 255)
            # In-place gray-penalty add (scaled to the active metric) avoids a
            # second (N,16) float32 alloc.
            d = quantize_distances_for(flat, perceptual=self._perceptual)
            d += self._gray_penalty * self._penalty_scale

        if self.palette_mode == "percell":
            bitmap_ram, screen_ram, color_ram, bg0 = self._compose_percell(d, flat)
        else:
            bitmap_ram, screen_ram, color_ram, bg0 = self._compose_global(d)
        return {
            "bitmap": bitmap_ram,
            "screen": screen_ram,
            "color": color_ram,
            "bg": bg0,
            "text": MHiresTextSurface(
                bitmap_ram, screen_ram, color_ram, double_height=self.text_double_height
            ),
        }

    def push(self, api: C64Backend, buffers: MHiresComposeBuffers) -> None:
        bg0 = buffers["bg"]
        bitmap_bytes = buffers["bitmap"].tobytes()
        screen_bytes = buffers["screen"].tobytes()
        color_bytes = buffers["color"].tobytes()
        if self.use_reu_staged:
            target_bank = 1 - self._displayed_bank
            _push_mhires_via_reu(api, bitmap_bytes, screen_bytes, color_bytes, bg0, target_bank)
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
        lut = build_fade_lut(self.fade_alpha)
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
            smoothed = _ema_counts(self, quantized)
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
        self, d: np.ndarray, flat: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
        """Per-cell path: pick bg0 globally, then for each 4×8 cell pick
        its own top-3 non-bg0 colors by population. Each pixel resolves
        against its cell's local {bg0, c1, c2, c3} set, so screen RAM and
        color RAM both carry per-cell content instead of one repeated byte.
        Returns (bitmap_ram, screen_ram, color_ram, bg0).

        Both the global bg0 pick and the per-cell top-3 picks go through
        EMA-smoothed counts (PALETTE_PICK_EMA_ALPHA for bg0,
        PERCELL_PICK_EMA_ALPHA for cells) so a few-pixel reshuffle from
        sensor noise can't flip which palette entries win a slot every
        frame. Without per-cell smoothing the unsmoothed top-3 flipped on
        ~7% of cells per frame even on static webcam content, rewriting
        screen + color RAM and remapping each affected cell's bitmap codes."""
        quantized = np.argmin(d, axis=1)  # (32000,) palette idx

        # Per-pixel decision hysteresis on the palette index: if the
        # previous frame's choice is within PERCELL_QUANT_HYSTERESIS_BONUS
        # of the new minimum distance, keep it. Stabilises the per-pixel
        # argmin against sensor noise / sub-pixel-shake aliasing on
        # textured static subjects (striped rug, slatted blinds) WITHOUT
        # smearing motion: a real colour change moves d² by far more than
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
        smoothed = _ema_counts(self, quantized)
        cand = int(np.argmax(smoothed))
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
        d_cell = d.reshape(25, 8, 40, 4, 16).transpose(0, 2, 1, 3, 4).reshape(1000, 32, 16)

        cell_ids = np.repeat(np.arange(1000), 32)
        combined = cell_ids * 16 + cells.ravel()
        cell_counts_raw = (
            np.bincount(combined, minlength=16000).reshape(1000, 16).astype(np.float32)
        )
        # EMA-smooth so a 1-2 pixel reshuffle from sensor noise on a flat
        # cell doesn't flip the 3rd top-3 slot every frame. The raw counts
        # are stored across all 16 entries (bg0 included) so a future bg0
        # change just remasks — the old-bg0's accumulated count stays valid
        # the moment it becomes pickable again.
        if self._smoothed_cell_counts is None:
            self._smoothed_cell_counts = cell_counts_raw
        else:
            a = PERCELL_PICK_EMA_ALPHA
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
        # / luminance / contrast / error-min — see _pick_cell_colors). All keep
        # the absent→bg0 poison-filler guard above.
        top3 = _pick_cell_colors(cell_counts, d_cell, bg0, self._cell_strategy)
        # Sort by palette index for delta-cache stability (otherwise the slot
        # order would flip even when the chosen SET is identical).
        top3 = np.sort(top3, axis=1)
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
            cand_bgr = C64_PALETTE_BGR[cand]  # (1000, 4, 3)
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
            # within PERCELL_CODE_HYSTERESIS_BONUS of the new minimum distance,
            # but only for cells whose cand is bit-identical to last frame (a
            # change in any cand slot means the codes 0..3 no longer point at
            # the same palette entries they did last frame, so previous codes
            # are meaningless). Suppresses the per-pixel boundary flicker that
            # remains after the per-cell EMA stabilises {bg0,c1,c2,c3}.
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
        screen_ram = ((top3[:, 0] << 4) | top3[:, 1]).astype(np.uint8)
        color_ram = top3[:, 2].astype(np.uint8)
        return bitmap_ram, screen_ram, color_ram, bg0
