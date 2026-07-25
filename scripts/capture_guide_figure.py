#!/usr/bin/env python3
"""Capture a User's Guide figure off real hardware, through the Cam Link.

[make_guide_figures.py](make_guide_figures.py) draws the placeholders; this is
the other half — it runs a config on the C64, grabs full-resolution frames off
the capture device, and crops the HDMI pillarbox away so what lands in
`docs/guide/img/` is the C64 frame and nothing else.

    # set the U64 up for capture (1080p, no scanlines), then put it back after
    capture_guide_figure.py hdmi --capture
    capture_guide_figure.py hdmi --restore

    # a scene, sampled across a run, then reviewed as one contact sheet
    capture_guide_figure.py shoot --config docs/guide/shots/fig-3-1-waveform.toml \
        --label wave --at 14 -n 15 --spacing 2
    capture_guide_figure.py sheet wave
    capture_guide_figure.py install wave_05 fig-3-1-waveform

    # quick-playback (positional media) instead of a config
    capture_guide_figure.py shoot --label video --at 10 -n 20 -- clip.mp4

    # back-to-back frames, for catching an exact scroll position
    capture_guide_figure.py shoot --config c.toml --label hello --at 37 --burst 110

Three things here were each worth a debugging session:

**The U64 ships at SD.** "HDMI Scan Resolution" defaults to SD (480p/576p), and
the Cam Link then offers only 640x480 — far too soft for print. `hdmi
--capture` switches it to FullHD and turns scanlines off (at 4.5 px per raster
line they alias badly when a figure is scaled down onto a page). Both are
volatile — the firmware only persists on an explicit save — but `--restore`
puts them back anyway rather than leaving someone's machine reconfigured.

**The frame is pillarboxed.** At 1080p the C64's 4:3 output sits at x 242..1681
of the 1920-wide HDMI frame. CROP below is measured, not guessed: a white
border over a black screen makes both edges findable.

**Hold the capture device open.** Re-opening per shot costs about a second of
handshake, which makes any timed sequence meaningless; `--burst` goes further
and buffers frames in RAM, because one big_text scroll cell lasts ~83 ms and
writing a 1440x1080 PNG takes longer than that.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "diags"))

import _diaglib as d  # noqa: E402  (path shim above must run first)

OUT_DIR = REPO_ROOT / "scripts" / "diags" / "out" / "guide"
IMG_DIR = REPO_ROOT / "docs" / "guide" / "img"

# The C64 frame inside the 1920x1080 HDMI frame, measured off a white-border /
# black-screen calibration capture: 1440x1080 of 4:3 picture, whose 1200x900
# active area is inset 120 px horizontally and 90 px vertically. That works out
# to 3.75 px per C64 pixel across and 4.5 px per raster line down.
CROP_X, CROP_W = 242, 1440
CROP_Y, CROP_H = 0, 1080
BORDER_X, BORDER_Y = 120, 90

HDMI_CATEGORY = "U64 Specific Settings"
HDMI_SETTINGS = {
    "capture": {"HDMI Scan Resolution": "FullHD (1080p)", "HDMI Scan lines": "Disabled"},
    "restore": {"HDMI Scan Resolution": "SD (480p/576p)", "HDMI Scan lines": "Enabled"},
}


def crop_c64(frame):
    """The C64 frame, border included, with the HDMI pillarbox removed."""
    return frame[CROP_Y : CROP_Y + CROP_H, CROP_X : CROP_X + CROP_W]


def read_image(path: Path):
    """cv2.imread returns None for anything it can't decode; say which file."""
    import cv2

    im = cv2.imread(str(path))
    if im is None:
        raise SystemExit(f"could not read image: {path}")
    return im


def cmd_hdmi(args) -> int:
    """Switch the U64's HDMI output between capture-grade and its normal setting."""
    which = "capture" if args.capture else "restore"
    for setting, value in HDMI_SETTINGS[which].items():
        ok = d.rest_set_config(HDMI_CATEGORY, setting, value, url=args.url)
        print(f"[hdmi] {setting} -> {value}: {'ok' if ok else 'FAILED'}")
    cfg = d.rest_get_config(HDMI_CATEGORY, url=args.url) or {}
    print(f"[hdmi] now: {cfg.get('HDMI Scan Resolution')} / scanlines {cfg.get('HDMI Scan lines')}")
    return 0


