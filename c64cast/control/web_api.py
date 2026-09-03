"""The web console's HTTP + WebSocket API (`--serve`).

The control plane answers questions about a session that already exists —
pause it, skip a scene, reload its config. These routes are the layer above:
they *create* and destroy sessions, so the server can outlive any show and a
browser can drive a machine that is currently doing nothing at all. They are
registered onto the same FastAPI app as `/status` and `/perf`, behind the same
token gate, because the whole point of a host console is one address.

Three shapes of answer, and which one a route gives is the design:

* **202 Accepted** for ``start`` / ``stop`` / ``switch``. Building a session
  blocks for many seconds on hardware; the supervisor claims the transition and
  returns, and the caller watches ``/api/ws`` for what happened. A route that
  waited would hold a request open across a machine reset.
* **409 Conflict** when the supervisor is busy — mapped from
  :class:`~c64cast.app.serve.SupervisorBusy`, never from a state check here.
  Reading the state and then acting on it is a race with the reap poller; the
  supervisor's own lock is the only place that decision can be made.
* **422** when the config doesn't validate. This is the payoff for
  :func:`~c64cast.app.session.validate_configs` being pure and hardware-free:
  the request that would have failed twenty seconds into a build fails
  immediately instead, with the supervisor still idle.

The state feed is the ``/perf`` payload with a ``session`` key added, so a
console that already renders the performance surface gains the supervisor for
free. Log lines ride along by sequence number rather than as a re-sent tail:
the push cadence is ~3/sec and re-sending 500 lines each time would dwarf
everything else on the socket.

``/api/configs`` is the other half: browse, read and edit the host's configs, so
a show can be authored and then started without a shell. Two ways to save, and
the split matters: ``PUT`` takes the text a client composed (the raw editor),
while ``PATCH`` takes named field edits and lets the server compose the text
through the config dataclasses (the generated form). Every path goes through
:class:`~c64cast.app.config_store.ConfigStore`, which is also what turns the
``config`` a start request may name into something safe to hand the loader —
the jail is not repeated here, because a second copy of it is a second thing to
get wrong.

Reading **one** config (``GET /api/configs/{ref}``) is the single route here
that needs more authorization than its verb carries: the store hands back the
file's raw text, secrets included, so it calls
:func:`~c64cast.control.auth.require_full` and a ``viewer`` gets a ``403``. The
index, the media listing and the screen stay readable by a viewer, which is
what a read-only link is for.

``/api/media`` is the other half: what a `file =` field could point at, and
where a dropped or picked file lands, from
:class:`~c64cast.app.media_store.MediaStore`. ``GET`` browses; ``PUT
/api/media/{name}`` uploads, streamed straight to disk rather than buffered
into memory (see the module's own docstring for why) and refused rather than
allowed to overwrite anything already there.

``/api/session/live-tune`` is where those two halves meet. A one-shot run asks
"save these knob changes?" at exit; a daemon has no exit and no terminal, and a
host that rewrote every show file it stopped would be unusable — so under
``--serve`` the tracker records and nothing acts on it. This route is what acts
on it, on a tap rather than on a shutdown. It is a config write and lives here
rather than on the performance socket for that reason: it takes the store's
refusals, its 422 report and its backup sibling, and a socket frame has nowhere
to put a status code.

Like :mod:`perf_console` and :mod:`auth`, this module deliberately does **not**
``from __future__ import annotations``: the WebSocket route annotates its
parameter with a name imported inside :func:`register_web_routes`, and
stringized annotations would make FastAPI mis-read it as a query parameter and
skip the injection entirely.
"""

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Generator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote

from c64cast.app import introspect, paths
from c64cast.app.config import Config, ConfigError
from c64cast.app.config_store import (
    ConfigInvalid,
    ConfigNotFound,
    ConfigStore,
    ConfigStoreError,
    ConfigTooLarge,
    EditRejected,
    PathRejected,
)
from c64cast.app.console_library import ConsoleLibrary
from c64cast.app.media_store import (
    MediaKindUnknown,
    MediaNameRejected,
    MediaNotUploadable,
    MediaStore,
    MediaStoreError,
    MediaTooLarge,
)
from c64cast.app.playlist import Playlist
from c64cast.app.serve import STARTABLE, SessionManager, SessionStatus, StartRequest, SupervisorBusy
from c64cast.app.session import SessionConfigError

from . import screen as screen_mod
from .auth import (
    LOGIN_PATH,
    SCOPE_ROLE_KEY,
    BodyTooLarge,
    ViewerCredential,
    is_viewer,
    read_body,
    require_full,
)
from .screen import ScreenFeed, ScreenUnavailable, multipart_frames
from .transport import COLOR_FIELD_NAMES, write_live_tune_row
from .web_static import landing_path

log = logging.getLogger(__name__)

