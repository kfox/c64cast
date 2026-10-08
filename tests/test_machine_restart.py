"""A machine that restarts under a running scene loses what the scene's setup
put there, and the run's live+volatile configuration. At the socket that
looks like a pulled cable, so a nonce in RAM a reset zeroes tells the two
apart, and the scene is set up again (c64cast#599)."""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import ast
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from test_playlist import FakeApi, FakeScene, _transition_factory

from c64cast.app import config as cfgmod
from c64cast.app import session
from c64cast.app.playlist import Playlist
from c64cast.app.playlist_support import (
    RESTART_CHECK_MIN_S,
    RESTART_SENTINEL_ADDR,
    RESTART_SENTINEL_LEN,
    MachineRestartWatch,
)
from c64cast.hw.api import Ultimate64API
from c64cast.hw.backend import LinkError

_SENTINEL = slice(RESTART_SENTINEL_ADDR, RESTART_SENTINEL_ADDR + RESTART_SENTINEL_LEN)


class _Machine(FakeApi):
    """A FakeApi with 64 KB of RAM, REST reads, and reset listeners, whose
    machine can restart (page 3 zeroed, the link redialed)."""

    def __init__(self) -> None:
        super().__init__()
        self.profile = SimpleNamespace(supports_read=True)
        self.ram = bytearray(0x10000)
        self.link_generation = 0
        self.reads = 0
        self.rest_down = False
        self.reset_listeners: list[Any] = []
        # A write the link drops: moves the epoch and lands nothing.
        self.drop_writes = False

    def add_reset_listener(self, callback: Any) -> None:
        self.reset_listeners.append(callback)

    def write_memory_file(self, address: str, data: bytes) -> None:
        if self.drop_writes:
            self.delivery_epoch += 1
            self.stats["errors"] += 1
            return
        addr = int(address, 16)
        self.ram[addr : addr + len(data)] = data
        self.stats["writes"] += 1

    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        self.reads += 1
        if self.rest_down:
            return None
        return bytes(self.ram[address : address + length])

    def restart(self) -> None:
        """The machine reboots: RAMTAS zeroes page 3, and the next command
        reaches it over a new connection."""
        self.ram[0x0002:0x0400] = bytes(0x03FE)
        self.link_generation += 1

    def c64cast_reset(self) -> None:
        """A reset c64cast issues itself (a SID scene's run_prg)."""
        self.ram[0x0002:0x0400] = bytes(0x03FE)
        for callback in self.reset_listeners:
            callback()


class MachineRestartWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _Machine()
        self.now = [100.0]
        self.watch = MachineRestartWatch(self.api, MagicMock(), lambda: self.now[0])
        self.watch.arm()

    def test_arm_writes_a_nonzero_nonce_where_a_reset_zeroes_it(self):
        nonce = bytes(self.api.ram[_SENTINEL])
        self.assertEqual(len(nonce), RESTART_SENTINEL_LEN)
        self.assertNotIn(0, nonce)

    def test_nothing_is_read_while_the_link_has_not_changed(self):
        for _ in range(5):
            self.assertFalse(self.watch.after_frame(True))
        self.assertEqual(self.api.reads, 0)

    def test_a_redial_to_the_same_machine_reads_once_and_is_not_a_restart(self):
        self.api.link_generation += 1
        self.assertFalse(self.watch.after_frame(True))
        self.now[0] += RESTART_CHECK_MIN_S
        self.assertFalse(self.watch.after_frame(True))
        self.assertEqual(self.api.reads, 1)

    def test_a_lost_write_also_prompts_a_look(self):
        self.api.delivery_epoch += 1
        self.assertFalse(self.watch.after_frame(True))
        self.assertEqual(self.api.reads, 1)

    def test_a_restart_is_reported_once(self):
        self.api.restart()
        self.assertTrue(self.watch.after_frame(True))
        self.now[0] += RESTART_CHECK_MIN_S
        self.api.link_generation += 1
        self.assertFalse(self.watch.after_frame(True), "a disarmed watch reported again")

    def test_no_look_until_a_frame_lands(self):
        self.api.restart()
        self.assertFalse(self.watch.after_frame(False))
        self.assertEqual(self.api.reads, 0)
        self.assertTrue(self.watch.after_frame(True))

    def test_an_unanswered_read_is_tried_again_later_not_taken_as_a_restart(self):
        self.api.restart()
        self.api.rest_down = True
        self.assertFalse(self.watch.after_frame(True))
        self.assertFalse(self.watch.after_frame(True), "read again before the interval")
        self.assertEqual(self.api.reads, 1)
        self.api.rest_down = False
        self.now[0] += RESTART_CHECK_MIN_S
        self.assertTrue(self.watch.after_frame(True))

    def test_looks_are_spaced_while_the_link_keeps_changing(self):
        for _ in range(5):
            self.api.link_generation += 1
            self.watch.after_frame(True)
            self.now[0] += RESTART_CHECK_MIN_S / 4
        self.assertEqual(self.api.reads, 2)

    def test_a_reset_c64cast_issued_rearms_instead_of_reporting(self):
        self.api.c64cast_reset()
        self.api.link_generation += 1
        self.assertFalse(self.watch.after_frame(True))
        self.assertNotIn(0, bytes(self.api.ram[_SENTINEL]), "the nonce was not written again")
        self.now[0] += RESTART_CHECK_MIN_S
        self.api.link_generation += 1
        self.assertFalse(self.watch.after_frame(True))

    def test_a_nonce_the_link_lost_is_never_read_back_as_a_restart(self):
        api = _Machine()
        api.drop_writes = True
        watch = MachineRestartWatch(api, MagicMock(), lambda: self.now[0])
        watch.arm()
        api.drop_writes = False
        api.link_generation += 1
        self.assertFalse(watch.after_frame(True))
        self.assertEqual(api.reads, 0)

    def test_bytes_written_over_the_nonce_are_not_a_restart(self):
        self.api.ram[_SENTINEL] = bytes(range(1, RESTART_SENTINEL_LEN + 1))
        self.api.link_generation += 1
        self.assertFalse(self.watch.after_frame(True))
        self.watch._log.warning.assert_called_once()
        self.now[0] += RESTART_CHECK_MIN_S
        self.api.link_generation += 1
        self.assertFalse(self.watch.after_frame(True))
        self.assertFalse(self.watch.restarted_before_setup())
        self.assertEqual(self.api.reads, 1, "an overwritten nonce is looked at again")

    def test_a_read_of_the_wrong_length_is_tried_again_not_taken_as_a_restart(self):
        self.api.restart()
        real_read = self.api.read_memory
        self.api.read_memory = lambda address, length, timeout=1.0: real_read(
            address, length - 1, timeout
        )
        self.assertFalse(self.watch.after_frame(True))
        self.api.read_memory = real_read
        self.now[0] += RESTART_CHECK_MIN_S
        self.assertTrue(self.watch.after_frame(True))

    def test_before_setup_a_restart_is_found_without_waiting_for_a_landed_frame(self):
        self.api.restart()
        self.assertTrue(self.watch.restarted_before_setup())
        self.assertFalse(self.watch.restarted_before_setup(), "a disarmed watch reported again")

    def test_before_setup_nothing_is_read_while_the_link_has_not_changed(self):
        self.assertFalse(self.watch.restarted_before_setup())
        self.assertEqual(self.api.reads, 0)

    def test_before_setup_an_unanswered_read_leaves_the_watch_armed(self):
        self.api.restart()
        self.api.rest_down = True
        self.assertFalse(self.watch.restarted_before_setup())
        self.api.rest_down = False
        self.assertTrue(self.watch.restarted_before_setup())

    def test_before_setup_a_reset_c64cast_issued_is_not_a_restart(self):
        self.api.c64cast_reset()
        self.api.link_generation += 1
        self.assertFalse(self.watch.restarted_before_setup())
        self.assertEqual(self.api.reads, 0)

    def test_a_suspended_watch_neither_writes_nor_reads_until_armed_again(self):
        program = bytes(range(1, 9))
        self.watch.suspend()
        self.api.c64cast_reset()
        self.api.ram[_SENTINEL] = program
        self.api.link_generation += 1
        self.assertFalse(self.watch.after_frame(True))
        self.assertFalse(self.watch.restarted_before_setup())
        self.assertEqual(bytes(self.api.ram[_SENTINEL]), program)
        self.assertEqual(self.api.reads, 0)
        self.watch.arm()
        self.assertNotEqual(bytes(self.api.ram[_SENTINEL]), program)


class WatchEnabledTest(unittest.TestCase):
    def test_a_backend_that_cannot_read_or_report_its_resets_is_not_watched(self):
        no_read = _Machine()
        no_read.profile = SimpleNamespace(supports_read=False)
        no_listener = FakeApi()
        no_listener.profile = SimpleNamespace(supports_read=True)
        for api in (no_read, no_listener, FakeApi(), MagicMock()):
            watch = MachineRestartWatch(api, MagicMock())
            self.assertFalse(watch.enabled, api)
            watch.arm()
            self.assertFalse(watch.after_frame(True))
        self.assertEqual(no_read.reads, 0)
        self.assertEqual(no_read.stats["writes"], 0)


