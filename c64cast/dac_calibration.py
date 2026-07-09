"""Per-system Mahoney 8-bit ``$D418`` DAC calibration: measure the SID transfer
curve for the *actual* SID chip(s) on the connected machine and persist a
per-unit amplitude→``$D418`` "sidtable", so playback can use a table matched
to the real chip instead of the baked emulated-UltiSID one.

Why per-system calibration
--------------------------
The baked ``mahoney_ultisid`` table in :mod:`c64cast.dac_curves` generalises
perfectly across the U64's *emulated* UltiSID (deterministic, model-knob
irrelevant). But **physical 6581/8580 chips vary enormously** chip-to-chip
(measured: curve correlation 0.74 between two 6581s; one chip's table on the
other → ~29 % RMS level error), dominated by the analog filter — and SID
replacements (ARM2SID/SwinSID/FPGASID) differ again. So a baked table cannot
serve a physical/replacement chip; the only correct path is to measure the
transfer curve of the device in front of you. ``c64cast --calibrate-dac`` does
that (Cam Link / any UVC audio capture on the SID output required).

Identity keys (not host/IP)
----------------------------
A calibration file is keyed by a *stable device identity*, not the connection
target, so a DHCP re-lease or a USB replug doesn't orphan it:

* **Ultimate (U64 or U2+)** — the REST ``GET /v1/info`` ``unique_id`` (e.g.
  ``"5D327C"``), fetched live via :meth:`~c64cast.api.Ultimate64API.get_device_info`.
* **TeensyROM, serial transport** — the attached board's USB serial number
  (:func:`c64cast.teensyrom_dma.usb_serial_number`), which identifies the
  *cartridge*, not whichever host machine it's plugged into.
* **Fallback** (no live backend — e.g. offline ``--doctor --skip-probe`` — or
  the live lookup fails): the pre-existing host/serial-device-path key.

``[audio].dac_calibration_profile`` overrides all of the above with a
user-chosen name. This is the only way to key a calibration correctly when
the connection itself can't identify the physical SID in front of it: a
TeensyROM+ has no config API, and it can be moved between different physical
C64s (or a U64) — its own USB serial number identifies the cartridge, not
whichever machine's SID it happens to be driving right now. A user who moves
a TR+ around names each host's calibration once (``--calibrate-dac
--dac-calibration-profile my-breadbin``) and passes the same name on every
playback run against that host.

Multi-socket U64/U2+ calibration
---------------------------------
A real U64 (Elite I/II, C64U) can carry **two physical SID sockets**, each
potentially holding a different chip. ``run_calibration`` queries the live
config (``sid_hw_config.detect_sockets`` — ``"SID Detected Socket N"``) and,
for every socket reporting a real chip, isolates it to ``$D400`` (the fixed
address the NMI DAC handler's hand-assembled ``STA $D418`` reaches — see
:mod:`c64cast.asid_sidmap`'s "chip 0 must land at $D400" trick, reused here
via ``_isolate_socket``) and measures it independently, restoring the
original SID address/socket config afterward. This is purely config-driven —
there's no U64-vs-U2+ model check — so it naturally measures 0, 1, or 2
sockets depending on what the live config reports (a U2+ with one socket +
one UltiSID core measures just that socket; a bare-UltiSID board measures
nothing and falls back to the single-measurement path below). A board with no
populated sockets, or a backend with no config API at all (TeensyROM), falls
back to one unlabeled measurement of whatever SID currently answers
``$D400``.

The resulting file (schema 2) holds one entry per measured SID, keyed
``"1"``/``"2"`` (socket number) or ``"default"`` (single-measurement
fallback) — see :func:`save_calibration`. At playback time,
``load_calibrated_table`` picks the entry matching whichever socket is
*currently* mapped to ``$D400`` (a live config read), so a calibrated
physical-chip table is never misapplied when ``$D400`` is actually owned by
an UltiSID core.

Measurement method (signed, AC-coupled)
---------------------------------------
Same primitive as ``scripts/diags/mahoney_dac_calib.py --signed`` (the
investigation tool this productionises). The SID output → Cam Link path is
AC-coupled, so a static code produces no steady signal — we measure a
*transition*. For each candidate ``$D418`` byte ``C`` we fill the NMI ring with
a square wave toggling between a reference byte and ``C`` every
:data:`TOGGLE_SAMPLES` samples (a 500 Hz tone at the 8 kHz NMI rate that tiles
the ring exactly), capture off the Cam Link, and read the FFT amplitude at
500 Hz = k·|L(C) − L(ref)|, the size of the output step. Measuring each code
against ``$00`` (master-volume-0 floor → |L(C)|) *and* ``$0F`` (positive full
scale) resolves the sign of each code's bipolar excursion; the signed levels
give a 256-entry amplitude→code ladder. Robust to the ~12 % non-uniform
avfoundation sample drops (a dropped sample perturbs amplitude a little, not the
dominant frequency). Stereo capture is folded to mono by averaging, so the SID
pan setting only scales all measurements uniformly and cancels in the
normalised ladder — no mixer changes needed.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import numpy as np

from .asid_sidmap import (
    ADDR_UNMAPPED,
    CAT_ADDRESSING,
    CAT_SOCKETS,
    ITEM_AUTO_MIRROR,
    ITEM_SOCKET1_ADDR,
    ITEM_SOCKET1_EN,
    ITEM_SOCKET1_TYPE,
    ITEM_SOCKET2_ADDR,
    ITEM_SOCKET2_EN,
    ITEM_SOCKET2_TYPE,
    ITEM_ULTISID1_ADDR,
    ITEM_ULTISID2_ADDR,
)
from .dac_curves import resolve_dac_curve
from .sid_hw_config import detect_sockets, restore_sid_config, snapshot_sid_config

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from .backend import C64Backend
    from .config import Config

log = logging.getLogger(__name__)

# --- persistence ------------------------------------------------------------

# Calibration tables live at the repo root (anchored to the package, not the
# cwd, so cron/ssh/one-liner runs find them). The directory is gitignored: a
# calibration is machine-specific captured data, not source. See calibration/
# README.md and .gitignore.
CALIBRATION_DIR: Path = Path(__file__).resolve().parent.parent / "calibration" / "dac"

_SCHEMA_VERSION = 2


def _sanitize(text: str) -> str:
    """Filesystem-safe token: keep alnum/dot/dash, fold everything else to '_'."""
    return "".join(c if (c.isalnum() or c in ".-") else "_" for c in text) or "unknown"


def resolve_calibration_key(cfg: Config, be: C64Backend | None = None) -> str:
    """Stable identity key for the connected system's calibration file.

    Resolution order — see the module docstring's "Identity keys" section:

    1. ``[audio].dac_calibration_profile``, if set — used verbatim (sanitized).
    2. A live device identity, when `be` is a reachable backend: the
       Ultimate's REST ``unique_id``, or a TeensyROM serial device's USB
       serial number.
    3. Fallback — host / serial-device-path, computable from `cfg` alone with
       no hardware access (used when `be` is None, e.g. offline
       ``--doctor --skip-probe``, or the live lookup fails).

    Two runs that resolve to the same key share a calibration file; different
    physical SIDs get different keys."""
    if cfg.audio.dac_calibration_profile:
        return f"profile-{_sanitize(cfg.audio.dac_calibration_profile)}"

    backend = cfg.hardware.backend
    if backend == "ultimate":
        if be is not None:
            try:
                uid = be.get_device_info().get("unique_id")
            except Exception:  # noqa: BLE001 — best-effort; fall back to host key
                log.debug("dac_calibration: live device-info lookup failed", exc_info=True)
                uid = None
            if uid:
                return f"ultimate-{_sanitize(uid)}"
        host = urlparse(cfg.ultimate64.url).hostname or cfg.ultimate64.url
        return f"ultimate-{_sanitize(host)}"

    # teensyrom
    tr = cfg.teensyrom
    if tr.transport == "tcp":
        return f"tr-tcp-{_sanitize(tr.host or 'unknown')}-{tr.tcp_port}"
    if be is not None and tr.serial_port:
        from .teensyrom_dma import usb_serial_number

        sn = usb_serial_number(tr.serial_port)
        if sn:
            return f"tr-{_sanitize(sn)}"
    return f"tr-serial-{_sanitize(tr.serial_port or 'auto')}"


def calibration_path(cfg: Config, be: C64Backend | None = None) -> Path:
    return CALIBRATION_DIR / f"{resolve_calibration_key(cfg, be)}.json"


def _select_sid_entry(cfg: Config, be: C64Backend | None, sids: dict[str, Any]) -> str | None:
    """Which entry in a loaded calibration's ``sids`` map applies right now."""
    has_socket_entries = "1" in sids or "2" in sids
    if (
        has_socket_entries
        and be is not None
        and cfg.hardware.backend == "ultimate"
        and getattr(be.profile, "supports_config", False)
    ):
        socket = _active_socket_at_d400(be)
        if socket is None:
            # The file has physical-chip table(s), but $D400 is currently
            # owned by something else (an UltiSID core) — applying a
            # physical-chip table there would be wrong. Let "auto" fall back
            # to the baked mahoney_ultisid table instead.
            return None
        key = str(socket)
        return key if key in sids else None
    if "default" in sids:
        return "default"
    if len(sids) == 1:
        return next(iter(sids))
    return None


