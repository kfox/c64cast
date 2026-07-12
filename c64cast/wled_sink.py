"""Receive a realtime pixel stream and display it on the C64 (WLED bridge Mode 2).

The inverse of Mode 3 (`wled_sync.py`): instead of driving LEDs *from* the SID,
this turns the C64 into a **virtual LED matrix** that any WLED-ecosystem pixel
sender on the LAN can stream to. A `WLEDSource` (a `FrameSource`, see
`frame_source.py`) assembles incoming UDP packets into a BGR numpy frame; a
`SourceScene` then quantizes it to the C64 through the ordinary display-mode
pipeline (palette / dither / color_match), exactly like a webcam or a generator.
Physical WLED pixel-count limits don't apply to a virtual sink — the matrix is
whatever `sink_width` × `sink_height` the scene declares (default 320×200).

Two wire protocols are accepted, on their standard ports, auto-detected per
datagram (a sender only needs to speak one):

* **DDP** (Distributed Display Protocol), UDP **4048** — what LedFx / xLights /
  Jinx! emit. A 10-byte header carries a byte-offset + length so a frame larger
  than one datagram spans several packets; the "push" flag on the last packet of
  a frame signals "display now".
* **WLED realtime UDP**, UDP **21324** — WLED's own protocol (a WLED device
  syncing, or a simple sender). Byte 0 selects the sub-format: WARLS (indexed),
  DRGB (from pixel 0), DRGBW (RGB+white, white dropped), DNRGB (16-bit start
  index, for >256 px). Byte 1 is a return-to-normal timeout we ignore (the scene
  owns the display lifetime).

E1.31/sACN (multi-universe reassembly) is a deliberate follow-up — LedFx and
xLights can both emit DDP, so it buys little for a lot of protocol surface.

Pure stdlib (`socket`/`struct`/`select`) + numpy — no new dependency and no
`wled` extra (unlike Modes 1/3, which need zeroconf/fastapi). The parsers are
side-effect-free so they unit-test against the documented byte layouts.
"""

from __future__ import annotations

import contextlib
import logging
import select
import socket
import struct
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from ._pollthread import PollThread
from .frame_source import BaseFrameSource

if TYPE_CHECKING:
    from .modulation import MusicModulation

log = logging.getLogger(__name__)

# Standard ports. DDP is fixed by spec; WLED realtime is WLED's documented
# realtime UDP port.
DDP_PORT = 4048
WLED_REALTIME_PORT = 21324

# --- DDP -------------------------------------------------------------------
# 10-byte header, big-endian offset+length. Layout (WLED's DDPoutput / the DDP
# spec): flags, sequence, data-type, dest-id, uint32 offset (bytes), uint16 len.
_DDP_HEADER_FMT = ">BBBBIH"
_DDP_HEADER_LEN = struct.calcsize(_DDP_HEADER_FMT)
assert _DDP_HEADER_LEN == 10, "DDP header must be 10 bytes"

_DDP_FLAG_VER1 = 0x40  # version bits (V1) — set on a valid data packet
_DDP_FLAG_PUSH = 0x01  # last packet of a frame → display it now
_DDP_FLAG_QUERY = 0x08  # a query/discovery packet, not pixel data
_DDP_FLAG_REPLY = 0x04  # a reply packet, not pixel data

# --- WLED realtime UDP -----------------------------------------------------
_WLED_WARLS = 1  # [index, r, g, b] per pixel
_WLED_DRGB = 2  # [r, g, b] from pixel 0
_WLED_DRGBW = 3  # [r, g, b, w] from pixel 0 (white dropped)
_WLED_DNRGB = 4  # [startHi, startLo] then [r, g, b] — >256 px capable


@dataclass(frozen=True)
class DdpPacket:
    """A decoded DDP data packet: a run of pixel bytes at a byte `offset`, plus
    whether this packet ends a frame (`push`)."""

    offset: int
    payload: bytes
    push: bool


def parse_ddp(datagram: bytes) -> DdpPacket | None:
    """Decode a DDP data packet, or None if it isn't one (too short, wrong
    version, or a query/reply rather than pixel data)."""
    if len(datagram) < _DDP_HEADER_LEN:
        return None
    header = struct.unpack(_DDP_HEADER_FMT, datagram[:_DDP_HEADER_LEN])
    flags, offset, length = header[0], header[4], header[5]
    if (flags & _DDP_FLAG_VER1) == 0:
        return None  # not a V1 packet
    if flags & (_DDP_FLAG_QUERY | _DDP_FLAG_REPLY):
        return None  # discovery/reply traffic, no pixels
    payload = datagram[_DDP_HEADER_LEN : _DDP_HEADER_LEN + length]
    return DdpPacket(offset=offset, payload=payload, push=bool(flags & _DDP_FLAG_PUSH))


