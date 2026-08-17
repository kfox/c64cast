"""Tests for c64cast.app.doctor — collect-all config validation surface."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import textwrap
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from _fakes import FakeAPI

import c64cast
from c64cast.app import config as cfgmod
from c64cast.app import config_serialize as ser
from c64cast.app import doctor, paths
from c64cast.audio import dac_calibration_store
from c64cast.hw.backend import HardwareProfile


def _write(path: str, body: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


@contextlib.contextmanager
def _fake_ultimate_api(*, base_url: str = "http://fake") -> Iterator[Any]:
    """A real-shaped Ultimate64API instance with no sockets: __init__ is
    bypassed (it would open TCP connections) and the class name in
    c64cast.hw.api is patched so validate_load_result receives this
    instance. Yields it so the test can shape `session.get` / `probe`
    before driving the probe — one builder instead of the same seven-line
    block in every connectivity test."""
    from c64cast.hw.api import Ultimate64API
    from c64cast.hw.backend import ULTIMATE_PROFILE

    with mock.patch.object(Ultimate64API, "__init__", return_value=None):
        api_instance = Ultimate64API.__new__(Ultimate64API)
        api_instance.base_url = base_url
        api_instance.session = mock.MagicMock()
        api_instance.probe = mock.MagicMock(return_value="HTTP 200")
        api_instance.close = mock.MagicMock()
        # The real __init__ always sets a profile; refine_capabilities (run
        # by _probe_one_system after a successful probe) reads it.
        api_instance.profile = ULTIMATE_PROFILE
        with mock.patch("c64cast.hw.api.Ultimate64API", return_value=api_instance):
            yield api_instance


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
            with self.assertLogs("c64cast.app.config", level="INFO"):
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
            with self.assertLogs("c64cast.app.config", level="INFO"):
                loaded = cfgmod.load_master(master)
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        orch_warnings = [d for d in diags if d.category == "orchestrator" and d.level == "warn"]
        self.assertEqual(orch_warnings, [])


class ExtrasProbeTest(unittest.TestCase):
    def test_missing_extra_reported_with_install_hint(self):
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

    def test_missing_extra_hint_suits_an_installed_package(self):
        # `uv sync` is meaningless without a project to sync: an installed user
        # re-runs the tool install, and it names `[all]` because extras do not
        # accumulate. Every extra is installed in the dev env, so force one
        # missing.
        real = doctor.importlib.util.find_spec

        def fake(name, *a, **kw):
            return None if name == "av" else real(name)

        with mock.patch.object(doctor, "_running_from_checkout", return_value=False):
            with mock.patch.object(doctor.importlib.util, "find_spec", side_effect=fake):
                diags = doctor._probe_extras()
        missing = [d for d in diags if d.level == "warn"]
        self.assertTrue(missing, "expected the faked-missing extra to warn")
        for d in missing:
            with self.subTest(extra=d.subject):
                self.assertEqual(d.hint, 'uv tool install --force "c64cast[all]"')

    def test_camera_extra_is_probed(self):
        # The camera extra (cv2-enumerate-cameras) must appear in the extras
        # report — present-or-warn, either level is fine.
        loaded = _load("")
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        cam_diags = [d for d in diags if d.category == "extras" and d.subject == "camera"]
        self.assertEqual(len(cam_diags), 1)
        self.assertIn(cam_diags[0].level, ("ok", "warn"))


class ConnectivityProbeTest(unittest.TestCase):
    def test_socket_dma_error_becomes_diagnostic_not_exception(self):
        from c64cast.hw.socket_dma import SocketDMAError

        loaded = _load("""
            [ultimate64]
            url = "http://unreachable.example"
        """)
        with mock.patch(
            "c64cast.hw.api.Ultimate64API.__init__",
            side_effect=SocketDMAError("connection refused"),
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

    def _probe_with_dead_rest(self, toml: str) -> list:
        """DMA connects (Ultimate64API.__init__ mocked to no-op) but the REST
        probe returns None (web server down). Returns the connectivity diags."""
        loaded = _load(toml)
        with _fake_ultimate_api() as api_instance:
            api_instance.probe.return_value = None
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        return [d for d in diags if d.category == "connectivity"]

    def test_rest_probe_failure_is_error_for_sid_scene(self):
        """A waveform scene starts via the REST run_prg endpoint, so a dead
        REST link (DMA up, web server down) is an error, not a warning."""
        conn = self._probe_with_dead_rest("""
            [ultimate64]
            url = "http://fake"
            [[scenes]]
            type = "waveform"
            file = "assets/sids/x.sid"
        """)
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0].level, "error")
        self.assertIn("REST probe failed", conn[0].message)
        self.assertIn("cannot start", conn[0].message)
        self.assertIn("waveform", conn[0].message)
        assert conn[0].hint is not None
        self.assertIn("web/remote-control service", conn[0].hint)

    def test_rest_probe_failure_is_error_for_launcher_scene(self):
        conn = self._probe_with_dead_rest("""
            [ultimate64]
            url = "http://fake"
            [[scenes]]
            type = "launcher"
            file = "assets/prg/x.prg"
        """)
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0].level, "error")
        self.assertIn("launcher", conn[0].message)

    def test_rest_probe_failure_is_warn_for_dma_only_scene(self):
        """Video / slideshow / webcam / blank scenes paint entirely over DMA,
        so a dead REST link only degrades (keyboard/reset/launch) — a warning."""
        conn = self._probe_with_dead_rest("""
            [ultimate64]
            url = "http://fake"
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0].level, "warn")
        self.assertIn("REST probe failed", conn[0].message)
        self.assertNotIn("cannot start", conn[0].message)


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
        fake_response = mock.MagicMock()
        fake_response.json.return_value = {
            "C64 and Cartridge Settings": {
                "RAM Expansion Unit": status,
                "REU Size": "16 MB",
            },
            "errors": [],
        }
        fake_response.raise_for_status = mock.MagicMock()

        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.return_value = fake_response
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
        even with REU disabled and a bitmap scene, no REU diagnostic fires.

        backend = "dac" isolates this from the sampler path (the sampler is a
        separate hard REU reason — its own provisioning test covers that)."""
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            backend = "dac"
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

    def test_reu_disabled_is_error_when_auto_reu_off(self):
        """REU disabled + a hard REU opt-in is an error ONLY when the user has
        opted out of auto-provisioning (auto_reu = false). With auto_reu on
        (the default) the run enables it live — see the next test."""
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            auto_reu = false
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
        self.assertEqual(reu[0].level, "error", "REU disabled + opt-in + auto_reu off = error")
        # Message names which config flag and what fails silently:
        self.assertIn("Disabled", reu[0].message)
        self.assertIn("silently", reu[0].message)
        # Hint points the user at auto_reu AND the U64 menu path:
        self.assertIsNotNone(reu[0].hint)
        assert reu[0].hint is not None  # narrow for type checker
        self.assertIn("auto_reu", reu[0].hint)
        self.assertIn("RAM Expansion Unit", reu[0].hint)

    def test_reu_disabled_with_auto_reu_is_ok(self):
        """With auto_reu on (default), REU disabled + a hard opt-in is NOT an
        error — the run provisions the REU live at startup, so the doctor
        reports 'ok' and points at the auto-enable behavior."""
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
        self.assertEqual(reu[0].level, "ok", "auto_reu (default) must not error on a disabled REU")
        self.assertIn("auto_reu", reu[0].message)
        self.assertIn("16 MB", reu[0].message)

    def test_rest_failure_during_reu_probe_warns(self):
        import requests

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
        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.side_effect = requests.Timeout("read timeout")
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
        from c64cast.hw.socket_dma import SocketDMAError

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
            "c64cast.hw.api.Ultimate64API.__init__",
            side_effect=SocketDMAError("connection refused"),
        ):
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        reu = [d for d in diags if d.subject.endswith("(REU)")]
        self.assertEqual(reu, [])


class SidStatusProbeTest(unittest.TestCase):
    """Emulated-SID enable check fires only when the config drives the SID
    (audio streaming, or a waveform/midi scene). Catches the U2+ case where
    the emulated SID ships disabled and every tune is silent while video +
    the host-emulated oscilloscope keep working."""

    def _patch_connectivity_to_sid_status(self, loaded, left: str, right: str):
        """Drive _probe_connectivity end-to-end with mocks. `left`/`right`
        are the values the REST endpoint returns for "SID Left"/"SID Right".
        Wire shape matches Ultimate firmware 3.x."""
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

        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.return_value = fake_response
            return doctor.validate_load_result(loaded, probe_u64=True)

    def test_no_sid_request_skips_sid_probe(self):
        """A config with no SID-driving scene and audio off must not run the
        SID REST query."""
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = false
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

        loaded = _load("""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.side_effect = requests.Timeout("read timeout")
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
        fake_response = mock.MagicMock()
        fake_response.json.return_value = {"Audio Output Settings": {}, "errors": []}
        fake_response.raise_for_status = mock.MagicMock()
        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.return_value = fake_response
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

    def test_midi_control_category_is_rendered(self):
        # Regression: category_order previously omitted "midi_control", so
        # a midi_control Diagnostic was silently dropped from the printed
        # report (still counted in the ok/warn/error totals, but invisible
        # to the reader — the worst kind of drift, since nothing failed).
        diags = [doctor.Diagnostic("ok", "midi_control", "midi_control", "11 entries")]
        buf = io.StringIO()
        doctor.print_report(diags, file=buf)
        self.assertIn("MIDI_CONTROL", buf.getvalue())
        self.assertIn("11 entries", buf.getvalue())

    def test_every_category_used_in_source_is_in_category_order(self):
        # General drift guard: every category="..." literal doctor.py
        # actually constructs a Diagnostic with must be covered by
        # print_report's category_order, or it silently vanishes from the
        # printed report (same failure class as the midi_control omission
        # above — this test would have caught it).
        import inspect
        import re

        source = inspect.getsource(doctor)
        used = set(re.findall(r'category="([a-z_]+)"', source))
        self.assertTrue(used, "regex found no categories — pattern drifted from doctor.py's style")
        # Extract print_report's category_order literal the same way, so
        # this test doesn't need to import a private name.
        report_source = inspect.getsource(doctor.print_report)
        order_match = re.search(r"category_order = \[(.*?)\]", report_source, re.DOTALL)
        assert order_match is not None
        covered = set(re.findall(r'"([a-z_]+)"', order_match.group(1)))
        self.assertEqual(
            used - covered, set(), "categories missing from print_report's category_order"
        )


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
        self.assertIn("c64cast version", subjects)
        for dep, _ in doctor._HARD_DEPS:
            self.assertIn(dep, subjects)

    def test_version_line_is_first_and_explains_the_uninstalled_sentinel(self):
        # The version is the first thing a bug report needs, so it leads the
        # section. "0+unknown" is meaningless on its own — say why.
        with mock.patch.object(doctor, "_probe_uv_lock", return_value=[]):
            diags = doctor._probe_environment()
        self.assertEqual(diags[0].subject, "c64cast version")
        self.assertEqual(diags[0].level, "ok")

        with (
            mock.patch.object(c64cast, "__version__", "0+unknown"),
            mock.patch.object(doctor, "_probe_uv_lock", return_value=[]),
        ):
            diags = doctor._probe_environment()
        self.assertIn("source checkout", diags[0].message)

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

    def test_uv_lock_probe_is_skipped_outside_a_source_checkout(self):
        # For an installed package _REPO_ROOT is site-packages, which has no
        # pyproject.toml. `uv lock --check` exits nonzero there for "no project
        # found" exactly as it does for real drift, so running it told installed
        # users their lockfile had drifted from a file they don't have.
        with (
            mock.patch.object(doctor, "_running_from_checkout", return_value=False),
            mock.patch.object(doctor, "_probe_uv_lock") as probe,
        ):
            diags = doctor._probe_environment()
        probe.assert_not_called()
        self.assertNotIn("uv.lock", {d.subject for d in diags})

    def test_running_from_checkout_detects_the_repo(self):
        # The repo we're testing from is, by construction, a source checkout.
        self.assertTrue(doctor._running_from_checkout())
        with mock.patch.object(doctor, "_REPO_ROOT", Path("/definitely/not/a/checkout")):
            self.assertFalse(doctor._running_from_checkout())

    def test_environment_runs_in_validate_load_result(self):
        with mock.patch.object(doctor, "_probe_uv_lock", return_value=[]):
            diags = doctor.validate_load_result(_load(""), probe_u64=False)
        self.assertTrue(any(d.category == "environment" for d in diags))