def _active_socket_at_d400(be: C64Backend) -> int | None:
    """Which physical SID socket (1 or 2), if any, currently answers $D400 —
    the fixed address the NMI DAC handler's hand-assembled ``STA $D418``
    reaches. None if neither socket owns it (an UltiSID core does, or
    nothing does)."""
    try:
        addressing = be.get_config_category(CAT_ADDRESSING)
        sockets = be.get_config_category(CAT_SOCKETS)
    except Exception:  # noqa: BLE001 — best-effort
        log.debug("dac_calibration: live SID addressing read failed", exc_info=True)
        return None
    for n, addr_item, en_item, type_item in (
        (1, ITEM_SOCKET1_ADDR, ITEM_SOCKET1_EN, ITEM_SOCKET1_TYPE),
        (2, ITEM_SOCKET2_ADDR, ITEM_SOCKET2_EN, ITEM_SOCKET2_TYPE),
    ):
        if (
            addressing.get(addr_item) == "$D400"
            and sockets.get(en_item) == "Enabled"
            and sockets.get(type_item, "None") not in ("None", "")
        ):
            return n
    return None


def load_calibrated_table(cfg: Config, *, be: C64Backend | None = None) -> bytes | None:
    """Return the 256-byte calibrated sidtable applicable to this system right
    now, or None if no (valid/applicable) calibration exists. Malformed files
    and schema mismatches return None rather than raising, so a stale or
    corrupt cache degrades to the baked/linear default."""
    path = calibration_path(cfg, be)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA_VERSION:
        return None
    sids = raw.get("sids")
    if not isinstance(sids, dict) or not sids:
        return None
    entry_key = _select_sid_entry(cfg, be, sids)
    if entry_key is None:
        return None
    entry = sids.get(entry_key)
    table = entry.get("sidtable") if isinstance(entry, dict) else None
    if not isinstance(table, list) or len(table) != 256:
        return None
    try:
        return bytes(int(v) & 0xFF for v in table)
    except (TypeError, ValueError):
        return None


