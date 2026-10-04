"""Shared helpers for the c64cast diagnostic tools in this directory.

Every tool in ``scripts/diags/`` imports from here so that path handling,
hardware defaults, and the U64 REST shims are solved once instead of being
re-derived (often wrongly) in each one-off script. The recurring pain points
this module exists to kill:

* **Project home.** ``import c64cast`` must work no matter what the cwd is.
  Importing this module inserts the repo root onto ``sys.path``.
* **Stable output paths.** Captures land under ``scripts/diags/out/`` (git
  ignored), not a coin-flip between ``/tmp`` and ``/private/tmp``.
* **Hardware indices drift.** The cv2 camera indices, the avfoundation audio
  index and the U64 URL all shift with hotplug + DHCP, so every default here
  is overridable by env var (and the tools expose matching CLI flags), and a
  capture device nobody named is picked by what it is, never by an index.
  Its audio input is the one named like it; no tool records from the
  system default input.

The values below are *defaults*, not ground truth: they are one rig's
working values, confirmed as of 2026-06-10. Point the env vars at yours.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "scripts" / "diags" / "out"

# Make `import c64cast` work regardless of cwd / how the tool was launched.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def out_dir() -> Path:
    """Return (creating if needed) the git-ignored capture output directory."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR


def stamped(name: str, ext: str) -> Path:
    """An ``out/``-relative path tagged with a wallclock stamp, e.g.
    ``out/frame_20260610-143002.png`` — so repeated runs don't clobber."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return out_dir() / f"{name}_{ts}.{ext}"


#: Default longest-edge (px) for verification captures written via ``save_image``.
#: The Cam Link grabs 1080p, but the C64 active area is only 320x200 — a frame
#: scaled to ~960px still resolves individual glyphs / per-cell color / tearing,
#: while costing a fraction of the image tokens a full 1080p PNG does when read
#: back into an agent's context. Pixel-peeping (fine bottom-row glyph shimmer)
#: can opt back to native with ``save_image(..., max_width=0)`` / a tool ``--full``.
DEFAULT_VERIFY_WIDTH = int(os.environ.get("C64_DIAG_VERIFY_WIDTH", "960"))


def save_image(frame, path, *, max_width: int = DEFAULT_VERIFY_WIDTH) -> tuple[int, int]:
    """Write ``frame`` (a cv2 BGR ndarray) to ``path``, downscaled so its longest
    edge is at most ``max_width`` px (``0`` = keep native). Returns the written
    ``(w, h)``. Use this instead of a bare ``cv2.imwrite`` for any capture an
    agent will Read back — a half-size frame is enough to verify what the VIC
    rendered and keeps captures from dominating the context window."""
    import cv2  # local import: keep module import cheap for non-capture tools

    h, w = frame.shape[:2]
    longest = max(w, h)
    if max_width and longest > max_width:
        scale = max_width / longest
        frame = cv2.resize(
            frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA
        )
        h, w = frame.shape[:2]
    cv2.imwrite(str(path), frame)
    return w, h


#: Ultimate 64. Override: C64_DIAG_URL.
U64_URL = os.environ.get("C64_DIAG_URL", "http://192.168.2.64")
#: Ultimate II+ on the same LAN. Override: C64_DIAG_U2P_URL.
U2P_URL = os.environ.get("C64_DIAG_U2P_URL", "http://192.168.2.65")


def capture_device_from_env() -> str | None:
    """The capture device the environment names, or ``None`` to auto-pick.

    ``$C64_DIAG_CAMERA`` takes an index, a name substring or a ``VID:PID``.
    The removed index-only ``$C64_DIAG_CV2`` stops the tool when it is set,
    so a value someone still exports is not silently ignored."""
    reject_removed_env()
    camera = os.environ.get("C64_DIAG_CAMERA", "").strip()
    return camera or None


def reject_removed_env() -> None:
    """Raise ``SystemExit`` when ``$C64_DIAG_CV2``, which was removed, is set."""
    legacy = os.environ.get("C64_DIAG_CV2", "").strip()
    if legacy:
        raise SystemExit(
            f"C64_DIAG_CV2={legacy!r} was removed. Unset it, and set C64_DIAG_CAMERA "
            "to the capture device's index, name or VID:PID instead."
        )


def add_capture_device_arg(parser: argparse.ArgumentParser, *aliases: str) -> None:
    """Add the capture-device option every capture tool shares, as ``--device``.

    ``aliases`` are extra option strings for the same value, such as ``-d`` or
    a tool's older ``--cv2-index``, so existing invocations keep working. The
    value is an index, a camera name substring or a USB ``VID:PID``; leaving it
    out stores ``None``, which :func:`open_capture` reads as "the environment's
    device, else the auto-picked one", never as an index."""
    short = [a for a in aliases if not a.startswith("--")]
    long = [a for a in aliases if a.startswith("--")]
    parser.add_argument(
        *short,
        "--device",
        *long,
        dest="device",
        default=None,
        metavar="DEVICE",
        help="capture device: a cv2 index, a camera name substring, or a USB VID:PID "
        "(default: $C64_DIAG_CAMERA, else the one connected camera that looks like "
        "an HDMI capture device; see `c64cast --list-devices`)",
    )


