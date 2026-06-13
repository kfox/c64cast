#!/usr/bin/env python3
"""Render a video file through a c64cast display mode OFFLINE (no hardware)
and report per-frame rendering churn — the things that show up on the U64 as
flicker or whole-screen flashes but can't be seen from a RAM dump.

It drives the *real* DisplayMode pipeline (quantization, palette-mode slot
picks, delta cache) against a recording backend that counts bytes-on-the-wire
per VIC region instead of sending them, so the numbers match what the live
SocketDMA path would push.

    scripts/diags/video_render_probe.py assets/videos/TRON.webm
    scripts/diags/video_render_probe.py path.mp4 --mode mhires --palette percell
    scripts/diags/video_render_probe.py path.mp4 --max-frames 600   # sample head
    scripts/diags/video_render_probe.py path.mp4 --csv out/churn.csv # per-frame dump

Two flash mechanisms it surfaces (both produce a brief whole-screen change on
real HW because $D021 lands in one tiny DMA write while the 8 KB bitmap is
still mid-upload behind it):

  * bg0 / $D021 transient flips — bg0 changes for one frame then reverts. The
    global background color flashes across every %00 pixel on screen.
  * full-bitmap-upload frames — a frame whose bitmap delta exceeds the cache's
    full_threshold re-uploads all ~8 KB; the tear during that push reads as a
    flash on high-motion / scene-cut frames.

Reported flips are printed with their source-video timestamp so they map onto
ranges you eyeball on the U64 (`--frame-numbers` on the commercial scene gives
the matching on-screen counter)."""

from __future__ import annotations

import argparse
import sys

import _diaglib as d  # noqa: F401 — inserts repo root on sys.path
import numpy as np

from c64cast.backend import BufferedWriteBackend  # noqa: E402
from c64cast.config import _build_display_mode  # type: ignore[attr-defined]  # noqa: E402


class RecordingBackend(BufferedWriteBackend):
    """Real delta-cache write path with a no-op transport. _emit just tallies
    bytes per VIC region so we can see exactly what each frame would push."""

    profile = None  # display modes used here never touch it

    def __init__(self) -> None:
        super().__init__()
        self.reset_frame()
        self.d021: int | None = None

    def reset_frame(self) -> None:
        self.region_bytes = {"bitmap": 0, "screen": 0, "color": 0, "regs": 0}

    def _emit(self, addr: int, payload: bytes) -> None:
        n = len(payload)
        if addr == 0xD021 or (addr <= 0xD021 < addr + n):
            # $D020/$D021 register coalesced write carries the bg0 byte.
            self.d021 = payload[0xD021 - addr] if addr <= 0xD021 else payload[0]
            self.region_bytes["regs"] += n
        elif 0x0400 <= addr < 0x0800:
            self.region_bytes["screen"] += n
        elif 0xD800 <= addr < 0xDC00:
            self.region_bytes["color"] += n
        elif 0x2000 <= addr < 0x4000:
            self.region_bytes["bitmap"] += n
        else:
            self.region_bytes["regs"] += n

    def flush(self) -> None: ...
    def close(self) -> None: ...
    def format_write_latency(self) -> str | None:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="path to a video file")
    ap.add_argument("--mode", default="mhires",
                    help="display mode name (default mhires)")
    ap.add_argument("--palette", default="percell",
                    help="palette_mode for mcm/mhires (default percell)")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="stop after N frames (0 = whole file)")
    ap.add_argument("--top", type=int, default=50,
                    help="how many flip/full-upload events to print")
    ap.add_argument("--csv", default=None,
                    help="write per-frame churn to this path")
    args = ap.parse_args()

    import av

    mode = _build_display_mode(args.mode, palette_mode=args.palette)
    api = RecordingBackend()
    mode.setup(api)
    # Full-upload threshold for the 8000-byte bitmap region (write_region's
    # default full_threshold=0.6 → ~4800 bytes is the "wide dirty" trigger).
    full_bytes = int(8000 * 0.6)

    container = av.open(args.video)
    v = container.streams.video[0]
    v.thread_type = "AUTO"
    fps = float(v.average_rate) if v.average_rate else 30.0

    bg: list[int | None] = []
    bmp: list[int] = []
    rows: list[tuple[int, int, int, int, int]] = []
    for n, frame in enumerate(container.decode(v)):
        if args.max_frames and n >= args.max_frames:
            break
        api.reset_frame()
        mode.render(api, frame.to_ndarray(format="bgr24"))
        rb = api.region_bytes
        bg.append(api.d021)
        bmp.append(rb["bitmap"])
        rows.append((rb["bitmap"], rb["screen"], rb["color"], rb["regs"],
                     api.d021 if api.d021 is not None else -1))
    container.close()
    total = len(bg)
    if total == 0:
        print("no frames decoded")
        return 1

    arr = np.array([b if b is not None else -1 for b in bg])
    bmp_arr = np.array(bmp)

    def ts(f: int) -> str:
        s = int(f / fps)
        return f"{s // 60}:{s % 60:02d}"

    print(f"video={args.video}")
    print(f"mode={args.mode} palette={args.palette} frames={total} fps={fps:.2f}")

    changes = int(np.count_nonzero(np.diff(arr) != 0))
    trans = [i for i in range(1, total - 1)
             if arr[i] != arr[i - 1] and arr[i] != arr[i + 1]
             and arr[i - 1] == arr[i + 1]]
    full_uploads = [i for i in range(total) if bmp_arr[i] >= full_bytes]
    print(f"\nbg0/$D021: changed on {changes}/{total} frames; "
          f"transient 1-frame flips (flash-and-revert): {len(trans)}")
    print(f"bitmap full-uploads (>= {full_bytes}B / 8000B): "
          f"{len(full_uploads)}/{total} frames "
          f"(mean bitmap push {bmp_arr.mean():.0f}B/frame)")

    if trans:
        print("\ntransient bg0 flips (frame  time  from->flash->back):")
        for i in trans[:args.top]:
            print(f"  f{i:5d} {ts(i):>6}  {arr[i-1]:2d}->{arr[i]:2d}->{arr[i+1]:2d}")

    vals, counts = np.unique(arr, return_counts=True)
    print("\nbg0 value distribution (palette idx: frames): "
          + ", ".join(f"{int(v)}:{int(c)}"
                      for v, c in zip(vals, counts, strict=True)))

    if args.csv:
        import csv
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["frame", "time_s", "bitmap_B", "screen_B",
                        "color_B", "reg_B", "bg0"])
            for i, r in enumerate(rows):
                w.writerow([i, f"{i / fps:.3f}", *r])
        print(f"\nwrote per-frame churn → {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
