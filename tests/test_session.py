"""Tests for the session lifecycle: validate -> build -> serve -> run -> tear down.

Everything here runs against mocked stacks. What the unittest suite cannot
reach — real DMA socket reuse after `close()`, a camera reopened straight
after `release()`, cv2 windows opened and closed across sessions in one
process on macOS — is hardware territory and goes through the
`hw-visual-verify` skill instead.

SystemStack and Session carry typed fields (Ultimate64API, Playlist, ...) —
we stuff MagicMocks into them, so silence pyright's attribute-access
complaints file-wide rather than spraying ignores on every assertion."""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
from __future__ import annotations

import argparse
import os
import tempfile
import threading
import unittest
from unittest import mock

from _fakes import fake_system_stack, tmp_cwd

from c64cast.app import config as cfgmod
from c64cast.app import profiler as profiler_mod
from c64cast.app import scene_factory, session
from c64cast.audio.audio import AudioStreamer


def _loaded(names: list[str], *, is_ensemble: bool = False) -> cfgmod.LoadResult:
    # Audio off by default: validate_configs rejects an audio-enabled config when
    # sounddevice is missing, which would tie every assertion to the 'mic' extra.
    cfgs = [cfgmod.Config() for _ in names]
    for cfg in cfgs:
        cfg.audio.enabled = False
    return cfgmod.LoadResult(
        cfgs=cfgs,
        names=list(names),
        paths=[None] * len(names),
        is_ensemble=is_ensemble,
        master_control=cfgs[0].control,
        master_midi_control=cfgs[0].midi_control,
    )


def _args(**overrides) -> argparse.Namespace:
    ns = argparse.Namespace(overwrite=False)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _session(*names: str, **overrides) -> session.Session:
    loaded = _loaded(list(names))
    return session.Session(
        args=_args(),
        loaded=loaded,
        cfgs=loaded.cfgs,
        stacks=[fake_system_stack(n) for n in names],
        ensemble=None,
        stop_event=threading.Event(),
        profiler=mock.MagicMock(name="profiler"),
        **overrides,
    )


class ReExportTest(unittest.TestCase):
    """The extraction is only inert if the names callers have always imported
    from `c64cast.app.cli` still resolve there — diag scripts under scripts/
    and the existing test suite both reach for them by that path."""

    def test_cli_still_exports_the_moved_names(self):
        from c64cast.app import cli

        for name in (
            "StackBuildError",
            "build_stack",
            "teardown_stack",
            "_pump_previews_until_done",
            "_coerce_reu_for_backend",
            "_maybe_save_live_tune",
            "_open_backend",
        ):
            self.assertIs(
                getattr(cli, name),
                getattr(session, name),
                f"cli.{name} is not session.{name}",
            )


class SessionConfigErrorTest(unittest.TestCase):
    def test_str_falls_back_to_the_exit_code_when_no_detail_is_given(self):
        e = session.SessionConfigError(5)
        self.assertEqual(e.detail, "")
        self.assertIn("exit code 5", str(e))

    def test_str_is_the_detail_when_one_is_given(self):
        e = session.SessionConfigError(3, "scene outro: no such file")
        self.assertEqual(str(e), "scene outro: no such file")