def autopick_capture() -> tuple[int, int]:
    """``(cv2_index, backend)`` of the camera the app's own
    :func:`c64cast.control.camera.pick_capture_camera` singles out.

    Raises ``SystemExit``, listing every camera found, when it accepts none or
    more than one, or when the ``camera`` extra is missing: no camera is opened
    that nobody named and the classifier did not single out, since it may be
    pointed at a person."""
    from c64cast.control import camera

    try:
        chosen = camera.pick_capture_camera()
    except camera.CaptureCameraError as e:
        if e.extra_missing:
            raise SystemExit(
                "picking the capture device needs the 'camera' extra: run `uv sync "
                "--all-extras`, or name one with --device. No camera is opened by index "
                "in its place."
            ) from None
        raise SystemExit(
            f"{e.reason}, so none is opened. Cameras found:\n{camera.camera_listing(e.cameras)}\n"
            "Choose one with --device NAME|VID:PID, or set C64_DIAG_CAMERA."
        ) from None
    print(
        f"[capture] auto-picked [{chosen.index}] {chosen.name} ({chosen.vidpid_str()})",
        file=sys.stderr,
    )
    return chosen.index, chosen.backend


def autopick_webcam() -> str:
    """The name of the one connected camera :func:`looks_like_hdmi_capture`
    rejects, for a tool that films a person rather than the C64.

    Raises ``SystemExit``, listing every camera found, when there is none,
    more than one, or one whose name also matches another camera's (the name
    is what the caller opens it by), or when the ``camera`` extra is missing."""
    from c64cast.control import camera

    if not camera.camera_enumeration_available():
        raise SystemExit(
            "picking the webcam needs the 'camera' extra: run `uv sync --all-extras`, "
            "or name one with --device."
        )
    cams = camera.enumerate_cameras()
    picked = [c for c in cams if not camera.looks_like_hdmi_capture(c.name, c.vidpid_str())]
    if len(picked) == 1:
        name = picked[0].name
        if sum(name.lower() in c.name.lower() for c in cams) == 1:
            print(f"[camera] auto-picked [{picked[0].index}] {name}", file=sys.stderr)
            return name
        reason = f"the webcam's name {name!r} also matches another camera"
    elif not picked:
        reason = "every connected camera looks like an HDMI capture device"
    else:
        reason = f"{len(picked)} connected cameras do not look like HDMI capture devices"
    raise SystemExit(
        f"{reason}, so none is opened. Cameras found:\n{camera.camera_listing(cams)}\n"
        "Choose one with --device NAME|VID:PID|INDEX."
    )


def resolve_capture(device: int | str | None) -> tuple[int, int | None]:
    """Resolve a capture-device value to ``(cv2_index, backend_or_None)``.

    ``None`` means :func:`capture_device_from_env`, and when that names nothing
    either, :func:`autopick_capture`. A named device resolves through the app's
    own :func:`c64cast.control.camera.resolve_camera_index` rather than a second
    copy of the matcher, so a diag tool and a ``[video].device`` in a config
    pick the same stick from the same string — including the **backend** the
    matched index is only valid against (an AVFoundation index opened with
    ``CAP_ANY`` is some other camera).

    Raises ``SystemExit`` when nothing matches: there is no fallback to an
    index or to any other camera, since that camera may be pointed at a
    person."""
    from c64cast.control import camera

    reject_removed_env()
    spec = capture_device_from_env() if device is None else device
    if spec is None:
        return autopick_capture()
    try:
        return camera.resolve_camera_index(spec)
    except RuntimeError as e:
        if not camera.camera_enumeration_available():
            raise SystemExit(
                f"finding capture device {spec!r} by name or VID:PID needs the 'camera' "
                "extra: run `uv sync --all-extras`. No camera is opened by index in "
                "its place."
            ) from e
        raise SystemExit(str(e)) from e


