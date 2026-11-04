#!/usr/bin/env python3
"""Score mhires video quality offline: run clips through the production
render path into a software VIC and measure, per displayed frame, how close
the picture is to its source, what it costs the link, and how much it churns.

    python -m scripts.diags.mhires_video_eval score --clips out/clips.json --label base
    python -m scripts.diags.mhires_video_eval score --clips out/clips.json --label subtle \\
        --set flicker_tolerance=subtle
    python -m scripts.diags.mhires_video_eval score --clips out/clips.json --label area \\
        --interp AREA
    python -m scripts.diags.mhires_video_eval compare base subtle

    python -m scripts.diags.mhires_video_eval score --clips out/clips.json --label reset \\
        --cuts out/cuts.json --at-cut reset
    python -m scripts.diags.mhires_video_eval compare base reset

`--clips` is a JSON list of {"name", "path", "t0", "t1"}; a `path` that is an
image is scored as a still (`--still-frames` identical frames, the last one
scored). `--cuts` is a JSON object mapping a clip name to the source times,
in seconds, of the first frame of each true shot, or `video_cut_eval.py`'s
labels file, whose `cuts` lists are those times. With it the summary adds
`post_cut_*`: the means over the `--post-cut` frames from each cut
(`_mhires_metrics.post_cut_windows`).

`--at-cut` makes the renderer do something at each of those cuts, so a run
with it, compared against one without, measures what that change buys in the
frames after a cut:
  * reset: drop the per-cell pick state (EMA and hysteresis).
  * refit: start a new online color fit, converging as at a scene's start.
  * ahead: install a fit taken over the new shot's first frames before the
    shot is shown, as a lookahead buffer could. A first pass over the clip
    computes these fits. `--fit-max-gain`, `--fit-max-sat`, `--fit-max-rise`
    (from the fit before the cut, or the clip's opening fit with
    `--fit-rise-from opening`) and `--fit-blend` limit the installed fit
    (`limited_fit`).
A new fit moves the quantizer's target, so score `refit` and `ahead` on
`fid_src`; `fid_q` is the quantizer's error against a target that differs
between the runs.

Per frame (CSV under out/mhires_eval/LABEL/):
  * fidelity: OKLab squared error of the render, both images blurred first in
    linear light (`_mhires_metrics.blurred_oklab`), reported x10^4; plus SSIM
    on OKLab L, unblurred. Each is taken twice: `_q` against the quantizer's
    target (`OfflineMhires.quantizer_target`, the frame after the deliberate
    color shaping, so the quantizer's own error), and `_src` against the
    source frame.
  * DMA cost: writes, bytes, and the link time the Ultimate's cost model
    charges for them. The offline backend has no REU, so this is the
    host-DMA push path, a relative figure rather than any one rig's.
  * cells whose displayed pixels changed, and shimmer: of the cells whose
    source barely moved (mean OKLab distance below --still), the fraction
    whose displayed pixels changed anyway.

The source each frame is scored against is decoded at full resolution and
area-resized, so a decode-filter comparison (`--interp`) is scored against
the same reference whichever filter feeds the renderer.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from c64cast.app.config import ColorCfg
from c64cast.scenes.scenes import ONLINE_FIT_WARMUP_FRAMES, _crop_to_aspect
from c64cast.video.modes import base as modes_base
from c64cast.video.palette import ColorFit, ColorFitAccumulator
from scripts.diags import _mhires_metrics as m
from scripts.diags._offline_video import (
    OfflineMhires,
    Tick,
    iter_frames,
    link_cost_s,
    reference_frame,
)

OUT_ROOT = Path(__file__).resolve().parent / "out" / "mhires_eval"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
DEFAULT_STILL_DELTA = 0.01
FIDELITY_SCALE = 1e4
LABEL_TIME_SLACK_S = 0.006
DEFAULT_POST_CUT_FRAMES = 30
AT_CUT_ACTIONS = ("reset", "refit", "ahead")


@dataclass
class Clip:
    name: str
    path: str
    t0: float = 0.0
    t1: float | None = None


@dataclass
class Variant:
    label: str
    color: dict[str, Any]
    interpolation: str | None
    fps: float | None
    sigma: float
    still_delta: float
    still_frames: int
    ema_alpha: float | None
    at_cut: list[str]
    post_cut: int
    host_palette: str
    fit_max_gain: float | None = None
    fit_max_sat: float | None = None
    fit_blend: float = 1.0
    fit_max_rise: float | None = None
    fit_rise_from: str = "before"


@dataclass
class FrameRow:
    index: int
    t: float
    fid_q: float
    fid_src: float
    ssim_q: float
    ssim_src: float
    writes: int
    bytes: int
    link_ms: float
    cells_changed: int
    still_cells: int
    still_changed: int


def _still_ticks(path: str, frames: int) -> list[Tick]:
    img = cv2.imread(path)
    if img is None:
        raise SystemExit(f"could not read image: {path}")
    ref = reference_frame(img)
    return [Tick(i, i / 25.0, img.copy(), ref) for i in range(frames)]


def _clip_ticks(clip: Clip, variant: Variant, *, reference: bool) -> Iterator[Tick]:
    return iter_frames(
        clip.path,
        clip.t0,
        clip.t1,
        fps=variant.fps,
        interpolation=variant.interpolation,
        reference=reference,
    )


def _cut_at(t: float, cut_times: list[float], passed: set[float]) -> float | None:
    """The labeled cut this frame is the first shown frame at or after."""
    for c in cut_times:
        if c not in passed and t >= c - LABEL_TIME_SLACK_S:
            return c
    return None


IDENTITY_FIT = ColorFit(black=0.0, white=255.0, sat_mult=1.0)


def limited_fit(
    shot: ColorFit | None,
    before: ColorFit | None,
    max_gain: float | None,
    blend: float,
    max_sat: float | None = None,
    max_rise: float | None = None,
    anchor: ColorFit | None = None,
) -> ColorFit | None:
    """`shot` with its stretch gain capped at `max_gain` (the range widened
    about its center) and its saturation at `max_sat`, then lerped from
    `before` toward it by `blend`. `max_rise` caps both again at that factor
    of `anchor`'s own (`before` when unset), so 1.0 lets a cut only relax the
    fit."""
    if shot is None and before is None:
        return None
    fit = shot or IDENTITY_FIT
    if max_rise is not None:
        prior = (anchor if anchor is not None else before) or IDENTITY_FIT
        rise_gain = 255.0 / max(prior.white - prior.black, 1e-6) * max_rise
        rise_sat = prior.sat_mult * max_rise
        max_gain = rise_gain if max_gain is None else min(max_gain, rise_gain)
        max_sat = rise_sat if max_sat is None else min(max_sat, rise_sat)
    if max_gain is not None and 255.0 / max(fit.white - fit.black, 1e-6) > max_gain:
        center = (fit.black + fit.white) / 2
        half = 255.0 / max_gain / 2
        black = max(0.0, min(center - half, 255.0 - 2 * half))
        fit = ColorFit(black=black, white=black + 2 * half, sat_mult=fit.sat_mult)
    if max_sat is not None and fit.sat_mult > max_sat:
        fit = ColorFit(black=fit.black, white=fit.white, sat_mult=max_sat)
    base = before or IDENTITY_FIT
    return ColorFit(
        black=base.black + (fit.black - base.black) * blend,
        white=base.white + (fit.white - base.white) * blend,
        sat_mult=base.sat_mult + (fit.sat_mult - base.sat_mult) * blend,
    )


def shot_fits(clip: Clip, variant: Variant, cut_times: list[float]) -> dict[float, ColorFit | None]:
    """Per labeled cut, the fit the online warm-up would reach over the new
    shot's first ONLINE_FIT_WARMUP_FRAMES frames, or the whole shot if it is
    shorter."""
    accs: dict[float, ColorFitAccumulator] = {}
    taken = 0
    for tick in _clip_ticks(clip, variant, reference=False):
        cut = _cut_at(tick.t, cut_times, set(accs))
        if cut is not None:
            accs[cut] = ColorFitAccumulator(strength=1.0)
            taken = 0
        if accs and taken < ONLINE_FIT_WARMUP_FRAMES:
            accs[list(accs)[-1]].add(_crop_to_aspect(tick.frame))
            taken += 1
    return {cut: acc.result() for cut, acc in accs.items()}


def score_clip(clip: Clip, variant: Variant, cut_times: list[float]) -> dict[str, Any]:
    cv2.setNumThreads(1)
    if variant.ema_alpha is not None:
        modes_base.PERCELL_PICK_EMA_ALPHA = variant.ema_alpha
    renderer = OfflineMhires(color=ColorCfg(**variant.color), host_palette=variant.host_palette)
    is_still = Path(clip.path).suffix.lower() in IMAGE_SUFFIXES
    ticks = (
        iter(_still_ticks(clip.path, variant.still_frames))
        if is_still
        else _clip_ticks(clip, variant, reference=True)
    )
    fits = shot_fits(clip, variant, cut_times) if "ahead" in variant.at_cut else {}
    passed: set[float] = set()
    opening: ColorFit | None = None
    rows: list[FrameRow] = []
    prev_out: np.ndarray | None = None
    prev_ref_lab: np.ndarray | None = None
    for tick in ticks:
        assert tick.reference is not None
        cut = _cut_at(tick.t, cut_times, passed)
        if cut is not None:
            passed.add(cut)
            if "reset" in variant.at_cut:
                renderer.reset_pick_state()
            if "refit" in variant.at_cut:
                renderer.restart_online_fit()
            if "ahead" in variant.at_cut:
                if not passed - {cut}:
                    opening = renderer.mode._color_fit or IDENTITY_FIT
                renderer.install_fit(
                    limited_fit(
                        fits[cut],
                        renderer.mode._color_fit,
                        variant.fit_max_gain,
                        variant.fit_blend,
                        variant.fit_max_sat,
                        variant.fit_max_rise,
                        opening if variant.fit_rise_from == "opening" else None,
                    )
                )
        writes = renderer.show(tick.frame, tick.t)
        out = renderer.render()
        target = renderer.quantizer_target()
        ref_lab = m.oklab(tick.reference)
        out_lab = m.oklab(out)
        target_lab = m.oklab(target)
        out_seen = m.blurred_oklab(out, variant.sigma)
        changed = (
            m.cells_changed(prev_out, out)
            if prev_out is not None
            else np.ones((m.CELL_ROWS, m.CELL_COLS), bool)
        )
        still = (
            m.cell_mean_delta(prev_ref_lab, ref_lab) < variant.still_delta
            if prev_ref_lab is not None
            else np.zeros_like(changed)
        )
        rows.append(
            FrameRow(
                index=tick.index,
                t=round(tick.t, 4),
                fid_q=m.oklab_mse(m.blurred_oklab(target, variant.sigma), out_seen)
                * FIDELITY_SCALE,
                fid_src=m.oklab_mse(m.blurred_oklab(tick.reference, variant.sigma), out_seen)
                * FIDELITY_SCALE,
                ssim_q=m.ssim(target_lab[..., 0], out_lab[..., 0]),
                ssim_src=m.ssim(ref_lab[..., 0], out_lab[..., 0]),
                writes=len(writes),
                bytes=sum(w.size for w in writes),
                link_ms=link_cost_s(writes) * 1000.0,
                cells_changed=int(changed.sum()),
                still_cells=int(still.sum()),
                still_changed=int((changed & still).sum()),
            )
        )
        prev_out, prev_ref_lab = out, ref_lab
    if is_still:
        rows = rows[-1:]
    return {"clip": clip.name, "rows": rows}


def summarize(rows: list[FrameRow], cut_times: list[float] | None, post_cut: int) -> dict[str, Any]:
    if not rows:
        return {"frames": 0}
    steady = rows[1:] or rows
    still = sum(r.still_cells for r in steady)
    summary: dict[str, Any] = {
        "frames": len(rows),
        "fid_q": float(np.mean([r.fid_q for r in rows])),
        "fid_src": float(np.mean([r.fid_src for r in rows])),
        "ssim_q": float(np.mean([r.ssim_q for r in rows])),
        "ssim_src": float(np.mean([r.ssim_src for r in rows])),
        "writes_per_frame": float(np.mean([r.writes for r in steady])),
        "bytes_per_frame": float(np.mean([r.bytes for r in steady])),
        "link_ms_per_frame": float(np.mean([r.link_ms for r in steady])),
        "cells_changed_per_frame": float(np.mean([r.cells_changed for r in steady])),
        "shimmer": sum(r.still_changed for r in steady) / still if still else None,
        "still_cell_share": still / (len(steady) * m.CELL_ROWS * m.CELL_COLS),
    }
    if cut_times:
        starts = [_first_index_at(rows, t) for t in cut_times]
        windows = m.post_cut_windows(len(rows), [i for i in starts if i is not None], post_cut)
        picked = [rows[i] for w in windows for i in w]
        summary["post_cut_cuts"] = len(windows)
        summary["post_cut_frames"] = len(picked)
        for key in POST_CUT_KEYS:
            summary[f"post_cut_{key}"] = (
                float(np.mean([getattr(r, key) for r in picked])) if picked else None
            )
    return summary


POST_CUT_KEYS = ("fid_q", "fid_src", "ssim_src", "link_ms", "cells_changed")


def _first_index_at(rows: list[FrameRow], t: float) -> int | None:
    """The first frame shown at or after a labeled time, which is given to
    the nearest millisecond or so."""
    for i, r in enumerate(rows):
        if r.t >= t - LABEL_TIME_SLACK_S:
            return i
    return None


def _parse_set(items: list[str]) -> dict[str, Any]:
    known = {f.name for f in fields(ColorCfg)}
    out: dict[str, Any] = {}
    for item in items:
        key, _, raw = item.partition("=")
        if key not in known:
            raise SystemExit(f"--set: [color] has no field {key!r}")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def load_cuts(path: str) -> dict[str, list[float]]:
    raw = json.loads(Path(path).read_text())
    return {name: v["cuts"] if isinstance(v, dict) else v for name, v in raw.items()}


def cmd_score(args: argparse.Namespace) -> int:
    clips = [Clip(**c) for c in json.loads(Path(args.clips).read_text())]
    if args.only:
        clips = [c for c in clips if c.name in set(args.only.split(","))]
    cuts = load_cuts(args.cuts) if args.cuts else None
    variant = Variant(
        label=args.label,
        color=_parse_set(args.set),
        interpolation=args.interp,
        fps=args.fps,
        sigma=args.sigma,
        still_delta=args.still,
        still_frames=args.still_frames,
        ema_alpha=args.ema_alpha,
        at_cut=args.at_cut,
        post_cut=args.post_cut,
        host_palette=args.host_palette,
        fit_max_gain=args.fit_max_gain,
        fit_blend=args.fit_blend,
        fit_max_sat=args.fit_max_sat,
        fit_max_rise=args.fit_max_rise,
        fit_rise_from=args.fit_rise_from,
    )
    if variant.at_cut and cuts is None:
        raise SystemExit("--at-cut needs --cuts")
    out_dir = OUT_ROOT / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = args.jobs or max(1, min(len(clips), (os.cpu_count() or 2) // 2))
    results: dict[str, dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(score_clip, c, variant, (cuts or {}).get(c.name, [])): c for c in clips
        }
        for fut in futures:
            clip = futures[fut]
            rows = fut.result()["rows"]
            with open(out_dir / f"{clip.name}.csv", "w", newline="") as fh:
                writer = csv.DictWriter(fh, [f.name for f in fields(FrameRow)])
                writer.writeheader()
                writer.writerows(r.__dict__ for r in rows)
            results[clip.name] = summarize(
                rows, None if cuts is None else cuts.get(clip.name), args.post_cut
            )
            print(f"[{args.label}] {clip.name}: {_fmt(results[clip.name])}", flush=True)
    meta = {"variant": variant.__dict__, "clips": results}
    (out_dir / "summary.json").write_text(json.dumps(meta, indent=2))
    print(f"wrote {out_dir}/summary.json")
    return 0


def _fmt(s: dict[str, Any]) -> str:
    if not s["frames"]:
        return "frames=0"
    parts = [
        f"frames={s['frames']}",
        f"fid_q={s['fid_q']:.2f} fid_src={s['fid_src']:.2f}",
        f"ssim_q={s['ssim_q']:.4f} ssim_src={s['ssim_src']:.4f}",
    ]
    parts.append(f"writes={s['writes_per_frame']:.2f} bytes={s['bytes_per_frame']:.0f}")
    parts.append(f"link_ms={s['link_ms_per_frame']:.1f} cells={s['cells_changed_per_frame']:.0f}")
    if s.get("shimmer") is not None:
        parts.append(f"shimmer={s['shimmer']:.3f}")
    if s.get("post_cut_fid_src") is not None:
        parts.append(
            f"post-cut fid_q={s['post_cut_fid_q']:.2f} fid_src={s['post_cut_fid_src']:.2f} "
            f"link_ms={s['post_cut_link_ms']:.1f} (cuts={s['post_cut_cuts']})"
        )
    return " ".join(parts)


COMPARE_KEYS = (
    "fid_q",
    "fid_src",
    "ssim_q",
    "link_ms_per_frame",
    "cells_changed_per_frame",
    "shimmer",
    "post_cut_fid_q",
    "post_cut_fid_src",
    "post_cut_link_ms",
)


def cmd_compare(args: argparse.Namespace) -> int:
    base = json.loads((OUT_ROOT / args.base / "summary.json").read_text())["clips"]
    for other_label in args.others:
        other = json.loads((OUT_ROOT / other_label / "summary.json").read_text())["clips"]
        print(
            f"\n{other_label} vs {args.base} (change in %; fidelity/link/cells/shimmer: lower is better)"
        )
        print(f"{'clip':<16}" + "".join(f"{k[:14]:>16}" for k in COMPARE_KEYS))
        for name in base:
            if name not in other:
                continue
            cells = []
            for k in COMPARE_KEYS:
                a, b = base[name].get(k), other[name].get(k)
                cells.append(
                    "—" if a in (None, 0) or b is None else f"{(b - a) / abs(a) * 100:+.1f}"
                )
            print(f"{name:<16}" + "".join(f"{c:>16}" for c in cells))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("score", help="score clips under one variant")
    sc.add_argument("--clips", required=True, help="JSON list of {name, path, t0, t1}")
    sc.add_argument("--label", required=True, help="run name; output goes to out/mhires_eval/LABEL")
    sc.add_argument("--only", help="comma-separated clip names to run")
    sc.add_argument(
        "--set", action="append", default=[], metavar="FIELD=VALUE", help="[color] override"
    )
    sc.add_argument("--interp", help="swscale filter for the decode downscale (AREA, BICUBIC, ...)")
    sc.add_argument(
        "--host-palette",
        default="u64",
        help="[hardware].host_palette the quantizer aims at and the render shows (pepto for a real VIC-II)",
    )
    sc.add_argument(
        "--fps", type=float, help="show at most this many frames/s (unset: every frame)"
    )
    sc.add_argument(
        "--sigma", type=float, default=m.DEFAULT_BLUR_SIGMA_PX, help="fidelity blur, px"
    )
    sc.add_argument("--still", type=float, default=DEFAULT_STILL_DELTA, help="still-cell threshold")
    sc.add_argument("--still-frames", type=int, default=3, help="frames to show an image for")
    sc.add_argument("--ema-alpha", type=float, help="rebind modes.base.PERCELL_PICK_EMA_ALPHA")
    sc.add_argument("--cuts", help="JSON {clip name: [cut times, s]}")
    sc.add_argument(
        "--at-cut", action="append", default=[], choices=AT_CUT_ACTIONS, help="act at each cut"
    )
    sc.add_argument(
        "--fit-max-gain",
        type=float,
        help="with --at-cut ahead: cap each shot fit's stretch gain, 255/(white-black)",
    )
    sc.add_argument(
        "--fit-max-sat",
        type=float,
        help="with --at-cut ahead: cap each shot fit's saturation multiplier",
    )
    sc.add_argument(
        "--fit-max-rise",
        type=float,
        help="with --at-cut ahead: cap gain and saturation at this factor of the fit before the cut (1.0: only relax)",
    )
    sc.add_argument(
        "--fit-rise-from",
        choices=("before", "opening"),
        default="before",
        help="what --fit-max-rise is a factor of: the fit before the cut, or the clip's opening fit",
    )
    sc.add_argument(
        "--fit-blend",
        type=float,
        default=1.0,
        help="with --at-cut ahead: move this far from the fit before the cut to the shot's (0..1)",
    )
    sc.add_argument(
        "--post-cut", type=int, default=DEFAULT_POST_CUT_FRAMES, help="frames scored after a cut"
    )
    sc.add_argument("--jobs", type=int, default=0, help="worker processes (0: half the cores)")
    sc.set_defaults(func=cmd_score)
    cp = sub.add_parser("compare", help="per-clip %% change of runs against a base run")
    cp.add_argument("base")
    cp.add_argument("others", nargs="+")
    cp.set_defaults(func=cmd_compare)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
