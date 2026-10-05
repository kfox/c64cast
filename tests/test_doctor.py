"""Tests for c64cast.app.doctor — collect-all config validation surface."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import tempfile
import textwrap
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from _fakes import FakeAPI, MachineSettingsIsolation, tmp_cwd

import c64cast
from c64cast.app import config as cfgmod
from c64cast.app import config_serialize as ser
from c64cast.app import doctor, paths, scene_factory
from c64cast.audio import dac_calibration_store
from c64cast.hw.backend import HardwareProfile
from c64cast.hw.c64 import max_safe_sample_rate

# The doctor loads configs, and loading reads the machine-settings file, so point
# $C64CAST_SETTINGS at a missing path for the module. Tests that want a machine
# layer write their own file and re-patch over this.
_iso = MachineSettingsIsolation()


def setUpModule():
    _iso.start()


def tearDownModule():
    _iso.stop()


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
    from c64cast.hw.c64 import U64_API

    with mock.patch.object(Ultimate64API, "__init__", return_value=None):
        api_instance = Ultimate64API.__new__(Ultimate64API)
        api_instance.base_url = base_url
        api_instance.session = mock.MagicMock()
        api_instance.probe = mock.MagicMock(return_value="HTTP 200")
        api_instance.close = mock.MagicMock()
        # The real __init__ always sets a profile; refine_capabilities (run
        # by _probe_one_system after a successful probe) reads it.
        api_instance.profile = ULTIMATE_PROFILE
        # Old firmware by default, so no route probe reaches `session`.
        api_instance._route_answers = {U64_API.MENU_SCREEN: "absent", U64_API.INPUT: "absent"}
        with mock.patch("c64cast.hw.api.Ultimate64API", return_value=api_instance):
            yield api_instance


def _load(toml: str, suffix: str = ".toml") -> cfgmod.LoadResult:
    """Helper: write a single-system TOML to a tempfile, load via
    load_master, return the LoadResult."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "single" + suffix)
        _write(path, toml)
        return cfgmod.load_master(path)


class RunDoctorMergedLoadResultTest(unittest.TestCase):
    """cli_commands.run_doctor rebuilds a LoadResult with the CLI-merged
    per-system configs — it must carry every field load_master produced
    forward, not just the ones it happens to list by hand (a hand-listed copy
    silently dropped `master_web` until this was fixed to `dataclasses.replace`)."""

    def test_master_web_survives_the_merge(self):
        from c64cast.app.cli_commands import run_doctor

        cfg = cfgmod.Config()
        cfg.debug.skip_probe = True
        web = cfgmod.WebCfg()
        web.token = "distinctive-token"
        loaded = cfgmod.LoadResult(
            cfgs=[cfg],
            names=["system"],
            paths=[None],
            is_ensemble=True,
            master_control=cfg.control,
            master_midi_control=cfg.midi_control,
            master_web=web,
        )
        captured: dict[str, cfgmod.LoadResult] = {}

        def fake_validate(merged, **kwargs):
            captured["merged"] = merged
            return []

        with mock.patch("c64cast.app.doctor.validate_load_result", side_effect=fake_validate):
            with contextlib.redirect_stdout(io.StringIO()):
                run_doctor(loaded, [cfg])
        self.assertIs(captured["merged"].master_web, web)


class ValidateScenesTest(unittest.TestCase):
    """Per-scene validation — every misconfig surfaces as its own
    Diagnostic instead of aborting at the first error."""

    def _pumped_blank_big_text_levels(self, *, is_ensemble: bool) -> list[str]:
        loaded = _load("""
            [audio]
            enabled = true
            use_reu_pump = true

            [[scenes]]
            type = "blank"
            name = "title"
            [[scenes.overlays]]
            type = "big_text"
            messages = [{ text = "HI" }]
        """)
        loaded = dataclasses.replace(loaded, is_ensemble=is_ensemble)
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        return [d.level for d in diags if d.subject == "system/title"]

    def test_an_ensemble_blank_big_text_scene_is_ok_with_the_pump_on(self):
        # Ensemble live scenes run silent, so they never start the pump (#559).
        self.assertEqual(self._pumped_blank_big_text_levels(is_ensemble=True), ["ok"])

    def test_a_single_system_blank_big_text_scene_is_an_error_with_the_pump_on(self):
        self.assertIn("error", self._pumped_blank_big_text_levels(is_ensemble=False))

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
        # `right` has a conductor 'morning-hello'; `left` has no scene by that name.
        # Both use big_text-shaped scenes, so nothing but the missing follower warns.
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
        # accumulate. Every extra is installed in the dev env, so force one missing.
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

    def test_rest_password_refusal_is_a_password_error(self):
        from c64cast.hw.api import RestAuthError

        loaded = _load('[ultimate64]\nurl = "http://fake"\n')
        with _fake_ultimate_api() as api_instance:
            api_instance.probe.side_effect = RestAuthError("REST API refused c64cast")
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        conn = [d for d in diags if d.category == "connectivity"]
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0].level, "error")
        self.assertIn("REST API refused c64cast", conn[0].message)
        assert conn[0].hint is not None
        self.assertIn("C64CAST_DMA_PASSWORD", conn[0].hint)
        api_instance.close.assert_called_once()

    def test_unsendable_password_is_a_connectivity_error(self):
        loaded = _load('[ultimate64]\nurl = "http://fake"\ndma_password = "pw\\n"\n')
        with mock.patch("c64cast.hw.socket_dma.SocketDMAClient.connect") as connect:
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        connect.assert_not_called()
        conn = [d for d in diags if d.category == "connectivity"]
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0].level, "error")
        self.assertIn("X-Password", conn[0].message)

    def test_an_unbuildable_backend_is_a_connectivity_error(self):
        from c64cast.hw.backend import BackendSetupError

        loaded = _load('[ultimate64]\nurl = "http://fake"\n')
        with mock.patch(
            "c64cast.hw.backend.make_backend", side_effect=BackendSetupError("no serial port")
        ):
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        conn = [d for d in diags if d.category == "connectivity"]
        self.assertEqual(
            [(d.level, d.message) for d in conn],
            [("error", "cannot connect to http://fake: no serial port")],
        )

    def test_an_unrelated_value_error_is_not_reported_as_a_connect_failure(self):
        loaded = _load('[ultimate64]\nurl = "http://fake"\n')
        with (
            mock.patch("c64cast.hw.backend.make_backend", side_effect=ValueError("a defect")),
            self.assertRaisesRegex(ValueError, "a defect"),
        ):
            doctor.validate_load_result(loaded, probe_u64=True)

    def test_unsendable_password_is_an_offline_error_too(self):
        loaded = _load('[ultimate64]\nurl = "http://fake"\ndma_password = "pw "\n')
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        conn = [d for d in diags if d.category == "connectivity"]
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0].level, "error")
        self.assertIn("X-Password", conn[0].message)
        self.assertNotIn("pw ", conn[0].message.replace("password", ""))

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


