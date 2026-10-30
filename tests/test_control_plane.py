"""Tests for the multi-system control plane.

The shape-and-routing tests (`ResponseShapeTest`) call the helper
functions directly and don't depend on fastapi/httpx. The end-to-end
HTTP tests (`SingleSystemBackCompatTest`, `MultiSystemTest`) drive the
real FastAPI app via TestClient and skip when httpx isn't available
(TestClient requires it)."""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportOptionalCall=false
from __future__ import annotations

import asyncio
import http.client
import socket
import threading
import time
import unittest
import warnings
from typing import Any
from unittest.mock import MagicMock, patch

try:
    import fastapi  # noqa: F401

    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

try:
    # TestClient also needs httpx — fastapi declares it as an optional extra,
    # so a bare `fastapi` can still leave the import failing at runtime. The
    # import itself warns (starlette wants httpx2), and the filter is
    # category-blind because starlette raises a UserWarning subclass only it names.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False
    TestClient = None  # type: ignore[misc,assignment]

from c64cast.app.playlist import Playlist


def _fake_playlist(name: str, *, scene_count: int = 2) -> Playlist:
    """A MagicMock'd Playlist that satisfies what the control plane reads:
    .current.name, .index, .scenes (list with .name + .duration_s), .pause/
    skip/resume events with .set()/is_set(), .api.stats, .api.format_write_latency(),
    .transitioning, .on_card, .request_reload()."""
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
    pl.on_card = False
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
        from c64cast.control.control_plane import _status_for

        pl = _fake_playlist("a")
        body = _status_for(pl)
        self.assertEqual(body["current_scene"], "a-scene-0")
        self.assertEqual(body["n_scenes"], 2)
        self.assertEqual(body["current_index"], 0)
        self.assertFalse(body["paused"])
        self.assertFalse(body["transitioning"])
        self.assertEqual(body["stats"], {"writes": 100})
        self.assertEqual(body["write_latency"], "lat 5ms")

    def test_status_reports_the_card_not_the_stale_flag(self):
        from c64cast.control.control_plane import _status_for

        pl = _fake_playlist("a")
        pl.transitioning = True
        self.assertFalse(_status_for(pl)["transitioning"])
        pl.on_card = True
        self.assertTrue(_status_for(pl)["transitioning"])

    def test_status_dict_reflects_pause_state(self):
        from c64cast.control.control_plane import _status_for

        pl = _fake_playlist("a")
        pl.pause_event.set()
        self.assertTrue(_status_for(pl)["paused"])

    def test_scenes_dict_shape(self):
        from c64cast.control.control_plane import _scenes_for

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
        from c64cast.control.control_plane import build_app

        with self.assertRaises(ValueError) as cm:
            build_app(playlists={}, config_loaders={}, interstitial_factories={})
        self.assertIn("at least one playlist", str(cm.exception))


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed (control extra)")
class StartControlServerTest(unittest.TestCase):
    def test_a_server_that_never_bound_is_raised_not_returned(self):
        from c64cast.control.control_plane import start_control_server

        with patch("c64cast.control.control_plane.ControlServer") as server_cls:
            server_cls.return_value.start.return_value = False
            with self.assertRaises(RuntimeError) as cm:
                start_control_server(
                    "127.0.0.1",
                    8765,
                    playlists={"a": _fake_playlist("a")},
                    config_loaders={},
                    interstitial_factories={},
                )
        self.assertIn("127.0.0.1:8765", str(cm.exception))


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class RegistryTest(unittest.TestCase):
    """`build_app_for_registry` reads its playlists through providers called
    per request, so one app can serve a host that starts and stops sessions
    under it. No session ⇒ 503 on every data/action route, and the *same*
    responses as `build_app` once one is running."""

    def _client(self, registry: dict) -> Any:
        from c64cast.control.control_plane import build_app_for_registry

        app = build_app_for_registry(
            lambda: registry,
            lambda: {n: (lambda p=p: p.scenes) for n, p in registry.items()},
            lambda: {n: (lambda: lambda nm: None) for n in registry},
        )
        return TestClient(app)

    def test_every_route_is_503_while_idle(self):
        client = self._client({})
        for method, path in (
            ("get", "/status"),
            ("get", "/scenes"),
            ("post", "/pause"),
            ("post", "/resume"),
            ("post", "/skip"),
            ("post", "/reload"),
        ):
            with self.subTest(path=path):
                r = getattr(client, method)(path)
                self.assertEqual(r.status_code, 503)
                self.assertIn("no session", r.json()["detail"])

    def test_a_session_appearing_is_picked_up_without_a_rebuild(self):
        registry: dict = {}
        client = self._client(registry)
        self.assertEqual(client.get("/status").status_code, 503)
        registry["system"] = _fake_playlist("system")
        r = client.get("/status")
        self.assertEqual(r.status_code, 200)
        # Same unwrapped single-system shape build_app hands back.
        self.assertEqual(r.json()["current_scene"], "system-scene-0")

    def test_a_session_going_away_returns_to_503(self):
        registry = {"system": _fake_playlist("system")}
        client = self._client(registry)
        self.assertEqual(client.post("/pause").status_code, 200)
        registry.clear()
        self.assertEqual(client.post("/pause").status_code, 503)

    def test_a_new_generation_replaces_the_old_playlists(self):
        # A restarted session swaps in fresh Playlists under the same names;
        # a route holding the old map would act on a torn-down generation.
        first = _fake_playlist("system")
        registry = {"system": first}
        client = self._client(registry)
        second = _fake_playlist("system")
        registry["system"] = second
        client.post("/skip")
        self.assertTrue(second.skip_event.is_set())
        self.assertFalse(first.skip_event.is_set())


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class SingleSystemBackCompatTest(unittest.TestCase):
    """When there's exactly one system, omitting ?system= must return the
    same un-wrapped JSON shape today's clients expect."""

    def _client(self):
        from c64cast.control.control_plane import build_app

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
class TransportOriginTest(unittest.TestCase):
    """The four transport verbs take only a query param and no body, so a
    cross-site form POST at one is a CORS-simple request with no preflight to
    refuse — and the unprompted default is `token = ""` on loopback. A page the
    performer visits mid-set could pause, skip or reload the running show."""

    def _client(self):
        from c64cast.control.control_plane import build_app

        pl = _fake_playlist("system")
        app = build_app(
            playlists={"system": pl},
            config_loaders={"system": lambda: pl.scenes},
            interstitial_factories={"system": lambda: lambda n: None},
        )
        return TestClient(app), pl

    def test_a_cross_origin_transport_verb_is_refused(self):
        client, pl = self._client()
        for path in ("/pause", "/resume", "/skip", "/reload"):
            with self.subTest(path=path):
                r = client.post(path, headers={"Origin": "http://evil.example"})
                self.assertEqual(r.status_code, 403)
        self.assertFalse(pl.pause_event.is_set())
        self.assertFalse(pl.skip_event.is_set())

    def test_a_same_origin_transport_verb_is_served(self):
        client, pl = self._client()
        r = client.post("/skip", headers={"Origin": "http://testserver"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(pl.skip_event.is_set())

    def test_no_origin_header_is_served(self):
        # curl, a script, Home Assistant — exactly the caller the open mode
        # describes, and why this is safe to add to a shipped API.
        client, pl = self._client()
        r = client.post("/pause")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(pl.pause_event.is_set())

    def test_reads_are_untouched(self):
        # The gate covers state changes only; a cross-origin browser read is
        # already refused by CORS itself, and `status` is what a dashboard
        # polls with no Origin at all.
        client, _ = self._client()
        r = client.get("/status", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 200)

    def test_every_state_changing_route_is_in_transport_paths(self):
        # The drift guard the constant's comment promises. TRANSPORT_PATHS is
        # hand-maintained, and a new verb added to this app without a matching
        # entry gets no gate at all — silently, since nothing else would fail.
        from c64cast.control.control_plane import TRANSPORT_PATHS

        # The two families the comment names as guarding themselves: they run
        # their own `same_origin` check (`/perf`) or are reachable only on a
        # `--serve` host behind a SameSite=Strict cookie (`/api`).
        self_guarded = ("/perf/", "/api/")

        client, _ = self._client()
        writes = {
            route.path
            for route in client.app.routes
            if {"POST", "PUT", "PATCH", "DELETE"} & set(getattr(route, "methods", ()) or ())
        }
        self.assertTrue(writes, "found no write routes — the walk is wrong, not the app")
        ungated = {p for p in writes if not p.startswith(self_guarded)} - set(TRANSPORT_PATHS)
        self.assertEqual(ungated, set(), f"state-changing route with no origin gate: {ungated}")
        # And nothing has been left in the constant that the app no longer serves.
        self.assertEqual(set(TRANSPORT_PATHS) - writes, set())


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class MultiSystemTest(unittest.TestCase):
    def _client(self):
        from c64cast.control.control_plane import build_app

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
        from c64cast.control.control_plane import build_app

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
        # Per-system reload for a system with no config_loader registered must
        # yield a friendly per-system error, not a KeyError turned 500.
        from c64cast.control.control_plane import build_app

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


try:
    import uvicorn  # noqa: F401
    import websockets  # noqa: F401

    HAVE_SERVER_STACK = True
except ImportError:
    HAVE_SERVER_STACK = False


async def _echo_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Answers a request with the size of its body, and echoes WebSocket text.
    `/slow` waits before it starts reading the body, the way a media upload
    waits on the disk; `/early` refuses without reading it, the way the token
    gate, a missing route or a size cap does."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            await send({"type": "websocket.send", "text": message.get("text", "")})
    if scope["path"] == "/slow":
        await asyncio.sleep(4 * _TEST_REQUEST_TIMEOUT_S)
    if scope["path"] == "/early":
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})
        return
    size = 0
    while True:
        message = await receive()
        size += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    body = str(size).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


