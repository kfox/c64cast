"""Smoke tests for c64cast.app.config — loader, defaults, CLI merge."""

# pyright: reportArgumentType=false
from __future__ import annotations

import argparse
import dataclasses
import math
import os
import tempfile
import unittest
from typing import cast
from unittest import mock

from _fakes import FakeAPI, MachineSettingsIsolation

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory
from c64cast.hw.backend import C64Backend
from c64cast.video.modes import BlankDisplayMode

# Tests here assert config defaults / precedence; isolate the module from any
# real ~/.config/c64cast/settings.toml on the dev machine (config.load applies
# the machine-settings layer). Tests that need their own settings file override
# $C64CAST_SETTINGS locally, which nests cleanly under this.
_settings_isolation = MachineSettingsIsolation()


def setUpModule() -> None:
    _settings_isolation.start()


def tearDownModule() -> None:
    _settings_isolation.stop()


class ConfigLoaderTest(unittest.TestCase):
    def test_load_none_returns_defaults_when_no_file(self):
        # Use a temp dir as cwd so the default-path lookup misses.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                cfg = cfgmod.load(None)
            finally:
                os.chdir(cwd)
        self.assertEqual(cfg.ultimate64.url, "http://192.168.2.64")
        self.assertEqual(cfg.audio.enabled, True)
        self.assertEqual(cfg.scenes, [])

    def test_load_path_parses_sections(self):
        toml = """
[ultimate64]
url = "http://example.local"
system = "PAL"

[audio]
enabled = true
sample_rate = 11025

[interstitial]
duration_s = 7.5
text_color = "yellow"

[[scenes]]
type = "webcam"
display = "petscii"
duration_s = 15.0

  [[scenes.overlays]]
  type = "scrolling_text"
  row = 22
  messages = [
    { text = "HELLO", color = "yellow" },
  ]

  [[scenes.overlays]]
  type = "clock"
  corner = "top-right"
"""
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            cfg = cfgmod.load(path)
        finally:
            os.unlink(path)

        self.assertEqual(cfg.ultimate64.url, "http://example.local")
        self.assertEqual(cfg.ultimate64.system, "PAL")
        self.assertTrue(cfg.audio.enabled)
        self.assertEqual(cfg.audio.sample_rate, 11025)
        self.assertEqual(cfg.interstitial.duration_s, 7.5)
        self.assertEqual(cfg.interstitial.text_color, "yellow")
        self.assertEqual(len(cfg.scenes), 1)
        self.assertEqual(cfg.scenes[0].type, "webcam")
        self.assertEqual(cfg.scenes[0].display, "petscii")
        self.assertEqual(len(cfg.scenes[0].overlays), 2)
        self.assertEqual(cfg.scenes[0].overlays[0]["type"], "scrolling_text")
        self.assertEqual(cfg.scenes[0].overlays[0]["row"], 22)
        self.assertEqual(cfg.scenes[0].overlays[1]["type"], "clock")


class ColorSectionTest(unittest.TestCase):
    def _load(self, toml):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            return cfgmod.load(path)
        finally:
            os.unlink(path)

    def test_default_palette_mode_is_percell(self):
        self.assertEqual(cfgmod.SceneCfg().palette_mode, "percell")
        self.assertEqual(cfgmod.Config().color.channel_boost, [])
        self.assertEqual(cfgmod.Config().color.hue_corrections, [])
        self.assertFalse(cfgmod.Config().color.hue_corrections_replace_defaults)

    def test_dither_defaults(self):
        c = cfgmod.Config().color
        self.assertEqual(c.dither, "auto")
        self.assertEqual(c.dither_strength, 0.5)

    def test_color_section_parses_dither(self):
        cfg = self._load('[color]\ndither = "ordered"\ndither_strength = 1.25\n')
        self.assertEqual(cfg.color.dither, "ordered")
        self.assertEqual(cfg.color.dither_strength, 1.25)

    def test_color_section_parses_channel_boost(self):
        cfg = self._load("[color]\nchannel_boost = [1.1, 1.2, 1.3]\n")
        self.assertEqual(cfg.color.channel_boost, [1.1, 1.2, 1.3])

    def test_auto_fit_defaults_on(self):
        self.assertTrue(cfgmod.Config().color.auto_fit)
        self.assertEqual(cfgmod.Config().color.auto_fit_strength, 1.0)

    def test_color_section_parses_auto_fit(self):
        cfg = self._load("[color]\nauto_fit = false\nauto_fit_strength = 0.5\n")
        self.assertFalse(cfg.color.auto_fit)
        self.assertEqual(cfg.color.auto_fit_strength, 0.5)

    def test_color_section_parses_hue_corrections(self):
        cfg = self._load("""
[color]
hue_corrections_replace_defaults = true

[[color.hue_corrections]]
name = "orange_pop"
hue_lo_deg = 20
hue_hi_deg = 45
sat_mult = 1.4

[[color.hue_corrections]]
name = "teal"
hue_lo_deg = 170
hue_hi_deg = 195
""")
        self.assertTrue(cfg.color.hue_corrections_replace_defaults)
        self.assertEqual(len(cfg.color.hue_corrections), 2)
        self.assertEqual(cfg.color.hue_corrections[0]["name"], "orange_pop")
        self.assertEqual(cfg.color.hue_corrections[1]["hue_lo_deg"], 170)

    def test_color_unknown_scalar_key_is_dropped(self):
        # Unknown scalar keys under [color] go through _apply_section, which
        # warns and drops them (same as other sections) rather than raising.
        # assertLogs both verifies the warning fires and keeps it off the
        # console (an expected message, not a real failure).
        with self.assertLogs("c64cast.app.config", level="WARNING") as cm:
            cfg = self._load("[color]\nbogus_key = 7\n")
        self.assertFalse(hasattr(cfg.color, "bogus_key"))
        self.assertTrue(any("bogus_key" in m for m in cm.output))

    def test_force_palette_defaults_off(self):
        c = cfgmod.Config().color
        self.assertFalse(c.force_palette)
        self.assertEqual(c.force_palette_colors, 16)
        self.assertEqual(cfgmod.resolved_force_palette(c), (16, None))

    def test_force_palette_colors_int_count(self):
        cfg = self._load("[color]\nforce_palette = true\nforce_palette_colors = 8\n")
        self.assertTrue(cfg.color.force_palette)
        self.assertEqual(cfg.color.force_palette_colors, 8)
        self.assertEqual(cfgmod.resolved_force_palette(cfg.color), (8, None))

    def test_force_palette_colors_index_list(self):
        cfg = self._load("[color]\nforce_palette_colors = [0, 2, 6]\n")
        self.assertEqual(cfg.color.force_palette_colors, [0, 2, 6])
        self.assertEqual(cfgmod.resolved_force_palette(cfg.color), (3, [0, 2, 6]))

    def test_force_palette_colors_name_list_normalizes_to_ints(self):
        # Names (fuzzy + case-insensitive) and indices may be mixed; the loader
        # canonicalizes the whole list to palette indices.
        cfg = self._load('[color]\nforce_palette_colors = ["black", "RED", "lgrn", 14]\n')
        self.assertEqual(cfg.color.force_palette_colors, [0, 2, 13, 14])
        self.assertEqual(cfgmod.resolved_force_palette(cfg.color), (4, [0, 2, 13, 14]))

    def test_force_palette_colors_out_of_range_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load("[color]\nforce_palette_colors = 1\n")
        self.assertIn("force_palette_colors", str(ctx.exception))

    def test_force_palette_colors_short_list_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load("[color]\nforce_palette_colors = [0]\n")
        self.assertIn("force_palette_colors", str(ctx.exception))

    def test_force_palette_colors_bad_index_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load("[color]\nforce_palette_colors = [0, 99]\n")
        self.assertIn("force_palette_colors", str(ctx.exception))

    def test_force_palette_colors_unknown_name_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load('[color]\nforce_palette_colors = ["black", "chartreuse"]\n')
        self.assertIn("chartreuse", str(ctx.exception))

    def test_force_palette_indices_now_unknown_key(self):
        # The old field was removed; a config still using it should warn (and be
        # dropped) rather than silently take effect.
        with self.assertLogs("c64cast.app.config", level="WARNING") as cm:
            cfg = self._load("[color]\nforce_palette_indices = [0, 2]\n")
        self.assertFalse(hasattr(cfg.color, "force_palette_indices"))
        self.assertTrue(any("force_palette_indices" in m for m in cm.output))

    def test_scene_border_background_accept_names(self):
        # border/background take a fuzzy color name or an index; the name is
        # preserved in the SceneCfg and resolved to an index when the display
        # mode is built.
        cfg = self._load(
            '[[scenes]]\ntype = "blank"\ndisplay = "blank"\n'
            'border = "light blue"\nbackground = "blk"\n'
        )
        s = cfg.scenes[0]
        self.assertEqual(s.border, "light blue")
        self.assertEqual(s.background, "blk")
        dm = scene_factory._validate_blank(s, cfg)
        assert isinstance(dm, BlankDisplayMode)
        self.assertEqual(dm.border, 14)
        self.assertEqual(dm.background, 0)

    def test_scene_border_index_still_works(self):
        cfg = self._load('[[scenes]]\ntype = "blank"\ndisplay = "blank"\nborder = 6\n')
        dm = scene_factory._validate_blank(cfg.scenes[0], cfg)
        assert isinstance(dm, BlankDisplayMode)
        self.assertEqual(dm.border, 6)

    def test_scene_border_unknown_name_raises_at_build(self):
        cfg = self._load('[[scenes]]\ntype = "blank"\ndisplay = "blank"\nborder = "chartreuse"\n')
        with self.assertRaises(ValueError):
            scene_factory._validate_blank(cfg.scenes[0], cfg)


class SidPanningConfigTest(unittest.TestCase):
    """[ultimate64].sid_panning — a bad pan value must fail at load, not
    mid-scene when the U64 mixer is configured (see c64cast/sid/sid_panning.py)."""

    def _load(self, toml):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            return cfgmod.load(path)
        finally:
            os.unlink(path)

    def test_defaults_to_empty_meaning_auto_spread(self):
        self.assertEqual(cfgmod.Config().ultimate64.sid_panning, [])

    def test_int_list_loads(self):
        cfg = self._load("[ultimate64]\nsid_panning = [-3, 3]\n")
        self.assertEqual(cfg.ultimate64.sid_panning, [-3, 3])

    def test_label_list_loads(self):
        cfg = self._load('[ultimate64]\nsid_panning = ["Left 4", "Center", "Right 4"]\n')
        self.assertEqual(cfg.ultimate64.sid_panning, ["Left 4", "Center", "Right 4"])

    def test_mixed_ints_and_labels_load(self):
        cfg = self._load('[ultimate64]\nsid_panning = [0, "Right 3"]\n')
        self.assertEqual(cfg.ultimate64.sid_panning, [0, "Right 3"])

    def test_out_of_range_int_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load("[ultimate64]\nsid_panning = [0, 9]\n")
        self.assertIn("sid_panning", str(ctx.exception))

    def test_unknown_label_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load('[ultimate64]\nsid_panning = ["Middle"]\n')
        self.assertIn("Middle", str(ctx.exception))

    def test_a_scalar_zero_is_refused_like_any_other_scalar(self):
        # 0 is a legal pan value (Center), so a truthiness guard let
        # `sid_panning = 0` past the list check while `sid_panning = -3` was
        # correctly rejected — and resolve_panning's own falsy test then
        # auto-spreads to [-3, +3], the opposite of centered.
        with self.assertRaises(ValueError) as ctx:
            self._load("[ultimate64]\nsid_panning = 0\n")
        self.assertIn("must be a list", str(ctx.exception))


class SidVolumeConfigTest(unittest.TestCase):
    """[ultimate64].sid_volume — a level the mixer can't represent must fail at
    load, not mid-scene when it is configured (see c64cast/sid/sid_volume.py)."""

    def _load(self, toml):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            return cfgmod.load(path)
        finally:
            os.unlink(path)

    def test_defaults_to_empty_meaning_auto(self):
        self.assertEqual(cfgmod.Config().ultimate64.sid_volume, [])

    def test_db_int_list_loads(self):
        cfg = self._load("[ultimate64]\nsid_volume = [0, -6]\n")
        self.assertEqual(cfg.ultimate64.sid_volume, [0, -6])

    def test_labels_and_off_load(self):
        cfg = self._load('[ultimate64]\nsid_volume = ["-6 dB", "off"]\n')
        self.assertEqual(cfg.ultimate64.sid_volume, ["-6 dB", "off"])

    def test_level_outside_the_ladder_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load("[ultimate64]\nsid_volume = [-20]\n")
        self.assertIn("sid_volume", str(ctx.exception))

    def test_unknown_label_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load('[ultimate64]\nsid_volume = ["loud"]\n')
        self.assertIn("loud", str(ctx.exception))

    def test_more_entries_than_sources_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load("[ultimate64]\nsid_volume = [0, 0, 0, 0, 0]\n")
        self.assertIn("sid_volume", str(ctx.exception))

    def test_a_scalar_zero_is_refused_like_any_other_scalar(self):
        # 0 dB is a legal level here, so the truthiness guard swallowed it.
        with self.assertRaises(ValueError) as ctx:
            self._load("[ultimate64]\nsid_volume = 0\n")
        self.assertIn("must be a list", str(ctx.exception))


