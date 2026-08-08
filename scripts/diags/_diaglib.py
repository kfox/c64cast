"""Shared helpers for the c64cast diagnostic tools in this directory.

Every tool in ``scripts/diags/`` imports from here so that path handling,
hardware defaults, and the U64 REST shims are solved once instead of being
re-derived (often wrongly) in each one-off script. The recurring pain points
this module exists to kill:

* **Project home.** ``import c64cast`` must work no matter what the cwd is.
  Importing this module inserts the repo root onto ``sys.path``.
* **Stable output paths.** Captures land under ``scripts/diags/out/`` (git
  ignored), not a coin-flip between ``/tmp`` and ``/private/tmp``.
* **Hardware indices drift.** The Cam Link cv2 index / avfoundation audio
  index and the U64 URL all shift with hotplug + DHCP, so every default here
  is overridable by env var (and the tools expose matching CLI flags).

The values below are *defaults*, not ground truth: they are one rig's
working values, confirmed as of 2026-06-10. Point the env vars at yours.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

# ---- paths ----------------------------------------------------------------

# scripts/diags/_diaglib.py -> repo root is two parents up.
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


# ---- hardware defaults (all env-overridable) ------------------------------

#: Ultimate 64. Override: C64_DIAG_URL.
U64_URL = os.environ.get("C64_DIAG_URL", "http://192.168.2.64")
#: Ultimate II+ on the same LAN. Override: C64_DIAG_U2P_URL.
U2P_URL = os.environ.get("C64_DIAG_U2P_URL", "http://192.168.2.65")

#: Cam Link 4K as an OpenCV capture index. Override: C64_DIAG_CV2.
CAMLINK_CV2_INDEX = int(os.environ.get("C64_DIAG_CV2", "0"))
#: Cam Link 4K avfoundation *audio* device. Override: C64_DIAG_AVF_AUDIO.
#: avfoundation video for the Cam Link is "[0]" but cv2 is more reliable for
#: frames (direct ffmpeg avfoundation video has thrown I/O errors here).
#: Unset (the default) means "resolve by name at run time" — see camlink_avf_audio.
#: Read as the module attribute ``CAMLINK_AVF_AUDIO``, which resolves on access
#: via the module __getattr__ at the bottom of this file, so the ffmpeg
#: enumeration only runs for tools that actually capture.
_AVF_AUDIO_ENV = os.environ.get("C64_DIAG_AVF_AUDIO")


def camlink_avf_audio(name: str = "Cam Link") -> str:
    """The Cam Link's avfoundation *audio* index, as ffmpeg's ``:N`` spec.

    Resolved by NAME on every call rather than pinned to a constant. macOS
    re-enumerates avfoundation devices as things are plugged in, joined or left
    (a call app, a stream mixer, a headset), so a hardcoded index silently
    becomes some other device: this was pinned at ``:3``, which had drifted onto
    a virtual mixer, and the capture failed with a bare "Invalid argument" while
    the run it was measuring carried on to completion. The sounddevice-based
    probes already resolve by name for exactly this reason.

    Falls back to ``:3`` only if enumeration itself fails, so a broken ffmpeg
    surfaces as the old behavior rather than a crash.
    """
    if _AVF_AUDIO_ENV:
        return _AVF_AUDIO_ENV
    import re
    import subprocess

    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ":3"
    audio = False
    for line in r.stderr.splitlines():
        if "audio devices" in line:
            audio = True
            continue
        if "video devices" in line:
            audio = False
            continue
        m = re.search(r"\[(\d+)\]\s+(.*\S)", line)
        if audio and m and name.lower() in m.group(2).lower():
            return f":{m.group(1)}"
    return ":3"


def python_exe() -> str:
    """The interpreter running this tool — use it to spawn ``-m c64cast``
    so the subprocess gets the same ``.venv`` rather than a stray system
    Python — mise sets ``UV_PYTHON`` to the bare toolchain interpreter, so a
    subprocess launched any other way can miss the project's installed
    extras and report them as unavailable."""
    return sys.executable


# ---- U64 REST shims -------------------------------------------------------
# Thin wrappers over the firmware REST API. Note: REST paths take addresses
# WITHOUT a `$` prefix (a recurring gotcha — see c64_u64_hardware_facts memory).


def rest_ping(url: str = U64_URL, timeout: float = 3.0) -> int | None:
    """GET / and return the HTTP status code, or None if unreachable."""
    import requests

    try:
        return requests.get(url + "/", timeout=timeout).status_code
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
        r = requests.get(
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
        return requests.put(url + "/v1/machine:reset", timeout=timeout).status_code
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
    from c64cast.config import Config
    from c64cast.connect import apply_to_config, parse_connection_uri
    from c64cast.hw.backend import make_backend
    from c64cast.hw.c64 import SID

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(url))
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


def rest_writemem(address: int, data: bytes, url: str = U64_URL, timeout: float = 2.0) -> bool:
    """POST /v1/machine:writemem?address=HHHH&data=<hex> — write raw bytes to C64
    memory over REST. Address WITHOUT a `$` prefix (the recurring gotcha).
    Returns True on HTTP 2xx. Coexists with c64cast's DMA socket (separate
    transport), like rest_readmem — fine to poke concurrently with a running app."""
    import requests

    try:
        r = requests.post(
            url + "/v1/machine:writemem",
            params={"address": f"{address:04X}", "data": data.hex()},
            timeout=timeout,
        )
        return r.ok
    except requests.RequestException:
        return False


def flash_border(url: str = U64_URL, color: int = 1, timeout: float = 2.0) -> bool:
    """Set the VIC border color register $D020 to `color` (0-15) over REST — the
    primitive behind the border-flash A/V sync marker (see the border-flash
    auto-memory): poke a bright color at known wall-clock times during a capture,
    then align the visible flashes to the source to measure playback tempo / A/V
    drift. $D020 is bus-clean to poke (one byte) and visible regardless of display
    mode. Returns True on success."""
    return rest_writemem(0xD020, bytes([color & 0x0F]), url, timeout)


def rest_reboot(url: str = U64_URL, timeout: float = 5.0) -> int | None:
    """PUT /v1/machine:reboot — full Ultimate reboot (re-applies FPGA-level
    settings like ``System Mode`` PAL/NTSC that a bare C64 reset won't pick up).
    Returns the status code, or None on failure. Caller must then poll
    ``rest_ping`` until the unit comes back."""
    import requests

    try:
        return requests.put(url + "/v1/machine:reboot", timeout=timeout).status_code
    except requests.RequestException:
        return None


def rest_get_config(category: str, url: str = U64_URL, timeout: float = 8.0) -> dict | None:
    """GET /v1/configs/<category> → the inner ``{setting: value}`` dict (the
    firmware nests it under the category name), or None on failure. Reusable
    for any config probe (REU enabled, System Mode, etc.)."""
    from urllib.parse import quote

    import requests

    try:
        r = requests.get(f"{url}/v1/configs/{quote(category)}", timeout=timeout)
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError):
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

    try:
        r = requests.put(
            f"{url}/v1/configs/{quote(category)}/{quote(setting)}",
            params={"value": value},
            timeout=timeout,
        )
        r.raise_for_status()
        errs = r.json().get("errors", ["<no errors key>"])
    except (requests.RequestException, ValueError):
        return False
    return errs == []


def __getattr__(name: str) -> object:
    """Resolve ``CAMLINK_AVF_AUDIO`` on first access (PEP 562).

    A dozen tools take it as an argparse default, so it has to keep reading
    like a constant — but resolving it at import would run an ffmpeg
    enumeration for every tool that merely imports this module, capture or
    not. Module-level __getattr__ gives the constant's ergonomics with the
    function's freshness.
    """
    if name == "CAMLINK_AVF_AUDIO":
        return camlink_avf_audio()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
