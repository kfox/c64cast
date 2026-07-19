"""Tests for TransportSession (MIDI live-tune Phase 2's DJ-style transport
engine): queued-event dispatch, the rw/ff hold-acceleration ramp, and
relative-jog decoding. Pure Python — no MIDI hardware, no VideoScene/PyAV;
dispatch targets are duck-typed stub scenes, matching how TransportSession
reaches a real VideoScene (see scenes.VideoScene's transport_* surface).

MMC SysEx parsing and note-release routing live in midi_control.py (the
message-decoding layer above this module) and are tested in
tests/test_midi_control.py instead.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from c64cast.transport import (
    LoopPresetStore,
    TransportEvent,
    TransportSession,
    _decode_relative_jog,
    loop_preset_key,
    loop_preset_path,
    make_loop_preset_store,
)


class _StubScene:
    """Records every transport_* call so a test can assert on call history
    without a real VideoScene/AVFileSource."""

    def __init__(self, *, position: float = 0.0, duration: float | None = 100.0):
        self._position = position
        self._duration = duration
        self.seeks: list[float] = []
        self.paused = False
        self.toggle_calls = 0
        self.loop_toggle_calls = 0
        self.record_calls = 0
        self.stop_calls = 0
        self.stop_return = False  # what transport_stop() reports (quit requested)
        self.loop_slot_calls: list[tuple[int, bool, bool]] = []  # (slot, save, clear)

    def transport_seek(self, target_s: float) -> None:
        self.seeks.append(target_s)
        self._position = target_s

    def transport_position(self) -> float:
        return self._position

    def transport_duration(self) -> float | None:
        return self._duration

    def transport_pause(self) -> None:
        self.paused = True

    def transport_toggle_pause(self) -> None:
        self.toggle_calls += 1

    def transport_loop_toggle(self) -> None:
        self.loop_toggle_calls += 1

    def transport_record(self) -> None:
        self.record_calls += 1

    def transport_stop(self) -> bool:
        self.stop_calls += 1
        return self.stop_return

    def transport_loop_slot(self, slot: int, *, save: bool, clear: bool) -> None:
        self.loop_slot_calls.append((slot, save, clear))


class _FakePlaylist:
    def __init__(self, scene=None, *, transitioning: bool = False):
        self.current = scene
        self.transitioning = transitioning
        self.stop_event = threading.Event()


def _tick(session: TransportSession, pl: _FakePlaylist, now: float) -> None:
    """Thin wrapper around TransportSession.tick — TransportSession only
    ever reads `.current`/`.transitioning` off its `pl` argument (duck-typed
    by design, see the module docstring), so `_FakePlaylist` deliberately
    isn't a real Playlist. Centralizes the one intentional type mismatch in
    one spot instead of a `# type: ignore` at every call site."""
    session.tick(pl, now)  # type: ignore[arg-type]


class RelativeJogDecodeTest(unittest.TestCase):
    def test_low_half_is_positive(self):
        self.assertEqual(_decode_relative_jog(1), 1)
        self.assertEqual(_decode_relative_jog(63), 63)

    def test_high_half_is_negative(self):
        self.assertEqual(_decode_relative_jog(65), -63)
        self.assertEqual(_decode_relative_jog(127), -1)

    def test_center_and_zero_are_no_motion(self):
        self.assertEqual(_decode_relative_jog(0), 0)
        self.assertEqual(_decode_relative_jog(64), 0)


class DispatchTests(unittest.TestCase):
    def test_play_pause_toggles(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="play_pause"))
        _tick(session, pl, 0.0)
        self.assertEqual(scene.toggle_calls, 1)

    def test_stop_calls_transport_stop(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="stop"))
        _tick(session, pl, 0.0)
        self.assertEqual(scene.stop_calls, 1)

    def test_stop_returning_true_sets_stop_event(self):
        scene = _StubScene()
        scene.stop_return = True
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="stop"))
        _tick(session, pl, 0.0)
        self.assertTrue(pl.stop_event.is_set())

    def test_stop_returning_false_does_not_set_stop_event(self):
        scene = _StubScene()
        scene.stop_return = False
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="stop"))
        _tick(session, pl, 0.0)
        self.assertFalse(pl.stop_event.is_set())

    def test_record_dispatches(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="record"))
        _tick(session, pl, 0.0)
        self.assertEqual(scene.record_calls, 1)

    def test_loop_toggle_dispatches(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="loop_toggle"))
        _tick(session, pl, 0.0)
        self.assertEqual(scene.loop_toggle_calls, 1)

    def test_no_current_scene_is_noop(self):
        pl = _FakePlaylist(None)
        session = TransportSession()
        session.enqueue(TransportEvent(action="play_pause"))
        _tick(session, pl, 0.0)  # must not raise

    def test_transitioning_is_noop(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene, transitioning=True)
        session = TransportSession()
        session.enqueue(TransportEvent(action="play_pause"))
        _tick(session, pl, 0.0)
        self.assertEqual(scene.toggle_calls, 0)

    def test_unknown_scene_type_missing_surface_is_noop(self):
        pl = _FakePlaylist(object())  # no transport_* methods at all
        session = TransportSession()
        session.enqueue(TransportEvent(action="play_pause"))
        _tick(session, pl, 0.0)  # must not raise


class JogTests(unittest.TestCase):
    def test_relative_jog_moves_by_ticks(self):
        scene = _StubScene(position=10.0)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="jog", value=5, mode="rel"))
        _tick(session, pl, 0.0)
        self.assertEqual(scene.seeks, [15.0])  # +5 ticks * 1.0s/tick

    def test_relative_jog_negative_direction(self):
        scene = _StubScene(position=10.0)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="jog", value=127, mode="rel"))  # -1 tick
        _tick(session, pl, 0.0)
        self.assertEqual(scene.seeks, [9.0])

    def test_relative_jog_center_is_noop(self):
        scene = _StubScene(position=10.0)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="jog", value=64, mode="rel"))
        _tick(session, pl, 0.0)
        self.assertEqual(scene.seeks, [])

    def test_absolute_jog_maps_value_over_duration(self):
        scene = _StubScene(position=0.0, duration=100.0)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="jog", value=127, mode="abs"))
        _tick(session, pl, 0.0)
        self.assertAlmostEqual(scene.seeks[0], 100.0, places=3)

    def test_absolute_jog_with_no_duration_targets_zero(self):
        scene = _StubScene(position=0.0, duration=None)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="jog", value=64, mode="abs"))
        _tick(session, pl, 0.0)
        self.assertEqual(scene.seeks[0], 0.0)


class HoldRampTests(unittest.TestCase):
    """rw/ff press starts a ramp that accelerates the longer the note is
    held, released on note-off; the ramp is applied once per tick()."""

    def test_press_then_tick_seeks_backward_for_rw(self):
        scene = _StubScene(position=50.0)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        _tick(session, pl, 0.0)  # prime _last_tick so the next tick has a dt
        session.enqueue(TransportEvent(action="rw", pressed=True))
        _tick(session, pl, 0.1)
        self.assertEqual(len(scene.seeks), 1)
        self.assertLess(scene.seeks[0], 50.0)

    def test_press_then_tick_seeks_forward_for_ff(self):
        scene = _StubScene(position=50.0)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        _tick(session, pl, 0.0)
        session.enqueue(TransportEvent(action="ff", pressed=True))
        _tick(session, pl, 0.1)
        self.assertEqual(len(scene.seeks), 1)
        self.assertGreater(scene.seeks[0], 50.0)

    def test_release_stops_the_ramp(self):
        scene = _StubScene(position=50.0)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        _tick(session, pl, 0.0)
        session.enqueue(TransportEvent(action="rw", pressed=True))
        _tick(session, pl, 0.1)
        session.enqueue(TransportEvent(action="rw", pressed=False))
        _tick(session, pl, 0.2)
        # The release event itself causes no seek; no further ramp ticks
        # follow it in this test, so exactly the one seek from the press
        # tick is recorded.
        self.assertEqual(len(scene.seeks), 1)

    def test_ramp_accelerates_with_hold_duration(self):
        scene = _StubScene(position=1000.0, duration=None)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        _tick(session, pl, 0.0)
        session.enqueue(TransportEvent(action="ff", pressed=True))
        _tick(session, pl, 0.1)
        delta_early = scene.seeks[-1] - 1000.0
        _tick(session, pl, 2.0)  # long hold — well past several speed-doublings
        delta_late = scene.seeks[-1] - scene.seeks[-2]
        self.assertGreater(delta_late, delta_early)

    def test_ramp_speed_is_capped(self):
        # At a very long hold duration the ramp must not exceed the cap
        # (30x media-seconds per real second) — no runaway seek.
        scene = _StubScene(position=100_000.0, duration=None)
        pl = _FakePlaylist(scene)
        session = TransportSession()
        _tick(session, pl, 0.0)
        session.enqueue(TransportEvent(action="ff", pressed=True))
        _tick(session, pl, 0.001)  # start the hold
        _tick(session, pl, 60.0)  # absurdly long hold
        dt = 60.0 - 0.001
        delta = scene.seeks[-1] - scene.seeks[-2]
        self.assertLessEqual(delta, 30.0 * dt + 1e-6)

    def test_held_bookkeeping_survives_no_current_scene(self):
        # A press recorded while no scene is current must still ramp once a
        # scene becomes current mid-hold (design: hold state is tracked
        # regardless of whether a scene is on screen right now).
        pl = _FakePlaylist(None)
        session = TransportSession()
        _tick(session, pl, 0.0)
        session.enqueue(TransportEvent(action="ff", pressed=True))
        _tick(session, pl, 0.1)  # no scene yet — no seek, but held bookkeeping sticks
        scene = _StubScene(position=10.0)
        pl.current = scene
        _tick(session, pl, 0.2)
        self.assertEqual(len(scene.seeks), 1)
        self.assertGreater(scene.seeks[0], 10.0)


class RecordStopChordTests(unittest.TestCase):
    """loop_slot pad presses resolve save/clear against whether Stop/Record
    is currently held: Stop-held+pad = save, Record-held+pad = clear,
    neither = plain recall (MIDI live-tune Phase 3's explicit-save design —
    no implicit auto-store). Held state auto-expires after
    _CHORD_HOLD_WINDOW_S even with no release, since an MMC-sourced
    Record/Stop press never generates one."""

    def test_no_hold_is_plain_recall(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="loop_slot", slot=3))
        _tick(session, pl, 0.0)
        self.assertEqual(scene.loop_slot_calls, [(3, False, False)])

    def test_record_held_clears(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="record", pressed=True))
        _tick(session, pl, 0.0)
        session.enqueue(TransportEvent(action="loop_slot", slot=5))
        _tick(session, pl, 0.5)
        self.assertEqual(scene.loop_slot_calls, [(5, False, True)])

    def test_stop_held_saves(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="stop", pressed=True))
        _tick(session, pl, 0.0)
        session.enqueue(TransportEvent(action="loop_slot", slot=2))
        _tick(session, pl, 0.5)
        self.assertEqual(scene.loop_slot_calls, [(2, True, False)])

    def test_release_clears_held_state_immediately(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="record", pressed=True))
        _tick(session, pl, 0.0)
        session.enqueue(TransportEvent(action="record", pressed=False))
        _tick(session, pl, 0.1)
        session.enqueue(TransportEvent(action="loop_slot", slot=1))
        _tick(session, pl, 0.2)
        self.assertEqual(scene.loop_slot_calls, [(1, False, False)])

    def test_held_state_expires_without_release(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="record", pressed=True))
        _tick(session, pl, 0.0)  # e.g. an MMC record press — no release ever follows
        session.enqueue(TransportEvent(action="loop_slot", slot=7))
        _tick(session, pl, 10.0)  # long past _CHORD_HOLD_WINDOW_S (5.0s)
        self.assertEqual(scene.loop_slot_calls, [(7, False, False)])

    def test_record_wins_when_both_held(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="stop", pressed=True))
        session.enqueue(TransportEvent(action="record", pressed=True))
        _tick(session, pl, 0.0)
        session.enqueue(TransportEvent(action="loop_slot", slot=4))
        _tick(session, pl, 0.1)
        self.assertEqual(scene.loop_slot_calls, [(4, False, True)])


class LoopPresetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # A not-yet-created subdir: the store must mkdir on first write.
        self.store = LoopPresetStore(
            Path(self._tmp.name) / "sub" / "loop-x.json", video_ref="clip.mp4", size=12345
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_reads_empty(self):
        self.assertEqual(self.store.load(), {})

    def test_save_load_round_trip_persists_to_disk(self):
        self.store.save(1, 12.3, 45.6)
        fresh = LoopPresetStore(self.store.path, video_ref="clip.mp4", size=12345)
        self.assertEqual(fresh.load(), {"1": {"a": 12.3, "b": 45.6}})

    def test_b_none_round_trips_as_loop_to_eof(self):
        self.store.save(2, 5.0, None)
        self.assertEqual(self.store.load(), {"2": {"a": 5.0, "b": None}})

    def test_delete(self):
        self.store.save(3, 1.0, 2.0)
        self.store.delete(3)
        self.assertEqual(self.store.load(), {})

    def test_delete_missing_slot_is_noop(self):
        self.store.delete(9)  # must not raise
        self.assertEqual(self.store.load(), {})

    def test_corrupt_file_reads_empty(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("not json", encoding="utf-8")
        self.assertEqual(self.store.load(), {})

    def test_atomic_write_leaves_no_temp_files(self):
        self.store.save(1, 1.0, 2.0)
        leftovers = list(self.store.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_writes_schema_and_video_metadata(self):
        self.store.save(1, 1.0, 2.0)
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["video"], "clip.mp4")
        self.assertEqual(data["size"], 12345)


class LoopPresetKeyTests(unittest.TestCase):
    """loop_preset_key/loop_preset_path: path-move-tolerant identity for
    local files (basename+size), URL-identity for URL-backed scenes."""

    def test_same_basename_and_size_yields_same_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            dir_a = Path(tmp) / "a"
            dir_b = Path(tmp) / "b"
            dir_a.mkdir()
            dir_b.mkdir()
            fa = dir_a / "clip.mp4"
            fb = dir_b / "clip.mp4"
            fa.write_bytes(b"x" * 100)
            fb.write_bytes(b"x" * 100)
            self.assertEqual(loop_preset_key(str(fa)), loop_preset_key(str(fb)))

    def test_different_size_yields_different_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "clip.mp4"
            f.write_bytes(b"x" * 100)
            key1 = loop_preset_key(str(f))
            f.write_bytes(b"x" * 200)
            key2 = loop_preset_key(str(f))
            self.assertNotEqual(key1, key2)

    def test_url_hashes_the_url_itself(self):
        key1 = loop_preset_key("https://example.com/clip.mp4")
        key2 = loop_preset_key("https://example.com/clip.mp4")
        self.assertEqual(key1, key2)
        self.assertNotEqual(key1, loop_preset_key("https://example.com/other.mp4"))

    def test_loop_preset_path_is_under_loops_dir(self):
        p = loop_preset_path("some/clip.mp4")
        self.assertEqual(p.parent.name, "loops")
        self.assertTrue(p.name.endswith(".json"))

    def test_make_loop_preset_store_uses_loop_preset_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"C64CAST_DATA_DIR": tmp}):
                store = make_loop_preset_store("clip.mp4")
                self.assertEqual(store.path, loop_preset_path("clip.mp4"))
                # The store lands under the redirected data dir.
                self.assertTrue(str(store.path).startswith(tmp))


if __name__ == "__main__":
    unittest.main()
