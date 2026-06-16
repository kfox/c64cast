#!/usr/bin/env python3
"""Measure c64cast's audio playback TEMPO vs the source and the A/V SYNC offset,
as ground truth off the Cam Link — the instrument for validating the host-DMA
"resample-of-residual" pitch compensation.

Two questions the R-probe (hostdma_drift_probe.py) can't answer from the C64
side, answered here from the CAPTURE:

  1. TEMPO — how slow (or fast) playback actually is. Playback speed =
     R / sample_rate; on heavy bitmap content R parks below sample_rate (bus-halt
     tick loss) so audio plays slow. This measures the real slowdown by locating
     known markers in the captured, played-back audio.
  2. A/V SYNC — whether the displayed frame lines up with the played audio, via
     the bus-clean $D020 border-flash marker (the run owns the border) detected
     in the captured video.

Tempo methods:
  * --gen-source (preferred, most robust): synthesize a clip that is a leading +
    trailing 100 ms chirp (c64cast.audio_marker) bracketing a tone bed, muxed to
    flat-colour video. The chirps survive the 4-bit sample-and-hold DAC, so
    cross-correlating the capture against the DAC-modelled reference finds both
    unambiguously; tempo = source_interval / captured_interval.
  * --source PATH (real content): decode the source, model it through the DAC,
    take the amplitude ENVELOPE of both source and capture (the envelope survives
    the lo-fi DAC; the raw waveform does not), and grid-search the tempo ratio
    that maximises envelope cross-correlation (scipy.signal.resample_poly).

Honest limitation (printed in the report): the border flash is host-driven and
visual-only, so the A/V offset is RELATIVE — it carries a constant
ring-latency/boot bias and is for COMPARING postures + catching gross desync, not
an absolute lip-sync number. An app-side coincident audio-click hook would make
it absolute (see the module note in the plan); not implemented here.

Usage:
    # capture + analyse on real hardware
    scripts/diags/av_sync_probe.py --config posture.toml --gen-source -t 25
    scripts/diags/av_sync_probe.py --config clip.toml --source clip.mp4 -t 30
    # re-run analysis on a saved capture (no hardware)
    scripts/diags/av_sync_probe.py --analyze out/avsync_20260615.mov --bed-s 8
    # offline self-check (no hardware): synth a known-tempo capture + verify
    scripts/diags/av_sync_probe.py --selftest

Dev-only tool: needs the `dev` group (scipy) + the `video` extra (PyAV). All
hardware defaults come from _diaglib and are env/flag overridable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import _diaglib as d
import numpy as np

from c64cast.audio_marker import MARKER_DURATION_S, _chirp_float

CAP_RATE = 48000  # Cam Link / avfoundation capture rate (mono)
BED_FREQ_HZ = 440.0  # tone bed between the chirps (gen-source)
DEFAULT_PLAYBACK_RATE = 10500  # matches AudioCfg.sample_rate default


# --------------------------------------------------------------------------- #
# audio I/O
# --------------------------------------------------------------------------- #
def _decode_audio_mono(path: str, rate: int = CAP_RATE) -> np.ndarray:
    """Decode any media/wav file's audio to float64 mono at `rate` (reuses the
    production PyAV decode so the resample matches c64cast)."""
    from c64cast.video import decode_audio_full

    pcm = decode_audio_full(str(path), rate)
    return pcm.astype(np.float64)


def _write_wav(path: Path, floats: np.ndarray, rate: int = CAP_RATE) -> None:
    """Write float [-1, 1] mono to a 16-bit PCM WAV (scipy.io.wavfile)."""
    from scipy.io import wavfile

    pcm = np.clip(floats, -1.0, 1.0)
    wavfile.write(str(path), rate, (pcm * 32767.0).astype(np.int16))


# --------------------------------------------------------------------------- #
# cross-correlation + chirp-pair tempo
# --------------------------------------------------------------------------- #
def _xcorr_fft(sig: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Mean-subtracted FFT cross-correlation of `ref` against `sig` (amplitude-
    invariant). corr[i] peaks where `ref` begins in `sig`."""
    sig = sig - sig.mean()
    ref = ref - ref.mean()
    n = len(sig) + len(ref) - 1
    nfft = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(sig, nfft) * np.conj(np.fft.rfft(ref, nfft)), nfft)
    return corr[: len(sig)]


def _refine_peak(corr: np.ndarray, i: int) -> float:
    """Sub-sample peak position by parabolic interpolation around index i."""
    if 0 < i < len(corr) - 1:
        a, b, c = corr[i - 1], corr[i], corr[i + 1]
        denom = a - 2 * b + c
        if denom != 0:
            return i + 0.5 * (a - c) / denom
    return float(i)


