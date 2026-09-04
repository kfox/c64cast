"""Tests for c64cast.control.page_assets — the shared control-page assets.

The two hand-written control pages (`/perf` and the WLED device page) each
carried their own copy of the reconnecting-socket-with-poll-fallback client,
and the copies drifted: different reconnect delays, and for a while no backoff
on either. What is checked here is that there is now one copy, that both pages
actually receive it, and that neither has quietly grown its own again.
"""

from __future__ import annotations

import unittest

from c64cast.control import page_assets
from c64cast.control.perf_console import perf_page_html
from c64cast.wled.wled_device import index_page_html

# Every page the splice serves, as (name, renderer, package, source file).
PAGES = (
    ("perf console", perf_page_html, "c64cast.control", "perf_console.html"),
    ("wled device", index_page_html, "c64cast.wled", "wled_index.html"),
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

    def test_neither_page_keeps_a_client_of_its_own(self):
        # The duplication this replaced. Both copies defined these.
        for name, render, _, _ in PAGES:
            with self.subTest(page=name):
                page = render()
                self.assertNotIn("function startWS()", page)
                self.assertNotIn("function retryWS()", page)


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


if __name__ == "__main__":
    unittest.main()