class _PaintingScene(FakeScene):
    """A scene whose every frame lands a write; the machine restarts at
    frame `restart_at` of its first setup."""

    def __init__(self, api: _Machine, restart_at: int) -> None:
        super().__init__("Video", frames_until_done=10_000)
        self.api = api
        self.restart_at = restart_at
        self.frames_by_setup: dict[int, int] = {}

    def process_frame(self, current_time: float) -> bool:
        super().process_frame(current_time)
        n = self.frames_by_setup.get(self.setup_count, 0) + 1
        self.frames_by_setup[self.setup_count] = n
        if self.setup_count == 1 and n == self.restart_at:
            self.api.restart()
        self.api.stats["writes"] += 1
        return True


class PlaylistSetsUpAgainAfterRestartTest(unittest.TestCase):
    def test_the_scene_is_set_up_again_after_the_machine_state_is_put_back(self):
        api = _Machine()
        scene = _PaintingScene(api, restart_at=3)
        stop = threading.Event()
        pl = Playlist(
            [scene],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=_transition_factory()[0],
        )
        order: list[str] = []
        pl.on_machine_restart = lambda: order.append(f"restore@{scene.teardown_count}")

        original_setup = scene.setup
        original_teardown = scene.teardown

        def setup() -> None:
            original_setup()
            order.append(f"setup{scene.setup_count}")
            if scene.setup_count == 2:
                threading.Timer(0.05, stop.set).start()

        def teardown() -> None:
            original_teardown()
            order.append("teardown")

        scene.setup = setup  # type: ignore[method-assign]
        scene.teardown = teardown  # type: ignore[method-assign]
        with self.assertLogs("c64cast.app.playlist", level="WARNING") as logs:
            pl.run()
        self.assertEqual(order[:4], ["setup1", "restore@0", "teardown", "setup2"])
        self.assertEqual(scene.keep_pick_count, 1, "the restart rolled a new pick")
        self.assertEqual(scene.frames_by_setup[1], 3)
        self.assertGreater(scene.frames_by_setup.get(2, 0), 0, "the scene never played again")
        warnings = [line for line in logs.output if "machine restarted" in line]
        self.assertEqual(len(warnings), 1, logs.output)
        self.assertIn("'Video'", warnings[0])
        self.assertNotIn(0, bytes(api.ram[_SENTINEL]), "the second setup did not re-arm")


class _OutlivedRestartScene(FakeScene):
    """A scene the machine restarts under at frame `restart_at`, whose time
    runs out while the link is still down: `_emit` swallows the failures, so
    no frame after the restart raises or lands a write."""

    def __init__(self, api: _Machine, restart_at: int, frames: int) -> None:
        super().__init__("Video", frames_until_done=frames)
        self.api = api
        self.restart_at = restart_at

    def process_frame(self, current_time: float) -> bool:
        still_active = super().process_frame(current_time)
        if self.frame_count == self.restart_at:
            self.api.restart()
        if self.frame_count < self.restart_at:
            self.api.stats["writes"] += 1
        else:
            self.api.stats["errors"] += 1
            self.api.delivery_epoch += 1
        return still_active


class _StopOnSetup(FakeScene):
    def __init__(self, name: str, stop: threading.Event) -> None:
        super().__init__(name, frames_until_done=10_000)
        self.stop = stop

    def setup(self) -> None:
        super().setup()
        self.stop.set()


class RestartBeforeTheNextSetupTest(unittest.TestCase):
    def test_a_restart_the_scene_outlived_is_put_back_before_the_next_setup(self):
        api = _Machine()
        stop = threading.Event()
        pl = Playlist(
            [_OutlivedRestartScene(api, restart_at=3, frames=10), _StopOnSetup("Next", stop)],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=_transition_factory()[0],
            loop=False,
        )
        restores: list[int] = []
        pl.on_machine_restart = lambda: restores.append(1)
        with self.assertLogs("c64cast.app.playlist", level="WARNING") as logs:
            pl.run()
        self.assertEqual(len(restores), 1, "the restart went unnoticed")
        warnings = [line for line in logs.output if "machine restarted" in line]
        self.assertEqual(len(warnings), 1, logs.output)
        self.assertIn("trans:Next", warnings[0])