def open_capture(device: int | str | None):
    """Open ``device`` as a cv2 capture, resolved by :func:`resolve_capture`.

    Returns the opened ``cv2.VideoCapture``; the caller releases it. Raises
    ``SystemExit`` on a device that resolves to nothing or will not open, since
    every caller here is a command-line tool."""
    import cv2  # local import: opencv is a hard dep but keep tool import cheap

    index, backend = resolve_capture(device)
    cap = cv2.VideoCapture(index) if backend is None else cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        raise SystemExit(
            f"could not open {describe_capture_device(device)} (cv2 index {index}). "
            f"Run `c64cast --list-devices` for names + VID:PID, or set C64_DIAG_CAMERA."
        )
    return cap


#: Seconds :func:`read_frame` keeps asking an open capture for a frame before it
#: gives up. A capture stick whose HDMI input is renegotiating answers each read
#: with nothing (on the Cam Link, after about a second) until the link settles,
#: and a single failed read is not evidence that the device is gone.
NO_FRAME_RETRY_S = 5.0
#: Pause between failed reads, so a device that fails instantly is not spun on.
NO_FRAME_POLL_S = 0.05


class NoFrameError(RuntimeError):
    """An open capture device returned no frame within the retry window."""


def describe_capture_device(device: int | str | None) -> str:
    """How an error message names ``device``; ``None`` is the default device."""
    if device is None:
        return "the default capture device"
    return f"capture device {device!r}"


def no_frame_message(device: int | str | None, what: str) -> str:
    """The error text for a capture that returned no frame: ``what`` says how
    long or where, and the rest names the causes worth checking first."""
    return (
        f"{describe_capture_device(device)} returned no frame {what}. Likely causes: the "
        "HDMI link is renegotiating (after a video-mode change on the machine "
        "this has lasted from seconds to over a minute; rerun once it settles), "
        "the source sends no signal, or "
        "another program holds the device. `c64cast --list-devices` shows "
        "whether the device is still listed."
    )


def read_frame(cap, device: int | str | None, *, timeout_s: float = NO_FRAME_RETRY_S):
    """Read one frame from the open capture ``cap``, retrying failed reads for
    up to ``timeout_s`` seconds. ``device`` is only used in the error message.

    Raises :class:`NoFrameError` when no read succeeds in that window."""
    deadline = time.monotonic() + timeout_s
    while True:
        ok, frame = cap.read()
        if ok and frame is not None:
            return frame
        if time.monotonic() >= deadline:
            raise NoFrameError(no_frame_message(device, f"for {timeout_s:g}s"))
        time.sleep(NO_FRAME_POLL_S)


class AudioInput(NamedTuple):
    """An audio input a tool records from: ``device`` is what the backend
    takes (a sounddevice index, or ffmpeg avfoundation's ``:N``), ``name``
    is what the device calls itself, which outlives a re-enumeration, and
    ``hostapi`` is the PortAudio host API listing it (always 0 for
    avfoundation)."""

    device: int | str
    name: str
    hostapi: int = 0


#: The environment variable that names each backend's audio input.
AUDIO_ENV = {"sd": "C64_DIAG_SD_AUDIO", "avf": "C64_DIAG_AVF_AUDIO"}


def add_audio_device_arg(
    parser: argparse.ArgumentParser, *flags: str, dest: str, backend: str
) -> None:
    """Add the option that names a tool's audio input, as ``flags``.

    Leaving it out stores ``None``, which :func:`resolve_audio_input` reads as
    "the environment's input, else the one named like the capture camera"."""
    parser.add_argument(
        *flags,
        dest=dest,
        default=None,
        metavar="AUDIO",
        help="audio input: an index or a name substring (default: "
        f"${AUDIO_ENV[backend]}, else the input named like the capture camera)",
    )


def sd_audio_inputs() -> list[AudioInput]:
    """Every sounddevice device with an input channel."""
    import sounddevice as sd

    return [
        AudioInput(i, str(dev["name"]), int(dev.get("hostapi", 0)))
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] > 0
    ]


