"""Tests for the shared-token gate on the control plane.

`AuthHelpersTest` drives the token plumbing directly and needs no FastAPI. The
end-to-end tests build the real control-plane app with a token and drive it
through TestClient, mirroring tests/test_control_plane.py.

`EveryRouteIsProtectedTest` is the payoff for choosing a middleware over
per-route dependencies: it walks `app.routes` and asserts that everything
outside `PUBLIC_PATHS` refuses an unauthenticated caller, so a route added
later cannot silently ship open.

Not unit-testable here, and covered by the hw-visual-verify pass instead: that
a real browser's WebSocket handshake carries the login cookie (TestClient's
cookie jar is not a browser's, and `SameSite` is never evaluated)."""

# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOptionalCall=false
from __future__ import annotations

import unittest
import warnings
from typing import Any
from unittest import mock

try:
    import fastapi  # noqa: F401

    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

try:
    # Silenced like test_control_plane's copy — the httpx2 deprecation is a
    # dependency decision, not per-worker test output.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from starlette.websockets import WebSocketDisconnect

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False
    TestClient = None  # type: ignore[misc,assignment]
    WebSocketDisconnect = Exception  # type: ignore[misc,assignment]

from c64cast.control.auth import (
    COOKIE_NAME,
    MIN_TOKEN_LENGTH,
    PUBLIC_PATHS,
    ROLE_FULL,
    ROLE_VIEWER,
    SCOPE_ROLE_KEY,
    BodyTooLarge,
    RoleRequired,
    TokenAuthMiddleware,
    _presented_token,
    _safe_next,
    install_auth,
    is_viewer,
    login_page,
    match_role,
    read_body,
    require_full,
    role_of,
)
from c64cast.control.transport import LiveTuneTracker

TOKEN = "full-token-value"
VIEWER = "viewer-token-value"


class _FakeTempo:
    bpm = 120.0
    running = True
    source = "internal"
    beats_per_bar = 4

    def beat_phase_at(self, now: float | None = None) -> float:
        return 0.0

    def bar_phase_at(self, now: float | None = None) -> float:
        return 0.0

    def tap(self, now: float) -> None:
        pass


class _FakePerf:
    def __init__(self) -> None:
        self.active_slot: int | None = None
        self.armed_slot: int | None = None
        self.armed_detail: tuple[int, str, float, float] | None = None
        self.events: list[tuple[int, bool]] = []
        self.look_events: list[tuple[int, bool]] = []

    def clips_info(self) -> list[dict[str, Any]]:
        return [{"slot": 1, "name": "A", "launch": "trigger", "quantize": "bar", "loop": True}]

    def enqueue(self, event: Any) -> None:
        self.events.append((event.slot, event.pressed))

    def enqueue_look(self, slot: int, *, save: bool) -> None:
        self.look_events.append((slot, save))

    def saved_look_slots(self) -> list[int]:
        return []


class _FakeScene:
    def __init__(self, name: str) -> None:
        self.name = name
        self.effects: list[Any] = []
        self.duration_s = 10.0


class _FakeApi:
    stats = {"writes": 1}

    def format_write_latency(self) -> str:
        return "lat 5ms"


class _FakePlaylist:
    """Satisfies both the control-plane routes and the perf bridge, since the
    auth gate spans them."""

    def __init__(self) -> None:
        import threading

        self.tempo = _FakeTempo()
        self.performance = _FakePerf()
        self.current = _FakeScene("demo")
        self.scenes = [self.current]
        self.index = 0
        self.transitioning = False
        self.api = _FakeApi()
        self.pause_event = threading.Event()
        self.resume_event = threading.Event()
        self.skip_event = threading.Event()
        self.live_tracker = LiveTuneTracker()
        self.config_path = ""


