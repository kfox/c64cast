"""Tests for c64cast.doctor — collect-all config validation surface."""

from __future__ import annotations

import io
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from c64cast import config as cfgmod
from c64cast import doctor


def _write(path: str, body: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


def _load(toml: str, suffix: str = ".toml") -> cfgmod.LoadResult:
    """Helper: write a single-system TOML to a tempfile, load via
    load_master, return the LoadResult."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "single" + suffix)
        _write(path, toml)
        return cfgmod.load_master(path)


class ValidateScenesTest(unittest.TestCase):
    """Per-scene validation — every misconfig surfaces as its own
    Diagnostic instead of aborting at the first error."""

    def test_valid_scene_produces_ok_diagnostic(self):
        loaded = _load("""
            [[scenes]]
            type = "blank"
            name = "title"
        """)
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        scene_diags = [d for d in diags if d.category == "scene"]
        self.assertEqual(len(scene_diags), 1)
        self.assertEqual(scene_diags[0].level, "ok")
        self.assertEqual(scene_diags[0].subject, "system/title")

    def test_unknown_display_mode_surfaces_as_error(self):
        loaded = _load("""
            [[scenes]]
            type = "webcam"
            display = "petsci"
            name = "typo"
        """)
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        scene_diags = [d for d in diags if d.category == "scene"]
        self.assertEqual(len(scene_diags), 1)
        self.assertEqual(scene_diags[0].level, "error")
        self.assertIn("unknown display mode", scene_diags[0].message)

    def test_multiple_bad_scenes_all_reported(self):
        """The whole point of doctor mode: scene 1's failure must not hide
        scene 2's failure. Use explicit-but-missing globs so the test is
        independent of whether the dev's repo has populated default
        asset dirs (video -> assets/videos, waveform -> assets/sids
        would otherwise satisfy the no-file fallback)."""
        loaded = _load("""
            [[scenes]]
            type = "video"
            name = "bad-file"
            display = "hires"
            file = "/nonexistent/*.mp4"

            [[scenes]]
            type = "waveform"
            name = "bad-sid"
            file = "/nonexistent/*.sid"

            [[scenes]]
            type = "blank"
            name = "good"
        """)
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        scene_diags = [d for d in diags if d.category == "scene"]
        self.assertEqual(len(scene_diags), 3)
        subjects = {d.subject: d.level for d in scene_diags}
        self.assertEqual(subjects["system/bad-file"], "error")
        self.assertEqual(subjects["system/bad-sid"], "error")
        self.assertEqual(subjects["system/good"], "ok")

    def test_overlay_incompatibility_surfaces_at_scene_level(self):
        # mcm is neither PETSCII- nor bitmap-text-compatible, so a text overlay
        # is rejected there (on hires/mhires it would now fold into the bitmap).
        loaded = _load("""
            [[scenes]]
            type = "webcam"
            display = "mcm"
            name = "clockless"
            [[scenes.overlays]]
            type = "clock"
        """)
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        scene_diags = [d for d in diags if d.category == "scene"]
        self.assertEqual(scene_diags[0].level, "error")
        self.assertIn("petscii", scene_diags[0].message)


class CrossSystemOrchestrationTest(unittest.TestCase):
    """Conductors need same-name follower scenes in every other system,
    else the Playlist silently falls back to the conductor cfg."""

    def _master(self, tmp: str, members: dict[str, str]) -> str:
        master_path = os.path.join(tmp, "master.toml")
        entries = ",\n    ".join(f'{{ name = "{n}", config = "{n}.toml" }}' for n in members)
        master_body = f"[ensemble]\nsystems = [\n    {entries}\n]\n"
        _write(master_path, master_body)
        for name, body in members.items():
            _write(os.path.join(tmp, f"{name}.toml"), body)
        return master_path

    def test_conductor_with_no_follower_warns(self):
        # `right` has a conductor 'morning-hello'; `left` has no scene by
        # that name. We use big_text-shaped scenes so the orchestrator
        # resolves cleanly.
        right = textwrap.dedent("""
            [ultimate64]
            url = "http://right.lan"

            [[scenes]]
            type = "blank"
            name = "morning-hello"
            orchestrate = true
            [[scenes.overlays]]
            type = "big_text"
            messages = ["GOOD MORNING"]
        """)
        left = textwrap.dedent("""
            [ultimate64]
            url = "http://left.lan"

            [[scenes]]
            type = "blank"
            name = "left-idle"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            master = self._master(tmp, {"right": right, "left": left})
            with self.assertLogs("c64cast.config", level="INFO"):
                loaded = cfgmod.load_master(master)
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        orch_diags = [d for d in diags if d.category == "orchestrator" and d.level == "warn"]
        self.assertEqual(len(orch_diags), 1)
        self.assertEqual(orch_diags[0].subject, "right/morning-hello")
        self.assertIn("left", orch_diags[0].message)

    def test_conductor_with_follower_in_every_system_no_warn(self):
        # Same conductor, this time `left` also has a 'morning-hello' scene.
        right = textwrap.dedent("""
            [ultimate64]
            url = "http://right.lan"

            [[scenes]]
            type = "blank"
            name = "morning-hello"
            orchestrate = true
            [[scenes.overlays]]
            type = "big_text"
            messages = ["HELLO"]
        """)
        left = textwrap.dedent("""
            [ultimate64]
            url = "http://left.lan"

            [[scenes]]
            type = "blank"
            name = "morning-hello"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            master = self._master(tmp, {"right": right, "left": left})
            with self.assertLogs("c64cast.config", level="INFO"):
                loaded = cfgmod.load_master(master)
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        orch_warnings = [d for d in diags if d.category == "orchestrator" and d.level == "warn"]
        self.assertEqual(orch_warnings, [])


class ExtrasProbeTest(unittest.TestCase):
    def test_missing_extra_reported_with_pip_hint(self):
        # Pretend `av` is not installed; everything else stays real.
        real = doctor.importlib.util.find_spec

        def fake(name):
            if name == "av":
                return None
            return real(name)

        loaded = _load("")  # no scenes; we only care about extras
        with mock.patch.object(doctor.importlib.util, "find_spec", side_effect=fake):
            diags = doctor.validate_load_result(loaded, probe_u64=False)
        video_diags = [d for d in diags if d.category == "extras" and d.subject == "video"]
        self.assertEqual(len(video_diags), 1)
        self.assertEqual(video_diags[0].level, "warn")
        self.assertEqual(video_diags[0].hint, "uv sync --all-extras")


class ConnectivityProbeTest(unittest.TestCase):
    def test_socket_dma_error_becomes_diagnostic_not_exception(self):
        from c64cast.socket_dma import SocketDMAError

        loaded = _load("""
            [ultimate64]
            url = "http://unreachable.example"
        """)
        with mock.patch(
            "c64cast.api.Ultimate64API.__init__", side_effect=SocketDMAError("connection refused")
        ):
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        conn = [d for d in diags if d.category == "connectivity"]
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0].level, "error")
        self.assertIn("connection refused", conn[0].message)
        self.assertIsNotNone(conn[0].hint)

    def test_probe_u64_false_skips_connectivity_entirely(self):
        loaded = _load("")
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        conn = [d for d in diags if d.category == "connectivity"]
        self.assertEqual(conn, [])


class ReuStatusProbeTest(unittest.TestCase):
    """REU enable check fires only when the config opts into a REU path.
    Catches the silent-failure mode where REU is off at the U64 — staged
    audio plays silence, staged video stays unchanged."""

    def _patch_connectivity_to_reu_status(self, loaded, status: str):
        """Drive _probe_connectivity end-to-end with mocks. Returns the
        Diagnostics. `status` is the value the REST endpoint should return
        for "RAM Expansion Unit". Wire shape matches Ultimate firmware
        3.x: top-level dict with the category name as a key wrapping the
        actual setting dict."""
        from c64cast.api import Ultimate64API

        fake_response = mock.MagicMock()
        fake_response.json.return_value = {
            "C64 and Cartridge Settings": {
                "RAM Expansion Unit": status,
                "REU Size": "16 MB",
            },
            "errors": [],
        }
        fake_response.raise_for_status = mock.MagicMock()

        # Build a real-shaped Ultimate64API instance but no actual sockets.
        with mock.patch.object(Ultimate64API, "__init__", return_value=None):
            api_instance = Ultimate64API.__new__(Ultimate64API)
            api_instance.base_url = "http://fake"
            api_instance.session = mock.MagicMock()
            api_instance.session.get.return_value = fake_response
            api_instance.probe = mock.MagicMock(return_value="HTTP 200")
            api_instance.close = mock.MagicMock()
            with mock.patch("c64cast.api.Ultimate64API", return_value=api_instance):
                return doctor.validate_load_result(loaded, probe_u64=True)

    def test_no_reu_request_skips_reu_probe(self):
        """Default config (no REU opt-in) must not run the REU REST query.
        Avoids slowing down doctor mode for users who don't use REU paths."""
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
        """)
        diags = self._patch_connectivity_to_reu_status(loaded, "Enabled")
        reu = [d for d in diags if d.subject.endswith("(REU)")]
        self.assertEqual(reu, [], "REU probe should not run without opt-in")

    def test_auto_use_reu_staged_is_not_a_hard_requirement(self):
        """The default `use_reu_staged = "auto"` is self-healing (it falls
        back to host-DMA when REU is off), so the doctor must NOT demand REU —
        even with REU disabled and a bitmap scene, no REU diagnostic fires."""
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [video]
            use_reu_staged = "auto"
            [[scenes]]
            type = "video"
            display = "mhires"
            file = "x.mp4"
        """)
        diags = self._patch_connectivity_to_reu_status(loaded, "Disabled")
        reu = [d for d in diags if d.subject.endswith("(REU)")]
        self.assertEqual(reu, [], "auto must not make the doctor require REU")

    def test_reu_enabled_is_ok_when_use_reu_pump(self):
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            use_reu_pump = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
            name = "mic"
        """)
        diags = self._patch_connectivity_to_reu_status(loaded, "Enabled")
        reu = [d for d in diags if d.subject.endswith("(REU)")]
        self.assertEqual(len(reu), 1)
        self.assertEqual(reu[0].level, "ok")
        self.assertIn("16 MB", reu[0].message)
        self.assertIn("use_reu_pump", reu[0].message)

    def test_reu_enabled_is_ok_when_use_reu_staged(self):
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [video]
            use_reu_staged = true
            [[scenes]]
            type = "blank"
        """)
        diags = self._patch_connectivity_to_reu_status(loaded, "Enabled")
        reu = [d for d in diags if d.subject.endswith("(REU)")]
        self.assertEqual(len(reu), 1)
        self.assertEqual(reu[0].level, "ok")
        self.assertIn("use_reu_staged", reu[0].message)

    def test_reu_disabled_is_error_with_actionable_hint(self):
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            use_reu_pump = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        diags = self._patch_connectivity_to_reu_status(loaded, "Disabled")
        reu = [d for d in diags if d.subject.endswith("(REU)")]
        self.assertEqual(len(reu), 1)
        self.assertEqual(reu[0].level, "error", "REU disabled + REU config opt-in must be an error")
        # Message names which config flag and what fails silently:
        self.assertIn("Disabled", reu[0].message)
        self.assertIn("silently", reu[0].message)
        # Hint points the user at the U64 menu path AND offers the
        # toml-side fallback:
        self.assertIsNotNone(reu[0].hint)
        assert reu[0].hint is not None  # narrow for type checker
        self.assertIn("F2", reu[0].hint)
        self.assertIn("RAM Expansion Unit", reu[0].hint)

    def test_rest_failure_during_reu_probe_warns(self):
        import requests

        from c64cast.api import Ultimate64API

        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            use_reu_pump = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        with mock.patch.object(Ultimate64API, "__init__", return_value=None):
            api_instance = Ultimate64API.__new__(Ultimate64API)
            api_instance.base_url = "http://fake"
            api_instance.session = mock.MagicMock()
            api_instance.session.get.side_effect = requests.Timeout("read timeout")
            api_instance.probe = mock.MagicMock(return_value="HTTP 200")
            api_instance.close = mock.MagicMock()
            with mock.patch("c64cast.api.Ultimate64API", return_value=api_instance):
                diags = doctor.validate_load_result(loaded, probe_u64=True)
        reu = [d for d in diags if d.subject.endswith("(REU)")]
        self.assertEqual(len(reu), 1)
        self.assertEqual(reu[0].level, "warn")
        self.assertIn("REST query", reu[0].message)
        # Hint still actionable when we can't tell:
        assert reu[0].hint is not None
        self.assertIn("RAM Expansion Unit", reu[0].hint)

    def test_dma_failure_skips_reu_probe(self):
        """When the DMA connect itself fails, we never reach REST, so no
        REU diagnostic. The single DMA error is the right user feedback —
        adding a redundant REU warn would just be noise."""
        from c64cast.socket_dma import SocketDMAError

        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            use_reu_pump = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        with mock.patch(
            "c64cast.api.Ultimate64API.__init__", side_effect=SocketDMAError("connection refused")
        ):
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        reu = [d for d in diags if d.subject.endswith("(REU)")]
        self.assertEqual(reu, [])


