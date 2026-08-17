"""Tests for the config wizard's pure helpers.

The questionary I/O shell isn't exercised here (no terminal); these cover the
buildable logic — config assembly, compat-filtering, asset scanning, and type
classification — which is where the correctness lives. (The `#:schema` line the
wizard writes is the serializer's to work out; see test_config_serialize.)
"""

from __future__ import annotations

import os
import tempfile
import unittest

from _fakes import MachineSettingsIsolation

from c64cast.app import config as cfgmod
from c64cast.app import config_serialize as ser
from c64cast.app import wizard

# The round-trip assertions (load(written) == built cfg) must hold independent
# of any real machine-settings file on the dev's machine (config.load applies
# that layer). Isolate it for the whole module.
_settings_isolation = MachineSettingsIsolation()


def setUpModule() -> None:
    _settings_isolation.start()


def tearDownModule() -> None:
    _settings_isolation.stop()


class FieldKindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(wizard.field_kind("bool"), "bool")
        self.assertEqual(wizard.field_kind("int"), "int")
        self.assertEqual(wizard.field_kind("float"), "float")
        self.assertEqual(wizard.field_kind("str"), "str")
        self.assertEqual(wizard.field_kind("str | None"), "str")
        self.assertEqual(wizard.field_kind("bool | None"), "bool")
        # list/dict are skipped in the generic walk.
        self.assertEqual(wizard.field_kind("list[str]"), "complex")
        self.assertEqual(wizard.field_kind("dict[str, str]"), "complex")
        self.assertEqual(wizard.field_kind("int | list[int]"), "complex")


class CompatibleOverlaysTest(unittest.TestCase):
    def test_text_overlay_offered_on_petscii_and_bitmap(self):
        # `clock` is a text overlay: it folds into the bitmap, so it's offered
        # on both petscii and hires now.
        petscii = {o.name for o in wizard.compatible_overlays("petscii", audio_enabled=False)}
        hires = {o.name for o in wizard.compatible_overlays("hires", audio_enabled=False)}
        self.assertIn("clock", petscii)
        self.assertIn("clock", hires)

    def test_non_text_overlay_stays_petscii_only(self):
        # spectrum_petscii draws bars, not a text run — petscii/blank only.
        petscii = {o.name for o in wizard.compatible_overlays("petscii", audio_enabled=True)}
        hires = {o.name for o in wizard.compatible_overlays("hires", audio_enabled=True)}
        self.assertIn("spectrum_petscii", petscii)
        self.assertNotIn("spectrum_petscii", hires)

    def test_hires_edges_maps_to_hires_runtime(self):
        # hires_edges and hires share runtime name 'hires' — same overlay set.
        a = {o.name for o in wizard.compatible_overlays("hires_edges", audio_enabled=False)}
        b = {o.name for o in wizard.compatible_overlays("hires", audio_enabled=False)}
        self.assertEqual(a, b)

    def test_spectrum_offered_with_audio_off(self):
        # The spectrum overlays read the scene's music features first (a SID
        # scene has those and no AudioStreamer), so they are no longer gated on
        # [audio] — they only WANT audio, for the FFT fallback.
        without = {o.name for o in wizard.compatible_overlays("petscii", audio_enabled=False)}
        with_ = {o.name for o in wizard.compatible_overlays("petscii", audio_enabled=True)}
        self.assertIn("spectrum_petscii", with_)
        self.assertIn("spectrum_petscii", without)

    def test_filter_matches_introspect_gate(self):
        # compatible_overlays must agree with the authority (overlay_mode_ok +
        # the audio requirement) for every display mode — that's what keeps it
        # from offering a mode-incompatible overlay.
        from c64cast.app import introspect

        modes = {m.runtime_name: m for m in introspect.display_modes()}
        for display, runtime in (
            ("petscii", "petscii"),
            ("blank", "blank"),
            ("hires_edges", "hires"),
            ("mcm", "mcm"),
        ):
            for audio in (False, True):
                expected = {
                    ov.name
                    for ov in introspect.overlay_docs()
                    if introspect.overlay_mode_ok(ov, modes[runtime])[0]
                    and (audio or not ov.requires_audio)
                }
                got = {ov.name for ov in wizard.compatible_overlays(display, audio_enabled=audio)}
                with self.subTest(display=display, audio=audio):
                    self.assertEqual(got, expected)

    def test_parameterless_overlay_validates(self):
        # A mode-compatible overlay with no required content (clock) must
        # validate on the modes the filter offers it for.
        cfg = cfgmod.Config()
        cfg.scenes = [
            cfgmod.SceneCfg(
                type="blank", display="blank", overlays=[{"type": "clock", "corner": "top-right"}]
            )
        ]
        self.assertIsNone(wizard.validate(cfg))


