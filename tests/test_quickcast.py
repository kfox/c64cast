"""Tests for quick playback — the positional-MEDIA path (c64cast.quickcast).

Covers the pure classification layer (extension/dir/glob -> scene type), the
in-memory Config builder (playlist semantics + per-flag overrides + the
scheme-aware connection target), URL routing (direct media vs yt-dlp, with a
fake yt_dlp module so no network is touched), and cli._resolve_configs dispatch
(positional args vs --config). The hardware run path (build_stack/
_run_playlists) is intentionally out of scope here — it's exercised by the
CLI/playlist suites.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from c64cast import quickcast
from c64cast.cli import _resolve_configs, build_parser
from c64cast.config import resolve_file_spec


def _parse(argv: list[str]):
    """Parse argv with the unified c64cast parser (quick playback now shares
    the one parser; positional MEDIA args trigger build_config)."""
    return build_parser().parse_args(argv)


class IsUrlTest(unittest.TestCase):
    def test_url_detection(self):
        self.assertTrue(quickcast._is_url("http://example.com/x"))
        self.assertTrue(quickcast._is_url("https://youtu.be/abc"))
        self.assertTrue(quickcast._is_url("HTTPS://EXAMPLE.COM"))
        self.assertFalse(quickcast._is_url("clip.mp4"))
        self.assertFalse(quickcast._is_url("/abs/path.sid"))
        self.assertFalse(quickcast._is_url("ftp://nope"))


class ClassifyLiteralFileTest(unittest.TestCase):
    """A literal file need not exist — classification is by extension only."""

    def _classify(self, name: str) -> str:
        return quickcast.classify_local(name, display=None, duration_s=None).type

    def test_extension_to_scene_type(self):
        self.assertEqual(self._classify("a.mp4"), "video")
        self.assertEqual(self._classify("a.MKV"), "video")  # case-insensitive
        self.assertEqual(self._classify("tune.sid"), "waveform")
        self.assertEqual(self._classify("pic.png"), "slideshow")
        self.assertEqual(self._classify("game.prg"), "launcher")
        self.assertEqual(self._classify("cart.crt"), "launcher")

    def test_audio_is_deferred(self):
        with self.assertRaises(ValueError) as cm:
            self._classify("song.mp3")
        self.assertIn("audio-only", str(cm.exception))

    def test_unknown_extension(self):
        with self.assertRaises(ValueError) as cm:
            self._classify("mystery.xyz")
        self.assertIn("unknown file type", str(cm.exception))

    def test_file_spec_is_preserved(self):
        scene = quickcast.classify_local("clip.mp4", display=None, duration_s=None)
        self.assertEqual(scene.file, "clip.mp4")

    def test_video_gets_mhires_default_display(self):
        scene = quickcast.classify_local("clip.mp4", display=None, duration_s=None)
        self.assertEqual(scene.display, "mhires")

    def test_display_override_applies_to_video(self):
        scene = quickcast.classify_local("clip.mp4", display="hires", duration_s=None)
        self.assertEqual(scene.display, "hires")

    def test_duration_applies_to_waveform_not_video(self):
        wav = quickcast.classify_local("t.sid", display=None, duration_s=42.0)
        self.assertEqual(wav.duration_s, 42.0)
        # Video rejects duration_s (video-driven) — must stay unset.
        com = quickcast.classify_local("c.mp4", display=None, duration_s=42.0)
        self.assertIsNone(com.duration_s)


class ClassifyDirAndGlobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for f in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    def _touch(self, *names: str):
        for n in names:
            open(os.path.join(self.tmp, n), "w").close()

    def test_directory_of_sids(self):
        self._touch("a.sid", "b.sid")
        scene = quickcast.classify_local(self.tmp, display=None, duration_s=None)
        self.assertEqual(scene.type, "waveform")
        # The directory spec is passed through verbatim (scene random-picks).
        self.assertEqual(scene.file, self.tmp)

    def test_empty_directory(self):
        with self.assertRaises(ValueError) as cm:
            quickcast.classify_local(self.tmp, display=None, duration_s=None)
        self.assertIn("empty", str(cm.exception))

    def test_directory_no_playable_files(self):
        self._touch("notes.txt", "data.bin")
        with self.assertRaises(ValueError) as cm:
            quickcast.classify_local(self.tmp, display=None, duration_s=None)
        self.assertIn("no playable files", str(cm.exception))

    def test_directory_audio_only(self):
        self._touch("a.mp3", "b.wav")
        with self.assertRaises(ValueError) as cm:
            quickcast.classify_local(self.tmp, display=None, duration_s=None)
        self.assertIn("audio", str(cm.exception))

    def test_directory_mixed_types(self):
        self._touch("a.sid", "b.mp4")
        with self.assertRaises(ValueError) as cm:
            quickcast.classify_local(self.tmp, display=None, duration_s=None)
        self.assertIn("mixes scene types", str(cm.exception))

    def test_glob(self):
        self._touch("one.png", "two.png", "skip.txt")
        pattern = os.path.join(self.tmp, "*.png")
        scene = quickcast.classify_local(pattern, display=None, duration_s=None)
        self.assertEqual(scene.type, "slideshow")
        self.assertEqual(scene.file, pattern)

    def test_glob_no_match(self):
        pattern = os.path.join(self.tmp, "*.mp4")
        with self.assertRaises(ValueError) as cm:
            quickcast.classify_local(pattern, display=None, duration_s=None)
        self.assertIn("matched no files", str(cm.exception))

    def test_existing_file_with_glob_chars_is_literal(self):
        # YouTube-style names contain [videoid]; an existing file must win over
        # glob interpretation rather than being read as a character class.
        name = "1983 Commodore ad [gO8P3oMijWs].mp4"
        self._touch(name)
        path = os.path.join(self.tmp, name)
        scene = quickcast.classify_local(path, display=None, duration_s=None)
        self.assertEqual(scene.type, "video")
        self.assertEqual(scene.file, path)


class BuildConfigTest(unittest.TestCase):
    def test_playlist_semantics(self):
        cfg = quickcast.build_config(_parse(["a.mp4", "b.sid"]))
        self.assertFalse(cfg.playlist.loop)
        self.assertFalse(cfg.playlist.interleave_videos)

    def test_loop_flag(self):
        cfg = quickcast.build_config(_parse(["--loop", "a.mp4"]))
        self.assertTrue(cfg.playlist.loop)

    def test_scene_order_preserved(self):
        cfg = quickcast.build_config(_parse(["a.mp4", "b.sid", "c.png"]))
        self.assertEqual([s.type for s in cfg.scenes], ["video", "waveform", "slideshow"])

    def test_audio_on_by_default(self):
        self.assertTrue(quickcast.build_config(_parse(["a.mp4"])).audio.enabled)

    def test_no_audio_flag(self):
        self.assertFalse(quickcast.build_config(_parse(["--no-audio", "a.mp4"])).audio.enabled)

    def test_overrides(self):
        cfg = quickcast.build_config(
            _parse(
                [
                    "-u",
                    "http://192.168.2.64",
                    "-s",
                    "PAL",
                    "--sid-model",
                    "8580",
                    "--skip-probe",
                    "--log-file",
                    "/tmp/c64cast.log",
                    "a.mp4",
                ]
            )
        )
        self.assertEqual(cfg.ultimate64.url, "http://192.168.2.64")
        self.assertEqual(cfg.ultimate64.system, "PAL")
        self.assertEqual(cfg.ultimate64.sid_model, "8580")
        self.assertTrue(cfg.debug.skip_probe)
        self.assertEqual(cfg.debug.log_file, "/tmp/c64cast.log")

    def test_log_file_default_kept_when_unset(self):
        self.assertIsNone(quickcast.build_config(_parse(["a.mp4"])).debug.log_file)

    def test_sid_model_default_kept_when_unset(self):
        self.assertEqual(quickcast.build_config(_parse(["a.mp4"])).ultimate64.sid_model, "auto")

    def test_url_default_kept_when_unset(self):
        # No -u and no $C64CAST_URL -> keep the built-in Config default.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("C64CAST_URL", None)
            cfg = quickcast.build_config(_parse(["a.mp4"]))
        self.assertEqual(cfg.ultimate64.url, "http://ultimate-64-ii.lan")

    def test_env_url_used_when_unset(self):
        # $C64CAST_URL is the fallback connection target when -u is absent.
        with mock.patch.dict(os.environ, {"C64CAST_URL": "u64://10.1.1.1"}):
            cfg = quickcast.build_config(_parse(["a.mp4"]))
        self.assertEqual(cfg.hardware.backend, "ultimate")
        self.assertEqual(cfg.ultimate64.url, "http://10.1.1.1")

    def test_tr_target_selects_teensyrom_backend(self):
        cfg = quickcast.build_config(_parse(["-u", "tr:///dev/cu.usbmodem9", "a.sid"]))
        self.assertEqual(cfg.hardware.backend, "teensyrom")
        self.assertEqual(cfg.teensyrom.transport, "serial")
        self.assertEqual(cfg.teensyrom.serial_port, "/dev/cu.usbmodem9")


class ResolveMediaUrlTest(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("yt_dlp", None)

    def test_direct_video_url_passthrough(self):
        url = "http://host/clip.mp4"
        self.assertEqual(quickcast.resolve_media_url(url), (url, "video", None))

    def test_direct_audio_url_passthrough(self):
        url = "http://host/song.mp3?x=1"
        self.assertEqual(quickcast.resolve_media_url(url), (url, "audio", None))

    def _install_fake_ytdlp(self, info: dict):
        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download):  # noqa: ARG002
                return info

        mod = types.ModuleType("yt_dlp")
        mod.YoutubeDL = FakeYDL  # type: ignore[attr-defined]
        sys.modules["yt_dlp"] = mod

    def test_ytdlp_video(self):
        self._install_fake_ytdlp(
            {"url": "http://stream/v.m3u8", "vcodec": "h264", "title": "Cool Video"}
        )
        self.assertEqual(
            quickcast.resolve_media_url("https://youtu.be/abc"),
            ("http://stream/v.m3u8", "video", "Cool Video"),
        )

    def test_ytdlp_audio_only(self):
        self._install_fake_ytdlp({"url": "http://stream/a", "vcodec": "none", "title": "Pod"})
        _, kind, _ = quickcast.resolve_media_url("https://example.com/pod")
        self.assertEqual(kind, "audio")

    def test_ytdlp_playlist_takes_first_entry(self):
        self._install_fake_ytdlp(
            {"entries": [None, {"url": "http://stream/e2", "vcodec": "vp9", "title": "E2"}]}
        )
        self.assertEqual(
            quickcast.resolve_media_url("https://example.com/list"),
            ("http://stream/e2", "video", "E2"),
        )

    def test_missing_ytdlp_raises_runtime_error(self):
        sys.modules.pop("yt_dlp", None)
        try:
            import yt_dlp  # type: ignore[import-untyped]  # noqa: F401,PLC0415  # pyright: ignore[reportMissingModuleSource]

            self.skipTest("yt-dlp is installed; missing-dep path not exercised")
        except ImportError:
            pass
        with self.assertRaises(RuntimeError) as cm:
            quickcast.resolve_media_url("https://youtu.be/abc")
        self.assertIn("yt-dlp", str(cm.exception))

    def test_ytdlp_extraction_failure_raises_clean_value_error(self):
        # An unavailable/private/removed video (or any other extraction
        # failure) must surface as a clean ValueError, not the raw
        # yt_dlp.DownloadError — that's the one cli.build_stack's existing
        # scene-build error handler catches without dumping a traceback.
        class FakeDownloadError(Exception):
            pass

        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download):  # noqa: ARG002
                raise FakeDownloadError("ERROR: [youtube] abc: This video is not available")

        mod = types.ModuleType("yt_dlp")
        mod.YoutubeDL = FakeYDL  # type: ignore[attr-defined]
        mod.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
        sys.modules["yt_dlp"] = mod

        with self.assertRaises(ValueError) as cm:
            quickcast.resolve_media_url("https://youtu.be/abc")
        # The redundant yt-dlp "ERROR: " prefix is stripped.
        self.assertIn("This video is not available", str(cm.exception))
        self.assertNotIn("ERROR: ERROR:", str(cm.exception))


class ParseTimestrTest(unittest.TestCase):
    def test_bare_seconds(self):
        self.assertEqual(quickcast._parse_timestr("90"), 90.0)
        self.assertEqual(quickcast._parse_timestr("90.5"), 90.5)
        self.assertEqual(quickcast._parse_timestr("0"), 0.0)

    def test_hms_form(self):
        self.assertEqual(quickcast._parse_timestr("90s"), 90.0)
        self.assertEqual(quickcast._parse_timestr("1m30s"), 90.0)
        self.assertEqual(quickcast._parse_timestr("1h2m3s"), 3723.0)
        self.assertEqual(quickcast._parse_timestr("1h"), 3600.0)
        self.assertEqual(quickcast._parse_timestr("2m"), 120.0)
        self.assertEqual(quickcast._parse_timestr("1H30M"), 5400.0)  # case-insensitive

    def test_unparseable_returns_none(self):
        self.assertIsNone(quickcast._parse_timestr(""))
        self.assertIsNone(quickcast._parse_timestr("abc"))
        self.assertIsNone(quickcast._parse_timestr("1x2y"))


class ParseStartOffsetTest(unittest.TestCase):
    def test_t_query_param(self):
        self.assertEqual(quickcast._parse_start_offset("https://youtu.be/x?t=90"), 90.0)
        self.assertEqual(quickcast._parse_start_offset("https://youtu.be/x?t=1m30s"), 90.0)

    def test_start_query_param(self):
        self.assertEqual(quickcast._parse_start_offset("https://yt/watch?v=x&start=45"), 45.0)

    def test_t_preferred_over_start(self):
        self.assertEqual(quickcast._parse_start_offset("https://yt/x?t=10&start=45"), 10.0)

    def test_fragment_form(self):
        self.assertEqual(quickcast._parse_start_offset("http://host/clip.mp4#t=30"), 30.0)
        self.assertEqual(quickcast._parse_start_offset("http://host/clip.mp4#t=1m"), 60.0)

    def test_no_timestamp(self):
        self.assertIsNone(quickcast._parse_start_offset("https://youtu.be/x"))
        self.assertIsNone(quickcast._parse_start_offset("http://host/clip.mp4?list=foo"))

    def test_garbage_timestamp_ignored(self):
        self.assertIsNone(quickcast._parse_start_offset("https://youtu.be/x?t=soon"))


class ClassifyUrlTest(unittest.TestCase):
    def test_video_url_becomes_video(self):
        # classify_url stores the URL verbatim — resolution is deferred to
        # config.build_scene (the single, shared resolution path).
        scene = quickcast.classify_url("https://youtu.be/abc", display=None)
        self.assertEqual(scene.type, "video")
        self.assertEqual(scene.file, "https://youtu.be/abc")
        self.assertEqual(scene.display, "mhires")
        self.assertIsNone(scene.start_s)

    def test_url_timestamp_sets_start_s(self):
        # The timestamp is parsed offline at classify time (no network/dep)
        # so it rides onto the SceneCfg regardless of later resolution.
        scene = quickcast.classify_url("https://youtu.be/abc?t=1m30s", display=None)
        self.assertEqual(scene.type, "video")
        self.assertEqual(scene.start_s, 90.0)

    def test_url_fragment_timestamp_sets_start_s(self):
        scene = quickcast.classify_url("http://host/clip.mp4#t=45", display=None)
        self.assertEqual(scene.start_s, 45.0)

    def test_audio_url_not_rejected_at_classify_time(self):
        # Audio rejection now lives in build_scene (via resolve_video_url), so
        # classify_url no longer raises — it just wraps the URL.
        scene = quickcast.classify_url("http://host/song.mp3", display=None)
        self.assertEqual(scene.type, "video")
        self.assertEqual(scene.file, "http://host/song.mp3")

    def test_build_config_routes_url(self):
        cfg = quickcast.build_config(_parse(["http://host/clip.mp4"]))
        self.assertEqual(cfg.scenes[0].type, "video")
        self.assertEqual(cfg.scenes[0].file, "http://host/clip.mp4")


class UrlNeedsYtdlpTest(unittest.TestCase):
    def test_direct_media_url_does_not_need_ytdlp(self):
        self.assertFalse(quickcast.url_needs_ytdlp("http://host/clip.mp4"))
        self.assertFalse(quickcast.url_needs_ytdlp("http://host/song.mp3?x=1"))

    def test_page_url_needs_ytdlp(self):
        self.assertTrue(quickcast.url_needs_ytdlp("https://youtu.be/abc?t=90"))
        self.assertTrue(quickcast.url_needs_ytdlp("https://example.com/watch?v=x"))


class ResolveVideoUrlTest(unittest.TestCase):
    """The shared resolver used by both quick playback and config.build_scene."""

    def tearDown(self):
        sys.modules.pop("yt_dlp", None)

    def _install_fake_ytdlp(self, info: dict):
        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download):  # noqa: ARG002
                return info

        mod = types.ModuleType("yt_dlp")
        mod.YoutubeDL = FakeYDL  # type: ignore[attr-defined]
        sys.modules["yt_dlp"] = mod

    def test_direct_video_url_passthrough_with_timestamp(self):
        url = "http://host/clip.mp4?t=2m"
        self.assertEqual(quickcast.resolve_video_url(url), (url, 120.0, None))

    def test_ytdlp_video_returns_stream_offset_title(self):
        self._install_fake_ytdlp({"url": "http://stream/v.m3u8", "vcodec": "h264", "title": "Cool"})
        self.assertEqual(
            quickcast.resolve_video_url("https://youtu.be/abc?t=30"),
            ("http://stream/v.m3u8", 30.0, "Cool"),
        )

    def test_audio_only_raises(self):
        with self.assertRaises(ValueError) as cm:
            quickcast.resolve_video_url("http://host/song.mp3")
        self.assertIn("audio", str(cm.exception))


class ResolveFileSpecUrlTest(unittest.TestCase):
    """The URL passthrough added to resolve_file_spec — URLs skip the
    extension/glob/existence checks that would otherwise reject them."""

    def test_url_passthrough(self):
        url = "https://host/stream.m3u8?token=abc"
        self.assertEqual(resolve_file_spec(url, (".mp4",), label="video"), [url])

    def test_url_mixed_with_local(self):
        url = "http://host/clip.mp4"
        out = resolve_file_spec(f"{url}, also.mp4", (".mp4",), label="video")
        self.assertIn(url, out)
        self.assertIn("also.mp4", out)

    def test_existing_file_with_glob_chars_is_literal(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "clip [abc123].mp4")
            open(path, "w").close()
            self.assertEqual(resolve_file_spec(path, (".mp4",), label="video"), [path])


class ResolveConfigsDispatchTest(unittest.TestCase):
    """cli._resolve_configs routes positional MEDIA to the quick-playback
    builder and rejects the inputs + --config conflict, with no disk/hardware."""

    def test_positional_media_builds_single_system(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("C64CAST_URL", None)
            loaded, cfgs = _resolve_configs(_parse(["a.mp4", "b.sid"]))
        self.assertFalse(loaded.is_ensemble)
        self.assertEqual(loaded.names, ["cast"])
        self.assertEqual(loaded.paths, [None])
        self.assertEqual(len(cfgs), 1)
        self.assertEqual([s.type for s in cfgs[0].scenes], ["video", "waveform"])

    def test_inputs_with_config_is_rejected(self):
        from c64cast.cli import _CliUsageError

        with self.assertRaises(_CliUsageError):
            _resolve_configs(_parse(["--config", "some.toml", "a.mp4"]))


if __name__ == "__main__":
    unittest.main()
