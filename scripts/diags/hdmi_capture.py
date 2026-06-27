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

Prints the written path(s). The capture device warms up slowly, so the first
few grabbed frames are discarded before the kept one.

Frames are downscaled to ``--width`` (default 960px longest edge) before writing
so a capture read back into an agent's context costs a fraction of the tokens a
full 1080p PNG does — plenty to verify what the VIC rendered. Pass ``--full`` for
native resolution when you need to pixel-peep (e.g. fine bottom-row glyph shimmer).
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
    args = ap.parse_args()

    if args.out and args.count != 1:
        ap.error("--out is only valid with -n 1")

    max_width = 0 if args.full else args.width
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
