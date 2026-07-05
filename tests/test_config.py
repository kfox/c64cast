"""Smoke tests for c64cast.config — loader, defaults, CLI merge."""

# pyright: reportArgumentType=false
from __future__ import annotations

import argparse
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
        # Memoization cache is module-global — clear it between tests.
        cfgmod._songlengths_cache.clear()

    def test_none_path_returns_none(self):
        self.assertIsNone(cfgmod._load_songlengths(None))
        self.assertIsNone(cfgmod._load_songlengths(""))

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
