"""Tests for BlankDisplayMode + BlankScene.

The blank mode is a standard PETSCII char mode with no video input —
just SC_SPACE everywhere with configurable border + background. Used
as a clean canvas for overlays (e.g. big_text title cards).
"""

# FakeAPI is a duck-typed stub of Ultimate64API; silence pyright's
# argument-type complaints across the file rather than spraying per-call
# ignores. compose(None) is allowed at runtime even though the annotated
# signature wants an ndarray.
# pyright: reportArgumentType=false
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from _fakes import FakeAPI

from c64cast.modes import BlankDisplayMode, MCMDisplayMode


class BlankDisplayModeTest(unittest.TestCase):
    def test_construct_with_defaults(self):
        m = BlankDisplayMode()
        self.assertEqual(m.border, 0)
        self.assertEqual(m.background, 0)
        self.assertEqual(m.name, "blank")
        self.assertTrue(m.is_petscii_compatible)
        self.assertTrue(m.supports_compose)

    def test_border_and_background_masked_to_nibble(self):
        # The C64 only has 16 colors; high bits should be silently dropped
        # rather than producing register-pollution writes.
        m = BlankDisplayMode(border=0xFF, background=0xF6)
        self.assertEqual(m.border, 0x0F)
        self.assertEqual(m.background, 0x06)

    def test_compose_returns_blank_buffers(self):
        m = BlankDisplayMode(background=6)
        out = m.compose()
        self.assertEqual(out["screen"].shape, (1000,))
        self.assertEqual(out["color"].shape, (1000,))
        # Every cell is SC_SPACE (0x20).
        self.assertTrue((out["screen"] == 0x20).all())
        # Every cell's FG = background, so SC_SPACE renders invisibly until
        # an overlay paints over it.
        self.assertTrue((out["color"] == 6).all())

    def test_compose_accepts_none_frame(self):
        # BlankScene calls _render_with_overlays(None, ...) which feeds
        # None into compose(). The signature must allow this.
        m = BlankDisplayMode()
        out = m.compose(None)
        self.assertTrue((out["screen"] == 0x20).all())

    def test_setup_writes_vic_registers(self):
        api = FakeAPI()
        m = BlankDisplayMode(border=2, background=6)
        m.setup(api)
        # D018 = char-mode pointer, D016/D011 = control registers.
        self.assertEqual(api.memories["D018"], "14")
        self.assertEqual(api.memories["D016"], "08")
        self.assertEqual(api.memories["D011"], "1b")
        # Border + background written as a contiguous pair.
        self.assertEqual(api.regs["D020"], (2, 6))

    def test_push_writes_screen_and_color(self):
        api = FakeAPI()
        m = BlankDisplayMode(background=6)
        m.push(api, m.compose())
        self.assertEqual(len(api.regions[0x0400]), 1000)
        self.assertEqual(len(api.regions[0xD800]), 1000)
        # Every screen byte = 0x20, every color byte = 6.
        self.assertTrue(all(b == 0x20 for b in api.regions[0x0400]))
        self.assertTrue(all(b == 6 for b in api.regions[0xD800]))


class BlankSceneTest(unittest.TestCase):
    def test_scene_constructs_without_source(self):
        from c64cast.scenes import BlankScene

        api = FakeAPI()
        mode = BlankDisplayMode()
        scene = BlankScene(api, audio=None, display_mode=mode, audio_cfg=MagicMock(), name="Blank")
        self.assertEqual(scene.name, "Blank")
        self.assertIsNone(scene.audio)

    def test_process_frame_returns_false_after_duration(self):
        from c64cast.scenes import BlankScene

        api = FakeAPI()
        mode = BlankDisplayMode()
        scene = BlankScene(api, audio=None, display_mode=mode, audio_cfg=MagicMock(), name="Blank")
        scene.duration_s = 0.1
        scene.setup()
        # First frame: well under duration.
        self.assertTrue(scene.process_frame(scene.start_time + 0.01))
        # Past duration: scene done.
        self.assertFalse(scene.process_frame(scene.start_time + 0.5))
        scene.teardown()


class PetsciiCompatibleValidationTest(unittest.TestCase):
    """`validate_for_scene` accepts PETSCII overlays on either petscii or
    blank modes via the is_petscii_compatible flag, and rejects them on
    other char modes (MCM)."""

    def test_blank_mode_accepts_petscii_overlay(self):
        from c64cast.overlays import build_overlay, validate_for_scene

        ov = build_overlay({"type": "clock"}, audio=None)
        validate_for_scene(ov, BlankDisplayMode())  # no raise

    def test_mcm_still_rejects_petscii_overlay(self):
        from c64cast.overlays import build_overlay, validate_for_scene

        ov = build_overlay({"type": "clock"}, audio=None)
        with self.assertRaises(ValueError):
            validate_for_scene(ov, MCMDisplayMode())


if __name__ == "__main__":
    unittest.main()
