"""Host-side unit tests for midi_control.py.

Mirrors test_midi_scene.py's approach: real mido.Message objects drive
dispatch, _open_port is exercised against a patched `mido` module (no real
MIDI hardware touched), and Playlist targets are MagicMock's satisfying only
what MidiControlListener reads (pause/resume/skip/cycle_event, scenes,
request_jump, current) — mirroring test_control_plane.py's _fake_playlist
helper. No FakeAPI/DMA fake needed anywhere here: this module never touches
hardware, only in-process Playlist Events and plain attribute writes.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any
from unittest import mock

try:
    import mido as _mido

    mido: Any = _mido
    HAVE_MIDI = True
except ImportError:
    mido = None
    HAVE_MIDI = False

from c64cast import config as cfgmod
from c64cast import midi_control
from c64cast.midi_control import (
    MidiControlListener,
    _parse_cc_map,
    _parse_mmc_sysex,
)
from c64cast.transport import TransportEvent


def _fake_playlist(name: str, *, scene_count: int = 16) -> Any:
    """A MagicMock'd Playlist satisfying what MidiControlListener reads:
    .name, .scenes (list), .pause/resume/skip/cycle_event (real Events),
    .request_jump(), .current (a scene stand-in with .effect/.source)."""
    pl = mock.MagicMock(name=f"playlist-{name}")
    pl.name = name
    pl.scenes = [mock.MagicMock() for _ in range(scene_count)]
    pl.pause_event = threading.Event()
    pl.resume_event = threading.Event()
    pl.skip_event = threading.Event()
    pl.cycle_event = threading.Event()
    pl.current = mock.MagicMock()
    return pl


class _FakePort:
    """Minimal mido-input stand-in: never yields a message, closes cleanly."""

    def __init__(self) -> None:
        self.closed = False

    def iter_pending(self):
        return iter(())

    def close(self) -> None:
        self.closed = True


class _ScriptedPort:
    """Yields one queued batch of messages on the first iter_pending() call,
    then nothing — mirrors test_midi_scene.py's _ScriptedPort."""

    def __init__(self, batch) -> None:
        self._batch = batch
        self._done = False
        self.closed = False

    def iter_pending(self):
        if self._done:
            return iter(())
        self._done = True
        return iter(self._batch)

    def close(self) -> None:
        self.closed = True


class ParseCCMapTests(unittest.TestCase):
    def test_valid_entries_parse(self):
        m = _parse_cc_map(
            [
                {"type": "note", "number": 36, "action": "skip"},
                {"type": "pc", "number": 0, "action": "jump", "scene": 2},
                {"type": "cc", "number": 13, "action": "param", "target": "effect.decay"},
            ]
        )
        self.assertEqual(m[("note", 36)].action, "skip")
        self.assertEqual(m[("pc", 0)].scene, 2)
        self.assertEqual(m[("cc", 13)].target, "effect.decay")

    def test_later_entry_overwrites_earlier_same_key(self):
        m = _parse_cc_map(
            [
                {"type": "note", "number": 36, "action": "skip"},
                {"type": "note", "number": 36, "action": "cycle_style"},
            ]
        )
        self.assertEqual(m[("note", 36)].action, "cycle_style")

    def test_bad_type_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map([{"type": "bogus", "number": 1, "action": "skip"}])

    def test_bad_number_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map([{"type": "cc", "number": 999, "action": "skip"}])

    def test_bad_action_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map([{"type": "cc", "number": 1, "action": "bogus"}])

    def test_jump_without_scene_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map([{"type": "note", "number": 1, "action": "jump"}])

    def test_param_without_target_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map([{"type": "cc", "number": 1, "action": "param"}])

    def test_non_dict_entry_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map(["not a dict"])  # type: ignore[list-item]

    def test_mmc_entry_parses(self):
        m = _parse_cc_map([{"type": "mmc", "number": 0x02, "action": "transport.play_pause"}])
        self.assertEqual(m[("mmc", 0x02)].action, "transport.play_pause")

    def test_mmc_bad_command_byte_rejected(self):
        # A syntactically valid 0..127 number that isn't a recognized MMC
        # transport command (0x7F isn't stop/play/ff/rw/record/pause).
        with self.assertRaises(ValueError):
            _parse_cc_map([{"type": "mmc", "number": 0x7F, "action": "transport.stop"}])

    def test_transport_actions_parse(self):
        for action in (
            "transport.play_pause",
            "transport.stop",
            "transport.loop_toggle",
            "transport.rw",
            "transport.ff",
            "transport.jog",
            "transport.record",
        ):
            m = _parse_cc_map([{"type": "note", "number": 60, "action": action}])
            self.assertEqual(m[("note", 60)].action, action)

    def test_loop_slot_parses_with_slot(self):
        m = _parse_cc_map([{"type": "note", "number": 60, "action": "loop_slot", "slot": 3}])
        self.assertEqual(m[("note", 60)].action, "loop_slot")
        self.assertEqual(m[("note", 60)].slot, 3)

    def test_loop_slot_without_slot_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map([{"type": "note", "number": 60, "action": "loop_slot"}])

    def test_loop_slot_zero_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map([{"type": "note", "number": 60, "action": "loop_slot", "slot": 0}])

    def test_loop_slot_negative_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map([{"type": "note", "number": 60, "action": "loop_slot", "slot": -1}])

    def test_jog_mode_abs_and_rel_accepted(self):
        for mode in ("abs", "rel"):
            m = _parse_cc_map(
                [{"type": "cc", "number": 20, "action": "transport.jog", "mode": mode}]
            )
            self.assertEqual(m[("cc", 20)].mode, mode)

    def test_jog_omitted_mode_stored_as_none(self):
        # None -> "rel" default is applied at dispatch time, not parse time.
        m = _parse_cc_map([{"type": "cc", "number": 20, "action": "transport.jog"}])
        self.assertIsNone(m[("cc", 20)].mode)

    def test_jog_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            _parse_cc_map(
                [{"type": "cc", "number": 20, "action": "transport.jog", "mode": "sideways"}]
            )


