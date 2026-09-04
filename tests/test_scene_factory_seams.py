"""The seams `scene_factory` assembles a scene through, and the drift each one
had accumulated.

Three copies of the display-mode/scene wiring existed: `build_scene`'s own
path, the `display = "random"` slideshow rebuild in `scenes.py`, and the
auto-interleave loop's direct `VideoScene(...)` — and both copies outside
`build_scene` had drifted. Two more lists of scene types existed alongside
`config.SCENE_TYPES` and `_BUILDERS` (the validator ladder and its `else`
message), with only the first two held to each other by a test. And the
`file =` grammar was written three times with two different ideas of what a
URL is. These tests hold each converged seam to one implementation.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from typing import cast
from unittest import mock

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory
from c64cast.app.config import ColorCfg, Config, ConfigError, SceneCfg
from c64cast.hw.api import Ultimate64API
from c64cast.scenes.scenes import SlideshowScene, VideoScene
from c64cast.video.modes import MultiHiresDisplayMode

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeAPI, MachineSettingsIsolation, quiet_logging  # noqa: E402

_iso = MachineSettingsIsolation()


def setUpModule():
    _iso.start()


def tearDownModule():
    _iso.stop()


def _api() -> Ultimate64API:
    return cast(Ultimate64API, FakeAPI())


class SceneTypeListsTest(unittest.TestCase):
    """`_BUILDERS` was pinned to `SCENE_TYPES` and the validator dispatch was
    not, so a new type could pass the drift test, pass config load, and then
    be refused as "unknown scene type" from inside `build_scene`."""

    def test_validators_builders_and_scene_types_are_the_same_set(self):
        self.assertEqual(set(scene_factory._VALIDATORS), set(cfgmod.SCENE_TYPES))
        self.assertEqual(set(scene_factory._VALIDATORS), set(scene_factory._BUILDERS))

    def test_the_unknown_type_message_is_built_from_scene_types(self):
        with self.assertRaises(ValueError) as cm:
            scene_factory.validate_scene_cfg(
                SceneCfg(type="scrolling_text"), Config(), audio_enabled=False
            )
        for name in cfgmod.SCENE_TYPES:
            self.assertIn(name, str(cm.exception))


class WebcamDisplayRefusalTest(unittest.TestCase):
    """webcam was the one frame-bearing type with no validator, so the
    blank/random refusal every sibling carries never ran for it: `display =
    "blank"` opened the camera, grabbed frames and painted an empty screen
    (BlankDisplayMode.compose ignores its frame argument) with no error."""

    def test_blank_display_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            scene_factory.validate_scene_cfg(
                SceneCfg(type="webcam", display="blank"), Config(), audio_enabled=False
            )
        self.assertIn("nothing", str(cm.exception))
        self.assertIn("mhires", str(cm.exception))

    def test_random_display_is_refused_with_the_slideshow_hint(self):
        with self.assertRaises(ValueError) as cm:
            scene_factory.validate_scene_cfg(
                SceneCfg(type="webcam", display="random"), Config(), audio_enabled=False
            )
        self.assertIn("only slideshow", str(cm.exception))

    def test_every_refusal_quotes_the_one_mode_list(self):
        # The three hand-written copies of this message listed three different
        # sets of suggested modes.
        for scene_type in ("webcam", "generative", "wled"):
            with self.assertRaises(ValueError) as cm:
                scene_factory.validate_scene_cfg(
                    SceneCfg(type=scene_type, display="blank"), Config(), audio_enabled=False
                )
            for name in scene_factory.QUANTIZING_DISPLAYS:
                self.assertIn(name, str(cm.exception))


class SlideshowDisplayResolutionTest(unittest.TestCase):
    """`resolve_scene_display` answered "hires_edges" for a default-display
    slideshow while the build produced "mhires". doctor calls it at four sites
    and branches on the answer, skipping the color_match report for
    hires_edges and the cell_strategy/motion_smoothing reports for anything
    but mhires — so the one type whose "auto" resolutions actually differ was
    the type dropped from all three."""

    def test_unset_slideshow_display_resolves_the_way_the_build_does(self):
        self.assertEqual(scene_factory.resolve_scene_display(None, "slideshow"), "mhires")
        self.assertEqual(scene_factory.resolve_scene_display("hires_edges", "slideshow"), "mhires")

    def test_random_stays_as_authored(self):
        # It has no single answer, and delegating would roll a die.
        self.assertEqual(scene_factory.resolve_scene_display("random", "slideshow"), "random")

    def test_other_types_are_unchanged(self):
        self.assertEqual(scene_factory.resolve_scene_display(None, "video"), "mhires")
        self.assertEqual(scene_factory.resolve_scene_display(None, "webcam"), "hires_edges")
        self.assertEqual(scene_factory.resolve_scene_display("hires_edges", "video"), "hires_edges")


def _only_display(name: str):
    """Shrink `display = "random"`'s pool to one entry, so `random.choice` has
    nothing to choose and a test can name the mode it means."""
    return mock.patch.object(scene_factory, "SLIDESHOW_RANDOM_DISPLAYS", (name,))


class SlideshowRebuildWiringTest(unittest.TestCase):
    """The `display = "random"` rebuild was a second copy of the wiring, and
    had already lost two kwargs the factory threads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        open(os.path.join(self.tmp.name, "one.png"), "wb").close()

    def _scene(self) -> SlideshowScene:
        s = SceneCfg(type="slideshow", display="random", file=self.tmp.name)
        return cast(SlideshowScene, scene_factory.build_scene(s, Config(), _api(), None, None))

    def test_the_rebuild_keeps_the_resolved_dither_and_cell_strategy(self):
        # [color].dither/cell_strategy = "auto" resolve to the documented
        # static-scene pair for a slideshow. The rebuild passed neither, so
        # _build_display_mode's "none"/"frequency" defaults took over from the
        # very first slide onward.
        # Narrow the pool instead of rolling until mhires turns up: 40 rolls
        # miss it about once in 6000 runs, which is a flake nobody would ever
        # reproduce, and the test is about the rebuild's wiring rather than
        # about the draw.
        scene = self._scene()
        with quiet_logging(), _only_display("mhires"):
            scene._maybe_rebuild_display_mode()
        mode = scene.display_mode
        assert isinstance(mode, MultiHiresDisplayMode), mode
        self.assertEqual(mode._dither_method, "floyd-steinberg")
        self.assertEqual(mode._cell_strategy, "error-min")

    def test_the_rebuild_withholds_double_buffer_under_the_reu_audio_pump(self):
        # The rebuild handed audio_reu_pump_active to resolve_flicker_tolerance
        # and withheld it from resolve_double_buffer in the same breath, so a
        # slideshow running the REU mic pump could get the $0314 raster IRQ the
        # pump already owns.
        cfg = Config()
        cfg.audio.use_reu_pump = True
        cfg.video.double_buffer = True
        s = SceneCfg(type="slideshow", display="random", file=self.tmp.name)
        scene = cast(SlideshowScene, scene_factory.build_scene(s, cfg, _api(), None, None))
        # Every member of the pool, once each — 40 random rolls could still
        # leave one of the five untried.
        for display in scene_factory.SLIDESHOW_RANDOM_DISPLAYS:
            with self.subTest(display=display):
                with quiet_logging(), _only_display(display):
                    scene._maybe_rebuild_display_mode()
                self.assertFalse(getattr(scene.display_mode, "_double_buffer", False))

    def test_the_wiring_is_the_factory_s_own_object(self):
        scene = self._scene()
        self.assertIsInstance(scene._display_wiring, scene_factory.DisplayWiring)
        self.assertEqual(scene._display_wiring.scene_type, "slideshow")


