#!/usr/bin/env python3
"""Soak the C128 VDC blit and attribute every lost bit to a VRAM data line.

The cartridge launches once; then, for each case, the host stages a "before"
and an "after" payload in C128 RAM, blits the before, reads VRAM back, blits
the after, reads it back, and tallies each wrong bit of either blit by data
line (D0-D7) and by what that bit held before the write. A fault in one VRAM
chip, its socket, or the VDC's own data bus shows up as one line. Each of the
two VRAM chips holds four of the eight lines (64K x 4, or 16K x 4 on a stock
16 KB VDC), so swapping them moves a chip fault by four lines; a fault that stays put is the socket,
a trace, or the VDC itself, and only swapping the VDC separates those.

    scripts/diags/vdc_bit_soak.py --serial <PORT>                 # 16 full frames, $FF over $FF
    scripts/diags/vdc_bit_soak.py --serial <PORT> --mixed         # five cases, top half only

The full-frame run writes all 24000 B of the 640x200 bitmap + attributes, so
the whole 80-column screen flashes. On a 16 KB VDC the run selects 16 KB
addressing first (the cartridge programs 64 KB on every machine), where
everything above $3FFF aliases onto the bottom 16 KB, so there the full-frame
run covers only the 16384 B that exist and the screen shows the wrap. ``--mixed`` covers only the first 8000 B
(the top half of the bitmap) and cycles $FF over $00, $FF over $FF, $00 over
$FF, a ramp over its inverse, and a ramp over itself.

Staging is read back and retried before every case and checked again after
it, so a TR+ write fault into staging is flagged rather than scored as VRAM
loss. The VRAM readback crosses the same link and is only cross-checked by
reading twice, so clear the link with ``tr_dma_integrity.py`` first. Blanks the VDC and resets the C128 on exit.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work from any cwd)
import vdc_c128 as v
import vdc_second_machine as sm

from c64cast.hw import vdc, vdc_rom
from c64cast.hw.teensyrom_dma import TRClient

MIXED_BYTES = 8000  # under 0x2000, so the before and after payloads sit side by side
SMALL_VRAM_BYTES = 0x4000
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


def read_staging(client: TRClient, addr: int, nbytes: int) -> bytes:
    return b"".join(
        client.read_segment(addr + off, min(CHUNK, nbytes - off)) for off in range(0, nbytes, CHUNK)
    )


def stage(client: TRClient, addr: int, data: bytes) -> None:
    for _ in range(4):
        for off in range(0, len(data), CHUNK):
            client.write_segment(addr + off, data[off : off + CHUNK])
        if read_staging(client, addr, len(data)) == data:
            return
    raise SystemExit("staging into C128 RAM will not stick; fix the DMA link first")


def blit(client: TRClient, src: int, nbytes: int) -> None:
    before = v.issue(client, vdc_rom.CMD_BLIT, dst=vdc.BITMAP_BASE, count=nbytes, src=src)
    v.wait_done(client, before, timeout=10.0)


class Tally:
    def __init__(self) -> None:
        self.by_line: Counter[int] = Counter()
        self.by_move: Counter[tuple[bool, bool]] = Counter()
        self.offsets: list[int] = []

    def score(self, held: bytes | None, want: bytes, got: bytes, skip: set[int]) -> int:
        """Tally every bit of ``got`` that differs from ``want``; returns the
        byte count. ``held`` is what VRAM held before the blit, or None when
        that is unknown, in which case the bits count by line only."""
        wrong = 0
        for i in range(len(want)):
            if i in skip or got[i] == want[i]:
                continue
            wrong += 1
            self.offsets.append(i)
            for bit in range(8):
                mask = 1 << bit
                if (got[i] ^ want[i]) & mask:
                    self.by_line[bit] += 1
                    if held is not None:
                        self.by_move[(bool(held[i] & mask), bool(want[i] & mask))] += 1
        return wrong


def spans_text(offsets: list[int], limit: int = 40) -> str:
    spans = v._spans(sorted(set(offsets)), gap=1)
    text = " ".join(f"{a}" if b == a + 1 else f"{a}-{b - 1}" for a, b in spans[:limit])
    return f"{len(spans)} spans: {text}{' ...' if len(spans) > limit else ''}"


def run_case(
    client: TRClient, name: str, prev: bytes, nxt: bytes, trials: int, offsets: list[int]
) -> None:
    nbytes = len(nxt)
    src_prev = vdc_rom.FRAMEBUF_ADDR
    # A full frame leaves no room for a second payload, and its cases blit the
    # same bytes twice anyway.
    src_next = src_prev if prev == nxt else src_prev + 0x2000
    staged = {src_prev: prev, src_next: nxt}
    for addr, data in staged.items():
        stage(client, addr, data)

    port = v.make_porthole(client)
    tally = Tally()
    wrong_before = wrong_after = unread = 0
    last: bytes | None = None
    last_noise: set[int] = set()
    try:
        for _ in range(trials):
            blit(client, src_prev, nbytes)
            held, noise_a = v._truth_read(port, vdc.BITMAP_BASE, nbytes)
            wrong_before += tally.score(last, prev, held, noise_a | last_noise)
            time.sleep(0.05)
            blit(client, src_next, nbytes)
            got, noise_b = v._truth_read(port, vdc.BITMAP_BASE, nbytes)
            wrong_after += tally.score(held, nxt, got, noise_a | noise_b)
            unread += len(noise_a) + len(noise_b)
            last, last_noise = got, noise_b
    finally:
        offsets.extend(tally.offsets)

    bits = sum(tally.by_line.values())
    transitions = "  ".join(
        f"{int(p)}->{int(w)}:{tally.by_move[(p, w)]}"
        for p in (False, True)
        for w in (False, True)
        if tally.by_move[(p, w)]
    )
    lines = " ".join(f"D{b}:{n}" for b, n in sorted(tally.by_line.items()))
    print(
        f"{name:16s} {trials}x{nbytes} B: {wrong_after} B wrong after, {wrong_before} before, "
        f"{bits} bits, {unread} B unread   {transitions}   {lines}",
        flush=True,
    )
    if tally.offsets:
        print(f"{'':16s} offsets {spans_text(tally.offsets)}", flush=True)
    moved = [f"${a:04X}" for a, data in staged.items() if read_staging(client, a, nbytes) != data]
    if moved:
        print(
            f"{'':16s} STAGING CHANGED at {', '.join(moved)} during the case: "
            "the errors above may be link faults, not VRAM",
            flush=True,
        )


def soak_bytes(port: vdc.VdcPorthole, mixed: bool) -> int | None:
    """How many bytes each blit covers, or None when VRAM size can't be read.

    The cartridge programs 64 KB addressing on every machine, which a 16 KB
    VDC decodes wrong, so the size comes from the Editor ROM's aliasing test
    and a 16 KB VDC is switched to 16 KB addressing before anything is blitted.
    ``vdc.probe_ram_size_kib`` is not used: it wants the whole inverted byte to
    alias, so a lost bit on the line this tool is hunting reads as 64 KB.
    Writing past the end of a 16 KB VRAM wraps onto its start, and the second
    write over a byte would hide whatever the first one lost."""
    small = sm.vram_is_16k(port)
    if small is None:
        print("could not read the VRAM size through the porthole", flush=True)
        return None
    print(f"VDC video RAM: {16 if small else 64} KB", flush=True)
    if small and not sm.select_16k_addressing(port):
        print("could not read R28 to select 16 KB addressing", flush=True)
        return None
    if mixed:
        return MIXED_BYTES
    if small:
        print(f"    full frame capped at {SMALL_VRAM_BYTES} B, all of VRAM", flush=True)
        return SMALL_VRAM_BYTES
    return vdc.FRAME_BYTES


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
    if args.trials is not None and args.trials < 1:
        ap.error("--trials must be at least 1")

    trials = args.trials if args.trials is not None else (5 if args.mixed else 16)
    client = v.connect(tcp=args.tcp, serial=args.serial)
    offsets: list[int] = []
    try:
        if not v.stage_launch(client, args.reset_settle):
            return 1
        nbytes = soak_bytes(v.make_porthole(client), args.mixed)
        if nbytes is None:
            return 1
        for name, prev, nxt in cases(nbytes, args.mixed):
            run_case(client, name, prev, nxt, trials, offsets)
    except TimeoutError:
        print("HANG: the resident loop stopped answering", flush=True)
        return 1
    finally:
        if offsets:
            print(f"all offsets: {spans_text(offsets)}", flush=True)
        try:
            v.blank_screen(v.make_porthole(client))
        except Exception as e:  # noqa: BLE001  (best effort on the way out)
            print(f"blank failed: {e}")
        try:
            client.reset()
        except Exception as e:  # noqa: BLE001  (an exception already in flight outranks this one)
            print(f"reset failed: {e}; reset the C128 by hand", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
