"""Connection-target URI parsing.

A single scheme-aware connection string selects the hardware backend *and* its
transport/endpoint, so the CLI needs only ``-u/--url`` (or the ``C64CAST_URL``
env var) to point c64cast at any supported device. It decomposes into the
existing config fields — ``[hardware].backend``, ``[ultimate64].url`` /
``dma_port``, ``[teensyrom].transport`` / ``serial_port`` / ``host`` /
``tcp_port`` / ``baud`` / ``storage`` — which remain the canonical store a TOML
config sets directly. This module is the CLI/env front-end that fills them in;
:func:`c64cast.hw.backend.make_backend` reads them unchanged.

Schemes::

    u64://HOST[:PORT]         Ultimate 64 / Ultimate II+ over REST + socket DMA.
                              -> backend=ultimate, url=http://HOST[:PORT]
    http://HOST  https://HOST Same target, passed to the REST client verbatim.
                              The Ultimate is the only HTTP-speaking backend
                              today, so an http(s):// target is deterministically
                              the Ultimate; the startup probe confirms liveness.
    tr://                     TeensyROM+ over USB serial, device auto-detected.
    tr:///dev/cu.usbmodemXYZ  TeensyROM+ over USB serial on that device node.
    tr://COM3                 (Windows) TeensyROM+ over that COM port.
    tr://HOST[:PORT]          TeensyROM+ over raw TCP (default port 2112).

The serial-vs-TCP split for ``tr://`` falls out of the URL shape: an empty
netloc (``tr://`` or ``tr:///dev/...``) is serial; a non-empty netloc is a TCP
host (with a ``COM<n>`` netloc special-cased back to a Windows serial port).

Rare per-link knobs ride along as ``?query`` params so they need no flags::

    u64://host?dma_port=64
    tr://host?tcp_port=2113
    tr:///dev/cu.usbmodem?baud=2000000
    tr://?storage=usb

A target must not carry a username/password: none of these schemes has any
use for one (the Ultimate's REST API has no HTTP auth, and ``requests``
would otherwise send it as a Basic-auth header on every request), and a
secret belongs in ``C64CAST_DMA_PASSWORD`` or ``[ultimate64].dma_password``
instead — never in a string that ``--save-settings`` can write to disk or
echo to stdout. An unrecognized or blank ``?query`` key is also rejected
rather than silently ignored, matching the strictness a TOML config gets.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Protocol

# Windows serial ports look like a host in a URL (``tr://COM3`` -> netloc
# "COM3"), so they're matched here and routed to the serial transport instead
# of TCP. Unix serial nodes are always /dev/... paths (empty netloc), so they
# don't need this.
_COM_RE = re.compile(r"^COM\d+$", re.IGNORECASE)

# Recognized schemes, for the error message on an unknown one.
_SCHEMES = ("u64", "http", "https", "tr")


class ConnectionURIError(ValueError):
    """Raised when a connection target string can't be parsed. A ``ValueError``
    so the CLI's existing usage-error handling reports it (exit code 2)."""


@dataclass(frozen=True)
class ConnectionSpec:
    """The connection fields a target URI resolves to. ``backend`` is always
    set; every other field is None unless the URI carried it, so
    :func:`apply_to_config` overlays only what was specified and leaves the
    config's own defaults (or a TOML's values) in place otherwise."""

    backend: str  # "ultimate" | "teensyrom"
    # --- ultimate ---
    url: str | None = None
    dma_port: int | None = None
    # --- teensyrom ---
    transport: str | None = None  # "serial" | "tcp"
    serial_port: str | None = None
    host: str | None = None
    tcp_port: int | None = None
    baud: int | None = None
    storage: str | None = None


def _int_query(query: dict[str, str], key: str, *, target: str) -> int | None:
    """Parse an integer ``?key=`` query param, or None if absent."""
    raw = query.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise ConnectionURIError(f"{target!r}: query param {key}={raw!r} is not an integer") from e


