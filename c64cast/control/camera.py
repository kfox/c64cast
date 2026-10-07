"""Camera enumeration + name / USB ``VID:PID`` device selection.

Lets ``[video].device`` be a **string** matched to a camera by name substring
or USB ``VID:PID``, not only a ``cv2`` integer index. Enumeration comes from
the optional ``cv2-enumerate-cameras`` package (the ``camera`` extra); integer
indices keep working without it.

See docs/architecture/control.md#camerapy--camera-enumeration--namevidpid-device-selection-optional-camera-extra.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from dataclasses import dataclass

import cv2  # hard dependency — the CAP_* backend constants live here

log = logging.getLogger(__name__)

# A USB VID:PID token, e.g. "0fd9:0066" (Elgato Cam Link 4K).
_VIDPID_RE = re.compile(r"^([0-9a-fA-F]{1,4}):([0-9a-fA-F]{1,4})$")

_EXTRA_HINT = "install the 'camera' extra: uv tool install --force 'c64cast[all]'"

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

    macOS → AVFoundation, Windows → Media Foundation, else ``CAP_ANY``, for
    which the package lists each Linux camera once per backend it supports
    (GStreamer, V4L2) at OpenCV's ``backend + N`` index, with ``backend``
    reported as ``CAP_ANY``. Only affects *which* backend we
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
    :func:`c64cast.hw.teensyrom_dma._list_comports`. Reads the package's result via
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
    :func:`c64cast.app.scene_factory.parse_wled_endpoint` (pure, ``field_name`` threaded into
    every message)."""
    if isinstance(value, int):
        return
    token = str(value).strip()
    if not token:
        from c64cast.app.config import ConfigError  # lazy: avoid config<->camera import cycle

        raise ConfigError(f"{field_name}: empty camera device string")
    if _looks_like_vidpid_attempt(token) and _parse_vidpid(token) is None:
        from c64cast.app.config import ConfigError

        raise ConfigError(
            f"{field_name}: {token!r} looks like a USB VID:PID but isn't two hex "
            "values (e.g. 0fd9:0066)"
        )


def _describe(cams: list[CameraInfo]) -> str:
    """One-line summary of enumerated cameras for error messages."""
    if not cams:
        return "(none enumerated)"
    parts = []
    for c in cams:
        vp = c.vidpid_str()
        parts.append(f"[{c.index}] {c.name}" + (f" ({vp})" if vp else ""))
    return ", ".join(parts)


#: Name substrings (lowercase) of video devices that are not HDMI capture
#: devices: built-in and USB webcams, phones, and virtual cameras. Webcams,
#: built-in cameras, phones and virtual devices mostly call themselves a
#: "camera"; an HDMI capture device names itself after the stick.
NOT_CAPTURE_NAME_PATTERNS: tuple[str, ...] = (
    "camera",
    # webcams
    "webcam",
    "facecam",
    "lifecam",
    "brio",
    "kiyo",
    "insta360",
    "obs",
    # phones
    "iphone",
    "ipad",
    "epoccam",
    "droidcam",
    "camo",
    # virtual cameras
    "virtual",
    "xsplit",
    "mmhmm",
    "broadcast",
    "screen",
)


def looks_like_hdmi_capture(name: str, usb_id: str | None) -> bool:
    """Whether a device called ``name``, with USB ``VID:PID`` ``usb_id``
    (``None`` when it reports none), may be auto-picked as the HDMI capture
    device: it must be a USB device with a non-empty name that matches none of
    :data:`NOT_CAPTURE_NAME_PATTERNS`. A device with no USB identity, or no
    name to judge, is not picked: built-in and virtual cameras report none."""
    lowered = name.strip().lower()
    if not lowered or not usb_id:
        return False
    return not any(pattern in lowered for pattern in NOT_CAPTURE_NAME_PATTERNS)


def camera_listing(cams: list[CameraInfo]) -> str:
    """Every enumerated camera, one per line, with its index, name and VID:PID."""
    if not cams:
        return "  (no cameras found)"
    return "\n".join(f"  [{c.index}] {c.name} ({c.vidpid_str() or 'no USB VID:PID'})" for c in cams)