#: How often the state feed pushes. Matches the `/perf` console's cadence —
#: the same beat grid is in the payload and the same local extrapolation runs
#: against it.
_PUSH_INTERVAL_S = 0.35

#: Lines of session log a fresh connection is handed before it starts
#: following along by sequence number.
_LOG_BACKLOG = 200

#: How many `GET /api/screen/stream` responses may be open at once, and the
#: size of the pool that drives them. A constant rather than a `[web]` field
#: because the number a host can afford is a property of this design (one
#: thread held continuously per watcher), not of a deployment: four browsers
#: watching one C64 is already an unusual show, and the refusal past it is a
#: 503 a console can render.
MAX_SCREEN_WATCHERS = 4

#: A request factory re-reads the config from disk and validates it, so a
#: start picks up an edit made since the host launched. Its argument is the
#: config path to run, or None for the one the host was launched with. It
#: raises ConfigError / SessionConfigError, which is what a 422 is built from.
RequestFactory = Callable[[str | None], StartRequest]
PlaylistRegistry = Callable[[], Mapping[str, Playlist]]

#: How a refusal from the config store reaches the caller. The store is
#: app-level and says nothing about HTTP; the mapping lives here so it doesn't
#: have to.
_STORE_STATUS: tuple[tuple[type[ConfigStoreError], int], ...] = (
    (ConfigInvalid, 422),
    (ConfigNotFound, 404),
    (ConfigTooLarge, 413),
    (PathRejected, 403),
    (EditRejected, 400),
)

#: Same idea as `_STORE_STATUS`, for `MediaStore`'s own refusals — kept apart
#: because the two stores don't share a common exception base, and a mapping
#: table is cheaper than a second `isinstance` chain hand-written into a route.
_MEDIA_STATUS: tuple[tuple[type[MediaStoreError], int], ...] = (
    (MediaKindUnknown, 400),
    (MediaNameRejected, 400),
    (MediaNotUploadable, 403),
    (MediaTooLarge, 413),
)


def _status_payload(status: SessionStatus, log_buffer: Any, store: ConfigStore) -> dict[str, Any]:
    out = status.as_dict()
    out["log_seq"] = log_buffer.seq if log_buffer is not None else 0
    # The browser's default selection: the config the host was launched with,
    # named the way the config browser names everything else. None when that
    # path isn't under a root this store knows (a quick-playback run, or one
    # started from outside any configured `config_roots`).
    out["config_ref"] = store.ref_for(Path(out["config_path"])) if out.get("config_path") else None
    return out


def _live_tune_edit(row: Mapping[str, Any]) -> dict[str, Any]:
    """One :meth:`LiveTuneTracker.pending` row as a :meth:`ConfigStore.patch`
    edit. ``scene`` is the row's own answer to where the value lives: an index
    for a knob a scene owns, None for one the whole show shares. A color field
    on an overriding scene additionally carries ``subsection: "color"``, which
    is what routes the edit into that scene's ``[scenes.color]`` dict instead
    of a plain scene attribute (see ``_apply_edit`` in config_store.py)."""
    if row["scene"] is None:
        where: dict[str, Any] = {"section": "color"}
    elif row["field"] in COLOR_FIELD_NAMES:
        where = {"scene": row["scene"], "subsection": "color"}
    else:
        where = {"scene": row["scene"]}
    return {**where, "field": row["field"], "value": row["new"]}


async def _until_gone(
    frames: Generator[bytes], request: Any, pool: ThreadPoolExecutor
) -> AsyncIterator[bytes]:
    """Drive a blocking frame generator from the event loop, and stop when the
    client does.

    Two problems, one adapter. The generator sleeps and encodes, so running it
    on the loop would stall every other request; and `is_disconnected` is a
    coroutine, so the generator cannot ask it. Pulling each part in a worker
    thread and checking between parts solves both. Without the check, a closed
    tab would leave a thread encoding frames nobody reads.

    ``pool`` is that worker thread's home and must **not** be the default
    executor: the generator's fps `sleep` happens inside `next()`, so a thread
    is held for the whole frame period rather than for the encode, and the
    default executor is shared with `api_media_upload`'s chunk writes and every
    sync route in this module. See :class:`StreamSlots`, which owns it.

    Nothing here closes the generator, which is the correction to the first
    version of this: a disconnect cancels this coroutine *while the worker
    thread is inside* `next()`, and closing a running generator raises
    `ValueError: generator already executing` — so the `finally` that was
    supposed to release the machine's stream never completed. The stream's
    lifetime belongs to the response instead (`ScreenFeed.release` as a
    background task); an abandoned generator is suspended at a yield, holds
    nothing, and is collected."""
    loop = asyncio.get_running_loop()
    while not await request.is_disconnected():
        part = await loop.run_in_executor(pool, lambda: next(frames, None))
        if part is None:
            return
        yield part


