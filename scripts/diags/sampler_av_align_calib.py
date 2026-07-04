#!/usr/bin/env python3
"""Highly-accurate, capture-artifact-IMMUNE measurement of the Ultimate Audio
sampler's true reference clock, via a differential SID-vs-sampler drift run.

Why this method
---------------
The sampler plays PCM out of a REU ring at REF/divider; a U64's real REF differs
from the firmware-nominal 6.25 MHz (~2% slow), so sampler audio drifts against the
host-clock-paced video. Measuring that with a single captured pitch is limited by
the Cam Link path's own ASRC offset (~0.5%) AND a uniform ~9-10% sample-rate
compression in the avfoundation capture (see avfoundation_capture_drops_samples).

This tool sidesteps ALL of that. At each wall-clock instant t_k = k*period it emits
TWO markers that both land in the SAME captured audio stream:

  * a SID tone burst (~700 Hz) — SID is clocked by the ACCURATE C64 system crystal,
    and the burst is host-triggered, so it marks true wall-clock time; AND
  * a sampler tone burst (~2500 Hz) pre-placed at sampler sample-index k*period*rate,
    so it plays out on the SAMPLER clock.
  (plus a border flash at the same instant, for visual A/V confirmation on video.)

If the sampler clock were exact, the two bursts coincide at every k. If it runs
slow, the sampler burst arrives progressively later. Fit onset times vs event index
for each band: slope_sid = period*f, slope_samp = period*f/ratio  (f = the capture's
unknown time-scale factor). Then ratio = slope_sid/slope_samp — f CANCELS. The
effective REF = nominal * ratio. One long run yields the answer; a second run
programmed AT that REF should show ~zero residual drift (confirmation).

Usage
-----
    scripts/diags/sampler_av_align_calib.py                       # measure @ nominal
    scripts/diags/sampler_av_align_calib.py --ref 6140000         # confirm a candidate
    scripts/diags/sampler_av_align_calib.py --seconds 150 --period 5
    scripts/diags/sampler_av_align_calib.py --analyze-only out/<file>.wav

Needs the Cam Link HDMI-audio rig + ffmpeg. Writes NO REST reads during capture
(SID/flash are DMA writes; the sampler is off-bus). Resets the machine on exit
unless --no-reset.
"""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
import wave
from pathlib import Path

import _diaglib as d
import numpy as np

NOMINAL_REF = 6_250_000
SID_HZ_BAND = (500.0, 950.0)  # SID burst ~700 Hz (accurate wall-clock marker)
SAMP_HZ_BAND = (2150.0, 2850.0)  # sampler burst ~2500 Hz (drifting signal)
BURST_S = 0.12  # marker burst duration


# ------------------------------------------------------------------ analysis
def _read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def _band_envelope(sig: np.ndarray, sr: int, band: tuple[float, float], hop_s: float = 0.004):
    """Short-time energy in [lo,hi] Hz. Returns (envelope, times)."""
    win = int(sr * 0.020)
    hop = max(1, int(sr * hop_s))
    lo, hi = band
    nfft = 1 << (int(win - 1).bit_length())
    freqs = np.fft.rfftfreq(nfft, 1 / sr)
    mask = (freqs >= lo) & (freqs <= hi)
    wnd = np.hanning(win).astype(np.float32)
    env = []
    times = []
    for start in range(0, len(sig) - win, hop):
        seg = sig[start : start + win] * wnd
        sp = np.abs(np.fft.rfft(seg, nfft))
        env.append(float(sp[mask].sum()))
        times.append((start + win / 2) / sr)
    return np.asarray(env), np.asarray(times)


def _onsets(env: np.ndarray, times: np.ndarray, period_hint_s: float) -> np.ndarray:
    """Rising-edge onset times: threshold at 35% of peak, min-gap ~0.5*period."""
    if env.max() <= 0:
        return np.asarray([])
    thr = 0.35 * env.max()
    above = env > thr
    onsets = []
    last = -1e9
    dt = times[1] - times[0] if len(times) > 1 else 0.004
    min_gap = 0.5 * period_hint_s
    for i in range(1, len(above)):
        if above[i] and not above[i - 1] and (times[i] - last) > min_gap:
            # sub-frame refine: linear crossing of thr between i-1 and i
            e0, e1 = env[i - 1], env[i]
            frac = 0.0 if e1 == e0 else (thr - e0) / (e1 - e0)
            t = times[i - 1] + frac * dt
            onsets.append(t)
            last = t
    return np.asarray(onsets)


