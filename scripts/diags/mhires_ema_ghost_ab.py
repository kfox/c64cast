#!/usr/bin/env python3
"""Offline A/B of the mhires percell per-cell EMA constant (PERCELL_PICK_EMA_ALPHA)
against after-image / ghost persistence across a hard shot cut.

The ghost the user reports — an outline from the previous shot lingering for a
couple of seconds after a cut — lives entirely in the percell path's per-cell
colour histogram EMA (`modes._smoothed_cell_counts`, blended each frame with
`PERCELL_PICK_EMA_ALPHA`). It is a *compose-level* artifact: it is baked into
the screen/colour/bitmap RAM before anything is pushed to the wire, so a
software-VIC render of the compose output reproduces it exactly, deterministically,
with no hardware.

This steps a real video window through the production render path
(`scenes._render_with_overlays` → `Framebuffer`) at a fixed compose framerate,
sweeping the EMA alpha, and writes a montage: rows = alpha, cols = time samples
across (and after) the cut. Higher alpha = faster decay = shorter ghost; the
cost (more per-frame colour churn on noisy content) is what the low default
0.15 buys. The absolute ghost *duration* depends on `--fps` (the effective
displayed-frame rate); the *direction* across alphas does not.

    python -m scripts.diags.mhires_ema_ghost_ab \
        --video assets/videos/WarGames.webm --t0 26 --t1 32 --fps 15

Confirm the winning alpha on real hardware afterward (this is fps-approximate).
"""

from __future__ import annotations

import argparse
import os
from types import SimpleNamespace

import av
import cv2
import numpy as np

from c64cast import modes as modes_mod
from c64cast.config import ColorCfg, _build_display_mode
from c64cast.framebuffer import Framebuffer
from c64cast.palette import ColorFitAccumulator
from c64cast.scenes import _crop_to_aspect, _render_with_overlays

# Reuse the offline no-wire backend from render_offline (framebuffer shadows
# writes through the normal listener hook).
from scripts.diags.render_offline import RenderBackend

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")


def decode_window(path: str, t0: float, t1: float) -> list[tuple[float, np.ndarray]]:
    """Decode [t0, t1] seconds of `path` to a PTS-sorted list of (pts, BGR)."""
    container = av.open(path)
    vs = container.streams.video[0]
    tb = float(vs.time_base) if vs.time_base else 0.0
    container.seek(int(t0 * 1_000_000))  # AV_TIME_BASE (microseconds)
    out: list[tuple[float, np.ndarray]] = []
    for frame in container.decode(vs):
        pts = float(frame.pts) * tb if frame.pts is not None else 0.0
        if pts < t0:
            continue
        if pts > t1:
            break
        out.append((pts, frame.to_ndarray(format="bgr24")))
    container.close()
    out.sort(key=lambda p: p[0])
    return out


def resample(frames: list[tuple[float, np.ndarray]], t0: float, t1: float, fps: float):
    """Yield (tick_time, nearest-decoded-frame) at `fps` across [t0, t1]."""
    if not frames:
        return
    dt = 1.0 / fps
    ptss = np.array([p for p, _ in frames])
    t = t0
    while t <= t1 + 1e-6:
        idx = int(np.argmin(np.abs(ptss - t)))
        yield t, frames[idx][1]
        t += dt


def _timecode(s: float) -> str:
    return f"{int(s // 60):02d}:{s % 60:05.2f}"


def _new_mode(fit):
    """Fresh mhires mode + its own offline framebuffer. Returns (mode, api, fb)."""
    api = RenderBackend()
    fb = Framebuffer()
    api.add_write_listener(fb.on_write)
    mode = _build_display_mode("mhires", cell_strategy="frequency", color=ColorCfg())
    mode.setup(api)
    if fit is not None:
        mode.set_color_fit(fit)
    return mode, api, fb


_SCENE = SimpleNamespace(name="wargames/mhires", effect=None, overlays=[])


def render_sequence(
    frames: list[tuple[float, np.ndarray]],
    t0: float,
    t1: float,
    fps: float,
    alpha: float,
    fit,
    save_every: float,
    stateless: bool = False,
    hyst_scale: float = 1.0,
) -> list[tuple[float, np.ndarray]]:
    """Compose the window at `fps`, returning a subsampled (every `save_every` s)
    list of (tick_time, rendered BGR). With `stateless=True`, a FRESH mode is
    built for every frame (no EMA/hysteresis history) — the ghost-free ground
    truth. Otherwise a single persistent mode accumulates temporal state with
    EMA `alpha` and hysteresis bonuses scaled by `hyst_scale`. The EMA advances
    at the FULL `fps`; only output is subsampled."""
    modes_mod.PERCELL_PICK_EMA_ALPHA = alpha  # module global, read per-frame in compose
    base_q, base_c = 5000.0, 5000.0  # PERCELL_QUANT/CODE_HYSTERESIS_BONUS defaults
    modes_mod.PERCELL_QUANT_HYSTERESIS_BONUS = base_q * hyst_scale  # read in mode __init__
    modes_mod.PERCELL_CODE_HYSTERESIS_BONUS = base_c * hyst_scale
    mode, api, fb = _new_mode(fit)
    saved: list[tuple[float, np.ndarray]] = []
    next_save = t0
    for t, raw in resample(frames, t0, t1, fps):
        img = _crop_to_aspect(raw)
        if stateless:
            mode, api, fb = _new_mode(fit)  # discard all history each frame
        _render_with_overlays(mode, api, img, [], t, _SCENE, None)  # type: ignore[arg-type]
        if t + 1e-6 >= next_save:
            saved.append((t, fb.render()))
            next_save += save_every
    return saved


