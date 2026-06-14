#!/usr/bin/env python3
"""Offline band-split A/B of 4-bit DAC quantizer variants (noise shaping).

Standalone analysis tool: measures WHERE a 4-bit ($D418) quantizer's noise
lands in the spectrum, so encoding tweaks can be compared before spending an
ears-on hardware capture. Self-contained — it embeds its own minimal encoder +
error-feedback noise shaper, so it keeps working regardless of what the
production audio path does (it was written for the noise-shaping leg of the
audio-quality initiative; that shaper was a NEGATIVE result on the real 6581 —
audible HF hiss at 8 kHz — and was reverted from production, but the analysis
harness is reusable for any future quantizer experiment).

    scripts/diags/quant_noise_ab.py --signal tone        # cleanest demo
    scripts/diags/quant_noise_ab.py --signal speech
    scripts/diags/quant_noise_ab.py SOURCE               # any PyAV-readable file
    scripts/diags/quant_noise_ab.py SOURCE --dither      # shaping + TPDF dither

For each shaper mode (off / first / second) it reconstructs the DAC codes back
to a waveform, writes a .wav under out/, and prints band-split quantization-
noise metrics (the noise = reconstructed-minus-ideal in the DAC-LSB domain, so
it isolates the quantizer's own contribution):

  * midband noise dB : 400-1500 Hz  (the band shaping should clear; LOWER better)
  * HF noise dB      : >2800 Hz      (where shaping pushes energy; RISES)
  * total noise dB   : full band     (rises slightly — shaping trades total for
                       in-band)
  * mid/HF ratio dB  : the tilt; drops hard when shaping works

Background: error-feedback noise shaping filters the past quantization error
and adds it back before the next quantize, so the residual noise spectrum is
shaped by  NTF(z) = 1 - Σ h[k]·z^-k  — a high-pass that tilts noise toward
Nyquist. At an 8 kHz DAC rate "toward Nyquist" is only 4 kHz, still audible,
which is why it lost the ears test. The metrics here prove the transform is
correct; whether it SOUNDS better was the (failed) ears question.
"""

from __future__ import annotations

import argparse
import sys
import wave

import _diaglib as d
import numpy as np

DAC_VOLUME_SCALE = 7.5
DAC_MAX_VOLUME = 15
NEUTRAL_SAMPLE = 7
NEUTRAL = 7.5  # reconstruction midpoint

# Error-feedback coefficients in DAC-LSB units; NTF(z) = 1 - Σ h[k] z^-k.
#   first  : 1 - z^-1        (6 dB/oct HF tilt)
#   second : (1 - z^-1)^2    (12 dB/oct)
SHAPER_COEFFS: dict[str, tuple[float, ...]] = {
    "off": (),
    "first": (1.0,),
    "second": (2.0, -1.0),
}
MODES = tuple(SHAPER_COEFFS)


def encode_4bit(
    floats: np.ndarray, *, mode: str, dither: bool, rng: np.random.Generator | None
) -> np.ndarray:
    """Float [-1, 1] → 4-bit DAC codes (uint8). `mode` selects the noise
    shaper. Mirrors the production encoder's contract: exact-zero input stays
    NEUTRAL; the off path truncates (matching the legacy quantizer), the shaped
    path rounds with error feedback (the feedback loop needs the symmetric
    error)."""
    vol = (floats.astype(np.float64) + 1.0) * DAC_VOLUME_SCALE
    if dither:
        draw = rng.random if rng is not None else np.random.random_sample
        dth = draw(floats.shape).astype(np.float64) - draw(floats.shape).astype(np.float64)
        dth[floats == 0] = 0.0
        vol = vol + dth
    coeffs = np.asarray(SHAPER_COEFFS[mode], dtype=np.float64)
    if coeffs.size == 0:
        return np.clip(vol, 0, DAC_MAX_VOLUME).astype(np.uint8)
    zero = floats == 0
    hist = np.zeros(coeffs.size, dtype=np.float64)
    out = np.empty(vol.size, dtype=np.uint8)
    for i in range(vol.size):
        if zero[i]:
            e = 0.0
            code = NEUTRAL_SAMPLE
        else:
            u = vol[i] + float(coeffs @ hist)
            q = np.floor(u + 0.5)
            e = u - q  # pre-clip error → bounded feedback
            code = int(min(max(q, 0), DAC_MAX_VOLUME))
        hist[1:] = hist[:-1]
        hist[0] = e
        out[i] = code
    return out


# ---- source acquisition ---------------------------------------------------


def _synth(kind: str, sr: int, secs: float) -> np.ndarray:
    rng = np.random.default_rng(1234)
    n = int(secs * sr)
    t = np.arange(n) / sr
    if kind == "speech":
        env = np.zeros(n, dtype=np.float32)
        pos = 0
        while pos < n:
            word = int(sr * rng.uniform(0.2, 0.5))
            gap = int(sr * rng.uniform(0.1, 0.4))
            env[pos : pos + word] = float(rng.uniform(0.05, 1.0))
            pos += word + gap
        k = np.hanning(int(sr * 0.02))
        env = np.convolve(env, k / k.sum(), mode="same")
        noise = rng.standard_normal(n).astype(np.float32)
        carrier = np.sin(2 * np.pi * 150 * t).astype(np.float32)
        sig = env * (0.6 * noise + 0.4 * carrier)
    elif kind == "music":
        chord = sum(np.sin(2 * np.pi * f * t) for f in (220, 277, 330, 440))
        sig = (chord / 4.0) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.2 * t))
    elif kind == "tone":
        sig = 0.25 * np.sin(2 * np.pi * 200 * t)
    else:
        raise SystemExit(f"unknown --signal {kind!r} (use speech|music|tone)")
    sig = np.asarray(sig, dtype=np.float32)
    peak = float(np.max(np.abs(sig))) or 1.0
    return (sig / peak * 0.5).astype(np.float32)