def avf_audio_inputs() -> list[AudioInput]:
    """Every ffmpeg avfoundation audio device, as ``(":N", name)``.

    Raises ``SystemExit`` when ffmpeg cannot list them: no index is assumed in
    their place."""
    import re
    import subprocess

    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SystemExit(
            f"could not list the avfoundation audio inputs with ffmpeg ({e}); a named "
            "input is checked against that list too, so ffmpeg has to run first"
        ) from e
    inputs: list[AudioInput] = []
    audio = False
    for line in r.stderr.splitlines():
        if "audio devices" in line:
            audio = True
            continue
        if "video devices" in line:
            audio = False
            continue
        m = re.search(r"\[(\d+)\]\s+(.*\S)", line)
        if audio and m:
            inputs.append(AudioInput(f":{m.group(1)}", m.group(2)))
    return inputs


def _audio_listing(inputs: list[AudioInput]) -> str:
    if not inputs:
        return "  (no audio inputs found)"
    return "\n".join(f"  {a.device}: {a.name}" for a in inputs)


def capture_camera_name(camera: int | str | None) -> str:
    """The name of the camera :func:`resolve_capture` picks for ``camera``."""
    from c64cast.control import camera as cam

    index, backend = resolve_capture(camera)
    if not cam.camera_enumeration_available():
        raise SystemExit(
            "naming the capture camera needs the 'camera' extra: run `uv sync "
            "--all-extras`, or name the audio input yourself."
        )
    for c in cam.enumerate_cameras():
        if c.index == index and (backend is None or c.backend == backend):
            return c.name
    raise SystemExit(f"capture camera at cv2 index {index} has no name to match audio by")


def _named_audio_input(inputs: list[AudioInput], spec: str, backend: str) -> AudioInput:
    """The one input ``spec`` names: an index (``N``, or ``:N`` for
    avfoundation) or a name, exact first and then as a substring."""
    text = spec.strip()
    index = text.removeprefix(":") if backend == "avf" else text
    if index.isdigit():
        key: int | str = f":{index}" if backend == "avf" else int(index)
        for a in inputs:
            if a.device == key:
                return a
        raise SystemExit(
            f"audio input {spec!r} is not an audio input index. Inputs found:\n"
            + _audio_listing(inputs)
        )
    from c64cast.audio.dac_capture_device import named_positions, one_device

    matches = [inputs[i] for i in named_positions([a.name for a in inputs], text)]
    one = one_device(matches)
    if one is not None:
        return one
    reason = "matches no audio input" if not matches else "matches more than one audio input"
    raise SystemExit(f"audio input {spec!r} {reason}. Inputs found:\n" + _audio_listing(inputs))


def resolve_audio_input(
    backend: str, spec: int | str | None = None, *, camera: int | str | None = None
) -> AudioInput:
    """The audio input a tool records from, on ``backend`` (``"sd"`` for
    sounddevice, ``"avf"`` for ffmpeg avfoundation).

    ``spec``, else ``$C64_DIAG_SD_AUDIO`` / ``$C64_DIAG_AVF_AUDIO``, names it
    by index or name. With neither, it is the one input whose name matches the
    capture camera's: ``camera`` as :func:`resolve_capture` resolves it, which
    with no device named is the auto-picked HDMI capture device. Raises
    ``SystemExit`` with the inputs listed when that matches none or several:
    the system default input is never used in its place, since it is usually
    a microphone."""
    inputs = sd_audio_inputs() if backend == "sd" else avf_audio_inputs()
    if spec is None:
        spec = os.environ.get(AUDIO_ENV[backend], "").strip() or None
    if spec is not None:
        return _named_audio_input(inputs, str(spec), backend)
    flag_hint = f"Name one with -D or ${AUDIO_ENV[backend]}. Inputs found:\n"
    try:
        camera_name = capture_camera_name(camera)
    except SystemExit as e:
        raise SystemExit(
            f"{e}\nThe audio input is picked by the capture camera's name, so none is "
            "opened. " + flag_hint + _audio_listing(inputs)
        ) from None
    from c64cast.audio.dac_capture_device import names_match, one_device

    matches = [a for a in inputs if names_match(a.name, camera_name)]
    one = one_device(matches)
    if one is not None:
        print(
            f"[audio] picked {one.device} {one.name} (named like capture camera {camera_name!r})",
            file=sys.stderr,
        )
        return one
    reason = "no audio input" if not matches else "more than one audio input"
    raise SystemExit(
        f"{reason} named like capture camera {camera_name!r}, so none is opened. "
        + flag_hint
        + _audio_listing(inputs)
    )