def label(img: np.ndarray, text: str) -> np.ndarray:
    out = img.copy()
    cv2.putText(out, text, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(out, text, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", default="assets/videos/WarGames.webm")
    ap.add_argument("--t0", type=float, default=26.0, help="window start (s)")
    ap.add_argument("--t1", type=float, default=32.0, help="window end (s)")
    ap.add_argument("--fps", type=float, default=15.0, help="compose (displayed-frame) rate")
    ap.add_argument(
        "--alphas",
        default="0.15,0.30,0.50,1.00",
        help="comma-separated EMA alphas (0.15 = current default)",
    )
    ap.add_argument(
        "--hyst-scale",
        type=float,
        default=1.0,
        help="scale the per-pixel/per-cell hysteresis bonuses (1.0 = current default, "
        "0.0 = no hysteresis) applied to every persistent render",
    )
    ap.add_argument("--save-every", type=float, default=0.4, help="montage column spacing (s)")
    ap.add_argument("--cell-w", type=int, default=384, help="montage cell width px")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    alphas = [float(a) for a in args.alphas.split(",")]

    print(f"[decode] {args.video} [{args.t0}, {args.t1}]s ...")
    frames = decode_window(args.video, args.t0, args.t1)
    print(f"[decode] {len(frames)} frames")
    if not frames:
        raise SystemExit("no frames decoded in window")

    # auto_fit: production prescans the whole file; a window scan is a faithful,
    # fast stand-in and the ghost mechanism is independent of it anyway.
    acc = ColorFitAccumulator()
    for _, im in frames:
        acc.add(im)
    fit = acc.result()
    print(f"[auto_fit] {'fit=' + str(fit) if fit else 'identity (no-op)'}")

    # Ghost isolator: render each frame STATELESSLY (fresh mode, no temporal
    # history) as the ghost-free ground truth, then measure how much each
    # stateful (persistent) render deviates from it. That deviation IS the
    # after-image, whatever mechanism (EMA / per-pixel quant hysteresis /
    # per-cell code hysteresis) produces it. A longer-decaying deviation curve
    # after a cut = a longer-lived ghost.
    print("[render] stateless reference (fresh mode per frame)")
    ref = render_sequence(
        frames, args.t0, args.t1, args.fps, 0.15, fit, args.save_every, stateless=True
    )
    ref_imgs = [im.astype(np.int16) for _, im in ref]

    col_times: list[float] = [t for t, _ in ref]
    metrics: dict[float, list[float]] = {}
    h = int(args.cell_w * 200 / 320)

    def row_of(seq, tag0: str) -> np.ndarray:
        cells = []
        for i, (t, im) in enumerate(seq):
            im = cv2.resize(im, (args.cell_w, h), interpolation=cv2.INTER_NEAREST)
            tag = tag0 + (f"  {_timecode(t)}" if i == 0 else "")
            cells.append(label(im, tag))
        return np.hstack(cells)

    rows = [row_of(ref, "stateless")]
    for alpha in alphas:
        print(f"[render] alpha={alpha}")
        seq = render_sequence(
            frames,
            args.t0,
            args.t1,
            args.fps,
            alpha,
            fit,
            args.save_every,
            hyst_scale=args.hyst_scale,
        )
        metrics[alpha] = [
            float(np.mean(np.abs(im.astype(np.int16) - r)))
            for (_, im), r in zip(seq, ref_imgs, strict=True)
        ]
        rows.append(row_of(seq, f"a={alpha:.2f}"))

    montage = np.vstack(rows)
    path = os.path.join(OUT_DIR, "mhires_ema_ghost_ab.png")
    cv2.imwrite(path, montage)
    print(f"[montage] wrote {path}  ({montage.shape[1]}x{montage.shape[0]})")

    # Ghost = deviation of the stateful render from the stateless ground truth.
    # A value that stays high for several samples after a cut = a lingering
    # after-image; watch whether raising alpha shrinks it.
    print("\n[ghost = mean|stateful - stateless|] per sample (higher = more ghost):")
    print("  alpha \\ t   " + " ".join(f"{t % 60:5.1f}" for t in col_times))
    for alpha in alphas:
        print(f"  a={alpha:<4.2f}    " + " ".join(f"{v:5.1f}" for v in metrics[alpha]))


if __name__ == "__main__":
    main()
