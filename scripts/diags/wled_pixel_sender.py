#!/usr/bin/env python3
"""Stream a moving test pattern to a c64cast WLED pixel sink (bridge Mode 2).

The WLED phone app is a *controller* — it cannot emit pixels — so this stands in
for a real pixel sender (LedFx / xLights) to drive and eyeball the Mode 2 sink
on real hardware without one. It generates an animated pattern (a scrolling hue
gradient with a bouncing white box, so both color reproduction and motion are
obvious after C64 quantization) and sends each frame over the wire in either
protocol the sink accepts:

* ``--protocol ddp``  → DDP (UDP 4048), the frame fragmented across packets with
  the push flag on the last one — what LedFx/xLights speak.
* ``--protocol wled`` → WLED realtime UDP (21324), DNRGB packets (16-bit start
  index, so >256 px works), byte-1 timeout set so the sink keeps the stream.

Point it at the host running c64cast (``--host``) with a ``wled`` scene active,
and set ``--width/--height`` to that scene's ``sink_width``/``sink_height``.

    scripts/diags/wled_pixel_sender.py --host 192.168.2.64 --protocol ddp
    scripts/diags/wled_pixel_sender.py --host 192.168.2.64 --protocol wled \\
        --width 64 --height 48 --fps 20 -t 30

Pure stdlib (socket/struct) — no c64cast import needed; runs from anywhere.
"""

from __future__ import annotations

import argparse
import colorsys
import math
import socket
import struct
import time

DDP_PORT = 4048
WLED_REALTIME_PORT = 21324

# DDP: keep each packet's payload under a safe UDP/Ethernet MTU. 1440 bytes =
# 480 RGB pixels/packet, the common WLED DDP chunk.
_DDP_MAX_PAYLOAD = 1440


def _frame_rgb(width: int, height: int, t: float) -> bytearray:
    """A width*height*3 RGB buffer: a scrolling horizontal hue gradient plus a
    bouncing white box. Deterministic in t, so any dropped frame is harmless."""
    buf = bytearray(width * height * 3)
    scroll = (t * 0.15) % 1.0
    # Precompute one row of the gradient (hue varies with x), reuse per row.
    row = bytearray(width * 3)
    for x in range(width):
        h = (x / max(1, width) + scroll) % 1.0
        r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(h, 1.0, 1.0))
        row[x * 3 : x * 3 + 3] = bytes((r, g, b))
    for y in range(height):
        buf[y * width * 3 : (y + 1) * width * 3] = row
    # Bouncing box.
    box = max(2, min(width, height) // 6)
    bx = int((0.5 + 0.5 * math.sin(t * 1.3)) * (width - box))
    by = int((0.5 + 0.5 * math.cos(t * 1.7)) * (height - box))
    for y in range(by, by + box):
        base = (y * width + bx) * 3
        buf[base : base + box * 3] = b"\xff" * (box * 3)
    return buf


def _send_ddp(sock: socket.socket, addr: tuple[str, int], frame: bytes) -> None:
    offset = 0
    remaining = len(frame)
    while remaining > 0:
        chunk = min(_DDP_MAX_PAYLOAD, remaining)
        last = chunk == remaining
        flags = 0x40 | (0x01 if last else 0x00)  # V1, push on the final packet
        header = struct.pack(">BBBBIH", flags, 0, 0, 1, offset, chunk)
        sock.sendto(header + frame[offset : offset + chunk], addr)
        offset += chunk
        remaining -= chunk


def _send_wled(sock: socket.socket, addr: tuple[str, int], frame: bytes) -> None:
    # DNRGB: [4, timeout, startHi, startLo] then RGB. Max 489 px/packet keeps
    # the datagram under ~1.5 KB. timeout=2 s so the sink holds the stream.
    px_per_pkt = 489
    total_px = len(frame) // 3
    for start in range(0, total_px, px_per_pkt):
        n = min(px_per_pkt, total_px - start)
        header = bytes([4, 2, (start >> 8) & 0xFF, start & 0xFF])
        sock.sendto(header + frame[start * 3 : (start + n) * 3], addr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True, help="c64cast host running a wled sink scene")
    ap.add_argument("--protocol", choices=("ddp", "wled"), default="ddp")
    ap.add_argument("--port", type=int, default=None, help="override the protocol's default port")
    ap.add_argument("--width", type=int, default=320)
    ap.add_argument("--height", type=int, default=200)
    ap.add_argument("--fps", type=float, default=20.0)
    ap.add_argument("-t", "--duration", type=float, default=30.0, help="seconds (0 = forever)")
    args = ap.parse_args()

    port = args.port or (DDP_PORT if args.protocol == "ddp" else WLED_REALTIME_PORT)
    addr = (args.host, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send = _send_ddp if args.protocol == "ddp" else _send_wled

    print(
        f"Streaming {args.width}x{args.height} {args.protocol.upper()} to {addr[0]}:{addr[1]} "
        f"at {args.fps:.0f} fps (Ctrl-C to stop)"
    )
    period = 1.0 / max(1.0, args.fps)
    start = time.time()
    frames = 0
    try:
        while args.duration == 0 or time.time() - start < args.duration:
            t = time.time() - start
            send(sock, addr, bytes(_frame_rgb(args.width, args.height, t)))
            frames += 1
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    print(f"Sent {frames} frames in {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
