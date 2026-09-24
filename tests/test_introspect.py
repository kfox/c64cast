"""Tests for the config-introspection layer.

Two jobs: (1) prove the renderers work for every entity so `--describe` /
`--list-*` / `--compat` never crash, and (2) prove the duplicated value
vocabularies + the static display-mode table in config.py / introspect.py
stay in sync with their authoritative runtime sources (so the convenience of
keeping config.py import-light can't silently drift).
"""

from __future__ import annotations

import dataclasses
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


def choices_tuple_names() -> frozenset[str]:
    """Every value vocabulary config.py exports: the `*_CHOICES` names, plus any
    other module-level name a field's `choices` metadata is bound to. The suffix
    alone is not the boundary — `SCENE_TYPES` and `HIRES_CELL_PICKS` are both
    `choices` under a name that does not carry it, so keying on the suffix would
    let the next one ship with no routing decision made."""
    surfaced = {
        id(f.metadata["choices"])
        for v in vars(cfgmod).values()
        if dataclasses.is_dataclass(v) and isinstance(v, type)
        for f in dataclasses.fields(v)
        if "choices" in f.metadata
    }
    return frozenset(
        n for n, v in vars(cfgmod).items() if n.endswith("_CHOICES") or id(v) in surfaced
    )


def mirrored_choices() -> dict[str, tuple[str, object]]:
    """`name -> (source label, expected value)` for every choices tuple
    config.py copies out of a runtime module to stay import-light. A `set`
    expectation is compared unordered; anything else is compared exactly."""
    from c64cast.control import midi_control
    from c64cast.hw import backend, hw_provision
    from c64cast.scenes import backgrounds, effects, generators
    from c64cast.sid import midi_scene, voice_scope
    from c64cast.video import modes
    from c64cast.video import petscii_styles as ps

    return {
        "HDMI_SCAN_RESOLUTION_CHOICES": (
            "('auto', 'keep') + hw.hw_provision.HDMI_RESOLUTION_CHOICES",
            ("auto", "keep") + hw_provision.HDMI_RESOLUTION_CHOICES,
        ),
        "SCENE_TYPES": (
            "app.scene_factory._BUILDERS",
            set(scene_factory._BUILDERS),
        ),
        "_BACKEND_CHOICES": ("hw.backend.BACKENDS", backend.BACKENDS),
        "_BACKGROUND_CHOICES": (
            "scenes.backgrounds.REGISTRY plus 'random'",
            set(backgrounds.REGISTRY) | {"random"},
        ),
        "_DISPLAY_CHOICES": (
            "introspect.display_modes() plus 'random'",
            {m.name for m in introspect.display_modes()} | {"random"},
        ),
        "_EFFECT_CHOICES": ("scenes.effects.effect_names()", effects.effect_names()),
        "_GENERATIVE_SOURCE_CHOICES": (
            "scenes.generators.generator_names()",
            generators.generator_names(),
        ),
        "_MIDI_ACTION_CHOICES": (
            "control.midi_control._ACTIONS",
            set(midi_control._ACTIONS),
        ),
        "_MIDI_CC_TYPE_CHOICES": (
            "control.midi_control._CC_TYPES",
            set(midi_control._CC_TYPES),
        ),
        "_MIDI_MMC_COMMAND_CHOICES": (
            "control.midi_control._MMC_COMMANDS",
            set(midi_control._MMC_COMMANDS),
        ),
        "_MIDI_VOICE_MODE_CHOICES": ("sid.midi_scene.VOICE_MODES", midi_scene.VOICE_MODES),
        "_MIDI_WAVEFORM_CHOICES": (
            "sid.midi_scene._WAVEFORM_BITS",
            set(midi_scene._WAVEFORM_BITS),
        ),
        "_PALETTE_MODE_CHOICES": ("video.modes.PALETTE_MODES", modes.PALETTE_MODES),
        "_PERSISTENCE_CHOICES": (
            "sid.voice_scope.PERSISTENCE_NAMES",
            voice_scope.PERSISTENCE_NAMES,
        ),
        "_STYLE_CHOICES": (
            "video.petscii_styles.STYLE_NAMES + (RANDOM_STYLE,)",
            ps.STYLE_NAMES + (ps.RANDOM_STYLE,),
        ),
        "_TIME_BASE_CHOICES": ("sid.voice_scope.TIME_BASE_NAMES", voice_scope.TIME_BASE_NAMES),
    }


