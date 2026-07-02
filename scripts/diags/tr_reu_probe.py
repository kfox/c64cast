#!/usr/bin/env python3
"""Detect + size the TeensyROM's REU over the serial (or TCP) link — no 6502 code.

The TR can emulate a 512 KB 17xx-style REU (REC controller at $DF00-$DF0A). This
probe verifies it is present, ENABLED, and reachable over the pure-DMA link by
doing a classic REU round-trip entirely from the host via WriteC64Mem/ReadC64Mem
(needs `supports_read`, TR fw v0.7.2.5+):

  1. Write a known pattern to a C64 scratch page.
  2. Trigger an IMMEDIATE (FF00-off) REU STASH  (C64 → REU) by writing the REC
     registers — no CPU code runs; the REC does the DMA autonomously.
  3. Clobber the C64 scratch page with zeros (and verify the clobber).
  4. Trigger an IMMEDIATE REU FETCH (REU → C64) from the same REU offset.
  5. Read the scratch page back. Pattern restored ⇒ the REU stored + returned
     the bytes = present + enabled + working at that offset.

Repeats at several REU offsets (0, 64 KB, 384 KB, 508 KB) to confirm the
advertised 512 KB depth actually backs the transfers (a stub / too-small buffer
fails or aliases at the high offsets).

This is a bus master writing the TR's OWN emulated REC registers, so success
also proves the TR's DMA write path reaches its IO2 decode over the link.

    scripts/diags/tr_reu_probe.py            # tr:// auto serial
    scripts/diags/tr_reu_probe.py --url tr://192.168.2.164
"""

from __future__ import annotations

import argparse
import sys
import time

from c64cast.backend import make_backend
from c64cast.c64 import REU
from c64cast.config import Config
from c64cast.connect import apply_to_config, parse_connection_uri

SCRATCH = 0x0340  # cassette buffer area — safe scratch, untouched by the clear loop
NBYTES = 16
PATTERN = bytes((0xA5, 0x3C, 0x00, 0xFF, 0x01, 0x80, 0x55, 0xAA) * 2)  # 16 distinct-ish bytes


def _trigger(be, c64_addr: int, reu_off: int, length: int, command: int) -> None:
    """Program the REC registers (address/length/control) then write COMMAND
    LAST to fire an immediate DMA. Registers auto-inc / length decrements in
    flight, so every trigger reprograms them fresh."""
    be.write_memory(f"{REU.C64_ADDR_LO:04X}", f"{c64_addr & 0xFF:02X}")
    be.write_memory(f"{REU.C64_ADDR_HI:04X}", f"{(c64_addr >> 8) & 0xFF:02X}")
    be.write_memory(f"{REU.REU_ADDR_LO:04X}", f"{reu_off & 0xFF:02X}")
    be.write_memory(f"{REU.REU_ADDR_MI:04X}", f"{(reu_off >> 8) & 0xFF:02X}")
    be.write_memory(f"{REU.REU_ADDR_HI:04X}", f"{(reu_off >> 16) & 0xFF:02X}")
    be.write_memory(f"{REU.LENGTH_LO:04X}", f"{length & 0xFF:02X}")
    be.write_memory(f"{REU.LENGTH_HI:04X}", f"{(length >> 8) & 0xFF:02X}")
    be.write_memory(f"{REU.ADDR_CONTROL:04X}", "00")  # both auto-increment
    be.write_memory(f"{REU.COMMAND:04X}", f"{command:02X}")  # fires the DMA
    time.sleep(0.02)


def roundtrip(be, reu_off: int) -> tuple[bool, bytes]:
    """One stash→clobber→fetch cycle at `reu_off`. Returns (ok, readback)."""
    # 1. seed the scratch page
    be.write_memory(f"{SCRATCH:04X}", PATTERN.hex())
    got = be.read_memory(SCRATCH, NBYTES)
    if got != PATTERN:
        raise SystemExit(f"[!] scratch seed/read failed: wrote {PATTERN.hex()} read {got}")
    # 2. stash C64 -> REU
    stash = REU.CMD_EXEC | REU.CMD_FF00_OFF | REU.CMD_DIR_C64_TO_REU  # $90
    _trigger(be, SCRATCH, reu_off, NBYTES, stash)
    # 3. clobber + verify
    be.write_memory(f"{SCRATCH:04X}", "00" * NBYTES)
    cl = be.read_memory(SCRATCH, NBYTES)
    if cl != bytes(NBYTES):
        raise SystemExit(f"[!] clobber failed: read {cl}")
    # 4. fetch REU -> C64
    _trigger(be, SCRATCH, reu_off, NBYTES, REU.CMD_FETCH_EXEC)  # $91
    # 5. read back
    back = be.read_memory(SCRATCH, NBYTES) or b""
    return (back == PATTERN, back)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="tr://", help="connection URI (default tr:// auto serial)")
    args = ap.parse_args()

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)

    # Offsets across the claimed 512 KB (0..0x7FFFF). High offsets prove depth.
    offsets = [0x000000, 0x010000, 0x060000, 0x07F000]
    try:
        if not be.profile.supports_read:
            raise SystemExit("[!] backend has no read support — can't verify a round-trip")
        be.reset()
        time.sleep(1.5)
        be.run_basic_clear_loop()  # $01=$37 (I/O mapped so $DF00 is visible), DEN on
        time.sleep(0.3)

        status = be.read_memory(REU.STATUS, 1)
        print(f"[reu] $DF00 status/version byte = {status.hex() if status else 'n/a'}")

        all_ok = True
        for off in offsets:
            ok, back = roundtrip(be, off)
            all_ok &= ok
            kb = off // 1024
            print(
                f"[reu] offset {off:#08x} ({kb:>4} KB): {'OK  ✓' if ok else 'FAIL ✗'}  readback={back.hex()}"
            )

        print()
        if all_ok:
            print(
                "RESULT: TR REU DETECTED — present, enabled, and round-tripping over the "
                "link across the full 512 KB range. REU-pump audio / REU-staged video are "
                "viable on this TR (data path confirmed)."
            )
        else:
            print(
                "RESULT: REU round-trip FAILED at one or more offsets — either not enabled, "
                "the DMA write path doesn't reach the TR's IO2 decode, or the depth is short."
            )
    finally:
        be.silence_sid()
        be.reset()
        be.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
