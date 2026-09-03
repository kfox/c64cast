"""``GET``/``POST /api/setup`` — the appliance first-run form.

Registered only while :mod:`c64cast.control.setup_gate` has setup pending (see
``serve.build_daemon_app``): once ``setup.json`` exists the app is rebuilt
without either module, so there is nothing here to disable at request time.
That is also why this asks for as little as possible — a connection target and
a token choice — rather than growing into a second config editor: everything
else is already reachable through the console's own Settings screen once
setup hands over to it. The form itself is a screen of the ordinary console
bundle (``web/src/lib/screens/Setup.svelte``), reachable because
``setup_gate`` leaves the shell and its assets alone; this module is only its
API.

**No second parser, no second serializer, no second writer.** The connection
target goes through :func:`c64cast.app.connect.parse_connection_uri`, the same
one ``-u``, ``--save-settings`` and quickcast use, and lands in machine
settings through :func:`c64cast.app.config_serialize.save_machine_settings` —
the same function ``cli_commands.run_save_settings`` calls, not a hand-copy of
its shape. That distinction is the whole point: this module used to *assert* in
prose that it mirrored the CLI's save path "exactly" while missing the one
guard that path had, and the result was a form that erased every secret already
in ``settings.toml`` (the ``dma_password`` a password-protected U64 needs, and
the ``[web] token`` pin ``token_settable`` exists to protect) on the first
successful POST. The seed-and-overlay is still per-caller, because each
overlays something different; everything from the serialize onward is shared.

**No credential leaves here until setup completes.** ``GET`` reports only
whether the token is *settable*; it never carries the token itself, because
anyone on the LAN can call it while the window is open. The full token rides
back exactly once, in the ``login_url`` of a successful ``POST`` — by then the
caller is the one who configured the box, which is the trust model SECURITY.md
describes for this window, and it is also the only way an appliance admin with
no console access can ever learn it.

A token is settable only when the host generated it. One named in
``[web].token``, ``[web].token_file`` or ``$C64CAST_WEB_TOKEN`` outranks the
generated file that :func:`_write_token` writes, so accepting a replacement
would answer "ok" and then lock the admin out on the next restart; that case is
refused here and shown as a disabled field in the form.

Deliberately does **not** ``from __future__ import annotations``, for the same
reason :mod:`c64cast.control.auth` doesn't: the routes below annotate a
``Request`` parameter, and a stringized annotation resolved against a name
that was imported *inside* the registering function would not resolve at all.
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

# `MIN_TOKEN_LENGTH` is imported above rather than declared here, and is named
# in `__all__` because this module's callers and tests still spell it here: the
# policy itself belongs to `auth`, since one enforced on one of four entry
# points is a policy nobody owns.
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
    disagreed otherwise: :func:`_write_token` persists what it is given and
    ``serve._generated_token`` reads that file back with ``.strip()``. A token
    pasted from a password manager with a trailing space went out in
    ``login_url`` urlencoded *with* the space and came back after the restart
    without it, so the one link an appliance admin was handed answered 401
    forever — and a whitespace-only token of 16 characters passed the length
    check, stripped to ``""`` on read, and made the host mint a brand-new
    credential nobody has ever seen with the setup window already closed. An
    interior newline is refused for the adjacent reason: ``_write_token`` adds
    its own, so the file's shape would be ambiguous."""
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
            # This route is unauthenticated while the window is open, so the
            # body has to be refused before it is resident (see `read_body`),
            # and the cap it tripped is the operator's business, not the
            # caller's.
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
            # `_connection_from`, which relays `ConnectionURIError`, and every
            # one of those is an f-string in `connect.py` quoting nothing but
            # the target the caller just submitted. The detail is the point —
            # "u64:// needs a host (e.g. u64://192.168.2.64)" is the same
            # advice `-u` prints, and it is all an admin staring at a refused
            # form has to go on. CodeQL flags `str()` of any caught exception
            # and cannot tell the two apart, hence the waiver; the marker sits
            # on the line it reports, the argument's own.
            return JSONResponse(
                {"ok": False, "error": str(e)},  # codeql[py/stack-trace-exposure]
                status_code=400,
            )

        # The connection first, then the token, then the marker. The token used
        # to go first, so a full disk or a read-only settings dir left the
        # host's credential already replaced by one the 500 never handed back —
        # on a box whose only interface is this form, a self-inflicted lockout
        # window even though the retry stays possible.
        try:
            _write_connection(spec)
            if chosen:
                _write_token(chosen)
            _mark_complete(target)
        except OSError as e:
            # The admin's only interface is this form, so a bare 500 with
            # FastAPI's empty body leaves them with no next step. Name the path
            # instead: `e.filename` is the file that could not be written, and
            # `strerror` is the OS's own reason, neither of which is a
            # traceback.
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