class MmcSysexParseTests(unittest.TestCase):
    def test_recognized_command_returns_byte(self):
        self.assertEqual(_parse_mmc_sysex((0x7F, 0x7F, 0x06, 0x02)), 0x02)  # play

    def test_device_byte_is_wildcarded(self):
        # Any device byte (not just the 0x7F "all devices" broadcast) matches.
        self.assertEqual(_parse_mmc_sysex((0x7F, 0x05, 0x06, 0x01)), 0x01)  # stop

    def test_unrecognized_command_returns_none(self):
        self.assertIsNone(_parse_mmc_sysex((0x7F, 0x7F, 0x06, 0x7E)))

    def test_non_mmc_frame_returns_none(self):
        self.assertIsNone(_parse_mmc_sysex((0x41, 0x10, 0x42)))  # some other SysEx

    def test_wrong_length_returns_none(self):
        self.assertIsNone(_parse_mmc_sysex((0x7F, 0x7F, 0x06)))


class MidiActionChoicesDriftTest(unittest.TestCase):
    """midi_control.py's runtime vocabulary and config.py's cc_map validation
    vocabulary must agree (config stays import-light and keeps its own copy —
    see both modules' docstrings), so a new action/type added to one always
    gets added to the other."""

    def test_actions_match(self):
        self.assertEqual(set(midi_control._ACTIONS), set(cfgmod._MIDI_ACTION_CHOICES))

    def test_cc_types_match(self):
        self.assertEqual(set(midi_control._CC_TYPES), set(cfgmod._MIDI_CC_TYPE_CHOICES))

    def test_mmc_commands_match(self):
        self.assertEqual(midi_control._MMC_COMMANDS, set(cfgmod._MIDI_MMC_COMMAND_CHOICES))


@unittest.skipUnless(HAVE_MIDI, "mido not installed (midi extra)")
class _MidiControlTestCase(unittest.TestCase):
    pass