def _fit_slope(onsets: np.ndarray) -> tuple[float, float]:
    """Linear fit onset ~ a + b*index. Returns (slope, r2)."""
    k = np.arange(len(onsets), dtype=np.float64)
    b, a = np.polyfit(k, onsets, 1)
    pred = a + b * k
    ss_res = float(((onsets - pred) ** 2).sum())
    ss_tot = float(((onsets - onsets.mean()) ** 2).sum()) or 1e-12
    return float(b), 1.0 - ss_res / ss_tot


def analyze(wav: Path, period_s: float, used_ref: int) -> int:
    sig, sr = _read_wav_mono(wav)
    dur = len(sig) / sr
    print(f"[analyze] {wav.name}: {dur:.1f}s @ {sr} Hz")
    se, st = _band_envelope(sig, sr, SID_HZ_BAND)
    me, mt = _band_envelope(sig, sr, SAMP_HZ_BAND)
    sid = _onsets(se, st, period_s)
    samp = _onsets(me, mt, period_s)
    print(f"  SID markers: {len(sid)}   sampler markers: {len(samp)}")
    n = min(len(sid), len(samp))
    if n < 4:
        print("  !! too few markers detected — check capture level / bands")
        return 1
    sid, samp = sid[:n], samp[:n]
    b_sid, r2_sid = _fit_slope(sid)
    b_samp, r2_samp = _fit_slope(samp)
    print(f"  slope SID     = {b_sid:.5f} s/interval (r2={r2_sid:.5f}, nominal {period_s})")
    print(f"  slope sampler = {b_samp:.5f} s/interval (r2={r2_samp:.5f})")
    if b_samp <= 0:
        print("  !! non-positive sampler slope — bad detection")
        return 1
    ratio = b_sid / b_samp  # true_rate / assumed_rate; f cancels
    eff_ref = used_ref * ratio
    drift_pct = (1.0 - ratio) * 100.0
    # residual drift of the sampler burst vs the SID reference, per interval:
    resid_ms = (b_samp - b_sid) * 1000.0
    print(
        f"\n  ratio (true/assumed) = {ratio:.5f}  "
        f"(sampler {'SLOW' if ratio < 1 else 'FAST'} {abs(drift_pct):.3f}%)"
    )
    print(f"  drift vs SID ref     = {resid_ms:+.2f} ms per {period_s:.0f}s interval")
    print(f"  ref clock driven     = {used_ref:,} Hz")
    print(f"  => EFFECTIVE REF     = {eff_ref:,.0f} Hz")
    print(f"  => set sampler_clock_hz = {round(eff_ref / 1000) * 1000}")
    if used_ref != NOMINAL_REF:
        print(
            f"  (confirmation run: residual {resid_ms:+.2f} ms/interval "
            f"⇒ {'ALIGNED ✓' if abs(resid_ms) < 3 else 'still drifting — refine'})"
        )
    return 0


# ------------------------------------------------------------------ playback
def _sid_setup(api) -> None:
    from c64cast.c64 import SID

    api.write_memory(f"{SID.MODE_VOL:04X}", "0F")  # master vol 15
    api.write_memory(f"{SID.BASE + 5:04X}", "00")  # AD = 0 (instant attack)
    api.write_memory(f"{SID.BASE + 6:04X}", "F0")  # SR = sustain 15 / release 0
    api.write_memory(f"{SID.BASE + 0:04X}", "00")  # freq lo
    api.write_memory(f"{SID.BASE + 1:04X}", "2D")  # freq hi -> ~700 Hz
    api.flush()


def _sid_gate(api, on: bool) -> None:
    from c64cast.c64 import SID

    api.write_memory(f"{SID.BASE + 4:04X}", "11" if on else "10")  # triangle + gate
    api.flush()


def _border(api, color: int) -> None:
    api.write_memory("D020", f"{color:02X}")
    api.flush()


