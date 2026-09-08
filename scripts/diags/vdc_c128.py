#!/usr/bin/env python3
"""Launch the C128-mode VDC cartridge on real hardware and measure the resident
blit path — the thing that makes a VDC display target viable.

Everything up to now drove the VDC from the host, one porthole access per byte,
at 5.5 KB/s. This launches ``hw/vdc_rom.py``'s cartridge instead: the C128 boots
into **native 128 mode**, the resident 8502 loop takes over, and the host's job
becomes a bulk DMA into C128 RAM plus a one-byte command. The 8502 does the
porthole work locally.

**No capture card needed for the pass/fail parts.** The ROM increments a
heartbeat byte in the mailbox on every idle pass, so a pair of reads proves the
cartridge booted and the loop is running. Stages 1-5 are all self-verifying that
way; only ``--pattern`` needs a human looking at the RGBI monitor.

    scripts/diags/vdc_c128.py --serial /dev/cu.usbmodemXXXX
    scripts/diags/vdc_c128.py --serial <PORT> --pattern image --image pic.jpg
    scripts/diags/vdc_c128.py --serial <PORT> --pattern animate --hold 30

Stages
  1. build + upload + launch the .crt, confirm the heartbeat is moving
  2. read back the boot state (VDC registers, cleared VRAM) through the porthole
  3. host -> C128 RAM DMA rate (the TeensyROM+ link's bulk path)
  4. RAM -> VRAM blit rate (the resident loop's inner loop)
  5. end-to-end frame time and the frame rate that implies
  6. optional visible pattern: image | animate | none

Leaves the C128 reset on the way out (the standing silence-and-reset rule),
unless --no-reset-exit.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from typing import Final

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work from any cwd)

from c64cast.hw import vdc, vdc_rom
from c64cast.hw.teensyrom_dma import (
    DEFAULT_BAUD,
    DEFAULT_TCP_PORT,
    SerialTransport,
    TcpTransport,
    TRClient,
    TRError,
)

UPLOAD_PATH = "/c64cast/vdcrom.crt"


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
    """The host's own porthole, for reading VDC state back while the resident
    loop is idle. Safe only because the idle loop touches nothing but RAM."""

    def write(addr: int, data: bytes) -> None:
        client.write_segment(addr, data)

    def read(addr: int, n: int) -> bytes | None:
        try:
            return client.read_segment(addr, n)
        except (OSError, TRError):
            return None

    return vdc.VdcPorthole(write, read)


# ---------------------------------------------------------------------------
# the mailbox protocol
# ---------------------------------------------------------------------------


def issue(
    client: TRClient, cmd: int, *, arg: int = 0, dst: int = 0, count: int = 0, src: int = 0
) -> int:
    """Stage the parameters, then commit with the command byte in its own
    write. Returns the MAIL_DONE value from before the command, for wait_done.

    The two writes are not an accident: the command byte is the commit, and
    bundling it with the parameters would let the loop see a command whose
    parameters had only partly landed."""
    before = client.read_segment(vdc_rom.MAIL_DONE, 1)[0]
    client.write_segment(
        vdc_rom.MAIL_ARG,
        bytes([arg, dst & 0xFF, dst >> 8, count & 0xFF, count >> 8, src & 0xFF, src >> 8]),
    )
    client.write_segment(vdc_rom.MAIL_CMD, bytes([cmd]))
    return before


#: Gap between MAIL_DONE polls while a command runs. **Not a politeness knob.**
#: Every DMA read halts the 8502, so polling while a command runs steals cycles
#: from the command being waited on. A no-sleep poll is fatal: a 24000-byte blit
#: that finishes in ~0.9 s under a sleeping poll does not finish in 15 s under a
#: tight one. 5 ms is still inside the damage, just less obviously — an A/B of
#: eight 4096-byte blits each hung twice at 5 ms against once at 50 ms, and left
#: two of three completions corrupt against four of seven clean. The blit itself
#: takes the same ~230 ms either way, so the only thing a shorter gap buys is
#: timing resolution, and 50 ms against 230 is resolution enough.
POLL_INTERVAL: Final = 0.050


def wait_done(client: TRClient, before: int, timeout: float = 20.0) -> float:
    """Seconds until the resident loop acknowledged, +/- POLL_INTERVAL."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if client.read_segment(vdc_rom.MAIL_DONE, 1)[0] != before:
            return time.perf_counter() - t0
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"resident loop never acknowledged command (>{timeout}s)")


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def stage_launch(client: TRClient, settle: float) -> bool:
    print("\n[1] build + upload + launch the C128-mode cartridge")
    crt = vdc_rom.build_crt()
    used = len(vdc_rom.assemble(vdc_rom.resident_source(), vdc_rom.RESIDENT_ADDR))
    print(f"    .crt {len(crt)} B   resident loop {used} B of {vdc_rom.RESIDENT_MAX}")

    print("    resetting to the TR menu ...")
    client.reset()
    time.sleep(settle)
    client._drain_stale(0.4)

    with contextlib.suppress(OSError, TRError):
        client.delete_file(UPLOAD_PATH)
    client.post_file(crt, UPLOAD_PATH)
    client.launch_file(UPLOAD_PATH)
    # LaunchFile acks and then streams console text; draining to silence is the
    # only safe way to know the link is ours again.
    client.drain_after_command(0.6)
    time.sleep(settle)

    print("    checking the heartbeat (proof the loop is running) ...")
    for attempt in range(12):
        try:
            a = client.read_segment(vdc_rom.MAIL_BEAT, 1)
            time.sleep(0.2)
            b = client.read_segment(vdc_rom.MAIL_BEAT, 1)
        except (OSError, TRError) as e:
            print(f"      attempt {attempt + 1}: link not answering yet ({e})")
            time.sleep(0.5)
            continue
        if a != b:
            print(f"    heartbeat moving: ${a[0]:02X} -> ${b[0]:02X}   ALIVE")
            print("    -> the 80-col screen should now be SOLID CYAN.")
            return True
        time.sleep(0.4)
    print("    heartbeat NOT moving. The cartridge did not boot, or DMA cannot")
    print("    reach C128 RAM while a C128-mode cart is running.")
    return False


