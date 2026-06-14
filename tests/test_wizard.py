"""Tests for the config wizard's pure helpers.

The questionary I/O shell isn't exercised here (no terminal); these cover the
buildable logic — config assembly, compat-filtering, asset scanning, type
classification, and the schema-directive path math — which is where the
correctness lives.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from c64cast import config as cfgmod
from c64cast import config_serialize as ser
from c64cast import wizard


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
    def test_petscii_allows_clock_bitmap_does_not(self):
        petscii = {o.name for o in wizard.compatible_overlays("petscii", audio_enabled=False)}
        hires = {o.name for o in wizard.compatible_overlays("hires", audio_enabled=False)}
        self.assertIn("clock", petscii)
        self.assertNotIn("clock", hires)

    def test_hires_edges_maps_to_hires_runtime(self):
        # hires_edges and hires share runtime name 'hires' — same overlay set.
        a = {o.name for o in wizard.compatible_overlays("hires_edges", audio_enabled=False)}
        b = {o.name for o in wizard.compatible_overlays("hires", audio_enabled=False)}
        self.assertEqual(a, b)

    def test_audio_overlay_gated_by_audio_flag(self):
        without = {o.name for o in wizard.compatible_overlays("petscii", audio_enabled=False)}
        with_ = {o.name for o in wizard.compatible_overlays("petscii", audio_enabled=True)}
        self.assertIn("spectrum_petscii", with_)
        self.assertNotIn("spectrum_petscii", without)

    def test_filter_matches_introspect_gate(self):
        # compatible_overlays must agree with the authority (overlay_mode_ok +
        # the audio requirement) for every display mode — that's what keeps it
        # from offering a mode-incompatible overlay.
        from c64cast import introspect

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
            playlist={"loop": False, "interleave_ads": True, "ads_dir": "assets/videos"},
            interstitial={"duration_s": 2.0, "background": "starfield"},
        )
        self.assertEqual([s.type for s in cfg.scenes], ["webcam", "blank"])
        self.assertEqual(cfg.ultimate64.url, "http://example.lan")
        self.assertEqual(cfg.ultimate64.system, "PAL")
        self.assertTrue(cfg.audio.enabled)
        self.assertIs(cfg.playlist.loop, False)
        self.assertTrue(cfg.playlist.interleave_ads)
        self.assertEqual(cfg.playlist.ads_dir, "assets/videos")
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
                # clock overlay on a bitmap scene -> rejected.
                wizard.make_scene(
                    "webcam", {"display": "hires", "name": "Bad"}, [{"type": "clock"}]
                ),
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


class SchemaDirectiveTest(unittest.TestCase):
    def test_finds_repo_schema_relative(self):
        # Run from the repo root, where c64cast.schema.json lives.
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cwd = os.getcwd()
        os.chdir(repo)
        try:
            same = wizard.schema_directive_for("c64cast.toml")
            self.assertEqual(same, "./c64cast.schema.json")
            nested = wizard.schema_directive_for("config/examples/foo.toml")
            self.assertEqual(nested, "../../c64cast.schema.json")
        finally:
            os.chdir(cwd)

    def test_falls_back_when_no_schema(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(wizard.schema_directive_for("x.toml"), ser.DEFAULT_SCHEMA_PATH)
            finally:
                os.chdir(cwd)


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

        from c64cast import wizard as wz

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out.toml")
            routes = {
                "Build a single": wizard._SINGLE_LABEL,
                "Scene type": lambda choices: next(c for c in choices if c.startswith("webcam")),
                "Display mode": "petscii",
                "Scene name": "My Scene",
                "Enable SID audio": True,
                "advanced": False,
                "Add overlays": True,
                "Select overlays": lambda choices: [c for c in choices if c.startswith("clock")],
                "clock.": "",  # leave clock params default
                "Ultimate 64 URL": "http://example.lan",
                "Video system": "PAL",
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
            self.assertEqual(cfg.ultimate64.system, "PAL")
            self.assertEqual(cfg.scenes[0].overlays[0]["type"], "clock")
            self.assertIsNone(wizard.validate(cfg))

    def test_headless_multi_scene_build_reorders_and_sets_loop(self):
        import contextlib
        import io

        from c64cast import wizard as wz

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "multi.toml")
            routes = {
                "Build a single": wizard._MULTI_LABEL,
                "Enable SID audio": False,  # global audio off
                # Two adds, one move, then done.
                "Playlist action": ["Add a scene", "Add a scene", "Move a scene", "Done"],
                # First add = webcam, second = commercial.
                "Scene type": [
                    lambda choices: next(c for c in choices if c.startswith("webcam")),
                    lambda choices: next(c for c in choices if c.startswith("commercial")),
                ],
                # commercial file picker (select branch -> custom -> text).
                "Pick a file": lambda choices: next(c for c in choices if "Type a path" in c),
                "file spec": "assets/videos/clip.mp4",
                "Display mode": lambda choices: choices[0],
                "Scene name": "",
                "advanced": False,
                "Add overlays": False,
                # Move webcam (scene 1) to the end -> [commercial, webcam].
                "Move which": lambda choices: choices[0],
                "Move to which": lambda choices: next(c for c in choices if "to the end" in c),
                "Loop the playlist": False,
                "Interleave": False,
                "Customize": False,
                "Ultimate 64 URL": "http://example.lan",
                "Video system": "NTSC",
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
            self.assertEqual([s.type for s in cfg.scenes], ["commercial", "webcam"])
            self.assertIs(cfg.playlist.loop, False)
            self.assertEqual(cfg.scenes[0].file, "assets/videos/clip.mp4")
            self.assertEqual(wizard.validate_all(cfg), [])

    def test_missing_dependency_returns_none(self):
        from c64cast import wizard as wz

        orig = wz._ensure_questionary
        wz._ensure_questionary = lambda: None  # type: ignore[assignment]
        try:
            import contextlib
            import io

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(wz.run_init(None))
        finally:
            wz._ensure_questionary = orig  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
