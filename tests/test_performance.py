"""Tests for the clip-launch grid (Live DJ/VJ Phase 2 — performance.py).

Offline and deterministic: the launch engine (`PerformanceSession`) is driven
against a lightweight fake playlist (a stand-in for the `_perf_swap_scene` /
`build_performance_scene` / `pl.tempo` surface it actually uses) with a
hand-controlled beat grid, so quantize boundaries and launch semantics are
asserted without real time, mido, scenes, or hardware. The background build
thread is real but fast (the factory returns a stub scene), so tests pump
`service()` until the state settles.

Config-level validation of `[[performance.clips]]` and the `clip_scene_cfg`
scene-spec derivation live in config.py and are covered here too. The
midi_control `clip_launch` action + auto-mapping is exercised through the same
real-`mido`-guarded listener path the rest of the suite uses.
"""

from __future__ import annotations

import time
import unittest
from typing import Any
from unittest import mock

from c64cast import config as cfgmod
from c64cast.performance import ClipEvent, PerformanceSession


class _FakeTempo:
    """A settable beat grid — the test moves `beat_phase`/`bar_phase` by hand."""

    def __init__(self) -> None:
        self.beat_phase = 0.0
        self.bar_phase = 0.0
        self.running = True


class _FakeScene:
    def __init__(self, label: str) -> None:
        self.name = label
        self.is_done = False
        self.setups = 0
        self.teardowns = 0

    def setup(self) -> None:  # pragma: no cover - not called directly by engine
        self.setups += 1

    def teardown(self) -> None:
        self.teardowns += 1


class _FakePlaylist:
    """Implements just the surface PerformanceSession touches."""

    def __init__(self, scene_labels: tuple[str, ...] = ("A", "B")) -> None:
        self.tempo = _FakeTempo()
        self.scenes = [_FakeScene(lbl) for lbl in scene_labels]
        self.index = 0
        self.current: Any = self.scenes[0]
        self.transitioning = False
        self.ensemble = None
        self.swaps: list[str] = []  # labels swapped in, in order
        # slot -> label to hand back from the build factory (each build returns a
        # distinct scene instance so teardown/setup counts are per-instance).
        self.clip_labels: dict[int, str] = {}
        self.build_performance_scene = self._build

    def _build(self, clip: dict[str, Any]) -> _FakeScene:
        slot = clip.get("slot", 0)
        return _FakeScene(self.clip_labels.get(slot, f"clip{slot}"))

    def _perf_swap_scene(self, new_scene: _FakeScene) -> bool:
        if self.current is not None:
            self.current.teardown()
        new_scene.setups += 1
        self.current = new_scene
        self.swaps.append(new_scene.name)
        return True


def _pump(session: PerformanceSession, pl: Any, *, times: int = 5) -> bool:
    """Call service() a few times (letting the background build thread finish),
    returning the last ownership result."""
    owned = False
    for _ in range(times):
        owned = session.service(pl)
        time.sleep(0.005)
    return owned


