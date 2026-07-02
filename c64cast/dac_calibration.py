"""Per-system Mahoney 8-bit ``$D418`` DAC calibration: measure the SID transfer
curve for the *actual* SID at ``$D400`` on the connected machine and persist a
per-unit amplitude→``$D418`` "sidtable", so playback can use a table matched to
the real chip instead of the baked emulated-UltiSID one.

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
that (Cam Link / any UVC audio capture on the SID output required) and writes a
table keyed by the connection target (host address or serial device). Playback's
default ``[audio].dac_curve = "auto"`` then prefers that calibrated table when
present (see :func:`resolve_dac_curve_for_backend`).

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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import numpy as np

from .dac_curves import resolve_dac_curve

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from .backend import C64Backend
    from .config import Config

# --- persistence ------------------------------------------------------------

# Calibration tables live at the repo root (anchored to the package, not the
# cwd, so cron/ssh/one-liner runs find them). The directory is gitignored: a
# calibration is machine-specific captured data, not source. See calibration/
# README.md and .gitignore.
CALIBRATION_DIR: Path = Path(__file__).resolve().parent.parent / "calibration" / "dac"

_SCHEMA_VERSION = 1


def _sanitize(text: str) -> str:
    """Filesystem-safe token: keep alnum/dot/dash, fold everything else to '_'."""
    return "".join(c if (c.isalnum() or c in ".-") else "_" for c in text) or "unknown"


def system_calibration_key(cfg: Config) -> str:
    """Stable key for the connected system: its unique serial device or host
    address. Two runs pointed at the same physical machine share a key (and thus
    a calibrated table); different machines get different keys."""
    backend = cfg.hardware.backend
    if backend == "ultimate":
        host = urlparse(cfg.ultimate64.url).hostname or cfg.ultimate64.url
        return f"u64-{_sanitize(host)}"
    # teensyrom
    tr = cfg.teensyrom
    if tr.transport == "tcp":
        return f"tr-tcp-{_sanitize(tr.host or 'unknown')}-{tr.tcp_port}"
    # serial (explicit device node, or auto-detect → a stable-enough placeholder)
    return f"tr-serial-{_sanitize(tr.serial_port or 'auto')}"


def calibration_path(cfg: Config) -> Path:
    return CALIBRATION_DIR / f"{system_calibration_key(cfg)}.json"


def load_calibrated_table(cfg: Config) -> bytes | None:
    """Return the 256-byte calibrated sidtable for this system, or None if no
    (valid) calibration file exists. Malformed files return None rather than
    raising, so a corrupt cache degrades to the baked/linear default."""
    path = calibration_path(cfg)
    try:
        raw = json.loads(path.read_text())
        table = raw["sidtable"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(table, list) or len(table) != 256:
        return None
    try:
        return bytes(int(v) & 0xFF for v in table)
    except (TypeError, ValueError):
        return None


def save_calibrated_table(cfg: Config, sidtable: Sequence[int], metrics: dict[str, Any]) -> Path:
    """Persist a calibrated sidtable + provenance metadata for this system."""
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    path = calibration_path(cfg)
    backend = cfg.hardware.backend
    endpoint = (
        urlparse(cfg.ultimate64.url).hostname or cfg.ultimate64.url
        if backend == "ultimate"
        else (cfg.teensyrom.host or cfg.teensyrom.serial_port or "auto")
    )
    doc = {
        "schema": _SCHEMA_VERSION,
        "key": system_calibration_key(cfg),
        "backend": backend,
        "endpoint": endpoint,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "metrics": metrics,
        "sidtable": [int(v) & 0xFF for v in sidtable],
    }
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


# --- playback curve resolution ----------------------------------------------


def resolve_dac_curve_for_backend(cfg: Config) -> tuple[str, bytes | None]:
    """Resolve ``[audio].dac_curve`` to an effective ``(label, table)`` pair for
    this system/backend. ``table`` is a 256-byte amplitude→``$D418`` map or None
    (the legacy linear 4-bit path).

    * ``"auto"`` (default) — prefer a calibrated table for this system if one
      exists; else ``mahoney_ultisid`` on the Ultimate (deterministic emulated
      SID); else ``linear`` (a physical/unknown SID with no calibration: the
      baked emulated table would not match it, so stay on the safe 4-bit path).
    * ``"calibrated"`` — force this system's calibrated table; raise if absent.
    * ``"linear"`` / ``"mahoney_ultisid"`` — explicit; passed through.
    """
    name = cfg.audio.dac_curve
    if name == "calibrated":
        table = load_calibrated_table(cfg)
        if table is None:
            raise ValueError(
                "[audio].dac_curve = 'calibrated' but no calibration exists for this "
                f"system ({system_calibration_key(cfg)}). Run `c64cast -u <target> "
                "--calibrate-dac` first, or use 'auto'."
            )
        return (f"calibrated:{system_calibration_key(cfg)}", table)
    if name == "auto":
        # Yield to an explicit digi_boost: both commandeer the SID voices, and
        # a user who set digi_boost meant it. (An explicit non-linear curve +
        # digi_boost is rejected by validate_dac_curve_cfg instead.)
        if cfg.audio.digi_boost:
            return ("linear", None)
        table = load_calibrated_table(cfg)
        if table is not None:
            return (f"calibrated:{system_calibration_key(cfg)}", table)
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
    path: Path


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


def run_calibration(
    be: C64Backend,
    cfg: Config,
    *,
    secs: float = 0.5,
    settle: float = 0.2,
    device: int | None = None,
    log_fn: Callable[[str], None] = print,
) -> CalibrationResult:
    """Measure the connected SID's Mahoney transfer curve and persist a
    per-system sidtable. Leaves the machine silenced + reset. Requires a capture
    device on the SID output (the ``mic`` extra / sounddevice).

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

    def capture_amp(code: int, ref: int, dev: int) -> float:
        be.write_memory_file(
            f"{RING_BUFFER_ADDR:04X}", build_toggle_ring(code, ref, RING_BUFFER_SIZE)
        )
        time.sleep(settle)
        rec = sd.rec(int(secs * CAP_SR), samplerate=CAP_SR, channels=2, device=dev, dtype="float32")
        sd.wait()
        mono = rec.mean(axis=1).astype(np.float64)
        return tone_amplitude(mono, CAP_SR, TOGGLE_FREQ)

    signed_raw: list[tuple[int, float, float]] = []
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
        log_fn(
            f"[calib] measuring 256 codes × 2 refs @ {TOGGLE_FREQ:.0f} Hz "
            f"({secs:.2f}s + {settle:.2f}s each, ~{256 * 2 * (secs + settle) / 60:.0f} min)…"
        )
        for c in range(256):
            p = capture_amp(c, REF_ZERO, dev)
            qv = capture_amp(c, REF_POS, dev)
            signed_raw.append((c, p, qv))
            if c % 16 == 0:
                log_fn(f"[calib]   code ${c:02X} ({c:3d}/255)  |L|={p:.5f}")
    finally:
        try:
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_ICR_CLEAR)
            be.silence_sid()
            be.reset()
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            log_fn(f"[calib] cleanup warning: {e}")

    sidtable, metrics = build_sidtable_from_signed(signed_raw)
    path = save_calibrated_table(cfg, sidtable, metrics)
    log_fn(
        f"[calib] done: {metrics['distinct_levels']} distinct levels "
        f"(~{metrics['effective_bits']} effective bits), span {metrics['signed_span']}"
    )
    log_fn(f"[calib] wrote {path}")
    return CalibrationResult(sidtable=sidtable, metrics=metrics, path=path)
