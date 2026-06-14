#!/usr/bin/env python3
"""Measure the U64's effective $D418 4-bit volume-DAC transfer curve via Cam Link.

The MOS 6581/8580 master-volume DAC (the low nibble of $D418, which the digi
audio path uses as a 4-bit DAC) is famously *non-linear* — the 16 output steps
are not evenly spaced. `encode_floats_to_dac` currently assumes a perfectly
linear ladder (`(x+1)*7.5`), so its rounding deviates from the true output and
adds harmonic distortion. This tool measures the real curve so we can decide
whether an inverse LUT is worth shipping (Q3a of the audio-quality initiative).

Measurement subtlety — the Cam Link is **AC-coupled** (it blocks DC), so you
cannot write a steady code N and read its level: the DC is gone by the time it
reaches the capture. Instead we drive a **square wave** that toggles between a
reference code and code N at an audible frequency (default 1 kHz) and measure
the captured fundamental's magnitude — which is proportional to the *output
step* between the two codes and survives AC coupling.

Two independent anchorings cross-check each other:
  * Set A: toggle 0 <-> N.  Code 0 = master volume off = true silence, so the
    captured fundamental M_A(N) is proportional to DAC(N) directly.
  * Set B: toggle 15 <-> N.  M_B(N) is proportional to DAC(15) - DAC(N).
Per-set normalization removes the (2/pi * bias * path-gain) constant; the two
estimates of the normalized curve should agree. Their mean is the result.

Robust transport: rather than streaming 48 s of audio through the realtime ring
(which laps the queue and desyncs), we **pre-fill the 8 KB NMI ring** with one
code-pair pattern (8192 B = exactly 1024 periods of the 8-sample square) and let
NMI loop it with ZERO host feeding — rock-steady, no underrun, no drift. Each
"segment" is a ring rewrite; code-0 silence between them gives the analyzer
clean energy gaps to segment on, and the segments are mapped in known order
from the full-scale marker.

    scripts/diags/dac_curve.py                       # measure + analyze (3 voices)
    scripts/diags/dac_curve.py --seg 3 --voices 3    # high-SNR run (longer tones)
    scripts/diags/dac_curve.py --voices 0            # production default (residual bias)
    scripts/diags/dac_curve.py --analyze out/x.wav   # re-analyze a capture

No ears needed — this is a pure FFT measurement. Output: a curve table + INL,
a JSON dump under scripts/diags/out/, and a suggested inverse LUT.

FINDING (2026-06-12, U64 6581): the master-volume DAC is close to LINEAR — a
smooth positive INL bow peaking at ~+1.6 LSB near code 10 (rms ~1 LSB), bias-
independent (so it's the DAC, not analog saturation). Simulated inverse-LUT
correction cuts THD 11-25 dB for loud signals (amp >= 0.7) and is neutral for
quiet ones — i.e. it helps most in the loud regime the Q2 compressor produces.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import _diaglib as d
import numpy as np
import sounddevice as sd

# c64cast imports work because _diaglib put the repo root on sys.path.
from c64cast.api import Ultimate64API
from c64cast.audio import RING_BUFFER_ADDR, RING_BUFFER_SIZE, AudioStreamer
from c64cast.c64 import SID

SR = 8000  # SID DAC sample rate (audio default)
CAP_SR = 48000  # Cam Link capture rate
CAP_DEVICE = 1  # Cam Link 4K sounddevice input index (see local-capture-hardware)
F0 = 1000.0  # square-wave toggle frequency (8 samples/period @ 8 kHz)
SEG_S = 1.0  # measurement-segment duration (per code); CLI --seg overrides
MARKER_S = 1.6  # leading marker duration (longer => unambiguous)
GAP_S = 0.6  # code-0 silence between segments
SETTLE_S = 0.25  # skip this at the start of each run (rewrite transient)
GUARD_S = 0.15  # also skip this at the run's tail


def ring_pattern(lo: int, hi: int) -> bytes:
    """8 KB of raw DAC codes: a 50%-duty square between codes lo and hi at F0.
    8192 / 8 = 1024 whole periods, so the NMI ring loops seamlessly."""
    half = int(round(SR / F0 / 2))  # 4 samples per half-period
    period = bytes([hi] * half + [lo] * half)
    reps = RING_BUFFER_SIZE // len(period) + 1
    return (period * reps)[:RING_BUFFER_SIZE]


SILENCE = bytes([0] * RING_BUFFER_SIZE)  # volume off => clean gap


def segment_order() -> list[dict]:
    """The fixed playback order: marker, set A (0<->N), set B (15<->N)."""
    plan = [{"set": "marker", "lo": 0, "hi": 15}]
    for n in range(1, 16):
        plan.append({"set": "A", "lo": 0, "hi": n})
    for n in range(0, 15):
        plan.append({"set": "B", "lo": 15, "hi": n})
    return plan


# --------------------------------------------------------------------------- #
# Capture I/O + analysis
# --------------------------------------------------------------------------- #
def read_wav_mono(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a / 32768.0, sr


def envelope(sig: np.ndarray, sr: int, win_s: float = 0.02) -> tuple[np.ndarray, int]:
    w = max(1, int(win_s * sr))
    n = sig.size // w
    blocks = sig[: n * w].reshape(n, w)
    return np.sqrt((blocks**2).mean(axis=1)), w


def find_runs(cap: np.ndarray, sr: int) -> list[tuple[float, float, float]]:
    """Segment the capture into tone runs (t_start_s, dur_s, mean_env) by
    detecting the code-0 SILENCE gaps between them. Gap-based (not loudness-
    based) so even the tiny top-end steps (15<->14) — whose fundamental is far
    below the full-scale marker — are still picked up: any real tone sits well
    above the true-silence gap floor."""
    env, w = envelope(cap, sr)
    # Noise floor from the quietest 10% of blocks (the silence gaps); a tone
    # run is anything a few × above it. Floored to a small fraction of peak so
    # a noisy capture can't drive the threshold to zero.
    floor = np.percentile(env, 10)
    thr = max(floor * 4.0, 0.02 * env.max())
    min_run = int(0.5 / (w / sr))  # blocks
    loud = env >= thr
    runs, i = [], 0
    while i < env.size:
        if loud[i]:
            j = i
            while j < env.size and loud[j]:
                j += 1
            if (j - i) >= min_run:
                runs.append((i * w / sr, (j - i) * w / sr, float(env[i:j].mean())))
            i = j
        else:
            i += 1
    return runs


def fundamental_mag(slice_: np.ndarray, sr: int) -> float:
    """Peak FFT magnitude in a band around F0 (robust to small clock drift)."""
    if slice_.size < 256:
        return 0.0
    spec = np.abs(np.fft.rfft(slice_ * np.hanning(slice_.size)))
    freqs = np.fft.rfftfreq(slice_.size, 1.0 / sr)
    band = (freqs >= F0 * 0.85) & (freqs <= F0 * 1.15)
    if not band.any():
        return 0.0
    bidx = np.where(band)[0]
    peak = bidx[np.argmax(spec[band])]
    lo, hi = max(0, peak - 1), min(spec.size, peak + 2)
    return float(spec[lo:hi].sum())


def analyze(cap: np.ndarray, sr: int) -> dict:
    runs = find_runs(cap, sr)
    plan = segment_order()
    print(f"capture {cap.size / sr:.1f}s; detected {len(runs)} runs (expect {len(plan)})")

    # The marker (full-scale 0<->15) is the FIRST tone after the clean lead-in
    # silence — anchor on it chronologically. (It can't be picked by loudness:
    # segment A:15 is also 0<->15 and equally loud.) Skip any too-short spurious
    # leading run from reset/boot.
    big = [i for i, r in enumerate(runs) if r[1] >= 1.0]
    if not big:
        raise SystemExit("no marker-length run found; capture may be bad")
    start_idx = big[0]
    seq = runs[start_idx : start_idx + len(plan)]
    if len(seq) < len(plan):
        print(f"WARNING: only {len(seq)} runs after marker; tail codes missing")

    mags: dict[tuple[str, int], float] = {}
    for seg, (t0, dur, _) in zip(plan, seq, strict=False):
        a = int((t0 + SETTLE_S) * sr)
        b = int((t0 + dur - GUARD_S) * sr)
        mags[(seg["set"], seg["hi"])] = fundamental_mag(cap[a:b], sr)

    # Set A: M_A(N) ∝ DAC(N).            tnorm_A(N) = M_A(N) / M_A(15)
    a_ref = mags.get(("A", 15), 0.0)
    tA = {0: 0.0}
    for n in range(1, 16):
        tA[n] = mags[("A", n)] / a_ref if a_ref and ("A", n) in mags else float("nan")
    # Set B: M_B(N) ∝ DAC(15)-DAC(N).    tnorm_B(N) = 1 - M_B(N) / M_B(0)
    b_ref = mags.get(("B", 0), 0.0)
    tB = {15: 1.0}
    for n in range(0, 15):
        tB[n] = (1.0 - mags[("B", n)] / b_ref) if b_ref and ("B", n) in mags else float("nan")

    curve = {}
    for n in range(16):
        vals = [v for v in (tA.get(n), tB.get(n)) if v is not None and np.isfinite(v)]
        curve[n] = float(np.mean(vals)) if vals else float("nan")
    mono = dict(curve)
    for n in range(1, 16):
        if np.isfinite(mono[n]) and np.isfinite(mono[n - 1]):
            mono[n] = max(mono[n], mono[n - 1])

    return {
        "mags": {f"{k[0]}:{k[1]}": v for k, v in mags.items()},
        "tnorm_A": tA,
        "tnorm_B": tB,
        "curve_raw": curve,
        "curve": mono,
    }


def report(res: dict) -> None:
    curve, tA, tB = res["curve"], res["tnorm_A"], res["tnorm_B"]
    print("\n code |   setA   setB   mean |  ideal |  INL(LSB) | DNL(LSB)")
    print("------+----------------------+--------+-----------+---------")
    prev = 0.0
    for n in range(16):
        c = curve[n]
        ideal = n / 15.0
        inl = (c - ideal) * 15.0 if np.isfinite(c) else float("nan")
        dnl = (c - prev) * 15.0 - 1.0 if n > 0 and np.isfinite(c) else float("nan")
        prev = c if np.isfinite(c) else prev
        print(
            f"  {n:2d}  | {tA.get(n, float('nan')):6.3f} {tB.get(n, float('nan')):6.3f} "
            f"{c:6.3f} | {ideal:6.3f} | {inl:+8.2f}  | {dnl:+7.2f}"
        )
    finite = np.array([curve[n] for n in range(16) if np.isfinite(curve[n])])
    ideals = np.array([n / 15.0 for n in range(16) if np.isfinite(curve[n])])
    if finite.size:
        max_inl = np.max(np.abs(finite - ideals)) * 15.0
        rms_inl = np.sqrt(np.mean(((finite - ideals) * 15.0) ** 2))
        print(f"\nmax |INL| = {max_inl:.2f} LSB    rms INL = {rms_inl:.2f} LSB")
        print("(INL near 0 across the board => DAC ~linear, LUT not worth it)")


def build_inverse_lut(curve: dict, size: int = 256) -> list[int]:
    """For each of `size` desired output levels in [0,1], the code whose
    measured output is closest. The encoder's replacement map."""
    codes = np.array([n for n in range(16) if np.isfinite(curve[n])])
    levels = np.array([curve[n] for n in codes])
    return [int(codes[np.argmin(np.abs(levels - i / (size - 1)))]) for i in range(size)]


