"""Tests for the config-introspection layer.

Two jobs: (1) prove the renderers work for every entity so `--describe` /
`--list-*` / `--compat` never crash, and (2) prove the duplicated value
vocabularies + the static display-mode table in config.py / introspect.py
stay in sync with their authoritative runtime sources (so the convenience of
keeping config.py import-light can't silently drift).
"""

from __future__ import annotations

import unittest

from c64cast import config as cfgmod
from c64cast import introspect


class RenderSmokeTest(unittest.TestCase):
    def test_list_renderers(self):
        for r in (
            introspect.render_list_scenes(),
            introspect.render_list_overlays(),
            introspect.render_list_modes(),
            introspect.render_compat(),
        ):
            self.assertIsInstance(r, str)
            self.assertTrue(r.strip())

    def test_describe_every_entity(self):
        names = (
            [f"section:{s.name}" for s in introspect.config_sections()]
            + [f"scene:{s.name}" for s in introspect.scene_types()]
            + [f"overlay:{o.name}" for o in introspect.overlay_docs()]
            + [f"mode:{m.name}" for m in introspect.display_modes()]
        )
        for n in names:
            out = introspect.render_describe(n)
            self.assertTrue(out.strip(), n)
            self.assertNotIn("unknown", out.split("\n", 1)[0].lower(), n)

    def test_describe_unprefixed_and_errors(self):
        self.assertIn("[audio]", introspect.render_describe("audio"))
        self.assertIn("nothing named", introspect.render_describe("nope"))
        self.assertIn("prefix", introspect.render_describe("bogus:audio"))

    def test_every_overlay_has_help(self):
        for od in introspect.overlay_docs():
            self.assertTrue(od.help, f"overlay {od.name} missing HELP")

    def test_overlay_required_params_have_no_default(self):
        # rss/logo/countdown/scrolling_text/big_text take a required arg.
        docs = {o.name: o for o in introspect.overlay_docs()}
        req = {p.name for p in docs["rss"].params if p.required}
        self.assertIn("url", req)


class ModeTableSyncTest(unittest.TestCase):
    """The static _MODES table must match what config._build_display_mode
    actually builds (flags + runtime name)."""

    def test_mode_flags_match_runtime(self):
        for m in introspect.display_modes():
            built = cfgmod._build_display_mode(m.name)
            self.assertEqual(built.name, m.runtime_name, m.name)
            self.assertEqual(bool(built.is_bitmapped), m.is_bitmapped, m.name)
            self.assertEqual(bool(built.is_petscii_compatible), m.is_petscii_compatible, m.name)


class ChoiceVocabSyncTest(unittest.TestCase):
    """config.py duplicates a few value lists to stay import-light; assert
    each matches its authoritative source of truth."""

    def test_palette_modes(self):
        from c64cast import modes

        self.assertEqual(cfgmod._PALETTE_MODE_CHOICES, modes.PALETTE_MODES)

    def test_styles(self):
        from c64cast import petscii_styles as ps

        self.assertEqual(cfgmod._STYLE_CHOICES, ps.STYLE_NAMES + (ps.RANDOM_STYLE,))

    def test_time_base_and_persistence(self):
        from c64cast import waveform

        self.assertEqual(cfgmod._TIME_BASE_CHOICES, waveform.TIME_BASE_NAMES)
        self.assertEqual(cfgmod._PERSISTENCE_CHOICES, waveform.PERSISTENCE_NAMES)

    def test_midi_waveforms(self):
        from c64cast import midi_scene

        self.assertEqual(set(cfgmod._MIDI_WAVEFORM_CHOICES), set(midi_scene._WAVEFORM_BITS))

    def test_backgrounds(self):
        from c64cast import backgrounds

        self.assertEqual(set(cfgmod._BACKGROUND_CHOICES) - {"random"}, set(backgrounds.REGISTRY))

    def test_scene_types(self):
        self.assertEqual(set(cfgmod.SCENE_TYPES), set(introspect.scene_type_names()))


class AppliesToTest(unittest.TestCase):
    def test_waveform_excludes_midi_fields(self):
        wf = next(s for s in introspect.scene_types() if s.name == "waveform")
        names = {f.name for f in wf.fields}
        self.assertIn("time_base", names)
        self.assertNotIn("midi_waveform", names)

    def test_midi_includes_scope_knobs_and_midi_fields(self):
        # MidiScene now shares the bitmap oscilloscope, so the scope knobs
        # (time_base etc.) apply to it as well as its own midi_* fields.
        midi = next(s for s in introspect.scene_types() if s.name == "midi")
        names = {f.name for f in midi.fields}
        self.assertIn("midi_waveform", names)
        self.assertIn("time_base", names)
        self.assertIn("persistence", names)
        self.assertIn("scroll_columns", names)
        # waveform-only fields (SID file playback) stay excluded.
        self.assertNotIn("song", names)

    def test_universal_fields_present_everywhere(self):
        for s in introspect.scene_types():
            names = {f.name for f in s.fields}
            self.assertIn("type", names, s.name)
            self.assertIn("overlays", names, s.name)


if __name__ == "__main__":
    unittest.main()
