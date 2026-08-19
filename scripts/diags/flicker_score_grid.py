#!/usr/bin/env python3
"""Which blend pairs actually flicker? Score them by eye, blind, one page at a
time, with the positions randomized.

This is the run `[color].flicker_max_warmth` is waiting on. The rule it ships
with — cap how far along the red-orange axis either colour of a pair may sit —
was fitted to a session that scored 21 pairs on a display and then generalized
from four of them. Both the axis angle and the default threshold come out of
that fit, so they parameterize the observation rather than predicting it. This
script produces a set of verdicts the fit has never seen.

WHY THE OLD LADDER CANNOT BE REUSED. It laid its patches out sorted by ΔY, so
screen position was perfectly confounded with the variable under test: row means
ran 1.29 → 2.00 → 2.36 top to bottom, which a pure position effect would also
produce. Anything read off it about *degree* is unusable. Three things here
exist to fix that:

  * Positions are shuffled inside each page, and page order is shuffled too.
    The seed is printed and can be pinned with --seed, so a disagreement between
    two sittings can be replayed rather than argued about.
  * Pairs predicted to flicker never share a page with pairs predicted not to.
    A violently alternating patch makes its neighbours much harder to judge, so
    mixing the pools would put the loudest patches next to the quietest ones and
    contaminate exactly the readings that decide the threshold.
  * Every page carries one SOLID patch, placed at random and never announced.
    A solid cannot flicker — both fields hold the same colour — so calling one
    unsteady says the reading is picking up something other than fusion (the
    bank-swap residual, a capture beat, the room's lighting) and the page should
    be discounted. It is the only negative control available here.

BLIND. The script prints patch numbers and nothing else while you score. Which
pair sits in which slot, and which side of the threshold it was predicted to
fall on, is revealed only after the last page. Scoring a patch already labelled
"predicted to flicker" is not scoring it.

PACE. It waits on Enter between pages, with no timer anywhere. Take as long as a
page needs — the picture is still up while you type, and the machine is doing
nothing but holding it.

WHAT TO WRITE DOWN. Per patch, the thing that matters is a rating you apply
consistently, not a precise one: none / very mild / mild / moderate / intense.
Two extras are worth more than precision on the scale: note if a patch reads as
a colour you cannot name from the 16 (the blend working), and note whether the
flicker sits still or crawls, because a crawl is the display's chroma decoding
and not fusion.

    scripts/diags/flicker_score_grid.py                 # every pair, ~6 pages
    scripts/diags/flicker_score_grid.py --pool cool     # only the ones the
                                                        # default admits
    scripts/diags/flicker_score_grid.py --seed 1234     # replay a sitting

Run it yourself rather than through an agent — it blocks on your keypress
between pages. Outputs and the revealed key land in
scripts/diags/out/flickergrid/. Resets the U64 on exit; note that rest_reset
needs an http:// URL and does nothing on a u64:// one.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import _diaglib as d
import cv2
import numpy as np


@dataclass(frozen=True)
class Entry:
    """One patch: what to paint, and what the reveal says about it afterwards."""

    kind: str  # "blend" or "solid"
    pair: tuple[int, int]
    name: str
    bgr: tuple[float, float, float]
    luma_delta: float
    warmth: float
    predicted: str


Pool = tuple[list[Entry], list[Entry]]

HOST_PALETTE = "u64"

# The whole admissible set, so colours that no shipping default lets through
# still get judged. MAX_ALLOWED_LUMA_DELTA is the photosensitivity ceiling and
# is not raised here: a pair past it is refused by the renderer whatever this
# script asks for, and it is the one limit that should not be probed by eye.
SCORE_WARMTH_CAP = 1.0

# 320x200, all offsets multiples of 8. A patch that straddled a character cell
# would put two blend entries in one cell, which hires resolves by picking one —
# so the patch would quietly stop being the pair it is labelled as.
PAGE_W, PAGE_H = 320, 200
COLS_X = (24, 120, 216)
ROWS_Y = (16, 112)
PATCH_W, PATCH_H = 80, 72
SLOTS = len(COLS_X) * len(ROWS_Y)


def _slot_rect(slot: int) -> tuple[int, int, int, int]:
    x = COLS_X[slot % len(COLS_X)]
    y = ROWS_Y[slot // len(COLS_X)]
    return x, y, x + PATCH_W, y + PATCH_H


def build_page(entries: list[Entry | None], gutter: tuple[int, ...]) -> np.ndarray:
    """One page image. `entries` is up to SLOTS records in slot order."""
    img = np.zeros((PAGE_H, PAGE_W, 3), np.uint8)
    img[:, :] = gutter
    for slot, entry in enumerate(entries):
        if entry is None:
            continue
        x0, y0, x1, y1 = _slot_rect(slot)
        img[y0:y1, x0:x1] = np.round(np.asarray(entry.bgr, dtype=np.float64)).astype(np.uint8)
        # Label in the gutter, never on the patch: a digit drawn into the patch
        # gives that cell a second foreground and breaks the blend exactly where
        # the eye is being pointed.
        cv2.putText(
            img,
            str(slot + 1),
            (x0 + PATCH_W // 2 - 4, y0 - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    return img


def collect_entries(rng: random.Random) -> tuple[Pool, Pool]:
    """Every scorable pair, split into the two pools the rule predicts."""
    from c64cast.video import flicker
    from c64cast.video.palette import (
        C64_PALETTE_BGR,
        HOST_PALETTES,
        color_display_name,
        set_host_palette,
    )

    set_host_palette(HOST_PALETTES[HOST_PALETTE], name=HOST_PALETTE)
    cap = flicker.MAX_ALLOWED_LUMA_DELTA
    warm: list[Entry] = []
    cool: list[Entry] = []
    for a, b in flicker.blend_pairs(cap, max_warmth=SCORE_WARMTH_CAP):
        b0, g0, r0 = (float(v) for v in flicker.fuse(a, b))
        warmth = round(max(flicker.color_warmth(a), flicker.color_warmth(b)), 4)
        entry = Entry(
            kind="blend",
            pair=(int(a), int(b)),
            name=f"{color_display_name(a)}+{color_display_name(b)}",
            bgr=(b0, g0, r0),
            luma_delta=round(flicker.pair_luma_delta(a, b), 4),
            warmth=warmth,
            predicted="flickers" if warmth > flicker.DEFAULT_MAX_WARMTH else "steady",
        )
        (warm if entry.predicted == "flickers" else cool).append(entry)

    # Solid controls drawn from whatever the pool's pairs are built out of, so a
    # control never introduces a colour the page would not otherwise show.
    def solids_for(pool: list[Entry]) -> list[Entry]:
        used = sorted({c for e in pool for c in e.pair})
        return [
            Entry(
                kind="solid",
                pair=(c, c),
                name=f"{color_display_name(c)} (solid control)",
                bgr=tuple(float(v) for v in C64_PALETTE_BGR[c]),  # type: ignore[arg-type]
                luma_delta=0.0,
                warmth=round(flicker.color_warmth(c), 4),
                predicted="cannot flicker",
            )
            for c in used
        ]

    rng.shuffle(warm)
    rng.shuffle(cool)
    return (warm, solids_for(warm)), (cool, solids_for(cool))


def paginate(
    pool: list[Entry], solids: list[Entry], rng: random.Random
) -> list[list[Entry | None]]:
    """Pages of one pool, each with exactly one solid control, positions shuffled."""
    pages: list[list[Entry | None]] = []
    per_page = SLOTS - 1
    for i in range(0, len(pool), per_page):
        entries: list[Entry | None] = list(pool[i : i + per_page])
        entries.append(rng.choice(solids))
        entries += [None] * (SLOTS - len(entries))
        rng.shuffle(entries)
        pages.append(entries)
    return pages


def verify_page(entries: list[Entry | None]) -> list[str]:
    """Check each patch quantizes to the pair it is labelled as.

    A patch is only evidence about its pair if the renderer actually picks that
    blend entry for it. Painting the fused colour is not the same as getting it
    back — a neighbouring entry can win, and the failure is invisible on screen.
    """
    from c64cast.video import flicker

    table = flicker.build_blend_table(flicker.MAX_ALLOWED_LUMA_DELTA, max_warmth=SCORE_WARMTH_CAP)
    problems = []
    for slot, entry in enumerate(entries):
        if entry is None:
            continue
        px = np.array([entry.bgr], dtype=np.float32)
        idx = int(flicker.quantize_flat_blend(px, table, perceptual=True)[0])
        got = sorted(int(v) for v in table.pairs[idx])
        if got != sorted(entry.pair):
            problems.append(f"slot {slot + 1}: {entry.name} quantizes to {got}")
    return problems


def write_config(cfg_path: Path, image: Path) -> None:
    cfg_path.write_text(
        f"""