def _check_known_query(query: dict[str, str], known: set[str], *, target: str) -> None:
    """Reject any ``?query`` key this scheme doesn't consume — a typo'd knob
    (``dmaport`` for ``dma_port``) would otherwise be parsed as absent and do
    nothing, with no diagnostic."""
    unknown = sorted(set(query) - known)
    if unknown:
        raise ConnectionURIError(
            f"{target!r}: unrecognized query param(s) {', '.join(unknown)} — "
            f"this scheme accepts: {', '.join(sorted(known)) or '(none)'}"
        )


def _netloc_port(parts: urllib.parse.SplitResult, target: str) -> int | None:
    """``parts.port``, or a :class:`ConnectionURIError` naming the bad netloc.

    ``urlsplit`` is lazy — ``.port`` only raises when read — so every branch
    that wants the netloc's port needs this same guard, not just ``tr://``."""
    try:
        return parts.port
    except ValueError as e:
        raise ConnectionURIError(f"{target!r}: bad port in {parts.netloc!r}") from e


def _reject_userinfo(parts: urllib.parse.SplitResult, target: str) -> None:
    """Refuse a ``user:pass@host`` netloc on every scheme.

    None of them has a use for it — the Ultimate's REST API has no HTTP auth
    of its own, and ``requests`` would send it as a Basic-auth header on every
    call regardless — so accepting it would only smuggle a secret into
    ``[ultimate64].url``, from which ``--save-settings`` both writes it to
    ``settings.toml`` and echoes it to stdout in plaintext."""
    if "@" in parts.netloc:
        raise ConnectionURIError(
            f"{target!r}: a connection target can't carry a username/password — "
            "put a secret in the C64CAST_DMA_PASSWORD env var or "
            "[ultimate64].dma_password instead"
        )


def _parse_tr(
    parts: urllib.parse.SplitResult, query: dict[str, str], target: str
) -> ConnectionSpec:
    """Resolve a ``tr://`` target to its serial/TCP transport + endpoint."""
    baud = _int_query(query, "baud", target=target)
    storage = query.get("storage")

    if not parts.netloc:
        # Serial. tr:// -> auto-detect (serial_port left None); tr:///dev/... ->
        # that explicit device node.
        _check_known_query(query, {"baud", "storage"}, target=target)
        return ConnectionSpec(
            backend="teensyrom",
            transport="serial",
            serial_port=parts.path or None,
            baud=baud,
            storage=storage,
        )

    if _COM_RE.match(parts.netloc):
        # Windows COM port. Use the netloc verbatim (urlsplit's .hostname would
        # lowercase it) and treat it as a serial device.
        _check_known_query(query, {"baud", "storage"}, target=target)
        return ConnectionSpec(
            backend="teensyrom",
            transport="serial",
            serial_port=parts.netloc,
            baud=baud,
            storage=storage,
        )

    # Non-empty, non-COM netloc -> raw TCP host[:port].
    port = _netloc_port(parts, target)
    tcp_port_query = _int_query(query, "tcp_port", target=target)
    _check_known_query(query, {"baud", "storage", "tcp_port"}, target=target)
    return ConnectionSpec(
        backend="teensyrom",
        transport="tcp",
        host=parts.hostname,
        tcp_port=port if port is not None else tcp_port_query,
        baud=baud,
        storage=storage,
    )