def imported_choices() -> dict[str, tuple[str, object]]:
    """`name -> (source label, source object)` for the vocabularies config.py
    imports from their owning module instead of copying."""
    from c64cast.audio import dac_curves
    from c64cast.sid import sid_autoconfig
    from c64cast.video import palette

    return {
        "DAC_CURVE_CHOICES": (
            "audio.dac_curves.DAC_CURVE_CHOICES",
            dac_curves.DAC_CURVE_CHOICES,
        ),
        "HIRES_CELL_PICKS": (
            "video.palette.HIRES_CELL_PICKS",
            palette.HIRES_CELL_PICKS,
        ),
        "SID_MODEL_CHOICES": (
            "sid.sid_autoconfig.SID_MODEL_CHOICES",
            sid_autoconfig.SID_MODEL_CHOICES,
        ),
    }


def local_choices() -> dict[str, str]:
    """`name -> where the value is consumed` for the vocabularies config.py
    owns outright: no module outside it enumerates them, so there is nothing
    to compare against. The reason is the exemption — a new tuple has to be
    routed here or into one of the two tables above before the suite passes."""
    return {
        "AUDIO_BACKEND_CHOICES": "scene_factory.resolve_audio_backend branches on the value",
        "HOST_SID_CHIP_MODEL_CHOICES": "config._validate_host_sid_chips is the only consumer",
        "HOST_SID_MODEL_CHOICES": "hw.backend carries the value onto HardwareProfile",
        "HOST_SID_TUNE_MATCH_CHOICES": "sid.waveform's pool picker branches on the value",
        "SID_PLAY_RATE_CHOICES": "'auto'/'off' plus any rate in Hz — the union enumerates nothing",
        "SID_VIDEO_MODE_CHOICES": "hw_provision tests `!= 'off'`",
        "SYSTEM_CHOICES": "hw.backend and hw.hw_provision .upper() the value",
        "_APPLY_CHOICES": "introspect.FieldDoc.apply carries the value to the on-C64 menu",
        "_ASPECT_MODE_CHOICES": "scenes._apply_aspect branches on the value",
        "_AUDIO_SOURCE_CHOICES": "no registry backs the AudioSource family (pinned as a literal below)",
        "_CLIP_LAUNCH_CHOICES": "performance.PerformanceSession reads the per-clip value",
        "_CLIP_PAD_TYPE_CHOICES": "control.midi_control maps the grid pad by the value",
        "_CLIP_QUANTIZE_CHOICES": "performance.PerformanceSession reads the per-clip value",
        "_COLOR_MODE_CHOICES": "voice_scope validates against an inline pair",
        "_INPUT_SOURCE_CHOICES": "scenes.LauncherScene branches on the value",
        "_MIDI_FILTER_MODE_CHOICES": "midi_scene maps the value to filter bits inline",
        "_MOD_SOURCE_CHOICES": "effects.FrameEffect.mod_source takes the value",
        "_TEMPO_SOURCE_CHOICES": "control.tempo reads the value off [performance]",
        "_TR_STORAGE_CHOICES": "connect.py sets and hw.backend branches on the value",
        "_TR_TRANSPORT_CHOICES": "connect.py sets and hw.backend branches on the value",
    }


