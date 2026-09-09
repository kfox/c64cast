#!/usr/bin/env python3
"""Soak the TeensyROM+ DMA path against C128/C64 RAM and report which data
lines fail.

`tr_read_probe.py` answers "does WriteC64Mem/ReadC64Mem work at all" with one
round trip. This answers "how often does it get a byte wrong, in which
direction, and on which bit" — which is the question once the round trip mostly
works but not always. No VDC, no video, no capture card: the only thing under
test is the DMA path between the host and RAM, so a result here cannot be
blamed on anything downstream of the expansion port.

Stages
  1. quiesce the machine, so nothing but this tool writes RAM
  2. write/read soak - write a pattern, read it back several times
  3. read-only soak - one verified write, many reads
  4. address check - distinct markers at spread addresses, all read back at once

Stages 2 and 3 split the failures the way the hardware does:

  * a byte that reads back **the same wrong value every time** is wrong in RAM,
    so the write leg corrupted it
  * a byte that reads back **differently between passes** was fine in RAM, so
    the read leg corrupted it

Stage 4 catches an address line rather than a data line: a marker that turns up
under somebody else's address means the transfer landed in the wrong place, and
no amount of data-bus cleaning will fix it.

The summary maps each failing bit to its expansion-port pin (D7 is pin 14
through D0 at pin 21), because a contiguous run of failing pins is a connector
problem and a lone bit spread across the connector is not.

    scripts/diags/tr_dma_integrity.py                    # autodetect the TR+
    scripts/diags/tr_dma_integrity.py --serial /dev/cu.usbmodemXXXX
    scripts/diags/tr_dma_integrity.py --tcp HOST --rounds 20

Resets the machine on the way out unless --no-reset-exit.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import sys
import time
from typing import Final

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work from any cwd)

from c64cast.hw import vdc_rom
from c64cast.hw.teensyrom_dma import (
    DEFAULT_BAUD,
    DEFAULT_TCP_PORT,
    SerialTransport,
    TcpTransport,
    TRClient,
    TRError,
    autodetect_serial_port,
)

UPLOAD_PATH: Final = "/c64cast/vdcquiet.crt"

#: Scratch RAM. Above the resident loop and its mailbox, and clear of the
#: KERNAL's own pages in every bank configuration the quiesced loop leaves set.
BLOCK_ADDR: Final = 0x4000
BLOCK_BYTES: Final = 4096  # TRClient.MAX_SEGMENT_BYTES: one segment per leg

#: Addresses for the address-line check, spread so that a stuck or shorted
#: address bit moves a marker somewhere another marker can be seen missing from.
MARKER_ADDRS: Final = (0x4000, 0x4100, 0x4200, 0x4400, 0x4800, 0x5000, 0x6000, 0x7000)
MARKER_BYTES: Final = 16

#: D7 is expansion-port pin 14 and the bus runs down to D0 at pin 21.
PIN_OF_BIT: Final = {bit: 21 - bit for bit in range(8)}


def patterns(n: int) -> dict[str, bytes]:
    """The payloads. Each one puts a different demand on the bus: a constant
    asks for no transition at all, the alternating pair asks for one on every
    line every byte, and the walking patterns isolate a single line."""
    ramp = bytes(i & 0xFF for i in range(n))
    walk1 = bytes(1 << (i % 8) for i in range(n))
    walk0 = bytes((~(1 << (i % 8))) & 0xFF for i in range(n))
    return {
        "$00": b"\x00" * n,
        "$FF": b"\xff" * n,
        "$55": b"\x55" * n,
        "$AA": b"\xaa" * n,
        "ramp": ramp,
        "walk1": walk1,
        "walk0": walk0,
    }


class Tally:
    """Per-bit error counts, split by direction and by which leg was at fault."""

    def __init__(self) -> None:
        self.cleared: collections.Counter[int] = collections.Counter()  # 1 -> 0
        self.set: collections.Counter[int] = collections.Counter()  # 0 -> 1
        self.write_leg = 0
        self.read_leg = 0
        self.bytes_checked = 0
        self.bytes_wrong = 0
        self.xors: collections.Counter[int] = collections.Counter()

    def add(self, want: int, got: int) -> None:
        diff = want ^ got
        if not diff:
            return
        self.bytes_wrong += 1
        self.xors[diff] += 1
        for bit in range(8):
            if not diff & (1 << bit):
                continue
            if want & (1 << bit):
                self.cleared[bit] += 1
            else:
                self.set[bit] += 1


def connect(*, tcp: str | None, serial: str | None) -> TRClient:
    if tcp:
        tx = TcpTransport(tcp, DEFAULT_TCP_PORT)
    else:
        port = serial or autodetect_serial_port()
        if not port:
            raise SystemExit(
                "no TeensyROM+ serial port found. Pass --serial PORT (macOS: "
                "/dev/cu.usbmodem*, Linux: /dev/ttyACM*, Windows: COM3) or --tcp HOST."
            )
        print(f"    serial port: {port}")
        tx = SerialTransport(port, DEFAULT_BAUD)
    client = TRClient(tx)
    client.connect()
    return client


def quiesce(client: TRClient, settle: float) -> bool:
    """Park the machine in the cartridge's idle loop.

    Without this the test shares RAM with whatever the menu or BASIC is doing,
    and a byte that changed underneath the tool is indistinguishable from a byte
    the link got wrong."""
    crt = vdc_rom.build_crt()
    client.reset()
    time.sleep(settle)
    client._drain_stale(0.4)
    with contextlib.suppress(OSError, TRError):
        client.delete_file(UPLOAD_PATH)
    client.post_file(crt, UPLOAD_PATH)
    client.launch_file(UPLOAD_PATH)
    client.drain_after_command(0.6)
    time.sleep(settle)
    for _ in range(12):
        try:
            a = client.read_segment(vdc_rom.MAIL_BEAT, 1)
            time.sleep(0.2)
            b = client.read_segment(vdc_rom.MAIL_BEAT, 1)
        except (OSError, TRError):
            time.sleep(0.5)
            continue
        if a != b:
            print(f"    heartbeat moving: ${a[0]:02X} -> ${b[0]:02X}   ALIVE")
            return True
        time.sleep(0.4)
    return False


def stage_write_read(
    client: TRClient, rounds: int, reads: int, tally: Tally, chosen: tuple[str, ...]
) -> None:
    pool = {k: v for k, v in patterns(BLOCK_BYTES).items() if k in chosen}
    print(f"\n[2] write/read soak ({BLOCK_BYTES} B x {rounds} rounds x {len(pool)} patterns)")
    print("    per round: bytes wrong, and whether they were wrong in RAM or wrong on the way back")
    for rnd in range(1, rounds + 1):
        for name, want in pool.items():
            client.write_segment(BLOCK_ADDR, want)
            passes = [client.read_segment(BLOCK_ADDR, BLOCK_BYTES) for _ in range(reads)]
            tally.bytes_checked += BLOCK_BYTES
            wrong = 0
            for i in range(BLOCK_BYTES):
                seen = {p[i] for p in passes}
                if seen == {want[i]}:
                    continue
                wrong += 1
                if len(seen) == 1:
                    tally.write_leg += 1
                    tally.add(want[i], passes[0][i])
                else:
                    tally.read_leg += 1
                    for p in passes:
                        if p[i] != want[i]:
                            tally.add(want[i], p[i])
                            break
            flag = "clean" if not wrong else f"{wrong} wrong"
            print(f"    round {rnd:2d}  {name:>5}: {flag}")


def stage_read_only(client: TRClient, reads: int) -> None:
    """One write that verified, then nothing but reads. Anything that moves now
    moved on the read leg."""
    print(f"\n[3] read-only soak ({BLOCK_BYTES} B x {reads} reads, one write)")
    want = patterns(BLOCK_BYTES)["ramp"]
    client.write_segment(BLOCK_ADDR, want)
    first = client.read_segment(BLOCK_ADDR, BLOCK_BYTES)
    if first != want:
        print("    the setup write did not land clean; read-leg numbers below are not isolated")
    varied: set[int] = set()
    xors: collections.Counter[int] = collections.Counter()
    for _ in range(reads):
        got = client.read_segment(BLOCK_ADDR, BLOCK_BYTES)
        for i in range(BLOCK_BYTES):
            if got[i] != first[i]:
                varied.add(i)
                xors[got[i] ^ first[i]] += 1
    if not varied:
        print(f"    every one of {reads} reads agreed byte for byte")
        return
    top = " ".join(f"${v:02X}x{c}" for v, c in xors.most_common(4))
    print(f"    {len(varied)} of {BLOCK_BYTES} byte positions varied between reads   xor {top}")


def stage_addresses(client: TRClient) -> None:
    """Distinct markers at spread addresses, then one read of each. A marker
    found under the wrong address is an address line, not a data line."""
    print(f"\n[4] address check ({len(MARKER_ADDRS)} markers of {MARKER_BYTES} B)")
    markers = {a: bytes([(a >> 8) ^ i for i in range(MARKER_BYTES)]) for a in MARKER_ADDRS}
    for addr, blob in markers.items():
        client.write_segment(addr, blob)
    misplaced = 0
    for addr, blob in markers.items():
        got = client.read_segment(addr, MARKER_BYTES)
        if got == blob:
            continue
        misplaced += 1
        owner = next((a for a, b in markers.items() if b == got), None)
        if owner is not None:
            print(f"    ${addr:04X}: holds the marker written to ${owner:04X}")
        else:
            print(f"    ${addr:04X}: {blob.hex()} -> {got.hex()}")
    if not misplaced:
        print("    every marker read back from its own address")


def summarize(tally: Tally) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    rate = 100.0 * tally.bytes_wrong / tally.bytes_checked if tally.bytes_checked else 0.0
    print(f"  bytes checked            {tally.bytes_checked}")
    print(f"  bytes wrong              {tally.bytes_wrong}  ({rate:.4f}%)")
    print(f"  wrong in RAM (write leg) {tally.write_leg}")
    print(f"  wrong on read back       {tally.read_leg}")
    if not tally.bytes_wrong:
        print("  no data-line errors to attribute")
        print("=" * 72)
        return
    top = " ".join(f"${v:02X}x{c}" for v, c in tally.xors.most_common(6))
    print(f"  error xors               {top}")
    print("\n  bit  line  pin   1->0    0->1")
    for bit in range(7, -1, -1):
        lo, hi = tally.cleared[bit], tally.set[bit]
        if not lo and not hi:
            continue
        print(f"   {bit}    D{bit}    {PIN_OF_BIT[bit]:2d}  {lo:6d}  {hi:6d}")
    pins = sorted(PIN_OF_BIT[b] for b in range(8) if tally.cleared[b] or tally.set[b])
    span = "contiguous" if pins == list(range(pins[0], pins[-1] + 1)) else "scattered"
    print(f"\n  failing pins             {pins}  ({span})")
    print("=" * 72)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tcp", metavar="HOST", help="TeensyROM+ over TCP")
    ap.add_argument("--serial", metavar="PORT", help="TeensyROM+ over serial (default: autodetect)")
    ap.add_argument("--rounds", type=int, default=5, help="write/read rounds per pattern")
    ap.add_argument(
        "--patterns",
        default=",".join(patterns(1)),
        help="comma-separated subset to soak, for narrowing a run onto the payload that fails",
    )
    ap.add_argument("--reads", type=int, default=3, help="readbacks per write in stage 2")
    ap.add_argument("--read-soak", type=int, default=20, help="readbacks in stage 3")
    ap.add_argument("--reset-settle", type=float, default=3.0)
    ap.add_argument("--no-quiesce", action="store_true", help="skip the cartridge, test as found")
    ap.add_argument("--no-reset-exit", action="store_true")
    args = ap.parse_args()

    chosen = tuple(x.strip() for x in args.patterns.split(","))
    unknown = [x for x in chosen if x not in patterns(1)]
    if unknown:
        ap.error(f"unknown pattern(s) {unknown}; choose from {list(patterns(1))}")

    print("[1] connect + quiesce")
    client = connect(tcp=args.tcp, serial=args.serial)
    tally = Tally()
    try:
        if args.no_quiesce:
            print("    --no-quiesce: the machine keeps running whatever it is running")
        elif not quiesce(client, args.reset_settle):
            print("    heartbeat NOT moving - the cartridge did not boot.")
            print("    Re-run with --no-quiesce to soak the link anyway; a link this")
            print("    unreliable may not be able to upload a cartridge intact.")
            return 1
        stage_write_read(client, args.rounds, args.reads, tally, chosen)
        stage_read_only(client, args.read_soak)
        stage_addresses(client)
        summarize(tally)
    except (OSError, TRError, TimeoutError) as e:
        print(f"\nABORTED: {e}")
        summarize(tally)
        return 1
    finally:
        if not args.no_reset_exit:
            print("\nresetting ...")
            with contextlib.suppress(OSError, TRError):
                client.reset()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