class DispatchActionTests(_MidiControlTestCase):
    """Direct _dispatch() calls, no thread — mirrors test_midi_scene.py's
    ControlChangeTests / HandleMsgDispatchTests style."""

    def _listener(self, cc_map, **kwargs) -> tuple[MidiControlListener, Any]:
        pl = _fake_playlist("system")
        listener = MidiControlListener({"system": pl}, cc_map, **kwargs)
        return listener, pl

    def test_note_on_dispatches_skip(self):
        listener, pl = self._listener([{"type": "note", "number": 36, "action": "skip"}])
        listener._dispatch(mido.Message("note_on", note=36, velocity=100))
        self.assertTrue(pl.skip_event.is_set())

    def test_note_on_dispatches_cycle_style(self):
        listener, pl = self._listener([{"type": "note", "number": 37, "action": "cycle_style"}])
        listener._dispatch(mido.Message("note_on", note=37, velocity=100))
        self.assertTrue(pl.cycle_event.is_set())

    def test_toggle_pause_sets_pause_when_not_paused(self):
        listener, pl = self._listener([{"type": "note", "number": 38, "action": "toggle_pause"}])
        listener._dispatch(mido.Message("note_on", note=38, velocity=100))
        self.assertTrue(pl.pause_event.is_set())
        self.assertFalse(pl.resume_event.is_set())

    def test_toggle_pause_sets_resume_when_paused(self):
        listener, pl = self._listener([{"type": "note", "number": 38, "action": "toggle_pause"}])
        pl.pause_event.set()
        listener._dispatch(mido.Message("note_on", note=38, velocity=100))
        self.assertTrue(pl.resume_event.is_set())

    def test_note_on_dispatches_jump(self):
        listener, pl = self._listener(
            [{"type": "note", "number": 40, "action": "jump", "scene": 3}]
        )
        listener._dispatch(mido.Message("note_on", note=40, velocity=100))
        pl.request_jump.assert_called_once_with(3, skip_interstitial=True)

    def test_jump_transition_interstitial_passes_skip_interstitial_false(self):
        listener, pl = self._listener(
            [{"type": "note", "number": 40, "action": "jump", "scene": 3}],
            jump_transition="interstitial",
        )
        listener._dispatch(mido.Message("note_on", note=40, velocity=100))
        pl.request_jump.assert_called_once_with(3, skip_interstitial=False)

    def test_jump_out_of_range_scene_is_noop(self):
        listener, pl = self._listener(
            [{"type": "note", "number": 40, "action": "jump", "scene": 99}]
        )
        listener._dispatch(mido.Message("note_on", note=40, velocity=100))
        pl.request_jump.assert_not_called()

    def test_program_change_dispatches_jump(self):
        listener, pl = self._listener([{"type": "pc", "number": 5, "action": "jump", "scene": 5}])
        listener._dispatch(mido.Message("program_change", program=5))
        pl.request_jump.assert_called_once_with(5, skip_interstitial=True)

    def test_note_off_never_dispatches(self):
        listener, pl = self._listener([{"type": "note", "number": 36, "action": "skip"}])
        listener._dispatch(mido.Message("note_off", note=36))
        self.assertFalse(pl.skip_event.is_set())

    def test_note_on_velocity_zero_never_dispatches(self):
        listener, pl = self._listener([{"type": "note", "number": 36, "action": "skip"}])
        listener._dispatch(mido.Message("note_on", note=36, velocity=0))
        self.assertFalse(pl.skip_event.is_set())

    def test_unmapped_message_is_noop(self):
        listener, pl = self._listener([{"type": "note", "number": 36, "action": "skip"}])
        listener._dispatch(mido.Message("note_on", note=99, velocity=100))
        self.assertFalse(pl.skip_event.is_set())