def stage_boot_state(port: vdc.VdcPorthole) -> None:
    print("\n[2] boot state, read back through the porthole")
    # R28's unused low bits read back as 1s on an 8563 R8/R9, so a written $18
    # reads as $3F. Compare only the bit that carries meaning here: bit 4, the
    # 64 KiB address select. The charset-base bits above it are don't-care in
    # bitmap mode.
    checks = [
        (vdc.R.V_DISPLAYED, "R6  rows displayed", 0xFF),
        (vdc.R.H_SCROLL_CTRL, "R25 bitmap/attr/hscroll", 0xFF),
        (vdc.R.CHARSET_ADDR, "R28 64K address select", 0x10),
    ]
    for reg, label, mask in checks:
        want = vdc.BITMAP_640x200_REGS[reg] & mask
        got = port.read_reg(reg)
        ok = got is not None and (got & mask) == want
        state = "OK" if ok else f"MISMATCH (wanted ${want:02X} under ${mask:02X})"
        got_text = "unreadable" if got is None else f"${got:02X}"
        print(f"    {label:26s} {got_text:>10s}  {state}")

    bitmap = port.read_ram(vdc.BITMAP_BASE, 64)
    attr = port.read_ram(vdc.ATTR_BASE, 64)
    print(f"    bitmap cleared to zero     {'OK' if bitmap == bytes(64) else 'NO'}")
    ok_attr = attr == bytes([vdc_rom.ATTR_INIT]) * 64
    print(f"    attributes flat-filled     {'OK' if ok_attr else 'NO'}")


def stage_dma_rate(client: TRClient, nbytes: int) -> bytes:
    print(f"\n[3] host -> C128 RAM DMA rate ({nbytes} B)")
    payload = bytes((i * 37) & 0xFF for i in range(nbytes))
    t0 = time.perf_counter()
    client.write_segment(vdc_rom.FRAMEBUF_ADDR, payload)
    # A read cannot be answered until everything queued ahead of it has been
    # processed, so it is what turns a buffered send into a measured transfer.
    back = client.read_segment(vdc_rom.FRAMEBUF_ADDR, 64)
    dt = time.perf_counter() - t0
    ok = back == payload[:64]
    print(f"    {nbytes / dt:9.0f} B/s   ({dt:.3f}s)   readback {'OK' if ok else 'MISMATCH'}")
    return payload


def stage_blit_rate(client: TRClient, port: vdc.VdcPorthole, payload: bytes) -> float:
    nbytes = len(payload)
    print(f"\n[4] RAM -> VRAM blit rate ({nbytes} B, resident 8502 loop)")
    before = issue(
        client, vdc_rom.CMD_BLIT, dst=vdc.BITMAP_BASE, count=nbytes, src=vdc_rom.FRAMEBUF_ADDR
    )
    dt = wait_done(client, before)
    print(f"    {nbytes / dt:9.0f} B/s   ({dt * 1000:.0f} ms +/- {POLL_INTERVAL * 1000:.0f} ms)")
    print(f"    -> versus 5500 B/s host-driven: {(nbytes / dt) / 5500:.0f}x")

    # Timing a blit without reading it back would call a corrupt one fast: the
    # VDC loses roughly one bit per thousand bytes written, always 1 -> 0, at
    # offsets that move from run to run. The whole region has to be compared,
    # because a lost porthole write shifts everything after it.
    got = port.read_ram(vdc.BITMAP_BASE, nbytes)
    if got is None:
        print("    verify: VRAM UNREADABLE")
        return dt
    bad = [i for i in range(nbytes) if got[i] != payload[i]]
    if not bad:
        print("    verify: every byte landed")
    else:
        bits = sum(bin(got[i] ^ payload[i]).count("1") for i in bad)
        print(f"    verify: {len(bad)} of {nbytes} bytes wrong ({bits} bits), first at {bad[0]}")
    return dt


