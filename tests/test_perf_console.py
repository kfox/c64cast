"""Tests for the phone/web performance console (Live DJ/VJ Phase 5).

The bridge tests (`PerfBridgeTest`) drive `PerfBridge` directly against a fake
playlist exposing exactly the surface it reads/writes — no FastAPI needed. The
end-to-end HTTP tests (`PerfEndpointsTest`) drive the real control-plane app via
TestClient and skip when fastapi/httpx isn't installed, mirroring
tests/test_control_plane.py."""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportOptionalCall=false
from __future__ import annotations

import unittest
from typing import Any

from c64cast.effects import TrailsEffect
from c64cast.perf_console import PerfBridge, _beats_remaining, _system_state

try:
    import fastapi  # noqa: F401
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

    def clips_info(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._clips]

    def enqueue(self, event: Any) -> None:
        self.events.append((event.slot, event.pressed))


class _FakeScene:
    def __init__(self, name: str, effects: list[Any] | None = None) -> None:
        self.name = name
        self.effects = effects or []


class _FakePlaylist:
    def __init__(
        self,
        *,
        clips: list[dict[str, Any]] | None = None,
        effects: list[Any] | None = None,
        scene_name: str = "demo",
    ) -> None:
        self.tempo = _FakeTempo()
        self.performance = _FakePerf(clips)
        self.current = _FakeScene(scene_name, effects)


def _bridge(**kw: Any) -> tuple[PerfBridge, _FakePlaylist]:
    pl = _FakePlaylist(**kw)
    return PerfBridge([("c64cast", pl)]), pl


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
        self.assertFalse(bridge.apply({"action": "bogus"}))

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
        bridge = PerfBridge([("a", _FakePlaylist()), ("b", _FakePlaylist())])
        st = bridge.state()
        self.assertTrue(st["multi"])
        self.assertEqual([s["name"] for s in st["systems"]], ["a", "b"])


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi + httpx required")
class PerfEndpointsTest(unittest.TestCase):
    """Drive the perf routes through the real control-plane app."""

    def _client(self) -> tuple[Any, _FakePlaylist]:
        from c64cast.control_plane import build_app

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


if __name__ == "__main__":
    unittest.main()
