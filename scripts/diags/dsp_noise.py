#!/usr/bin/env python3
"""Noise-stage A/B: the legacy mic hard gate vs the DSP expander, on real
noisy speech (the Kaggle speech-noise-dataset under assets/audio/).

The clean clips used by dsp_ab.py have ~no noise floor, so they can't exercise
the two stages whose whole job is noise: the expander-with-hysteresis (which
replaces the mic hard gate) and the AGC's noise-floor hold. This tool uses the
dataset's MATCHED clean↔noisy pairs: the clean clip gives a ground-truth mask of
where speech is silent (the gaps), and we compare what each noise-reduction
approach does to the noisy clip in those gaps vs during speech.

    scripts/diags/dsp_noise.py 1          # pair id 1 from the dataset
    scripts/diags/dsp_noise.py 1 --gate 0.05 --sens 1.5   # legacy gate knobs
    scripts/diags/dsp_noise.py 1 --config my.toml         # [dsp] expander params

What it reports (all at the 8 kHz DAC rate):
  * gap residual dB   — noise left in the silent gaps. Lower = cleaner.
  * gate events/s     — gain on↔off transitions IN the gaps. The hard gate
                        chatters when noise hovers at its threshold; the
                        expander's hysteresis should toggle far less.
  * speech retain dB  — level change during real speech. Near 0 = speech
                        passed intact (a gate that eats word onsets scores
                        negative here).

Reconstructed wavs for both paths are written to out/ for listening.
"""
from __future__ import annotations

import argparse
import sys
import wave

import _diaglib as d
import numpy as np

from c64cast.audio import encode_floats_to_dac
from c64cast.config import DSPCfg
from c64cast.config import load as load_config
from c64cast.dsp import Expander
from c64cast.video import decode_audio_full

NEUTRAL = 7.5
DATASET = "assets/audio/speech-noise-dataset"


def _db(x: float) -> float:
    return 20.0 * np.log10(max(x, 1e-9))


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0


def _write_wav(path, mono: np.ndarray, sr: int) -> None:
    pcm = np.clip(mono * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _encode_decode(floats: np.ndarray) -> np.ndarray:
    """Round-trip through the 4-bit DAC encode so metrics see what plays."""
    codes = encode_floats_to_dac(floats, dither=False)
    return ((codes.astype(np.float32) - NEUTRAL) / NEUTRAL).astype(np.float32)


def _gap_mask(clean: np.ndarray, sr: int, gap_db: float = -45.0) -> np.ndarray:
    """Per-sample boolean: True where the CLEAN reference is silent (a gap).
    Computed on 20 ms frames of clean RMS, then expanded to per-sample."""
    win = max(1, int(0.02 * sr))
    nwin = len(clean) // win
    frames = clean[:nwin * win].reshape(-1, win)
    frame_db = 20.0 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-9)
    gap = np.repeat(frame_db < gap_db, win)
    # Pad the trailing (< win) leftover samples as non-gap so the mask matches
    # the full signal length.
    if gap.size < len(clean):
        gap = np.concatenate([gap, np.zeros(len(clean) - gap.size, bool)])
    return gap