def cmd_shoot(args) -> int:
    """Run c64cast and grab frames off the capture device while it plays."""
    import cv2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    argv = [d.python_exe(), "-m", "c64cast"]
    if args.config:
        argv += ["--config", args.config]
    argv += args.media

    log = OUT_DIR / f"{args.label}.log"
    print(f"[run] {' '.join(argv[2:])}  (log -> {log})")
    written: list[Path] = []
    cap = None
    with open(log, "w") as fh:
        app = subprocess.Popen(argv, cwd=REPO_ROOT, stdout=fh, stderr=subprocess.STDOUT)
        t0 = time.monotonic()
        try:
            cap = cv2.VideoCapture(args.index)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
            for _ in range(10):  # let the stick's exposure/handshake settle
                cap.read()

            def wait_until(target: float) -> None:
                # Keep draining while waiting: a queued stale frame is worse
                # than a late one when the point is a specific moment.
                while (remaining := target - (time.monotonic() - t0)) > 0:
                    cap.read()
                    if remaining > 0.05:
                        time.sleep(min(remaining, 0.02))

            def keep(frame, i: int) -> None:
                shot = frame if args.raw else crop_c64(frame)
                p = OUT_DIR / f"{args.label}_{i:02d}.png"
                cv2.imwrite(str(p), shot)
                written.append(p)

            if args.burst:
                wait_until(args.at)
                frames = []
                for _ in range(args.burst):
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        frames.append(frame)
                print(f"[burst] {len(frames)} frames at ~60 fps; writing")
                for i, frame in enumerate(frames):
                    keep(frame, i)
            else:
                for i in range(args.shots):
                    wait_until(args.at + i * args.spacing)
                    if app.poll() is not None:
                        print(f"[run] app exited early rc={app.returncode}")
                        break
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        keep(frame, i)
                        print(f"[frame] {written[-1]}")
        finally:
            if cap is not None:
                cap.release()
            # SIGTERM, never SIGKILL: killing mid-DMA wedges the U64 hard
            # enough to need a power cycle.
            print("[run] stopping c64cast")
            app.terminate()
            try:
                app.wait(timeout=15)
            except subprocess.TimeoutExpired:
                app.wait(timeout=20)

    if not args.no_reset:
        time.sleep(1.0)
        print(f"[reset] HTTP {d.rest_reset(args.url)}")
    print(f"[out] {len(written)} frame(s) in {OUT_DIR}")
    return 0


