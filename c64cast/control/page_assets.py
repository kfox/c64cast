"""Packaged assets for the two hand-written control pages.

The `/perf` console (`control/perf_console.html`) and the WLED bridge's device
page (`wled/wled_index.html`) are each one self-contained HTML document: no
second request, no third-party resource, nothing from a CDN. That is what lets
`perf_console._PAGE_HEADERS` be as strict as it is, and what makes both pages
work on a phone that can reach the show host and nothing else.

Sharing code between them therefore cannot mean serving a `.js` file. It means
splicing one at render time, which is what :func:`with_live_socket` does —
`control/live_socket.js`, the reconnecting-socket-with-poll-fallback both pages
need, lands in place of a marker comment.

Everything here reads through :mod:`importlib.resources` rather than
``__file__``, so a zipped distribution answers too (``read_text`` needs no real
filesystem path), and everything is cached: the same bytes go out on every
request.
"""

from __future__ import annotations

import functools

#: Marker a page puts inside its own ``<script>`` block, on its own line, where
#: the shared client should land.
LIVE_SOCKET_MARKER = "//@include live_socket.js"


def package_text(package: str, name: str) -> str:
    """A packaged text asset's contents.

    Both trees need their assets listed in ``[tool.setuptools.package-data]``
    (``control/*.html``, ``control/*.js``, ``wled/*.html``) — without that the
    wheel ships only ``.py`` files and the page 500s on a fresh install, which
    is why `test_perf_console` and `test_wled_device` each read theirs.
    """
    from importlib.resources import files  # noqa: PLC0415  (lazy; import-time cost)

    return files(package).joinpath(name).read_text(encoding="utf-8")


def with_live_socket(html: str) -> str:
    """`html` with :data:`LIVE_SOCKET_MARKER` replaced by `live_socket.js`.

    Raises if the marker is absent rather than serving a page whose
    `liveSocket` is undefined: the page would render, every control would look
    live, and only the state pushes would be missing — the failure mode hardest
    to notice from a phone at the back of a room.
    """
    if LIVE_SOCKET_MARKER not in html:
        raise ValueError(f"page is missing the {LIVE_SOCKET_MARKER!r} marker")
    return html.replace(LIVE_SOCKET_MARKER, package_text("c64cast.control", "live_socket.js"))


@functools.cache
def page_html(package: str, name: str) -> str:
    """A packaged control page, assembled once — the same bytes go out on
    every request, so neither the read nor the splice repeats."""
    return with_live_socket(package_text(package, name))
