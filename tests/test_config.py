"""Smoke tests for c64cast.config — loader, defaults, CLI merge."""

# pyright: reportArgumentType=false
from __future__ import annotations

import argparse
import math
import os
import tempfile
import unittest
from typing import cast
from unittest import mock

from _fakes import FakeAPI

from c64cast import config as cfgmod
from c64cast.backend import C64Backend
from c64cast.modes import BlankDisplayMode


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
        self.assertEqual(cfg.ultimate64.url, "http://ultimate-64-ii.lan")
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
        with self.assertLogs("c64cast.config", level="WARNING") as cm:
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
        with self.assertLogs("c64cast.config", level="WARNING") as cm:
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
        dm = cfgmod._validate_blank(s, cfg)
        assert isinstance(dm, BlankDisplayMode)
        self.assertEqual(dm.border, 14)
        self.assertEqual(dm.background, 0)

    def test_scene_border_index_still_works(self):
        cfg = self._load('[[scenes]]\ntype = "blank"\ndisplay = "blank"\nborder = 6\n')
        dm = cfgmod._validate_blank(cfg.scenes[0], cfg)
        assert isinstance(dm, BlankDisplayMode)
        self.assertEqual(dm.border, 6)

    def test_scene_border_unknown_name_raises_at_build(self):
        cfg = self._load('[[scenes]]\ntype = "blank"\ndisplay = "blank"\nborder = "chartreuse"\n')
        with self.assertRaises(ValueError):
            cfgmod._validate_blank(cfg.scenes[0], cfg)


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
        r = cfgmod.resolve_double_buffer
        # No-REU backend (TeensyROM), bitmap, REU staging off → auto enables.
        self.assertTrue(r("auto", "mhires", use_reu_staged=False, backend_supports_reu=False))
        self.assertTrue(r("auto", "hires", use_reu_staged=False, backend_supports_reu=False))
        # Char modes never (no second VIC bank to flip).
        self.assertFalse(r("auto", "petscii", use_reu_staged=False, backend_supports_reu=False))

    def test_auto_off_on_reu_backend_and_when_staged(self):
        r = cfgmod.resolve_double_buffer
        # U64 (has REU), overlay-free bitmap: auto leaves it off — the REU path
        # is the better tear-free option there.
        self.assertFalse(r("auto", "mhires", use_reu_staged=False, backend_supports_reu=True))
        # Mutually exclusive with REU staging (both flip $DD00).
        self.assertFalse(r("auto", "mhires", use_reu_staged=True, backend_supports_reu=False))

    def test_auto_enables_for_text_overlay_on_reu_backend(self):
        r = cfgmod.resolve_double_buffer
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
        r = cfgmod.resolve_double_buffer
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
        r = cfgmod.resolve_double_buffer
        self.assertTrue(r(True, "mhires", use_reu_staged=False, backend_supports_reu=True))
        self.assertFalse(r(True, "petscii", use_reu_staged=False, backend_supports_reu=False))
        self.assertFalse(r(True, "mhires", use_reu_staged=True, backend_supports_reu=False))
        self.assertFalse(r(False, "mhires", use_reu_staged=False, backend_supports_reu=False))

    def test_bad_string_rejected_at_load(self):
        with self.assertRaises(ValueError) as ctx:
            self._load('[video]\ndouble_buffer = "yes"\n')
        self.assertIn("double_buffer", str(ctx.exception))


class MidiControlLoopAudioTest(unittest.TestCase):
    """validate_midi_control_cfg guards the Phase 4 loop_audio choice."""

    def _cfg(self, loop_audio: str) -> cfgmod.MidiControlCfg:
        from dataclasses import replace

        return replace(cfgmod.MidiControlCfg(), enabled=True, loop_audio=loop_audio)

    def test_on_and_mute_pass(self):
        for good in ("on", "mute"):
            cfgmod.validate_midi_control_cfg(self._cfg(good))  # must not raise

    def test_bad_value_raises(self):
        with self.assertRaises(cfgmod.ConfigError) as ctx:
            cfgmod.validate_midi_control_cfg(self._cfg("loud"))
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
        from c64cast import config_serialize as ser

        cfg = cfgmod.Config()
        cfg.video.device = "Cam Link"
        reloaded = self._load(ser.dumps(cfg))
        self.assertEqual(reloaded.video.device, "Cam Link")


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