class _Launcher(FakeScene):
    """Like LauncherScene: its setup runs a program of the user's, here one
    living at $0334, through a runner that resets the machine."""

    HANDS_OVER_MACHINE = True
    PROGRAM = bytes(range(0xA0, 0xA8))

    def __init__(self, api: _Machine, stop: threading.Event) -> None:
        super().__init__("Launcher", frames_until_done=10_000)
        self.api = api
        self.stop = stop

    def setup(self) -> None:
        super().setup()
        self.api.ram[_SENTINEL] = self.PROGRAM
        for callback in self.api.reset_listeners:
            callback()

    def process_frame(self, current_time: float) -> bool:
        super().process_frame(current_time)
        self.api.stats["writes"] += 1
        self.api.link_generation += 1
        if self.frame_count == 5:
            self.stop.set()
        return True


class LauncherProgramUntouchedTest(unittest.TestCase):
    def test_a_launched_program_keeps_its_bytes_and_is_never_taken_for_a_restart(self):
        api = _Machine()
        stop = threading.Event()
        pl = Playlist(
            [_Launcher(api, stop)],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop,
        )
        restores: list[int] = []
        pl.on_machine_restart = lambda: restores.append(1)
        with self.assertNoLogs("c64cast.app.playlist", level="WARNING"):
            pl.run()
        self.assertEqual(bytes(api.ram[_SENTINEL]), _Launcher.PROGRAM)
        self.assertEqual(api.reads, 0)
        self.assertEqual(restores, [])


def _run_restart_show(test: unittest.TestCase, on_restart: Any) -> tuple[_PaintingScene, list[str]]:
    """Run a one-scene playlist whose machine restarts at frame 3, until the
    scene has been set up a second time; returns the scene and the WARNING+
    log lines."""
    api = _Machine()
    scene = _PaintingScene(api, restart_at=3)
    stop = threading.Event()
    pl = Playlist(
        [scene],
        api,
        target_fps=10000.0,
        heartbeat_interval=0.0,
        stop_event=stop,
        interstitial_factory=_transition_factory()[0],
    )
    pl.on_machine_restart = on_restart
    original_setup = scene.setup

    def setup() -> None:
        original_setup()
        if scene.setup_count == 2:
            threading.Timer(0.05, stop.set).start()

    scene.setup = setup  # type: ignore[method-assign]
    with test.assertLogs("c64cast.app.playlist", level="WARNING") as logs:
        pl.run()
    return scene, logs.output


class RestoreFailureTest(unittest.TestCase):
    def test_a_restore_that_raises_is_logged_and_the_scene_is_set_up_again(self):
        def broken() -> None:
            raise RuntimeError("provisioning blew up")

        scene, lines = _run_restart_show(self, broken)
        self.assertGreaterEqual(scene.setup_count, 2, "a failed restore stranded the scene")
        self.assertTrue(any("restoring the machine's state" in line for line in lines), lines)


class _RestartThenNoFrameLands(FakeScene):
    """Frames 1-2 land writes; the machine restarts at frame 3, and every
    frame from then on lands nothing (`mode` "no_write") or lands a write and
    then raises `LinkError` ("link_error")."""

    def __init__(self, api: _Machine, stop: threading.Event, mode: str) -> None:
        super().__init__("Video", frames_until_done=10_000)
        self.api = api
        self.stop = stop
        self.mode = mode

    def process_frame(self, current_time: float) -> bool:
        super().process_frame(current_time)
        if self.frame_count < 3:
            self.api.stats["writes"] += 1
            return True
        if self.frame_count == 3:
            self.api.restart()
        if self.frame_count == 8:
            self.stop.set()
        if self.mode == "link_error":
            self.api.stats["writes"] += 1
            raise LinkError("down")
        return True


class FrameMustLandBeforeALookTest(unittest.TestCase):
    def _run(self, mode: str) -> tuple[_Machine, _RestartThenNoFrameLands]:
        api = _Machine()
        stop = threading.Event()
        scene = _RestartThenNoFrameLands(api, stop, mode)
        pl = Playlist(
            [scene],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            stop_event=stop,
            interstitial_factory=_transition_factory()[0],
        )
        pl.on_machine_restart = lambda: None
        if mode == "link_error":
            with self.assertLogs("c64cast", level="WARNING"):
                pl.run()
        else:
            with self.assertNoLogs("c64cast", level="WARNING"):
                pl.run()
        return api, scene

    def test_a_frame_that_raised_a_link_error_is_not_looked_after(self):
        api, scene = self._run("link_error")
        self.assertEqual(api.reads, 0)
        self.assertEqual(scene.setup_count, 1)

    def test_a_frame_that_landed_nothing_is_not_looked_after(self):
        api, scene = self._run("no_write")
        self.assertEqual(api.reads, 0)
        self.assertEqual(scene.setup_count, 1)