class ValidateConfigsTest(unittest.TestCase):
    """validate_configs must reach a verdict without touching hardware — that
    is what lets a caller reject a config while a session is running."""

    def test_audio_without_sounddevice_is_exit_3(self):
        loaded = _loaded(["a"])
        loaded.cfgs[0].audio.enabled = True
        with mock.patch.object(session, "AUDIO_AVAILABLE", False):
            with self.assertLogs("c64cast", level="ERROR") as logged:
                with self.assertRaises(session.SessionConfigError) as cm:
                    session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 3)
        self.assertIn("sounddevice is not installed", logged.output[0])
        self.assertIn("sounddevice is not installed", cm.exception.detail)

    def test_a_config_error_from_any_validator_is_exit_5(self):
        loaded = _loaded(["a"])

        def bad_dither(cfg: cfgmod.Config) -> None:
            raise cfgmod.ConfigError("bad dither")

        with mock.patch.object(session.scene_factory, "PER_SYSTEM_VALIDATORS", (bad_dither,)):
            with self.assertLogs("c64cast", level="ERROR") as logged:
                with self.assertRaises(session.SessionConfigError) as cm:
                    session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 5)
        self.assertIn("bad dither", logged.output[0])
        self.assertEqual(cm.exception.detail, "bad dither")

    def test_an_open_control_plane_on_a_network_host_is_exit_5(self):
        # The gate has to be here, not at bind time: start_services runs after the
        # hardware is up, so a warning there arrives with a show already on screen.
        loaded = _loaded(["a"])
        loaded.master_control.enabled = True
        loaded.master_control.host = "0.0.0.0"
        with self.assertLogs("c64cast", level="ERROR") as logged:
            with self.assertRaises(session.SessionConfigError) as cm:
                session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 5)
        self.assertIn("allow_unauthenticated", logged.output[0])

    def _pumped_blank_big_text(self, *, is_ensemble: bool) -> cfgmod.LoadResult:
        loaded = _loaded(["a"], is_ensemble=is_ensemble)
        cfg = loaded.cfgs[0]
        cfg.audio.enabled = True
        cfg.audio.use_reu_pump = True
        cfg.scenes = [
            cfgmod.SceneCfg(
                type="blank", overlays=[{"type": "big_text", "messages": [{"text": "HI"}]}]
            )
        ]
        return loaded

    def test_an_ensemble_blank_big_text_scene_passes_with_the_pump_on(self):
        # Ensemble live scenes run silent, so they never start the pump (#559).
        loaded = self._pumped_blank_big_text(is_ensemble=True)
        with mock.patch.object(session, "AUDIO_AVAILABLE", True):
            session.validate_configs(loaded, loaded.cfgs)  # no raise

    def test_a_single_system_blank_big_text_scene_is_exit_3_with_the_pump_on(self):
        loaded = self._pumped_blank_big_text(is_ensemble=False)
        with mock.patch.object(session, "AUDIO_AVAILABLE", True):
            with self.assertLogs("c64cast", level="ERROR") as logged:
                with self.assertRaises(session.SessionConfigError) as cm:
                    session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 3)
        self.assertIn("use_reu_pump", logged.output[0])

    def test_clean_configs_pass(self):
        loaded = _loaded(["a", "b"])
        session.validate_configs(loaded, loaded.cfgs)  # no raise

    def test_a_bad_scene_is_exit_3_before_any_hardware_is_opened(self):
        # Exit 3 is what build_stack returns for the same error once
        # scenes_from_config reaches it, so checking earlier keeps the CLI's answer
        # the same.
        loaded = _loaded(["a"])
        loaded.cfgs[0].scenes = [cfgmod.SceneCfg(type="video", duration_s=5.0)]
        # From an empty cwd: a video scene with no `file` resolves against
        # assets/videos/, so "unresolvable" is only true where that is empty.
        with tmp_cwd(), self.assertLogs("c64cast", level="ERROR"):
            with self.assertRaises(session.SessionConfigError) as cm:
                session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 3)

    def test_the_diagnostic_names_the_scene_that_failed(self):
        loaded = _loaded(["a"])
        loaded.cfgs[0].scenes = [
            cfgmod.SceneCfg(type="blank"),
            cfgmod.SceneCfg(type="video", name="outro", duration_s=5.0),
        ]
        with tmp_cwd(), self.assertLogs("c64cast", level="ERROR") as logged:
            with self.assertRaises(session.SessionConfigError) as cm:
                session.validate_configs(loaded, loaded.cfgs)
        self.assertIn("outro", logged.output[0])
        self.assertIn("outro", cm.exception.detail)

    def test_a_follower_only_scene_is_validated_too(self):
        # It is built lazily at broadcast time, so a bad one would otherwise
        # surface mid-show rather than before the run.
        loaded = _loaded(["a"])
        loaded.cfgs[0].scenes = [cfgmod.SceneCfg(type="video", follower_only=True, duration_s=5.0)]
        with tmp_cwd(), self.assertLogs("c64cast", level="ERROR"):
            with self.assertRaises(session.SessionConfigError):
                session.validate_configs(loaded, loaded.cfgs)

    def test_a_bad_scene_force_palette_override_is_exit_5_not_an_unhandled_error(self):
        # force_palette_colors is range-checked by scene_color(), which raises a
        # plain ValueError; it must surface as SessionConfigError (cli.py's handler
        # around validate_configs catches only ConfigError), not escape unhandled.
        loaded = _loaded(["a"])
        loaded.cfgs[0].scenes = [cfgmod.SceneCfg(type="video", color={"force_palette_colors": 999})]
        with self.assertLogs("c64cast", level="ERROR") as logged:
            with self.assertRaises(session.SessionConfigError) as cm:
                session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 5)
        self.assertIn("force_palette_colors", logged.output[0])

    def test_transport_coercion_runs_before_any_stack_is_built(self):
        # [audio].use_reu_pump has no seek/splice support, so a transport.* MIDI
        # mapping must force it off here — build_stack bakes the flag into the
        # AudioStreamer constructor.
        loaded = _loaded(["a"])
        loaded.cfgs[0].audio.use_reu_pump = True
        loaded.master_midi_control.enabled = True
        loaded.master_midi_control.cc_map = [{"cc": 1, "action": "transport.seek"}]
        session.validate_configs(loaded, loaded.cfgs)
        self.assertFalse(loaded.cfgs[0].audio.use_reu_pump)


class PerSystemValidatorsTest(unittest.TestCase):
    """`validate_configs` used to name its nine validators one by one, and had
    already fallen a validator behind: `validate_wled_cfg` reached `--doctor`
    and no actual run, so a bad [wled] section failed mid-show instead of
    before the hardware was opened."""

    def test_the_tuple_covers_every_whole_config_validator_in_scene_factory(self):
        import inspect

        from c64cast.app import scene_factory

        defined = set()
        for name, fn in vars(scene_factory).items():
            if not name.startswith("validate_") or not inspect.isfunction(fn):
                continue
            params = list(inspect.signature(fn).parameters.values())
            if len(params) == 1 and params[0].annotation == "Config":
                defined.add(fn)
        self.assertEqual(defined, set(scene_factory.PER_SYSTEM_VALIDATORS))

    def test_a_bad_wled_endpoint_is_rejected_before_any_hardware_is_opened(self):
        loaded = _loaded(["a"])
        loaded.cfgs[0].wled.listen = ":70000"
        with self.assertLogs("c64cast", level="ERROR"):
            with self.assertRaises(session.SessionConfigError) as cm:
                session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 5)


