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
  * wide-bitmap-push frames — a frame whose dirty span covers most of the
    region pushes several KB in one write; the tear during that push reads as
    a flash on high-motion / scene-cut frames. (These used to be re-uploads of
    the whole 8 KB region; write_region now sends only the dirty span, so the
    tear is proportional to the span rather than fixed at full-screen.)

Reported flips are printed with their source-video timestamp so they map onto
ranges you eyeball on the U64 (`--frame-numbers` on the video scene gives
the matching on-screen counter)."""

from __future__ import annotations

import argparse
import sys

import _diaglib as d  # noqa: F401 — inserts repo root on sys.path
import numpy as np

from c64cast.app.scene_factory import _build_display_mode
from c64cast.hw.backend import ULTIMATE_PROFILE, BufferedWriteBackend  # noqa: E402

# The link cost model comes from the profile rather than being restated here:
# write_region makes its chunking decision from those same numbers, so a second
# copy would let this probe report a frame cost the renderer doesn't believe.
# Re-measure with scripts/diags/link_cost_model.py and update the profile.
COST_FLOOR_S = ULTIMATE_PROFILE.write_cost_floor_s
write_cost_s = ULTIMATE_PROFILE.write_cost_s


class RecordingBackend(BufferedWriteBackend):
    """Real delta-cache write path with a no-op transport. _emit tallies bytes
    *and writes* per VIC region, so we can see exactly what each frame would
    push and what it would cost.

    Counting writes as well as bytes is the whole point: the link's per-write
    floor means a frame split into k writes costs k times the floor no matter
    how few bytes each carries, so a bytes-only tally scores a sparse frame as
    cheap when it is the most expensive kind there is.
    """

    # The cost model write_region consults now lives on the profile, so this
    # has to be a real one: the probe reports what the Ultimate would spend.
    profile = ULTIMATE_PROFILE

    def __init__(self) -> None:
        super().__init__()
        self.reset_frame()
        self.d021: int | None = None

    def reset_frame(self) -> None:
        self.region_bytes = {"bitmap": 0, "screen": 0, "color": 0, "regs": 0}
        self.region_writes = {"bitmap": 0, "screen": 0, "color": 0, "regs": 0}
        self.frame_cost_s = 0.0

    def _emit(self, addr: int, payload: bytes) -> None:
        n = len(payload)
        if addr == 0xD021 or (addr <= 0xD021 < addr + n):
            # $D020/$D021 register coalesced write carries the bg0 byte.
            self.d021 = payload[0xD021 - addr] if addr <= 0xD021 else payload[0]
            region = "regs"
        elif 0x0400 <= addr < 0x0800:
            region = "screen"
        elif 0xD800 <= addr < 0xDC00:
            region = "color"
        elif 0x2000 <= addr < 0x4000:
            region = "bitmap"
        else:
            region = "regs"
        self.region_bytes[region] += n
        self.region_writes[region] += 1
        self.frame_cost_s += write_cost_s(n)

    def flush(self) -> None: ...
    def close(self) -> None: ...
    def format_write_latency(self) -> str | None:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("video", help="path to a video file")
    ap.add_argument("--mode", default="mhires", help="display mode name (default mhires)")
    ap.add_argument(
        "--palette", default="percell", help="palette_mode for mcm/mhires (default percell)"
    )
    ap.add_argument(
        "--max-frames", type=int, default=0, help="stop after N frames (0 = whole file)"
    )
    ap.add_argument("--top", type=int, default=50, help="how many flip/full-upload events to print")
    ap.add_argument("--csv", default=None, help="write per-frame churn to this path")
    args = ap.parse_args()

    import av

    mode = _build_display_mode(args.mode, palette_mode=args.palette)
    api = RecordingBackend()
    mode.setup(api)
    # A push covering 60%+ of the 8000-byte bitmap region in one write — the
    # tear-prone case. Not a threshold write_region uses; just where a push
    # gets big enough to read as a flash.
    full_bytes = int(8000 * 0.6)

    container = av.open(args.video)
    v = container.streams.video[0]
    v.thread_type = "AUTO"
    fps = float(v.average_rate) if v.average_rate else 30.0

    bg: list[int | None] = []
    bmp: list[int] = []
    writes: list[int] = []
    costs: list[float] = []
    region_rows: list[dict[str, int]] = []
    rows: list[tuple[int, int, int, int, int, int, float]] = []
    for n, frame in enumerate(container.decode(v)):
        if args.max_frames and n >= args.max_frames:
            break
        api.reset_frame()
        mode.render(api, frame.to_ndarray(format="bgr24"))
        rb = api.region_bytes
        rw = api.region_writes
        bg.append(api.d021)
        bmp.append(rb["bitmap"])
        nw = sum(rw.values())
        writes.append(nw)
        costs.append(api.frame_cost_s)
        region_rows.append(dict(rw))
        rows.append(
            (
                rb["bitmap"],
                rb["screen"],
                rb["color"],
                rb["regs"],
                api.d021 if api.d021 is not None else -1,
                nw,
                api.frame_cost_s * 1000.0,
            )
        )
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
    trans = [
        i
        for i in range(1, total - 1)
        if arr[i] != arr[i - 1] and arr[i] != arr[i + 1] and arr[i - 1] == arr[i + 1]
    ]
    full_uploads = [i for i in range(total) if bmp_arr[i] >= full_bytes]
    print(
        f"\nbg0/$D021: changed on {changes}/{total} frames; "
        f"transient 1-frame flips (flash-and-revert): {len(trans)}"
    )
    print(
        f"wide bitmap pushes (>= {full_bytes}B / 8000B in one frame): "
        f"{len(full_uploads)}/{total} frames "
        f"(mean bitmap push {bmp_arr.mean():.0f}B/frame)"
    )

    w_arr = np.array(writes)
    c_arr = np.array(costs)
    print(
        f"\nwrites/frame: mean {w_arr.mean():.1f}  median {np.median(w_arr):.0f}  "
        f"p95 {np.percentile(w_arr, 95):.0f}  max {w_arr.max()}"
    )
    print(
        f"modelled frame cost: mean {c_arr.mean() * 1000:.1f} ms  "
        f"median {np.median(c_arr) * 1000:.1f} ms  "
        f"p95 {np.percentile(c_arr, 95) * 1000:.1f} ms  "
        f"max {c_arr.max() * 1000:.1f} ms"
    )
    # The link ceiling this implies, against what the mode is asking for.
    mean_fps_cap = 1.0 / c_arr.mean() if c_arr.mean() > 0 else float("inf")
    print(
        f"  → link-bound ceiling at the mean frame: {mean_fps_cap:.1f} fps "
        f"(source is {fps:.1f} fps)"
    )
    floor_share = w_arr.mean() * COST_FLOOR_S / c_arr.mean() if c_arr.mean() > 0 else 0.0
    print(
        f"  → {floor_share * 100:.0f}% of that time is per-write floor, "
        f"not payload — the part extra writes multiply"
    )

    # A region split across several writes is write_region's chunked branch
    # firing. Priced against pushing that region whole, which is the only
    # comparison that says whether the split paid for itself.
    print("\nper-region writes/frame (a region split into >1 write took the chunked branch):")
    sizes = {"bitmap": 8000, "screen": 1000, "color": 1000}
    for region in ("bitmap", "screen", "color"):
        per = np.array([r[region] for r in region_rows])
        split = int(np.count_nonzero(per > 1))
        if per.max() == 0:
            continue
        whole = write_cost_s(sizes[region])
        # Frames where the split cost more than one push of the whole region.
        wasted = np.maximum(per * COST_FLOOR_S - whole, 0.0)
        n_worse = int(np.count_nonzero(per * COST_FLOOR_S > whole))
        print(
            f"  {region:>7}: max {per.max():>2}  split on {split:>4}/{total} frames  "
            f"worse-than-full-push on {n_worse:>4}  "
            f"(wasted {wasted.sum() * 1000:.0f} ms over the clip)"
        )

    if trans:
        print("\ntransient bg0 flips (frame  time  from->flash->back):")
        for i in trans[: args.top]:
            print(f"  f{i:5d} {ts(i):>6}  {arr[i - 1]:2d}->{arr[i]:2d}->{arr[i + 1]:2d}")

    vals, counts = np.unique(arr, return_counts=True)
    print(
        "\nbg0 value distribution (palette idx: frames): "
        + ", ".join(f"{int(v)}:{int(c)}" for v, c in zip(vals, counts, strict=True))
    )

    if args.csv:
        import csv

        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                [
                    "frame",
                    "time_s",
                    "bitmap_B",
                    "screen_B",
                    "color_B",
                    "reg_B",
                    "bg0",
                    "writes",
                    "cost_ms",
                ]
            )
            for i, r in enumerate(rows):
                w.writerow([i, f"{i / fps:.3f}", *r])
        print(f"\nwrote per-frame churn → {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
