#!/usr/bin/env python3
"""Prove/disprove that the Cam Link avfoundation AUDIO capture drops samples
non-uniformly (the claim behind the avfoundation_capture_drops_samples memory,
which tells other tools not to trust capture TIMING for drift/tempo).

Instrument
----------
The Ultimate Audio FPGA PCM sampler plays from a streaming REU ring at a fixed
hardware divider of its ~6.25 MHz reference — steady, off the C64 bus, zero
SID/NMI/CPU. So its playback timing is sample-accurate on the C64 side (only a
UNIFORM clock ratio, the ~2% "sampler runs slow" per sampler_clock_calibration).
That makes it the perfect known source: play a signal with exact timing through
it, capture via the Cam Link, and any NON-uniform timing error in the capture is
the CAPTURE PATH's fault, not the source's.

The signal is a CLICK TRAIN: a short 2 kHz burst every 1.000 s for N seconds.
Inter-click intervals in the capture are the measurement:

  * uniform intervals (low coefficient of variation), total span ≈ N × ratio
      → capture is FAITHFUL; the "~12% short non-uniform" memory is wrong
        (likely conflated with the sampler's uniform clock slowness / decode lag).
      mean interval > 1.0 s just measures the sampler clock ratio (a bonus
        cross-check of the ~2% slow finding).
  * intervals scattered non-uniformly, total span well under N × (mean ratio)
      → capture really does drop samples non-uniformly; the memory stands.

No REST polling of the C64 during capture (wedges the unit —
no_rapid_u64_reads_during_capture memory). Makes sound on the real U64; resets
on exit unless --no-reset.

    scripts/diags/capture_fidelity_probe.py                 # generate + play + analyze
    scripts/diags/capture_fidelity_probe.py --seconds 30
    scripts/diags/capture_fidelity_probe.py --analyze-only out/capfid_*.wav
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import _diaglib as d
import numpy as np

CAP_SR = 48000
CLICK_HZ = 2000.0
CLICK_MS = 25.0
CLICK_PERIOD_S = 1.0


def _make_click_video(out: Path, seconds: int) -> Path:
    """Generate a click-train video: exact 1.000 s spacing, black 320x200 @ 15fps
    so c64cast plays it as an ordinary video scene (sampler feeds off its audio)."""
    n = int(round(seconds * CAP_SR))
    audio = np.zeros(n, dtype=np.float32)
    burst_len = int(CLICK_MS / 1000.0 * CAP_SR)
    t = np.arange(burst_len) / CAP_SR
    win = np.hanning(burst_len)
    burst = (0.9 * np.sin(2 * np.pi * CLICK_HZ * t) * win).astype(np.float32)
    n_clicks = int(seconds // CLICK_PERIOD_S)
    for k in range(n_clicks):
        s = int(round(k * CLICK_PERIOD_S * CAP_SR))
        if s + burst_len <= n:
            audio[s : s + burst_len] = burst
    wav = out / "capfid_src.wav"
    _write_wav(wav, audio, CAP_SR)
    mp4 = out / "capfid_src.mp4"
    # Black video + the click wav, muxed. 15fps low-res like dsp_test_*.
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=black:s=320x200:r=15:d={seconds}",
         "-i", str(wav), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(mp4)],
        check=True,
    )  # fmt: skip
    return mp4


def _write_wav(path: Path, sig: np.ndarray, sr: int) -> None:
    import wave

    pcm = np.clip(sig, -1, 1)
    pcm16 = (pcm * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())


def _decode_mono(path: str) -> tuple[np.ndarray, int]:
    import av

    cont = av.open(path)
    a = cont.streams.audio[0]
    sr = a.sample_rate
    rs = av.AudioResampler(format="s16", layout="mono", rate=sr)
    chunks = []
    for fr in cont.decode(a):
        for r in rs.resample(fr):
            chunks.append(r.to_ndarray().reshape(-1).astype(np.float64))
    cont.close()
    sig = np.concatenate(chunks) if chunks else np.zeros(0)
    peak = np.abs(sig).max()
    return (sig / peak if peak > 0 else sig), sr


def _click_times(x: np.ndarray, sr: int) -> np.ndarray:
    """Detect click onsets: band-limited energy envelope, threshold, take the
    rising edge of each above-threshold run. Robust to the DAC/HDMI noise floor."""
    # Envelope of the 2 kHz burst energy: rectify + short smooth.
    env = np.abs(x)
    k = max(1, int(0.004 * sr))  # 4 ms smoothing
    env = np.convolve(env, np.ones(k) / k, mode="same")
    thr = max(env.max() * 0.30, env.mean() * 4)
    above = env > thr
    # Rising edges, with a refractory gap so one burst = one detection.
    edges = np.where((~above[:-1]) & (above[1:]))[0] + 1
    if edges.size == 0:
        return edges.astype(float) / sr
    refractory = int(0.4 * sr)
    keep = [edges[0]]
    for e in edges[1:]:
        if e - keep[-1] >= refractory:
            keep.append(e)
    return np.array(keep, dtype=float) / sr


def analyze(cap_wav: str, seconds: int) -> None:
    cap, sr = _decode_mono(cap_wav)
    if cap.size < sr:
        print(f"  !! capture too short / silent: {cap_wav}")
        return
    times = _click_times(cap, sr)
    if times.size < 3:
        print(f"  !! only {times.size} clicks detected — can't assess timing")
        return
    intervals = np.diff(times)
    # Trim the first/last (boot pad + tail truncation can clip edge bursts).
    core = intervals[1:-1] if intervals.size > 4 else intervals
    mean_iv = float(core.mean())
    std_iv = float(core.std())
    cv = std_iv / mean_iv if mean_iv else float("inf")
    span = float(times[-1] - times[0])
    n = times.size
    expected_span_uniform = (n - 1) * mean_iv  # if perfectly uniform at the mean

    print(f"    capture            : {Path(cap_wav).name}  ({cap.size / sr:.1f}s)")
    print(f"    clicks detected    : {n}  (source emitted {int(seconds // CLICK_PERIOD_S)})")
    print(f"    mean interval      : {mean_iv * 1000:.2f} ms  (source = 1000.00 ms)")
    print(f"    → playback ratio   : {mean_iv / CLICK_PERIOD_S:.4f}  "
          f"({(mean_iv / CLICK_PERIOD_S - 1) * 100:+.2f} % vs source clock)")  # fmt: skip
    print(f"    interval std / CV  : {std_iv * 1000:.2f} ms  /  {cv * 100:.2f} %")
    print(f"    min / max interval : {core.min() * 1000:.2f} / {core.max() * 1000:.2f} ms")
    print(f"    span               : {span:.3f}s  (uniform-at-mean = {expected_span_uniform:.3f}s)")
    # Verdict. A faithful capture has tiny CV (all intervals ~equal); real
    # non-uniform drops scatter the intervals (high CV) and lose total span.
    if cv < 0.02:
        print("    VERDICT: UNIFORM — capture timing is FAITHFUL "
              "(any offset from 1000 ms is the sampler clock ratio, not drops).")  # fmt: skip
    elif cv < 0.05:
        print("    VERDICT: mostly uniform — minor jitter, no gross non-uniform drops.")
    else:
        print("    VERDICT: NON-UNIFORM — intervals scatter; capture-path timing corruption "
              "is real (the drops memory stands).")  # fmt: skip


def _write_config(out: Path, clip: Path) -> Path:
    toml = f"""# generated by capture_fidelity_probe.py — sampler-backed click train