def refind_sd_audio_input(audio: AudioInput) -> int:
    """The sounddevice index of ``audio`` once PortAudio has re-enumerated,
    found by its exact name in its own host API: another input whose name
    merely contains it is a different device, and the same name in another
    host API is the same device listed again. Among several inputs with that
    exact name in that host API, the one still at ``audio``'s index is taken.

    Raises ``SystemExit`` listing the inputs when the name is gone from that
    host API, or is shared there by several inputs none of which is at that
    index."""
    inputs = sd_audio_inputs()
    named = [
        a
        for a in inputs
        if a.hostapi == audio.hostapi and a.name.strip().lower() == audio.name.strip().lower()
    ]
    reason = "is gone" if not named else "now names more than one input"
    if len(named) > 1:
        named = [a for a in named if a.device == audio.device]
    if len(named) == 1:
        return int(named[0].device)
    raise SystemExit(
        f"audio input {audio.name!r} {reason} after the re-enumeration, so none is "
        "opened. Inputs found:\n" + _audio_listing(inputs)
    )


def python_exe() -> str:
    """The interpreter running this tool — use it to spawn ``-m c64cast``
    so the subprocess gets the same ``.venv`` rather than a stray system
    Python — mise sets ``UV_PYTHON`` to the bare toolchain interpreter, so a
    subprocess launched any other way can miss the project's installed
    extras and report them as unavailable."""
    return sys.executable


def rest_request(method: str, url: str, **kwargs):
    """One REST request sent the way c64cast sends it: ``C64CAST_DMA_PASSWORD``
    as ``X-Password`` when set, and no environment proxy handed that header.
    A fresh session per call, like the bare ``requests.get`` it replaces, so
    concurrent pollers share nothing."""
    from c64cast.hw.api import make_rest_session

    with make_rest_session(os.environ.get("C64CAST_DMA_PASSWORD")) as session:
        return session.request(method, url, **kwargs)


def rest_ping(url: str = U64_URL, timeout: float = 3.0) -> int | None:
    """GET / and return the HTTP status code, or None if unreachable."""
    import requests

    try:
        return rest_request("GET", url + "/", timeout=timeout).status_code
    except requests.RequestException:
        return None


def dma_service_up(url: str = U64_URL, timeout: float = 3.0) -> bool:
    """True if the Ultimate DMA Service TCP socket (port 64) accepts a
    connection. This is the service that must be enabled (F2 -> Network
    Settings) before c64cast will start."""
    import socket
    from urllib.parse import urlparse

    host = urlparse(url).hostname or url
    try:
        with socket.create_connection((host, 64), timeout=timeout):
            return True
    except OSError:
        return False


