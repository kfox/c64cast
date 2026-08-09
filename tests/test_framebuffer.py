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
        # A supplied 2KB char-ROM dump is used verbatim instead of the builtin.
        from c64cast.video.framebuffer import Framebuffer

        custom = bytes(range(256)) * 8  # 2048 bytes, distinctive
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
        from c64cast.hw import char_rom
        from c64cast.video.framebuffer import Framebuffer

        char_rom.invalidate_cache()
        self.addCleanup(char_rom.invalidate_cache)
        with self.assertLogs("c64cast.video.framebuffer", level="WARNING"):
            fb = Framebuffer(charset_path="/nonexistent/charset.bin")
        self.assertEqual(len(fb.charset), 2048)


if __name__ == "__main__":
    unittest.main()
