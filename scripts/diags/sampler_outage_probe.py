#!/usr/bin/env python3
"""Cut the DMA link under a sampler video scene and record what the sampler
plays through the outage and after it.

The sampler loops a REU ring on its own, so when the host's writes stop it
plays on from whatever the ring holds. The channel's length register is kept
at the end of what the ring holds for the current lap (the dead-man deadline
in c64cast/audio/sampler.py), so it should play out the audio written before
the cut, go silent, stay silent, and play again once the link is back.

    scripts/diags/hw_lock.py uv run scripts/diags/sampler_outage_probe.py
    scripts/diags/sampler_outage_probe.py --at 15 --for 8 --seconds 40
    scripts/diags/sampler_outage_probe.py --no-deadline        # the baseline
    scripts/diags/sampler_outage_probe.py --analyze out/sampler_outage_....wav

The outage is simulated in this process, below c64cast's link code: while it
lasts every DMA command and every redial raises ``OSError``, as a pulled cable
does once TCP gives up, and the client closes its socket between whole
commands. Nothing is sent to the machine to stop it, so the machine plays on
from what it already has, which is what the probe measures. Audio comes from
the HDMI capture device's audio input, recorded across the whole run, and the
report gives the AC level in each window plus the longest silent stretch.

The capture path drops samples under DMA load (docs/architecture/audio.md,
"The measurement"), so capture time runs short of wall time: read the
silent stretch against ``--for``, less the lead (about 1 s), scaled by that.

c64cast runs a built-in test clip (a 440 Hz tone under a test pattern) in
quick playback, so the run ends on its own and c64cast's clean exit resets
the machine.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import _diaglib as d

SILENT_DB = -60.0  # AC level below which a window counts as silent


def make_clip(seconds: float) -> Path:
    out = d.out_dir() / f"outage_tone_{int(seconds)}s.mp4"
    if out.exists():
        return out
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x200:rate=25:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=44100:duration={seconds}",
            "-filter:a",
            "volume=0.25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],
        check=True,
        timeout=120,
    )
    return out


class Outage:
    """Fails the socket-DMA client's sends, round trips and dials while on."""

    def __init__(self) -> None:
        self.on = False
        self.marks: list[tuple[str, float]] = []

    def install(self) -> None:
        from c64cast.hw import socket_dma

        client = socket_dma.SocketDMAClient
        send, identify = client._send_cmd_locked, client._identify_roundtrip_locked
        dial = socket_dma.socket.create_connection
        outage = self

        def failing(real):
            def call(*a, **kw):
                if outage.on:
                    raise OSError("simulated link outage")
                return real(*a, **kw)

            return call

        client._send_cmd_locked = failing(send)  # type: ignore[method-assign]
        client._identify_roundtrip_locked = failing(identify)  # type: ignore[method-assign]
        socket_dma.socket.create_connection = failing(dial)

    def run(self, start: threading.Event, at: float, length: float, t0: float) -> None:
        if not start.wait(timeout=120):
            return
        time.sleep(at)
        self.on = True
        self.marks.append(("outage start", time.monotonic() - t0))
        time.sleep(length)
        self.on = False
        self.marks.append(("outage end", time.monotonic() - t0))


class _RingUp(logging.Handler):
    def __init__(self, event: threading.Event, marks: list[tuple[str, float]], t0: float) -> None:
        super().__init__()
        self.event, self.marks, self.t0 = event, marks, t0

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "streaming ring up" in message and not self.event.is_set():
            self.marks.append(("sampler gated on", time.monotonic() - self.t0))
            self.event.set()
        elif "reached its deadline" in message:
            self.marks.append(("channel restarted", time.monotonic() - self.t0))


def analyze(path: str, window: float = 0.25) -> None:
    import numpy as np

    with wave.open(path) as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768
    n = int(rate * window)
    levels = []
    for i in range(0, len(x) - n + 1, n):
        seg = x[i : i + n]
        ac = seg - seg.mean()
        levels.append(20 * np.log10(np.sqrt(np.mean(ac**2)) + 1e-9))
    print(f"--- AC level per {window:g} s window (dBFS), {path} ---")
    for k in range(0, len(levels), 8):
        row = "  ".join(
            f"{(k + j) * window:6.2f}s {v:6.1f}" for j, v in enumerate(levels[k : k + 8])
        )
        print(row)
    loud = [i for i, v in enumerate(levels) if v > SILENT_DB]
    if not loud:
        print("no audio in the capture at all")
        return
    best = (0, 0)
    run_start = None
    for i in range(loud[0], loud[-1] + 1):
        if levels[i] <= SILENT_DB:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            best = max(best, (i - run_start, run_start))
            run_start = None
    length, start = best
    print(
        f"audio from {loud[0] * window:.2f}s to {(loud[-1] + 1) * window:.2f}s; longest silence "
        f"inside it: {length * window:.2f}s from {start * window:.2f}s"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="u64://192.168.2.64", help="connection URI")
    d.add_audio_device_arg(ap, "-D", "--device", dest="device", backend="avf")
    ap.add_argument("--at", type=float, default=15.0, help="outage start, s after the gate-on")
    ap.add_argument("--for", dest="length", type=float, default=8.0, help="outage length (s)")
    ap.add_argument("--seconds", type=float, default=40.0, help="test clip length (s)")
    ap.add_argument("--clip", default=None, help="play this instead of the built-in tone clip")
    ap.add_argument("--analyze", metavar="WAV", default=None, help="only analyze this capture")
    ap.add_argument(
        "--no-deadline",
        action="store_true",
        help="loop the whole ring with no deadline, as before it existed: the A/B baseline",
    )
    args = ap.parse_args()
    if args.analyze:
        analyze(args.analyze)
        return 0

    device = str(d.resolve_audio_input("avf", args.device).device)
    clip = Path(args.clip) if args.clip else make_clip(args.seconds)
    wav = str(d.stamped("sampler_outage", "wav"))
    from c64cast.app.cli import main as c64cast_main

    if args.no_deadline:
        from c64cast.audio import sampler

        init = sampler.UltimateAudioSampler.__init__

        def without_deadline(self, *a, **kw):
            init(self, *a, **kw)
            self._uses_deadline = False

        sampler.UltimateAudioSampler.__init__ = without_deadline  # type: ignore[method-assign]
    outage = Outage()
    outage.install()
    gated = threading.Event()
    record_s = args.seconds + 20.0
    rec = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "avfoundation",
            "-i",
            device,
            "-t",
            str(record_s),
            "-ac",
            "1",
            "-ar",
            "48000",
            wav,
        ]
    )
    t0 = time.monotonic()
    logging.getLogger("c64cast.audio.sampler").addHandler(_RingUp(gated, outage.marks, t0))
    timer = threading.Thread(target=outage.run, args=(gated, args.at, args.length, t0), daemon=True)
    timer.start()
    try:
        rc = c64cast_main(["-u", args.url, str(clip)])
    finally:
        outage.on = False
        try:
            rec.wait(timeout=record_s + 30)
        except subprocess.TimeoutExpired:
            rec.kill()
    print(f"c64cast exited {rc}")
    for name, at in outage.marks:
        print(f"{name:18s} {at:7.2f} s after the capture started")
    analyze(wav)
    return 0


if __name__ == "__main__":
    sys.exit(main())
