#!/usr/bin/env python3
"""Which W (pump write pointer) source is actually readable per pump path?

The rate servo (audio.py) steers on the ring write pointer W. Where W lives
depends on the path:

* tracked path (mhires + use_reu_staged): the $C200 RAM tracker mirrors the
  REU dst at $C203/$C204 every IRQ — reliable RAM.
* plain path (petscii/mcm): nothing maintains $C200; W lives only in the REU
  dst register $DF02/$DF03.

reu_margin_probe.py found that on the PLAIN path the $C203/$C204 tracker reads
a STATIC in-ring-looking garbage value, so it must NOT be trusted there — and
the servo's own W read ($DF02/$DF03) is unverified. This tool samples R, the
REU dst register, AND the tracker side-by-side and reports, for each candidate,
whether it MOVES and stays in-ring — i.e. whether it's a usable W for the servo.

    scripts/diags/reu_w_source_probe.py --config scripts/diags/reu_audio_plain.toml -t 15
    scripts/diags/reu_w_source_probe.py --config scripts/diags/reu_audio_tracked.toml -t 15

Read-only over REST; launches c64cast and resets on exit.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

import _diaglib as d

from c64cast.audio import (
    NMI_ROUTINE_ADDR,
    REU_AUDIO_SRC_TRACKER_ADDR,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
)

READ_PTR_ADDR = NMI_ROUTINE_ADDR + 5  # $C025 R
REU_DST_REG_ADDR = 0xDF02  # $DF02 plain W
TRK_DST_ADDR = REU_AUDIO_SRC_TRACKER_ADDR + 3  # $C203 tracked W


def _u16(b: bytes | None) -> int | None:
    return (b[0] | (b[1] << 8)) if b and len(b) == 2 else None


def _in_ring(a: int | None) -> bool:
    return a is not None and RING_BUFFER_ADDR <= a < RING_BUFFER_END


def _summary(name: str, vals: list[int | None]) -> str:
    good = [v for v in vals if v is not None]
    in_ring = [v for v in good if _in_ring(v)]
    distinct = sorted(set(in_ring))
    moves = len(distinct) > 1
    none_n = sum(1 for v in vals if v is None)
    oor_n = len(good) - len(in_ring)
    verdict = (
        "USABLE (moves, in-ring)"
        if moves and in_ring
        else "STATIC (in-ring but never changes → garbage)"
        if in_ring and not moves
        else "UNREADABLE"
    )
    sample = (
        f"min=${min(in_ring):04X} max=${max(in_ring):04X} distinct={len(distinct)}"
        if in_ring
        else "no in-ring reads"
    )
    return f"  {name:<10} {verdict}\n             {sample}  none={none_n} out-of-ring={oor_n}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", required=True)
    ap.add_argument("-t", "--seconds", type=float, default=15.0)
    ap.add_argument("--hz", type=float, default=20.0)
    ap.add_argument("--boot", type=float, default=7.0)
    ap.add_argument("--url", default=d.U64_URL)
    args = ap.parse_args()

    cfg = Path(args.config)
    if not cfg.exists():
        ap.error(f"config not found: {cfg}")
    print(f"[run] python -m c64cast --config {cfg}")
    app = subprocess.Popen(
        [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", args.url]
    )
    print(f"[boot] waiting {args.boot:g}s")
    time.sleep(args.boot)

    rs: list[int | None] = []
    dsts: list[int | None] = []
    trks: list[int | None] = []
    period = 1.0 / args.hz
    t0 = time.time()
    nxt = t0
    try:
        while time.time() - t0 < args.seconds:
            now = time.time()
            if now < nxt:
                time.sleep(nxt - now)
            nxt += period
            rs.append(_u16(d.rest_readmem(READ_PTR_ADDR, 2, args.url)))
            dsts.append(_u16(d.rest_readmem(REU_DST_REG_ADDR, 2, args.url)))
            trks.append(_u16(d.rest_readmem(TRK_DST_ADDR, 2, args.url)))
    finally:
        app.terminate()
        try:
            app.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.kill()
        print(f"[reset] machine:reset -> {d.rest_reset(args.url)}")

    print(
        f"\n[w-source] {len(rs)} samples over {args.seconds:g}s "
        f"(ring=${RING_BUFFER_ADDR:04X}-${RING_BUFFER_END - 1:04X})"
    )
    print(_summary("R $C025", rs))
    print(_summary("dst $DF02", dsts))
    print(_summary("trk $C203", trks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
