#!/usr/bin/env python3
"""Offline A/B of the mhires percell per-cell EMA constant (PERCELL_PICK_EMA_ALPHA)
against after-image / ghost persistence across a hard shot cut.

The ghost the user reports — an outline from the previous shot lingering for a
couple of seconds after a cut — lives entirely in the percell path's per-cell
color histogram EMA (`modes._smoothed_cell_counts`, blended each frame with
`PERCELL_PICK_EMA_ALPHA`). It is a *compose-level* artifact: it is baked into
the screen/color/bitmap RAM before anything is pushed to the wire, so a
software-VIC render of the compose output reproduces it exactly, deterministically,
with no hardware.

This steps a real video window through the production render path
(`scenes._render_with_overlays` → `Framebuffer`) at a fixed compose framerate,
sweeping the EMA alpha, and writes a montage: rows = alpha, cols = time samples
across (and after) the cut. Higher alpha = faster decay = shorter ghost; the
cost (more per-frame color churn on noisy content) is what the low default
0.15 buys. The absolute ghost *duration* depends on `--fps` (the effective
displayed-frame rate); the *direction* across alphas does not.

    python -m scripts.diags.mhires_ema_ghost_ab \
        --video CLIP --t0 26 --t1 32 --fps 15

Confirm the winning alpha on real hardware afterward (this is fps-approximate).
"""

from __future__ import annotations

import argparse
import os

import cv2
import numpy as np

from c64cast.app.config import ColorCfg
from c64cast.scenes.scenes import _crop_to_aspect
from c64cast.video.modes import base as modes_base
from c64cast.video.palette import ColorFit, ColorFitAccumulator
from scripts.diags._offline_video import OfflineMhires, iter_frames

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")


def _timecode(s: float) -> str:
    return f"{int(s // 60):02d}:{s % 60:05.2f}"


def fixed_fit_renderer(fit: ColorFit | None) -> OfflineMhires:
    return OfflineMhires(color=ColorCfg(auto_fit=False), fit=fit)


def render_sequence(
    video: str,
    t0: float,
    t1: float,
    fps: float,
    alpha: float,
    fit: ColorFit | None,
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
    modes_base.PERCELL_PICK_EMA_ALPHA = alpha  # modes.base global, read per-frame in compose
    base_q, base_c = 5000.0, 5000.0  # PERCELL_QUANT/CODE_HYSTERESIS_BONUS defaults
    modes_base.PERCELL_QUANT_HYSTERESIS_BONUS = base_q * hyst_scale  # read in mode __init__
    modes_base.PERCELL_CODE_HYSTERESIS_BONUS = base_c * hyst_scale
    renderer = fixed_fit_renderer(fit)
    saved: list[tuple[float, np.ndarray]] = []
    next_save = t0
    for tick in iter_frames(video, t0, t1, fps=fps):
        if stateless:
            renderer = fixed_fit_renderer(fit)  # discard all history each frame
        renderer.show(tick.frame, tick.t)
        if tick.t + 1e-6 >= next_save:
            saved.append((tick.t, renderer.render()))
            next_save += save_every
    return saved


def label(img: np.ndarray, text: str) -> np.ndarray:
    out = img.copy()
    cv2.putText(out, text, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(out, text, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
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

    # auto_fit: a fit fixed over the whole window keeps the color fit out of
    # the comparison, so only the pick state differs between the renders.
    acc = ColorFitAccumulator()
    frames = 0
    for tick in iter_frames(args.video, args.t0, args.t1, fps=args.fps):
        acc.add(_crop_to_aspect(tick.frame))
        frames += 1
    if not frames:
        raise SystemExit("no frames decoded in window")
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
        args.video, args.t0, args.t1, args.fps, 0.15, fit, args.save_every, stateless=True
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
            args.video,
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
