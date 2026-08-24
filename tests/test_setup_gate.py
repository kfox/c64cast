"""Tests for the appliance setup window's gate.

`MiddlewareUnitTest` drives `SetupGateMiddleware` directly over a raw ASGI
scope, the same way `AuthHelpersTest` in test_control_auth.py exercises
`TokenAuthMiddleware` without a real app. `IntegrationTest` builds a real
FastAPI app, installs the gate through `install_setup_gate` (so `owned_segments`
does the real work), and drives it with TestClient."""

# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOptionalCall=false
from __future__ import annotations

import asyncio
import unittest
import warnings
from typing import Any

try:
    import fastapi  # noqa: F401

    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False
    TestClient = None  # type: ignore[misc,assignment]

from c64cast.control.setup_gate import (
    SETUP_PAGE_PATH,
    SETUP_PATH,
    SetupGateMiddleware,
    install_setup_gate,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _RecordingApp:
    """The next ASGI callable in the chain — records whether it was reached."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"reached"})


def _http_scope(path: str, method: str = "GET") -> dict[str, Any]:
    return {"type": "http", "path": path, "method": method, "headers": []}


async def _collect(app: Any, scope: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent, True


class MiddlewareUnitTest(unittest.TestCase):
    def test_reserved_segment_is_denied(self):
        inner = _RecordingApp()
        mw = SetupGateMiddleware(inner, reserved=frozenset({"status", "api"}))
        sent, _ = _run(_collect(mw, _http_scope("/status")))
        self.assertEqual(inner.calls, [])
        self.assertEqual(sent[0]["status"], 503)

    def test_setup_path_is_always_allowed_even_when_its_segment_is_reserved(self):
        inner = _RecordingApp()
        mw = SetupGateMiddleware(inner, reserved=frozenset({"api"}))
        sent, _ = _run(_collect(mw, _http_scope(SETUP_PATH)))
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(sent[0]["status"], 200)

    def test_assets_pass_through_even_when_owned_segments_reports_them(self):
        inner = _RecordingApp()
        mw = SetupGateMiddleware(inner, reserved=frozenset({"assets", "api"}))
        sent, _ = _run(_collect(mw, _http_scope("/assets/app.js")))
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(sent[0]["status"], 200)

    def test_an_unclaimed_path_falls_through_to_the_shell(self):
        inner = _RecordingApp()
        mw = SetupGateMiddleware(inner, reserved=frozenset({"api", "perf"}))
        sent, _ = _run(_collect(mw, _http_scope("/")))
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(sent[0]["status"], 200)

    def test_the_shells_own_route_for_the_form_passes_through(self):
        # A client route: no server route claims the segment, so it reaches the
        # shell's catch-all and the form comes back after a reload.
        inner = _RecordingApp()
        mw = SetupGateMiddleware(inner, reserved=frozenset({"api", "perf", "status"}))
        sent, _ = _run(_collect(mw, _http_scope(SETUP_PAGE_PATH)))
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(sent[0]["status"], 200)

    def test_lifespan_scope_passes_straight_through(self):
        inner = _RecordingApp()
        mw = SetupGateMiddleware(inner, reserved=frozenset({"api"}))
        _run(_collect(mw, {"type": "lifespan"}))
        self.assertEqual(len(inner.calls), 1)

    def test_a_denied_websocket_closes_with_1013(self):
        inner = _RecordingApp()
        mw = SetupGateMiddleware(inner, reserved=frozenset({"api"}))
        scope = {"type": "websocket", "path": "/api/ws"}
        sent, _ = _run(_collect(mw, scope))
        self.assertEqual(inner.calls, [])
        self.assertEqual(sent[0], {"type": "websocket.close", "code": 1013})


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class IntegrationTest(unittest.TestCase):
    """A real app, so `owned_segments` (read off `app.routes`) does the actual
    work instead of a hand-fed `reserved` set."""

    def _app(self):
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/status")
        def status():
            return {"ok": True}

        @app.get(SETUP_PATH)
        def setup():
            return {"pending": True}

        @app.get("/assets/{name}")
        def assets(name: str):
            return {"name": name}

        install_setup_gate(app)
        return app

    def test_a_registered_route_is_blocked_while_pending(self):
        client = TestClient(self._app())
        resp = client.get("/status")
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.json()["setup_required"])

    def test_the_setup_route_itself_is_reachable(self):
        client = TestClient(self._app())
        resp = client.get(SETUP_PATH)
        self.assertEqual(resp.status_code, 200)

    def test_assets_are_reachable_despite_owning_a_segment(self):
        client = TestClient(self._app())
        resp = client.get("/assets/app.js")
        self.assertEqual(resp.status_code, 200)

    def test_an_unregistered_path_is_not_blocked_by_the_gate(self):
        # No route claims "/whatever", so the gate lets it through — whatever
        # answers next (here, FastAPI's own 404) is a separate concern.
        client = TestClient(self._app())
        resp = client.get("/whatever")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("setup_required", resp.text)


if __name__ == "__main__":
    unittest.main()
