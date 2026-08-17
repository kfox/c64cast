"""Tests for the config-introspection layer.

Two jobs: (1) prove the renderers work for every entity so `--describe` /
`--list-*` / `--compat` never crash, and (2) prove the duplicated value
vocabularies + the static display-mode table in config.py / introspect.py
stay in sync with their authoritative runtime sources (so the convenience of
keeping config.py import-light can't silently drift).
"""

from __future__ import annotations

import unittest

from c64cast.app import config as cfgmod
from c64cast.app import introspect, scene_factory


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
    """The static _MODES table must match what scene_factory._build_display_mode
    actually builds (flags + runtime name)."""

    def test_mode_flags_match_runtime(self):
        for m in introspect.display_modes():
            built = scene_factory._build_display_mode(m.name)
            self.assertEqual(built.name, m.runtime_name, m.name)
            self.assertEqual(bool(built.is_bitmapped), m.is_bitmapped, m.name)
            self.assertEqual(bool(built.is_petscii_compatible), m.is_petscii_compatible, m.name)
            self.assertEqual(
                bool(getattr(built, "is_bitmap_text_compatible", False)),
                m.is_bitmap_text_compatible,
                m.name,
            )


class CompatMatrixTest(unittest.TestCase):
    """The --compat matrix (overlay_mode_ok) must mirror the real
    overlays.validate_for_scene gate, including bitmap text support."""

    def _doc(self, name):
        return next(o for o in introspect.overlay_docs() if o.name == name)

    def _mode(self, name):
        return next(m for m in introspect.display_modes() if m.name == name)

    def test_text_overlays_attach_on_bitmap(self):
        for ov_name in (
            "clock",
            "marquee",
            "scrolling_text",
            "callsign",
            "countdown",
            "network",
            "weather",
            "rss",
            "logo",
        ):
            od = self._doc(ov_name)
            self.assertTrue(od.supports_bitmap_text, ov_name)
            for mode_name in ("hires", "hires_edges", "mhires", "petscii", "blank"):
                ok, _ = introspect.overlay_mode_ok(od, self._mode(mode_name))
                self.assertTrue(ok, f"{ov_name} should attach on {mode_name}")
            ok, _ = introspect.overlay_mode_ok(od, self._mode("mcm"))
            self.assertFalse(ok, f"{ov_name} must not attach on mcm")

    def test_non_text_petscii_overlay_stays_char_only(self):
        # spectrum_petscii writes screen RAM but doesn't fold glyphs.
        od = self._doc("spectrum_petscii")
        self.assertFalse(od.supports_bitmap_text)
        ok, _ = introspect.overlay_mode_ok(od, self._mode("hires"))
        self.assertFalse(ok)

    def test_matrix_mirrors_validate_for_scene(self):
        from c64cast.scenes.overlays import build_overlay, validate_for_scene

        ov = build_overlay({"type": "clock"}, audio=None)
        od = self._doc("clock")
        for m in introspect.display_modes():
            built_mode = scene_factory._build_display_mode(m.name)
            try:
                validate_for_scene(ov, built_mode)
                raised = False
            except ValueError:
                raised = True
            ok, _ = introspect.overlay_mode_ok(od, m)
            self.assertEqual(ok, not raised, m.name)


class ChoiceVocabSyncTest(unittest.TestCase):
    """config.py duplicates a few value lists to stay import-light; assert
    each matches its authoritative source of truth."""

    def test_palette_modes(self):
        from c64cast.video import modes

        self.assertEqual(cfgmod._PALETTE_MODE_CHOICES, modes.PALETTE_MODES)

    def test_styles(self):
        from c64cast.video import petscii_styles as ps

        self.assertEqual(cfgmod._STYLE_CHOICES, ps.STYLE_NAMES + (ps.RANDOM_STYLE,))

    def test_time_base_and_persistence(self):
        from c64cast.sid import waveform

        self.assertEqual(cfgmod._TIME_BASE_CHOICES, waveform.TIME_BASE_NAMES)
        self.assertEqual(cfgmod._PERSISTENCE_CHOICES, waveform.PERSISTENCE_NAMES)

    def test_midi_waveforms(self):
        from c64cast.sid import midi_scene

        self.assertEqual(set(cfgmod._MIDI_WAVEFORM_CHOICES), set(midi_scene._WAVEFORM_BITS))

    def test_midi_voice_modes(self):
        from c64cast.sid import midi_scene

        self.assertEqual(cfgmod._MIDI_VOICE_MODE_CHOICES, midi_scene.VOICE_MODES)

    def test_backgrounds(self):
        from c64cast.scenes import backgrounds

        self.assertEqual(set(cfgmod._BACKGROUND_CHOICES) - {"random"}, set(backgrounds.REGISTRY))

    def test_generative_sources(self):
        from c64cast.scenes import generators

        self.assertEqual(cfgmod._GENERATIVE_SOURCE_CHOICES, generators.generator_names())

    def test_effects(self):
        from c64cast.scenes import effects

        self.assertEqual(cfgmod._EFFECT_CHOICES, effects.effect_names())

    def test_audio_source_choices_pinned(self):
        # No registry backs the AudioSource family (it's a fixed protocol set);
        # pin the literal so a new value can't be added to the SceneCfg field
        # metadata without build_scene learning to construct it.
        self.assertEqual(cfgmod._AUDIO_SOURCE_CHOICES, ("none", "mic", "listen", "file", "sid"))
        # SceneCfg metadata must match the constant.
        from dataclasses import fields

        meta = {f.name: f for f in fields(cfgmod.SceneCfg)}["audio_source"].metadata
        self.assertEqual(meta["choices"], cfgmod._AUDIO_SOURCE_CHOICES)
        self.assertEqual(meta["applies_to"], ("generative",))

    def test_audio_backend_choices_pinned(self):
        # The video-audio backend selector is a fixed literal set (no registry):
        # pin it so a new value can't be added to AudioCfg.backend metadata
        # without resolve_audio_backend + build_scene learning to honor it.
        self.assertEqual(cfgmod.AUDIO_BACKEND_CHOICES, ("auto", "dac", "sampler"))
        from dataclasses import fields

        meta = {f.name: f for f in fields(cfgmod.AudioCfg)}["backend"].metadata
        self.assertEqual(meta["choices"], cfgmod.AUDIO_BACKEND_CHOICES)

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

    def test_generative_includes_source_audio_and_sid_fields(self):
        gen = next(s for s in introspect.scene_types() if s.name == "generative")
        names = {f.name for f in gen.fields}
        self.assertIn("source", names)
        self.assertIn("audio_source", names)
        self.assertIn("effect", names)
        # file + song now surface for generative (used when audio_source = sid).
        self.assertIn("file", names)
        self.assertIn("song", names)

    def test_universal_fields_present_everywhere(self):
        for s in introspect.scene_types():
            names = {f.name for f in s.fields}
            self.assertIn("type", names, s.name)
            self.assertIn("overlays", names, s.name)


