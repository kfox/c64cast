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

import unittest

from c64cast.transport import (
    TransportEvent,
    TransportSession,
    _decode_relative_jog,
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


class _FakePlaylist:
    def __init__(self, scene=None, *, transitioning: bool = False):
        self.current = scene
        self.transitioning = transitioning


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

    def test_stop_is_pause_only_this_phase(self):
        scene = _StubScene()
        pl = _FakePlaylist(scene)
        session = TransportSession()
        session.enqueue(TransportEvent(action="stop"))
        _tick(session, pl, 0.0)
        self.assertTrue(scene.paused)

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


if __name__ == "__main__":
    unittest.main()