def _app(*, token: str = TOKEN, viewer_token: Any = VIEWER) -> tuple[Any, _FakePlaylist]:
    from c64cast.control.control_plane import build_app

    pl = _FakePlaylist()
    app = build_app(
        playlists={"c64cast": pl},
        config_loaders={},
        interstitial_factories={},
        token=token,
        viewer_token=viewer_token,
    )
    return app, pl


def _scope(
    *, headers: list[tuple[bytes, bytes]] | None = None, query: bytes = b""
) -> dict[str, Any]:
    return {
        "type": "http",
        "path": "/status",
        "method": "GET",
        "headers": headers or [],
        "query_string": query,
    }


class AuthHelpersTest(unittest.TestCase):
    def test_match_role_picks_the_right_role(self):
        self.assertEqual(match_role(TOKEN, TOKEN, VIEWER), "full")
        self.assertEqual(match_role(VIEWER, TOKEN, VIEWER), "viewer")
        self.assertIsNone(match_role("nope", TOKEN, VIEWER))
        self.assertIsNone(match_role("", TOKEN, VIEWER))
        self.assertIsNone(match_role(None, TOKEN, VIEWER))

    def test_match_role_ignores_viewer_when_unset(self):
        self.assertIsNone(match_role(VIEWER, TOKEN, ""))

    def test_non_ascii_token_mismatches_rather_than_raising(self):
        # hmac.compare_digest rejects non-ASCII str with TypeError; the UTF-8
        # encode is what keeps a junk token a 401 instead of a 500.
        self.assertIsNone(match_role("tökén", TOKEN, VIEWER))

    def test_token_source_precedence(self):
        self.assertEqual(
            _presented_token(
                _scope(
                    headers=[
                        (b"authorization", b"Bearer from-bearer"),
                        (b"x-c64cast-token", b"from-header"),
                        (b"cookie", f"{COOKIE_NAME}=from-cookie".encode()),
                    ],
                    query=b"token=from-query",
                )
            ),
            "from-bearer",
        )
        self.assertEqual(
            _presented_token(
                _scope(headers=[(b"x-c64cast-token", b"from-header")], query=b"token=from-query")
            ),
            "from-header",
        )
        self.assertEqual(
            _presented_token(
                _scope(
                    headers=[(b"cookie", f"{COOKIE_NAME}=from-cookie".encode())],
                    query=b"token=from-query",
                )
            ),
            "from-query",
        )
        self.assertEqual(
            _presented_token(_scope(headers=[(b"cookie", f"{COOKIE_NAME}=c".encode())])),
            "c",
        )
        self.assertIsNone(_presented_token(_scope()))

    def test_header_name_is_matched_case_insensitively(self):
        self.assertEqual(
            _presented_token(_scope(headers=[(b"X-C64Cast-Token", b"t")])),
            "t",
        )

    def test_cookie_header_without_our_cookie(self):
        self.assertIsNone(_presented_token(_scope(headers=[(b"cookie", b"other=1")])))

    def test_a_malformed_sibling_cookie_does_not_hide_ours(self):
        # CPython's SimpleCookie discards the WHOLE jar on the first segment
        # its pattern rejects, without raising — so any other service on the
        # same host setting a cookie with an illegal character used to make
        # this console unreachable in that browser: a 401, the login form, a
        # fresh Set-Cookie that replaces ours and not the offender, and a 401
        # again. Both orders, because a bad sibling after ours wiped it too.
        for header in (
            f"bad cookie here; {COOKIE_NAME}=SECRET".encode(),
            f"{COOKIE_NAME}=SECRET; bad cookie here".encode(),
            f'x="un}}quoted; {COOKIE_NAME}=SECRET'.encode(),
        ):
            with self.subTest(header=header):
                self.assertEqual(_presented_token(_scope(headers=[(b"cookie", header)])), "SECRET")

    def test_a_quoted_cookie_value_is_unwrapped(self):
        self.assertEqual(
            _presented_token(_scope(headers=[(b"cookie", f'{COOKIE_NAME}="q"'.encode())])),
            "q",
        )

    def test_an_empty_bearer_header_falls_through_to_the_next_source(self):
        # `Authorization: Bearer ` is what some proxies emit for an unset
        # credential. It carries no claim, so it must not suppress a perfectly
        # good cookie and turn a valid session into a 401 that looks like a
        # wrong token.
        scope = _scope(
            headers=[
                (b"authorization", b"Bearer "),
                (b"cookie", f"{COOKIE_NAME}=from-cookie".encode()),
            ]
        )
        self.assertEqual(_presented_token(scope), "from-cookie")
        self.assertEqual(
            _presented_token(_scope(headers=[(b"authorization", b"Bearer ")], query=b"token=q")),
            "q",
        )
        self.assertIsNone(_presented_token(_scope(headers=[(b"authorization", b"Bearer ")])))

    def test_safe_next_rejects_anything_offsite(self):
        self.assertEqual(_safe_next("/scenes"), "/scenes")
        self.assertEqual(_safe_next("//evil.example"), "/perf")
        self.assertEqual(_safe_next("https://evil.example"), "/perf")
        # `/\host` resolves offsite too (the URL spec's relative-slash state).
        # It is neutralized downstream by Starlette's percent-encoding today,
        # which is not where this function's own contract should live.
        self.assertEqual(_safe_next("/\\evil.example"), "/perf")
        self.assertEqual(_safe_next(""), "/perf")
        self.assertEqual(_safe_next(None), "/perf")

    def test_middleware_refuses_an_empty_token(self):
        with self.assertRaises(ValueError):
            TokenAuthMiddleware(None, token="")

    def test_the_public_path_floor_is_the_middlewares_own(self):
        # The docstring promised "never narrower than PUBLIC_PATHS" and only
        # one caller happened to union it in. A caller passing a set without
        # the login exchange would get an app whose 401 serves a form that
        # posts back to a route that can only 401 again.
        gate = TokenAuthMiddleware(None, token=TOKEN, public_paths=("/api/setup",))
        self.assertLessEqual(PUBLIC_PATHS, gate._public)
        self.assertIn("/api/setup", gate._public)