class StreamSlots:
    """How many screen streams may be open at once, and the pool that drives
    them.

    Both halves belong together because they are the same number: ``_until_gone``
    holds one worker thread per open stream essentially continuously (the fps
    `sleep` happens inside the generator's `next()`, not around it), so a pool
    of ``limit`` threads and a cap of ``limit`` watchers is one decision.

    The pool is dedicated on purpose. `run_in_executor(None, …)` would put the
    streams on the default executor — ``min(32, cpu + 4)`` threads, 8 on a
    four-core appliance — which is also where `api_media_upload`'s chunk writes
    and *every* sync route in this module run. A dozen `GET
    /api/screen/stream` requests from one read-only credential used to starve
    all of it, with a healthy process and an empty log.

    :meth:`claim` refuses rather than queues: a queued stream would connect
    and then show nothing."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.pool = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="c64cast-screen")
        self._slots = threading.BoundedSemaphore(limit)

    def claim(self) -> bool:
        """Take a slot, or ``False`` when every one is in use."""
        return self._slots.acquire(blocking=False)

    def release(self) -> None:
        """Give a claimed slot back — from the response's own background task,
        so it happens however the body ended."""
        self._slots.release()


def _opt_index(value: Any, name: str) -> int | None:
    """A scene index from a request body, or None.

    ``EditRejected`` rather than an ``HTTPException`` so this stays sayable
    without FastAPI in scope; ``_store_error`` maps it to the 400 it deserves.
    ``bool`` is rejected because it is an ``int`` in Python, and
    ``{"copy": true}`` would otherwise read as scene 1."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise EditRejected(f"`{name}` is a scene index, got {value!r}")
    return value


def _restamp(cfg: Config, rows: Sequence[Mapping[str, Any]]) -> None:
    """Put what the file just took back onto the running Config, so the C64
    menu's own whole-config save-back can't quietly revert it.

    A scene index the config has no block for is skipped rather than clamped —
    it can only mean the file changed shape under the run, and writing the value
    into whichever scene inherited the index would be worse than not writing it.
    See :func:`write_live_tune_row` for where each row actually lands (a color
    field on an overriding scene goes into its ``[scenes.color]`` dict, not
    onto the scene itself)."""
    for row in rows:
        write_live_tune_row(cfg, row)