class MenuOpenProbeTest(unittest.TestCase):
    """The menu-open warning: a warn row when firmware 3.15 says the menu is
    open, and nothing at all when it is closed or the firmware has no route."""

    def _connectivity(self, route: str, screen: object) -> list:
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
        """)
        with _fake_ultimate_api() as api_instance:
            from c64cast.hw.c64 import U64_API

            api_instance._route_answers = {U64_API.MENU_SCREEN: route, U64_API.INPUT: "absent"}
            api_instance.read_menu_screen = mock.MagicMock(return_value=screen)
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        return [d for d in diags if d.subject.endswith("(menu)")]

    def test_open_menu_is_a_warning(self):
        from c64cast.hw.menu_screen import decode_menu_screen

        screen = decode_menu_screen(b" " * 2000)
        (row,) = self._connectivity("present", screen)
        self.assertEqual(row.level, "warn")
        self.assertIn("menu is open", row.message)

    def test_closed_menu_says_nothing(self):
        self.assertEqual(self._connectivity("present", None), [])

    def test_firmware_without_the_route_says_nothing(self):
        self.assertEqual(self._connectivity("absent", object()), [])


class DeviceIdentityProbeTest(unittest.TestCase):
    """Doctor names the unit and its firmware build, so a pasted report says
    which machine and which build it came from."""

    def _identity(self, info_response: Any) -> list:
        loaded = _load("""
            [ultimate64]
            url = "http://fake"
        """)
        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.side_effect = info_response
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        return [d for d in diags if d.subject == "system (device)"]

    def test_identity_line_carries_the_build_hash(self):
        def get(url, **_kwargs):
            r = mock.MagicMock()
            r.json.return_value = (
                {
                    "product": "Ultimate 64-II",
                    "firmware_version": "3.15a",
                    "git_commit_hash": "dddd29b2",
                    "fpga_version": "125",
                    "core_version": "1.50",
                    "unique_id": "B95B01",
                    "wifi_mac": "48:CA:43:5A:73:78",
                }
                if url.endswith("/v1/info")
                else {}
            )
            return r

        ident = self._identity(get)
        self.assertEqual(len(ident), 1)
        self.assertEqual(ident[0].level, "ok")
        self.assertEqual(
            ident[0].message,
            "Ultimate 64-II B95B01 (firmware 3.15a build dddd29b2, FPGA 125, core 1.50)",
        )

    def test_unanswered_info_is_reported_not_fatal(self):
        import requests

        ident = self._identity(requests.ConnectionError("down"))
        self.assertEqual(len(ident), 1)
        self.assertEqual(ident[0].level, "ok")
        self.assertIn("not reported", ident[0].message)


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
        self.assertIn("Disabled", reu[0].message)
        self.assertIn("silently", reu[0].message)
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


def _config_rest(
    sections: dict[str, dict[str, str]],
    *,
    absent_answer: str = "200",
    listed: list[str] | None = None,
) -> Any:
    """A `session.get` side effect serving the Ultimate config API from
    `sections`: `GET /v1/configs` lists `listed` (default: the section names),
    and a GET for a category not in `sections` answers the way the firmware
    does — `"200"` (before 3.15, C64 Ultimate 1.1.0) with only the errors
    array, `"404"` (3.15 on) with a JSON error naming the category. Any other
    URL answers an empty 200."""
    from urllib.parse import unquote

    import requests

    def _get(url, timeout=3.0, **_kwargs):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.raise_for_status = mock.MagicMock()
        if url.endswith("/v1/configs"):
            resp.json.return_value = {
                "categories": list(sections) if listed is None else listed,
                "errors": [],
            }
            return resp
        if "/v1/configs/" not in url:
            resp.json.return_value = {}
            return resp
        cat = unquote(url.split("/v1/configs/")[-1])
        body: dict[str, object] = {"errors": []}
        if cat in sections:
            body[cat] = sections[cat]
        elif absent_answer == "404":
            resp.status_code = 404
            body["errors"] = [f"No configuration category matches '{cat}'."]
            resp.raise_for_status.side_effect = requests.HTTPError(
                f"404 Client Error: Not Found for url: {url}"
            )
        resp.json.return_value = body
        return resp

    return _get


class SidStatusProbeTest(unittest.TestCase):
    """Emulated-SID enable check fires only when the config drives the SID
    (audio streaming, or a waveform/midi scene). Catches the U2+ case where
    the emulated SID ships disabled and every tune is silent while video +
    the host-emulated oscilloscope keep working."""

    def _patch_connectivity_to_sid_status(self, loaded, left: str, right: str):
        """Drive _probe_connectivity end-to-end with mocks against a U2+
        whose "SID Left"/"SID Right" read `left`/`right`. Wire shape matches
        Ultimate firmware 3.x."""
        sections = {
            "Audio Output Settings": {
                "SID Left": left,
                "SID Left Base": "Snoop $D400",
                "SID Right": right,
            },
        }
        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.side_effect = _config_rest(sections)
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
        # From an empty cwd: the slideshow has no `file`, so validation would
        # otherwise list the developer's own assets/pictures/.
        with tmp_cwd():
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
        # The category list answers (a U2+), then every category read times out.
        serve = _config_rest({"Audio Output Settings": {}})

        def _get(url, timeout=3.0, **kwargs):
            if url.endswith("/v1/configs"):
                return serve(url, timeout, **kwargs)
            raise requests.Timeout("read timeout")

        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.side_effect = _get
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
        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.side_effect = _config_rest({"Audio Output Settings": {}})
            diags = doctor.validate_load_result(loaded, probe_u64=True)
        sid = [d for d in diags if d.subject.endswith("(SID)")]
        self.assertEqual(sid, [])


class SidStatusOnUltimate64Test(unittest.TestCase):
    """An Ultimate 64 registers no "Audio Output Settings" category, so the
    emulated-SID enable check has nothing to say there — on either firmware
    answer for an absent category (#519: 3.15 answers 404 and the probe
    warned "REST query for SID status failed" on every U64 with audio)."""

    _AUDIO_ON = """
        [ultimate64]
        url = "http://fake"
        [audio]
        enabled = true
        [[scenes]]
        type = "webcam"
        display = "petscii"
    """

    def _sid_diags(self, sections, **rest_kwargs):
        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.side_effect = _config_rest(sections, **rest_kwargs)
            diags = doctor.validate_load_result(_load(self._AUDIO_ON), probe_u64=True)
        return [d for d in diags if d.subject.endswith("(SID)")]

    def test_u64_on_firmware_315_is_quiet(self):
        self.assertEqual(self._sid_diags({"Audio Mixer": {}}, absent_answer="404"), [])

    def test_u64_on_earlier_firmware_is_quiet(self):
        self.assertEqual(self._sid_diags({"Audio Mixer": {}}, absent_answer="200"), [])

    def test_skipped_without_the_emusid_capability(self):
        # The category answers "both SIDs disabled", but the device's category
        # list does not carry it: the capability gate alone keeps the probe off.
        sections = {"Audio Output Settings": {"SID Left": "Disabled", "SID Right": "Disabled"}}
        self.assertEqual(self._sid_diags(sections, listed=["Audio Mixer"]), [])

    def test_absent_category_reads_as_absent_not_failed(self):
        # The capability says yes but the GET says no such category: the 404
        # is the "absent" answer, which stays quiet, not a REST failure.
        self.assertEqual(
            self._sid_diags({}, absent_answer="404", listed=["Audio Output Settings"]), []
        )


class MasterVolumeProbeTest(unittest.TestCase):
    """Firmware 3.15's Vol Master scales every source, so at OFF the machine
    is silent whatever the per-source rows say. The probe names it when the
    run wants audio, and has nothing to say where the item does not exist
    (3.14e, C64 Ultimate 1.1.0)."""

    _VIDEO = """
        [ultimate64]
        url = "http://fake"
        [audio]
        enabled = true
        [[scenes]]
        type = "video"
        file = "x.mp4"
    """

    def _diags(self, sections, toml=_VIDEO, **rest_kwargs):
        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.side_effect = _config_rest(sections, **rest_kwargs)
            return doctor.validate_load_result(_load(toml), probe_u64=True)

    @staticmethod
    def _u64(master=None, sampler="Enabled"):
        mixer = {"Vol Sampler L": " 0 dB", "Vol Sampler R": " 0 dB"}
        if master is not None:
            mixer["Vol Master"] = master
        return {
            "Audio Mixer": mixer,
            "C64 and Cartridge Settings": {"Map Ultimate Audio $DF20-DFFF": sampler},
        }

    @staticmethod
    def _of(diags, suffix):
        return [d for d in diags if d.subject.endswith(suffix)]

    def test_absent_master_is_quiet_on_either_firmware_answer(self):
        for answer in ("200", "404"):
            with self.subTest(absent_answer=answer):
                diags = self._diags(self._u64(), absent_answer=answer)
                self.assertEqual(self._of(diags, "(master volume)"), [])
                sampler = self._of(diags, "(Ultimate Audio sampler)")
                self.assertIn("mapped + audible", sampler[0].message)

    def test_master_at_unity_is_ok(self):
        master = self._of(self._diags(self._u64(" 0 dB")), "(master volume)")
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0].level, "ok")
        self.assertIn("Vol Master at 0 dB", master[0].message)

    def test_master_off_is_named_and_will_be_raised(self):
        diags = self._diags(self._u64("OFF"))
        master = self._of(diags, "(master volume)")
        self.assertEqual(len(master), 1)
        self.assertIn("Vol Master is OFF", master[0].message)
        self.assertIn("raised to 0 dB", master[0].message)
        assert master[0].hint is not None
        self.assertIn("Audio Mixer", master[0].hint)
        sampler = self._of(diags, "(Ultimate Audio sampler)")
        self.assertIn("Vol Master OFF", sampler[0].message)

    def test_u2plus_master_off_names_its_category(self):
        sections = {"Audio Output Settings": {"Vol Master": "OFF"}}
        master = self._of(self._diags(sections, absent_answer="404"), "(master volume)")
        self.assertEqual(len(master), 1)
        assert master[0].hint is not None
        self.assertIn("Audio Output Settings", master[0].hint)

    def test_not_probed_when_the_run_makes_no_sound(self):
        toml = """
            [ultimate64]
            url = "http://fake"
            [audio]
            enabled = false
            [[scenes]]
            type = "blank"
        """
        diags = self._diags(self._u64("OFF"), toml=toml)
        self.assertEqual(self._of(diags, "(master volume)"), [])

    def test_unreadable_mixer_warns(self):
        import requests

        serve = _config_rest(self._u64("OFF"))

        def _get(url, timeout=3.0, **kwargs):
            if "/v1/configs/Audio" in url:
                raise requests.Timeout("read timeout")
            return serve(url, timeout, **kwargs)

        with _fake_ultimate_api() as api_instance:
            api_instance.session.get.side_effect = _get
            diags = doctor.validate_load_result(_load(self._VIDEO), probe_u64=True)
        master = self._of(diags, "(master volume)")
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0].level, "warn")
        self.assertIn("Vol Master", master[0].message)


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
        # Regression: category_order omitted "midi_control", so such a Diagnostic
        # was dropped from the printed report while still counting in the totals.
        diags = [doctor.Diagnostic("ok", "midi_control", "midi_control", "11 entries")]
        buf = io.StringIO()
        doctor.print_report(diags, file=buf)
        self.assertIn("MIDI_CONTROL", buf.getvalue())
        self.assertIn("11 entries", buf.getvalue())

    def test_every_category_used_in_source_is_in_category_order(self):
        # Every category="..." literal doctor.py constructs a Diagnostic with must
        # be in print_report's category_order, or it vanishes from the report.
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
        # "bare python resolved somewhere else" trap. Only meaningful when the
        # project .venv exists to compare against (it does in dev/CI).
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
        # found" exactly as it does for real drift.
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


class UpdateProbeTest(unittest.TestCase):
    """_probe_updates: the PyPI update check --doctor folds into the
    ENVIRONMENT section, gated by validate_load_result's `probe_updates`
    switch so a caller that only wants offline checks never triggers it."""

    def test_probe_updates_defaults_to_off(self):
        # probe_updates defaults to False: a caller (like config_store's
        # pre-flight) that doesn't ask for it must not make a network call.
        with mock.patch.object(doctor, "_probe_updates") as probe:
            doctor.validate_load_result(_load(""), probe_u64=False, probe_environment=False)
        probe.assert_not_called()

    def test_probe_updates_runs_when_requested(self):
        with mock.patch.object(doctor, "_probe_updates", return_value=[]) as probe:
            doctor.validate_load_result(
                _load(""), probe_u64=False, probe_environment=False, probe_updates=True
            )
        probe.assert_called_once()

    def test_skipped_in_a_source_checkout(self):
        with mock.patch.object(doctor, "_running_from_checkout", return_value=True):
            diags = doctor._probe_updates()
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "ok")
        self.assertEqual(diags[0].category, "environment")
        self.assertIn("source checkout", diags[0].message)

    def test_skipped_when_pypi_is_unreachable(self):
        with (
            mock.patch.object(doctor, "_running_from_checkout", return_value=False),
            mock.patch("c64cast.app.upgrade.latest_release", return_value=None),
        ):
            diags = doctor._probe_updates()
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("could not reach PyPI", diags[0].message)

    def test_warns_with_an_upgrade_hint_when_a_newer_release_exists(self):
        with (
            mock.patch.object(doctor, "_running_from_checkout", return_value=False),
            mock.patch("c64cast.app.upgrade.latest_release", return_value="99.0.0"),
            mock.patch("c64cast.app.upgrade.is_newer", return_value=True),
        ):
            diags = doctor._probe_updates()
        self.assertEqual(diags[0].level, "warn")
        self.assertIn("99.0.0", diags[0].message)
        self.assertEqual(diags[0].hint, "c64cast --upgrade")

    def test_ok_when_already_current(self):
        with (
            mock.patch.object(doctor, "_running_from_checkout", return_value=False),
            mock.patch("c64cast.app.upgrade.latest_release", return_value="0.3.0"),
            mock.patch("c64cast.app.upgrade.is_newer", return_value=False),
        ):
            diags = doctor._probe_updates()
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("up to date", diags[0].message)

    def test_ok_when_versions_could_not_be_compared(self):
        with (
            mock.patch.object(doctor, "_running_from_checkout", return_value=False),
            mock.patch("c64cast.app.upgrade.latest_release", return_value="bogus"),
            mock.patch("c64cast.app.upgrade.is_newer", return_value=None),
        ):
            diags = doctor._probe_updates()
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("could not compare", diags[0].message)

    def test_never_errors_on_a_network_failure(self):
        # A flaky network says nothing about whether the install is broken —
        # this probe must never reach "error", only "ok" or "warn".
        with (
            mock.patch.object(doctor, "_running_from_checkout", return_value=False),
            mock.patch("c64cast.app.upgrade.latest_release", return_value=None),
        ):
            diags = doctor._probe_updates()
        self.assertTrue(all(d.level != "error" for d in diags))


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
        with open(self._path, "w", encoding="utf-8") as f:
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
        # Even with stale calibration AND preset files at the legacy repo location,
        # the probe stays silent — both are surfaced at use time, not here.
        legacy = os.path.join(self._tmp.name, "repo")
        data = os.path.join(self._tmp.name, "data")
        for sub in (("calibration", "dac"), ("presets",)):
            d = os.path.join(legacy, *sub)
            os.makedirs(d)
            with open(os.path.join(d, "x.json"), "w", encoding="utf-8") as f:
                f.write("{}")
        with mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": data}):
            with mock.patch("c64cast.app.paths.legacy_data_root", return_value=Path(legacy)):
                diags = doctor._probe_data_dirs()
        self.assertEqual([d for d in diags if d.level == "warn"], [])


class EnsembleSharedDmaPasswordTest(unittest.TestCase):
    """`dma_password` cascades from the master where `url` does not, so one
    secret can unlock every machine — intended, but invisible from any single
    per-system file, which is what this row states."""

    def _diags(self, master_body: str, members: dict[str, str]) -> list[doctor.Diagnostic]:
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.toml")
            entries = ",\n    ".join(f'{{ name = "{n}", config = "{n}.toml" }}' for n in members)
            _write(master_path, f"[ensemble]\nsystems = [\n    {entries}\n]\n{master_body}")
            for name, body in members.items():
                _write(os.path.join(tmp, f"{name}.toml"), body)
            loaded = cfgmod.load_master(master_path)
        return doctor._validate_ensemble_shared_dma_password(loaded)

    def test_a_cascaded_password_names_every_system_it_reached(self):
        diags = self._diags('[ultimate64]\ndma_password = "hunter2"\n', {"left": "", "right": ""})
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "ok")
        self.assertIn("2 systems", diags[0].message)
        self.assertIn("left", diags[0].message)
        self.assertIn("right", diags[0].message)
        # The whole point is to describe the secret without disclosing it.
        self.assertNotIn("hunter2", diags[0].message)
        self.assertNotIn("hunter2", diags[0].hint or "")

    def test_different_per_system_passwords_are_not_sharing(self):
        # Each named its own, so the cascade filled neither — reporting them
        # as sharing would be a false alarm.
        diags = self._diags(
            "",
            {
                "left": '[ultimate64]\ndma_password = "one"\n',
                "right": '[ultimate64]\ndma_password = "two"\n',
            },
        )
        self.assertEqual(diags, [])

    def test_a_single_system_with_a_password_says_nothing(self):
        diags = self._diags('[ultimate64]\ndma_password = "hunter2"\n', {"only": ""})
        self.assertEqual(diags, [])

    def test_no_password_anywhere_says_nothing(self):
        self.assertEqual(self._diags("", {"left": "", "right": ""}), [])


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
        # It shows up under a CONFIG heading and in the summary tally, rather than
        # as a line above the report.
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


class ControlPlaneAuthDiagnosticTest(unittest.TestCase):
    """An open control plane on a network address is a doctor error, so it is
    answerable offline — before a show is up and the port is already live."""

    def test_open_network_bind_is_an_error_row(self):
        loaded = _load('[control]\nenabled = true\nhost = "0.0.0.0"\n')
        diags = doctor._validate_control(loaded)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].level, "error")
        self.assertEqual(diags[0].category, "control")
        self.assertIn("allow_unauthenticated", diags[0].message)

    def test_loopback_reports_nothing(self):
        loaded = _load('[control]\nenabled = true\nhost = "127.0.0.1"\n')
        self.assertEqual(doctor._validate_control(loaded), [])

    def test_the_opt_out_reports_nothing(self):
        loaded = _load(
            '[control]\nenabled = true\nhost = "0.0.0.0"\nallow_unauthenticated = true\n'
        )
        self.assertEqual(doctor._validate_control(loaded), [])

    def test_validate_load_result_includes_the_row(self):
        loaded = _load('[control]\nenabled = true\nhost = "0.0.0.0"\n')
        diags = doctor.validate_load_result(loaded, probe_u64=False)
        self.assertTrue(any(d.category == "control" for d in diags))

    def test_the_row_renders_and_fails_the_report(self):
        buf = io.StringIO()
        code = doctor.print_report(
            doctor._validate_control(_load('[control]\nenabled = true\nhost = "0.0.0.0"\n')),
            file=buf,
        )
        out = buf.getvalue()
        self.assertNotEqual(code, 0)  # error-level: the run should not start
        self.assertIn("CONTROL", out)


class SceneColorOverrideDiagnosticTest(unittest.TestCase):
    """A scene's own [scenes.color] override resolves and reports separately
    from the global [color] section — including a bad value, which has to
    name the scene it came from."""

    def test_a_scene_override_gets_its_own_resolution_note(self):
        loaded = _load(
            '[color]\ndither = "blue_noise"\n\n'
            '[[scenes]]\ntype = "video"\nfile = "clip.mp4"\n'
            '  [scenes.color]\n  dither = "auto"\n'
        )
        diags = doctor._validate_dither(loaded)
        override_notes = [d for d in diags if "override" in d.message]
        self.assertTrue(override_notes)
        self.assertEqual(override_notes[0].level, "ok")

    def test_a_bad_scene_override_is_an_error_naming_the_scene(self):
        loaded = _load(
            '[[scenes]]\ntype = "video"\nfile = "clip.mp4"\n  [scenes.color]\n  dither = "bogus"\n'
        )
        diags = doctor._validate_dither(loaded)
        self.assertEqual([d.level for d in diags], ["error"])
        self.assertIn("[[scenes]][0].color.dither", diags[0].message)

    def test_a_bad_scene_override_does_not_hide_other_scenes_diagnostics(self):
        # Scene "broken"'s override is invalid; scene "fine" has none and must still
        # get its resolution "ok" diagnostic rather than being skipped after it.
        loaded = _load(
            '[[scenes]]\nname = "broken"\ntype = "video"\nfile = "a.mp4"\n'
            '  [scenes.color]\n  dither = "bogus"\n\n'
            '[[scenes]]\nname = "fine"\ntype = "video"\nfile = "b.mp4"\n  [scenes.color]\n'
            '  dither = "auto"\n'
        )
        diags = doctor._validate_dither(loaded)
        self.assertEqual(sorted(d.level for d in diags), ["error", "ok"])
        error, ok = sorted(diags, key=lambda d: d.level)
        self.assertIn("broken", error.subject)
        self.assertIn("fine", ok.subject)

    def test_override_note_only_names_the_field_actually_overridden(self):
        # This scene overrides force_palette, not dither — the dither
        # resolution note must not falsely claim a per-scene override.
        loaded = _load(
            '[color]\ndither = "auto"\n\n'
            '[[scenes]]\ntype = "video"\nfile = "clip.mp4"\n'
            "  [scenes.color]\n  force_palette = true\n"
        )
        diags = doctor._validate_dither(loaded)
        ok = [d for d in diags if d.level == "ok"]
        self.assertTrue(ok)
        self.assertNotIn("override", ok[0].message)


class HardwarePaletteDiagnosticTest(unittest.TestCase):
    """The refusals `--doctor --skip-probe` reports must be the ones a run
    raises at startup, or a config passes the offline check and then fails
    with the hardware already open."""

    def _diags(self, toml: str) -> list[doctor.Diagnostic]:
        return doctor.validate_load_result(_load(toml), probe_u64=False, probe_environment=False)

    def _hardware_palette_errors(self, toml: str) -> list[doctor.Diagnostic]:
        return [d for d in self._diags(toml) if d.subject.endswith("/hardware_palette")]

    def test_source_with_force_palette_is_an_error(self):
        errors = self._hardware_palette_errors(
            '[color]\nhardware_palette = "source"\nforce_palette = true\n'
        )
        self.assertEqual([d.level for d in errors], ["error"])
        self.assertIn("force_palette", errors[0].message)

    def test_source_with_flicker_blending_is_an_error(self):
        errors = self._hardware_palette_errors(
            '[color]\nhardware_palette = "source"\nflicker_tolerance = "clean"\n'
        )
        self.assertEqual([d.level for d in errors], ["error"])
        self.assertIn("flicker_tolerance", errors[0].message)

    def test_a_scene_override_is_an_error_naming_the_scene(self):
        errors = self._hardware_palette_errors(
            '[[scenes]]\ntype = "video"\nfile = "clip.mp4"\n'
            '  [scenes.color]\n  hardware_palette = "source"\n  force_palette = true\n'
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("[[scenes]][0].color", errors[0].message)

    def test_source_on_its_own_reports_nothing(self):
        self.assertEqual(
            self._hardware_palette_errors('[color]\nhardware_palette = "source"\n'), []
        )

    _REFUSED_SCENE = (
        '[[scenes]]\ntype = "video"\nfile = "{file}"\n'
        '  [scenes.color]\n  hardware_palette = "source"\n  force_palette = true\n'
    )

    def test_every_refused_scene_is_reported(self):
        errors = self._hardware_palette_errors(
            self._REFUSED_SCENE.format(file="a.mp4") + self._REFUSED_SCENE.format(file="b.mp4")
        )
        self.assertEqual(len(errors), 2)
        self.assertIn("[[scenes]][0].color", errors[0].message)
        self.assertIn("[[scenes]][1].color", errors[1].message)

    def test_an_unresolvable_override_neither_hides_nor_is_reported_as_one(self):
        errors = self._hardware_palette_errors(
            '[[scenes]]\ntype = "video"\nfile = "z.mp4"\n'
            '  [scenes.color]\n  force_palette_colors = ["black"]\n'
            + self._REFUSED_SCENE.format(file="a.mp4")
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("[[scenes]][1].color", errors[0].message)

    def test_an_unresolvable_clip_override_is_still_reported(self):
        # No other doctor check builds a clip, and a run refuses this one at
        # startup, so the offline check must not pass it.
        diags = self._diags(
            '[[scenes]]\ntype = "blank"\n\n'
            '[[performance.clips]]\nslot = 1\ntype = "video"\nfile = "z.mp4"\n'
            '  [performance.clips.color]\n  force_palette_colors = ["black"]\n'
        )
        errors = [d for d in diags if d.level == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].subject, "system/[[performance.clips]][0].color")
        self.assertIn("force_palette_colors", errors[0].message)

    def test_an_unresolvable_scene_override_is_left_to_validate_scenes(self):
        diags = self._diags(
            '[[scenes]]\ntype = "video"\nfile = "z.mp4"\n'
            '  [scenes.color]\n  force_palette_colors = ["black"]\n'
        )
        self.assertEqual(
            [d.subject for d in diags if d.level == "error" and d.subject.endswith(".color")],
            [],
        )
        self.assertIn(
            ("scene", "system/video#0"),
            [(d.category, d.subject) for d in diags if d.level == "error"],
        )


class ClipColorDiagnosticTest(unittest.TestCase):
    """A clip's color override that a run refuses at startup must not pass
    `--doctor --skip-probe`."""

    _CLIP = (
        '[[scenes]]\ntype = "blank"\n\n'
        '[[performance.clips]]\nslot = 1\ntype = "video"\nfile = "z.mp4"\n'
        "  [performance.clips.color]\n  {key} = {value}\n"
    )

    def _clip_errors(self, key: str, value: str) -> list[doctor.Diagnostic]:
        loaded = _load(self._CLIP.format(key=key, value=value))
        diags = doctor.validate_load_result(loaded, probe_u64=False, probe_environment=False)
        return [d for d in diags if d.level == "error"]

    def test_each_value_a_run_refuses_is_reported_against_the_clip(self):
        bad = {
            "dither": '"bogus"',
            "color_match": '"bogus"',
            "cell_strategy": '"bogus"',
            "motion_smoothing": "5.0",
            "flicker_tolerance": '"bogus"',
        }
        for key, value in bad.items():
            with self.subTest(key=key):
                loaded = _load(self._CLIP.format(key=key, value=value))
                with self.assertRaises(cfgmod.ConfigError):
                    for validate in scene_factory.PER_SYSTEM_VALIDATORS:
                        validate(loaded.cfgs[0])
                errors = self._clip_errors(key, value)
                self.assertEqual(
                    [d.subject for d in errors], ["system/[[performance.clips]][0].color"]
                )
                self.assertIn(key, errors[0].message)

    def test_a_scenes_bad_value_is_left_to_the_per_scene_check(self):
        loaded = _load(
            '[[scenes]]\ntype = "video"\nfile = "z.mp4"\n  [scenes.color]\n  dither = "bogus"\n'
        )
        diags = doctor.validate_load_result(loaded, probe_u64=False, probe_environment=False)
        subjects = [d.subject for d in diags if d.level == "error"]
        self.assertEqual(len(subjects), 1)
        self.assertTrue(subjects[0].endswith("/dither"))

    def test_a_bad_global_flicker_tolerance_is_reported_once_against_color(self):
        loaded = _load(
            '[color]\nflicker_tolerance = "bogus"\n\n'
            + self._CLIP.format(key="dither_strength", value="1.0")
        )
        diags = doctor.validate_load_result(loaded, probe_u64=False, probe_environment=False)
        self.assertEqual(
            [d.subject for d in diags if d.level == "error"], ["system/flicker_tolerance"]
        )

    def test_a_bad_global_flicker_tolerance_a_scene_reports_is_not_repeated(self):
        loaded = _load(
            '[color]\nflicker_tolerance = "bogus"\n\n[[scenes]]\ntype = "video"\nfile = "z.mp4"\n'
        )
        diags = doctor.validate_load_result(loaded, probe_u64=False, probe_environment=False)
        errors = [d for d in diags if d.level == "error"]
        self.assertEqual([d.subject for d in errors], ["system/video#0"])
        self.assertIn("flicker_tolerance", errors[0].message)

    def test_a_clip_value_differing_from_a_bad_global_is_reported(self):
        loaded = _load(
            '[color]\ndither = "bogus"\n\n' + self._CLIP.format(key="dither", value='"other"')
        )
        diags = doctor.validate_load_result(loaded, probe_u64=False, probe_environment=False)
        self.assertEqual(
            [d.subject for d in diags if d.level == "error"],
            ["system/dither", "system/[[performance.clips]][0].color"],
        )

    def test_a_scene_override_its_per_scene_check_skips_is_reported(self):
        bad = {
            "cell_strategy": '"bogus"',
            "motion_smoothing": "5.0",
        }
        for key, value in bad.items():
            with self.subTest(key=key):
                loaded = _load(
                    '[[scenes]]\ntype = "video"\nfile = "z.mp4"\ndisplay = "hires"\n'
                    f"  [scenes.color]\n  {key} = {value}\n"
                )
                diags = doctor.validate_load_result(
                    loaded, probe_u64=False, probe_environment=False
                )
                errors = [d for d in diags if d.level == "error"]
                self.assertEqual([d.subject for d in errors], ["system/[[scenes]][0].color"])
                self.assertIn(key, errors[0].message)
        loaded = _load(
            '[[scenes]]\ntype = "video"\nfile = "z.mp4"\ndisplay = "hires_edges"\n'
            '  [scenes.color]\n  color_match = "bogus"\n'
        )
        diags = doctor.validate_load_result(loaded, probe_u64=False, probe_environment=False)
        self.assertEqual(
            [d.subject for d in diags if d.level == "error"], ["system/[[scenes]][0].color"]
        )

    def test_a_flicker_override_on_a_scene_whose_build_skips_it_is_reported(self):
        loaded = _load(
            '[[scenes]]\ntype = "generative"\naudio_source = "sid"\nfile = "z.sid"\n'
            'display = "petscii"\n  [scenes.color]\n  flicker_tolerance = "bogus"\n'
        )
        diags = doctor.validate_load_result(loaded, probe_u64=False, probe_environment=False)
        errors = [d for d in diags if d.level == "error"]
        self.assertEqual([d.subject for d in errors], ["system/[[scenes]][0].color"])
        self.assertIn("flicker_tolerance", errors[0].message)

    def test_a_flicker_override_the_scene_build_reports_is_not_repeated(self):
        loaded = _load(
            '[[scenes]]\ntype = "video"\nfile = "z.mp4"\n'
            '  [scenes.color]\n  flicker_tolerance = "bogus"\n'
        )
        diags = doctor.validate_load_result(loaded, probe_u64=False, probe_environment=False)
        self.assertEqual([d.subject for d in diags if d.level == "error"], ["system/video#0"])

    def test_a_valid_clip_override_reports_nothing(self):
        self.assertEqual(self._clip_errors("dither", '"ordered"'), [])


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


class SampleRateHintTest(unittest.TestCase):
    """The hint used to spell out the safe rates by hand and drifted: it kept
    recommending 10500 for two releases after 12000 became the default."""

    def test_hint_quotes_the_shipped_default(self):
        hint = doctor._sample_rate_hint("NTSC", 20000)
        self.assertIn(str(cfgmod.AudioCfg().sample_rate), hint)

    def test_hint_quotes_both_standard_ceilings(self):
        hint = doctor._sample_rate_hint("NTSC", 20000)
        for system in ("NTSC", "PAL"):
            self.assertIn(str(max_safe_sample_rate(system)), hint)

    def test_a_rate_too_slow_for_the_timer_is_told_to_go_up(self):
        # 10 Hz is refused for the 16-bit latch, not the handler budget, so
        # "Lower [audio].sample_rate" would send the user the wrong way.
        loaded = _load(
            """
            [audio]
            enabled = true
            sample_rate = 10
            """
        )
        found = doctor._validate_audio_nmi_rate(loaded)
        self.assertEqual([d.level for d in found], ["error"])
        hint = found[0].hint or ""
        self.assertTrue(hint.startswith("Raise [audio].sample_rate"), hint)
        self.assertIn("16 Hz", hint)

    def test_a_rate_on_the_ceiling_latch_reports_no_negative_headroom(self):
        # 13700 Hz NTSC rounds onto the ceiling latch, so load accepts it, but it
        # sits above max_safe_sample_rate; the adaptive loop has 0 % left, not -0.5 %.
        loaded = _load(
            """
            [audio]
            enabled = true
            sample_rate = 13700
            nmi_rate_adaptive = true
            [ultimate64]
            system = "NTSC"
            """
        )
        found = doctor._validate_audio_nmi_rate(loaded)
        self.assertEqual([d.level for d in found], ["warn"])
        self.assertIn("only 0.0% NMI headroom", found[0].message)

    def test_unsafe_rate_carries_the_hint(self):
        loaded = _load(
            """
            [audio]
            enabled = true
            sample_rate = 20000
            """
        )
        found = doctor._validate_audio_nmi_rate(loaded)
        self.assertTrue(found)
        self.assertEqual(found[0].level, "error")
        self.assertEqual(found[0].hint, doctor._sample_rate_hint("NTSC", 20000))


if __name__ == "__main__":
    unittest.main()
