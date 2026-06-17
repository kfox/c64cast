#!/usr/bin/env python3
"""Characterize the per-DMA-write SID perturbation (the ~chunk-cadence modulation
that survives in the servo-only baseline — see memory u2p_audio_pulsing).

Each host-DMA write of the audio ring (one per chunk_size samples) briefly
perturbs the SID output. On a steady tone that shows up as AM + FM sidebands at
the WRITE CADENCE = sample_rate / chunk_size Hz (and harmonics). This probe plays
a steady carrier through the real host-DMA worker, captures it (Cam Link audio),
and measures the modulation:
  * AM: amplitude-envelope spectrum (modulation index, %)
  * FM: instantaneous-frequency-deviation spectrum (Hz)
and reports the dominant modulation frequency + depth, vs the predicted cadence.

`--no-worker` prefills a STATIC ring (no writes) as the clean floor reference.
`--chunk N` sets the worker chunk_size — the AM/FM peak should move to
sample_rate/N, confirming the perturbation is our writes (predictable → feedforward
cancellable). `--jitter F` adds uniform +-F*chunk_period dither to the write pace
(candidate (a): cadence randomization → should whiten the tonal buzz).

    uv run python scripts/diags/write_perturbation_probe.py --url http://192.168.2.64 -t 16
    uv run python scripts/diags/write_perturbation_probe.py --chunk 512 -t 16
    uv run python scripts/diags/write_perturbation_probe.py --no-worker -t 12   # floor
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
from scipy.signal import hilbert

from c64cast.api import Ultimate64API
from c64cast.audio import (
    RING_BUFFER_ADDR,
    RING_BUFFER_SIZE,
    AudioStreamer,
    encode_floats_to_dac,
)

CAP_RATE = 48000
CARRIER_HZ = 1000.0  # steady tone; well above the modulation band, below Nyquist


def _capture(device: str, seconds: float, out_path: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "avfoundation",
         "-i", device, "-t", str(seconds), "-ac", "1", "-ar", str(CAP_RATE), out_path],
        stderr=subprocess.DEVNULL,
    )  # fmt: skip


def _analyze(wav_path: str, cadence_hz: float) -> dict:
    with wave.open(wav_path, "rb") as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)
    # steady region: drop boot + tail via 0.05s RMS envelope
    win = int(0.05 * sr)
    env0 = np.array([np.sqrt(np.mean(a[i : i + win] ** 2)) for i in range(0, len(a) - win, win)])
    loud = np.where(env0 > 0.5 * env0.max())[0]
    if len(loud) < 20:
        return {"ok": False}
    seg = a[(loud[0] + 4) * win : (loud[-1] - 4) * win]
    # find carrier, bandpass +-300 Hz around it via FFT mask
    F = np.fft.rfft(seg)
    fr = np.fft.rfftfreq(len(seg), 1 / sr)
    car_i = np.argmax(np.abs(F) * ((fr > 400) & (fr < 1600)))
    carrier = fr[car_i]
    mask = (fr > carrier - 300) & (fr < carrier + 300)
    band = np.zeros_like(F)
    band[mask] = F[mask]
    bp = np.fft.irfft(band, n=len(seg))
    analytic = hilbert(bp)
    amp = np.abs(analytic)
    inst_f = np.diff(np.unwrap(np.angle(analytic))) / (2 * np.pi) * sr

    # modulation spectra (mean-removed envelope/freq), look 2..200 Hz
    def mod_spectrum(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        x = x - x.mean()
        X = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        f = np.fft.rfftfreq(len(x), 1 / sr)
        return f, X

    fa, Xa = mod_spectrum(amp)
    ff, Xf = mod_spectrum(inst_f)
    mb = (fa >= 2) & (fa <= 200)
    mbf = (ff >= 2) & (ff <= 200)  # inst_f is one shorter (np.diff) → its own mask
    # AM depth: peak sideband amplitude relative to carrier amplitude (mean amp)
    am_peak_i = np.argmax(Xa[mb])
    am_peak_hz = fa[mb][am_peak_i]
    # parseval-ish: envelope peak / DC(mean amp)*len → modulation index proxy
    am_index = 2.0 * Xa[mb][am_peak_i] / (amp.mean() * len(amp)) if amp.mean() else 0.0
    fm_peak_i = np.argmax(Xf[mbf])
    fm_peak_hz = ff[mbf][fm_peak_i]
    fm_dev_hz = 2.0 * Xf[mbf][fm_peak_i] / len(inst_f)
    # energy at the cadence +-1 Hz (AM) for a cadence-locked number
    cad_mask = (fa >= cadence_hz - 1.5) & (fa <= cadence_hz + 1.5)
    am_at_cadence = float(Xa[cad_mask].max() / (amp.mean() * len(amp)) * 2.0) if amp.mean() else 0.0
    return {
        "ok": True,
        "carrier_hz": round(float(carrier), 1),
        "am_peak_hz": round(float(am_peak_hz), 1),
        "am_index_pct": round(float(am_index) * 100, 2),
        "am_at_cadence_pct": round(am_at_cadence * 100, 2),
        "fm_peak_hz": round(float(fm_peak_hz), 1),
        "fm_dev_hz": round(float(fm_dev_hz), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--system", default="NTSC")
    ap.add_argument("--sample-rate", type=int, default=10500)
    ap.add_argument(
        "--chunk", type=int, default=0, help="worker chunk_size override (0 = default 1024)"
    )
    ap.add_argument(
        "--jitter", type=float, default=0.0, help="write-pace jitter, fraction of chunk_period"
    )
    ap.add_argument(
        "--no-worker", action="store_true", help="static ring (no writes) = floor reference"
    )
    ap.add_argument("-t", "--seconds", type=float, default=16.0)
    ap.add_argument("--device", default=d.CAMLINK_AVF_AUDIO)
    args = ap.parse_args()

    api = Ultimate64API(args.url)
    api.reset()
    time.sleep(2.0)
    api.run_basic_clear_loop()
    time.sleep(0.5)

    s = AudioStreamer(api, args.sample_rate, args.system, dither=False, host_dma_servo=True)
    if args.chunk:
        s.chunk_size = args.chunk
    chunk = s.chunk_size
    cadence = args.sample_rate / chunk
    print(
        f"sr={args.sample_rate} chunk={chunk} -> write cadence = {cadence:.1f} Hz "
        f"({'STATIC ring, no worker' if args.no_worker else 'host-DMA worker'}, jitter={args.jitter})"
    )

    stop = threading.Event()
    wav = d.stamped("writeperturb_audio", "wav")
    cap = _capture(args.device, args.seconds + 1.0, str(wav))
    time.sleep(0.8)

    if args.no_worker:
        s._upload_nmi_and_buffers()
        K = round(CARRIER_HZ * RING_BUFFER_SIZE / args.sample_rate)
        t = np.arange(RING_BUFFER_SIZE)
        sig = (0.8 * np.sin(2 * np.pi * K * t / RING_BUFFER_SIZE)).astype(np.float32)
        api.write_memory_file(
            f"{RING_BUFFER_ADDR:04X}", encode_floats_to_dac(sig, dither=False).tobytes()
        )
        s._start_nmi_timer()
    else:
        if args.jitter > 0:
            _install_jitter(s, args.jitter)
        s.start_for_external_source()

        def _producer() -> None:
            push = 1024
            period = push / args.sample_rate
            nxt = time.monotonic()
            phase = 0
            while not stop.is_set():
                k = np.arange(push)
                sig = (
                    0.7 * np.sin(2 * np.pi * CARRIER_HZ * (phase + k) / args.sample_rate)
                ).astype(np.float32)
                phase += push
                s._encode_and_enqueue(sig, block_on_full=True)
                nxt += period
                sl = nxt - time.monotonic()
                if sl > 0:
                    time.sleep(sl)

        threading.Thread(target=_producer, daemon=True).start()

    time.sleep(args.seconds)
    stop.set()
    if not args.no_worker:
        s.stop()
    cap.wait(timeout=8)

    res = _analyze(str(wav), cadence) if Path(wav).exists() else {"ok": False}
    if not res.get("ok"):
        print("analysis failed (capture too short/silent)")
    else:
        print(f"  carrier  = {res['carrier_hz']} Hz")
        print(
            f"  AM: peak {res['am_peak_hz']} Hz  index {res['am_index_pct']}%  "
            f"@cadence({cadence:.1f}Hz) {res['am_at_cadence_pct']}%"
        )
        print(f"  FM: peak {res['fm_peak_hz']} Hz  deviation {res['fm_dev_hz']} Hz")

    if args.no_worker:
        api.silence_sid()
    api.reset()
    print("[reset] done")
    return 0


def _install_jitter(s: AudioStreamer, frac: float) -> None:
    """Monkeypatch _next_pace_increment to add uniform +-frac*chunk_period jitter
    (candidate (a): cadence randomization). Investigation-only; not shipped."""
    import random

    orig = s._next_pace_increment

    def jittered(write_addr: int, chunk_period: float) -> float:
        return orig(write_addr, chunk_period) + random.uniform(-frac, frac) * chunk_period

    s._next_pace_increment = jittered  # type: ignore[method-assign]


if __name__ == "__main__":
    main()
