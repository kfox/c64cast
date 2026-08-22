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
import threading
import unittest
from unittest import mock

from _fakes import fake_system_stack

from c64cast.app import config as cfgmod
from c64cast.app import session


def _loaded(names: list[str], *, is_ensemble: bool = False) -> cfgmod.LoadResult:
    # Audio off by default: validate_configs rejects an audio-enabled config
    # outright when sounddevice is missing, which would otherwise make every
    # assertion below depend on whether the 'mic' extra is installed.
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
            "_run_playlists",
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
        with mock.patch.object(
            session.scene_factory,
            "validate_dither_cfg",
            side_effect=cfgmod.ConfigError("bad dither"),
        ):
            with self.assertLogs("c64cast", level="ERROR") as logged:
                with self.assertRaises(session.SessionConfigError) as cm:
                    session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 5)
        self.assertIn("bad dither", logged.output[0])
        self.assertEqual(cm.exception.detail, "bad dither")

    def test_an_open_control_plane_on_a_network_host_is_exit_5(self):
        # The gate has to be here, not at bind time: start_services runs after
        # the hardware is up, so a warning there arrives with a show already
        # on screen.
        loaded = _loaded(["a"])
        loaded.master_control.enabled = True
        loaded.master_control.host = "0.0.0.0"
        with self.assertLogs("c64cast", level="ERROR") as logged:
            with self.assertRaises(session.SessionConfigError) as cm:
                session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 5)
        self.assertIn("allow_unauthenticated", logged.output[0])

    def test_clean_configs_pass(self):
        loaded = _loaded(["a", "b"])
        session.validate_configs(loaded, loaded.cfgs)  # no raise

    def test_a_bad_scene_is_exit_3_before_any_hardware_is_opened(self):
        # Exit 3 is what build_stack returns for the same error once
        # scenes_from_config reaches it, so moving the check earlier keeps the
        # CLI's answer to a bad scene identical.
        loaded = _loaded(["a"])
        loaded.cfgs[0].scenes = [cfgmod.SceneCfg(type="video", duration_s=5.0)]
        with self.assertLogs("c64cast", level="ERROR"):
            with self.assertRaises(session.SessionConfigError) as cm:
                session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 3)

    def test_the_diagnostic_names_the_scene_that_failed(self):
        loaded = _loaded(["a"])
        loaded.cfgs[0].scenes = [
            cfgmod.SceneCfg(type="blank"),
            cfgmod.SceneCfg(type="video", name="outro", duration_s=5.0),
        ]
        with self.assertLogs("c64cast", level="ERROR") as logged:
            with self.assertRaises(session.SessionConfigError) as cm:
                session.validate_configs(loaded, loaded.cfgs)
        self.assertIn("outro", logged.output[0])
        self.assertIn("outro", cm.exception.detail)

    def test_a_follower_only_scene_is_validated_too(self):
        # It is built lazily at broadcast time, so a bad one would otherwise
        # surface mid-show rather than before the run.
        loaded = _loaded(["a"])
        loaded.cfgs[0].scenes = [cfgmod.SceneCfg(type="video", follower_only=True, duration_s=5.0)]
        with self.assertLogs("c64cast", level="ERROR"):
            with self.assertRaises(session.SessionConfigError):
                session.validate_configs(loaded, loaded.cfgs)

    def test_a_bad_scene_force_palette_override_is_exit_5_not_an_unhandled_error(self):
        # force_palette_colors is range-checked by scene_color(), which raises
        # a plain ValueError — it must surface as SessionConfigError (caught
        # by cli.py's ConfigError-only handler around validate_configs), not
        # escape as an unhandled exception.
        loaded = _loaded(["a"])
        loaded.cfgs[0].scenes = [cfgmod.SceneCfg(type="video", color={"force_palette_colors": 999})]
        with self.assertLogs("c64cast", level="ERROR") as logged:
            with self.assertRaises(session.SessionConfigError) as cm:
                session.validate_configs(loaded, loaded.cfgs)
        self.assertEqual(cm.exception.exit_code, 5)
        self.assertIn("force_palette_colors", logged.output[0])

    def test_transport_coercion_runs_before_any_stack_is_built(self):
        # [audio].use_reu_pump has no seek/splice support, so a transport.*
        # MIDI mapping must force it off — and it has to happen here, because
        # build_stack bakes the flag into the AudioStreamer constructor.
        loaded = _loaded(["a"])
        loaded.cfgs[0].audio.use_reu_pump = True
        loaded.master_midi_control.enabled = True
        loaded.master_midi_control.cc_map = [{"cc": 1, "action": "transport.seek"}]
        session.validate_configs(loaded, loaded.cfgs)
        self.assertFalse(loaded.cfgs[0].audio.use_reu_pump)


class BuildSessionTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
