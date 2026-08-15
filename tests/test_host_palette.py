"""Tests for the host-palette layer in c64cast.video.palette — the named
tables, .vpl parsing, and the in-place swap that repoints the whole color
pipeline at the colors a particular machine emits.

The swap is process-wide and mutates arrays other modules imported by
reference, so most of what is worth testing here is that nothing is left
holding the old colors."""

from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from c64cast.video import palette


class PaletteSwapTestCase(unittest.TestCase):
    """Base for anything that calls set_host_palette: puts the process-wide
    palette back afterwards so a failure here can't cascade into every other
    color test in the suite."""

    def setUp(self):
        before = palette.C64_PALETTE_BGR.copy(), palette.active_host_palette_name()
        self.addCleanup(lambda: palette.set_host_palette(before[0], name=before[1]))


class ParseVplTest(unittest.TestCase):
    """palette.parse_vpl() — the VICE .vpl format, which is how a machine with
    a custom palette gets described."""

    def test_reads_sixteen_bgr_triples(self):
        text = "\n".join(f"{i:02x} {i * 2:02x} {i * 3:02x}" for i in range(16))
        colors = palette.parse_vpl(text)
        self.assertEqual(len(colors), 16)
        # RR GG BB in the file, BGR in the returned table.
        self.assertEqual(colors[5], (15, 10, 5))

    def test_ignores_comments_and_blank_lines(self):
        body = "\n".join(f"{i:02x} {i:02x} {i:02x}" for i in range(16))
        text = "# VICE palette file\n\n" + body + "\n\n# trailing note\n"
        self.assertEqual(len(palette.parse_vpl(text)), 16)

    def test_drops_the_vice_dither_column(self):
        """A fourth column is VICE's dither value, not an alpha channel."""
        text = "\n".join(f"{i:02x} {i:02x} {i:02x} 0f" for i in range(16))
        self.assertEqual(len(palette.parse_vpl(text)), 16)

    def test_rejects_a_short_file(self):
        text = "\n".join("00 00 00" for _ in range(15))
        with self.assertRaisesRegex(ValueError, "found 15"):
            palette.parse_vpl(text)

    def test_rejects_non_hex(self):
        lines = ["00 00 00"] * 15 + ["zz 00 00"]
        with self.assertRaisesRegex(ValueError, "non-hex"):
            palette.parse_vpl("\n".join(lines))

    def test_rejects_a_truncated_line(self):
        lines = ["00 00 00"] * 15 + ["ff ff"]
        with self.assertRaisesRegex(ValueError, "RR GG BB"):
            palette.parse_vpl("\n".join(lines))


class ResolveHostPaletteTest(unittest.TestCase):
    """palette.resolve_host_palette() — a built-in name or a path to a .vpl."""

    def test_known_names(self):
        self.assertIs(palette.resolve_host_palette("pepto"), palette.PEPTO_PALETTE_BGR)
        self.assertIs(palette.resolve_host_palette("u64"), palette.U64_PALETTE_BGR)

    def test_an_unknown_name_lists_the_known_ones(self):
        with self.assertRaises(ValueError) as ctx:
            palette.resolve_host_palette("colodore")
        msg = str(ctx.exception)
        self.assertIn("pepto", msg)
        self.assertIn("u64", msg)
        self.assertIn(".vpl", msg)

    def test_a_vpl_path(self):
        body = "\n".join(f"{i:02x} 00 00" for i in range(16))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mine.vpl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            table = palette.resolve_host_palette(path)
        self.assertEqual(table[3], (0, 0, 3))

    def test_a_missing_vpl_path_names_the_file(self):
        with self.assertRaisesRegex(ValueError, "nope.vpl"):
            palette.resolve_host_palette("/nonexistent/nope.vpl")


class U64PaletteTableTest(unittest.TestCase):
    """The Ultimate 64's table is measurement-confirmed against its own HDMI
    output, so it is pinned rather than left to drift."""

    def test_pinned_to_the_firmware_table(self):
        # u64_config.cc `default_colors`, transcribed to BGR order. Spot-check
        # the entries that move furthest from a real VIC-II.
        self.assertEqual(palette.U64_PALETTE_BGR[8], (0x20, 0x4E, 0x98))  # orange
        self.assertEqual(palette.U64_PALETTE_BGR[3], (0xCD, 0xD4, 0x6A))  # cyan
        self.assertEqual(palette.U64_PALETTE_BGR[1], (0xF7, 0xF7, 0xF7))  # white
        self.assertEqual(palette.U64_PALETTE_BGR[0], (0, 0, 0))

    def test_far_enough_from_pepto_to_matter(self):
        """The whole point of the knob: these are not two roundings of one
        table. If this ever shrinks to a couple of counts, the [hardware]
        setting is no longer earning its place."""
        a = np.array(palette.PEPTO_PALETTE_BGR, dtype=np.float32)
        b = np.array(palette.U64_PALETTE_BGR, dtype=np.float32)
        self.assertGreater(np.abs(a - b).mean(), 20.0)

    def test_the_wrong_table_picks_different_colors(self):
        """Quantizing the colors a U64 emits against a real VIC-II's table
        does not just shade them differently — it sends a real fraction of
        them to a different palette index outright."""
        emitted = np.array(palette.U64_PALETTE_BGR, dtype=np.float32)
        got = palette.quantize_flat_for(emitted, perceptual=True)
        wrong = int((got != np.arange(16)).sum())
        self.assertGreater(wrong, 0)