def save_calibration(
    cfg: Config,
    key: str,
    entries: dict[str, CalibrationResult],
    device_info: dict[str, str],
) -> Path:
    """Persist one or more per-socket sidtables + provenance for this system."""
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    path = CALIBRATION_DIR / f"{key}.json"
    doc = {
        "schema": _SCHEMA_VERSION,
        "key": key,
        "backend": cfg.hardware.backend,
        "device": device_info,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "sids": {
            name: {
                "detected": r.detected,
                "sidtable": [int(v) & 0xFF for v in r.sidtable],
                "metrics": r.metrics,
            }
            for name, r in entries.items()
        },
    }
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


# --- playback curve resolution ----------------------------------------------


def resolve_dac_curve_for_backend(
    cfg: Config, be: C64Backend | None = None
) -> tuple[str, bytes | None]:
    """Resolve ``[audio].dac_curve`` to an effective ``(label, table)`` pair for
    this system/backend. ``table`` is a 256-byte amplitude→``$D418`` map or None
    (the legacy linear 4-bit path).

    * ``"auto"`` (default) — prefer a calibrated table applicable to this
      system/socket if one exists; else ``mahoney_ultisid`` on the Ultimate
      (deterministic emulated SID); else ``linear`` (a physical/unknown SID
      with no calibration: the baked emulated table would not match it, so
      stay on the safe 4-bit path).
    * ``"calibrated"`` — force the applicable calibrated table; raise if absent.
    * ``"linear"`` / ``"mahoney_ultisid"`` — explicit; passed through.

    `be`, when given a live/reachable backend, lets the resolution pick the
    correct per-socket entry from a multi-SID calibration file (see
    :func:`load_calibrated_table`). Without it (e.g. offline ``--doctor
    --skip-probe``), resolution is best-effort."""
    name = cfg.audio.dac_curve
    if name == "calibrated":
        table = load_calibrated_table(cfg, be=be)
        if table is None:
            raise ValueError(
                "[audio].dac_curve = 'calibrated' but no matching calibration exists "
                f"for this system ({resolve_calibration_key(cfg, be)}). Run `c64cast "
                "-u <target> --calibrate-dac` first, or use 'auto'."
            )
        return (f"calibrated:{resolve_calibration_key(cfg, be)}", table)
    if name == "auto":
        # Yield to an explicit digi_boost: both commandeer the SID voices, and
        # a user who set digi_boost meant it. (An explicit non-linear curve +
        # digi_boost is rejected by validate_dac_curve_cfg instead.)
        if cfg.audio.digi_boost:
            return ("linear", None)
        table = load_calibrated_table(cfg, be=be)
        if table is not None:
            return (f"calibrated:{resolve_calibration_key(cfg, be)}", table)
        if cfg.hardware.backend == "ultimate":
            return ("mahoney_ultisid", resolve_dac_curve("mahoney_ultisid"))
        return ("linear", None)
    return (name, resolve_dac_curve(name))


