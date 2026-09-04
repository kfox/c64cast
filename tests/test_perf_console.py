"""Tests for the phone/web performance console (Live DJ/VJ Phase 5).

The bridge tests (`PerfBridgeTest`) drive `PerfBridge` directly against a fake
playlist exposing exactly the surface it reads/writes — no FastAPI needed. The
end-to-end HTTP tests (`PerfEndpointsTest`) drive the real control-plane app via
TestClient and skip when fastapi/httpx isn't installed, mirroring
tests/test_control_plane.py.

Not covered here, and deliberately: `perf_ws`'s guard around the state-frame
build (a frame that raises logs and closes rather than going quiet, which is how
a stale fake once turned into a suite that hung instead of failing). Asserting it
needs the *server* to close an accepted socket, and `TestClient`'s websocket
teardown blocks on that — the assertion would reintroduce the hang it exists to
prevent. The behavior was checked by hand against an exploding playlist."""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportOptionalCall=false
from __future__ import annotations

import inspect
import re
import threading
import unittest
import warnings
from typing import Any
from unittest.mock import patch

from c64cast.control import perf_console
from c64cast.control.auth import ROLE_FULL, ROLE_VIEWER, SCOPE_ROLE_KEY
from c64cast.control.perf_console import (
    _PERF_HTML,
    MAX_COMMAND_BYTES,
    MAX_TARGET_CHARS,
    TRANSPORT_VERBS,
    PerfBridge,
    SocketReader,
    _beats_remaining,
    _system_state,
    with_role,
)
from c64cast.control.transport import JsonSlotStore, LiveTuneTracker, TransportEvent
from c64cast.scenes.effects import TrailsEffect

try:
    import fastapi  # noqa: F401

    # Silenced like test_control_plane's copy — the httpx2 deprecation is a
    # dependency decision, not per-worker test output.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False
    TestClient = None  # type: ignore[misc,assignment]


class _FakeTempo:
    """Deterministic beat grid: phases are fixed values, not time-driven."""

    def __init__(self) -> None:
        self.bpm = 128.0
        self.running = True
        self.source = "internal"
        self.beats_per_bar = 4
        self._beat = 5.5
        self._bar = 1.375
        self.taps = 0

    def beat_phase_at(self, now: float | None = None) -> float:
        return self._beat

    def bar_phase_at(self, now: float | None = None) -> float:
        return self._bar

    def tap(self, now: float) -> None:
        self.taps += 1


class _FakePerf:
    def __init__(self, clips: list[dict[str, Any]] | None = None) -> None:
        self.active_slot: int | None = None
        self.armed_slot: int | None = None
        self.armed_detail: tuple[int, str, float, float] | None = None
        self._clips = clips or []
        self.events: list[tuple[int, bool]] = []
        self.look_events: list[tuple[int, bool]] = []
        self.looks: list[int] = []

    def clips_info(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._clips]

    def enqueue(self, event: Any) -> None:
        self.events.append((event.slot, event.pressed))

    def enqueue_look(self, slot: int, *, save: bool) -> None:
        self.look_events.append((slot, save))

    def saved_look_slots(self) -> list[int]:
        return list(self.looks)


class _FakeScene:
    def __init__(self, name: str, effects: list[Any] | None = None, source: Any = None) -> None:
        self.name = name
        self.effects = effects or []
        self.duration_s = 30.0
        if source is not None:
            self.source = source


class _FakeTransportScene(_FakeScene):
    """A scene declaring the DJ transport surface (Live DJ/VJ Phase 7) —
    `_FakeScene` has none, which is what exercises `_transport_dict`'s "no
    transport bar for this scene" branch."""

    def __init__(
        self,
        name: str = "clip",
        *,
        paused: bool = False,
        position: float = 12.5,
        duration: float | None = 60.0,
        loop: dict[str, Any] | None = None,
        loop_slots: list[int] | None = None,
    ) -> None:
        super().__init__(name)
        self._paused = paused
        self._position = position
        self._duration = duration
        self._loop = loop or {"state": "none", "a": None, "b": None}
        self._loop_slots = loop_slots or []

    def transport_is_paused(self) -> bool:
        return self._paused

    def transport_position(self) -> float:
        return self._position

    def transport_duration(self) -> float | None:
        return self._duration

    def transport_loop_info(self) -> dict[str, Any]:
        return self._loop

    def transport_loop_slots(self) -> list[int]:
        return self._loop_slots


class _FakeTransport:
    def __init__(self) -> None:
        self.events: list[TransportEvent] = []

    def enqueue(self, event: TransportEvent) -> None:
        self.events.append(event)


class _FakeSource:
    """A scene generator declaring one real live-tune target. Named after the
    range `introspect.live_targets()` reports for `source.scale`, so the panel
    tests measure the same knob the picker offers."""

    LIVE_PARAMS = {"scale": (0.1, 4.0)}

    def __init__(self) -> None:
        self.scale = 2.05


class _FakePlaylist:
    def __init__(
        self,
        *,
        clips: list[dict[str, Any]] | None = None,
        effects: list[Any] | None = None,
        scene_name: str = "demo",
        source: Any = None,
        config_path: str = "",
        scene: Any = None,
    ) -> None:
        self.tempo = _FakeTempo()
        self.performance = _FakePerf(clips)
        self.current = scene if scene is not None else _FakeScene(scene_name, effects, source)
        self.scenes = [self.current]
        self.index = 0
        self.pause_event = threading.Event()
        self.resume_event = threading.Event()
        self.skip_event = threading.Event()
        # The real tracker: it is a pure in-memory recorder with no hardware
        # behind it, and the save-back block the console renders is exactly
        # what it reports — a fake would only be able to agree with itself.
        self.live_tracker = LiveTuneTracker()
        self.config_path = config_path
        self.osd: list[str] = []
        self.jumps: list[tuple[int, bool]] = []
        # A real TransportSession would getattr-probe the scene and touch a
        # frame; the bridge only ever enqueues onto it, so a plain recorder is
        # enough to assert what was queued without a playlist thread to drain it.
        self.transport = _FakeTransport()

    def post_osd(self, text: str) -> None:
        self.osd.append(text)

    def request_jump(self, index: int, *, skip_interstitial: bool = True) -> None:
        self.jumps.append((index, skip_interstitial))