class ChoiceVocabSyncTest(unittest.TestCase):
    """config.py duplicates a few value lists to stay import-light. Assert
    every value vocabulary it exports (see `choices_tuple_names`) is routed —
    to the source it mirrors, to the source it re-exports, or to a recorded
    reason it has neither — and that each mirror still equals its source."""

    def test_every_choices_tuple_is_routed(self):
        routed = set(mirrored_choices()) | set(imported_choices()) | set(local_choices())
        exported = choices_tuple_names()
        self.assertEqual(
            exported - routed,
            set(),
            "unrouted config.py choices tuple(s): add them to mirrored_choices(), "
            "imported_choices() or local_choices() in this module",
        )
        self.assertEqual(
            routed - exported,
            set(),
            "routed name(s) config.py no longer exports",
        )

    def test_routing_is_a_partition(self):
        tables = (set(mirrored_choices()), set(imported_choices()), set(local_choices()))
        for i, a in enumerate(tables):
            for b in tables[i + 1 :]:
                self.assertEqual(a & b, set(), "a choices tuple is routed twice")

    def test_local_choices_record_a_reason(self):
        for name, reason in local_choices().items():
            self.assertTrue(reason.strip(), f"{name} is exempted with no reason")

    def test_mirrored_choices_match_their_source(self):
        for name, (label, expected) in mirrored_choices().items():
            with self.subTest(choices=name):
                got = getattr(cfgmod, name)
                if isinstance(expected, (set, frozenset)):
                    self.assertEqual(set(got), set(expected), f"{name} drifted from {label}")
                else:
                    self.assertEqual(got, expected, f"{name} drifted from {label}")

    def test_imported_choices_are_their_source_object(self):
        for name, (label, source) in imported_choices().items():
            with self.subTest(choices=name):
                self.assertIs(getattr(cfgmod, name), source, f"{name} is no longer {label}")

    def test_audio_source_choices_pinned(self):
        # No registry backs the AudioSource family, so pin the literal: a new
        # value must not reach SceneCfg metadata without build_scene learning
        # to construct it.
        self.assertEqual(cfgmod._AUDIO_SOURCE_CHOICES, ("none", "mic", "listen", "file", "sid"))
        # SceneCfg metadata must match the constant.
        from dataclasses import fields

        meta = {f.name: f for f in fields(cfgmod.SceneCfg)}["audio_source"].metadata
        self.assertEqual(meta["choices"], cfgmod._AUDIO_SOURCE_CHOICES)
        self.assertEqual(meta["applies_to"], ("generative",))

    def test_audio_backend_choices_pinned(self):
        # The video-audio backend selector is a fixed literal set, pinned so a
        # new value cannot reach AudioCfg.backend metadata without
        # resolve_audio_backend + build_scene honoring it.
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
        # MidiScene shares the bitmap oscilloscope, so the scope knobs
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
        # file + song surface for generative, used when audio_source = sid.
        self.assertIn("file", names)
        self.assertIn("song", names)

    def test_universal_fields_present_everywhere(self):
        for s in introspect.scene_types():
            names = {f.name for f in s.fields}
            self.assertIn("type", names, s.name)

    def test_overlays_is_offered_on_every_type_that_accepts_one(self):
        # `overlays` is universal except on `launcher`, where
        # scene_factory._validate_launcher hard-rejects it (the launched
        # program owns screen + color RAM), so offering it there would let
        # --describe, the wizard and the web console build a rejected scene.
        for s in introspect.scene_types():
            names = {f.name for f in s.fields}
            self.assertEqual("overlays" in names, s.name != "launcher", s.name)