_TEST_REQUEST_TIMEOUT_S = 0.4
_CLOSE_WAIT_S = 5.0


@unittest.skipUnless(HAVE_SERVER_STACK, "uvicorn/websockets not installed")
class ConnectionReapingTest(unittest.TestCase):
    """A real `ControlServer` on loopback, held to a short request timeout.

    An unauthenticated peer that opens connections and sends nothing used to
    hold each one forever, until the process ran out of file descriptors and
    its next outbound connect (the DMA redial) failed with `EMFILE`."""

    def _serve(self, app: Any = _echo_app, *, max_connections: int = 64) -> int:
        from c64cast.control import control_plane

        for name, value in (
            ("REQUEST_TIMEOUT_S", _TEST_REQUEST_TIMEOUT_S),
            ("MAX_CONNECTIONS", max_connections),
        ):
            p = patch.object(control_plane, name, value)
            p.start()
            self.addCleanup(p.stop)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        server = control_plane.ControlServer("127.0.0.1", port, app, label="probe")
        self.addCleanup(server.stop)
        with self.assertLogs("c64cast.control.control_plane", "INFO"):
            self.assertTrue(server.start())
        return port

    def _connect(self, port: int) -> socket.socket:
        s = socket.create_connection(("127.0.0.1", port), timeout=_CLOSE_WAIT_S)
        self.addCleanup(s.close)
        return s

    def _assert_closed_by_server(self, s: socket.socket) -> None:
        try:
            data = s.recv(1024)
        except ConnectionResetError:
            return
        except TimeoutError:
            self.fail(f"the server still held the connection after {_CLOSE_WAIT_S} s")
        self.assertEqual(data, b"")

    def _request(self, port: int, body: bytes = b"") -> bytes:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=_CLOSE_WAIT_S)
        try:
            conn.request("POST", "/", body=body)
            return conn.getresponse().read()
        finally:
            conn.close()

    def test_a_connection_that_sends_nothing_is_closed(self):
        port = self._serve()
        self._assert_closed_by_server(self._connect(port))

    def test_a_request_head_that_never_finishes_is_closed(self):
        port = self._serve()
        self._assert_dripped_head_is_cut_off(self._connect(port))

    def test_a_second_head_that_never_finishes_is_closed(self):
        """Any byte cancels uvicorn's keep-alive timer, so after one served
        request a dripped head is bounded by the request timer alone."""
        port = self._serve()
        s = self._connect(port)
        s.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        reply = http.client.HTTPResponse(s)
        reply.begin()
        self.assertEqual(reply.read(), b"0")
        self._assert_dripped_head_is_cut_off(s)

    def test_an_idle_keep_alive_connection_outlives_the_request_timeout(self):
        port = self._serve()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=_CLOSE_WAIT_S)
        self.addCleanup(conn.close)
        conn.request("POST", "/", body=b"a")
        self.assertEqual(conn.getresponse().read(), b"1")
        time.sleep(4 * _TEST_REQUEST_TIMEOUT_S)
        conn.request("POST", "/", body=b"bb")
        self.assertEqual(conn.getresponse().read(), b"2")

    def _assert_dripped_head_is_cut_off(self, s: socket.socket) -> None:
        self._assert_drip_is_cut_off(s, b"GET / HTTP/1.1\r\nHost: x\r\n", b"X-Drip: 1\r\n")

    def _assert_drip_is_cut_off(self, s: socket.socket, opening: bytes, drip: bytes) -> None:
        s.sendall(opening)
        s.settimeout(_TEST_REQUEST_TIMEOUT_S / 4)
        deadline = time.monotonic() + _CLOSE_WAIT_S
        while time.monotonic() < deadline:
            try:
                s.sendall(drip)
                if s.recv(1024) == b"":
                    return
            except TimeoutError:
                continue
            except ConnectionError:
                return
        self.fail(f"a request still arriving after {_CLOSE_WAIT_S} s was never cut off")

    def test_a_body_dripped_after_a_401_is_closed(self):
        """The token gate answers without reading the body, and uvicorn then
        discards whatever body arrives, so a peer with no token could drip it
        forever."""
        from c64cast.control.control_plane import build_app_for_registry

        app = build_app_for_registry(dict, dict, dict, token="t" * 32)
        port = self._serve(app)
        self._assert_drip_is_cut_off(
            self._connect(port),
            b"POST /pause HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000000\r\n\r\n",
            b"x",
        )

    def test_a_body_dripped_after_any_early_response_is_closed(self):
        port = self._serve()
        self._assert_drip_is_cut_off(
            self._connect(port),
            b"PUT /early HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000000\r\n\r\n",
            b"x",
        )

    def test_a_body_finished_after_an_early_response_then_idle_is_closed(self):
        """uvicorn arms its keep-alive timer when the response completes, and
        the late body's bytes cancel it again."""
        port = self._serve()
        s = self._connect(port)
        s.sendall(b"PUT /early HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\n")
        reply = http.client.HTTPResponse(s)
        reply.begin()
        self.assertEqual(reply.status, 413)
        reply.read()
        s.sendall(b"12345")
        self._assert_closed_by_server(s)

    def test_a_body_that_stops_arriving_is_closed(self):
        port = self._serve()
        s = self._connect(port)
        s.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n0123456789")
        self._assert_closed_by_server(s)

    def test_a_body_that_keeps_arriving_is_not_cut_off(self):
        """A media upload over a slow link takes as long as it takes; what is
        bounded is the silence between its chunks."""
        port = self._serve()
        chunk = b"x" * 16
        chunks = 12
        s = self._connect(port)
        s.sendall(
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % (len(chunk) * chunks)
        )
        for _ in range(chunks):
            time.sleep(_TEST_REQUEST_TIMEOUT_S / 4)
            s.sendall(chunk)
        reply = http.client.HTTPResponse(s)
        reply.begin()
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.read(), str(len(chunk) * chunks).encode())

    def test_a_body_the_app_is_slow_to_read_is_not_cut_off(self):
        """Past uvicorn's buffer high-water mark it stops reading the socket
        until the app catches up, so the peer goes quiet through no fault of
        its own."""
        port = self._serve()
        body = b"x" * (1 << 20)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=_CLOSE_WAIT_S)
        self.addCleanup(conn.close)
        conn.request("POST", "/slow", body=body)
        self.assertEqual(conn.getresponse().read(), str(len(body)).encode())

    def test_connections_past_the_cap_are_refused_and_the_slot_comes_back(self):
        from c64cast.control import control_plane

        port = self._serve(max_connections=3)
        p = patch.object(control_plane, "_last_refusal_warning", float("-inf"))
        p.start()
        self.addCleanup(p.stop)
        held = [self._connect(port) for _ in range(3)]
        with self.assertLogs("c64cast.control.control_plane", "WARNING"):
            refused = self._connect(port)
            self._assert_closed_by_server(refused)
        for s in held:
            s.close()
        # Each held socket's close reaches the server asynchronously.
        deadline = time.monotonic() + _CLOSE_WAIT_S
        while True:
            try:
                self.assertEqual(self._request(port, b"abc"), b"3")
                return
            except (ConnectionResetError, http.client.RemoteDisconnected):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def test_an_idle_websocket_outlives_the_request_timeout(self):
        """The performance console sits on its socket between pushes; once a
        connection is a WebSocket, uvicorn's ping/pong is what reaps it."""
        from websockets.sync.client import connect

        port = self._serve()
        with connect(f"ws://127.0.0.1:{port}/perf/ws", open_timeout=_CLOSE_WAIT_S) as ws:
            time.sleep(4 * _TEST_REQUEST_TIMEOUT_S)
            ws.send("still here")
            self.assertEqual(ws.recv(timeout=_CLOSE_WAIT_S), "still here")

    def test_a_keep_alive_connection_serves_a_second_request(self):
        port = self._serve()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=_CLOSE_WAIT_S)
        self.addCleanup(conn.close)
        for body in (b"a", b"bb"):
            conn.request("POST", "/", body=body)
            self.assertEqual(conn.getresponse().read(), str(len(body)).encode())


if __name__ == "__main__":
    unittest.main()
