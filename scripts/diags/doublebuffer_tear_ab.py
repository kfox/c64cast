#!/usr/bin/env python3
"""A/B the host-DMA double-buffer against single-buffer for scene-cut tearing
on a REU backend (U64) with a bitmap + text-overlay scene.

Both phases pin `use_reu_staged = false`, because `"auto"` would put a bitmap
scene on the REU bank-swap on this backend. This harness demonstrates the
difference visually + quantitatively.

Method: build an abrupt-cut test video (two full-screen images, swapped colors
top/bottom, alternating every few frames so a partial single-buffer update is a
detectable raster split). Play it as an mhires `video` scene + a marquee overlay
on the U64, once with double_buffer=false (single-buffer) and once with
double_buffer=true. Burst-grab
consecutive Cam Link frames through each run, then:

  * classify each frame's top-third and bottom-third against the clean A/B
    color states (references learned from the double-buffer run, which is
    tear-free by construction) and count frames whose halves disagree or match
    neither state — a tear,
  * save a few example frames from each run for direct visual inspection.

The test video carries a steady tone and audio plays as it does for a user, so
on a TeensyROM the `$D418` DAC's NMI player runs while the bitmap is pushed —
the one condition under which the backend slices its writes. `--no-audio`
isolates the video path instead, and then no TeensyROM write is ever sliced.

`--slicing MODE[,MODE…]` scores `[teensyrom].dma_slicing` instead of the
buffer: the double-buffer reference phase is pinned to `off`, and each MODE
adds a double-buffered phase of its own, scored against the states learned
from that reference. Each phase prints the backend's resolved write mode, so a
MODE the firmware cannot honor shows up as a fallback rather than as a clean
result.

    scripts/diags/doublebuffer_tear_ab.py            # full A/B + reset
    scripts/diags/doublebuffer_tear_ab.py --seconds 8
    scripts/diags/doublebuffer_tear_ab.py --url tr:// --slicing auto

Outputs land under scripts/diags/out/dbtear/. Resets the machine on exit.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path

import _diaglib as d
import cv2
import numpy as np

from c64cast.app.connect import parse_connection_uri

TONE_HZ = 440


def build_test_video(path: Path, *, fps: int = 30, seconds: int = 12, hold: int = 9) -> None:
    """Two full-screen states, A and B, alternating every `hold` frames.

    A: top RED / bottom BLUE.  B: top GREEN / bottom YELLOW.  Each half carries
    fine vertical stripes so a bitmap-vs-color desync also shows as visible
    noise, but the half is color-dominant so the region classifier is robust.
    A clean cut recolors the whole screen; a single-buffer partial update
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
        # makes a bitmap/color desync visible without dominating the region.
        img[:, ::8] = (img[:, ::8] * 0.45).astype(np.uint8)
        return img

    a = field("red", "blue")
    b = field("green", "yellow")
    silent = path.with_name(f"{path.stem}_silent{path.suffix}")
    vw = cv2.VideoWriter(str(silent), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    total = fps * seconds
    for i in range(total):
        vw.write(a if (i // hold) % 2 == 0 else b)
    vw.release()
    # A file with no audio stream starts no audio, so no NMI consumer.
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(silent),
         "-f", "lavfi", "-i", f"sine=frequency={TONE_HZ}:duration={seconds}",
         "-c:v", "copy", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )  # fmt: skip


def write_config(
    cfg_path: Path, video_path: Path, double_buffer: bool, *, audio: bool, teensyrom: str = ""
) -> None:
    db = "true" if double_buffer else "false"
    cfg_path.write_text(
        f"""
[audio]
enabled = {"true" if audio else "false"}

[video]
use_reu_staged = false
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
{teensyrom}"""
    )


def burst_capture(label: str, seconds: float, device: str | None) -> list[np.ndarray]:
    """Grab consecutive Cam Link frames for `seconds`, as fast as the device
    yields them. Returns BGR frames."""
    cap = d.open_capture(device)
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


def analyze(
    reference: list[np.ndarray], scored: dict[str, list[np.ndarray]], out: Path
) -> dict[str, float]:
    """Learn the two clean states (A,B) for top and bottom regions from the
    tear-free reference run (double-buffered, unsliced), then score each run in
    `scored`: a frame is 'torn' when its top and bottom don't agree on the same
    state (A or B), or match neither. Returns each label's torn percentage."""
    dtop = np.array([_region_colors(f)[0] for f in reference])
    dbot = np.array([_region_colors(f)[1] for f in reference])

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

    # Save a few example frames for visual inspection: clean A/B + first torn.
    out.mkdir(parents=True, exist_ok=True)
    if reference:
        cv2.imwrite(str(out / "double_clean_00.png"), reference[len(reference) // 3])
        cv2.imwrite(str(out / "double_clean_01.png"), reference[2 * len(reference) // 3])
    pcts: dict[str, float] = {}
    for label, frames in scored.items():
        pcts[label], torn_idx = torn_pct(frames)
        for n, i in enumerate(torn_idx[:3]):
            cv2.imwrite(str(out / f"{label}_torn_{n:02d}.png"), frames[i])
    return pcts


_READY_TIMEOUT_S = 90.0
_SETTLE_S = 8.0
_ARMED = "double-buffer armed"
# Logged at every scene activation; the single-buffer phase never logs _ARMED.
_SCENE_ACTIVE = "SCENE_CONFIG_JSON"


def _logged(log_text: str, marker: str) -> bool:
    """Log lines wrap at the console width, so match across whitespace."""
    return marker in " ".join(log_text.split())


_NEW_RECORD = re.compile(r"^(\d\d:\d\d:\d\d)?\s+(DEBUG|INFO|WARNING|ERROR)\s")


def _tr_writes(log_text: str) -> str | None:
    """The backend's resolved write mode, which it logs only when it slices.
    The record wraps at the console width onto indented continuation lines."""
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        if "TR writes:" in line:
            parts = [line.split("TR writes:", 1)[1].strip()]
            for cont in lines[i + 1 :]:
                if not cont.startswith(" ") or _NEW_RECORD.match(cont):
                    break
                parts.append(cont.strip())
            return " ".join(parts)
    return None


def run_phase(
    label: str, cfg: Path, url: str, seconds: float, device: str | None, *, ready: str
) -> list[np.ndarray]:
    log = d.out_dir() / "dbtear" / f"{label}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # The previous phase's exit reset reboots a TeensyROM into its menu, and a
    # c64cast connecting into that reboot's chatter misreads its probes and
    # comes up on a different idle path than the phase before it did.
    time.sleep(_SETTLE_S)
    print(f"[{label}] launching c64cast …")
    with open(log, "w") as lf:
        proc = subprocess.Popen(
            [
                d.python_exe(),
                "-m",
                "c64cast",
                "--config",
                str(cfg),
                "--url",
                url,
                "-v",
                "--profile",
            ],
            stdout=lf,
            stderr=subprocess.STDOUT,
        )
        try:
            # Wait for the phase's own on-screen marker: a fixed sleep guessed
            # short on a TeensyROM, whose reset + bring-up runs far longer than
            # an Ultimate's, and captured the cartridge menu.
            deadline = time.monotonic() + _READY_TIMEOUT_S
            while time.monotonic() < deadline and not _logged(log.read_text(), ready):
                if proc.poll() is not None:
                    break
                time.sleep(0.5)
            if _logged(log.read_text(), ready):
                time.sleep(3.0)  # first rendered frames
                frames = burst_capture(label, seconds, device)
            else:
                # Capturing anyway would score whatever is on screen instead.
                print(
                    f"[{label}] {ready!r} never logged (c64cast exited, or "
                    f"{_READY_TIMEOUT_S:.0f}s passed) — not capturing; see {log}"
                )
                frames = []
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    text = log.read_text()
    armed = _logged(text, _ARMED)
    print(f"[{label}] host-DMA double-buffer armed in log: {armed}")
    if parse_connection_uri(url).backend == "teensyrom":
        print(f"[{label}] TR writes: {_tr_writes(text) or 'WriteC64Mem (none logged)'}")
    for line in text.splitlines():
        if "fps" in line.lower() and "scene" in line.lower():
            print(f"[{label}] log: {line.strip()[-160:]}")
    return frames


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--seconds", type=float, default=8.0, help="capture window per phase")
    d.add_capture_device_arg(ap, "--cv2-index")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--no-audio", action="store_true", help="mute, isolating the video path")
    ap.add_argument(
        "--slicing",
        type=lambda s: [m.strip() for m in s.split(",") if m.strip()],
        default=[],
        metavar="MODE[,MODE…]",
        help="add a double-buffered phase per [teensyrom].dma_slicing MODE; "
        "the reference phase is pinned to 'off'",
    )
    args = ap.parse_args()
    d.resolve_capture(args.device)  # fail before c64cast touches the machine

    out = d.out_dir() / "dbtear"
    out.mkdir(parents=True, exist_ok=True)
    video = out / "ab_cuts.mp4"
    print(f"[build] test video → {video}")
    build_test_video(video)

    audio = not args.no_audio
    reference_tr = d.tr_slicing_toml("off") if args.slicing else ""
    # (label, double_buffer, [teensyrom] table, ready marker)
    phases = [
        ("single", False, reference_tr, _SCENE_ACTIVE),
        ("double", True, reference_tr, _ARMED),
        *((f"double-{mode}", True, d.tr_slicing_toml(mode), _ARMED) for mode in args.slicing),
    ]
    captured: dict[str, list[np.ndarray]] = {}
    try:
        for label, double_buffer, teensyrom, ready in phases:
            cfg = out / f"{label}.toml"
            write_config(cfg, video, double_buffer, audio=audio, teensyrom=teensyrom)
            captured[label] = run_phase(
                label, cfg, args.url, args.seconds, args.device, ready=ready
            )
    finally:
        if not args.no_reset:
            ok = d.machine_reset(args.url)
            print(f"[reset] {args.url}: {'OK' if ok else 'FAILED'}")

    if not all(captured.get(label) for label, *_ in phases):
        print("[error] no frames captured in one phase — see its output above")
        return 1
    pcts = analyze(captured["double"], captured, out)
    print("\n=== tear rate (frames with a top/bottom state mismatch) ===")
    for label, pct in pcts.items():
        print(f"  {label:<15}: {pct:5.1f}%  ({len(captured[label])} frames)")
    print(f"  sample frames  : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
