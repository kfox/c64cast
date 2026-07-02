#!/usr/bin/env python3
"""TR+ (TeensyROM, serial) NMI $D418 DAC sample-rate ceiling sweep — CAPTURE ONLY.

The 4-bit ``$D418`` DAC streams via a CIA-driven NMI handler (audio.NMI_ROUTINE).
Each NMI pulls one sample; the handler needs up to ~81 cycles when a VIC badline
steals 40 cycles mid-handler (+ ~7 entry latency). If the sample PERIOD
(cpu_clock / sample_rate) drops below what the handler can complete, NMIs queue
and fire back-to-back → the EFFECTIVE consumption rate R_eff falls below the
configured rate → **pitch drops**. c64.max_safe_sample_rate models this at
~11.6 kHz NTSC / ~11.1 kHz PAL (conservative); the clean HW wall on the U64 was
~14 kHz. This tool measures it directly on the TR+.

METHOD (RING-PREFILL LOOP, capture-only — no worker, no host feed, no C64 reads
during playback; TR serial reads contend with the DMA write path and rapid reads
during capture are banned):
  The entire 8 KB NMI ring is filled ONCE with a sine that tiles it exactly
  (RING_CYCLES periods → RING_SIZE/RING_CYCLES samples/period), the NMI is armed
  at the swept rate R, and the handler loops the ring forever with ZERO host
  feeding (this is the turbo-era ring-prefill trick — immune to the host-DMA
  worker's feed/underrun edge cases that garble a fed tone on the TR serial
  link). The played frequency is RING_CYCLES · R_eff / RING_SIZE where R_eff is
  the EFFECTIVE consumption rate: if the handler keeps up R_eff = R and pitch =
  R / (RING_SIZE/RING_CYCLES); when the handler OVERRUNS, NMIs queue, R_eff < R,
  and the pitch drops by exactly R_eff / R. We capture off the Cam Link and read
  the pitch from the FFT peak (robust to the ~12% non-uniform avfoundation
  sample drops — a dropped sample shifts amplitude, not the dominant frequency).

  Per rate we report R_eff and R_eff/R, NORMALIZED to the lowest (known-safe)
  rate so any constant PAL/NTSC latch-math offset cancels — the KNEE (where
  R_eff/R starts falling) is the overrun ceiling.

This makes sound on the real C64. Silences + resets the TR on exit.

    scripts/diags/tr_nmi_rate_ceiling.py --rates 8000,10500,11025,11600,13000,14000,15000
"""

from __future__ import annotations

import argparse
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
    NEUTRAL_SAMPLE,
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
CAP_DEVICE = 1  # Cam Link 4K audio (sounddevice idx)
OUT = Path(__file__).resolve().parent / "out"

# The ring holds exactly RING_CYCLES sine periods so it loops seamlessly (no
# wrap discontinuity). 512 cycles over the 8 KB ring = 16 samples/period → a
# clean sine; clean-handler pitch = RING_CYCLES * R / RING_SIZE = R / 16.
RING_CYCLES = 512
SAMPLES_PER_CYCLE = RING_BUFFER_SIZE // RING_CYCLES  # 16


def build_ring_tone() -> bytes:
    """The full 8 KB ring filled with RING_CYCLES sine periods, encoded to
    4-bit DAC codes (0..15, centered on NEUTRAL_SAMPLE). Tiles exactly so the
    NMI loops it with no wrap glitch."""
    t = np.arange(RING_BUFFER_SIZE) / SAMPLES_PER_CYCLE  # cycles
    sine = np.sin(2 * np.pi * t)
    codes = np.rint(NEUTRAL_SAMPLE + 7.0 * sine).astype(int)
    codes = np.clip(codes, 0, 15).astype(np.uint8)
    return codes.tobytes()


def measured_pitch(cap: np.ndarray, sr: int, lo: float, hi: float) -> float:
    """Dominant frequency in [lo, hi] Hz via FFT peak + parabolic interpolation."""
    x = cap - cap.mean()
    win = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * win))
    f = np.fft.rfftfreq(x.size, 1.0 / sr)
    band = (f >= lo) & (f <= hi)
    idx = np.where(band)[0]
    k = idx[np.argmax(spec[idx])]
    # parabolic interpolation around the peak bin for sub-bin accuracy
    if 0 < k < spec.size - 1:
        a, b, c = spec[k - 1], spec[k], spec[k + 1]
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if denom else 0.0
    else:
        delta = 0.0
    df = f[1] - f[0]
    return float(f[k] + delta * df)


def find_camlink(fallback: int) -> int:
    """Resolve the Cam Link audio input index by NAME (robust to PortAudio
    re-enumeration after an HDMI hotplug), falling back to `fallback`."""
    for i, dev in enumerate(sd.query_devices()):
        if "cam link" in dev["name"].lower() and dev["max_input_channels"] > 0:
            return i
    return fallback


def latch_for(rate: int, system: str) -> int:
    """CIA #2 Timer A latch (period = latch+1 cycles) for `rate` — the nominal
    consumer latch, same math as AudioStreamer._nmi_latch_value."""
    clock = CLOCK_NTSC if system == "NTSC" else CLOCK_PAL
    return max(1, round(clock / rate) - 1)


