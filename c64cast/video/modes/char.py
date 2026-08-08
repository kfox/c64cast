"""Character-mode mid-base: CharDisplayMode + the shared char-screen clear."""

from __future__ import annotations

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import SCREEN
from c64cast.video.palette import build_fade_lut

from .base import ComposeBuffers, DisplayMode


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
        lut = build_fade_lut(self._fade_lut_alpha)
        out["color"] = lut[buffers["color"]]
        return out


def clear_char_screen(api: C64Backend, screen_code: int = 0x20) -> None:
    """Zero screen RAM ($0400) to `screen_code` + color RAM ($D800) to black.

    The char-mode sibling of ``engage_bitmap_mode``'s clear step: called
    BEFORE a char mode's ``$D011``/``$D020``/``$D021`` bring-up pokes so a
    scene switch (especially away from a bitmap scene, whose screen RAM holds
    packed nibble colors rather than glyph codes) never reveals stale
    ``$0400``/``$D800`` content as garbled characters. ``screen_code`` defaults
    to PETSCII space (invisible regardless of color); MCM passes 0x00, whose
    2-bit sub-cell code selects bg slot 0 for every pixel."""
    api.write_memory_file(f"{SCREEN.RAM:04X}", bytes([screen_code]) * SCREEN.N_CELLS)
    api.write_memory_file(f"{SCREEN.COLOR_RAM:04X}", bytes(SCREEN.N_CELLS))
