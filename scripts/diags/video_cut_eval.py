#!/usr/bin/env python3
"""Label the true shot cuts in a few clips, then score cut detectors against
those labels.

    # 1. Candidate frames and contact sheets to label from:
    python -m scripts.diags.video_cut_eval candidates --clips out/clips.json --only mv-fast-a
    # 2. Read the sheets, write out/cut_labels.json by hand (format below).
    # 3. Score the detectors:
    python -m scripts.diags.video_cut_eval score --clips out/clips.json --labels out/cut_labels.json

Candidates are the union of loose settings of all three detectors in
`_cut_detectors.py`, so a cut any of them could find at its real setting is
on a sheet. Each sheet row is one candidate: the two frames before it, the
candidate, and the two after, with the candidate's time.

The labels file maps a clip name to lists of source times in seconds, each
the first frame of the event:

    {"mv-fast-a": {"cuts": [31.24, ...], "flash": [], "fade": [], "motion": []}}

`cuts` are the true cuts. The other three name the things a detector may
mistake for a cut, so each false positive within `--tolerance` frames of one
is counted under it; any other false positive counts as "other". The times
also feed `mhires_video_eval.py --cuts`.

Clips and labels name local, untracked videos; both stay under
scripts/diags/out/, which is gitignored.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from c64cast.scenes.scenes import _crop_to_aspect
from scripts.diags._cut_detectors import (
    AdaptiveRatio,
    FfmpegScene,
    ProductionHistogram,
    analysis_frame,
)
from scripts.diags._mhires_metrics import match_events
from scripts.diags._offline_video import iter_frames

OUT_ROOT = Path(__file__).resolve().parent / "out" / "cut_eval"
SHEET_ROWS = 8
THUMB = (160, 100)
EVENT_KINDS = ("flash", "fade", "motion")
# Below 0.2 the ffmpeg score fires on most frames of fast motion, which
# buries the cuts on the sheets under hundreds of non-events.
LOOSE_FFMPEG_THRESHOLD = 0.2


def analysis_frames(clip: dict[str, Any]) -> tuple[list[float], list[np.ndarray]]:
    times: list[float] = []
    frames: list[np.ndarray] = []
    for tick in iter_frames(clip["path"], clip.get("t0", 0.0), clip.get("t1")):
        times.append(tick.t)
        frames.append(analysis_frame(_crop_to_aspect(tick.frame)))
    return times, frames


def detectors() -> dict[str, Callable[[], Any]]:
    return {
        "hist-prod": ProductionHistogram,
        "hist-every": lambda: ProductionHistogram(every_frame=True),
        "adaptive": AdaptiveRatio,
        "ffmpeg-0.3": lambda: FfmpegScene(0.3),
        "ffmpeg-0.4": lambda: FfmpegScene(0.4),
    }


def run_detector(make: Callable[[], Any], frames: list[np.ndarray]) -> list[int]:
    det = make()
    found: list[int] = []
    for frame in frames:
        found.extend(det.push(frame))
    return found


def loose_candidates(frames: list[np.ndarray]) -> list[int]:
    found: set[int] = set()
    found.update(run_detector(lambda: AdaptiveRatio(ratio=2.0, min_score=8.0, min_shot=1), frames))
    found.update(run_detector(lambda: FfmpegScene(LOOSE_FFMPEG_THRESHOLD), frames))
    found.update(run_detector(lambda: ProductionHistogram(every_frame=True), frames))
    return sorted(found)


def _thumb(frame: np.ndarray, text: str, mark: bool) -> np.ndarray:
    img = cv2.resize(frame, THUMB, interpolation=cv2.INTER_AREA)
    if mark:
        cv2.rectangle(img, (0, 0), (THUMB[0] - 1, THUMB[1] - 1), (0, 0, 255), 2)
    cv2.putText(img, text, (3, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, (3, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def cmd_candidates(args: argparse.Namespace) -> int:
    for clip in _clips(args):
        times, frames = analysis_frames(clip)
        cands = loose_candidates(frames)
        out = OUT_ROOT / clip["name"]
        out.mkdir(parents=True, exist_ok=True)
        for stale in out.glob("sheet_*.png"):
            stale.unlink()
        rows = []
        for k, i in enumerate(cands):
            strip = [
                _thumb(frames[j], f"{k} t={times[j]:.2f}" if j == i else f"{times[j]:.2f}", j == i)
                if 0 <= j < len(frames)
                else np.zeros((THUMB[1], THUMB[0], 3), np.uint8)
                for j in range(i - 2, i + 3)
            ]
            rows.append(np.hstack(strip))
        for s in range(0, len(rows), SHEET_ROWS):
            cv2.imwrite(
                str(out / f"sheet_{s // SHEET_ROWS:02d}.png"), np.vstack(rows[s : s + SHEET_ROWS])
            )
        (out / "candidates.json").write_text(
            json.dumps([{"k": k, "index": i, "t": round(times[i], 4)} for k, i in enumerate(cands)])
        )
        print(f"{clip['name']}: {len(frames)} frames, {len(cands)} candidates -> {out}")
    return 0


def _index_of(times: list[float], t: float) -> int:
    return int(np.argmin(np.abs(np.asarray(times) - t)))


def cmd_score(args: argparse.Namespace) -> int:
    labels = json.loads(Path(args.labels).read_text())
    totals: dict[str, dict[str, int]] = {}
    for clip in _clips(args):
        if clip["name"] not in labels:
            continue
        lab = labels[clip["name"]]
        times, frames = analysis_frames(clip)
        truth = [_index_of(times, t) for t in lab["cuts"]]
        events = {kind: [_index_of(times, t) for t in lab.get(kind, [])] for kind in EVENT_KINDS}
        print(f"\n{clip['name']}: {len(frames)} frames, {len(truth)} cuts")
        print(f"  {'detector':<12}{'P':>7}{'R':>7}{'F1':>7}{'TP':>5}{'FP':>5}{'FN':>5}  FP by kind")
        for name, make in detectors().items():
            match = match_events(run_detector(make, frames), truth, args.tolerance)
            kinds = _fp_kinds(match.false_positives, events, args.tolerance)
            print(
                f"  {name:<12}{match.precision:7.2f}{match.recall:7.2f}{match.f1:7.2f}"
                f"{len(match.true_positives):5d}{len(match.false_positives):5d}"
                f"{len(match.false_negatives):5d}  {kinds}"
            )
            tot = totals.setdefault(name, {"tp": 0, "fp": 0, "fn": 0} | dict.fromkeys(kinds, 0))
            tot["tp"] += len(match.true_positives)
            tot["fp"] += len(match.false_positives)
            tot["fn"] += len(match.false_negatives)
            for kind, n in kinds.items():
                tot[kind] = tot.get(kind, 0) + n
    print(f"\nall clips (tolerance ±{args.tolerance} frame)")
    for name, t in totals.items():
        p = t["tp"] / (t["tp"] + t["fp"]) if t["tp"] + t["fp"] else 0.0
        r = t["tp"] / (t["tp"] + t["fn"]) if t["tp"] + t["fn"] else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        kinds = {k: t[k] for k in (*EVENT_KINDS, "other")}
        print(f"  {name:<12}{p:7.2f}{r:7.2f}{f1:7.2f}{t['tp']:5d}{t['fp']:5d}{t['fn']:5d}  {kinds}")
    return 0


def _fp_kinds(fps: list[int], events: dict[str, list[int]], tol: int) -> dict[str, int]:
    kinds: dict[str, int] = dict.fromkeys((*EVENT_KINDS, "other"), 0)
    for f in fps:
        kind = next((k for k in EVENT_KINDS if any(abs(f - e) <= tol for e in events[k])), "other")
        kinds[kind] += 1
    return kinds


def _clips(args: argparse.Namespace) -> list[dict[str, Any]]:
    clips = json.loads(Path(args.clips).read_text())
    only = set(args.only.split(",")) if args.only else None
    return [c for c in clips if only is None or c["name"] in only]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    cand = sub.add_parser("candidates", help="write candidate cuts and contact sheets")
    cand.add_argument("--clips", required=True)
    cand.add_argument("--only")
    cand.set_defaults(func=cmd_candidates)
    sc = sub.add_parser("score", help="score the detectors against labeled cuts")
    sc.add_argument("--clips", required=True)
    sc.add_argument("--labels", required=True)
    sc.add_argument("--only")
    sc.add_argument("--tolerance", type=int, default=1, help="frames either side")
    sc.set_defaults(func=cmd_score)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
