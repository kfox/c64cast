"""Tests for LauncherScene's idle-timeout state machine and input
snapshot logic. Pure-Python: the api is a Mock; no hardware is touched."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from unittest.mock import MagicMock

from c64cast.hw import machine_input
from c64cast.hw.c64 import CIA1
from c64cast.scenes import scenes
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

    def test_an_input_the_firmware_would_refuse_raises_and_leaves_the_sender_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._running_scene(tmp, supported=True)
            for port, direction in ((3, "fire"), (True, "fire"), (2, "start")):
                with self.assertRaises(ValueError):
                    scene.inject_joystick(port, direction, True)
            self.assertTrue(scene._injected.empty())
            scene.inject_joystick(2, "fire", True)
            self.assertTrue(self.posted.wait(2.0))
            self.assertEqual(api.send_input.call_args_list[0].args[0], [_joy(2, "press", "fire")])

    def test_an_event_queued_after_teardown_is_not_posted_on_the_next_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._running_scene(tmp, supported=True)
            scene.teardown()
            # What an injection that passed its running check just before
            # teardown stopped the sender leaves behind.
            scene._enqueue(_joy(2, "press", "fire"))
            self.posted.clear()
            scene.setup()
            scene.inject_joystick(2, "up", True)
            self.assertTrue(self.posted.wait(2.0))
            self.assertEqual(api.send_input.call_args_list[0].args[0], [_joy(2, "press", "up")])

    def test_a_sender_that_outlived_teardown_finishes_before_the_next_pass_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._running_scene(tmp, supported=True)
            in_post = threading.Event()
            unblock = threading.Event()
            self.addCleanup(unblock.set)

            def send(events):
                if events == [machine_input.RELEASE_ALL]:
                    return {}
                if not in_post.is_set():
                    in_post.set()
                    unblock.wait(5.0)
                    return None
                self.posted.set()
                return {}

            api.send_input.side_effect = send
            scene.inject_joystick(2, "fire", True)
            self.assertTrue(in_post.wait(2.0))
            scene._sender._join_timeout = 0.05
            with self.assertLogs("c64cast._pollthread", level="WARNING"):
                scene.teardown()
            self.assertTrue(scene._sender.is_running())
            scene._sender._join_timeout = 5.0
            releaser = threading.Timer(0.2, unblock.set)
            releaser.start()
            self.addCleanup(releaser.join)
            scene.setup()
            self.assertFalse(scene._resync)
            self.assertEqual(scene._pressed_at, {})
            scene.inject_joystick(2, "up", True)
            self.assertTrue(self.posted.wait(2.0))
            self.assertEqual(api.send_input.call_args.args[0], [_joy(2, "press", "up")])

    def test_each_pass_of_the_scene_warns_once_about_dropped_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = self._running_scene(tmp, supported=False)
            for _ in range(2):
                with self.assertLogs("c64cast.scenes.scenes", level="WARNING") as cm:
                    scene.inject_joystick(2, "up", True)
                    scene.inject_joystick(2, "up", False)
                self.assertEqual(len(cm.records), 1)
                scene.teardown()
                scene.setup()

    def test_events_still_queued_at_teardown_are_discarded_not_posted(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._running_scene(tmp, supported=True)
            scene.inject_joystick(2, "fire", True)
            self.assertTrue(self.posted.wait(2.0))
            scene._sender.stop()
            scene._enqueue(_joy(2, "press", "up"))
            scene._carry = (time.monotonic(), _joy(2, "release", "fire"))
            scene._collapsed.append((time.monotonic(), _joy(2, "press", "left")))
            api.send_input.reset_mock()
            scene.teardown()
            api.send_input.assert_called_once_with([machine_input.RELEASE_ALL])
            self.assertTrue(scene._injected.empty())
            self.assertIsNone(scene._carry)
            self.assertFalse(scene._collapsed)
            self.assertEqual(scene._held, set())


# One tick of a coarse monotonic clock (Windows before 3.13).
_CLOCK_TICK_S = 0.016


def _joy(port, transition, *inputs):
    return machine_input.joystick_event(port, transition, list(inputs))


class JoystickSenderTest(unittest.TestCase):
    """The sender's batching, resync after a failed post, and stop on a
    revoked API, driven without its thread."""

    def setUp(self):
        # Only a test that backdates its events collapses a backlog, however
        # long a loaded runner takes between queuing an event and sending it.
        lag = mock.patch.object(scenes, "_INJECT_MAX_LAG_S", 30.0)
        lag.start()
        self.addCleanup(lag.stop)

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
                scene._enqueue(event)
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
                scene._enqueue(event)
            self.assertEqual(
                self._drain(scene),
                [[_joy(2, "press", "up")], [_joy(2, "release", "up")]],
            )

    def test_a_stale_backlog_collapses_to_at_most_three_changes_per_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = self._scene(tmp)
            scene._held.add((2, "fire"))
            queued_at = time.monotonic() - scenes._INJECT_MAX_LAG_S - 1.0
            for event in (
                _joy(2, "press", "up"),
                _joy(2, "release", "up"),
                _joy(2, "release", "fire"),
                _joy(2, "press", "fire"),
                _joy(2, "release", "fire"),
                _joy(2, "press", "fire"),
                _joy(2, "release", "fire"),
                _joy(2, "press", "up"),
                _joy(2, "press", "left"),
                _joy(2, "release", "left"),
            ):
                scene._injected.put((queued_at, event))
            self.assertEqual(
                self._drain(scene),
                [
                    [_joy(2, "release", "fire")],
                    [_joy(2, "press", "fire")],
                    [_joy(2, "release", "fire"), _joy(2, "press", "up"), _joy(2, "press", "left")],
                    [_joy(2, "release", "left")],
                ],
            )
            self.assertEqual(scene._held, {(2, "up")})

    def test_a_tap_in_a_stale_backlog_still_reaches_the_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = self._scene(tmp)
            queued_at = time.monotonic() - scenes._INJECT_MAX_LAG_S - 1.0
            for event in (_joy(2, "press", "fire"), _joy(2, "release", "fire")):
                scene._injected.put((queued_at, event))
            self.assertEqual(
                self._drain(scene),
                [[_joy(2, "press", "fire")], [_joy(2, "release", "fire")]],
            )

    def test_a_collapse_never_holds_an_input_over_one_pressed_after_it_came_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = self._scene(tmp)
            queued_at = time.monotonic() - scenes._INJECT_MAX_LAG_S - 1.0
            for event in (
                _joy(2, "press", "fire"),
                _joy(2, "release", "fire"),
                _joy(2, "press", "up"),
                _joy(2, "release", "up"),
                _joy(2, "press", "fire"),
                _joy(2, "release", "fire"),
            ):
                scene._injected.put((queued_at, event))
            self.assertEqual(
                self._drain(scene),
                [
                    [_joy(2, "press", "up")],
                    [_joy(2, "release", "up"), _joy(2, "press", "fire")],
                    [_joy(2, "release", "fire")],
                ],
            )

    def test_a_collapse_never_holds_a_held_input_over_one_pressed_after_its_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = self._scene(tmp)
            scene._held.add((2, "fire"))
            queued_at = time.monotonic() - scenes._INJECT_MAX_LAG_S - 1.0
            for event in (
                _joy(2, "release", "fire"),
                _joy(2, "press", "up"),
                _joy(2, "release", "up"),
                _joy(2, "press", "fire"),
                _joy(2, "release", "fire"),
            ):
                scene._injected.put((queued_at, event))
            self.assertEqual(
                self._drain(scene),
                [
                    [_joy(2, "release", "fire"), _joy(2, "press", "up")],
                    [_joy(2, "release", "up"), _joy(2, "press", "fire")],
                    [_joy(2, "release", "fire")],
                ],
            )

    def test_a_collapsed_backlog_keeps_the_order_it_was_queued_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = self._scene(tmp)
            queued_at = time.monotonic() - scenes._INJECT_MAX_LAG_S - 1.0
            for event in (
                _joy(2, "press", "left"),
                _joy(2, "press", "fire"),
                _joy(2, "release", "left"),
            ):
                scene._injected.put((queued_at, event))
            self.assertEqual(
                self._drain(scene),
                [
                    [_joy(2, "press", "left"), _joy(2, "press", "fire")],
                    [_joy(2, "release", "left")],
                ],
            )

    def test_a_collapse_starts_from_the_carried_event_and_uses_it_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, _ = self._scene(tmp)
            scene._held.add((2, "fire"))
            queued_at = time.monotonic() - scenes._INJECT_MAX_LAG_S - 1.0
            scene._carry = (queued_at, _joy(2, "release", "fire"))
            scene._injected.put((queued_at, _joy(2, "press", "up")))
            self.assertEqual(
                self._drain(scene),
                [[_joy(2, "release", "fire"), _joy(2, "press", "up")]],
            )
            scene._enqueue(_joy(2, "press", "fire"))
            self.assertEqual(self._drain(scene), [[_joy(2, "press", "fire")]])

    def test_failed_post_resends_the_held_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)
            for event in (
                _joy(2, "press", "up"),
                _joy(1, "press", "fire"),
                _joy(2, "press", "fire"),
                _joy(2, "release", "fire"),
            ):
                scene._enqueue(event)
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
            scene._enqueue(_joy(2, "press", "up"))
            scene._enqueue(_joy(2, "release", "up"))
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

    def test_a_quick_tap_stays_down_for_the_minimum_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)
            done = threading.Event()
            posted_at = []

            def send(events):
                posted_at.append(time.monotonic())
                if len(posted_at) == 2:
                    done.set()
                return {}

            api.send_input.side_effect = send
            scene._enqueue(_joy(2, "press", "fire"))
            scene._enqueue(_joy(2, "release", "fire"))
            scene._sender.start()
            self.addCleanup(scene._sender.stop)
            self.assertTrue(done.wait(2.0))
            scene._sender.stop()
            self.assertGreaterEqual(
                posted_at[1] - posted_at[0], scenes._INJECT_MIN_HOLD_S - _CLOCK_TICK_S
            )

    def test_a_press_landed_by_a_resync_stays_down_for_the_minimum_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)
            done = threading.Event()
            posts = []

            def send(events):
                posts.append(time.monotonic())
                # The press and the first resync fail; the second resync lands.
                if len(posts) == 4:
                    done.set()
                return {} if len(posts) >= 3 else None

            api.send_input.side_effect = send
            scene._enqueue(_joy(2, "press", "fire"))
            scene._enqueue(_joy(2, "release", "fire"))
            scene._sender.start()
            self.addCleanup(scene._sender.stop)
            self.assertTrue(done.wait(3.0))
            scene._sender.stop()
            self.assertGreaterEqual(posts[3] - posts[2], scenes._INJECT_MIN_HOLD_S - _CLOCK_TICK_S)

    def test_stop_during_the_hold_ends_the_wait_and_posts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)
            scene._held.add((2, "fire"))
            scene._pressed_at[(2, "fire")] = time.monotonic()
            scene._enqueue(_joy(2, "release", "fire"))
            stop = threading.Event()
            loop = threading.Thread(target=scene._send_loop, args=(stop,))
            with mock.patch.object(scenes, "_INJECT_MIN_HOLD_S", 30.0):
                loop.start()
                self.addCleanup(loop.join, 5.0)
                self.addCleanup(stop.set)
                time.sleep(0.1)
                stop.set()
                loop.join(2.0)
            self.assertFalse(loop.is_alive())
            api.send_input.assert_not_called()

    def test_batch_dequeued_after_stop_is_not_posted(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene, api = self._scene(tmp)
            stop = threading.Event()
            scene._carry = (time.monotonic(), _joy(2, "press", "up"))
            scene._enqueue(_joy(2, "press", "fire"))
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