class BuildSessionTest(unittest.TestCase):
    def setUp(self):
        # build_session installs the profiler process-wide via set_profiler and never
        # puts back what was there; the global outlives the test either way.
        self.addCleanup(profiler_mod.set_profiler, profiler_mod.get_profiler())

    def test_builds_one_stack_per_system(self):
        loaded = _loaded(["a", "b"])
        stacks = [fake_system_stack("a"), fake_system_stack("b")]
        with mock.patch.object(session, "build_stack", side_effect=stacks) as bs:
            sess = session.build_session(_args(), loaded, loaded.cfgs)
        self.assertEqual(sess.stacks, stacks)
        self.assertEqual(bs.call_count, 2)
        # Every playlist shares one stop_event, so one stop reaches them all.
        for call in bs.call_args_list:
            self.assertIs(call.kwargs["stop_event"], sess.stop_event)

    def test_a_failed_build_tears_down_what_came_up_in_reverse(self):
        # A partial failure must not leave hardware held: the machine is
        # unreachable until whatever opened it closes it again.
        loaded = _loaded(["a", "b", "c"])
        built = [fake_system_stack("a"), fake_system_stack("b")]
        torn: list[str] = []
        with (
            mock.patch.object(
                session, "build_stack", side_effect=[*built, session.StackBuildError(4)]
            ),
            mock.patch.object(
                session, "teardown_stack", side_effect=lambda st: torn.append(st.name)
            ),
        ):
            with self.assertRaises(session.StackBuildError) as cm:
                session.build_session(_args(), loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 4)
        self.assertEqual(torn, ["b", "a"])

    def test_any_exception_from_a_later_build_tears_down_what_came_up(self):
        # A provisioning step raising an OSError or RuntimeError, or a Ctrl+C
        # mid-build, leaves system a's socket and provisioning just as held.
        for exc in (OSError("socket"), RuntimeError("streamer"), KeyboardInterrupt()):
            loaded = _loaded(["a", "b", "c"], is_ensemble=True)
            built = [fake_system_stack("a"), fake_system_stack("b")]
            with (
                self.subTest(exc=type(exc).__name__),
                mock.patch.object(session, "build_stack", side_effect=[*built, exc]),
                mock.patch.object(session, "teardown_stack") as teardown,
            ):
                with self.assertRaises(type(exc)) as cm:
                    session.build_session(_args(), loaded, loaded.cfgs)
                self.assertIs(cm.exception, exc)
                torn = [c.args[0].name for c in teardown.call_args_list]
                self.assertEqual(torn, ["b", "a"])

    def test_a_teardown_that_raises_still_tears_down_the_stacks_under_it(self):
        # A second Ctrl+C while system b's teardown runs must not strand a.
        loaded = _loaded(["a", "b", "c"], is_ensemble=True)
        built = [fake_system_stack("a"), fake_system_stack("b")]
        torn: list[str] = []

        def teardown(st):
            torn.append(st.name)
            if st.name == "b":
                raise KeyboardInterrupt

        with (
            mock.patch.object(
                session, "build_stack", side_effect=[*built, session.StackBuildError(4)]
            ),
            mock.patch.object(session, "teardown_stack", side_effect=teardown),
        ):
            with self.assertRaises(KeyboardInterrupt):
                session.build_session(_args(), loaded, loaded.cfgs)
        self.assertEqual(torn, ["b", "a"])

    def test_a_failure_wiring_the_ensemble_tears_down_every_stack(self):
        loaded = _loaded(["a", "b"], is_ensemble=True)
        built = [fake_system_stack("a"), fake_system_stack("b")]
        built[1].playlist.bind_ensemble.side_effect = KeyboardInterrupt
        with (
            mock.patch.object(session, "build_stack", side_effect=built),
            mock.patch.object(session, "teardown_stack") as teardown,
        ):
            with self.assertRaises(KeyboardInterrupt):
                session.build_session(_args(), loaded, loaded.cfgs)
        torn = [c.args[0].name for c in teardown.call_args_list]
        self.assertEqual(torn, ["b", "a"])

    def test_a_successful_build_tears_nothing_down(self):
        loaded = _loaded(["a", "b"], is_ensemble=True)
        built = [fake_system_stack("a"), fake_system_stack("b")]
        with (
            mock.patch.object(session, "build_stack", side_effect=built),
            mock.patch.object(session, "teardown_stack") as teardown,
        ):
            session.build_session(_args(), loaded, loaded.cfgs)
        teardown.assert_not_called()

    def test_ensemble_mode_binds_every_playlist(self):
        loaded = _loaded(["a", "b"], is_ensemble=True)
        stacks = [fake_system_stack("a"), fake_system_stack("b")]
        with mock.patch.object(session, "build_stack", side_effect=stacks):
            sess = session.build_session(_args(), loaded, loaded.cfgs)
        self.assertIsNotNone(sess.ensemble)
        self.assertIs(sess.ensemble.stop_event, sess.stop_event)
        for st in stacks:
            st.playlist.bind_ensemble.assert_called_once()

    def test_follower_scene_factories_capture_their_own_stack(self):
        # The classic late-binding trap: build the factories in a loop and
        # every one of them ends up pointing at the last stack.
        loaded = _loaded(["a", "b"], is_ensemble=True)
        stacks = [fake_system_stack("a"), fake_system_stack("b")]
        with mock.patch.object(session, "build_stack", side_effect=stacks):
            session.build_session(_args(), loaded, loaded.cfgs)
        factories = [
            st.playlist.bind_ensemble.call_args.kwargs["build_follower_scene"] for st in stacks
        ]
        with mock.patch.object(session.scene_factory, "build_scene") as bs:
            for f in factories:
                f(mock.MagicMock(name="scene_cfg"))
        self.assertEqual([c.args[2] for c in bs.call_args_list], [stacks[0].api, stacks[1].api])