# --------------------------------------------------------------------------- #
# Bias + playback (ring-prefill loop)
# --------------------------------------------------------------------------- #
def set_bias(api: Ultimate64API, voices: int, sustain: int) -> None:
    """Lock `voices` SID voices into a steady DC pulse (test bit) at the given
    sustain nibble, feeding a constant bias into the master mixer (same trick as
    AudioStreamer's digi-boost, but parameterized so we can vary the bias level
    and check whether the measured curve is bias-dependent => analog saturation,
    or bias-independent => the true volume-DAC nonlinearity)."""
    ctrl = SID.WAVE_PULSE | SID.TEST | SID.GATE  # $49
    for v in range(voices):
        base = SID.voice_base(v)
        api.write_regs(f"{base + SID.OFF_AD:04X}", 0x00, (sustain & 0xF) << 4)
        api.write_regs(f"{base + SID.OFF_PW_LO:04X}", 0x00, 0x08)
        api.write_memory(f"{base + SID.OFF_CONTROL:04X}", f"{ctrl:02X}")


def play_sequence(streamer: AudioStreamer, voices: int, sustain: int, seg_s: float) -> None:
    """Bring up NMI (no worker) and walk the segment order by rewriting the
    ring, with code-0 silence gaps between segments."""
    streamer._upload_nmi_and_buffers()  # NMI routine + neutral ring (no digiboost)
    set_bias(streamer.api, voices, sustain)  # parameterized bias
    streamer._start_nmi_timer()  # arm NMI; ring loops forever
    api = streamer.api
    addr = f"{RING_BUFFER_ADDR:04X}"

    def hold(pattern: bytes, secs: float) -> None:
        api.write_memory_file(addr, pattern)
        api.flush()
        time.sleep(secs)

    hold(SILENCE, 1.5)  # clean lead-in
    for seg in segment_order():
        dur = MARKER_S if seg["set"] == "marker" else seg_s
        hold(ring_pattern(seg["lo"], seg["hi"]), dur)
        hold(SILENCE, GAP_S)
    hold(SILENCE, 0.5)