class HostSidChipsConfigTest(unittest.TestCase):
    """[hardware].host_sid_chips — a machine with an internal dual-SID mod. A
    typo'd address here would otherwise surface as a chip silently missing from
    the resolved-audio verdict, so it must fail at load."""

    def _load(self, toml):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            return cfgmod.load(path)
        finally:
            os.unlink(path)

    def test_defaults_to_empty(self):
        self.assertEqual(cfgmod.Config().hardware.host_sid_chips, {})

    def test_dual_sid_table_loads(self):
        cfg = self._load('[hardware]\nhost_sid_chips = { d400 = "6581", d420 = "8580" }\n')
        self.assertEqual(cfg.hardware.host_sid_chips, {"d400": "6581", "d420": "8580"})

    def test_dollar_prefixed_address_loads(self):
        cfg = self._load('[hardware]\nhost_sid_chips = { "$D420" = "8580" }\n')
        self.assertEqual(cfg.hardware.host_sid_chips, {"$D420": "8580"})

    def test_unknown_model_loads(self):
        cfg = self._load('[hardware]\nhost_sid_chips = { d420 = "unknown" }\n')
        self.assertEqual(cfg.hardware.host_sid_chips, {"d420": "unknown"})

    def test_non_hex_address_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load('[hardware]\nhost_sid_chips = { sid2 = "8580" }\n')
        self.assertIn("hex address", str(ctx.exception))

    def test_address_outside_the_sid_window_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load('[hardware]\nhost_sid_chips = { c000 = "8580" }\n')
        self.assertIn("out of range", str(ctx.exception))

    def test_address_off_a_chip_boundary_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._load('[hardware]\nhost_sid_chips = { d425 = "8580" }\n')
        self.assertIn("out of range", str(ctx.exception))

    def test_auto_is_not_a_per_chip_model(self):
        # There is nothing to infer for a chip the user is asserting exists.
        with self.assertRaises(ValueError) as ctx:
            self._load('[hardware]\nhost_sid_chips = { d400 = "auto" }\n')
        self.assertIn("host_sid_chips", str(ctx.exception))

    def test_tune_match_defaults_to_off(self):
        self.assertEqual(cfgmod.Config().hardware.host_sid_tune_match, "off")

    def test_tune_match_accepts_every_choice(self):
        for mode in cfgmod.HOST_SID_TUNE_MATCH_CHOICES:
            cfg = self._load(f'[hardware]\nhost_sid_tune_match = "{mode}"\n')
            self.assertEqual(cfg.hardware.host_sid_tune_match, mode)

    def test_tune_match_typo_raises(self):
        # A typo would otherwise read as "off" and do nothing, which is
        # indistinguishable from the feature not working.
        with self.assertRaises(ValueError) as ctx:
            self._load('[hardware]\nhost_sid_tune_match = "preferred"\n')
        self.assertIn("host_sid_tune_match", str(ctx.exception))


class DoubleBufferTest(unittest.TestCase):
    """[video].double_buffer — the host-DMA page-flip path for no-REU backends."""

    def _load(self, toml):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            return cfgmod.load(path)
        finally:
            os.unlink(path)

    def test_default_is_auto(self):
        self.assertEqual(cfgmod.VideoCfg().double_buffer, "auto")

    def test_auto_enables_on_no_reu_bitmap_only(self):
        r = scene_factory.resolve_double_buffer
        # No-REU backend (TeensyROM), bitmap, REU staging off → auto enables.
        self.assertTrue(r("auto", "mhires", use_reu_staged=False, backend_supports_reu=False))
        self.assertTrue(r("auto", "hires", use_reu_staged=False, backend_supports_reu=False))
        # Char modes never (no second VIC bank to flip).
        self.assertFalse(r("auto", "petscii", use_reu_staged=False, backend_supports_reu=False))

    def test_auto_off_on_reu_backend_and_when_staged(self):
        r = scene_factory.resolve_double_buffer
        # U64 (has REU), overlay-free bitmap: auto leaves it off — the REU path
        # is the better tear-free option there.
        self.assertFalse(r("auto", "mhires", use_reu_staged=False, backend_supports_reu=True))
        # Mutually exclusive with REU staging (both flip $DD00).
        self.assertFalse(r("auto", "mhires", use_reu_staged=True, backend_supports_reu=False))

    def test_auto_enables_for_text_overlay_on_reu_backend(self):
        r = scene_factory.resolve_double_buffer
        # U64 (has REU) + a buffer-painting text overlay: resolve_use_reu_staged
        # turned the REU path off (shimmer), leaving single-buffer host-DMA that
        # tears on cuts. auto picks the host-DMA double-buffer (tear-free + crisp
        # text) instead.
        self.assertTrue(
            r(
                "auto",
                "mhires",
                use_reu_staged=False,
                backend_supports_reu=True,
                has_buffer_overlays=True,
            )
        )
        self.assertTrue(
            r(
                "auto",
                "hires",
                use_reu_staged=False,
                backend_supports_reu=True,
                has_buffer_overlays=True,
            )
        )
        # Still scoped to bitmap modes — a text overlay on a char mode is the
        # single-buffer-cheap path, no second bank to flip.
        self.assertFalse(
            r(
                "auto",
                "petscii",
                use_reu_staged=False,
                backend_supports_reu=True,
                has_buffer_overlays=True,
            )
        )

    def test_reu_mic_pump_gates_double_buffer_off(self):
        r = scene_factory.resolve_double_buffer
        # The host-DMA swap and the REU mic pump both own $0314 with no merged
        # dispatcher for the pair — gate double-buffer off so they can't collide.
        # Applies even to the text-overlay auto case and to an explicit `true`.
        self.assertFalse(
            r(
                "auto",
                "mhires",
                use_reu_staged=False,
                backend_supports_reu=True,
                has_buffer_overlays=True,
                audio_reu_pump_active=True,
            )
        )
        self.assertFalse(
            r(
                True,
                "mhires",
                use_reu_staged=False,
                backend_supports_reu=True,
                audio_reu_pump_active=True,
            )
        )

    def test_explicit_scoped_to_bitmap_and_loses_to_reu(self):
        r = scene_factory.resolve_double_buffer
        self.assertTrue(r(True, "mhires", use_reu_staged=False, backend_supports_reu=True))
        self.assertFalse(r(True, "petscii", use_reu_staged=False, backend_supports_reu=False))
        self.assertFalse(r(True, "mhires", use_reu_staged=True, backend_supports_reu=False))
        self.assertFalse(r(False, "mhires", use_reu_staged=False, backend_supports_reu=False))

    def test_bad_string_rejected_at_load(self):
        with self.assertRaises(ValueError) as ctx:
            self._load('[video]\ndouble_buffer = "yes"\n')
        self.assertIn("double_buffer", str(ctx.exception))


