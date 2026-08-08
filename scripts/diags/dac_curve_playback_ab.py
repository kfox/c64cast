#!/usr/bin/env python3
"""Objective A/B of the ``$D418`` DAC curves, measured off the capture device.

Plays the *same* test signal through the real encoder (``encode_floats_to_dac``,
the single source of truth every input path shares) once per curve — ``linear``,
``mahoney_ultisid``, and whichever calibrated table applies — captures the
result, and reports SNDR/THD after level-matching.

Why measured and not listened to: the curves differ in loudness by ~10 dB, which
by itself reads as "better", and the differences that matter (crossover grit on
quiet passages, harmonic distortion from a mis-ordered ladder) are exactly the
ones a level-mismatched listening test gets wrong. Level-matched SNDR against a
known input is the honest comparison, and it is repeatable.

    scripts/diags/dac_curve_playback_ab.py --url u64://HOST --socket 1

Reports, per curve:

* ``sndr_db``     signal-to-noise-and-distortion at the test tone — the headline.
* ``thd_db``      harmonics 2..10 only, relative to the fundamental.
* ``level``       captured amplitude, before matching (the loudness difference).
* ``quiet_sndr``  the same, at −30 dBFS: where a bad ladder's crossover gap bites.

This makes sound on the real C64. Silences + resets the machine on the way out.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np
import sounddevice as sd

from c64cast.audio import dac_calibration as dc
from c64cast.audio import dac_calibration_store as dcs
from c64cast.audio import dac_capture_device as dcap
from c64cast.audio import dac_slot_ring as dsr
from c64cast.audio.audio import AudioStreamer
from c64cast.audio.audio_handlers import (
    CIA2_CRA_STOP,
    CIA2_ICR_DISABLE_ALL,
    CIA2_ICR_ENABLE_TIMER_A_NMI,
    CIA2_TIMER_A_CONTINUOUS,
    RING_BUFFER_ADDR,
    RING_BUFFER_SIZE,
    encode_floats_to_dac,
)
from c64cast.audio.dac_curves import resolve_dac_curve
from c64cast.audio.dsp import DSPParams
from c64cast.config import Config
from c64cast.connect import apply_to_config, parse_connection_uri
from c64cast.hw.backend import make_backend
from c64cast.hw.c64 import CIA2, CLOCK_NTSC, CLOCK_PAL
from c64cast.sid.sid_hw_config import restore_sid_config, snapshot_sid_config

# Tone cycles per ring: an integer, so the ring tiles seamlessly and the NMI
# loops it with no discontinuity to smear the spectrum.
TONE_CYCLES = 128
TONE_HZ = dsr.NMI_RATE * TONE_CYCLES / RING_BUFFER_SIZE  # 125 Hz at 8 kHz / 8192


def make_tone(amp: float) -> np.ndarray:
    t = np.arange(RING_BUFFER_SIZE) / RING_BUFFER_SIZE
    return amp * np.sin(2 * np.pi * TONE_CYCLES * t)


def analyze(cap: np.ndarray, sr: int, f0: float) -> dict[str, float]:
    """SNDR and THD of a captured single tone. The capture clock and the NMI
    clock are independent, so the tone does not land on an exact FFT bin —
    everything is measured in narrow bands around the expected frequencies."""
    x = cap - cap.mean()
    win = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * win)) ** 2
    freq = np.fft.rfftfreq(x.size, 1.0 / sr)
    total = float(spec[freq > 20].sum())

    def band(f: float, width: float = 0.02) -> float:
        sel = (freq > f * (1 - width)) & (freq < f * (1 + width))
        return float(spec[sel].sum())

    fund = band(f0)
    harm = sum(band(f0 * n) for n in range(2, 11) if f0 * n < sr / 2)
    rest = max(total - fund, 1e-30)
    return {
        "sndr_db": 10 * np.log10(fund / rest),
        "thd_db": 10 * np.log10(max(harm, 1e-30) / fund),
        "level": float(np.sqrt(np.mean(x**2))),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--socket", type=int, default=1, choices=(1, 2))
    ap.add_argument("--system", default="NTSC", choices=("NTSC", "PAL"))
    ap.add_argument("--profile", default=None, help="[audio].dac_calibration_profile")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--secs", type=float, default=3.0)
    args = ap.parse_args()

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    cfg.audio.dac_calibration_profile = args.profile
    be = make_backend(cfg)

    def as_table(raw: bytes | None) -> np.ndarray | None:
        return None if raw is None else np.frombuffer(raw, dtype=np.uint8)

    calibrated = dcs.load_calibrated_table(cfg, be=be)
    curves: list[tuple[str, np.ndarray | None]] = [
        ("linear (4-bit)", None),
        ("mahoney_ultisid", as_table(resolve_dac_curve("mahoney_ultisid"))),
    ]
    if calibrated is not None:
        key = dcs.resolve_calibration_key(cfg, be)
        curves.append((f"calibrated:{key}", as_table(calibrated)))
    else:
        print("!! no calibrated table applies here — comparing the two baked curves only")

    saved: dict[tuple[str, str], str] = {}
    try:
        be.reset()
        time.sleep(1.5)
        be.run_basic_clear_loop()
        st = AudioStreamer(
            be,
            dsr.NMI_RATE,
            args.system,
            dither=False,
            digi_boost=False,
            dac_curve="mahoney_ultisid",
            host_dma_servo=False,
            nmi_rate_adaptive=False,
            dsp_params=DSPParams(enabled=False),
        )
        st.running = True
        st._upload_nmi_and_buffers()  # installs the Mahoney SID env
        clock = CLOCK_NTSC if args.system == "NTSC" else CLOCK_PAL
        latch = max(1, round(clock / dsr.NMI_RATE) - 1)
        be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
        be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
        be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)

        time.sleep(3.0)
        sd._terminate()
        sd._initialize()
        dev = dcap.find_capture_device(args.device)
        print(f"[cap] device idx {dev}: {sd.query_devices(dev)['name']}")
        print(f"[cap] test tone {TONE_HZ:.1f} Hz, {args.secs}s per curve\n")

        saved = snapshot_sid_config(be)
        dc._isolate_socket(be, args.socket)
        print(f"[hw] socket {args.socket} isolated at $D400\n")
        time.sleep(0.3)

        for amp, label in ((0.9, "full scale"), (0.0316, "-30 dBFS")):
            print(f"--- {label} ---")
            tone = make_tone(amp)
            for name, curve in curves:
                ring = encode_floats_to_dac(tone, dither=False, curve=curve)
                be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", ring.tobytes())
                time.sleep(0.4)
                rec = sd.rec(
                    int(args.secs * dsr.CAP_SR),
                    samplerate=dsr.CAP_SR,
                    channels=2,
                    device=dev,
                    dtype="float32",
                )
                sd.wait()
                m = analyze(rec.mean(axis=1).astype(np.float64), dsr.CAP_SR, TONE_HZ)
                print(
                    f"  {name:34s} SNDR {m['sndr_db']:6.2f} dB   THD {m['thd_db']:7.2f} dB"
                    f"   level {m['level']:.4f} ({20 * np.log10(max(m['level'], 1e-9)):+.1f} dBFS)"
                )
            print()
    finally:
        try:
            if saved:
                restore_sid_config(be, saved)
            be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_CRA_STOP)
            be.silence_sid()
            be.reset()
            print("[hw] SID config restored, machine silenced + reset.")
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            print(f"[hw] cleanup warning: {e}")
        be.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