class ScanAssetsTest(unittest.TestCase):
    def test_scans_matching_extensions(self):
        with tempfile.TemporaryDirectory() as d:
            for fn in ("a.sid", "b.SID", "c.txt", "d.mp4"):
                open(os.path.join(d, fn), "w").close()
            found = wizard.scan_assets(d, (".sid",))
            self.assertEqual([os.path.basename(f) for f in found], ["a.sid", "b.SID"])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(wizard.scan_assets("/no/such/dir", (".sid",)), [])


class BuildConfigTest(unittest.TestCase):
    def test_minimal_webcam_round_trips_and_validates(self):
        cfg = wizard.build_config(
            scene_type="webcam",
            scene_fields={"display": "petscii", "style": "neon"},
            overlays=[{"type": "clock", "corner": "top-right"}],
            url="http://example.lan",
            system="PAL",
            audio_enabled=True,
        )
        self.assertIsNone(wizard.validate(cfg))
        self.assertEqual(cfg.ultimate64.url, "http://example.lan")
        self.assertEqual(cfg.ultimate64.system, "PAL")
        self.assertTrue(cfg.audio.enabled)
        self.assertEqual(cfg.scenes[0].overlays[0]["type"], "clock")
        # Round-trips through the serializer.
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as f:
            f.write(ser.dumps(cfg))
            path = f.name
        try:
            self.assertEqual(cfgmod.load(path), cfg)
        finally:
            os.unlink(path)

    def test_waveform_scene_builds(self):
        cfg = wizard.build_config(
            scene_type="waveform",
            scene_fields={"file": "assets/sids/x.sid", "persistence": "long"},
            overlays=[],
        )
        self.assertEqual(cfg.scenes[0].type, "waveform")
        self.assertEqual(cfg.scenes[0].persistence, "long")


class MakeSceneTest(unittest.TestCase):
    def test_make_scene_applies_fields_and_copies_overlays(self):
        ov: list[dict[str, object]] = [{"type": "clock", "corner": "top-right"}]
        scene = wizard.make_scene("webcam", {"display": "petscii", "name": "Cam"}, ov)
        self.assertEqual(scene.type, "webcam")
        self.assertEqual(scene.display, "petscii")
        self.assertEqual(scene.name, "Cam")
        # overlays are copied, not aliased.
        self.assertEqual(scene.overlays, ov)
        self.assertIsNot(scene.overlays[0], ov[0])


class BuildMultiConfigTest(unittest.TestCase):
    def _two_scenes(self):
        return [
            wizard.make_scene("webcam", {"display": "petscii"}, []),
            wizard.make_scene("blank", {"display": "blank", "name": "Card"}, []),
        ]

    def test_preserves_order_and_applies_overrides(self):
        cfg = wizard.build_multi_config(
            scenes=self._two_scenes(),
            url="http://example.lan",
            system="PAL",
            audio_enabled=True,
            playlist={"loop": False, "interleave_videos": True, "videos_dir": "assets/videos"},
            interstitial={"duration_s": 2.0, "background": "starfield"},
        )
        self.assertEqual([s.type for s in cfg.scenes], ["webcam", "blank"])
        self.assertEqual(cfg.ultimate64.url, "http://example.lan")
        self.assertEqual(cfg.ultimate64.system, "PAL")
        self.assertTrue(cfg.audio.enabled)
        self.assertIs(cfg.playlist.loop, False)
        self.assertTrue(cfg.playlist.interleave_videos)
        self.assertEqual(cfg.playlist.videos_dir, "assets/videos")
        self.assertEqual(cfg.interstitial.duration_s, 2.0)
        self.assertEqual(cfg.interstitial.background, "starfield")

    def test_round_trips_through_serializer(self):
        cfg = wizard.build_multi_config(
            scenes=self._two_scenes(),
            url="http://example.lan",
            system="NTSC",
            playlist={"loop": False},
        )
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as f:
            f.write(ser.dumps(cfg))
            path = f.name
        try:
            self.assertEqual(cfgmod.load(path), cfg)
        finally:
            os.unlink(path)

    def test_no_overrides_leaves_section_defaults(self):
        cfg = wizard.build_multi_config(scenes=self._two_scenes())
        self.assertEqual(cfg.playlist, cfgmod.PlaylistCfg())
        self.assertEqual(cfg.interstitial, cfgmod.InterstitialCfg())