class ControlPlaneAuthCfgTest(unittest.TestCase):
    """validate_control_cfg refuses an open plane on a network address."""

    def _cfg(self, **kw: object) -> cfgmod.ControlPlaneCfg:
        from dataclasses import replace

        return replace(cfgmod.ControlPlaneCfg(), enabled=True, **kw)  # type: ignore[arg-type]

    def test_loopback_without_a_token_passes(self):
        for host in cfgmod.LOOPBACK_HOSTS:
            scene_factory.validate_control_cfg(self._cfg(host=host))  # must not raise

    def test_network_host_with_a_token_passes(self):
        scene_factory.validate_control_cfg(self._cfg(host="0.0.0.0", token="s3cret"))

    def test_disabled_is_not_checked(self):
        from dataclasses import replace

        cfg = replace(cfgmod.ControlPlaneCfg(), enabled=False, host="0.0.0.0")
        scene_factory.validate_control_cfg(cfg)  # must not raise

    def test_network_host_without_a_token_raises(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            scene_factory.validate_control_cfg(self._cfg(host="0.0.0.0"))
        msg = str(ctx.exception)
        self.assertIn("0.0.0.0", msg)
        self.assertIn("allow_unauthenticated", msg)

    def test_the_opt_out_permits_it(self):
        scene_factory.validate_control_cfg(
            self._cfg(host="0.0.0.0", allow_unauthenticated=True)
        )  # must not raise

    def test_a_viewer_token_alone_does_not_count(self):
        """viewer_token is ignored unless `token` is set, so it cannot be what
        makes an off-loopback bind acceptable."""
        with self.assertRaises(cfgmod.ConfigError):
            scene_factory.validate_control_cfg(self._cfg(host="0.0.0.0", viewer_token="v"))


class MidiControlLoopAudioTest(unittest.TestCase):
    """validate_midi_control_cfg guards the Phase 4 loop_audio choice."""

    def _cfg(self, loop_audio: str) -> cfgmod.MidiControlCfg:
        from dataclasses import replace

        return replace(cfgmod.MidiControlCfg(), enabled=True, loop_audio=loop_audio)

    def test_on_and_mute_pass(self):
        for good in ("on", "mute"):
            scene_factory.validate_midi_control_cfg(self._cfg(good))  # must not raise

    def test_bad_value_raises(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            scene_factory.validate_midi_control_cfg(self._cfg("loud"))
        self.assertIn("loop_audio", str(ctx.exception))


class ConfigErrorTest(unittest.TestCase):
    def test_missing_file_raises_config_error(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            cfgmod.load("/nonexistent/path/that/does/not/exist.toml")
        self.assertIn("not found", str(ctx.exception))
        self.assertIn(".toml", str(ctx.exception))

    def test_toml_syntax_error_message_shows_line_and_caret(self):
        # `audio = tru` — typo for `true`. Same shape as the example the
        # user reported.
        toml = "[audio]\nenabled = true\n[video]\ndevice = tru\n"
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            with self.assertRaises(cfgmod.ConfigError) as ctx:
                cfgmod.load(path)
        finally:
            os.unlink(path)
        msg = str(ctx.exception)
        # No raw traceback / parser internals.
        self.assertNotIn("tomllib", msg)
        self.assertNotIn("Traceback", msg)
        # Points at the right file + the right line.
        self.assertIn(path, msg)
        self.assertIn("line 4", msg)
        # Includes the offending source line and a caret marker.
        self.assertIn("device = tru", msg)
        self.assertIn("^", msg)


class VideoDeviceTest(unittest.TestCase):
    """[video].device accepts an int index or a string (name substring / VID:PID)."""

    def _load(self, toml):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            return cfgmod.load(path)
        finally:
            os.unlink(path)

    def test_int_device_loads(self):
        cfg = self._load("[video]\ndevice = 2\n")
        self.assertEqual(cfg.video.device, 2)

    def test_name_string_device_loads(self):
        cfg = self._load('[video]\ndevice = "Cam Link"\n')
        self.assertEqual(cfg.video.device, "Cam Link")

    def test_vidpid_string_device_loads(self):
        cfg = self._load('[video]\ndevice = "0fd9:0066"\n')
        self.assertEqual(cfg.video.device, "0fd9:0066")

    def test_malformed_vidpid_raises_config_error(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            self._load('[video]\ndevice = "0fzz:0066"\n')
        self.assertIn("[video].device", str(ctx.exception))

    def test_string_device_round_trips_through_serialize(self):
        from c64cast.app import config_serialize as ser

        cfg = cfgmod.Config()
        cfg.video.device = "Cam Link"
        reloaded = self._load(ser.dumps(cfg))
        self.assertEqual(reloaded.video.device, "Cam Link")


class UltimateUrlTest(unittest.TestCase):
    """[ultimate64].url takes the same connection targets -u/--url does."""

    def _load(self, toml: str) -> cfgmod.Config:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            return cfgmod.load(path)
        finally:
            os.unlink(path)

    def _url(self, url: str) -> str:
        return self._load(f'[ultimate64]\nurl = "{url}"\n').ultimate64.url

    def test_a_plain_base_url_is_left_alone(self):
        self.assertEqual(self._url("http://10.0.0.5"), "http://10.0.0.5")
        self.assertEqual(self._url("https://10.0.0.5:8080"), "https://10.0.0.5:8080")

    def test_the_cli_scheme_becomes_the_base_url_it_means(self):
        self.assertEqual(self._url("u64://10.0.0.5"), "http://10.0.0.5")

    def test_a_port_survives_the_rewrite(self):
        self.assertEqual(self._url("u64://10.0.0.5:8080"), "http://10.0.0.5:8080")

    def test_a_bare_host_gets_the_scheme_it_can_only_have_meant(self):
        # The one place this field is right to be looser than -u: inside
        # [ultimate64] there is no backend left to pick with a scheme.
        self.assertEqual(self._url("10.0.0.5"), "http://10.0.0.5")
        self.assertEqual(self._url("c64.local:8080"), "http://c64.local:8080")

    def test_another_backends_target_is_refused_here(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            self._url("tr:///dev/cu.usbmodem1234")
        self.assertIn("[hardware].backend", str(ctx.exception))

    def test_a_query_param_points_at_the_field_that_holds_it(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            self._url("u64://10.0.0.5?dma_port=64")
        self.assertIn("dma_port = 64", str(ctx.exception))

    def test_an_unknown_scheme_is_refused(self):
        with self.assertRaises(cfgmod.ConfigError):
            self._url("ftp://10.0.0.5")

    def test_the_rewritten_url_round_trips_through_serialize(self):
        from c64cast.app import config_serialize as ser

        cfg = self._load('[ultimate64]\nurl = "u64://10.0.0.5"\n')
        self.assertEqual(self._load(ser.dumps(cfg)).ultimate64.url, "http://10.0.0.5")

    def test_machine_settings_are_normalized_too(self):
        # Both layers go through _apply_toml_sections, which is the point of
        # putting the rewrite there rather than in load().
        cfg = cfgmod.Config()
        cfgmod._apply_toml_sections(
            cfg, {"ultimate64": {"url": "u64://10.0.0.5"}}, source="settings.toml"
        )
        self.assertEqual(cfg.ultimate64.url, "http://10.0.0.5")

    def test_a_bare_host_cannot_smuggle_credentials_past_the_parser(self):
        # The scheme-less fast path used to prefix http:// and return without
        # ever calling connect.parse_connection_uri, so the refusal this field
        # documents applied to "http://user:pass@host" and not to
        # "user:pass@host" — and --save-settings writes [ultimate64].url to
        # disk and echoes it to stdout.
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            self._url("admin:hunter2@10.0.0.5")
        self.assertIn("username/password", str(ctx.exception))

    def test_the_refusal_does_not_echo_the_credential(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            self._url("admin:hunter2@10.0.0.5")
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_a_bare_host_query_param_is_refused_like_a_scheme_carrying_one(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            self._url("10.0.0.5?dma_port=9999")
        self.assertIn("dma_port = 9999", str(ctx.exception))


class AudioDeviceTest(unittest.TestCase):
    """[audio].device accepts an int index or a device name substring."""

    def _load(self, toml: str) -> cfgmod.Config:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            return cfgmod.load(path)
        finally:
            os.unlink(path)

    def test_int_device_loads(self):
        cfg = self._load("[audio]\ndevice = 2\n")
        self.assertEqual(cfg.audio.device, 2)

    def test_name_string_device_loads(self):
        cfg = self._load('[audio]\ndevice = "Cam Link"\n')
        self.assertEqual(cfg.audio.device, "Cam Link")

    def test_empty_string_raises_config_error(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            self._load('[audio]\ndevice = "   "\n')
        self.assertIn("[audio].device", str(ctx.exception))

    def test_string_device_round_trips_through_serialize(self):
        from c64cast.app import config_serialize as ser

        cfg = cfgmod.Config()
        cfg.audio.device = "Cam Link"
        reloaded = self._load(ser.dumps(cfg))
        self.assertEqual(reloaded.audio.device, "Cam Link")


class FormatTomlErrorTest(unittest.TestCase):
    """The pure TOML-error formatter — both the structured-attrs path and the
    regex-fallback path used when the parser doesn't expose lineno/colno."""

    def test_uses_error_attrs_when_present(self):
        err = type(
            "E", (), {"lineno": 2, "colno": 5, "msg": "bad value", "doc": "a = 1\nb = ?\n"}
        )()
        out = cfgmod._format_toml_error("cfg.toml", err)
        self.assertIn("line 2, column 5: bad value", out)
        self.assertIn("b = ?", out)  # offending source line echoed
        self.assertIn("^", out)  # caret marker

    def test_falls_back_to_regex_when_attrs_missing(self):
        # A bare exception whose str() matches the parser's classic
        # "msg (at line N, column C)" shape → positions recovered via regex.
        err = Exception("Expected '=' after a key (at line 3, column 7)")
        out = cfgmod._format_toml_error("cfg.toml", err)
        self.assertIn("line 3, column 7", out)

    def test_no_position_available(self):
        err = Exception("totally opaque parser failure")
        out = cfgmod._format_toml_error("cfg.toml", err)
        self.assertIn("totally opaque parser failure", out)
        self.assertIn("cfg.toml", out)

    def test_a_credential_bearing_offending_line_is_redacted(self):
        # cli.py logs a ConfigError at error level and --log-file mirrors it to
        # disk, so echoing the offending source line copied the credential
        # there — and a TOML typo is exactly the error whose log gets pasted
        # into an issue. The position still carries the diagnostic value.
        err = type(
            "E",
            (),
            {
                "lineno": 2,
                "colno": 26,
                "msg": "bad value",
                "doc": '[ultimate64]\ndma_password = "hunter2" oops\n',
            },
        )()
        out = cfgmod._format_toml_error("cfg.toml", err)
        self.assertNotIn("hunter2", out)
        self.assertIn("dma_password", out)
        self.assertIn("line 2, column 26", out)

    def test_an_innocent_line_keeps_its_caret(self):
        err = type(
            "E", (), {"lineno": 2, "colno": 5, "msg": "bad value", "doc": "a = 1\nb = ?\n"}
        )()
        self.assertIn("^", cfgmod._format_toml_error("cfg.toml", err))


class LoadSonglengthsTest(unittest.TestCase):
    def setUp(self):
        # Memoization caches are module-global — clear between tests.
        scene_factory._songlengths_cache.clear()
        self._orig_autodetected = scene_factory._songlengths_autodetected
        scene_factory._songlengths_autodetected = scene_factory._UNSET

    def tearDown(self):
        scene_factory._songlengths_autodetected = self._orig_autodetected

    def test_empty_string_disables_autodetect(self):
        # Explicit "" opts out — unlike None, it never probes assets/sids/.
        with mock.patch.object(scene_factory, "_autodetect_songlengths_path") as auto:
            self.assertIsNone(scene_factory._load_songlengths(""))
            auto.assert_not_called()

    def test_none_path_autodetects_when_present(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md5", delete=False) as f:
            f.write("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=1:23\n")
            path = f.name
        try:
            with mock.patch.object(
                scene_factory, "_autodetect_songlengths_path", return_value=path
            ):
                with self.assertLogs("c64cast.app.scene_factory", level="INFO") as logs:
                    db = scene_factory._load_songlengths(None)
            self.assertIsNotNone(db)
            self.assertTrue(any("auto-detected" in m for m in logs.output))
        finally:
            os.unlink(path)

    def test_none_path_returns_none_when_nothing_detected(self):
        with mock.patch.object(scene_factory, "_autodetect_songlengths_path", return_value=None):
            self.assertIsNone(scene_factory._load_songlengths(None))

    def test_missing_file_warns_and_caches_none(self):
        with self.assertLogs("c64cast.app.scene_factory", level="WARNING"):
            self.assertIsNone(scene_factory._load_songlengths("/no/such/db.md5"))
        # The None result is memoized so a second call doesn't re-warn.
        self.assertIn("/no/such/db.md5", scene_factory._songlengths_cache)
        self.assertIsNone(scene_factory._load_songlengths("/no/such/db.md5"))

    def test_loads_and_memoizes_real_db(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md5", delete=False) as f:
            f.write("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=1:23\n")
            path = f.name
        try:
            db1 = scene_factory._load_songlengths(path)
            db2 = scene_factory._load_songlengths(path)
            self.assertIsNotNone(db1)
            self.assertIs(db1, db2)  # second call hits the cache
        finally:
            os.unlink(path)


class AutodetectSonglengthsTest(unittest.TestCase):
    def setUp(self):
        self._orig_autodetected = scene_factory._songlengths_autodetected
        scene_factory._songlengths_autodetected = scene_factory._UNSET

    def tearDown(self):
        scene_factory._songlengths_autodetected = self._orig_autodetected

    def test_no_root_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "assets", "sids")
            self.assertIsNone(scene_factory._autodetect_songlengths_path(missing))

    def test_finds_full_hvsc_tree_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = os.path.join(tmp, "C64Music", "DOCUMENTS")
            os.makedirs(docs)
            expected = os.path.join(docs, "Songlengths.md5")
            open(expected, "w").close()
            self.assertEqual(scene_factory._autodetect_songlengths_path(tmp), expected)

    def test_finds_contents_only_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = os.path.join(tmp, "DOCUMENTS")
            os.makedirs(docs)
            expected = os.path.join(docs, "Songlengths.md5")
            open(expected, "w").close()
            self.assertEqual(scene_factory._autodetect_songlengths_path(tmp), expected)

    def test_falls_back_to_full_scan_for_nonstandard_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            odd = os.path.join(tmp, "somewhere", "else")
            os.makedirs(odd)
            expected = os.path.join(odd, "Songlengths.md5")
            open(expected, "w").close()
            self.assertEqual(scene_factory._autodetect_songlengths_path(tmp), expected)

    def test_no_match_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "MUSICIANS"))
            self.assertIsNone(scene_factory._autodetect_songlengths_path(tmp))

    def test_result_is_memoized(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = os.path.join(tmp, "DOCUMENTS")
            os.makedirs(docs)
            open(os.path.join(docs, "Songlengths.md5"), "w").close()
            first = scene_factory._autodetect_songlengths_path(tmp)
            # A second call with a different (nonexistent) root still
            # returns the memoized first result — proves it isn't re-probed.
            second = scene_factory._autodetect_songlengths_path(os.path.join(tmp, "nope"))
            self.assertEqual(first, second)


class MergeCLITest(unittest.TestCase):
    def _make_args(self, **kw) -> argparse.Namespace:
        # Every overridable option defaults to None; only set what's passed.
        defaults = dict.fromkeys(cfgmod.CLI_TO_CFG)
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_none_values_leave_config_untouched(self):
        cfg = cfgmod.Config()
        cfg.ultimate64.system = "PAL"
        merged = cfgmod.merge_cli(cfg, self._make_args())
        self.assertEqual(merged.ultimate64.system, "PAL")

    def test_cli_value_overrides_config_value(self):
        # Connection fields (url/backend/etc.) are NOT in CLI_TO_CFG — they come
        # from the scheme-aware -u target (see connect.py / test_connect.py).
        # merge_cli still overlays the remaining mapped fields like system/audio.
        cfg = cfgmod.Config()
        cfg.ultimate64.system = "NTSC"
        cfg.audio.enabled = False
        merged = cfgmod.merge_cli(cfg, self._make_args(system="PAL", audio=True))
        self.assertEqual(merged.ultimate64.system, "PAL")
        self.assertTrue(merged.audio.enabled)

    def test_cli_can_override_nested_audio_fields(self):
        cfg = cfgmod.Config()
        merged = cfgmod.merge_cli(
            cfg,
            self._make_args(audio_device=3, sample_rate=22050, mic_sensitivity=2.0, noise_gate=0.1),
        )
        self.assertEqual(merged.audio.device, 3)
        self.assertEqual(merged.audio.sample_rate, 22050)
        self.assertEqual(merged.audio.mic_sensitivity, 2.0)
        self.assertEqual(merged.audio.noise_gate, 0.1)


class MachineSettingsTest(unittest.TestCase):
    """The machine-settings layer (defaults → machine settings → config →
    CLI). Every test points $C64CAST_SETTINGS at a tmp file so it never
    touches the real ~/.config/c64cast/settings.toml."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "settings.toml")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_settings(self, content: str) -> None:
        with open(self._path, "w") as f:
            f.write(content)

    def _env(self):
        return mock.patch.dict(os.environ, {"C64CAST_SETTINGS": self._path})

    def _write_config(self, content: str) -> str:
        p = os.path.join(self._tmp.name, "c64cast.toml")
        with open(p, "w") as f:
            f.write(content)
        return p

    def test_missing_file_is_noop(self):
        # No file at the pointed path → machine settings are empty.
        with self._env():
            self.assertEqual(cfgmod.load_machine_settings(), {})
            cfg = cfgmod.load(None)
        self.assertEqual(cfg.ultimate64.url, "http://192.168.2.64")

    def test_machine_settings_applied_in_load(self):
        self._write_settings(
            '[ultimate64]\nurl = "http://machine.lan"\nsid_model = "8580"\n[video]\ndevice = 4\n'
        )
        with self._env():
            cfg = cfgmod.load(None)
        self.assertEqual(cfg.ultimate64.url, "http://machine.lan")
        self.assertEqual(cfg.ultimate64.sid_model, "8580")
        self.assertEqual(cfg.video.device, 4)

    def test_config_overrides_machine(self):
        # defaults < machine < config
        self._write_settings('[ultimate64]\nsid_model = "8580"\nsystem = "PAL"\n')
        cfg_path = self._write_config('[ultimate64]\nsid_model = "6581"\n')
        with self._env():
            cfg = cfgmod.load(cfg_path)
        self.assertEqual(cfg.ultimate64.sid_model, "6581")  # config wins
        self.assertEqual(cfg.ultimate64.system, "PAL")  # machine-only field kept

    def test_hue_corrections_are_replaced_by_the_layer_above_not_appended(self):
        # Appending made the two layers concatenate, so the project file could
        # not override, reorder or remove a band the machine layer set —
        # against "every layer above the defaults overrides the ones below it",
        # and against scene_color()'s replace semantics for the same field.
        self._write_settings(
            '[color]\n[[color.hue_corrections]]\nname = "machine"\nhue_lo_deg = 10\n'
        )
        cfg_path = self._write_config(
            '[color]\n[[color.hue_corrections]]\nname = "project"\nhue_lo_deg = 20\n'
        )
        with self._env():
            cfg = cfgmod.load(cfg_path)
        self.assertEqual([hc["name"] for hc in cfg.color.hue_corrections], ["project"])

    def test_a_project_file_silent_on_hue_corrections_keeps_the_machine_bands(self):
        self._write_settings(
            '[color]\n[[color.hue_corrections]]\nname = "machine"\nhue_lo_deg = 10\n'
        )
        cfg_path = self._write_config('[color]\ndither = "none"\n')
        with self._env():
            cfg = cfgmod.load(cfg_path)
        self.assertEqual([hc["name"] for hc in cfg.color.hue_corrections], ["machine"])

    def test_two_layers_round_trip_through_the_serializer(self):
        # config_serialize writes a list-of-tables whole or not at all, so
        # appending made dumps() -> load() apply the machine band twice.
        from c64cast.app import config_serialize as ser

        self._write_settings(
            '[color]\n[[color.hue_corrections]]\nname = "machine"\nhue_lo_deg = 10\n'
        )
        cfg_path = self._write_config(
            '[color]\n[[color.hue_corrections]]\nname = "machine"\nhue_lo_deg = 10\n'
            '[[color.hue_corrections]]\nname = "project"\nhue_lo_deg = 20\n'
        )
        with self._env():
            cfg = cfgmod.load(cfg_path)
            reloaded = cfgmod.load(self._write_config(ser.dumps(cfg)))
        self.assertEqual(cfg.color.hue_corrections, reloaded.color.hue_corrections)

    def test_an_empty_hue_corrections_list_clears_the_machine_bands(self):
        self._write_settings(
            '[color]\n[[color.hue_corrections]]\nname = "machine"\nhue_lo_deg = 10\n'
        )
        cfg_path = self._write_config("[color]\nhue_corrections = []\n")
        with self._env():
            cfg = cfgmod.load(cfg_path)
        self.assertEqual(cfg.color.hue_corrections, [])

    def test_the_info_line_names_the_tables_it_supplied(self):
        # "(4 fields)" cannot tell an operator that this is the layer which
        # turned a network switch on.
        self._write_settings('[ultimate64]\nurl = "http://machine.lan"\n')
        with self._env():
            with self.assertLogs("c64cast.app.config", level="INFO") as logs:
                cfgmod.load(None)
        self.assertTrue(any("[ultimate64]" in m and "machine settings:" in m for m in logs.output))

    def test_the_info_line_is_logged_once_per_file_state(self):
        # In ensemble mode the layer is re-applied once per system plus twice
        # more (master defaults, cascade baseline), so one file used to print
        # N+2 identical lines.
        self._write_settings('[ultimate64]\nurl = "http://machine.lan"\n')
        with self._env():
            with self.assertLogs("c64cast.app.config", level="INFO") as logs:
                for _ in range(4):
                    cfgmod.apply_machine_settings(cfgmod.Config())
        announced = [m for m in logs.output if "machine settings:" in m]
        self.assertEqual(len(announced), 1)

    def test_cli_overrides_machine_and_config(self):
        # defaults < machine < config < CLI
        self._write_settings('[ultimate64]\nsid_model = "8580"\n')
        cfg_path = self._write_config('[ultimate64]\nsid_model = "6581"\n')
        with self._env():
            cfg = cfgmod.load(cfg_path)
            args = argparse.Namespace(**dict.fromkeys(cfgmod.CLI_TO_CFG))
            args.sid_model = "off"
            merged = cfgmod.merge_cli(cfg, args)
        self.assertEqual(merged.ultimate64.sid_model, "off")

    def test_scenes_section_rejected(self):
        self._write_settings('[ultimate64]\nurl = "http://m.lan"\n[[scenes]]\ntype = "blank"\n')
        with self._env(), self.assertLogs("c64cast.app.config", level="WARNING") as cm:
            data = cfgmod.load_machine_settings()
            cfg = cfgmod.load(None)
        self.assertIn("[scenes] ignored", "\n".join(cm.output))
        self.assertNotIn("scenes", data)
        self.assertEqual(cfg.scenes, [])
        self.assertEqual(cfg.ultimate64.url, "http://m.lan")  # other sections still applied

    def test_ensemble_section_rejected(self):
        self._write_settings(
            "[ensemble]\nsystems = [{name='a', config='a.toml'}]\n[audio]\nsample_rate = 8000\n"
        )
        with self._env(), self.assertLogs("c64cast.app.config", level="WARNING") as cm:
            data = cfgmod.load_machine_settings()
            cfg = cfgmod.load(None)
        self.assertIn("[ensemble] ignored", "\n".join(cm.output))
        self.assertNotIn("ensemble", data)
        self.assertEqual(cfg.audio.sample_rate, 8000)

    def test_corrupt_file_raises_config_error(self):
        self._write_settings("[ultimate64]\nurl = \n")  # missing value
        with self._env():
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load_machine_settings()

    def test_unknown_key_warns_but_loads(self):
        self._write_settings('[ultimate64]\nurl = "http://m.lan"\nbogus_key = 1\n')
        with self._env():
            with self.assertLogs("c64cast.app.config", level="WARNING"):
                cfg = cfgmod.load(None)
        self.assertEqual(cfg.ultimate64.url, "http://m.lan")


class UnknownKeyCollectionTest(unittest.TestCase):
    """`load_master` collects stray keys instead of logging them, so --doctor
    can render them as report rows rather than a preamble line above it."""

    def _master(self, toml: str) -> cfgmod.LoadResult:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.toml")
            with open(path, "w") as f:
                f.write(toml)
            return cfgmod.load_master(path)

    def test_load_master_collects_and_stays_silent(self):
        with mock.patch.object(cfgmod.log, "warning") as warn:
            loaded = self._master("[color]\nbogus_key = 7\n")
        warn.assert_not_called()
        self.assertEqual(len(loaded.unknown_keys), 1)
        rec = loaded.unknown_keys[0]
        self.assertEqual((rec.section, rec.key), ("color", "bogus_key"))
        self.assertTrue(rec.source and rec.source.endswith("c.toml"))

    def test_bare_load_still_logs_inline(self):
        # SIGHUP reload and the interstitial factory call load() with no
        # collector; those keys must still reach stderr.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.toml")
            with open(path, "w") as f:
                f.write("[color]\nbogus_key = 7\n")
            with self.assertLogs("c64cast.app.config", level="WARNING") as cm:
                cfgmod.load(path)
        self.assertTrue(any("bogus_key" in m for m in cm.output))

    def test_valid_key_in_wrong_section_names_the_right_one(self):
        # The case within-section difflib can never catch: spelled perfectly,
        # just in the wrong table. This is the whole reason the index exists.
        loaded = self._master('[color]\npalette_mode = "grayscale"\n')
        hint = loaded.unknown_keys[0].hint or ""
        self.assertIn("[[scenes]]", hint)
        self.assertIn("move it there", hint)

    def test_typo_in_right_section_suggests_the_near_miss(self):
        loaded = self._master("[color]\ndither_strenth = 0.5\n")
        self.assertIn("dither_strength", loaded.unknown_keys[0].hint or "")

    def test_unrecognizable_key_has_no_hint(self):
        loaded = self._master("[playlist]\nfrobnicate = true\n")
        self.assertIsNone(loaded.unknown_keys[0].hint)

    def test_weak_cross_section_match_is_not_volunteered(self):
        # The cross-section pool is big enough that difflib's default cutoff
        # offers junk; 'strayA' scores 0.62 against 'storage' in [teensyrom],
        # which would send someone to edit an unrelated table.
        loaded = self._master("[playlist]\nstrayA = 1\n")
        self.assertIsNone(loaded.unknown_keys[0].hint)

    def test_strong_cross_section_typo_is_still_offered(self):
        loaded = self._master("[playlist]\ndither_strenth = 0.5\n")
        hint = loaded.unknown_keys[0].hint or ""
        self.assertIn("dither_strength", hint)
        self.assertIn("[color]", hint)

    def test_scene_keys_are_collected_with_their_section(self):
        loaded = self._master('[[scenes]]\ntype = "blank"\nchannel_boost = [1, 2]\n')
        rec = loaded.unknown_keys[0]
        self.assertEqual((rec.section, rec.key), ("scenes", "channel_boost"))
        self.assertIn("[color]", rec.hint or "")

    def test_known_key_index_covers_every_applied_section(self):
        # The index is what makes "wrong table" answerable; a section missing
        # from it silently degrades those hints back to plain difflib.
        index = cfgmod._known_key_index()
        sections = {s for names in index.values() for s in names}
        for name in (*cfgmod._TOML_SCALAR_SECTIONS, "color"):
            self.assertIn(name, sections)
        self.assertIn("[scenes]", sections)

    def test_dedupe_collapses_repeats_of_one_key(self):
        rec = cfgmod.UnknownKey("color", "bogus", "f.toml", None)
        other = cfgmod.UnknownKey("color", "bogus", "g.toml", None)
        self.assertEqual(cfgmod._dedupe_unknown([rec, rec, other]), [rec, other])


class DmaPasswordEnvTest(unittest.TestCase):
    """C64CAST_DMA_PASSWORD env var is the final layer (env > config > default)
    — folded in by merge_cli. Previously untested (a gap this change closes)."""

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(**dict.fromkeys(cfgmod.CLI_TO_CFG))

    def test_env_sets_password(self):
        cfg = cfgmod.Config()
        with mock.patch.dict(os.environ, {"C64CAST_DMA_PASSWORD": "sekret"}):
            merged = cfgmod.merge_cli(cfg, self._args())
        self.assertEqual(merged.ultimate64.dma_password, "sekret")

    def test_env_overrides_config_value(self):
        cfg = cfgmod.Config()
        cfg.ultimate64.dma_password = "from-config"
        with mock.patch.dict(os.environ, {"C64CAST_DMA_PASSWORD": "from-env"}):
            merged = cfgmod.merge_cli(cfg, self._args())
        self.assertEqual(merged.ultimate64.dma_password, "from-env")

    def test_no_env_keeps_config_value(self):
        cfg = cfgmod.Config()
        cfg.ultimate64.dma_password = "from-config"
        env = {k: v for k, v in os.environ.items() if k != "C64CAST_DMA_PASSWORD"}
        with mock.patch.dict(os.environ, env, clear=True):
            merged = cfgmod.merge_cli(cfg, self._args())
        self.assertEqual(merged.ultimate64.dma_password, "from-config")


class ControlTokenEnvTest(unittest.TestCase):
    """C64CAST_CONTROL_TOKEN / _VIEWER_TOKEN ride the same final merge_cli
    layer as the DMA password, so a config file shared between machines (or
    committed) doesn't have to carry the credential."""

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(**dict.fromkeys(cfgmod.CLI_TO_CFG))

    def _clean_env(self) -> dict[str, str]:
        drop = {"C64CAST_CONTROL_TOKEN", "C64CAST_CONTROL_VIEWER_TOKEN"}
        return {k: v for k, v in os.environ.items() if k not in drop}

    def test_env_sets_both_tokens(self):
        with mock.patch.dict(
            os.environ,
            {"C64CAST_CONTROL_TOKEN": "full-t", "C64CAST_CONTROL_VIEWER_TOKEN": "view-t"},
        ):
            merged = cfgmod.merge_cli(cfgmod.Config(), self._args())
        self.assertEqual(merged.control.token, "full-t")
        self.assertEqual(merged.control.viewer_token, "view-t")

    def test_env_overrides_config_value(self):
        cfg = cfgmod.Config()
        cfg.control.token = "from-config"
        with mock.patch.dict(os.environ, {"C64CAST_CONTROL_TOKEN": "from-env"}):
            merged = cfgmod.merge_cli(cfg, self._args())
        self.assertEqual(merged.control.token, "from-env")

    def test_no_env_keeps_config_value(self):
        cfg = cfgmod.Config()
        cfg.control.token = "from-config"
        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            merged = cfgmod.merge_cli(cfg, self._args())
        self.assertEqual(merged.control.token, "from-config")

    def test_default_is_no_token(self):
        with mock.patch.dict(os.environ, self._clean_env(), clear=True):
            merged = cfgmod.merge_cli(cfgmod.Config(), self._args())
        self.assertEqual(merged.control.token, "")
        self.assertEqual(merged.control.viewer_token, "")

    def test_an_exported_but_empty_var_counts_as_unset(self):
        # `VAR=$UNSET_OTHER` in a service unit exports "" — a string, not None,
        # so an `is not None` fold silently blanked a configured token, which
        # on loopback means no authentication at all.
        cfg = cfgmod.Config()
        cfg.control.token = "from-config"
        cfg.control.viewer_token = "viewer-from-config"
        cfg.ultimate64.dma_password = "pw-from-config"
        with mock.patch.dict(
            os.environ,
            {
                "C64CAST_CONTROL_TOKEN": "",
                "C64CAST_CONTROL_VIEWER_TOKEN": "",
                "C64CAST_DMA_PASSWORD": "",
            },
        ):
            merged = cfgmod.merge_cli(cfg, self._args())
        self.assertEqual(merged.control.token, "from-config")
        self.assertEqual(merged.control.viewer_token, "viewer-from-config")
        self.assertEqual(merged.ultimate64.dma_password, "pw-from-config")


class MergeCliValidatesTest(unittest.TestCase):
    """merge_cli is the last layer that writes into a Config, and every section
    validator used to fire one layer below it — at parse time — so a CLI flag or
    an env var reached the run unchecked and failed mid-show instead."""

    def _args(self, **over: object) -> argparse.Namespace:
        ns = argparse.Namespace(**dict.fromkeys(cfgmod.CLI_TO_CFG))
        for k, v in over.items():
            setattr(ns, k, v)
        return ns

    def test_a_cli_choice_outside_the_vocabulary_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            cfgmod.merge_cli(cfgmod.Config(), self._args(system="ntscc"))
        self.assertIn("[ultimate64].system", str(ctx.exception))

    def test_a_cli_audio_device_that_is_blank_is_refused(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.merge_cli(cfgmod.Config(), self._args(audio_device="   "))

    def test_a_valid_cli_value_still_merges(self):
        merged = cfgmod.merge_cli(cfgmod.Config(), self._args(system="PAL"))
        self.assertEqual(merged.ultimate64.system, "PAL")


class ChoiceEnforcementTest(unittest.TestCase):
    """`choices` metadata is the single source of truth, and it is now enforced
    generically for the scalar sections — the fields nobody hand-wrote a
    validator for used to fail *open*, and `sid_video_mode` failed open into a
    machine retiming plus an HDMI output-mode switch (hw_provision tests it as
    `!= "off"`)."""

    def _load(self, body: str) -> cfgmod.Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.toml")
            with open(path, "w") as f:
                f.write(body)
            return cfgmod.load(path)

    def test_sid_video_mode_typo_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._load('[ultimate64]\nsid_video_mode = "on"\n')
        self.assertIn("sid_video_mode", str(ctx.exception))

    def test_host_sid_model_typo_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('[hardware]\nhost_sid_model = "6851"\n')

    def test_teensyrom_storage_typo_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('[teensyrom]\nstorage = "sdcard"\n')

    def test_hdmi_scan_resolution_typo_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('[ultimate64]\nhdmi_scan_resolution = "1080"\n')

    def test_a_declared_choice_is_accepted(self):
        self.assertEqual(
            self._load('[ultimate64]\nsid_video_mode = "auto"\n').ultimate64.sid_video_mode,
            "auto",
        )

    def test_system_is_matched_case_insensitively(self):
        # hw/backend.py and hw/hw_provision.py both .upper() this, so the
        # lowercase spelling works today and has to keep working.
        self.assertEqual(self._load('[ultimate64]\nsystem = "ntsc"\n').ultimate64.system, "ntsc")

    def test_a_system_typo_is_still_refused(self):
        with self.assertRaises(ValueError):
            self._load('[ultimate64]\nsystem = "ntscc"\n')

    def test_an_open_vocabulary_still_takes_its_other_shape(self):
        # sid_play_rate is "auto"/"off" *plus* any rate in Hz.
        self.assertEqual(
            self._load("[ultimate64]\nsid_play_rate = 50.1\n").ultimate64.sid_play_rate, 50.1
        )

    def test_every_exemption_names_a_real_choices_field(self):
        # An exemption must not outlive the field it excuses.
        probe = cfgmod.Config()
        for key in (*cfgmod._CHOICES_OPEN, *cfgmod._CHOICES_CASE_INSENSITIVE):
            section, _, name = key.partition(".")
            self.assertIn(section, cfgmod._TOML_SCALAR_SECTIONS, key)
            fld = {f.name: f for f in dataclasses.fields(getattr(probe, section))}[name]
            self.assertTrue(fld.metadata.get("choices"), key)


class BoolFieldTypingTest(unittest.TestCase):
    """Every consumer of a bool field is a plain truthiness test, so a quoted
    TOML `"false"` stored the truthy string "false" and meant the opposite of
    what it read as — on the two switches that decide network exposure."""

    def _load(self, body: str) -> cfgmod.Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.toml")
            with open(path, "w") as f:
                f.write(body)
            return cfgmod.load(path)

    def test_a_quoted_false_on_a_security_gate_is_refused(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            self._load('[control]\nallow_unauthenticated = "false"\n')
        self.assertIn("allow_unauthenticated", str(ctx.exception))

    def test_a_quoted_false_on_the_setup_wizard_is_refused(self):
        with self.assertRaises(cfgmod.ConfigError):
            self._load('[web]\nsetup_wizard = "false"\n')

    def test_an_int_for_a_bool_is_refused(self):
        with self.assertRaises(cfgmod.ConfigError):
            self._load("[control]\nenabled = 1\n")

    def test_a_real_bool_is_accepted(self):
        self.assertTrue(
            self._load("[control]\nallow_unauthenticated = true\n").control.enabled is False
        )

    def test_the_tri_states_are_untouched(self):
        # use_reu_staged is `bool | str`, so "auto" is legal and its own
        # validator owns the vocabulary.
        self.assertEqual(
            self._load('[video]\nuse_reu_staged = "auto"\n').video.use_reu_staged, "auto"
        )


class InternalFieldNotAuthorableTest(unittest.TestCase):
    """`cc_map_is_default` is derived run state: it is set False only when a
    layer really authored a cc_map. Authoring the flag directly inverted
    midi_control.resolve_effective_cc_map's precedence with no cc_map in
    sight — the `internal` metadata only hid the field from the four rendering
    surfaces, never from the apply path."""

    def test_it_is_reported_as_an_unknown_key(self):
        unknown: list[cfgmod.UnknownKey] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.toml")
            with open(path, "w") as f:
                f.write("[midi_control]\ncc_map_is_default = false\n")
            cfg = cfgmod.load(path, unknown)
        self.assertTrue(cfg.midi_control.cc_map_is_default)
        self.assertEqual(
            [(r.section, r.key) for r in unknown], [("midi_control", "cc_map_is_default")]
        )

    def test_authoring_a_cc_map_still_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.toml")
            with open(path, "w") as f:
                f.write("[midi_control]\ncc_map = []\n")
            cfg = cfgmod.load(path, [])
        self.assertFalse(cfg.midi_control.cc_map_is_default)


class UnknownTableTest(unittest.TestCase):
    """A misspelled *table* is the one stray-key shape the per-section walk
    could never see, because a table the loader applies to nothing never
    reaches _apply_section at all — which is how a master `[hardware]` block
    could vanish with no diagnostic anywhere."""

    def _load(self, body: str) -> list[cfgmod.UnknownKey]:
        unknown: list[cfgmod.UnknownKey] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.toml")
            with open(path, "w") as f:
                f.write(body)
            cfgmod.load(path, unknown)
        return unknown

    def test_an_unrecognized_table_is_collected(self):
        recs = self._load('[ultimate65]\nurl = "http://x"\n')
        self.assertEqual([(r.section, r.key) for r in recs], [("", "ultimate65")])
        self.assertIn("ultimate64", recs[0].describe() + (recs[0].hint or ""))

    def test_it_describes_itself_as_a_table(self):
        recs = self._load("[nonsense]\nx = 1\n")
        self.assertIn("unknown config table [nonsense]", recs[0].describe())

    def test_known_tables_are_silent(self):
        self.assertEqual(
            self._load('[ultimate64]\nurl = "http://x"\n[[scenes]]\ntype = "blank"\n'), []
        )


class BuildersTableTest(unittest.TestCase):
    def test_every_scene_type_has_a_builder(self):
        # A new entry in SCENE_TYPES must land in _BUILDERS the day it's
        # added — a missing one would otherwise surface as a KeyError deep
        # in build_scene instead of a failing test.
        self.assertEqual(set(scene_factory._BUILDERS), set(cfgmod.SCENE_TYPES))


class ValidateSceneCfgTest(unittest.TestCase):
    """Direct tests for `validate_scene_cfg` — the seam doctor mode and
    `build_scene` both go through. Covers every per-scene ValueError path
    that used to live inline in `build_scene`."""

    def _cfg(self) -> cfgmod.Config:
        return cfgmod.Config()

    def test_valid_blank_scene_passes(self):
        s = cfgmod.SceneCfg(type="blank")
        scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_negative_duration_rejected(self):
        s = cfgmod.SceneCfg(type="blank", duration_s=-1.0)
        with self.assertRaisesRegex(ValueError, "duration_s must be >= 0"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_zero_duration_allowed(self):
        # 0 is the "run forever" sentinel, not an error.
        s = cfgmod.SceneCfg(type="blank", duration_s=0)
        scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_blank_scene_rejects_wrong_display(self):
        s = cfgmod.SceneCfg(type="blank", display="mhires")
        with self.assertRaisesRegex(ValueError, "blank scene must use"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_scene_falls_back_to_default_dir(self):
        # No `file =` set → resolve from assets/videos/. Tests must run
        # from a tmp cwd so the dev's real assets/videos doesn't satisfy
        # the fallback silently.
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "assets", "videos"))
            with open(os.path.join(tmp, "assets", "videos", "ok.mp4"), "w") as f:
                f.write("")
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="video")
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
                # validate_scene_cfg normalizes s.file to the default dir.
                self.assertEqual(s.file, scene_factory.DEFAULT_VIDEO_DIR)
            finally:
                os.chdir(cwd)

    def test_video_scene_no_file_and_no_default_dir_raises(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="video")
                with self.assertRaisesRegex(ValueError, "default directory .* is missing or empty"):
                    scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
            finally:
                os.chdir(cwd)

    def test_video_scene_rejects_duration_s(self):
        # Video lifetime is video-driven; a finite duration_s would
        # either be a silent no-op or truncate the file. Loader must reject
        # it at config time rather than letting the inconsistency lurk.
        s = cfgmod.SceneCfg(type="video", file="video.mp4", duration_s=30.0)
        with self.assertRaisesRegex(ValueError, "does not accept .*duration_s"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_scene_without_duration_s_passes(self):
        # The default (None) means "no duration_s declared" and must pass
        # validation cleanly — that's the supported config shape.
        s = cfgmod.SceneCfg(type="video", file="video.mp4")
        scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_scene_accepts_start_s(self):
        s = cfgmod.SceneCfg(type="video", file="video.mp4", start_s=90.0)
        scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_scene_rejects_negative_start_s(self):
        s = cfgmod.SceneCfg(type="video", file="video.mp4", start_s=-1.0)
        with self.assertRaisesRegex(ValueError, "start_s must be >= 0"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_start_s_rejected_on_non_video(self):
        # start_s is a video-only seek; setting it elsewhere is a no-op the
        # loader rejects rather than silently ignores.
        s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", start_s=10.0)
        with self.assertRaisesRegex(ValueError, "start_s is only supported on video"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_url_needing_ytdlp_rejected_without_extra(self):
        # Offline doctor/load check: a YouTube-style URL needs yt-dlp; without
        # the `yt` extra, flag it up front instead of failing at playback.
        s = cfgmod.SceneCfg(type="video", file="https://youtu.be/abc?t=90")
        with mock.patch("c64cast.app.quickcast._ytdlp_available", return_value=False):
            with self.assertRaisesRegex(ValueError, "yt-dlp"):
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_url_needing_ytdlp_passes_with_extra(self):
        s = cfgmod.SceneCfg(type="video", file="https://youtu.be/abc?t=90")
        with mock.patch("c64cast.app.quickcast._ytdlp_available", return_value=True):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_direct_media_url_does_not_require_extra(self):
        # A direct media URL plays via PyAV without yt-dlp — no extra needed
        # even when it's absent.
        s = cfgmod.SceneCfg(type="video", file="http://host/clip.mp4")
        with mock.patch("c64cast.app.quickcast._ytdlp_available", return_value=False):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_waveform_scene_falls_back_to_default_dir(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "assets", "sids"))
            with open(os.path.join(tmp, "assets", "sids", "tune.sid"), "w") as f:
                f.write("")
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="waveform")
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
                self.assertEqual(s.file, scene_factory.DEFAULT_WAVEFORM_DIR)
            finally:
                os.chdir(cwd)

    def test_waveform_scene_no_file_and_no_default_dir_raises(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="waveform")
                with self.assertRaisesRegex(ValueError, "default directory .* is missing or empty"):
                    scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
            finally:
                os.chdir(cwd)

    def test_slideshow_scene_falls_back_to_default_dir(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "assets", "pictures"))
            with open(os.path.join(tmp, "assets", "pictures", "p.jpg"), "w") as f:
                f.write("")
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="slideshow")
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
                self.assertEqual(s.file, scene_factory.DEFAULT_SLIDESHOW_DIR)
            finally:
                os.chdir(cwd)

    def test_slideshow_scene_no_file_and_no_default_dir_raises(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="slideshow")
                with self.assertRaisesRegex(ValueError, "default directory .* is missing or empty"):
                    scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
            finally:
                os.chdir(cwd)

    def test_slideshow_image_duration_s_must_be_positive(self):
        s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", image_duration_s=0.0)
        with self.assertRaisesRegex(ValueError, "image_duration_s must be > 0"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_slideshow_aspect_mode_accepts_known_choices(self):
        for mode in cfgmod._ASPECT_MODE_CHOICES:
            s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", aspect_mode=mode)
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_slideshow_aspect_mode_rejects_unknown(self):
        s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", aspect_mode="contain")
        with self.assertRaisesRegex(ValueError, "aspect_mode must be one of"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_slideshow_display_random_resolves_to_known_mode(self):
        for _ in range(10):
            picked = scene_factory._resolve_slideshow_display("random")
            self.assertIn(picked, scene_factory.SLIDESHOW_RANDOM_DISPLAYS)

    def test_slideshow_display_hires_edges_substituted_with_mhires(self):
        # The SceneCfg global default ("hires_edges") is tuned for live
        # webcam Canny edges; slideshow swaps it for mhires (best color
        # for stills).
        self.assertEqual(scene_factory._resolve_slideshow_display("hires_edges"), "mhires")
        # Other explicit choices pass through.
        for name in ("hires", "mhires", "mcm", "petscii"):
            self.assertEqual(scene_factory._resolve_slideshow_display(name), name)

    def test_slideshow_scene_rejects_blank_display(self):
        s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", display="blank")
        with self.assertRaisesRegex(ValueError, "cannot use display"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_midi_scene_rejects_wrong_adsr_length(self):
        s = cfgmod.SceneCfg(type="midi", midi_adsr=[0, 0, 0])
        with self.assertRaisesRegex(ValueError, "midi_adsr must have 4"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_midi_scene_rejects_bad_voice_mode(self):
        s = cfgmod.SceneCfg(type="midi", midi_voice_mode="poly")
        with self.assertRaisesRegex(ValueError, "midi_voice_mode"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_midi_scene_rejects_bad_voice_waveform(self):
        s = cfgmod.SceneCfg(type="midi", midi_voice_waveforms=["pulse", "square"])
        with self.assertRaisesRegex(ValueError, "midi_voice_waveforms"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_midi_scene_accepts_combined_voice_waveforms(self):
        s = cfgmod.SceneCfg(
            type="midi", midi_voice_waveforms=["pulse+triangle", "sawtooth", "noise"]
        )
        scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)  # no raise

    def test_midi_scene_rejects_bad_voice_channels_when_multitimbral(self):
        s = cfgmod.SceneCfg(
            type="midi", midi_voice_mode="multitimbral", midi_voice_channels=[1, 1, 99]
        )
        with self.assertRaisesRegex(ValueError, "midi_voice_channels"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_unknown_scene_type_rejected(self):
        s = cfgmod.SceneCfg(type="something-bogus")
        with self.assertRaisesRegex(ValueError, "unknown scene type"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_unknown_display_mode_rejected(self):
        s = cfgmod.SceneCfg(type="webcam", display="petsci")
        with self.assertRaisesRegex(ValueError, "unknown display mode"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_text_overlay_accepted_on_mhires(self):
        # `clock` is a text overlay (REQUIRES_PETSCII + SUPPORTS_BITMAP_TEXT):
        # it folds its glyphs into the bitmap, so mhires is valid now.
        s = cfgmod.SceneCfg(type="webcam", display="mhires", overlays=[{"type": "clock"}])
        scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)  # no raise

    def test_text_overlay_rejected_on_mcm(self):
        # mcm is neither PETSCII- nor bitmap-text-compatible (color-RAM bit 3).
        s = cfgmod.SceneCfg(type="webcam", display="mcm", overlays=[{"type": "clock"}])
        with self.assertRaisesRegex(ValueError, "petscii"):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_overlay_requires_audio_gate(self):
        # No shipped overlay sets REQUIRES_AUDIO (the spectrum overlays only
        # WANT audio — they read the scene's music features first), but the
        # gate is a live framework facility, so cover it with a stub.
        from c64cast.scenes import overlays as overlays_mod

        class _NeedsAudio(overlays_mod.Overlay):
            name = "_needs_audio"
            REQUIRES_AUDIO = True

            def __init__(self, audio=None):
                self.audio = audio

        overlays_mod._load_all()
        s = cfgmod.SceneCfg(type="webcam", display="petscii", overlays=[{"type": "_needs_audio"}])
        with mock.patch.dict(overlays_mod._REGISTRY, {"_needs_audio": _NeedsAudio}):
            # audio_enabled=True supplies the sentinel, so validation succeeds.
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=True)
            with self.assertRaisesRegex(ValueError, "requires audio"):
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_spectrum_overlay_valid_without_audio(self):
        # It falls back to the scene's music features / paints nothing, rather
        # than refusing to build.
        s = cfgmod.SceneCfg(
            type="webcam", display="petscii", overlays=[{"type": "spectrum_petscii"}]
        )
        scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_orchestrate_with_no_claiming_subclass_rejected(self):
        # A `blank` scene with no orchestrator-specific shape won't be
        # claimed by BigTextSpanOrchestrator.
        s = cfgmod.SceneCfg(type="blank", name="solo", orchestrate=True)
        from c64cast.app.orchestrator import OrchestratorError

        with self.assertRaises(OrchestratorError):
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def _prg(self, tmp: str) -> str:
        p = os.path.join(tmp, "demo.prg")
        with open(p, "wb") as f:
            f.write(b"\x01\x08")
        return p

    def test_launcher_scene_falls_back_to_default_dir(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "assets", "programs"))
            with open(os.path.join(tmp, "assets", "programs", "g.prg"), "wb") as f:
                f.write(b"\x01\x08")
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="launcher")
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
                self.assertEqual(s.file, scene_factory.DEFAULT_PROGRAM_DIR)
            finally:
                os.chdir(cwd)

    def test_launcher_scene_no_file_and_no_default_dir_raises(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="launcher")
                with self.assertRaisesRegex(ValueError, "default directory .* is missing or empty"):
                    scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
            finally:
                os.chdir(cwd)

    def test_launcher_scene_valid_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = cfgmod.SceneCfg(
                type="launcher", file=self._prg(tmp), duration_s=90.0, input_source="cia"
            )
            scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_launcher_scene_rejects_overlays(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = cfgmod.SceneCfg(type="launcher", file=self._prg(tmp), overlays=[{"type": "clock"}])
            with self.assertRaisesRegex(ValueError, "cannot carry overlays"):
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_launcher_scene_rejects_non_default_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = cfgmod.SceneCfg(type="launcher", file=self._prg(tmp), display="mcm")
            with self.assertRaisesRegex(ValueError, "does not use .*display"):
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_launcher_scene_rejects_bad_input_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = cfgmod.SceneCfg(type="launcher", file=self._prg(tmp), input_source="bogus")
            with self.assertRaisesRegex(ValueError, "input_source must be"):
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_launcher_scene_rejects_bad_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            d64 = os.path.join(tmp, "game.d64")
            with open(d64, "wb") as f:
                f.write(b"")
            s = cfgmod.SceneCfg(type="launcher", file=d64)
            with self.assertRaises(ValueError):
                scene_factory.validate_scene_cfg(s, self._cfg(), audio_enabled=False)


class ResolveFileSpecTest(unittest.TestCase):
    """Direct tests for `resolve_file_spec` — the comma/dir/glob expander
    that backs the `file =` field on video + waveform scenes."""

    EXTS = (".sid",)

    def _make_files(self, root: str, names: list[str]) -> list[str]:
        paths: list[str] = []
        for n in names:
            p = os.path.join(root, n)
            with open(p, "w") as f:
                f.write("")
            paths.append(p)
        return sorted(paths)

    def test_literal_path_returns_one_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            [p] = self._make_files(tmp, ["one.sid"])
            self.assertEqual(scene_factory.resolve_file_spec(p, self.EXTS, label="waveform"), [p])

    def test_literal_path_with_wrong_extension_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected extension"):
            scene_factory.resolve_file_spec("not-a-sid.mp4", self.EXTS, label="waveform")

    def test_directory_expands_to_all_matching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_files(tmp, ["a.sid", "b.sid", "skip.mp4"])
            got = scene_factory.resolve_file_spec(tmp, self.EXTS, label="waveform")
            self.assertEqual([os.path.basename(p) for p in got], ["a.sid", "b.sid"])

    def test_directory_with_no_matches_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_files(tmp, ["only.mp4"])
            with self.assertRaisesRegex(ValueError, "contains no files with extension"):
                scene_factory.resolve_file_spec(tmp, self.EXTS, label="waveform")

    def test_default_waveform_dir_recurses(self):
        # The waveform scene's default directory (assets/sids) is the one
        # exception to the shallow-directory-listing rule: it's walked
        # recursively so an unpacked HVSC tree works with no `file =` set.
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            sids_dir = os.path.join(tmp, scene_factory.DEFAULT_WAVEFORM_DIR)
            os.makedirs(os.path.join(sids_dir, "MUSICIANS", "H", "Hubbard_Rob"))
            self._make_files(sids_dir, ["top.sid"])
            self._make_files(
                os.path.join(sids_dir, "MUSICIANS", "H", "Hubbard_Rob"),
                ["Monty_on_the_Run.sid", "skip.txt"],
            )
            os.chdir(tmp)
            try:
                got = scene_factory.resolve_file_spec(
                    scene_factory.DEFAULT_WAVEFORM_DIR, self.EXTS, label="waveform"
                )
                self.assertEqual(
                    sorted(os.path.basename(p) for p in got),
                    ["Monty_on_the_Run.sid", "top.sid"],
                )
            finally:
                os.chdir(cwd)

    def test_other_directories_stay_shallow_even_for_waveform(self):
        # Only the exact default dir gets the recursive treatment — any
        # other directory (e.g. a subdir of it, or an unrelated one) keeps
        # the ordinary shallow listing.
        with tempfile.TemporaryDirectory() as tmp:
            sub = os.path.join(tmp, "sub")
            os.makedirs(sub)
            self._make_files(tmp, ["top.sid"])
            self._make_files(sub, ["deep.sid"])
            got = scene_factory.resolve_file_spec(tmp, self.EXTS, label="waveform")
            self.assertEqual([os.path.basename(p) for p in got], ["top.sid"])

    def test_default_waveform_dir_not_recursive_for_other_labels(self):
        # The recursion exception is keyed to label="waveform" specifically
        # (the scene this default directory belongs to) — a directory
        # spelled "assets/sids" under any other label stays shallow.
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            sids_dir = os.path.join(tmp, scene_factory.DEFAULT_WAVEFORM_DIR)
            os.makedirs(os.path.join(sids_dir, "nested"))
            self._make_files(sids_dir, ["top.sid"])
            self._make_files(os.path.join(sids_dir, "nested"), ["deep.sid"])
            os.chdir(tmp)
            try:
                got = scene_factory.resolve_file_spec(
                    scene_factory.DEFAULT_WAVEFORM_DIR, self.EXTS, label="generative sid audio"
                )
                self.assertEqual([os.path.basename(p) for p in got], ["top.sid"])
            finally:
                os.chdir(cwd)

    def test_tilde_is_expanded(self):
        # A TOML file has no shell to expand `~/…`, and glob/os.path treat a
        # leading `~` as a literal directory name — so without expansion here
        # every `file = "~/Music/…"` in a config fails to match anything.
        with tempfile.TemporaryDirectory() as tmp:
            music = os.path.join(tmp, "Music")
            os.makedirs(music)
            expected = self._make_files(music, ["tune.sid"])
            home_env = {"HOME": tmp}
            if os.name == "nt":  # pragma: no cover - Windows only
                # ntpath.expanduser reads USERPROFILE (then HOMEDRIVE +
                # HOMEPATH) and never looks at HOME, so patching HOME alone
                # leaves `~` pointing at the real profile directory.
                drive, tail = os.path.splitdrive(tmp)
                home_env |= {"USERPROFILE": tmp, "HOMEDRIVE": drive, "HOMEPATH": tail}
            with mock.patch.dict(os.environ, home_env):
                for spec in ("~/Music", "~/Music/tune.sid", "~/Music/*.sid"):
                    with self.subTest(spec=spec):
                        got = scene_factory.resolve_file_spec(spec, self.EXTS, label="waveform")
                        # normpath because the claim under test is "the same
                        # files", not "the same spelling": expansion keeps the
                        # separators the spec was written with, so on Windows a
                        # `~/Music/…` spec yields a working but mixed-separator
                        # path that os.path.join would have spelled with `\`.
                        self.assertEqual(
                            [os.path.normpath(p) for p in got],
                            [os.path.normpath(p) for p in expected],
                        )

    def test_urls_are_not_treated_as_paths(self):
        # A URL passes through untouched — it must not be globbed, expanded,
        # or existence-checked.
        url = "https://example.com/clip.mp4"
        self.assertEqual(scene_factory.resolve_file_spec(url, (".mp4",), label="video"), [url])

    def test_glob_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_files(tmp, ["alpha.sid", "beta.sid", "skip.txt"])
            got = scene_factory.resolve_file_spec(
                os.path.join(tmp, "*.sid"), self.EXTS, label="waveform"
            )
            self.assertEqual([os.path.basename(p) for p in got], ["alpha.sid", "beta.sid"])

    def test_glob_with_no_matches_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "matched no files"):
                scene_factory.resolve_file_spec(
                    os.path.join(tmp, "nope-*.sid"), self.EXTS, label="waveform"
                )

    def test_recursive_glob_walks_subdirectories(self):
        # `**` recurses into subdirs (an unpacked HVSC tree lives under nested
        # dirs) and matches zero-or-more levels, so a top-level file is found too.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "a", "b"))
            self._make_files(tmp, ["top.sid"])
            self._make_files(os.path.join(tmp, "a"), ["mid.sid"])
            self._make_files(os.path.join(tmp, "a", "b"), ["deep.sid", "skip.txt"])
            got = scene_factory.resolve_file_spec(
                os.path.join(tmp, "**", "*.sid"), self.EXTS, label="waveform"
            )
            self.assertEqual(
                sorted(os.path.basename(p) for p in got), ["deep.sid", "mid.sid", "top.sid"]
            )

    def test_nonrecursive_glob_unaffected(self):
        # A plain `*` glob still matches only its own level (no `**`) — the
        # recursive=True flag is backward-compatible.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sub"))
            self._make_files(tmp, ["top.sid"])
            self._make_files(os.path.join(tmp, "sub"), ["deep.sid"])
            got = scene_factory.resolve_file_spec(
                os.path.join(tmp, "*.sid"), self.EXTS, label="waveform"
            )
            self.assertEqual([os.path.basename(p) for p in got], ["top.sid"])

    def test_comma_combination_unions_and_dedupes(self):
        # Mix of literal + directory + glob; overlapping picks dedupe.
        with tempfile.TemporaryDirectory() as tmp:
            self._make_files(tmp, ["x.sid", "y.sid", "z.sid", "skip.mp4"])
            literal = os.path.join(tmp, "x.sid")
            spec = (
                f"{literal}, {tmp}, "  # x.sid + dir (x,y,z)
                f"{os.path.join(tmp, 'z.sid')}"
            )  # dup
            got = scene_factory.resolve_file_spec(spec, self.EXTS, label="waveform")
            self.assertEqual([os.path.basename(p) for p in got], ["x.sid", "y.sid", "z.sid"])

    def test_empty_spec_raises(self):
        with self.assertRaisesRegex(ValueError, "file spec is empty"):
            scene_factory.resolve_file_spec("", self.EXTS, label="waveform")

    def test_whitespace_only_entries_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            [p] = self._make_files(tmp, ["solo.sid"])
            # Trailing comma + a whitespace-only entry shouldn't break it.
            self.assertEqual(
                scene_factory.resolve_file_spec(f"{p}, , ", self.EXTS, label="waveform"), [p]
            )

    def test_video_scene_resolves_glob_at_validate_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_files(tmp, ["a.mp4", "b.mp4"])
            s = cfgmod.SceneCfg(type="video", file=os.path.join(tmp, "*.mp4"))
            # Should NOT raise.
            scene_factory.validate_scene_cfg(s, cfgmod.Config(), audio_enabled=False)

    def test_video_scene_rejects_dir_with_no_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Put only SIDs in a directory the video scene points at.
            with open(os.path.join(tmp, "nope.sid"), "w") as f:
                f.write("")
            s = cfgmod.SceneCfg(type="video", file=tmp)
            with self.assertRaisesRegex(ValueError, "contains no files with extension"):
                scene_factory.validate_scene_cfg(s, cfgmod.Config(), audio_enabled=False)


class SceneAudioAttachmentTest(unittest.TestCase):
    """build_scene wires each scene's `audio` field from the global
    [audio].enabled flag, with per-scene `audio = false` as an opt-out.
    Verifies the wiring without instantiating the real AudioStreamer
    (which would touch sounddevice and a live U64)."""

    def setUp(self):
        # Local imports keep this test file importable without the test
        # _fakes module on sys.path elsewhere.
        import os
        import sys
        from typing import cast

        from c64cast.audio.audio import AudioStreamer
        from c64cast.hw.api import Ultimate64API

        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from _fakes import FakeAPI

        self.api = cast(Ultimate64API, FakeAPI())
        # AudioStreamer's only role in build_scene is to be stored on the
        # Scene; a sentinel object is enough to verify the wiring.
        self.audio_sentinel = cast(AudioStreamer, object())
        # WebcamSource is similarly only stored on the scene; the webcam
        # branch checks `source is None`, anything truthy passes.
        from c64cast.video.video import WebcamSource

        self.source = cast(WebcamSource, object())
        self.cfg = cfgmod.Config()

    def test_webcam_picks_up_global_audio_by_default(self):
        # [audio].enabled (on by default) constructs an AudioStreamer at
        # startup. A webcam scene with no per-scene override must attach
        # it automatically — otherwise audio is silently a no-op, which is
        # what the user reported.
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = scene_factory.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertIs(scene.audio, self.audio_sentinel)

    def test_webcam_audio_false_opts_out_even_when_global_on(self):
        s = cfgmod.SceneCfg(type="webcam", display="petscii", audio=False)
        scene = scene_factory.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertIsNone(scene.audio)

    def test_webcam_no_audio_when_global_off(self):
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = scene_factory.build_scene(s, self.cfg, self.api, None, self.source)
        self.assertIsNone(scene.audio)

    def test_blank_picks_up_global_audio_by_default(self):
        s = cfgmod.SceneCfg(type="blank")
        scene = scene_factory.build_scene(s, self.cfg, self.api, self.audio_sentinel, None)
        self.assertIs(scene.audio, self.audio_sentinel)

    def test_blank_audio_false_opts_out_even_when_global_on(self):
        s = cfgmod.SceneCfg(type="blank", audio=False)
        scene = scene_factory.build_scene(s, self.cfg, self.api, self.audio_sentinel, None)
        self.assertIsNone(scene.audio)

    def test_pre_emphasis_falls_back_to_global(self):
        # No per-scene value → scene inherits the global [dsp].pre_emphasis.
        self.cfg.dsp.pre_emphasis = 0.4
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = scene_factory.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertEqual(scene.pre_emphasis, 0.4)

    def test_pre_emphasis_scene_override_wins(self):
        self.cfg.dsp.pre_emphasis = 0.4
        s = cfgmod.SceneCfg(type="webcam", display="petscii", pre_emphasis=0.9)
        scene = scene_factory.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertEqual(scene.pre_emphasis, 0.9)

    def test_pre_emphasis_defaults_to_none_auto(self):
        # Both unset → None propagates (AudioDSP resolves source-aware later).
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = scene_factory.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertIsNone(scene.pre_emphasis)


class SceneDurationDefaultTest(unittest.TestCase):
    """build_scene's duration resolution: webcam/blank default to infinite
    in a single-scene playlist ("leave the camera running"), keep 30 s in a
    multi-scene playlist (so the rotation still advances), and treat
    duration_s = 0 as a universal "run forever" sentinel."""

    def setUp(self):
        import os
        import sys
        from typing import cast

        from c64cast.hw.api import Ultimate64API
        from c64cast.video.video import WebcamSource

        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from _fakes import FakeAPI

        self.api = cast(Ultimate64API, FakeAPI())
        self.source = cast(WebcamSource, object())

    def _cfg(self, *scenes: cfgmod.SceneCfg) -> cfgmod.Config:
        cfg = cfgmod.Config()
        cfg.scenes = list(scenes)
        return cfg

    def _build(self, cfg: cfgmod.Config, s: cfgmod.SceneCfg):
        return scene_factory.build_scene(s, cfg, self.api, None, self.source)

    def test_single_scene_webcam_unset_is_infinite(self):
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = self._build(self._cfg(s), s)
        self.assertTrue(math.isinf(scene.duration_s))

    def test_single_scene_blank_unset_is_infinite(self):
        s = cfgmod.SceneCfg(type="blank")
        scene = self._build(self._cfg(s), s)
        self.assertTrue(math.isinf(scene.duration_s))

    def test_multi_scene_webcam_unset_stays_30s(self):
        # Two scenes → rotation; an infinite live scene would wedge it, so
        # the webcam keeps the finite base default and advances.
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        other = cfgmod.SceneCfg(type="blank")
        scene = self._build(self._cfg(s, other), s)
        self.assertEqual(scene.duration_s, 30.0)

    def test_zero_is_run_forever_sentinel_even_in_multi_scene(self):
        # Explicit 0 overrides the finite multi-scene default.
        s = cfgmod.SceneCfg(type="webcam", display="petscii", duration_s=0)
        other = cfgmod.SceneCfg(type="blank")
        scene = self._build(self._cfg(s, other), s)
        self.assertTrue(math.isinf(scene.duration_s))

    def test_positive_duration_honored(self):
        s = cfgmod.SceneCfg(type="webcam", display="petscii", duration_s=45.0)
        scene = self._build(self._cfg(s), s)
        self.assertEqual(scene.duration_s, 45.0)


class FollowerOnlyRotationFilterTest(unittest.TestCase):
    """scenes_from_config skips follower_only scenes — they're available
    for follower-override lookup via cfg.scenes but must never reach the
    Playlist's rotation list."""

    def setUp(self):
        import os
        import sys
        from typing import cast

        from c64cast.hw.api import Ultimate64API

        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from _fakes import FakeAPI

        self.api = cast(Ultimate64API, FakeAPI())
        self.cfg = cfgmod.Config()
        self.cfg.playlist.interleave_videos = False

    def test_follower_only_excluded_from_rotation(self):
        self.cfg.scenes = [
            cfgmod.SceneCfg(type="blank", name="idle"),
            cfgmod.SceneCfg(type="blank", name="hello", follower_only=True),
        ]
        built = scene_factory.scenes_from_config(self.cfg, self.api, None, None)
        names = [s.name for s in built]
        self.assertEqual(names, ["idle"])

    def test_a_scenes_cfg_index_is_its_place_in_the_file(self):
        # The live-tune save-back writes a per-scene knob by index into a config
        # it re-reads, so the stamp has to count [[scenes]] blocks — including
        # the follower-only one, which is in the file and not in the rotation.
        self.cfg.scenes = [
            cfgmod.SceneCfg(type="blank", name="idle"),
            cfgmod.SceneCfg(type="blank", name="hello", follower_only=True),
            cfgmod.SceneCfg(type="blank", name="outro"),
        ]
        built = scene_factory.scenes_from_config(self.cfg, self.api, None, None)
        self.assertEqual([(s.name, s.cfg_index) for s in built], [("idle", 0), ("outro", 2)])

    def test_a_scene_no_block_named_carries_no_index(self):
        # The no-scenes fallback is built here and is in no config, so there is
        # nothing for a save-back to address — and None says so.
        from c64cast.video.video import WebcamSource

        source = cast(WebcamSource, object())
        self.cfg.scenes = []
        built = scene_factory.scenes_from_config(self.cfg, self.api, None, source)
        self.assertEqual([s.cfg_index for s in built], [None])

    def test_follower_only_still_validated(self):
        # A bad cfg in a follower_only scene must surface at load time,
        # not at the moment the broadcast actually fires.
        self.cfg.scenes = [
            cfgmod.SceneCfg(type="blank", name="idle"),
            cfgmod.SceneCfg(
                type="blank", name="hello", follower_only=True, display="hires"
            ),  # invalid for blank scene
        ]
        with self.assertRaises(ValueError):
            scene_factory.scenes_from_config(self.cfg, self.api, None, None)


class BuildSceneVideoUrlTest(unittest.TestCase):
    """build_scene resolves a single media URL in the config path — the same
    yt-dlp resolution quick playback uses — so configs accept YouTube et al.
    `_ytdlp_available` is forced True so the offline gate doesn't trip when the
    `yt` extra is absent (e.g. in CI), and resolve_media_url is faked so no
    network/dep is needed."""

    def _build(self, file: str, **kw):
        from c64cast.scenes.scenes import VideoScene

        s = cfgmod.SceneCfg(type="video", display="mhires", file=file, **kw)
        scene = scene_factory.build_scene(
            s, cfgmod.Config(), cast(C64Backend, FakeAPI()), None, None
        )
        assert isinstance(scene, VideoScene)  # narrows for start_s/file_spec access
        return scene

    def test_youtube_url_resolved_with_timestamp_and_title(self):
        from c64cast.app.quickcast import ResolvedMedia

        with (
            mock.patch("c64cast.app.quickcast._ytdlp_available", return_value=True),
            mock.patch(
                "c64cast.app.quickcast.resolve_media_url",
                return_value=ResolvedMedia("http://stream/v.m3u8", "video", title="Cool Tune"),
            ),
        ):
            scene = self._build("https://youtu.be/abc?t=1m30s")
        self.assertEqual(scene.file_spec, "http://stream/v.m3u8")
        self.assertEqual(scene.start_s, 90.0)
        self.assertEqual(scene.name, "Cool Tune")

    def test_youtube_url_carries_uploader_license_webpage_url_onto_scene(self):
        from c64cast.app.quickcast import ResolvedMedia

        with (
            mock.patch("c64cast.app.quickcast._ytdlp_available", return_value=True),
            mock.patch(
                "c64cast.app.quickcast.resolve_media_url",
                return_value=ResolvedMedia(
                    "http://stream/v.m3u8",
                    "video",
                    title="Cool Tune",
                    uploader="Some Channel",
                    license="CC BY",
                    webpage_url="https://youtu.be/abc",
                ),
            ),
        ):
            scene = self._build("https://youtu.be/abc")
        assert scene.source_info is not None
        self.assertEqual(scene.source_info.uploader, "Some Channel")
        self.assertEqual(scene.source_info.license, "CC BY")
        self.assertEqual(scene.source_info.webpage_url, "https://youtu.be/abc")

    def test_local_video_has_no_source_attribution(self):
        scene = self._build("video.mp4")
        self.assertIsNone(scene.source_info)

    def test_explicit_start_s_wins_over_url_timestamp(self):
        from c64cast.app.quickcast import ResolvedMedia

        with (
            mock.patch("c64cast.app.quickcast._ytdlp_available", return_value=True),
            mock.patch(
                "c64cast.app.quickcast.resolve_media_url",
                return_value=ResolvedMedia("http://stream/v.m3u8", "video", title="T"),
            ),
        ):
            scene = self._build("https://youtu.be/abc?t=30", start_s=99.0)
        self.assertEqual(scene.start_s, 99.0)

    def test_audio_only_url_rejected_at_build(self):
        from c64cast.app.quickcast import ResolvedMedia

        with (
            mock.patch("c64cast.app.quickcast._ytdlp_available", return_value=True),
            mock.patch(
                "c64cast.app.quickcast.resolve_media_url",
                return_value=ResolvedMedia("http://stream/a", "audio"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "audio"):
                self._build("https://youtu.be/abc")


class DacBitmapTempoValidationTest(unittest.TestCase):
    """validate_dac_bitmap_tempo_cfg bounds the bitmap+DAC tempo fractions to
    0.5..1.0 (atempo's single-stage floor; 1.0 = off)."""

    def _cfg(self, **audio_kw) -> cfgmod.Config:
        cfg = cfgmod.Config()
        for k, v in audio_kw.items():
            setattr(cfg.audio, k, v)
        return cfg

    def test_defaults_ok(self):
        scene_factory.validate_dac_bitmap_tempo_cfg(self._cfg())  # default 0.88

    def test_off_value_ok(self):
        scene_factory.validate_dac_bitmap_tempo_cfg(
            self._cfg(dac_bitmap_tempo_hires=1.0, dac_bitmap_tempo_mhires=1.0)
        )

    def test_lower_bound_ok(self):
        scene_factory.validate_dac_bitmap_tempo_cfg(self._cfg(dac_bitmap_tempo_mhires=0.5))

    def test_below_floor_raises(self):
        with self.assertRaisesRegex(cfgmod.ConfigError, "dac_bitmap_tempo_mhires"):
            scene_factory.validate_dac_bitmap_tempo_cfg(self._cfg(dac_bitmap_tempo_mhires=0.4))

    def test_above_one_raises(self):
        with self.assertRaisesRegex(cfgmod.ConfigError, "dac_bitmap_tempo_hires"):
            scene_factory.validate_dac_bitmap_tempo_cfg(self._cfg(dac_bitmap_tempo_hires=1.1))

    def test_noop_when_audio_disabled(self):
        # A bad value shouldn't block a run with audio off.
        cfg = self._cfg(dac_bitmap_tempo_hires=0.1)
        cfg.audio.enabled = False
        scene_factory.validate_dac_bitmap_tempo_cfg(cfg)


class DitherResolutionTest(unittest.TestCase):
    """resolve_dither_method's "auto" picks the best method that's actually
    USEFUL per scene type: floyd-steinberg (composed once, cost is a
    non-issue) for static slideshow scenes, blue_noise (vectorized, no added
    shimmer, no Bayer grid structure) for everything recomposed every frame.
    Non-auto values — including the older 'ordered' Bayer method — pass
    through unchanged regardless of scene type."""

    def test_auto_resolves_static_scene_to_floyd_steinberg(self):
        self.assertEqual(
            scene_factory.resolve_dither_method("auto", "slideshow"), "floyd-steinberg"
        )

    def test_auto_resolves_motion_scenes_to_blue_noise(self):
        for scene_type in ("video", "webcam", "generative"):
            with self.subTest(scene_type=scene_type):
                self.assertEqual(
                    scene_factory.resolve_dither_method("auto", scene_type), "blue_noise"
                )

    def test_explicit_value_passes_through_on_any_scene_type(self):
        for scene_type in ("slideshow", "video", "webcam", "generative"):
            with self.subTest(scene_type=scene_type):
                self.assertEqual(
                    scene_factory.resolve_dither_method("floyd-steinberg", scene_type),
                    "floyd-steinberg",
                )
                self.assertEqual(scene_factory.resolve_dither_method("none", scene_type), "none")
                self.assertEqual(
                    scene_factory.resolve_dither_method("ordered", scene_type), "ordered"
                )


class ValidateDitherCfgTest(unittest.TestCase):
    def test_default_config_is_valid(self):
        scene_factory.validate_dither_cfg(cfgmod.Config())

    def test_unknown_method_raises(self):
        cfg = cfgmod.Config()
        cfg.color.dither = "bogus"
        with self.assertRaisesRegex(cfgmod.ConfigError, "dither"):
            scene_factory.validate_dither_cfg(cfg)

    def test_strength_out_of_range_raises(self):
        cfg = cfgmod.Config()
        cfg.color.dither_strength = 3.0
        with self.assertRaisesRegex(cfgmod.ConfigError, "dither_strength"):
            scene_factory.validate_dither_cfg(cfg)

    def test_negative_strength_raises(self):
        cfg = cfgmod.Config()
        cfg.color.dither_strength = -0.1
        with self.assertRaisesRegex(cfgmod.ConfigError, "dither_strength"):
            scene_factory.validate_dither_cfg(cfg)


class ColorMatchResolutionTest(unittest.TestCase):
    """resolve_color_match's "auto" resolves to perceptual on the quantizing
    modes (mcm/mhires/hires/petscii) and rgb on the non-color-picking ones
    (blank/hires_edges). Explicit rgb/perceptual pass through on any mode."""

    def test_auto_resolves_quantizing_modes_to_perceptual(self):
        for mode in ("mcm", "mhires", "hires", "petscii"):
            with self.subTest(mode=mode):
                self.assertTrue(scene_factory.resolve_color_match("auto", mode))

    def test_auto_resolves_non_color_modes_to_rgb(self):
        for mode in ("blank", "hires_edges"):
            with self.subTest(mode=mode):
                self.assertFalse(scene_factory.resolve_color_match("auto", mode))

    def test_explicit_value_passes_through_on_any_mode(self):
        for mode in ("mcm", "mhires", "hires", "petscii", "blank", "hires_edges"):
            with self.subTest(mode=mode):
                self.assertTrue(scene_factory.resolve_color_match("perceptual", mode))
                self.assertFalse(scene_factory.resolve_color_match("rgb", mode))


class ValidateColorMatchCfgTest(unittest.TestCase):
    def test_default_config_is_valid(self):
        scene_factory.validate_color_match_cfg(cfgmod.Config())

    def test_explicit_values_valid(self):
        for v in ("rgb", "perceptual"):
            cfg = cfgmod.Config()
            cfg.color.color_match = v
            scene_factory.validate_color_match_cfg(cfg)

    def test_unknown_value_raises(self):
        cfg = cfgmod.Config()
        cfg.color.color_match = "lab"  # not a valid choice name
        with self.assertRaisesRegex(cfgmod.ConfigError, "color_match"):
            scene_factory.validate_color_match_cfg(cfg)


class CellStrategyResolutionTest(unittest.TestCase):
    """resolve_cell_strategy's "auto" picks error-min for static slideshow
    scenes (composed once, so the per-cell trio search cost is paid once) and
    frequency for motion scenes (recomposed every frame, where frequency's
    temporal stability avoids per-frame slot churn). Explicit values pass
    through unchanged regardless of scene type."""

    def test_auto_resolves_static_scene_to_error_min(self):
        self.assertEqual(scene_factory.resolve_cell_strategy("auto", "slideshow"), "error-min")

    def test_auto_resolves_motion_scenes_to_frequency(self):
        for scene_type in ("video", "webcam", "generative"):
            with self.subTest(scene_type=scene_type):
                self.assertEqual(
                    scene_factory.resolve_cell_strategy("auto", scene_type), "frequency"
                )

    def test_explicit_value_passes_through_on_any_scene_type(self):
        for scene_type in ("slideshow", "video", "webcam", "generative"):
            for strat in ("frequency", "luminance", "contrast", "error-min"):
                with self.subTest(scene_type=scene_type, strat=strat):
                    self.assertEqual(scene_factory.resolve_cell_strategy(strat, scene_type), strat)


class ValidateCellStrategyCfgTest(unittest.TestCase):
    def test_default_config_is_valid(self):
        scene_factory.validate_cell_strategy_cfg(cfgmod.Config())

    def test_explicit_values_valid(self):
        for v in ("frequency", "luminance", "contrast", "error-min"):
            cfg = cfgmod.Config()
            cfg.color.cell_strategy = v
            scene_factory.validate_cell_strategy_cfg(cfg)

    def test_unknown_value_raises(self):
        cfg = cfgmod.Config()
        cfg.color.cell_strategy = "median"  # not a valid choice name
        with self.assertRaisesRegex(cfgmod.ConfigError, "cell_strategy"):
            scene_factory.validate_cell_strategy_cfg(cfg)


class ValidateMotionSmoothingCfgTest(unittest.TestCase):
    def test_default_config_is_valid(self):
        scene_factory.validate_motion_smoothing_cfg(cfgmod.Config())

    def test_range_bounds_valid(self):
        for v in (0.0, 0.5, 1.0):
            cfg = cfgmod.Config()
            cfg.color.motion_smoothing = v
            scene_factory.validate_motion_smoothing_cfg(cfg)

    def test_out_of_range_raises(self):
        for v in (-0.1, 1.5):
            cfg = cfgmod.Config()
            cfg.color.motion_smoothing = v
            with self.assertRaisesRegex(cfgmod.ConfigError, "motion_smoothing"):
                scene_factory.validate_motion_smoothing_cfg(cfg)


class MotionSmoothingWiringTest(unittest.TestCase):
    """[color].motion_smoothing scales BOTH temporal buffers in the mhires
    percell path: 1.0 = the legacy EMA alpha + hysteresis; 0.0 = no smoothing
    (EMA passthrough, zero hysteresis) so the render tracks the source exactly."""

    def _mode(self, s):
        from c64cast.video.modes import MultiHiresDisplayMode

        return MultiHiresDisplayMode(motion_smoothing=s, perceptual=True)

    def test_full_smoothing_matches_legacy(self):
        from c64cast.video import modes

        m = self._mode(1.0)
        self.assertAlmostEqual(m._ema_alpha, modes.PERCELL_PICK_EMA_ALPHA)
        # hysteresis == base * perceptual penalty scale (mhires auto → perceptual)
        self.assertAlmostEqual(
            m._quant_hysteresis, modes.PERCELL_QUANT_HYSTERESIS_BONUS * m._penalty_scale
        )

    def test_zero_smoothing_disables_both_buffers(self):
        m = self._mode(0.0)
        self.assertAlmostEqual(m._ema_alpha, 1.0)  # new frame fully replaces history
        self.assertEqual(m._quant_hysteresis, 0.0)
        self.assertEqual(m._code_hysteresis, 0.0)

    def test_monotonic_between(self):
        lo, mid, hi = self._mode(0.0), self._mode(0.5), self._mode(1.0)
        # more smoothing → smaller EMA alpha (longer memory) and larger hysteresis
        self.assertGreater(lo._ema_alpha, mid._ema_alpha)
        self.assertGreater(mid._ema_alpha, hi._ema_alpha)
        self.assertLess(lo._quant_hysteresis, mid._quant_hysteresis)
        self.assertLess(mid._quant_hysteresis, hi._quant_hysteresis)

    def test_config_path_forwards_value(self):
        from typing import cast

        from c64cast.video.modes import MultiHiresDisplayMode

        mode = scene_factory._build_display_mode(
            "mhires", color=cfgmod.ColorCfg(motion_smoothing=0.0)
        )
        self.assertAlmostEqual(cast(MultiHiresDisplayMode, mode)._ema_alpha, 1.0)


class BuildSceneTempoScaleTest(unittest.TestCase):
    """build_scene resolves VideoScene.tempo_scale: the observed bitmap+DAC
    speed fraction on the host-DMA DAC path over a bitmap mode, else 1.0 (off)
    for the sampler, the REU pump, char modes, and muted scenes."""

    def setUp(self):
        from c64cast.audio.audio import AudioStreamer

        self._tmp = tempfile.TemporaryDirectory()
        self.clip = os.path.join(self._tmp.name, "clip.mp4")
        with open(self.clip, "wb") as f:
            f.write(b"\x00")  # resolve_file_spec only checks existence + ext
        self.audio = cast(AudioStreamer, object())

    def tearDown(self):
        self._tmp.cleanup()

    def _scene(self, cfg: cfgmod.Config, *, display: str, audio, **build_kw):
        from c64cast.scenes.scenes import VideoScene

        s = cfgmod.SceneCfg(type="video", display=display, file=self.clip)
        scene = scene_factory.build_scene(
            s, cfg, cast(C64Backend, FakeAPI()), audio, None, **build_kw
        )
        assert isinstance(scene, VideoScene)
        return scene

    def _dac_cfg(self) -> cfgmod.Config:
        cfg = cfgmod.Config()
        cfg.audio.backend = "dac"
        # Distinct per-mode values prove the mode→field mapping.
        cfg.audio.dac_bitmap_tempo_hires = 0.90
        cfg.audio.dac_bitmap_tempo_mhires = 0.80
        return cfg

    def test_dac_mhires_uses_mhires_factor(self):
        scene = self._scene(self._dac_cfg(), display="mhires", audio=self.audio)
        self.assertEqual(scene.tempo_scale, 0.80)

    def test_dac_hires_uses_hires_factor(self):
        scene = self._scene(self._dac_cfg(), display="hires", audio=self.audio)
        self.assertEqual(scene.tempo_scale, 0.90)

    def test_dac_hires_edges_uses_hires_factor(self):
        # hires_edges shares the Hires VIC fetch → the hires factor.
        scene = self._scene(self._dac_cfg(), display="hires_edges", audio=self.audio)
        self.assertEqual(scene.tempo_scale, 0.90)

    def test_dac_petscii_is_off(self):
        scene = self._scene(self._dac_cfg(), display="petscii", audio=self.audio)
        self.assertEqual(scene.tempo_scale, 1.0)

    def test_dac_mcm_is_off(self):
        scene = self._scene(self._dac_cfg(), display="mcm", audio=self.audio)
        self.assertEqual(scene.tempo_scale, 1.0)

    def test_muted_bitmap_is_off(self):
        # No audio streamer → nothing to compensate.
        scene = self._scene(self._dac_cfg(), display="mhires", audio=None)
        self.assertEqual(scene.tempo_scale, 1.0)

    def test_reu_pump_bitmap_is_off(self):
        cfg = self._dac_cfg()
        cfg.audio.use_reu_pump = True
        scene = self._scene(cfg, display="mhires", audio=self.audio)
        self.assertEqual(scene.tempo_scale, 1.0)

    def test_sampler_bitmap_is_off(self):
        # Sampler path (off the C64 bus) never stretches → no compensation.
        cfg = self._dac_cfg()
        cfg.audio.backend = "auto"
        import dataclasses

        api = FakeAPI()
        api.profile = dataclasses.replace(api.profile, supports_sampler=True)
        with mock.patch("c64cast.app.scene_factory.UltimateAudioSampler", return_value=object()):
            from c64cast.scenes.scenes import VideoScene

            s = cfgmod.SceneCfg(type="video", display="mhires", file=self.clip)
            scene = scene_factory.build_scene(
                s, cfg, cast(C64Backend, api), self.audio, None, sampler_available=True
            )
        assert isinstance(scene, VideoScene)
        self.assertEqual(scene.tempo_scale, 1.0)


if __name__ == "__main__":
    unittest.main()