class OpenBackendPasswordTest(unittest.TestCase):
    """A network-password problem stops the stack with exit 4, the code a
    rejected DMA password already gets, and logs the reason."""

    def test_a_rest_password_refusal_is_exit_4(self):
        from c64cast.hw.api import RestAuthError

        backend = mock.MagicMock(name="backend")
        backend.probe.side_effect = RestAuthError("REST API refused c64cast")
        with (
            mock.patch.object(session, "make_backend", return_value=backend),
            self.assertLogs("c64cast", "ERROR") as logs,
            self.assertRaises(session.StackBuildError) as caught,
        ):
            session._open_backend(cfgmod.Config(), "system")
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertIn("(system): REST API refused c64cast", "\n".join(logs.output))
        backend.close.assert_called_once()

    def test_an_unsendable_password_or_unbuildable_backend_is_exit_4(self):
        from c64cast.hw.api import InvalidPasswordError
        from c64cast.hw.backend import BackendSetupError

        for exc in (InvalidPasswordError("bad header"), BackendSetupError("no serial port")):
            with (
                self.subTest(exc=type(exc).__name__),
                mock.patch.object(session, "make_backend", side_effect=exc),
                self.assertLogs("c64cast", "ERROR") as logs,
                self.assertRaises(session.StackBuildError) as caught,
            ):
                session._open_backend(cfgmod.Config(), "system")
            self.assertEqual(caught.exception.exit_code, 4)
            self.assertIn(str(exc), "\n".join(logs.output))

    def test_an_unrelated_value_error_is_not_reported_as_a_connect_failure(self):
        with (
            mock.patch.object(session, "make_backend", side_effect=ValueError("a defect")),
            self.assertNoLogs("c64cast", "ERROR"),
            self.assertRaisesRegex(ValueError, "a defect"),
        ):
            session._open_backend(cfgmod.Config(), "system")


class BuildStackCameraTest(unittest.TestCase):
    """build_stack opens the camera only when something needs it — and a
    [[performance.clips]] table counts: `type` defaults to "webcam" there, so
    a clip grid can hold webcam clips with no webcam [[scenes]] entry at all.
    With `source` left None the clip build factory raises at launch and
    PerformanceSession's background build swallows it into `armed.error`, so
    the pad dies silently for the whole show."""

    def _camera_opens_for(self, cfg: cfgmod.Config) -> mock.MagicMock:
        # _open_backend is the first thing after the camera decision, so
        # failing it there keeps this hardware-free.
        with (
            mock.patch.object(session, "WebcamSource") as source_cls,
            mock.patch.object(session, "_open_backend", side_effect=session.StackBuildError(4)),
        ):
            with self.assertRaises(session.StackBuildError):
                session.build_stack(
                    cfg,
                    "a",
                    stop_event=threading.Event(),
                    profiler=mock.MagicMock(name="profiler"),
                )
        return source_cls

    def test_a_clip_that_names_no_type_is_a_webcam_clip(self):
        cfg = cfgmod.Config()
        cfg.scenes = []
        cfg.performance.clips = [{"pad": 1}]
        self._camera_opens_for(cfg).assert_called_once()

    def test_a_clip_of_another_type_leaves_the_camera_shut(self):
        cfg = cfgmod.Config()
        cfg.scenes = []
        cfg.performance.clips = [{"pad": 1, "type": "blank"}]
        self._camera_opens_for(cfg).assert_not_called()


class WarnIfMenuOpenTest(unittest.TestCase):
    """The post-bring-up menu check: one WARNING when the menu is open, and
    no read at all on firmware without the route."""

    def _api(self, *, supported: bool, screen: object) -> mock.MagicMock:
        api = mock.MagicMock(name="api")
        api.profile.supports_menu_screen = supported
        api.read_menu_screen.return_value = screen
        return api

    def test_open_menu_warns(self):
        api = self._api(supported=True, screen=object())
        with self.assertLogs("c64cast", level="WARNING") as cm:
            session._warn_if_menu_open(api)
        self.assertIn("menu is open", cm.output[0])

    def test_closed_menu_is_quiet(self):
        api = self._api(supported=True, screen=None)
        with self.assertNoLogs("c64cast", level="WARNING"):
            session._warn_if_menu_open(api)
        api.read_menu_screen.assert_called_once()

    def test_firmware_without_the_route_is_not_asked(self):
        api = self._api(supported=False, screen=object())
        session._warn_if_menu_open(api)
        api.read_menu_screen.assert_not_called()


