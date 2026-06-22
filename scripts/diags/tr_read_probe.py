#!/usr/bin/env python3
"""Validate the TeensyROM+ ReadC64Mem path (token 0x64FD) end-to-end on hardware.

ReadC64Mem shipped in the cycle-clean TR+ firmware (v0.7.2.5); it's the read
half of the protocol the Ultimate has always had over REST, and it's what
unlocks `read_memory` -> the $028D keyboard poller (physical pause/skip/cycle/
menu control) on the TR backend. This tool confirms a connected TR actually
answers it, over whichever transport(s) you point it at, with no Cam Link
needed — the read IS the readback.

Stages per transport (--tcp HOST and/or --serial PORT):

  1. Connect + FWCheck + Ping (liveness).
  2. ROM read: ReadC64Mem $FFFC, 2 bytes. A stock machine returns E2 FC
     (the $FCE2 KERNAL reset vector) — proves the read lands and the wire
     byte-order (BE addr/len out, LE ack in, raw data after) is right. The
     value is reported, not asserted (a cartridge may map $FFFC differently);
     the round-trip *succeeding* is the pass signal.
  3. RAM round-trip: WriteC64Mem a known pattern to $4000, ReadC64Mem it back,
     and compare byte-for-byte. This is the real proof — writes and reads agree
     through the same DMA path.
  4. Live $028D: read the kernal keyboard-modifier byte a few times so you can
     eyeball it changing while you hold C= / CTRL / SHIFT on the C64 (bit0
     SHIFT, bit1 COMMODORE, bit2 CTRL). Only meaningful once an IRQ-enabled
     idle (c64cast's clear-loop bring-up) is running so the kernal keyboard
     scan updates $028D — at the bare TR menu it may read 0.

    scripts/diags/tr_read_probe.py --tcp 192.168.2.164
    scripts/diags/tr_read_probe.py --serial /dev/cu.usbmodem193075601
    scripts/diags/tr_read_probe.py --tcp 192.168.2.164 --serial <PORT> --watch 028d

Resets the C64 on the way out (the standing silence-and-reset rule) unless
--no-reset-exit; reset boots the TR to its menu.
"""

from __future__ import annotations

import argparse
import contextlib
import time

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work)

from c64cast.teensyrom_dma import (
    DEFAULT_BAUD,
    DEFAULT_TCP_PORT,
    SerialTransport,
    TcpTransport,
    TRClient,
    TRError,
)

RAM_ADDR = 0x4000  # plain RAM, clear of BASIC / screen / our helpers
ROM_ADDR = 0xFFFC  # KERNAL reset vector -> E2 FC on a stock machine
MOD_ADDR = 0x028D  # kernal keyboard-modifier scratch byte


def connect(kind: str, *, tcp_host: str | None, serial_port: str | None) -> TRClient:
    if kind == "tcp":
        tx = TcpTransport(tcp_host, DEFAULT_TCP_PORT)  # type: ignore[arg-type]
    else:
        tx = SerialTransport(serial_port, DEFAULT_BAUD)  # type: ignore[arg-type]
    client = TRClient(tx)
    client.connect()  # raises TRError on a bad link
    return client