def _chirp_reference(playback_rate: int, cap_rate: int = CAP_RATE) -> np.ndarray:
    """The marker chirp as it appears at the capture device: 4-bit DAC codes at
    the playback rate, sample-and-held to the capture rate at the EXACT (non-
    integer) cap/playback ratio. c64cast.audio_marker.synthesize_capture_reference
    floors that ratio, which distorts the timebase when cap_rate isn't a multiple
    of playback_rate (e.g. 48000/10500 → 4 vs the true 4.571) and depresses the
    correlation; the exact hold here keeps the reference matched at any rate."""
    floats = _chirp_float(playback_rate)
    codes = np.clip(np.round((floats + 1.0) * 7.5), 0, 15) - 7.5  # 4-bit, centred
    hold = cap_rate / playback_rate
    idx = np.floor(np.arange(int(round(len(codes) * hold))) / hold).astype(int)
    return codes[np.clip(idx, 0, len(codes) - 1)].astype(np.float64)


def _normalized_xcorr(sig: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Normalised cross-correlation coefficient (~0..1) of `ref` against `sig`:
    the raw correlation divided by the matched-filter energy and the sliding
    local window energy. A real chirp match peaks near 0.3-0.8; a tone bed or
    noise stays low — a scale-free confidence the bare MAD metric can't give."""
    sig0 = sig - sig.mean()
    ref0 = ref - ref.mean()
    raw = _xcorr_fft(sig, ref)
    ref_e = float(np.sqrt(np.sum(ref0 * ref0))) or 1.0
    n = len(ref0)
    csum = np.cumsum(np.concatenate([[0.0], sig0 * sig0]))
    local_e = np.sqrt(np.maximum(csum[n:] - csum[:-n], 0.0))  # energy of sig0[i:i+n]
    m = min(len(raw), len(local_e))
    ncc = np.zeros(len(raw))
    ncc[:m] = raw[:m] / (ref_e * local_e[:m] + 1e-9)
    return ncc


def _signal_region(x: np.ndarray, cap_rate: int, frame_s: float = 0.05) -> tuple[int, int]:
    """Sample indices [onset, offset) of the played clip, trimming boot/teardown
    silence via a coarse 50 ms-frame RMS gate (active = above 10% of the loudest
    frame). The chirps bracket this region; everything between is the tone bed."""
    n = max(1, int(frame_s * cap_rate))
    nf = len(x) // n
    if nf < 2:
        return 0, len(x)
    rms = np.sqrt((x[: nf * n].reshape(nf, n).astype(np.float64) ** 2).mean(axis=1))
    active = np.where(rms > 0.1 * rms.max())[0]
    if active.size == 0:
        return 0, len(x)
    return int(active[0] * n), int((active[-1] + 1) * n)


def find_chirp_pair(
    capture: np.ndarray, playback_rate: int, cap_rate: int = CAP_RATE, edge_win_s: float = 1.5
) -> tuple[float, float, float]:
    """Locate the leading + trailing chirp starts (sub-sample) plus a normalised-
    correlation confidence (~0..1). The played clip is chirp-bed-chirp, so the
    chirps sit at the EDGES of the active signal region — search the NCC for the
    best peak near the onset (lead) and near the offset (trail). Edge-constraining
    beats "two strongest peaks anywhere": the steady bed throws false peaks the
    global search latched onto on real captures."""
    cap = capture.astype(np.float64)
    ncc = _normalized_xcorr(cap, _chirp_reference(playback_rate, cap_rate))
    onset, offset = _signal_region(cap, cap_rate)
    win = int(edge_win_s * cap_rate)
    back = win // 3  # allow a little before the gated onset / after the offset
    lead_lo, lead_hi = max(0, onset - back), min(len(ncc), onset + win)
    trail_lo, trail_hi = max(0, offset - win), min(len(ncc), offset + back)
    lead_i = lead_lo + int(np.argmax(ncc[lead_lo:lead_hi])) if lead_hi > lead_lo else onset
    trail_i = trail_lo + int(np.argmax(ncc[trail_lo:trail_hi])) if trail_hi > trail_lo else offset
    conf = float(min(ncc[lead_i], ncc[trail_i]))  # both chirps must be strong
    return _refine_peak(ncc, lead_i), _refine_peak(ncc, trail_i), conf


def _matched_confidence(
    capture: np.ndarray, playback_rate: int, ratio: float, lead: float, cap_rate: int
) -> float:
    """Normalised-correlation fit quality at the MEASURED tempo: stretch the
    reference chirp by 1/ratio so its sweep rate matches the slowed capture, then
    take the NCC near the located lead. This makes confidence reflect how well the
    chirp actually fits, not how far the tempo is from 1.0 (a fixed-tempo
    reference correlates worse the more the capture is stretched)."""
    from scipy.signal import resample_poly

    ref = _chirp_reference(playback_rate, cap_rate)
    num, den = _ratio_to_fraction(1.0 / ratio)
    ref_s = resample_poly(ref, num, den) if (num, den) != (1, 1) else ref
    ncc = _normalized_xcorr(capture.astype(np.float64), ref_s)
    w = len(ref_s)
    lo, hi = max(0, int(lead) - w), min(len(ncc), int(lead) + w)
    return float(np.max(ncc[lo:hi])) if hi > lo else 0.0


def measure_tempo_chirp(
    capture: np.ndarray, bed_s: float, playback_rate: int, cap_rate: int = CAP_RATE
) -> dict:
    """Tempo ratio from the chirp-pair: source start-to-start interval is
    MARKER_DURATION_S + bed_s; captured interval is (trail - lead) / cap_rate.
    ratio = source / captured (< 1.0 = playback is that fraction slow)."""
    lead, trail, _rough = find_chirp_pair(capture, playback_rate, cap_rate)
    src_interval = MARKER_DURATION_S + bed_s
    cap_interval = (trail - lead) / cap_rate
    if cap_interval <= 0:
        return {"method": "chirp_pair", "tempo_ratio": 0.0, "confidence": 0.0, "ok": False}
    ratio = src_interval / cap_interval
    conf = _matched_confidence(capture, playback_rate, ratio, lead, cap_rate)
    return {
        "method": "chirp_pair",
        "tempo_ratio": round(ratio, 5),
        "slowdown_pct": round((1.0 - ratio) * 100.0, 3),
        "confidence": round(conf, 3),
        "lead_s": round(lead / cap_rate, 4),
        "trail_s": round(trail / cap_rate, 4),
        "ok": conf >= 0.3,
    }


# --------------------------------------------------------------------------- #
# envelope-grid tempo (real content)
# --------------------------------------------------------------------------- #
def _dac_model(src_48k: np.ndarray, playback_rate: int, cap_rate: int = CAP_RATE) -> np.ndarray:
    """Model real source audio through the 4-bit sample-and-hold DAC: downsample
    to playback_rate, quantise to 4 bits, hold each code to cap_rate. Makes the
    reference's spectral envelope match the captured lo-fi audio."""
    from scipy.signal import resample_poly

    peak = float(np.max(np.abs(src_48k))) or 1.0
    norm = src_48k / peak
    g = _gcd(cap_rate, playback_rate)
    pb = resample_poly(norm, playback_rate // g, cap_rate // g)
    codes = np.clip(np.round((pb + 1.0) * 7.5), 0, 15) - 7.5  # 4-bit, centred
    factor = max(1, round(cap_rate / playback_rate))
    return np.repeat(codes, factor)


def _gcd(a: int, b: int) -> int:
    import math

    return math.gcd(a, b)


def _envelope(x: np.ndarray, cap_rate: int = CAP_RATE) -> np.ndarray:
    """Amplitude envelope: rectify + ~50 Hz low-pass (the part of the signal that
    survives the lo-fi DAC). Decimated to keep the grid search cheap."""
    from scipy.signal import butter, filtfilt

    b, a = butter(2, 50.0 / (cap_rate / 2), btype="low")
    env = filtfilt(b, a, np.abs(x.astype(np.float64)))
    return env[::10]  # 4.8 kHz envelope rate is plenty


def measure_tempo_envelope(
    capture: np.ndarray, source_48k: np.ndarray, playback_rate: int, cap_rate: int = CAP_RATE
) -> dict:
    """Grid-search the tempo ratio maximising envelope cross-correlation between
    the captured audio and the DAC-modelled source. Robust to the 4-bit DAC
    because it correlates ENVELOPES, not raw waveforms."""
    from scipy.signal import resample_poly

    ref = _dac_model(source_48k, playback_rate, cap_rate)
    cap_env = _envelope(capture, cap_rate)
    ref_env = _envelope(ref, cap_rate)
    cap_env = cap_env - cap_env.mean()

    def best_corr(ratio: float) -> float:
        # Stretch the reference envelope by 1/ratio (slow playback = longer).
        num, den = _ratio_to_fraction(1.0 / ratio)
        r = resample_poly(ref_env, num, den)
        r = r - r.mean()
        if len(r) < 16 or len(cap_env) < 16:
            return 0.0
        c = _xcorr_fft(cap_env, r)
        norm = np.sqrt(np.sum(r * r) * np.sum(cap_env * cap_env)) or 1.0
        return float(np.max(c) / norm)

    coarse = np.arange(0.90, 1.011, 0.005)
    scores = [best_corr(r) for r in coarse]
    r0 = float(coarse[int(np.argmax(scores))])
    fine = np.arange(r0 - 0.005, r0 + 0.0051, 0.001)
    fscores = [best_corr(r) for r in fine]
    best = float(fine[int(np.argmax(fscores))])
    peak = max(fscores)
    # Confidence = peak sharpness (peak vs the ±0.5% shoulder).
    shoulder = max(best_corr(best - 0.005), best_corr(best + 0.005))
    sharp = peak / (shoulder or 1e-9)
    return {
        "method": "envelope_grid",
        "tempo_ratio": round(best, 5),
        "slowdown_pct": round((1.0 - best) * 100.0, 3),
        "confidence": round(peak, 4),
        "peak_sharpness": round(sharp, 3),
        "ok": peak > 0.3 and sharp > 1.05,
    }


def _ratio_to_fraction(x: float, max_den: int = 2000) -> tuple[int, int]:
    """Rational approximation of x for resample_poly (up, down)."""
    from fractions import Fraction

    f = Fraction(x).limit_denominator(max_den)
    return f.numerator, f.denominator


# --------------------------------------------------------------------------- #
# border-flash detection (A/V offset)
# --------------------------------------------------------------------------- #
def detect_video_flashes(path: str, ring_frac: float = 0.08) -> dict:
    """Detect $D020 border-flash pulses in a captured video. Returns flash times
    (video-stream seconds), the border-luma trace, and fps. The border ring (the
    outer `ring_frac` margin) isolates the $D020 change from picture content."""
    import av
    from scipy.signal import find_peaks

    container = av.open(path)
    try:
        if not container.streams.video:
            return {"flashes": [], "fps": 0.0, "times": [], "luma": []}
        vs = container.streams.video[0]
        tb = float(vs.time_base) if vs.time_base else 1.0 / 30.0
        times: list[float] = []
        luma: list[float] = []
        for frame in container.decode(vs):
            img = frame.to_ndarray(format="gray")
            h, w = img.shape
            m = max(1, int(ring_frac * min(h, w)))
            ring = np.concatenate(
                [
                    img[:m, :].ravel(),
                    img[-m:, :].ravel(),
                    img[:, :m].ravel(),
                    img[:, -m:].ravel(),
                ]
            )
            luma.append(float(ring.mean()))
            pts = frame.pts if frame.pts is not None else len(times)
            times.append(float(pts * tb))
    finally:
        container.close()
    if len(luma) < 3:
        return {"flashes": [], "fps": 0.0, "times": times, "luma": luma}
    arr = np.array(luma)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med))) or 1.0
    peaks, _ = find_peaks(arr, height=med + 5.0 * mad, distance=2)
    dur = times[-1] - times[0] if len(times) > 1 else 1.0
    fps = (len(times) - 1) / dur if dur > 0 else 0.0
    return {
        "flashes": [round(times[p], 4) for p in peaks],
        "fps": round(fps, 3),
        "times": times,
        "luma": luma,
    }


