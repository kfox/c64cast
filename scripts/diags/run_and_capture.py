#!/usr/bin/env python3
"""Launch c64cast with a config, capture A/V ground-truth from the Cam Link,
then tear down and reset the machine. This is the harness that kept getting
re-created as ``/tmp/run_and_capture.sh`` — committed here so it stops drifting.

    scripts/diags/run_and_capture.py --config /tmp/wave_tol.toml -t 20
    scripts/diags/run_and_capture.py --config c.toml -t 30 --frames 6
    scripts/diags/run_and_capture.py --config c.toml -t 20 --no-audio
    scripts/diags/run_and_capture.py --config c.toml -t 20 --no-reset  # keep state to inspect

Ordering matters (and is the reason a shared harness beats ad-hoc shells):
the audio capture starts BEFORE c64cast so the ~5s boot + first-PLAY window
isn't missed; frames are grabbed across the run; on exit c64cast is stopped
and — unless --no-reset — the machine is reset (the standing end-of-test rule).

Outputs (audio wav + frames + a label) land under scripts/diags/out/.
Uses the same interpreter (.venv) to spawn `-m c64cast`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import _diaglib as d


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", required=True, help="c64cast TOML config")
    ap.add_argument(
        "-t",
        "--seconds",
        type=float,
        default=20.0,
        help="how long to let the scene run (default 20)",
    )
    ap.add_argument("--label", default="run", help="output filename prefix")
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument(
        "--frames", type=int, default=3, help="HDMI frames to grab across the run (0 = none)"
    )
    ap.add_argument("--no-audio", action="store_true", help="skip audio capture")
    ap.add_argument(
        "--no-reset",
        action="store_true",
        help="leave the machine running for inspection (default: reset)",
    )
    ap.add_argument("--cv2-index", type=int, default=d.CAMLINK_CV2_INDEX)
    ap.add_argument("--avf-audio", default=d.CAMLINK_AVF_AUDIO)
    args = ap.parse_args()

    cfg = Path(args.config)
    if not cfg.exists():
        ap.error(f"config not found: {cfg}")

    boot_margin = 6.0  # c64cast boot + reach first PLAY
    audio_len = args.seconds + boot_margin + 2.0
    out = d.out_dir()

    audio_proc = None
    if not args.no_audio:
        wav = str(d.stamped(f"{args.label}_audio", "wav"))
        # Non-blocking: start the recorder, THEN launch c64cast.
        audio_proc = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "avfoundation",
                "-i",
                args.avf_audio,
                "-t",
                str(audio_len),
                "-ac",
                "1",
                "-ar",
                "48000",
                wav,
            ],
        )
        print(f"[audio] recording {audio_len:g}s -> {wav}")
        time.sleep(1.5)  # let the avfoundation stream actually come up

    print(f"[run] python -m c64cast --config {cfg}")
    app = subprocess.Popen(
        [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", args.url]
    )

    # Grab frames spread across the active window (after boot).
    import cv2

    frame_times = []
    if args.frames > 0:
        start = boot_margin
        span = max(0.0, args.seconds - 1.0)
        frame_times = [start + span * (i + 1) / (args.frames + 1) for i in range(args.frames)]

    t0 = time.time()
    grabbed = 0
    try:
        for ft in frame_times:
            wait = ft - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)
            cap = cv2.VideoCapture(args.cv2_index)
            for _ in range(4):
                cap.read()
            ok, frame = cap.read()
            cap.release()
            if ok and frame is not None:
                p = out / f"{args.label}_frame{grabbed:02d}.png"
                cv2.imwrite(str(p), frame)
                print(f"[frame] {p}")
                grabbed += 1
        # idle out the remainder
        remaining = args.seconds + boot_margin - (time.time() - t0)
        if remaining > 0:
            time.sleep(remaining)
    finally:
        print("[run] stopping c64cast")
        app.terminate()
        try:
            app.wait(timeout=8)
        except subprocess.TimeoutExpired:
            app.kill()
        if audio_proc is not None:
            try:
                audio_proc.wait(timeout=max(2.0, audio_len))
            except subprocess.TimeoutExpired:
                audio_proc.kill()
        if not args.no_reset:
            code = d.rest_reset(args.url)
            print(f"[reset] {args.url}: {'HTTP ' + str(code) if code else 'FAILED'}")

    # Analyze audio if we have any.
    if audio_proc is not None:
        from audio_capture import analyze  # reuse the volumedetect summary

        analyze(wav)
    return 0


if __name__ == "__main__":
    sys.exit(main())