class ReuIsEnabledHelperTest(unittest.TestCase):
    """doctor.reu_is_enabled() — the cli build_stack uses this to resolve the
    [video].use_reu_staged "auto" setting. True/False on a clean read, None on
    any failure or unrecognized shape (treated as "not available" upstream)."""

    def _api(self, *, json_value=None, get_side_effect=None):
        api = mock.MagicMock()
        api.base_url = "http://fake"
        if get_side_effect is not None:
            api.session.get.side_effect = get_side_effect
        else:
            resp = mock.MagicMock()
            resp.json.return_value = json_value
            resp.raise_for_status = mock.MagicMock()
            api.session.get.return_value = resp
        return api

    def _section(self, status):
        return {
            "C64 and Cartridge Settings": {"RAM Expansion Unit": status, "REU Size": "16 MB"},
            "errors": [],
        }

    def test_enabled_true(self):
        api = self._api(json_value=self._section("Enabled"))
        self.assertIs(doctor.reu_is_enabled(api), True)

    def test_disabled_false(self):
        api = self._api(json_value=self._section("Disabled"))
        self.assertIs(doctor.reu_is_enabled(api), False)

    def test_query_failure_none(self):
        import requests

        api = self._api(get_side_effect=requests.Timeout("read timeout"))
        self.assertIsNone(doctor.reu_is_enabled(api))

    def test_unrecognized_shape_none(self):
        api = self._api(json_value=["unexpected"])
        self.assertIsNone(doctor.reu_is_enabled(api))