class RandomSlideshowOverlayValidationTest(unittest.TestCase):
    """A "random" slideshow's overlays were validated against one random pick,
    so the same unchanged config loaded on about four runs in five — and the
    runtime re-pick never re-validated at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        open(os.path.join(self.tmp.name, "one.png"), "wb").close()

    def _validate(self, display):
        scene_factory.validate_scene_cfg(
            SceneCfg(
                type="slideshow",
                display=display,
                file=self.tmp.name,
                overlays=[{"type": "clock"}],
            ),
            Config(),
            audio_enabled=False,
        )

    def test_a_text_overlay_on_a_random_slideshow_is_refused_every_time(self):
        # mcm is in the pool and rejects a buffer-painting text overlay, so the
        # answer has to be "no" deterministically rather than 1-in-5.
        for _ in range(20):
            with self.assertRaises(ValueError):
                self._validate("random")

    def test_a_concrete_slideshow_display_is_unaffected(self):
        self._validate("mhires")


class InterleavedVideoWiringTest(unittest.TestCase):
    """Auto-interleaved videos were constructed directly, reproducing one of
    the six things `_build_video` does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        open(os.path.join(self.tmp.name, "clip.mp4"), "wb").close()

    def _interleaved(self, cfg, audio) -> list[VideoScene]:
        cfg.playlist.interleave_videos = True
        cfg.playlist.videos_dir = self.tmp.name
        cfg.scenes = [SceneCfg(type="blank", name="a"), SceneCfg(type="blank", name="b")]
        with mock.patch.object(scene_factory, "ensure_pyav", return_value=True):
            built = scene_factory.scenes_from_config(cfg, _api(), audio, None)
        return [s for s in built if isinstance(s, VideoScene)]

    def test_the_bitmap_dac_tempo_compensation_is_applied(self):
        # hires_edges over the $D418 DAC is exactly the case
        # [audio].dac_bitmap_tempo_hires exists to correct; the direct
        # construction left tempo_scale at VideoScene's 1.0 default, so every
        # interleaved clip played the documented ~11-12% slow.
        cfg = Config()
        videos = self._interleaved(cfg, cast(object, object()))
        self.assertTrue(videos)
        for v in videos:
            self.assertEqual(v.tempo_scale, cfg.audio.dac_bitmap_tempo_hires)

    def test_the_epilogue_stamps_reach_an_interleaved_video(self):
        cfg = Config()
        cfg.midi_control.osd = "off"
        cfg.debug.frame_numbers = True
        videos = self._interleaved(cfg, cast(object, object()))
        self.assertTrue(videos)
        for v in videos:
            self.assertFalse(v.osd.enabled)
            self.assertTrue(v.show_frame_numbers)
            self.assertEqual(getattr(v._cfg, "type", None), "video")

    def test_a_muted_interleaved_video_gets_no_tempo_compensation(self):
        videos = self._interleaved(Config(), None)
        self.assertTrue(videos)
        for v in videos:
            self.assertEqual(v.tempo_scale, 1.0)


