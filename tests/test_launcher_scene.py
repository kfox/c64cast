"""Tests for LauncherScene's idle-timeout state machine and input
snapshot logic. Pure-Python: the api is a Mock; no hardware is touched."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from c64cast.hw import machine_input
from c64cast.hw.c64 import CIA1
from c64cast.scenes.scenes import LauncherScene


def _make_scene(tmp, **kwargs):
    p = os.path.join(tmp, "demo.prg")
    with open(p, "wb") as f:
        f.write(b"\x01\x08")
    api = MagicMock()
    scene = LauncherScene(api, p, **kwargs)
    return scene, api


class IdleTimeoutTest(unittest.TestCase):
    def test_idle_advance_when_input_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = _make_scene(tmp)
            scene.duration_s = 10.0
            scene.start_time = 0.0
            scene._last_input_t = 0.0
            # 11 s since last input > 10 s idle timeout → advance.
            self.assertFalse(scene.process_frame(11.0))

    def test_stay_when_input_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = _make_scene(tmp)
            scene.duration_s = 10.0
            scene.start_time = 0.0
            scene._last_input_t = 8.0  # input 3 s ago at t=11
            self.assertTrue(scene.process_frame(11.0))

    def test_min_duration_floor_blocks_early_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = _make_scene(tmp, min_duration_s=30.0)
            scene.duration_s = 5.0
            scene.start_time = 0.0
            scene._last_input_t = 0.0  # idle the whole time
            # Idle timeout would fire at t=5, but the floor holds until t=30.
            self.assertTrue(scene.process_frame(10.0))
            self.assertFalse(scene.process_frame(31.0))

    def test_max_duration_ceiling_advances_despite_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = _make_scene(tmp, max_duration_s=20.0)
            scene.duration_s = 60.0
            scene.start_time = 0.0
            scene._last_input_t = 19.0  # input is "recent" at t=21
            self.assertFalse(scene.process_frame(21.0))


class AudioLockTest(unittest.TestCase):
    def test_default_contends_for_audio_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = _make_scene(tmp)
            self.assertTrue(scene.competes_for_audio_lock())

    def test_bypass_does_not_contend(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = _make_scene(tmp, bypass_audio_lock=True)
            self.assertFalse(scene.competes_for_audio_lock())


class InputSnapshotTest(unittest.TestCase):
    def test_cia_snapshot_masks_to_joystick_bits(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = _make_scene(tmp, input_source="cia")
            # Upper bits set (keyboard-scan noise) must be masked away.
            api.read_memory.return_value = bytes([0xEF, 0xFF])
            snap = scene._read_snapshot()
            self.assertEqual(snap, bytes([0xEF & CIA1.JOY_MASK, 0xFF & CIA1.JOY_MASK]))
            api.read_memory.assert_called_once_with(CIA1.PORT_A, 2)

    def test_kernal_snapshot_reads_scratch_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = _make_scene(tmp, input_source="kernal")
            api.read_memory.return_value = bytes([0x41, 0x01])
            snap = scene._read_snapshot()
            self.assertEqual(snap, bytes([0x41, 0x01]))

    def test_failed_read_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = _make_scene(tmp, input_source="cia")
            api.read_memory.return_value = None
            self.assertIsNone(scene._read_snapshot())

    def test_never_reads_modifier_byte(self):
        # $028D (modifier keys) must never be polled — it's the app's own
        # pause/skip/cycle signal and must not count as player input.
        with tempfile.TemporaryDirectory() as tmp:
            for src in ("cia", "kernal", "auto"):
                scene, api = _make_scene(tmp, input_source=src)
                api.read_memory.return_value = bytes([0, 0])
                scene._read_snapshot()
                for call in api.read_memory.call_args_list:
                    self.assertNotEqual(call.args[0], 0x028D)


class JoystickInjectionTest(unittest.TestCase):
    """inject_joystick queues for the sender thread, which posts it; teardown
    releases what was injected; a machine without the input API drops it."""

    def _running_scene(self, tmp, *, supported: bool):
        scene, api = _make_scene(tmp, input_source="none", reset_before_launch=False)
        api.profile.supports_rest_input = supported
        self.posted = threading.Event()

        def send(events):
            self.posted.set()
            return {}

        api.send_input.side_effect = send
        scene.setup()
        self.addCleanup(scene._sender.stop)
        return scene, api

    def test_press_is_posted_and_teardown_releases_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._running_scene(tmp, supported=True)
            scene._last_input_t = 0.0
            scene.inject_joystick(2, "fire", True)
            self.assertTrue(self.posted.wait(2.0))
            self.assertEqual(
                api.send_input.call_args_list[0].args[0],
                [machine_input.joystick_event(2, "press", ["fire"])],
            )
            self.assertGreater(scene._last_input_t, 0.0)
            scene.teardown()
            self.assertEqual(api.send_input.call_args.args[0], [machine_input.RELEASE_ALL])
            self.assertFalse(scene._sender.is_running())

    def test_teardown_without_injection_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._running_scene(tmp, supported=True)
            scene.teardown()
            api.send_input.assert_not_called()

    def test_machine_without_the_api_drops_with_one_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._running_scene(tmp, supported=False)
            with self.assertLogs("c64cast.scenes.scenes", level="WARNING") as cm:
                scene.inject_joystick(2, "up", True)
                scene.inject_joystick(2, "up", False)
            self.assertEqual(len(cm.records), 1)
            self.assertIn("no input API", cm.output[0])
            scene.teardown()
            api.send_input.assert_not_called()

    def test_supported_machine_without_a_running_program_drops_with_one_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._running_scene(tmp, supported=True)
            scene.teardown()
            self.assertFalse(scene._sender.is_running())
            with self.assertLogs("c64cast.scenes.scenes", level="WARNING") as cm:
                scene.inject_joystick(2, "up", True)
                scene.inject_joystick(2, "up", False)
            self.assertEqual(len(cm.records), 1)
            self.assertIn("program is not running", cm.output[0])
            self.assertTrue(scene._injected.empty())

    def test_events_still_queued_at_teardown_are_discarded_not_posted(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._running_scene(tmp, supported=True)
            scene.inject_joystick(2, "fire", True)
            self.assertTrue(self.posted.wait(2.0))
            scene._sender.stop()
            scene._injected.put(_joy(2, "press", "up"))
            scene._carry = _joy(2, "release", "fire")
            api.send_input.reset_mock()
            scene.teardown()
            api.send_input.assert_called_once_with([machine_input.RELEASE_ALL])
            self.assertTrue(scene._injected.empty())
            self.assertIsNone(scene._carry)
            self.assertEqual(scene._held, set())


def _joy(port, transition, *inputs):
    return machine_input.joystick_event(port, transition, list(inputs))


class JoystickSenderTest(unittest.TestCase):
    """The sender's batching, resync after a failed post, and stop on a
    revoked API, driven without its thread."""

    def _scene(self, tmp):
        scene, api = _make_scene(tmp, input_source="none")
        api.profile.supports_rest_input = True
        return scene, api

    def _drain(self, scene):
        batches = []
        while batch := scene._next_batch():
            batches.append(batch)
        return batches

    def test_press_and_release_of_one_input_go_in_separate_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = self._scene(tmp)
            for event in (
                _joy(2, "press", "fire"),
                _joy(2, "press", "up"),
                _joy(2, "release", "fire"),
                _joy(1, "press", "fire"),
                _joy(2, "press", "fire"),
            ):
                scene._injected.put(event)
            self.assertEqual(
                self._drain(scene),
                [
                    [_joy(2, "press", "fire"), _joy(2, "press", "up")],
                    [_joy(2, "release", "fire"), _joy(1, "press", "fire")],
                    [_joy(2, "press", "fire")],
                ],
            )

    def test_an_event_that_changes_nothing_costs_no_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = self._scene(tmp)
            for event in (
                _joy(2, "release", "up"),
                _joy(2, "press", "up"),
                _joy(2, "press", "up"),
                _joy(2, "press", "up"),
                _joy(2, "release", "up"),
                _joy(2, "release", "up"),
            ):
                scene._injected.put(event)
            self.assertEqual(
                self._drain(scene),
                [[_joy(2, "press", "up")], [_joy(2, "release", "up")]],
            )

    def test_failed_post_resends_the_held_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)
            for event in (
                _joy(2, "press", "up"),
                _joy(1, "press", "fire"),
                _joy(2, "press", "fire"),
                _joy(2, "release", "fire"),
            ):
                scene._injected.put(event)
            api.send_input.return_value = None
            for batch in self._drain(scene):
                scene._post(batch)
            self.assertTrue(scene._resync)
            self.assertEqual(
                scene._held_state(),
                [machine_input.RELEASE_ALL, _joy(1, "press", "fire"), _joy(2, "press", "up")],
            )

    def test_sender_resyncs_then_carries_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)
            done = threading.Event()
            answers = iter([None, {}, {}])

            def send(events):
                answer = next(answers)
                if api.send_input.call_count == 3:
                    done.set()
                return answer

            api.send_input.side_effect = send
            scene._injected.put(_joy(2, "press", "up"))
            scene._injected.put(_joy(2, "release", "up"))
            scene._sender.start()
            self.addCleanup(scene._sender.stop)
            self.assertTrue(done.wait(2.0))
            scene._sender.stop()
            self.assertEqual(
                [c.args[0] for c in api.send_input.call_args_list],
                [
                    [_joy(2, "press", "up")],
                    [machine_input.RELEASE_ALL, _joy(2, "press", "up")],
                    [_joy(2, "release", "up")],
                ],
            )

    def test_batch_dequeued_after_stop_is_not_posted(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)
            stop = threading.Event()
            scene._carry = _joy(2, "press", "up")
            scene._injected.put(_joy(2, "press", "fire"))
            real_next_batch = scene._next_batch

            def next_batch_then_stop():
                batch = real_next_batch()
                stop.set()
                return batch

            scene._next_batch = next_batch_then_stop
            scene._send_loop(stop)
            api.send_input.assert_not_called()

    def test_teardown_releases_after_a_slow_post_in_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)
            api.launch_program.return_value = None
            scene.input_source = "none"
            scene.reset_before_launch = False
            in_flight = threading.Event()
            order = []

            def send(events):
                order.append(("start", events))
                if events != [machine_input.RELEASE_ALL]:
                    in_flight.set()
                    threading.Event().wait(0.8)
                order.append(("end", events))
                return {}

            api.send_input.side_effect = send
            api.reset.side_effect = lambda: order.append(("reset", None))
            scene.setup()
            self.addCleanup(scene._sender.stop)
            scene.inject_joystick(2, "up", True)
            self.assertTrue(in_flight.wait(2.0))
            scene.teardown()
            self.assertFalse(scene._sender.is_running())
            press = [_joy(2, "press", "up")]
            self.assertEqual(
                order,
                [
                    ("start", press),
                    ("end", press),
                    ("start", [machine_input.RELEASE_ALL]),
                    ("end", [machine_input.RELEASE_ALL]),
                    ("reset", None),
                ],
            )

    def test_revoked_api_stops_the_sender_and_drops_with_the_no_api_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)

            def send(events):
                api.profile.supports_rest_input = False
                return None

            api.send_input.side_effect = send
            scene._sender.start()
            self.addCleanup(scene._sender.stop)
            scene.inject_joystick(2, "up", True)
            for _ in range(200):
                if not scene._sender.is_running():
                    break
                threading.Event().wait(0.01)
            self.assertFalse(scene._sender.is_running())
            self.assertFalse(scene._resync)
            with self.assertLogs("c64cast.scenes.scenes", level="WARNING") as cm:
                scene.inject_joystick(2, "up", False)
            self.assertIn("no input API", cm.output[0])
            api.send_input.assert_called_once()


if __name__ == "__main__":
    unittest.main()