class SetUpAgainBranchesTest(unittest.TestCase):
    def _playlist(self, scene: FakeScene) -> Playlist:
        pl = Playlist([scene], _Machine(), target_fps=10000.0, heartbeat_interval=0.0)
        pl.current = scene
        return pl

    def test_a_refused_audio_claim_leaves_no_current_scene_and_no_setup(self):
        scene = FakeScene("Video")
        pl = self._playlist(scene)
        with (
            patch.object(pl.ensemble_coord, "wait_for_audio_claim", return_value=False),
            patch.object(pl, "safe_setup") as setup,
            self.assertLogs("c64cast.app.playlist", level="WARNING"),
        ):
            pl._set_up_again_after_restart()
        self.assertIsNone(pl.current)
        setup.assert_not_called()

    def test_the_scene_is_not_done_once_it_is_set_up_again(self):
        scene = FakeScene("Video")
        scene.is_done = True
        pl = self._playlist(scene)
        with (
            patch.object(pl, "safe_setup") as setup,
            self.assertLogs("c64cast.app.playlist", level="WARNING"),
        ):
            pl._set_up_again_after_restart()
        setup.assert_called_once()
        self.assertFalse(scene.is_done)


class LinkGenerationTest(unittest.TestCase):
    def test_the_ultimate_follows_its_dma_clients_redial_count(self):
        with patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True):
            api = Ultimate64API("http://example.invalid")
        self.assertEqual(api.link_generation, 0)
        api.socket_dma.reconnect_count = 3
        self.assertEqual(api.link_generation, 3)
        with patch.object(api.socket_dma, "close"):
            api.close()


class InterstitialResetupTest(unittest.TestCase):
    def test_a_card_set_up_again_names_the_scene_it_announces(self):
        api = _Machine()
        nxt = FakeScene("Next")
        pl = Playlist(
            [FakeScene("First"), nxt],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            interstitial_factory=_transition_factory()[0],
        )
        pl.index = 1
        pl.current = pl._card = FakeScene("trans:Next")
        pl.transitioning = True
        with (
            patch.object(pl, "safe_setup") as setup,
            self.assertLogs("c64cast.app.playlist", level="WARNING"),
        ):
            pl._set_up_again_after_restart()
        self.assertIs(setup.call_args.kwargs["announcing"], nxt)

    def test_a_clip_launched_over_the_card_announces_nothing(self):
        api = _Machine()
        pl = Playlist(
            [FakeScene("First"), FakeScene("Next")],
            api,
            target_fps=10000.0,
            heartbeat_interval=0.0,
            interstitial_factory=_transition_factory()[0],
        )
        pl.index = 1
        pl._card = FakeScene("trans:Next")
        pl.current = FakeScene("Clip")
        pl.transitioning = True
        with (
            patch.object(pl, "safe_setup") as setup,
            self.assertLogs("c64cast.app.playlist", level="WARNING"),
        ):
            pl._set_up_again_after_restart()
        self.assertIsNone(setup.call_args.kwargs["announcing"])