def save_wav(path: str, mono: np.ndarray, sr: int) -> None:
    pcm = np.clip(mono * 32767, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument(
        "--device",
        type=int,
        default=CAP_DEVICE,
        help=f"sounddevice input index (default {CAP_DEVICE} = Cam Link)",
    )
    ap.add_argument(
        "--voices",
        type=int,
        default=3,
        help="SID voices to bias (0-3). More = stronger bias / SNR. "
        "0 = production default (residual ADSR leak only).",
    )
    ap.add_argument(
        "--sustain",
        type=int,
        default=15,
        help="sustain nibble 0-15 of the bias envelope (lower = "
        "weaker bias). Vary with --voices to probe saturation.",
    )
    ap.add_argument(
        "--seg",
        type=float,
        default=SEG_S,
        help=f"per-code segment duration (s, default {SEG_S}); longer = lower per-code noise",
    )
    ap.add_argument("--label", default="dac_curve")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--analyze", metavar="WAV", help="skip capture; re-analyze this wav")
    args = ap.parse_args()

    if args.analyze:
        cap, sr = read_wav_mono(args.analyze)
        report(analyze(cap, sr))
        return 0

    plan = segment_order()
    dur_s = 1.5 + len(plan) * (args.seg + GAP_S) + (MARKER_S - args.seg) + 0.5
    cap_s = dur_s + 4.0
    nframes = int(cap_s * CAP_SR)
    wav = str(d.stamped(args.label, "wav"))

    print(
        f"capturing {cap_s:.1f}s @ device {args.device}; "
        f"bias = {args.voices} voices, sustain {args.sustain}"
    )
    rec = sd.rec(nframes, samplerate=CAP_SR, channels=2, device=args.device, dtype="float32")
    time.sleep(2.0)  # capture warmup before audio starts

    api = Ultimate64API(args.url)
    streamer = AudioStreamer(api, SR, "NTSC", dither=False, digi_boost=False)
    try:
        api.reset()
        time.sleep(1.0)
        api.run_basic_clear_loop()
        play_sequence(streamer, args.voices, args.sustain, args.seg)
    finally:
        api.silence_sid()
        if not args.no_reset:
            api.reset()
        api.close()

    sd.wait()
    cap = rec.mean(axis=1).astype(np.float64)
    save_wav(wav, cap, CAP_SR)
    res = analyze(cap, CAP_SR)
    report(res)

    res["bias"] = {"voices": args.voices, "sustain": args.sustain}
    res["inverse_lut_256"] = build_inverse_lut(res["curve"])
    json_path = str(d.stamped(args.label, "json"))
    Path(json_path).write_text(json.dumps(res, indent=2))
    print(f"\nsaved: {json_path}\n       {wav}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