class _AdvancingPlaylist(_FakePlaylist):
    """A playlist whose ``current`` hands back a different scene on every read.

    `Playlist._advance` writes `index` and `current` as two separate statements
    (with a teardown between them, during which `current` is None), so a frame
    builder that re-read `current` could describe two scenes in one snapshot.
    This holds that window permanently open: scene A has an effect chain and no
    source, scene B has a source and no chain, so a frame built from two reads
    disagrees with itself visibly."""

    def __init__(self) -> None:
        self.reads = 0
        self._scene_a = _FakeScene("A", [TrailsEffect(decay=0.0)])
        self._scene_b = _FakeScene("B", [], _FakeSource())
        super().__init__(scene=self._scene_a)
        self.scenes = [self._scene_a, self._scene_b]
        self.reads = 0  # the base __init__ read `current` on the way past

    @property
    def current(self) -> Any:
        self.reads += 1
        return self._scene_a if self.reads == 1 else self._scene_b

    @current.setter
    def current(self, scene: Any) -> None:
        """Swallowed: the pair above is what this fake is for."""


def _bridge(**kw: Any) -> tuple[PerfBridge, _FakePlaylist]:
    pl = _FakePlaylist(**kw)
    return PerfBridge(lambda: [("c64cast", pl)]), pl


