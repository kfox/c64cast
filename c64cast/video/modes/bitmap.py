"""Bitmap-mode mid-base: engage_bitmap_mode (the clear-then-reveal VIC
bring-up choreography) + BitmapDisplayMode."""

from __future__ import annotations

import logging
from typing import cast

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import (
    CIA2,
    D018_HIRES_PAGE_A,
    D018_HIRES_PAGE_B,
    SCREEN,
    VIC_BANK_0,
    VIC_BANK_2,
    RegionID,
)
from c64cast.video.flicker import (
    FLASH_CRITERION_LUMA_DELTA,
    WARN_LUMA_DELTA,
    BlendTable,
)
from c64cast.video.modes_irq import (
    BANK_SWAP_IRQ_HANDLER_ADDR,
    DD00_BANK_0,
    DD00_BANK_2,
    FLICKER_SWAP_IRQ_HANDLER,
    FLICKER_TRACKER_LEN,
    FRAME_TRACKER_ADDR,
    HOSTDMA_SWAP_IRQ_HANDLER,
    HOSTDMA_TRACKER_LEN,
    REU_VIDEO_BITMAP_LEN,
    REU_VIDEO_BITMAP_SCREEN_LEN,
    install_bank_swap_irq,
)
from c64cast.video.palette import build_fade_lut

from .base import BitmapComposeBuffers, DisplayMode, FlickerComposeBuffers, fade_nibbles