class ValidateAllTest(unittest.TestCase):
    def test_all_valid_returns_empty(self):
        cfg = wizard.build_multi_config(
            scenes=[
                wizard.make_scene("webcam", {"display": "petscii"}, []),
                wizard.make_scene("blank", {"display": "blank"}, []),
            ]
        )
        self.assertEqual(wizard.validate_all(cfg), [])

    def test_collects_one_message_per_bad_scene(self):
        cfg = wizard.build_multi_config(
            scenes=[
                wizard.make_scene("blank", {"display": "blank"}, []),  # ok
                # clock overlay on an mcm scene -> rejected (mcm isn't
                # PETSCII- or bitmap-text-compatible; hires would now fold it).
                wizard.make_scene("webcam", {"display": "mcm", "name": "Bad"}, [{"type": "clock"}]),
            ]
        )
        errs = wizard.validate_all(cfg)
        self.assertEqual(len(errs), 1)
        self.assertIn("scene 2 (Bad)", errs[0])


class SupportedDisplaysTest(unittest.TestCase):
    def test_waveform_has_no_displays(self):
        self.assertEqual(wizard.supported_displays("waveform"), ())

    def test_webcam_displays(self):
        self.assertIn("petscii", wizard.supported_displays("webcam"))


class _Resp:
    def __init__(self, value):
        self._value = value

    def ask(self):
        return self._value


class _FakeQ:
    """A scripted stand-in for the questionary module: routes each prompt to a
    canned answer by matching a substring of its label. Lets run_init's shell
    be driven headlessly so the wiring (propagation, file write) is covered.

    A route whose value is a ``list`` is *sequenced*: consumed one item per
    call (the i-th time that label is matched yields the i-th item). This drives
    the multi-scene flow, where "Scene type", "Display mode", "Playlist
    action", etc. are each asked repeatedly with the same label."""

    def __init__(self, routes: dict, write_path: str):
        self._routes = routes
        self._write_path = write_path
        self._seq: dict = {}

    def _answer(self, label, choices=None):
        for key, val in self._routes.items():
            if key in label:
                if isinstance(val, list):
                    i = self._seq.get(key, 0)
                    self._seq[key] = i + 1
                    val = val[i]
                return val(choices) if callable(val) else val
        raise AssertionError(f"unscripted prompt: {label!r}")

    def select(self, label, choices=None, default=None, instruction=None):
        return _Resp(self._answer(label, choices))

    def text(self, label, default="", validate=None, instruction=None):
        ans = self._answer(label)
        return _Resp(self._write_path if "Write to" in label else ans)

    def confirm(self, label, default=False, instruction=None):
        v = self._answer(label)
        return _Resp(v if isinstance(v, bool) else bool(v))

    def checkbox(self, label, choices=None, instruction=None):
        return _Resp(self._answer(label, choices))