def measure_av_offset(video_flashes: list[float], host_flashes: list[float]) -> dict:
    """Relative A/V offset + capture-clock sanity. The host emits flashes at
    `host_flashes` offsets; the video shows them at `video_flashes`. The mean
    (video - host) difference is the end-to-end VIDEO pipeline delay (with a
    constant unknown bias); the spacing comparison catches video stutter/desync.

    Returns a RELATIVE number — see the module docstring's honesty note."""
    out: dict = {"flashes_detected": len(video_flashes), "flashes_logged": len(host_flashes)}
    if len(video_flashes) >= 2:
        out["video_interval_s"] = round(float(np.mean(np.diff(video_flashes))), 4)
    if len(host_flashes) >= 2:
        out["host_interval_s"] = round(float(np.mean(np.diff(host_flashes))), 4)
    n = min(len(video_flashes), len(host_flashes))
    if n >= 1:
        # Align on the first detected flash, then compare residual spacing drift.
        v = np.array(video_flashes[:n]) - video_flashes[0]
        h = np.array(host_flashes[:n]) - host_flashes[0]
        out["spacing_drift_ms_max"] = round(float(np.max(np.abs(v - h)) * 1000.0), 1)
    out["bias_note"] = (
        "relative: host-driven visual-only flash carries a constant ring-latency/"
        "boot bias; compare across postures, not as absolute lip-sync"
    )
    return out


