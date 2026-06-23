"""Connection-target URI parsing.

A single scheme-aware connection string selects the hardware backend *and* its
transport/endpoint, so the CLI needs only ``-u/--url`` (or the ``C64CAST_URL``
env var) to point c64cast at any supported device. It decomposes into the
existing config fields — ``[hardware].backend``, ``[ultimate64].url`` /
``dma_port``, ``[teensyrom].transport`` / ``serial_port`` / ``host`` /
``tcp_port`` / ``baud`` / ``storage`` — which remain the canonical store a TOML
config sets directly. This module is the CLI/env front-end that fills them in;
:func:`c64cast.backend.make_backend` reads them unchanged.

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
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

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


def _parse_tr(
    parts: urllib.parse.SplitResult, query: dict[str, str], target: str
) -> ConnectionSpec:
    """Resolve a ``tr://`` target to its serial/TCP transport + endpoint."""
    baud = _int_query(query, "baud", target=target)
    storage = query.get("storage")

    if not parts.netloc:
        # Serial. tr:// -> auto-detect (serial_port left None); tr:///dev/... ->
        # that explicit device node.
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
        return ConnectionSpec(
            backend="teensyrom",
            transport="serial",
            serial_port=parts.netloc,
            baud=baud,
            storage=storage,
        )

    # Non-empty, non-COM netloc -> raw TCP host[:port].
    try:
        port = parts.port
    except ValueError as e:
        raise ConnectionURIError(f"{target!r}: bad port in {parts.netloc!r}") from e
    return ConnectionSpec(
        backend="teensyrom",
        transport="tcp",
        host=parts.hostname,
        tcp_port=port or _int_query(query, "tcp_port", target=target),
        baud=baud,
        storage=storage,
    )


def parse_connection_uri(target: str) -> ConnectionSpec:
    """Parse a scheme-aware connection target into a :class:`ConnectionSpec`.

    Raises :class:`ConnectionURIError` (a ``ValueError``) on an empty string, a
    missing/unknown scheme, or a malformed component."""
    target = target.strip()
    if not target:
        raise ConnectionURIError("empty connection target")
    parts = urllib.parse.urlsplit(target)
    scheme = parts.scheme.lower()
    query = dict(urllib.parse.parse_qsl(parts.query))

    if scheme in ("http", "https"):
        # The Ultimate is the only HTTP-speaking backend; pass the URL through
        # verbatim (the REST client wants the full scheme://host).
        return ConnectionSpec(
            backend="ultimate", url=target, dma_port=_int_query(query, "dma_port", target=target)
        )

    if scheme == "u64":
        if not parts.netloc:
            raise ConnectionURIError(f"{target!r}: u64:// needs a host (e.g. u64://192.168.2.64)")
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


def apply_to_config(cfg: object, spec: ConnectionSpec) -> None:
    """Overlay a parsed :class:`ConnectionSpec` onto a Config in place.

    ``cfg`` is duck-typed (this module stays free of a config import): it must
    expose ``.hardware``, ``.ultimate64`` and ``.teensyrom`` sub-objects with
    the matching attributes. Only the spec's non-None fields are written, so a
    bare ``tr://`` leaves ``serial_port`` at its default (None) for
    make_backend's auto-detect, and rare knobs absent from the URI keep the
    config/TOML values."""
    cfg.hardware.backend = spec.backend  # type: ignore[attr-defined]
    u64 = cfg.ultimate64  # type: ignore[attr-defined]
    tr = cfg.teensyrom  # type: ignore[attr-defined]
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
