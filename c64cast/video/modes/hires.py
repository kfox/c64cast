"""320x200 hires bitmap mode + its style table."""

from __future__ import annotations

import logging

import cv2
import numpy as np

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import CIA2, VIC_BANK_0, VIC_BANK_2, RegionID
from c64cast.text_surface import HiresTextSurface
from c64cast.video.dither import DITHER_METHODS, error_diffuse_cells
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
from c64cast.video.palette import C64_PALETTE_BGR, COLOR_MATCH_MODES, quantize_flat_for

from .base import ORDERED_DITHER_OFFSET_FNS, BitmapComposeBuffers
from .bitmap import BitmapDisplayMode, engage_bitmap_mode

log = logging.getLogger(__name__)


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
    LIVE_CHOICES = {"dither_method": DITHER_METHODS, "color_match": COLOR_MATCH_MODES}

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
        edges styles). Hires carries no d²-space penalty to rescale."""
        self._perceptual = value == "perceptual"
        return f"color_match={value}"

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
        if self.use_reu_staged or self.double_buffer:
            uninstall_bank_swap_irq(api)
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
            offset_fn = ORDERED_DITHER_OFFSET_FNS.get(self._dither_method)
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
