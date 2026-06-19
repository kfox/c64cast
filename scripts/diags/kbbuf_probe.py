#!/usr/bin/env python3
"""Does a CMD_KEYB-style buffer write survive for the 10 Hz poller to read?

Isolation probe (run with c64cast NOT running, so this owns the single DMA
socket and there's no menu poller to confound the readback). Brings the U64 to
c64cast's normal runtime state (reset + BASIC clear loop), writes a decoded
keystroke into KEYD ($0277) + NDX ($00C6) over DMA, then reads NDX/KEYD back
over REST several times to see whether the kernal leaves the buffer alone (so
the value persists) or clears/consumes it. This is the ground-truth check
behind the on-C64 menu's DMA key-injection path.

    scripts/diags/kbbuf_probe.py
    scripts/diags/kbbuf_probe.py --code 0x20   # SPACE (default)
"""

from __future__ import annotations

import argparse
import sys
import time

import _diaglib as d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--code", default="0x20", help="PETSCII code to inject (default 0x20 SPACE)")
    ap.add_argument("--boot-s", type=float, default=4.0)
    args = ap.parse_args()
    code = int(args.code, 0)

    from c64cast import config as cfgmod
    from c64cast.cli import make_backend

    cfg = cfgmod.Config()
    cfg.ultimate64.url = args.url
    cfg.debug.skip_probe = True
    api = make_backend(cfg)

    try:
        print("[setup] reset + BASIC clear loop")
        api.reset()
        api.run_basic_clear_loop()
        time.sleep(args.boot_s)

        # Baseline: what's in the buffer at rest?
        ndx0 = api.read_memory(0x00C6, 1)
        print(f"[baseline] NDX($00C6)={ndx0!r}")

        print(f"[inject] KEYD[0]=${code:02X}, NDX=1 (over DMA)")
        api.write_memory("0277", f"{code:02X}")
        api.write_memory("00C6", "01")
        api.flush()

        # Read back several times across ~1.5s to see persistence / decay.
        for i in range(8):
            ndx = api.read_memory(0x00C6, 1)
            keyd = api.read_memory(0x0277, 1)
            n = ndx[0] if ndx else -1
            k = keyd[0] if keyd else -1
            print(f"[t+{i * 0.2:.1f}s] NDX={n} KEYD[0]=${k:02X}")
            time.sleep(0.2)
    finally:
        code_reset = d.rest_reset(args.url)
        print(f"[reset] {'HTTP ' + str(code_reset) if code_reset else 'FAILED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
