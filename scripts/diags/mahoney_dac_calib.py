#!/usr/bin/env python3
"""Mahoney 8-bit ``$D418`` DAC calibration — measure the SID transfer curve for
the full 8-bit volume/mode register and build the amplitude→code "sidtable".

BACKGROUND. The classic c64cast digi path writes the low 4 bits of ``$D418``
(16 volume levels). Pex 'Mahoney' Tufvesson's 2014 technique (Musik Run/Stop)
parks all three SID voices as steady DC sources and routes voices 1+2 through
the analog filter (whose gain is ≈ −1). With that "environment" set up ONCE,
writing the FULL 8-bit ``$D418`` byte — volume nibble + HP/BP/LP filter-mode
bits + the "3 OFF" bit — yields ~256 distinct DC output levels instead of 16,
because the mode bits re-route the parked voices additively/subtractively.
The map desired-amplitude → ``$D418``-byte is strongly NON-linear and per-chip,
so it must be measured empirically. This is (b) a genuine >4-bit resolution
trick, NOT companding of the 16 volume levels (that was tried + closed).

Mahoney's one-time SID environment (§XIV of his white paper):
    $D404/$D40B/$D412 = $49   ; 3 voices: pulse waveform + TEST + GATE
    $D405/$D40C/$D413 = $0F   ; attack=0, decay=15
    $D406/$D40D/$D414 = $FF   ; sustain=15, release=15
    $D415 = $FF, $D416 = $FF  ; filter cutoff maxed
    $D417 = $03               ; route voices 1+2 through filter, resonance=0
Per sample the C64 then just writes one byte to $D418 — our NMI handler
(``audio.NMI_ROUTINE``: ``LDA sample → STA $D418``, no 4-bit mask) is unchanged.

MEASUREMENT (AC-coupled, ring-prefill loop; same harness as
``tr_nmi_rate_ceiling.py``). The SID output + Cam Link are AC-coupled (a ~16 Hz
high-pass), so a static code produces no steady signal — we must measure a
TRANSITION. For each test code C we fill the 8 KB NMI ring with a square wave
that toggles between a fixed reference byte R0 and C every 8 samples (→ 500 Hz
at the 8 kHz NMI rate, tiling the ring exactly), arm the NMI, capture off the
Cam Link, and read the FFT amplitude at 500 Hz = k·|L(C) − L(R0)|, the size of
the output step between the two codes. With R0 = $00 (master volume 0 ≈ the
mixer's zero-gain floor) this magnitude is essentially the absolute output level
of code C. Robust to the ~12% non-uniform avfoundation sample drops (a dropped
sample perturbs amplitude a little, not the dominant frequency).

  --probe (default): sweep ~17 codes spanning 0..255 — a fast look that answers
      "does THIS chip resolve many more than 16 levels?" before investing in the
      full calibration. Reports the count of DISTINGUISHABLE levels.
  --full: all 256 codes → writes a candidate sidtable (amplitude→code) + CSV +
      per-code wavs to scripts/diags/out/.

This makes sound on the real C64. Silences + resets the U64 on exit, and (U64
only) restores the Socket-1 pan it changed for a centered capture.

    scripts/diags/mahoney_dac_calib.py --url u64://HOST            # probe
    scripts/diags/mahoney_dac_calib.py --url u64://HOST --full     # full 256
    scripts/diags/mahoney_dac_calib.py --url u64://HOST --signed   # signed calibration
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from c64cast.audio import (
    CIA2_ICR_CLEAR,
    CIA2_ICR_DISABLE_ALL,
    CIA2_ICR_ENABLE_TIMER_A_NMI,
    CIA2_TIMER_A_CONTINUOUS,
    RING_BUFFER_ADDR,
    RING_BUFFER_SIZE,
    AudioStreamer,
)
from c64cast.backend import make_backend
from c64cast.c64 import CIA2, CLOCK_NTSC, CLOCK_PAL
from c64cast.config import Config
from c64cast.connect import apply_to_config, parse_connection_uri
from c64cast.dsp import DSPParams

CAP_SR = 48000
CAP_DEVICE = 1  # Cam Link 4K audio (sounddevice idx); resolved by name too
OUT = Path(__file__).resolve().parent / "out"

NMI_RATE = 8000  # consumer rate; well under the ~14 kHz handler ceiling
TOGGLE_SAMPLES = 8  # ring holds R0 for 8 samples then C for 8 → 500 Hz square
TOGGLE_FREQ = NMI_RATE / (2 * TOGGLE_SAMPLES)  # 500 Hz

# Mahoney §XIV environment, as consecutive-register writes (control, AD, SR per
# voice; then filter cutoff lo/hi + res/route). Each tuple is (addr, [bytes]).
MAHONEY_ENV: tuple[tuple[int, list[int]], ...] = (
    (0xD404, [0x49, 0x0F, 0xFF]),  # voice 1: pulse+TEST+GATE, A=0/D=15, S=15/R=15
    (0xD40B, [0x49, 0x0F, 0xFF]),  # voice 2
    (0xD412, [0x49, 0x0F, 0xFF]),  # voice 3
    (0xD415, [0xFF, 0xFF, 0x03]),  # cutoff lo, cutoff hi, res=0 + FILT voices 1+2
)


def build_toggle_ring(code: int, ref: int) -> bytes:
    """8 KB ring toggling ref↔code every TOGGLE_SAMPLES samples (tiles exactly,
    so the NMI loops it with no wrap glitch). Bytes are FULL 8-bit $D418 values."""
    idx = np.arange(RING_BUFFER_SIZE)
    hi = ((idx // TOGGLE_SAMPLES) % 2).astype(bool)
    ring = np.where(hi, code, ref).astype(np.uint8)
    return ring.tobytes()


def tone_amplitude(cap: np.ndarray, sr: int, freq: float) -> float:
    """Amplitude of the `freq` component via windowed FFT (peak in a narrow
    band around freq, scaled to a physical amplitude by the window's coherent
    gain). Comparable across captures of equal length."""
    x = cap - cap.mean()
    win = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * win))
    f = np.fft.rfftfreq(x.size, 1.0 / sr)
    band = (f >= freq * 0.8) & (f <= freq * 1.2)
    idx = np.where(band)[0]
    peak = float(spec[idx].max()) if idx.size else 0.0
    # coherent-gain normalization: Hann sums to N/2; ×2 for one-sided rfft.
    return peak * 2.0 / win.sum()


def find_camlink(fallback: int) -> int:
    for i, dev in enumerate(sd.query_devices()):
        if "cam link" in dev["name"].lower() and dev["max_input_channels"] > 0:
            return i
    return fallback


def latch_for(rate: int, system: str) -> int:
    clock = CLOCK_NTSC if system == "NTSC" else CLOCK_PAL
    return max(1, round(clock / rate) - 1)


def write_mahoney_env(be) -> None:
    """Set up the one-time Mahoney SID environment (all 3 voices as DC sources,
    voices 1+2 through the filter). Replaces the usual digi-boost env."""
    for addr, vals in MAHONEY_ENV:
        be.write_regs(f"{addr:04X}", *vals)


def setup(be, system: str) -> None:
    """One-time bring-up: reset ONCE (HDMI renegotiates once), running IRQ clear
    loop, upload the NMI handler + neutral ring + NMI vector (NO digi-boost —
    we install the Mahoney env instead), then arm the NMI at NMI_RATE."""
    be.reset()
    time.sleep(1.5)
    be.run_basic_clear_loop()
    st = AudioStreamer(
        be,
        NMI_RATE,
        system,
        dither=False,
        digi_boost=False,
        host_dma_servo=False,
        nmi_rate_adaptive=False,
        dsp_params=DSPParams(enabled=False),
    )
    st.running = True
    st._upload_nmi_and_buffers()  # handler + neutral ring + NMI vector
    write_mahoney_env(be)
    # Arm the NMI once; the rate never changes, only the ring contents do.
    latch = latch_for(NMI_RATE, system)
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_ICR_CLEAR)
    be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)


def measure_code(
    be, code: int, ref: int, secs: float, settle: float, device: int, save: bool
) -> float:
    """Load the ref↔code toggle into the (already-playing) ring, let it settle,
    capture, and return the 500 Hz step amplitude = k·|L(code) − L(ref)|."""
    be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", build_toggle_ring(code, ref))
    time.sleep(settle)
    rec = sd.rec(int(secs * CAP_SR), samplerate=CAP_SR, channels=2, device=device, dtype="float32")
    sd.wait()
    mono = rec.mean(axis=1).astype(np.float64)
    amp = tone_amplitude(mono, CAP_SR, TOGGLE_FREQ)
    if save:
        OUT.mkdir(exist_ok=True)
        with wave.open(str(OUT / f"mahoney_code_{code:03d}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(CAP_SR)
            w.writeframes(np.clip(mono * 32767, -32768, 32767).astype(np.int16).tobytes())
    return amp


def _report_signed(raw: list[tuple[int, float, float]], ref_pos: int) -> int:
    """Reconstruct signed output levels from the two-reference measurements and
    build the amplitude→code sidtable.

    For each code C we have p=|L(C)−L($00)|=|L(C)| (L($00)=0, master vol 0) and
    q=|L(C)−L($0F)|. With Lmax=L($0F) the positive full-scale anchor:
      * C positive & in-range: p+q ≈ Lmax
      * C negative:            q−p ≈ Lmax  (q = Lmax + p)
    So sign(L(C)) = +1 when (p+q) is closer to Lmax than (q−p) is, else −1.
    signed level = sign · p. The sidtable maps 256 uniform target levels across
    the measured signed span to the code whose level is closest."""
    code = np.array([c for c, _, _ in raw])
    p = np.array([pp for _, pp, _ in raw])
    q = np.array([qq for _, _, qq in raw])
    lmax = float(p[code == ref_pos][0]) if np.any(code == ref_pos) else float(p.max())

    d_pos = np.abs((p + q) - lmax)
    d_neg = np.abs((q - p) - lmax)
    sign = np.where(d_pos <= d_neg, 1.0, -1.0)
    level = sign * p  # signed output level per code, in capture-amplitude units

    OUT.mkdir(exist_ok=True)
    curve = OUT / "mahoney_6581_signed.csv"
    with open(curve, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "p_absL", "q_absLminusLpos", "sign", "signed_level"])
        for i in range(len(code)):
            w.writerow(
                [int(code[i]), f"{p[i]:.6f}", f"{q[i]:.6f}", int(sign[i]), f"{level[i]:.6f}"]
            )

    lo, hi = float(level.min()), float(level.max())
    targets = np.linspace(lo, hi, 256)
    sidtable = np.array([int(code[np.argmin(np.abs(level - t))]) for t in targets])
    tbl = OUT / "mahoney_6581_sidtable.csv"
    with open(tbl, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["amplitude_index", "d418_code", "target_level", "achieved_level"])
        for i, cc in enumerate(sidtable):
            ach = float(level[code == cc][0])
            w.writerow([i, int(cc), f"{targets[i]:.6f}", f"{ach:.6f}"])

    # Quality: distinct achieved levels + monotonicity of the chosen ladder.
    nf = float(np.median(p[(code & 0x0F) == 0]))  # vol-nibble-0 noise floor
    srt = np.sort(np.unique(level))
    distinct = 1 + int(np.sum(np.diff(srt) > nf))
    ach_levels = (
        level[np.searchsorted(code, sidtable)]
        if np.all(np.diff(code) >= 0)
        else np.array([level[code == cc][0] for cc in sidtable])
    )
    max_gap = float(np.max(np.diff(ach_levels))) if ach_levels.size > 1 else 0.0
    span = hi - lo
    print(f"\nsigned span: {lo:.5f} .. {hi:.5f}  (Lmax=${ref_pos:02X}={lmax:.5f})")
    print(
        f"noise floor ~{nf:.5f}  → dynamic range {20 * np.log10(span / max(nf, 1e-6)):.1f} dB "
        f"(~{np.log2(span / max(nf, 1e-6)):.1f} bits)"
    )
    print(
        f"distinct signed levels separated by > floor: {distinct} "
        f"(~{np.log2(max(distinct, 1)):.1f} effective bits)  [vs 16 / 4-bit]"
    )
    print(
        f"sidtable worst level gap (uniformity of the 256-step ladder): {max_gap:.5f} "
        f"({max_gap / span * 100:.1f}% of span)"
    )
    print(f"\nwrote {curve}\nwrote {tbl}")
    print("\nU64 silenced + reset.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--url",
        default=os.environ.get("C64CAST_URL", "u64://ultimate-64.lan"),
        help="connection URI ($C64CAST_URL fallback)",
    )
    ap.add_argument("--system", default="NTSC", choices=["NTSC", "PAL"])
    ap.add_argument("--ref", type=lambda s: int(s, 0), default=0x00, help="reference $D418 byte")
    ap.add_argument("--full", action="store_true", help="sweep all 256 codes (else ~17-code probe)")
    ap.add_argument(
        "--signed",
        action="store_true",
        help="full 256-code SIGNED calibration: measure each code vs $00 (zero) AND "
        "vs $0F (positive max) to resolve the sign of its excursion, build the "
        "bipolar transfer curve + amplitude→code sidtable. Implies --full.",
    )
    ap.add_argument("--secs", type=float, default=1.2, help="capture seconds per code")
    ap.add_argument("--settle", type=float, default=0.5, help="settle seconds after ring swap")
    ap.add_argument("--device", type=int, default=CAP_DEVICE, help="Cam Link audio sd index")
    ap.add_argument(
        "--pan-center",
        action="store_true",
        help="(U64) set Audio Mixer Pan Socket 1 = Center for capture, restore on exit",
    )
    args = ap.parse_args()

    full = args.full or args.signed
    codes = (
        list(range(256)) if full else [round(i * 255 / 16) for i in range(17)]  # 0,16,...,255
    )
    REF_POS = 0x0F  # positive-max anchor for the signed pass (measured full-scale)

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)

    saved_pan: str | None = None
    results: list[tuple[int, float]] = []
    signed_raw: list[tuple[int, float, float]] = []  # (code, p=|L-L0|, q=|L-Lpos|)
    try:
        if args.pan_center and hasattr(be, "put_config_item"):
            try:
                saved_pan = "Left 3"  # verified current value; firmware reverts on power-cycle
                be.put_config_item("Audio Mixer", "Pan Socket 1", "Center")
                print("[cfg] Pan Socket 1 → Center")
            except Exception as e:  # noqa: BLE001
                print(f"[cfg] pan-center failed (continuing): {e}")

        setup(be, args.system)
        print("[cap] settling HDMI + re-initializing PortAudio…")
        time.sleep(3.0)
        sd._terminate()
        sd._initialize()
        device = find_camlink(args.device)
        print(f"[cap] device idx {device}: {sd.query_devices(device)['name']}")
        print(f"[cap] toggle freq {TOGGLE_FREQ:.0f} Hz, ref=${args.ref:02X}, {len(codes)} codes\n")

        for c in codes:
            if args.signed:
                p = measure_code(be, c, 0x00, args.secs, args.settle, device, False)
                q = measure_code(be, c, REF_POS, args.secs, args.settle, device, False)
                signed_raw.append((c, p, q))
                print(f"  code ${c:02X} ({c:3d}) : |L-0|={p:.5f}  |L-Lpos|={q:.5f}")
            else:
                amp = measure_code(be, c, args.ref, args.secs, args.settle, device, args.full)
                results.append((c, amp))
                print(f"  code ${c:02X} ({c:3d}) : step amp = {amp:.5f}")
    finally:
        be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_ICR_CLEAR)
        be.silence_sid()
        be.reset()
        if saved_pan is not None and hasattr(be, "put_config_item"):
            try:
                be.put_config_item("Audio Mixer", "Pan Socket 1", saved_pan)
                print(f"[cfg] Pan Socket 1 → {saved_pan} (restored)")
            except Exception as e:  # noqa: BLE001
                print(f"[cfg] pan restore failed: {e}")
        be.close()

    if args.signed:
        return _report_signed(signed_raw, REF_POS)

    amps = np.array([a for _, a in results])
    full_scale = amps.max() if amps.size else 1.0
    print(f"\n{'code':>6} {'hex':>5} {'step':>9} {'norm':>7}")
    for c, a in results:
        print(f"{c:>6} {'$' + format(c, '02X'):>5} {a:>9.5f} {a / full_scale:>7.3f}")

    # How many DISTINGUISHABLE levels? Sort amplitudes and count clusters
    # separated by more than an estimated noise floor (median abs step between
    # neighbours in the low-amplitude region is a rough per-step resolution).
    srt = np.sort(amps)
    if srt.size > 2:
        gaps = np.diff(srt)
        noise = max(gaps[gaps > 0].min() if np.any(gaps > 0) else 0.0, full_scale * 0.01)
        distinct = 1 + int(np.sum(gaps > noise * 1.5))
        print(f"\nfull-scale step amp = {full_scale:.5f}")
        print(
            f"≈ distinct resolvable levels among tested codes: {distinct} (vs 16 for classic 4-bit)"
        )
        print(f"→ ~{np.log2(max(distinct, 1)):.1f} effective bits over the tested set")

    if args.full:
        OUT.mkdir(exist_ok=True)
        # Candidate sidtable: for each of 256 uniform target levels between the
        # measured min and max, the code whose measured level is closest.
        lo, hi = amps.min(), amps.max()
        targets = np.linspace(lo, hi, 256)
        code_arr = np.array([c for c, _ in results])
        sidtable = np.array([code_arr[np.argmin(np.abs(amps - t))] for t in targets], dtype=int)
        csv_path = OUT / "mahoney_6581_curve.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "step_amp"])
            for c, a in results:
                w.writerow([c, f"{a:.6f}"])
        tbl_path = OUT / "mahoney_6581_sidtable.csv"
        with open(tbl_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["amplitude_index", "d418_code"])
            for i, code in enumerate(sidtable):
                w.writerow([i, int(code)])
        print(f"\nwrote {csv_path}\nwrote {tbl_path}")
        print(
            "NOTE: this pass is magnitude-vs-ref (unsigned). A signed two-reference "
            "pass is needed before wiring the encoder — see the plan."
        )

    print("\nU64 silenced + reset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
