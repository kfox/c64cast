"""Camera enumeration + name / USB ``VID:PID`` device selection.

The webcam ``[video].device`` was historically a bare ``cv2`` integer index.
That index is unstable across reboots/replugs and opaque (which capture stick is
index 1 today?). This module lets ``device`` also be a **string** resolved by
camera *name substring* or *USB ``VID:PID``* — the same "identify hardware by its
USB identity, not by an OS-assigned slot" idea the TeensyROM+ serial auto-detect
already uses (:func:`c64cast.teensyrom_dma.autodetect_serial_port`), applied to
the video-input side.

Enumeration (name + VID/PID + the *correct backend index*) comes from the
optional ``cv2-enumerate-cameras`` package (the ``camera`` extra). Everything
here degrades gracefully when it is absent: integer indices keep working exactly
as before, ``--list-devices`` falls back to its probe, and a *string* device
raises an actionable "install the ``camera`` extra" error.

Design mirrors :mod:`c64cast.teensyrom_dma`: a lazy import behind a best-effort
enumerator, a pure duck-typed matcher (VID/PID primary, name substring
fallback), and a resolver that warns (never silently guesses) on ambiguity.

Backend/apiPreference correctness: the enumerated index is only valid for the
backend it was queried with (macOS → ``CAP_AVFOUNDATION``). A string-resolved
device therefore reports the matched backend so the caller opens
``cv2.VideoCapture(index, backend)``; a plain-int device reports ``None`` so the
caller keeps the historical single-arg ``CAP_ANY`` open (byte-identical
behavior for existing configs).
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from dataclasses import dataclass

import cv2  # hard dependency — the CAP_* backend constants live here

log = logging.getLogger(__name__)

# A USB VID:PID token: two 1-4 digit hex halves, e.g. "0fd9:0066" (Elgato Cam
# Link 4K). Case-insensitive.
_VIDPID_RE = re.compile(r"^([0-9a-fA-F]{1,4}):([0-9a-fA-F]{1,4})$")

_EXTRA_HINT = (
    "install the 'camera' extra: uv sync --extra camera (or pip install 'c64cast[camera]')"
)

# Cache of importlib.util.find_spec — cheap, but this is hit per-resolve.
_ENUM_AVAILABLE: bool | None = None


@dataclass
class CameraInfo:
    """One enumerated camera. ``backend`` is the ``cv2.CAP_*`` apiPreference the
    ``index`` is valid for — pass it back to ``cv2.VideoCapture(index, backend)``
    so the index resolves against the same backend it was enumerated with."""

    index: int
    name: str
    vid: int | None
    pid: int | None
    backend: int

    def vidpid_str(self) -> str | None:
        """``"vvvv:pppp"`` (lowercase, zero-padded) when both IDs are known."""
        if self.vid is None or self.pid is None:
            return None
        return f"{self.vid:04x}:{self.pid:04x}"


def _platform_api_preference() -> int:
    """The ``cv2.CAP_*`` backend to enumerate against for this platform.

    macOS → AVFoundation, Windows → Media Foundation, else ``CAP_ANY`` (Linux
    lets the package pick V4L2/GStreamer). Only affects *which* backend we
    enumerate; the caller always opens with the per-camera ``backend`` reported
    on :class:`CameraInfo`, so the index stays consistent regardless."""
    if sys.platform == "darwin":
        return int(cv2.CAP_AVFOUNDATION)
    if sys.platform == "win32":
        return int(cv2.CAP_MSMF)
    return int(cv2.CAP_ANY)


def camera_enumeration_available() -> bool:
    """True if the optional ``cv2-enumerate-cameras`` package is importable.
    Uses ``find_spec`` (no import side effects) so it is safe offline and cheap
    to call from ``--doctor``/resolve. Result is cached."""
    global _ENUM_AVAILABLE
    if _ENUM_AVAILABLE is None:
        try:
            _ENUM_AVAILABLE = importlib.util.find_spec("cv2_enumerate_cameras") is not None
        except (ImportError, ValueError):  # pragma: no cover - defensive
            _ENUM_AVAILABLE = False
    return _ENUM_AVAILABLE


def enumerate_cameras() -> list[CameraInfo]:
    """Enumerate connected cameras as :class:`CameraInfo`, best-effort.

    Returns ``[]`` when the ``camera`` extra is absent or enumeration fails
    (logged at debug) — same never-raises contract as
    :func:`c64cast.teensyrom_dma._list_comports`. Reads the package's result via
    ``getattr`` so a fake list can drive tests without the extra installed."""
    try:
        from cv2_enumerate_cameras import enumerate_cameras as _enum
    except ImportError as e:
        log.debug("camera enumeration unavailable (%s): %s", _EXTRA_HINT, e)
        return []
    try:
        raw = _enum(_platform_api_preference())
    except Exception as e:  # pragma: no cover - defensive; enumerates the OS
        log.debug("camera enumeration failed: %s", e)
        return []
    out: list[CameraInfo] = []
    for info in raw:
        out.append(
            CameraInfo(
                index=int(getattr(info, "index", -1)),
                name=str(getattr(info, "name", "") or ""),
                vid=getattr(info, "vid", None),
                pid=getattr(info, "pid", None),
                backend=int(getattr(info, "backend", _platform_api_preference())),
            )
        )
    return out


def _parse_vidpid(token: str) -> tuple[int, int] | None:
    """``(vid, pid)`` if ``token`` is a ``VID:PID`` hex pair, else ``None``."""
    m = _VIDPID_RE.match(token)
    if not m:
        return None
    return (int(m.group(1), 16), int(m.group(2), 16))


def _looks_like_vidpid_attempt(token: str) -> bool:
    """A spaceless token containing ``:`` is *intended* as a ``VID:PID`` (a real
    camera name that is spaceless and colon-bearing is vanishingly rare) — so a
    malformed one (``0fzz:0066``) is a hard error rather than a silent
    name-substring miss."""
    return ":" in token and not any(c.isspace() for c in token)


def parse_camera_device(value: int | str, *, field_name: str) -> None:
    """Offline syntax check for a ``[video].device`` value. Raises ``ConfigError``
    on a malformed ``VID:PID``; everything else (an int, an int-in-a-string, a
    name substring, a valid ``VID:PID``) passes. Does **not** enumerate hardware
    — actual resolution happens at :func:`resolve_camera_index` (runtime). Models
    :func:`c64cast.config.parse_wled_endpoint` (pure, ``field_name`` threaded into
    every message)."""
    if isinstance(value, int):
        return
    token = str(value).strip()
    if not token:
        from .config import ConfigError  # lazy: avoid config<->camera import cycle

        raise ConfigError(f"{field_name}: empty camera device string")
    if _looks_like_vidpid_attempt(token) and _parse_vidpid(token) is None:
        from .config import ConfigError

        raise ConfigError(
            f"{field_name}: {token!r} looks like a USB VID:PID but isn't two hex "
            "values (e.g. 0fd9:0066)"
        )
    # Plain name substring or an int-in-a-string — both always syntactically OK.


def _describe(cams: list[CameraInfo]) -> str:
    """One-line summary of enumerated cameras for error messages."""
    if not cams:
        return "(none enumerated)"
    parts = []
    for c in cams:
        vp = c.vidpid_str()
        parts.append(f"[{c.index}] {c.name}" + (f" ({vp})" if vp else ""))
    return ", ".join(parts)


def resolve_camera_index(device: int | str) -> tuple[int, int | None]:
    """Resolve a ``[video].device`` value to ``(cv2_index, backend_or_None)``.

    - ``int`` (incl. ``-1`` → 0) → ``(index, None)`` — the historical ``CAP_ANY``
      single-arg open, unchanged.
    - int-in-a-string (``"0"``) → treated as that index.
    - name substring or ``VID:PID`` → enumerate + match; returns the matched
      camera's index and the backend it was enumerated with.

    Raises ``RuntimeError`` (actionable message) when the ``camera`` extra is
    missing or no camera matches. Warns and takes the first on multiple matches
    (mirrors :func:`c64cast.teensyrom_dma.autodetect_serial_port`)."""
    if isinstance(device, int):
        return (0 if device < 0 else device, None)
    token = device.strip()
    try:
        idx = int(token)
    except ValueError:
        pass
    else:
        return (0 if idx < 0 else idx, None)

    if not camera_enumeration_available():
        raise RuntimeError(
            f"selecting a camera by name/VID:PID ({token!r}) needs the "
            f"'camera' extra — {_EXTRA_HINT}. Or use an integer index."
        )
    cams = enumerate_cameras()
    vidpid = _parse_vidpid(token)
    if vidpid is not None:
        vid, pid = vidpid
        matches = [c for c in cams if c.vid == vid and c.pid == pid]
    else:
        low = token.lower()
        matches = [c for c in cams if low in c.name.lower()]

    if not matches:
        raise RuntimeError(
            f"no camera matched device {token!r}. Available: {_describe(cams)}. "
            "Run `c64cast --list-devices` to see names + VID:PID."
        )
    if len(matches) > 1:
        log.warning(
            "camera device %r matched %d cameras (%s) — using [%d] %s; "
            "narrow it with a VID:PID or a more specific name",
            token,
            len(matches),
            _describe(matches),
            matches[0].index,
            matches[0].name,
        )
    chosen = matches[0]
    log.info(
        "resolved camera device %r -> index %d (%s%s)",
        token,
        chosen.index,
        chosen.name,
        f", {chosen.vidpid_str()}" if chosen.vidpid_str() else "",
    )
    return (chosen.index, chosen.backend)