class TransportDispatchTests(_MidiControlTestCase):
    """transport.* actions enqueue a TransportEvent on pl.transport instead
    of mutating scene state directly on the MIDI reader thread — see
    transport.TransportSession."""

    def _listener(self, cc_map, **kwargs) -> tuple[MidiControlListener, Any]:
        pl = _fake_playlist("system")
        listener = MidiControlListener({"system": pl}, cc_map, **kwargs)
        return listener, pl

    def test_note_on_enqueues_press(self):
        listener, pl = self._listener(
            [{"type": "note", "number": 41, "action": "transport.loop_toggle"}]
        )
        listener._dispatch(mido.Message("note_on", note=41, velocity=100))
        pl.transport.enqueue.assert_called_once_with(
            TransportEvent(action="loop_toggle", pressed=True, value=100, mode="rel")
        )

    def test_cc_enqueues_press_with_value(self):
        listener, pl = self._listener([{"type": "cc", "number": 20, "action": "transport.jog"}])
        listener._dispatch(mido.Message("control_change", control=20, value=65))
        pl.transport.enqueue.assert_called_once_with(
            TransportEvent(action="jog", pressed=True, value=65, mode="rel")
        )

    def test_jog_mode_abs_threaded_through(self):
        listener, pl = self._listener(
            [{"type": "cc", "number": 20, "action": "transport.jog", "mode": "abs"}]
        )
        listener._dispatch(mido.Message("control_change", control=20, value=64))
        pl.transport.enqueue.assert_called_once_with(
            TransportEvent(action="jog", pressed=True, value=64, mode="abs")
        )

    def test_rw_note_release_enqueues_pressed_false(self):
        listener, pl = self._listener([{"type": "note", "number": 42, "action": "transport.rw"}])
        listener._dispatch(mido.Message("note_on", note=42, velocity=100))
        listener._dispatch(mido.Message("note_off", note=42))
        self.assertEqual(pl.transport.enqueue.call_count, 2)
        release = pl.transport.enqueue.call_args_list[1].args[0]
        self.assertEqual(release, TransportEvent(action="rw", pressed=False, value=0, mode="rel"))

    def test_ff_note_release_via_velocity_zero_enqueues_pressed_false(self):
        listener, pl = self._listener([{"type": "note", "number": 43, "action": "transport.ff"}])
        listener._dispatch(mido.Message("note_on", note=43, velocity=100))
        listener._dispatch(mido.Message("note_on", note=43, velocity=0))
        release = pl.transport.enqueue.call_args_list[1].args[0]
        self.assertEqual(release, TransportEvent(action="ff", pressed=False, value=0, mode="rel"))

    def test_non_hold_action_release_is_discarded(self):
        # loop_toggle isn't hold-aware — a release for it carries no meaning
        # and must not reach TransportSession at all.
        listener, pl = self._listener(
            [{"type": "note", "number": 41, "action": "transport.loop_toggle"}]
        )
        listener._dispatch(mido.Message("note_off", note=41))
        pl.transport.enqueue.assert_not_called()

    def test_sysex_mmc_play_dispatches(self):
        listener, pl = self._listener(
            [{"type": "mmc", "number": 0x02, "action": "transport.play_pause"}]
        )
        listener._dispatch(mido.Message("sysex", data=(0x7F, 0x7F, 0x06, 0x02)))
        pl.transport.enqueue.assert_called_once_with(
            TransportEvent(action="play_pause", pressed=True, value=127, mode="rel")
        )

    def test_record_note_release_enqueues_pressed_false(self):
        # Phase 3: record is hold-aware too (the loop_slot pad chords need
        # its release), same as rw/ff.
        listener, pl = self._listener(
            [{"type": "note", "number": 44, "action": "transport.record"}]
        )
        listener._dispatch(mido.Message("note_on", note=44, velocity=100))
        listener._dispatch(mido.Message("note_off", note=44))
        self.assertEqual(pl.transport.enqueue.call_count, 2)
        release = pl.transport.enqueue.call_args_list[1].args[0]
        self.assertEqual(
            release, TransportEvent(action="record", pressed=False, value=0, mode="rel")
        )

    def test_stop_note_release_enqueues_pressed_false(self):
        listener, pl = self._listener([{"type": "note", "number": 45, "action": "transport.stop"}])
        listener._dispatch(mido.Message("note_on", note=45, velocity=100))
        listener._dispatch(mido.Message("note_off", note=45))
        self.assertEqual(pl.transport.enqueue.call_count, 2)
        release = pl.transport.enqueue.call_args_list[1].args[0]
        self.assertEqual(release, TransportEvent(action="stop", pressed=False, value=0, mode="rel"))

    def test_sysex_mmc_record_dispatches(self):
        listener, pl = self._listener(
            [{"type": "mmc", "number": 0x06, "action": "transport.record"}]
        )
        listener._dispatch(mido.Message("sysex", data=(0x7F, 0x7F, 0x06, 0x06)))
        pl.transport.enqueue.assert_called_once_with(
            TransportEvent(action="record", pressed=True, value=127, mode="rel")
        )

    def test_loop_slot_enqueues_with_slot(self):
        listener, pl = self._listener(
            [{"type": "note", "number": 60, "action": "loop_slot", "slot": 3}]
        )
        listener._dispatch(mido.Message("note_on", note=60, velocity=100))
        pl.transport.enqueue.assert_called_once_with(
            TransportEvent(action="loop_slot", pressed=True, value=100, mode="rel", slot=3)
        )

    def test_loop_slot_release_is_discarded(self):
        # loop_slot itself isn't hold-aware (only record/stop are) — a pad
        # release carries no meaning.
        listener, pl = self._listener(
            [{"type": "note", "number": 60, "action": "loop_slot", "slot": 3}]
        )
        listener._dispatch(mido.Message("note_off", note=60))
        pl.transport.enqueue.assert_not_called()

    def test_sysex_unrecognized_command_is_noop(self):
        listener, pl = self._listener(
            [{"type": "mmc", "number": 0x02, "action": "transport.play_pause"}]
        )
        listener._dispatch(mido.Message("sysex", data=(0x7F, 0x7F, 0x06, 0x7E)))
        pl.transport.enqueue.assert_not_called()

    def test_sysex_command_with_no_mapping_is_noop(self):
        listener, pl = self._listener(
            [{"type": "mmc", "number": 0x02, "action": "transport.play_pause"}]
        )
        # 0x01 (stop) is a recognized MMC command, but isn't in the cc_map.
        listener._dispatch(mido.Message("sysex", data=(0x7F, 0x7F, 0x06, 0x01)))
        pl.transport.enqueue.assert_not_called()