class PerfBridgeTest(unittest.TestCase):
    def test_state_shape_single_system(self):
        clips = [
            {
                "slot": 1,
                "name": "Trails",
                "type": "generative",
                "launch": "trigger",
                "quantize": "bar",
                "loop": True,
                "pad": 40,
                "pad_type": "note",
            },
        ]
        bridge, pl = _bridge(clips=clips, effects=[TrailsEffect(decay=0.48)])
        st = bridge.state()
        self.assertFalse(st["multi"])
        self.assertEqual(len(st["systems"]), 1)
        sys = st["systems"][0]
        self.assertEqual(sys["name"], "c64cast")
        self.assertEqual(sys["current_scene"], "demo")
        self.assertEqual(sys["tempo"]["bpm"], 128.0)
        self.assertEqual(sys["tempo"]["beat_phase"], 5.5)
        # Clip carries a rendered state.
        self.assertEqual(sys["clips"][0]["state"], "loaded")
        # Effect rack generated from the layer's own LIVE_PARAMS.
        fx = sys["effects"][0]
        self.assertEqual(fx["name"], "trails")
        self.assertTrue(fx["enabled"])
        self.assertEqual(fx["params"][0]["name"], "decay")
        self.assertAlmostEqual(fx["params"][0]["value"], 0.48, places=4)
        # norm = 0.48 / 0.96 = 0.5
        self.assertAlmostEqual(fx["params"][0]["norm"], 0.5, places=3)
        # Saved-look slots surface for the console's look pads (Phase 6).
        self.assertEqual(sys["looks"], [])

    def test_saved_looks_surface_in_state(self):
        bridge, pl = _bridge()
        pl.performance.looks = [2, 5]
        self.assertEqual(_system_state("c64cast", pl)["looks"], [2, 5])

    def test_look_enqueues_save_and_recall(self):
        bridge, pl = _bridge()
        self.assertTrue(bridge.look(None, 3, save=True))
        self.assertTrue(bridge.look(None, 3, save=False))
        self.assertEqual(pl.performance.look_events, [(3, True), (3, False)])
        self.assertFalse(bridge.look("nope", 1, save=True))

    def test_clip_state_reflects_active_and_armed(self):
        clips = [
            {"slot": 1, "name": "A"},
            {"slot": 2, "name": "B"},
            {"slot": 3, "name": "C"},
        ]
        bridge, pl = _bridge(clips=clips)
        pl.performance.active_slot = 1
        pl.performance.armed_slot = 2
        states = {c["slot"]: c["state"] for c in _system_state("c64cast", pl)["clips"]}
        self.assertEqual(states, {1: "active", 2: "armed", 3: "loaded"})

    def test_launch_enqueues_clip_event(self):
        bridge, pl = _bridge(clips=[{"slot": 2, "name": "B"}])
        self.assertTrue(bridge.launch(None, 2, pressed=True))
        self.assertTrue(bridge.launch(None, 2, pressed=False))
        self.assertEqual(pl.performance.events, [(2, True), (2, False)])

    def test_launch_unknown_system_returns_false(self):
        bridge, pl = _bridge()
        self.assertFalse(bridge.launch("nope", 1))
        self.assertEqual(pl.performance.events, [])

    def test_tap_hits_the_grid(self):
        bridge, pl = _bridge()
        self.assertTrue(bridge.tap(None))
        self.assertEqual(pl.tempo.taps, 1)

    def test_fx_bypass_toggles_enabled(self):
        eff = TrailsEffect()
        bridge, pl = _bridge(effects=[eff])
        self.assertTrue(eff.enabled)
        bridge.fx_bypass(None, 0, False)
        self.assertFalse(eff.enabled)
        bridge.fx_bypass(None, 0, True)
        self.assertTrue(eff.enabled)

    def test_fx_bypass_out_of_range_is_noop_but_ok(self):
        eff = TrailsEffect()
        bridge, pl = _bridge(effects=[eff])
        self.assertTrue(bridge.fx_bypass(None, 9, False))  # addressed a valid system
        self.assertTrue(eff.enabled)  # untouched

    def test_fx_param_scales_into_range(self):
        eff = TrailsEffect(decay=0.0)
        bridge, pl = _bridge(effects=[eff])
        bridge.fx_param(None, 0, "decay", 0.5)  # 0.5 * 0.96
        self.assertAlmostEqual(eff.decay, 0.48, places=4)
        bridge.fx_param(None, 0, "decay", 2.0)  # clamps to 1.0 -> 0.96
        self.assertAlmostEqual(eff.decay, 0.96, places=4)

    def test_fx_param_unknown_param_is_noop(self):
        eff = TrailsEffect(decay=0.3)
        bridge, pl = _bridge(effects=[eff])
        self.assertTrue(bridge.fx_param(None, 0, "nope", 0.9))
        self.assertAlmostEqual(eff.decay, 0.3, places=4)

    def test_apply_dispatch(self):
        eff = TrailsEffect(decay=0.0)
        bridge, pl = _bridge(clips=[{"slot": 1, "name": "A"}], effects=[eff])
        bridge.apply({"action": "launch", "slot": 1})
        bridge.apply({"action": "tap"})
        bridge.apply({"action": "fx", "layer": 0, "enabled": False})
        bridge.apply({"action": "fx", "layer": 0, "param": "decay", "value": 0.25})
        self.assertEqual(pl.performance.events, [(1, True)])
        self.assertEqual(pl.tempo.taps, 1)
        self.assertFalse(eff.enabled)
        self.assertAlmostEqual(eff.decay, 0.24, places=4)
        bridge.apply({"action": "look", "slot": 4, "save": True})
        bridge.apply({"action": "look", "slot": 4})  # recall (save defaults False)
        self.assertEqual(pl.performance.look_events, [(4, True), (4, False)])
        self.assertFalse(bridge.apply({"action": "bogus"}))

    def test_live_panel_lists_only_what_the_scene_declares(self):
        # `source.scale` is a declared live target (introspect.live_targets);
        # the scene here has a source that declares it, and declares nothing
        # else — so exactly one row comes back, generated rather than listed.
        bridge, pl = _bridge(source=_FakeSource())
        rows = {r["target"]: r for r in bridge.state()["systems"][0]["live"]}
        self.assertEqual(list(rows), ["source.scale"])
        row = rows["source.scale"]
        self.assertEqual(row["group"], "Generator")
        self.assertEqual(row["kind"], "scalar")
        self.assertAlmostEqual(row["value"], 2.05, places=3)
        # norm = (2.05 - 0.1) / (4.0 - 0.1) = 0.5
        self.assertAlmostEqual(row["norm"], 0.5, places=3)

    def test_live_panel_is_empty_for_a_scene_with_no_knobs(self):
        bridge, _pl = _bridge()
        self.assertEqual(bridge.state()["systems"][0]["live"], [])

    def test_live_sets_a_scalar_from_a_slider_position(self):
        src = _FakeSource()
        bridge, pl = _bridge(source=src)
        self.assertTrue(bridge.live(None, "source.scale", norm=0.0))
        self.assertAlmostEqual(src.scale, 0.1, places=4)
        self.assertTrue(bridge.live(None, "source.scale", norm=1.0))
        self.assertAlmostEqual(src.scale, 4.0, places=4)

    def test_live_does_not_reach_the_audience_screen(self):
        # The console exists so a performer has a readout the audience doesn't,
        # so unlike the MIDI and WLED surfaces it posts no OSD line.
        src = _FakeSource()
        bridge, pl = _bridge(source=src)
        bridge.live(None, "source.scale", norm=0.75)
        self.assertEqual(pl.osd, [])

    def test_live_on_a_target_the_scene_lacks_is_a_noop_not_a_refusal(self):
        bridge, pl = _bridge()
        self.assertTrue(bridge.live(None, "mode.dither_strength", norm=0.5))

    def test_live_with_no_session_is_refused(self):
        self.assertFalse(PerfBridge(lambda: []).live(None, "source.scale", norm=0.5))

    def test_transport_sets_the_playlists_own_events(self):
        bridge, pl = _bridge()
        self.assertTrue(bridge.transport(None, "pause"))
        self.assertTrue(pl.pause_event.is_set())
        self.assertTrue(bridge.transport(None, "resume"))
        self.assertTrue(pl.resume_event.is_set())
        self.assertTrue(bridge.transport(None, "skip"))
        self.assertTrue(pl.skip_event.is_set())

    def test_transport_rejects_a_verb_it_does_not_have(self):
        bridge, pl = _bridge()
        self.assertFalse(bridge.transport(None, "rewind"))
        self.assertFalse(pl.pause_event.is_set())

    def test_freeze_and_unfreeze_enqueue_their_own_verb(self):
        # The idempotency check against transport_is_paused now happens on
        # the playlist thread, inside TransportSession._dispatch (see
        # tests/test_transport.py) — not here at enqueue time, so that two
        # requests racing ahead of a single drain can't both read the same
        # stale state and cancel each other out.
        bridge, pl = _bridge(scene=_FakeTransportScene())
        self.assertTrue(bridge.transport(None, "freeze"))
        self.assertTrue(bridge.transport(None, "unfreeze"))
        self.assertEqual([e.action for e in pl.transport.events], ["freeze", "unfreeze"])

    def test_freeze_on_a_scene_with_no_transport_surface_still_enqueues(self):
        # _dispatch's own missing-surface check (duck-typed getattr) makes
        # this a no-op once drained — see
        # test_transport.test_unknown_scene_type_missing_surface_is_noop.
        bridge, pl = _bridge()
        self.assertTrue(bridge.transport(None, "freeze"))
        self.assertEqual([e.action for e in pl.transport.events], ["freeze"])

    def test_rw_and_ff_enqueue_holds_with_pressed(self):
        bridge, pl = _bridge(scene=_FakeTransportScene())
        self.assertTrue(bridge.transport(None, "rw", pressed=True))
        self.assertTrue(bridge.transport(None, "ff", pressed=False))
        got = [(e.action, e.pressed) for e in pl.transport.events]
        self.assertEqual(got, [("rw", True), ("ff", False)])

    def test_seek_requires_a_target(self):
        bridge, pl = _bridge(scene=_FakeTransportScene())
        self.assertFalse(bridge.transport(None, "seek"))
        self.assertEqual(pl.transport.events, [])
        self.assertTrue(bridge.transport(None, "seek", target=12.5))
        self.assertEqual(pl.transport.events[0].target, 12.5)

    def test_loop_toggle_enqueues(self):
        bridge, pl = _bridge(scene=_FakeTransportScene())
        self.assertTrue(bridge.transport(None, "loop_toggle"))
        self.assertEqual(pl.transport.events[0].action, "loop_toggle")

    def test_loop_slot_carries_slot_and_explicit_save_clear(self):
        # A console has no "hold Stop" gesture, so it says save/clear outright
        # rather than relying on the MIDI chord-timing fallback.
        bridge, pl = _bridge(scene=_FakeTransportScene())
        self.assertTrue(bridge.transport(None, "loop_slot", slot=3, save=True))
        event = pl.transport.events[0]
        self.assertEqual(
            (event.action, event.slot, event.save, event.clear), ("loop_slot", 3, True, None)
        )

    def test_paused_and_scenes_are_in_the_state(self):
        bridge, pl = _bridge(scene_name="opener")
        state = bridge.state()["systems"][0]
        self.assertFalse(state["paused"])
        self.assertEqual(state["scene_index"], 0)
        self.assertEqual(
            state["scenes"],
            [{"index": 0, "name": "opener", "duration_s": 30.0, "is_current": True}],
        )
        pl.pause_event.set()
        self.assertTrue(bridge.state()["systems"][0]["paused"])

    def test_transport_is_none_for_a_scene_with_no_transport_surface(self):
        bridge, _pl = _bridge()
        self.assertIsNone(bridge.state()["systems"][0]["transport"])

    def test_transport_reflects_the_scenes_own_surface(self):
        scene = _FakeTransportScene(
            paused=True,
            position=12.5,
            duration=60.0,
            loop={"state": "active", "a": 1.0, "b": 5.0},
            loop_slots=[2, 5],
        )
        bridge, _pl = _bridge(scene=scene)
        transport = bridge.state()["systems"][0]["transport"]
        self.assertEqual(
            transport,
            {
                "position": 12.5,
                "duration": 60.0,
                "frozen": True,
                "loop": {"state": "active", "a": 1.0, "b": 5.0},
                "loop_slots": [2, 5],
            },
        )

    def test_jump_is_a_cut(self):
        bridge, pl = _bridge()
        self.assertTrue(bridge.jump(None, 0))
        self.assertEqual(pl.jumps, [(0, True)])

    def test_jump_off_the_end_is_a_noop_but_addressed(self):
        bridge, pl = _bridge()
        self.assertTrue(bridge.jump(None, 9))
        self.assertEqual(pl.jumps, [])

    def test_apply_dispatches_the_new_actions(self):
        src = _FakeSource()
        bridge, pl = _bridge(source=src)
        bridge.apply({"action": "live", "target": "source.scale", "norm": 1.0})
        self.assertAlmostEqual(src.scale, 4.0, places=4)
        bridge.apply({"action": "transport", "verb": "skip"})
        self.assertTrue(pl.skip_event.is_set())
        bridge.apply({"action": "jump", "index": 0})
        self.assertEqual(pl.jumps, [(0, True)])

    def test_apply_carries_the_transport_verbs_extra_fields(self):
        bridge, pl = _bridge(scene=_FakeTransportScene())
        bridge.apply({"action": "transport", "verb": "seek", "target": 30.0})
        bridge.apply({"action": "transport", "verb": "rw", "pressed": False})
        bridge.apply({"action": "transport", "verb": "loop_slot", "slot": 4, "clear": True})
        got = [
            (e.action, e.target, e.pressed, e.slot, e.save, e.clear) for e in pl.transport.events
        ]
        self.assertEqual(
            got,
            [
                ("seek", 30.0, True, 0, None, None),
                ("rw", None, False, 0, None, None),
                ("loop_slot", None, True, 4, None, True),
            ],
        )

    def test_beats_remaining(self):
        pl = _FakePlaylist()
        pl.tempo._bar = 1.375  # 1.375 bars -> next bar boundary at 2.0
        # bar quantize: (2 - 1.375) bars * 4 beats = 2.5 beats
        bar_rem = _beats_remaining(pl, (1, "bar", 5.5, 1.375))
        assert bar_rem is not None
        self.assertAlmostEqual(bar_rem, 2.5, places=3)
        pl.tempo._beat = 5.5  # next beat boundary at 6.0
        beat_rem = _beats_remaining(pl, (1, "beat", 5.5, 1.375))
        assert beat_rem is not None
        self.assertAlmostEqual(beat_rem, 0.5, places=3)
        self.assertEqual(_beats_remaining(pl, (1, "off", 5.5, 1.375)), 0.0)
        pl.tempo.running = False
        self.assertIsNone(_beats_remaining(pl, (1, "bar", 5.5, 1.375)))

    def test_armed_block_in_state(self):
        bridge, pl = _bridge(clips=[{"slot": 1, "name": "A"}])
        pl.performance.armed_slot = 1
        pl.performance.armed_detail = (1, "off", 5.5, 1.375)
        armed = bridge.state()["systems"][0]["armed"]
        self.assertEqual(armed["slot"], 1)
        self.assertEqual(armed["beats_remaining"], 0.0)

    def test_multi_system_flag(self):
        bridge = PerfBridge(lambda: [("a", _FakePlaylist()), ("b", _FakePlaylist())])
        st = bridge.state()
        self.assertTrue(st["multi"])
        self.assertEqual([s["name"] for s in st["systems"]], ["a", "b"])


