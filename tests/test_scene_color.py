"""Per-scene [color] overrides: config.scene_color's resolution, the load-time
validation of a [scenes.color] table, and scene_factory's per-scene guard
extension (validate_dither_cfg et al. / validate_scene_cfg's type rejection).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory


@contextmanager
def _loaded(toml: str):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(toml)
        path = f.name
    try:
        yield cfgmod.load(path)
    finally:
        os.unlink(path)


class SceneColorResolutionTest(unittest.TestCase):
    def test_no_override_returns_the_global_section_itself(self):
        cfg = cfgmod.Config()
        s = cfgmod.SceneCfg(type="video")
        self.assertIs(cfgmod.scene_color(cfg, s), cfg.color)

    def test_an_override_wins_over_the_global_value(self):
        cfg = cfgmod.Config()
        cfg.color.dither = "blue_noise"
        s = cfgmod.SceneCfg(type="video", color={"dither": "floyd-steinberg"})
        self.assertEqual(cfgmod.scene_color(cfg, s).dither, "floyd-steinberg")
        # The global section itself is untouched.
        self.assertEqual(cfg.color.dither, "blue_noise")

    def test_an_override_back_to_the_dataclass_default_wins(self):
        # The case that motivates storing authored keys rather than a merged
        # ColorCfg: a "differs from ColorCfg()" merge would treat this as
        # unauthored and let the non-default global win instead.
        cfg = cfgmod.Config()
        cfg.color.force_palette = True
        self.assertNotEqual(cfg.color.force_palette, cfgmod.ColorCfg().force_palette)
        s = cfgmod.SceneCfg(type="video", color={"force_palette": False})
        self.assertFalse(cfgmod.scene_color(cfg, s).force_palette)

    def test_unset_fields_still_follow_the_global(self):
        cfg = cfgmod.Config()
        cfg.color.motion_smoothing = 0.9
        s = cfgmod.SceneCfg(type="video", color={"dither": "none"})
        effective = cfgmod.scene_color(cfg, s)
        self.assertEqual(effective.dither, "none")
        self.assertEqual(effective.motion_smoothing, 0.9)

    def test_hue_corrections_replaces_rather_than_extends(self):
        cfg = cfgmod.Config()
        cfg.color.hue_corrections = [{"name": "global_band"}]
        s = cfgmod.SceneCfg(type="video", color={"hue_corrections": [{"name": "scene_band"}]})
        effective = cfgmod.scene_color(cfg, s)
        self.assertEqual(effective.hue_corrections, [{"name": "scene_band"}])

    def test_force_palette_colors_is_normalized_on_the_override(self):
        # _validate_force_palette runs again on the merged copy, so a scene
        # override using color names normalizes to indices same as [color].
        cfg = cfgmod.Config()
        s = cfgmod.SceneCfg(type="video", color={"force_palette_colors": ["black", "white"]})
        effective = cfgmod.scene_color(cfg, s)
        self.assertEqual(effective.force_palette_colors, [0, 1])


class SceneColorLoadTest(unittest.TestCase):
    def test_scalar_override_loads(self):
        toml = """
[[scenes]]
type = "video"
file = "clip.mp4"
[scenes.color]
dither = "floyd-steinberg"
force_palette = true
force_palette_colors = 8
"""
        with _loaded(toml) as cfg:
            self.assertEqual(
                cfg.scenes[0].color,
                {"dither": "floyd-steinberg", "force_palette": True, "force_palette_colors": 8},
            )

    def test_hue_corrections_subtable_loads(self):
        toml = """
[[scenes]]
type = "video"
file = "clip.mp4"
[scenes.color]
dither = "none"
[[scenes.color.hue_corrections]]
name = "test_band"
hue_lo_deg = 10
hue_hi_deg = 20
"""
        with _loaded(toml) as cfg:
            self.assertEqual(
                cfg.scenes[0].color["hue_corrections"],
                [{"name": "test_band", "hue_lo_deg": 10, "hue_hi_deg": 20}],
            )

    def test_no_color_table_leaves_the_dict_empty(self):
        toml = '[[scenes]]\ntype = "video"\nfile = "clip.mp4"\n'
        with _loaded(toml) as cfg:
            self.assertEqual(cfg.scenes[0].color, {})

    def test_unknown_key_logs_a_warning_naming_scenes_color(self):
        toml = """
