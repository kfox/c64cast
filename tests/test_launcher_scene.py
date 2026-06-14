"""Tests for LauncherScene's idle-timeout state machine and input
snapshot logic. Pure-Python: the api is a Mock; no hardware is touched."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from c64cast.c64 import CIA1
from c64cast.scenes import LauncherScene


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
            # Ceiling wins regardless of recent input.
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


if __name__ == "__main__":
    unittest.main()