def parse_wled_realtime(datagram: bytes) -> list[tuple[int, int, int, int]] | None:
    """Decode a WLED realtime UDP packet into `(pixel_index, r, g, b)` writes,
    or None if the protocol byte isn't a supported pixel format.

    Byte 0 = protocol id, byte 1 = timeout (ignored). Truncated trailing pixels
    are dropped rather than erroring — a short final group is just ignored."""
    if len(datagram) < 2:
        return None
    proto = datagram[0]
    body = datagram[2:]
    writes: list[tuple[int, int, int, int]] = []
    if proto == _WLED_WARLS:
        for i in range(0, len(body) - 3, 4):
            idx, r, g, b = body[i], body[i + 1], body[i + 2], body[i + 3]
            writes.append((idx, r, g, b))
        return writes
    if proto == _WLED_DRGB:
        for i in range(0, len(body) - 2, 3):
            writes.append((i // 3, body[i], body[i + 1], body[i + 2]))
        return writes
    if proto == _WLED_DRGBW:
        for i in range(0, len(body) - 3, 4):
            writes.append((i // 4, body[i], body[i + 1], body[i + 2]))
        return writes
    if proto == _WLED_DNRGB:
        if len(body) < 2:
            return []
        start = (body[0] << 8) | body[1]
        rgb = body[2:]
        for i in range(0, len(rgb) - 2, 3):
            writes.append((start + i // 3, rgb[i], rgb[i + 1], rgb[i + 2]))
        return writes
    return None


class PixelFrameAssembler:
    """Accumulates pixel writes into a `width*height*3` RGB byte buffer and
    hands out BGR frames.

    DDP delivers byte runs at absolute offsets (`apply_ddp`); WLED realtime
    delivers `(index, r, g, b)` writes (`apply_pixels`). Both clip to the
    buffer so a sender configured larger than the sink can't overflow it.
    `snapshot_bgr` reshapes to `(H, W, 3)` and swaps RGB→BGR for the cv2
    display pipeline. Not internally locked — the receiver thread owns it and
    publishes finished frames under its own lock."""

    def __init__(self, width: int, height: int):
        self.width = int(width)
        self.height = int(height)
        self._nbytes = self.width * self.height * 3
        self._buf = bytearray(self._nbytes)

    def apply_ddp(self, offset: int, payload: bytes) -> None:
        if offset < 0 or offset >= self._nbytes or not payload:
            return
        end = min(offset + len(payload), self._nbytes)
        self._buf[offset:end] = payload[: end - offset]

    def apply_pixels(self, writes: list[tuple[int, int, int, int]]) -> None:
        n = self.width * self.height
        for idx, r, g, b in writes:
            if 0 <= idx < n:
                o = idx * 3
                self._buf[o] = r
                self._buf[o + 1] = g
                self._buf[o + 2] = b

    def snapshot_bgr(self) -> np.ndarray:
        rgb = np.frombuffer(bytes(self._buf), dtype=np.uint8).reshape(self.height, self.width, 3)
        return rgb[..., ::-1].copy()  # RGB → BGR


class WledPixelReceiver:
    """Daemon thread listening for DDP + WLED-realtime pixel streams.

    Binds both standard UDP ports, `select`s on them, feeds each datagram to the
    right parser → `PixelFrameAssembler`, and publishes the assembled BGR frame:
    for DDP on the push flag (falling back to every packet if a sender never
    pushes), for WLED-realtime after each datagram. The freshest published frame
    is available via `latest()`. A bind failure (port already in use) is stored
    in `bind_error` so the owning scene can self-abort cleanly."""

    # select() wakeup cadence so the blocking loop can honor the stop event.
    _SELECT_TIMEOUT = 0.25
    _RECV_BUF = 65535

    def __init__(
        self,
        width: int,
        height: int,
        *,
        host: str = "0.0.0.0",
        ddp_port: int = DDP_PORT,
        wled_port: int = WLED_REALTIME_PORT,
    ):
        self._assembler = PixelFrameAssembler(width, height)
        self._host = host
        self._ddp_port = ddp_port
        self._wled_port = wled_port
        self._sockets: list[socket.socket] = []
        self._ddp_sock: socket.socket | None = None
        self._latest: np.ndarray | None = None
        self._lock = threading.Lock()
        self._poll: PollThread | None = None
        self._saw_ddp_push = False
        self.bind_error: OSError | None = None

    def _bind(self, port: int) -> socket.socket | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self._host, port))
        except OSError as e:
            s.close()
            self.bind_error = e
            log.warning("WLED sink: cannot bind UDP %s:%d (%s)", self._host, port, e)
            return None
        return s

    def start(self) -> bool:
        """Bind the sockets and start the receive thread. Returns False (and
        stores `bind_error`) if either port is unavailable."""
        if self._poll is not None and self._poll.is_running():
            return True
        ddp = self._bind(self._ddp_port)
        wled = self._bind(self._wled_port)
        if ddp is None or wled is None:
            for s in (ddp, wled):
                if s is not None:
                    s.close()
            return False
        self._ddp_sock = ddp
        self._sockets = [ddp, wled]
        log.info(
            "WLED sink: listening for DDP on %s:%d and WLED realtime on %s:%d (%dx%d)",
            self._host,
            ddp.getsockname()[1],
            self._host,
            wled.getsockname()[1],
            self._assembler.width,
            self._assembler.height,
        )
        self._poll = PollThread(self._worker, name="wled-sink", manual=True)
        self._poll.start()
        return True

    def stop(self) -> None:
        if self._poll is not None:
            self._poll.stop()
            self._poll = None
        for s in self._sockets:
            with contextlib.suppress(OSError):
                s.close()
        self._sockets = []
        self._ddp_sock = None

    def bound_ports(self) -> tuple[int, int] | None:
        """The (ddp, wled) ports actually bound, or None if not started. Useful
        when the receiver was created with ephemeral ports (port 0)."""
        if len(self._sockets) != 2:
            return None
        return (self._sockets[0].getsockname()[1], self._sockets[1].getsockname()[1])

    def latest(self) -> np.ndarray | None:
        with self._lock:
            return self._latest

    def _publish(self) -> None:
        with self._lock:
            self._latest = self._assembler.snapshot_bgr()

    def _handle(self, sock: socket.socket, datagram: bytes) -> None:
        if sock is self._ddp_sock:
            pkt = parse_ddp(datagram)
            if pkt is None:
                return
            self._assembler.apply_ddp(pkt.offset, pkt.payload)
            if pkt.push:
                self._saw_ddp_push = True
                self._publish()
            elif not self._saw_ddp_push:
                # A sender that never sets the push flag: publish every packet so
                # the display still updates (once we've seen one push we trust it).
                self._publish()
            return
        writes = parse_wled_realtime(datagram)
        if writes is None:
            return
        self._assembler.apply_pixels(writes)
        self._publish()

    def _worker(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                ready, _, _ = select.select(self._sockets, [], [], self._SELECT_TIMEOUT)
            except (OSError, ValueError):
                break  # sockets closed under us during stop()
            for sock in ready:
                try:
                    datagram = sock.recvfrom(self._RECV_BUF)[0]
                except OSError:
                    continue
                self._handle(sock, datagram)


class WLEDSource(BaseFrameSource):
    """A `FrameSource` fed by a `WledPixelReceiver`.

    Infinite source (the scene's `duration_s` governs its lifetime). `read`
    returns the latest received BGR frame, or None until the first packet
    arrives — the scene skips the render on a None, so an idle sink simply shows
    nothing (then holds the last frame) rather than erroring. If the UDP ports
    can't be bound, `setup` flags `_bind_failed` so the scene aborts and the
    playlist advances (mirrors a failed audio source)."""

    def __init__(self, width: int, height: int, *, host: str = "0.0.0.0"):
        self._receiver = WledPixelReceiver(width, height, host=host)
        self._bind_failed = False

    def setup(self) -> None:
        if not self._receiver.start():
            self._bind_failed = True
            log.error(
                "WLED sink: failed to start receiver (%s) — scene will abort",
                self._receiver.bind_error,
            )

    def read(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray | None:
        return self._receiver.latest()

    @property
    def finished(self) -> bool:
        return self._bind_failed

    def teardown(self) -> None:
        self._receiver.stop()
