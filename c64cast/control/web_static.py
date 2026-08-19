"""Serving the built web console.

The console's sources live in ``web/`` at the repo root and are compiled by
Vite into ``c64cast/web/dist/``, which is **committed** and shipped as package
data. That is the whole reason this module is three routes rather than a build
integration: a ``uv sync`` install has no Node, no ``npm``, and no network, and
the console still has to come up. Node is required to *change* the UI, never to
run it.

Registered last, after every API route, because the fallback is a catch-all.
FastAPI matches in registration order, so ``/api/session`` reaches its handler
and ``/anything-else`` reaches the app shell — which is what lets the client
grow routes later without a server change. What the catch-all refuses is read
off ``app.routes`` at mount time rather than listed: a mistyped
``/api/sessions`` answering ``200`` with a page of HTML is a worse failure than
a ``404`` — a ``fetch`` would parse it as success — and a hand-written list of
the paths to protect would be a second copy of the route table.

Assets are served by hand rather than by ``StaticFiles`` for one reason:
:mod:`vite.config.ts` gives them **fixed names** (``assets/app.js``), so a
browser that cached one across an upgrade would run the old console against the
new API. ``no-cache`` on every response makes each load revalidate, which on a
LAN costs a round trip and buys correctness. Content-hashed names would be the
other answer, and were rejected: they add a file to git on every rebuild and
leave the old one behind, which makes a committed artifact unreviewable.

Serving by hand means owning the traversal question, and the answer here is to
not have one: the bundle's files are **cataloged at mount time** and a request
looks its name up as a dictionary key. Nothing a client sends ever becomes a
path component, so there is no ``..`` to normalize and no containment check to
get subtly wrong — and no static analyzer has to be persuaded that the check
was correct.

Nothing here is behind its own auth check. The app is mounted onto an app the
token middleware already wraps, so the shell is gated exactly like the API it
talks to — a browser reaches the console by way of ``/api/login?token=…``,
which sets the cookie and redirects.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The compiled console, inside the package so it survives a wheel.
DIST_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"

INDEX_NAME = "index.html"
ASSETS_NAME = "assets"

#: Only what Vite emits. An allowlist rather than a MIME guess so a file that
#: somehow lands in the bundle directory can't be served as something the
#: browser will execute in a context we didn't intend.
_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".ico": "image/vnd.microsoft.icon",
}


def owned_segments(app: Any) -> frozenset[str]:
    """The first path segment of every route already on ``app``.

    Read off the app rather than listed here, because a list would be a second
    copy of the route table: every path the server answers is registered before
    the console is mounted, so this is exact by construction and a route added
    to :mod:`web_api` tomorrow is covered without anybody remembering."""
    segments = set()
    for route in getattr(app, "routes", []):
        path = str(getattr(route, "path", ""))
        head = path.lstrip("/").split("/", 1)[0]
        if head:
            segments.add(head)
    return frozenset(segments)


def bundle_dir(directory: Path | None = None) -> Path | None:
    """The directory holding a usable console build, or ``None``.

    "Usable" means the entry point is actually there — a half-populated
    ``dist/`` (an interrupted build, a checkout with the tree but not the
    files) should read as absent rather than serve a blank page."""
    base = DIST_DIR if directory is None else Path(directory)
    return base if (base / INDEX_NAME).is_file() else None


def landing_path() -> str:
    """Where a successful login should drop somebody: the console when its
    bundle was built, else the zero-dependency ``/perf`` page.

    One answer, shared by the URL the daemon prints at startup and the
    read-only link the console hands out — a shared link that landed somewhere
    else would be a second answer to the same question."""
    return "/" if bundle_dir() is not None else "/perf"


def mount_web_app(app: Any, *, directory: Path | None = None) -> bool:
    """Serve the console from ``app``. Returns whether a build was found.

    A missing bundle is not an error: running ``--serve`` from a checkout that
    has never run ``make web`` still gets the API and the ``/perf`` fallback
    console, which is the whole reason that page was kept."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, Response

    dist = bundle_dir(directory)
    if dist is None:
        log.info(
            "web console: no built UI at %s — serving the API and /perf only "
            "(run `make web` in a checkout to build it)",
            DIST_DIR if directory is None else directory,
        )
        return False

    index = dist / INDEX_NAME
    assets = (dist / ASSETS_NAME).resolve()
    # Cataloged once at mount rather than resolved per request, so a request
    # *names a key* and never contributes a path component: there is no
    # traversal question to answer, and no `is_relative_to` check standing
    # between a user string and the filesystem. The bundle is a handful of
    # files with fixed names, so the map is cheap and complete. A rebuild while
    # the host is up therefore needs a restart — which is what `npm run dev`
    # is for, and not something a deployment does.
    catalog: dict[str, tuple[Path, str]] = {}
    if assets.is_dir():
        for entry in sorted(assets.iterdir()):
            media_type = _CONTENT_TYPES.get(entry.suffix.lower())
            if media_type is not None and entry.is_file():
                catalog[entry.name] = (entry, media_type)
    # Plus the asset prefix itself: a path under it that no file backs is a
    # broken bundle, not a client route, and answering it with the shell would
    # hide that behind a page that loads and does nothing.
    reserved = owned_segments(app) | {ASSETS_NAME}

    def _no_cache(path: Path, media_type: str) -> Response:
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get(f"/{ASSETS_NAME}/{{name}}")
    def web_asset(name: str) -> Response:
        entry = catalog.get(name)
        if entry is None:
            raise HTTPException(404, "no such asset")
        return _no_cache(*entry)

    @app.get("/")
    def web_index() -> Response:
        return _no_cache(index, "text/html; charset=utf-8")

    @app.get("/{path:path}")
    def web_fallback(path: str) -> Response:
        if path.lstrip("/").split("/", 1)[0] in reserved:
            raise HTTPException(404, "not found")
        return _no_cache(index, "text/html; charset=utf-8")

    log.info("web console: serving the UI from %s", dist)
    return True
