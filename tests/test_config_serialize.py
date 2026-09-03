"""Round-trip + behavior tests for config_serialize.

The contract is ``load(dumps(cfg)) == cfg``. The strongest enforcement is the
corpus test: every shipped example config is loaded, serialized, and reloaded,
and the two Configs must compare equal — which exercises every field type,
overlay, and scene type the project actually uses against a real TOML parse.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from _fakes import MachineSettingsIsolation

from c64cast import __version__
from c64cast.app import config as cfgmod
from c64cast.app import config_serialize as ser
from c64cast.app import introspect, paths

# The round-trip contract load(dumps(cfg)) == cfg must hold independent of any
# real machine-settings file on the dev's machine (config.load applies that
# layer). Isolate it for the whole module.
_settings_isolation = MachineSettingsIsolation()


def setUpModule() -> None:
    _settings_isolation.start()


def tearDownModule() -> None:
    _settings_isolation.stop()


def _reload(cfg: cfgmod.Config, **kwargs: object) -> cfgmod.Config:
    """dumps(cfg) → temp file → load() back into a fresh Config."""
    text = ser.dumps(cfg, **kwargs)  # type: ignore[arg-type]
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as f:
        f.write(text)
        path = f.name
    try:
        return cfgmod.load(path)
    finally:
        os.unlink(path)


class RoundTripDefaultsTest(unittest.TestCase):
    def test_defaults_round_trip(self):
        cfg = cfgmod.Config()
        self.assertEqual(_reload(cfg), cfg)

    def test_defaults_round_trip_non_minimal(self):
        cfg = cfgmod.Config()
        self.assertEqual(_reload(cfg, minimal=False), cfg)

    def test_defaults_round_trip_unannotated(self):
        cfg = cfgmod.Config()
        self.assertEqual(_reload(cfg, annotate=False), cfg)


class RoundTripCorpusTest(unittest.TestCase):
    """Every top-level example config must survive load → dumps → load."""

    def test_examples_round_trip(self):
        examples = paths.example_config_paths()
        self.assertTrue(examples, "no example configs found")
        for path in examples:
            with self.subTest(example=paths.example_name(path)):
                original = cfgmod.load(str(path))
                # Skip ensemble masters (not serializable; the packaged
                # `ensemble/` demo is one).
                if original.ensemble is not None:
                    continue
                self.assertEqual(_reload(original), original)
                # ...and in non-minimal / unannotated modes too.
                self.assertEqual(_reload(original, minimal=False), original)
                self.assertEqual(_reload(original, annotate=False), original)


class RoundTripTrickyFieldsTest(unittest.TestCase):
    """Hand-built configs that hit the awkward field shapes directly."""

    def test_waveform_lists_and_dicts(self):
        cfg = cfgmod.Config()
        cfg.scenes = [
            cfgmod.SceneCfg(
                type="waveform",
                file="assets/sids/x.sid",
                voice_colors=["red", "green", "light_blue"],
                waveform_colors={"pulse": "cyan", "sawtooth": "light_red"},
                color_mode="per_waveform",
                scroll_columns=[2, 0, 5],
                persistence="long",
            )
        ]
        self.assertEqual(_reload(cfg), cfg)

    def test_scene_with_overlays(self):
        cfg = cfgmod.Config()
        cfg.scenes = [
            cfgmod.SceneCfg(
                type="blank",
                display="blank",
                border=6,
                background=0,
                overlays=[
                    {"type": "clock", "corner": "top_right"},
                    {"type": "marquee", "text": 'hello "world"', "row": 24},
                ],
            )
        ]
        self.assertEqual(_reload(cfg), cfg)

    def test_color_hue_corrections(self):
        cfg = cfgmod.Config()
        cfg.color.channel_boost = [1.4, 1.1, 0.95]
        cfg.color.hue_corrections = [
            {
                "name": "test",
                "hue_lo_deg": 250,
                "hue_hi_deg": 280,
                "hue_target_deg": 300,
                "sat_mult": 1.5,
            },
        ]
        cfg.color.hue_corrections_replace_defaults = True
        self.assertEqual(_reload(cfg), cfg)

    def test_scene_color_override(self):
        cfg = cfgmod.Config()
        cfg.color.dither = "blue_noise"
        cfg.scenes = [
            cfgmod.SceneCfg(
                type="video",
                file="clip.mp4",
                color={
                    "dither": "floyd-steinberg",
                    "force_palette": True,
                    "force_palette_colors": 8,
                    "hue_corrections": [{"name": "scene_band", "hue_lo_deg": 10}],
                },
            )
        ]
        reloaded = _reload(cfg)
        self.assertEqual(reloaded, cfg)
        # The global section is untouched by the scene's override.
        self.assertEqual(reloaded.color.dither, "blue_noise")

    def test_scene_color_override_with_empty_hue_corrections(self):
        # `color` is the scene's sparse dict, so `{"hue_corrections": []}`
        # is an authored key distinct from "no override" and has to survive
        # as such, not collapse to `{}` the way a bare `[scenes.color]`
        # header with nothing under it would reload.
        cfg = cfgmod.Config()
        cfg.scenes = [cfgmod.SceneCfg(type="video", file="clip.mp4", color={"hue_corrections": []})]
        reloaded = _reload(cfg)
        self.assertEqual(reloaded, cfg)
        self.assertEqual(reloaded.scenes[0].color, {"hue_corrections": []})

    def test_scene_field_not_applicable_to_current_type_still_round_trips(self):
        # `applies_to` is enforced only by introspect's rendering (schema,
        # describe, and dumps' own per-type field list) — never by the
        # loader — so a value set while the scene was a different type (or
        # by a structured edit) has to survive a re-serialize even though
        # the current `type` doesn't claim the field.
        cfg = cfgmod.Config()
        cfg.scenes = [cfgmod.SceneCfg(type="video", file="clip.mp4", image_duration_s=3.0)]
        reloaded = _reload(cfg)
        self.assertEqual(reloaded, cfg)
        self.assertEqual(reloaded.scenes[0].image_duration_s, 3.0)

    def test_scene_color_override_back_to_the_dataclass_default(self):
        # The case the sparse-dict design exists for: a scene override equal
        # to ColorCfg()'s default, while the global section differs from it —
        # both keys must round-trip, or "minimal" would drop the override as
        # if it were unauthored.
        cfg = cfgmod.Config()
        cfg.color.force_palette = True
        cfg.scenes = [
            cfgmod.SceneCfg(type="video", file="clip.mp4", color={"force_palette": False})
        ]
        reloaded = _reload(cfg)
        self.assertEqual(reloaded, cfg)
        self.assertTrue(reloaded.color.force_palette)
        self.assertEqual(reloaded.scenes[0].color, {"force_palette": False})

    def test_per_scene_audio_false(self):
        cfg = cfgmod.Config()
        cfg.scenes = [cfgmod.SceneCfg(type="webcam", audio=False)]
        reloaded = _reload(cfg)
        self.assertEqual(reloaded, cfg)
        self.assertIs(reloaded.scenes[0].audio, False)

    def test_dac_bitmap_tempo_non_default(self):
        cfg = cfgmod.Config()
        cfg.audio.dac_bitmap_tempo_hires = 0.91
        cfg.audio.dac_bitmap_tempo_mhires = 0.86
        reloaded = _reload(cfg)
        self.assertEqual(reloaded, cfg)
        self.assertEqual(reloaded.audio.dac_bitmap_tempo_hires, 0.91)
        self.assertEqual(reloaded.audio.dac_bitmap_tempo_mhires, 0.86)

    def test_string_escaping(self):
        cfg = cfgmod.Config()
        cfg.scenes = [
            cfgmod.SceneCfg(
                type="blank",
                display="blank",
                overlays=[{"type": "callsign", "text": 'tab\there "quote" back\\slash'}],
            )
        ]
        self.assertEqual(_reload(cfg), cfg)


class BehaviorTest(unittest.TestCase):
    def test_secret_never_emitted(self):
        cfg = cfgmod.Config()
        cfg.ultimate64.dma_password = "hunter2"
        text = ser.dumps(cfg)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("dma_password", text)

    def test_web_and_control_tokens_never_emitted(self):
        # Each grants remote control of the host the same way the DMA
        # password does — see SECRET_FIELDS's docstring.
        cfg = cfgmod.Config()
        cfg.web.token = "watchme"
        cfg.web.viewer_token = "peekaboo"
        cfg.control.token = "controlme"
        cfg.control.viewer_token = "peekaboo2"
        text = ser.dumps(cfg)
        for secret in ("watchme", "peekaboo", "controlme", "peekaboo2"):
            self.assertNotIn(secret, text)

    def test_schema_directive_first_line(self):
        cfg = cfgmod.Config()
        first = ser.dumps(cfg).splitlines()[0]
        self.assertEqual(first, f"#:schema {ser.DEFAULT_SCHEMA_PATH}")

    def test_schema_directive_omittable(self):
        cfg = cfgmod.Config()
        text = ser.dumps(cfg, schema_path=None)
        self.assertNotIn("#:schema", text)

    def test_custom_schema_path(self):
        cfg = cfgmod.Config()
        text = ser.dumps(cfg, schema_path="../data/c64cast.schema.json")
        self.assertEqual(text.splitlines()[0], "#:schema ../data/c64cast.schema.json")

    def test_minimal_omits_defaults(self):
        cfg = cfgmod.Config()
        text = ser.dumps(cfg)  # minimal=True default
        # Pure defaults → no section bodies at all (just the directive).
        self.assertNotIn("[audio]", text)
        self.assertNotIn("enabled = false", text)

    def test_non_minimal_writes_sections(self):
        cfg = cfgmod.Config()
        text = ser.dumps(cfg, minimal=False)
        self.assertIn("[audio]", text)
        self.assertIn("enabled = false", text)

    def test_annotate_adds_comments(self):
        cfg = cfgmod.Config()
        cfg.audio.enabled = False  # non-default (audio defaults on) so it emits
        with_comments = ser.dumps(cfg, annotate=True)
        without = ser.dumps(cfg, annotate=False)
        self.assertIn("#", with_comments)
        # The bare form still parses and only carries the directive comment.
        self.assertIn("enabled = false", without)

    def test_type_always_emitted_even_when_default(self):
        cfg = cfgmod.Config()
        cfg.scenes = [cfgmod.SceneCfg(type="webcam")]  # webcam is the default
        text = ser.dumps(cfg)
        self.assertIn('type = "webcam"', text)

    def test_output_is_valid_toml(self):
        cfg = cfgmod.Config()
        cfg.audio.enabled = True
        cfg.scenes = [cfgmod.SceneCfg(type="webcam", display="petscii")]
        tomllib.loads(ser.dumps(cfg))  # raises on malformed output

    def test_ensemble_master_rejected(self):
        cfg = cfgmod.Config()
        cfg.ensemble = cfgmod.EnsembleCfg(
            systems=[cfgmod.SystemEntryCfg(name="left", config="left.toml")]
        )
        with self.assertRaises(ser.SerializeError):
            ser.dumps(cfg)

    def test_non_finite_float_rejected(self):
        cfg = cfgmod.Config()
        cfg.scenes = [cfgmod.SceneCfg(type="webcam", duration_s=float("inf"))]
        with self.assertRaises(ser.SerializeError):
            ser.dumps(cfg)


class BaselineTest(unittest.TestCase):
    """`minimal` measures against the caller's baseline, so a save-back does not
    write this machine's settings into a show config. The three save-back paths
    (the web console's form, `--init`, the on-C64 live-tune save) all serialize a
    Config the loader built on the machine layer; the baseline is how that layer
    stays where it was authored."""

    def _machine(self, **video: object) -> cfgmod.Config:
        """A stand-in for `config.machine_baseline()` — the settings file's
        effect on a Config, without a settings file."""
        base = cfgmod.Config()
        for key, value in video.items():
            setattr(base.video, key, value)
        return base

    def test_value_inherited_from_the_baseline_is_not_written(self):
        baseline = self._machine(device=3)
        cfg = self._machine(device=3)  # the loader's result: file said nothing
        self.assertNotIn("device", ser.dumps(cfg, baseline=baseline))

    def test_the_same_value_is_written_without_a_baseline(self):
        # The bug the baseline fixes, kept as a test: measured against the
        # dataclass defaults, a machine setting lands in the file.
        cfg = self._machine(device=3)
        self.assertIn("device = 3", ser.dumps(cfg))

    def test_a_value_the_file_sets_is_still_written(self):
        baseline = self._machine(device=3)
        cfg = self._machine(device=3)
        cfg.video.device = 5  # what this file actually says
        self.assertIn("device = 5", ser.dumps(cfg, baseline=baseline))

    def test_a_value_back_at_the_dataclass_default_is_written(self):
        # Overriding a machine setting *with* the shipped default is a real
        # answer, and the only way to record it is to write it out.
        baseline = self._machine(device=3)
        cfg = self._machine(device=3)
        cfg.video.device = cfgmod.VideoCfg().device
        self.assertIn(f"device = {cfgmod.VideoCfg().device}", ser.dumps(cfg, baseline=baseline))

    def test_scenes_ignore_the_baseline(self):
        # Machine settings hold no playlist, so there is no layer under a scene.
        baseline = self._machine(device=3)
        cfg = self._machine(device=3)
        cfg.scenes = [cfgmod.SceneCfg(type="blank", duration_s=9.0)]
        self.assertIn("duration_s = 9.0", ser.dumps(cfg, baseline=baseline))

    def test_a_list_field_already_in_the_baseline_is_not_rewritten(self):
        rows = [{"from": 10, "to": 20}]
        baseline = cfgmod.Config()
        baseline.color.hue_corrections = list(rows)
        cfg = cfgmod.Config()
        cfg.color.hue_corrections = list(rows)
        self.assertNotIn("hue_corrections", ser.dumps(cfg, baseline=baseline))

    def test_a_list_field_the_file_extends_is_written_whole(self):
        baseline = cfgmod.Config()
        baseline.color.hue_corrections = [{"from": 10, "to": 20}]
        cfg = cfgmod.Config()
        cfg.color.hue_corrections = [{"from": 10, "to": 20}, {"from": 30, "to": 40}]
        text = ser.dumps(cfg, baseline=baseline)
        self.assertEqual(text.count("[[color.hue_corrections]]"), 2)

    def test_round_trip_holds_over_a_baseline(self):
        # The contract survives the change *because* the loader re-applies the
        # same layer: what the file omits, the baseline puts back.
        baseline = self._machine(device=3)
        cfg = self._machine(device=3)
        cfg.audio.enabled = False
        reloaded = _reload(cfg, baseline=baseline)
        reloaded.video.device = baseline.video.device  # the layer, re-applied
        self.assertEqual(reloaded, cfg)


class MachineBaselineTest(unittest.TestCase):
    """`config.machine_baseline()` itself — the settings file as a Config."""

    def test_reads_the_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = os.path.join(tmp, "settings.toml")
            with open(settings, "w", encoding="utf-8") as f:
                f.write("[video]\ndevice = 7\n")
            with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": settings}):
                self.assertEqual(cfgmod.machine_baseline().video.device, 7)

    def test_a_missing_file_is_the_dataclass_defaults(self):
        self.assertEqual(cfgmod.machine_baseline(), cfgmod.Config())

    def test_each_call_is_a_fresh_instance(self):
        # Callers mutate what they are handed (the wizard builds onto it).
        first = cfgmod.machine_baseline()
        first.video.device = 99
        self.assertNotEqual(cfgmod.machine_baseline().video.device, 99)


class SchemaDirectiveTest(unittest.TestCase):
    def test_points_at_the_packaged_schema(self):
        # Whatever form it takes, the directive must resolve to the real file
        # from the output config's own directory — that is the whole contract,
        # and it is what makes the line survive an upgrade: the file it names is
        # the one the next version rewrites.
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "c64cast.toml")
            directive = ser.schema_directive_for(out)
            resolved = os.path.normpath(os.path.join(d, directive))
            self.assertTrue(os.path.isfile(resolved), f"{directive!r} → {resolved}")
            self.assertEqual(
                os.path.realpath(resolved), os.path.realpath(paths.packaged_schema_path())
            )

    def test_relative_when_the_schema_is_inside_the_output_tree(self):
        # A source checkout (config at the repo root) or a project-local
        # .venv — the relative form survives moving the tree.
        pkg_parent = str(paths.packaged_schema_path().parent.parent.parent)
        directive = ser.schema_directive_for(os.path.join(pkg_parent, "c64cast.toml"))
        self.assertEqual(directive, os.path.join(".", "c64cast", "data", "c64cast.schema.json"))

    def test_absolute_as_soon_as_it_would_need_to_climb(self):
        # A user-level install: the relative form is an unreadable climb out to
        # site-packages and breaks when the config moves, so go absolute.
        with tempfile.TemporaryDirectory() as d:
            directive = ser.schema_directive_for(os.path.join(d, "c64cast.toml"))
            self.assertEqual(directive, str(paths.packaged_schema_path()))

    def test_falls_back_when_no_schema(self):
        with mock.patch.object(paths, "packaged_schema_path", return_value=Path("/nope/x.json")):
            self.assertEqual(ser.schema_directive_for("x.toml"), ser.DEFAULT_SCHEMA_PATH)

    def test_never_a_moving_ref(self):
        # The URL fallback is pinned on purpose: a schema newer than the program
        # stops flagging real mistakes and starts offering keys this install
        # rejects. Only an unreleased version may point at a branch.
        self.assertIn("/v1.2.3/", ser._published_schema_url("1.2.3"))
        self.assertIn("/main/", ser._published_schema_url("unreleased"))


class PinnedUrlVersionTest(unittest.TestCase):
    def test_a_published_url_reads_back(self):
        # Pins the regex to the template it has to match — they are spelled
        # separately for readability, so nothing but this catches a change to
        # one and not the other.
        for version in ("0.1.0", "1.2.3", "2.0.0rc1"):
            self.assertEqual(ser.pinned_url_version(ser._published_schema_url(version)), version)

    def test_this_install_reads_back_as_its_own_version(self):
        pinned = ser.pinned_url_version(ser.DEFAULT_SCHEMA_PATH)
        self.assertIn(pinned, (__version__, None), "released → pinned; unreleased → main, so None")

    def test_a_local_path_is_not_a_pin(self):
        self.assertIsNone(ser.pinned_url_version("./c64cast/data/c64cast.schema.json"))
        self.assertIsNone(ser.pinned_url_version(str(paths.packaged_schema_path())))

    def test_somebody_elses_url_is_not_a_pin(self):
        # A fork or a mirror carries no promise about which c64cast it
        # describes, so it must not be read as one of our version pins.
        self.assertIsNone(
            ser.pinned_url_version(
                "https://raw.githubusercontent.com/someone/c64cast-fork/v9.9.9"
                "/c64cast/data/c64cast.schema.json"
            )
        )
        self.assertIsNone(ser.pinned_url_version(ser._published_schema_url("1.0.0") + "?raw=1"))


class TableArrayRoutingTest(unittest.TestCase):
    """`_SECTION_TABLE_ARRAYS` is the one place `_emit_section` decides which
    fields render as `[[section.field]]` blocks. A `list[dict[...]]` section
    field missing from it would fall through to `_fmt_value`, which renders
    it as a legal-but-wrong inline array of inline tables — round-trippable,
    so nothing else catches the mistake."""

    def test_every_list_of_table_section_field_is_routed(self):
        for sd in introspect.config_sections():
            for fd in sd.fields:
                if fd.type.startswith("list[dict"):
                    self.assertIn(
                        sd.name,
                        ser._SECTION_TABLE_ARRAYS,
                        f"{sd.name}.{fd.name} is list-of-tables with no routing entry",
                    )
                    self.assertEqual(ser._SECTION_TABLE_ARRAYS[sd.name][0], fd.name)


if __name__ == "__main__":
    unittest.main()