class OfflineDacCurveCalibrationUncertaintyTest(unittest.TestCase):
    """_validate_dac_curve_resolution (offline — no live device identity)
    must not claim a confident 'no calibration applies' when a live run's
    key (unique_id / USB serial) could differ from the offline fallback key
    it's stuck with."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Redirect the data root at the env layer; calibration files resolve
        # under paths.calibration_dir() (= $C64CAST_DATA_DIR/calibration/dac).
        self._env = mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _write_calibration(self, filename: str, backend: str = "ultimate") -> None:
        cal_dir = dac_calibration_store.paths.calibration_dir()
        cal_dir.mkdir(parents=True, exist_ok=True)
        _write(
            str(cal_dir / filename),
            f"""
            {{"schema": 2, "backend": "{backend}", "sids": {{"default": {{"sidtable": {[0] * 256}}}}}}}
            """,
        )

    def _loaded(self, dac_curve: str, extra: str = "") -> cfgmod.LoadResult:
        return _load(f"""
            [ultimate64]
            url = "http://192.168.2.64"
            [audio]
            enabled = true
            dac_curve = "{dac_curve}"
            {extra}
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)

    def test_auto_no_files_anywhere_is_plain_ok(self):
        diags = doctor._validate_dac_curve_resolution(self._loaded("auto"))
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("resolves to 'mahoney_ultisid' on this system", diags[0].message)
        self.assertNotIn("cannot confirm", diags[0].message)

    def test_auto_unmatched_files_on_disk_flags_uncertainty(self):
        self._write_calibration("ultimate-SOMEOTHERUNIT.json")
        diags = doctor._validate_dac_curve_resolution(self._loaded("auto"))
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("1 calibration file(s) on disk", diags[0].message)
        self.assertIn("--skip-probe", diags[0].hint or "")

    def test_calibrated_no_files_anywhere_is_still_a_hard_error(self):
        diags = doctor._validate_dac_curve_resolution(self._loaded("calibrated"))
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "error")

    def test_calibrated_unmatched_files_on_disk_downgrades_to_warn(self):
        self._write_calibration("ultimate-SOMEOTHERUNIT.json")
        diags = doctor._validate_dac_curve_resolution(self._loaded("calibrated"))
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "warn")
        self.assertIn("cannot confirm", diags[0].message)

    def test_profile_override_stays_authoritative_despite_stray_files(self):
        self._write_calibration("ultimate-SOMEOTHERUNIT.json")
        diags = doctor._validate_dac_curve_resolution(
            self._loaded("calibrated", extra='dac_calibration_profile = "my-rig"')
        )
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "error")

    def test_live_connectivity_suppresses_the_duplicate_offline_line(self):
        """A live doctor run (probe_u64=True, connectivity reachable) must
        report dac_curve resolution ONCE — the precise live answer from
        _probe_dac_calibration_status — not also the offline guess from
        _validate_dac_curve_resolution, which end-to-end reproduces the bug
        report: `--doctor` without --skip-probe still showed the offline
        'resolves to mahoney_ultisid' AUDIO-section line even though the
        CONNECTIVITY section already had the precise live answer."""
        loaded = self._loaded("auto")

        fake_response = mock.MagicMock()
        fake_response.json.return_value = {"errors": []}
        fake_response.raise_for_status = mock.MagicMock()
        with _fake_ultimate_api(base_url="http://192.168.2.64") as api_instance:
            api_instance.session.get.return_value = fake_response
            diags = doctor.validate_load_result(loaded, probe_u64=True)

        offline_audio_line = [
            d for d in diags if d.category == "audio" and d.subject == "system/dac_curve"
        ]
        live_line = [d for d in diags if d.subject == "system (DAC calibration)"]
        self.assertEqual(offline_audio_line, [])
        self.assertEqual(len(live_line), 1)