class OpenBackendIdentityTest(unittest.TestCase):
    """The connect line asks for the firmware build only at -v, where the
    root logger is at DEBUG."""

    def _detailed_at(self, level: str) -> bool:
        cfg = cfgmod.Config()
        api = mock.MagicMock()
        api.probe.return_value = "HTTP 200"
        api.describe_device.return_value = "Ultimate 64-II"
        with (
            mock.patch.object(session, "make_backend", return_value=api),
            mock.patch.object(session.hw_provision, "resolve_system"),
            mock.patch.object(session.hw_provision, "resolve_palette"),
            self.assertLogs("c64cast", level=level) as logs,
        ):
            session._open_backend(cfg, "system")
        self.assertIn("connected device: Ultimate 64-II", "\n".join(logs.output))
        return api.describe_device.call_args.kwargs["detailed"]

    def test_default_verbosity_leaves_the_build_out(self):
        self.assertFalse(self._detailed_at("INFO"))

    def test_debug_asks_for_the_build(self):
        self.assertTrue(self._detailed_at("DEBUG"))


class BuildStackHardwarePaletteTest(unittest.TestCase):
    """build_stack provisions the run's palette pusher after the startup
    resets, and a build that fails later gives the machine its own palette
    back. Everything around the call is stubbed; the step after it fails the
    build, which runs the unwind ladder."""

    def _build(self, cfg: cfgmod.Config, control: object) -> mock.MagicMock:
        api = mock.MagicMock(name="api")
        api.profile.max_fps = None
        api.disable_case_switch.side_effect = session.StackBuildError(4)
        api.read_menu_screen.return_value = None
        self.cleared_before_provision: list[bool] = []

        def provision_after_noting(*_args, **_kwargs):
            self.cleared_before_provision.append(api.run_basic_clear_loop.called)
            return control

        provision = mock.MagicMock(side_effect=provision_after_noting)
        with (
            mock.patch.object(session, "_open_backend", return_value=api),
            mock.patch.object(session, "hw_provision"),
            mock.patch.object(session, "_build_audio", return_value=mock.MagicMock(name="audio")),
            mock.patch.object(session, "_resolve_reu_available", return_value=False),
            mock.patch.object(session, "_resolve_sampler_available", return_value=False),
            mock.patch.object(session.scene_factory, "scenes_from_config", return_value=[]),
            mock.patch.object(session.char_rom, "ensure_installed"),
            mock.patch.object(session.time, "sleep"),
            mock.patch.object(session.hardware_palette, "provision_hardware_palette", provision),
        ):
            with self.assertRaises(session.StackBuildError):
                session.build_stack(
                    cfg,
                    "a",
                    stop_event=threading.Event(),
                    profiler=mock.MagicMock(name="profiler"),
                    is_ensemble=True,
                )
        return provision

    def test_it_provisions_after_the_startup_reset_for_this_run(self):
        cfg = cfgmod.Config()
        cfg.scenes = []
        provision = self._build(cfg, None)
        self.assertEqual(self.cleared_before_provision, [True])
        provision.assert_called_once()
        self.assertIs(provision.call_args.args[1], cfg)
        self.assertTrue(provision.call_args.kwargs["is_ensemble"])

    def test_a_build_that_fails_afterwards_restores_the_machines_palette(self):
        cfg = cfgmod.Config()
        cfg.scenes = []
        control = mock.MagicMock(name="control")
        self._build(cfg, control)
        control.restore.assert_called_once()