def setup(be, system: str) -> None:
    """One-time C64 bring-up: reset, running IRQ clear loop, upload the NMI
    handler + tiled tone ring + NMI vector + digi-boost. Reset happens ONCE
    here so the HDMI link renegotiates only once (not per rate)."""
    be.reset()
    time.sleep(1.5)
    be.run_basic_clear_loop()
    st = AudioStreamer(
        be,
        8000,  # rate here only affects the (unused) worker; we arm the latch by hand
        system,
        dither=False,
        digi_boost=True,
        host_dma_servo=False,
        nmi_rate_adaptive=False,
        dsp_params=DSPParams(enabled=True),
    )
    st.running = True
    st._upload_nmi_and_buffers()  # handler + neutral ring + NMI vector + digi-boost
    be.write_memory_file(f"{RING_BUFFER_ADDR:04X}", build_ring_tone())  # tone into the ring


def arm(be, rate: int, system: str) -> None:
    """(Re)arm the NMI at `rate`: disarm, set the Timer A latch, enable."""
    latch = latch_for(rate, system)
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_ICR_CLEAR)
    be.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
    be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)


def capture_rate(be, rate: int, system: str, secs: float, device: int) -> tuple[float, str]:
    """Arm the tone at `rate`, let it stabilize, capture steady-state off the
    Cam Link, and read the played pitch. The C64 is ALREADY running the tone
    loop (setup done, HDMI settled) so the capture never spans a mode switch."""
    expected = RING_CYCLES * rate / RING_BUFFER_SIZE  # clean-handler pitch = R/16
    print(f"\n=== {rate} Hz  (expect ~{expected:.0f} Hz if clean, {secs:.0f}s) ===")
    arm(be, rate, system)
    time.sleep(1.0)  # let the tone stabilize before recording
    rec = sd.rec(int(secs * CAP_SR), samplerate=CAP_SR, channels=2, device=device, dtype="float32")
    sd.wait()
    mono = rec.mean(axis=1).astype(np.float64)
    OUT.mkdir(exist_ok=True)
    wav = str(OUT / f"tr_nmirate_{rate}.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(CAP_SR)
        w.writeframes(np.clip(mono * 32767, -32768, 32767).astype(np.int16).tobytes())
    rms = float(np.sqrt(np.mean(mono**2)))
    pitch = measured_pitch(mono, CAP_SR, expected * 0.5, expected * 1.25)
    print(f"    measured pitch = {pitch:.1f} Hz (expected {expected:.0f})  rms={rms:.4f}  -> {wav}")
    return pitch, wav


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="tr://", help="connection URI (default tr:// auto serial)")
    ap.add_argument("--system", default="NTSC", choices=["NTSC", "PAL"])
    ap.add_argument("--secs", type=float, default=6.0, help="seconds per rate")
    ap.add_argument(
        "--rates",
        default="8000,10500,11025,11600,13000,14000,15000",
        help="comma-separated sample rates to sweep",
    )
    ap.add_argument("--device", type=int, default=CAP_DEVICE, help="Cam Link audio sd index")
    args = ap.parse_args()

    rates = [int(r) for r in args.rates.split(",")]
    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)

    results: list[tuple[int, float]] = []
    try:
        setup(be, args.system)
        # HDMI renegotiated on the one reset above; let it settle, then force
        # PortAudio to re-enumerate so the Cam Link capture device is fresh.
        print("[cap] settling HDMI + re-initializing PortAudio for the capture device…")
        time.sleep(3.0)
        sd._terminate()
        sd._initialize()
        device = find_camlink(args.device)
        print(f"[cap] capturing from device idx {device}: {sd.query_devices(device)['name']}")
        for r in rates:
            pitch, _ = capture_rate(be, r, args.system, args.secs, device)
            results.append((r, pitch))
            time.sleep(0.3)
    finally:
        be.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_ICR_CLEAR)
        be.silence_sid()
        be.reset()
        be.close()

    # R_eff = pitch * RING_SIZE / RING_CYCLES (inverse of pitch = cycles*R_eff/size).
    # Normalize R_eff/R to the lowest rate so any constant clock offset cancels.
    scale = RING_BUFFER_SIZE / RING_CYCLES
    base_rate, base_pitch = results[0]
    base_ratio = (base_pitch * scale) / base_rate
    print(f"\n{'rate':>7} {'pitch(Hz)':>10} {'R_eff':>9} {'R_eff/R':>9} {'norm':>7}  verdict")
    for r, p in results:
        r_eff = p * scale
        ratio = r_eff / r
        norm = ratio / base_ratio
        verdict = "clean" if norm >= 0.98 else ("MARGINAL" if norm >= 0.95 else "OVERRUN")
        print(f"{r:>7} {p:>10.1f} {r_eff:>9.0f} {ratio:>9.3f} {norm:>7.3f}  {verdict}")
    print(
        "\nKnee = the first rate where norm drops below ~0.98 (pitch no longer "
        "tracks the configured rate = NMI handler overrun). EARS check too: a "
        "clean rate holds a steady 1 kHz; an overrun warbles / drops in pitch."
    )
    print("TR silenced + reset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