# --- measurement core -------------------------------------------------------

NMI_RATE = 8000  # consumer rate; well under the ~14 kHz NMI DAC handler ceiling
TOGGLE_SAMPLES = 8  # ring holds ref for 8 samples then code for 8 → 500 Hz square
TOGGLE_FREQ = NMI_RATE / (2 * TOGGLE_SAMPLES)  # 500 Hz
CAP_SR = 48000  # Cam Link capture sample rate
REF_ZERO = 0x00  # master-volume-0 floor reference
REF_POS = 0x0F  # positive full-scale anchor (measured L($0F))


@dataclass(frozen=True)
class CalibrationResult:
    sidtable: list[int]  # 256 entries: amplitude index → $D418 byte
    metrics: dict[str, Any]
    detected: str | None = None  # e.g. "6581" (SID Detected Socket N), or None


@dataclass(frozen=True)
class CalibrationRun:
    key: str
    path: Path
    entries: dict[str, CalibrationResult]  # "1" / "2" / "default" -> result


def build_toggle_ring(code: int, ref: int, ring_size: int) -> bytes:
    """Ring toggling ref↔code every TOGGLE_SAMPLES samples (tiles exactly, so
    the NMI loops it with no wrap glitch). Bytes are FULL 8-bit ``$D418`` values."""
    idx = np.arange(ring_size)
    hi = ((idx // TOGGLE_SAMPLES) % 2).astype(bool)
    return np.where(hi, code, ref).astype(np.uint8).tobytes()


def tone_amplitude(cap: np.ndarray, sr: int, freq: float) -> float:
    """Amplitude of the ``freq`` component via a Hann-windowed FFT (peak in a
    narrow band around freq, scaled to a physical amplitude by the window's
    coherent gain). Comparable across captures of equal length."""
    x = cap - cap.mean()
    win = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * win))
    f = np.fft.rfftfreq(x.size, 1.0 / sr)
    band = (f >= freq * 0.8) & (f <= freq * 1.2)
    idx = np.where(band)[0]
    peak = float(spec[idx].max()) if idx.size else 0.0
    return peak * 2.0 / win.sum()  # Hann coherent gain (sum=N/2) ×2 one-sided