class SidStatusProbeTest(unittest.TestCase):
    """Emulated-SID enable check fires only when the config drives the SID
    (audio streaming, or a waveform/midi scene). Catches the U2+ case where
    the emulated SID ships disabled and every tune is silent while video +
    the host-emulated oscilloscope keep working."""

    def _patch_connectivity_to_sid_status(self, loaded, left: str, right: str):
        """Drive _probe_connectivity end-to-end with mocks. `left`/`right`
        are the values the REST endpoint returns for "SID Left"/"SID Right".
        Wire shape matches Ultimate firmware 3.x."""
        from c64cast.api import Ultimate64API

        fake_response = mock.MagicMock()
        fake_response.json.return_value = {
            "Audio Output Settings": {
                "SID Left": left,
                "SID Left Base": "Snoop $D400",
                "SID Right": right,
            },
            "errors": [],
        }
        fake_response.raise_for_status = mock.MagicMock()

        with mock.patch.object(Ultimate64API, "__init__", return_value=None):
            api_instance = Ultimate64API.__new__(Ultimate64API)
            api_instance.base_url = "http://fake"
            api_instance.session = mock.MagicMock()
            api_instance.session.get.return_value = fake_response
            api_instance.probe = mock.MagicMock(return_value="HTTP 200")
            api_instance.close = mock.MagicMock()
            with mock.patch("c64cast.api.Ultimate64API", return_value=api_instance):
                return doctor.validate_load_result(loaded, probe_u64=True)

    def test_no_sid_request_skips_sid_probe(self):
        """A config with no SID-driving scene and audio off must not run the
        SID REST query."""
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [[scenes]]
            type = "slideshow"
            display = "mhires"
        """)
        diags = self._patch_connectivity_to_sid_status(loaded, "Enabled", "Enabled")
        sid = [d for d in diags if d.subject.endswith("(SID)")]
        self.assertEqual(sid, [], "SID probe should not run without SID audio")

    def test_sid_enabled_is_ok_when_audio_streaming(self):
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        diags = self._patch_connectivity_to_sid_status(loaded, "Enabled", "Disabled")
        sid = [d for d in diags if d.subject.endswith("(SID)")]
        self.assertEqual(len(sid), 1)
        self.assertEqual(sid[0].level, "ok")
        self.assertIn("[audio].enabled", sid[0].message)

    def test_waveform_scene_drives_sid_even_with_audio_off(self):
        """A waveform scene plays the SID via run_sid_player regardless of
        [audio].enabled, so the SID probe must still fire."""
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [[scenes]]
            type = "waveform"
            file = "x.sid"
        """)
        diags = self._patch_connectivity_to_sid_status(loaded, "Disabled", "Disabled")
        sid = [d for d in diags if d.subject.endswith("(SID)")]
        self.assertEqual(len(sid), 1)
        self.assertEqual(sid[0].level, "warn")
        self.assertIn("waveform", sid[0].message)

    def test_both_sids_disabled_is_warn_with_actionable_hint(self):
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        diags = self._patch_connectivity_to_sid_status(loaded, "Disabled", "Disabled")
        sid = [d for d in diags if d.subject.endswith("(SID)")]
        self.assertEqual(len(sid), 1)
        self.assertEqual(sid[0].level, "warn", "both SIDs off + SID audio wanted is a warn")
        self.assertIn("silent", sid[0].message)
        assert sid[0].hint is not None
        self.assertIn("Audio Output Settings", sid[0].hint)
        self.assertIn("Snoop $D400", sid[0].hint)

    def test_rest_failure_during_sid_probe_warns(self):
        import requests

        from c64cast.api import Ultimate64API

        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        with mock.patch.object(Ultimate64API, "__init__", return_value=None):
            api_instance = Ultimate64API.__new__(Ultimate64API)
            api_instance.base_url = "http://fake"
            api_instance.session = mock.MagicMock()
            api_instance.session.get.side_effect = requests.Timeout("read timeout")
            api_instance.probe = mock.MagicMock(return_value="HTTP 200")
            api_instance.close = mock.MagicMock()
            with mock.patch("c64cast.api.Ultimate64API", return_value=api_instance):
                diags = doctor.validate_load_result(loaded, probe_u64=True)
        sid = [d for d in diags if d.subject.endswith("(SID)")]
        self.assertEqual(len(sid), 1)
        self.assertEqual(sid[0].level, "warn")
        self.assertIn("REST query", sid[0].message)

    def test_unrecognized_shape_stays_quiet(self):
        """Firmware that doesn't expose SID Left/Right must not emit a
        misleading warning."""
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        from c64cast.api import Ultimate64API

        fake_response = mock.MagicMock()
        fake_response.json.return_value = {"Audio Output Settings": {}, "errors": []}
        fake_response.raise_for_status = mock.MagicMock()
        with mock.patch.object(Ultimate64API, "__init__", return_value=None):
            api_instance = Ultimate64API.__new__(Ultimate64API)
            api_instance.base_url = "http://fake"
            api_instance.session = mock.MagicMock()
            api_instance.session.get.return_value = fake_response
            api_instance.probe = mock.MagicMock(return_value="HTTP 200")
            api_instance.close = mock.MagicMock()
            with mock.patch("c64cast.api.Ultimate64API", return_value=api_instance):
                diags = doctor.validate_load_result(loaded, probe_u64=True)
        sid = [d for d in diags if d.subject.endswith("(SID)")]
        self.assertEqual(sid, [])