class PaletteAccuracyTest(PaletteSwapTestCase):
    """The numeric claim the [hardware].host_palette knob rests on: aiming at
    the table the machine emits reduces perceptual error, and by enough to be
    worth a config field. A guard against a future palette edit that quietly
    gives the win back."""

    @staticmethod
    def _lab(bgr: np.ndarray) -> np.ndarray:
        import cv2

        u8 = np.clip(bgr, 0, 255).astype(np.uint8).reshape(-1, 1, 3)
        return cv2.cvtColor(u8, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)

    def test_the_right_table_reconstructs_better(self):
        # A coarse sweep of the sRGB cube rather than one image, so the result
        # isn't a statement about one photograph's color distribution.
        axis = np.arange(0, 256, 16, dtype=np.float32)
        b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
        px = np.stack([b.ravel(), g.ravel(), r.ravel()], axis=1).astype(np.float32)
        source = self._lab(px)
        emitted = np.array(palette.U64_PALETTE_BGR, dtype=np.float32)

        palette.set_host_palette(palette.PEPTO_PALETTE_BGR, name="pepto")
        wrong_idx = palette.quantize_flat_for(px, perceptual=True)
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        right_idx = palette.quantize_flat_for(px, perceptual=True)

        # Both index sets are DISPLAYED in the colors the machine emits — that
        # is what makes this a fair comparison rather than each table grading
        # its own homework.
        wrong_err = np.linalg.norm(self._lab(emitted[wrong_idx]) - source, axis=1).mean()
        right_err = np.linalg.norm(self._lab(emitted[right_idx]) - source, axis=1).mean()
        self.assertLess(right_err, wrong_err)
        self.assertGreater(wrong_err / right_err - 1.0, 0.05)
        self.assertGreater(float((wrong_idx != right_idx).mean()), 0.10)


class SetHostPaletteTest(PaletteSwapTestCase):
    """palette.set_host_palette() — the in-place swap."""

    def test_rejects_a_wrong_shape(self):
        with self.assertRaisesRegex(ValueError, "16 BGR triples"):
            palette.set_host_palette(np.zeros((8, 3), dtype=np.float32))

    def test_updates_the_derived_tables(self):
        before_luma = palette.PALETTE_LUMA.copy()
        before_lab = palette._PALETTE_LAB.copy()
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        self.assertFalse(np.array_equal(before_luma, palette.PALETTE_LUMA))
        self.assertFalse(np.array_equal(before_lab, palette._PALETTE_LAB))

    def test_modules_holding_a_reference_see_the_change(self):
        """framebuffer and the display modes bind C64_PALETTE_BGR at import
        time, so the swap has to mutate the array rather than rebind the name.
        Rebinding would leave the software mirror painting the old colors."""
        from c64cast.video import framebuffer

        self.assertIs(framebuffer.C64_PALETTE_BGR, palette.C64_PALETTE_BGR)
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        self.assertEqual(tuple(framebuffer.C64_PALETTE_BGR[8]), (32.0, 78.0, 152.0))

    def test_quantizing_follows_the_active_palette(self):
        u64_orange = np.array([palette.U64_PALETTE_BGR[8]], dtype=np.float32)
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        self.assertEqual(int(palette.quantize_flat_for(u64_orange, perceptual=True)[0]), 8)

    def test_clears_the_fade_lut_cache(self):
        """A fade LUT is a palette-to-palette mapping, so a cached one from the
        previous palette would dim to the wrong colors."""
        palette.build_fade_lut(0.5)
        self.assertTrue(palette._FADE_LUT_CACHE)
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        self.assertFalse(palette._FADE_LUT_CACHE)

    def test_records_the_active_name(self):
        palette.set_host_palette(palette.U64_PALETTE_BGR, name="u64")
        self.assertEqual(palette.active_host_palette_name(), "u64")


if __name__ == "__main__":
    unittest.main()
