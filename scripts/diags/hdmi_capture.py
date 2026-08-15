#!/usr/bin/env python3
"""Grab still frame(s) from the Cam Link (U64 HDMI output) for visual
ground-truth — the thing the REST readmem API can't give you (what the VIC
actually rendered: char-ROM mismatches, MCM bit-3 surprises, mode-switch
artifacts).

    scripts/diags/hdmi_capture.py                 # one frame -> out/ (downscaled)
    scripts/diags/hdmi_capture.py -n 5 --delay 1  # 5 frames, 1s apart
    scripts/diags/hdmi_capture.py --index 1       # different cv2 device
    scripts/diags/hdmi_capture.py -o /tmp/x.png   # explicit path
    scripts/diags/hdmi_capture.py --full          # keep native 1080p (pixel-peek)
    scripts/diags/hdmi_capture.py --width 640      # custom longest-edge
    scripts/diags/hdmi_capture.py --burst 8        # consecutive frames at capture rate

Prints the written path(s). The capture device warms up slowly, so the first
few grabbed frames are discarded before the kept one.

Frames are downscaled to ``--width`` (default 960px longest edge) before writing
so a capture read back into an agent's context costs a fraction of the tokens a
full 1080p PNG does — plenty to verify what the VIC rendered. Pass ``--full`` for
native resolution when you need to pixel-peep (e.g. fine bottom-row glyph shimmer).

``--burst`` keeps the device open and grabs frames back-to-back, for anything
that changes *between* fields rather than between seconds — a raster split, a
$D018 page flip, a two-field colour alternation. ``-n`` cannot do this: it
reopens the device per frame and eats a fresh warm-up each time, so its floor
is around a second per frame however small ``--delay`` gets.

Burst defaults to 1280x720 because the rate matters more than the pixels here
and capture sticks tend to negotiate 1080p at 25 fps — which aliases onto a
25 Hz two-field alternation and can hold one phase for a whole run, making a
working alternation look like a dead one. 720p is where the same hardware
offers 60. Pass ``--capture-size``/``--capture-fps`` to override.

Always read the measured rate the run prints, and treat it as part of the
result rather than a progress message: an oversampled capture (say 62 fps
against 60 Hz fields) duplicates a sample every ~20 frames, which shows up in
the frames as an occasional doubled phase and is easy to misread as the C64
losing sync. It is the capture beating against the display, not the machine.
"""

from __future__ import annotations

import argparse
import sys
import time

import _diaglib as d


def grab(index: int, warmup: int = 5):
    import cv2  # local import: opencv is a hard dep but keep tool import cheap

    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise SystemExit(
            f"could not open cv2 capture device {index} "
            f"(Cam Link default is {d.CAMLINK_CV2_INDEX}; "
            f"override with --index or C64_DIAG_CV2)"
        )
    try:
        for _ in range(max(0, warmup)):  # let exposure/handshake settle
            cap.read()
        ok, frame = cap.read()
        if not ok or frame is None:
            raise SystemExit(f"capture device {index} opened but returned no frame")
        return frame
    finally:
        cap.release()


def burst(index: int, count: int, *, size: tuple[int, int], fps: int, warmup: int = 12):
    """Grab `count` consecutive frames from one open device.

    Returns (frames, measured_fps). The device is asked for `size`/`fps` before
    the warm-up because a UVC stick renegotiates the stream on those calls, and
    frames pulled across that switch are torn or stale.
    """
    import cv2

    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise SystemExit(
            f"could not open cv2 capture device {index} "
            f"(Cam Link default is {d.CAMLINK_CV2_INDEX}; "
            f"override with --index or C64_DIAG_CV2)"
        )
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        cap.set(cv2.CAP_PROP_FPS, fps)
        for _ in range(max(0, warmup)):
            cap.read()
        frames, stamps = [], []
        while len(frames) < count:
            ok, frame = cap.read()
            if not ok or frame is None:
                raise SystemExit(f"capture device {index} returned no frame mid-burst")
            frames.append(frame)
            stamps.append(time.perf_counter())
        span = stamps[-1] - stamps[0]
        measured = (len(stamps) - 1) / span if span > 0 else float("nan")
        return frames, measured
    finally:
        cap.release()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--index",
        type=int,
        default=d.CAMLINK_CV2_INDEX,
        help=f"cv2 capture index (default {d.CAMLINK_CV2_INDEX})",
    )
    ap.add_argument("-n", "--count", type=int, default=1, help="frames to grab")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between frames when -n > 1")
    ap.add_argument("-o", "--out", default=None, help="explicit output path (only valid with -n 1)")
    ap.add_argument(
        "--width",
        type=int,
        default=d.DEFAULT_VERIFY_WIDTH,
        help=f"downscale longest edge to this many px (default {d.DEFAULT_VERIFY_WIDTH})",
    )
    ap.add_argument(
        "--full", action="store_true", help="keep native resolution (overrides --width)"
    )
    ap.add_argument(
        "--burst",
        type=int,
        default=0,
        metavar="N",
        help="grab N consecutive frames from one open device (for between-field changes)",
    )
    ap.add_argument(
        "--capture-size",
        default="1280x720",
        help="stream size to request in burst mode (default 1280x720)",
    )
    ap.add_argument(
        "--capture-fps", type=int, default=60, help="stream rate to request in burst mode"
    )
    args = ap.parse_args()

    if args.out and args.count != 1:
        ap.error("--out is only valid with -n 1")

    max_width = 0 if args.full else args.width

    if args.burst:
        try:
            cw, ch = (int(part) for part in args.capture_size.lower().split("x"))
        except ValueError:
            ap.error(f"--capture-size wants WxH, got {args.capture_size!r}")
        frames, measured = burst(args.index, args.burst, size=(cw, ch), fps=args.capture_fps)
        print(f"burst of {len(frames)} frames at {measured:.2f} fps measured")
        for i, frame in enumerate(frames):
            path = str(d.stamped(f"burst_{i:02d}", "png"))
            w, h = d.save_image(frame, path, max_width=max_width)
            print(f"wrote {path} ({w}x{h})")
        return 0

    for i in range(args.count):
        frame = grab(args.index)
        path = args.out if args.out else str(d.stamped(f"hdmi_{i:02d}", "png"))
        w, h = d.save_image(frame, path, max_width=max_width)
        print(f"wrote {path} ({w}x{h})")
        if i + 1 < args.count:
            time.sleep(args.delay)
    return 0


if __name__ == "__main__":
    sys.exit(main())