class LaunchSemanticsTest(unittest.TestCase):
    def _session(self, clips: list[dict[str, Any]]) -> tuple[PerformanceSession, Any]:
        pl = _FakePlaylist()
        for c in clips:
            pl.clip_labels[c["slot"]] = f"S{c['slot']}"
        return PerformanceSession(clips), pl

    def test_trigger_launches_at_bar_boundary_not_before(self):
        s, pl = self._session([{"slot": 1, "type": "generative", "quantize": "bar"}])
        pl.tempo.bar_phase = 2.3
        s.enqueue(ClipEvent(slot=1, pressed=True))
        # Build completes, but the bar hasn't turned over — no swap yet.
        owned = _pump(s, pl)
        self.assertFalse(owned)
        self.assertEqual(pl.swaps, [])
        # Cross into the next whole bar -> swap on the next service().
        pl.tempo.bar_phase = 3.01
        owned = _pump(s, pl)
        self.assertTrue(owned)
        self.assertEqual(pl.swaps, ["S1"])
        self.assertEqual(s.active_slot, 1)

    def test_quantize_off_launches_immediately(self):
        s, pl = self._session([{"slot": 1, "type": "generative", "quantize": "off"}])
        s.enqueue(ClipEvent(slot=1, pressed=True))
        self.assertTrue(_pump(s, pl))
        self.assertEqual(pl.swaps, ["S1"])

    def test_stopped_clock_falls_back_to_immediate(self):
        s, pl = self._session([{"slot": 1, "type": "generative", "quantize": "bar"}])
        pl.tempo.running = False  # a bar boundary would never arrive
        s.enqueue(ClipEvent(slot=1, pressed=True))
        self.assertTrue(_pump(s, pl))
        self.assertEqual(pl.swaps, ["S1"])

    def test_gate_returns_to_playlist_on_release(self):
        s, pl = self._session(
            [{"slot": 1, "type": "generative", "quantize": "off", "launch": "gate"}]
        )
        base = pl.current
        s.enqueue(ClipEvent(slot=1, pressed=True))
        self.assertTrue(_pump(s, pl))
        self.assertEqual(s.active_slot, 1)
        # Release -> restore the playlist scene that was interrupted.
        s.enqueue(ClipEvent(slot=1, pressed=False))
        owned = _pump(s, pl)
        self.assertFalse(owned)
        self.assertIsNone(s.active_slot)
        self.assertIs(pl.current, base)
        self.assertEqual(pl.swaps[-1], base.name)

    def test_toggle_latches_on_and_off(self):
        s, pl = self._session(
            [{"slot": 1, "type": "generative", "quantize": "off", "launch": "toggle"}]
        )
        s.enqueue(ClipEvent(slot=1, pressed=True))
        self.assertTrue(_pump(s, pl))
        self.assertEqual(s.active_slot, 1)
        # A release does NOT end a toggle clip (unlike gate).
        s.enqueue(ClipEvent(slot=1, pressed=False))
        self.assertTrue(_pump(s, pl))
        self.assertEqual(s.active_slot, 1)
        # A second press latches it off -> back to the playlist.
        s.enqueue(ClipEvent(slot=1, pressed=True))
        self.assertFalse(_pump(s, pl))
        self.assertIsNone(s.active_slot)

    def test_trigger_one_shot_restores_when_scene_finishes(self):
        s, pl = self._session([{"slot": 1, "type": "generative", "quantize": "off", "loop": False}])
        base = pl.current
        s.enqueue(ClipEvent(slot=1, pressed=True))
        self.assertTrue(_pump(s, pl))
        clip_scene = pl.current
        # The clip plays through, then reports done -> restore the playlist.
        clip_scene.is_done = True
        self.assertFalse(_pump(s, pl))
        self.assertIs(pl.current, base)

    def test_loop_resetups_the_same_scene_on_done(self):
        s, pl = self._session([{"slot": 1, "type": "generative", "quantize": "off", "loop": True}])
        s.enqueue(ClipEvent(slot=1, pressed=True))
        self.assertTrue(_pump(s, pl))
        clip_scene = pl.current
        before = list(pl.swaps)
        clip_scene.is_done = True
        self.assertTrue(s.service(pl))  # loop -> re-swap same instance, still owns
        self.assertIs(pl.current, clip_scene)
        self.assertEqual(pl.swaps, before + [clip_scene.name])

    def test_gate_over_a_loop_returns_to_the_prior_clip(self):
        s, pl = self._session(
            [
                {"slot": 1, "type": "generative", "quantize": "off", "loop": True},
                {"slot": 2, "type": "generative", "quantize": "off", "launch": "gate"},
            ]
        )
        s.enqueue(ClipEvent(slot=1, pressed=True))
        self.assertTrue(_pump(s, pl))
        self.assertEqual(s.active_slot, 1)
        # Gate slot 2 over the running loop.
        s.enqueue(ClipEvent(slot=2, pressed=True))
        self.assertTrue(_pump(s, pl))
        self.assertEqual(s.active_slot, 2)
        # Release -> rebuild + return to the prior clip (slot 1), not the playlist.
        s.enqueue(ClipEvent(slot=2, pressed=False))
        self.assertTrue(_pump(s, pl))
        self.assertEqual(s.active_slot, 1)

    def test_reconcile_relinquishes_when_current_torn_down(self):
        # pause/reload/broadcast tear down pl.current out from under an active
        # clip; the next service() must relinquish ownership (not think it still
        # owns a torn-down scene, which would strand the run loop on `current is
        # None`).
        s, pl = self._session([{"slot": 1, "type": "generative", "quantize": "off"}])
        s.enqueue(ClipEvent(slot=1, pressed=True))
        self.assertTrue(_pump(s, pl))
        self.assertEqual(s.active_slot, 1)
        pl.current = None  # simulate _handle_pause tearing the clip down
        self.assertFalse(s.service(pl))
        self.assertIsNone(s.active_slot)
        self.assertIsNone(s.armed_slot)

    def test_unknown_slot_is_ignored(self):
        s, pl = self._session([{"slot": 1, "type": "generative"}])
        s.enqueue(ClipEvent(slot=99, pressed=True))
        self.assertFalse(_pump(s, pl))
        self.assertEqual(pl.swaps, [])

    def test_empty_session_is_inert(self):
        pl: Any = _FakePlaylist()
        s = PerformanceSession(None)
        self.assertFalse(s.has_clips)
        self.assertFalse(s.service(pl))


