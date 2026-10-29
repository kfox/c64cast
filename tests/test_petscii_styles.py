"""Tests for c64cast.video.petscii_styles + PETSCIIDisplayMode style cycling.

Each PetsciiStyle.compose() takes a 25×40 BGR image and returns 1000-byte
screen + color buffers. Tests cover registration, shape correctness,
SHIFT-driven cycling, and the "random" sentinel resolution.
"""

# FakeAPI is a duck-typed stub of Ultimate64API.
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest

import numpy as np

from c64cast.video import petscii_styles as ps
from c64cast.video.modes import PETSCIIDisplayMode
from c64cast.video.palette import CHANNEL_BOOST, DEFAULT_HUE_CORRECTIONS

# The global [color] shaping PETSCIIDisplayMode passes into every style.compose.
_BOOST = CHANNEL_BOOST
_HUE = DEFAULT_HUE_CORRECTIONS


def _frame():
    """Reasonably interesting 25×40 BGR frame for style smoke tests."""
    img = np.zeros((25, 40, 3), dtype=np.uint8)
    img[:, :10] = (40, 40, 200)  # red-ish
    img[:, 10:20] = (40, 200, 40)  # green-ish
    img[:, 20:30] = (200, 40, 40)  # blue-ish
    img[:, 30:] = (180, 180, 180)  # light gray
    return img


class StyleRegistryTest(unittest.TestCase):
    def test_all_styles_in_cycle_list_construct(self):
        for name in ps.STYLE_NAMES:
            style = ps.make_style(name)
            self.assertEqual(style.name, name)

    def test_validate_style_rejects_unknown(self):
        with self.assertRaises(ValueError):
            ps.validate_style("bogus")
        ps.validate_style("default")
        ps.validate_style(ps.RANDOM_STYLE)  # sentinel accepted

    def test_random_sentinel_resolves_to_a_concrete_style(self):
        chosen = ps.pick_random_style_name()
        self.assertIn(chosen, ps.STYLE_NAMES)


class StyleComposeShapeTest(unittest.TestCase):
    """Every style must return (1000,) uint8 screen + color buffers."""

    def test_every_style_returns_correctly_shaped_buffers(self):
        img = _frame()
        for name in ps.STYLE_NAMES:
            for perceptual in (False, True):
                style = ps.make_style(name)
                screen, color = style.compose(img, _BOOST, _HUE, perceptual)
                label = f"{name}/perceptual={perceptual}"
                self.assertEqual(screen.shape, (1000,), label)
                self.assertEqual(color.shape, (1000,), label)
                self.assertEqual(screen.dtype, np.uint8, label)
                self.assertEqual(color.dtype, np.uint8, label)
                # Color RAM holds palette indices 0..15 (low nibble matters).
                self.assertTrue(
                    (color & 0x0F == color).all(),
                    f"{label}: color RAM has high-nibble bits set",
                )


class IndividualStyleBehaviorTest(unittest.TestCase):
    def test_default_uses_default_char_ramp(self):
        img = np.zeros((25, 40, 3), dtype=np.uint8)
        screen, _ = ps.DefaultStyle().compose(img, _BOOST, _HUE)
        # First entry of the ramp is SC_SPACE (0x20).
        self.assertEqual(int(screen[0]), 0x20)

    def test_color_only_paints_every_cell_full_block(self):
        screen, _ = ps.ColorOnlyStyle().compose(_frame(), _BOOST, _HUE)
        self.assertTrue(
            (screen == 0xA0).all(), "color_only must fill every cell with SC_FULL_BLOCK"
        )

    def test_inverse_pop_screen_is_only_space_or_block(self):
        screen, color = ps.InversePopStyle().compose(_frame(), _BOOST, _HUE)
        unique = {int(v) for v in np.unique(screen)}
        self.assertLessEqual(
            unique, {0x20, 0xA0}, f"inverse_pop screen has unexpected codes {unique}"
        )
        pop = {int(v) for v in ps.InversePopStyle.POP_PALETTE_INDICES}
        for c in np.unique(color):
            self.assertIn(int(c), pop, f"inverse_pop produced non-pop FG {int(c)}")

    def test_neon_color_avoids_gray_axis(self):
        from c64cast.video.palette import GRAY_INDICES

        _, color = ps.NeonStyle().compose(_frame(), _BOOST, _HUE)
        for c in np.unique(color):
            self.assertNotIn(int(c), GRAY_INDICES, f"neon picked gray-axis color {int(c)}")

    def test_random_glyph_is_stable_per_cell(self):
        style = ps.RandomGlyphStyle()
        s1, _ = style.compose(_frame(), _BOOST, _HUE)
        img2 = np.full((25, 40, 3), 200, dtype=np.uint8)
        s2, _ = style.compose(img2, _BOOST, _HUE)
        np.testing.assert_array_equal(
            s1, s2, "random_glyph cell→glyph mapping must be stable across frames"
        )

    def test_letter_rain_uses_only_a_to_z(self):
        screen, _ = ps.LetterRainStyle().compose(_frame(), _BOOST, _HUE)
        self.assertTrue(
            (screen >= 0x01).all() and (screen <= 0x1A).all(),
            "letter_rain must only emit screen codes 0x01..0x1A (A-Z)",
        )

    def test_hue_corrections_reach_styles_and_rescue_purple(self):
        # A dark blue-leaning violet (BGR) like the TRON arena glyphs: it
        # quantizes to gray/blue without the purple-rescue hue band and to
        # C64 purple (index 4) with it.
        violet = np.full((25, 40, 3), (114, 57, 74), dtype=np.uint8)
        _, plain = ps.ColorOnlyStyle().compose(violet, _BOOST, ())
        _, rescued = ps.ColorOnlyStyle().compose(violet, _BOOST, _HUE)
        self.assertNotIn(
            4, {int(c) for c in np.unique(plain)}, "expected NO purple without the hue band"
        )
        self.assertIn(
            4,
            {int(c) for c in np.unique(rescued)},
            "expected C64 purple (index 4) with the purple rescue",
        )