class BuildStackDacCurveTest(unittest.TestCase):
    """build_stack resolves [audio].dac_curve only for a run with audio, and
    turns a 'calibrated' curve with no calibration into a StackBuildError, so
    build_session tears down the stacks that did come up."""

    def _build(self, cfg: cfgmod.Config, resolve: mock.MagicMock) -> None:
        api = self.api = mock.MagicMock(name="api")
        api.profile.max_fps = None
        api.disable_case_switch.side_effect = session.StackBuildError(4)
        api.read_menu_screen.return_value = None
        self.hw_provision = mock.MagicMock(name="hw_provision")
        with (
            mock.patch.object(session, "_open_backend", return_value=api),
            mock.patch.object(session, "hw_provision", self.hw_provision),
            mock.patch.object(session, "_build_audio", return_value=mock.MagicMock(name="audio")),
            mock.patch.object(session.dac_curve_resolve, "resolve_dac_curve_for_backend", resolve),
            mock.patch.object(session, "_resolve_reu_available", return_value=False),
            mock.patch.object(session, "_resolve_sampler_available", return_value=False),
            mock.patch.object(session.scene_factory, "scenes_from_config", return_value=[]),
            mock.patch.object(session.char_rom, "ensure_installed"),
            mock.patch.object(session.time, "sleep"),
            mock.patch.object(session.hardware_palette, "provision_hardware_palette"),
        ):
            session.build_stack(
                cfg, "a", stop_event=threading.Event(), profiler=mock.MagicMock(name="profiler")
            )

    def test_a_missing_calibration_is_a_stack_build_error(self):
        cfg = cfgmod.Config()
        cfg.scenes = []
        resolve = mock.MagicMock(side_effect=ValueError("no usable calibration"))
        with (
            self.assertLogs("c64cast", "ERROR") as cm,
            self.assertRaises(session.StackBuildError) as raised,
        ):
            self._build(cfg, resolve)
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("no usable calibration", "\n".join(cm.output))

    def test_a_missing_calibration_fails_before_the_machine_is_touched(self):
        # Master volume, the HDMI mode switch (a capture device re-locks on it)
        # and the reset would all happen only to be reverted by the unwind.
        cfg = cfgmod.Config()
        cfg.scenes = []
        resolve = mock.MagicMock(side_effect=ValueError("no usable calibration"))
        with (
            self.assertLogs("c64cast", "ERROR"),
            self.assertRaises(session.StackBuildError),
        ):
            self._build(cfg, resolve)
        resolve.assert_called_once()
        self.assertEqual(
            [c[0] for c in self.hw_provision.mock_calls if c[0].startswith("provision_")], []
        )
        self.api.reset.assert_not_called()
        self.api.close.assert_called_once()

    def test_a_run_without_audio_resolves_no_curve(self):
        cfg = cfgmod.Config()
        cfg.scenes = []
        cfg.audio.enabled = False
        resolve = mock.MagicMock()
        with self.assertRaises(session.StackBuildError):
            self._build(cfg, resolve)
        resolve.assert_not_called()


class BuildPreviewAndRecordingTest(unittest.TestCase):
    def test_a_recorder_that_fails_to_start_detaches_the_framebuffer(self):
        # The write listener costs a shadow-memory update on every DMA write for the
        # rest of the run, and nothing else in the tree reads the framebuffer — so
        # preview off plus a bad fourcc must leave nothing registered.
        cfg = cfgmod.Config()
        cfg.preview.enabled = False
        cfg.recording.enabled = True
        api = mock.MagicMock(name="api")
        with (
            mock.patch("c64cast.video.framebuffer.Framebuffer") as fb_cls,
            mock.patch("c64cast.video.preview.StreamRecorder", side_effect=RuntimeError("fourcc")),
        ):
            with self.assertLogs("c64cast", level="ERROR"):
                framebuffer, preview_window, recorder = session._build_preview_and_recording(
                    cfg, api, "a", is_ensemble=False
                )
        self.assertIsNone(framebuffer)
        self.assertIsNone(preview_window)
        self.assertIsNone(recorder)
        on_write = fb_cls.return_value.on_write
        api.add_write_listener.assert_called_once_with(on_write)
        api.remove_write_listener.assert_called_once_with(on_write)


class StartServicesTest(unittest.TestCase):
    def test_a_control_plane_that_refuses_to_start_does_not_kill_the_session(self):
        sess = _session("a")
        sess.cfgs[0].control.enabled = True
        with mock.patch(
            "c64cast.control.control_plane.start_control_server",
            side_effect=RuntimeError("port in use"),
        ):
            with self.assertLogs("c64cast", level="ERROR") as logged:
                session.start_services(sess)  # no raise
        self.assertIn("control plane disabled: port in use", logged.output[0])
        self.assertIsNone(sess.control_server)

    def test_a_non_interactive_session_skips_the_in_session_control_plane(self):
        # A long-lived host serves its own API and is already holding the
        # port; starting a second server on it would collide.
        sess = _session("a", interactive=False)
        sess.cfgs[0].control.enabled = True
        with mock.patch("c64cast.control.control_plane.start_control_server") as start:
            session.start_services(sess)
        start.assert_not_called()


