"""Serving the built web console.

The console's sources live in ``web/`` and Vite compiles them into
``c64cast/web/dist/``, which is **committed** and shipped as package data, so a
``uv sync`` install with no Node still gets a console. :func:`mount_web_app`
registers three routes and must be registered **last**, after every API route,
because the third is a catch-all.

See docs/architecture/control.md#web_staticpy--the-consoles-built-ui-committed-and-served.
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

    Exact only when called after every other route is registered."""
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


def _asset_catalog(dist: Path) -> dict[str, tuple[Path, str]]:
    """The bundle's servable asset files, keyed by the name a request may ask
    for.

    Cataloged once rather than resolved per request: a request *names a key*
    and never contributes a path component, so there is no traversal to
    normalize and no containment check to get subtly wrong. A rebuild while
    the host is up therefore needs a restart."""
    assets = (dist / ASSETS_NAME).resolve()
    if not assets.is_dir():
        return {}
    catalog: dict[str, tuple[Path, str]] = {}
    for entry in sorted(assets.iterdir()):
        media_type = _CONTENT_TYPES.get(entry.suffix.lower())
        if media_type is not None and entry.is_file():
            catalog[entry.name] = (entry, media_type)
    return catalog


def shell_paths(directory: Path | None = None) -> tuple[str, ...]:
    """Every exact path a browser needs to load the console shell and nothing
    more — ``/`` and each of the bundle's assets — or ``()`` with no bundle.

    Read off the same catalog :func:`mount_web_app` serves from. The one
    caller is :func:`c64cast.app.serve.build_daemon_app`, handing them to
    ``TokenAuthMiddleware``'s exact-match ``public_paths`` during the appliance
    setup window (:mod:`c64cast.control.setup_gate`), where the shell has to
    load before any credential exists."""
    dist = bundle_dir(directory)
    if dist is None:
        return ()
    return ("/", *(f"/{ASSETS_NAME}/{name}" for name in _asset_catalog(dist)))


def landing_path(directory: Path | None = None) -> str:
    """Where a successful login should drop somebody: the console when its
    bundle was built, else the zero-dependency ``/perf`` page.

    One answer, shared by the URL the daemon prints at startup and the
    read-only link the console hands out — a shared link that landed somewhere
    else would be a second answer to the same question.

    ``directory`` must be the one :func:`mount_web_app` was given, or this
    answers for a bundle that is not the one being served."""
    return "/" if bundle_dir(directory) is not None else "/perf"


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
    catalog = _asset_catalog(dist)
    # The asset prefix too: an unbacked path under it is a broken bundle, not
    # a client route.
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