# --------------------------------------------------------------------------- #
# gen-source clip
# --------------------------------------------------------------------------- #
def gen_source_clip(
    out_dir: Path, bed_s: float, label: str, motion: bool = True
) -> tuple[Path, float]:
    """Synthesize a chirp-lead + tone-bed + chirp-trail clip muxed onto video.
    Returns (mp4_path, bed_s). The chirp is the audio_marker chirp at the capture
    rate so it plays through the DAC into the detectable reference.

    motion=True uses a high-motion mandelbrot source so an mhires scene churns
    hard (per-frame REU bank-swap + badline DMA) — that bus-halt load is what
    produces the slowdown the tempo measurement exists to catch. A flat source
    would show ~no slowdown and miss the point. The $D020 border-flash marker is
    in the VIC border, not the active picture, so motion doesn't hurt detection."""
    chirp = _chirp_float(CAP_RATE)
    bed = 0.6 * np.sin(2 * np.pi * BED_FREQ_HZ * np.arange(int(bed_s * CAP_RATE)) / CAP_RATE)
    audio = np.concatenate([chirp, bed, chirp]).astype(np.float64)
    wav = out_dir / f"{label}_gensrc.wav"
    _write_wav(wav, audio, CAP_RATE)
    mp4 = out_dir / f"{label}_gensrc.mp4"
    vsrc = "mandelbrot=s=320x240:rate=30" if motion else "color=c=navy:s=320x240:r=30"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", vsrc,
        "-i", str(wav),
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(mp4),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    return mp4, bed_s


# --------------------------------------------------------------------------- #
# capture (muxed avfoundation, with cv2 fallback)
# --------------------------------------------------------------------------- #
def probe_video_mode(vidx: str) -> tuple[int, int, float] | None:
    """Query the avfoundation device's supported (w, h, fps) — required because
    ffmpeg rejects any framerate but the device's exact reported value."""
    import re

    cmd = [
        "ffmpeg", "-hide_banner", "-f", "avfoundation",
        "-video_size", "99999x99999", "-i", vidx, "-t", "0",
    ]  # fmt: skip
    r = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"(\d{3,4})x(\d{3,4})@\[?([\d.]+)", r.stderr)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), float(m.group(3))