[[scenes]]
type = "video"
file = "clip.mp4"
[scenes.color]
dithre = "none"
"""
        with self.assertLogs("c64cast.app.config", level="WARNING") as logs:
            with _loaded(toml):
                pass
        self.assertTrue(any("scenes.color" in r.getMessage() for r in logs.records))
        self.assertTrue(any("dithre" in r.getMessage() for r in logs.records))

    def test_non_table_color_raises(self):
        toml = """
[[scenes]]
type = "video"
file = "clip.mp4"
color = "oops"
"""
        with self.assertRaises(ValueError):
            with _loaded(toml):
                pass


class SceneColorAppliesToTest(unittest.TestCase):
    """[scenes.color] only means anything on a scene that paints a frame —
    the same set `effect`/`effects` are scoped to."""

    def test_frame_bearing_types_accept_it(self):
        # video/slideshow need an explicit `file` — without one they fall
        # back to scanning assets/videos or assets/pictures, which is empty
        # on a fresh checkout (CI has no sample media, unlike a dev machine).
        files = {"video": "clip.mp4", "slideshow": "pic.jpg"}
        for scene_type in ("webcam", "video", "slideshow", "generative", "wled"):
            with self.subTest(scene_type=scene_type):
                s = cfgmod.SceneCfg(
                    type=scene_type, color={"dither": "none"}, file=files.get(scene_type)
                )
                scene_factory.validate_scene_cfg(s, cfgmod.Config(), audio_enabled=False)

    def test_non_frame_bearing_types_reject_it(self):
        for scene_type in ("waveform", "midi", "asid", "blank"):
            with self.subTest(scene_type=scene_type):
                s = cfgmod.SceneCfg(type=scene_type, color={"dither": "none"})
                with self.assertRaisesRegex(ValueError, "color is not supported"):
                    scene_factory.validate_scene_cfg(s, cfgmod.Config(), audio_enabled=False)

    def test_launcher_rejects_it_too(self):
        s = cfgmod.SceneCfg(type="launcher", file="game.prg", color={"dither": "none"})
        with self.assertRaisesRegex(ValueError, "color is not supported"):
            scene_factory.validate_scene_cfg(s, cfgmod.Config(), audio_enabled=False)


class EffectiveColorsValidationTest(unittest.TestCase):
    """The four validate_*_cfg guards loop the global section plus every
    scene override (scene_factory.effective_colors) rather than just [color]."""

    def test_bad_global_dither_still_raises(self):
        cfg = cfgmod.Config()
        cfg.color.dither = "bogus"
        with self.assertRaisesRegex(cfgmod.ConfigError, r"\[color\]\.dither"):
            scene_factory.validate_dither_cfg(cfg)

    def test_bad_scene_override_raises_naming_the_scene(self):
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="video", color={"dither": "bogus"}))
        with self.assertRaisesRegex(cfgmod.ConfigError, r"\[\[scenes\]\]\[0\]\.color\.dither"):
            scene_factory.validate_dither_cfg(cfg)

    def test_a_scene_with_no_override_is_not_revalidated(self):
        # Only scenes carrying an override are checked separately; a plain
        # scene is covered by the global check alone.
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="video"))
        scene_factory.validate_dither_cfg(cfg)  # does not raise

    def test_bad_scene_color_match_override_raises(self):
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="video", color={"color_match": "lab"}))
        with self.assertRaisesRegex(cfgmod.ConfigError, r"color_match"):
            scene_factory.validate_color_match_cfg(cfg)

    def test_bad_scene_cell_strategy_override_raises(self):
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="video", color={"cell_strategy": "median"}))
        with self.assertRaisesRegex(cfgmod.ConfigError, r"cell_strategy"):
            scene_factory.validate_cell_strategy_cfg(cfg)

    def test_bad_scene_motion_smoothing_override_raises(self):
        cfg = cfgmod.Config()
        cfg.scenes.append(cfgmod.SceneCfg(type="video", color={"motion_smoothing": 5.0}))
        with self.assertRaisesRegex(cfgmod.ConfigError, r"motion_smoothing"):
            scene_factory.validate_motion_smoothing_cfg(cfg)


if __name__ == "__main__":
    unittest.main()
