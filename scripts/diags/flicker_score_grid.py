#!/usr/bin/env python3
"""Which blend pairs actually flicker? Score them by eye, blind, one page at a
time, with the positions randomized.

This IS `flicker.SCORED_FLICKER`: the tier table `[color].flicker_tolerance`
cuts across is a recording of a sitting with this script, not a rule. Two fitted
rules came before it — a ΔY threshold and a red-orange "warmth" axis — and both
were refuted the first time a run they had not been fitted to was scored. So
there is nothing left to predict with, and the table only changes by looking.

Run this to re-score the table, to settle a pair whose tier is in doubt, or to
score pairs another `host_palette` brings into range that nobody has judged yet.

NOTHING HERE IS FILTERED BY THE TABLE IT FEEDS. Each page's config sets
`[color].flicker_score_pairs` to exactly that page's pairs, which replaces the
blend set outright — no tier filter, no luma cap. Being bounded by the tiers
would make a wrong one permanent (a pair scored `intense` is in no blend table,
so it could never be rendered to be re-judged) and would leave an unscored
palette unscorable. Consequently this script can and does put pairs on screen
that no `flicker_tolerance` will ever admit.

WHY THE OLD LADDER CANNOT BE REUSED. It laid its patches out sorted by ΔY, so
screen position was perfectly confounded with the variable under test: row means
ran 1.29 → 2.00 → 2.36 top to bottom, which a pure position effect would also
produce. Anything read off it about *degree* is unusable. Three things here
exist to fix that:

  * Positions are shuffled inside each page, and page order is shuffled too.
    The seed is printed and can be pinned with --seed, so a disagreement between
    two sittings can be replayed rather than argued about.
  * Pairs already scored loud never share a page with pairs already scored quiet.
    A violently alternating patch makes its neighbors much harder to judge, so
    mixing the pools would put the loudest patches next to the quietest ones and
    contaminate exactly the readings that matter most. A pair with no tier yet
    is dealt with the loud pool: an unknown among the quiet patches could be
    anything, and that is the pool whose readings are easiest to spoil.
  * Every page carries one SOLID patch, placed at random and never announced.
    A solid cannot flicker — both fields hold the same color — so calling one
    unsteady says the reading is picking up something other than fusion (the
    bank-swap residual, a capture beat, the room's lighting) and the page should
    be discounted. It is the only negative control available here.

WATCH THE C64's OWN DISPLAY. The CRT, or a monitor driven straight off the
machine — never a capture preview or a screen recording. A capture samples the
alternation at its own rate and beats against it, which reads as the picture
slowly flipping between two versions of itself every few seconds, and on a still
page that is completely convincing. It is the capture, not the effect. The
calibration page runs a capture-based check that the C64 really is alternating
every field, so a slow flip after that check passes is known to be downstream.

CALIBRATION FIRST. One page comes up before any scoring, with its three patches
named: the loudest pair ever scored, a solid that cannot flicker at
all, and a real blend that reads as near-still. Without it the first scored page
is judged against nothing, and which page that is depends on the shuffle — a
scorer who opens on an all-quiet page sees a still picture and has no way to tell
"nothing here is flickering" apart from "I do not know what I am looking for".
The two pools are also dealt alternately rather than shuffled outright, so a run
of five all-loud pages cannot walk the scale in one direction unchecked.

BLIND after that. The script prints patch numbers and nothing else while you
score. Which pair sits in which slot, and what it was scored at last time, is
revealed only after the last page. Scoring a patch already labeled "this one
flickers" is not scoring it.

PACE. It waits on Enter between pages, with no timer anywhere. Take as long as a
page needs — the picture is still up while you type, and the machine is doing
nothing but holding it.

WHAT YOU ARE LOOKING FOR. A patch that shimmers in place: a fine, fast
unsteadiness over the whole patch, continuous for as long as you look at it. It
is not an event, and it has nothing to do with the page changing — the picture
settling as a page comes up is the scene starting, so ignore the first second.

WHAT TO WRITE DOWN. Per patch, a rating applied consistently matters more than a
precise one: none / very mild / mild / moderate / intense, printed with a gloss
on each before every page. Two extras are worth more than precision on the
scale: note if a patch reads as a color you cannot name from the 16 (the blend
working), and note whether any shimmer sits still or crawls, because a crawl is
the display's chroma decoding and not fusion.

    scripts/diags/flicker_score_grid.py                 # every pair, ~6 pages
    scripts/diags/flicker_score_grid.py --pool quiet    # only the ones already
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
from collections.abc import Callable
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
    prior: str
    predicted: str


Pool = tuple[list[Entry], list[Entry]]

HOST_PALETTE = "u64"

# Only has to be something other than "off": flicker_score_pairs supplies the
# actual set, and cannot switch blending on by itself.
SCORE_TOLERANCE = "clean"

# 320x200, all offsets multiples of 8. A patch that straddled a character cell
# would put two blend entries in one cell, which hires resolves by picking one —
# so the patch would quietly stop being the pair it is labeled as.
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


def candidate_pairs(cap: float) -> list[tuple[int, int]]:
    """Every pair worth putting on the chart: under the cap and far enough from
    a solid to be worth a page, whatever its tier is or is not.

    Deliberately not flicker.blend_pairs — that applies the tier filter this
    script exists to produce."""
    import itertools

    import numpy as np

    from c64cast.video import flicker

    out = []
    for a, b in itertools.combinations(range(16), 2):
        if flicker.pair_luma_delta(a, b) > cap:
            continue
        fused = flicker._to_lab(flicker.fuse(a, b)[None, :])
        gain = float(np.min(np.linalg.norm(flicker._PALETTE_LAB - fused, axis=1)))
        if gain >= flicker.MIN_BLEND_LAB_GAIN:
            out.append((a, b))
    return out


def collect_entries(rng: random.Random) -> tuple[Pool, Pool]:
    """Every scorable pair, split into the two pools by how it scored last time."""
    from c64cast.video import flicker
    from c64cast.video.palette import (
        C64_PALETTE_BGR,
        HOST_PALETTES,
        color_display_name,
        set_host_palette,
    )

    set_host_palette(HOST_PALETTES[HOST_PALETTE], name=HOST_PALETTE)
    cap = flicker.FLASH_CRITERION_LUMA_DELTA
    loud: list[Entry] = []
    quiet: list[Entry] = []
    for a, b in candidate_pairs(cap):
        b0, g0, r0 = (float(v) for v in flicker.fuse(a, b))
        tier = flicker.pair_flicker_tier(a, b)
        entry = Entry(
            kind="blend",
            pair=(int(a), int(b)),
            name=f"{color_display_name(a)}+{color_display_name(b)}",
            bgr=(b0, g0, r0),
            luma_delta=round(flicker.pair_luma_delta(a, b), 4),
            prior=tier or "unscored",
            predicted="unscored" if tier is None else tier,
        )
        is_quiet = tier is not None and flicker.FLICKER_TIERS.index(tier) <= 2
        (quiet if is_quiet else loud).append(entry)

    # Solid controls drawn from whatever the pool's pairs are built out of, so a
    # control never introduces a color the page would not otherwise show.
    def solids_for(pool: list[Entry]) -> list[Entry]:
        used = sorted({c for e in pool for c in e.pair})
        return [
            Entry(
                kind="solid",
                pair=(c, c),
                name=f"{color_display_name(c)} (solid control)",
                bgr=tuple(float(v) for v in C64_PALETTE_BGR[c]),  # type: ignore[arg-type]
                luma_delta=0.0,
                prior="n/a",
                predicted="cannot flicker",
            )
            for c in used
        ]

    rng.shuffle(loud)
    rng.shuffle(quiet)
    return (loud, solids_for(loud)), (quiet, solids_for(quiet))


# Anchors for the calibration page, both from the previous scoring run. The loud
# end is a pair no flicker_tolerance admits, which is exactly what it should be:
# the top of the scale has to be shown to be a reference, and flicker_score_pairs
# is what makes showing it possible.
CALIBRATION_LOUD = (6, 8)  # Blue + Orange — scored "intense"
CALIBRATION_QUIET = (6, 9)  # Blue + Brown — the one pair scored "none"


def calibration_page() -> list[Entry | None]:
    """A page with its answers given away, shown before any scoring starts.

    Without it the first scored page is judged against nothing, and which page
    that is depends on the shuffle. Opening on an all-quiet page shows a still
    picture, no reference for what the artifact looks like, and no way to tell
    "nothing here is flickering" apart from "I do not know what I am looking
    for". Not scored, and not in the key.
    """
    from c64cast.video import flicker
    from c64cast.video.palette import C64_PALETTE_BGR, color_display_name

    def blend(pair: tuple[int, int], label: str) -> Entry:
        a, b = pair
        b0, g0, r0 = (float(v) for v in flicker.fuse(a, b))
        return Entry(
            kind="calibration",
            pair=(a, b),
            name=f"{color_display_name(a)}+{color_display_name(b)}",
            bgr=(b0, g0, r0),
            luma_delta=round(flicker.pair_luma_delta(a, b), 4),
            prior=flicker.pair_flicker_tier(a, b) or "unscored",
            predicted=label,
        )

    solid_idx = 12
    solid = Entry(
        kind="calibration",
        pair=(solid_idx, solid_idx),
        name=f"{color_display_name(solid_idx)}",
        bgr=tuple(float(v) for v in C64_PALETTE_BGR[solid_idx]),  # type: ignore[arg-type]
        luma_delta=0.0,
        prior="n/a",
        predicted="a solid — cannot flicker",
    )
    return [
        blend(CALIBRATION_LOUD, "loudest available"),
        solid,
        blend(CALIBRATION_QUIET, "quietest"),
        None,
        None,
        None,
    ]


def interleave(
    quiet: list[list[Entry | None]], loud: list[list[Entry | None]]
) -> list[list[Entry | None]]:
    """Alternate the two pools page by page.

    Pages cannot mix pools — a loud patch makes its neighbors unjudgeable — but
    a fully shuffled page order can still deal five all-loud pages in a row,
    which walks the scorer's sense of scale in one direction with nothing to
    re-anchor against.
    """
    out: list[list[Entry | None]] = []
    for i in range(max(len(quiet), len(loud))):
        if i < len(quiet):
            out.append(quiet[i])
        if i < len(loud):
            out.append(loud[i])
    return out


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


def page_pairs(entries: list[Entry | None]) -> list[tuple[int, int]]:
    """The blend pairs on one page, for that page's flicker_score_pairs.

    Solid controls are excluded — a solid is the pair (c, c), which is already
    entry c of any table and would only collide with it."""
    seen: list[tuple[int, int]] = []
    for entry in entries:
        if entry is None or entry.pair[0] == entry.pair[1]:
            continue
        pair = (min(entry.pair), max(entry.pair))
        if pair not in seen:
            seen.append(pair)
    return seen


def verify_page(entries: list[Entry | None]) -> list[str]:
    """Check each patch quantizes to the pair it is labeled as.

    A patch is only evidence about its pair if the renderer actually picks that
    blend entry for it. Painting the fused color is not the same as getting it
    back — a neighboring entry can win, and the failure is invisible on screen.
    """
    from c64cast.video import flicker

    table = flicker.build_blend_table(
        flicker.FLASH_CRITERION_LUMA_DELTA,
        tolerance=SCORE_TOLERANCE,
        score_pairs=page_pairs(entries),
    )
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


def write_config(cfg_path: Path, image: Path, entries: list[Entry | None]) -> None:
    cfg_path.write_text(
        f"""
