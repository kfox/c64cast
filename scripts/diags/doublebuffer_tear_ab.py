#!/usr/bin/env python3
"""A/B the host-DMA double-buffer against single-buffer for scene-cut tearing
on a REU backend (U64) with a bitmap + text-overlay scene.

The tweak under test: resolve_double_buffer's "auto" now enables the host-DMA
page-flip for a bitmap scene WITH a text overlay even on a REU backend (where
resolve_use_reu_staged turns the REU bank-swap off to dodge the swap shimmer,
otherwise leaving single-buffer host-DMA that tears on cuts). This harness
demonstrates the difference visually + quantitatively.

Method: build an abrupt-cut test video (two full-screen images, swapped colours
top/bottom, alternating every few frames so a partial single-buffer update is a
detectable raster split). Play it as an mhires `video` scene + a marquee overlay
on the U64, once with double_buffer=false (single-buffer) and once with
double_buffer="auto" (→ on, because the marquee is a buffer overlay). Burst-grab
consecutive Cam Link frames through each run, then:

  * classify each frame's top-third and bottom-third against the clean A/B
    colour states (references learned from the double-buffer run, which is
    tear-free by construction) and count frames whose halves disagree or match
    neither state — a tear,
  * save a few example frames from each run for direct visual inspection.

Audio is muted to isolate the video path (double-buffer + NMI-DAC audio
coexistence is already HW-proven on the TeensyROM, which ships that exact pair).

    scripts/diags/doublebuffer_tear_ab.py            # full A/B + reset
    scripts/diags/doublebuffer_tear_ab.py --seconds 8

Outputs land under scripts/diags/out/dbtear/. Resets the U64 on exit.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

import _diaglib as d
import cv2
import numpy as np


def build_test_video(path: Path, *, fps: int = 30, seconds: int = 12, hold: int = 9) -> None:
    """Two full-screen states, A and B, alternating every `hold` frames.

    A: top RED / bottom BLUE.  B: top GREEN / bottom YELLOW.  Each half carries
    fine vertical stripes so a bitmap-vs-colour desync also shows as visible
    noise, but the half is colour-dominant so the region classifier is robust.
    A clean cut recolours the whole screen; a single-buffer partial update
    leaves top and bottom in different states (the detectable tear)."""
    w, h = 640, 400
    bgr = {  # OpenCV BGR
        "red": (40, 40, 220),
        "blue": (220, 40, 40),
        "green": (40, 200, 40),
        "yellow": (40, 210, 210),
    }

    def field(top_color: str, bottom_color: str) -> np.ndarray:
        img = np.zeros((h, w, 3), np.uint8)
        img[: h // 2] = bgr[top_color]
        img[h // 2 :] = bgr[bottom_color]
        # Fine stripes (every 8 px) darken alternate columns — structure that
        # makes a bitmap/colour desync visible without dominating the region.
        img[:, ::8] = (img[:, ::8] * 0.45).astype(np.uint8)
        return img

    a = field("red", "blue")
    b = field("green", "yellow")
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    total = fps * seconds
    for i in range(total):
        vw.write(a if (i // hold) % 2 == 0 else b)
    vw.release()


def write_config(cfg_path: Path, video_path: Path, double_buffer: bool | str) -> None:
    db = "true" if double_buffer is True else ("false" if double_buffer is False else '"auto"')
    cfg_path.write_text(
        f"""
[audio]
enabled = false

[video]
double_buffer = {db}

[playlist]
interleave_videos = false
loop = true

[[scenes]]
type = "video"
display = "mhires"
file = "{video_path}"

  [[scenes.overlays]]
  type = "marquee"
  text = "C64CAST // DOUBLE-BUFFER TEAR TEST // BOTTOM ROW HIGH-CONTRAST TEXT // "
  row = 24
  speed_cells_per_s = 4.0
  fg_color = "white"
"""
    )


def burst_capture(label: str, seconds: float, cv2_index: int) -> list[np.ndarray]:
    """Grab consecutive Cam Link frames for `seconds`, as fast as the device
    yields them. Returns BGR frames."""
    cap = cv2.VideoCapture(cv2_index)
    for _ in range(8):  # warmup / flush stale buffer
        cap.read()
    frames: list[np.ndarray] = []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        ok, frame = cap.read()
        if ok and frame is not None:
            frames.append(frame)
    cap.release()
    print(f"[{label}] captured {len(frames)} frames")
    return frames


def _region_colors(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Mean BGR of the central top-third and bottom-third (skip the marquee row
    and screen edges so overlay/border don't skew the region means)."""
    h, w, _ = frame.shape
    x0, x1 = int(w * 0.15), int(w * 0.85)
    top = frame[int(h * 0.10) : int(h * 0.33), x0:x1].reshape(-1, 3).mean(0)
    bot = frame[int(h * 0.55) : int(h * 0.80), x0:x1].reshape(-1, 3).mean(0)
    return top, bot