def rest_readmem(
    address: int, length: int, url: str = U64_URL, timeout: float = 1.0
) -> bytes | None:
    """GET /v1/machine:readmem?address=HHHH&length=N — raw bytes or None.

    A standalone shim (not via Ultimate64API) so a probe can poll memory over
    REST while c64cast owns the single-connection DMA socket — REST reads
    don't contend with the DMA writes. Address is sent WITHOUT a `$` prefix
    (the recurring REST gotcha). Reads of main RAM ($0000-$CFFF) are reliable;
    reads of the REU register block ($DF00-$DF0A) reflect live REC state but
    some bits read back as garbage (e.g. $DF06 src_hi) — prefer the $C200
    RAM tracker when the tracked pump path is active.
    """
    import requests

    try:
        r = rest_request(
            "GET",
            url + "/v1/machine:readmem",
            params={"address": f"{address:04X}", "length": str(length)},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.content
    except requests.RequestException:
        return None


def rest_reset(url: str = U64_URL, timeout: float = 5.0) -> int | None:
    """PUT /v1/machine:reset. Returns the status code, or None on failure.

    Per the standing end-of-session rule (silence-and-reset-after-testing
    memory), every diag tool that drives the machine should call this on the
    way out — and the standalone ``u64_probe.py --reset`` is the manual hook.

    REST-only, so it is Ultimate-only. Use ``machine_reset`` unless the caller
    genuinely means "over REST"; a ``tr://`` target has no REST endpoint and
    every attempt here returns None.
    """
    import requests

    try:
        return rest_request("PUT", url + "/v1/machine:reset", timeout=timeout).status_code
    except requests.RequestException:
        return None


def machine_reset(url: str) -> bool:
    """Silence the SID and reset whatever backend ``url`` names. True on success.

    Scheme-aware because the end-of-session reset is a safety rule, and the
    REST path only exists on the Ultimate. A ``tr://`` target sent through
    ``rest_reset`` fails on every call — so a TeensyROM run through a diag
    harness printed "reset: FAILED" and left the machine running the last
    thing it was driving, with the rule *appearing* to have been applied. Same
    trap as u64_probe's --reset-only on a non-http URL.

    Goes through c64cast's own backend, so it works for every scheme the app
    itself accepts and needs no per-tool knowledge of the transport.
    """
    from c64cast.app.config import Config
    from c64cast.app.connect import apply_to_config, parse_connection_uri
    from c64cast.hw.backend import make_backend
    from c64cast.hw.c64 import SID

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(url))
    # Two writes need no write path, and the WriteC64Spans probe would put one
    # more half-sent command between a just-killed run and the safety reset.
    cfg.teensyrom.dma_slicing = "off"
    api = None
    try:
        api = make_backend(cfg)
        api.write_memory(f"{SID.MODE_VOL:04X}", "00")  # silence before reset
        api.reset()
        return True
    except Exception as e:  # noqa: BLE001 — a diag teardown must not mask the run
        print(f"[reset] {url}: {type(e).__name__}: {e}")
        return False
    finally:
        close = getattr(api, "close", None)
        if close:
            close()


#: Most bytes the query-string form of ``writemem`` carries, on every firmware.
REST_WRITEMEM_MAX = 128


def rest_writemem(address: int, data: bytes, url: str = U64_URL, timeout: float = 2.0) -> None:
    """PUT /v1/machine:writemem?address=HHHH&data=<hex> — write 1 to 128 raw
    bytes to C64 memory over REST. Coexists with c64cast's DMA socket (separate
    transport), like rest_readmem — fine to poke concurrently with a running app.

    PUT is the form Ultimate 3.14e, 3.15a and C64 Ultimate 1.1.0 all accept
    with the bytes in the URL. POST takes the bytes only as a request body, and
    3.15a answers a bodiless POST with HTTP 412 "Expected Body, but got none.".

    Raises ``ValueError`` for a write the firmware would refuse by its length
    or end address, and ``requests.HTTPError`` carrying the firmware's error
    text for any non-2xx answer; transport failures propagate as
    ``requests.RequestException``. A caller that wants to carry on catches."""
    import requests

    if not 1 <= len(data) <= REST_WRITEMEM_MAX:
        raise ValueError(f"writemem takes 1 to {REST_WRITEMEM_MAX} bytes, got {len(data)}")
    if address < 0 or address + len(data) > 0x10000:
        raise ValueError(f"writemem of {len(data)} bytes at ${address:04X} passes $FFFF")
    r = rest_request(
        "PUT",
        url + "/v1/machine:writemem",
        params={"address": f"{address:04X}", "data": data.hex()},
        timeout=timeout,
    )
    if not r.ok:
        raise requests.HTTPError(
            f"PUT writemem ${address:04X} ({len(data)} bytes): "
            f"HTTP {r.status_code} {r.text.strip()[:200]}",
            response=r,
        )


def flash_border(url: str = U64_URL, color: int = 1, timeout: float = 2.0) -> None:
    """Set the VIC border color register $D020 to `color` (0-15) over REST — the
    primitive behind the border-flash A/V sync marker (see the border-flash
    auto-memory): poke a bright color at known wall-clock times during a capture,
    then align the visible flashes to the source to measure playback tempo / A/V
    drift. $D020 is bus-clean to poke (one byte) and visible regardless of display
    mode. Raises as :func:`rest_writemem` does."""
    rest_writemem(0xD020, bytes([color & 0x0F]), url, timeout)