[audio]
enabled = true
backend = "sampler"

[playlist]
loop = false
interleave_videos = false

[[scenes]]
type = "video"
file = "{clip}"
display = "mhires"
"""
    p = out / "capfid.toml"
    p.write_text(toml)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="u64://192.168.2.64", help="connection target (must be U64)")
    ap.add_argument("--seconds", type=int, default=25, help="click-train length (s)")
    ap.add_argument("--avf-audio", default=d.CAMLINK_AVF_AUDIO, help="ffmpeg avfoundation audio in")
    ap.add_argument("--no-reset", action="store_true", help="leave the machine running")
    ap.add_argument("--analyze-only", metavar="WAV", help="skip playback; analyze an existing wav")
    args = ap.parse_args()

    if args.analyze_only:
        analyze(args.analyze_only, args.seconds)
        return 0

    out = d.out_dir()
    print(f"generating {args.seconds}s click train (1.000 s spacing) ...")
    clip = _make_click_video(out, args.seconds)
    cfg = _write_config(out, clip)
    wav = str(d.stamped("capfid", "wav"))
    boot_margin = 7.0
    cap_len = args.seconds + boot_margin + 2.0

    print(f"=== sampler click-train capture ({args.seconds}s) ===")
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "avfoundation", "-i", args.avf_audio, "-t", str(cap_len),
         "-ac", "1", "-ar", str(CAP_SR), wav],
    )  # fmt: skip
    time.sleep(1.5)

    app_log = d.stamped("capfid_app", "log")
    argv = [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", args.url, "-vv"]
    try:
        with open(app_log, "w") as fh:
            app = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT)
            try:
                time.sleep(args.seconds + boot_margin)
            finally:
                app.terminate()
                try:
                    app.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    app.kill()
        ff.wait()
        print(f"    app log: {app_log}")
        analyze(wav, args.seconds)
    finally:
        if not args.no_reset:
            code = d.rest_reset(args.url if args.url.startswith("http") else d.U64_URL)
            print(f"\n[reset] {'HTTP ' + str(code) if code else 'FAILED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
