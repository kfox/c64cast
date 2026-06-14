#!/usr/bin/env python3
"""Live gesture-metric readout for tuning [vision] thresholds.

Runs the SAME camera broker + MediaPipe recognizer + classifiers the vision
controller uses (c64cast.vision), but instead of firing pause/skip/cycle it
prints the raw measurements each gesture is decided from:

  * pinch distance  — thumb-tip <-> index-tip, normalized (drives pinch_threshold)
  * extended fingers — 0..4 (drives the open-hand = cycle decision)
  * wrist x-velocity — frame-widths/sec (drives swipe_velocity = skip)

Use it to set each [vision] threshold from real numbers: capture a window while
performing one gesture, read the printed min/median/max, pick a threshold that
cleanly separates that gesture from rest. No U64 needed — camera only.

    scripts/diags/vision_tune.py                 # run until Ctrl-C, device 1
    scripts/diags/vision_tune.py -t 6            # one 6-second capture window
    scripts/diags/vision_tune.py --device 0      # pick a different camera index
    scripts/diags/vision_tune.py --pinch 0.06 --swipe 1.0   # preview thresholds

Camera index trap (see local_capture_hardware memory): on this Mac cv2 idx 1 is
the FaceTime camera, idx 0 is the Cam Link. Default here is 1; override with
--device. Grab a frame and look if unsure.
"""

from __future__ import annotations

import argparse
import time

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work)
import numpy as np

from c64cast.config import VisionCfg
from c64cast.video import WebcamSource
from c64cast.vision import (
    INDEX_TIP,
    THUMB_TIP,
    WRIST,
    Gesture,
    MediaPipeHandRecognizer,
    classify_static,
    count_extended_fingers,
)


def _pinch_distance(hand) -> float:
    a, b = hand.point(THUMB_TIP), hand.point(INDEX_TIP)
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


def main() -> None:
    defaults = VisionCfg()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--device", type=int, default=1, help="cv2 camera index (default 1 = FaceTime on this Mac)"
    )
    ap.add_argument(
        "--model",
        default=defaults.model_path,
        help=f"HandLandmarker .task path (default {defaults.model_path})",
    )
    ap.add_argument(
        "-t",
        "--seconds",
        type=float,
        default=None,
        help="capture window length; omit to run until Ctrl-C",
    )
    ap.add_argument(
        "--pinch",
        type=float,
        default=defaults.pinch_threshold,
        help=f"pinch_threshold to preview (default {defaults.pinch_threshold})",
    )
    ap.add_argument(
        "--swipe",
        type=float,
        default=defaults.swipe_velocity,
        help=f"swipe_velocity to preview (default {defaults.swipe_velocity})",
    )
    ap.add_argument(
        "--rate",
        type=float,
        default=defaults.poll_interval_s,
        help=f"seconds between ticks (default {defaults.poll_interval_s})",
    )
    ap.add_argument(
        "--mirror",
        action="store_true",
        default=defaults.mirror,
        help="mirror the frame (match the webcam view)",
    )
    args = ap.parse_args()

    print(
        f"opening camera index {args.device} + loading model "
        f"(thresholds preview: pinch<{args.pinch} swipe>={args.swipe}) ..."
    )
    src = WebcamSource(args.device)
    rec = MediaPipeHandRecognizer(args.model)
    print("ready. perform a gesture. Ctrl-C to stop.\n")

    import cv2

    pinch_ds: list[float] = []
    vels: list[float] = []
    finger_hist = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    frames = hands = 0
    prev: tuple[float, float] | None = None
    fired = {"pinch": 0, "swipe": 0, "open": 0}
    t0 = time.monotonic()
    last_print = 0.0

    try:
        while True:
            now = time.monotonic()
            if args.seconds is not None and now - t0 >= args.seconds:
                break
            frame = src.read()
            if frame is None:
                time.sleep(args.rate)
                continue
            if args.mirror:
                frame = cv2.flip(frame, 1)
            frames += 1
            hand = rec.process(frame, int((now - t0) * 1000))
            if hand is None:
                prev = None
                if now - last_print >= 0.4:
                    print(f"t={now - t0:5.1f}  (no hand)")
                    last_print = now
                time.sleep(args.rate)
                continue
            hands += 1
            pd = _pinch_distance(hand)
            nf = count_extended_fingers(hand)
            wx = float(hand.point(WRIST)[0])
            wy = float(hand.point(WRIST)[1])
            dt = max(args.rate, 1e-3)
            vx = 0.0 if prev is None else abs(wx - prev[0]) / dt
            vy = 0.0 if prev is None else abs(wy - prev[1]) / dt
            horizontal = prev is not None and abs(wx - prev[0]) > abs(wy - prev[1])
            prev = (wx, wy)
            static = classify_static(hand, pinch_threshold=args.pinch)
            # Mirror the controller's swipe rule: fast AND horizontally dominant.
            is_swipe = vx >= args.swipe and horizontal

            pinch_ds.append(pd)
            vels.append(vx)
            finger_hist[nf] += 1
            if static == Gesture.PINCH:
                fired["pinch"] += 1
            if is_swipe:
                fired["swipe"] += 1
            if static == Gesture.OPEN_HAND:
                fired["open"] += 1

            if now - last_print >= 0.4:
                tag = static.value.upper()
                hot = "  <SWIPE" if is_swipe else ""
                print(
                    f"t={now - t0:5.1f}  pinch_d={pd:.3f}  fingers={nf}  "
                    f"vx={vx:5.2f}  vy={vy:5.2f}  -> {tag}{hot}"
                )
                last_print = now
            time.sleep(args.rate)
    except KeyboardInterrupt:
        print("\n(stopped)")
    finally:
        src.release()
        rec.close()

    def pct(a: list[float], p: float) -> float:
        return float(np.percentile(a, p)) if a else float("nan")

    dur = time.monotonic() - t0
    print("\n==== summary ====")
    print(
        f"window {dur:.1f}s  frames={frames}  hand-present={hands} "
        f"({100 * hands / max(frames, 1):.0f}%)"
    )
    if pinch_ds:
        print(
            f"pinch_distance : min={min(pinch_ds):.3f}  p10={pct(pinch_ds, 10):.3f}  "
            f"median={pct(pinch_ds, 50):.3f}  p90={pct(pinch_ds, 90):.3f}  "
            f"max={max(pinch_ds):.3f}"
        )
        print(
            f"wrist_velocity : median={pct(vels, 50):.2f}  p90={pct(vels, 90):.2f}  "
            f"p99={pct(vels, 99):.2f}  max={max(vels):.2f}"
        )
        print(f"finger counts  : {finger_hist}")
        print(
            f"would-fire (at pinch<{args.pinch}, swipe>={args.swipe}): "
            f"pinch={fired['pinch']} swipe={fired['swipe']} open_hand={fired['open']} "
            f"(of {hands} hand frames)"
        )
    print("=================")


if __name__ == "__main__":
    main()