def register_web_routes(
    app: Any,
    *,
    manager: SessionManager,
    request_factory: RequestFactory,
    playlists: PlaylistRegistry,
    store: ConfigStore,
    library: ConsoleLibrary,
    media: MediaStore,
    log_buffer: Any = None,
    viewer: ViewerCredential | None = None,
    screen_fps: float = 10.0,
) -> None:
    """Register the ``/api/*`` routes on an existing FastAPI ``app``.

    Called by :func:`c64cast.app.serve.run_daemon` after the control-plane
    routes and the auth middleware are already on the app, so everything here
    is gated by the same token — a route added to this module can't ship
    unauthenticated by omission.

    ``library`` and ``media`` are **required**, unlike the two optional
    injections below, and the asymmetry is the point: their constructors resolve
    into the data dir and write there, so a ``None`` default meant a caller who
    forgot one got a component quietly writing under
    ``~/.local/share/c64cast`` instead of a ``TypeError`` — the exact footgun
    this project's "a test never writes outside a temp dir" rule exists to
    prevent, and invisible at the call site.

    The two that stay optional both mean **absent**, not "use a real default".
    ``viewer`` is the same :class:`~c64cast.control.auth.ViewerCredential` the
    gate holds, so a token issued by ``/api/viewer-link`` is accepted by the
    next request without a restart; ``None`` leaves the route registered and
    answering ``501``, which keeps the console's one code path honest on a host
    built without one. ``log_buffer`` ``None`` means there is no buffer, and the
    state feed reports ``log_seq`` 0 rather than a tail.

    ``screen_fps`` caps how often a watched screen is encoded, not how fast the
    machine sends — it is already sending every frame, and the ones not encoded
    are the ones no longer in the receiver's `latest()`."""
    from fastapi import HTTPException, Request, Response, WebSocket, WebSocketDisconnect
    from fastapi.responses import StreamingResponse
    from starlette.background import BackgroundTask

    from .perf_console import PerfBridge, SocketReader

    bridge = PerfBridge(lambda: list(playlists().items()))
    # One playlist per system, each holding the backend that system's writes go
    # through — and the screen is a property of that same machine.
    screen = ScreenFeed(lambda: {name: pl.api for name, pl in playlists().items()})

    # Built once: ~150 KB of JSON assembled by walking every config dataclass,
    # every scene type and every overlay. It describes the code, not the run,
    # so it cannot change while the process is up. The lock is what makes
    # "once" true: `api_introspect` is a sync `def`, so FastAPI runs it in the
    # threadpool and two cold-cache requests would otherwise both walk the
    # whole model, under the GIL, on the process serving the state socket.
    introspection: dict[str, Any] = {}
    introspection_lock = threading.Lock()

    streams = StreamSlots(MAX_SCREEN_WATCHERS)

    def _make_request(path: str | None) -> StartRequest:
        """Load + validate, mapping either failure to a 422 the browser can
        render. `SessionConfigError` carries the CLI's exit code, which is the
        closest thing to a machine-readable reason the validators produce."""
        try:
            return request_factory(path)
        except SessionConfigError as e:
            detail = e.detail or f"config did not validate (exit code {e.exit_code}); see the log"
            raise HTTPException(422, detail) from e
        except ConfigError as e:
            raise HTTPException(422, str(e)) from e

    def _busy(e: SupervisorBusy) -> HTTPException:
        return HTTPException(409, str(e))

    def _store_error(e: ConfigStoreError) -> HTTPException:
        for kind, status in _STORE_STATUS:
            if isinstance(e, kind):
                detail = getattr(e, "report", None) or str(e)
                return HTTPException(status, detail)
        return HTTPException(400, str(e))

    def _media_error(e: MediaStoreError) -> HTTPException:
        for kind, status in _MEDIA_STATUS:
            if isinstance(e, kind):
                return HTTPException(status, str(e))
        return HTTPException(400, str(e))

    def _resolve_ref(ref: str | None) -> str | None:
        """A browser names a config by ref; the loader wants a filesystem path.
        This is the only crossing between the two, and the store is what makes
        it safe."""
        if not ref:
            return None
        try:
            return str(store.resolve(str(ref)))
        except ConfigStoreError as e:
            raise _store_error(e) from e

    async def _body(request: Request) -> Mapping[str, Any]:
        """The optional JSON body of a POST. Absent is not an error — a start
        with no body is a start of whatever the host was launched with.

        Capped by `auth.read_body`, so the transport refuses an oversized body
        before it is resident: `ConfigStore`'s own `ConfigTooLarge` (the 413 in
        `_STORE_STATUS`) protects the *file*, and only ever saw a body the host
        had already buffered whole."""
        try:
            raw = await read_body(request)
        except BodyTooLarge as e:
            raise HTTPException(413, str(e)) from e
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise HTTPException(400, "request body is not JSON") from e
        if not isinstance(parsed, Mapping):
            raise HTTPException(400, "request body must be a JSON object")
        return parsed

    def _session_state(scope: Mapping[str, Any]) -> dict[str, Any]:
        state = _status_payload(manager.status(), log_buffer, store)
        state["role"] = scope.get(SCOPE_ROLE_KEY)
        return state

    @app.get("/api/introspect")
    def api_introspect() -> dict[str, Any]:
        nonlocal introspection
        with introspection_lock:
            if not introspection:
                introspection = introspect.as_dict()
            return introspection

    @app.get("/api/update")
    def api_update() -> dict[str, Any]:
        """The last recorded PyPI check (`update_state.py`), for the
        console's dismissible update banner. Never queries PyPI itself —
        that only happens from `c64cast --check-for-updates --write-state`,
        so opening this tab never makes an outbound request on its own. A
        GET, so a `viewer` token can see it same as it can watch the
        screen. The recorded verdict is re-answered against the version
        actually running (`update_state.rechecked`), so an install upgraded
        since the last check stops advertising the release it already took.
        `unanswered_since` rides along so the console can say when PyPI last
        stopped answering — a machine off the internet for a month is one
        the banner has something else to tell — and `stale_after_days` with
        it, so how long "a month" is stays one number here rather than one
        here and another in the bundle."""
        from c64cast import __version__
        from c64cast.app.update_state import STALE_AFTER_DAYS, read_update_state, rechecked

        check = rechecked(read_update_state(path=paths.update_check_path()), __version__)
        if check is None:
            return {
                "checked": False,
                "running_version": __version__,
                "stale_after_days": STALE_AFTER_DAYS,
            }
        return {
            "checked": True,
            "checked_at": check.checked_at,
            "running_version": check.running_version,
            "latest_version": check.latest_version,
            "newer": check.newer,
            "unanswered_since": check.unanswered_since,
            "stale_after_days": STALE_AFTER_DAYS,
        }

    # The screen. All three are GETs, so the read-only role can watch — a
    # viewer who cannot see the show has been handed a link to nothing. What a
    # GET here does have is a side effect on the machine (it starts the VIC
    # stream while somebody is looking), and that is the right trade: it changes
    # nothing about what the C64 is doing, and every alternative means a viewer
    # cannot see the screen at all.
    def _screen_system(system: str | None) -> str:
        """The system whose screen to show, or the reason there isn't one.

        ``screen_fps = 0`` is the off switch, and it is refused here rather than
        by leaving the routes unregistered: a console asking a host that has the
        picture turned off should hear *that*, not a 404 it would read as an
        older host."""
        if screen_fps <= 0:
            raise ScreenUnavailable("this host has the live screen turned off ([web].screen_fps)")
        return screen.resolve(system)

    @app.get("/api/screen")
    def api_screen() -> dict[str, Any]:
        """Which systems can show a picture, without starting anything."""
        return {
            "systems": {} if screen_fps <= 0 else screen.available(),
            "fps": screen_fps,
        }

    @app.get("/api/screen.png")
    def api_screen_png(system: str | None = None) -> Response:
        try:
            return Response(
                screen.latest_png(_screen_system(system)),
                media_type="image/png",
                # A still is a *now*, and a browser that cached one would show
                # a screen from before the change that was made to see it.
                headers={"Cache-Control": "no-store"},
            )
        except ScreenUnavailable as e:
            raise HTTPException(501, str(e)) from e

    @app.get("/api/screen/stream")
    def api_screen_stream(request: Request, system: str | None = None) -> StreamingResponse:
        """The machine's screen as `multipart/x-mixed-replace`, which one
        `<img>` renders with no script and no decoder in the page."""
        try:
            name = _screen_system(system)
        except ScreenUnavailable as e:
            raise HTTPException(501, str(e)) from e

        # A viewer token reaches this route (it is a GET) and used to be able
        # to open as many streams as it liked. See `StreamSlots`.
        if not streams.claim():
            raise HTTPException(503, f"this host is already streaming to {streams.limit} watchers")

        # The watch is held by the *response*, not by the generator: acquired
        # here and released by a background task, which Starlette runs once the
        # body is done however it ended. Putting the release in the generator's
        # own `finally` is the obvious thing and it does not work — the
        # generator runs on a worker thread, a disconnect cancels the async task
        # while that thread is inside it, and closing it from there raises
        # rather than unwinding. See `ScreenFeed.release`. The stream slot rides
        # the same background task for the same reason.
        def _done() -> None:
            screen.release(name)
            streams.release()

        try:
            read = screen.acquire(name)
        except BaseException:
            streams.release()
            raise
        return StreamingResponse(
            _until_gone(multipart_frames(read, fps=screen_fps), request, streams.pool),
            media_type=f"multipart/x-mixed-replace; boundary={screen_mod.BOUNDARY}",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(_done),
        )

    # POST, not GET, for two reasons that point the same way: it may mint a
    # credential, and the auth gate lets a `viewer` token through every GET —
    # a read-only guest must not be able to ask for the link that made them one.
    @app.post("/api/viewer-link")
    def api_viewer_link() -> dict[str, Any]:
        """The read-only login link to hand somebody, minting the token on the
        first ask.

        Returns a *path*: the host may be bound to ``0.0.0.0`` and have no idea
        which of its addresses the phone in your hand reached it on, whereas the
        browser asking has that in `location.origin`."""
        if viewer is None:
            raise HTTPException(501, "this host was built without a read-only credential")
        token, minted = viewer.issue()
        if minted:
            log.info("web console: issued a read-only token")
        return {
            "token": token,
            "path": f"{LOGIN_PATH}?token={quote(token)}&next={quote(landing_path())}",
            "minted": minted,
        }

    @app.get("/api/session")
    def api_session(request: Request) -> dict[str, Any]:
        state = _session_state(request.scope)
        if log_buffer is not None:
            state["log"] = log_buffer.tail(_LOG_BACKLOG)
        return state

    @app.post("/api/session/start", status_code=202)
    async def api_start(request: Request) -> dict[str, Any]:
        ref = (await _body(request)).get("config")
        req = _make_request(_resolve_ref(ref))
        try:
            generation = manager.start(req)
        except SupervisorBusy as e:
            raise _busy(e) from e
        # Recorded from every surface that starts a show, not just this one —
        # `_apply_command` does the same for the socket. A falsy ref is the
        # host's own default config, which has nothing to add to a *config*
        # library.
        if ref:
            library.record_recent(str(ref))
        return {"ok": True, "generation": generation, "state": str(manager.state)}

    @app.post("/api/session/stop", status_code=202)
    def api_stop() -> dict[str, Any]:
        # Not an error from idle: a console that stops twice, or stops a show
        # that just ended by itself, has got what it asked for.
        return {"ok": True, "stopping": manager.stop(), "state": str(manager.state)}

    @app.post("/api/session/switch", status_code=202)
    async def api_switch(request: Request) -> dict[str, Any]:
        ref = (await _body(request)).get("config")
        req = _make_request(_resolve_ref(ref))
        try:
            generation = manager.switch(req)
        except SupervisorBusy as e:
            raise _busy(e) from e
        if ref:
            library.record_recent(str(ref))
        return {"ok": True, "generation": generation, "state": str(manager.state)}

    @app.post("/api/session/reload")
    def api_reload() -> dict[str, Any]:
        try:
            manager.reload()
        except SupervisorBusy as e:
            raise _busy(e) from e
        return {"ok": True, "generation": manager.generation}

    def _tuned(system: Any) -> Playlist:
        """The playlist whose live-tune record a save-back acts on."""
        running = playlists()
        if not running:
            raise HTTPException(409, "nothing is running, so nothing has been tuned")
        if system is None:
            return next(iter(running.values()))
        pl = running.get(str(system))
        if pl is None:
            raise HTTPException(404, f"no system named {system!r} is running")
        return pl

    # A performance command goes over the socket; this does not. It *writes a
    # config file*, so it belongs with the store's other writers: same refusals,
    # same 422 report, same backup sibling — and a status code, which a socket
    # frame has nowhere to put.
    @app.post("/api/session/live-tune")
    async def api_live_tune(request: Request) -> dict[str, Any]:
        """Keep (or drop) the knob changes made since the show started.

        The CLI asks this question at exit, on a terminal the daemon does not
        have. Here it is a tap instead, and the write goes through
        :meth:`ConfigStore.patch` rather than the menu's whole-config dump for
        the reason the form's save does: the file is re-read first, so a field
        edited in the config editor since the show started is still there
        afterwards, and a patch that would stop the config loading is refused
        with the file untouched.

        A change goes to ``[color]`` or to the ``[[scenes]]`` block it was made
        on, whichever is that knob's home — the tracker's row says which, and the
        two kinds ride in one patch so a save is one write, one backup and one
        refusal. What no config carries is reported and left alone, so nothing is
        silently dropped on the way to the file."""
        body = await _body(request)
        action = str(body.get("action", "save"))
        pl = _tuned(body.get("system"))
        # One snapshot for the whole request: a knob turned between two reads
        # would make the list disagree with what actually gets written.
        rows = pl.live_tracker.pending()
        if action == "discard":
            return {"ok": True, "discarded": pl.live_tracker.forget(r["key"] for r in rows)}
        if action != "save":
            raise HTTPException(400, 'a live-tune command needs action "save" or "discard"')
        savable = [r for r in rows if r["field"] is not None]
        if not savable:
            raise HTTPException(
                409,
                "nothing tuned here has a config field behind it"
                if rows
                else "nothing has been tuned since this show started",
            )
        ref = store.ref_for(Path(pl.config_path)) if pl.config_path else None
        if ref is None:
            raise HTTPException(
                409,
                f"{pl.config_path or 'this run'} is not a config under a root this host can "
                "write, so there is no file to keep these in",
            )
        edits = [_live_tune_edit(r) for r in savable]
        try:
            out = store.patch(ref, edits)
        except ConfigStoreError as e:
            raise _store_error(e) from e
        # Only now: a refused patch leaves the record intact, so the console can
        # show the reason and the same Save button still means something.
        pl.live_tracker.forget(r["key"] for r in savable)
        # And bring the run's own Config up to what the file now says. The C64's
        # menu save-back dumps *that* object wholesale, so leaving it stale would
        # let somebody at the machine quietly revert what was just saved from the
        # browser. Only these fields, and only after the file took them.
        if pl.config is not None:
            _restamp(pl.config, savable)
        out["saved"] = [r["target"] for r in savable]
        out["kept_out"] = [r["target"] for r in rows if r["field"] is None]
        return out

    # -- favorites + recents --------------------------------------------------

    @app.get("/api/library")
    def api_library() -> dict[str, Any]:
        return library.as_dict()

    @app.post("/api/library/favorites")
    async def api_library_favorite(request: Request) -> dict[str, Any]:
        body = await _body(request)
        ref = str(body.get("ref", ""))
        if not ref:
            raise HTTPException(400, 'a favorite needs a "ref"')
        return {"favorites": library.set_favorite(ref, bool(body.get("on", True)))}

    # -- the config browser -------------------------------------------------

    @app.get("/api/configs")
    def api_configs() -> dict[str, Any]:
        return store.index()

    # A GET, so the viewer role may browse it — it lists media a config
    # already names, and a viewer who can watch the screen can already see it.
    @app.get("/api/media")
    def api_media(kind: str, q: str = "") -> dict[str, Any]:
        try:
            return media.index(kind, q)
        except MediaKindUnknown as e:
            raise HTTPException(400, str(e)) from e

    # PUT, not POST: `name` names the resource being created, same shape as
    # `/api/configs/{ref:path}`'s save. Streamed straight through to
    # `MediaStore.receive` rather than read into memory first — see that
    # module's docstring for why a `bytes` buffer isn't acceptable here. A
    # `viewer` token is refused with no code at all: `auth.READ_METHODS`
    # covers only GET/HEAD/OPTIONS, so a PUT never reaches this function.
    #
    # Each `write` (and the commit `receive` runs on a clean exit — fsync,
    # then the rename) is blocking disk I/O, so it's pushed through
    # `run_in_executor` same as `_until_gone` does for frame encoding: this
    # is the one event loop also serving the control websocket and status
    # polling for a show that may be running right now.
    #
    # The context-manager protocol is driven by hand for that offload, and
    # three of its contracts are load-bearing here: `__exit__(None, None,
    # None)` *is* the commit, `handle.result` is only meaningful after that
    # commit has returned, and a failure mid-body still needs `__exit__` called
    # with the exception triple to clean the part file up. Turning these back
    # into a `with` block would block the event loop on fsync.
    @app.put("/api/media/{name}")
    async def api_media_upload(name: str, request: Request) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        upload = media.receive(name)
        try:
            handle = upload.__enter__()
        except MediaStoreError as e:
            raise _media_error(e) from e
        try:
            async for chunk in request.stream():
                await loop.run_in_executor(None, handle.write, chunk)
        except BaseException as e:
            upload.__exit__(type(e), e, e.__traceback__)
            if isinstance(e, MediaStoreError):
                raise _media_error(e) from e
            raise
        await loop.run_in_executor(None, upload.__exit__, None, None, None)
        return handle.result

    # A path (not a ref) that names the new file: it doesn't exist yet, so
    # there is nothing for `ConfigStore.resolve` to have found and turned into
    # a ref. `copy_of` is a ref, the same identifier the list already shows.
    @app.post("/api/configs")
    async def api_config_create(request: Request) -> dict[str, Any]:
        body = await _body(request)
        ref = str(body.get("path", ""))
        if not ref:
            raise HTTPException(400, 'creating a config needs a "path"')
        copy_of = body.get("copy_of")
        try:
            return store.create(ref, copy_of=str(copy_of) if copy_of else None)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    # Registered before the bare `{ref:path}` route so a POST can't be read as
    # a write to a file whose name happens to end in "/validate". A body with
    # no "text" key checks the file as it stands on disk — the console's
    # pre-flight before a start — rather than silently validating "".
    @app.post("/api/configs/{ref:path}/validate")
    async def api_config_validate(ref: str, request: Request) -> dict[str, Any]:
        body = await _body(request)
        try:
            if "text" in body:
                return store.validate_text(str(body["text"]), ref)
            return store.validate_ref(ref)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    # **Full token only**, and the one route in this module where the HTTP verb
    # is not the whole authorization story. `ConfigStore.read` returns `text`
    # as the file verbatim — every `config_serialize.SECRET_FIELDS` value it
    # carries included, which is what its own docstring says and what
    # `describe()`'s `form` deliberately withholds — so a read-only link handed
    # to a guest used to read `[web] token` out of any config under a root and
    # come back as the host's administrator. `require_full` is the seam
    # (`auth.py`); `tests/test_web_api.py::RouteRoleContractTest` is what stops
    # the next GET from shipping unclassified.
    @app.get("/api/configs/{ref:path}")
    def api_config_read(ref: str, request: Request) -> dict[str, Any]:
        require_full(request.scope)
        try:
            return store.read(ref)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    @app.put("/api/configs/{ref:path}")
    async def api_config_write(ref: str, request: Request) -> dict[str, Any]:
        body = await _body(request)
        if "text" not in body:
            raise HTTPException(400, 'a config write needs a "text" key')
        try:
            return store.write(ref, str(body["text"]))
        except ConfigStoreError as e:
            raise _store_error(e) from e

    # Registered before the bare `{ref:path}` route for the same reason
    # `/validate` is: a config whose name ends in "/scenes" must not swallow it.
    #
    # Structural, not a field edit — which is why it is its own route and not
    # another kind of PATCH body. Adding a clip to a show is the most common
    # change there is to a show file, and it was the one that still required
    # opening the text editor.
    @app.post("/api/configs/{ref:path}/scenes")
    async def api_scene_add(ref: str, request: Request) -> dict[str, Any]:
        body = await _body(request)
        try:
            return store.add_scene(
                ref,
                scene_type=str(body.get("type") or ""),
                copy_of=_opt_index(body.get("copy"), "copy"),
                after=_opt_index(body.get("after"), "after"),
            )
        except ConfigStoreError as e:
            raise _store_error(e) from e

    @app.delete("/api/configs/{ref:path}/scenes/{index}")
    def api_scene_remove(ref: str, index: int) -> dict[str, Any]:
        try:
            return store.remove_scene(ref, index)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    # Registered before the bare `{ref:path}` PATCH below for the same reason
    # `/validate` and `/scenes` are: a config named "…/scenes/3" must not be
    # read as a request to PATCH a file by that name.
    @app.patch("/api/configs/{ref:path}/scenes/{index}")
    async def api_scene_move(ref: str, index: int, request: Request) -> dict[str, Any]:
        body = await _body(request)
        try:
            to = _opt_index(body.get("to"), "to")
            if to is None:
                raise HTTPException(400, 'moving a scene needs a "to" index')
            return store.move_scene(ref, index, to)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    # Registered after the more specific DELETE route above, so a delete of
    # "…/scenes/3" is never read as a request to remove a file named that.
    @app.delete("/api/configs/{ref:path}")
    def api_config_delete(ref: str) -> dict[str, Any]:
        status = manager.status()
        # `status.config_path` names the last config this host started even at
        # idle (see SessionManager's docstring on `_launch_config_path`), so
        # matching on the path alone would refuse a delete long after the
        # session that ran it has stopped. Only a config the supervisor is
        # actually mid-show with — not startable again without a fresh
        # start — is the one this route needs to protect.
        active = status.state not in STARTABLE and status.config_path
        if active and store.ref_for(Path(status.config_path)) == ref:
            raise HTTPException(409, f"{ref} is the running config — stop it first")
        try:
            return store.delete(ref)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    # The generated form's save. PUT replaces the file with the text a client
    # composed; PATCH names fields and lets the *server* compose the text, so a
    # form never has to know how a TOML is written — and two consoles editing
    # different fields of one config don't overwrite each other's sections.
    @app.patch("/api/configs/{ref:path}")
    async def api_config_patch(ref: str, request: Request) -> dict[str, Any]:
        body = await _body(request)
        edits = body.get("edits")
        if not isinstance(edits, list):
            raise HTTPException(400, 'a config patch needs an "edits" list')
        try:
            return store.patch(ref, edits)
        except ConfigStoreError as e:
            raise _store_error(e) from e

    # -- the state feed -----------------------------------------------------

    def _apply_command(cmd: Mapping[str, Any]) -> bool:
        """Session commands over the socket, falling through to the
        performance engine for everything else — one inbound channel, so a
        console doesn't need a second connection to launch a clip."""
        action = cmd.get("session")
        if action is None:
            return bool(bridge.apply(cmd))
        try:
            ref = cmd.get("config")
            path = str(store.resolve(str(ref))) if ref else None
            if action == "start":
                manager.start(request_factory(path))
                if ref:
                    library.record_recent(str(ref))
            elif action == "switch":
                manager.switch(request_factory(path))
                if ref:
                    library.record_recent(str(ref))
            elif action == "stop":
                manager.stop()
            elif action == "reload":
                manager.reload()
            else:
                return False
        except (SupervisorBusy, SessionConfigError, ConfigError, ConfigStoreError) as e:
            # The socket has no status code; the refusal shows up as the state
            # simply not changing, so say why in the log the console renders.
            log.warning("web console: %s refused: %s", action, e)
            return False
        return True

    @app.websocket("/api/ws")
    async def api_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        # The one gap the auth middleware can't cover: a socket is a single
        # `GET` handshake, so inbound command frames are dropped here (see
        # perf_console.perf_ws, which has the same hole for the same reason).
        read_only = is_viewer(websocket.scope)
        reader = SocketReader(websocket, label="web console")
        sent_seq = 0 if log_buffer is None else max(0, log_buffer.seq - _LOG_BACKLOG)
        try:
            while True:
                # Same split as `perf_ws`: a frame that raises is our bug, and
                # swallowing it below would leave the console waiting forever
                # for a push that is never coming.
                # Cheap, and the only regular tick this process has: it is what
                # stops a video stream whose watchers have gone or whose show
                # has ended. A timer of its own would be a thread paid for at
                # idle to notice that nothing is happening.
                screen.sweep()
                try:
                    frame = bridge.state()
                    frame["role"] = websocket.scope.get(SCOPE_ROLE_KEY)
                    frame["session"] = _status_payload(manager.status(), log_buffer, store)
                except Exception:
                    log.exception("web console: could not build a state frame")
                    break
                if log_buffer is not None:
                    lines = log_buffer.since(sent_seq)
                    if lines:
                        sent_seq = lines[-1]["seq"]
                    frame["log"] = lines
                await websocket.send_json(frame)
                arrived, msg = await reader.poll(_PUSH_INTERVAL_S)
                if arrived and isinstance(msg, Mapping) and not read_only:
                    _apply_command(msg)
        except WebSocketDisconnect:
            pass
        except (ConnectionError, RuntimeError):
            # An abrupt client close surfaces as a transport error rather than
            # a `WebSocketDisconnect`, so these stay at debug. Anything else is
            # a socket nobody asked to close — a `send_json` that could not
            # serialize a new payload field, a raise out of `bridge.apply` —
            # and at debug the symptom was a console that flickered or showed
            # stale state with a clean log, which on an appliance means an SSH
            # session and a restart to see anything at all.
            log.debug("web console: websocket closed", exc_info=True)
        except Exception:
            log.exception("web console: websocket closed unexpectedly")
        finally:
            await reader.close()
