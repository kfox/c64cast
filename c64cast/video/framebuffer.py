"""Software VIC-II framebuffer for local preview + recording.

Maintains a shadow copy of relevant C64 memory ranges (screen RAM, color
RAM, bitmap area, VIC registers) by subscribing to ``Ultimate64API``
write events, then on demand renders the current state to a 320×200 RGB
image you can display in a window or pipe to a video file.

Supports the modes c64cast actually renders to:
  * Standard text mode (PETSCII char + color)
  * Multicolor text mode (MCM)
  * Hires bitmap
  * Multicolor bitmap (mhires)

Text modes need a 2 KB character set. By default we use a hand-rolled 8×8
ASCII-only font shipped with the package — it's not the real C64 ROM but
covers letters/digits/punctuation enough for the visible scene previews
to be legible. To get pixel-accurate PETSCII glyphs, pass a 2 KB char-ROM
dump as `charset_path` (read from a real C64 / VICE / U64).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from c64cast.app import paths
from c64cast.hw.c64 import SCREEN, VECTORS, VIC

from .flicker import fuse_indices
from .modes_irq import (
    BANK_SWAP_IRQ_HANDLER_ADDR,
    FLICKER_SWAP_IRQ_HANDLER,
    FLICKER_TRACKER_OFF_D018,
    FRAME_TRACKER_ADDR,
)
from .palette import C64_PALETTE_BGR

log = logging.getLogger(__name__)


def _builtin_charset() -> bytes:
    """Render an 8×8 ASCII-only charset using cv2.putText for the visible
    glyphs (screen codes 0x20-0x5F → ASCII space..underscore). Returns a
    2048-byte block in C64 charset layout (each char = 8 bytes, one row
    per byte, MSB = leftmost pixel)."""
    import cv2

    cs = bytearray(2048)
    for code in range(0x20, 0x60):
        ch = chr(code)
        img = np.zeros((8, 8), dtype=np.uint8)
        cv2.putText(img, ch, (0, 7), cv2.FONT_HERSHEY_PLAIN, 0.5, 255, 1, cv2.LINE_8)
        # Threshold to 1-bit.
        bits = (img > 128).astype(np.uint8)
        # Pack each row's 8 bits into a byte (MSB = col 0).
        for row in range(8):
            byte = 0
            for col in range(8):
                byte |= int(bits[row, col]) << (7 - col)
            cs[code * 8 + row] = byte
    # Map upper-case to screen codes 0x01-0x1A (where C64 PETSCII puts them).
    # The C64 default charset has @ at screen code 0, A at 1, ..., Z at 26.
    for code in range(0x01, 0x1B):
        ascii_code = 0x40 + code  # A..Z
        ch = chr(ascii_code)
        img = np.zeros((8, 8), dtype=np.uint8)
        cv2.putText(img, ch, (0, 7), cv2.FONT_HERSHEY_PLAIN, 0.5, 255, 1, cv2.LINE_8)
        bits = (img > 128).astype(np.uint8)
        for row in range(8):
            byte = 0
            for col in range(8):
                byte |= int(bits[row, col]) << (7 - col)
            cs[code * 8 + row] = byte
    cs[0x60 * 8 : 0x60 * 8 + 8] = bytes([0xFF] * 8)
    # Screen codes $80-$FF are the reverse-video twins of $00-$7F, so mirror
    # the real ROM and make them the bitwise complement. Without this the whole
    # upper half is blank, and the codes c64cast leans on hardest are up there:
    # SC_FULL_BLOCK ($A0) is what big_text paints its glyph pixels with and
    # what the `blocks` PETSCII style fills every cell with, and the shading
    # ramp in petscii_styles is mostly $E0-$F2. They all rendered as nothing.
    for i in range(1024):
        cs[1024 + i] = ~cs[i] & 0xFF
    return bytes(cs)


class Framebuffer:
    """Shadow + renderer. Register with `api.add_write_listener(fb.on_write)`."""

    def __init__(self, charset_path: str | None = None):
        # 64K shadow. Plenty cheap.
        self.ram = bytearray(0x10000)
        # VIC mode defaults (post-reset).
        self.ram[VIC.D011_CONTROL_1] = 0x1B
        self.ram[VIC.D016_CONTROL_2] = 0x08
        self.ram[VIC.D018_MEMORY] = 0x14
        self.ram[VIC.D020_BORDER] = 14  # light blue
        self.ram[VIC.D021_BG0] = 6  # blue
        # Color RAM defaults to light blue (matches boot).
        for i in range(SCREEN.N_CELLS):
            self.ram[SCREEN.COLOR_RAM + i] = 14
        self._lock = threading.Lock()
        # Resolve through char_rom so the preview shows the same glyphs the C64
        # does (a dumped ROM under the data dir, or `charset_path` when set).
        # A configured-but-unreadable path degrades to the builtin font with a
        # warning: this window is a mirror, and killing the whole run over a
        # mistyped preview path would be a spectacularly bad trade.
        from c64cast.hw.char_rom import load_glyphs

        if charset_path and not Path(paths.expand_user(charset_path)).is_file():
            log.warning(
                "[preview] charset_path %s does not exist — falling back to the "
                "built-in font. Leave it unset to use the character ROM c64cast "
                "dumps off your C64.",
                charset_path,
            )
        self.charset = load_glyphs(charset_path)

    def on_write(self, address: int, data: bytes):
        """Shadow a memory write. Safe to call from the API's writer thread."""
        if not data:
            return
        end = address + len(data)
        if end > 0x10000:
            data = data[: 0x10000 - address]
            end = 0x10000
        with self._lock:
            self.ram[address:end] = data

    def render(self) -> np.ndarray:
        """Produce a (200, 320, 3) uint8 BGR image of the current screen."""
        with self._lock:
            ram = bytes(self.ram)  # snapshot
        d011 = ram[VIC.D011_CONTROL_1]
        d016 = ram[VIC.D016_CONTROL_2]
        is_bitmap = bool(d011 & 0x20)
        is_multicolor = bool(d016 & 0x10)
        if is_bitmap and not is_multicolor:
            return self._render_hires(ram)
        if is_bitmap and is_multicolor:
            return self._render_mhires(ram)
        if is_multicolor:
            return self._render_mcm(ram)
        return self._render_text(ram)

    # ---- bitmap modes -------------------------------------------------------

    def _flicker_page_b(self, ram: bytes) -> int | None:
        """Address of the second screen page when flicker blending is live, else
        None.

        Detected purely from the outbound write stream — the IRQ vector points
        at the swap handler AND the handler bytes at that address are the flicker
        flavor — so the mirror keeps reconstructing rather than being told
        anything out of band. Checking the vector matters: teardown unhooks
        $0314 but leaves both the handler and its tracker in RAM, so the page
        bytes alone would keep reporting a blend into the next scene."""
        vector = ram[VECTORS.IRQ] | (ram[VECTORS.IRQ + 1] << 8)
        if vector != BANK_SWAP_IRQ_HANDLER_ADDR:
            return None
        installed = ram[
            BANK_SWAP_IRQ_HANDLER_ADDR : BANK_SWAP_IRQ_HANDLER_ADDR + len(FLICKER_SWAP_IRQ_HANDLER)
        ]
        if installed != FLICKER_SWAP_IRQ_HANDLER:
            return None
        page_b = ram[FRAME_TRACKER_ADDR + FLICKER_TRACKER_OFF_D018 + 1]
        # $D018's matrix nibble is bank-relative; the mirror models bank 0 only
        # (see caveats.md), which is where a flicker scene's first frame lands.
        return ((page_b >> 4) & 0x0F) * 0x400

    def _render_hires(self, ram: bytes) -> np.ndarray:
        """320×200 hires bitmap. Each 8×8 cell has FG (high nibble of screen
        RAM byte) and BG (low nibble)."""
        # Cell layout: 25 cell rows × 40 cells × 8 bytes/cell.
        bitmap = np.frombuffer(
            ram[SCREEN.BITMAP : SCREEN.BITMAP + SCREEN.BITMAP_BYTES],
            dtype=np.uint8,
        ).reshape(25, 40, 8)
        screen = np.frombuffer(
            ram[SCREEN.RAM : SCREEN.RAM + SCREEN.N_CELLS],
            dtype=np.uint8,
        ).reshape(25, 40)
        fg = (screen >> 4) & 0x0F
        bg = screen & 0x0F
        page_b = self._flicker_page_b(ram)
        if page_b is None:
            fg_palette = C64_PALETTE_BGR[fg]
            bg_palette = C64_PALETTE_BGR[bg]
        else:
            # Fuse the two fields' cell colors once, then render a single pass:
            # equivalent to alternating them, and it is the frame the eye
            # integrates. Both pages share the bitmap, so only the colors differ.
            screen_b = np.frombuffer(ram[page_b : page_b + SCREEN.N_CELLS], dtype=np.uint8).reshape(
                25, 40
            )
            fg_palette = fuse_indices(fg, (screen_b >> 4) & 0x0F)
            bg_palette = fuse_indices(bg, screen_b & 0x0F)
        img = np.empty((200, 320, 3), dtype=np.uint8)
        for cy in range(25):
            for cx in range(40):
                cell = bitmap[cy, cx]
                fg_col = fg_palette[cy, cx]
                bg_col = bg_palette[cy, cx]
                for row in range(8):
                    bits = cell[row]
                    for col in range(8):
                        on = (bits >> (7 - col)) & 1
                        img[cy * 8 + row, cx * 8 + col] = fg_col if on else bg_col
        return img

    def _render_mhires(self, ram: bytes) -> np.ndarray:
        """160×200 multicolor bitmap. 4 colors per cell: 00=$D021, 01=high
        nibble of screen RAM, 10=low nibble of screen RAM, 11=color RAM."""
        bitmap = np.frombuffer(
            ram[SCREEN.BITMAP : SCREEN.BITMAP + SCREEN.BITMAP_BYTES],
            dtype=np.uint8,
        ).reshape(25, 40, 8)
        screen = np.frombuffer(
            ram[SCREEN.RAM : SCREEN.RAM + SCREEN.N_CELLS],
            dtype=np.uint8,
        ).reshape(25, 40)
        color_ram = np.frombuffer(
            ram[SCREEN.COLOR_RAM : SCREEN.COLOR_RAM + SCREEN.N_CELLS],
            dtype=np.uint8,
        ).reshape(25, 40)
        bg0 = ram[VIC.D021_BG0] & 0x0F
        img = np.empty((200, 320, 3), dtype=np.uint8)
        for cy in range(25):
            for cx in range(40):
                cell = bitmap[cy, cx]
                colors = [
                    C64_PALETTE_BGR[bg0],
                    C64_PALETTE_BGR[(screen[cy, cx] >> 4) & 0x0F],
                    C64_PALETTE_BGR[screen[cy, cx] & 0x0F],
                    C64_PALETTE_BGR[color_ram[cy, cx] & 0x0F],
                ]
                for row in range(8):
                    b = cell[row]
                    for col in range(4):
                        pair = (b >> (6 - col * 2)) & 0x03
                        c = colors[pair]
                        x = cx * 8 + col * 2
                        img[cy * 8 + row, x] = c
                        img[cy * 8 + row, x + 1] = c
        return img

    # ---- char modes ---------------------------------------------------------

    def _render_text(self, ram: bytes) -> np.ndarray:
        """Standard 40×25 char mode. Each cell: screen code, FG from color
        RAM, BG from $D021."""
        screen = np.frombuffer(
            ram[SCREEN.RAM : SCREEN.RAM + SCREEN.N_CELLS],
            dtype=np.uint8,
        ).reshape(25, 40)
        color_ram = np.frombuffer(
            ram[SCREEN.COLOR_RAM : SCREEN.COLOR_RAM + SCREEN.N_CELLS],
            dtype=np.uint8,
        ).reshape(25, 40)
        bg0 = ram[VIC.D021_BG0] & 0x0F
        bg_col = C64_PALETTE_BGR[bg0]
        img = np.empty((200, 320, 3), dtype=np.uint8)
        img[:, :] = bg_col
        cs = self.charset
        for cy in range(25):
            for cx in range(40):
                code = int(screen[cy, cx])
                fg_col = C64_PALETTE_BGR[color_ram[cy, cx] & 0x0F]
                glyph_off = code * 8
                for row in range(8):
                    bits = cs[glyph_off + row]
                    for col in range(8):
                        if (bits >> (7 - col)) & 1:
                            img[cy * 8 + row, cx * 8 + col] = fg_col
        return img

    def _render_mcm(self, ram: bytes) -> np.ndarray:
        """Multicolor text. If color RAM bit 3 = 0, behave as standard text
        (FG = color RAM low 3 bits). If bit 3 = 1, multicolor: 4 colors per
        cell — 00=$D021, 01=$D022, 10=$D023, 11=color RAM low 3 bits."""
        screen = np.frombuffer(
            ram[SCREEN.RAM : SCREEN.RAM + SCREEN.N_CELLS],
            dtype=np.uint8,
        ).reshape(25, 40)
        color_ram = np.frombuffer(
            ram[SCREEN.COLOR_RAM : SCREEN.COLOR_RAM + SCREEN.N_CELLS],
            dtype=np.uint8,
        ).reshape(25, 40)
        bg0 = ram[VIC.D021_BG0] & 0x0F
        bg1 = ram[VIC.D022_BG1] & 0x0F
        bg2 = ram[VIC.D023_BG2] & 0x0F
        img = np.empty((200, 320, 3), dtype=np.uint8)
        img[:, :] = C64_PALETTE_BGR[bg0]
        cs = self.charset
        for cy in range(25):
            for cx in range(40):
                code = int(screen[cy, cx])
                colbyte = color_ram[cy, cx]
                glyph_off = code * 8
                if not (colbyte & 0x08):
                    # Mono: same as standard text.
                    fg_col = C64_PALETTE_BGR[colbyte & 0x07]
                    for row in range(8):
                        bits = cs[glyph_off + row]
                        for col in range(8):
                            if (bits >> (7 - col)) & 1:
                                img[cy * 8 + row, cx * 8 + col] = fg_col
                else:
                    colors = [
                        C64_PALETTE_BGR[bg0],
                        C64_PALETTE_BGR[bg1],
                        C64_PALETTE_BGR[bg2],
                        C64_PALETTE_BGR[colbyte & 0x07],
                    ]
                    for row in range(8):
                        b = cs[glyph_off + row]
                        for col in range(4):
                            pair = (b >> (6 - col * 2)) & 0x03
                            c = colors[pair]
                            x = cx * 8 + col * 2
                            img[cy * 8 + row, x] = c
                            img[cy * 8 + row, x + 1] = c
        return img