class DacCalibrationStatusProbeTest(unittest.TestCase):
    """_probe_dac_calibration_status is the LIVE counterpart to the offline
    _validate_dac_curve_resolution check: it can read which SID socket is
    actually mapped to $D400 right now, so it's precise where the offline
    check is only approximate — and, per validate_load_result, it wins over
    the offline check whenever both would otherwise report on the same
    system."""

    def _cfg(self, dac_curve: str = "auto") -> cfgmod.Config:
        loaded = _load(f"""
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = true
            dac_curve = "{dac_curve}"
            [[scenes]]
            type = "webcam"
            display = "petscii"
        """)
        return loaded.cfgs[0]

    def test_not_wanted_when_audio_disabled(self):
        cfg = self._cfg()
        cfg.audio.enabled = False
        self.assertEqual(doctor._probe_dac_calibration_status("sys", cfg, FakeAPI()), [])

    def test_not_wanted_for_explicit_linear(self):
        cfg = self._cfg("linear")
        self.assertEqual(doctor._probe_dac_calibration_status("sys", cfg, FakeAPI()), [])

    def test_auto_with_no_calibration_is_ok(self):
        cfg = self._cfg("auto")
        api = FakeAPI()
        api.profile = HardwareProfile(
            name="Fake U64", family="fake", supports_config=True, supports_sid_config=True
        )
        diags = doctor._probe_dac_calibration_status("sys", cfg, api)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("mahoney_ultisid", diags[0].message)

    def test_calibrated_missing_is_error_with_hint(self):
        cfg = self._cfg("calibrated")
        api = FakeAPI()
        api.profile = HardwareProfile(
            name="Fake U64", family="fake", supports_config=True, supports_sid_config=True
        )
        diags = doctor._probe_dac_calibration_status("sys", cfg, api)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "error")
        self.assertIsNotNone(diags[0].hint)
        self.assertIn("--calibrate-dac", diags[0].hint or "")


