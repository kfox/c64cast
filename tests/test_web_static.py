"""Tests for serving the built web console.

Three things worth pinning, and they are the three ways this can go wrong
silently. **A missing bundle must not be an error** — `--serve` from a checkout
that never ran `make web` still has to come up with the API and the `/perf`
fallback. **The catch-all must not swallow the API** — its whole job is to
answer unknown paths with the app shell, and a mistyped `/api/...` coming back
as `200 text/html` would be parsed by a `fetch` as success. And **the committed
bundle has to match what the server expects to find**: the filenames are fixed
by `web/vite.config.ts` rather than content-hashed, so a change there that
nothing checks would ship a console that 404s its own script.

Nothing here mounts the real bundle onto a real app: the fixtures are three
bytes of text in a temp directory, which is enough to exercise every routing
decision and keeps the suite indifferent to whether Node has ever run.

Not covered here, and left to the `hw-visual-verify` skill: a browser actually
executing the bundle, and the login cookie surviving a navigation into it."""

# pyright: reportOptionalCall=false
from __future__ import annotations

import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

    HAVE_TESTCLIENT = True
except (ImportError, RuntimeError):
    HAVE_TESTCLIENT = False
    FastAPI = None  # type: ignore[misc,assignment]
    TestClient = None  # type: ignore[misc,assignment]

from c64cast.control import web_static

INDEX_BODY = "<!doctype html><title>c64cast</title><div id=app></div>"
SCRIPT_BODY = "console.log('hello')"


def _bundle(root: Path) -> Path:
    """The smallest thing `bundle_dir` will call a build."""
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(INDEX_BODY, encoding="utf-8")
    (root / "assets" / "app.js").write_text(SCRIPT_BODY, encoding="utf-8")
    (root / "assets" / "app.css").write_text("body{}", encoding="utf-8")
    return root


class BundleDiscoveryTest(unittest.TestCase):
    def test_a_directory_with_an_index_is_a_bundle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _bundle(Path(tmp))
            self.assertEqual(web_static.bundle_dir(root), root)

    def test_a_missing_directory_is_not(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(web_static.bundle_dir(Path(tmp) / "never-built"))

    def test_a_directory_without_an_index_is_not(self) -> None:
        # An interrupted build leaves the tree but not the entry point, and
        # serving that is a blank page rather than an honest "no console here".
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "assets").mkdir()
            self.assertIsNone(web_static.bundle_dir(Path(tmp)))


@unittest.skipUnless(HAVE_TESTCLIENT, "fastapi.testclient (httpx) not installed")
class MountTest(unittest.TestCase):
    """Everything below builds a bare app with one stand-in API route, so the
    reserved-prefix logic is exercised against a real route table."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dist = _bundle(Path(self._tmp.name))
        self.app: Any = FastAPI()

        @self.app.get("/api/session")
        def _session() -> dict[str, str]:
            return {"state": "idle"}

        @self.app.get("/status")
        def _status() -> dict[str, str]:
            return {"ok": "yes"}

    def _client(self) -> Any:
        self.assertTrue(web_static.mount_web_app(self.app, directory=self.dist))
        return TestClient(self.app)

    def test_a_missing_bundle_mounts_nothing_and_says_so(self) -> None:
        missing = Path(self._tmp.name) / "never-built"
        with self.assertLogs("c64cast.control.web_static", level="INFO"):
            self.assertFalse(web_static.mount_web_app(self.app, directory=missing))
        # The API is untouched, and an unknown path is still an honest 404
        # rather than a page that never loads.
        client = TestClient(self.app)
        self.assertEqual(client.get("/api/session").status_code, 200)
        self.assertEqual(client.get("/").status_code, 404)

    def test_the_root_serves_the_shell(self) -> None:
        r = self._client().get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, INDEX_BODY)
        self.assertTrue(r.headers["content-type"].startswith("text/html"))

    def test_assets_are_served_with_their_own_content_type(self) -> None:
        r = self._client().get("/assets/app.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, SCRIPT_BODY)
        self.assertTrue(r.headers["content-type"].startswith("text/javascript"))
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")

    def test_nothing_is_cached(self) -> None:
        # The filenames are fixed rather than content-hashed, so a cached copy
        # would survive an upgrade and run yesterday's console against today's
        # API. See web_static's module docstring for why the trade goes this way.
        client = self._client()
        for path in ("/", "/assets/app.js", "/assets/app.css"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).headers["cache-control"], "no-cache")

    def test_an_unknown_asset_is_a_404(self) -> None:
        self.assertEqual(self._client().get("/assets/nope.js").status_code, 404)

    def test_an_unexpected_suffix_is_not_served(self) -> None:
        (self.dist / "assets" / "secrets.txt").write_text("hunter2", encoding="utf-8")
        self.assertEqual(self._client().get("/assets/secrets.txt").status_code, 404)

    def test_an_asset_cannot_escape_the_bundle(self) -> None:
        # A client collapses a literal `..` in a URL before sending it, so the
        # form that actually reaches the route is percent-encoded.
        outside = Path(self._tmp.name).parent / "outside.js"
        outside.write_text("nope", encoding="utf-8")
        self.addCleanup(outside.unlink)
        r = self._client().get(f"/assets/%2e%2e/%2e%2e/{outside.name}")
        self.assertEqual(r.status_code, 404)

    def test_an_unknown_path_falls_back_to_the_shell(self) -> None:
        # What lets the client grow routes without a server change.
        r = self._client().get("/configs/shows/gig.toml")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, INDEX_BODY)

    def test_the_fallback_never_answers_for_a_route_the_server_owns(self) -> None:
        client = self._client()
        for path in ("/api/nope", "/api/session/start", "/status/nope", "/assets/sub/app.js"):
            with self.subTest(path=path):
                r = client.get(path)
                self.assertEqual(r.status_code, 404, f"{path} was answered with the app shell")

    def test_the_api_still_wins(self) -> None:
        self.assertEqual(self._client().get("/api/session").json(), {"state": "idle"})


class OwnedSegmentsTest(unittest.TestCase):
    def test_segments_are_read_off_the_app(self) -> None:
        # Read rather than listed: a hand-written list of paths to protect is a
        # second copy of the route table, and it is the copy that goes stale.
        class _Route:
            def __init__(self, path: str) -> None:
                self.path = path

        class _App:
            routes = [_Route("/api/session"), _Route("/perf/ws"), _Route("/status"), _Route("/")]

        self.assertEqual(web_static.owned_segments(_App()), frozenset({"api", "perf", "status"}))


class CommittedBundleTest(unittest.TestCase):
    """The bundle under `c64cast/web/dist` is build output that is committed on
    purpose, so that installing c64cast never needs Node. These assertions are
    what stop `web/vite.config.ts` and this module from drifting apart — CI
    additionally rebuilds it and fails on a diff."""

    def test_the_console_is_committed(self) -> None:
        self.assertIsNotNone(
            web_static.bundle_dir(),
            f"no built console at {web_static.DIST_DIR} — run `make web`",
        )

    def test_the_shell_references_the_fixed_asset_names(self) -> None:
        dist = web_static.bundle_dir()
        assert dist is not None
        index = (dist / web_static.INDEX_NAME).read_text(encoding="utf-8")
        for name in ("/assets/app.js", "/assets/app.css"):
            with self.subTest(name=name):
                self.assertIn(
                    name,
                    index,
                    "the build emitted a filename the server does not expect — "
                    "content-hashed names would 404 and make every rebuild a new file",
                )
                self.assertTrue((dist / name.lstrip("/")).is_file())


if __name__ == "__main__":
    unittest.main()