def capture_muxed(
    mov: Path, vidx: str, aidx: str, seconds: float, mode: tuple[int, int, float]
) -> bool:
    """One avfoundation invocation capturing Cam Link video+audio to a single
    container (shared clock — what makes the A/V offset recoverable)."""
    w, h, fps = mode
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "avfoundation", "-framerate", f"{fps}", "-video_size", f"{w}x{h}",
        "-pixel_format", "uyvy422", "-i", f"{vidx}:{aidx}", "-t", f"{seconds:.2f}",
        "-map", "0:v", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-map", "0:a", "-c:a", "pcm_s16le", "-ar", str(CAP_RATE), "-ac", "1",
        str(mov),
    ]  # fmt: skip
    return subprocess.run(cmd, capture_output=True).returncode == 0


# --------------------------------------------------------------------------- #
# R-probe cross-check (best-effort, during the live run)
# --------------------------------------------------------------------------- #
def _r_rate_probe(url: str, stop: threading.Event, samples: list[tuple[float, int]]) -> None:
    # 5 Hz (not 50): a rate fit over ~10 s needs only ~50 samples, and the
    # concurrent flash-writer + c64cast already load the U64's REST server —
    # the earlier 50 Hz poll + flash writes were rejected (Connection reset by
    # peer) and lost the flash markers. Gentle polling keeps both alive.
    from c64cast.audio import NMI_ROUTINE_ADDR, RING_BUFFER_ADDR, RING_BUFFER_SIZE

    read_ptr = NMI_ROUTINE_ADDR + 5
    end = RING_BUFFER_ADDR + RING_BUFFER_SIZE
    while not stop.is_set():
        b = d.rest_readmem(read_ptr, 2, url)
        if b and len(b) == 2:
            addr = b[0] | (b[1] << 8)
            if RING_BUFFER_ADDR <= addr < end:
                samples.append((time.monotonic(), addr))
        stop.wait(0.2)


def _fit_r_rate(samples: list[tuple[float, int]]) -> float | None:
    from c64cast.audio import RING_BUFFER_SIZE

    if len(samples) < 20:
        return None
    ts = [s[0] for s in samples]
    cum, prev = [], samples[0][1]
    total = 0
    for _, a in samples:
        delta = (a - prev) % RING_BUFFER_SIZE
        if delta >= RING_BUFFER_SIZE // 2:  # torn/backward read
            delta = 0
        total += delta
        cum.append(total)
        prev = a
    t0 = ts[0]
    xs = np.array([t - t0 for t in ts])
    ys = np.array(cum, dtype=float)
    slope = float(np.polyfit(xs, ys, 1)[0])
    return slope


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
@dataclass
class Report:
    label: str
    config: str | None = None
    tempo: dict = field(default_factory=dict)
    av_sync: dict = field(default_factory=dict)
    capture: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)


def _print_summary(rep: Report) -> None:
    t = rep.tempo
    print(f"\n=== av_sync_probe: {rep.label} ===")
    if t:
        slow = t.get("slowdown_pct", 0.0)
        print(
            f"  tempo  : ratio={t.get('tempo_ratio')}  ({slow:+.2f}% "
            f"{'slow' if slow > 0 else 'fast'})  via {t.get('method')}  "
            f"conf={t.get('confidence')}  ok={t.get('ok')}"
        )
        if "r_probe_ratio" in t:
            print(
                f"           R-probe ratio={t['r_probe_ratio']}  "
                f"(R≈{t.get('r_rate_bps')} / {t.get('sample_rate')} Hz) — capture is the arbiter"
            )
    av = rep.av_sync
    if av:
        print(
            f"  a/v    : flashes {av.get('flashes_detected')}/{av.get('flashes_logged')}  "
            f"video_int={av.get('video_interval_s')}s host_int={av.get('host_interval_s')}s  "
            f"drift_max={av.get('spacing_drift_ms_max')}ms"
        )
        print(f"           {av.get('bias_note', '')}")
    for w in rep.warnings:
        print(f"  ! {w}")