class SingleScenePlaylistCountTest(unittest.TestCase):
    """`scenes_from_config` skips every follower_only scene and
    `Playlist.single_scene` counts what it was handed, so one live scene plus a
    follower-only sibling — the canonical ensemble shape — really is a
    single-scene playlist."""

    def test_a_follower_only_sibling_does_not_make_it_multi_scene(self):
        cfg = Config()
        cfg.scenes = [
            SceneCfg(type="blank", name="idle"),
            SceneCfg(type="blank", name="hello", follower_only=True),
        ]
        scene = scene_factory.build_scene(cfg.scenes[0], cfg, _api(), None, None)
        self.assertEqual(scene.duration_s, float("inf"))

    def test_two_rotating_scenes_still_take_the_finite_default(self):
        cfg = Config()
        cfg.scenes = [SceneCfg(type="blank", name="a"), SceneCfg(type="blank", name="b")]
        scene = scene_factory.build_scene(cfg.scenes[0], cfg, _api(), None, None)
        self.assertNotEqual(scene.duration_s, float("inf"))


class FileSpecGrammarTest(unittest.TestCase):
    """One grammar and one URL predicate, where there were three of each."""

    EXTS = (".mp4",)

    def test_a_url_keeps_its_own_commas(self):
        # The standard Akamai HLS shape. It used to be cut into a truncated URL
        # plus fragments reported as paths the user never typed — and yt-dlp's
        # own resolved stream URLs, which this module writes back into a file
        # spec, routinely carry commas in query parameters.
        url = "https://cdn.example.com/i/clip_,300,700,.mp4.csmil/master.m3u8"
        self.assertEqual(scene_factory.split_file_spec(url), [url])
        self.assertTrue(scene_factory._is_single_url_spec(url))
        self.assertEqual(scene_factory.resolve_file_spec(url, self.EXTS, label="video"), [url])

    def test_a_separating_comma_after_a_url_announces_itself(self):
        self.assertEqual(
            scene_factory.split_file_spec("http://h/a.mp4, b.mp4"), ["http://h/a.mp4", "b.mp4"]
        )
        self.assertEqual(
            scene_factory.split_file_spec("http://h/a.mp4,http://h/b.mp4"),
            ["http://h/a.mp4", "http://h/b.mp4"],
        )

    def test_local_specs_split_on_every_comma_as_before(self):
        self.assertEqual(scene_factory.split_file_spec("a.mp4,b.mp4 ,"), ["a.mp4", "b.mp4"])

    def test_missing_media_uses_the_same_grammar(self):
        self.assertEqual(
            scene_factory.missing_media("http://h/a.mp4?x=1,2, /nope/x.mp4"), ["/nope/x.mp4"]
        )

    def test_an_existing_directory_beats_glob_interpretation(self):
        # yt-dlp's `%(title)s [%(id)s]` convention produces such directories
        # for playlist downloads. `[2024]` is a character class matching one of
        # 2/0/4, so the glob branch found nothing and the populated directory
        # was reported as "glob matched no files".
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "Clips [2024]")
            os.makedirs(d)
            clip = os.path.join(d, "one.mp4")
            open(clip, "wb").close()
            self.assertEqual(scene_factory.resolve_file_spec(d, self.EXTS, label="video"), [clip])

    def test_a_recursive_glob_rooted_at_the_filesystem_root_is_refused(self):
        # Reached from the network validate route, where it would walk every
        # mounted volume inside one HTTP request.
        for pattern in ("/**/*.mp4", "/*/**/*.mp4"):
            with self.assertRaises(ValueError) as cm:
                scene_factory.resolve_file_spec(pattern, self.EXTS, label="video")
            self.assertIn("filesystem root", str(cm.exception))

    def test_a_rooted_glob_without_a_recursive_segment_is_still_allowed(self):
        # One directory listing, not a walk.
        with self.assertRaises(ValueError) as cm:
            scene_factory.resolve_file_spec("/*.mp4", self.EXTS, label="video")
        self.assertIn("matched no files", str(cm.exception))