class MachineSettingsProbeTest(unittest.TestCase):
    """_probe_machine_settings — the ENVIRONMENT-section report on the
    machine-settings file (absent / present+sections / parse error / rejected
    sections). $C64CAST_SETTINGS points at a tmp path so the real file is never
    read."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = os.path.join(self._tmp.name, "settings.toml")

    def _probe(self):
        with mock.patch.dict(os.environ, {"C64CAST_SETTINGS": self._path}):
            return doctor._probe_machine_settings()

    def _write(self, content: str) -> None:
        with open(self._path, "w") as f:
            f.write(content)

    def test_absent_is_ok(self):
        diags = self._probe()
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("none", diags[0].message)

    def test_present_reports_sections(self):
        self._write('[ultimate64]\nurl = "http://m.lan"\n[video]\ndevice = 1\n')
        diags = self._probe()
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("ultimate64", diags[0].message)
        self.assertIn("video", diags[0].message)

    def test_parse_error_is_error(self):
        self._write("[ultimate64]\nurl = \n")
        diags = self._probe()
        self.assertTrue(any(d.level == "error" for d in diags))

    def test_rejected_section_warns(self):
        self._write('[ultimate64]\nurl = "http://m.lan"\n[[scenes]]\ntype = "blank"\n')
        diags = self._probe()
        self.assertTrue(any(d.level == "warn" and "scenes" in d.message for d in diags))


class DataDirsProbeTest(unittest.TestCase):
    """_probe_data_dirs — reports the resolved data root + controllers dir, and
    nothing else. There is no legacy-repo migration nudge any more: DAC
    calibration is surfaced at curve resolution (see MissingCalibrationLogTest
    in test_dac_calibration) and orphaned presets at preset-store load (see
    LegacyPresetsWarnTest in test_transport)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_reports_data_root(self):
        data = os.path.join(self._tmp.name, "data")
        with mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": data}):
            diags = doctor._probe_data_dirs()
        self.assertEqual(len(diags), 2)
        self.assertTrue(all(d.level == "ok" for d in diags))
        subjects = {d.subject for d in diags}
        self.assertEqual(subjects, {"data dir", "controllers dir"})
        self.assertTrue(all(data in d.message for d in diags))

    def test_never_warns_about_legacy_repo_files(self):
        # Even with stale calibration AND preset files at the legacy repo
        # location, the doctor probe stays silent (both are surfaced at use
        # time now, not here).
        legacy = os.path.join(self._tmp.name, "repo")
        data = os.path.join(self._tmp.name, "data")
        for sub in (("calibration", "dac"), ("presets",)):
            d = os.path.join(legacy, *sub)
            os.makedirs(d)
            with open(os.path.join(d, "x.json"), "w") as f:
                f.write("{}")
        with mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": data}):
            with mock.patch("c64cast.app.paths.legacy_data_root", return_value=Path(legacy)):
                diags = doctor._probe_data_dirs()
        self.assertEqual([d for d in diags if d.level == "warn"], [])