log = logging.getLogger(__name__)


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
    garbage and not a color ghost of the prior scene. A zeroed hires bitmap
    makes every pixel select its cell's BACKGROUND color, and in HIRES that
    background is the LOW nibble of the cell's screen-RAM byte (NOT ``$D021``) —
    so leaving stale ``$0400`` (e.g. the previous scene's PETSCII codes / color
    grid) paints a 40×25 color ghost on engage. Zeroing ``$0400`` too forces
    every cell's background to black. (In mhires/MCBM ``%00`` reads ``$D021``,
    set here via ``bg0``, so the screen clear is belt-and-braces there.) ``$D011``
    is written LAST so the configured sub-bank pointers + colors are already in
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
      register). ``None`` leaves that register untouched. Hires and MultiHires
      both pass ``0x00`` for both on every path (including REU-staged) so the
      engage is unconditionally black — per-frame code (``push()`` for hires,
      the swap-tracker IRQ for REU-staged mhires) takes over from the first
      real frame onward; this call only covers the window before that.
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
    # 3. Configure the sub-bank pointers ($D018/$D016) + background colors.
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
    # an REU. See the handler block in modes_irq.py. Subclasses own
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
                DD00_BANK_0,
            )
        return (
            1,
            VIC_BANK_2.BITMAP,
            VIC_BANK_2.SCREEN,
            RegionID.BITMAP_BANK2,
            RegionID.SCREEN_BANK2,
            DD00_BANK_2,
        )

    def _arm_hostdma_swap(self, api: C64Backend, bg0: int, dd00_value: int) -> None:
        """Write the 3-byte swap tracker [bg0, bank, ready=1] as one ACK-gated
        segment. By the time it returns the off-screen bank is fully staged, so
        the next vblank IRQ flips $DD00 to a complete frame (and sets $D021 from
        bg0 atomically with the swap — for hires $D021 is unused, harmless)."""
        tracker = bytes([bg0 & 0x0F, dd00_value & 0xFF, 0x01])
        api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)

    # --- Flicker blend ([color].flicker_tolerance) ------------------------------
    # The double-buffer above, plus a second screen page per bank. Both pages
    # go into the off-screen bank each frame over the same host-DMA path; the
    # $C500 handler alternates $D018 between them every field so the eye fuses
    # each cell's color pair. See modes_irq.FLICKER_SWAP_IRQ_HANDLER and
    # video/flicker.py.
    def _flicker_swap_target(self) -> tuple[int, int, int, int, int, int, int, int]:
        """Resolve the current off-screen bank to (target_bank, bitmap_addr,
        page_a_addr, page_b_addr, bitmap_region, page_a_region, page_b_region,
        dd00_value). The caller toggles self._displayed_bank after the writes."""
        if 1 - self._displayed_bank == 0:
            return (
                0,
                VIC_BANK_0.BITMAP,
                VIC_BANK_0.SCREEN,
                VIC_BANK_0.SCREEN_ALT,
                RegionID.BITMAP,
                RegionID.SCREEN,
                RegionID.SCREEN_ALT,
                DD00_BANK_0,
            )
        return (
            1,
            VIC_BANK_2.BITMAP,
            VIC_BANK_2.SCREEN,
            VIC_BANK_2.SCREEN_ALT,
            RegionID.BITMAP_BANK2,
            RegionID.SCREEN_BANK2,
            RegionID.SCREEN_ALT_BANK2,
            DD00_BANK_2,
        )

    def _flicker_tracker(self, bg0: int, dd00_value: int, *, ready: bool) -> bytes:
        """The 6-byte flicker tracker. `phase` is seeded 0 and thereafter owned
        by the handler — re-arming must not reset it, or the field alternation
        would restart from page A on every staged frame and stall the blend."""
        return bytes(
            [
                bg0 & 0x0F,
                dd00_value & 0xFF,
                0x01 if ready else 0x00,
                0x00,
                D018_HIRES_PAGE_A,
                D018_HIRES_PAGE_B,
            ]
        )

    def _arm_flicker_swap(self, api: C64Backend, bg0: int, dd00_value: int) -> None:
        """Arm a staged flicker frame. Only the first three tracker bytes are
        written: the handler owns $C703 (phase) and the page pair at $C704 is
        constant for the scene, so re-sending them would fight the alternation."""
        tracker = self._flicker_tracker(bg0, dd00_value, ready=True)[:3]
        api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)

    def _setup_flicker_doublebuffer(self, api: C64Backend) -> None:
        """Zero both banks' bitmap + both screen pages, pin bank 0, and install
        the flicker swap IRQ with its page pair pre-seeded (see
        install_bank_swap_irq's tracker_init)."""
        zeros_bitmap = bytes(REU_VIDEO_BITMAP_LEN)
        zeros_screen = bytes(REU_VIDEO_BITMAP_SCREEN_LEN)
        for addr in (
            VIC_BANK_0.BITMAP,
            VIC_BANK_2.BITMAP,
        ):
            api.write_memory_file(f"{addr:04X}", zeros_bitmap)
        for addr in (
            VIC_BANK_0.SCREEN,
            VIC_BANK_0.SCREEN_ALT,
            VIC_BANK_2.SCREEN,
            VIC_BANK_2.SCREEN_ALT,
        ):
            api.write_memory_file(f"{addr:04X}", zeros_screen)
        api.write_memory(f"{CIA2.PORT_A:04X}", f"{DD00_BANK_0:02X}")
        self._displayed_bank = 0
        install_bank_swap_irq(
            api,
            FLICKER_SWAP_IRQ_HANDLER,
            FLICKER_TRACKER_LEN,
            audio_pump_active=False,
            tracker_init=self._flicker_tracker(0, DD00_BANK_0, ready=False),
        )

    def _log_flicker_arming(self, table: BlendTable, *, blendable: str) -> None:
        """The arming line for a blended scene, plus the warnings that go with
        settings a reader should not reach by accident.

        `blendable` names which of the mode's color slots can actually hold a
        pair, which differs by mode and is the thing a reader most needs: hires
        alternates both nibbles of every screen byte, mhires only two of its
        four slots."""
        # Logged against the concrete mode's own module so `hires:`/`mhires:`
        # lines keep arriving on the logger a reader filters for, rather than on
        # this shared base's.
        log = logging.getLogger(type(self).__module__)
        log.info(
            "%s: flicker blend armed — %d blend pairs from %s, "
            "ΔY <= %.3f (%d effective colors), blendable slots: %s, "
            "pages $%04X/$%04X, IRQ @ $%04X",
            self.name,
            table.blend_count,
            "an explicit flicker_score_pairs list"
            if table.scoring
            else f"tolerance {table.tolerance!r}",
            table.max_luma_delta,
            table.size,
            blendable,
            VIC_BANK_0.SCREEN,
            VIC_BANK_0.SCREEN_ALT,
            BANK_SWAP_IRQ_HANDLER_ADDR,
        )
        if table.max_luma_delta > FLASH_CRITERION_LUMA_DELTA:
            log.warning(
                "%s: flicker_max_luma_delta = %.3f is past %.2f, where a blended "
                "area's luminance modulation approaches the 20%%-of-peak-white flash "
                "criterion the photosensitive-seizure guidance is written around (it "
                "alternates at 25 Hz PAL / 30 Hz NTSC, inside the risk band). Allowed "
                "rather than refused, because a pair you have looked at and accepted "
                "outranks this number — but don't raise it for a stream anyone else "
                "will watch without knowing that.",
                self.name,
                table.max_luma_delta,
                FLASH_CRITERION_LUMA_DELTA,
            )
        elif table.max_luma_delta > WARN_LUMA_DELTA:
            log.warning(
                "%s: flicker_max_luma_delta = %.3f is above %.2f, where pairs "
                "start to read as luminance flicker rather than color.",
                self.name,
                table.max_luma_delta,
                WARN_LUMA_DELTA,
            )
        if table.scoring:
            log.warning(
                "%s: flicker_score_pairs is set — the blend set is this explicit "
                "list, not the scored one, so neither flicker_tolerance nor "
                "flicker_max_luma_delta is filtering it. This is the scoring path; "
                "a pair reachable only this way was excluded on evidence, and some "
                "of them flicker hard. Don't leave it set for anything but scoring.",
                self.name,
            )
        elif table.tolerance == "visible":
            log.warning(
                "%s: flicker_tolerance = %r admits pairs scored as visibly "
                "flickering rather than fusing. A blended area alternates at the "
                "video field rate (25 Hz PAL / 30 Hz NTSC), inside the recognized "
                "photosensitive-seizure band — don't use this for a stream anyone "
                "else will watch.",
                self.name,
                table.tolerance,
            )
        # Which pairs specifically — the thing you want when deciding whether
        # flicker_max_luma_delta is set where you meant it.
        log.debug("%s: flicker pairs = %s", self.name, ", ".join(table.describe()))

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
        api.write_memory(f"{CIA2.PORT_A:04X}", f"{DD00_BANK_0:02X}")
        self._displayed_bank = 0
        install_bank_swap_irq(
            api, HOSTDMA_SWAP_IRQ_HANDLER, HOSTDMA_TRACKER_LEN, audio_pump_active=False
        )

    def apply_fade(self, buffers: BitmapComposeBuffers) -> BitmapComposeBuffers:
        """Hires per-cell colors are packed into the screen byte (hi nibble =
        fg, lo nibble = bg) plus the global bg/border scalar; the bitmap is a
        per-pixel fg/bg selector, so it's left untouched. Dim both nibbles and
        the bg. MultiHires overrides to also dim its per-cell color RAM (c3)."""
        out: BitmapComposeBuffers = dict(buffers)  # type: ignore[assignment]
        lut = build_fade_lut(self._fade_lut_alpha)
        out["screen"] = fade_nibbles(buffers["screen"], lut)
        out["bg"] = int(lut[buffers["bg"]])
        if "screen_b" in buffers:
            # Under flicker blending the second page holds the same kind of
            # packed nibble pair. Dimming only page A would turn every blended
            # cell into an alternation between a dimmed color and an undimmed
            # one — a fade that introduces flicker instead of removing it.
            flicker = cast(FlickerComposeBuffers, out)
            flicker["screen_b"] = fade_nibbles(
                cast(FlickerComposeBuffers, buffers)["screen_b"], lut
            )
        return out
