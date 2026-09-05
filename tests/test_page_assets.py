"""Tests for c64cast.control.page_assets — the shared control-page assets.

The two hand-written control pages (`/perf` and the WLED device page) each
carried their own copy of the reconnecting-socket-with-poll-fallback client,
and the copies drifted: different reconnect delays, and for a while no backoff
on either. What is checked here is that there is now one copy, that both pages
actually receive it, and that neither has quietly grown its own again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser

from c64cast.control import page_assets
from c64cast.control.perf_console import perf_page_html
from c64cast.wled.wled_device import index_page_html

# Every page the splice serves, as (name, renderer, package, source file).
PAGES = (
    ("perf console", perf_page_html, "c64cast.control", "perf_console.html"),
    ("wled device", index_page_html, "c64cast.wled", "wled_index.html"),
)

# Every hand-written source that could open a socket of its own.
SOCKET_SOURCES = (
    ("c64cast.control", "live_socket.js"),
    ("c64cast.control", "perf_console.html"),
    ("c64cast.wled", "wled_index.html"),
)


class PackagedAssetTest(unittest.TestCase):
    """Each asset needs a `[tool.setuptools.package-data]` entry or the wheel
    ships only .py files and the page 500s on a fresh install. Reading them is
    what notices."""

    def test_the_shared_client_ships(self):
        js = page_assets.package_text("c64cast.control", "live_socket.js")
        self.assertIn("function liveSocket(", js)

    def test_every_page_source_ships(self):
        for name, _, package, filename in PAGES:
            with self.subTest(page=name):
                body = page_assets.package_text(package, filename)
                self.assertIn("<!doctype html>", body)


class SpliceTest(unittest.TestCase):
    def test_every_page_asks_for_the_client_exactly_once(self):
        for name, _, package, filename in PAGES:
            with self.subTest(page=name):
                body = page_assets.package_text(package, filename)
                self.assertEqual(body.count(page_assets.LIVE_SOCKET_MARKER), 1)

    def test_every_rendered_page_carries_the_client_and_no_marker(self):
        for name, render, _, _ in PAGES:
            with self.subTest(page=name):
                page = render()
                self.assertIn("function liveSocket(", page)
                self.assertNotIn(page_assets.LIVE_SOCKET_MARKER, page)

    def test_a_page_without_the_marker_is_refused(self):
        # Rather than serving a page whose liveSocket is undefined: it would
        # render, every control would look live, and only the state pushes
        # would be missing — the hardest failure to spot from a phone.
        with self.assertRaises(ValueError):
            page_assets.with_live_socket("<!doctype html><html></html>")

    def test_only_the_shared_client_opens_a_socket(self):
        # The property, not the two identifier names the deleted copies
        # happened to use: those exist nowhere now, so asserting their absence
        # could only ever catch a reintroduction spelled exactly the old way.
        # A third page, or a copy named `connectWS`, fails this one.
        for package, filename in SOCKET_SOURCES:
            with self.subTest(source=filename):
                body = page_assets.package_text(package, filename)
                expected = 1 if filename == "live_socket.js" else 0
                self.assertEqual(body.count("new WebSocket("), expected)


class WiringTest(unittest.TestCase):
    """The shared client is parameterized, so each page has to hand it the
    right socket path and fallback endpoint."""

    def test_each_page_names_its_own_socket(self):
        self.assertIn("path: '/perf/ws'", perf_page_html())
        self.assertIn("path: '/ws'", index_page_html())

    def test_the_backoff_bound_reaches_both_pages(self):
        for name, render, _, _ in PAGES:
            with self.subTest(page=name):
                self.assertIn("WS_RETRY_MAX_MS", render())


class _InlineScripts(HTMLParser):
    """Collects the bodies of inline (non-`src`) ``<script>`` elements.

    A parser rather than a regexp. An HTML end tag may carry junk the parser
    ignores — `</script >`, `</script foo>` — and every regexp written to cover
    that is one CodeQL finds another hole in (py/bad-tag-filter, twice). The
    stdlib already knows the grammar, and it switches to CDATA mode inside
    `<script>` on its own, so the body arrives in one piece.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.bodies: list[str] = []
        self._inline = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._inline = not any(name == "src" for name, _ in attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inline = False

    def handle_data(self, data: str) -> None:
        if self._inline:
            self.bodies.append(data)


def _inline_scripts(html: str) -> list[str]:
    parser = _InlineScripts()
    parser.feed(html)
    parser.close()
    return [body for body in parser.bodies if body.strip()]


class ScriptSyntaxTest(unittest.TestCase):
    """The splice is a textual replace into each page's *existing* `<script>`
    scope, so a top-level name the shared client defines colliding with one the
    page defines is a whole-script SyntaxError — every control on the page goes
    dead, not just the socket, and the page still renders looking live. Nothing
    else in CI parses these pages."""

    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node not on PATH")

    def test_every_rendered_page_parses(self):
        for name, render, _, _ in PAGES:
            with self.subTest(page=name):
                bodies = _inline_scripts(render())
                self.assertTrue(bodies, "page serves no inline script")
                with tempfile.TemporaryDirectory() as tmp:
                    for i, body in enumerate(bodies):
                        path = os.path.join(tmp, f"{i}.js")
                        with open(path, "w", encoding="utf-8") as fh:
                            fh.write(body)
                        proc = subprocess.run(
                            ["node", "--check", path],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