class EnsembleRecordingPathTest(unittest.TestCase):
    """`resolve_recording_path` only disambiguates systems that left `path`
    alone, so two spelled-out identical paths still collide — and a
    cv2.VideoWriter losing that race reports nothing."""

    def _master(self, tmp: str, members: dict[str, str]) -> str:
        master_path = os.path.join(tmp, "master.toml")
        entries = ",\n    ".join(f'{{ name = "{n}", config = "{n}.toml" }}' for n in members)
        _write(master_path, f"[ensemble]\nsystems = [\n    {entries}\n]\n")
        for name, body in members.items():
            _write(os.path.join(tmp, f"{name}.toml"), body)
        return master_path

    def _diags(self, members: dict[str, str]) -> list[doctor.Diagnostic]:
        with tempfile.TemporaryDirectory() as tmp:
            loaded = cfgmod.load_master(self._master(tmp, members))
        return doctor._validate_ensemble_recording_paths(loaded)

    def test_shared_explicit_path_is_an_error(self):
        body = '[recording]\nenabled = true\npath = "wall.mp4"\n'
        diags = self._diags({"left": body, "right": body})
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "error")
        self.assertIn("left", diags[0].message)
        self.assertIn("right", diags[0].message)

    def test_derived_paths_do_not_collide(self):
        body = "[recording]\nenabled = true\n"
        self.assertEqual(self._diags({"left": body, "right": body}), [])

    def test_disabled_systems_are_not_counted(self):
        # Only one of the two actually opens the file, so it is not a clash.
        diags = self._diags(
            {
                "left": '[recording]\nenabled = true\npath = "wall.mp4"\n',
                "right": '[recording]\nenabled = false\npath = "wall.mp4"\n',
            }
        )
        self.assertEqual(diags, [])

    def test_paths_are_compared_after_expansion(self):
        # "~/wall.mp4" and the spelled-out home path name one file;
        # comparing the raw strings would call them distinct. Single-quoted
        # TOML so a Windows home path's backslashes stay literal.
        home = os.path.join(os.path.expanduser("~"), "wall.mp4")
        diags = self._diags(
            {
                "left": "[recording]\nenabled = true\npath = '~/wall.mp4'\n",
                "right": f"[recording]\nenabled = true\npath = '{home}'\n",
            }
        )
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "error")

    def test_relative_and_absolute_spellings_collide(self):
        # A bare name is opened relative to the cwd, so it is the same file
        # as the absolute path — the check has to say so.
        rel = "wall.mp4"
        absolute = os.path.join(os.getcwd(), rel)
        diags = self._diags(
            {
                "left": f"[recording]\nenabled = true\npath = '{rel}'\n",
                "right": f"[recording]\nenabled = true\npath = '{absolute}'\n",
            }
        )
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "error")