class RestoreAfterMachineRestartTest(unittest.TestCase):
    def test_every_step_runs_in_order_and_one_failing_does_not_stop_the_rest(self):
        calls: list[str] = []
        api = MagicMock()
        api.profile.supports_sid_config = True
        api.run_basic_clear_loop.side_effect = lambda: calls.append("clear loop")
        api.disable_case_switch.side_effect = lambda: calls.append("case switch")

        def step(name: str, fail: bool = False) -> Any:
            def run(*_a: Any) -> None:
                calls.append(name)
                if fail:
                    raise RuntimeError(f"{name} failed")

            return run

        curve = object()
        with (
            patch.object(session.hw_provision, "provision_reu", side_effect=step("reu")),
            patch.object(
                session.hw_provision, "provision_sampler", side_effect=step("sampler", True)
            ),
            patch.object(
                session.hw_provision, "provision_master_volume", side_effect=step("master")
            ),
            patch.object(session.hw_provision, "provision_video_output", side_effect=step("video")),
            patch.object(
                session.dac_curve_resolve,
                "provision_calibrated_chip_model",
                side_effect=step("dac"),
            ) as dac,
            self.assertLogs("c64cast", level="ERROR") as logs,
        ):
            session._restore_after_machine_restart(MagicMock(), api, curve)
        self.assertEqual(
            calls, ["reu", "sampler", "master", "video", "dac", "clear loop", "case switch"]
        )
        self.assertIs(dac.call_args.args[1], curve)
        self.assertIn("sampler", logs.output[0])

    def test_no_dac_curve_skips_the_chip_model(self):
        api = MagicMock()
        with (
            patch.object(session.hw_provision, "provision_reu"),
            patch.object(session.hw_provision, "provision_sampler"),
            patch.object(session.hw_provision, "provision_master_volume"),
            patch.object(session.hw_provision, "provision_video_output"),
            patch.object(session.dac_curve_resolve, "provision_calibrated_chip_model") as dac,
        ):
            session._restore_after_machine_restart(MagicMock(), api, None)
        dac.assert_not_called()
        api.run_basic_clear_loop.assert_called_once_with()

    def test_a_chip_model_the_backend_cannot_set_is_skipped(self):
        api = MagicMock()
        api.profile.supports_sid_config = False
        with (
            patch.object(session.hw_provision, "provision_reu"),
            patch.object(session.hw_provision, "provision_sampler"),
            patch.object(session.hw_provision, "provision_master_volume"),
            patch.object(session.hw_provision, "provision_video_output"),
            patch.object(session.dac_curve_resolve, "provision_calibrated_chip_model") as dac,
        ):
            session._restore_after_machine_restart(MagicMock(), api, object())
        dac.assert_not_called()


class StackWiringTest(unittest.TestCase):
    def test_the_playlist_restores_with_the_runs_config_backend_and_dac_curve(self):
        cfg = cfgmod.Config()
        cfg.scenes = []
        api = MagicMock(name="api")
        api.profile.max_fps = None
        api.profile.default_fps = 50.0
        api.read_menu_screen.return_value = None
        curve = object()
        with (
            patch.object(session, "_open_backend", return_value=api),
            patch.object(session, "hw_provision"),
            patch.object(session, "_build_audio", return_value=MagicMock(name="audio")),
            patch.object(
                session.dac_curve_resolve, "resolve_dac_curve_for_backend", return_value=curve
            ),
            patch.object(session.dac_curve_resolve, "provision_calibrated_chip_model"),
            patch.object(session, "_resolve_reu_available", return_value=False),
            patch.object(session, "_resolve_sampler_available", return_value=False),
            patch.object(
                session.scene_factory, "scenes_from_config", return_value=[FakeScene("Video")]
            ),
            patch.object(session.char_rom, "ensure_installed"),
            patch.object(session.time, "sleep"),
            patch.object(session.hardware_palette, "provision_hardware_palette"),
            patch.object(session, "_build_input_controls", return_value=(None, None)),
            patch.object(session, "_build_preview_and_recording", return_value=(None, None, None)),
            patch.object(session, "interstitial_factory"),
            patch.object(session, "_performance_scene_factory"),
            patch.object(session, "_restore_after_machine_restart") as restore,
        ):
            stack = session.build_stack(
                cfg, "a", stop_event=threading.Event(), profiler=MagicMock(name="profiler")
            )
            restore_hook = stack.playlist.on_machine_restart
            assert restore_hook is not None, "the playlist was left without a restart hook"
            restore_hook()
        restore.assert_called_once_with(cfg, api, curve)


class RestoreMirrorsStartupTest(unittest.TestCase):
    """The restore repeats the startup provisioning by hand, so a provisioner
    added to `_acquire_stack` and not to `_restore_after_machine_restart`
    would leave a restarted machine without it."""

    @staticmethod
    def _provisioners(function: str) -> set[str]:
        tree = ast.parse(Path(session.__file__).read_text(encoding="utf-8"))
        (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function]
        return {
            f"{call.func.value.id}.{call.func.attr}"
            for call in ast.walk(fn)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in ("hw_provision", "dac_curve_resolve")
            and call.func.attr.startswith("provision_")
        }

    def test_the_restore_calls_every_provisioner_the_startup_does(self):
        startup = self._provisioners("_acquire_stack")
        self.assertIn("hw_provision.provision_reu", startup, "the sweep found nothing to compare")
        self.assertEqual(startup, self._provisioners("_restore_after_machine_restart"))


if __name__ == "__main__":
    unittest.main()
