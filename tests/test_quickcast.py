"""Tests for the `cast` quick-playback shortcut (c64cast.quickcast).

Covers the pure classification layer (extension/dir/glob -> scene type), the
in-memory Config builder (playlist semantics + per-flag overrides), and URL
routing (direct media vs yt-dlp, with a fake yt_dlp module so no network is
touched). The hardware run path (build_stack/_run_playlists) is intentionally
out of scope here — it's exercised by the CLI/playlist suites.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest

from c64cast import quickcast
from c64cast.config import resolve_file_spec


def _parse(argv: list[str]):
    return quickcast._build_parser().parse_args(argv)


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
            _parse(["-u", "http://192.168.2.64", "-s", "PAL", "--skip-probe", "a.mp4"])
        )
        self.assertEqual(cfg.ultimate64.url, "http://192.168.2.64")
        self.assertEqual(cfg.ultimate64.system, "PAL")
        self.assertTrue(cfg.debug.skip_probe)

    def test_url_default_kept_when_unset(self):
        # No -u and no $C64CAST_URL -> keep the built-in Config default.
        cfg = quickcast.build_config(_parse(["a.mp4"]))
        self.assertEqual(cfg.ultimate64.url, "http://ultimate-64-ii.lan")


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


class ClassifyUrlTest(unittest.TestCase):
    def test_video_url_becomes_video(self):
        scene = quickcast.classify_url("http://host/clip.mp4", display=None)
        self.assertEqual(scene.type, "video")
        self.assertEqual(scene.file, "http://host/clip.mp4")
        self.assertEqual(scene.display, "mhires")

    def test_audio_url_deferred(self):
        with self.assertRaises(ValueError) as cm:
            quickcast.classify_url("http://host/song.mp3", display=None)
        self.assertIn("audio", str(cm.exception))

    def test_build_config_routes_url(self):
        cfg = quickcast.build_config(_parse(["http://host/clip.mp4"]))
        self.assertEqual(cfg.scenes[0].type, "video")
        self.assertEqual(cfg.scenes[0].file, "http://host/clip.mp4")


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


if __name__ == "__main__":
    unittest.main()
