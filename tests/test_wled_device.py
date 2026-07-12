"""Tests for the virtual WLED device / control surface (bridge Mode 1).

Covers the combined `[wled]` endpoint parser, the WledBridge state/info/effects
mapping, the apply() → playlist translation (transport, scene jump, live-param
sliders), and the WLED JSON HTTP + WS API via FastAPI's TestClient. No mDNS /
socket binding is exercised — those are the untestable-without-a-LAN parts.
"""

from __future__ import annotations

import threading
import unittest
from typing import cast

from c64cast import config as cfgmod
from c64cast.playlist import Playlist
from c64cast.wled_device import WledBridge, build_wled_app

try:
    # The WLED JSON API tests drive the real FastAPI app via TestClient, which
    # also needs httpx (fastapi declares it optional). CI runs without the
    # `wled`/`control` extra, so guard the API class like test_control_plane
    # does; the bridge + parser tests below need none of this.
    from fastapi.testclient import TestClient  # noqa: F401

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False

# --- fakes ------------------------------------------------------------------


class _FakeSource:
    # Declares a LIVE_PARAM the WLED speed slider (source.speed) should drive.
    LIVE_PARAMS = {"speed": (0.0, 10.0)}

    def __init__(self) -> None:
        self.speed = 1.0


class _FakeScene:
    def __init__(self, name: str, source: _FakeSource | None = None) -> None:
        self.name = name
        self.source = source


class _FakePlaylist:
    """Minimal Playlist surface the bridge touches."""

    def __init__(self, name: str, scene_names: list[str], *, source: _FakeSource | None = None):
        self.name = name
        self.scenes = [_FakeScene(n) for n in scene_names]
        if source is not None and self.scenes:
            self.scenes[0].source = source
        self.index = 0
        self.current = self.scenes[0] if self.scenes else None
        self.single_scene = len(self.scenes) <= 1
        self.pause_event = threading.Event()
        self.resume_event = threading.Event()
        self.skip_event = threading.Event()
        self.jumps: list[int] = []

    def request_jump(self, index: int, *, skip_interstitial: bool = True) -> None:
        if not 0 <= index < len(self.scenes):
            raise ValueError("out of range")
        self.jumps.append(index)


def _bridge(*, source: _FakeSource | None = None) -> tuple[WledBridge, _FakePlaylist]:
    pl = _FakePlaylist("main", ["Waveform", "Plasma", "Tunnel"], source=source)
    systems = cast("list[tuple[str, Playlist]]", [("main", pl)])
    return WledBridge(systems, "c64cast"), pl


# --- endpoint parser --------------------------------------------------------


class EndpointParserTests(unittest.TestCase):
    def _parse(self, value: str | None) -> tuple[bool, str, int]:
        return cfgmod.parse_wled_endpoint(value, "0.0.0.0", 8080, field_name="[wled].listen")

    def test_none_and_disabled_are_off(self):
        self.assertEqual(self._parse(None), (False, "0.0.0.0", 8080))
        self.assertEqual(self._parse("disabled"), (False, "0.0.0.0", 8080))
        self.assertEqual(self._parse(""), (False, "0.0.0.0", 8080))
        self.assertEqual(self._parse("DISABLED"), (False, "0.0.0.0", 8080))

    def test_enabled_uses_defaults(self):
        self.assertEqual(self._parse("enabled"), (True, "0.0.0.0", 8080))
        self.assertEqual(self._parse("  Enabled  "), (True, "0.0.0.0", 8080))

    def test_host_only(self):
        self.assertEqual(self._parse("192.168.1.9"), (True, "192.168.1.9", 8080))

    def test_host_and_port(self):
        self.assertEqual(self._parse("192.168.1.9:9090"), (True, "192.168.1.9", 9090))

    def test_port_only(self):
        self.assertEqual(self._parse(":7000"), (True, "0.0.0.0", 7000))

    def test_host_with_trailing_colon(self):
        self.assertEqual(self._parse("10.0.0.5:"), (True, "10.0.0.5", 8080))

    def test_bad_port_raises(self):
        with self.assertRaises(cfgmod.ConfigError):
            self._parse("host:notaport")

    def test_out_of_range_port_raises(self):
        with self.assertRaises(cfgmod.ConfigError):
            self._parse(":70000")

    def test_resolvers_use_mode_defaults(self):
        cfg = cfgmod.Config()
        self.assertEqual(cfgmod.resolve_wled_broadcast(cfg), (False, "239.0.0.1", 11988))
        self.assertEqual(cfgmod.resolve_wled_listen(cfg), (False, "0.0.0.0", 8080))
        cfg.wled.broadcast = "enabled"
        cfg.wled.listen = "enabled"
        self.assertEqual(cfgmod.resolve_wled_broadcast(cfg), (True, "239.0.0.1", 11988))
        self.assertEqual(cfgmod.resolve_wled_listen(cfg), (True, "0.0.0.0", 8080))


# --- bridge reads -----------------------------------------------------------


