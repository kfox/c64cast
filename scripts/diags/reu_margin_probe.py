#!/usr/bin/env python3
"""Probe the REU audio-pump pointer margin on a live U64.

The REU-staged audio path (``[audio].use_reu_pump = true``) free-runs two
pointers around the 8 KB ring at $4000-$5FFF at the same average rate:

* **R** — the NMI DAC *read* pointer, the self-modifying operand at
  $C025 (LO) / $C026 (HI) inside ``NMI_ROUTINE`` (audio.py). Reliable RAM read.
* **W** — the pump *write* pointer (REU->main DMA destination). In the
  bank-swap *tracked* path it's mirrored in the $C200 tracker (dst at
  $C203/$C204); in the plain path it lives only in the REU register
  $DF02/$DF03.

Correctness is purely about the *phase* between W and R: both modes lap the
ring once per period, so what matters is the gap. Seeding W half a ring behind
R (REU_PUMP_INITIAL_MARGIN, the echo/overlap fix) should hold the phase near
RING/2 = 4096 and keep the *nearest-lap distance* — min(phase, RING-phase) —
far from 0. A dip toward 0 is a near-lap = the audible echo/overlap.

This is a read-only memory probe (no ears needed): it polls R and W over REST
while c64cast plays, so it coexists with c64cast's DMA socket. It can
launch c64cast itself (``--config``) or attach to an already-running one
(``--attach``).

    scripts/diags/reu_margin_probe.py --config /tmp/reu_audio.toml -t 30
    scripts/diags/reu_margin_probe.py --attach -t 20          # c64cast already up

Writes a per-sample CSV under scripts/diags/out/ and prints a summary.
Resets the machine on exit unless --no-reset / --attach.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import time
from pathlib import Path

import _diaglib as d

from c64cast.audio import (
    NMI_ROUTINE_ADDR,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_PUMP_INITIAL_MARGIN,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_SIZE,
)

# Read-pointer operand: NMI_ROUTINE offset 5/6 -> LDA $00?? operand = $C025/$C026.
READ_PTR_ADDR = NMI_ROUTINE_ADDR + 5  # $C025 (LO), $C026 (HI)
# REU register block: dst pointer is $DF02 (LO) / $DF03 (HI).
REU_DST_REG_ADDR = 0xDF02


def _in_ring(addr: int) -> bool:
    return RING_BUFFER_ADDR <= addr < RING_BUFFER_END


def _read_pointers(url: str, w_pref: str = "auto") -> tuple[int | None, int | None, str]:
    """Return (R, W, w_source). Any pointer that can't be read sanely is None.

    ``w_pref``: which W (pump write pointer) source to read —
      "reg"     → REU dst register $DF02/$DF03 (the PLAIN-path source; the
                  $C200 tracker is NOT maintained there and reads static
                  garbage — see reu_w_source_probe.py).
      "tracker" → $C203/$C204 RAM tracker (the bank-swap TRACKED path).
      "auto"    → tracker if in-ring else reg (legacy; unsafe on plain).
    """
    r_bytes = d.rest_readmem(READ_PTR_ADDR, 2, url)
    r = (r_bytes[0] | (r_bytes[1] << 8)) if r_bytes and len(r_bytes) == 2 else None

    def _reg() -> int | None:
        reg = d.rest_readmem(REU_DST_REG_ADDR, 2, url)
        cand = (reg[0] | (reg[1] << 8)) if reg and len(reg) == 2 else None
        return cand if _in_ring(cand) else None

    def _tracker() -> int | None:
        trk = d.rest_readmem(REU_AUDIO_SRC_TRACKER_ADDR, 5, url)  # src LO/MI/HI, dst LO/HI
        cand = (trk[3] | (trk[4] << 8)) if trk and len(trk) == 5 else None
        return cand if _in_ring(cand) else None

    if w_pref == "reg":
        return r, _reg(), "reu_reg"
    if w_pref == "tracker":
        return r, _tracker(), "tracker"
    # auto (legacy)
    w = _tracker()
    if w is not None:
        return r, w, "tracker"
    return r, _reg(), "reu_reg"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--config", help="c64cast TOML (REU-staged audio) to launch")
    g.add_argument(
        "--attach",
        action="store_true",
        help="probe an already-running c64cast (don't launch/reset)",
    )
    ap.add_argument(
        "-t", "--seconds", type=float, default=30.0, help="poll duration after boot (default 30)"
    )
    ap.add_argument("--hz", type=float, default=20.0, help="poll rate (default 20)")
    ap.add_argument(
        "--boot",
        type=float,
        default=7.0,
        help="seconds to wait for c64cast boot+first-PLAY (default 7)",
    )
    ap.add_argument("--label", default="reu_margin")
    ap.add_argument(
        "--w",
        choices=["auto", "reg", "tracker"],
        default="auto",
        help="W (pump write ptr) source: reg=$DF02 (PLAIN path), "
        "tracker=$C203 (bank-swap TRACKED path), auto=legacy",
    )
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument(
        "--no-reset", action="store_true", help="leave the machine running (implied by --attach)"
    )
    args = ap.parse_args()

    app: subprocess.Popen[bytes] | None = None
    if args.config:
        cfg = Path(args.config)
        if not cfg.exists():
            ap.error(f"config not found: {cfg}")
        print(f"[run] python -m c64cast --config {cfg}")
        app = subprocess.Popen(
            [d.python_exe(), "-m", "c64cast", "--config", str(cfg), "--url", args.url]
        )
        print(f"[boot] waiting {args.boot:g}s for boot + first PLAY")
        time.sleep(args.boot)

    period = 1.0 / args.hz
    csv_path = d.stamped(args.label, "csv")
    rows: list[tuple[float, int, int, int, int]] = []  # t, R, W, phase, nearest_lap
    sources: set[str] = set()
    missed = 0

    print(f"[probe] polling R($C025/26) vs W for {args.seconds:g}s @ {args.hz:g}Hz")
    print(
        f"        ring=${RING_BUFFER_ADDR:04X}-${RING_BUFFER_END - 1:04X} "
        f"size={RING_BUFFER_SIZE} target_phase={REU_PUMP_INITIAL_MARGIN}"
    )
    t0 = time.time()
    next_t = t0
    try:
        while time.time() - t0 < args.seconds:
            now = time.time()
            if now < next_t:
                time.sleep(next_t - now)
            next_t += period
            r, w, src = _read_pointers(args.url, args.w)
            if r is None or w is None:
                missed += 1
                continue
            sources.add(src)
            phase = (w - r) % RING_BUFFER_SIZE  # how far W leads R, forward
            nearest = min(phase, RING_BUFFER_SIZE - phase)
            rows.append((round(now - t0, 3), r, w, phase, nearest))
    finally:
        if app is not None:
            app.terminate()
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.kill()
        if not args.no_reset and not args.attach:
            code = d.rest_reset(args.url)
            print(f"[reset] machine:reset -> {code}")

    if not rows:
        print(
            f"[FAIL] no valid pointer samples ({missed} missed reads). "
            "Is REU-staged audio actually playing? Is the REU enabled?"
        )
        return 1

    with csv_path.open("w") as f:
        f.write("t_s,R,W,phase,nearest_lap\n")
        for t, r, w, ph, nl in rows:
            f.write(f"{t},{r},{w},{ph},{nl}\n")

    phases = [r[3] for r in rows]
    nearest = [r[4] for r in rows]
    print(f"\n[probe] {len(rows)} samples, {missed} missed, W source(s)={sorted(sources)}")
    print(
        f"  phase (W-R mod ring): min={min(phases)} "
        f"median={int(statistics.median(phases))} max={max(phases)} "
        f"(target ~{REU_PUMP_INITIAL_MARGIN})"
    )
    print(
        f"  nearest-lap distance: min={min(nearest)} "
        f"median={int(statistics.median(nearest))} "
        f"(0 = lapped/echo; higher = safer)"
    )
    danger = sum(1 for n in nearest if n < 512)
    print(f"  near-lap events (<512 B ~64ms): {danger} / {len(rows)}")
    print(f"  csv -> {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