def cmd_sheet(args) -> int:
    """Tile a label's frames into one labelled contact sheet.

    Reviewing a burst one full-size PNG at a time is slow and, for an agent
    reading them back, expensive; a sheet makes picking the keeper one look.
    """
    import cv2
    import numpy as np

    paths = sorted(OUT_DIR.glob(f"{args.label}_[0-9][0-9].png"))
    if not paths:
        print(f"no frames for {args.label!r} in {OUT_DIR}")
        return 1
    tiles = []
    for p in paths:
        im = read_image(p)
        h = round(im.shape[0] * args.width / im.shape[1])
        im = cv2.resize(im, (args.width, h), interpolation=cv2.INTER_AREA)
        im = cv2.copyMakeBorder(im, 24, 6, 6, 6, cv2.BORDER_CONSTANT, value=(40, 40, 40))
        cv2.putText(im, p.stem[-2:], (10, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        tiles.append(im)
    rows = []
    for i in range(0, len(tiles), args.cols):
        chunk = tiles[i : i + args.cols]
        chunk += [np.zeros_like(tiles[0])] * (args.cols - len(chunk))
        rows.append(np.hstack(chunk))
    out = OUT_DIR / f"sheet_{args.label}.png"
    cv2.imwrite(str(out), np.vstack(rows))
    print(out)
    return 0


def cmd_centre(args) -> int:
    """Report how far each frame's drawn content sits from screen centre.

    For scroller figures: 'looks centred' by eye is routinely a character cell
    out, and at 12 cells/s the difference between frames is ~83 ms. Measuring
    beats squinting. Offsets are in character cells, + meaning right of centre.
    """
    import numpy as np

    # Inset a few pixels: the outermost active columns pick up scaler ringing
    # from the border, which otherwise pins every measurement to the edge.
    x0, x1 = CROP_X - CROP_X + BORDER_X + 4, CROP_W - BORDER_X - 4
    y0, y1 = BORDER_Y + 6, CROP_H - BORDER_Y - 6
    span = CROP_W - 2 * BORDER_X
    cell = span / 40.0

    scored = []
    for p in sorted(OUT_DIR.glob(f"{args.label}_[0-9][0-9].png")):
        im = read_image(p)[y0:y1, x0:x1]
        bg = np.median(im.reshape(-1, 3), axis=0)
        diff = np.abs(im.astype(int) - bg).sum(axis=2)
        cols = np.where((diff > 90).sum(axis=0) > 20)[0]
        if len(cols) < 50:
            continue
        lo, hi = cols.min() + x0, cols.max() + x0
        offset = ((lo + hi) / 2 - (BORDER_X + span / 2)) / cell
        scored.append((abs(offset), p.stem, (hi - lo + 1) / cell, offset))
    if not scored:
        print(f"nothing measurable for {args.label!r}")
        return 1
    scored.sort()
    print(f"{'frame':12}{'cells':>8}{'offset':>9}")
    for _, stem, width, offset in scored[: args.top]:
        print(f"{stem:12}{width:8.2f}{offset:+9.2f}")
    return 0


def cmd_plate(args) -> int:
    """Compose several captures into one labelled grid (fig-4-1's four modes).

    A figure that compares things needs the things named inside the image: the
    guide's captions are one line and don't enumerate panels. Labels are set in
    Inconsolata, the book's mono face, because what they name — a display mode
    — is a config value, and the guide sets those in code style throughout.
    """
    from PIL import Image, ImageDraw, ImageFont

    panel_w, margin, gutter, label_h, label_gap = 700, 26, 26, 44, 8
    accent, frame_rule, bg = (0x2B, 0x73, 0xB5), (0x9F, 0xC0, 0xDE), (255, 255, 255)
    mono = Path.home() / "Library/Fonts/Inconsolata[wdth,wght].ttf"

    panels = []
    for spec in args.panels:
        stem, sep, label = spec.partition("=")
        # A trailing "=" means an unlabelled panel: some plates compare named
        # things (display modes), others are just adjacent screens.
        if sep and not label:
            label = ""
        elif not sep:
            label = stem
        im = Image.open(OUT_DIR / f"{stem}.png")
        h = round(im.height * panel_w / im.width)
        panels.append((label, im.resize((panel_w, h), Image.LANCZOS)))

    ph = panels[0][1].height
    labelled = any(label for label, _ in panels)
    cell_h = ph + (label_gap + label_h if labelled else 0)
    rows = (len(panels) + args.cols - 1) // args.cols
    w = margin * 2 + panel_w * args.cols + gutter * (args.cols - 1)
    h = margin * 2 + cell_h * rows + gutter * (rows - 1)

    sheet = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(mono), 30)
    for i, (label, panel) in enumerate(panels):
        x = margin + (i % args.cols) * (panel_w + gutter)
        y = margin + (i // args.cols) * (cell_h + gutter)
        sheet.paste(panel, (x, y))
        draw.rectangle([x - 1, y - 1, x + panel_w, y + ph], outline=frame_rule, width=1)
        if label:
            tw = draw.textlength(label, font=font)
            draw.text((x + (panel_w - tw) / 2, y + ph + label_gap), label, font=font, fill=accent)

    dst = IMG_DIR / f"{args.figure}.png"
    sheet.save(dst)
    print(f"wrote {dst.relative_to(REPO_ROOT)}  {sheet.width}x{sheet.height}")
    return 0


def cmd_install(args) -> int:
    """Copy a chosen frame over the placeholder it replaces."""
    src = OUT_DIR / f"{args.frame}.png"
    if not src.exists():
        print(f"no such frame: {src}")
        return 1
    dst = IMG_DIR / f"{args.figure}.png"
    dst.write_bytes(src.read_bytes())
    print(f"installed {dst.relative_to(REPO_ROOT)} <- {src.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL, help="U64 base URL")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("hdmi", help="set the U64's HDMI output for capture, or restore it")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", action="store_true", help="FullHD, scanlines off")
    mode.add_argument("--restore", action="store_true", help="back to SD, scanlines on")
    p.set_defaults(func=cmd_hdmi)

    p = sub.add_parser("shoot", help="run a config and grab frames")
    p.add_argument("--config", help="c64cast TOML config")
    p.add_argument("--label", required=True, help="output filename prefix")
    p.add_argument("--at", type=float, default=14.0, help="seconds after launch for the first shot")
    p.add_argument("-n", "--shots", type=int, default=8)
    p.add_argument("--spacing", type=float, default=2.0, help="seconds between shots")
    p.add_argument("--burst", type=int, default=0, help="instead: N back-to-back frames at --at")
    p.add_argument("--index", type=int, default=d.CAMLINK_CV2_INDEX, help="cv2 capture index")
    p.add_argument("--raw", action="store_true", help="keep the full pillarboxed 1920x1080 frame")
    p.add_argument("--no-reset", action="store_true", help="leave the machine running")
    p.add_argument("media", nargs="*", help="quick-playback media args, after --")
    p.set_defaults(func=cmd_shoot)

    p = sub.add_parser("sheet", help="tile a label's frames into a contact sheet")
    p.add_argument("label")
    p.add_argument("--cols", type=int, default=5)
    p.add_argument("--width", type=int, default=260, help="tile width in px")
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("centre", help="rank frames by how centred their content is")
    p.add_argument("label")
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_centre)

    p = sub.add_parser("plate", help="compose several frames into one labelled grid")
    p.add_argument("figure", help="figure name, e.g. fig-4-1-modes")
    p.add_argument("panels", nargs="+", metavar="FRAME[=LABEL]", help="e.g. p-mcm_00=mcm")
    p.add_argument("--cols", type=int, default=2)
    p.set_defaults(func=cmd_plate)

    p = sub.add_parser("install", help="copy a frame over the figure it replaces")
    p.add_argument("frame", help="frame stem, e.g. wave_05")
    p.add_argument("figure", help="figure name, e.g. fig-3-1-waveform")
    p.set_defaults(func=cmd_install)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
