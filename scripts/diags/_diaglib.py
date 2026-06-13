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

Local-machine specifics (which cv2 index is the Cam Link today, U64 IP) are
documented in auto-memory, not hard-coded as truth — the values below are
*defaults*, confirmed working as of 2026-06-10.
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


# ---- hardware defaults (all env-overridable) ------------------------------

#: Real Ultimate-64 (see auto-memory u64-hardware). Override: C64_DIAG_URL.
U64_URL = os.environ.get("C64_DIAG_URL", "http://192.168.2.64")
#: Ultimate II+ on the same LAN. Override: C64_DIAG_U2P_URL.
U2P_URL = os.environ.get("C64_DIAG_U2P_URL", "http://192.168.2.65")

#: Cam Link 4K as an OpenCV capture index. Override: C64_DIAG_CV2.
CAMLINK_CV2_INDEX = int(os.environ.get("C64_DIAG_CV2", "0"))
#: Cam Link 4K avfoundation *audio* device. Override: C64_DIAG_AVF_AUDIO.
#: avfoundation video for the Cam Link is "[0]" but cv2 is more reliable for
#: frames (direct ffmpeg avfoundation video has thrown I/O errors here).
CAMLINK_AVF_AUDIO = os.environ.get("C64_DIAG_AVF_AUDIO", ":3")


def python_exe() -> str:
    """The interpreter running this tool — use it to spawn ``-m c64cast``
    so the subprocess gets the same ``.venv`` rather than a stray system
    Python (the mise/uv footgun called out in CLAUDE.md)."""
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
    """
    import requests

    try:
        return requests.put(url + "/v1/machine:reset", timeout=timeout).status_code
    except requests.RequestException:
        return None
