"""Standard PETSCII char mode with no video input (overlay canvas)."""

from __future__ import annotations

import numpy as np

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import SCREEN, RegionID
from c64cast.scenes.text_surface import CharTextSurface
from c64cast.video.modes_irq import push_screen_via_reu
from c64cast.video.palette import C64_COLORS, color_display_name, resolve_color

from .base import ComposeBuffers
from .char import CharDisplayMode, clear_char_screen


class BlankDisplayMode(CharDisplayMode):
    """Standard PETSCII char mode with no video input.

    Paints the whole screen as SC_SPACE, leaving overlays to provide all
    the visible content. Useful as a clean canvas for big-text title cards
    where a webcam feed would just compete with the text. Configurable
    border + background palette indices.
    """

    name = "blank"
    is_petscii_compatible = True

    # introspect.live_targets tags these `vocabulary="c64color"`, which is what
    # makes the console render swatches instead of a <select>.
    LIVE_CHOICES = {
        "border": tuple(C64_COLORS.keys()),
        "background": tuple(C64_COLORS.keys()),
    }

    def __init__(self, border: int = 0, background: int = 0, *, use_reu_staged: bool = False):
        self.border = int(border) & 0x0F
        self.background = int(background) & 0x0F
        self.use_reu_staged = use_reu_staged

    def setup(self, api):
        super().setup(api)
        # Clear-then-reveal: blank the screen before the register pokes and
        # flip $D011 last, so a scene switch never shows stale glyphs.
        clear_char_screen(api)
        api.write_memory("d018", "14")
        api.write_memory("d016", "08")
        api.write_regs("d020", self.border, self.background)
        api.write_memory("d011", "1b")

    def set_border(self, api: C64Backend, value: str) -> str:
        """Live-tune ``mode.border``. Unlike ``background`` (re-read into
        color RAM every frame by ``compose()``/``push()``), $D020 is only
        written at ``setup()``, so a live change has to poke it directly."""
        self.border = resolve_color(value)
        api.write_regs("d020", self.border, self.background)
        return f"border {color_display_name(self.border)}"

    def set_background(self, api: C64Backend, value: str) -> str:
        """Live-tune ``mode.background``. Pokes $D021 for the instant border-
        style feedback; the color-RAM fill (what actually shows through the
        blank screen) follows on the next frame's ``compose()``."""
        self.background = resolve_color(value)
        api.write_regs("d020", self.border, self.background)
        return f"background {color_display_name(self.background)}"

    def compose(self, frame=None) -> ComposeBuffers:
        screen = np.full(1000, 0x20, dtype=np.uint8)  # SC_SPACE
        # Color RAM is every cell's FG; at `background` the spaces are invisible
        # until an overlay paints over them.
        color = np.full(1000, self.background, dtype=np.uint8)
        return {"screen": screen, "color": color, "text": CharTextSurface(screen, color)}

    def push(self, api: C64Backend, buffers: ComposeBuffers) -> None:
        screen_bytes = buffers["screen"].tobytes()
        if self.use_reu_staged:
            push_screen_via_reu(api, screen_bytes, SCREEN.RAM)
        else:
            api.write_region(SCREEN.RAM, screen_bytes, region_id=RegionID.SCREEN)
        api.write_region(SCREEN.COLOR_RAM, buffers["color"].tobytes(), region_id=RegionID.COLOR)
