"""``GET``/``POST /api/setup`` — the appliance first-run form.

Registered only while :mod:`c64cast.control.setup_gate` has setup pending (see
``serve.build_daemon_app``): once ``setup.json`` exists the app is rebuilt
without either module, so there is nothing here to disable at request time.
The form itself is a screen of the ordinary console bundle
(``web/src/lib/screens/Setup.svelte``); this module is only its API.

**No second parser, no second serializer, no second writer.** The connection
target goes through :func:`c64cast.app.connect.parse_connection_uri` — the same
one ``-u``, ``--save-settings`` and quickcast use — and lands in machine
settings through :func:`c64cast.app.config_serialize.save_machine_settings`,
the same function ``cli_commands.run_save_settings`` calls. A hand-copy of that
writer's shape once dropped every secret already in ``settings.toml``.

**No credential leaves here until setup completes.** ``GET`` reports only
whether the token is *settable* and never the token itself, because anyone on
the LAN can call it while the window is open; the full token rides back exactly
once, in the ``login_url`` of a successful ``POST``. A token is settable only
when the host generated it — one named in ``[web].token``, ``[web].token_file``
or ``$C64CAST_WEB_TOKEN`` outranks the file :func:`_write_token` writes, so
accepting a replacement would answer "ok" and lock the admin out on the next
restart.

Deliberately does **not** ``from __future__ import annotations``, for the same
reason :mod:`c64cast.control.auth` doesn't: the routes below annotate a
``Request`` parameter, and a stringized annotation resolved against a name
that was imported *inside* the registering function would not resolve at all.

See docs/architecture/control.md#setup_gatepy--setup_apipy--the-appliance-first-run-setup-window.
"""

import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from c64cast.app import config as cfgmod
from c64cast.app import config_serialize, paths
from c64cast.app.connect import (
    ConnectionSpec,
    ConnectionURIError,
    apply_to_config,
    parse_connection_uri,
)

from .auth import BODY_TOO_LARGE_ERROR, LOGIN_PATH, MIN_TOKEN_LENGTH, BodyTooLarge, read_body
from .transport import atomic_write_text
from .web_static import landing_path

log = logging.getLogger(__name__)

# `MIN_TOKEN_LENGTH` is imported rather than declared here, and named in
# `__all__` because this module's callers and tests still spell it here: the
# policy belongs to `auth`.
__all__ = ["MIN_TOKEN_LENGTH", "SetupRefused", "login_url", "register_setup_routes"]


class SetupRefused(ValueError):
    """Something the admin can fix by typing something else — answered as a
    ``400`` the form shows back to them, never as a server fault."""


def _connection_from(body: dict[str, Any]) -> tuple[str, ConnectionSpec]:
    """``(target, spec)`` for the connection this form was submitted with."""
    target = body.get("connection")
    if not isinstance(target, str) or not target.strip():
        raise SetupRefused("a connection target is required")
    try:
        return target.strip(), parse_connection_uri(target.strip())
    except ConnectionURIError as e:
        raise SetupRefused(str(e)) from e


def _token_from(body: dict[str, Any], *, settable: bool) -> str:
    """The admin's chosen token, or ``""`` to keep the host's own.

    The "not settable" refusal comes before the length check on purpose: when
    the token is pinned by configuration, *no* replacement is acceptable, and
    telling somebody to type a longer one would be advice that cannot work.

    **Stripped before every check**, because the two ends of this contract
    disagree otherwise: :func:`_write_token` persists what it is given and
    ``serve._generated_token`` reads that file back with ``.strip()``, so an
    untrimmed token goes out in ``login_url`` and comes back different — and a
    whitespace-only one would pass the length check and strip to ``""``. An
    interior newline is refused because ``_write_token`` appends its own."""
    chosen = body.get("token")
    if chosen is None or chosen == "":
        return ""
    if not isinstance(chosen, str):
        raise SetupRefused("token must be a string")
    chosen = chosen.strip()
    if not chosen:
        return ""
    if "\n" in chosen or "\r" in chosen:
        raise SetupRefused("token must be a single line")
    if not settable:
        raise SetupRefused(
            "this host's token is fixed by its configuration ([web] token or "
            "token_file, or $C64CAST_WEB_TOKEN) and cannot be changed here"
        )
    if len(chosen) < MIN_TOKEN_LENGTH:
        raise SetupRefused(f"token must be at least {MIN_TOKEN_LENGTH} characters")
    return chosen


def _write_connection(spec: ConnectionSpec) -> None:
    """Overlay ``spec`` onto machine settings, merged with what is already
    there, through the one writer ``--save-settings`` also uses — which is what
    keeps the secrets this merge just read out of the file from being dropped
    on the way back in (see this module's docstring)."""
    cfg = cfgmod.Config()
    cfgmod.apply_machine_settings(cfg)
    apply_to_config(cfg, spec)
    config_serialize.save_machine_settings(cfg)


