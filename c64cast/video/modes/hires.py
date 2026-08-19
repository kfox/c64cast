"""320x200 hires bitmap mode + its style table."""

from __future__ import annotations

import logging
from typing import cast

import cv2
import numpy as np

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import CIA2, VIC, VIC_BANK_0, VIC_BANK_2, RegionID
from c64cast.scenes.text_surface import HiresTextSurface
from c64cast.video.dither import DITHER_METHODS, error_diffuse_cells
from c64cast.video.flicker import (
    DEFAULT_TOLERANCE,
    FLICKER_TOLERANCES,
    WARN_LUMA_DELTA,
    BlendTable,
    blend_distances_for,
    build_blend_table,
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

# Per-cell foreground stickiness for the error-min pick, in d² space (the units
# quantize_distances returns — scaled by PERCEPTUAL_DIST_SCALE under the Lab
# metric, the same convention the percell bonuses in base.py use).
#
# Well below base.py's per-pixel 5000 because the quantity is different: this
# thresholds a mean d² over a cell's 64 pixels, which averages most of the
# sensor noise out before the comparison ever happens, where the percell bonus
# thresholds one pixel's own distance. Swept on a noisy static subject and a
# panning one: 2000 already takes static-subject screen churn to zero for +0.06
# Lab on the panning case, and everything above it only buys lag — 5000 costs
# +0.28, 15000 +1.05, 50000 +6.6, for progressively less churn benefit. Since
# this is a decision hysteresis and not a smoother, over-damping shows up
# directly as motion inaccuracy, so the knee is the right place to sit.
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
      "error-min" (default) minimises the cell's own error; "sample" reads
      one pixel per cell. See _errmin_fg for why the accurate pick is also
      the stabler one.

    use_reu_staged: opt into the REU bank-swap double-buffer pipeline.
      Each frame's bitmap + screen are REUWRITE-staged into REU SRAM
      (bus-clean) then dropped into the OFF-SCREEN VIC bank via two
      REU→main DMAs while VIC keeps rendering the on-screen bank. A
      C64-side raster IRQ at vblank flips $DD00 to bring up the new
      bank tear-free. See push_bitmap_via_reu / install_bank_swap_irq
      and the REU_VIDEO_BITMAP_* constants in modes_irq.py.

      Cannot coexist with [audio].use_reu_pump (both share REC + $0314)
      — validate_scene_cfg enforces this at load time. Color RAM isn't
      used by hires (color is in screen RAM nibbles), so the shared-
      $D800 mid-frame-mismatch problem the other display modes would
      have doesn't apply.
    """

    name = "hires"
    frame_target_size = (320, 200)
    # Live-tune surface (see DisplayMode.LIVE_PARAMS). Hires quantizes (in the
    # "normal" style) with spatial dither + nearest-palette matching, so those
    # are live; it does NOT apply the adaptive color fit (no auto_fit_strength)
    # and has no palette_mode / per-cell axis.
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
    ):
        _validate_hires_style(style)
        _validate_cell_pick(cell_pick)
        self.style = style
        # Flicker blend ([color].flicker_tolerance). None = off, and every
        # blend branch is keyed on that rather than a bool so the plain path
        # keeps running the 16-entry quantizer it always did. Only the "normal"
        # style picks colour, so blending is a no-op on the edges styles.
        _blending = FLICKER_TOLERANCES.get(flicker_tolerance, -1) >= 0
        self._blend_table: BlendTable | None = (
            build_blend_table(flicker_max_luma_delta, tolerance=flicker_tolerance)
            if _blending and style == "normal"
            else None
        )
        self._last_bg_index: int | None = None
        # Which colour each cell's foreground takes ([color].hires_cell_pick).
        # Only the "normal" style picks colour at all — the edges styles are
        # fixed 2-colour, so this is a no-op there, same as _perceptual.
        self._cell_pick = cell_pick
        self._last_fg: np.ndarray | None = None
        # Perceptual (CIE-Lab) nearest-palette matching ([color].color_match).
        # Only the "normal" style quantizes color (bg + per-cell fg samples); the
        # edges styles are fixed 2-color, so this is a no-op there.
        self._perceptual = bool(perceptual)
        if self._blend_table is not None and not self._perceptual:
            # Blending is defined perceptually — a pair's fused colour is a
            # linear-light average and its eligibility is a Lab gap — so fitting
            # in weighted-BGR optimises a different space than the one the extra
            # entries live in. Measured, that mismatch is enough to make the
            # widened palette score WORSE than the 16 solids on photographic
            # content (+2.5%) and on a luminance ramp (+6.3%), where under the
            # Lab metric the same frames improve by 2-3%. Forced rather than
            # refused: color_match's own default already resolves here, so this
            # only fires when a config explicitly asked for "rgb".
            log.info("hires: flicker blend forces color_match=perceptual (blends are Lab-defined)")
            self._perceptual = True
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

    # --- live-tune setters (see DisplayMode.LIVE_PARAMS / LIVE_CHOICES) ---
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
        edges styles). Hires carries no d²-space penalty to rescale.

        Pinned while blending, for the reason __init__ gives: the widened
        palette is Lab-defined and measurably regresses under weighted-BGR."""
        if self._blend_table is not None:
            return "color_match=perceptual (pinned by flicker_tolerance)"
        self._perceptual = value == "perceptual"
        return f"color_match={value}"

    def set_cell_pick(self, value: str) -> str:
        """Live-swap the per-cell foreground pick. Drops the hysteresis state:
        the two strategies choose from the same 16 entries but by different
        criteria, so carrying a "previous pick" across the swap would hold the
        old strategy's answers for a frame."""
        _validate_cell_pick(value)
        self._cell_pick = value
        self._last_fg = None
        return f"cell_pick={value}"

    def _sticky_bg(self, counts: np.ndarray) -> int:
        """Pick the global background entry, holding the previous one unless a
        challenger beats it by BG0_HYSTERESIS_MARGIN.

        Blend-only. bg fills every %0 pixel, so under blending a bg flip does not
        merely recolour the field — it can switch the whole background between
        steady and alternating, which reads far harder than the colour change
        itself. The margin is the one mhires uses on $D021, for the same reason:
        track a sustained shift, ignore a near-tie."""
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
        """Pick each cell's foreground by minimising that cell's own error, and
        return (per-cell fg (25, 40), per-pixel fg mask (200, 320)).

        Every pixel ends up showing whichever of {bg, fg} is nearer, so a
        candidate's cost for a cell is exactly that elementwise minimum averaged
        over the cell's 64 pixels — no search, one argmin over the 16 entries.
        The distance matrix it needs is the one the quantizer already built.

        The `"sample"` alternative reads a single pixel per cell instead. It was
        kept for a long time on the grounds that it costs less and holds still
        better, and the second half of that turns out not to survive
        measurement: against `"sample"` on a noisy static subject this scores
        -34% mean Lab error AND drops per-frame screen churn to zero (`"sample"`
        sits at ~33 bytes/frame), because a one-pixel read tracks sensor noise
        directly while a whole-cell mean averages it out. It is the more
        accurate pick and the stabler one at once; the cost half of the claim is
        real but small (≈+0.8 ms/frame). `"sample"` stays available for the
        tightest CPU budgets.
        """
        entries = dist.shape[1]
        # (1000, 64, E): each cell's 8×8 pixels against every candidate. Same
        # row/col interleave the dither path uses for pixels_cell below.
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
        # Single-buffer bring-up clears $2000+$0400 before the $D011 flip
        # (engage clean-field — see engage_bitmap_mode); the REU / host-DMA
        # double-buffer paths zero both VIC banks themselves below, so they pass
        # clear=False and only take the register pokes. border=0x00 here closes
        # the window between this setup() and the first push() (which re-asserts
        # the real per-frame border on every subsequent frame) — without it the
        # engage would reveal a clean black bitmap under whatever border color
        # the previous scene left behind. bg0=0x00 is belt-and-braces (hires
        # ignores $D021 — background is the screen-RAM nibble, already zeroed
        # by the clear above) but matches voice_scope's hires bring-up so the
        # register isn't left holding a stale value from the prior scene.
        single_buffer = not self.use_reu_staged and not self.double_buffer
        engage_bitmap_mode(
            api, d011="3b", d018="18", d016="08", border=0x00, bg0=0x00, clear=single_buffer
        )
        # None (not 0) so the first push() unconditionally re-asserts the
        # border/bg0 pair even when the first frame's bg happens to be black —
        # push() also touches $D021 (unused in hires, but other code/tests
        # treat the pair as atomic), which the setup-time write above doesn't.
        self._last_bg = None
        self._last_fg = None
        self._last_bg_index = None
        if self._blend_table is not None:
            # Bank-swapping double-buffer with a second screen page per bank and
            # the field-alternating swap IRQ. self.double_buffer stays False for
            # this: the plain host-DMA path installs a swap handler with no $D018
            # phase toggle, and the two cannot share $0314.
            table = self._blend_table
            self._setup_flicker_doublebuffer(api)
            log.info(
                "hires: flicker blend armed — %d blend pairs at tolerance %r, "
                "ΔY <= %.3f (%d effective colours), pages $%04X/$%04X, "
                "IRQ @ $%04X",
                table.blend_count,
                table.tolerance,
                table.max_luma_delta,
                table.size,
                VIC_BANK_0.SCREEN,
                VIC_BANK_0.SCREEN_ALT,
                BANK_SWAP_IRQ_HANDLER_ADDR,
            )
            if table.max_luma_delta > WARN_LUMA_DELTA:
                log.warning(
                    "hires: flicker_max_luma_delta = %.3f is above %.2f, where pairs "
                    "start to read as luminance flicker rather than color. A blended "
                    "area alternates at the video field rate (25 Hz PAL / 30 Hz NTSC), "
                    "which is inside the recognized photosensitive-seizure band — "
                    "don't raise this for a stream anyone else will watch.",
                    table.max_luma_delta,
                    WARN_LUMA_DELTA,
                )
            if table.tolerance in ("visible", "strobe"):
                log.warning(
                    "hires: flicker_tolerance = %r admits pairs scored as visibly "
                    "flickering rather than fusing. A blended area alternates at the "
                    "video field rate (25 Hz PAL / 30 Hz NTSC), inside the recognized "
                    "photosensitive-seizure band — don't use this for a stream anyone "
                    "else will watch.",
                    table.tolerance,
                )
            # Which pairs specifically — the thing you want when deciding
            # whether flicker_max_luma_delta is set where you meant it.
            log.debug("hires: flicker pairs = %s", ", ".join(table.describe()))
        elif self.double_buffer:
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
                # uninstall restores $DD00 but not $D018, and the flicker handler
                # may have left it on the $0C00 page. Nothing else re-asserts it
                # on the way into a char scene, which would then read its matrix
                # from the wrong offset. Safe only after uninstall — before it,
                # the next field's IRQ would put the page value straight back.
                api.write_memory(f"{VIC.D018_MEMORY:04X}", "14")
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
            # Blending only swaps the candidate set: a blend entry is the pair
            # (a, b) and a solid is (c, c), so every index below is just an
            # entry, and picking, dithering and bit-packing are shared verbatim.
            # Only the final nibble split cares which kind an entry is.
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
                # one of them more or less at random and the widened palette
                # then measures WORSE than the 16 solids. The fit is what makes
                # the second screen page pay for itself.
                sample_fg, is_fg = self._errmin_fg(dist, bg)
            else:
                sample_fg = quantized[4::8, 4::8]  # one sample per 8×8 cell
                is_fg = quantized != bg
            if self._dither_method in ("floyd-steinberg", "atkinson"):
                # Re-dither each 8×8 cell's own pixels against its 2-color set
                # {bg, cell fg}, replacing the nearest-of-two assignment above.
                # Only the per-pixel fill dithers — the cell's two colours are
                # already fixed by this point, whichever way they were picked.
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

        # Split each entry into the palette index its field shows. The two pages
        # share the bitmap, so a cell whose entry is a solid writes the same byte
        # to both and simply doesn't alternate.
        fg_a, fg_b = table.field_pages(sample_fg)
        bg_a, bg_b = (int(v) for v in table.pairs[bg])
        screen_ram = _pack_screen(fg_a, bg_a)
        screen_b = _pack_screen(fg_b, bg_b)
        flicker: FlickerComposeBuffers = {
            "bitmap": bitmap_ram,
            "screen": screen_ram,
            "screen_b": screen_b,
            # $D020 is a single register the field IRQ doesn't manage, so the
            # border can't blend — it takes the field-A component. Widening the
            # handler to alternate it too would buy a blended frame around the
            # picture and cost bytes in the one routine that must fit in vblank.
            "bg": bg_a,
            "text": HiresTextSurface(bitmap_ram, screen_ram),
        }
        return flicker

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
        if self._blend_table is not None:
            # Both pages plus the shared bitmap into the off-screen bank, then
            # arm. The field alternation is already free-running against the
            # displayed bank, so this stages a whole new pair set and the next
            # phase-0 vblank brings it up in one piece.
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
            # Drop into the off-screen bank, then cue a vblank swap.
            target_bank = 1 - self._displayed_bank
            push_bitmap_via_reu(api, bitmap_bytes, screen_bytes, target_bank)
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
