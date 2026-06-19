#!/usr/bin/env python3
"""Automated, in-process test of the on-C64 menu, driven entirely over DMA.

Injects SPACE / cursor / RETURN keystrokes by writing the kernal keyboard
buffer (KEYD $0277 + NDX $00C6) over the SAME DMA socket c64cast already
holds — the CMD_KEYB path. Injection therefore touches ZERO REST: the menu
poller's normal 10 Hz reads are the only REST traffic, and the buffer path
makes those safe (a buffered keystroke persists until the poller consumes it,
unlike the matrix byte $00CB the kernal rewrites 60×/s). This is the avenue
the REST-writemem injection attempt wedged on; doing it over DMA is the fix.

Why in-process: the U64 DMA service is single-connection, so a second socket
can't inject while c64cast holds the first. We build the real stack here, run
its playlist in a thread, and inject through `stack.api`.

Verifies the full loop on hardware: SPACE opens the menu, the cursor keys
navigate and change a value live (CRSR-right/left = next/prev, CRSR-down/up =
move), SPACE on a dirty menu enters the save-confirm, RETURN saves back to the
(temp) config with a .bak. With --frames it grabs Cam Link stills at each step
for a visual check. Resets the machine at the end (standing rule).

    scripts/diags/menu_inject.py
    scripts/diags/menu_inject.py --frames
    scripts/diags/menu_inject.py --display petscii --frames
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

import _diaglib as d

# Decoded PETSCII codes as they sit in KEYD (see c64.KEYBUF). The kernal folds
# SHIFT into the up/left codes, so these are exactly what a human's keypresses
# would leave in the buffer — we just place them there ourselves.
SPACE, RETURN, DOWN, UP, RIGHT, LEFT = 0x20, 0x0D, 0x11, 0x91, 0x1D, 0x9D

TEMPLATE = """\
[ultimate64]
url = "{url}"

[video]
use_reu_staged = false

[audio]
enabled = false

[menu]
enabled = true
prompt_to_save = true

[playlist]
interleave_videos = false

[[scenes]]
type = "generative"
name = "Menu inject"
source = "{source}"
display = "{display}"
audio_source = "none"
duration_s = 600.0
"""


def _inject(api, code: int) -> None:
    """Place one decoded keystroke in the buffer over DMA: KEYD[0] = code,
    then NDX = 1 — the two writes the U64's CMD_KEYB opcode performs."""
    api.write_memory("0277", f"{code:02X}")
    api.write_memory("00C6", "01")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--source", default="plasma", help="generative source")
    ap.add_argument("--display", default="mhires", help="display mode (petscii/blank/hires/mhires)")
    ap.add_argument("--frames", action="store_true", help="grab Cam Link stills at each step")
    ap.add_argument("--cv2-index", type=int, default=d.CAMLINK_CV2_INDEX)
    ap.add_argument("--no-reset", action="store_true", help="leave the machine up for inspection")
    ap.add_argument("--boot-s", type=float, default=9.0, help="seconds to wait before injecting")
    ap.add_argument("--step-s", type=float, default=0.4, help="gap between injected keys")
    args = ap.parse_args()

    from c64cast import config as cfgmod
    from c64cast.cli import build_parser, build_stack, configure_logging, teardown_stack
    from c64cast.profiler import NullProfiler, set_profiler

    configure_logging(1)  # INFO: surface the poller's SPACE/menu log lines

    out = d.out_dir()
    tmp = out / "menu_inject.toml"
    tmp.write_text(TEMPLATE.format(url=args.url, source=args.source, display=args.display))
    bak = tmp.with_name(tmp.name + ".bak")
    if bak.exists():
        bak.unlink()
    before = tmp.read_text()

    cfg = cfgmod.load(str(tmp))
    cli_args = build_parser().parse_args(["--skip-probe"])
    profiler = NullProfiler()
    set_profiler(profiler)
    stop_event = threading.Event()

    stack = build_stack(
        cfg, "menu-inject", cli_args, stop_event=stop_event, profiler=profiler, config_path=str(tmp)
    )
    run_thread = threading.Thread(target=stack.playlist.run, name="menu-inject-run", daemon=True)
    run_thread.start()
    api = stack.api

    frames: list[str] = []

    def grab(tag: str) -> None:
        if not args.frames:
            return
        import cv2

        cap = cv2.VideoCapture(args.cv2_index)
        for _ in range(15):  # flush the Cam Link's buffered (stale) frames
            cap.read()
        ok, frame = cap.read()
        cap.release()
        if ok and frame is not None:
            p = out / f"menu_inject_{tag}.png"
            cv2.imwrite(str(p), frame)
            frames.append(str(p))
            print(f"[frame] {p}")
        else:
            print(f"[frame] capture failed for {tag}")

    try:
        print(f"[boot] waiting {args.boot_s:g}s for scene setup")
        time.sleep(args.boot_s)
        pl = stack.playlist
        cur = pl.current
        mode = getattr(cur, "display_mode", None)
        print(
            f"[state] menu_cfg={pl.menu_cfg!r} enabled="
            f"{getattr(pl.menu_cfg, 'enabled', None)} "
            f"eligible={pl.menu_eligible.is_set()} "
            f"scene={getattr(cur, 'name', None)!r} mode={getattr(mode, 'name', None)!r}"
        )
        grab("00_scene")

        print("[inject] SPACE -> open menu")
        _inject(api, SPACE)
        time.sleep(0.6)
        grab("01_open")

        print("[inject] CRSR-right x3 -> change selected value (PALETTE)")
        for _ in range(3):
            _inject(api, RIGHT)
            time.sleep(args.step_s)
        grab("02_changed")

        print("[inject] CRSR-down then CRSR-up -> move selection and back")
        _inject(api, DOWN)
        time.sleep(args.step_s)
        _inject(api, UP)
        time.sleep(args.step_s)

        print("[inject] SPACE -> begin close (dirty => save-confirm)")
        _inject(api, SPACE)
        time.sleep(0.6)
        grab("03_confirm")

        print("[inject] RETURN -> save")
        _inject(api, RETURN)
        time.sleep(0.8)
        grab("04_saved")
    finally:
        stop_event.set()
        run_thread.join(timeout=8)
        teardown_stack(stack)
        if not args.no_reset:
            code = d.rest_reset(args.url)
            print(f"[reset] {args.url}: {'HTTP ' + str(code) if code else 'FAILED'}")

    after = tmp.read_text()
    print(f"[verify] .bak written: {bak.exists()}")
    print(f"[verify] config changed by save: {before != after}")
    for line in after.splitlines():
        if "palette_mode" in line:
            print(f"[verify] saved palette_mode: {line.strip()}")
    if frames:
        print(f"[verify] frames: {frames}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
