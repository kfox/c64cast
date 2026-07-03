#!/usr/bin/env python3
"""Hardware A/B(/C) of the NMI $D418 DAC-path audio PITCH on the U64, through the
FULL video pipeline (so real bus-halt tick-loss is present — unlike nmi_rate_ab.py,
which feeds a clip straight through AudioStreamer with no VIC load).

The question
------------
Video audio on the ``[audio].backend = "dac"`` path is resampled ONCE to a fixed
``[audio].sample_rate`` (default 11600 Hz), but the NMI consumer rate R is what
actually clocks samples to the DAC. Playback pitch = R / sample_rate. So any gap
between the achieved R and the fixed resample rate shows up DIRECTLY as pitch
error. Two suspected symptoms of the adaptive NMI-rate loop:

  1. Startup glide — R ramps from the seed to convergence over the warmup.
  2. Steady offset — R parks up to ~one integer-latch quantum (~1.3 %, the loop
     deadband) off target, observed HIGH on HW (R≈11752 / 11600).

Because pitch == R / sample_rate, the CAPTURED pitch of each run measures R
directly — no REST polling of the C64 needed (which can wedge the unit mid-run).
That also lets the adaptive-OFF + pitch_mult=1.0 run quantify the RAW bus-halt
loss under today's fps caps: if it sits at ~1.0x, the adaptive loop is
compensating a symptom the fps management already removed.

Conditions (each a full ``python -m c64cast`` run of the same clip @ mhires/DAC;
each sets its knobs EXPLICITLY, so results are independent of the shipped default):
  * ``adaptive``    nmi_rate_adaptive = true                (the retired default)
  * ``static_raw``  nmi_rate_adaptive = false, pitch_mult_mhires = 1.0
                    → RAW bus-halt loss (pitch ratio == R/sample_rate); this IS
                      the shipped default now
  * ``static_comp`` nmi_rate_adaptive = false, pitch_mult_mhires = 1.015 (the old
                    static default — shows the now-stale value overcorrecting)

Pitch measurement
-----------------
avfoundation drops ~12 % of capture samples non-uniformly (corrupts TIMING, not
PITCH — see the avfoundation_capture_drops_samples memory), so we do NOT trust
capture length or beep spacing. Instead: average magnitude spectrum of capture vs
the clip's OWN decoded audio track, both mapped onto a shared LOG-frequency grid
over a band both share (below the DAC Nyquist), spectral-tilt-flattened, then
cross-correlated — the peak lag is ln(pitch_ratio). Robust to dropped samples and
to the DAC low-pass. Early-vs-late windows within a run quantify the glide.

    scripts/diags/nmi_pitch_ab.py                       # A/B/C, default clip
    scripts/diags/nmi_pitch_ab.py --seconds 22 --only adaptive static_raw
    scripts/diags/nmi_pitch_ab.py --analyze-only out/pitch_adaptive_*.wav

Makes sound on the real U64. Resets the machine on exit unless --no-reset.
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
DEFAULT_CLIP = "assets/videos/dsp_test_music.mp4"
# Band both the full-band source AND the DAC-limited capture carry energy in:
# above room rumble, below the ~5 kHz effective DAC/Nyquist ceiling. The pitch
# shift is a pure translation on the log-f axis, so any shared band works; this
# one maximizes shared spectral structure (the music's fundamentals + low
# harmonics) while staying clear of the DAC roll-off.
BAND_LO = 150.0
BAND_HI = 3500.0
LOG_GRID_N = 2048  # log-f bins across [BAND_LO, BAND_HI]


# --- capture / source spectra --------------------------------------------
def _decode_mono(path: str) -> tuple[np.ndarray, int]:
    """Decode any media file's first audio stream to mono float64 at its native
    rate (via PyAV). Used for both the reference clip and the captured wav."""
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
    if not chunks:
        return np.zeros(0), sr
    sig = np.concatenate(chunks)
    peak = np.abs(sig).max()
    return (sig / peak if peak > 0 else sig), sr


def _trim_silence(x: np.ndarray, sr: int) -> np.ndarray:
    """Drop leading/trailing near-silence (the capture's boot/pad window)."""
    env = np.abs(x)
    thr = max(env.max() * 0.03, 1e-4)
    idx = np.where(env > thr)[0]
    if idx.size == 0:
        return x
    return x[max(0, idx[0] - sr // 20) : min(x.size, idx[-1] + sr // 20)]


def _log_spectrum(x: np.ndarray, sr: int) -> np.ndarray:
    """Tilt-flattened average magnitude spectrum on the shared log-f grid.

    Welch-average |rfft| over 8192-sample Hann windows, sample onto a log-f grid
    across [BAND_LO, BAND_HI], take log, then subtract a wide moving average to
    remove the broad spectral tilt (source is full-band, capture is DAC-low-passed
    — the tilt differs, but the harmonic PEAK structure that pins the pitch shift
    is preserved). Returns a zero-mean array of length LOG_GRID_N."""
    n = 8192
    if x.size < n:
        x = np.pad(x, (0, n - x.size))
    win = np.hanning(n)
    acc = np.zeros(n // 2 + 1)
    cnt = 0
    for i in range(0, x.size - n, n // 2):
        acc += np.abs(np.fft.rfft(x[i : i + n] * win)) ** 2
        cnt += 1
    if cnt:
        acc /= cnt
    f = np.fft.rfftfreq(n, 1.0 / sr)
    log_f_grid = np.exp(np.linspace(np.log(BAND_LO), np.log(BAND_HI), LOG_GRID_N))
    valid = f > 0
    mag = np.interp(log_f_grid, f[valid], np.sqrt(acc[valid]))
    lg = np.log(mag + 1e-9)
    # Flatten broad tilt: subtract a wide (≈1/8 grid) moving average.
    k = LOG_GRID_N // 8
    kernel = np.ones(k) / k
    smooth = np.convolve(lg, kernel, mode="same")
    out = lg - smooth
    return out - out.mean()


def _pitch_ratio(cap: np.ndarray, ref: np.ndarray) -> tuple[float, float]:
    """Cross-correlate two log-f spectra; peak lag → (ratio, sharpness).

    ``cap`` and ``ref`` are _log_spectrum outputs on the same grid. A pitch scale
    r shifts capture features to r·f, i.e. +ln r along the log-f axis, so
    cap(u) ≈ ref(u − ln r). argmax of correlate(cap, ref) sits at +ln r (in grid
    steps); parabolic interpolation gives sub-bin. ratio = exp(lag · du).
    ``sharpness`` = peak / |peak-neighbour mean| as a confidence read."""
    corr = np.correlate(cap, ref, mode="full")
    lags = np.arange(-(len(ref) - 1), len(cap))
    # Restrict to plausible pitch range (±6 % → the interesting window is tiny).
    du = (np.log(BAND_HI) - np.log(BAND_LO)) / (LOG_GRID_N - 1)
    max_lag = int(np.ceil(np.log(1.10) / du))
    center = len(ref) - 1
    win = corr[center - max_lag : center + max_lag + 1]
    wl = lags[center - max_lag : center + max_lag + 1]
    k = int(np.argmax(win))
    if 0 < k < len(win) - 1:
        a, b, c = win[k - 1], win[k], win[k + 1]
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
    else:
        delta = 0.0
    lag = wl[k] + delta
    ratio = float(np.exp(lag * du))
    peak = win[k]
    sharp = float(peak / (np.abs(win).mean() + 1e-9))
    return ratio, sharp


def analyze(cap_wav: str, ref_path: str) -> None:
    """Report overall pitch ratio vs the reference clip, plus early/late glide."""
    ref, ref_sr = _decode_mono(ref_path)
    cap, cap_sr = _decode_mono(cap_wav)
    cap = _trim_silence(cap, cap_sr)
    if cap.size < cap_sr:
        print(f"  !! capture too short / silent: {cap_wav}")
        return
    ref_spec = _log_spectrum(ref, ref_sr)

    overall, sharp = _pitch_ratio(_log_spectrum(cap, cap_sr), ref_spec)
    # Early vs late thirds of the (trimmed) capture → glide.
    third = cap.size // 3
    early, _ = _pitch_ratio(_log_spectrum(cap[:third], cap_sr), ref_spec)
    late, _ = _pitch_ratio(_log_spectrum(cap[-third:], cap_sr), ref_spec)

    print(f"    capture: {Path(cap_wav).name}  ({cap.size / cap_sr:.1f}s tonal, sharp={sharp:.1f})")
    print(f"    overall pitch ratio vs source : {overall:.4f}  ({(overall - 1) * 100:+.2f} %)")
    print(f"    early third                   : {early:.4f}  ({(early - 1) * 100:+.2f} %)")
    print(f"    late  third                   : {late:.4f}  ({(late - 1) * 100:+.2f} %)")
    glide = (late / early - 1) * 100
    print(f"    GLIDE (late/early)            : {glide:+.2f} %")


# --- one condition = one c64cast run + capture ---------------------------
def _write_config(out: Path, clip: str, label: str, adaptive: bool, pitch_mult: float) -> Path:
    """Minimal single-scene DAC-path video TOML for one condition."""
    abs_clip = str(Path(clip).resolve())
    toml = f"""# generated by nmi_pitch_ab.py — condition {label}
[audio]
enabled = true
backend = "dac"
nmi_rate_adaptive = {"true" if adaptive else "false"}
pitch_mult_mhires = {pitch_mult}

[playlist]
loop = false
interleave_videos = false

[[scenes]]
type = "video"
file = "{abs_clip}"
display = "mhires"
"""
    p = out / f"pitch_ab_{label}.toml"
    p.write_text(toml)
    return p


def run_condition(
    url: str, clip: str, label: str, adaptive: bool, pitch_mult: float, secs: float, avf: str
) -> str:
    out = d.out_dir()
    cfg = _write_config(out, clip, label, adaptive, pitch_mult)
    wav = str(d.stamped(f"pitch_{label}", "wav"))
    boot_margin = 6.0
    cap_len = secs + boot_margin + 2.0

    print(f"\n=== {label}: adaptive={adaptive} pitch_mult_mhires={pitch_mult} ===")
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "avfoundation", "-i", avf, "-t", str(cap_len),
         "-ac", "1", "-ar", str(CAP_SR), wav],
    )  # fmt: skip
    time.sleep(1.5)  # let the avfoundation stream come up before c64cast boots

    app_log = d.stamped(f"pitch_{label}_app", "log")
    argv = [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", url, "-vv"]
    with open(app_log, "w") as fh:
        app = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT)
        try:
            time.sleep(secs + boot_margin)
        finally:
            app.terminate()
            try:
                app.wait(timeout=8)
            except subprocess.TimeoutExpired:
                app.kill()
    ff.wait()
    print(f"    app log: {app_log}")
    # Surface the adaptive loop's own R telemetry if it logged any (condition A).
    try:
        hits = [ln for ln in Path(app_log).read_text().splitlines() if "adaptive NMI rate" in ln]
        for ln in hits[-4:]:
            print(f"    [log] {ln.split('] ', 1)[-1]}")
    except OSError:
        pass
    analyze(wav, clip)
    return wav


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--clip", default=DEFAULT_CLIP, help="video clip to play (default dsp_test_music)"
    )
    ap.add_argument("--url", default="u64://192.168.2.64", help="connection target")
    ap.add_argument("--seconds", type=float, default=22.0, help="play seconds per condition")
    ap.add_argument(
        "--avf-audio", default=d.CAMLINK_AVF_AUDIO, help="ffmpeg avfoundation audio input"
    )
    ap.add_argument(
        "--only",
        nargs="+",
        choices=["adaptive", "static_raw", "static_comp"],
        help="run only these conditions (default: all three)",
    )
    ap.add_argument("--no-reset", action="store_true", help="leave the machine running")
    ap.add_argument("--analyze-only", metavar="WAV", help="skip playback; analyze an existing wav")
    args = ap.parse_args()

    if args.analyze_only:
        analyze(args.analyze_only, args.clip)
        return 0

    if not Path(args.clip).exists():
        ap.error(f"clip not found: {args.clip}")

    # (label, adaptive, pitch_mult_mhires)
    conditions = [
        ("adaptive", True, 1.015),
        ("static_raw", False, 1.0),
        ("static_comp", False, 1.015),
    ]
    if args.only:
        conditions = [c for c in conditions if c[0] in args.only]

    try:
        for label, adaptive, pm in conditions:
            run_condition(args.url, args.clip, label, adaptive, pm, args.seconds, args.avf_audio)
            time.sleep(0.5)
    finally:
        if not args.no_reset:
            code = d.rest_reset(args.url if args.url.startswith("http") else d.U64_URL)
            print(f"\n[reset] {'HTTP ' + str(code) if code else 'FAILED'}")

    print(
        "\nRead: static_raw's overall ratio == the RAW bus-halt loss under current "
        "fps caps (pitch = R/sample_rate). adaptive's GLIDE + steady offset == the "
        "loop's cost. Ears decide too."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
