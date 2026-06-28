#!/usr/bin/env python3
"""Measure the Ultimate Audio sampler's TRUE playback rate on a given machine,
artifact-free, and report the effective reference clock to plug into
``[audio].sampler_clock_hz``.

Why this exists
---------------
The sampler streams PCM from a REU ring; the host paces the ring write head off
the *host* monotonic clock while the FPGA clocks samples out at REF/divider. If a
unit's real REF differs from the firmware's nominal 6.25 MHz, the audio plays
off-speed and drifts against the (host-clock-paced) video — audible as the beep
sliding off the flash on an A/V-sync test, worse over minutes. Host-side
telemetry can't see it (it's all referenced to the same host clock), and the
avfoundation audio capture drops samples (corrupting *timing*) — but it preserves
*pitch*. So this plays a sustained tone through the PRODUCTION sampler, captures
the HDMI audio, and measures the tone's pitch: pitch/nominal = real_rate/assumed,
which yields the effective REF.

Usage
-----
    scripts/diags/sampler_clock_calib.py                       # default machine
    scripts/diags/sampler_clock_calib.py --url u64://192.168.2.64
    scripts/diags/sampler_clock_calib.py --url http://192.168.2.65   # U2+
    scripts/diags/sampler_clock_calib.py --ref 6120000         # verify a candidate REF
    scripts/diags/sampler_clock_calib.py --analyze-only out/<file>.wav

Re-run after any firmware update (or on a new unit) to re-measure. Requires the
Cam Link rig (HDMI->USB) and ffmpeg; resets the machine on exit unless --no-reset.
"""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from pathlib import Path

import _diaglib as d
import numpy as np

NOMINAL_REF = 6_250_000  # firmware design value (effective 50 MHz / 8)


def _fft_pitch(seg: np.ndarray, sr: int) -> float | None:
    """Parabolic-interpolated dominant frequency of one window."""
    if len(seg) < sr // 2:
        return None
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    k = int(np.argmax(sp[1:])) + 1
    if not (1 < k < len(sp) - 1):
        return None
    lo, mid, hival = (np.log(sp[k + o] + 1e-12) for o in (-1, 0, 1))
    delta = 0.5 * (lo - hival) / (lo - 2 * mid + hival)
    return (k + delta) * sr / len(seg)


def measure_pitch(wav: Path, nominal_freq: float) -> tuple[float, float] | None:
    """(median pitch, half-spread) Hz of the sustained tone in ``wav``.

    Mono-downmixes (stereo interleave would otherwise halve the apparent pitch),
    finds the tonal region, then takes the MEDIAN per-2s-window FFT pitch across
    it — robust to the startup underrun pads (silence gaps) that bias a single
    whole-window FFT (~1% run-to-run otherwise). Half-spread (p84−p16)/2 reports
    confidence. Returns None if no steady tone."""
    import av

    cont = av.open(str(wav))
    a = cont.streams.audio[0]
    sr = a.sample_rate
    rs = av.AudioResampler(format="s16", layout="mono", rate=sr)
    chunks = []
    for fr in cont.decode(a):
        for r in rs.resample(fr):
            chunks.append(r.to_ndarray().reshape(-1).astype(np.float64))
    cont.close()
    if not chunks:
        return None
    sig = np.concatenate(chunks)
    peak = np.abs(sig).max()
    if peak <= 0:
        return None
    sig /= peak
    ewin = int(0.05 * sr)
    n = len(sig) // ewin
    env = np.sqrt((sig[: n * ewin].reshape(n, ewin) ** 2).mean(axis=1) + 1e-12)
    hi = np.where(env > 0.5 * env.max())[0]
    if len(hi) < 80:  # < ~4s of tone
        return None
    start = hi[0] * ewin + int(2 * sr)  # skip gate/feeder transients
    end = hi[-1] * ewin - int(2 * sr)
    region = sig[start:end]
    if len(region) < 2 * sr:
        return None
    # Per-2s-window FFT pitch; drop windows with a silence gap (pad) — their RMS
    # dips well below the region median — then take the median of the rest.
    w = 2 * sr
    pitches = []
    for i in range(0, len(region) - w, w):
        seg = region[i : i + w]
        if np.sqrt((seg**2).mean()) < 0.1:  # contains a pad/silence
            continue
        p = _fft_pitch(seg, sr)
        if p is not None and 0.5 * nominal_freq < p < 2 * nominal_freq:
            pitches.append(p)
    if len(pitches) < 3:
        return None
    arr = np.array(pitches)
    return float(np.median(arr)), float((np.percentile(arr, 84) - np.percentile(arr, 16)) / 2)


