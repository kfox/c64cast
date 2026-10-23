#!/usr/bin/env python3
"""Count the short flashes a bitmap video loses, per TeensyROM slicing mode.

A slower link leaves frames queued, and a frame that is on screen only briefly
can be skipped: that is the cost of slicing that a frame-rate average hides.
This plays a clip of full-screen white flashes on black, each 100 or 133 ms
long with a beep under it, as an mhires `video` scene with audio on, so on a
TeensyROM the `$D418` DAC's NMI player runs while the bitmap is pushed. Each
`--slicing` mode gets a phase of its own, and each phase captures the Cam Link
and reports:

  * flashes seen against flashes played in the capture window (missed ones),
  * the shortest and median time a flash stayed on screen.

    scripts/diags/flash_drop_ab.py --url tr:// --cv2-index "Cam Link"
    scripts/diags/flash_drop_ab.py --url tr:// --slicing off,locked --seconds 30

Outputs land under scripts/diags/out/flashdrop/. Resets the machine on exit.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import _diaglib as d
import cv2
import numpy as np
from doublebuffer_tear_ab import _SCENE_ACTIVE, run_phase

FPS = 30
FLASH_FRAMES = (3, 4)  # 100 and 133 ms at 30 fps
PERIOD_FRAMES = 21  # one flash every 0.7 s
BEEP_HZ = 1000
CLIP_SECONDS = 21


def build_flash_video(path: Path) -> None:
    """Black, with a white flash every PERIOD_FRAMES frames lasting each of
    FLASH_FRAMES in turn, and a beep for each flash's length."""
    w, h = 640, 400
    black = np.zeros((h, w, 3), np.uint8)
    white = np.full((h, w, 3), 255, np.uint8)
    silent = path.with_name(f"{path.stem}_silent{path.suffix}")
    vw = cv2.VideoWriter(str(silent), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (w, h))
    beeps = []
    for i in range(FPS * CLIP_SECONDS):
        n, at = divmod(i, PERIOD_FRAMES)
        length = FLASH_FRAMES[n % len(FLASH_FRAMES)]
        vw.write(white if at < length else black)
        if at == 0:
            beeps.append((i / FPS, (i + length) / FPS))
    vw.release()
    gate = "+".join(f"between(t,{a:.4f},{b:.4f})" for a, b in beeps)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(silent),
         "-f", "lavfi", "-i", f"sine=frequency={BEEP_HZ}:duration={CLIP_SECONDS}",
         "-af", f"volume=volume='{gate}':eval=frame",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )  # fmt: skip


def write_config(cfg_path: Path, video_path: Path, teensyrom: str) -> None:
    cfg_path.write_text(
        f"""
[audio]
enabled = true

[playlist]
interleave_videos = false
loop = true

[[scenes]]
type = "video"
display = "mhires"
file = "{video_path}"
{teensyrom}"""
    )


def flash_runs(frames: list[np.ndarray]) -> tuple[list[int], int]:
    """The length, in captured frames, of each run of bright frames, and how
    many flashes began in the capture. A run cut by the capture's start or end
    has no length, but one cut by the end still began in it."""
    lit = [float(f[f.shape[0] // 4 : 3 * f.shape[0] // 4].mean()) > 128 for f in frames]
    runs: list[int] = []
    onsets = 0
    start = None
    for i, on in enumerate(lit):
        if on and start is None:
            start = i
            if i > 0:
                onsets += 1
        elif not on and start is not None:
            if start > 0:
                runs.append(i - start)
            start = None
    return runs, onsets


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="tr://")
    ap.add_argument("--seconds", type=float, default=20.0, help="capture window per phase")
    d.add_capture_device_arg(ap, "--cv2-index")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument(
        "--slicing",
        type=lambda s: [m.strip() for m in s.split(",") if m.strip()],
        default=["off", "locked"],
        metavar="MODE[,MODE…]",
        help="one phase per [teensyrom].dma_slicing MODE",
    )
    args = ap.parse_args()
    d.resolve_capture(args.device)  # fail before c64cast touches the machine

    out = d.out_dir() / "flashdrop"
    out.mkdir(parents=True, exist_ok=True)
    video = out / "flashes.mp4"
    print(f"[build] test video → {video}")
    build_flash_video(video)

    captured: dict[str, list[np.ndarray]] = {}
    try:
        for mode in args.slicing:
            cfg = out / f"{mode}.toml"
            write_config(cfg, video, d.tr_slicing_toml(mode))
            captured[mode] = run_phase(
                mode, cfg, args.url, args.seconds, args.device, ready=_SCENE_ACTIVE
            )
    finally:
        if not args.no_reset:
            ok = d.machine_reset(args.url)
            print(f"[reset] {args.url}: {'OK' if ok else 'FAILED'}")

    period_s = PERIOD_FRAMES / FPS
    print("\n=== flashes (each 100 or 133 ms, one per 0.7 s) ===")
    print(f"{'mode':>10} {'played':>7} {'seen':>5} {'missed':>7} {'min ms':>7} {'median ms':>10}")
    for mode, frames in captured.items():
        if not frames:
            print(f"{mode:>10}  no frames captured")
            continue
        frame_ms = args.seconds * 1000 / len(frames)
        runs, seen = flash_runs(frames)
        # Whether the window holds the lower or the upper count of onsets
        # depends on its phase, so the lower one never reports a phantom miss.
        played = int(args.seconds / period_s)
        ms = [r * frame_ms for r in runs]
        print(
            f"{mode:>10} {played:>7} {seen:>5} {max(0, played - seen):>7} "
            f"{min(ms, default=0):>7.0f} {float(np.median(ms)) if ms else 0:>10.0f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