class ReloadableSectionsTest(unittest.TestCase):
    """`RELOADABLE_SECTIONS` is what the web console reads to say, at the moment
    of saving, which changes a reload will apply and which need a restart. It is
    a claim about `session.reload_all`, so it has to name real sections and the
    docstring that describes the behaviour has to point at it."""

    def test_every_named_section_exists(self):
        known = {s.name for s in introspect.config_sections()}
        self.assertTrue(known >= cfgmod.RELOADABLE_SECTIONS, cfgmod.RELOADABLE_SECTIONS - known)

    def test_the_flag_reaches_introspection(self):
        flags = {s.name: s.reload for s in introspect.config_sections()}
        for name in cfgmod.RELOADABLE_SECTIONS:
            self.assertTrue(flags[name], name)
        self.assertFalse(flags["ultimate64"])  # the connection is built once
        self.assertFalse(flags["audio"])  # its threads start with the session

    def test_reload_all_documents_the_same_rule(self):
        from c64cast.app.session import reload_all

        self.assertIn("RELOADABLE_SECTIONS", reload_all.__doc__ or "")


class VocabularyTest(unittest.TestCase):
    """`FieldDoc.vocabulary` names the small set a field's *string* values come
    from, which is how a form knows to offer swatches for a colour and a text
    box for everything else. `choices` cannot say it: these fields take an index
    as well, and a picker built from `choices` would refuse one."""

    def _scene_field(self, name: str) -> introspect.FieldDoc:
        fields = {f.name: f for st in introspect.scene_types() for f in st.fields}
        return fields[name]

    def test_the_colour_fields_declare_it(self):
        for name in ("border", "background"):
            self.assertEqual(self._scene_field(name).vocabulary, "c64color", name)

    def test_a_field_that_is_not_a_colour_does_not(self):
        # `[video].device` is `int | str` too, and its strings are camera names.
        fields = {f.name: f for s in introspect.config_sections() for f in s.fields}
        self.assertEqual(fields["device"].vocabulary, "")

    def test_it_reaches_the_document_the_console_renders(self):
        doc = introspect.as_dict()
        scene_fields = {f["name"]: f for st in doc["scene_types"] for f in st["fields"]}
        self.assertEqual(scene_fields["border"]["vocabulary"], "c64color")


class PaletteSwatchTest(unittest.TestCase):
    """The swatch picker's colours are served rather than copied into the
    browser — a second palette to keep in step with the first is the bug this
    avoids."""

    def test_sixteen_colours_with_writable_names(self):
        from c64cast.video.palette import resolve_color

        swatches = introspect.palette_swatches()
        self.assertEqual(len(swatches), 16)
        for index, swatch in enumerate(swatches):
            self.assertEqual(swatch["index"], index)
            # Both spellings have to survive the loader, because the picker
            # writes one of them into a config.
            self.assertEqual(resolve_color(swatch["name"]), index)
            self.assertEqual(resolve_color(swatch["label"]), index)
            self.assertRegex(swatch["hex"], r"^#[0-9a-f]{6}$")

    def test_the_hex_is_rgb_not_the_bgr_it_is_stored_as(self):
        from c64cast.video.palette import C64_PALETTE_BGR

        blue, green, red = (int(c) for c in C64_PALETTE_BGR[2])  # red
        swatch = introspect.palette_swatches()[2]
        self.assertEqual(swatch["hex"], f"#{red:02x}{green:02x}{blue:02x}")
        self.assertGreater(red, blue)

    def test_it_rides_along_in_the_introspection_document(self):
        self.assertEqual(len(introspect.as_dict()["palette"]), 16)


if __name__ == "__main__":
    unittest.main()
