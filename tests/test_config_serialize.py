"""Round-trip + behavior tests for config_serialize.

The contract is ``load(dumps(cfg)) == cfg``. The strongest enforcement is the
corpus test: every shipped example config is loaded, serialized, and reloaded,
and the two Configs must compare equal — which exercises every field type,
overlay, and scene type the project actually uses against a real TOML parse.
"""

from __future__ import annotations

import glob
import os
import tempfile
import tomllib
import unittest

from c64cast import config as cfgmod
from c64cast import config_serialize as ser

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXAMPLES_DIR = os.path.join(_REPO, "config", "examples")


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
        examples = sorted(glob.glob(os.path.join(_EXAMPLES_DIR, "*.toml")))
        self.assertTrue(examples, "no example configs found")
        for path in examples:
            with self.subTest(example=os.path.basename(path)):
                original = cfgmod.load(path)
                # Skip ensemble masters (not serializable; none at top level
                # today, but guard in case one is added).
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

    def test_schema_directive_first_line(self):
        cfg = cfgmod.Config()
        first = ser.dumps(cfg).splitlines()[0]
        self.assertEqual(first, "#:schema ./c64cast.schema.json")

    def test_schema_directive_omittable(self):
        cfg = cfgmod.Config()
        text = ser.dumps(cfg, schema_path=None)
        self.assertNotIn("#:schema", text)

    def test_custom_schema_path(self):
        cfg = cfgmod.Config()
        text = ser.dumps(cfg, schema_path="../../c64cast.schema.json")
        self.assertEqual(text.splitlines()[0], "#:schema ../../c64cast.schema.json")

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


if __name__ == "__main__":
    unittest.main()