class CaptureCameraError(RuntimeError):
    """No single connected camera can be picked as the HDMI capture device.

    ``extra_missing`` is true when the ``camera`` extra that enumerates them is
    not installed; otherwise ``cameras`` is what was enumerated, so the caller
    can list it beside its own advice on naming a device."""

    def __init__(self, reason: str, cameras: list[CameraInfo], *, extra_missing: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.cameras = cameras
        self.extra_missing = extra_missing


def pick_capture_camera() -> CameraInfo:
    """The one connected camera :func:`looks_like_hdmi_capture` accepts.

    Raises :class:`CaptureCameraError` when it accepts none or more than one,
    or when the ``camera`` extra is missing: there is no fallback to an index
    or to any other camera, since that camera may be pointed at a person.
    Enumerating opens no camera."""
    if not camera_enumeration_available():
        raise CaptureCameraError(
            f"picking the capture device needs the 'camera' extra — {_EXTRA_HINT}",
            [],
            extra_missing=True,
        )
    cams = enumerate_cameras()
    picked = [c for c in cams if looks_like_hdmi_capture(c.name, c.vidpid_str())]
    devices = {device_identity(c) for c in picked}
    if len(devices) == 1:
        return _listing_to_open(picked)
    reason = (
        "no connected camera looks like an HDMI capture device"
        if not picked
        else f"{len(devices)} connected cameras look like HDMI capture devices"
    )
    raise CaptureCameraError(reason, cams)


def device_identity(cam: CameraInfo) -> tuple[str, str | None, int]:
    """What one physical camera has in common across its listings: on Linux
    each backend lists it at ``backend + N`` (see
    :func:`_platform_api_preference`), and OpenCV reads ``N`` back as
    ``index % 100``."""
    return (cam.name, cam.vidpid_str(), cam.index % 100)


def _listing_to_open(listings: list[CameraInfo]) -> CameraInfo:
    """Which of one camera's listings to open it by: the V4L2 one when the
    enumerator listed it per Linux backend, since OpenCV opens ``backend + N``
    under ``CAP_ANY`` with that backend and its pip wheels lack GStreamer;
    otherwise the first."""
    for cam in listings:
        if cam.backend == int(cv2.CAP_ANY) and cam.index - cam.index % 100 == int(cv2.CAP_V4L2):
            return cam
    return listings[0]


def resolve_camera_index(device: int | str) -> tuple[int, int | None]:
    """Resolve a ``[video].device`` value to ``(cv2_index, backend_or_None)``.

    - ``int`` (incl. ``-1`` → 0) → ``(index, None)`` — the historical ``CAP_ANY``
      single-arg open, unchanged.
    - int-in-a-string (``"0"``) → treated as that index.
    - name substring or ``VID:PID`` → enumerate + match; returns the matched
      camera's index and the backend it was enumerated with.

    Raises ``RuntimeError`` (actionable message) when the ``camera`` extra is
    missing or no camera matches. Warns and takes the first camera when several
    match (mirrors :func:`c64cast.hw.teensyrom_dma.autodetect_serial_port`),
    counting a Linux camera's per-backend listings as one and returning its
    V4L2 listing when it has one."""
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
    first = device_identity(matches[0])
    chosen = _listing_to_open([c for c in matches if device_identity(c) == first])
    devices = {device_identity(c) for c in matches}
    if len(devices) > 1:
        log.warning(
            "camera device %r matched %d cameras (%s) — using [%d] %s; "
            "narrow it with a VID:PID or a more specific name",
            token,
            len(devices),
            _describe(matches),
            chosen.index,
            chosen.name,
        )
    log.info(
        "resolved camera device %r -> index %d (%s%s)",
        token,
        chosen.index,
        chosen.name,
        f", {chosen.vidpid_str()}" if chosen.vidpid_str() else "",
    )
    return (chosen.index, chosen.backend)