def _load_source(path: str, sr: int, secs: float | None) -> np.ndarray:
    from c64cast.video import decode_audio_full

    int16 = decode_audio_full(path, sr)
    if int16.size == 0:
        raise SystemExit(f"no audio decoded from {path}")
    if secs is not None:
        int16 = int16[: int(secs * sr)]
    return int16.astype(np.float32) / 32768.0


def _peak_normalize(floats: np.ndarray) -> np.ndarray:
    from c64cast.video import _compute_normalization_gain

    gain = _compute_normalization_gain(int(np.max(np.abs(floats)) * 32768))
    return np.clip(floats * gain, -1.0, 1.0).astype(np.float32)


def _write_wav(path, codes: np.ndarray, sr: int) -> None:
    recon = (codes.astype(np.float32) - NEUTRAL) / NEUTRAL
    pcm = np.clip(recon * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# ---- metrics --------------------------------------------------------------


def _db(power: float) -> float:
    return 10.0 * np.log10(max(power, 1e-12))


def _noise_metrics(codes: np.ndarray, ideal: np.ndarray, sr: int) -> dict[str, float]:
    vol_ideal = (ideal.astype(np.float64) + 1.0) * DAC_VOLUME_SCALE
    err = codes.astype(np.float64) - vol_ideal
    err -= err.mean()  # drop DC bias (truncation offset lives only in bin 0)
    psd = np.abs(np.fft.rfft(err * np.hanning(err.size))) ** 2
    freqs = np.fft.rfftfreq(err.size, 1 / sr)
    mid = float(psd[(freqs > 400) & (freqs < 1500)].sum())
    hf = float(psd[freqs > 2800].sum())
    return {
        "mid_db": _db(mid),
        "hf_db": _db(hf),
        "total_db": _db(float(psd.sum())),
        "mid_hf_ratio_db": _db(mid) - _db(hf),
        "codes_used": float(len(np.unique(codes))),
    }


_FMT = [
    ("mid_db", "midband noise dB (400-1500)", "{:+.2f}"),
    ("hf_db", "HF noise dB (>2800)", "{:+.2f}"),
    ("total_db", "total noise dB", "{:+.2f}"),
    ("mid_hf_ratio_db", "mid/HF ratio dB", "{:+.2f}"),
    ("codes_used", "DAC codes used /16", "{:.0f}"),
]


def _print_table(rows: dict[str, dict[str, float]]) -> None:
    cols = list(rows)
    head = f"{'metric':<30}" + "".join(f"{c:>12}" for c in cols)
    print("\n" + head + "\n" + "-" * len(head))
    for key, label, fmt in _FMT:
        print(f"{label:<30}" + "".join(fmt.format(rows[c][key]).rjust(12) for c in cols))
    base = rows["off"]
    print("\nDeltas vs off (shaping win = midband DOWN, HF UP):")
    for c in cols:
        if c == "off":
            continue
        print(
            f"  {c:<8} midband {rows[c]['mid_db'] - base['mid_db']:+.2f} dB"
            f"   HF {rows[c]['hf_db'] - base['hf_db']:+.2f} dB"
        )
    print(
        "\nReading: a working shaper drops midband noise and raises HF; the HF"
        "\nrise is the cost. On the real 6581 at 8 kHz that HF hiss was audible"
        "\nand off won the ears test — metrics correct, ears decide."
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("source", nargs="?", help="audio/video file (PyAV)")
    ap.add_argument(
        "--signal",
        choices=("speech", "music", "tone"),
        help="synthetic test signal instead of a file",
    )
    ap.add_argument("--sr", type=int, default=8000, help="DAC sample rate")
    ap.add_argument("--seconds", type=float, default=None, help="truncate to N seconds")
    ap.add_argument(
        "--dither", action="store_true", help="enable TPDF dither (shaping composes with it)"
    )
    ap.add_argument("--prefix", default=None, help="output filename prefix")
    args = ap.parse_args()

    if args.signal:
        floats = _synth(args.signal, args.sr, args.seconds or 8.0)
        name = args.prefix or f"synth_{args.signal}"
    elif args.source:
        import os

        floats = _load_source(args.source, args.sr, args.seconds)
        name = args.prefix or os.path.splitext(os.path.basename(args.source))[0]
    else:
        ap.error("give a SOURCE file or --signal speech|music|tone")

    ideal = _peak_normalize(floats)
    print(
        f"source: {name}  ({ideal.size} samples @ {args.sr} Hz, "
        f"{ideal.size / args.sr:.1f}s)  dither={args.dither}"
    )

    rows: dict[str, dict[str, float]] = {}
    for mode in MODES:
        rng = np.random.default_rng(2024) if args.dither else None
        codes = encode_4bit(ideal, mode=mode, dither=args.dither, rng=rng)
        rows[mode] = _noise_metrics(codes, ideal, args.sr)
        out = d.stamped(f"qnoise_{name}_{mode}", "wav")
        _write_wav(out, codes, args.sr)
        print(f"  wrote {out}")

    _print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
