#!/usr/bin/env python3
"""Capture the U64's real SID audio (Cam Link) and hunt for regular choppiness.

The REU audio pump sounds "regularly choppy" on real hardware even with the
C64-side governor (zero host bus writes), while the host-DMA path sounds clean.
"Regular choppiness" = a periodic amplitude modulation (the audio envelope
wobbling at some fixed rate). This tool launches c64cast on a given config,
records N seconds of the U64 audio via the Cam Link (avfoundation audio device
:3), and FFTs the *amplitude envelope* (rectified + smoothed signal) to expose
modulation peaks — e.g. a spike at the ~62 Hz pump-IRQ rate or its skip-pattern
subharmonic would be the smoking gun.

Run it on two configs and compare:

    scripts/diags/reu_audio_spectrum.py --config scripts/diags/out/reu_gov_pinned.toml --label reu_gov
    scripts/diags/reu_audio_spectrum.py --config scripts/diags/out/hostdma_pinned.toml --label hostdma

Writes the WAV + an envelope-spectrum summary under scripts/diags/out/.
Resets the machine on exit. Cam Link must be free (not in QuickTime).
"""

from __future__ import annotations

import argparse
import subprocess
import time
import wave
from pathlib import Path

import _diaglib as d
import numpy as np

AUDIO_DEV = ":3"  # Cam Link 4K audio (avfoundation index 3); see memory
CAP_RATE = 48000


def _capture(wav_path: Path, seconds: float) -> bool:
    """Record `seconds` of Cam Link audio to wav_path via ffmpeg. False on fail."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "avfoundation",
        "-i",
        AUDIO_DEV,
        "-t",
        f"{seconds:g}",
        "-ac",
        "1",
        "-ar",
        str(CAP_RATE),
        str(wav_path),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"[ffmpeg] FAILED: {r.stderr.decode(errors='replace')[:400]}")
        return False
    return wav_path.exists() and wav_path.stat().st_size > 1024


def _load_mono(wav_path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
        width = w.getsampwidth()
    dt = {1: np.int8, 2: np.int16, 4: np.int32}[width]
    sig = np.frombuffer(raw, dtype=dt).astype(np.float64)
    if ch > 1:
        sig = sig.reshape(-1, ch).mean(axis=1)
    return sig, sr


def _envelope_spectrum(sig: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Amplitude-envelope spectrum: rectify, low-pass (~moving avg), FFT.

    A steady tone has a flat envelope (energy only at DC). Periodic choppiness
    puts energy at the modulation frequency. Returns (freqs_hz, magnitude) for
    0..400 Hz (where pump-rate artifacts live)."""
    sig = sig - sig.mean()
    env = np.abs(sig)
    # Smooth the rectified signal a touch (200-tap moving avg ~4 ms @ 48k) so
    # we measure the envelope, not the carrier.
    k = 200
    env = np.convolve(env, np.ones(k) / k, mode="same")
    env = env - env.mean()
    # Window + FFT
    win = np.hanning(len(env))
    spec = np.abs(np.fft.rfft(env * win))
    freqs = np.fft.rfftfreq(len(env), 1.0 / sr)
    mask = freqs <= 400
    return freqs[mask], spec[mask]


def _report(label: str, sig: np.ndarray, sr: int) -> None:
    # Use a steady middle slice (skip first/last second — boot + teardown).
    if len(sig) > sr * 4:
        sig = sig[sr:-sr]
    rms = np.sqrt(np.mean(sig**2))
    freqs, spec = _envelope_spectrum(sig, sr)
    # Ignore the sub-2 Hz region (slow drift / content). Strength = peak height
    # relative to the median envelope-spectrum level (how far it stands out).
    band = freqs >= 2.0
    fb, sb = freqs[band], spec[band]
    med = float(np.median(sb)) or 1.0
    print(
        f"\n[{label}] audio RMS={rms:.0f}, top envelope-modulation peaks "
        f"(2-400 Hz, strength = ×median):"
    )
    for i in np.argsort(sb)[::-1][:6]:
        print(f"    {fb[i]:6.1f} Hz   {sb[i] / med:6.1f}×")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", required=True)
    ap.add_argument("--label", default="cap")
    ap.add_argument("-t", "--seconds", type=float, default=12.0)
    ap.add_argument("--boot", type=float, default=8.0)
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--wav", help="analyze an existing WAV instead of capturing")
    args = ap.parse_args()

    if args.wav:
        sig, sr = _load_mono(Path(args.wav))
        _report(args.label, sig, sr)
        return 0

    cfg = Path(args.config)
    if not cfg.exists():
        ap.error(f"config not found: {cfg}")
    wav_path = d.stamped(f"audiocap_{args.label}", "wav")

    print(f"[run] python -m c64cast --config {cfg}")
    app = subprocess.Popen(
        [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", args.url]
    )
    rc = 0
    try:
        print(f"[boot] waiting {args.boot:g}s")
        time.sleep(args.boot)
        print(f"[capture] {args.seconds:g}s Cam Link audio → {wav_path.name}")
        if not _capture(wav_path, args.seconds):
            rc = 1
    finally:
        app.terminate()
        try:
            app.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.kill()
        print(f"[reset] machine:reset -> {d.rest_reset(args.url)}")

    if rc == 0:
        sig, sr = _load_mono(wav_path)
        _report(args.label, sig, sr)
        print(f"  wav -> {wav_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
