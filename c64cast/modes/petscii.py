"""40x25 PETSCII character mode: luma -> glyph, hue -> color RAM."""

from __future__ import annotations

import cv2

from ..backend import C64Backend
from ..c64 import SCREEN, RegionID
from ..modes_irq import push_screen_via_reu
from ..palette import COLOR_MATCH_MODES, apply_color_fit
from ..petscii_styles import (
    RANDOM_STYLE,
    STYLE_NAMES,
    make_style,
    pick_random_style_name,
    validate_style,
)
from ..text_surface import CharTextSurface
from .base import ComposeBuffers, resolve_color_shaping
from .char import CharDisplayMode, clear_char_screen


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
    # Live-tune surface: petscii applies the adaptive color fit and picks
    # nearest-palette per cell, so auto_fit_strength + color_match are live; it
    # has no dither / per-cell / palette_mode axis.
    LIVE_PARAMS = {"auto_fit_strength": (0.0, 1.0)}
    LIVE_CHOICES = {"color_match": COLOR_MATCH_MODES}

    def __init__(
        self,
        style: str = "default",
        *,
        use_reu_staged: bool = False,
        hue_corrections: list[dict] | None = None,
        hue_corrections_replace: bool = False,
        channel_boost: list[float] | None = None,
        perceptual: bool = False,
        auto_fit_strength: float = 1.0,
    ):
        validate_style(style)
        self._auto_fit_strength = float(min(1.0, max(0.0, auto_fit_strength)))
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
        self._channel_boost, self._hue_corrections = resolve_color_shaping(
            channel_boost, hue_corrections, hue_corrections_replace
        )
        # Opt-in REU-staged screen RAM push. See push_screen_via_reu and
        # the REU_VIDEO_SCREEN_BASE block in modes_irq.py for details + caveats.
        # Color RAM stays on the DMAWRITE delta path regardless.
        self.use_reu_staged = use_reu_staged

    def setup(self, api):
        super().setup(api)
        # Clear-then-reveal (mirrors engage_bitmap_mode): blank $0400/$D800
        # BEFORE the register pokes, and flip $D011 LAST, so a scene switch
        # never shows the previous scene's stale glyphs/colors — especially
        # coming from a bitmap scene, whose screen RAM holds nibble-packed
        # colors that would otherwise render as garbled characters here.
        clear_char_screen(api)
        api.write_memory("d018", "14")
        api.write_memory("d016", "08")
        # Each style declares its own border + background; push them now
        # so we don't carry the previous scene's choices into the first
        # frame. Bordr + bg are contiguous at $D020-$D021.
        api.write_regs("d020", self._style.border, self._style.background)
        api.write_memory("d011", "1b")

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

    def set_color_match(self, value: str) -> str:
        """Live-swap the nearest-palette metric ([color].color_match). petscii's
        styles read `_perceptual` at compose time, so no other state re-derives."""
        self._perceptual = value == "perceptual"
        return f"color_match={value}"

    def compose(self, frame) -> ComposeBuffers:
        assert self.frame_target_size is not None
        img = cv2.resize(frame, self.frame_target_size, interpolation=cv2.INTER_AREA)
        fit = self._fit_for_apply()
        if fit is not None:
            img = apply_color_fit(img, fit)
        screen, color = self._style.compose(
            img, self._channel_boost, self._hue_corrections, self._perceptual
        )
        return {"screen": screen, "color": color, "text": CharTextSurface(screen, color)}

    def push(self, api: C64Backend, buffers: ComposeBuffers) -> None:
        screen_bytes = buffers["screen"].tobytes()
        if self.use_reu_staged:
            push_screen_via_reu(api, screen_bytes, SCREEN.RAM)
        else:
            api.write_region(SCREEN.RAM, screen_bytes, region_id=RegionID.SCREEN)
        api.write_region(SCREEN.COLOR_RAM, buffers["color"].tobytes(), region_id=RegionID.COLOR)