class ParamActionTests(_MidiControlTestCase):
    def _listener_with_effect(self, decay_range=(0.0, 0.96)):
        pl = _fake_playlist("system")
        effect = mock.MagicMock()
        type(effect).LIVE_PARAMS = {"decay": decay_range}
        pl.current.effect = effect
        listener = MidiControlListener(
            {"system": pl},
            [{"type": "cc", "number": 13, "action": "param", "target": "effect.decay"}],
        )
        return listener, pl, effect

    def test_cc_scales_and_sets_live_param(self):
        listener, pl, effect = self._listener_with_effect()
        listener._dispatch(mido.Message("control_change", control=13, value=127))
        self.assertAlmostEqual(effect.decay, 0.96, places=4)

    def test_cc_zero_maps_to_range_minimum(self):
        listener, pl, effect = self._listener_with_effect()
        listener._dispatch(mido.Message("control_change", control=13, value=0))
        self.assertAlmostEqual(effect.decay, 0.0, places=4)

    def test_cc_midpoint_scales_linearly(self):
        listener, pl, effect = self._listener_with_effect(decay_range=(0.0, 1.0))
        listener._dispatch(mido.Message("control_change", control=13, value=64))
        self.assertAlmostEqual(effect.decay, 64 / 127.0, places=4)

    def test_target_without_live_param_entry_is_noop(self):
        # effect declares LIVE_PARAMS but not the mapped name.
        pl = _fake_playlist("system")
        effect = mock.MagicMock()
        type(effect).LIVE_PARAMS = {"decay": (0.0, 0.96)}
        pl.current.effect = effect
        listener = MidiControlListener(
            {"system": pl},
            [{"type": "cc", "number": 13, "action": "param", "target": "effect.nonexistent"}],
        )
        before = effect.mock_calls
        listener._dispatch(mido.Message("control_change", control=13, value=64))
        # No new attribute assertion recorded beyond what MagicMock already had.
        self.assertEqual(effect.mock_calls, before)

    def test_holder_missing_is_noop(self):
        pl = _fake_playlist("system")
        pl.current = mock.MagicMock(spec=[])  # no .effect/.source attrs at all
        listener = MidiControlListener(
            {"system": pl},
            [{"type": "cc", "number": 13, "action": "param", "target": "effect.decay"}],
        )
        # Should not raise.
        listener._dispatch(mido.Message("control_change", control=13, value=64))

    def test_no_current_scene_is_noop(self):
        pl = _fake_playlist("system")
        pl.current = None
        listener = MidiControlListener(
            {"system": pl},
            [{"type": "cc", "number": 13, "action": "param", "target": "effect.decay"}],
        )
        listener._dispatch(mido.Message("control_change", control=13, value=64))

    def test_scene_prefix_targets_the_scene_itself(self):
        # `scene.<name>` resolves the holder to the scene, not a source/effect
        # attribute — the scope-scene seam (VoiceScopeRenderer.gain). Mirrors
        # wled_device._set_live_param's `scene.` case verbatim.
        pl = _fake_playlist("system")
        scene = mock.MagicMock()
        type(scene).LIVE_PARAMS = {"gain": (0.25, 3.0)}
        pl.current = scene
        listener = MidiControlListener(
            {"system": pl},
            [{"type": "cc", "number": 13, "action": "param", "target": "scene.gain"}],
        )
        listener._dispatch(mido.Message("control_change", control=13, value=127))
        self.assertAlmostEqual(scene.gain, 3.0, places=4)


class ChannelTargetingTests(_MidiControlTestCase):
    def test_single_system_ignores_channel(self):
        pl = _fake_playlist("only")
        listener = MidiControlListener(
            {"only": pl}, [{"type": "note", "number": 36, "action": "skip"}]
        )
        listener._dispatch(mido.Message("note_on", note=36, velocity=100, channel=7))
        self.assertTrue(pl.skip_event.is_set())

    def _ensemble(self, n=3):
        pls = {f"sys{i}": _fake_playlist(f"sys{i}") for i in range(n)}
        listener = MidiControlListener(
            pls,
            [{"type": "note", "number": 36, "action": "skip"}],
            broadcast_channel=16,
        )
        return listener, pls

    def test_channel_targets_nth_system(self):
        listener, pls = self._ensemble()
        listener._dispatch(mido.Message("note_on", note=36, velocity=100, channel=1))
        self.assertFalse(pls["sys0"].skip_event.is_set())
        self.assertTrue(pls["sys1"].skip_event.is_set())
        self.assertFalse(pls["sys2"].skip_event.is_set())

    def test_broadcast_channel_targets_all(self):
        listener, pls = self._ensemble()
        listener._dispatch(mido.Message("note_on", note=36, velocity=100, channel=15))
        for pl in pls.values():
            self.assertTrue(pl.skip_event.is_set())

    def test_out_of_range_channel_targets_nothing(self):
        listener, pls = self._ensemble()
        listener._dispatch(mido.Message("note_on", note=36, velocity=100, channel=10))
        for pl in pls.values():
            self.assertFalse(pl.skip_event.is_set())