class RoleSeamTest(unittest.TestCase):
    """The per-route half of authorization, with no app in sight.

    The middleware can only say "is this a write?", which is blind to a GET
    that hands back host-authored text or a credential. `require_full` is
    where such a route says so, and it raises rather than importing FastAPI
    precisely so this test needs neither."""

    def test_role_reads_off_the_scope_under_one_key(self):
        self.assertEqual(role_of({SCOPE_ROLE_KEY: ROLE_FULL}), ROLE_FULL)
        self.assertIsNone(role_of({}))

    def test_is_viewer_is_the_only_place_the_comparison_is_spelled(self):
        self.assertTrue(is_viewer({SCOPE_ROLE_KEY: ROLE_VIEWER}))
        self.assertFalse(is_viewer({SCOPE_ROLE_KEY: ROLE_FULL}))
        # No gate installed: historically open, and deliberately not a viewer.
        self.assertFalse(is_viewer({}))

    def test_require_full_refuses_a_viewer_and_passes_everyone_else(self):
        with self.assertRaises(RoleRequired):
            require_full({SCOPE_ROLE_KEY: ROLE_VIEWER})
        self.assertIsNone(require_full({SCOPE_ROLE_KEY: ROLE_FULL}))
        self.assertIsNone(require_full({}))


class ReadBodyTest(unittest.TestCase):
    """The cap on the two routes reachable with no credential at all."""

    class _Request:
        def __init__(self, chunks, *, content_length=None):
            self._chunks = chunks
            self.headers = {} if content_length is None else {"content-length": content_length}

        async def stream(self):
            for chunk in self._chunks:
                yield chunk

    def _read(self, request, **kwargs):
        import asyncio

        return asyncio.run(read_body(request, **kwargs))

    def test_a_body_under_the_cap_is_returned_whole(self):
        self.assertEqual(self._read(self._Request([b"ab", b"cd"])), b"abcd")

    def test_a_claimed_content_length_over_the_cap_is_refused_before_reading(self):
        request = self._Request([b"x"], content_length="99")
        with self.assertRaises(BodyTooLarge):
            self._read(request, max_bytes=8)

    def test_a_lying_content_length_is_still_refused_while_streaming(self):
        # The only check a chunked body cannot lie about.
        request = self._Request([b"x" * 8, b"x" * 8], content_length="2")
        with self.assertRaises(BodyTooLarge):
            self._read(request, max_bytes=8)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed (control extra)")
