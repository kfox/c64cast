#!/usr/bin/env python3
"""Soak the TeensyROM+ launcher upload against the keyboard poller's reads.

    scripts/diags/tr_launcher_soak.py --url tr:// --cycles 300
    scripts/diags/tr_launcher_soak.py --url tr:// --cycles 300 --no-poller

Each cycle resets the C64 and launches a 12 KB PRG the way `launch_program`
does for `LauncherScene` (upload, then LaunchFile), while a
`CommodoreKeyPoller` reads $028D every 100 ms on the same link; `--no-poller`
is the control run without it. Then it reads the program back from $0801 and
compares; a cycle whose upload or LaunchFile failed names that step instead. The
PRG is `0 GOTO 0` followed by filler, so nothing decrunches over it or writes
to it while it runs.

A mismatch is read a second time: one that reads back clean then is counted as
a readback glitch, not a corrupt load. Pass = no corrupt load and no failed
launch. Resets the machine on exit.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time

import _diaglib as d

from c64cast.app.config import Config
from c64cast.app.connect import apply_to_config, parse_connection_uri
from c64cast.control.keyboard import CommodoreKeyPoller
from c64cast.hw.backend import make_backend
from c64cast.hw.teensyrom_api import _UPLOAD_DIR, TeensyROMBackend
from c64cast.hw.teensyrom_dma import TRError

BASIC_START = 0x0801
PRG_BYTES = 12 * 1024
# Load address $0801, then `0 GOTO 0`: link $0808, line 0, GOTO, "0", end of
# line, end of program.
GOTO_0 = bytes([0x01, 0x08, 0x08, 0x08, 0x00, 0x00, 0x89, 0x30, 0x00, 0x00, 0x00])
LAUNCH_SETTLE_S = 2.0
PROGRESS_EVERY = 25


def soak_prg() -> bytes:
    filler = bytes(i & 0xFF for i in range(PRG_BYTES - len(GOTO_0)))
    return GOTO_0 + filler


def describe_mismatch(got: bytes, want: bytes) -> str:
    first = next(i for i, (g, w) in enumerate(zip(got, want, strict=True)) if g != w)
    shape = ""
    if got[first:] == want[first + 1 :] + got[-1:]:
        shape = ", one byte dropped there"
    window = got[max(first - 2, 0) : first + 6].hex(" ")
    return f"first wrong byte at ${BASIC_START + first:04X}{shape}; read {window}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="tr://")
    ap.add_argument("--cycles", type=int, default=300)
    ap.add_argument(
        "--no-poller", action="store_true", help="control run: no keyboard poller on the link"
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="    %(name)s %(levelname)s: %(message)s")

    prg = soak_prg()
    want = prg[2:]
    dest = f"{_UPLOAD_DIR}/soak.prg"

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)
    if not isinstance(be, TeensyROMBackend):
        print("[abort] not a TeensyROM URL")
        be.close()
        return 1
    poller = CommodoreKeyPoller(be)
    clean = corrupt = glitch = failed = 0
    t0 = time.monotonic()
    try:
        poller_state = "off" if args.no_poller else "on"
        print(
            f"firmware: {be.tr.ping()!r}; {args.cycles} cycles of a {len(prg)} B PRG,"
            f" keyboard poller {poller_state}"
        )
        if not args.no_poller:
            poller.start(threading.Event(), threading.Event())
        for cycle in range(1, args.cycles + 1):
            be.reset()
            try:
                # launch_program's own two steps, split so a failure names the one it hit.
                step = "upload"
                be.invalidate_cache()
                be._upload(prg, dest)
                step = "LaunchFile"
                be.tr.launch_file(dest, be._drive)
            except (OSError, TRError) as e:
                failed += 1
                print(f"  cycle {cycle}: {step} failed: {type(e).__name__}: {e}")
                continue
            time.sleep(LAUNCH_SETTLE_S)
            got = be.read_memory(BASIC_START, len(want))
            if got == want:
                clean += 1
            elif be.read_memory(BASIC_START, len(want)) == want:
                glitch += 1
                print(f"  cycle {cycle}: readback glitch, clean on the second read")
            else:
                corrupt += 1
                detail = "read failed" if got is None else describe_mismatch(got, want)
                print(f"  cycle {cycle}: CORRUPT load: {detail}")
            if cycle % PROGRESS_EVERY == 0:
                print(
                    f"  {cycle}/{args.cycles}: clean={clean} corrupt={corrupt}"
                    f" glitch={glitch} failed={failed} ({time.monotonic() - t0:.0f} s)"
                )
    finally:
        poller.stop()
        be.close()
        d.machine_reset(args.url)
    print(
        f"\nclean={clean} corrupt={corrupt} readback_glitch={glitch} launch_failed={failed}"
        f" of {args.cycles} ({time.monotonic() - t0:.0f} s)"
    )
    return 0 if corrupt == 0 and failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