def run_transport(label: str, client: TRClient, args) -> dict:
    print(f"\n=== Transport: {label} ===")
    print(f"  connected via {client.transport.description}; firmware: {client.firmware}")
    ping = client.ping()
    if ping:
        print(f"  ping: {ping!r}")
    # Drain any post-connect/menu chatter before the read hot path (which, like
    # the write hot path, deliberately doesn't drain).
    client._drain_stale(0.3)

    result: dict = {"transport": label, "rom_ok": False, "ram_ok": False, "error": None}

    # ---- (2) ROM read ----
    try:
        rom = client.read_segment(ROM_ADDR, 2)
        result["rom_bytes"] = rom.hex()
        result["rom_ok"] = len(rom) == 2
        note = "  (= $FCE2 stock KERNAL reset vector)" if rom == b"\xe2\xfc" else ""
        print(f"  [rom]  ReadC64Mem ${ROM_ADDR:04X} -> {rom.hex(' ')}{note}")
    except (OSError, TRError) as e:
        result["error"] = f"rom read: {e}"
        print(f"  [rom]  FAILED: {e}")
        return result

    # ---- (3) RAM write/read round-trip ----
    pattern = bytes((0xA5 ^ i) & 0xFF for i in range(16))
    try:
        client.write_segment(RAM_ADDR, pattern)
        back = client.read_segment(RAM_ADDR, len(pattern))
        result["ram_ok"] = back == pattern
        if result["ram_ok"]:
            print(f"  [ram]  wrote+read {len(pattern)} B @ ${RAM_ADDR:04X}: MATCH ✅")
        else:
            print(f"  [ram]  MISMATCH ❌ wrote {pattern.hex(' ')} got {back.hex(' ')}")
    except (OSError, TRError) as e:
        result["error"] = f"ram round-trip: {e}"
        print(f"  [ram]  FAILED: {e}")
        return result

    # ---- (4) live $028D watch ----
    if args.watch:
        print(
            f"  [$028D] reading {args.watch_count}x @ {args.watch_interval}s "
            "(hold C=/CTRL/SHIFT on the C64; bit0 SHIFT, bit1 C=, bit2 CTRL):"
        )
        for _ in range(args.watch_count):
            try:
                b = client.read_segment(MOD_ADDR, 1)
                v = b[0]
                flags = (
                    "".join(
                        name
                        for bit, name in ((0, "SHIFT"), (1, "C="), (2, "CTRL"))
                        if v & (1 << bit)
                    )
                    or "-"
                )
                print(f"        $028D = {v:#04x}  [{flags}]")
            except (OSError, TRError) as e:
                print(f"        read failed: {e}")
            time.sleep(args.watch_interval)

    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tcp", metavar="HOST", default=None, help="probe over TCP to this TR host")
    ap.add_argument("--serial", metavar="PORT", default=None, help="probe over this serial device")
    ap.add_argument("--watch", action="store_true", help="poll $028D live (needs an IRQ idle)")
    ap.add_argument("--watch-count", type=int, default=20, help="how many $028D reads (default 20)")
    ap.add_argument(
        "--watch-interval",
        type=float,
        default=0.5,
        help="seconds between $028D reads (default 0.5)",
    )
    ap.add_argument("--no-reset-exit", action="store_true", help="skip the final reset")
    args = ap.parse_args()
    if not args.tcp and not args.serial:
        ap.error("specify at least one of --tcp HOST / --serial PORT")

    results: list[dict] = []
    clients: list[TRClient] = []
    try:
        for label, kind, val in (("tcp", "tcp", args.tcp), ("serial", "serial", args.serial)):
            if not val:
                continue
            try:
                client = connect(kind, tcp_host=args.tcp, serial_port=args.serial)
            except TRError as e:
                print(f"\n=== Transport: {label} ===\n  CONNECT FAILED: {e}")
                continue
            clients.append(client)
            try:
                results.append(run_transport(label, client, args))
            except (OSError, TRError) as e:
                print(f"  {label}: ABORTED after transport error: {e}")
                results.append(
                    {"transport": label, "rom_ok": False, "ram_ok": False, "error": str(e)}
                )
    finally:
        for c in clients:
            if not args.no_reset_exit:
                with contextlib.suppress(OSError, TRError):
                    c.reset()
            c.close()

    print("\n===================== SUMMARY =====================")
    if not results:
        print("nothing ran.")
        return 2
    for r in results:
        verdict = "PASS ✅" if (r["rom_ok"] and r["ram_ok"]) else "FAIL ❌"
        extra = f"  ({r['error']})" if r.get("error") else ""
        print(
            f"  {r['transport']:>7}: rom={r['rom_ok']} ram_roundtrip={r['ram_ok']}  {verdict}{extra}"
        )
    return 0 if all(r["rom_ok"] and r["ram_ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