def _write_token(token: str) -> None:
    """Persist an admin-chosen token exactly the way ``serve._generated_token``
    persists a minted one — same directory, same ``0600``."""
    path = paths.web_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, token + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        log.warning("could not restrict permissions on %s", path)


def _mark_complete(connection: str) -> None:
    """Write the completion marker **last**, after the connection and any
    token are already on disk — a failure partway through this route leaves
    setup pending rather than half-configured, and every write before this one
    is idempotent under the retry that then becomes possible."""
    path = paths.setup_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"completed_at": time.time(), "connection": connection}
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def login_url(token: str) -> str:
    """The one link that gets an appliance admin into the console they just
    configured: the same ``/api/login?token=…&next=…`` the daemon prints at
    startup, which is no use to somebody who has no terminal on the box."""
    return f"{LOGIN_PATH}?{urlencode({'token': token, 'next': landing_path()})}"


def register_setup_routes(
    app: Any, *, token: str, token_settable: bool, on_complete: Callable[[], None]
) -> None:
    """Register the setup form's API onto ``app``.

    ``token`` is the console's *current* full token (generated or configured);
    it is never reported by ``GET``, only handed back in the ``login_url`` of a
    completed ``POST``. ``token_settable`` is whether writing a new one would
    actually take effect — see this module's docstring. ``on_complete`` is
    called after every write below has landed: ``serve.run_daemon``'s restart
    loop rebuilds the app from scratch once it fires, which is what actually
    stops serving this route."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response

    @app.get("/api/setup")
    def get_setup() -> Response:
        return JSONResponse({"pending": True, "token_settable": token_settable})

    @app.post("/api/setup")
    async def post_setup(request: Request) -> Response:
        try:
            body = json.loads(await read_body(request))
        except BodyTooLarge as e:
            # Unauthenticated while the window is open, so the body has to be
            # refused before it is resident (see `read_body`); the cap it
            # tripped is the operator's business, not the caller's.
            log.debug("setup body refused: %s", e)
            return JSONResponse({"ok": False, "error": BODY_TOO_LARGE_ERROR}, status_code=413)
        except Exception:
            body = None
        try:
            if not isinstance(body, dict):
                raise SetupRefused("malformed request body")
            target, spec = _connection_from(body)
            chosen = _token_from(body, settable=token_settable)
        except SetupRefused as e:
            # Every message that reaches here is authored prose, not a
            # traceback: `SetupRefused` is raised only in this module and in
            # `_connection_from`, which relays `ConnectionURIError` — f-strings
            # in `connect.py` quoting nothing but the target just submitted.
            # The detail is the point: it is all an admin staring at a refused
            # form has to go on. CodeQL flags `str()` of any caught exception
            # and cannot tell the two apart, hence the waiver.
            #
            # The marker must stay on its **own line, immediately above** the
            # line it waives. `CodeQlSuppressionComment` in CodeQL's
            # `shared/util/codeql/util/suppression/AlertSuppression.qll` only
            # constructs when no AST node precedes the comment on its line, so
            # a trailing `# codeql[...]` is inert; and its `covers` is
            # `startline - 1`, so the one it does form applies to the next
            # line. (Same-line placement belongs to `lgtm[...]` and `noqa`.)
            #
            # Moving a marker shifts the flagged line and mints a new alert
            # number; the replacement is dismissed on the next `main` run.
            return JSONResponse(
                # codeql[py/stack-trace-exposure]
                {"ok": False, "error": str(e)},
                status_code=400,
            )

        # The connection first, then the token, then the marker. The token used
        # to go first, so a full disk or a read-only settings dir left the
        # host's credential already replaced by one the 500 never handed back.
        try:
            _write_connection(spec)
            if chosen:
                _write_token(chosen)
            _mark_complete(target)
        except OSError as e:
            # The admin's only interface is this form, so a bare 500 with
            # FastAPI's empty body leaves them with no next step. `e.filename`
            # is the file that could not be written and `strerror` the OS's own
            # reason, neither of which is a traceback.
            log.exception("web console: setup could not write its state")
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"could not write {e.filename or 'the host state'}: "
                    f"{e.strerror or 'the write failed'}. Setup is still pending, so "
                    "this can be retried once the host can write to its own data "
                    "directory.",
                },
                status_code=500,
            )
        log.info("web console: setup completed (%s)", spec.backend)
        on_complete()
        return JSONResponse({"ok": True, "login_url": login_url(chosen or token)})