class RunInitShellTest(unittest.TestCase):
    def test_headless_webcam_build_writes_valid_config(self):
        import contextlib
        import io

        from c64cast.app import wizard as wz

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out.toml")
            routes = {
                "Build a single": wizard._SINGLE_LABEL,
                "Scene type": lambda choices: next(c for c in choices if c.startswith("webcam")),
                "Display mode": "petscii",
                "Scene name": "My Scene",
                "Enable SID audio": True,
                "Video-audio backend": "auto",  # audio on → backend prompt fires
                "advanced": False,
                "Add overlays": True,
                "Select overlays": lambda choices: [c for c in choices if c.startswith("clock")],
                "clock.": "",  # leave clock params default
                "Ultimate 64 URL": "http://example.lan",
                "Machine timing": "PAL",
                "Write to": out,  # text() special-cases this
                "Write ": True,  # "Write <path>?"
                "Launch": False,
            }
            orig = wz._ensure_questionary
            wz._ensure_questionary = lambda: _FakeQ(routes, out)  # type: ignore[assignment]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = wz.run_init(out)
            finally:
                wz._ensure_questionary = orig  # type: ignore[assignment]

            assert result is not None  # narrow for the type checker
            path, launch = result
            self.assertEqual(path, out)
            self.assertFalse(launch)
            # The written file loads, validates, and has what we asked for.
            cfg = cfgmod.load(out)
            self.assertEqual(cfg.scenes[0].type, "webcam")
            self.assertEqual(cfg.scenes[0].display, "petscii")
            self.assertEqual(cfg.scenes[0].name, "My Scene")
            self.assertTrue(cfg.audio.enabled)
            self.assertEqual(cfg.audio.backend, "auto")
            self.assertEqual(cfg.ultimate64.system, "PAL")
            self.assertEqual(cfg.scenes[0].overlays[0]["type"], "clock")
            self.assertIsNone(wizard.validate(cfg))

    def test_headless_multi_scene_build_reorders_and_sets_loop(self):
        import contextlib
        import io

        from c64cast.app import wizard as wz

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "multi.toml")
            routes = {
                "Build a single": wizard._MULTI_LABEL,
                "Enable SID audio": False,  # global audio off
                # Two adds, one move, then done.
                "Playlist action": ["Add a scene", "Add a scene", "Move a scene", "Done"],
                # First add = webcam, second = video.
                "Scene type": [
                    lambda choices: next(c for c in choices if c.startswith("webcam")),
                    lambda choices: next(c for c in choices if c.startswith("video")),
                ],
                # video file picker (select branch -> custom -> text).
                "Pick a file": lambda choices: next(c for c in choices if "Type a path" in c),
                "file spec": "assets/videos/clip.mp4",
                "Display mode": lambda choices: choices[0],
                "Scene name": "",
                "advanced": False,
                "Add overlays": False,
                # Move webcam (scene 1) to the end -> [video, webcam].
                "Move which": lambda choices: choices[0],
                "Move to which": lambda choices: next(c for c in choices if "to the end" in c),
                "Loop the playlist": False,
                "Interleave": False,
                "Customize": False,
                "Ultimate 64 URL": "http://example.lan",
                "Machine timing": "NTSC",
                "Write to": out,
                "Write ": True,
                "Launch": False,
            }
            orig = wz._ensure_questionary
            wz._ensure_questionary = lambda: _FakeQ(routes, out)  # type: ignore[assignment]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = wz.run_init(out)
            finally:
                wz._ensure_questionary = orig  # type: ignore[assignment]

            assert result is not None
            path, launch = result
            self.assertEqual(path, out)
            self.assertFalse(launch)
            cfg = cfgmod.load(out)
            self.assertEqual([s.type for s in cfg.scenes], ["video", "webcam"])
            self.assertIs(cfg.playlist.loop, False)
            self.assertEqual(cfg.scenes[0].file, "assets/videos/clip.mp4")
            self.assertEqual(wizard.validate_all(cfg), [])

    def test_missing_dependency_returns_none(self):
        from c64cast.app import wizard as wz

        orig = wz._ensure_questionary
        wz._ensure_questionary = lambda: None  # type: ignore[assignment]
        try:
            import contextlib
            import io

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(wz.run_init(None))
        finally:
            wz._ensure_questionary = orig  # type: ignore[assignment]


class FieldKindsTest(unittest.TestCase):
    """`field_kind` answers with one kind because a prompt asks one question.
    `field_kinds` answers with the whole union because a form has room for it —
    which is what stops `border` (`int | str`) rendering as a number box under
    help text that says you may write "light blue"."""

    def test_a_plain_type_is_one_kind(self):
        self.assertEqual(wizard.field_kinds("float"), ("float",))
        self.assertEqual(wizard.field_kinds("str"), ("str",))

    def test_a_union_keeps_both_halves_in_declaration_order(self):
        self.assertEqual(wizard.field_kinds("int | str"), ("int", "str"))
        self.assertEqual(wizard.field_kinds("str | int"), ("str", "int"))

    def test_a_list_member_does_not_leak_its_element_types(self):
        # The `str` inside the list is not something the field accepts on its
        # own, so splitting has to stop at the top level.
        self.assertEqual(wizard.field_kinds("int | list[int | str]"), ("int", "complex"))

    def test_repeats_collapse(self):
        self.assertEqual(wizard.field_kinds("int | float | int"), ("int", "float"))

    def test_the_one_question_classifier_is_unchanged(self):
        # A prompt has to handle the hardest member, and the wizard skips
        # `complex` in its generic walk — so this must keep saying "complex".
        self.assertEqual(wizard.field_kind("int | list[int | str]"), "complex")
        self.assertEqual(wizard.field_kind("int | str"), "int")

    def test_the_real_union_fields_split_as_expected(self):
        from c64cast.app import introspect

        scene_fields = {f.name: f for st in introspect.scene_types() for f in st.fields}
        self.assertEqual(wizard.field_kinds(scene_fields["border"].type), ("int", "str"))
        color = {f.name: f for s in introspect.config_sections() for f in s.fields}
        self.assertEqual(wizard.field_kinds(color["force_palette_colors"].type), ("int", "complex"))


if __name__ == "__main__":
    unittest.main()