def build_sidtable_from_signed(
    signed_raw: list[tuple[int, float, float]],
) -> tuple[list[int], dict[str, Any]]:
    """Reconstruct signed output levels from the two-reference measurements and
    build the 256-entry amplitude→code sidtable + quality metrics.

    For each code C: p = |L(C) − L($00)| = |L(C)| (L($00)=0 at master vol 0),
    q = |L(C) − L($0F)|. With Lmax = L($0F) the positive anchor: a positive
    in-range code has p+q ≈ Lmax; a negative code has q−p ≈ Lmax. So the sign is
    + when (p+q) is closer to Lmax than (q−p) is. signed level = sign·p. The
    sidtable maps 256 uniform target levels across the measured signed span to
    the code whose level is nearest."""
    code = np.array([c for c, _, _ in signed_raw])
    p = np.array([pp for _, pp, _ in signed_raw])
    q = np.array([qq for _, _, qq in signed_raw])
    lmax = float(p[code == REF_POS][0]) if np.any(code == REF_POS) else float(p.max())

    sign = np.where(np.abs((p + q) - lmax) <= np.abs((q - p) - lmax), 1.0, -1.0)
    level = sign * p  # signed output level per code, in capture-amplitude units

    lo, hi = float(level.min()), float(level.max())
    span = hi - lo
    targets = np.linspace(lo, hi, 256)
    sidtable = [int(code[np.argmin(np.abs(level - t))]) for t in targets]

    nf = float(np.median(p[(code & 0x0F) == 0]))  # vol-nibble-0 noise floor
    srt = np.sort(np.unique(level))
    distinct = 1 + int(np.sum(np.diff(srt) > nf)) if srt.size > 1 else 1
    ach = np.array([float(level[code == c][0]) for c in sidtable])
    max_gap = float(np.max(np.diff(ach))) if ach.size > 1 else 0.0
    metrics = {
        "signed_span": [round(lo, 6), round(hi, 6)],
        "lmax": round(lmax, 6),
        "noise_floor": round(nf, 6),
        "distinct_levels": distinct,
        "effective_bits": round(float(np.log2(max(distinct, 1))), 2),
        "worst_gap_frac": round(max_gap / span, 4) if span else 0.0,
    }
    return sidtable, metrics


def find_capture_device(preferred: int | None) -> int:
    """Resolve the Cam Link (or explicit) capture device index. Searches device
    names for 'cam link' with an input channel; falls back to ``preferred`` or 0."""
    import sounddevice as sd

    if preferred is not None:
        return preferred
    for i, dev in enumerate(sd.query_devices()):
        if "cam link" in dev["name"].lower() and dev["max_input_channels"] > 0:
            return i
    default_in = sd.default.device[0]
    return int(default_in) if default_in is not None and default_in >= 0 else 0


class CaptureUnavailableError(RuntimeError):
    """Raised when sounddevice / a usable capture device isn't available."""