class LoadSonglengthsTest(unittest.TestCase):
    def setUp(self):
        # Memoization caches are module-global — clear between tests.
        cfgmod._songlengths_cache.clear()
        self._orig_autodetected = cfgmod._songlengths_autodetected
        cfgmod._songlengths_autodetected = cfgmod._UNSET

    def tearDown(self):
        cfgmod._songlengths_autodetected = self._orig_autodetected

    def test_empty_string_disables_autodetect(self):
        # Explicit "" opts out — unlike None, it never probes assets/sids/.
        with mock.patch.object(cfgmod, "_autodetect_songlengths_path") as auto:
            self.assertIsNone(cfgmod._load_songlengths(""))
            auto.assert_not_called()

    def test_none_path_autodetects_when_present(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md5", delete=False) as f:
            f.write("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=1:23\n")
            path = f.name
        try:
            with mock.patch.object(cfgmod, "_autodetect_songlengths_path", return_value=path):
                with self.assertLogs("c64cast.config", level="INFO") as logs:
                    db = cfgmod._load_songlengths(None)
            self.assertIsNotNone(db)
            self.assertTrue(any("auto-detected" in m for m in logs.output))
        finally:
            os.unlink(path)

    def test_none_path_returns_none_when_nothing_detected(self):
        with mock.patch.object(cfgmod, "_autodetect_songlengths_path", return_value=None):
            self.assertIsNone(cfgmod._load_songlengths(None))

    def test_missing_file_warns_and_caches_none(self):
        with self.assertLogs("c64cast.config", level="WARNING"):
            self.assertIsNone(cfgmod._load_songlengths("/no/such/db.md5"))
        # The None result is memoized so a second call doesn't re-warn.
        self.assertIn("/no/such/db.md5", cfgmod._songlengths_cache)
        self.assertIsNone(cfgmod._load_songlengths("/no/such/db.md5"))

    def test_loads_and_memoizes_real_db(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md5", delete=False) as f:
            f.write("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=1:23\n")
            path = f.name
        try:
            db1 = cfgmod._load_songlengths(path)
            db2 = cfgmod._load_songlengths(path)
            self.assertIsNotNone(db1)
            self.assertIs(db1, db2)  # second call hits the cache
        finally:
            os.unlink(path)


class AutodetectSonglengthsTest(unittest.TestCase):
    def setUp(self):
        self._orig_autodetected = cfgmod._songlengths_autodetected
        cfgmod._songlengths_autodetected = cfgmod._UNSET

    def tearDown(self):
        cfgmod._songlengths_autodetected = self._orig_autodetected

    def test_no_root_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "assets", "sids")
            self.assertIsNone(cfgmod._autodetect_songlengths_path(missing))

    def test_finds_full_hvsc_tree_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = os.path.join(tmp, "C64Music", "DOCUMENTS")
            os.makedirs(docs)
            expected = os.path.join(docs, "Songlengths.md5")
            open(expected, "w").close()
            self.assertEqual(cfgmod._autodetect_songlengths_path(tmp), expected)

    def test_finds_contents_only_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = os.path.join(tmp, "DOCUMENTS")
            os.makedirs(docs)
            expected = os.path.join(docs, "Songlengths.md5")
            open(expected, "w").close()
            self.assertEqual(cfgmod._autodetect_songlengths_path(tmp), expected)

    def test_falls_back_to_full_scan_for_nonstandard_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            odd = os.path.join(tmp, "somewhere", "else")
            os.makedirs(odd)
            expected = os.path.join(odd, "Songlengths.md5")
            open(expected, "w").close()
            self.assertEqual(cfgmod._autodetect_songlengths_path(tmp), expected)

    def test_no_match_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "MUSICIANS"))
            self.assertIsNone(cfgmod._autodetect_songlengths_path(tmp))

    def test_result_is_memoized(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = os.path.join(tmp, "DOCUMENTS")
            os.makedirs(docs)
            open(os.path.join(docs, "Songlengths.md5"), "w").close()
            first = cfgmod._autodetect_songlengths_path(tmp)
            # A second call with a different (nonexistent) root still
            # returns the memoized first result — proves it isn't re-probed.
            second = cfgmod._autodetect_songlengths_path(os.path.join(tmp, "nope"))
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


class ValidateSceneCfgTest(unittest.TestCase):
    """Direct tests for `validate_scene_cfg` — the seam doctor mode and
    `build_scene` both go through. Covers every per-scene ValueError path
    that used to live inline in `build_scene`."""

    def _cfg(self) -> cfgmod.Config:
        return cfgmod.Config()

    def test_valid_blank_scene_passes(self):
        s = cfgmod.SceneCfg(type="blank")
        cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_negative_duration_rejected(self):
        s = cfgmod.SceneCfg(type="blank", duration_s=-1.0)
        with self.assertRaisesRegex(ValueError, "duration_s must be >= 0"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_zero_duration_allowed(self):
        # 0 is the "run forever" sentinel, not an error.
        s = cfgmod.SceneCfg(type="blank", duration_s=0)
        cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_blank_scene_rejects_wrong_display(self):
        s = cfgmod.SceneCfg(type="blank", display="mhires")
        with self.assertRaisesRegex(ValueError, "blank scene must use"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

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
                cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
                # validate_scene_cfg normalizes s.file to the default dir.
                self.assertEqual(s.file, cfgmod.DEFAULT_VIDEO_DIR)
            finally:
                os.chdir(cwd)

    def test_video_scene_no_file_and_no_default_dir_raises(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="video")
                with self.assertRaisesRegex(ValueError, "default directory .* is missing or empty"):
                    cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
            finally:
                os.chdir(cwd)

    def test_video_scene_rejects_duration_s(self):
        # Video lifetime is video-driven; a finite duration_s would
        # either be a silent no-op or truncate the file. Loader must reject
        # it at config time rather than letting the inconsistency lurk.
        s = cfgmod.SceneCfg(type="video", file="video.mp4", duration_s=30.0)
        with self.assertRaisesRegex(ValueError, "does not accept .*duration_s"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_scene_without_duration_s_passes(self):
        # The default (None) means "no duration_s declared" and must pass
        # validation cleanly — that's the supported config shape.
        s = cfgmod.SceneCfg(type="video", file="video.mp4")
        cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_scene_accepts_start_s(self):
        s = cfgmod.SceneCfg(type="video", file="video.mp4", start_s=90.0)
        cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_scene_rejects_negative_start_s(self):
        s = cfgmod.SceneCfg(type="video", file="video.mp4", start_s=-1.0)
        with self.assertRaisesRegex(ValueError, "start_s must be >= 0"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_start_s_rejected_on_non_video(self):
        # start_s is a video-only seek; setting it elsewhere is a no-op the
        # loader rejects rather than silently ignores.
        s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", start_s=10.0)
        with self.assertRaisesRegex(ValueError, "start_s is only supported on video"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_url_needing_ytdlp_rejected_without_extra(self):
        # Offline doctor/load check: a YouTube-style URL needs yt-dlp; without
        # the `yt` extra, flag it up front instead of failing at playback.
        s = cfgmod.SceneCfg(type="video", file="https://youtu.be/abc?t=90")
        with mock.patch("c64cast.quickcast._ytdlp_available", return_value=False):
            with self.assertRaisesRegex(ValueError, "yt-dlp"):
                cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_video_url_needing_ytdlp_passes_with_extra(self):
        s = cfgmod.SceneCfg(type="video", file="https://youtu.be/abc?t=90")
        with mock.patch("c64cast.quickcast._ytdlp_available", return_value=True):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_direct_media_url_does_not_require_extra(self):
        # A direct media URL plays via PyAV without yt-dlp — no extra needed
        # even when it's absent.
        s = cfgmod.SceneCfg(type="video", file="http://host/clip.mp4")
        with mock.patch("c64cast.quickcast._ytdlp_available", return_value=False):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_waveform_scene_falls_back_to_default_dir(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "assets", "sids"))
            with open(os.path.join(tmp, "assets", "sids", "tune.sid"), "w") as f:
                f.write("")
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="waveform")
                cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
                self.assertEqual(s.file, cfgmod.DEFAULT_WAVEFORM_DIR)
            finally:
                os.chdir(cwd)

    def test_waveform_scene_no_file_and_no_default_dir_raises(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="waveform")
                with self.assertRaisesRegex(ValueError, "default directory .* is missing or empty"):
                    cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
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
                cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
                self.assertEqual(s.file, cfgmod.DEFAULT_SLIDESHOW_DIR)
            finally:
                os.chdir(cwd)

    def test_slideshow_scene_no_file_and_no_default_dir_raises(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="slideshow")
                with self.assertRaisesRegex(ValueError, "default directory .* is missing or empty"):
                    cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
            finally:
                os.chdir(cwd)

    def test_slideshow_image_duration_s_must_be_positive(self):
        s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", image_duration_s=0.0)
        with self.assertRaisesRegex(ValueError, "image_duration_s must be > 0"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_slideshow_aspect_mode_accepts_known_choices(self):
        for mode in cfgmod._ASPECT_MODE_CHOICES:
            s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", aspect_mode=mode)
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_slideshow_aspect_mode_rejects_unknown(self):
        s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", aspect_mode="contain")
        with self.assertRaisesRegex(ValueError, "aspect_mode must be one of"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_slideshow_display_random_resolves_to_known_mode(self):
        for _ in range(10):
            picked = cfgmod._resolve_slideshow_display("random")
            self.assertIn(picked, cfgmod.SLIDESHOW_RANDOM_DISPLAYS)

    def test_slideshow_display_hires_edges_substituted_with_mhires(self):
        # The SceneCfg global default ("hires_edges") is tuned for live
        # webcam Canny edges; slideshow swaps it for mhires (best color
        # for stills).
        self.assertEqual(cfgmod._resolve_slideshow_display("hires_edges"), "mhires")
        # Other explicit choices pass through.
        for name in ("hires", "mhires", "mcm", "petscii"):
            self.assertEqual(cfgmod._resolve_slideshow_display(name), name)

    def test_slideshow_scene_rejects_blank_display(self):
        s = cfgmod.SceneCfg(type="slideshow", file="pic.jpg", display="blank")
        with self.assertRaisesRegex(ValueError, "cannot use display"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_midi_scene_rejects_wrong_adsr_length(self):
        s = cfgmod.SceneCfg(type="midi", midi_adsr=[0, 0, 0])
        with self.assertRaisesRegex(ValueError, "midi_adsr must have 4"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_midi_scene_rejects_bad_voice_mode(self):
        s = cfgmod.SceneCfg(type="midi", midi_voice_mode="poly")
        with self.assertRaisesRegex(ValueError, "midi_voice_mode"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_midi_scene_rejects_bad_voice_waveform(self):
        s = cfgmod.SceneCfg(type="midi", midi_voice_waveforms=["pulse", "square"])
        with self.assertRaisesRegex(ValueError, "midi_voice_waveforms"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_midi_scene_accepts_combined_voice_waveforms(self):
        s = cfgmod.SceneCfg(
            type="midi", midi_voice_waveforms=["pulse+triangle", "sawtooth", "noise"]
        )
        cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)  # no raise

    def test_midi_scene_rejects_bad_voice_channels_when_multitimbral(self):
        s = cfgmod.SceneCfg(
            type="midi", midi_voice_mode="multitimbral", midi_voice_channels=[1, 1, 99]
        )
        with self.assertRaisesRegex(ValueError, "midi_voice_channels"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_unknown_scene_type_rejected(self):
        s = cfgmod.SceneCfg(type="something-bogus")
        with self.assertRaisesRegex(ValueError, "unknown scene type"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_unknown_display_mode_rejected(self):
        s = cfgmod.SceneCfg(type="webcam", display="petsci")
        with self.assertRaisesRegex(ValueError, "unknown display mode"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_text_overlay_accepted_on_mhires(self):
        # `clock` is a text overlay (REQUIRES_PETSCII + SUPPORTS_BITMAP_TEXT):
        # it folds its glyphs into the bitmap, so mhires is valid now.
        s = cfgmod.SceneCfg(type="webcam", display="mhires", overlays=[{"type": "clock"}])
        cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)  # no raise

    def test_text_overlay_rejected_on_mcm(self):
        # mcm is neither PETSCII- nor bitmap-text-compatible (color-RAM bit 3).
        s = cfgmod.SceneCfg(type="webcam", display="mcm", overlays=[{"type": "clock"}])
        with self.assertRaisesRegex(ValueError, "petscii"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_overlay_requires_audio_with_audio_enabled_passes(self):
        # `spectrum_petscii` has REQUIRES_AUDIO; passing audio_enabled=True
        # supplies the sentinel so validation succeeds.
        s = cfgmod.SceneCfg(
            type="webcam", display="petscii", overlays=[{"type": "spectrum_petscii"}]
        )
        cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=True)

    def test_overlay_requires_audio_with_audio_disabled_rejected(self):
        s = cfgmod.SceneCfg(
            type="webcam", display="petscii", overlays=[{"type": "spectrum_petscii"}]
        )
        with self.assertRaisesRegex(ValueError, "requires audio"):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_orchestrate_with_no_claiming_subclass_rejected(self):
        # A `blank` scene with no orchestrator-specific shape won't be
        # claimed by BigTextSpanOrchestrator.
        s = cfgmod.SceneCfg(type="blank", name="solo", orchestrate=True)
        from c64cast.orchestrator import OrchestratorError

        with self.assertRaises(OrchestratorError):
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

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
                cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
                self.assertEqual(s.file, cfgmod.DEFAULT_PROGRAM_DIR)
            finally:
                os.chdir(cwd)

    def test_launcher_scene_no_file_and_no_default_dir_raises(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                s = cfgmod.SceneCfg(type="launcher")
                with self.assertRaisesRegex(ValueError, "default directory .* is missing or empty"):
                    cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)
            finally:
                os.chdir(cwd)

    def test_launcher_scene_valid_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = cfgmod.SceneCfg(
                type="launcher", file=self._prg(tmp), duration_s=90.0, input_source="cia"
            )
            cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_launcher_scene_rejects_overlays(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = cfgmod.SceneCfg(type="launcher", file=self._prg(tmp), overlays=[{"type": "clock"}])
            with self.assertRaisesRegex(ValueError, "cannot carry overlays"):
                cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_launcher_scene_rejects_non_default_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = cfgmod.SceneCfg(type="launcher", file=self._prg(tmp), display="mcm")
            with self.assertRaisesRegex(ValueError, "does not use .*display"):
                cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_launcher_scene_rejects_bad_input_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = cfgmod.SceneCfg(type="launcher", file=self._prg(tmp), input_source="bogus")
            with self.assertRaisesRegex(ValueError, "input_source must be"):
                cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)

    def test_launcher_scene_rejects_bad_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            d64 = os.path.join(tmp, "game.d64")
            with open(d64, "wb") as f:
                f.write(b"")
            s = cfgmod.SceneCfg(type="launcher", file=d64)
            with self.assertRaises(ValueError):
                cfgmod.validate_scene_cfg(s, self._cfg(), audio_enabled=False)


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
            self.assertEqual(cfgmod.resolve_file_spec(p, self.EXTS, label="waveform"), [p])

    def test_literal_path_with_wrong_extension_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected extension"):
            cfgmod.resolve_file_spec("not-a-sid.mp4", self.EXTS, label="waveform")

    def test_directory_expands_to_all_matching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_files(tmp, ["a.sid", "b.sid", "skip.mp4"])
            got = cfgmod.resolve_file_spec(tmp, self.EXTS, label="waveform")
            self.assertEqual([os.path.basename(p) for p in got], ["a.sid", "b.sid"])

    def test_directory_with_no_matches_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_files(tmp, ["only.mp4"])
            with self.assertRaisesRegex(ValueError, "contains no files with extension"):
                cfgmod.resolve_file_spec(tmp, self.EXTS, label="waveform")

    def test_default_waveform_dir_recurses(self):
        # The waveform scene's default directory (assets/sids) is the one
        # exception to the shallow-directory-listing rule: it's walked
        # recursively so an unpacked HVSC tree works with no `file =` set.
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            sids_dir = os.path.join(tmp, cfgmod.DEFAULT_WAVEFORM_DIR)
            os.makedirs(os.path.join(sids_dir, "MUSICIANS", "H", "Hubbard_Rob"))
            self._make_files(sids_dir, ["top.sid"])
            self._make_files(
                os.path.join(sids_dir, "MUSICIANS", "H", "Hubbard_Rob"),
                ["Monty_on_the_Run.sid", "skip.txt"],
            )
            os.chdir(tmp)
            try:
                got = cfgmod.resolve_file_spec(
                    cfgmod.DEFAULT_WAVEFORM_DIR, self.EXTS, label="waveform"
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
            got = cfgmod.resolve_file_spec(tmp, self.EXTS, label="waveform")
            self.assertEqual([os.path.basename(p) for p in got], ["top.sid"])

    def test_default_waveform_dir_not_recursive_for_other_labels(self):
        # The recursion exception is keyed to label="waveform" specifically
        # (the scene this default directory belongs to) — a directory
        # spelled "assets/sids" under any other label stays shallow.
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            sids_dir = os.path.join(tmp, cfgmod.DEFAULT_WAVEFORM_DIR)
            os.makedirs(os.path.join(sids_dir, "nested"))
            self._make_files(sids_dir, ["top.sid"])
            self._make_files(os.path.join(sids_dir, "nested"), ["deep.sid"])
            os.chdir(tmp)
            try:
                got = cfgmod.resolve_file_spec(
                    cfgmod.DEFAULT_WAVEFORM_DIR, self.EXTS, label="generative sid audio"
                )
                self.assertEqual([os.path.basename(p) for p in got], ["top.sid"])
            finally:
                os.chdir(cwd)

    def test_glob_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_files(tmp, ["alpha.sid", "beta.sid", "skip.txt"])
            got = cfgmod.resolve_file_spec(os.path.join(tmp, "*.sid"), self.EXTS, label="waveform")
            self.assertEqual([os.path.basename(p) for p in got], ["alpha.sid", "beta.sid"])

    def test_glob_with_no_matches_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "matched no files"):
                cfgmod.resolve_file_spec(
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
            got = cfgmod.resolve_file_spec(
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
            got = cfgmod.resolve_file_spec(os.path.join(tmp, "*.sid"), self.EXTS, label="waveform")
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
            got = cfgmod.resolve_file_spec(spec, self.EXTS, label="waveform")
            self.assertEqual([os.path.basename(p) for p in got], ["x.sid", "y.sid", "z.sid"])

    def test_empty_spec_raises(self):
        with self.assertRaisesRegex(ValueError, "file spec is empty"):
            cfgmod.resolve_file_spec("", self.EXTS, label="waveform")

    def test_whitespace_only_entries_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            [p] = self._make_files(tmp, ["solo.sid"])
            # Trailing comma + a whitespace-only entry shouldn't break it.
            self.assertEqual(cfgmod.resolve_file_spec(f"{p}, , ", self.EXTS, label="waveform"), [p])

    def test_video_scene_resolves_glob_at_validate_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_files(tmp, ["a.mp4", "b.mp4"])
            s = cfgmod.SceneCfg(type="video", file=os.path.join(tmp, "*.mp4"))
            # Should NOT raise.
            cfgmod.validate_scene_cfg(s, cfgmod.Config(), audio_enabled=False)

    def test_video_scene_rejects_dir_with_no_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Put only SIDs in a directory the video scene points at.
            with open(os.path.join(tmp, "nope.sid"), "w") as f:
                f.write("")
            s = cfgmod.SceneCfg(type="video", file=tmp)
            with self.assertRaisesRegex(ValueError, "contains no files with extension"):
                cfgmod.validate_scene_cfg(s, cfgmod.Config(), audio_enabled=False)


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

        from c64cast.api import Ultimate64API
        from c64cast.audio import AudioStreamer

        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from _fakes import FakeAPI

        self.api = cast(Ultimate64API, FakeAPI())
        # AudioStreamer's only role in build_scene is to be stored on the
        # Scene; a sentinel object is enough to verify the wiring.
        self.audio_sentinel = cast(AudioStreamer, object())
        # WebcamSource is similarly only stored on the scene; the webcam
        # branch checks `source is None`, anything truthy passes.
        from c64cast.video import WebcamSource

        self.source = cast(WebcamSource, object())
        self.cfg = cfgmod.Config()

    def test_webcam_picks_up_global_audio_by_default(self):
        # [audio].enabled (on by default) constructs an AudioStreamer at
        # startup. A webcam scene with no per-scene override must attach
        # it automatically — otherwise audio is silently a no-op, which is
        # what the user reported.
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = cfgmod.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertIs(scene.audio, self.audio_sentinel)

    def test_webcam_audio_false_opts_out_even_when_global_on(self):
        s = cfgmod.SceneCfg(type="webcam", display="petscii", audio=False)
        scene = cfgmod.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertIsNone(scene.audio)

    def test_webcam_no_audio_when_global_off(self):
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = cfgmod.build_scene(s, self.cfg, self.api, None, self.source)
        self.assertIsNone(scene.audio)

    def test_blank_picks_up_global_audio_by_default(self):
        s = cfgmod.SceneCfg(type="blank")
        scene = cfgmod.build_scene(s, self.cfg, self.api, self.audio_sentinel, None)
        self.assertIs(scene.audio, self.audio_sentinel)

    def test_blank_audio_false_opts_out_even_when_global_on(self):
        s = cfgmod.SceneCfg(type="blank", audio=False)
        scene = cfgmod.build_scene(s, self.cfg, self.api, self.audio_sentinel, None)
        self.assertIsNone(scene.audio)

    def test_pre_emphasis_falls_back_to_global(self):
        # No per-scene value → scene inherits the global [dsp].pre_emphasis.
        self.cfg.dsp.pre_emphasis = 0.4
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = cfgmod.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertEqual(scene.pre_emphasis, 0.4)

    def test_pre_emphasis_scene_override_wins(self):
        self.cfg.dsp.pre_emphasis = 0.4
        s = cfgmod.SceneCfg(type="webcam", display="petscii", pre_emphasis=0.9)
        scene = cfgmod.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
        self.assertEqual(scene.pre_emphasis, 0.9)

    def test_pre_emphasis_defaults_to_none_auto(self):
        # Both unset → None propagates (AudioDSP resolves source-aware later).
        s = cfgmod.SceneCfg(type="webcam", display="petscii")
        scene = cfgmod.build_scene(s, self.cfg, self.api, self.audio_sentinel, self.source)
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

        from c64cast.api import Ultimate64API
        from c64cast.video import WebcamSource

        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from _fakes import FakeAPI

        self.api = cast(Ultimate64API, FakeAPI())
        self.source = cast(WebcamSource, object())

    def _cfg(self, *scenes: cfgmod.SceneCfg) -> cfgmod.Config:
        cfg = cfgmod.Config()
        cfg.scenes = list(scenes)
        return cfg

    def _build(self, cfg: cfgmod.Config, s: cfgmod.SceneCfg):
        return cfgmod.build_scene(s, cfg, self.api, None, self.source)

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

        from c64cast.api import Ultimate64API

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
        built = cfgmod.scenes_from_config(self.cfg, self.api, None, None)
        names = [s.name for s in built]
        self.assertEqual(names, ["idle"])

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
            cfgmod.scenes_from_config(self.cfg, self.api, None, None)


class BuildSceneVideoUrlTest(unittest.TestCase):
    """build_scene resolves a single media URL in the config path — the same
    yt-dlp resolution quick playback uses — so configs accept YouTube et al.
    `_ytdlp_available` is forced True so the offline gate doesn't trip when the
    `yt` extra is absent (e.g. in CI), and resolve_media_url is faked so no
    network/dep is needed."""

    def _build(self, file: str, **kw):
        from c64cast.scenes import VideoScene

        s = cfgmod.SceneCfg(type="video", display="mhires", file=file, **kw)
        scene = cfgmod.build_scene(s, cfgmod.Config(), cast(C64Backend, FakeAPI()), None, None)
        assert isinstance(scene, VideoScene)  # narrows for start_s/file_spec access
        return scene

    def test_youtube_url_resolved_with_timestamp_and_title(self):
        with (
            mock.patch("c64cast.quickcast._ytdlp_available", return_value=True),
            mock.patch(
                "c64cast.quickcast.resolve_media_url",
                return_value=("http://stream/v.m3u8", "video", "Cool Tune"),
            ),
        ):
            scene = self._build("https://youtu.be/abc?t=1m30s")
        self.assertEqual(scene.file_spec, "http://stream/v.m3u8")
        self.assertEqual(scene.start_s, 90.0)
        self.assertEqual(scene.name, "Cool Tune")

    def test_explicit_start_s_wins_over_url_timestamp(self):
        with (
            mock.patch("c64cast.quickcast._ytdlp_available", return_value=True),
            mock.patch(
                "c64cast.quickcast.resolve_media_url",
                return_value=("http://stream/v.m3u8", "video", "T"),
            ),
        ):
            scene = self._build("https://youtu.be/abc?t=30", start_s=99.0)
        self.assertEqual(scene.start_s, 99.0)

    def test_audio_only_url_rejected_at_build(self):
        with (
            mock.patch("c64cast.quickcast._ytdlp_available", return_value=True),
            mock.patch(
                "c64cast.quickcast.resolve_media_url",
                return_value=("http://stream/a", "audio", None),
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
        cfgmod.validate_dac_bitmap_tempo_cfg(self._cfg())  # default 0.88

    def test_off_value_ok(self):
        cfgmod.validate_dac_bitmap_tempo_cfg(
            self._cfg(dac_bitmap_tempo_hires=1.0, dac_bitmap_tempo_mhires=1.0)
        )

    def test_lower_bound_ok(self):
        cfgmod.validate_dac_bitmap_tempo_cfg(self._cfg(dac_bitmap_tempo_mhires=0.5))

    def test_below_floor_raises(self):
        with self.assertRaisesRegex(cfgmod.ConfigError, "dac_bitmap_tempo_mhires"):
            cfgmod.validate_dac_bitmap_tempo_cfg(self._cfg(dac_bitmap_tempo_mhires=0.4))

    def test_above_one_raises(self):
        with self.assertRaisesRegex(cfgmod.ConfigError, "dac_bitmap_tempo_hires"):
            cfgmod.validate_dac_bitmap_tempo_cfg(self._cfg(dac_bitmap_tempo_hires=1.1))

    def test_noop_when_audio_disabled(self):
        # A bad value shouldn't block a run with audio off.
        cfg = self._cfg(dac_bitmap_tempo_hires=0.1)
        cfg.audio.enabled = False
        cfgmod.validate_dac_bitmap_tempo_cfg(cfg)


class DitherResolutionTest(unittest.TestCase):
    """resolve_dither_method's "auto" picks the best method that's actually
    USEFUL per scene type: floyd-steinberg (composed once, cost is a
    non-issue) for static slideshow scenes, blue_noise (vectorized, no added
    shimmer, no Bayer grid structure) for everything recomposed every frame.
    Non-auto values — including the older 'ordered' Bayer method — pass
    through unchanged regardless of scene type."""

    def test_auto_resolves_static_scene_to_floyd_steinberg(self):
        self.assertEqual(cfgmod.resolve_dither_method("auto", "slideshow"), "floyd-steinberg")

    def test_auto_resolves_motion_scenes_to_blue_noise(self):
        for scene_type in ("video", "webcam", "generative"):
            with self.subTest(scene_type=scene_type):
                self.assertEqual(cfgmod.resolve_dither_method("auto", scene_type), "blue_noise")

    def test_explicit_value_passes_through_on_any_scene_type(self):
        for scene_type in ("slideshow", "video", "webcam", "generative"):
            with self.subTest(scene_type=scene_type):
                self.assertEqual(
                    cfgmod.resolve_dither_method("floyd-steinberg", scene_type), "floyd-steinberg"
                )
                self.assertEqual(cfgmod.resolve_dither_method("none", scene_type), "none")
                self.assertEqual(cfgmod.resolve_dither_method("ordered", scene_type), "ordered")


class ValidateDitherCfgTest(unittest.TestCase):
    def test_default_config_is_valid(self):
        cfgmod.validate_dither_cfg(cfgmod.Config())

    def test_unknown_method_raises(self):
        cfg = cfgmod.Config()
        cfg.color.dither = "bogus"
        with self.assertRaisesRegex(cfgmod.ConfigError, "dither"):
            cfgmod.validate_dither_cfg(cfg)

    def test_strength_out_of_range_raises(self):
        cfg = cfgmod.Config()
        cfg.color.dither_strength = 3.0
        with self.assertRaisesRegex(cfgmod.ConfigError, "dither_strength"):
            cfgmod.validate_dither_cfg(cfg)

    def test_negative_strength_raises(self):
        cfg = cfgmod.Config()
        cfg.color.dither_strength = -0.1
        with self.assertRaisesRegex(cfgmod.ConfigError, "dither_strength"):
            cfgmod.validate_dither_cfg(cfg)


class ColorMatchResolutionTest(unittest.TestCase):
    """resolve_color_match's "auto" resolves to perceptual on the quantizing
    modes (mcm/mhires/hires/petscii) and rgb on the non-color-picking ones
    (blank/hires_edges). Explicit rgb/perceptual pass through on any mode."""

    def test_auto_resolves_quantizing_modes_to_perceptual(self):
        for mode in ("mcm", "mhires", "hires", "petscii"):
            with self.subTest(mode=mode):
                self.assertTrue(cfgmod.resolve_color_match("auto", mode))

    def test_auto_resolves_non_color_modes_to_rgb(self):
        for mode in ("blank", "hires_edges"):
            with self.subTest(mode=mode):
                self.assertFalse(cfgmod.resolve_color_match("auto", mode))

    def test_explicit_value_passes_through_on_any_mode(self):
        for mode in ("mcm", "mhires", "hires", "petscii", "blank", "hires_edges"):
            with self.subTest(mode=mode):
                self.assertTrue(cfgmod.resolve_color_match("perceptual", mode))
                self.assertFalse(cfgmod.resolve_color_match("rgb", mode))


class ValidateColorMatchCfgTest(unittest.TestCase):
    def test_default_config_is_valid(self):
        cfgmod.validate_color_match_cfg(cfgmod.Config())

    def test_explicit_values_valid(self):
        for v in ("rgb", "perceptual"):
            cfg = cfgmod.Config()
            cfg.color.color_match = v
            cfgmod.validate_color_match_cfg(cfg)

    def test_unknown_value_raises(self):
        cfg = cfgmod.Config()
        cfg.color.color_match = "lab"  # not a valid choice name
        with self.assertRaisesRegex(cfgmod.ConfigError, "color_match"):
            cfgmod.validate_color_match_cfg(cfg)


class CellStrategyResolutionTest(unittest.TestCase):
    """resolve_cell_strategy's "auto" picks error-min for static slideshow
    scenes (composed once, so the per-cell trio search cost is paid once) and
    frequency for motion scenes (recomposed every frame, where frequency's
    temporal stability avoids per-frame slot churn). Explicit values pass
    through unchanged regardless of scene type."""

    def test_auto_resolves_static_scene_to_error_min(self):
        self.assertEqual(cfgmod.resolve_cell_strategy("auto", "slideshow"), "error-min")

    def test_auto_resolves_motion_scenes_to_frequency(self):
        for scene_type in ("video", "webcam", "generative"):
            with self.subTest(scene_type=scene_type):
                self.assertEqual(cfgmod.resolve_cell_strategy("auto", scene_type), "frequency")

    def test_explicit_value_passes_through_on_any_scene_type(self):
        for scene_type in ("slideshow", "video", "webcam", "generative"):
            for strat in ("frequency", "luminance", "contrast", "error-min"):
                with self.subTest(scene_type=scene_type, strat=strat):
                    self.assertEqual(cfgmod.resolve_cell_strategy(strat, scene_type), strat)


class ValidateCellStrategyCfgTest(unittest.TestCase):
    def test_default_config_is_valid(self):
        cfgmod.validate_cell_strategy_cfg(cfgmod.Config())

    def test_explicit_values_valid(self):
        for v in ("frequency", "luminance", "contrast", "error-min"):
            cfg = cfgmod.Config()
            cfg.color.cell_strategy = v
            cfgmod.validate_cell_strategy_cfg(cfg)

    def test_unknown_value_raises(self):
        cfg = cfgmod.Config()
        cfg.color.cell_strategy = "median"  # not a valid choice name
        with self.assertRaisesRegex(cfgmod.ConfigError, "cell_strategy"):
            cfgmod.validate_cell_strategy_cfg(cfg)


class ValidateMotionSmoothingCfgTest(unittest.TestCase):
    def test_default_config_is_valid(self):
        cfgmod.validate_motion_smoothing_cfg(cfgmod.Config())

    def test_range_bounds_valid(self):
        for v in (0.0, 0.5, 1.0):
            cfg = cfgmod.Config()
            cfg.color.motion_smoothing = v
            cfgmod.validate_motion_smoothing_cfg(cfg)

    def test_out_of_range_raises(self):
        for v in (-0.1, 1.5):
            cfg = cfgmod.Config()
            cfg.color.motion_smoothing = v
            with self.assertRaisesRegex(cfgmod.ConfigError, "motion_smoothing"):
                cfgmod.validate_motion_smoothing_cfg(cfg)


class MotionSmoothingWiringTest(unittest.TestCase):
    """[color].motion_smoothing scales BOTH temporal buffers in the mhires
    percell path: 1.0 = the legacy EMA alpha + hysteresis; 0.0 = no smoothing
    (EMA passthrough, zero hysteresis) so the render tracks the source exactly."""

    def _mode(self, s):
        from c64cast.modes import MultiHiresDisplayMode

        return MultiHiresDisplayMode(motion_smoothing=s, perceptual=True)

    def test_full_smoothing_matches_legacy(self):
        from c64cast import modes

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

        from c64cast.modes import MultiHiresDisplayMode

        mode = cfgmod._build_display_mode("mhires", color=cfgmod.ColorCfg(motion_smoothing=0.0))
        self.assertAlmostEqual(cast(MultiHiresDisplayMode, mode)._ema_alpha, 1.0)


class BuildSceneTempoScaleTest(unittest.TestCase):
    """build_scene resolves VideoScene._tempo_scale: the observed bitmap+DAC
    speed fraction on the host-DMA DAC path over a bitmap mode, else 1.0 (off)
    for the sampler, the REU pump, char modes, and muted scenes."""

    def setUp(self):
        from c64cast.audio import AudioStreamer

        self._tmp = tempfile.TemporaryDirectory()
        self.clip = os.path.join(self._tmp.name, "clip.mp4")
        with open(self.clip, "wb") as f:
            f.write(b"\x00")  # resolve_file_spec only checks existence + ext
        self.audio = cast(AudioStreamer, object())

    def tearDown(self):
        self._tmp.cleanup()

    def _scene(self, cfg: cfgmod.Config, *, display: str, audio, **build_kw):
        from c64cast.scenes import VideoScene

        s = cfgmod.SceneCfg(type="video", display=display, file=self.clip)
        scene = cfgmod.build_scene(s, cfg, cast(C64Backend, FakeAPI()), audio, None, **build_kw)
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
        self.assertEqual(scene._tempo_scale, 0.80)

    def test_dac_hires_uses_hires_factor(self):
        scene = self._scene(self._dac_cfg(), display="hires", audio=self.audio)
        self.assertEqual(scene._tempo_scale, 0.90)

    def test_dac_hires_edges_uses_hires_factor(self):
        # hires_edges shares the Hires VIC fetch → the hires factor.
        scene = self._scene(self._dac_cfg(), display="hires_edges", audio=self.audio)
        self.assertEqual(scene._tempo_scale, 0.90)

    def test_dac_petscii_is_off(self):
        scene = self._scene(self._dac_cfg(), display="petscii", audio=self.audio)
        self.assertEqual(scene._tempo_scale, 1.0)

    def test_dac_mcm_is_off(self):
        scene = self._scene(self._dac_cfg(), display="mcm", audio=self.audio)
        self.assertEqual(scene._tempo_scale, 1.0)

    def test_muted_bitmap_is_off(self):
        # No audio streamer → nothing to compensate.
        scene = self._scene(self._dac_cfg(), display="mhires", audio=None)
        self.assertEqual(scene._tempo_scale, 1.0)

    def test_reu_pump_bitmap_is_off(self):
        cfg = self._dac_cfg()
        cfg.audio.use_reu_pump = True
        scene = self._scene(cfg, display="mhires", audio=self.audio)
        self.assertEqual(scene._tempo_scale, 1.0)

    def test_sampler_bitmap_is_off(self):
        # Sampler path (off the C64 bus) never stretches → no compensation.
        cfg = self._dac_cfg()
        cfg.audio.backend = "auto"
        import dataclasses

        api = FakeAPI()
        api.profile = dataclasses.replace(api.profile, supports_sampler=True)
        with mock.patch("c64cast.sampler.UltimateAudioSampler", return_value=object()):
            from c64cast.scenes import VideoScene

            s = cfgmod.SceneCfg(type="video", display="mhires", file=self.clip)
            scene = cfgmod.build_scene(
                s, cfg, cast(C64Backend, api), self.audio, None, sampler_available=True
            )
        assert isinstance(scene, VideoScene)
        self.assertEqual(scene._tempo_scale, 1.0)


if __name__ == "__main__":
    unittest.main()