# Rows of the uppercase character ROM for the glyphs the ramps below use;
# a code with bit 7 set draws the inverse of the code 0x80 below it.
_ROM_ROWS = {
    0x20: "0000000000000000",
    0x4D: "C0E070381C0E0703",
    0x4E: "03070E1C3870E0C0",
    0x56: "C3E77E3C3C7EE7C3",
    0x58: "1818666618183C00",
    0x61: "F0F0F0F0F0F0F0F0",
    0x62: "00000000FFFFFFFF",
    0x64: "00000000000000FF",
    0x6C: "000000000F0F0F0F",
    0x6F: "000000000000FFFF",
    0x7E: "F0F0F0F000000000",
}


def _ink(screen_code: int) -> int:
    rows = bytes.fromhex(_ROM_ROWS[screen_code & 0x7F])
    lit = sum(bin(b).count("1") for b in rows)
    return 64 - lit if screen_code & 0x80 else lit


def _uppercase_char(screen_code: int) -> str:
    """The character an uppercase-ROM screen code draws; codes from 0x40 up
    are graphics glyphs, shown as `?`."""
    if screen_code >= 0x40:
        return "?"
    return chr(screen_code + 0x40) if screen_code < 0x20 else chr(screen_code)


class GlyphRampTest(unittest.TestCase):
    """A ramp's screen codes draw the glyphs it means, in the order it means.

    Screen codes are not PETSCII: `@` is PETSCII 0x40 but screen code 0x00,
    and screen code 0x40 draws a horizontal bar."""

    def test_default_ramp_is_the_ascii_density_ramp_then_a_full_block(self):
        chars = "".join(_uppercase_char(int(c)) for c in ps.DefaultStyle.CHARS[:-1])
        self.assertEqual(chars, " .:-=+*#%@")
        self.assertEqual(int(ps.DefaultStyle.CHARS[-1]), 0xA0)

    def test_halftone_coverage_rises_by_a_quarter_per_step(self):
        self.assertEqual([_ink(int(c)) for c in ps.HalftoneStyle.CHARS], [0, 16, 32, 48, 64])

    def test_hatch_ends_on_the_cross_before_the_full_block(self):
        inks = [_ink(int(c)) for c in ps.HatchStyle.CHARS]
        self.assertEqual(inks, sorted(inks))
        self.assertEqual(int(ps.HatchStyle.CHARS[3]), 0x56)


class CyclingTest(unittest.TestCase):
    """SHIFT-driven cycling rotates through STYLE_NAMES in declared order."""

    def test_petscii_display_mode_cycle_rotates(self):
        from _fakes import FakeAPI

        api = FakeAPI()
        m = PETSCIIDisplayMode(style="default")
        self.assertEqual(m.style, "default")
        seen = [m.style]
        for _ in range(len(ps.STYLE_NAMES)):
            label = m.cycle_style(api)
            self.assertIsNotNone(label)
            self.assertIn(m.style, label)  # type: ignore[arg-type]
            seen.append(m.style)
        self.assertEqual(seen[0], seen[-1])
        self.assertEqual(set(seen), set(ps.STYLE_NAMES))
        # Cache invalidated each cycle so the next compose fully repaints.
        self.assertEqual(api.cache_invalidations, len(ps.STYLE_NAMES))

    def test_petscii_random_resolves_at_construction(self):
        m = PETSCIIDisplayMode(style=ps.RANDOM_STYLE)
        self.assertIn(m.style, ps.STYLE_NAMES, "'random' must resolve to a concrete style")

    def test_petscii_invalid_style_rejected(self):
        with self.assertRaises(ValueError):
            PETSCIIDisplayMode(style="bogus")

    def test_cycle_updates_border_and_background_registers(self):
        # inverse_pop pushes border + background to $D020/$D021 in one PUT.
        from _fakes import FakeAPI

        api = FakeAPI()
        m = PETSCIIDisplayMode(style="default")
        while m.style != "inverse_pop":
            m.cycle_style(api)
        regs = api.regs.get("d020") or api.regs.get("D020")
        self.assertIsNotNone(regs, "expected a write to D020/D021 on cycle")
        assert regs is not None
        self.assertEqual(len(regs), 2)
        self.assertEqual(regs[0], ps.InversePopStyle.border)
        self.assertEqual(regs[1], ps.InversePopStyle.background)


if __name__ == "__main__":
    unittest.main()
