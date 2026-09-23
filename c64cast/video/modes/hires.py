"""320x200 hires bitmap mode + its style table."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import cast

import cv2
import numpy as np

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import CIA2, D018_HIRES_PAGE_A, VIC, VIC_BANK_0, VIC_BANK_2, RegionID
from c64cast.scenes.text_surface import HiresTextSurface
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
    BANK_SWAP_IRQ_HANDLER,
    BANK_SWAP_IRQ_HANDLER_ADDR,
    BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER,
    DD00_BANK_0,
    FRAME_TRACKER_ADDR,
    REU_VIDEO_BITMAP_LEN,
    REU_VIDEO_BITMAP_SCREEN_LEN,
    install_bank_swap_irq,
    push_bitmap_via_reu,
    uninstall_bank_swap_irq,
)
from c64cast.video.palette import (
    C64_PALETTE_BGR,
    COLOR_MATCH_MODES,
    HIRES_CELL_PICKS,
    PERCEPTUAL_DIST_SCALE,
    quantize_distances_for,
)

from .base import (
    BG0_HYSTERESIS_MARGIN,
    ORDERED_DITHER_OFFSET_FNS,
    BitmapComposeBuffers,
    FlickerComposeBuffers,
)
from .bitmap import BitmapDisplayMode, engage_bitmap_mode

log = logging.getLogger(__name__)


HIRES_STYLES = ("normal", "edges", "edges_inverted")

# Per-cell foreground stickiness for the error-min pick, in d² space (scaled by
# PERCEPTUAL_DIST_SCALE under the Lab metric, as base.py's percell bonuses are).
# Swept on noisy static and panning sequences; see
# docs/architecture/video-color.md#colorhires_cell_pick--which-color-fills-a-hires-cell.
HIRES_CELL_HYSTERESIS_BONUS = 2000.0


def _pack_screen(fg: np.ndarray, bg: int) -> np.ndarray:
    """Pack per-cell foreground + global background palette indices into the
    1000-byte VIC screen matrix (hi nibble = fg, lo nibble = bg)."""
    return ((np.asarray(fg).astype(np.uint8) << 4) | np.uint8(bg & 0x0F)).ravel()


def _validate_hires_style(style: str) -> None:
    if style not in HIRES_STYLES:
        raise ValueError(f"hires style must be one of {HIRES_STYLES}, got {style!r}")


def _validate_cell_pick(pick: str) -> None:
    if pick not in HIRES_CELL_PICKS:
        raise ValueError(f"hires cell_pick must be one of {HIRES_CELL_PICKS}, got {pick!r}")


class HiresDisplayMode(BitmapDisplayMode):
    """320×200 bitmap.

    style:
      "normal"          — luma-quantized: per-cell fg + dominant bg.
      "edges"           — Canny edges in white on black.
      "edges_inverted"  — Canny edges in black on white (negative print).

    cell_pick: how the "normal" style chooses each cell's foreground.
      "error-min" (default) minimizes the cell's own error; "sample" reads
      one pixel per cell. See
      docs/architecture/video-color.md#colorhires_cell_pick--which-color-fills-a-hires-cell.

    use_reu_staged: opt into the REU bank-swap double-buffer pipeline.
      Each frame's bitmap + screen are REUWRITE-staged into REU SRAM
      (bus-clean) then dropped into the OFF-SCREEN VIC bank via two
      REU→main DMAs while VIC keeps rendering the on-screen bank. A
      C64-side raster IRQ at vblank flips $DD00 to bring up the new
      bank tear-free. See push_bitmap_via_reu / install_bank_swap_irq
      and the REU_VIDEO_BITMAP_* constants in modes_irq.py.

      Runs alongside [audio].use_reu_pump. Both drive REC and $0314,
      so setup() installs BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER — the merged
      dispatcher — whenever audio_reu_pump_active is set. Color RAM
      isn't used by hires (color is in screen RAM nibbles), so the
      shared-$D800 mid-frame-mismatch problem the other display modes
      would have doesn't apply.
    """

    name = "hires"
    frame_target_size = (320, 200)
    LIVE_PARAMS = {"dither_strength": (0.0, 2.0)}
    LIVE_CHOICES = {
        "dither_method": DITHER_METHODS,
        "color_match": COLOR_MATCH_MODES,
        "cell_pick": HIRES_CELL_PICKS,
    }

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
        cell_pick: str = "error-min",
        flicker_tolerance: str = DEFAULT_TOLERANCE,
        flicker_max_luma_delta: float = 0.075,
        flicker_score_pairs: Sequence[str] | None = None,
    ):
        _validate_hires_style(style)
        _validate_cell_pick(cell_pick)
        self.style = style
        # `self._blend_table is None` is the off state every blend branch keys
        # on. Only the "normal" style picks color, so it is inert on edges.
        _blending = FLICKER_TOLERANCES.get(flicker_tolerance, -1) >= 0
        _scored = parse_scoring_pairs(flicker_score_pairs) if flicker_score_pairs else None
        self._blend_table: BlendTable | None = (
            build_blend_table(
                flicker_max_luma_delta, tolerance=flicker_tolerance, score_pairs=_scored
            )
            if _blending and style == "normal"
            else None
        )
        self._last_bg_index: int | None = None
        self._cell_pick = cell_pick
        self._last_fg: np.ndarray | None = None
        self._perceptual = bool(perceptual)
        if self._blend_table is not None and not self._perceptual:
            # Blends are Lab-defined, and measured under weighted-BGR the
            # widened palette scores worse than the 16 solids (+2.5% on a photo,
            # +6.3% on a luminance ramp).
            log.info("hires: flicker blend forces color_match=perceptual (blends are Lab-defined)")
            self._perceptual = True
        self._dither_method = dither_method
        self._dither_strength = dither_strength
        self._last_bg: int | None = None
        self.use_reu_staged = use_reu_staged
        # Mutually exclusive with use_reu_staged; resolve_double_buffer
        # guarantees it.
        self.double_buffer = double_buffer
        # Selects BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER in setup(), whose dispatcher
        # falls through to the $C100 audio pump on non-raster (CIA #1) IRQs.
        self.audio_reu_pump_active = audio_reu_pump_active
        # Which VIC bank is displayed: 0 = bank 0 (paint bank 2 next),
        # 1 = bank 2 (paint bank 0 next).
        self._displayed_bank = 0

    @property
    def dither_strength(self) -> float:
        return self._dither_strength

    @dither_strength.setter
    def dither_strength(self, value: float) -> None:
        self._dither_strength = float(value)

    def set_dither_method(self, value: str) -> str:
        self._dither_method = value
        return f"dither_method={value}"

    def set_color_match(self, value: str) -> str:
        """Live-swap the nearest-palette metric (no-op on the fixed 2-color
        edges styles), pinned to perceptual while blending."""
        if self._blend_table is not None:
            return "color_match=perceptual (pinned by flicker_tolerance)"
        self._perceptual = value == "perceptual"
        return f"color_match={value}"

    def set_cell_pick(self, value: str) -> str:
        """Live-swap the per-cell foreground pick, dropping the hysteresis
        state — the two strategies pick by different criteria, so a carried-over
        previous pick would hold the old strategy's answers for a frame."""
        _validate_cell_pick(value)
        self._cell_pick = value
        self._last_fg = None
        return f"cell_pick={value}"

    def _sticky_bg(self, counts: np.ndarray) -> int:
        """Pick the global background entry, holding the previous one unless a
        challenger beats it by BG0_HYSTERESIS_MARGIN.

        Blend-only. bg fills every %0 pixel, so under blending a bg flip can
        switch the whole background between steady and alternating. The margin
        is the one mhires uses on $D021."""
        best = int(counts.argmax())
        prev = self._last_bg_index
        if (
            prev is not None
            and prev < counts.shape[0]
            and counts[prev] >= counts[best] * (1.0 - BG0_HYSTERESIS_MARGIN)
        ):
            best = prev
        self._last_bg_index = best
        return best

    def _errmin_fg(self, dist: np.ndarray, bg: int) -> tuple[np.ndarray, np.ndarray]:
        """Pick each cell's foreground by minimizing that cell's own error, and
        return (per-cell fg (25, 40), per-pixel fg mask (200, 320)).

        Every pixel ends up showing whichever of {bg, fg} is nearer, so a
        candidate's cost for a cell is exactly that elementwise minimum averaged
        over the cell's 64 pixels — no search, one argmin over the 16 entries.
        The distance matrix it needs is the one the quantizer already built.

        The `"sample"` alternative reads a single pixel per cell instead, and
        stays available for the tightest CPU budgets: this path costs ≈+0.8
        ms/frame.
        """
        entries = dist.shape[1]
        # (1000, 64, E): each cell's 8×8 pixels against every candidate, in the
        # same row/col interleave the dither path's pixels_cell uses.
        per_cell = (
            dist.reshape(25, 8, 40, 8, entries).transpose(0, 2, 1, 3, 4).reshape(1000, 64, entries)
        )
        d_bg = per_cell[:, :, bg : bg + 1]
        cell_cost = np.minimum(d_bg, per_cell).mean(axis=1)  # (1000, E)
        best = cell_cost.argmin(axis=1)
        prev = self._last_fg
        if prev is not None and prev.shape == best.shape:
            rows = np.arange(cell_cost.shape[0])
            bonus = HIRES_CELL_HYSTERESIS_BONUS * (
                PERCEPTUAL_DIST_SCALE if self._perceptual else 1.0
            )
            keep = cell_cost[rows, prev] <= cell_cost[rows, best] + bonus
            best = np.where(keep, prev, best)
        self._last_fg = best
        rows2 = np.arange(1000)[:, None]
        cols = np.arange(64)[None, :]
        is_fg_cell = per_cell[rows2, cols, best[:, None]] < d_bg[:, :, 0]
        is_fg = is_fg_cell.reshape(25, 40, 8, 8).transpose(0, 2, 1, 3).reshape(200, 320)
        return best.reshape(25, 40), is_fg

    def setup(self, api):
        super().setup(api)
        # The double-buffer paths zero both VIC banks themselves below, so they
        # take only the register pokes. border=0x00 covers the window between
        # here and the first push(); hires ignores $D021, so bg0=0x00 just keeps
        # the register off the previous scene's value.
        single_buffer = not self.use_reu_staged and not self.double_buffer
        engage_bitmap_mode(
            api,
            d011="3b",
            d018=f"{D018_HIRES_PAGE_A:02X}",
            d016="08",
            border=0x00,
            bg0=0x00,
            clear=single_buffer,
        )
        # None, not 0, so the first push() re-asserts the border/bg0 pair even
        # when the first frame's bg is black.
        self._last_bg = None
        self._last_fg = None
        self._last_bg_index = None
        if self._blend_table is not None:
            # self.double_buffer stays False: the plain host-DMA path installs a
            # swap handler with no $D018 phase toggle, and the two cannot both
            # own $0314.
            self._setup_flicker_doublebuffer(api)
            self._log_flicker_arming(self._blend_table, blendable="fg + bg (both screen nibbles)")
        elif self.double_buffer:
            self._setup_hostdma_doublebuffer(api)
            log.info(
                "hires: host-DMA double-buffer armed (bank 0 ↔ bank 2, "
                "IRQ @ $%04X, tracker @ $%04X)",
                BANK_SWAP_IRQ_HANDLER_ADDR,
                FRAME_TRACKER_ADDR,
            )
        if self.use_reu_staged:
            # So the off-screen bank shows no garbage on the first swap.
            zeros_bitmap = bytes(REU_VIDEO_BITMAP_LEN)
            zeros_screen = bytes(REU_VIDEO_BITMAP_SCREEN_LEN)
            api.write_memory_file(f"{VIC_BANK_0.BITMAP:04X}", zeros_bitmap)
            api.write_memory_file(f"{VIC_BANK_0.SCREEN:04X}", zeros_screen)
            api.write_memory_file(f"{VIC_BANK_2.BITMAP:04X}", zeros_bitmap)
            api.write_memory_file(f"{VIC_BANK_2.SCREEN:04X}", zeros_screen)
            # Pin VIC bank 0, so a transition in from a non-default bank still
            # starts from a known state.
            api.write_memory(f"{CIA2.PORT_A:04X}", f"{DD00_BANK_0:02X}")
            self._displayed_bank = 0
            handler = (
                BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER
                if self.audio_reu_pump_active
                else BANK_SWAP_IRQ_HANDLER
            )
            install_bank_swap_irq(api, handler, audio_pump_active=self.audio_reu_pump_active)
            log.info(
                "hires: REU bank-swap pipeline armed "
                "(bank 0 ↔ bank 2, IRQ @ $%04X, tracker @ $%04X, "
                "audio_pump=%s)",
                BANK_SWAP_IRQ_HANDLER_ADDR,
                FRAME_TRACKER_ADDR,
                self.audio_reu_pump_active,
            )

    def teardown(self, api):
        if self.use_reu_staged or self.double_buffer or self._blend_table is not None:
            uninstall_bank_swap_irq(api)
            if self._blend_table is not None:
                # uninstall restores $DD00 but not $D018, which the flicker
                # handler may have left on the $0C00 page — a char scene would
                # then read its matrix from the wrong offset. Only safe after
                # uninstall: before it, the next field's IRQ restores the page.
                api.write_memory(f"{VIC.D018_MEMORY:04X}", f"{VIC.D018_CHAR_DEFAULT:02X}")
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
        table = self._blend_table

        if self.style == "normal":
            flat = np.clip(img.reshape(-1, 3).astype(np.float32), 0, 255)
            offset_fn = ORDERED_DITHER_OFFSET_FNS.get(self._dither_method)
            if offset_fn is not None:
                offset = offset_fn(200, 320, self._dither_strength)
                flat = np.clip(flat + offset.reshape(-1, 1), 0, 255)
            # A blend entry is the pair (a, b) and a solid is (c, c), so every
            # index below is just an entry and only the final nibble split cares
            # which kind it is.
            if table is None:
                dist = quantize_distances_for(flat, perceptual=self._perceptual)
            else:
                dist = blend_distances_for(flat, table, perceptual=self._perceptual)
            entry_bgr = C64_PALETTE_BGR if table is None else table.bgr
            quantized = dist.argmin(axis=1).reshape(200, 320)
            counts = np.bincount(quantized.ravel(), minlength=dist.shape[1])
            bg = int(counts.argmax()) if table is None else self._sticky_bg(counts)
            if self._cell_pick == "error-min" or table is not None:
                # Blending forces the cell fit regardless of cell_pick: a blend
                # entry sits between its two solids, so a single sample lands on
                # one of them at random and the widened palette then measures
                # worse than the 16 solids.
                sample_fg, is_fg = self._errmin_fg(dist, bg)
            else:
                sample_fg = quantized[4::8, 4::8]  # one sample per 8×8 cell
                is_fg = quantized != bg
            if self._dither_method in ("floyd-steinberg", "atkinson"):
                # Re-dither each 8×8 cell's pixels against its {bg, cell fg} set,
                # replacing the nearest-of-two assignment above. The cell's two
                # colors are already fixed by this point.
                pixels_cell = (
                    flat.reshape(200, 320, 3)
                    .reshape(25, 8, 40, 8, 3)
                    .transpose(0, 2, 1, 3, 4)
                    .reshape(1000, 8, 8, 3)
                )
                cand_bgr = np.stack(
                    [
                        np.broadcast_to(entry_bgr[bg], (1000, 3)),
                        entry_bgr[sample_fg.ravel()],
                    ],
                    axis=1,
                )  # (1000, 2, 3)
                codes = error_diffuse_cells(
                    pixels_cell, cand_bgr, self._dither_method, self._dither_strength
                )
                is_fg = codes.reshape(25, 40, 8, 8).transpose(0, 2, 1, 3).reshape(200, 320) == 1
            fg_const: int | None = None
        else:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 75, 150)
            is_fg = edges > 128
            quantized = None
            # Swapping which index plays bg vs fg is enough: VIC packs both into
            # one byte per cell, so the bit pattern is identical either way.
            if self.style == "edges_inverted":
                bg, fg_const = 1, 0
            else:
                bg, fg_const = 0, 1

        # VIC bitmap layout: 25 rows × 40 cells × 8 bytes.
        packed = np.packbits(is_fg.astype(np.uint8), axis=1)  # (200, 40)
        bitmap_ram = packed.reshape(25, 8, 40).transpose(0, 2, 1).reshape(-1)

        if fg_const is not None or table is None:
            if fg_const is not None:
                screen_ram = np.full(1000, (fg_const << 4) | bg, dtype=np.uint8)
            else:
                screen_ram = _pack_screen(sample_fg, bg)
            plain: BitmapComposeBuffers = {
                "bitmap": bitmap_ram,
                "screen": screen_ram,
                "bg": bg,
                "text": HiresTextSurface(bitmap_ram, screen_ram),
            }
            return plain

        # Split each entry into the index its field shows. Both pages share the
        # bitmap, so a solid entry writes the same byte to each and never
        # alternates.
        fg_a, fg_b = table.field_pages(sample_fg)
        bg_a, bg_b = (int(v) for v in table.pairs[bg])
        screen_ram = _pack_screen(fg_a, bg_a)
        screen_b = _pack_screen(fg_b, bg_b)
        flicker: FlickerComposeBuffers = {
            "bitmap": bitmap_ram,
            "screen": screen_ram,
            "screen_b": screen_b,
            # $D020 is a single register the field IRQ does not manage, so the
            # border cannot blend; it takes the field-A component.
            "bg": bg_a,
            "text": HiresTextSurface(bitmap_ram, screen_ram),
        }
        return flicker

    def push(self, api: C64Backend, buffers: BitmapComposeBuffers) -> None:
        bg = buffers["bg"]
        # $D020 is a single global register the REU bank-swap IRQ does not
        # manage, so the host writes it on both paths.
        if bg != self._last_bg:
            api.write_regs("d020", bg, bg)
            self._last_bg = bg
        bitmap_bytes = buffers["bitmap"].tobytes()
        screen_bytes = buffers["screen"].tobytes()
        if self._blend_table is not None:
            # The field alternation free-runs against the displayed bank, so
            # staging both pages plus the bitmap lets the next phase-0 vblank
            # bring the whole pair set up in one piece.
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
            page_b_bytes = cast(FlickerComposeBuffers, buffers)["screen_b"].tobytes()
            api.write_region(bm_addr, bitmap_bytes, region_id=bm_id)
            api.write_region(page_a, screen_bytes, region_id=page_a_id)
            api.write_region(page_b, page_b_bytes, region_id=page_b_id)
            self._arm_flicker_swap(api, bg, dd00)
            self._displayed_bank = target
            return
        if self.use_reu_staged:
            target_bank = 1 - self._displayed_bank
            push_bitmap_via_reu(api, bitmap_bytes, screen_bytes, target_bank)
            self._displayed_bank = target_bank
            return
        if self.double_buffer:
            # Hires has no color RAM, so this swap is fully tear-free.
            target, bm_addr, scr_addr, bm_id, scr_id, dd00 = self._hostdma_swap_target()
            api.write_region(bm_addr, bitmap_bytes, region_id=bm_id)
            api.write_region(scr_addr, screen_bytes, region_id=scr_id)
            self._arm_hostdma_swap(api, bg, dd00)
            self._displayed_bank = target
            return
        api.write_region(0x2000, bitmap_bytes, region_id=RegionID.BITMAP)
        api.write_region(0x0400, screen_bytes, region_id=RegionID.SCREEN)