def _gen_content(rate: int, run_s: float, period_s: float) -> tuple[np.ndarray, int]:
    """Silence with a 2500 Hz burst at each k*period sample position."""
    total = int((run_s + 2.0) * rate)
    buf = np.zeros(total, dtype=np.int16)
    nb = int(BURST_S * rate)
    env = np.hanning(nb).astype(np.float32)
    k = 1
    events = 0
    while k * period_s <= run_s:
        i0 = int(round(k * period_s * rate))
        if i0 + nb < total:
            t = np.arange(nb) / rate
            burst = (0.6 * env * np.sin(2 * np.pi * 2500.0 * t) * 32767).astype(np.int16)
            buf[i0 : i0 + nb] = burst
            events += 1
        k += 1
    return buf, events


def play_and_capture(url: str, ref_hz: int, run_s: float, period_s: float, no_reset: bool) -> Path:
    import c64cast.config as cfgmod
    import c64cast.doctor as doctor
    from c64cast.backend import make_backend
    from c64cast.connect import apply_to_config, parse_connection_uri
    from c64cast.sampler import UltimateAudioSampler

    cfg = cfgmod.Config()
    apply_to_config(cfg, parse_connection_uri(url))
    cfg.ultimate64.auto_reu = True
    api = make_backend(cfg)
    rest_url = url.replace("u64://", "http://").split("?", 1)[0]
    if not rest_url.startswith("http"):
        rest_url = d.U64_URL

    reu_restore = doctor.provision_reu(api, cfg)
    samp_restore = doctor.provision_sampler(api, cfg)
    wav = d.stamped(f"samp_align_ref{ref_hz}", "wav")
    try:
        sampler = UltimateAudioSampler(api, sample_rate=44100, bits=16, ref_clock_hz=ref_hz)
        rate = sampler.sample_rate
        content, nevents = _gen_content(rate, run_s, period_s)
        print(
            f"[play] ref {ref_hz} Hz -> rate {rate} Hz; {nevents} markers @ {period_s}s over {run_s}s"
        )
        _sid_setup(api)

        cap_secs = run_s + 8
        print(f"[cap] {cap_secs:.0f}s HDMI audio ({d.CAMLINK_AVF_AUDIO}) -> {wav.name}")
        ff = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "avfoundation",
                "-i",
                d.CAMLINK_AVF_AUDIO,
                "-t",
                str(cap_secs),
                "-y",
                str(wav),
            ],
        )
        time.sleep(1.5)  # let ffmpeg warm up before markers begin

        sampler.start()
        t0 = sampler._gate_time  # audio-position 0 == this monotonic instant

        def feed() -> None:
            chunk = int(rate * 0.1)
            for i in range(0, len(content), chunk):
                sampler.push_samples(content[i : i + chunk])
            sampler.mark_eof()

        threading.Thread(target=feed, daemon=True).start()

        # Marker scheduler: SID burst + border flash at wall-clock t0 + k*period.
        k = 1
        while k * period_s <= run_s:
            target = t0 + k * period_s
            now = time.monotonic()
            if target - now > 0:
                time.sleep(target - now)
            _sid_gate(api, True)
            _border(api, 0x02)  # red flash
            time.sleep(BURST_S)
            _sid_gate(api, False)
            _border(api, 0x0E)  # back to standard border
            k += 1

        ff.wait()
        sampler.stop()
    finally:
        doctor.restore_sampler(api, samp_restore)
        del reu_restore
        if not no_reset:
            d.rest_reset(rest_url)
            print("[teardown] machine reset.")
    return wav


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="u64://192.168.2.64")
    ap.add_argument(
        "--ref",
        type=int,
        default=NOMINAL_REF,
        help="drive at this REF (nominal=measure; candidate=confirm)",
    )
    ap.add_argument("--seconds", type=float, default=150.0, help="marker run length")
    ap.add_argument("--period", type=float, default=5.0, help="seconds between markers")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--analyze-only", metavar="WAV")
    a = ap.parse_args()
    if a.analyze_only:
        return analyze(Path(a.analyze_only), a.period, a.ref)
    wav = play_and_capture(a.url, a.ref, a.seconds, a.period, a.no_reset)
    print()
    return analyze(wav, a.period, a.ref)


if __name__ == "__main__":
    raise SystemExit(main())