class InstallAuthTest(unittest.TestCase):
    def test_no_token_leaves_the_app_open(self):
        from fastapi import FastAPI

        self.assertFalse(install_auth(FastAPI(), token=""))

    def test_viewer_token_alone_warns_and_stays_off(self):
        from fastapi import FastAPI

        with self.assertLogs("c64cast.control.auth", level="WARNING") as logs:
            self.assertFalse(install_auth(FastAPI(), token="", viewer_token=VIEWER))
        self.assertIn("stays OFF", "\n".join(logs.output))

    def test_identical_tokens_rejected(self):
        from fastapi import FastAPI

        with self.assertRaises(ValueError):
            install_auth(FastAPI(), token=TOKEN, viewer_token=TOKEN)

    def test_token_turns_it_on(self):
        from fastapi import FastAPI

        self.assertTrue(install_auth(FastAPI(), token=TOKEN, viewer_token=VIEWER))

    def test_a_short_token_is_warned_about_but_honored(self):
        # Nothing in this tree throttles login attempts, so `token = "c64"` is
        # a console that falls to a few thousand requests. Refusing one
        # outright would break runs that work today, so it warns — and the
        # policy is now declared once, here, rather than on the one setup
        # route that used to own it.
        from fastapi import FastAPI

        short = "c" * (MIN_TOKEN_LENGTH - 1)
        with self.assertLogs("c64cast.control.auth", level="WARNING") as logs:
            self.assertTrue(install_auth(FastAPI(), token=short))
        self.assertIn(str(MIN_TOKEN_LENGTH), "\n".join(logs.output))


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class ControlPlaneAuthTest(unittest.TestCase):
    def test_no_token_configured_means_no_gate(self):
        app, _pl = _app(token="", viewer_token="")
        self.assertEqual(TestClient(app).get("/status").status_code, 200)

    def test_missing_token_is_401(self):
        app, _pl = _app()
        r = TestClient(app).get("/status")
        self.assertEqual(r.status_code, 401)
        self.assertIn("bearer", r.headers.get("www-authenticate", "").lower())

    def test_wrong_token_is_401(self):
        app, _pl = _app()
        r = TestClient(app).get("/status", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_every_accepted_token_source(self):
        app, _pl = _app()
        cases = {
            "bearer": {"headers": {"Authorization": f"Bearer {TOKEN}"}},
            "header": {"headers": {"X-C64Cast-Token": TOKEN}},
            "query": {"params": {"token": TOKEN}},
        }
        for label, kw in cases.items():
            with self.subTest(source=label):
                r = TestClient(app).get("/status", **kw)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["current_scene"], "demo")
        with self.subTest(source="cookie"):
            client = TestClient(app)
            client.cookies.set(COOKIE_NAME, TOKEN)
            self.assertEqual(client.get("/status").status_code, 200)

    def test_writes_need_the_full_token(self):
        app, pl = _app()
        client = TestClient(app)
        self.assertEqual(client.post("/pause").status_code, 401)
        self.assertFalse(pl.pause_event.is_set())
        r = client.post("/pause", headers={"X-C64Cast-Token": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(pl.pause_event.is_set())


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class LoginTest(unittest.TestCase):
    """`/api/login` is the only public path: it is how a browser — which can
    set no headers on a navigation or a WebSocket handshake — gets a token in
    the first place."""

    def test_login_is_reachable_without_a_token(self):
        app, _pl = _app()
        r = TestClient(app).get("/api/login")
        # The route's own 401, not the middleware's: JSON, no WWW-Authenticate.
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["ok"], False)
        self.assertNotIn("www-authenticate", r.headers)

    def test_get_sets_a_cookie_and_redirects_to_the_console(self):
        app, _pl = _app()
        client = TestClient(app)
        r = client.get("/api/login", params={"token": TOKEN}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/perf")
        cookie = r.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        # The jar now authenticates everything else, as a browser's would.
        self.assertEqual(client.get("/status").status_code, 200)

    def test_cookie_carries_the_configured_secret_not_the_callers_string(self):
        # Byte-equal by the time the cookie is written, so this can only ever
        # fail if the route starts echoing the request back — which is the
        # shape CodeQL flags and the shape a reordering would reintroduce.
        app, _pl = _app()
        r = TestClient(app).get("/api/login", params={"token": VIEWER}, follow_redirects=False)
        self.assertIn(f"{COOKIE_NAME}={VIEWER};", r.headers["set-cookie"])

    def test_next_must_stay_on_this_server(self):
        app, _pl = _app()
        r = TestClient(app).get(
            "/api/login",
            params={"token": TOKEN, "next": "//evil.example"},
            follow_redirects=False,
        )
        self.assertEqual(r.headers["location"], "/perf")

    def test_next_honors_a_relative_path(self):
        app, _pl = _app()
        r = TestClient(app).get(
            "/api/login", params={"token": TOKEN, "next": "/scenes"}, follow_redirects=False
        )
        self.assertEqual(r.headers["location"], "/scenes")

    def test_post_reports_the_role(self):
        app, _pl = _app()
        client = TestClient(app)
        r = client.post("/api/login", json={"token": VIEWER})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "role": "viewer"})
        self.assertEqual(client.get("/status").status_code, 200)

    def test_post_rejects_a_bad_body(self):
        app, _pl = _app()
        client = TestClient(app)
        self.assertEqual(client.post("/api/login", content=b"not json").status_code, 401)
        self.assertEqual(client.post("/api/login", json={"token": 7}).status_code, 401)
        self.assertEqual(client.post("/api/login", json={}).status_code, 401)

    def test_post_refuses_an_oversized_body_rather_than_buffering_it(self):
        # This route is public, so an uncapped `request.json()` is a remote
        # memory exhaustion on an appliance with 1-2 GB — and the process it
        # takes down owns live hardware.
        from c64cast.control import auth

        app, _pl = _app()
        with mock.patch.object(auth, "MAX_BODY_BYTES", 64):
            r = TestClient(app).post("/api/login", content=b"x" * 512)
        self.assertEqual(r.status_code, 413)


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class LoginPageTest(unittest.TestCase):
    """A 401 answers a *browser* with somewhere to put the token.

    The daemon prints a URL with the token in it at startup, and that is the
    only entry point a phone gets — so a console whose cookie has gone needs a
    front door, not a line of plain text. Everything that is not a navigation
    keeps the plain-text 401 it had, because a `fetch` reading a login page as
    its error message is worse than useless."""

    def test_a_navigation_gets_a_form(self):
        app, _pl = _app()
        r = TestClient(app).get("/status", headers={"Accept": "text/html,*/*"})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.headers["content-type"].startswith("text/html"))
        self.assertIn('action="/api/login"', r.text)
        self.assertIn('name="token"', r.text)

    def test_the_form_returns_to_where_the_browser_was_going(self):
        app, _pl = _app()
        r = TestClient(app).get("/scenes", headers={"Accept": "text/html"})
        self.assertIn('name="next" value="/scenes"', r.text)

    def test_the_return_path_cannot_leave_this_server(self):
        # `next` is reflected into the page, so it goes through the same
        # `_safe_next` the redirect uses rather than a second, weaker check.
        app, _pl = _app()
        page = login_page("//evil.example")
        self.assertIn('value="/perf"', page)
        self.assertNotIn("evil.example", page)
        self.assertNotIn("<script", login_page('"><script>alert(1)</script>'))

    def test_a_fetch_still_gets_plain_text(self):
        app, _pl = _app()
        r = TestClient(app).get("/status", headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertEqual(r.text, "authentication required")

    def test_a_viewer_denied_a_write_does_not_get_a_login_page(self):
        # 403, not 401: the token is fine and re-entering it changes nothing.
        app, _pl = _app()
        r = TestClient(app).post(
            "/pause", headers={"Accept": "text/html", "X-C64Cast-Token": VIEWER}
        )
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class ViewerRoleTest(unittest.TestCase):
    def test_viewer_reads_but_cannot_write(self):
        app, pl = _app()
        client = TestClient(app)
        headers = {"X-C64Cast-Token": VIEWER}
        self.assertEqual(client.get("/status", headers=headers).status_code, 200)
        self.assertEqual(client.get("/scenes", headers=headers).status_code, 200)
        for path in ("/pause", "/skip", "/reload"):
            with self.subTest(path=path):
                self.assertEqual(client.post(path, headers=headers).status_code, 403)
        self.assertFalse(pl.pause_event.is_set())
        self.assertFalse(pl.skip_event.is_set())

    def test_viewer_cannot_launch_a_clip(self):
        app, pl = _app()
        r = TestClient(app).post(
            "/perf/command",
            json={"action": "launch", "slot": 1},
            headers={"X-C64Cast-Token": VIEWER},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(pl.performance.events, [])

    def test_state_carries_the_role(self):
        app, _pl = _app()
        client = TestClient(app)
        self.assertEqual(
            client.get("/perf/state", headers={"X-C64Cast-Token": VIEWER}).json()["role"], "viewer"
        )
        self.assertEqual(
            client.get("/perf/state", headers={"X-C64Cast-Token": TOKEN}).json()["role"], "full"
        )

    def test_role_is_null_when_the_server_has_no_token(self):
        app, _pl = _app(token="", viewer_token="")
        self.assertIsNone(TestClient(app).get("/perf/state").json()["role"])


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class WebSocketAuthTest(unittest.TestCase):
    """The reason this is middleware and not a dependency: a handler can only
    reject *after* accept(), which a browser cannot distinguish from a normal
    disconnect. A pre-accept close becomes a 403 on the handshake."""

    def test_unauthenticated_socket_is_refused(self):
        app, _pl = _app()
        # Raised by `websocket_connect` itself: the close landed before any
        # accept, which is what makes it a handshake failure rather than a
        # connection that opened and then dropped.
        with self.assertRaises(WebSocketDisconnect):
            with TestClient(app).websocket_connect("/perf/ws"):
                pass  # pragma: no cover - the connect above must raise

    def test_query_token_authenticates_a_socket(self):
        app, _pl = _app()
        with TestClient(app).websocket_connect(f"/perf/ws?token={TOKEN}") as ws:
            state = ws.receive_json()
        self.assertEqual(state["role"], "full")

    def test_viewer_socket_reads_but_its_commands_are_dropped(self):
        app, pl = _app()
        with TestClient(app).websocket_connect(f"/perf/ws?token={VIEWER}") as ws:
            first = ws.receive_json()
            ws.send_json({"action": "launch", "slot": 1})
            # The next push only happens after the frame above was handled, so
            # this read is the barrier that makes the assertion meaningful.
            ws.receive_json()
        self.assertEqual(first["role"], "viewer")
        self.assertEqual(pl.performance.events, [])

    def test_full_socket_command_is_applied(self):
        app, pl = _app()
        with TestClient(app).websocket_connect(f"/perf/ws?token={TOKEN}") as ws:
            ws.receive_json()
            ws.send_json({"action": "launch", "slot": 1})
            ws.receive_json()
        self.assertEqual(pl.performance.events, [(1, True)])


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class EveryRouteIsProtectedTest(unittest.TestCase):
    def test_no_route_outside_public_paths_answers_unauthenticated(self):
        from starlette.routing import WebSocketRoute

        app, _pl = _app()
        client = TestClient(app)
        checked = 0
        for route in app.routes:
            path = getattr(route, "path", None)
            if path is None or path in PUBLIC_PATHS or "{" in path:
                continue
            checked += 1
            with self.subTest(path=path):
                if isinstance(route, WebSocketRoute):
                    with self.assertRaises(WebSocketDisconnect):
                        with client.websocket_connect(path):
                            pass  # pragma: no cover - the connect must raise
                    continue
                for method in sorted(getattr(route, "methods", {"GET"})):
                    if method == "HEAD":
                        continue
                    self.assertEqual(client.request(method, path).status_code, 401)
        # Guard against the loop silently checking nothing (a routing change
        # that renames `path` would otherwise make this test vacuously pass).
        self.assertGreaterEqual(checked, 8)


class ViewerCredentialTest(unittest.TestCase):
    """The read-only token exists only once somebody asks for a link to share.

    Two properties matter. It is not minted at startup — a credential nobody
    asked for is one more thing that can leak. And once minted it is honored
    *immediately*: the app is built once, around a listening socket and every
    connected console, so "restart to use the token you just made" would make
    the feature useless at the moment it is wanted."""

    def test_nothing_exists_until_it_is_asked_for(self):
        from c64cast.control.auth import ViewerCredential

        cred = ViewerCredential()
        self.assertEqual(cred.token, "")
        self.assertFalse(cred)

    def test_issuing_mints_once_and_persists_it(self):
        from c64cast.control.auth import ViewerCredential

        kept: list[str] = []
        cred = ViewerCredential(store=kept.append)
        token, minted = cred.issue()
        self.assertTrue(minted)
        self.assertTrue(token)
        self.assertEqual(kept, [token])
        # Asking twice is one credential, not two.
        self.assertEqual(cred.issue(), (token, False))
        self.assertEqual(kept, [token])

    def test_a_configured_token_is_never_replaced(self):
        from c64cast.control.auth import ViewerCredential

        kept: list[str] = []
        cred = ViewerCredential("from-config", store=kept.append)
        self.assertEqual(cred.issue(), ("from-config", False))
        self.assertEqual(kept, [])

    def test_the_gate_honors_a_token_minted_after_the_app_was_built(self):
        from starlette.testclient import TestClient

        from c64cast.control.auth import ViewerCredential

        cred = ViewerCredential()
        app, _pl = _app(viewer_token=cred)
        client = TestClient(app)

        token, _ = cred.issue()
        # A read the viewer role is allowed…
        self.assertEqual(client.get("/status", params={"token": token}).status_code, 200)
        # …and a write it is not, on the same freshly-minted token.
        self.assertEqual(client.post("/pause", params={"token": token}).status_code, 403)

    def test_the_get_login_a_shared_link_uses_honors_it_too(self):
        # `GET`, not `POST`: the gate lets a viewer through read methods only,
        # so `POST /api/login` is a 403 for a viewer token however valid it is.
        # That is why the link handed out is the redirect form.
        from starlette.testclient import TestClient

        from c64cast.control.auth import COOKIE_NAME, ViewerCredential

        cred = ViewerCredential()
        app, _pl = _app(viewer_token=cred)
        client = TestClient(app)
        token, _ = cred.issue()
        answer = client.get(
            "/api/login", params={"token": token, "next": "/perf"}, follow_redirects=False
        )
        self.assertEqual(answer.status_code, 303)
        self.assertEqual(answer.headers["location"], "/perf")
        self.assertEqual(answer.cookies[COOKIE_NAME], token)


if __name__ == "__main__":
    unittest.main()
