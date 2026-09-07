#!/usr/bin/env python3
"""Probe a Commodore 128's VDC (8563/8568) through a TeensyROM+ — presence,
chip version, VRAM size, and the porthole/block transfer rates that decide
whether a VDC display target in c64cast is viable.

**No capture card needed.** The TR's ``ReadC64Mem`` token can read the VDC
back through its own ``$D600``/``$D601`` porthole, so every stage here
self-verifies: it writes a pattern to VDC RAM and reads it back to confirm.
The optional ``--pattern`` stage puts something on the RGBI monitor for a
human to eyeball as a secondary check.

Runs in the C128's **C64 mode** (a TeensyROM boots the C128 that way): the VDC
is alive in C64 mode and responds at ``$D600``. No C128-mode CRT is needed for
this — that comes later, for the actual blit path.

    scripts/diags/vdc_probe.py --serial /dev/cu.usbmodemXXXX
    scripts/diags/vdc_probe.py --tcp 192.168.2.66
    scripts/diags/vdc_probe.py --serial <PORT> --pattern bars
    scripts/diags/vdc_probe.py --serial <PORT> --pattern bitmap --image pic.jpg

Stages
  1. presence / version / VRAM size (all non-destructive)
  2. host -> VRAM poke rate (one WriteC64Mem per byte — the slow path)
  3. VDC hardware block-fill rate (the near-free primitive)
  4. VDC hardware block-copy rate (64 KiB parts only)
  5. optional visible pattern: bars (fast, via block-fill) | bitmap (full
     24 KB upload from --image) | none

Leaves the VDC blanked and resets the C128 on the way out (the standing
silence-and-reset rule), unless --keep / --no-reset-exit.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work from any cwd)

from c64cast.hw import vdc
from c64cast.hw.teensyrom_dma import (
    DEFAULT_BAUD,
    DEFAULT_TCP_PORT,
    SerialTransport,
    TcpTransport,
    TRClient,
    TRError,
)


def connect(*, tcp: str | None, serial: str | None) -> TRClient:
    if tcp:
        tx = TcpTransport(tcp, DEFAULT_TCP_PORT)
    elif serial:
        tx = SerialTransport(serial, DEFAULT_BAUD)
    else:  # pragma: no cover - argparse guards this
        raise SystemExit("specify --tcp HOST or --serial PORT")
    client = TRClient(tx)
    client.connect()
    return client


def make_porthole(client: TRClient) -> vdc.VdcPorthole:
    """Wrap the TR's WriteC64Mem / ReadC64Mem in a VdcPorthole. Reads return
    ``None`` on a transport error (the porthole treats that as 'couldn't tell')
    rather than crashing the probe mid-stage."""

    def write(addr: int, data: bytes) -> None:
        client.write_segment(addr, data)

    def read(addr: int, n: int) -> bytes | None:
        try:
            return client.read_segment(addr, n)
        except (OSError, TRError):
            return None

    return vdc.VdcPorthole(write, read)


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def stage_identify(port: vdc.VdcPorthole) -> dict:
    print("\n[1] presence / version / VRAM size")
    present = vdc.probe_present(port)
    print(f"    VDC present at $D600:  {'yes' if present else 'NO'}")
    if not present:
        print("    -> $D600 is not answering like a VDC. Either this isn't a C128,")
        print("       or WriteC64Mem/ReadC64Mem can't reach $D600 on this firmware.")
        return {"present": False}
    version = vdc.probe_version(port)
    size = vdc.probe_ram_size_kib(port)
    print(f"    chip version:          {version}")
    print(f"    VRAM size:             {size} KiB" if size else "    VRAM size:  unknown")
    if size == 16:
        print("    -> 16 KiB: 640x200 mono only; no 8x2-colour bitmap, no page-flip buffer.")
    return {"present": True, "version": version, "ram_kib": size}


def _time(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def stage_poke_rate(port: vdc.VdcPorthole, nbytes: int) -> None:
    print(f"\n[2] host -> VRAM poke rate ({nbytes} bytes, one WriteC64Mem each)")
    payload = bytes((i * 37) & 0xFF for i in range(nbytes))
    dt = _time(lambda: port.write_ram(0x0400, payload))
    rate = nbytes / dt
    back = port.read_ram(0x0400, min(nbytes, 64))
    ok = back == payload[: len(back or b"")]
    print(
        f"    {rate:8.0f} B/s   ({dt:.2f}s for {nbytes} B)   readback {'OK' if ok else 'MISMATCH'}"
    )
    print(f"    -> full 24 KB bitmap frame ~= {vdc.FRAME_BYTES / rate:5.1f} s")
    print(f"    -> 4 KB text frame          ~= {4000 / rate:5.1f} s")


def stage_block_fill(port: vdc.VdcPorthole, nbytes: int) -> None:
    print(f"\n[3] VDC hardware block-fill rate ({nbytes} bytes)")
    dt = _time(lambda: port.block_fill(0x0000, 0x5A, nbytes))
    spots = [0, nbytes // 3, nbytes - 1]
    got = [port.read_ram(0x0000 + s, 1) for s in spots]
    ok = all(g == b"\x5a" for g in got)
    print(
        f"    {nbytes / dt:10.0f} B/s   ({dt * 1000:.1f} ms)   readback {'OK' if ok else 'MISMATCH'}"
    )


def stage_block_copy(port: vdc.VdcPorthole, nbytes: int, ram_kib: int | None) -> None:
    print(f"\n[4] VDC hardware block-copy rate ({nbytes} bytes)")
    if ram_kib != 64:
        print("    skipped — needs a 64 KiB VDC (no room for a second buffer).")
        return
    src, dst = 0x0000, 0x8000
    port.write_ram(src, bytes(range(64)))
    dt = _time(lambda: port.block_copy(src, dst, nbytes))
    ok = port.read_ram(dst, 64) == bytes(range(64))
    print(
        f"    {nbytes / dt:10.0f} B/s   ({dt * 1000:.1f} ms)   readback {'OK' if ok else 'MISMATCH'}"
    )


def _enter_bitmap_mode(port: vdc.VdcPorthole) -> None:
    # Absolute values, not a read-modify-write: in C64 mode the VDC's registers
    # are unprogrammed and read back $FF, so OR-ing onto them sets every bit.
    port.write_regs(vdc.BITMAP_640x200_REGS)
    port.block_fill(vdc.BITMAP_BASE, 0x00, vdc.BITMAP_BYTES)  # clear pixels -> all bg


def pattern_bars(port: vdc.VdcPorthole) -> None:
    print("\n[5] pattern: 16 horizontal colour bars (bitmap mode, via block-fill)")
    _enter_bitmap_mode(port)
    for row in range(vdc.ATTR_ROWS):
        color = (row * 16) // vdc.ATTR_ROWS
        port.block_fill(vdc.ATTR_BASE + row * vdc.ATTR_COLS, color << 4, vdc.ATTR_COLS)
    print("    -> the RGBI monitor should show 16 stacked colour bands, black at top.")


def pattern_bitmap(port: vdc.VdcPorthole, image_path: str) -> None:
    import cv2

    print(f"\n[5] pattern: {image_path} -> 640x200 8x2-colour bitmap (full upload)")
    img = cv2.imread(image_path)
    if img is None:
        raise SystemExit(f"could not read image {image_path!r}")
    img = cv2.resize(img, (vdc.BITMAP_W, vdc.BITMAP_H), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    idx = vdc.quantize_to_vdc(rgb)
    bitmap, attr = vdc.pack_bitmap_frame(idx)
    _enter_bitmap_mode(port)
    dt = _time(
        lambda: (port.write_ram(vdc.BITMAP_BASE, bitmap), port.write_ram(vdc.ATTR_BASE, attr))
    )
    print(f"    uploaded {vdc.FRAME_BYTES} B in {dt:.1f}s  ({vdc.FRAME_BYTES / dt:.0f} B/s)")
    print("    -> compare the RGBI monitor to scripts/diags/vdc_preview.py output for this image.")


def blank_vdc(port: vdc.VdcPorthole) -> None:
    with contextlib.suppress(OSError, TRError):
        port.block_fill(vdc.BITMAP_BASE, 0x00, vdc.BITMAP_BYTES)
        port.block_fill(vdc.ATTR_BASE, 0x00, vdc.ATTR_BYTES)


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tcp", metavar="HOST", help="TR over TCP")
    ap.add_argument("--serial", metavar="PORT", help="TR over serial")
    ap.add_argument(
        "--pattern",
        choices=("none", "bars", "bitmap"),
        default="bars",
        help="visible test (default bars)",
    )
    ap.add_argument("--image", help="image file for --pattern bitmap")
    ap.add_argument("--rate-bytes", type=int, default=2000, help="bytes for the poke-rate test")
    ap.add_argument(
        "--hold",
        type=float,
        default=0.0,
        help="seconds to leave the pattern up before blanking (default: prompt on a "
        "tty, 5s otherwise — set this when driving the probe from a script)",
    )
    ap.add_argument("--reset-settle", type=float, default=3.0)
    ap.add_argument("--keep", action="store_true", help="leave the pattern on VRAM (don't blank)")
    ap.add_argument("--no-reset-exit", action="store_true", help="don't reset the C128 on exit")
    args = ap.parse_args()
    if not args.tcp and not args.serial:
        ap.error("specify --tcp HOST or --serial PORT")
    if args.pattern == "bitmap" and not args.image:
        ap.error("--pattern bitmap needs --image PATH")

    try:
        client = connect(tcp=args.tcp, serial=args.serial)
    except TRError as e:
        print(f"connect failed: {e}")
        return 2
    print(f"connected via {client.transport.description}; firmware: {client.firmware}")

    port = make_porthole(client)
    try:
        print("resetting C128 to a known state (-> TR menu, C64 mode) ...")
        client.reset()
        time.sleep(args.reset_settle)
        client._drain_stale(0.4)

        ident = stage_identify(port)
        if not ident.get("present"):
            return 1

        stage_poke_rate(port, args.rate_bytes)
        stage_block_fill(port, vdc.BITMAP_BYTES)
        stage_block_copy(port, vdc.BITMAP_BYTES, ident.get("ram_kib"))

        if args.pattern == "bars":
            pattern_bars(port)
        elif args.pattern == "bitmap":
            pattern_bitmap(port, args.image)

        if args.pattern != "none":
            if args.hold:
                print(f"\nlook at the RGBI monitor — holding {args.hold:.0f}s ...")
                for left in range(int(args.hold), 0, -15):
                    print(f"    {left}s ...", flush=True)
                    time.sleep(min(15, left))
            elif sys.stdin.isatty():
                input("\nlook at the RGBI monitor. press Enter to blank + reset ... ")
            else:
                time.sleep(5)
    except (OSError, TRError) as e:
        print(f"\nABORTED: {e}")
        return 1
    finally:
        if not args.keep:
            blank_vdc(port)
        if not args.no_reset_exit:
            with contextlib.suppress(OSError, TRError):
                client.reset()
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