def stage_end_to_end(client: TRClient) -> None:
    print(f"\n[5] end-to-end frame ({vdc.FRAME_BYTES} B: DMA + blit)")
    bitmap = bytes((i * 11) & 0xFF for i in range(vdc.BITMAP_BYTES))
    attr = bytes([0x60]) * vdc.ATTR_BYTES

    t0 = time.perf_counter()
    client.write_segment(vdc_rom.FRAMEBUF_ADDR, bitmap + attr)
    before = issue(
        client,
        vdc_rom.CMD_BLIT,
        dst=vdc.BITMAP_BASE,
        count=vdc.FRAME_BYTES,
        src=vdc_rom.FRAMEBUF_ADDR,
    )
    wait_done(client, before, timeout=30.0)
    dt = time.perf_counter() - t0

    print(f"    {dt:.2f}s per frame  ->  {1 / dt:.2f} fps")
    print(
        f"    (attribute plane only, {vdc.ATTR_BYTES} B: "
        f"~{1 / (dt * vdc.ATTR_BYTES / vdc.FRAME_BYTES):.1f} fps)"
    )


def pattern_image(client: TRClient, image_path: str) -> None:
    import cv2

    print(f"\n[6] pattern: {image_path} -> 640x200 8x2-color bitmap")
    img = cv2.imread(image_path)
    if img is None:
        raise SystemExit(f"could not read image {image_path!r}")
    img = cv2.resize(img, (vdc.BITMAP_W, vdc.BITMAP_H), interpolation=cv2.INTER_AREA)
    idx = vdc.quantize_to_vdc(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    bitmap, attr = vdc.pack_bitmap_frame(idx)

    client.write_segment(vdc_rom.FRAMEBUF_ADDR, bitmap + attr)
    before = issue(
        client,
        vdc_rom.CMD_BLIT,
        dst=vdc.BITMAP_BASE,
        count=vdc.FRAME_BYTES,
        src=vdc_rom.FRAMEBUF_ADDR,
    )
    wait_done(client, before, timeout=30.0)
    print("    -> the 80-col screen should show the picture.")
    print("       Compare against scripts/diags/vdc_preview.py for this image.")


def pattern_animate(client: TRClient, seconds: float) -> None:
    """A moving bar, blitting only the attribute plane. Deliberately the cheap
    plane: it is the honest demonstration of what this path can sustain."""
    print(f"\n[6] pattern: scrolling attribute bar for {seconds:.0f}s")
    print("    -> a horizontal color band should sweep DOWN the 80-col screen,")
    print("       smoothly and without tearing or speckle.")
    frames = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        row = frames % vdc.ATTR_ROWS
        plane = bytearray([0x00]) * vdc.ATTR_BYTES
        for r in range(row, min(row + 8, vdc.ATTR_ROWS)):
            plane[r * vdc.ATTR_COLS : (r + 1) * vdc.ATTR_COLS] = (
                bytes([((r % 15) + 1) << 4]) * vdc.ATTR_COLS
            )
        client.write_segment(vdc_rom.FRAMEBUF_ADDR, bytes(plane))
        before = issue(
            client,
            vdc_rom.CMD_BLIT,
            dst=vdc.ATTR_BASE,
            count=vdc.ATTR_BYTES,
            src=vdc_rom.FRAMEBUF_ADDR,
        )
        wait_done(client, before)
        frames += 1
    dt = time.perf_counter() - t0
    print(f"    {frames} frames in {dt:.1f}s  ->  {frames / dt:.1f} fps sustained")


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tcp", metavar="HOST", help="TR over TCP")
    ap.add_argument("--serial", metavar="PORT", help="TR over serial")
    ap.add_argument("--pattern", choices=("none", "image", "animate"), default="none")
    ap.add_argument("--image", help="source image for --pattern image")
    ap.add_argument(
        "--hold",
        type=float,
        default=20.0,
        help="seconds to run --pattern animate / hold a still image",
    )
    ap.add_argument(
        "--rate-bytes",
        type=int,
        default=vdc.FRAME_BYTES,
        help="payload size for the DMA and blit rate stages",
    )
    ap.add_argument("--reset-settle", type=float, default=3.0)
    ap.add_argument(
        "--no-reset-exit",
        action="store_true",
        help="leave the cartridge running (it will not stop on its own)",
    )
    args = ap.parse_args()

    if args.pattern == "image" and not args.image:
        ap.error("--pattern image needs --image PATH")

    client = connect(tcp=args.tcp, serial=args.serial)
    try:
        if not stage_launch(client, args.reset_settle):
            return 1
        port = make_porthole(client)
        stage_boot_state(port)

        payload = stage_dma_rate(client, args.rate_bytes)
        stage_blit_rate(client, port, payload)
        stage_end_to_end(client)

        if args.pattern == "image":
            pattern_image(client, args.image)
            print(f"\nlook at the RGBI monitor — holding {args.hold:.0f}s ...")
            time.sleep(args.hold)
        elif args.pattern == "animate":
            pattern_animate(client, args.hold)
    except (OSError, TRError, TimeoutError) as e:
        print(f"\nABORTED: {e}")
        return 1
    finally:
        if not args.no_reset_exit:
            print("\nresetting the C128 ...")
            with contextlib.suppress(OSError, TRError):
                client.reset()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