class GatherVideosTest(unittest.TestCase):
    """`[playlist].videos_dir` and a scene's `file =` directory now use one
    lister; the interleave one had no isfile guard."""

    def test_a_directory_named_like_a_clip_is_not_a_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "clips.mp4"))
            real = os.path.join(tmp, "real.mp4")
            open(real, "wb").close()
            self.assertEqual(scene_factory._gather_videos(tmp), [real])

    def test_an_absent_directory_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(scene_factory._gather_videos(os.path.join(tmp, "nope")), [])


class SidDefaultDirRecursionTest(unittest.TestCase):
    """The recursion into the default SID directory was keyed to
    `label == "waveform"`, and `label` is documented as message text — so the
    generative SID arm, which shares that default directory, silently stayed
    shallow on every real HVSC layout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        nested = os.path.join(
            self.tmp.name, scene_factory.DEFAULT_WAVEFORM_DIR, "C64Music", "MUSICIANS"
        )
        os.makedirs(nested)
        open(os.path.join(nested, "tune.sid"), "wb").close()
        os.chdir(self.tmp.name)

    def test_a_generative_sid_scene_finds_a_nested_hvsc_tree(self):
        s = SceneCfg(type="generative", audio_source="sid")
        with quiet_logging():
            mode = scene_factory._validate_generative(s, Config())
        self.assertIsNotNone(mode)
        self.assertEqual(s.file, scene_factory.DEFAULT_WAVEFORM_DIR)

    def test_the_keyword_is_what_recurses_not_the_label(self):
        got = scene_factory.resolve_file_spec(
            scene_factory.DEFAULT_WAVEFORM_DIR,
            (".sid",),
            label="anything at all",
            recurse_default_sid_dir=True,
        )
        self.assertEqual([os.path.basename(p) for p in got], ["tune.sid"])


class SidHeaderReadIsBoundedTest(unittest.TestCase):
    """`_check_first_sid_clears_display` read the whole candidate file with no
    guard, inside the network-reachable validate path."""

    def test_an_oversized_candidate_is_skipped_rather_than_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "huge.sid")
            with open(path, "wb") as f:
                f.write(b"\0" * (scene_factory.MAX_SID_BYTES + 1))
            s = SceneCfg(type="generative", audio_source="sid", file=path)
            opened: list[str] = []
            real_open = open

            def spy(target, *a, **kw):
                opened.append(str(target))
                return real_open(target, *a, **kw)

            with mock.patch("builtins.open", spy):
                scene_factory._check_first_sid_clears_display(
                    s, scene_factory._build_display_mode("hires"), "hires"
                )
            self.assertNotIn(path, opened)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFOs only")
    def test_a_fifo_named_like_a_sid_is_never_opened(self):
        # `resolve_file_spec` admits it (a literal path is not required to
        # exist, and only its extension is checked), so without the isfile
        # guard the read blocked the validate request thread forever.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pipe.sid")
            # Suppressed because CI runs `pyright --pythonplatform Windows`,
            # where os.mkfifo does not exist; the skipUnless above is the
            # runtime guard.
            os.mkfifo(path)  # type: ignore[attr-defined]
            s = SceneCfg(type="generative", audio_source="sid", file=path)
            opened: list[str] = []
            real_open = open

            def spy(target, *a, **kw):
                opened.append(str(target))
                return real_open(target, *a, **kw)

            with mock.patch("builtins.open", spy):
                scene_factory._check_first_sid_clears_display(
                    s, scene_factory._build_display_mode("hires"), "hires"
                )
            self.assertNotIn(path, opened)


class MediaSpecRedactionTest(unittest.TestCase):
    """A private asset is legitimately reached with a credential-bearing URL,
    and this module quotes the spec into the log file, the console's error
    report, and a published video description."""

    def test_userinfo_is_stripped(self):
        self.assertEqual(
            scene_factory.redact_media_spec("https://user:tok@cdn.example/clip.mp4"),
            "https://REDACTED@cdn.example/clip.mp4",
        )

    def test_the_port_survives_the_strip(self):
        self.assertEqual(
            scene_factory.redact_media_spec("https://u:p@cdn.example:8443/clip.mp4"),
            "https://REDACTED@cdn.example:8443/clip.mp4",
        )

    def test_a_secret_query_parameter_is_masked(self):
        self.assertIn("REDACTED", scene_factory.redact_media_spec("https://cdn/x.mp4?token=abc"))

    def test_a_plain_local_path_is_unchanged(self):
        self.assertEqual(
            scene_factory.redact_media_spec("assets/videos/clip.mp4"), "assets/videos/clip.mp4"
        )

    def test_a_wrong_extension_failure_does_not_echo_the_credential(self):
        with self.assertRaises(ValueError) as cm:
            scene_factory.resolve_file_spec(
                "https://cdn.example/clip.mp4?token=s3cret, /nope/clip.wav",
                (".mp4",),
                label="video",
            )
        # The bad entry is named, and the sibling URL's token never appears.
        self.assertIn("clip.wav", str(cm.exception))
        self.assertNotIn("s3cret", str(cm.exception))

    def test_the_missing_ytdlp_refusal_does_not_echo_the_credential(self):
        from c64cast.app import quickcast

        s = SceneCfg(type="video", file="https://user:tok@vids.example/watch/1")
        with mock.patch.object(quickcast, "_ytdlp_available", return_value=False):
            with self.assertRaises(ValueError) as cm:
                scene_factory._validate_video(s, Config())
        self.assertNotIn("tok", str(cm.exception))
        self.assertIn("REDACTED", str(cm.exception))


class SceneNameFromMediaTitleTest(unittest.TestCase):
    """The resolved title comes from the page the operator pointed at and lands
    in log lines interpolated with no args, the OSD, and the
    SCENE_CONFIG_JSON snapshot."""

    def test_control_characters_and_runs_collapse(self):
        self.assertEqual(
            scene_factory._clean_scene_name("a\nERROR forged\tline  here"),
            "a ERROR forged line here",
        )

    def test_the_name_is_length_capped(self):
        got = scene_factory._clean_scene_name("x" * 500)
        self.assertEqual(len(got), scene_factory.MAX_SCENE_NAME_CHARS)


class MultiEntryUrlSpecTest(unittest.TestCase):
    """A page URL mixed into a multi-entry spec was never resolved at all, so
    PyAV was handed the HTML — even with the `yt` extra installed."""

    def test_a_page_url_beside_a_local_file_is_refused_at_validate_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = os.path.join(tmp, "a.mp4")
            open(clip, "wb").close()
            s = SceneCfg(type="video", file=f"{clip}, https://www.youtube.com/watch?v=dQw4w9WgXcQ")
            with self.assertRaises(ValueError) as cm:
                scene_factory._validate_video(s, Config())
            self.assertIn("multi-entry", str(cm.exception))

    def test_a_direct_media_url_beside_a_local_file_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = os.path.join(tmp, "a.mp4")
            open(clip, "wb").close()
            s = SceneCfg(type="video", file=f"{clip}, https://cdn.example/b.mp4")
            scene_factory._validate_video(s, Config())


class FlickerToleranceValidatorTest(unittest.TestCase):
    """flicker_tolerance was the one [color] field outside the
    effective_colors validation family: a typo raised a plain ValueError from
    deep inside the display build, on a different exit code, and only if some
    scene in the playlist happened to paint a frame."""

    def test_a_bad_global_value_is_a_config_error(self):
        cfg = Config()
        cfg.color.flicker_tolerance = "sotf"
        with self.assertRaises(cfgmod.ConfigError) as cm:
            scene_factory.validate_flicker_cfg(cfg)
        self.assertIn("[color].flicker_tolerance", str(cm.exception))

    def test_a_bad_scene_override_names_the_scene(self):
        cfg = Config()
        cfg.scenes = [SceneCfg(type="webcam", color={"flicker_tolerance": "nope"})]
        with self.assertRaises(cfgmod.ConfigError) as cm:
            scene_factory.validate_flicker_cfg(cfg)
        self.assertIn("[[scenes]][0].color.flicker_tolerance", str(cm.exception))

    def test_a_sid_only_playlist_is_checked_too(self):
        # The session runs whole-Config validators, so a blank/waveform-only
        # playlist — whose display modes are built with color=None, meaning
        # resolve_flicker_tolerance's own raise is unreachable — is covered now
        # where it never used to be.
        cfg = Config()
        cfg.color.flicker_tolerance = "bogus"
        cfg.scenes = [SceneCfg(type="blank")]
        self.assertIn(scene_factory.validate_flicker_cfg, scene_factory.PER_SYSTEM_VALIDATORS)
        with self.assertRaises(cfgmod.ConfigError):
            scene_factory.validate_flicker_cfg(cfg)

    def test_a_good_value_passes(self):
        scene_factory.validate_flicker_cfg(Config())


class WledSinkAllowTest(unittest.TestCase):
    """`wled_sink._bind` is AF_INET only, so the peer address the allowlist is
    compared against is always a dotted quad — an accepted IPv6 entry could
    never match, and the sink drops a non-matching sender with no log line."""

    def test_an_ipv6_entry_is_refused(self):
        s = SceneCfg(type="wled", sink_allow=["2001:db8::1"])
        with self.assertRaises(ValueError) as cm:
            scene_factory._validate_wled(s, Config())
        self.assertIn("IPv4 only", str(cm.exception))

    def test_an_ipv4_entry_is_accepted(self):
        scene_factory._validate_wled(SceneCfg(type="wled", sink_allow=["10.0.0.5"]), Config())

    def test_a_non_address_is_still_refused_as_before(self):
        with self.assertRaises(ValueError) as cm:
            scene_factory._validate_wled(SceneCfg(type="wled", sink_allow=["nope"]), Config())
        self.assertIn("not a valid IP address", str(cm.exception))


class WledListenExposureTest(unittest.TestCase):
    """Mode 1 covers everything `[control]`'s four verbs do and more, carries
    no token, and is advertised over mDNS — so it fails closed off loopback
    exactly like `validate_control_cfg`, with `allow_unauthenticated` as the
    opt-in `[control]` already models."""

    def test_a_non_loopback_listen_is_refused(self):
        # `listen = "enabled"` alone reaches this: Mode 1's default endpoint is
        # 0.0.0.0:8080, so the exposed bind is the one you get by default.
        cfg = Config()
        cfg.wled.listen = "enabled"
        with self.assertRaises(ConfigError) as cm:
            scene_factory.validate_wled_cfg(cfg)
        self.assertIn("allow_unauthenticated", str(cm.exception))

    def test_a_loopback_listen_needs_no_opt_in(self):
        cfg = Config()
        cfg.wled.listen = "127.0.0.1:8080"
        with self.assertNoLogs("c64cast.app.scene_factory", level="WARNING"):
            scene_factory.validate_wled_cfg(cfg)

    def test_the_opt_in_permits_a_network_bind(self):
        cfg = Config()
        cfg.wled.listen = "enabled"
        cfg.wled.allow_unauthenticated = True
        scene_factory.validate_wled_cfg(cfg)


class SonglengthsCacheResetTest(unittest.TestCase):
    """The memos are process-global with no invalidation, so a long-lived host
    could not notice HVSC being unpacked after the first miss was cached."""

    def setUp(self):
        self.addCleanup(scene_factory.reset_songlengths_cache)
        self.tmp = tempfile.TemporaryDirectory()
        # addCleanup is LIFO, so the chdir-back must be registered *after* the
        # directory's own cleanup to run before it: Windows refuses to remove a
        # directory that is any process's current working directory.
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp.name)
        scene_factory.reset_songlengths_cache()

    def test_the_reset_lets_a_newly_unpacked_tree_be_found(self):
        self.assertIsNone(scene_factory._autodetect_songlengths_path())
        docs = os.path.join(scene_factory._AUTODETECT_SONGLENGTHS_ROOT, "C64Music", "DOCUMENTS")
        os.makedirs(docs)
        path = os.path.join(docs, "Songlengths.md5")
        open(path, "w", encoding="utf-8").close()
        # Still the cached miss...
        self.assertIsNone(scene_factory._autodetect_songlengths_path())
        scene_factory.reset_songlengths_cache()
        self.assertEqual(scene_factory._autodetect_songlengths_path(), path)

    def test_the_reset_clears_the_loaded_database_cache_too(self):
        scene_factory._songlengths_cache["sentinel"] = None
        scene_factory.reset_songlengths_cache()
        self.assertEqual(scene_factory._songlengths_cache, {})


class DisplayWiringTest(unittest.TestCase):
    """The wiring object itself: one entry point, and a default that is usable
    without a config."""

    def test_a_default_wiring_builds_a_mode(self):
        mode = scene_factory.build_wired_display_mode(
            "mhires", scene_factory.DisplayWiring(color=ColorCfg())
        )
        self.assertIsInstance(mode, MultiHiresDisplayMode)

    def test_force_host_dma_clears_every_swap_path(self):
        wiring = scene_factory.DisplayWiring(
            use_reu_staged=True, double_buffer=True, reu_available=True, force_host_dma=True
        )
        mode = scene_factory.build_wired_display_mode("mhires", wiring)
        self.assertFalse(getattr(mode, "_use_reu_staged", False))
        self.assertFalse(getattr(mode, "_double_buffer", False))

    def test_the_scene_type_drives_the_auto_dither_resolution(self):
        static = cast(
            MultiHiresDisplayMode,
            scene_factory.build_wired_display_mode(
                "mhires", scene_factory.DisplayWiring(scene_type="slideshow")
            ),
        )
        motion = cast(
            MultiHiresDisplayMode,
            scene_factory.build_wired_display_mode(
                "mhires", scene_factory.DisplayWiring(scene_type="webcam")
            ),
        )
        self.assertEqual(static._dither_method, "floyd-steinberg")
        self.assertEqual(motion._dither_method, "blue_noise")


if __name__ == "__main__":
    unittest.main()
