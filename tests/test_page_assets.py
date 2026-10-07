"""Tests for c64cast.control.page_assets — the shared control-page assets.

The two hand-written control pages (`/perf` and the WLED device page) each
carried their own copy of the reconnecting-socket-with-poll-fallback client,
and the copies drifted: different reconnect delays, and for a while no backoff
on either. What is checked here is that there is now one copy, that both pages
actually receive it, and that neither has quietly grown its own again.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from html.parser import HTMLParser

from _child_process import run_bounded

from c64cast.control import page_assets
from c64cast.control.perf_console import perf_page_html
from c64cast.wled.wled_device import index_page_html

# Every page the splice serves, as (name, renderer, package, source file).
PAGES = (
    ("perf console", perf_page_html, "c64cast.control", "perf_console.html"),
    ("wled device", index_page_html, "c64cast.wled", "wled_index.html"),
)

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
        # A page whose liveSocket is undefined still renders with every
        # control looking live; only the state pushes go missing.
        with self.assertRaises(ValueError):
            page_assets.with_live_socket("<!doctype html><html></html>")

    def test_only_the_shared_client_opens_a_socket(self):
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


# Compiles each script in the JSON manifest named by argv[1] and prints one
# result per script, in order: null when it parses, else the error's
# `filename:line`, the offending line, a caret and the message. A classic
# `vm.Script` rather than a CommonJS module, because a browser runs an inline
# `<script>` as a classic script.
_PARSE_ALL_JS = r"""
const fs = require('fs');
const vm = require('vm');
const scripts = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const results = scripts.map(({filename, source}) => {
  try {
    new vm.Script(source, {filename});
    return null;
  } catch (err) {
    return String(err && err.stack || err).split('\n    at ')[0];
  }
});
process.stdout.write(JSON.stringify(results));
"""


class ScriptSyntaxTest(unittest.TestCase):
    """The splice is a textual replace into each page's *existing* `<script>`
    scope, so a top-level name the shared client defines colliding with one the
    page defines is a whole-script SyntaxError — every control on the page goes
    dead, not just the socket, and the page still renders looking live. Nothing
    else in CI parses these pages.

    Every script goes to one `node` process rather than one each. The first
    `node` launch on a fresh Windows runner can outlast the 20 s child bound
    (#616), so the test starts as few as it can.
    """

    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node not on PATH")

    def test_every_rendered_page_parses(self):
        scripts = []
        for name, render, _, _ in PAGES:
            with self.subTest(page=name):
                bodies = _inline_scripts(render())
                self.assertTrue(bodies, "page serves no inline script")
                scripts += [
                    {"page": name, "filename": f"{name} script {i}", "source": body}
                    for i, body in enumerate(bodies)
                ]

        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "scripts.json")
            with open(manifest, "w", encoding="utf-8") as fh:
                json.dump(scripts, fh)
            proc = run_bounded(
                ["node", "-e", _PARSE_ALL_JS, manifest],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        errors = json.loads(proc.stdout)
        self.assertEqual(len(errors), len(scripts), proc.stdout)

        for script, error in zip(scripts, errors, strict=True):
            with self.subTest(page=script["page"], script=script["filename"]):
                if error is not None:
                    self.fail(error)


if __name__ == "__main__":
    unittest.main()