def analyze(single: list[np.ndarray], double: list[np.ndarray], out: Path) -> tuple[float, float]:
    """Learn the two clean states (A,B) for top and bottom regions from the
    tear-free double-buffer run, then score both runs: a frame is 'torn' when
    its top and bottom don't agree on the same state (A or B), or match neither.
    Returns (single_torn_pct, double_torn_pct)."""
    dtop = np.array([_region_colors(f)[0] for f in double])
    dbot = np.array([_region_colors(f)[1] for f in double])

    def two_states(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        # 2-means via the brightest-channel split — A and B differ strongly in
        # hue, so a 1-D projection separates them cleanly.
        proj = samples[:, 1].astype(float) - samples[:, 2].astype(float)  # G - R
        lo = samples[proj <= np.median(proj)].mean(0)
        hi = samples[proj > np.median(proj)].mean(0)
        return lo, hi

    top_s = two_states(dtop)
    bot_s = two_states(dbot)

    def classify_region(c: np.ndarray, states: tuple[np.ndarray, np.ndarray]) -> int:
        d0 = np.linalg.norm(c - states[0])
        d1 = np.linalg.norm(c - states[1])
        # margin guard: ambiguous (near-equidistant) → -1 ("neither")
        if abs(d0 - d1) < 25:
            return -1
        return 0 if d0 < d1 else 1

    def torn_pct(frames: list[np.ndarray]) -> tuple[float, list[int]]:
        torn_idx: list[int] = []
        for i, f in enumerate(frames):
            t, b = _region_colors(f)
            st, sb = classify_region(t, top_s), classify_region(b, bot_s)
            if st < 0 or sb < 0 or st != sb:
                torn_idx.append(i)
        return 100.0 * len(torn_idx) / max(1, len(frames)), torn_idx

    s_pct, s_idx = torn_pct(single)
    d_pct, d_idx = torn_pct(double)

    # Save a few example frames for visual inspection: clean A/B + first torn.
    out.mkdir(parents=True, exist_ok=True)
    if double:
        cv2.imwrite(str(out / "double_clean_00.png"), double[len(double) // 3])
        cv2.imwrite(str(out / "double_clean_01.png"), double[2 * len(double) // 3])
    for n, i in enumerate(s_idx[:3]):
        cv2.imwrite(str(out / f"single_torn_{n:02d}.png"), single[i])
    for n, i in enumerate(d_idx[:2]):
        cv2.imwrite(str(out / f"double_torn_{n:02d}.png"), double[i])
    return s_pct, d_pct


def run_phase(label: str, cfg: Path, url: str, seconds: float, cv2_index: int) -> list[np.ndarray]:
    log = d.out_dir() / "dbtear" / f"{label}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    print(f"[{label}] launching c64cast …")
    with open(log, "w") as lf:
        proc = subprocess.Popen(
            [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", url, "-v"],
            stdout=lf,
            stderr=subprocess.STDOUT,
        )
        try:
            time.sleep(7.0)  # boot + first rendered frames
            frames = burst_capture(label, seconds, cv2_index)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    armed = "double-buffer armed" in log.read_text()
    print(f"[{label}] host-DMA double-buffer armed in log: {armed}")
    return frames


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--seconds", type=float, default=8.0, help="capture window per phase")
    ap.add_argument("--cv2-index", type=int, default=d.CAMLINK_CV2_INDEX)
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()

    out = d.out_dir() / "dbtear"
    out.mkdir(parents=True, exist_ok=True)
    video = out / "ab_cuts.mp4"
    print(f"[build] test video → {video}")
    build_test_video(video)

    cfg_single = out / "single.toml"
    cfg_double = out / "double.toml"
    write_config(cfg_single, video, double_buffer=False)
    write_config(cfg_double, video, double_buffer="auto")

    try:
        single = run_phase("single", cfg_single, args.url, args.seconds, args.cv2_index)
        double = run_phase("double", cfg_double, args.url, args.seconds, args.cv2_index)
    finally:
        if not args.no_reset:
            code = d.rest_reset(args.url)
            print(f"[reset] {args.url}: {'HTTP ' + str(code) if code else 'FAILED'}")

    if not single or not double:
        print("[error] no frames captured in one phase — check Cam Link index")
        return 1
    s_pct, db_pct = analyze(single, double, out)
    print("\n=== tear rate (frames with a top/bottom state mismatch) ===")
    print(f"  single-buffer  : {s_pct:5.1f}%  ({len(single)} frames)")
    print(f"  double-buffer  : {db_pct:5.1f}%  ({len(double)} frames)")
    print(f"  sample frames  : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