class ReloadableSectionsTest(unittest.TestCase):
    """`RELOADABLE_SECTIONS` is what the web console reads to say, at the moment
    of saving, which changes a reload will apply and which need a restart. It is
    a claim about `session.reload_all`, so it has to name real sections and the
    docstring that describes the behavior has to point at it."""

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
    from, which is how a form knows to offer swatches for a color and a text
    box for everything else. `choices` cannot say it: these fields take an index
    as well, and a picker built from `choices` would refuse one."""

    def _scene_field(self, name: str) -> introspect.FieldDoc:
        fields = {f.name: f for st in introspect.scene_types() for f in st.fields}
        return fields[name]

    def test_the_color_fields_declare_it(self):
        for name in ("border", "background"):
            self.assertEqual(self._scene_field(name).vocabulary, "c64color", name)

    def test_a_field_that_is_not_a_color_does_not(self):
        # `[video].device` is `int | str` too, and its strings are camera names.
        fields = {f.name: f for s in introspect.config_sections() for f in s.fields}
        self.assertEqual(fields["device"].vocabulary, "")

    def test_it_reaches_the_document_the_console_renders(self):
        doc = introspect.as_dict()
        scene_fields = {f["name"]: f for st in doc["scene_types"] for f in st["fields"]}
        self.assertEqual(scene_fields["border"]["vocabulary"], "c64color")

    def test_file_declares_the_media_vocabulary(self):
        self.assertEqual(self._scene_field("file").vocabulary, "media")

    def test_every_field_whose_values_are_c64_color_names_declares_it(self):
        # Without it the console renders a free-text box, so a fuzzy-matchable
        # color name is typed blind and a typo surfaces at scene build.
        for name in ("border", "background", "voice_colors", "waveform_colors"):
            self.assertEqual(self._scene_field(name).vocabulary, "c64color", name)
        sections = {f.name: f for s in introspect.config_sections() for f in s.fields}
        for name in ("force_palette_colors", "text_color"):
            self.assertEqual(sections[name].vocabulary, "c64color", name)


class MetadataVocabularyTest(unittest.TestCase):
    """config.py's premise is that the field metadata is the single source of
    truth, which only holds while each key means one thing everywhere and
    something reads the values it declares."""

    def test_applies_to_names_scene_types_and_only_scene_types(self):
        # The key names three vocabularies across the tree — scene types on
        # SceneCfg, display modes on the ColorCfg flicker trio, a backend on
        # two Ultimate64Cfg fields — inert only while consumers iterate
        # SceneCfg.
        for f in dataclasses.fields(cfgmod.SceneCfg):
            for value in f.metadata.get("applies_to", ()):
                self.assertIn(value, cfgmod.SCENE_TYPES, f"{f.name}: {value}")

    def test_no_section_field_carries_applies_to(self):
        probe = cfgmod.Config()
        for name in (*cfgmod._TOML_SCALAR_SECTIONS, "color"):
            for f in dataclasses.fields(getattr(probe, name)):
                self.assertNotIn("applies_to", f.metadata, f"[{name}].{f.name}")

    def test_every_apply_value_is_a_declared_one(self):
        # introspect reads the key as md.get("apply", "rebuild"), so a
        # misspelling silently downgrades a live-tunable knob to read-only
        # with no error and no test failure.
        probe = cfgmod.Config()
        holders = [
            cfgmod.SceneCfg,
            *(type(getattr(probe, n)) for n in cfgmod._TOML_SCALAR_SECTIONS),
        ]
        seen = 0
        for dc in [*holders, cfgmod.ColorCfg]:
            for f in dataclasses.fields(dc):
                apply = f.metadata.get("apply")
                if apply is None:
                    continue
                seen += 1
                self.assertIn(apply, cfgmod._APPLY_CHOICES, f"{dc.__name__}.{f.name}")
        self.assertTrue(seen, "no field carries `apply` — has the key been renamed?")

    def test_the_cc_map_help_documents_every_action_it_accepts(self):
        # cc_map is a list[dict], so its help is the only surface --describe,
        # the schema and the wizard can show for the `action` vocabulary.
        help_text = {f.name: f for f in dataclasses.fields(cfgmod.MidiControlCfg)}[
            "cc_map"
        ].metadata["help"]
        for action in cfgmod._MIDI_ACTION_CHOICES:
            self.assertIn(repr(action), help_text, action)


class MediaKindTest(unittest.TestCase):
    """`SceneTypeDoc.media_kinds` says which media_store.py kind(s) a scene
    type's `file =` field browses. It can't live on the field itself — the
    same `file` field means videos on `video` and .sid files on `waveform` —
    so this is the drift guard that keeps its lookup table honest against the
    field's own `applies_to`."""

    def test_every_type_with_a_file_field_has_media_kinds(self):
        for st in introspect.scene_types():
            has_file = any(f.name == "file" for f in st.fields)
            self.assertEqual(bool(st.media_kinds), has_file, st.name)

    def test_a_type_with_no_file_field_has_no_media_kinds(self):
        webcam = next(st for st in introspect.scene_types() if st.name == "webcam")
        self.assertEqual(webcam.media_kinds, ())

    def test_it_reaches_the_document_the_console_renders(self):
        doc = introspect.as_dict()
        kinds = {st["name"]: st["media_kinds"] for st in doc["scene_types"]}
        self.assertEqual(kinds["video"], ["video"])
        self.assertEqual(kinds["generative"], ["sid", "audio"])


class PaletteSwatchTest(unittest.TestCase):
    """The swatch picker's colors are served rather than copied into the
    browser — a second palette to keep in step with the first is the bug this
    avoids."""

    def test_sixteen_colors_with_writable_names(self):
        from c64cast.video.palette import resolve_color

        swatches = introspect.palette_swatches()
        self.assertEqual(len(swatches), 16)
        for index, swatch in enumerate(swatches):
            self.assertEqual(swatch["index"], index)
            # Both spellings have to survive the loader, because the picker writes
            # one of them into a config.
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