[audio]
enabled = false

[hardware]
host_palette = "{HOST_PALETTE}"

[color]
# auto_fit would remap the very colors the patches were painted to be, so the
# patch would stop being the pair it is labeled as. Same for dither.
auto_fit = false
dither = "none"
flicker_max_luma_delta = {0.12}
# Wide open on purpose: the point is to score pairs the shipping default
# refuses, so the default cannot be what decides what gets looked at.
flicker_tolerance = "{SCORE_TOLERANCE}"
# The page's own pairs, verbatim. Bypasses the tier table and the luma cap —
# this script has to be able to render what it is being asked to judge.
flicker_score_pairs = {[f"{a}+{b}" for a, b in page_pairs(entries)]}

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


SCALE = (
    "  none        sits perfectly still — a flat color, nothing moving in it",
    "  very mild   you have to look for it; only obvious against a still patch",
    "  mild        clearly unsteady once you notice, easy to stop noticing",
    "  moderate    obviously shimmering the whole time you look at it",
    "  intense     unpleasant; you would not put this on screen",
)


def check_source_alternation(device: int | str, seconds: float = 4.0) -> str:
    """Confirm the C64 is alternating at the field rate, with the page up.

    Worth the seconds it costs, because the failure it rules out is invisible
    from the host: everything about a slow flip between two pictures looks like
    a broken page-flip, and the C64 side can be perfect while the surface being
    watched samples it into a beat. A capture card sampling a 59.83 Hz
    alternation at its own rate shows one field for seconds at a time, and a
    still picture is exactly where that is most convincing.

    What is checked is that the two pages differ at all, which is the part the
    C64 owns. Whether *consecutive captured frames* alternate is a fact about
    the capture rate, not about the machine: a card delivering 30 fps against a
    59.83 Hz alternation samples the same field repeatedly and beats, which is
    normal and says nothing about the C64. An earlier version tested for
    alternating consecutive frames and failed a perfectly good sitting for it.
    """
    import numpy as np

    cap = d.open_capture(device)
    try:
        for _ in range(15):
            cap.read()
        frames = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            ok, f = cap.read()
            if ok and f is not None:
                frames.append(f[::8, ::8].astype(np.float32))
    finally:
        cap.release()
    if len(frames) < 30:
        return f"  source check SKIPPED — only {len(frames)} frames captured (no signal?)"
    stack = np.stack(frames)
    fps = len(frames) / seconds
    dist = np.abs(stack - stack[0]).mean(axis=(1, 2, 3))
    spread = float(dist.max())
    if spread < 0.5:
        return (
            "  SOURCE CHECK FAILED: every captured frame is the same picture.\n"
            "  The two pages are not alternating at all, so nothing here is\n"
            "  scorable — stop and report this rather than rating anything."
        )
    diffs = np.abs(np.diff(stack, axis=0)).mean(axis=(1, 2, 3))
    alternating = float((diffs > spread * 0.5).mean())
    note = "  SOURCE VERIFIED: the two pages differ and the C64 is flipping between them.\n"
    if alternating > 0.9:
        note += f"  The capture ({fps:.0f} fps) is resolving every field.\n"
    else:
        note += (
            f"  Note: the capture ({fps:.0f} fps) is too slow to resolve every field, so it\n"
            f"  beats — {1 - alternating:.0%} of its frame pairs are identical. That is the\n"
            "  capture, not the machine.\n"
        )
    return note + (
        "  Either way: if the DISPLAY slowly flips between two versions of the\n"
        "  picture, that is your screen sampling it and scoring is meaningless."
    )