def play_tone(url: str, seconds: float, freq: float, ref_hz: int, no_reset: bool) -> None:
    import c64cast.config as cfgmod
    import c64cast.doctor as doctor
    from c64cast.backend import make_backend
    from c64cast.connect import apply_to_config, parse_connection_uri
    from c64cast.sampler import UltimateAudioSampler

    cfg = cfgmod.Config()
    apply_to_config(cfg, parse_connection_uri(url))
    cfg.ultimate64.auto_reu = True
    api = make_backend(cfg)
    reu_restore = doctor.provision_reu(api, cfg)  # noqa: F841 (kept on the unit for the run)
    samp_restore = doctor.provision_sampler(api, cfg)
    try:
        sampler = UltimateAudioSampler(api, sample_rate=44100, bits=16, ref_clock_hz=ref_hz)
        rate = sampler.sample_rate
        print(f"[play] ref={ref_hz} Hz -> sample_rate {rate} Hz; tone {freq} Hz for {seconds}s")
        t = np.arange(int(seconds * rate)) / rate
        tone = (0.5 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
        sampler.start()

        def feed() -> None:
            chunk = int(rate * 0.1)
            for i in range(0, len(tone), chunk):
                sampler.push_samples(tone[i : i + chunk])

        th = threading.Thread(target=feed, daemon=True)
        th.start()
        time.sleep(seconds + 1.0)
        sampler.mark_eof()
        time.sleep(0.4)
        sampler.stop()
    finally:
        doctor.restore_sampler(api, samp_restore)
        if not no_reset:
            d.rest_reset(url if url.startswith("http") else d.U64_URL)


def report(pitch: float, spread: float, freq: float, used_ref: int) -> None:
    ratio = pitch / freq
    real_ref = ratio * used_ref
    print(
        f"\n  measured pitch     : {pitch:.2f} Hz (nominal {freq:.0f}, "
        f"±{spread / freq * 100:.2f}% across windows)"
    )
    print(
        f"  rate ratio         : {ratio:.5f}  (audio {'FAST' if ratio > 1 else 'SLOW'} "
        f"{abs(ratio - 1) * 100:.3f}%)"
    )
    print(f"  ref clock used      : {used_ref} Hz")
    print(f"  => effective REF    : {real_ref:,.0f} Hz  (±{spread / freq * real_ref:,.0f})")
    print(
        f"  => set [audio].sampler_clock_hz = {round(real_ref / 1000) * 1000} (STARTING estimate)"
    )
    print(
        f"  residual drift over 60/300s if used as-is: "
        f"{abs(ratio - 1) * 60:.2f}/{abs(ratio - 1) * 300:.2f} s"
    )
    print("  NOTE: this is a starting point — the capture path (Cam Link ASRC / HDMI")
    print("  audio clock) carries its own ~0.5% offset vs the host clock that paces")
    print("  the video, so fine-tune by EAR against a video: beep drifts BEHIND the")
    print("  flash -> lower the value; drifts AHEAD -> raise it (~6000 Hz per 0.1%).")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help="connection target (u64://host, http://host)")
    ap.add_argument("--seconds", type=float, default=24.0, help="tone duration")
    ap.add_argument("--freq", type=float, default=1000.0, help="tone frequency (Hz)")
    ap.add_argument(
        "--ref",
        type=int,
        default=NOMINAL_REF,
        help=f"sampler reference clock to drive playback with (default {NOMINAL_REF}). "
        "Use the nominal to MEASURE the real REF; pass a candidate to VERIFY it (pitch "
        "should land on --freq).",
    )
    ap.add_argument("--no-reset", action="store_true", help="leave the machine running")
    ap.add_argument("--analyze-only", metavar="WAV", help="skip playback; analyze an existing wav")
    args = ap.parse_args()

    if args.analyze_only:
        res = measure_pitch(Path(args.analyze_only), args.freq)
        if res is None:
            print("!! no steady tone found")
            return 1
        report(res[0], res[1], args.freq, args.ref)
        return 0

    wav = d.stamped("sampler_clock", "wav")
    cap_secs = args.seconds + 6
    print(f"[cap] {cap_secs:.0f}s of HDMI audio ({d.CAMLINK_AVF_AUDIO}) -> {wav}")
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
    try:
        play_tone(args.url, args.seconds, args.freq, args.ref, args.no_reset)
    finally:
        ff.wait()
    res = measure_pitch(wav, args.freq)
    if res is None:
        print(f"!! no steady tone in {wav} (check capture device / volume)")
        return 1
    report(res[0], res[1], args.freq, args.ref)
    print(f"\n  wav: {wav}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