def _isolate_socket(be: C64Backend, socket: int) -> None:
    """Route SID Socket `socket` (1 or 2) to $D400 — the fixed address the
    NMI DAC handler's hand-assembled ``STA $D418`` reaches — and silence
    everything else that could also respond there (the other socket, both
    UltiSID cores), so a capture measures only the target chip."""
    other = 2 if socket == 1 else 1
    addr_item = ITEM_SOCKET1_ADDR if socket == 1 else ITEM_SOCKET2_ADDR
    en_item = ITEM_SOCKET1_EN if socket == 1 else ITEM_SOCKET2_EN
    other_en_item = ITEM_SOCKET1_EN if other == 1 else ITEM_SOCKET2_EN
    be.put_config_item(CAT_ADDRESSING, addr_item, "$D400")
    be.put_config_item(CAT_SOCKETS, en_item, "Enabled")
    be.put_config_item(CAT_SOCKETS, other_en_item, "Disabled")
    be.put_config_item(CAT_ADDRESSING, ITEM_ULTISID1_ADDR, ADDR_UNMAPPED)
    be.put_config_item(CAT_ADDRESSING, ITEM_ULTISID2_ADDR, ADDR_UNMAPPED)
    be.put_config_item(CAT_ADDRESSING, ITEM_AUTO_MIRROR, "Disabled")


