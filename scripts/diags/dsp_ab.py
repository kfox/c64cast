#!/usr/bin/env python3
"""Offline A/B of the host DSP chain on the 4-bit SID DAC stream.

This is the risk-free, ears-not-required first pass for the audio-quality
initiative (see auto-memory project-audio-quality-initiative). It runs a source
through BOTH encode paths and compares what actually reaches the DAC:

    legacy : peak-normalize → linear encode_floats_to_dac   (current behavior)
    dsp    : peak-normalize → AudioDSP chain → encode        (the new path)

For each variant it reconstructs the DAC codes back to a normalized waveform
(``amp = (code - 7.5) / 7.5``) — exactly what the SID volume DAC emits, modulo
the chip's DAC nonlinearity — writes it to a .wav under out/, and prints
objective metrics. No hardware: this measures the signal-processing effect, so
parameters can be tuned before spending a real-hardware capture (let alone the
user's ears).

    scripts/diags/dsp_ab.py SOURCE                  # any wav/mp4/etc PyAV reads
    scripts/diags/dsp_ab.py --signal speech         # synthetic speech-like test
    scripts/diags/dsp_ab.py --signal music          # synthetic music test
    scripts/diags/dsp_ab.py SOURCE --config my.toml # pull [dsp] from a config
    scripts/diags/dsp_ab.py SOURCE --mic            # mic chain (AGC active)

The metrics that matter for a 4-bit DAC:
  * RMS / crest factor — louder + lower crest = more average level retained.
  * DAC codes used     — distinct 0..15 levels exercised (range utilization).
  * loud-body DR       — p95 minus p50 short-term RMS (dB); compression should
                         tighten the BODY of the signal (this is the dyn-range
                         reduction, isolated from the silence-rescue effect).
  * silent windows %   — fraction of 50 ms windows quantized to the neutral
                         code 7 (pure silence). Legacy throws low-level detail
                         away here; DSP makeup rescues it, so this should DROP.
                         (A naive p10-based dyn-range reads HIGHER for DSP for
                         exactly this reason — the rescued windows lower p10 —
                         which is why this tool reports silence directly.)

Whether the compression PUMPS audibly is not an offline question — the memory
u64-reu-socket-dma records repeatedly that spectra mislead on this hardware. The
metrics here establish "the transform does the right thing"; the ears decide.

Hardware follow-up (separate, uses the user's ears — ASK FIRST): write a config
with [audio].enabled + the same [dsp] block, run a video scene, and capture
with audio_capture.py for a real-6581 A/B.
"""

from __future__ import annotations

import argparse
import sys
import wave

import _diaglib as d
import numpy as np

# c64cast imports work because _diaglib put the repo root on sys.path.
from c64cast.audio import encode_floats_to_dac
from c64cast.config import DSPCfg
from c64cast.config import load as load_config
from c64cast.dsp import AudioDSP, DSPParams
from c64cast.video import _compute_normalization_gain, decode_audio_full

NEUTRAL = 7.5  # 4-bit DAC midpoint (encode maps float 0 → 7 by truncation)


# ---- source acquisition ---------------------------------------------------


def _synth(kind: str, sr: int, secs: float) -> np.ndarray:
    """Synthetic test signals (float [-1, 1]) with realistic dynamics so the
    compressor/expander have something to act on. RNG is seedless-but-fixed via
    a constant seed for reproducible A/B across runs."""
    rng = np.random.default_rng(1234)
    n = int(secs * sr)
    t = np.arange(n) / sr
    if kind == "speech":
        # Amplitude-modulated band-limited noise: bursts (words) with quiet
        # gaps (pauses) — wide dynamic range, the case compression helps most.
        env = np.zeros(n, dtype=np.float32)
        pos = 0
        while pos < n:
            word = int(sr * rng.uniform(0.2, 0.5))
            gap = int(sr * rng.uniform(0.1, 0.4))
            lvl = float(rng.uniform(0.05, 1.0))  # word-to-word level variation
            env[pos : pos + word] = lvl
            pos += word + gap
        # Smooth the envelope and modulate ~150 Hz-centered noise (voice-ish).
        k = np.hanning(int(sr * 0.02))
        env = np.convolve(env, k / k.sum(), mode="same")
        noise = rng.standard_normal(n).astype(np.float32)
        carrier = np.sin(2 * np.pi * 150 * t).astype(np.float32)
        sig = env * (0.6 * noise + 0.4 * carrier)
    elif kind == "music":
        # A steady chord + slow swell — narrower dynamics, tests that we don't
        # pump or over-compress sustained material.
        chord = sum(np.sin(2 * np.pi * f * t) for f in (220, 277, 330, 440))
        swell = 0.5 + 0.5 * np.sin(2 * np.pi * 0.2 * t)
        sig = (chord / 4.0) * swell
    else:
        raise SystemExit(f"unknown --signal {kind!r} (use speech|music)")
    sig = np.asarray(sig, dtype=np.float32)
    peak = float(np.max(np.abs(sig))) or 1.0
    return (sig / peak * 0.5).astype(np.float32)  # leave headroom like a source