class MalformedCommandTest(unittest.TestCase):
    """A decodable frame never raises out of `PerfBridge.apply`.

    `SocketReader` closed the *undecodable* half of this: a text frame that
    isn't JSON, or a binary one, is ignored rather than allowed to tear the
    console's only feed down. The dispatch one layer below it defeated that for
    a frame that decodes fine and then names an action without its fields —
    `{"action": "launch"}` was a `KeyError` from inside the push loop, a
    traceback at default verbosity, and a closed socket. The page that builds
    these frames is hand-written JS that nothing type-checks, so a cached phone
    page from an older build is the expected caller."""

    def test_an_action_missing_its_field_is_refused_rather_than_raised(self):
        bridge, pl = _bridge(clips=[{"slot": 1, "name": "A"}], effects=[TrailsEffect()])
        for cmd in (
            {"action": "launch"},
            {"action": "fx"},
            {"action": "live"},
            {"action": "jump"},
            {"action": "look"},
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(bridge.apply(cmd))
        self.assertEqual(pl.performance.events, [])
        self.assertEqual(pl.jumps, [])

    def test_a_non_numeric_field_is_refused_rather_than_raised(self):
        bridge, pl = _bridge(clips=[{"slot": 1, "name": "A"}], effects=[TrailsEffect()])
        for cmd in (
            {"action": "launch", "slot": "x"},
            {"action": "jump", "index": "x"},
            {"action": "fx", "layer": None},
            {"action": "fx", "layer": 0, "param": "decay", "value": "loud"},
            {"action": "look", "slot": [1]},
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(bridge.apply(cmd))

    def test_a_non_finite_number_is_refused(self):
        # `json.loads` accepts the bare literals `1e400`, `Infinity` and `NaN`,
        # and `int(float("inf"))` raises OverflowError — an ArithmeticError, so
        # not in the (KeyError, TypeError, ValueError) tuple a fix would reach
        # for first.
        bridge, _pl = _bridge(clips=[{"slot": 1, "name": "A"}])
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                self.assertFalse(bridge.apply({"action": "launch", "slot": value}))
                self.assertFalse(
                    bridge.apply({"action": "transport", "verb": "seek", "target": value})
                )

    def test_a_boolean_is_not_a_slot(self):
        # `bool` is an `int` in Python, so `{"slot": true}` would read as slot 1.
        bridge, pl = _bridge(clips=[{"slot": 1, "name": "A"}])
        self.assertFalse(bridge.apply({"action": "launch", "slot": True}))
        self.assertEqual(pl.performance.events, [])

    def test_an_absurdly_long_target_is_refused(self):
        # `live_tune.resolve_holder` parses the `fx<n>` prefix with `int()`, and
        # CPython refuses an integer literal past 4300 digits — so a crafted
        # target raised ValueError from a place no reader would guard.
        bridge, _pl = _bridge(effects=[TrailsEffect()])
        target = "fx" + "9" * (MAX_TARGET_CHARS * 2) + ".amount"
        self.assertFalse(bridge.apply({"action": "live", "target": target, "norm": 0.5}))
        self.assertFalse(bridge.live(None, ""))

    def test_a_well_formed_frame_still_works(self):
        # The guard rails must not have narrowed what a real console sends.
        bridge, pl = _bridge(clips=[{"slot": 1, "name": "A"}], effects=[TrailsEffect(decay=0.0)])
        self.assertTrue(bridge.apply({"action": "launch", "slot": "2"}))
        self.assertTrue(bridge.apply({"action": "fx", "layer": 0, "enabled": False}))
        self.assertEqual(pl.performance.events, [(2, True)])


class TransportDispatchTest(unittest.TestCase):
    """`TRANSPORT_VERBS` is the gate and the branch chain below it is the
    dispatch, and nothing held the two lists to each other: the method's last
    statement used to be an unconditional `loop_slot` enqueue, so a verb added
    to the tuple without a branch silently saved or cleared one of the
    performer's persisted loop presets."""

    def test_every_verb_in_the_tuple_has_its_own_effect(self):
        for verb in TRANSPORT_VERBS:
            with self.subTest(verb=verb):
                bridge, pl = _bridge(scene=_FakeTransportScene())
                extra: dict[str, Any] = {}
                if verb == "seek":
                    extra["target"] = 3.0
                if verb == "loop_slot":
                    extra["slot"] = 2
                self.assertTrue(bridge.transport(None, verb, **extra))
                if verb in ("pause", "resume", "skip"):
                    event = {
                        "pause": pl.pause_event,
                        "resume": pl.resume_event,
                        "skip": pl.skip_event,
                    }[verb]
                    self.assertTrue(event.is_set())
                    self.assertEqual(pl.transport.events, [])
                else:
                    self.assertEqual([e.action for e in pl.transport.events], [verb])

    def test_a_verb_with_no_branch_is_refused_rather_than_writing_a_loop_slot(self):
        bridge, pl = _bridge(scene=_FakeTransportScene())
        with patch.object(perf_console, "TRANSPORT_VERBS", (*TRANSPORT_VERBS, "wobble")):
            self.assertFalse(bridge.transport(None, "wobble"))
        self.assertEqual(pl.transport.events, [])

    def test_a_loop_slot_outside_the_pad_range_is_refused(self):
        # The one verb here that writes and deletes persisted state on disk.
        # Unbounded, a caller could loop an incrementing slot and grow
        # `loop-*.json` without limit, each save rewriting the whole file on
        # the playlist thread that drives the hardware.
        bridge, pl = _bridge(scene=_FakeTransportScene())
        for slot in (0, -1, JsonSlotStore.SLOT_MAX + 1, 10**6):
            with self.subTest(slot=slot):
                self.assertFalse(bridge.transport(None, "loop_slot", slot=slot, save=True))
        self.assertEqual(pl.transport.events, [])
        self.assertTrue(bridge.transport(None, "loop_slot", slot=JsonSlotStore.SLOT_MAX, save=True))


class StateFrameCoherenceTest(unittest.TestCase):
    def test_one_state_frame_describes_one_scene(self):
        pl = _AdvancingPlaylist()
        state = _system_state("c64cast", pl)
        # Scene A has an effect chain and no source; scene B has a source and
        # no chain. A frame built from more than one read of `pl.current`
        # renders A's name over B's rack.
        self.assertEqual(state["current_scene"], "A")
        self.assertEqual(len(state["effects"]), 1)
        self.assertEqual(state["live"], [])
        self.assertEqual(pl.reads, 1)

    def test_the_catalog_cache_has_a_reset_hook(self):
        # A process-global with no reset let one test inherit whatever the
        # previous test in the same worker had cached.
        before = [doc.target for doc in perf_console._live_target_docs()]
        self.assertTrue(before)
        perf_console.reset_live_target_docs()
        self.assertIsNone(perf_console._LIVE_TARGETS)
        self.assertEqual([doc.target for doc in perf_console._live_target_docs()], before)


class ViewerFrameTest(unittest.TestCase):
    """What a read-only credential — the link this system is designed to hand
    to a guest — is told about the host."""

    def _frame(self, role: str | None) -> dict[str, Any]:
        state = {
            "systems": [
                {"tuned": {"config_path": "/Users/someone/shows/gig.toml", "config_name": "gig"}}
            ]
        }
        scope = {} if role is None else {SCOPE_ROLE_KEY: role}
        return with_role(state, scope)

    def test_a_viewer_is_not_told_the_running_config_path(self):
        tuned = self._frame(ROLE_VIEWER)["systems"][0]["tuned"]
        self.assertEqual(tuned["config_path"], "")
        # The name is what the page actually renders, and it stays.
        self.assertEqual(tuned["config_name"], "gig")

    def test_the_full_token_still_gets_the_path_it_saves_to(self):
        frame = self._frame(ROLE_FULL)
        self.assertEqual(frame["role"], ROLE_FULL)
        self.assertTrue(frame["systems"][0]["tuned"]["config_path"])

    def test_an_ungated_run_reports_no_role(self):
        self.assertIsNone(self._frame(None)["role"])


class TunedBlockTest(unittest.TestCase):
    """`_tuned_dict` — what the console is shown about knobs already turned.

    A daemon has no exit prompt, so this block is the whole offer: it has to
    say what changed, which of it a config can hold, and where that config is."""

    def test_nothing_tuned_is_an_empty_offer(self):
        bridge, _pl = _bridge()
        tuned = bridge.state()["systems"][0]["tuned"]
        self.assertEqual(tuned["changes"], [])
        self.assertEqual(tuned["savable"], 0)
        self.assertNotIn("snippet", tuned)

    def test_a_color_knob_is_savable_and_names_its_field(self):
        bridge, pl = _bridge(config_path="/shows/demo.toml")
        pl.live_tracker.record("mode.dither_strength", 0.5, 0.8)
        tuned = bridge.state()["systems"][0]["tuned"]
        self.assertEqual(tuned["savable"], 1)
        self.assertEqual(tuned["config_path"], "/shows/demo.toml")
        # The bare name is what both consoles show — no directory, no `.toml`.
        self.assertEqual(tuned["config_name"], "demo")
        self.assertEqual(tuned["changes"][0]["field"], "dither_strength")
        self.assertEqual(tuned["changes"][0]["old"], 0.5)
        self.assertEqual(tuned["changes"][0]["new"], 0.8)
        # A file to write to means no pasteable block — the offer is a button.
        self.assertNotIn("snippet", tuned)

    def test_a_renamed_field_reports_the_config_name(self):
        # The mode calls it dither_method; [color] calls it dither, and what the
        # console shows has to be the name that ends up in the file.
        bridge, pl = _bridge(config_path="/shows/demo.toml")
        pl.live_tracker.record("mode.dither_method", "bayer4", "atkinson")
        self.assertEqual(bridge.state()["systems"][0]["tuned"]["changes"][0]["field"], "dither")

    def test_a_runtime_only_knob_is_listed_but_not_counted(self):
        # A generator knob has no config home, and a palette mode turned on a
        # scene the config never named has no block to go in. Both end with the
        # show; listing them is the point, since silence would read as "saved".
        bridge, pl = _bridge(config_path="/shows/demo.toml")
        pl.live_tracker.record("source.scale", 1.0, 2.0)
        pl.live_tracker.record("mode.palette_mode", "auto", "vivid", scene=None)
        tuned = bridge.state()["systems"][0]["tuned"]
        self.assertEqual(len(tuned["changes"]), 2)
        self.assertEqual(tuned["savable"], 0)
        self.assertEqual([c["field"] for c in tuned["changes"]], [None, None])

    def test_a_per_scene_knob_is_savable_and_says_which_scene(self):
        # The console renders the scene number, and the save route addresses the
        # block by it — so it has to be on the feed, not inferred from the name.
        bridge, pl = _bridge(config_path="/shows/demo.toml")
        pl.live_tracker.record("mode.palette_mode", "percell", "vivid", scene=2)
        tuned = bridge.state()["systems"][0]["tuned"]
        self.assertEqual(tuned["savable"], 1)
        (change,) = tuned["changes"]
        self.assertEqual(change["field"], "palette_mode")
        self.assertEqual(change["scene"], 2)
        # `key` is what a discard sends back, and it is not the target: the same
        # knob on two scenes is two rows.
        self.assertEqual(change["key"], "mode.palette_mode@2")

    def test_a_run_with_no_config_gets_the_pasteable_block(self):
        # Quick playback has no file to write back to; the CLI prints a [color]
        # snippet in that case and the browser gets the same one.
        bridge, pl = _bridge()
        pl.live_tracker.record("mode.dither_strength", 0.5, 0.8)
        tuned = bridge.state()["systems"][0]["tuned"]
        self.assertEqual(tuned["config_path"], "")
        self.assertEqual(tuned["config_name"], "")
        self.assertIn("[color]", tuned["snippet"])
        self.assertIn("dither_strength", tuned["snippet"])


class PerfBridgeRegistryTest(unittest.TestCase):
    """The bridge reads its systems through a provider, so a host can start and
    stop sessions under a console that stays connected."""

    def test_systems_are_re_read_per_call(self):
        systems: list[tuple[str, Any]] = []
        bridge = PerfBridge(lambda: systems)
        self.assertEqual(bridge.state()["systems"], [])
        systems.append(("late", _FakePlaylist(scene_name="after")))
        st = bridge.state()
        self.assertEqual(st["systems"][0]["current_scene"], "after")

    def test_commands_with_no_session_are_refused_not_raised(self):
        bridge = PerfBridge(lambda: [])
        # Every write reports "not addressed" rather than IndexErroring on an
        # empty ensemble — an idle console still has live buttons.
        self.assertFalse(bridge.launch(None, 1))
        self.assertFalse(bridge.tap(None))
        self.assertFalse(bridge.fx_bypass(None, 0, False))
        self.assertFalse(bridge.fx_param(None, 0, "decay", 0.5))
        self.assertFalse(bridge.look(None, 1, save=True))
        self.assertFalse(bridge.apply({"action": "tap"}))

    def test_state_with_no_session_is_empty_not_an_error(self):
        st = PerfBridge(lambda: []).state()
        self.assertFalse(st["multi"])
        self.assertEqual(st["systems"], [])


class PerfPageControlsTest(unittest.TestCase):
    """The zero-dependency page and the bridge under it move together.

    The page is hand-written DOM in a Python string, so nothing type-checks the
    commands it builds. These read the commands back out of it."""

    def _page_actions(self) -> set[str]:
        return set(re.findall(r"action: '(\w+)'", _PERF_HTML))

    def test_the_page_reaches_every_action_the_bridge_dispatches(self):
        # Read off `PerfBridge.apply`'s own dispatch rather than a list here: a
        # bridge action with no control on the page is exactly the gap this
        # closes, and a second copy of the list would hide the next one.
        dispatched = set(re.findall(r'action == "(\w+)"', inspect.getsource(PerfBridge.apply)))
        self.assertEqual(self._page_actions(), dispatched)

    def test_the_page_only_sends_transport_verbs_the_bridge_takes(self):
        verbs = set(re.findall(r"verb: '(\w+)'", _PERF_HTML))
        self.assertTrue(verbs)
        self.assertLessEqual(verbs, set(TRANSPORT_VERBS))

    def test_the_gesture_controls_blur_so_the_panels_keep_re_rendering(self):
        # renderFx and renderTune skip a rebuild while something inside them
        # has focus, and a range keeps focus after a drag and a <select> after
        # a change (per the browser) — so without a blur the first drag froze
        # that panel for the rest of the session: a bypass flipped from a MIDI
        # pad stopped showing, and after a scene advance the tune panel kept
        # offering the previous scene's knobs. wled_device.py's page carries
        # the same fix, and its comment is the record of the failure mode.
        self.assertGreaterEqual(_PERF_HTML.count("blur()"), 3)

    def test_the_reconnect_backs_off_rather_than_retrying_forever(self):
        # Every open phone retrying a downed host at a fixed interval is the
        # load `MAX_CONSOLE_SOCKETS` exists to bound.
        self.assertIn("function retryWS()", _PERF_HTML)
        self.assertIn("WS_RETRY_MAX_MS", _PERF_HTML)
        self.assertNotIn("setTimeout(startWS, 2500)", _PERF_HTML)

    def test_the_idle_branch_clears_the_tempo_readout(self):
        # `animate()` renders clock.bpm unconditionally, so leaving the anchor
        # alone showed the last show's BPM — or a confident 120 from the
        # initializer — above "No session running."
        self.assertIn("bpm: 0", _PERF_HTML)

    def test_the_screen_is_re_pointed_when_the_system_tab_changes(self):
        # The src bakes `?system=` in and was only ever rebuilt by the WATCH
        # tap, so on an ensemble run a tab tap moved every control to the new
        # machine and left the old machine's picture streaming underneath.
        self.assertIn("screenSys", _PERF_HTML)
        self.assertIn("!== screenSys", _PERF_HTML)

    def test_the_save_back_posts_where_the_write_route_lives(self):
        # Not on /perf/command: a config write needs a status code, which a
        # performance command has nowhere to put. See web_api.api_live_tune.
        self.assertIn("'/api/session/live-tune'", _PERF_HTML)


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi + httpx required")
class PerfEndpointsTest(unittest.TestCase):
    """Drive the perf routes through the real control-plane app."""

    def _client(self) -> tuple[Any, _FakePlaylist]:
        from c64cast.control.control_plane import build_app

        pl = _FakePlaylist(
            clips=[{"slot": 1, "name": "A", "launch": "trigger", "quantize": "bar", "loop": True}],
            effects=[TrailsEffect(decay=0.0)],
        )
        app = build_app(playlists={"c64cast": pl}, config_loaders={}, interstitial_factories={})
        return TestClient(app), pl

    def test_page_served(self):
        client, _pl = self._client()
        r = client.get("/perf")
        self.assertEqual(r.status_code, 200)
        self.assertIn("performance", r.text.lower())
        self.assertIn("/perf/ws", r.text)

    def test_the_page_has_a_panel_for_every_part_of_the_payload(self):
        client, _pl = self._client()
        text = client.get("/perf").text
        for panel in ("clips", "fx", "tune", "tuned", "looks", "scenes", "pause", "skip"):
            self.assertIn(f'id="{panel}"', text)

    def test_the_screen_is_an_img_against_the_stream_route(self):
        # Not a bridge action — an /api route this page reaches without a
        # decoder or a second socket, which is what makes it sayable on a page
        # with no build step. The route only exists on a --serve host, so the
        # page has to handle its absence (test below).
        client, _pl = self._client()
        text = client.get("/perf").text
        self.assertIn('id="screen"', text)
        self.assertIn("/api/screen/stream", text)

    def test_the_screen_starts_off_rather_than_on_page_load(self):
        # Opening it is what starts a couple of megabytes a second moving, so
        # it is a tap and not something every idle console does.
        client, _pl = self._client()
        text = client.get("/perf").text
        self.assertRegex(text, r'<img id="screen"[^>]*\bhidden\b')
        self.assertIn("screenOn = false", text)

    def test_the_page_names_both_reasons_there_might_be_no_picture(self):
        # An <img> error cannot tell "this run has no /api" from "this machine
        # has no VIC", and the page is served by the control plane, which a
        # plain CLI run has without any of /api.
        client, _pl = self._client()
        text = client.get("/perf").text
        self.assertIn("serves no screen", text)
        self.assertIn("no video", text)

    def test_state_endpoint(self):
        client, _pl = self._client()
        r = client.get("/perf/state")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("systems", body)
        self.assertEqual(body["systems"][0]["name"], "c64cast")

    def test_command_launch(self):
        client, pl = self._client()
        r = client.post("/perf/command", json={"action": "launch", "slot": 1})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(pl.performance.events, [(1, True)])

    def test_command_fx_param(self):
        client, pl = self._client()
        r = client.post(
            "/perf/command", json={"action": "fx", "layer": 0, "param": "decay", "value": 0.5}
        )
        self.assertTrue(r.json()["ok"])
        self.assertAlmostEqual(pl.current.effects[0].decay, 0.48, places=4)

    def test_command_bogus_returns_not_ok(self):
        client, _pl = self._client()
        r = client.post("/perf/command", json={"action": "nope"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])

    def test_ws_pushes_state(self):
        client, _pl = self._client()
        with client.websocket_connect("/perf/ws") as ws:
            msg = ws.receive_json()
            self.assertIn("systems", msg)
            self.assertEqual(msg["systems"][0]["name"], "c64cast")

    def test_a_malformed_command_frame_leaves_the_feed_alive(self):
        # The whole point of the dispatch guard: this used to raise KeyError
        # inside the push loop, land on `except Exception`, and close the
        # console's only channel — with a full traceback per frame.
        client, pl = self._client()
        with client.websocket_connect("/perf/ws") as ws:
            ws.receive_json()
            ws.send_json({"action": "launch"})
            self.assertIn("systems", ws.receive_json())
            ws.send_json({"action": "jump", "index": "nope"})
            self.assertIn("systems", ws.receive_json())
            # Still driving commands afterward, so the loop is intact.
            ws.send_json({"action": "launch", "slot": 1})
            self.assertIn("systems", ws.receive_json())
        self.assertEqual(pl.performance.events, [(1, True)])

    def test_a_malformed_command_post_is_not_ok_rather_than_a_500(self):
        client, _pl = self._client()
        r = client.post("/perf/command", json={"action": "launch"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])

    def test_the_page_refuses_to_be_framed(self):
        client, _pl = self._client()
        headers = client.get("/perf").headers
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    def test_a_command_body_bigger_than_the_cap_is_refused(self):
        # `await request.json()` buffered every chunk with nothing bounding it
        # — the hazard `auth.read_body` was written for, on the one POST in the
        # package that skipped it.
        client, _pl = self._client()
        r = client.post(
            "/perf/command",
            content=b"x" * (MAX_COMMAND_BYTES + 1),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(r.status_code, 413)

    def test_a_body_that_is_not_json_is_a_400(self):
        client, _pl = self._client()
        r = client.post(
            "/perf/command", content=b"not json", headers={"content-type": "application/json"}
        )
        self.assertEqual(r.status_code, 400)
        r = client.post(
            "/perf/command", content=b"[1, 2]", headers={"content-type": "application/json"}
        )
        self.assertEqual(r.status_code, 400)

    def test_a_text_plain_post_cannot_reach_the_dispatcher(self):
        # `Request.json()` never looks at Content-Type, so a cross-site
        # `<form enctype="text/plain">` whose field name and value sandwich the
        # JSON is a CORS-simple POST with no preflight to refuse.
        client, pl = self._client()
        r = client.post(
            "/perf/command",
            content=b'{"action": "launch", "slot": 1}',
            headers={"content-type": "text/plain"},
        )
        self.assertEqual(r.status_code, 415)
        self.assertEqual(pl.performance.events, [])


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi + httpx required")
class PerfOriginTest(unittest.TestCase):
    """A WebSocket handshake is exempt from CORS entirely, so any page the
    performer visits could open `/perf/ws`, read every state frame and send
    command frames that drive the running show — the open, no-token mode being
    the default once `[control] enabled = true`."""

    def _client(self) -> tuple[Any, _FakePlaylist]:
        from c64cast.control.control_plane import build_app

        pl = _FakePlaylist(clips=[{"slot": 1, "name": "A"}])
        app = build_app(playlists={"c64cast": pl}, config_loaders={}, interstitial_factories={})
        return TestClient(app), pl

    def test_a_cross_origin_handshake_is_closed_before_accept(self):
        from starlette.websockets import WebSocketDisconnect

        client, _pl = self._client()
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/perf/ws", headers={"origin": "http://evil.example"}):
                pass

    def test_a_same_origin_handshake_is_served(self):
        client, _pl = self._client()
        with client.websocket_connect("/perf/ws", headers={"origin": "http://testserver"}) as ws:
            self.assertIn("systems", ws.receive_json())

    def test_a_handshake_with_no_origin_is_served(self):
        # No Origin is a non-browser caller (`curl`, `wscat`, a script), which
        # is exactly the "whoever already has a shell here" the open mode is
        # justified by — so it stays served.
        client, _pl = self._client()
        with client.websocket_connect("/perf/ws") as ws:
            self.assertIn("systems", ws.receive_json())

    def test_a_cross_origin_command_is_refused(self):
        client, pl = self._client()
        r = client.post(
            "/perf/command",
            json={"action": "launch", "slot": 1},
            headers={"origin": "http://evil.example"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(pl.performance.events, [])

    def test_a_same_origin_command_is_dispatched(self):
        client, pl = self._client()
        r = client.post(
            "/perf/command",
            json={"action": "launch", "slot": 1},
            headers={"origin": "http://testserver"},
        )
        self.assertTrue(r.json()["ok"])
        self.assertEqual(pl.performance.events, [(1, True)])


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi + httpx required")
class PerfSocketCapTest(unittest.TestCase):
    """Nothing capped the console sockets, and each one runs its own push loop
    over a frame that reads two stores off disk. The same decision
    `MAX_SCREEN_WATCHERS`/`StreamSlots` already made for the screen stream:
    refuse past the cap rather than queue, because a queued console connects
    and then shows nothing."""

    def test_a_socket_past_the_cap_is_refused_before_accept(self):
        from starlette.websockets import WebSocketDisconnect

        from c64cast.control.control_plane import build_app

        # Patched before the app is built: the feed reads the cap once, at
        # construction, so a test does not have to open the real number.
        with patch.object(perf_console, "MAX_CONSOLE_SOCKETS", 1):
            app = build_app(
                playlists={"c64cast": _FakePlaylist()},
                config_loaders={},
                interstitial_factories={},
            )
        client = TestClient(app)
        with client.websocket_connect("/perf/ws") as ws:
            ws.receive_json()
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/perf/ws"):
                    pass
        # The slot comes back when the first socket goes away.
        with client.websocket_connect("/perf/ws") as ws:
            self.assertIn("systems", ws.receive_json())


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi + httpx required")
class PerfIdleTest(unittest.TestCase):
    """The console outlives the session: with nothing running it stays
    loadable and reports an empty ensemble, where the control-plane data
    routes answer 503."""

    def _client(self) -> Any:
        from c64cast.control.control_plane import build_app_for_registry

        app = build_app_for_registry(dict, dict, dict)
        return TestClient(app)

    def test_page_still_served(self):
        r = self._client().get("/perf")
        self.assertEqual(r.status_code, 200)

    def test_state_is_empty_not_503(self):
        r = self._client().get("/perf/state")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["systems"], [])

    def test_command_reports_not_ok(self):
        r = self._client().post("/perf/command", json={"action": "tap"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])

    def test_ws_pushes_the_empty_state(self):
        with self._client().websocket_connect("/perf/ws") as ws:
            self.assertEqual(ws.receive_json()["systems"], [])


class SocketReaderTest(unittest.TestCase):
    """The inbound half of both console sockets, driven with a fake WebSocket.

    Shared by `/perf/ws` and `web_api`'s `/api/ws`, which is why it is a class
    rather than the `asyncio.wait_for` one-liner both used to spell
    separately."""

    class _Socket:
        """Hands out queued frames, then blocks forever — the shape a real
        socket has between a client's messages, and the shape the old
        `wait_for` cancelled into."""

        def __init__(self, frames: list[Any]) -> None:
            self.frames = list(frames)
            self.receives = 0

        async def receive_json(self) -> Any:
            import asyncio

            self.receives += 1
            if not self.frames:
                await asyncio.Event().wait()
            frame = self.frames.pop(0)
            if isinstance(frame, BaseException):
                raise frame
            return frame

    def _drive(self, socket: Any, polls: int, timeout: float = 0.05) -> list[Any]:
        import asyncio

        async def run() -> list[Any]:
            reader = SocketReader(socket, label="test console")
            try:
                return [await reader.poll(timeout) for _ in range(polls)]
            finally:
                await reader.close()

        return asyncio.run(run())

    def test_a_frame_arrives_and_the_next_poll_waits_for_the_next_one(self):
        socket = self._Socket([{"action": "tap"}])
        self.assertEqual(self._drive(socket, 2), [(True, {"action": "tap"}), (False, None)])

    def test_a_timeout_leaves_the_receive_pending_rather_than_cancelling_it(self):
        # The whole point: cancelling a receive that has already popped a
        # message off uvicorn's queue consumes the frame and never returns it.
        # One `receive_json` call across many polls is what proves the task
        # survives a timeout.
        socket = self._Socket([])
        self.assertEqual(self._drive(socket, 4), [(False, None)] * 4)
        self.assertEqual(socket.receives, 1)

    def test_an_undecodable_frame_is_reported_as_none_rather_than_raised(self):
        # A text frame that isn't JSON raises JSONDecodeError; a binary one
        # raises KeyError("text"). Both used to close the console's only feed.
        for boom in (ValueError("not json"), KeyError("text")):
            with self.subTest(boom=type(boom).__name__):
                socket = self._Socket([boom, {"action": "tap"}])
                self.assertEqual(self._drive(socket, 2), [(True, None), (True, {"action": "tap"})])

    def test_a_disconnect_still_propagates(self):
        class _Gone(Exception):
            pass

        socket = self._Socket([_Gone()])
        with self.assertRaises(_Gone):
            self._drive(socket, 1)


if __name__ == "__main__":
    unittest.main()