class CrashGuardTests(_MidiControlTestCase):
    def test_one_target_raising_does_not_block_others(self):
        good = _fake_playlist("good")
        bad = _fake_playlist("bad")
        bad.skip_event = mock.MagicMock()
        bad.skip_event.set.side_effect = RuntimeError("boom")
        listener = MidiControlListener(
            {"bad": bad, "good": good},
            [{"type": "note", "number": 36, "action": "skip"}],
            broadcast_channel=16,
        )
        with self.assertLogs("c64cast.midi_control", level="ERROR"):
            listener._dispatch(mido.Message("note_on", note=36, velocity=100, channel=15))
        self.assertTrue(good.skip_event.is_set())

    def test_reader_thread_survives_dispatch_exception(self):
        pl = _fake_playlist("system")
        listener = MidiControlListener(
            {"system": pl}, [{"type": "note", "number": 36, "action": "skip"}]
        )
        with (
            mock.patch.object(listener, "_dispatch", side_effect=RuntimeError("boom")),
            self.assertLogs("c64cast.midi_control", level="ERROR"),
        ):
            listener._midi_port = _ScriptedPort([mido.Message("note_on", note=36, velocity=100)])
            listener._stop.clear()
            t = threading.Thread(target=listener._reader, daemon=True)
            t.start()
            time.sleep(0.05)
            self.assertTrue(t.is_alive())
            listener._stop.set()
            t.join(timeout=1.0)
            self.assertFalse(t.is_alive())


class PortSelectionTests(_MidiControlTestCase):
    """_open_port name resolution, exercised with mido patched so no real
    MIDI hardware is touched — mirrors test_midi_scene.py's
    PortSelectionTests."""

    def _patch_mido(self, names, opened):
        fake = mock.MagicMock()
        fake.get_input_names.return_value = names
        fake.open_input.side_effect = lambda n: opened.append(n) or _FakePort()
        return mock.patch.object(midi_control, "mido", fake)

    def _listener(self, port=None):
        pl = _fake_playlist("system")
        return MidiControlListener({"system": pl}, [], port=port)

    def test_empty_port_picks_first(self):
        listener = self._listener(port="")
        opened: list[str] = []
        with self._patch_mido(["Port A", "Port B"], opened):
            listener._open_port()
        self.assertEqual(opened, ["Port A"])

    def test_no_ports_raises(self):
        listener = self._listener(port="")
        with self._patch_mido([], []):
            with self.assertRaises(RuntimeError):
                listener._open_port()

    def test_substring_match(self):
        listener = self._listener(port="launch")
        opened: list[str] = []
        with self._patch_mido(["IAC Bus 1", "Launch Control XL"], opened):
            listener._open_port()
        self.assertEqual(opened, ["Launch Control XL"])

    def test_no_match_raises(self):
        listener = self._listener(port="nonexistent")
        with self._patch_mido(["IAC Bus 1"], []):
            with self.assertRaises(RuntimeError):
                listener._open_port()


class LifecycleTests(_MidiControlTestCase):
    def test_start_stop_lifecycle(self):
        pl = _fake_playlist("system")
        listener = MidiControlListener(
            {"system": pl}, [{"type": "note", "number": 36, "action": "skip"}]
        )
        with mock.patch.object(
            listener,
            "_open_port",
            side_effect=lambda: setattr(listener, "_midi_port", _FakePort()),
        ):
            listener.start()
        try:
            reader = listener._reader_thread
            assert reader is not None
            self.assertTrue(reader.is_alive())
        finally:
            port = listener._midi_port
            listener.stop()
            self.assertTrue(port.closed)
            self.assertIsNone(listener._reader_thread)

    def test_empty_playlists_rejected(self):
        with self.assertRaises(ValueError):
            MidiControlListener({}, [])