class ClipPadMappingTest(unittest.TestCase):
    def test_pads_surface_for_auto_mapping(self):
        s = PerformanceSession(
            [
                {"slot": 1, "type": "video", "pad": 60},
                {"slot": 2, "type": "generative", "pad": 61, "pad_type": "pc"},
                {"slot": 3, "type": "blank"},  # no pad -> not auto-mapped
            ]
        )
        self.assertEqual(
            sorted(s.clip_pad_mappings()),
            [("note", 60, 1), ("pc", 61, 2)],
        )


class ClipConfigTest(unittest.TestCase):
    def test_scene_cfg_derivation_strips_launch_keys(self):
        sc = cfgmod.clip_scene_cfg(
            {
                "slot": 1,
                "type": "generative",
                "source": "tunnel",
                "display": "mhires",
                "launch": "gate",
                "quantize": "beat",
                "pad": 60,
                "loop": True,
            }
        )
        self.assertEqual(sc.type, "generative")
        self.assertEqual(sc.source, "tunnel")
        self.assertEqual(sc.display, "mhires")
        # A looping continuous scene is pinned to run-forever.
        self.assertEqual(sc.duration_s, 0.0)

    def test_video_clip_keeps_unset_duration(self):
        sc = cfgmod.clip_scene_cfg({"slot": 1, "type": "video", "file": "x.mp4", "loop": True})
        self.assertIsNone(sc.duration_s)  # video rejects duration_s; engine re-setups to loop

    def test_one_shot_keeps_default_duration(self):
        sc = cfgmod.clip_scene_cfg({"slot": 1, "type": "generative", "loop": False})
        self.assertIsNone(sc.duration_s)  # scene-type default applies

    def test_validate_rejects_bad_clips(self):
        cases = [
            [{"type": "video"}],  # no slot
            [{"slot": 0}],  # slot < 1
            [{"slot": 1}, {"slot": 1}],  # dup
            [{"slot": 1, "type": "nope"}],  # bad type
            [{"slot": 1, "launch": "x"}],  # bad launch
            [{"slot": 1, "quantize": "x"}],  # bad quantize
            [{"slot": 1, "pad_type": "x"}],  # bad pad_type
            [{"slot": 1, "pad": 999}],  # pad out of range
            [{"slot": 1, "loop": "yes"}],  # non-bool loop
            [{"slot": 1, "overlays": []}],  # denied scene field
            [{"slot": 1, "flie": "x"}],  # unknown key
        ]
        for bad in cases:
            with self.assertRaises(ValueError, msg=f"should reject {bad!r}"):
                cfgmod._validate_clips(bad)

    def test_validate_accepts_a_good_grid(self):
        cfgmod._validate_clips(
            [
                {"slot": 1, "type": "video", "file": "a.mp4", "pad": 60, "launch": "trigger"},
                {"slot": 2, "type": "generative", "source": "plasma", "quantize": "beat"},
            ]
        )

    def test_load_parses_clips_from_toml(self):
        import tempfile

        toml = (
            "[performance]\n"
            'tempo_source = "internal"\n'
            "[[performance.clips]]\n"
            "slot = 1\n"
            'type = "generative"\n'
            'source = "plasma"\n'
            "pad = 60\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml)
            path = f.name
        try:
            cfg = cfgmod.load(path)
        finally:
            import os

            os.unlink(path)
        self.assertEqual(len(cfg.performance.clips), 1)
        self.assertEqual(cfg.performance.clips[0]["slot"], 1)


try:
    import mido as _mido

    mido: Any = _mido
    HAVE_MIDI = True
except ImportError:
    mido = None
    HAVE_MIDI = False


def _fake_playlist_with_perf(name: str, clips: list[dict[str, Any]]) -> Any:
    pl = mock.MagicMock(name=f"playlist-{name}")
    pl.name = name
    pl.performance = PerformanceSession(clips)
    return pl


@unittest.skipUnless(HAVE_MIDI, "mido not installed")
class MidiClipLaunchTest(unittest.TestCase):
    def _listener(self, playlists, cc_map=None):
        from c64cast.midi_control import MidiControlListener

        return MidiControlListener(
            {pl.name: pl for pl in playlists},
            cc_map or [],
        )

    def test_explicit_cc_map_enqueues_clip_event(self):
        pl = _fake_playlist_with_perf("a", [{"slot": 3, "type": "generative"}])
        cc_map = [{"type": "note", "number": 48, "action": "clip_launch", "slot": 3}]
        lis = self._listener([pl], cc_map)
        lis._dispatch(mido.Message("note_on", note=48, velocity=100))
        ev = pl.performance._queue.get_nowait()
        self.assertEqual((ev.slot, ev.pressed), (3, True))
        # Release is delivered too (gate/toggle need it).
        lis._dispatch(mido.Message("note_on", note=48, velocity=0))
        ev = pl.performance._queue.get_nowait()
        self.assertEqual((ev.slot, ev.pressed), (3, False))

    def test_clip_pad_auto_maps_without_a_cc_map_entry(self):
        pl = _fake_playlist_with_perf("a", [{"slot": 5, "type": "generative", "pad": 64}])
        lis = self._listener([pl], [])
        lis._add_clip_pad_mappings()
        lis._dispatch(mido.Message("note_on", note=64, velocity=100))
        ev = pl.performance._queue.get_nowait()
        self.assertEqual((ev.slot, ev.pressed), (5, True))

    def test_explicit_cc_map_wins_over_clip_pad(self):
        # A clip's own pad and an explicit cc_map entry collide on note 64;
        # the explicit entry (slot 9) must win.
        pl = _fake_playlist_with_perf("a", [{"slot": 5, "type": "generative", "pad": 64}])
        # The listener parses the explicit cc_map (note 64 -> slot 9) at
        # construction; the auto-mapper must not overwrite it.
        cc_map = [{"type": "note", "number": 64, "action": "clip_launch", "slot": 9}]
        lis = self._listener([pl], cc_map)
        lis._add_clip_pad_mappings()
        lis._dispatch(mido.Message("note_on", note=64, velocity=100))
        ev = pl.performance._queue.get_nowait()
        self.assertEqual(ev.slot, 9)


if __name__ == "__main__":
    unittest.main()