[audio]
enabled = false

[hardware]
host_palette = "{HOST_PALETTE}"

[color]
# auto_fit would remap the very colours the patches were painted to be, so the
# patch would stop being the pair it is labelled as. Same for dither.
auto_fit = false
dither = "none"
flicker_blend = true
flicker_max_luma_delta = {0.12}
# Wide open on purpose: the point is to score pairs the shipping default
# refuses, so the default cannot be what decides what gets looked at.
flicker_max_warmth = {SCORE_WARMTH_CAP}

[video]
use_reu_staged = false

[playlist]
loop = true

[[scenes]]
type = "slideshow"
display = "hires"
file = "{image}"
duration_s = 0
aspect_mode = "stretch"
"""
    )


def show_page(n: int, total: int, cfg: Path, url: str, log: Path) -> None:
    print(f"\n[page {n}/{total}] launching …")
    with open(log, "w") as lf:
        proc = subprocess.Popen(
            [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", url, "-v"],
            stdout=lf,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
        )
        try:
            time.sleep(9.0)  # boot + first rendered frame
            print(f"[page {n}/{total}] up. Patches are numbered on screen, 1-6.")
            print("             Score every numbered patch, then press Enter for the next page.")
            input("             > ")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument(
        "--pool",
        choices=("all", "warm", "cool"),
        default="all",
        help="'cool' is the set the shipping default admits; 'warm' is what it refuses.",
    )
    ap.add_argument("--seed", type=int, default=None, help="pin the layout to replay a sitting")
    ap.add_argument(
        "--gutter",
        default="black",
        choices=("black", "dark-gray", "medium-gray"),
        help="the solid the patches sit on (default black)",
    )
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    rng = random.Random(seed)
    out = d.out_dir() / "flickergrid"
    out.mkdir(parents=True, exist_ok=True)

    (warm, warm_solids), (cool, cool_solids) = collect_entries(rng)
    pages: list[list[Entry | None]] = []
    if args.pool in ("all", "cool"):
        pages += paginate(cool, cool_solids, rng)
    if args.pool in ("all", "warm"):
        pages += paginate(warm, warm_solids, rng)
    rng.shuffle(pages)

    from c64cast.video.palette import C64_PALETTE_BGR

    gutter_idx = {"black": 0, "dark-gray": 11, "medium-gray": 12}[args.gutter]
    gutter = tuple(int(v) for v in C64_PALETTE_BGR[gutter_idx])

    problems = [p for page in pages for p in verify_page(page)]
    if problems:
        print("REFUSING TO RUN — some patches do not render as the pair they are labelled as:")
        for p in problems:
            print("  " + p)
        raise SystemExit(2)

    manifest = {"seed": seed, "pool": args.pool, "pages": []}
    for n, entries in enumerate(pages, 1):
        img_path = out / f"page{n:02d}.png"
        cv2.imwrite(str(img_path), build_page(entries, gutter))
        cfg = out / f"page{n:02d}.toml"
        write_config(cfg, img_path)
        manifest["pages"].append(
            {"page": n, "image": str(img_path), "slots": [e and e.name for e in entries]}
        )

    print(f"seed {seed} — pass --seed {seed} to lay this out again")
    print(f"{len(pages)} pages, {sum(1 for pg in pages for e in pg if e)} patches")
    print("\nRate every numbered patch: none / very mild / mild / moderate / intense.")
    print("Also worth noting: does it read as a colour outside the 16, and does any")
    print("flicker sit still or crawl? Nothing is timed — take as long as you need.\n")

    try:
        for n in range(1, len(pages) + 1):
            show_page(n, len(pages), out / f"page{n:02d}.toml", args.url, out / f"page{n:02d}.log")
    finally:
        if not args.no_reset:
            print(f"\n[reset] {args.url}: {d.rest_reset(args.url)}")

    # Revealed only now. Everything above this line was deliberately blind.
    key = out / "key.json"
    detail = []
    for page, entries in zip(manifest["pages"], pages, strict=True):
        rows = []
        for slot, entry in enumerate(entries, 1):
            if entry is None:
                continue
            rows.append(
                {
                    "slot": slot,
                    "name": entry.name,
                    "kind": entry.kind,
                    "luma_delta": entry.luma_delta,
                    "warmth": entry.warmth,
                    "predicted": entry.predicted,
                }
            )
        detail.append({"page": page["page"], "patches": rows})
    key.write_text(json.dumps({"seed": seed, "pool": args.pool, "pages": detail}, indent=2))

    print("\n=== key (what was in each slot) ===")
    for page in detail:
        print(f"\npage {page['page']}")
        for row in sorted(page["patches"], key=lambda r: r["slot"]):
            print(
                f"  {row['slot']}  {row['name']:32s} ΔY {row['luma_delta']:.4f}  "
                f"warmth {row['warmth']:.3f}  predicted {row['predicted']}"
            )
    print(f"\nkey + pages + configs + logs: {out}")


if __name__ == "__main__":
    main()