class TeardownSessionTest(unittest.TestCase):
    def test_order_is_inputs_then_servers_then_stacks_reversed(self):
        sess = _session("a", "b")
        order: list[str] = []
        sess.midi_control_listener = mock.MagicMock()
        sess.midi_control_listener.stop.side_effect = lambda: order.append("midi")
        sess.wled_device_server = mock.MagicMock()
        sess.wled_device_server.stop.side_effect = lambda: order.append("wled")
        sess.control_server = mock.MagicMock()
        sess.control_server.stop.side_effect = lambda: order.append("control")
        with mock.patch.object(
            session, "teardown_stack", side_effect=lambda st: order.append(f"stack-{st.name}")
        ):
            session.teardown_session(sess, save_live_tune=False)
        self.assertEqual(order, ["midi", "wled", "control", "stack-b", "stack-a"])

    def test_a_teardown_that_raises_still_tears_down_the_stacks_under_it(self):
        # A second Ctrl+C while system b's teardown runs must not cost a its
        # final reset.
        sess = _session("a", "b")
        torn: list[str] = []

        def teardown(st):
            torn.append(st.name)
            if st.name == "b":
                raise KeyboardInterrupt

        with mock.patch.object(session, "teardown_stack", side_effect=teardown):
            with self.assertRaises(KeyboardInterrupt):
                session.teardown_session(sess, save_live_tune=False)
        self.assertEqual(torn, ["b", "a"])

    def test_the_playlists_are_stopped_and_drained_before_the_stacks(self):
        # teardown_stack closes audio, resets and closes the API. Running that
        # underneath a worker still issuing DMA writes is the mid-DMA cut that wedges
        # the machine into needing a power cycle, and "safe from a finally:" has to
        # cover the escapes where the caller never got to drain them.
        sess = _session("a")
        order: list[str] = []

        def worker() -> None:
            sess.stop_event.wait()
            order.append("thread")

        t = threading.Thread(target=worker, name="playlist-a")
        t.start()
        sess.threads = [t]
        with mock.patch.object(
            session, "teardown_stack", side_effect=lambda st: order.append("stack")
        ):
            session.teardown_session(sess, save_live_tune=False)
        self.assertEqual(order, ["thread", "stack"])
        self.assertFalse(t.is_alive())

    def test_a_failing_server_shutdown_still_reaches_the_stacks(self):
        # The stacks are where the final reset lives. Nothing upstream of it
        # may be allowed to cost the run that reset.
        sess = _session("a")
        sess.control_server = mock.MagicMock()
        sess.control_server.stop.side_effect = RuntimeError("already dead")
        with mock.patch.object(session, "teardown_stack") as td:
            with self.assertLogs("c64cast", level="ERROR"):
                session.teardown_session(sess, save_live_tune=False)
        td.assert_called_once()

    def test_a_failing_live_tune_save_does_not_mask_the_shutdown(self):
        sess = _session("a")
        with (
            mock.patch.object(session, "teardown_stack"),
            mock.patch.object(session, "_maybe_save_live_tune", side_effect=RuntimeError("boom")),
        ):
            with self.assertLogs("c64cast", level="ERROR"):
                session.teardown_session(sess)  # no raise

    def test_a_non_interactive_session_never_reaches_the_save_prompt(self):
        # _maybe_save_live_tune calls input(); on a daemon with a tty that
        # would park the stop path forever.
        sess = _session("a", interactive=False)
        with (
            mock.patch.object(session, "teardown_stack"),
            mock.patch.object(session, "_maybe_save_live_tune") as save,
        ):
            session.teardown_session(sess)
        save.assert_not_called()


class ReloadAllTest(unittest.TestCase):
    def test_a_system_with_no_config_file_is_skipped(self):
        sess = _session("a")  # paths are all None
        with mock.patch.object(session.scene_factory, "scenes_from_config") as sfc:
            session.reload_all(sess)
        sfc.assert_not_called()

    def test_a_bad_reload_keeps_the_current_playlist(self):
        sess = _session("a")
        sess.loaded.paths[0] = "show.toml"
        with (
            mock.patch.object(session.cfgmod, "load", side_effect=cfgmod.ConfigError("bad toml")),
            self.assertLogs("c64cast", level="ERROR"),
        ):
            session.reload_all(sess)
        sess.stacks[0].playlist.request_reload.assert_not_called()