def parse_connection_uri(target: str) -> ConnectionSpec:
    """Parse a scheme-aware connection target into a :class:`ConnectionSpec`.

    Raises :class:`ConnectionURIError` (a ``ValueError``) on an empty string, a
    missing/unknown scheme, a malformed component, embedded userinfo, or an
    unrecognized/blank ``?query`` key."""
    target = target.strip()
    if not target:
        raise ConnectionURIError("empty connection target")
    parts = urllib.parse.urlsplit(target)
    scheme = parts.scheme.lower()
    _reject_userinfo(parts, target)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    blank = sorted(k for k, v in query.items() if v == "")
    if blank:
        raise ConnectionURIError(f"{target!r}: empty value for query param(s) {', '.join(blank)}")

    if scheme in ("http", "https"):
        _netloc_port(parts, target)
        _check_known_query(query, {"dma_port"}, target=target)
        # The Ultimate is the only HTTP-speaking backend. Rebuild without the
        # query/fragment — ``target`` passed through whole would leave
        # ``?dma_port=64`` inside the base URL that Ultimate64API concatenates
        # every REST path onto.
        return ConnectionSpec(
            backend="ultimate",
            url=urllib.parse.urlunsplit((scheme, parts.netloc, parts.path, "", "")),
            dma_port=_int_query(query, "dma_port", target=target),
        )

    if scheme == "u64":
        if not parts.netloc:
            raise ConnectionURIError(f"{target!r}: u64:// needs a host (e.g. u64://192.168.2.64)")
        _netloc_port(parts, target)
        _check_known_query(query, {"dma_port"}, target=target)
        return ConnectionSpec(
            backend="ultimate",
            url=f"http://{parts.netloc}",
            dma_port=_int_query(query, "dma_port", target=target),
        )

    if scheme == "tr":
        return _parse_tr(parts, query, target)

    if not scheme:
        raise ConnectionURIError(
            f"{target!r}: connection target needs a scheme — "
            f"{', '.join(s + '://' for s in _SCHEMES)} "
            "(e.g. u64://192.168.2.64, tr://, or tr:///dev/cu.usbmodem1234)"
        )
    raise ConnectionURIError(
        f"{target!r}: unknown scheme {scheme!r}:// — known schemes: "
        f"{', '.join(s + '://' for s in _SCHEMES)}"
    )


class _Hardware(Protocol):
    backend: str


class _Ultimate64(Protocol):
    url: str
    dma_port: int


class _Teensyrom(Protocol):
    transport: str
    serial_port: str | None
    host: str | None
    tcp_port: int
    baud: int
    storage: str


class _Cfg(Protocol):
    """The shape ``apply_to_config`` needs — a structural stand-in for
    ``config.Config`` so this module stays import-free of the ``config``
    module (see the module docstring) without giving up type-checking: a
    config-side rename of any of these fields now fails pyright/mypy at the
    call site instead of succeeding as a silent ``setattr`` of a dead
    attribute on a plain dataclass.

    These three are ``@property`` (read-only) rather than plain attributes:
    ``apply_to_config`` only ever mutates *fields of* ``hardware`` /
    ``ultimate64`` / ``teensyrom``, never replaces the sub-object itself, and
    a plain (read-write) Protocol attribute is invariant — mypy then refuses
    `Config` here because a *different* concrete class could satisfy
    ``_Hardware`` without literally being one. Read-only members are
    covariant, which is all this actually needs."""

    @property
    def hardware(self) -> _Hardware: ...
    @property
    def ultimate64(self) -> _Ultimate64: ...
    @property
    def teensyrom(self) -> _Teensyrom: ...


def apply_to_config(cfg: _Cfg, spec: ConnectionSpec) -> None:
    """Overlay a parsed :class:`ConnectionSpec` onto a Config in place.

    ``cfg`` is duck-typed against :class:`_Cfg` (this module stays free of a
    config import): it must expose ``.hardware``, ``.ultimate64`` and
    ``.teensyrom`` sub-objects with the matching attributes. Only the spec's
    non-None fields are written, so a bare ``tr://`` leaves ``serial_port`` at
    its default (None) for make_backend's auto-detect, and rare knobs absent
    from the URI keep the config/TOML values."""
    cfg.hardware.backend = spec.backend
    u64 = cfg.ultimate64
    tr = cfg.teensyrom
    if spec.url is not None:
        u64.url = spec.url
    if spec.dma_port is not None:
        u64.dma_port = spec.dma_port
    if spec.transport is not None:
        tr.transport = spec.transport
    if spec.serial_port is not None:
        tr.serial_port = spec.serial_port
    if spec.host is not None:
        tr.host = spec.host
    if spec.tcp_port is not None:
        tr.tcp_port = spec.tcp_port
    if spec.baud is not None:
        tr.baud = spec.baud
    if spec.storage is not None:
        tr.storage = spec.storage
