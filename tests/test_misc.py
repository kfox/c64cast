"""Tests for songlengths + framebuffer."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

# ---------------------------------------------------------------------------
# SongLengths
# ---------------------------------------------------------------------------

class SongLengthsTest(unittest.TestCase):

    def test_parse_and_lookup(self):
        from c64cast.songlengths import LengthsDB, md5_of_sid
        # Build a minimal SID with a known data section.
        header = bytearray(124)
        header[0:4] = b"PSID"
        header[6:8] = (0x7C).to_bytes(2, "big")  # data_offset = 124
        header[14:16] = (3).to_bytes(2, "big")
        data_payload = b"\x12\x34" * 32
        sid_bytes = bytes(header) + data_payload
        expected_md5 = hashlib.md5(data_payload).hexdigest()

        with tempfile.NamedTemporaryFile("w", suffix=".md5",
                                          delete=False) as f:
            f.write("; comment\n")
            f.write(f"{expected_md5}=1:23 2:34 0:30.500\n")
            path = f.name
        try:
            db = LengthsDB.load(path)
        finally:
            os.unlink(path)

        self.assertEqual(md5_of_sid(sid_bytes), expected_md5)
        s1 = db.lookup(sid_bytes, 1)
        s2 = db.lookup(sid_bytes, 2)
        s3 = db.lookup(sid_bytes, 3)
        assert s1 is not None and s2 is not None and s3 is not None
        self.assertAlmostEqual(s1, 83.0)
        self.assertAlmostEqual(s2, 154.0)
        self.assertAlmostEqual(s3, 30.5)
        self.assertIsNone(db.lookup(sid_bytes, 99))

    def test_unknown_sid_returns_none(self):
        from c64cast.songlengths import LengthsDB
        with tempfile.NamedTemporaryFile("w", suffix=".md5",
                                          delete=False) as f:
            f.write("aaaa=1:00\n")
            path = f.name
        try:
            db = LengthsDB.load(path)
        finally:
            os.unlink(path)
        sid = b"PSID" + b"\x00" * 124
        self.assertIsNone(db.lookup(sid, 1))


# ---------------------------------------------------------------------------
# Framebuffer
# ---------------------------------------------------------------------------

class FramebufferTest(unittest.TestCase):

    def test_shadows_writes(self):
        from c64cast.framebuffer import Framebuffer
        fb = Framebuffer()
        fb.on_write(0x0400, b"\x01\x02\x03\x04")
        self.assertEqual(fb.ram[0x0400], 0x01)
        self.assertEqual(fb.ram[0x0403], 0x04)

    def test_render_hires_runs_and_returns_image(self):
        from c64cast.framebuffer import Framebuffer
        fb = Framebuffer()
        # Set hires mode: $D011 bit 5 = 1.
        fb.on_write(0xD011, b"\x3B")
        # Fill bitmap with alternating bytes.
        fb.on_write(0x2000, b"\xAA" * 8000)
        # Set screen RAM colors: FG=white(1), BG=black(0).
        fb.on_write(0x0400, b"\x10" * 1000)
        img = fb.render()
        self.assertEqual(img.shape, (200, 320, 3))
        self.assertEqual(img.dtype.name, "uint8")

    def test_on_write_clamps_past_top_of_ram(self):
        from c64cast.framebuffer import Framebuffer
        fb = Framebuffer()
        # Writing across the 64K boundary must truncate, not raise/overflow.
        fb.on_write(0xFFFE, b"\xAA\xBB\xCC\xDD")
        self.assertEqual(fb.ram[0xFFFE], 0xAA)
        self.assertEqual(fb.ram[0xFFFF], 0xBB)
        self.assertEqual(len(fb.ram), 0x10000)

    def test_on_write_empty_is_noop(self):
        from c64cast.framebuffer import Framebuffer
        fb = Framebuffer()
        before = bytes(fb.ram)
        fb.on_write(0x0400, b"")
        self.assertEqual(bytes(fb.ram), before)

    def test_render_text_solid_block_glyph(self):
        # Default post-reset mode is standard text. Screen code 0x60 in the
        # builtin charset is a solid block, so cell (0,0) with FG=white renders
        # as a fully-white 8×8 square.
        from c64cast.framebuffer import Framebuffer
        from c64cast.palette import C64_PALETTE_BGR
        fb = Framebuffer()
        fb.on_write(0xD021, b"\x00")          # bg0 = black
        fb.on_write(0x0400, b"\x60")          # cell (0,0) = solid block
        fb.on_write(0xD800, b"\x01")          # color RAM (0,0) = white
        img = fb.render()
        white = C64_PALETTE_BGR[1]
        self.assertTrue((img[0:8, 0:8] == white).all())

    def test_render_mcm_mono_cell(self):
        # MCM with color-RAM bit 3 clear behaves like standard text.
        from c64cast.framebuffer import Framebuffer
        from c64cast.palette import C64_PALETTE_BGR
        fb = Framebuffer()
        fb.on_write(0xD016, b"\x18")          # multicolor on
        fb.on_write(0xD021, b"\x00")
        fb.on_write(0x0400, b"\x60")          # solid block
        fb.on_write(0xD800, b"\x01")          # bit3 clear → mono FG = white
        img = fb.render()
        self.assertTrue((img[0:8, 0:8] == C64_PALETTE_BGR[1]).all())

    def test_render_mcm_multicolor_cell(self):
        # MCM with color-RAM bit 3 set: a 0xFF glyph is all '11' bit-pairs,
        # which selects color3 = color RAM low 3 bits.
        from c64cast.framebuffer import Framebuffer
        from c64cast.palette import C64_PALETTE_BGR
        fb = Framebuffer()
        fb.on_write(0xD016, b"\x18")
        fb.on_write(0x0400, b"\x60")          # solid block → all bit-pairs = 11
        fb.on_write(0xD800, b"\x0D")          # bit3 set + low3 = 5 (green)
        img = fb.render()
        # Multicolor halves horizontal resolution (doubled pixels); the cell
        # should be entirely color index 5.
        self.assertTrue((img[0:8, 0:8] == C64_PALETTE_BGR[5]).all())

    def test_render_mhires_cell(self):
        # Multicolor bitmap: bitmap byte 0xFF = all '11' pairs → color3 =
        # color RAM low nibble.
        from c64cast.framebuffer import Framebuffer
        from c64cast.palette import C64_PALETTE_BGR
        fb = Framebuffer()
        fb.on_write(0xD011, b"\x3B")          # bitmap mode
        fb.on_write(0xD016, b"\x18")          # multicolor
        fb.on_write(0x2000, b"\xFF" * 8)      # cell (0,0) bitmap all-set
        fb.on_write(0xD800, b"\x05")          # color RAM (0,0) = green
        img = fb.render()
        self.assertTrue((img[0:8, 0:8] == C64_PALETTE_BGR[5]).all())

    def test_charset_path_loaded(self):
        # A supplied 2KB char-ROM dump is used verbatim instead of the builtin.
        from c64cast.framebuffer import Framebuffer
        custom = bytes(range(256)) * 8          # 2048 bytes, distinctive
        with tempfile.NamedTemporaryFile("wb", suffix=".bin",
                                          delete=False) as f:
            f.write(custom)
            path = f.name
        try:
            fb = Framebuffer(charset_path=path)
            self.assertEqual(fb.charset, custom)
        finally:
            os.unlink(path)

    def test_short_charset_is_padded_with_warning(self):
        from c64cast.framebuffer import Framebuffer
        with tempfile.NamedTemporaryFile("wb", suffix=".bin",
                                          delete=False) as f:
            f.write(b"\xFF" * 100)              # far short of 2KB
            path = f.name
        try:
            with self.assertLogs("c64cast.framebuffer", level="WARNING"):
                fb = Framebuffer(charset_path=path)
            self.assertEqual(len(fb.charset), 2048)
            self.assertEqual(fb.charset[:100], b"\xFF" * 100)
            self.assertEqual(fb.charset[100:], b"\x00" * (2048 - 100))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