class OpenCVProviderProbeTest(unittest.TestCase):
    """Several opencv wheels unpack to one `cv2/` directory, so a second
    install silently replaces the pinned one and no metadata records it."""

    def test_two_providers_warn(self):
        with mock.patch(
            "importlib.metadata.packages_distributions",
            return_value={"cv2": ["opencv-python", "opencv-contrib-python"]},
        ):
            diags = doctor._probe_opencv_provider()
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "warn")
        self.assertIn("opencv-contrib-python", diags[0].message)

    def test_single_provider_is_ok(self):
        with mock.patch(
            "importlib.metadata.packages_distributions",
            return_value={"cv2": ["opencv-python"]},
        ):
            diags = doctor._probe_opencv_provider()
        self.assertEqual([d.level for d in diags], ["ok"])

    def test_no_cv2_provider_says_nothing(self):
        # The hard-dependency probe already reports an unimportable cv2;
        # a second voice saying so would just be noise.
        with mock.patch("importlib.metadata.packages_distributions", return_value={}):
            self.assertEqual(doctor._probe_opencv_provider(), [])

    def test_unreadable_metadata_is_not_fatal(self):
        with mock.patch(
            "importlib.metadata.packages_distributions",
            side_effect=OSError("no metadata"),
        ):
            self.assertEqual(doctor._probe_opencv_provider(), [])