def rest_reboot(url: str = U64_URL, timeout: float = 5.0) -> int | None:
    """PUT /v1/machine:reboot — full Ultimate reboot (re-applies FPGA-level
    settings like ``System Mode`` PAL/NTSC that a bare C64 reset won't pick up).
    Returns the status code, or None on failure. Caller must then poll
    ``rest_ping`` until the unit comes back."""
    import requests

    try:
        return rest_request("PUT", url + "/v1/machine:reboot", timeout=timeout).status_code
    except requests.RequestException:
        return None


def rest_get_config(category: str, url: str = U64_URL, timeout: float = 8.0) -> dict | None:
    """GET /v1/configs/<category> → the inner ``{setting: value}`` dict (the
    firmware nests it under the category name), or None on failure. Reusable
    for any config probe (REU enabled, System Mode, etc.)."""
    from urllib.parse import quote

    import requests

    from c64cast._json import decode_json

    try:
        r = rest_request("GET", f"{url}/v1/configs/{quote(category)}", timeout=timeout)
        r.raise_for_status()
        body = decode_json(r)
    except requests.RequestException:
        return None
    if not isinstance(body, dict):
        return None
    inner = body.get(category)
    return inner if isinstance(inner, dict) else body


def rest_set_config(
    category: str, setting: str, value: str, url: str = U64_URL, timeout: float = 10.0
) -> bool:
    """PUT /v1/configs/<category>/<setting>?value=<value>. The firmware verb is
    setting-in-path + a ``value`` query param (a flat ``?setting=value`` is
    rejected with "Function none requires parameter value"). Returns True when
    the reply carries an empty ``errors`` list.

    LIVE + VOLATILE: the PUT applies immediately (the handler calls
    ConfigStore::at_close_config → effectuate) but does NOT write flash — only a
    separate ``:save_to_flash`` command persists (verified in 1541ultimate
    software/api/route_configs.cc + components/config.h at_close_config). So a
    change reverts on the next power-cycle. Still restore any setting you change
    at end of session, so the running machine returns to its prior state."""
    from urllib.parse import quote

    import requests

    from c64cast._json import decode_json

    try:
        r = rest_request(
            "PUT",
            f"{url}/v1/configs/{quote(category)}/{quote(setting)}",
            params={"value": value},
            timeout=timeout,
        )
        r.raise_for_status()
        body = decode_json(r)
    except requests.RequestException:
        return False
    return isinstance(body, dict) and body.get("errors", ["<no errors key>"]) == []


def add_tr_slicing_args(ap) -> None:
    """``--tr-slicing`` / ``--slice-bytes`` / ``--slice-gap``: the
    ``[teensyrom].dma_slicing`` knobs, so one tool can measure the same
    condition with WriteC64Mem and with sliced WriteC64Spans. Unset, each takes
    the config default. No effect on an Ultimate URL."""
    from c64cast.hw.teensyrom_dma import SPANS_FIELD_MAX

    def header_byte(text: str) -> int:
        # Both ride WriteC64Spans' header as one byte; past it, write_spans
        # raises ValueError, which the backend's _emit does not absorb.
        value = int(text)
        if not 0 <= value <= SPANS_FIELD_MAX:
            raise argparse.ArgumentTypeError(f"{value} is not 0-{SPANS_FIELD_MAX}")
        return value

    ap.add_argument("--tr-slicing", choices=["auto", "on", "off"], default=None)
    ap.add_argument(
        "--slice-bytes", type=header_byte, default=None, help="[teensyrom].dma_slice_bytes"
    )
    ap.add_argument(
        "--slice-gap", type=header_byte, default=None, help="[teensyrom].dma_slice_gap_us"
    )


def apply_tr_slicing(cfg, args) -> None:
    """Write the ``add_tr_slicing_args`` flags that were given into ``cfg``."""
    tr = cfg.teensyrom
    if args.tr_slicing is not None:
        tr.dma_slicing = args.tr_slicing
    if args.slice_bytes is not None:
        tr.dma_slice_bytes = args.slice_bytes
    if args.slice_gap is not None:
        tr.dma_slice_gap_us = args.slice_gap


def describe_tr_writes(be) -> str:
    """How a TeensyROM backend is writing, for a tool's setup banner — the
    resolved mode, not the requested one, since 'auto' and 'on' both fall
    back to WriteC64Mem on firmware without WriteC64Spans."""
    from c64cast.hw.teensyrom_api import TeensyROMBackend

    if not isinstance(be, TeensyROMBackend):
        return "not a TeensyROM (slicing flags ignored)"
    return be.describe_writes()
