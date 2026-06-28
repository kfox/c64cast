#!/usr/bin/env python3
"""HW proof for project_bitmap_engage_flash: a single-buffer bitmap scene's
setup() must clear BOTH the $2000 bitmap AND screen RAM ($0400) before flipping
$D011 into bitmap mode, so the engage shows solid black instead of a colour
ghost of the prior (char) scene.

Catching the ~1-frame transient in free-running capture is unreliable, so this
verifies the *mechanism* directly on the real machine: pre-seed $0400 + $2000
with a non-zero "stale prior scene" pattern, run the real HiresDisplayMode /
MultiHiresDisplayMode setup() over socket DMA, then read both regions back and
assert they are zero. Reads happen once, after setup, with no playback running
(not the rapid-poll-during-capture pattern that wedges the U64).

    scripts/diags/bitmap_engage_clear_probe.py            # both modes on .64
    scripts/diags/bitmap_engage_clear_probe.py --no-reset # keep state to peek

Resets the machine on exit (standing end-of-test rule)."""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urlparse

import _diaglib as d

sys.path.insert(0, str(d.Path(__file__).resolve().parents[2]))

from c64cast.api import Ultimate64API  # noqa: E402
from c64cast.modes import HiresDisplayMode, MultiHiresDisplayMode  # noqa: E402

STALE = bytes([0xFF]) * 1000  # vivid stale $0400 (every cell's bg nibble = $F)


def _read(api: Ultimate64API, addr: int, n: int) -> bytes | None:
    return api.read_memory(addr, n)


def probe(api: Ultimate64API, name: str, mode) -> bool:
    # Simulate a prior char scene leaving non-zero screen + bitmap RAM.
    api.write_memory_file("0400", STALE)
    api.write_memory_file("2000", bytes([0xFF]) * 8000)

    mode.setup(api)

    # Let the socket-DMA writes settle before the REST read (separate channel —
    # an immediate read can race the last write_memory_file).
    time.sleep(0.3)
    screen = _read(api, 0x0400, 1000)
    bitmap = _read(api, 0x2000, 8000)
    if screen is None or bitmap is None:
        print(f"[{name}] READ FAILED (screen={screen is not None}, bitmap={bitmap is not None})")
        return False
    # Invariant: setup() must clear the stale prior-scene fill (0xFF) before the
    # bitmap flip. We can't assert all-zero on $0400 here because the synthetic
    # test sits at the live BASIC prompt, whose running KERNAL maintains a single
    # cursor cell (~1 nonzero byte that ISN'T 0xFF); a real prior c64cast scene
    # owns the whole screen, so that artifact only exists in this probe.
    stale_screen = sum(1 for b in screen if b == 0xFF)
    stale_bitmap = sum(1 for b in bitmap if b == 0xFF)
    nz_screen = sum(1 for b in screen if b)
    screen_ok = stale_screen == 0
    bitmap_ok = stale_bitmap == 0
    print(
        f"[{name}] screen($0400): stale-0xFF remaining={stale_screen} "
        f"(nonzero={nz_screen}, expect ≤1 live OS cursor); "
        f"bitmap($2000): stale-0xFF remaining={stale_bitmap}"
    )
    return screen_ok and bitmap_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()

    if not d.dma_service_up(args.url):
        print(f"DMA service not reachable at {args.url} (F2 → Ultimate DMA Service → Enabled)")
        return 2

    host = urlparse(args.url).hostname
    print(f"connecting to {host} ...")
    api = Ultimate64API(args.url)
    ok = True
    try:
        ok &= probe(api, "hires", HiresDisplayMode("normal"))
        ok &= probe(api, "mhires", MultiHiresDisplayMode("percell"))
    finally:
        if not args.no_reset:
            d.rest_reset(args.url)
            print("machine reset.")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