def _plot(rep: Report, flashes: dict, png: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    luma = flashes.get("luma") or []
    times = flashes.get("times") or []
    if not luma:
        return
    fig, ax = plt.subplots(figsize=(10, 3))
    ax.plot(times, luma, lw=0.8, label="border luma")
    for f in flashes.get("flashes", []):
        ax.axvline(f, color="r", alpha=0.4)
    ax.set_title(f"{rep.label}: border-flash detection ({len(flashes.get('flashes', []))} pulses)")
    ax.set_xlabel("video time (s)")
    fig.tight_layout()
    fig.savefig(str(png), dpi=90)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# analysis (shared by live + --analyze)
# --------------------------------------------------------------------------- #
def analyze_capture(
    rep: Report,
    audio_path: str,
    bed_s: float | None,
    source: str | None,
    playback_rate: int,
    video_path: str | None,
    host_flashes: list[float],
) -> None:
    cap = _decode_audio_mono(audio_path, CAP_RATE)
    rms = float(np.sqrt(np.mean((cap / 32768.0) ** 2))) if cap.size else 0.0
    rep.capture["audio_rms"] = round(rms, 5)
    rep.capture["audio_s"] = round(cap.size / CAP_RATE, 2)
    if cap.size < CAP_RATE:
        rep.warnings.append("captured audio < 1 s; tempo unreliable")
    if rms < 0.005:
        rep.warnings.append(
            f"captured audio is ~silent (rms={rms:.5f}) — the U64 likely wasn't playing "
            "(wedge / audio didn't start), not a measurable result"
        )
    if bed_s is not None:
        rep.tempo = measure_tempo_chirp(cap, bed_s, playback_rate)
    elif source:
        src = _decode_audio_mono(source, CAP_RATE)
        rep.tempo = measure_tempo_envelope(cap, src, playback_rate)
    else:
        rep.warnings.append("no --bed-s/--gen-source layout and no --source; skipping tempo")
    if not rep.tempo.get("ok", True):
        rep.warnings.append("tempo confidence low — check the capture / source content")

    if video_path:
        flashes = detect_video_flashes(video_path)
        rep.av_sync = measure_av_offset(flashes.get("flashes", []), host_flashes)
        rep.capture["fps"] = flashes.get("fps")
        png = d.stamped(f"{rep.label}_avsync", "png")
        _plot(rep, flashes, png)
        rep.capture["plot"] = str(png)


# --------------------------------------------------------------------------- #
# offline self-check
# --------------------------------------------------------------------------- #
def selftest(playback_rate: int) -> int:
    """No hardware: synthesize a chirp-pair source, simulate a capture slowed by a
    known tempo + DAC degradation + noise, and verify the recovered tempo. Also
    exercises flash detection on a synthetic pulsed video."""
    from scipy.signal import resample_poly

    print("[selftest] tempo recovery on a synthetic slowed capture")
    bed_s = 6.0
    chirp = _chirp_float(CAP_RATE)
    bed = 0.6 * np.sin(2 * np.pi * BED_FREQ_HZ * np.arange(int(bed_s * CAP_RATE)) / CAP_RATE)
    source = np.concatenate([chirp, bed, chirp])
    rng = np.random.default_rng(0)
    g = _gcd(CAP_RATE, playback_rate)
    failures = 0
    for true_tempo in (0.93, 0.973, 1.0):
        # Model the real DAC path: downsample to the playback rate, 4-bit
        # quantise, then sample-and-hold each code for CAP_RATE/(rate*tempo)
        # capture samples — i.e. consumed at the slow rate R = rate*tempo. This
        # is the staircase synthesize_capture_reference models, slowed by tempo.
        pb = resample_poly(source, playback_rate // g, CAP_RATE // g)
        codes = (np.clip(np.round((pb + 1.0) * 7.5), 0, 15) - 7.5) / 7.5
        hold = CAP_RATE / (playback_rate * true_tempo)
        idx = np.floor(np.arange(int(len(codes) * hold)) / hold).astype(int)
        cap = codes[np.clip(idx, 0, len(codes) - 1)]
        cap = cap + 0.02 * rng.standard_normal(len(cap))
        cap = np.concatenate([np.zeros(int(0.4 * CAP_RATE)), cap])  # boot silence
        res = measure_tempo_chirp(cap, bed_s, playback_rate)
        err = abs(res["tempo_ratio"] - true_tempo)
        ok = err < 0.01 and res["ok"]
        print(
            f"  true={true_tempo:.3f}  measured={res['tempo_ratio']:.3f}  "
            f"err={err * 100:.2f}%  conf={res['confidence']}  -> {'OK' if ok else 'FAIL'}"
        )
        failures += 0 if ok else 1

    print("[selftest] border-flash detection on a synthetic pulsed video")
    try:
        flashes_ok = _selftest_flashes()
        print(f"  flash detection -> {'OK' if flashes_ok else 'FAIL'}")
        failures += 0 if flashes_ok else 1
    except Exception as e:  # cv2/ffmpeg not available — don't fail the whole check
        print(f"  flash detection SKIPPED ({e})")

    print(f"[selftest] {'PASS' if failures == 0 else f'FAIL ({failures})'}")
    return 0 if failures == 0 else 1


def _selftest_flashes() -> bool:
    import cv2

    out = d.out_dir()
    mp4 = out / "selftest_flashes.mp4"
    fps, n = 30, 150
    w, h = 320, 240
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]
    vw = cv2.VideoWriter(str(mp4), fourcc, fps, (w, h))
    flash_frames = {30, 60, 90, 120}  # 1 s apart at 30 fps
    for i in range(n):
        val = 230 if i in flash_frames else 16
        frame = np.full((h, w, 3), 40, dtype=np.uint8)  # dark picture
        frame[: int(0.08 * h), :] = val  # bright border ring on flash frames
        frame[-int(0.08 * h) :, :] = val
        frame[:, : int(0.08 * w)] = val
        frame[:, -int(0.08 * w) :] = val
        vw.write(frame)
    vw.release()
    res = detect_video_flashes(str(mp4))
    return abs(len(res["flashes"]) - len(flash_frames)) <= 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--config", help="c64cast TOML to launch (captures on hardware)")
    src.add_argument("--attach", action="store_true", help="probe an already-running c64cast")
    src.add_argument("--analyze", metavar="PATH", help="re-run analysis on a saved capture")
    src.add_argument("--selftest", action="store_true", help="offline self-check (no hardware)")
    ap.add_argument("--source", help="real source clip to correlate against (envelope-grid tempo)")
    ap.add_argument(
        "--gen-source", action="store_true", help="generate + play a chirp-anchored test clip"
    )
    ap.add_argument("--bed-s", type=float, default=8.0, help="gen-source tone-bed seconds")
    ap.add_argument("--display", default="mhires", help="gen-source scene display mode")
    ap.add_argument(
        "--pitch-mult",
        type=float,
        default=0.0,
        help="gen-source: set pitch_mult_<display> (host-side resample comp; 0 = "
        "config default). Use back-to-back runs at 1.0 vs X and compare the bed "
        "Hz ratio (capture-clock offset cancels) to validate the fixed resampler.",
    )
    ap.add_argument(
        "--target-fps",
        type=int,
        default=0,
        help="gen-source scene target_fps (0 = system default; 30 avoids the mhires-60fps wedge)",
    )
    ap.add_argument("-t", "--seconds", type=float, default=25.0, help="active run length")
    ap.add_argument("--boot", type=float, default=8.0, help="boot + first-PLAY wait")
    ap.add_argument("--border-flash", type=float, default=2.0, metavar="HZ", help="flash rate")
    ap.add_argument("--flash-color", type=int, default=1)
    ap.add_argument("--sample-rate", type=int, default=DEFAULT_PLAYBACK_RATE)
    ap.add_argument("--label", default="avsync")
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--avf-video", default="0", help="avfoundation VIDEO index (Cam Link = 0)")
    ap.add_argument("--avf-audio", default=d.CAMLINK_AVF_AUDIO)
    ap.add_argument("--no-video", action="store_true", help="audio-only (tempo, no A/V offset)")
    ap.add_argument(
        "--rprobe",
        action="store_true",
        help="poll the C64 read pointer for an R-rate cross-check (adds REST load; off by default)",
    )
    ap.add_argument(
        "--no-flash",
        action="store_true",
        help="skip the border-flash A/V marker (zero REST writes)",
    )
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--json", help="explicit JSON output path")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.sample_rate)

    rep = Report(label=args.label, config=args.config)

    # ---- analyze a saved capture (no hardware) ----
    if args.analyze:
        path = Path(args.analyze)
        if not path.exists():
            ap.error(f"capture not found: {path}")
        host_flashes: list[float] = []
        side = path.with_name(path.stem + "_flashes.json")
        if side.exists():
            host_flashes = json.loads(side.read_text()).get("flash_offsets_s", [])
        bed = args.bed_s if (args.gen_source or args.source is None) else None
        video = None if args.no_video else str(path)
        analyze_capture(rep, str(path), bed, args.source, args.sample_rate, video, host_flashes)
        out = Path(args.json) if args.json else d.stamped(args.label, "json")
        out.write_text(rep.to_json())
        _print_summary(rep)
        print(f"\n[json] {out}")
        return 0

    # ---- live capture on hardware ----
    out = d.out_dir()
    gen_mp4: Path | None = None
    bed_layout: float | None = None
    if args.gen_source:
        print("[gen] synthesizing chirp-anchored source clip")
        gen_mp4, bed_layout = gen_source_clip(out, args.bed_s, args.label)
        cfg_to_launch = _write_gen_config(out, gen_mp4, args)
    elif args.config:
        cfg_to_launch = Path(args.config)
        if not cfg_to_launch.exists():
            ap.error(f"config not found: {cfg_to_launch}")
    elif args.attach:
        cfg_to_launch = None
    else:
        ap.error("need one of --config / --gen-source / --attach / --analyze / --selftest")

    mov = d.stamped(args.label, "mov")
    mode = None if args.no_video else probe_video_mode(args.avf_video)
    cap_len = args.boot + args.seconds + 2.0

    app: subprocess.Popen | None = None
    flash_stop = threading.Event()
    flash_marks: list[float] = []
    r_samples: list[tuple[float, int]] = []
    r_stop = threading.Event()
    cap_proc: subprocess.Popen | None = None
    t0 = time.time()

    try:
        # Capture starts BEFORE launch so boot + first PLAY isn't missed.
        if mode and not args.no_video:
            cap_proc = subprocess.Popen(
                _muxed_cmd(mov, args.avf_video, args.avf_audio.lstrip(":"), cap_len, mode),
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.5)
        else:
            if not args.no_video:
                rep.warnings.append("avfoundation video mode probe failed; audio-only capture")
            wav = d.stamped(args.label + "_audio", "wav")
            cap_proc = _spawn_audio_capture(wav, args.avf_audio, cap_len)
            mov = wav  # analysis reads audio from here; no video → no A/V offset
            time.sleep(1.5)

        if cfg_to_launch is not None:
            print(f"[run] python -m c64cast --config {cfg_to_launch}")
            app = subprocess.Popen(
                [d.python_exe(), "-m", "c64cast", "--config", str(cfg_to_launch), "--url", args.url]
            )
        # Concurrent REST threads contend with the U64's REST server (the 50 Hz
        # poll + flash writes were rejected with Connection-reset and may have
        # aggravated wedges). Both are OPT-OUT / OPT-IN so a clean tempo capture
        # can run with zero concurrent REST: tempo comes from the audio chirps
        # alone. Flash (A/V sync) defaults on but low-rate; R-probe (cross-check
        # only) defaults OFF.
        if not args.no_flash and args.border_flash > 0:
            threading.Thread(
                target=_flash_loop,
                args=(args.url, args.border_flash, args.flash_color, t0, flash_stop, flash_marks),
                daemon=True,
            ).start()
        if args.rprobe:
            threading.Thread(
                target=_r_rate_probe, args=(args.url, r_stop, r_samples), daemon=True
            ).start()

        time.sleep(args.boot + args.seconds)
    finally:
        flash_stop.set()
        r_stop.set()
        if app is not None:
            app.terminate()
            try:
                app.wait(timeout=8)
            except subprocess.TimeoutExpired:
                app.kill()
        if cap_proc is not None:
            try:
                cap_proc.wait(timeout=max(3.0, cap_len))
            except subprocess.TimeoutExpired:
                cap_proc.kill()
        if not args.no_reset and not args.attach:
            print(f"[reset] {args.url}: {d.rest_reset(args.url)}")

    # ---- analyse ----
    video = None if (args.no_video or not str(mov).endswith(".mov")) else str(mov)
    analyze_capture(rep, str(mov), bed_layout, args.source, args.sample_rate, video, flash_marks)
    r_rate = _fit_r_rate(r_samples)
    if r_rate and rep.tempo:
        rep.tempo["r_rate_bps"] = round(r_rate, 1)
        rep.tempo["r_probe_ratio"] = round(r_rate / args.sample_rate, 5)
        rep.tempo["sample_rate"] = args.sample_rate
    out_json = Path(args.json) if args.json else d.stamped(args.label, "json")
    out_json.write_text(rep.to_json())
    _print_summary(rep)
    print(f"\n[json] {out_json}\n[capture] {mov}")
    return 0


def _muxed_cmd(mov, vidx, aidx, seconds, mode):
    w, h, fps = mode
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "avfoundation", "-framerate", f"{fps}", "-video_size", f"{w}x{h}",
        "-pixel_format", "uyvy422", "-i", f"{vidx}:{aidx}", "-t", f"{seconds:.2f}",
        "-map", "0:v", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-map", "0:a", "-c:a", "pcm_s16le", "-ar", str(CAP_RATE), "-ac", "1", str(mov),
    ]  # fmt: skip


