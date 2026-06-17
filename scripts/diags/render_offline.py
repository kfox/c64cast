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
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    api = RenderBackend()
    fb = Framebuffer()
    api.add_write_listener(fb.on_write)

    mode = _build_display_mode(args.display)
    mode.setup(api)
    src = build_generator(args.source)
    effect = build_effect(args.effect) if args.effect else None
    scene = SimpleNamespace(name=f"{args.source}/{args.display}", effect=effect, overlays=[])

    save_at = args.save_frame if args.save_frame is not None else args.frames - 1
    saved_path = None
    for i in range(args.frames):
        t = i * args.t_step
        frame = src.read(t)
        _render_with_overlays(mode, api, frame, [], t, scene)
        if i == save_at:
            img = fb.render()
            tag = f"{args.source}_{args.display}" + (f"_{args.effect}" if args.effect else "")
            saved_path = args.out or os.path.join(OUT_DIR, f"render_{tag}_f{i}.png")
            cv2.imwrite(saved_path, img)

    print(
        f"wrote {saved_path}  (display={args.display} source={args.source} "
        f"effect={args.effect} frames={args.frames})"
    )


if __name__ == "__main__":
    main()
