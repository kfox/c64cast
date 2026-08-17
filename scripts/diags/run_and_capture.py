#!/usr/bin/env python3
"""Launch c64cast with a config, capture A/V ground-truth from the Cam Link,
then tear down and reset the machine. This is the harness that kept getting
re-created as ``/tmp/run_and_capture.sh`` — committed here so it stops drifting.

    scripts/diags/run_and_capture.py --config /tmp/wave_tol.toml -t 20
    scripts/diags/run_and_capture.py --config c.toml -t 30 --frames 6
    scripts/diags/run_and_capture.py --config c.toml -t 20 --no-audio
    scripts/diags/run_and_capture.py --config c.toml -t 20 --no-reset  # keep state to inspect
    scripts/diags/run_and_capture.py --config c.toml -t 20 --field-burst 12

``--field-burst`` grabs N *consecutive* frames mid-run via hdmi_capture.burst,
for anything that changes between video fields rather than between seconds — a
raster split, a $D018 page flip, a two-field colour alternation. ``--burst``
cannot resolve those: it deliberately down-samples to --burst-fps to cover a
multi-second window, and it inherits the device's 1080p default, whose 25 fps
aliases onto a 25 Hz alternation. The two answer different questions, so a
"does it alternate, and cleanly?" run usually wants both.

Ordering matters (and is the reason a shared harness beats ad-hoc shells):
the audio capture starts BEFORE c64cast so the ~5s boot + first-PLAY window
isn't missed; frames are grabbed across the run; on exit c64cast is stopped
and — unless --no-reset — the machine is reset (the standing end-of-test rule).

Outputs (audio wav + frames + a label) land under scripts/diags/out/.
Uses the same interpreter (.venv) to spawn `-m c64cast`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import _diaglib as d


def _flash_loop(
    url: str, hz: float, color: int, t0: float, stop: threading.Event, marks: list[float]
) -> None:
    """Pulse the VIC border ($D020) bright at `hz` while the scene plays, logging
    each pulse's wall-clock offset from t0. The captured video then carries timed
    markers to align against the source (border-flash A/V sync marker — measures
    playback tempo / A/V drift). Bus-clean REST pokes; coexists with the app."""
    period = 1.0 / hz
    nxt = time.monotonic()
    while not stop.is_set():
        now = time.monotonic()
        if now < nxt:
            stop.wait(min(nxt - now, period))
            continue
        nxt += period
        if d.flash_border(url, color):  # bright pulse
            marks.append(round(time.time() - t0, 4))
        stop.wait(0.06)  # ~60 ms visible pulse
        d.flash_border(url, 0)  # back to black (the run owns the border as a marker)


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
    ap.add_argument(
        "--burst",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="ALSO grab frames continuously (single device open, --burst-fps/sec) "
        "from app launch until SECONDS after it — catches short windows like "
        "scene-setup progress bars that the spread --frames miss (0 = off)",
    )
    ap.add_argument("--burst-fps", type=float, default=6.0, help="burst grab rate (frames/sec)")
    ap.add_argument(
        "--field-burst",
        type=int,
        default=0,
        metavar="N",
        help="grab N CONSECUTIVE frames once, mid-run, for between-field changes",
    )
    ap.add_argument(
        "--field-burst-at",
        type=float,
        default=0.5,
        metavar="FRAC",
        help="where in the active window to take the field burst (0..1, default 0.5)",
    )
    ap.add_argument(
        "--field-burst-size",
        default="1280x720",
        help="stream size to request for the field burst (default 1280x720)",
    )
    ap.add_argument("--no-audio", action="store_true", help="skip audio capture")
    ap.add_argument(
        "--no-reset",
        action="store_true",
        help="leave the machine running for inspection (default: reset)",
    )
    ap.add_argument(
        "-d",
        "--device",
        default=d.CAMLINK_DEVICE,
        help="capture device: a cv2 index, a camera name substring, or a USB "
        f"VID:PID (default {d.CAMLINK_DEVICE!r}; see `c64cast --list-devices`)",
    )
    ap.add_argument("--cv2-index", dest="device", help="alias for --device")
    ap.add_argument("--avf-audio", default=d.CAMLINK_AVF_AUDIO)
    ap.add_argument(
        "--border-flash",
        type=float,
        default=0.0,
        metavar="HZ",
        help="flash the VIC border at HZ during the run as an A/V sync marker; "
        "pulse times are written to <label>_flashes.json (0 = off)",
    )
    ap.add_argument(
        "--flash-color", type=int, default=1, help="border color for the flash pulse (0-15)"
    )
    ap.add_argument(
        "--app-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra argument forwarded verbatim to `python -m c64cast` (repeatable); "
        "e.g. --app-arg -v to surface INFO logs like the sampler write-ahead lead. "
        "The app's stdout+stderr are tee'd to <label>_app.log under out/.",
    )
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

    app_argv = [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", args.url]
    app_argv += args.app_arg
    app_log = d.stamped(f"{args.label}_app", "log")
    print(f"[run] {' '.join(app_argv[2:])}  (log -> {app_log})")
    # Lifetime spans Popen → wait → close below, so a `with` doesn't fit.
    app_log_fh = open(app_log, "w")  # noqa: SIM115
    app = subprocess.Popen(app_argv, stdout=app_log_fh, stderr=subprocess.STDOUT)

    # Grab frames spread across the active window (after boot).
    frame_times = []
    if args.frames > 0:
        start = boot_margin
        span = max(0.0, args.seconds - 1.0)
        frame_times = [start + span * (i + 1) / (args.frames + 1) for i in range(args.frames)]

    t0 = time.time()
    grabbed = 0
    flash_stop = threading.Event()
    flash_marks: list[float] = []
    flash_thread: threading.Thread | None = None
    if args.border_flash > 0:
        flash_thread = threading.Thread(
            target=_flash_loop,
            args=(args.url, args.border_flash, args.flash_color, t0, flash_stop, flash_marks),
            daemon=True,
        )
        flash_thread.start()
        print(f"[flash] border marker at {args.border_flash:g} Hz")
    try:
        if args.burst > 0:
            # Single device open for the whole window: per-frame re-opens cost
            # ~1 s each, far too coarse for a scene-setup window of a few
            # seconds. cap.read() blocks at the device rate; the deadline loop
            # down-samples that to --burst-fps.
            cap = d.open_capture(args.device)
            for _ in range(4):
                cap.read()  # discard warm-up frames
            n_burst = 0
            period = 1.0 / max(0.5, args.burst_fps)
            nxt = time.time()
            while time.time() - t0 < args.burst:
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                if time.time() < nxt:
                    continue  # keep draining the device between deadlines
                nxt += period
                stamp = time.time() - t0
                p = out / f"{args.label}_burst{n_burst:03d}_t{stamp:05.1f}s.png"
                d.save_image(frame, p)
                n_burst += 1
            cap.release()
            print(f"[burst] {n_burst} frames over {args.burst:g}s -> {out}/{args.label}_burst*.png")
        if args.field_burst > 0:
            from hdmi_capture import burst as field_burst

            at = boot_margin + max(0.0, args.seconds - 1.0) * args.field_burst_at
            wait = at - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)
            fw, fh = (int(v) for v in args.field_burst_size.lower().split("x"))
            frames, measured = field_burst(args.device, args.field_burst, size=(fw, fh), fps=60)
            for i, frame in enumerate(frames):
                p = out / f"{args.label}_field{i:02d}.png"
                d.save_image(frame, p, max_width=0)  # native: fields differ by a nibble
            print(
                f"[field] {len(frames)} consecutive frames at {measured:.1f} fps "
                f"-> {out}/{args.label}_field*.png"
            )

        for ft in frame_times:
            wait = ft - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)
            cap = d.open_capture(args.device)
            for _ in range(4):
                cap.read()
            ok, frame = cap.read()
            cap.release()
            if ok and frame is not None:
                p = out / f"{args.label}_frame{grabbed:02d}.png"
                d.save_image(frame, p)  # downscaled to ~960px (cheap to read back)
                print(f"[frame] {p}")
                grabbed += 1
        # idle out the remainder
        remaining = args.seconds + boot_margin - (time.time() - t0)
        if remaining > 0:
            time.sleep(remaining)
    finally:
        if flash_thread is not None:
            flash_stop.set()
            flash_thread.join(timeout=2.0)
            fp = out / f"{args.label}_flashes.json"
            fp.write_text(json.dumps({"t0_epoch": t0, "flash_offsets_s": flash_marks}, indent=1))
            print(f"[flash] {len(flash_marks)} pulses -> {fp}")
        print("[run] stopping c64cast")
        app.terminate()
        try:
            app.wait(timeout=8)
        except subprocess.TimeoutExpired:
            app.kill()
        app_log_fh.close()
        print(f"[run] app log: {app_log}")
        if audio_proc is not None:
            try:
                audio_proc.wait(timeout=max(2.0, audio_len))
            except subprocess.TimeoutExpired:
                audio_proc.kill()
        if not args.no_reset:
            ok = d.machine_reset(args.url)
            print(f"[reset] {args.url}: {'OK' if ok else 'FAILED — RESET THE MACHINE BY HAND'}")

    # Analyze audio if we have any.
    if audio_proc is not None:
        from audio_capture import analyze  # reuse the volumedetect summary

        analyze(wav)
    return 0


if __name__ == "__main__":
    sys.exit(main())