def _spawn_audio_capture(wav, aidx, seconds):
    return subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "avfoundation",
            "-i",
            aidx,
            "-t",
            f"{seconds:.2f}",
            "-ac",
            "1",
            "-ar",
            str(CAP_RATE),
            str(wav),
        ],  # fmt: skip
    )


def _flash_loop(url, hz, color, t0, stop, marks):
    period = 1.0 / hz
    nxt = time.monotonic()
    while not stop.is_set():
        now = time.monotonic()
        if now < nxt:
            stop.wait(min(nxt - now, period))
            continue
        nxt += period
        if d.flash_border(url, color):
            marks.append(round(time.time() - t0, 4))
        stop.wait(0.06)
        d.flash_border(url, 0)


def _write_gen_config(out_dir: Path, mp4: Path, args) -> Path:
    """Write a minimal single-scene video config that plays the generated clip
    with audio on (servo-only baseline; the host-DMA servo holds tempo). With
    --pitch-mult X set, also pins the per-display pitch_mult so the fixed-ratio
    resampler runs at ratio = 1/X (back-to-back 1.0 vs X validates the mechanism
    by the bed-Hz ratio, which cancels the capture-clock offset)."""
    cfg = out_dir / f"{args.label}_gen.toml"
    # hires_edges shares pitch_mult_hires (same VIC fetch / mode.name == "hires").
    mult_key = "pitch_mult_hires" if args.display == "hires_edges" else f"pitch_mult_{args.display}"
    pitch_line = f"{mult_key} = {args.pitch_mult}" if args.pitch_mult else ""
    cfg.write_text(
        f'''# auto-generated by av_sync_probe --gen-source
[ultimate64]
system = "NTSC"

[audio]
enabled = true
sample_rate = {args.sample_rate}
{pitch_line}

# Play the clip ONCE — looping would replay the chirp pair every ~8 s, which
# both confuses the chirp-pair tempo detector (it expects exactly one pair) and
# sounds like recurring modulation.
[playlist]
loop = false

[[scenes]]
type = "video"
file = "{mp4}"
display = "{args.display}"
{f"target_fps = {args.target_fps}" if args.target_fps else ""}
'''
    )
    return cfg


if __name__ == "__main__":
    sys.exit(main())
