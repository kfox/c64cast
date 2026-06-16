#!/usr/bin/env python3
"""Calibrate the true DAC playback rate vs the read-pointer measurement, with
ZERO bus contention, to settle whether the read-pointer R is a real slowdown or
a measurement artifact (and to calibrate the Cam Link capture clock).

Method: prefill the 8 KB audio ring with an EXACT periodic waveform (K full sine
cycles over RING_BUFFER_SIZE samples), bring up the NMI DAC at the nominal latch,
and DO NOT start the worker — so the NMI reads the static ring in a loop with no
host-DMA writes and no video. Then:
  * capture Cam Link audio -> the captured tone fundamental = K * R_dac / RING,
    so R_dac = captured_hz * RING / K  (the TRUE, uncontended DAC rate).
  * simultaneously poll the read pointer ($C025/$C026) over REST -> its dr/dt is
    the read-pointer rate the resampler/adaptive trust.
Compare. If the captured tone says R_dac ~= nominal (10543 @ sr=10500) but the
read pointer reads ~9800, the read-pointer dr/dt is a MEASUREMENT artifact, not a
real slowdown -> the resampler over-decimates against a bad signal.

    uv run python scripts/diags/dac_clock_calib.py --url http://192.168.2.64 -t 14
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

from c64cast.api import Ultimate64API
from c64cast.audio import (
    NMI_ROUTINE_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_SIZE,
    AudioStreamer,
    encode_floats_to_dac,
)
from c64cast.c64 import CLOCK_NTSC, CLOCK_PAL

READ_PTR_ADDR = NMI_ROUTINE_ADDR + 5  # $C025 (LO) / $C026 (HI)
CAP_RATE = 48000


def _read_r(url: str) -> int | None:
    b = d.rest_readmem(READ_PTR_ADDR, 2, url)
    if not b or len(b) != 2:
        return None
    a = b[0] | (b[1] << 8)
    return a if RING_BUFFER_ADDR <= a < RING_BUFFER_END else None


def _poll_r(url: str, stop: threading.Event, out: list[tuple[float, int]]) -> None:
    t0 = time.monotonic()
    while not stop.is_set():
        r = _read_r(url)
        if r is not None:
            out.append((time.monotonic() - t0, r))
        time.sleep(0.02)


def _fit_r_rate(samples: list[tuple[float, int]]) -> float:
    """Unwrap the ring-wrapped read pointer into a monotonic byte count, then
    least-squares the slope (bytes/s). Unwrap step = RING_BUFFER_SIZE on a
    backward jump (a lap)."""
    if len(samples) < 4:
        return 0.0
    ts = [s[0] for s in samples]
    cum = []
    base = 0
    prev = samples[0][1] - RING_BUFFER_ADDR
    for _, raw in samples:
        v = raw - RING_BUFFER_ADDR
        if v < prev - RING_BUFFER_SIZE // 2:  # wrapped past end -> lap
            base += RING_BUFFER_SIZE
        cum.append(base + v)
        prev = v
    n = len(ts)
    mt = sum(ts) / n
    my = sum(cum) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    sxy = sum((t - mt) * (y - my) for t, y in zip(ts, cum, strict=False))
    return sxy / sxx if sxx else 0.0


def _capture(device: str, seconds: float, out_path: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "avfoundation",
         "-i", device, "-t", str(seconds), "-ac", "1", "-ar", str(CAP_RATE), out_path],
        stderr=subprocess.DEVNULL,
    )  # fmt: skip


def _tone_hz(wav_path: str) -> float:
    with wave.open(wav_path, "rb") as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)
    win = int(0.05 * sr)
    env = np.array([np.sqrt(np.mean(a[i : i + win] ** 2)) for i in range(0, len(a) - win, win)])
    loud = np.where(env > 0.4 * env.max())[0]
    if len(loud) < 4:
        return 0.0
    seg = a[(loud[0] + 2) * win : (loud[-1] - 2) * win]
    seg = seg * np.hanning(len(seg))
    F = np.abs(np.fft.rfft(seg))
    fr = np.fft.rfftfreq(len(seg), 1 / sr)
    band = (fr >= 200) & (fr <= 1500)
    return float(fr[band][np.argmax(F[band])])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--system", default="NTSC")
    ap.add_argument("--sample-rate", type=int, default=10500)
    ap.add_argument("--cycles", type=int, default=342, help="full sine cycles in the 8KB ring")
    ap.add_argument("-t", "--seconds", type=float, default=14.0)
    ap.add_argument("--device", default=d.CAMLINK_AVF_AUDIO)
    ap.add_argument("--no-capture", action="store_true")
    ap.add_argument(
        "--contention-bps",
        type=int,
        default=0,
        help="background host-DMA write rate (B/s) to scratch RAM, mimicking "
        "the audio worker / video bus load (0 = none = idle bus)",
    )
    args = ap.parse_args()

    clock = CLOCK_NTSC if args.system == "NTSC" else CLOCK_PAL
    nominal_latch = max(1, round(clock / args.sample_rate) - 1)
    nominal_rate = clock / (nominal_latch + 1)
    K = args.cycles
    expect_hz_nominal = K * nominal_rate / RING_BUFFER_SIZE
    print(
        f"system={args.system} sr={args.sample_rate} nominal_latch={nominal_latch} "
        f"nominal_rate={nominal_rate:.1f} Hz"
    )
    print(
        f"ring tone: {K} cycles / {RING_BUFFER_SIZE} samples -> "
        f"{expect_hz_nominal:.1f} Hz IF R_dac=nominal & capture accurate\n"
    )

    api = Ultimate64API(args.url)
    api.reset()
    time.sleep(2.0)
    api.run_basic_clear_loop()
    time.sleep(0.5)

    s = AudioStreamer(api, args.sample_rate, args.system, dither=False, host_dma_servo=True)
    s._upload_nmi_and_buffers()
    # Static periodic ring: K full sine cycles across the whole ring.
    t = np.arange(RING_BUFFER_SIZE)
    sig = (0.8 * np.sin(2 * np.pi * K * t / RING_BUFFER_SIZE)).astype(np.float32)
    vol = encode_floats_to_dac(sig, dither=False)
    api.write_memory_file(f"{RING_BUFFER_ADDR:04X}", vol.tobytes())
    s._start_nmi_timer()  # arm NMI at nominal latch; NO worker started

    wav = d.stamped("dac_calib_audio", "wav")
    cap = None if args.no_capture else _capture(args.device, args.seconds + 1.0, str(wav))
    time.sleep(0.8)

    stop = threading.Event()
    rs: list[tuple[float, int]] = []
    pth = threading.Thread(target=_poll_r, args=(args.url, stop, rs), daemon=True)
    pth.start()

    # Optional bus contention: hammer host-DMA writes to scratch RAM ($C800) at
    # the requested byte rate, mimicking the audio worker / video DMA that halts
    # the C64 bus. Tests whether that load causes a REAL DAC slowdown (tone drops)
    # or just a read-pointer measurement artifact (tone holds, read-ptr drops).
    cth = None
    if args.contention_bps > 0:
        blob = bytes(1024)
        period = 1024.0 / args.contention_bps

        def _contend() -> None:
            nxt = time.monotonic()
            while not stop.is_set():
                api.write_memory_file("C800", blob)
                nxt += period
                sl = nxt - time.monotonic()
                if sl > 0:
                    time.sleep(sl)

        cth = threading.Thread(target=_contend, daemon=True)
        cth.start()
        print(f"[contention] {args.contention_bps} B/s host-DMA writes to $C800")

    time.sleep(args.seconds)
    stop.set()
    pth.join(timeout=1.0)
    if cth is not None:
        cth.join(timeout=1.0)
    if cap is not None:
        cap.wait(timeout=8)

    r_rate = _fit_r_rate(rs)
    print(
        f"read-pointer rate (uncontended) = {r_rate:.1f} B/s  "
        f"({r_rate / nominal_rate * 100:.1f}% of nominal)  [{len(rs)} samples]"
    )

    if cap is not None and Path(wav).exists():
        hz = _tone_hz(str(wav))
        r_dac = hz * RING_BUFFER_SIZE / K
        print(f"captured tone = {hz:.1f} Hz  -> R_dac(true) = {r_dac:.1f} B/s")
        if hz > 0:
            cap_offset = hz / expect_hz_nominal
            print(
                f"capture-clock offset (if R_dac=nominal) = {cap_offset:.4f} "
                f"({(cap_offset - 1) * 100:+.2f}%)"
            )
            print(
                f"\nread-ptr vs true DAC: {r_rate / r_dac:.4f} "
                f"(<1 = read pointer UNDER-reports the true rate => measurement bias)"
            )

    api.silence_sid()
    api.reset()
    print("\n[reset] done")
    return 0


if __name__ == "__main__":
    main()
