"""Standard PETSCII char mode with no video input (overlay canvas)."""

from __future__ import annotations

import numpy as np

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import SCREEN, RegionID
from c64cast.text_surface import CharTextSurface
from c64cast.video.modes_irq import push_screen_via_reu

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
        # Clear-then-reveal (see PETSCIIDisplayMode.setup / engage_bitmap_mode):
        # blank the screen before the register pokes, flip $D011 last.
        clear_char_screen(api)
        api.write_memory("d018", "14")
        api.write_memory("d016", "08")
        api.write_regs("d020", self.border, self.background)
        api.write_memory("d011", "1b")

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
            push_screen_via_reu(api, screen_bytes, SCREEN.RAM)
        else:
            api.write_region(SCREEN.RAM, screen_bytes, region_id=RegionID.SCREEN)
        api.write_region(SCREEN.COLOR_RAM, buffers["color"].tobytes(), region_id=RegionID.COLOR)
