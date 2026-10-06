#!/usr/bin/env python3
"""Soak the C128 VDC blit and attribute every lost bit to a VRAM data line.

The cartridge launches once; then, for each case, the host stages a "before"
and an "after" payload in C128 RAM, blits the before, reads VRAM back, blits
the after, reads it back, and tallies each wrong bit by data line (D0-D7) and
by what that bit held before the write. A fault in one VRAM chip, its socket,
or the VDC's own data bus shows up as one line. Swapping the two 64K x 4 VRAM
chips moves a chip fault by four lines; a fault that stays put is the socket,
a trace, or the VDC itself, and only swapping the VDC separates those.

    scripts/diags/vdc_bit_soak.py --serial <PORT>                 # 16 full frames, $FF over $FF
    scripts/diags/vdc_bit_soak.py --serial <PORT> --mixed         # five cases, top half only

The full-frame run writes all 24000 B of the 640x200 bitmap + attributes, so
the whole 80-column screen flashes. ``--mixed`` covers only the first 8000 B
(the top half of the bitmap) and cycles $FF over $00, $FF over $FF, $00 over
$FF, a ramp over its inverse, and a ramp over itself.

Staging is read back and retried before every case, so a TR+ link fault is
never scored as a VRAM error; clear the link with ``tr_dma_integrity.py``
first anyway. Blanks the VDC and resets the C128 on exit.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work from any cwd)
import vdc_c128 as v

from c64cast.hw import vdc, vdc_rom
from c64cast.hw.teensyrom_dma import TRClient

MIXED_BYTES = 8000  # under 0x2000, so the before and after payloads sit side by side
CHUNK = 4096


def cases(nbytes: int, mixed: bool) -> list[tuple[str, bytes, bytes]]:
    ones, zeros = b"\xff" * nbytes, b"\x00" * nbytes
    if not mixed:
        return [("FF over FF", ones, ones)]
    ramp = bytes((i * 37) & 0xFF for i in range(nbytes))
    inv = bytes(b ^ 0xFF for b in ramp)
    return [
        ("FF over 00", zeros, ones),
        ("FF over FF", ones, ones),
        ("00 over FF", ones, zeros),
        ("ramp over ~ramp", inv, ramp),
        ("ramp over ramp", ramp, ramp),
    ]


def stage(client: TRClient, addr: int, data: bytes) -> None:
    for _ in range(4):
        for off in range(0, len(data), CHUNK):
            client.write_segment(addr + off, data[off : off + CHUNK])
        back = b"".join(
            client.read_segment(addr + off, min(CHUNK, len(data) - off))
            for off in range(0, len(data), CHUNK)
        )
        if back == data:
            return
    raise SystemExit("staging into C128 RAM will not stick; fix the DMA link first")


def blit(client: TRClient, src: int, nbytes: int) -> None:
    before = v.issue(client, vdc_rom.CMD_BLIT, dst=vdc.BITMAP_BASE, count=nbytes, src=src)
    v.wait_done(client, before, timeout=10.0)


def run_case(
    client: TRClient, name: str, prev: bytes, nxt: bytes, trials: int, offsets: list[int]
) -> None:
    nbytes = len(nxt)
    src_prev = vdc_rom.FRAMEBUF_ADDR
    # A full frame leaves no room for a second payload, and its cases blit the
    # same bytes twice anyway.
    src_next = src_prev if prev == nxt else src_prev + 0x2000
    stage(client, src_prev, prev)
    if src_next != src_prev:
        stage(client, src_next, nxt)

    tally: Counter[object] = Counter()
    wrong = 0
    for _ in range(trials):
        blit(client, src_prev, nbytes)
        port = v.make_porthole(client)
        held, noise_a = v._truth_read(port, vdc.BITMAP_BASE, nbytes)
        time.sleep(0.05)
        blit(client, src_next, nbytes)
        got, noise_b = v._truth_read(port, vdc.BITMAP_BASE, nbytes)
        for i in range(nbytes):
            if i in noise_a or i in noise_b or got[i] == nxt[i]:
                continue
            wrong += 1
            offsets.append(i)
            for bit in range(8):
                mask = 1 << bit
                if (got[i] ^ nxt[i]) & mask:
                    tally[(bool(held[i] & mask), bool(nxt[i] & mask))] += 1
                    tally[f"D{bit}"] += 1

    bits = sum(n for k, n in tally.items() if isinstance(k, tuple))
    transitions = "  ".join(
        f"{int(p)}->{int(w)}:{tally[(p, w)]}"
        for p in (False, True)
        for w in (False, True)
        if tally[(p, w)]
    )
    lines = " ".join(f"{k}:{n}" for k, n in sorted(tally.items(), key=str) if isinstance(k, str))
    print(
        f"{name:16s} {trials}x{nbytes} B: {wrong} B wrong, {bits} bits   {transitions}   {lines}",
        flush=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    link = ap.add_mutually_exclusive_group(required=True)
    link.add_argument("--serial", metavar="PORT", help="TR over serial")
    link.add_argument("--tcp", metavar="HOST", help="TR over TCP")
    ap.add_argument(
        "--mixed", action="store_true", help="five before/after cases over the top half"
    )
    ap.add_argument("--trials", type=int, help="passes per case (default 16 full, 5 mixed)")
    ap.add_argument("--reset-settle", type=float, default=5.0)
    args = ap.parse_args()

    nbytes = MIXED_BYTES if args.mixed else vdc.FRAME_BYTES
    trials = args.trials or (5 if args.mixed else 16)
    client = v.connect(tcp=args.tcp, serial=args.serial)
    offsets: list[int] = []
    try:
        if not v.stage_launch(client, args.reset_settle):
            return 1
        for name, prev, nxt in cases(nbytes, args.mixed):
            run_case(client, name, prev, nxt, trials, offsets)
    except TimeoutError:
        print("HANG: the resident loop stopped answering", flush=True)
        return 1
    finally:
        print(f"offsets: {sorted(offsets)}", flush=True)
        try:
            v.blank_screen(v.make_porthole(client))
        except Exception as e:  # noqa: BLE001  (best effort on the way out)
            print(f"blank failed: {e}")
        client.reset()
    return 0


if __name__ == "__main__":
    sys.exit(main())
