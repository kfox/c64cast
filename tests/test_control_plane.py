"""Tests for the multi-system control plane.

The shape-and-routing tests (`ResponseShapeTest`) call the helper
functions directly and don't depend on fastapi/httpx. The end-to-end
HTTP tests (`SingleSystemBackCompatTest`, `MultiSystemTest`) drive the
real FastAPI app via TestClient and skip when httpx isn't available
(TestClient requires it)."""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportOptionalCall=false
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

try:
    import fastapi  # noqa: F401

    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

try:
    # TestClient also needs httpx — fastapi declares it as an optional
    # extra, so a bare `pip install fastapi` can still leave the import
    # failing at runtime. Catch any ImportError, not just fastapi's.
    from fastapi.testclient import TestClient

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False
    TestClient = None  # type: ignore[misc,assignment]

from c64cast.playlist import Playlist


def _fake_playlist(name: str, *, scene_count: int = 2) -> Playlist:
    """A MagicMock'd Playlist that satisfies what the control plane reads:
    .current.name, .index, .scenes (list with .name + .duration_s), .pause/
    skip/resume events with .set()/is_set(), .api.stats, .api.format_write_latency(),
    .transitioning, .request_reload()."""
    pl = MagicMock(name=f"playlist-{name}")
    pl.name = name
    pl.current = MagicMock()
    pl.current.name = f"{name}-scene-0"
    pl.index = 0
    pl.scenes = [MagicMock() for _ in range(scene_count)]
    for i, s in enumerate(pl.scenes):
        s.name = f"{name}-scene-{i}"
        s.duration_s = 10.0 + i
    pl.transitioning = False
    pl.api.stats = {"writes": 100}
    pl.api.format_write_latency.return_value = "lat 5ms"
    # Real Events so .set() / .is_set() round-trip cleanly.
    pl.pause_event = threading.Event()
    pl.resume_event = threading.Event()
    pl.skip_event = threading.Event()
    return pl


class ResponseShapeTest(unittest.TestCase):
    """No-FastAPI direct tests of the per-system response-shaping helpers.

    Verifies the JSON dicts the route handlers return are sensible. The
    routing logic (which endpoint handles which path + ?system= dispatch)
    is FastAPI's responsibility and exercised end-to-end below when
    httpx is available."""

    def test_status_dict_shape(self):
        from c64cast.control_plane import _status_for

        pl = _fake_playlist("a")
        body = _status_for(pl)
        self.assertEqual(body["current_scene"], "a-scene-0")
        self.assertEqual(body["n_scenes"], 2)
        self.assertEqual(body["current_index"], 0)
        self.assertFalse(body["paused"])
        self.assertFalse(body["transitioning"])
        self.assertEqual(body["u64_stats"], {"writes": 100})
        self.assertEqual(body["u64_dma_latency"], "lat 5ms")

    def test_status_dict_reflects_pause_state(self):
        from c64cast.control_plane import _status_for

        pl = _fake_playlist("a")
        pl.pause_event.set()
        self.assertTrue(_status_for(pl)["paused"])

    def test_scenes_dict_shape(self):
        from c64cast.control_plane import _scenes_for

        pl = _fake_playlist("a", scene_count=3)
        body = _scenes_for(pl)
        self.assertEqual(len(body["scenes"]), 3)
        self.assertEqual(body["scenes"][0]["name"], "a-scene-0")
        self.assertEqual(body["scenes"][0]["is_current"], True)
        self.assertEqual(body["scenes"][1]["is_current"], False)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed (control extra)")