def _load_source(path: str, sr: int, secs: float | None) -> np.ndarray:
    """Decode any PyAV-readable file to mono float [-1, 1] at sr."""
    int16 = decode_audio_full(path, sr)
    if int16.size == 0:
        raise SystemExit(f"no audio decoded from {path}")
    if secs is not None:
        int16 = int16[: int(secs * sr)]
    return int16.astype(np.float32) / 32768.0


# ---- the two encode paths -------------------------------------------------


def _peak_normalize(floats: np.ndarray) -> np.ndarray:
    """Reproduce AVFileSource's peak-normalization (int16 domain) in float."""
    peak = int(np.max(np.abs(floats)) * 32768)
    gain = _compute_normalization_gain(peak)
    return np.clip(floats * gain, -1.0, 1.0).astype(np.float32)


def _reconstruct(codes: np.ndarray) -> np.ndarray:
    """4-bit DAC codes (0..15) → normalized waveform the DAC would emit."""
    return ((codes.astype(np.float32) - NEUTRAL) / NEUTRAL).astype(np.float32)


def _write_wav(path, mono: np.ndarray, sr: int) -> None:
    pcm = np.clip(mono * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# ---- metrics --------------------------------------------------------------


def _db(x: float) -> float:
    return 20.0 * np.log10(max(x, 1e-9))


def _metrics(codes: np.ndarray, recon: np.ndarray, sr: int) -> dict[str, float]:
    rms = float(np.sqrt(np.mean(recon**2)))
    peak = float(np.max(np.abs(recon))) or 1e-9
    win = max(1, int(0.05 * sr))  # 50 ms windows
    nwin = len(recon) // win
    trimmed = recon[: nwin * win]
    code_win = codes[: nwin * win].reshape(-1, win) if nwin else codes[None, :]
    if trimmed.size:
        st = np.sqrt(np.mean(trimmed.reshape(-1, win) ** 2, axis=1))
    else:
        st = np.array([rms], dtype=np.float32)
    # A window is "lost to silence" when every DAC code in it is the neutral
    # code 7 — legacy quantization throws low-level detail away here; DSP makeup
    # rescues it. (This is why a naive p10-based dyn-range READS higher for DSP:
    # the rescued-from-silence windows lower the 10th percentile. Misleading —
    # so we report it directly instead.)
    silent = float(np.mean(np.all(code_win == 7, axis=1))) if nwin else 0.0
    st_nz = st[st > 1e-6]
    p50 = float(np.percentile(st_nz, 50)) if st_nz.size else 1e-9
    p95 = float(np.percentile(st_nz, 95)) if st_nz.size else 1e-9
    return {
        "rms_db": _db(rms),
        "peak_db": _db(peak),
        "crest_db": _db(peak) - _db(rms),
        "codes_used": float(len(np.unique(codes))),
        "loud_body_dr_db": _db(p95) - _db(p50),  # compression tightens this
        "silent_pct": silent * 100.0,  # detail lost to code-7 silence
    }


_FMT = [
    ("rms_db", "RMS dBFS", "{:+.2f}"),
    ("crest_db", "crest dB", "{:.2f}"),
    ("codes_used", "DAC codes used /16", "{:.0f}"),
    ("loud_body_dr_db", "loud-body DR dB", "{:.2f}"),
    ("silent_pct", "silent windows %", "{:.1f}"),
]


def _print_table(a: dict[str, float], b: dict[str, float]) -> None:
    print(f"\n{'metric':<22}{'legacy':>12}{'dsp':>12}{'delta':>12}")
    print("-" * 58)
    for key, label, fmt in _FMT:
        av, bv = a[key], b[key]
        print(f"{label:<22}{fmt.format(av):>12}{fmt.format(bv):>12}{bv - av:>+12.3f}")
    print()
    print(
        "Reading (the unambiguous 4-bit wins): dsp should show higher RMS\n"
        "(more average level), more DAC codes used, lower crest (peaks tamed),\n"
        "tighter loud-body DR (compression evening the body), and FEWER silent\n"
        "windows (quiet detail rescued above the quantization floor). Whether\n"
        "the compression pumps audibly is an EARS question — capture + listen."
    )


def _resolve_params(args) -> DSPParams:
    if args.config:
        cfg = load_config(args.config)
        params = cfg.dsp.to_params()
        if not params.enabled:
            print(f"note: [dsp].enabled is false in {args.config}; forcing on for the A/B")
            params.enabled = True
        return params
    # Default preset: the shipped DSPCfg defaults, enabled.
    return DSPCfg(enabled=True).to_params()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("source", nargs="?", help="audio/video file (PyAV)")
    ap.add_argument(
        "--signal",
        choices=("speech", "music"),
        help="use a synthetic test signal instead of a file",
    )
    ap.add_argument("--sr", type=int, default=8000, help="DAC sample rate")
    ap.add_argument(
        "--seconds", type=float, default=None, help="truncate source/synth to N seconds"
    )
    ap.add_argument("--config", help="pull [dsp] params from this config file")
    ap.add_argument(
        "--mic", action="store_true", help="use the mic chain (AGC active) instead of line"
    )
    ap.add_argument("--prefix", default=None, help="output filename prefix")
    args = ap.parse_args()

    if args.signal:
        floats = _synth(args.signal, args.sr, args.seconds or 8.0)
        name = args.prefix or f"synth_{args.signal}"
    elif args.source:
        floats = _load_source(args.source, args.sr, args.seconds)
        import os

        name = args.prefix or os.path.splitext(os.path.basename(args.source))[0]
    else:
        ap.error("give a SOURCE file or --signal speech|music")

    params = _resolve_params(args)
    print(f"source: {name}  ({floats.size} samples @ {args.sr} Hz, {floats.size / args.sr:.1f}s)")
    print(
        f"chain: {'mic (AGC)' if args.mic else 'line'} | "
        f"compress={params.compress} expander={params.expander} "
        f"limiter={params.limiter} pre_emphasis={params.pre_emphasis} "
        f"agc={params.agc and args.mic}"
    )

    # Both paths share the same peak-normalize front-end (what AVFileSource and
    # the REU pre-encode do). The mic path normally wouldn't peak-normalize, but
    # for an apples-to-apples A/B we feed both variants the same input.
    norm = _peak_normalize(floats)

    legacy_codes = encode_floats_to_dac(norm, dither=False)
    dsp = AudioDSP(params, sample_rate=args.sr, is_mic=args.mic)
    dsp_codes = encode_floats_to_dac(dsp.process(norm), dither=False)

    legacy_recon = _reconstruct(legacy_codes)
    dsp_recon = _reconstruct(dsp_codes)

    out_legacy = d.stamped(f"dsp_ab_{name}_legacy", "wav")
    out_dsp = d.stamped(f"dsp_ab_{name}_dsp", "wav")
    _write_wav(out_legacy, legacy_recon, args.sr)
    _write_wav(out_dsp, dsp_recon, args.sr)

    _print_table(
        _metrics(legacy_codes, legacy_recon, args.sr), _metrics(dsp_codes, dsp_recon, args.sr)
    )
    print(f"\nwrote:\n  {out_legacy}\n  {out_dsp}")
    print("(play both to compare; these are the exact DAC waveforms minus the 6581 DAC curve)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