def _gate_events(gain: np.ndarray, mask: np.ndarray, sr: int) -> float:
    """Transitions of the gain across 0.5 within the masked region, per second.
    A proxy for gate chatter — the hard gate flips 0↔1 on every noise excursion
    past threshold; the hysteresis expander should be far steadier."""
    g = gain[:len(mask)][mask[:len(gain)]] if mask.any() else gain
    if g.size < 2:
        return 0.0
    state = g >= 0.5
    transitions = int(np.count_nonzero(np.diff(state.astype(np.int8)) != 0))
    secs = g.size / sr
    return transitions / secs if secs > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pair_id", help="dataset pair id (e.g. 1 → clean_speech/1.wav)")
    ap.add_argument("--sr", type=int, default=8000)
    ap.add_argument("--gate", type=float, default=0.05,
                    help="legacy hard-gate threshold (mic noise_gate default)")
    ap.add_argument("--sens", type=float, default=1.5,
                    help="legacy mic sensitivity multiplier")
    ap.add_argument("--config", help="pull expander params from this [dsp] block")
    ap.add_argument("--dataset", default=DATASET)
    args = ap.parse_args()

    clean = decode_audio_full(f"{args.dataset}/clean_speech/{args.pair_id}.wav",
                              args.sr).astype(np.float32) / 32768.0
    noisy = decode_audio_full(f"{args.dataset}/noisy_speech/{args.pair_id}.wav",
                              args.sr).astype(np.float32) / 32768.0
    n = min(len(clean), len(noisy))
    clean, noisy = clean[:n], noisy[:n]
    if n == 0:
        raise SystemExit("empty clip")

    snr = _db(_rms(clean)) - _db(_rms(noisy - clean))
    gap = _gap_mask(clean, args.sr)
    speech = ~gap
    print(f"pair {args.pair_id}: {n / args.sr:.1f}s  est_SNR={snr:.1f}dB  "
          f"gap={100 * gap.mean():.0f}%  speech={100 * speech.mean():.0f}%")

    # --- legacy mic path: sensitivity, then hard gate ---
    leg_in = noisy * args.sens
    leg_gate = (np.abs(leg_in) >= args.gate).astype(np.float32)  # 1=open
    legacy = _encode_decode(leg_in * leg_gate)

    # --- DSP expander path (the gate replacement) ---
    cfg = load_config(args.config).dsp if args.config else DSPCfg()
    exp = Expander(sample_rate=args.sr,
                   threshold_db=cfg.expander_threshold_db,
                   ratio=cfg.expander_ratio,
                   hysteresis_db=cfg.expander_hysteresis_db,
                   floor_db=cfg.expander_floor_db,
                   attack_ms=cfg.expander_attack_ms,
                   release_ms=cfg.expander_release_ms)
    dsp_in = noisy * args.sens
    dsp_out = exp.process(dsp_in)
    dsp = _encode_decode(dsp_out)
    # Per-sample applied gain for the chatter metric (guard tiny denominators).
    with np.errstate(divide="ignore", invalid="ignore"):
        dsp_gain = np.where(np.abs(dsp_in) > 1e-4, dsp_out / dsp_in, 1.0)
    dsp_gain = np.clip(np.nan_to_num(dsp_gain, nan=1.0), 0.0, 2.0)

    def block(label, sig, gain):
        gap_res = _db(_rms(sig[gap])) if gap.any() else float("nan")
        sp_in = _db(_rms((noisy * args.sens)[speech])) if speech.any() else 0.0
        sp_out = _db(_rms(sig[speech])) if speech.any() else 0.0
        ev = _gate_events(gain, gap, args.sr)
        return label, gap_res, sp_out - sp_in, ev

    rows = [block("legacy gate", legacy, leg_gate),
            block("dsp expander", dsp, dsp_gain)]
    print(f"\n{'path':<14}{'gap residual dB':>16}{'speech retain dB':>18}"
          f"{'gate events/s':>15}")
    print("-" * 63)
    for label, gap_res, retain, ev in rows:
        print(f"{label:<14}{gap_res:>16.1f}{retain:>+18.2f}{ev:>15.1f}")
    print("\nReading: dsp expander should give a LOWER (cleaner) gap residual or"
          " comparable,\nFAR fewer gate events/s (hysteresis vs chatter), and "
          "speech retain near 0\n(the hard gate often eats quiet word onsets → "
          "more negative retain).")

    out_leg = d.stamped(f"dsp_noise_{args.pair_id}_legacy", "wav")
    out_dsp = d.stamped(f"dsp_noise_{args.pair_id}_dsp", "wav")
    _write_wav(out_leg, legacy, args.sr)
    _write_wav(out_dsp, dsp, args.sr)
    print(f"\nwrote:\n  {out_leg}\n  {out_dsp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
