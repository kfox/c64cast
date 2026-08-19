#!/usr/bin/env python3
"""Trace what the display does over a LONG window at FIELD resolution, and
report its temporal structure instead of a pile of frames.

    scripts/diags/field_trace.py -t 30           # 30s at the device's rate
    scripts/diags/field_trace.py -t 30 --index 1
    scripts/diags/field_trace.py -t 20 --tol 1.5 # looser state clustering

hdmi_capture --burst answers "what changed between consecutive fields" but is
bounded by how many frames you are willing to write to disk — at 1080p60 a few
seconds is the practical limit, which is blind to anything with a multi-second
period. This keeps the same field-rate sampling and drops the storage: each
frame is reduced to a small signature, only the signatures are kept, and one
representative PNG is written per distinct state.

That combination is what separates the two failure modes that look alike in a
short capture:

  * a fast alternation sampled at a rate that aliases against it, which
    produces long runs of one phase and looks like a slow change, and
  * a genuinely slow change in the displayed content, which a few seconds of
    consecutive frames cannot see at all.

The report gives run lengths (1 = strict field alternation) and the wall-clock
time of every state transition, so those two read differently: aliasing gives
runs with no change in the underlying state set, a real change adds states.
"""

from __future__ import annotations

import argparse
import sys
import time

import _diaglib as d
import numpy as np


def trace(index: int, seconds: float, *, size: tuple[int, int], fps: int, tol: float):
    """Sample the capture device for `seconds`, returning (states, assign, stamps).

    `states` holds one full frame per distinct display state; `assign[i]` is the
    state index of sample i. Signatures are a 32x20 mean-pooled thumbnail, which
    is coarse enough to ignore capture noise and fine enough that a single
    changed cell color still moves it well past `tol`.
    """
    import cv2

    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise SystemExit(f"could not open cv2 capture device {index}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
    cap.set(cv2.CAP_PROP_FPS, fps)
    for _ in range(12):
        cap.read()

    sigs: list[np.ndarray] = []
    states: list[np.ndarray] = []
    assign: list[int] = []
    stamps: list[float] = []
    t0 = time.perf_counter()
    try:
        while time.perf_counter() - t0 < seconds:
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            sig = cv2.resize(frame, (32, 20), interpolation=cv2.INTER_AREA).astype(np.float32)
            for k, known in enumerate(sigs):
                if np.abs(sig - known).mean() < tol:
                    assign.append(k)
                    break
            else:
                sigs.append(sig)
                states.append(frame.copy())
                assign.append(len(sigs) - 1)
            stamps.append(time.perf_counter() - t0)
    finally:
        cap.release()
    return states, assign, stamps


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--index", type=int, default=d.CAMLINK_CV2_INDEX)
    ap.add_argument("-t", "--seconds", type=float, default=20.0)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--tol", type=float, default=1.0, help="state-clustering tolerance")
    ap.add_argument("--label", default="trace")
    args = ap.parse_args()

    w, h = (int(v) for v in args.size.lower().split("x"))
    states, assign, stamps = trace(
        args.index, args.seconds, size=(w, h), fps=args.fps, tol=args.tol
    )
    if not assign:
        raise SystemExit("no frames captured")

    rate = (len(stamps) - 1) / (stamps[-1] - stamps[0])
    print(f"{len(assign)} samples over {stamps[-1]:.2f}s = {rate:.2f} fps")
    print(f"distinct display states: {len(states)}")

    runs, cur = [], 1
    for i in range(1, len(assign)):
        if assign[i] == assign[i - 1]:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)
    hist: dict[int, int] = {}
    for r in runs:
        hist[r] = hist.get(r, 0) + 1
    print(f"run lengths (1 = strict field alternation): {dict(sorted(hist.items()))}")

    print("\nfirst appearance of each state:")
    seen: set[int] = set()
    for i, a in enumerate(assign):
        if a not in seen:
            seen.add(a)
            print(f"  state {a} at t={stamps[i]:6.2f}s")

    # Transitions between the *set* of states in play, which is what separates a
    # steady two-phase alternation from content that actually changes.
    window = max(1, int(rate * 0.5))
    print("\nstates present per 0.5s window:")
    line = []
    for start in range(0, len(assign), window):
        chunk = sorted(set(assign[start : start + window]))
        line.append("".join(str(c) for c in chunk))
    print("  " + " ".join(line))

    out = d.out_dir()
    for k, frame in enumerate(states):
        p = out / f"{args.label}_state{k}.png"
        d.save_image(frame, p)
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