class UnknownKeyDiagnosticTest(unittest.TestCase):
    """A stray TOML key has to land in the report body. It used to be a log
    line printed above it, which reads as preamble next to the formatted rows
    — the run then continues on defaults and the setting silently does
    nothing."""

    def test_unknown_key_becomes_a_config_diagnostic(self):
        loaded = _load('[color]\npalette_mode = "grayscale"\n')
        diags = doctor._validate_unknown_keys(loaded)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "warn")
        self.assertEqual(diags[0].category, "config")
        self.assertIn("palette_mode", diags[0].message)
        self.assertIn("[[scenes]]", diags[0].hint or "")

    def test_clean_config_reports_nothing(self):
        loaded = _load('[color]\ndither = "ordered"\n')
        self.assertEqual(doctor._validate_unknown_keys(loaded), [])

    def test_config_rows_render_and_count_as_warnings(self):
        # The point of the change: it shows up under a CONFIG heading and in
        # the summary tally, not as a line above the report.
        buf = io.StringIO()
        code = doctor.print_report(
            doctor._validate_unknown_keys(_load('[color]\npalette_mode = "x"\n')), file=buf
        )
        out = buf.getvalue()
        self.assertEqual(code, 0)  # warn-level: an ignored key still runs
        self.assertIn("CONFIG", out)
        self.assertIn("1 warn", out)

    def test_validate_load_result_includes_config_rows(self):
        loaded = _load('[color]\npalette_mode = "grayscale"\n')
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        self.assertTrue(any(d.category == "config" for d in diags))


