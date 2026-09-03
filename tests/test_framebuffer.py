"""Tests for c64cast.video.framebuffer — the software VIC-II framebuffer
behind preview + recording."""

from __future__ import annotations

import os
import tempfile
import unittest


class FramebufferTest(unittest.TestCase):
    def test_shadows_writes(self):
        from c64cast.video.framebuffer import Framebuffer

        fb = Framebuffer()
        fb.on_write(0x0400, b"\x01\x02\x03\x04")
        self.assertEqual(fb.ram[0x0400], 0x01)
        self.assertEqual(fb.ram[0x0403], 0x04)

    def test_render_hires_runs_and_returns_image(self):
        from c64cast.video.framebuffer import Framebuffer

        fb = Framebuffer()
        # Set hires mode: $D011 bit 5 = 1.
        fb.on_write(0xD011, b"\x3b")
        # Fill bitmap with alternating bytes.
        fb.on_write(0x2000, b"\xaa" * 8000)
        # Set screen RAM colors: FG=white(1), BG=black(0).
        fb.on_write(0x0400, b"\x10" * 1000)
        img = fb.render()
        self.assertEqual(img.shape, (200, 320, 3))
        self.assertEqual(img.dtype.name, "uint8")

    def test_render_follows_hostdma_double_buffer_to_bank_2(self):
        """The host-DMA double-buffer's $DD00 swap runs inside the C64-side
        raster IRQ, so the host never writes it and the shadow's own $DD00
        byte never moves. render() has to follow the tracker's own
        pending-bank byte (see Framebuffer._vic_bank_base) instead of always
        reading bank 0, or it shows one push's worth of stale content on
        every frame the real swap has moved to bank 2."""
        from c64cast.hw.c64 import VECTORS, VIC_BANK_0, VIC_BANK_2
        from c64cast.video.framebuffer import Framebuffer
        from c64cast.video.modes_irq import (
            BANK_SWAP_IRQ_HANDLER_ADDR,
            DD00_BANK_2,
            FRAME_TRACKER_ADDR,
            HOSTDMA_SWAP_IRQ_HANDLER,
            HOSTDMA_TRACKER_OFF_BANK,
        )

        fb = Framebuffer()
        fb.on_write(0xD011, b"\x3b")  # hires bitmap mode
        fb.on_write(
            VECTORS.IRQ,
            bytes([BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF, BANK_SWAP_IRQ_HANDLER_ADDR >> 8]),
        )
        fb.on_write(BANK_SWAP_IRQ_HANDLER_ADDR, HOSTDMA_SWAP_IRQ_HANDLER)
        # Stale bank-0 content from an earlier push — should NOT be shown.
        fb.on_write(VIC_BANK_0.BITMAP, b"\x00" * 8000)
        fb.on_write(VIC_BANK_0.SCREEN, b"\x00" * 1000)
        # The live frame: bank 2, white-on-black.
        fb.on_write(VIC_BANK_2.BITMAP, b"\xff" * 8000)
        fb.on_write(VIC_BANK_2.SCREEN, b"\x10" * 1000)
        fb.on_write(FRAME_TRACKER_ADDR + HOSTDMA_TRACKER_OFF_BANK, bytes([DD00_BANK_2]))
        self.assertEqual(fb.render()[100, 160].tolist(), [255, 255, 255])

    def test_on_write_clamps_past_top_of_ram(self):
        from c64cast.video.framebuffer import Framebuffer

        fb = Framebuffer()
        # Writing across the 64K boundary must truncate, not raise/overflow.
        fb.on_write(0xFFFE, b"\xaa\xbb\xcc\xdd")
        self.assertEqual(fb.ram[0xFFFE], 0xAA)
        self.assertEqual(fb.ram[0xFFFF], 0xBB)
        self.assertEqual(len(fb.ram), 0x10000)

    def test_on_write_empty_is_noop(self):
        from c64cast.video.framebuffer import Framebuffer

        fb = Framebuffer()
        before = bytes(fb.ram)
        fb.on_write(0x0400, b"")
        self.assertEqual(bytes(fb.ram), before)

    def test_render_text_solid_block_glyph(self):
        # Default post-reset mode is standard text. SC_FULL_BLOCK ($A0) is the
        # reverse-space glyph — solid in the real character ROM and in the
        # builtin fallback alike, so this pins render behavior rather than
        # whichever charset happens to resolve on the machine running the test.
        from c64cast.hw.c64 import SCREEN
        from c64cast.video.framebuffer import Framebuffer
        from c64cast.video.palette import C64_PALETTE_BGR

        fb = Framebuffer()
        fb.on_write(0xD021, b"\x00")  # bg0 = black
        fb.on_write(0x0400, bytes([SCREEN.SC_FULL_BLOCK]))  # cell (0,0)
        fb.on_write(0xD800, b"\x01")  # color RAM (0,0) = white
        img = fb.render()
        white = C64_PALETTE_BGR[1]
        self.assertTrue((img[0:8, 0:8] == white).all())

    def test_render_mcm_mono_cell(self):
        # MCM with color-RAM bit 3 clear behaves like standard text.
        from c64cast.hw.c64 import SCREEN
        from c64cast.video.framebuffer import Framebuffer
        from c64cast.video.palette import C64_PALETTE_BGR

        fb = Framebuffer()
        fb.on_write(0xD016, b"\x18")  # multicolor on
        fb.on_write(0xD021, b"\x00")
        fb.on_write(0x0400, bytes([SCREEN.SC_FULL_BLOCK]))  # solid block
        fb.on_write(0xD800, b"\x01")  # bit3 clear → mono FG = white
        img = fb.render()
        self.assertTrue((img[0:8, 0:8] == C64_PALETTE_BGR[1]).all())

    def test_render_mcm_multicolor_cell(self):
        # MCM with color-RAM bit 3 set: a 0xFF glyph is all '11' bit-pairs,
        # which selects color3 = color RAM low 3 bits.
        from c64cast.hw.c64 import SCREEN
        from c64cast.video.framebuffer import Framebuffer
        from c64cast.video.palette import C64_PALETTE_BGR

        fb = Framebuffer()
        fb.on_write(0xD016, b"\x18")
        fb.on_write(0x0400, bytes([SCREEN.SC_FULL_BLOCK]))  # all bit-pairs = 11
        fb.on_write(0xD800, b"\x0d")  # bit3 set + low3 = 5 (green)
        img = fb.render()
        # Multicolor halves horizontal resolution (doubled pixels); the cell
        # should be entirely color index 5.
        self.assertTrue((img[0:8, 0:8] == C64_PALETTE_BGR[5]).all())

    def test_render_mhires_cell(self):
        # Multicolor bitmap: bitmap byte 0xFF = all '11' pairs → color3 =
        # color RAM low nibble.
        from c64cast.video.framebuffer import Framebuffer
        from c64cast.video.palette import C64_PALETTE_BGR

        fb = Framebuffer()
        fb.on_write(0xD011, b"\x3b")  # bitmap mode
        fb.on_write(0xD016, b"\x18")  # multicolor
        fb.on_write(0x2000, b"\xff" * 8)  # cell (0,0) bitmap all-set
        fb.on_write(0xD800, b"\x05")  # color RAM (0,0) = green
        img = fb.render()
        self.assertTrue((img[0:8, 0:8] == C64_PALETTE_BGR[5]).all())

    def test_charset_path_loaded(self):
        # A supplied 2KB char-ROM dump is used verbatim instead of the
        # builtin — but only once char_rom.verify() accepts it as a real
        # charset (not just 2 KB of arbitrary bytes), so this one is built to
        # pass: reverse-video half complements the normal half, $20 blank,
        # $01 not.
        from c64cast.video.framebuffer import Framebuffer

        normal = bytearray()
        for code in range(0x80):
            normal += b"\x00" * 8 if code == 0x20 else bytes((code | 0x01,) * 8)
        custom = bytes(normal) + bytes((~b) & 0xFF for b in normal)
        with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as f:
            f.write(custom)
            path = f.name
        try:
            fb = Framebuffer(charset_path=path)
            self.assertEqual(fb.charset, custom)
        finally:
            os.unlink(path)

    def test_short_charset_falls_back_with_warning(self):
        # A truncated file is not usable as glyphs — zero-padding it would show
        # 1900 blank cells and look like a render bug. Fall back to the builtin
        # font instead, loudly.
        from c64cast.hw import char_rom
        from c64cast.video.framebuffer import Framebuffer, _builtin_charset

        char_rom.invalidate_cache()
        self.addCleanup(char_rom.invalidate_cache)
        with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as f:
            f.write(b"\xff" * 100)  # far short of 2KB
            path = f.name
        try:
            with self.assertLogs("c64cast.hw.char_rom", level="WARNING"):
                fb = Framebuffer(charset_path=path)
            self.assertEqual(fb.charset, _builtin_charset())
        finally:
            os.unlink(path)

    def test_missing_charset_path_warns_and_falls_back(self):
        # A configured-but-missing path used to raise FileNotFoundError out of
        # __init__ and kill the run; the preview is a mirror, it degrades.
        # The warning now comes from char_rom itself (the single resolver
        # every glyph consumer goes through), not a framebuffer-local check.
        from c64cast.hw import char_rom
        from c64cast.video.framebuffer import Framebuffer

        char_rom.invalidate_cache()
        self.addCleanup(char_rom.invalidate_cache)
        with self.assertLogs("c64cast.hw.char_rom", level="WARNING"):
            fb = Framebuffer(charset_path="/nonexistent/charset.bin")
        self.assertEqual(len(fb.charset), 2048)


if __name__ == "__main__":
    unittest.main()
