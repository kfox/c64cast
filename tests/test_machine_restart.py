"""A machine that restarts under a running scene loses what the scene's setup
put there, and the run's live+volatile configuration. At the socket that
looks like a pulled cable, so a nonce in RAM a reset zeroes tells the two
apart, and the scene is set up again (c64cast#599)."""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from test_playlist import FakeApi, FakeScene, _transition_factory

from c64cast.app import session
from c64cast.app.playlist import Playlist
from c64cast.app.playlist_support import (
    RESTART_CHECK_MIN_S,
    RESTART_SENTINEL_ADDR,
    RESTART_SENTINEL_LEN,
    MachineRestartWatch,
)

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

        def setup() -> None:
            original_setup()
            order.append(f"setup{scene.setup_count}")
            if scene.setup_count == 2:
                threading.Timer(0.05, stop.set).start()

        scene.setup = setup  # type: ignore[method-assign]
        with self.assertLogs("c64cast.app.playlist", level="INFO") as logs:
            pl.run()
        self.assertEqual(order, ["setup1", "restore@0", "setup2"])
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
        pl.current = FakeScene("trans:Next")
        pl.transitioning = True
        with (
            patch.object(pl, "safe_setup") as setup,
            self.assertLogs("c64cast.app.playlist", level="WARNING"),
        ):
            pl._set_up_again_after_restart()
        self.assertIs(setup.call_args.kwargs["announcing"], nxt)


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


if __name__ == "__main__":
    unittest.main()