def run_calibration(
    be: C64Backend,
    cfg: Config,
    *,
    secs: float = 0.5,
    settle: float = 0.2,
    device: int | None = None,
    log_fn: Callable[[str], None] = print,
) -> CalibrationRun:
    """Measure the connected SID's (or SIDs', on a U64/U2+ with populated
    physical sockets) Mahoney transfer curve and persist a per-system
    calibration file. Leaves the machine silenced + reset. Requires a capture
    device on the SID output (the ``mic`` extra / sounddevice).

    On a backend with a config API (``profile.supports_config`` — Ultimate
    only), every physical SID socket reporting a detected chip
    (``sid_hw_config.detect_sockets``) is measured independently — isolated to
    ``$D400`` via :func:`_isolate_socket`, measured, then every socket's
    original SID address/socket config is restored. A board with no populated
    sockets, or a backend with no config API at all, falls back to a single
    unlabeled measurement of whatever SID currently answers ``$D400``.

    Raises :class:`CaptureUnavailableError` if capture can't be set up.
    """
    try:
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001 — optional dep
        raise CaptureUnavailableError(
            "audio capture (sounddevice) is required for --calibrate-dac. Install "
            "the 'mic' extra: uv sync --extra mic"
        ) from e

    from .audio import (
        CIA2_ICR_CLEAR,
        CIA2_ICR_DISABLE_ALL,
        CIA2_ICR_ENABLE_TIMER_A_NMI,
        CIA2_TIMER_A_CONTINUOUS,
        RING_BUFFER_ADDR,
        RING_BUFFER_SIZE,
        AudioStreamer,
    )
    from .c64 import CIA2, CLOCK_NTSC, CLOCK_PAL
    from .dsp import DSPParams

    system = cfg.ultimate64.system
    clock = CLOCK_NTSC if system == "NTSC" else CLOCK_PAL
    latch = max(1, round(clock / NMI_RATE) - 1)

    key = resolve_calibration_key(cfg, be)
    supports_config = bool(getattr(be.profile, "supports_config", False))
    device_info: dict[str, str] = {}
    if supports_config:
        try:
            device_info = be.get_device_info()
        except Exception:  # noqa: BLE001 — best-effort provenance only
            log_fn("[calib] could not read device info (product/unique_id)")
    elif cfg.hardware.backend == "teensyrom":
        tr = cfg.teensyrom
        device_info = (
            {"transport": "tcp", "host": tr.host or "", "port": str(tr.tcp_port)}
            if tr.transport == "tcp"
            else {"transport": "serial", "port": tr.serial_port or ""}
        )

    def capture_amp(code: int, ref: int, dev: int) -> float:
        be.write_memory_file(
            f"{RING_BUFFER_ADDR:04X}", build_toggle_ring(code, ref, RING_BUFFER_SIZE)
        )
        time.sleep(settle)
        rec = sd.rec(int(secs * CAP_SR), samplerate=CAP_SR, channels=2, device=dev, dtype="float32")
        sd.wait()
        mono = rec.mean(axis=1).astype(np.float64)
        return tone_amplitude(mono, CAP_SR, TOGGLE_FREQ)

    def measure_one(dev: int, label: str) -> tuple[list[int], dict[str, Any]]:
        signed_raw: list[tuple[int, float, float]] = []
        log_fn(
            f"[calib] measuring {label}: 256 codes × 2 refs @ {TOGGLE_FREQ:.0f} Hz "
            f"({secs:.2f}s + {settle:.2f}s each, ~{256 * 2 * (secs + settle) / 60:.0f} min)…"
        )
        for c in range(256):
            p = capture_amp(c, REF_ZERO, dev)
            qv = capture_amp(c, REF_POS, dev)
            signed_raw.append((c, p, qv))
            if c % 16 == 0:
                log_fn(f"[calib]   {label} code ${c:02X} ({c:3d}/255)  |L|={p:.5f}")
        return build_sidtable_from_signed(signed_raw)

    sockets_present: list[tuple[int, str]] = []
    saved_sid_config: dict[tuple[str, str], str] = {}
    entries: dict[str, CalibrationResult] = {}
    try:
        # Bring-up: reset once (HDMI renegotiates), running IRQ clear loop, then
        # the NMI handler + neutral ring + the Mahoney SID env (installed by
        # _upload_nmi_and_buffers when the curve is a companding one).
        log_fn("[calib] resetting + bringing up NMI DAC + Mahoney env…")
        be.reset()
        time.sleep(1.5)
        be.run_basic_clear_loop()
        st = AudioStreamer(
            be,
            NMI_RATE,
            system,
            dither=False,
            digi_boost=False,
            dac_curve="mahoney_ultisid",
            host_dma_servo=False,
            nmi_rate_adaptive=False,
            dsp_params=DSPParams(enabled=False),
        )
        st.running = True
        st._upload_nmi_and_buffers()
        be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_ICR_CLEAR)
        be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
        be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)

        log_fn("[calib] settling HDMI + (re)initializing capture…")
        time.sleep(3.0)
        sd._terminate()
        sd._initialize()
        dev = find_capture_device(device)
        log_fn(f"[calib] capture device idx {dev}: {sd.query_devices(dev)['name']}")

        if supports_config:
            try:
                s1, s2 = detect_sockets(be)
                if s1 or s2:
                    sockets_info = be.get_config_category(CAT_SOCKETS)
                    if s1:
                        sockets_present.append((1, sockets_info.get(ITEM_SOCKET1_TYPE, "")))
                    if s2:
                        sockets_present.append((2, sockets_info.get(ITEM_SOCKET2_TYPE, "")))
            except Exception:  # noqa: BLE001 — best-effort; fall back to single measurement
                log_fn("[calib] socket detection failed — falling back to a single measurement")

        if sockets_present:
            saved_sid_config = snapshot_sid_config(be)
            try:
                for socket, detected in sockets_present:
                    log_fn(
                        f"[calib] isolating SID socket {socket} "
                        f"({detected or 'detected'}) at $D400…"
                    )
                    _isolate_socket(be, socket)
                    time.sleep(0.2)
                    sidtable, metrics = measure_one(dev, f"socket {socket}")
                    entries[str(socket)] = CalibrationResult(sidtable, metrics, detected or None)
            finally:
                restore_sid_config(be, saved_sid_config)
        else:
            sidtable, metrics = measure_one(dev, "SID")
            entries["default"] = CalibrationResult(sidtable, metrics, None)
    finally:
        try:
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_ICR_CLEAR)
            be.silence_sid()
            be.reset()
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            log_fn(f"[calib] cleanup warning: {e}")

    path = save_calibration(cfg, key, entries, device_info)
    for name, r in entries.items():
        log_fn(
            f"[calib] {name}: {r.metrics['distinct_levels']} distinct levels "
            f"(~{r.metrics['effective_bits']} effective bits), span {r.metrics['signed_span']}"
        )
    log_fn(f"[calib] wrote {path}")
    return CalibrationRun(key=key, path=path, entries=entries)