def show_page(
    n: int,
    total: int,
    cfg: Path,
    url: str,
    log: Path,
    *,
    banner: str = "",
    probe: Callable[[], str] | None = None,
) -> None:
    label = "calibration" if n == 0 else f"page {n}/{total}"
    print(f"\n[{label}] launching …")
    with open(log, "w") as lf:
        proc = subprocess.Popen(
            [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", url, "-v"],
            stdout=lf,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
        )
        try:
            time.sleep(9.0)  # boot + first rendered frame
            print(f"[{label}] up.")
            if probe is not None:
                print(probe())
            if banner:
                print(banner)
            else:
                print("  Numbered patches, 1-6. Ignore the first second — the picture settling")
                print("  as the page comes up is the scene starting, not the effect.")
                print("  Rate each numbered patch:")
                print("\n".join(SCALE))
                print("  Also: does it read as a color outside the C64's 16, and does any")
                print("  shimmer sit still or crawl sideways?")
            print("  Take as long as you need, then press Enter.")
            input("  > ")
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
        choices=("all", "loud", "quiet"),
        default="all",
        help="'quiet' is what scored none/verymild/mild last time; 'loud' is the rest, plus anything unscored.",
    )
    ap.add_argument("--seed", type=int, default=None, help="pin the layout to replay a sitting")
    ap.add_argument(
        "--gutter",
        default="black",
        choices=("black", "dark-gray", "medium-gray"),
        help="the solid the patches sit on (default black)",
    )
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument(
        "--device",
        default=d.CAMLINK_DEVICE,
        help="capture device for the source self-check on the calibration page",
    )
    ap.add_argument(
        "--no-source-check",
        action="store_true",
        help="skip the capture-based check that the C64 is alternating every field",
    )
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    rng = random.Random(seed)
    out = d.out_dir() / "flickergrid"
    out.mkdir(parents=True, exist_ok=True)

    (loud, loud_solids), (quiet, quiet_solids) = collect_entries(rng)
    quiet_pages = paginate(quiet, quiet_solids, rng) if args.pool in ("all", "quiet") else []
    loud_pages = paginate(loud, loud_solids, rng) if args.pool in ("all", "loud") else []
    pages = interleave(quiet_pages, loud_pages)

    from c64cast.video.palette import C64_PALETTE_BGR

    gutter_idx = {"black": 0, "dark-gray": 11, "medium-gray": 12}[args.gutter]
    gutter = tuple(int(v) for v in C64_PALETTE_BGR[gutter_idx])

    problems = [p for page in pages for p in verify_page(page)]
    if problems:
        print("REFUSING TO RUN — some patches do not render as the pair they are labeled as:")
        for p in problems:
            print("  " + p)
        raise SystemExit(2)

    manifest = {"seed": seed, "pool": args.pool, "pages": []}
    for n, entries in enumerate(pages, 1):
        img_path = out / f"page{n:02d}.png"
        cv2.imwrite(str(img_path), build_page(entries, gutter))
        cfg = out / f"page{n:02d}.toml"
        write_config(cfg, img_path, entries)
        manifest["pages"].append(
            {"page": n, "image": str(img_path), "slots": [e and e.name for e in entries]}
        )

    calib = calibration_page()
    loud, solid, quiet = (e for e in calib[:3] if e is not None)
    calib_img = out / "page00.png"
    cv2.imwrite(str(calib_img), build_page(calib, gutter))
    write_config(out / "page00.toml", calib_img, calib)

    print(f"seed {seed} — pass --seed {seed} to lay this out again")
    print(f"{len(pages)} pages, {sum(1 for pg in pages for e in pg if e)} patches")
    print("\nWhat you are looking for: a patch that SHIMMERS IN PLACE — a fine, fast")
    print("unsteadiness across the whole patch, continuous for as long as you look at")
    print("it. It is not an event and has nothing to do with the page changing.\n")
    print("Rate each numbered patch:")
    print("\n".join(SCALE))
    print("\nA calibration page comes first, with its answers given away, so you can")
    print("see the two ends of that scale before anything is scored blind.\n")
    print("WATCH THE C64's OWN DISPLAY — the CRT, or a monitor driven straight off the")
    print("machine. NOT a capture preview and NOT a screen recording. A capture samples")
    print("the alternation at its own rate and beats against it, which shows up as the")
    print("picture slowly flipping between two versions of itself every few seconds.")
    print("That is the capture, not the effect, and it makes scoring meaningless.\n")

    banner = (
        "  CALIBRATION — not scored, and the only page whose contents you are told.\n"
        f"    1  {loud.name}  — the loudest pair scored, admitted by no setting\n"
        f"    2  {solid.name}  — a solid: two identical fields, so it CANNOT flicker\n"
        f"    3  {quiet.name}  — a real blend that reads as near-still\n"
        "  Look until 1 and 3 are clearly different to you, and until 2 looks like\n"
        "  nothing at all. That is the range every later patch is rated against."
    )

    try:
        probe = None if args.no_source_check else (lambda: check_source_alternation(args.device))
        show_page(
            0,
            len(pages),
            out / "page00.toml",
            args.url,
            out / "page00.log",
            banner=banner,
            probe=probe,
        )
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
                    "prior": entry.prior,
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
                f"previously {row['prior']}"
            )
    print(f"\nkey + pages + configs + logs: {out}")


if __name__ == "__main__":
    main()