class BuildListenerTests(unittest.TestCase):
    def test_raises_when_midi_unavailable(self):
        pl = _fake_playlist("system")
        fake_cfg = mock.MagicMock()
        fake_cfg.cc_map = []
        with mock.patch.object(midi_control, "MIDI_AVAILABLE", False):
            with self.assertRaises(RuntimeError):
                midi_control.build_midi_control_listener({"system": pl}, fake_cfg)


# ----------------------------------------------------- LED feedback (Phase 4) ---
class _FakeOutPort:
    """mido-output stand-in: records every sent Message, closes cleanly."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.closed = False

    def send(self, msg: Any) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        self.closed = True


class _FakePerf:
    def __init__(self, *, clip_pads=(), active=None, armed=None) -> None:
        self._clip_pads = list(clip_pads)  # (pad_type, number, slot)
        self.active_slot = active
        self.armed_slot = armed

    def clip_pad_mappings(self):
        return list(self._clip_pads)


class _FakeEffect:
    def __init__(self, enabled=True) -> None:
        self.enabled = enabled


class _FakeScene:
    def __init__(self, effects=()) -> None:
        self.effects = list(effects)


def _perf_playlist(name, *, clip_pads=(), active=None, armed=None, effects=()):
    pl = mock.MagicMock(name=name)
    pl.name = name
    pl.performance = _FakePerf(clip_pads=clip_pads, active=active, armed=armed)
    pl.current = _FakeScene(effects=effects)
    return pl


class ComputePadLedsTests(unittest.TestCase):
    """The pure state→velocity mapping (no mido, no threads)."""

    def setUp(self):
        self.fm = midi_control.FeedbackMap()  # Launchpad-X defaults

    def test_loaded_active_armed(self):
        out = midi_control.compute_pad_leds(
            [(60, 1), (61, 2), (62, 3)], [], {1}, {2}, set(), self.fm, blink_on=True
        )
        self.assertEqual(out[60], self.fm.active)  # slot 1 is live
        self.assertEqual(out[61], self.fm.armed)  # slot 2 arming, blink on-phase
        self.assertEqual(out[62], self.fm.loaded)  # slot 3 idle

    def test_armed_blink_off_phase_extinguishes(self):
        out = midi_control.compute_pad_leds(
            [(61, 2)], [], set(), {2}, set(), self.fm, blink_on=False
        )
        self.assertEqual(out[61], self.fm.off)

    def test_fx_pad_lit_when_layer_enabled(self):
        on = midi_control.compute_pad_leds([], [(70, 0)], set(), set(), {0}, self.fm, blink_on=True)
        off = midi_control.compute_pad_leds(
            [], [(70, 0)], set(), set(), set(), self.fm, blink_on=True
        )
        self.assertEqual(on[70], self.fm.fx_on)
        self.assertEqual(off[70], self.fm.off)

    def test_brighter_role_wins_on_shared_note(self):
        # A note claimed as both a loaded clip pad and a lit fx pad → the brighter.
        out = midi_control.compute_pad_leds(
            [(70, 9)], [(70, 0)], set(), set(), {0}, self.fm, blink_on=True
        )
        self.assertEqual(out[70], max(self.fm.loaded, self.fm.fx_on))


class FeedbackMapTests(unittest.TestCase):
    def test_none_and_empty_yield_defaults(self):
        self.assertEqual(midi_control.FeedbackMap.from_dict(None), midi_control.FeedbackMap())
        self.assertEqual(midi_control.FeedbackMap.from_dict({}), midi_control.FeedbackMap())

    def test_override_and_tolerant_parsing(self):
        fm = midi_control.FeedbackMap.from_dict(
            {"active": 5, "loaded": 200, "armed": True, "bogus": 1, "port": "X"}
        )
        d = midi_control.FeedbackMap()
        self.assertEqual(fm.active, 5)  # valid override
        self.assertEqual(fm.loaded, d.loaded)  # 200 out of range → default
        self.assertEqual(fm.armed, d.armed)  # bool rejected → default

    def test_round_trip_to_dict(self):
        fm = midi_control.FeedbackMap(channel=1, active=7)
        self.assertEqual(midi_control.FeedbackMap.from_dict(fm.to_dict()), fm)


class _FeedbackListenerTestCase(unittest.TestCase):
    def _listener(self, playlists, cc_map=None, **kw):
        return MidiControlListener(
            {pl.name: pl for pl in playlists}, cc_map or [], feedback_enabled=True, **kw
        )


class LedPadListTests(_FeedbackListenerTestCase):
    def test_build_led_pad_lists_from_clips_and_fx(self):
        pl = _perf_playlist("a", clip_pads=[("note", 60, 1), ("pc", 5, 2)])
        cc_map = [{"type": "note", "number": 70, "action": "fx_toggle", "slot": 2}]
        lis = self._listener([pl], cc_map)
        lis._build_led_pad_lists()
        self.assertEqual(lis._led_clip_pads, [(60, 1)])  # pc pad excluded (no note-on)
        self.assertEqual(lis._led_fx_pads, [(70, 2)])

    def test_compute_led_map_reads_playlist_state(self):
        fx = _FakeScene(effects=[_FakeEffect(True), _FakeEffect(False)])
        pl = _perf_playlist("a", clip_pads=[("note", 60, 1)], active=1)
        pl.current = fx
        cc_map = [
            {"type": "note", "number": 70, "action": "fx_toggle", "slot": 0},
            {"type": "note", "number": 71, "action": "fx_toggle", "slot": 1},
        ]
        lis = self._listener([pl], cc_map)
        lis._build_led_pad_lists()
        m = lis._compute_led_map(blink_on=True)
        fmap = lis._fmap
        self.assertEqual(m[60], fmap.active)  # clip slot 1 is live
        self.assertEqual(m[70], fmap.fx_on)  # layer 0 enabled
        self.assertEqual(m[71], fmap.off)  # layer 1 bypassed


class LedEmitTests(_FeedbackListenerTestCase):
    def test_emit_diff_only_sends_changes(self):
        lis = self._listener([_perf_playlist("a")])
        lis._out_port = _FakeOutPort()
        lis._emit_led_diff({60: 21, 61: 1})
        self.assertEqual(len(lis._out_port.sent), 2)
        lis._emit_led_diff({60: 21, 61: 1})  # unchanged → no sends
        self.assertEqual(len(lis._out_port.sent), 2)
        lis._emit_led_diff({60: 5, 61: 1})  # only 60 changed
        self.assertEqual(len(lis._out_port.sent), 3)
        last = lis._out_port.sent[-1]
        self.assertEqual((last.type, last.note, last.velocity), ("note_on", 60, 5))

    def test_extinguish_all_sends_off_for_managed_pads(self):
        lis = self._listener([_perf_playlist("a")])
        lis._out_port = _FakeOutPort()
        lis._led_clip_pads = [(60, 1)]
        lis._led_fx_pads = [(70, 0)]
        lis._led_state = {60: 21, 99: 1}
        lis._extinguish_all()
        offed = {m.note for m in lis._out_port.sent}
        self.assertEqual(offed, {60, 70, 99})
        self.assertTrue(all(m.velocity == lis._fmap.off for m in lis._out_port.sent))
        self.assertEqual(lis._led_state, {})


class FeedbackLifecycleTests(_FeedbackListenerTestCase):
    def test_start_opens_output_and_runs_thread_then_extinguishes(self):
        pl = _perf_playlist("a", clip_pads=[("note", 60, 1)])
        lis = self._listener([pl])
        out = _FakeOutPort()
        with (
            mock.patch.object(
                lis,
                "_open_port",
                side_effect=lambda: (
                    setattr(lis, "_midi_port", _FakePort()),
                    setattr(lis, "_opened_port_name", "Launchpad"),
                ),
            ),
            mock.patch.object(midi_control.mido, "get_output_names", return_value=["Launchpad"]),
            mock.patch.object(midi_control.mido, "open_output", return_value=out),
        ):
            lis.start()
        try:
            self.assertIs(lis._out_port, out)
            fb = lis._feedback_thread
            assert fb is not None
            self.assertTrue(fb.is_alive())
            # The clip pad gets painted (loaded) within a couple of poll cycles.
            deadline = time.monotonic() + 1.0
            while not out.sent and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(any(m.note == 60 for m in out.sent))
        finally:
            lis.stop()
        self.assertIsNone(lis._feedback_thread)
        self.assertTrue(out.closed)
        # stop() extinguished pad 60 last (off velocity).
        offs = [m for m in out.sent if m.note == 60 and m.velocity == lis._fmap.off]
        self.assertTrue(offs)

    def test_no_output_ports_disables_feedback_gracefully(self):
        pl = _perf_playlist("a", clip_pads=[("note", 60, 1)])
        lis = self._listener([pl])
        with (
            mock.patch.object(
                lis,
                "_open_port",
                side_effect=lambda: setattr(lis, "_midi_port", _FakePort()),
            ),
            mock.patch.object(midi_control.mido, "get_output_names", return_value=[]),
        ):
            lis.start()
        try:
            self.assertIsNone(lis._out_port)
            self.assertIsNone(lis._feedback_thread)
        finally:
            lis.stop()


if __name__ == "__main__":
    unittest.main()