class PrintReportTest(unittest.TestCase):
    def test_exit_code_zero_when_no_errors(self):
        diags = [
            doctor.Diagnostic("ok", "scene", "s/a", "fine"),
            doctor.Diagnostic("warn", "extras", "obs", "missing"),
        ]
        buf = io.StringIO()
        self.assertEqual(doctor.print_report(diags, file=buf), 0)
        self.assertIn("1 ok, 1 warn, 0 error", buf.getvalue())

    def test_exit_code_one_when_any_error(self):
        diags = [
            doctor.Diagnostic("ok", "scene", "s/a", "fine"),
            doctor.Diagnostic("error", "scene", "s/b", "bad"),
        ]
        buf = io.StringIO()
        self.assertEqual(doctor.print_report(diags, file=buf), 1)
        self.assertIn("[ERR ]", buf.getvalue())


class EnvironmentProbeTest(unittest.TestCase):
    """The env probe is the dev-environment guard: it catches the desynced
    .venv / wrong-interpreter case where a hard dependency won't import."""

    def test_reports_interpreter_and_every_hard_dep(self):
        # Skip the uv subprocess; this test is about the import surface.
        with mock.patch.object(doctor, "_probe_uv_lock", return_value=[]):
            diags = doctor._probe_environment()
        self.assertTrue(diags)
        self.assertTrue(all(d.category == "environment" for d in diags))
        subjects = {d.subject for d in diags}
        self.assertIn("interpreter", subjects)
        for dep, _ in doctor._HARD_DEPS:
            self.assertIn(dep, subjects)

    def test_hard_deps_import_ok_in_synced_env(self):
        with mock.patch.object(doctor, "_probe_uv_lock", return_value=[]):
            diags = doctor._probe_environment()
        dep_levels = {d.subject: d.level for d in diags if d.subject in dict(doctor._HARD_DEPS)}
        self.assertTrue(all(lvl == "ok" for lvl in dep_levels.values()), dep_levels)

    def test_missing_hard_dep_is_error_with_sync_hint(self):
        with (
            mock.patch.object(doctor, "_HARD_DEPS", (("no_such_module_xyz", "test only"),)),
            mock.patch.object(doctor, "_probe_uv_lock", return_value=[]),
        ):
            diags = doctor._probe_environment()
        errs = [d for d in diags if d.level == "error"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].subject, "no_such_module_xyz")
        self.assertIn("make sync", errs[0].hint or "")

    def test_interpreter_mismatch_warns(self):
        # A live but wrong interpreter (not the project .venv) should warn — the
        # exact "bare python resolved somewhere else" trap. Only meaningful when
        # the project .venv exists to compare against (it does in dev/CI).
        if not (doctor._REPO_ROOT / ".venv").exists():
            self.skipTest("no project .venv to compare against")
        with (
            mock.patch.object(doctor.sys, "prefix", "/tmp/definitely-not-the-venv"),
            mock.patch.object(doctor, "_probe_uv_lock", return_value=[]),
        ):
            diags = doctor._probe_environment()
        interp = [d for d in diags if d.subject == "interpreter"]
        self.assertEqual(len(interp), 1)
        self.assertEqual(interp[0].level, "warn")
        self.assertIsNotNone(interp[0].hint)

    def test_uv_lock_skipped_when_uv_absent(self):
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            diags = doctor._probe_uv_lock()
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("skipped", diags[0].message)

    def test_uv_lock_drift_warns(self):
        fake = mock.MagicMock(returncode=1, stdout="", stderr="")
        with (
            mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/uv"),
            mock.patch.object(doctor.subprocess, "run", return_value=fake),
        ):
            diags = doctor._probe_uv_lock()
        self.assertEqual(diags[0].level, "warn")
        self.assertIn("out of date", diags[0].message)

    def test_environment_runs_in_validate_load_result(self):
        with mock.patch.object(doctor, "_probe_uv_lock", return_value=[]):
            diags = doctor.validate_load_result(_load(""), probe_u64=False)
        self.assertTrue(any(d.category == "environment" for d in diags))


if __name__ == "__main__":
    unittest.main()