class BridgeReadTests(unittest.TestCase):
    def test_effects_are_scene_names(self):
        bridge, _ = _bridge()
        self.assertEqual(bridge.effects(), ["Waveform", "Plasma", "Tunnel"])

    def test_state_shape_one_segment_per_system(self):
        bridge, _ = _bridge()
        state = bridge.state_dict()
        self.assertTrue(state["on"])  # not paused
        self.assertEqual(len(state["seg"]), 1)
        seg = state["seg"][0]
        self.assertEqual(seg["id"], 0)
        self.assertEqual(seg["fx"], 0)
        self.assertTrue(seg["on"])

    def test_state_reports_paused_as_off(self):
        bridge, pl = _bridge()
        pl.pause_event.set()
        state = bridge.state_dict()
        self.assertFalse(state["on"])
        self.assertFalse(state["seg"][0]["on"])

    def test_fx_tracks_current_index(self):
        bridge, pl = _bridge()
        pl.index = 2
        self.assertEqual(bridge.state_dict()["seg"][0]["fx"], 2)

    def test_info_identifies_as_wled(self):
        bridge, _ = _bridge()
        info = bridge.info_dict()
        self.assertEqual(info["product"], "c64cast")
        self.assertEqual(info["name"], "c64cast")
        self.assertEqual(info["fxcount"], 3)
        self.assertIn("ver", info)

    def test_full_has_all_sections(self):
        bridge, _ = _bridge()
        full = bridge.full()
        self.assertEqual(set(full), {"state", "info", "effects", "palettes"})


# --- bridge writes (apply) --------------------------------------------------


class BridgeApplyTests(unittest.TestCase):
    def test_master_off_pauses(self):
        bridge, pl = _bridge()
        bridge.apply({"on": False})
        self.assertTrue(pl.pause_event.is_set())

    def test_bri_zero_pauses(self):
        bridge, pl = _bridge()
        bridge.apply({"bri": 0})
        self.assertTrue(pl.pause_event.is_set())

    def test_master_on_resumes_when_paused(self):
        bridge, pl = _bridge()
        pl.pause_event.set()
        bridge.apply({"on": True, "bri": 128})
        self.assertTrue(pl.resume_event.is_set())

    def test_seg_fx_jumps(self):
        bridge, pl = _bridge()
        bridge.apply({"seg": [{"id": 0, "fx": 2}]})
        self.assertEqual(pl.jumps, [2])

    def test_seg_fx_out_of_range_ignored(self):
        bridge, pl = _bridge()
        bridge.apply({"seg": [{"id": 0, "fx": 99}]})
        self.assertEqual(pl.jumps, [])

    def test_seg_off_pauses_that_system(self):
        bridge, pl = _bridge()
        bridge.apply({"seg": [{"id": 0, "on": False}]})
        self.assertTrue(pl.pause_event.is_set())

    def test_sx_slider_drives_live_param(self):
        src = _FakeSource()
        bridge, _pl = _bridge(source=src)
        bridge.apply({"seg": [{"id": 0, "sx": 255}]})  # full slider -> hi (10.0)
        self.assertAlmostEqual(src.speed, 10.0)
        bridge.apply({"seg": [{"id": 0, "sx": 0}]})  # zero -> lo (0.0)
        self.assertAlmostEqual(src.speed, 0.0)

    def test_slider_without_matching_param_is_noop(self):
        bridge, _pl = _bridge()  # no source declaring LIVE_PARAMS
        bridge.apply({"seg": [{"id": 0, "ix": 200}]})  # must not raise
        self.assertEqual(bridge.state_dict()["seg"][0]["ix"], 200)  # still echoed

    def test_echo_roundtrips_bri_and_pal(self):
        bridge, _ = _bridge()
        bridge.apply({"seg": [{"id": 0, "bri": 77, "pal": 3}]})
        seg = bridge.state_dict()["seg"][0]
        self.assertEqual(seg["bri"], 77)
        self.assertEqual(seg["pal"], 3)


# --- HTTP + WS API ----------------------------------------------------------


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class WledApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        self.bridge, self.pl = _bridge()
        self.client = TestClient(build_wled_app(self.bridge))

    def test_get_json(self):
        r = self.client.get("/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"state", "info", "effects", "palettes"})

    def test_get_state_info_eff_pal_si(self):
        self.assertEqual(self.client.get("/json/state").json()["seg"][0]["id"], 0)
        self.assertEqual(self.client.get("/json/info").json()["product"], "c64cast")
        self.assertEqual(self.client.get("/json/eff").json(), ["Waveform", "Plasma", "Tunnel"])
        self.assertEqual(self.client.get("/json/pal").json(), ["Default"])
        self.assertEqual(set(self.client.get("/json/si").json()), {"state", "info"})

    def test_post_state_pauses(self):
        r = self.client.post("/json/state", json={"on": False})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.assertTrue(self.pl.pause_event.is_set())

    def test_post_json_jumps(self):
        self.client.post("/json", json={"seg": [{"id": 0, "fx": 1}]})
        self.assertEqual(self.pl.jumps, [1])

    def test_ws_sends_state_on_connect_and_applies(self):
        with self.client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            self.assertIn("state", hello)
            self.assertIn("info", hello)
            ws.send_json({"seg": [{"id": 0, "fx": 2}]})
            update = ws.receive_json()
            self.assertIn("state", update)
        self.assertEqual(self.pl.jumps, [2])


if __name__ == "__main__":
    unittest.main()
