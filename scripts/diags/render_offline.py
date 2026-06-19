#!/usr/bin/env python3
"""Render a generative source (optionally with a pixel effect) through a real
display mode to a PNG — entirely offline, no hardware.

Feeds the production render path (`scenes._render_with_overlays`) into a
`Framebuffer` software-VIC mirror via the backend's write-listener hook, then
saves `framebuffer.render()` as a PNG. This is the fast iteration loop for
generative scenes + effects: eyeball the C64-quantized result before touching
the U64. Reusable for any FrameSource × display × effect combination.

Examples:
  python -m scripts.diags.render_offline --source plasma --display mhires
  python -m scripts.diags.render_offline --source plasma --display petscii
  python -m scripts.diags.render_offline --source tunnel --display mhires \\
      --effect trails --frames 30 --save-frame 29 --t-step 0.1

  # Music-reactive: feed a synthetic MusicModulation (a transient flash at
  # onset=1, tempo-driven hue at beat_phase=3) to eyeball the reactive plasma.
  python -m scripts.diags.render_offline --source plasma --display mcm \\
      --onset 1.0 --beat-phase 3.0 --level 0.6
"""

from __future__ import annotations

import argparse
import os
from types import SimpleNamespace

import cv2

from c64cast.backend import BufferedWriteBackend
from c64cast.config import _build_display_mode
from c64cast.effects import build_effect
from c64cast.framebuffer import Framebuffer
from c64cast.generators import build_generator
from c64cast.scenes import _render_with_overlays

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")


class RenderBackend(BufferedWriteBackend):
    """Offline backend: writes go nowhere on the wire (`_emit` no-op); the
    framebuffer shadows them through the normal write-listener hook."""

    _EMIT_WRITE_LABEL = "render"
    _EMIT_DEVICE_LABEL = "framebuffer"

    def __init__(self) -> None:
        super().__init__()
        self.profile = None  # type: ignore[assignment]  # unused offline

    def _emit(self, addr: int, payload: bytes) -> None:
        self._note_emit_success()

    def reu_write(self, reu_offset: int, data: bytes) -> None:  # pragma: no cover
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def format_write_latency(self) -> str | None:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="plasma", help="generative source name")
    ap.add_argument("--display", default="mhires", help="display mode")
    ap.add_argument("--effect", default=None, help="pixel effect name (e.g. trails)")
    ap.add_argument("--frames", type=int, default=1, help="frames to advance before saving")
    ap.add_argument("--t-step", type=float, default=0.1, help="scene-seconds per frame")
    ap.add_argument(
        "--save-frame", type=int, default=None, help="which frame index to save (default: last)"
    )
    ap.add_argument("--out", default=None, help="output PNG path")
    # Synthetic music modulation (reactive path). If any is set, a
    # MusicModulation is passed to the source's read() so the reactive
    # generator path is exercised offline.
    ap.add_argument("--onset", type=float, default=None, help="MusicModulation.onset [0,1]")
    ap.add_argument("--beat-phase", type=float, default=None, help="MusicModulation.beat_phase")
    ap.add_argument("--level", type=float, default=None, help="MusicModulation.level [0,1]")
    # On-C64 menu overlay: paint the panel on top to eyeball its legibility on
    # this display mode. --menu-sel highlights an item; --menu-confirm shows the
    # save-confirmation screen.
    ap.add_argument("--menu", action="store_true", help="overlay the on-C64 menu panel")
    ap.add_argument("--menu-sel", type=int, default=0, help="selected menu item index")
    ap.add_argument("--menu-confirm", action="store_true", help="show the save-confirm screen")
    # Text overlay: attach a registered PETSCII text overlay (clock / callsign /
    # marquee / scrolling_text / …) to eyeball its legibility on a bitmap mode.
    ap.add_argument("--overlay", default=None, help="text overlay type (e.g. clock, callsign)")
    ap.add_argument("--overlay-text", default="C64CAST", help="text for callsign/marquee overlays")
    ap.add_argument("--overlay-corner", default="top-right", help="corner for corner-text overlays")
    args = ap.parse_args()

    modulation = None
    if args.onset is not None or args.beat_phase is not None or args.level is not None:
        from c64cast.modulation import MusicModulation

        modulation = MusicModulation(
            level=args.level or 0.0,
            onset=args.onset or 0.0,
            beat_phase=args.beat_phase or 0.0,
            bpm=120.0,
            voice_freqs=(0.0, 0.0, 0.0),
            voice_gates=(False, False, False),
        )

    os.makedirs(OUT_DIR, exist_ok=True)
    api = RenderBackend()
    fb = Framebuffer()
    api.add_write_listener(fb.on_write)

    mode = _build_display_mode(args.display)
    mode.setup(api)
    src = build_generator(args.source)
    effect = build_effect(args.effect) if args.effect else None

    overlays = []
    if args.overlay:
        from c64cast.overlays import build_overlay

        cfg = {"type": args.overlay}
        if args.overlay in ("callsign", "marquee", "scrolling_text"):
            if args.overlay == "scrolling_text":
                cfg["messages"] = [{"text": args.overlay_text}]
            else:
                cfg["text"] = args.overlay_text
        if args.overlay in ("clock", "callsign", "weather", "countdown", "network"):
            cfg["corner"] = args.overlay_corner
        ov = build_overlay(cfg, audio=None)
        ov.setup(api, SimpleNamespace())  # type: ignore[arg-type]
        overlays.append(ov)

    scene = SimpleNamespace(name=f"{args.source}/{args.display}", effect=effect, overlays=overlays)

    menu = None
    if args.menu:
        from c64cast.config import SceneCfg
        from c64cast.overlays.menu import MenuOverlay

        # Give the scene the attributes the menu option-model reads.
        scene._cfg = SceneCfg(type="generative", display=args.display, source=args.source)
        scene.display_mode = mode
        scene.duration_s = 120.0
        scene.target_fps = None
        menu = MenuOverlay(scene, api, can_save=False, prompt_to_save=False, save_fn=lambda: True)
        menu.sel = max(0, min(args.menu_sel, len(menu.items) - 1)) if menu.items else 0
        if args.menu_confirm:
            menu.dirty = True
            menu.state = "confirm"

    save_at = args.save_frame if args.save_frame is not None else args.frames - 1
    saved_path = None
    for i in range(args.frames):
        t = i * args.t_step
        frame = src.read(t, modulation)
        _render_with_overlays(mode, api, frame, overlays, t, scene, modulation)
        if menu is not None:
            menu.process_frame(api, scene, t)
        if i == save_at:
            img = fb.render()
            tag = (
                f"{args.source}_{args.display}"
                + (f"_{args.effect}" if args.effect else "")
                + (f"_{args.overlay}" if args.overlay else "")
                + ("_menu" if args.menu else "")
            )
            saved_path = args.out or os.path.join(OUT_DIR, f"render_{tag}_f{i}.png")
            cv2.imwrite(saved_path, img)

    print(
        f"wrote {saved_path}  (display={args.display} source={args.source} "
        f"effect={args.effect} frames={args.frames})"
    )


if __name__ == "__main__":
    main()