class BuildAppConstructionTest(unittest.TestCase):
    """build_app should reject obviously broken inputs at construction
    time rather than yielding a half-built FastAPI app."""

    def test_empty_playlists_rejected(self):
        from c64cast.control_plane import build_app

        with self.assertRaises(ValueError) as cm:
            build_app(playlists={}, config_loaders={}, interstitial_factories={})
        self.assertIn("at least one playlist", str(cm.exception))


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class SingleSystemBackCompatTest(unittest.TestCase):
    """When there's exactly one system, omitting ?system= must return the
    same un-wrapped JSON shape today's clients expect."""

    def _client(self):
        from c64cast.control_plane import build_app

        pl = _fake_playlist("system")
        app = build_app(
            playlists={"system": pl},
            config_loaders={"system": lambda: pl.scenes},
            interstitial_factories={"system": lambda: lambda n: None},
        )
        return TestClient(app), pl

    def test_status_unwrapped(self):
        client, pl = self._client()
        r = client.get("/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # No `systems` wrapper — flat shape, today's contract.
        self.assertIn("current_scene", body)
        self.assertNotIn("systems", body)
        self.assertEqual(body["current_scene"], "system-scene-0")
        self.assertEqual(body["n_scenes"], 2)
        self.assertFalse(body["paused"])

    def test_scenes_unwrapped(self):
        client, _ = self._client()
        r = client.get("/scenes")
        body = r.json()
        self.assertIn("scenes", body)
        self.assertNotIn("systems", body)
        self.assertEqual(len(body["scenes"]), 2)

    def test_pause_resume_round_trip(self):
        client, pl = self._client()
        r = client.post("/pause")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(pl.pause_event.is_set())
        r = client.post("/resume")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(pl.resume_event.is_set())

    def test_resume_without_pause_returns_409(self):
        client, _ = self._client()
        r = client.post("/resume")
        self.assertEqual(r.status_code, 409)

    def test_skip_sets_event(self):
        client, pl = self._client()
        r = client.post("/skip")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(pl.skip_event.is_set())


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class MultiSystemTest(unittest.TestCase):
    def _client(self):
        from c64cast.control_plane import build_app

        playlists = {n: _fake_playlist(n) for n in ("left", "right")}
        app = build_app(
            playlists=playlists,
            config_loaders={n: (lambda p=p: p.scenes) for n, p in playlists.items()},
            interstitial_factories={n: (lambda: lambda nm: None) for n in playlists},
        )
        return TestClient(app), playlists

    def test_status_no_system_wraps(self):
        client, _ = self._client()
        r = client.get("/status")
        body = r.json()
        self.assertIn("systems", body)
        self.assertEqual(set(body["systems"].keys()), {"left", "right"})
        self.assertEqual(body["systems"]["left"]["current_scene"], "left-scene-0")

    def test_status_specific_system_unwraps(self):
        client, _ = self._client()
        r = client.get("/status?system=right")
        body = r.json()
        self.assertNotIn("systems", body)
        self.assertEqual(body["current_scene"], "right-scene-0")

    def test_status_unknown_system_404(self):
        client, _ = self._client()
        r = client.get("/status?system=top")
        self.assertEqual(r.status_code, 404)
        self.assertIn("top", r.json()["detail"])
        self.assertIn("left", r.json()["detail"])

    def test_pause_no_system_applies_to_all(self):
        client, playlists = self._client()
        r = client.post("/pause")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(playlists["left"].pause_event.is_set())
        self.assertTrue(playlists["right"].pause_event.is_set())
        self.assertEqual(set(r.json()["paused"]), {"left", "right"})

    def test_pause_specific_system_only(self):
        client, playlists = self._client()
        r = client.post("/pause?system=left")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(playlists["left"].pause_event.is_set())
        self.assertFalse(playlists["right"].pause_event.is_set())

    def test_resume_only_acts_on_paused_systems(self):
        client, playlists = self._client()
        playlists["left"].pause_event.set()
        # right is NOT paused; resume?system=all should resume left only
        # and not 409 since at least one resume happened.
        r = client.post("/resume?system=all")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["resumed"], ["left"])
        self.assertEqual(body["skipped_not_paused"], ["right"])

    def test_reload_per_system_errors_dont_block_others(self):
        # Stub one loader to raise; the other still reloads.
        from c64cast.control_plane import build_app

        playlists = {n: _fake_playlist(n) for n in ("a", "b")}

        def _boom():
            raise RuntimeError("disk on fire")

        app = build_app(
            playlists=playlists,
            config_loaders={
                "a": _boom,
                "b": lambda: playlists["b"].scenes,
            },
            interstitial_factories={
                "a": lambda: lambda nm: None,
                "b": lambda: lambda nm: None,
            },
        )
        client = TestClient(app)
        r = client.post("/reload?system=all")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["reloaded"], {"b": 2})
        self.assertIn("disk on fire", body["errors"]["a"])
        playlists["b"].request_reload.assert_called_once()
        playlists["a"].request_reload.assert_not_called()

    def test_reload_for_system_without_loader_surfaces_clean_error(self):
        # Per-system reload for a system with no config_loader registered
        # (e.g. defaults-only single-system mode) must yield a friendly
        # per-system error, not a KeyError → 500.
        from c64cast.control_plane import build_app

        playlists = {n: _fake_playlist(n) for n in ("with_path", "no_path")}
        app = build_app(
            playlists=playlists,
            config_loaders={"with_path": lambda: playlists["with_path"].scenes},
            interstitial_factories={
                "with_path": lambda: lambda nm: None,
            },
        )
        client = TestClient(app)
        r = client.post("/reload?system=no_path")
        # Single-system reload with no loader → all failed → 500
        self.assertEqual(r.status_code, 500)
        self.assertIn("no_path", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