class ReloadPinsReuPumpTest(unittest.TestCase):
    """A reload resolves scenes against the REU pump the running streamer has,
    not against what the files now say: a pump the streamer runs would
    otherwise read as off whenever the files no longer turn it on, and let a
    petscii scene stage through the REU the pump drives."""

    def setUp(self):
        from c64cast.app.scene_factory import _warn_host_rec_staging_dropped

        _warn_host_rec_staging_dropped.cache_clear()
        self.addCleanup(_warn_host_rec_staging_dropped.cache_clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        master = os.path.join(tmp.name, "master.toml")
        with open(master, "w", encoding="utf-8") as f:
            f.write(
                '[ensemble]\nsystems = [{ name = "a", config = "a.toml" }]\n'
                "[audio]\nuse_reu_pump = true\n"
            )
        self.system_toml = os.path.join(tmp.name, "a.toml")
        with open(self.system_toml, "w", encoding="utf-8") as f:
            f.write(
                '[ultimate64]\nurl = "u64://192.0.2.1"\n'
                "[video]\nuse_reu_staged = true\n"
                '[[scenes]]\ntype = "blank"\n'
            )
        self.loaded = cfgmod.load_master(master)
        self.assertTrue(self.loaded.cfgs[0].audio.use_reu_pump)
        stack = fake_system_stack("a")
        stack.audio = mock.MagicMock(spec=AudioStreamer)
        stack.audio.use_reu_pump = True
        stack.reu_available = True
        self.sess = session.Session(
            args=_args(),
            loaded=self.loaded,
            cfgs=self.loaded.cfgs,
            stacks=[stack],
            ensemble=None,
            stop_event=threading.Event(),
            profiler=mock.MagicMock(name="profiler"),
        )

    def _petscii_staged(self, cfg: cfgmod.Config) -> bool:
        wiring = scene_factory.display_wiring_for_scene(
            cfg.scenes[0], cfg, reu_available=True, backend_supports_reu=True
        )
        with self.assertLogs("c64cast.app.scene_factory", level="WARNING"):
            mode = scene_factory.build_wired_display_mode("petscii", wiring)
        return bool(mode.use_reu_staged)

    def test_reload_all_rebuilds_petscii_without_reu_staging(self):
        with mock.patch.object(session.scene_factory, "scenes_from_config", return_value=[]) as sfc:
            session.reload_all(self.sess)
        cfg = sfc.call_args.args[0]
        self.assertTrue(cfg.audio.use_reu_pump)
        self.assertFalse(self._petscii_staged(cfg))

    def test_control_plane_reload_rebuilds_petscii_without_reu_staging(self):
        loaders, _ = session.reload_registries(self.sess)
        with mock.patch.object(session.scene_factory, "scenes_from_config", return_value=[]) as sfc:
            loaders["a"]()
        cfg = sfc.call_args.args[0]
        self.assertTrue(cfg.audio.use_reu_pump)
        self.assertFalse(self._petscii_staged(cfg))

    def _system_file_turns_the_pump_on(self):
        with open(self.system_toml, "a", encoding="utf-8") as f:
            f.write("[audio]\nuse_reu_pump = true\n")

    def test_no_streamer_pins_the_pump_off(self):
        self._system_file_turns_the_pump_on()
        self.sess.stacks[0].audio = None
        with mock.patch.object(session.scene_factory, "scenes_from_config", return_value=[]) as sfc:
            session.reload_all(self.sess)
        self.assertFalse(sfc.call_args.args[0].audio.use_reu_pump)

    def test_streamer_without_the_pump_pins_it_off(self):
        self._system_file_turns_the_pump_on()
        self.sess.stacks[0].audio.use_reu_pump = False
        with mock.patch.object(session.scene_factory, "scenes_from_config", return_value=[]) as sfc:
            session.reload_all(self.sess)
        self.assertFalse(sfc.call_args.args[0].audio.use_reu_pump)


class ReloadComposesLikeStartupTest(unittest.TestCase):
    """A reload composes each system's Config the way startup did (#557): the
    master's cascaded defaults and the backend coercion are re-applied, not
    just the system file plus the CLI."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def _write(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _session_for(self, config_path: str) -> session.Session:
        loaded = cfgmod.load_master(config_path)
        return session.Session(
            args=_args(),
            loaded=loaded,
            cfgs=loaded.cfgs,
            stacks=[fake_system_stack(n) for n in loaded.names],
            ensemble=None,
            stop_event=threading.Event(),
            profiler=mock.MagicMock(name="profiler"),
        )

    def _ensemble(self, master_sections: str) -> session.Session:
        master = self._write(
            "master.toml",
            '[ensemble]\nsystems = [{ name = "a", config = "a.toml" }]\n' + master_sections,
        )
        self._write("a.toml", '[ultimate64]\nurl = "u64://192.0.2.1"\n[[scenes]]\ntype = "blank"\n')
        return self._session_for(master)

    def _reloaded_cfg(self, sess: session.Session) -> cfgmod.Config:
        with (
            mock.patch.object(session.scene_factory, "scenes_from_config", return_value=[]) as sfc,
            mock.patch.object(session, "interstitial_factory"),
        ):
            session.reload_all(sess)
        return sfc.call_args.args[0]

    def test_a_master_playlist_loop_survives_a_reload(self):
        # `loop` defaults to true, so only a master that turns it off can tell
        # an inherited value from a reverted one.
        sess = self._ensemble("[playlist]\nloop = false\n")
        self.assertFalse(sess.cfgs[0].playlist.loop)
        self.assertFalse(self._reloaded_cfg(sess).playlist.loop)

    def test_a_master_color_setting_survives_a_reload(self):
        sess = self._ensemble("[color]\nauto_fit = false\n")
        self.assertFalse(sess.cfgs[0].color.auto_fit)
        self.assertFalse(self._reloaded_cfg(sess).color.auto_fit)

    def test_the_control_plane_reload_keeps_the_master_interstitial(self):
        sess = self._ensemble("[interstitial]\nduration_s = 2.5\n")
        _, factories = session.reload_registries(sess)
        with mock.patch.object(session, "interstitial_factory") as factory:
            factories["a"]()
        self.assertEqual(factory.call_args.args[1].duration_s, 2.5)

    def _teensyrom_staged(self) -> session.Session:
        path = self._write(
            "tr.toml",
            '[hardware]\nbackend = "teensyrom"\n'
            "[video]\nuse_reu_staged = true\n"
            '[[scenes]]\ntype = "blank"\n',
        )
        sess = self._session_for(path)
        sess.stacks[0].api.profile.supports_reu = False
        return sess

    def test_a_teensyrom_reload_keeps_reu_staging_off(self):
        sess = self._teensyrom_staged()
        with self.assertLogs("c64cast", level="WARNING") as logs:
            cfg = self._reloaded_cfg(sess)
        self.assertIs(cfg.video.use_reu_staged, False)
        self.assertIn("use_reu_staged", "\n".join(logs.output))

    def test_a_teensyrom_control_plane_reload_keeps_reu_staging_off(self):
        sess = self._teensyrom_staged()
        loaders, _ = session.reload_registries(sess)
        with (
            mock.patch.object(session.scene_factory, "scenes_from_config", return_value=[]) as sfc,
            self.assertLogs("c64cast", level="WARNING"),
        ):
            loaders["system"]()
        self.assertIs(sfc.call_args.args[0].video.use_reu_staged, False)


if __name__ == "__main__":
    unittest.main()
