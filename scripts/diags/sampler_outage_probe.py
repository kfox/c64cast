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
END_MARGIN_S = 5.0  # the outage ends at least this long before the clip does


def make_clip(seconds: float) -> Path:
    out = d.out_dir() / f"outage_tone_{seconds:g}s.mp4"
    if out.exists():
        return out
    # Written aside and renamed: a run cut short must not leave a partial
    # clip under the name the next run reuses.
    part = out.with_name(f"{out.stem}.part{out.suffix}")
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
            str(part),
        ],
        check=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    part.replace(out)
    return out


class Outage:
    """Fails the socket-DMA client's sends, round trips and dials while on."""

    def __init__(self) -> None:
        self.on = False
        self.marks: list[tuple[str, float]] = []
        self._restore: list[tuple[object, str, object]] = []

    def install(self) -> None:
        from c64cast.hw import socket_dma

        client = socket_dma.SocketDMAClient
        send, identify = client._send_cmd_locked, client._identify_roundtrip_locked
        dial = socket_dma.socket.create_connection
        self._restore = [
            (client, "_send_cmd_locked", send),
            (client, "_identify_roundtrip_locked", identify),
            (socket_dma.socket, "create_connection", dial),
        ]
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

    def uninstall(self) -> None:
        self.on = False
        for owner, name, real in self._restore:
            setattr(owner, name, real)
        self._restore = []

    def run(
        self, start: threading.Event, done: threading.Event, at: float, length: float, t0: float
    ) -> None:
        if not start.wait(timeout=120) or done.wait(timeout=at):
            return
        self.on = True
        self.marks.append(("outage start", time.monotonic() - t0))
        done.wait(timeout=length)
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
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path}: {8 * w.getsampwidth()}-bit samples; this reads 16-bit PCM")
        rate, channels = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768
    x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
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
    ap.add_argument("--url", default=d.U64_URL, help="connection URI (default: %(default)s)")
    d.add_audio_device_arg(ap, "-D", "--device", dest="device", backend="avf")
    ap.add_argument("--at", type=float, default=15.0, help="outage start, s after the gate-on")
    ap.add_argument("--for", dest="length", type=float, default=8.0, help="outage length (s)")
    ap.add_argument(
        "--seconds",
        type=float,
        default=40.0,
        help="test clip length (s); with --clip, that clip's length, which sizes the recording",
    )
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
    if args.at < 0 or args.length <= 0 or args.at + args.length + END_MARGIN_S > args.seconds:
        # An outage still on when the clip ends cuts c64cast's own teardown,
        # so the sampler's gate-off never lands and the ring plays on.
        ap.error(
            f"--at {args.at:g} + --for {args.length:g} must end at least {END_MARGIN_S:g} s "
            f"before the clip does (--seconds {args.seconds:g})"
        )

    device = str(d.resolve_audio_input("avf", args.device).device)
    clip = Path(args.clip) if args.clip else make_clip(args.seconds)
    wav = str(d.stamped("sampler_outage", "wav"))
    from c64cast.app.cli import main as c64cast_main
    from c64cast.audio import sampler

    init = sampler.UltimateAudioSampler.__init__
    if args.no_deadline:

        def without_deadline(self, *a, **kw):
            init(self, *a, **kw)
            self._uses_deadline = False

        sampler.UltimateAudioSampler.__init__ = without_deadline  # type: ignore[method-assign]
    outage = Outage()
    outage.install()
    gated, done = threading.Event(), threading.Event()
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
        ],
        stdin=subprocess.DEVNULL,
    )
    t0 = time.monotonic()
    sampler_log = logging.getLogger("c64cast.audio.sampler")
    # DEBUG: a restart after the first logs at debug, and the root handlers
    # keep their own level, so the terminal is unchanged.
    sampler_log.setLevel(logging.DEBUG)
    ring_up = _RingUp(gated, outage.marks, t0)
    sampler_log.addHandler(ring_up)
    timer = threading.Thread(
        target=outage.run, args=(gated, done, args.at, args.length, t0), daemon=True
    )
    timer.start()
    try:
        rc = c64cast_main(["-u", args.url, str(clip)])
    finally:
        done.set()
        outage.uninstall()
        sampler.UltimateAudioSampler.__init__ = init  # type: ignore[method-assign]
        sampler_log.removeHandler(ring_up)
        try:
            rec.wait(timeout=record_s + 30)
        except subprocess.TimeoutExpired:
            rec.kill()
            rec.wait(timeout=5)
        timer.join(timeout=1)
    print(f"c64cast exited {rc}")
    for name, at in outage.marks:
        print(f"{name:18s} {at:7.2f} s after the capture started")
    if not Path(wav).exists():
        print(f"the recording failed (ffmpeg exited {rec.returncode}); nothing to analyze")
        return 1
    if rec.returncode != 0:
        print(f"ffmpeg exited {rec.returncode}: the capture may be cut short")
    analyze(wav)
    if not any(name == "outage start" for name, _ in outage.marks):
        print(
            "no outage was simulated: the sampler never logged its ring up, so this "
            "capture says nothing about an outage"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