@contextlib.contextmanager
def _loaded_config_file(body: str) -> Iterator[tuple[cfgmod.LoadResult, str]]:
    """A loaded single-system config whose file is still on disk, plus its
    directory. `_load` deletes the tempdir before returning, which is fine for
    every check that reads the parsed Config — but the `#:schema` check reads
    line 1 back off the file, so it needs the file to outlive the load."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "single.toml")
        _write(path, body)
        yield cfgmod.load_master(path), tmp


class SchemaDirectiveDiagnosticTest(unittest.TestCase):
    """The `#:schema` first line is the one thing an upgrade can leave behind:
    nothing reads it at run time, so a config pinned to the version its author
    installed keeps quietly judging itself by that release's schema."""

    def _rows(self, body: str) -> list[doctor.Diagnostic]:
        with _loaded_config_file(body) as (loaded, _):
            return doctor._validate_schema_directive(loaded)

    def test_no_directive_is_not_a_finding(self):
        # The line is optional. A row for every config lacking one would be an
        # advertisement in the middle of a diagnostic report.
        self.assertEqual(self._rows('[color]\ndither = "ordered"\n'), [])

    def test_a_pin_to_another_version_is_flagged_with_the_line_to_paste(self):
        url = ser._published_schema_url("0.1.0")
        with mock.patch.object(c64cast, "__version__", "9.9.9"):
            rows = self._rows(f"#:schema {url}\n")
        self.assertEqual([r.level for r in rows], ["warn"])
        self.assertEqual(rows[0].category, "config")
        self.assertIn("0.1.0", rows[0].message)
        self.assertIn("9.9.9", rows[0].message)
        self.assertIn("#:schema ", rows[0].hint or "")
        self.assertIn("--print-schema-path", rows[0].hint or "")

    def test_a_pin_to_this_version_is_fine(self):
        with mock.patch.object(c64cast, "__version__", "9.9.9"):
            rows = self._rows(f"#:schema {ser._published_schema_url('9.9.9')}\n")
        self.assertEqual([r.level for r in rows], ["ok"])

    def test_the_installed_schema_tracks_this_install(self):
        # What --init writes, and what --print-schema-path prints: the answer
        # that needs no maintenance, because an upgrade rewrites that file.
        with _loaded_config_file("") as (loaded, tmp):
            path = os.path.join(tmp, "single.toml")
            _write(path, f"#:schema {ser.schema_directive_for(path)}\n")
            rows = doctor._validate_schema_directive(loaded)
        self.assertEqual([r.level for r in rows], ["ok"])
        self.assertIn("tracks this install", rows[0].message)

    def test_a_schema_that_is_gone_is_flagged(self):
        # An install that moved, or an upgrade onto a new Python version: the
        # site-packages path in the line no longer exists.
        rows = self._rows("#:schema ./gone/c64cast.schema.json\n")
        self.assertEqual([r.level for r in rows], ["warn"])
        self.assertIn("isn't there", rows[0].message)

    def test_a_stale_copy_of_our_schema_is_flagged_by_content(self):
        # A leftover venv still on disk answers the path but describes a
        # different c64cast — which is exactly the case a path check misses.
        with _loaded_config_file("#:schema ./c64cast.schema.json\n") as (loaded, tmp):
            _write(os.path.join(tmp, "c64cast.schema.json"), '{"title": "an older c64cast"}')
            rows = doctor._validate_schema_directive(loaded)
        self.assertEqual([r.level for r in rows], ["warn"])
        self.assertIn("isn't the one this install generates", rows[0].message)

    def test_an_identical_copy_elsewhere_is_fine(self):
        # Judged by content, not by location: a vendored copy that matches is
        # doing its job, and nagging about it would train people to ignore this.
        with _loaded_config_file("#:schema ./c64cast.schema.json\n") as (loaded, tmp):
            body = paths.packaged_schema_path().read_text(encoding="utf-8")
            _write(os.path.join(tmp, "c64cast.schema.json"), body)
            rows = doctor._validate_schema_directive(loaded)
        self.assertEqual([r.level for r in rows], ["ok"])

    def test_a_hand_picked_schema_is_left_alone(self):
        # `./house-style.schema.json` is a deliberate choice, not a stale
        # pointer at ours — the filename is how the two are told apart.
        self.assertEqual(self._rows("#:schema ./house-style.schema.json\n"), [])

    def test_somebody_elses_url_is_left_alone(self):
        rows = self._rows(
            "#:schema https://example.invalid/schemas/c64cast.schema.json\n",
        )
        self.assertEqual(rows, [])

    def test_validate_load_result_runs_the_check(self):
        url = ser._published_schema_url("0.1.0")
        with (
            mock.patch.object(c64cast, "__version__", "9.9.9"),
            _loaded_config_file(f"#:schema {url}\n") as (loaded, _),
        ):
            diags = doctor.validate_load_result(loaded, probe_u64=False)
        self.assertTrue(any("#:schema" in d.message for d in diags))


class PrintReportCategoryTest(unittest.TestCase):
    def test_unlisted_category_still_prints(self):
        # The renderer used to iterate a fixed category list, so a probe
        # with a new category returned findings that never reached the user.
        buf = io.StringIO()
        code = doctor.print_report(
            [doctor.Diagnostic("error", "brand_new_category", "subj", "boom")], file=buf
        )
        self.assertEqual(code, 1)
        self.assertIn("BRAND_NEW_CATEGORY", buf.getvalue())
        self.assertIn("boom", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
