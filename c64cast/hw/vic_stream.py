"""The Ultimate 64's own VIC stream, received and reassembled into frames.

The machine's FPGA taps the VIC's own pixel stream and pushes it out of the
Ethernet MAC as UDP, with no C64 involvement at all. Socket DMA command
`0xFF20` turns it on and names the destination; `0xFF30` stops it. **Ultimate
64 only** — the firmware compiles both commands under `#ifdef U64`, and callers
check `HardwareProfile.supports_video_stream`.

## The wire format

UDP, one packet per few scanlines, 12-byte header then packed pixels:

```
 0  uint16  sequence number         (rises monotonically; wraps)
 2  uint16  frame number            (rises per frame; wraps)
 4  uint16  line number             (bit 15 set on a frame's LAST packet)
 6  uint16  pixels per line         (384)
 8  ...     four more header bytes  (lines/packet, bpp, encoding)
12  ...     payload
```

The payload is 4 bits per pixel, two pixels per byte, **low nibble first** —
so byte 0 is pixels 0 and 1, and each nibble indexes the sixteen C64 colors
directly. At 384 pixels that is 192 bytes a line. Width is read from the
header and height is counted from how much of a frame arrived (~272 on PAL,
~240 on NTSC); the last four header bytes are unused here, as they are in the
firmware's own reference client.

See docs/architecture/hardware-io.md#vic_streampy--the-machines-own-vic-output.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import struct
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass

import numpy as np

from c64cast._pollthread import PollThread

from .socket_dma import SocketDMAError

log = logging.getLogger(__name__)


@contextlib.contextmanager
def _link_trouble(what: str) -> Generator[None]:
    """Swallow a link failure during `what`, but say so.

    A stop that cannot reach the machine has still stopped listening, and a
    watchdog re-arm that misses one round is renewed on the next — neither is
    worth failing a caller over. Logged rather than suppressed outright,
    because a bare `contextlib.suppress` here is what made #419 invisible: the
    OFF command was reaching a closed DMA client, raising, and being dropped
    with nothing anywhere to say the machine was never told."""
    try:
        yield
    except (OSError, SocketDMAError) as e:
        log.debug("vic stream: %s: %s", what, e)


#: The firmware's default port for stream 0 (`11000 + streamID`). We bind an
#: ephemeral port and name it in the command instead, so two hosts on one LAN
#: never contend — this is here to document what the machine defaults to.
DEFAULT_VIC_PORT = 11000

#: Bytes of header before the pixels.
HEADER_BYTES = 12

#: `seq`, `frame`, `line`, `pixels per line` — the four fields worth reading.
_HEADER = struct.Struct("<HHHH")

#: Set in the `line` field on the last packet of a frame.
_LAST_PACKET = 0x8000

#: Big enough for any packet the firmware emits (it sends ~768-byte payloads).
_RECV_BYTES = 2048

#: How long the machine is told to keep streaming, and how often that is
#: renewed. The gap is generous on purpose: a re-arm re-runs the firmware's
#: destination resolution, so renewing every couple of seconds would be a
#: needless ARP round for no more safety than this.
WATCHDOG_S = 20.0
REARM_EVERY_S = 7.0

#: How soon after starting to re-issue the ON once, which doubles as the retry
#: for a cold ARP table. Before the firmware can stream to a unicast address it
#: resolves the destination's MAC by sending it up to ten two-byte probes and
#: watching its own ARP table; if the entry does not appear in time it gives up
#: and the stream never starts. Those probes are exactly what *populates* the
#: table, so the attempt that failed has left the next one ready to succeed —
#: observed on a real machine as "nothing at all, then instant on the retry".
#: One early renewal turns that into a delay instead of a dead panel.
PRIME_AFTER_S = 1.5

#: Give up on the frame in hand after this long without a packet — the machine
#: was stopped, or the network dropped the last packet of a frame and the
#: `line & 0x8000` that would have finished it is never coming.
_STALE_FRAME_S = 1.0

# Cap on the partial-frame buffer, far above one real frame (~52 KB across ~68
# packets): a flood that never sets LAST_PACKET must not grow it without limit.
_MAX_PARTIAL_BYTES = 1 << 19


@dataclass(frozen=True)
class VicFrame:
    """One reassembled frame: C64 color indices, one byte per pixel.

    ``indices`` is ``(height, width)`` uint8 in 0..15 — palette indices, not
    color. Keeping it that way is the point: it is what the VIC actually
    selected per pixel, so a caller comparing against what c64cast *meant* to
    draw compares indices with indices, and a caller displaying it maps through
    whichever palette it believes in."""

    indices: np.ndarray
    number: int
    received_at: float

    @property
    def width(self) -> int:
        return int(self.indices.shape[1])

    @property
    def height(self) -> int:
        return int(self.indices.shape[0])


def unpack_pixels(payload: bytes, width: int) -> np.ndarray:
    """Decode a frame's packed 4bpp payload into ``(height, width)`` indices.

    Two pixels a byte, **low nibble first**. Height is whatever the payload
    holds — the stream says how tall a frame is by how much of one it sent, and
    a trailing partial line is dropped rather than padded, because half a line
    of a frame is not a line of that frame."""
    if width <= 0 or width % 2:
        raise ValueError(f"a 4bpp line needs an even, positive width, got {width}")
    per_line = width // 2
    usable = len(payload) - len(payload) % per_line
    if usable == 0:
        raise ValueError(f"payload of {len(payload)} bytes is under one {width}px line")
    packed = np.frombuffer(payload[:usable], dtype=np.uint8).reshape(-1, per_line)
    out = np.empty((packed.shape[0], width), dtype=np.uint8)
    out[:, 0::2] = packed & 0x0F
    out[:, 1::2] = packed >> 4
    return out


class VicStreamReceiver:
    """Ask a machine to stream its VIC output here, and keep the latest frame.

    Latest, not a queue: a viewer wants what is on the screen now, and a queue
    of frames nobody read is a way to fall behind by a second and not notice.
    The socket's own receive buffer absorbs a slow reader for the few
    milliseconds that matters, and past that dropping is the correct answer.

    Not self-driving from a constructor: `start()` opens the socket, works out
    which of this host's addresses the machine can reach, and only then tells it
    to send — so a failure to bind never leaves a machine streaming at nothing.
    """

    def __init__(self, dma, *, machine_host: str, bind_host: str = "") -> None:
        self._dma = dma
        self._machine_host = machine_host
        self._bind_host = bind_host
        self._sock: socket.socket | None = None
        self._poll: PollThread | None = None
        self._lock = threading.Lock()
        self._latest: VicFrame | None = None
        self._machine_addr = ""
        self._parts: list[bytes] = []
        self._parts_bytes = 0
        self._width = 0
        self._last_packet_at = 0.0
        self._rearm_at = 0.0
        self._destination = ""
        self._frames = 0
        self._dropped = 0

    def start(self) -> None:
        """Bind, tell the machine where to send, and start reassembling."""
        if self._poll is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # One frame is ~52 KB across ~68 back-to-back packets; the default
        # receive buffer is under that on some systems, which tears frames.
        with _link_trouble("receive buffer resize"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        sock.settimeout(0.2)
        try:
            # `_bind_host` defaults to "" (all interfaces), not loopback: the
            # sender is the Ultimate 64 out on the LAN. Same waiver as
            # wled_sink.py's `_bind`; `bind_host` narrows it.
            # codeql[py/bind-socket-all-network-interfaces]
            sock.bind((self._bind_host, 0))
            port = sock.getsockname()[1]
            self._destination = f"{self._reachable_address()}:{port}"
            self._machine_addr = socket.gethostbyname(self._machine_host)
            self._dma.vicstream_on(self._destination, stop_after_s=WATCHDOG_S)
        except (OSError, SocketDMAError):
            sock.close()
            raise
        self._sock = sock
        self._rearm_at = time.monotonic() + PRIME_AFTER_S
        self._poll = PollThread(self._run, name="vic-stream", manual=True, join_timeout=1.0)
        self._poll.start()
        log.info("vic stream: %s -> %s", self._machine_host, self._destination)

    def stop(self) -> None:
        """Tell the machine to stop, then close. In that order: the socket must
        outlive the command by the round trip, or the last packets land on a
        closed port and the OS answers them with ICMP nobody asked for."""
        poll, self._poll = self._poll, None
        sock, self._sock = self._sock, None
        with _link_trouble("stream off"):
            self._dma.vicstream_off()
        if poll is not None:
            poll.stop()
        if sock is not None:
            sock.close()
        with self._lock:
            self._latest = None
            self._parts = []
            self._parts_bytes = 0
        log.info("vic stream: stopped (%d frames, %d dropped)", self._frames, self._dropped)

    def latest(self) -> VicFrame | None:
        """The most recent complete frame, or None if none has arrived yet."""
        with self._lock:
            return self._latest

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"frames": self._frames, "dropped": self._dropped}

    def _run(self, stop: threading.Event) -> None:
        sock = self._sock
        while not stop.is_set() and sock is not None:
            try:
                packet, addr = sock.recvfrom(_RECV_BYTES)
            except TimeoutError:
                self._expire_partial()
                self._maybe_rearm()
                continue
            except OSError:
                return  # closed under us by stop()
            if not self._is_from_machine(addr):
                continue
            self._accept(packet)
            self._maybe_rearm()

    def _is_from_machine(self, addr: tuple[str, int]) -> bool:
        """True if a datagram's source is the machine we told to stream.

        The wire protocol has no authentication, so without this check
        anyone on the segment who guesses the bound port can inject frames,
        or a flood of forged partial packets."""
        return addr[0] == self._machine_addr

    def _accept(self, packet: bytes) -> None:
        if len(packet) <= HEADER_BYTES:
            return
        _, number, line, width = _HEADER.unpack_from(packet, 0)
        self._last_packet_at = time.monotonic()
        if width != self._width:
            # A width change is a mode change on the machine; the frame in
            # hand was measured in the old one.
            self._parts = []
            self._parts_bytes = 0
            self._width = width
        self._parts.append(packet[HEADER_BYTES:])
        self._parts_bytes += len(packet) - HEADER_BYTES
        if self._parts_bytes > _MAX_PARTIAL_BYTES:
            self._parts = []
            self._parts_bytes = 0
            self._dropped += 1
            return
        if not line & _LAST_PACKET:
            return
        payload, self._parts = b"".join(self._parts), []
        self._parts_bytes = 0
        try:
            indices = unpack_pixels(payload, width)
        except ValueError:
            self._dropped += 1
            return
        self._frames += 1
        with self._lock:
            self._latest = VicFrame(indices, number, time.monotonic())

    def _expire_partial(self) -> None:
        """Drop a frame that stopped arriving. Without this, the packets of a
        frame whose end was lost would be joined to the *next* frame and shown
        as one tall wrong picture rather than as the dropped frame it is."""
        if not self._parts:
            return
        if time.monotonic() - self._last_packet_at < _STALE_FRAME_S:
            return
        self._parts = []
        self._parts_bytes = 0
        self._dropped += 1

    def _maybe_rearm(self) -> None:
        """Renew the machine's auto-stop timer while we are still listening."""
        now = time.monotonic()
        if now < self._rearm_at:
            return
        self._rearm_at = now + REARM_EVERY_S
        with _link_trouble("watchdog re-arm"):
            self._dma.vicstream_on(self._destination, stop_after_s=WATCHDOG_S)

    def _reachable_address(self) -> str:
        """This host's address *as the machine would reach it*.

        Asked of the routing table rather than of `gethostname`, which on a
        laptop with a VPN, a container bridge and a Wi-Fi interface answers with
        whichever one it feels like. Connecting a UDP socket sends nothing; it
        just makes the kernel pick the source address for that destination,
        which is precisely the question."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((self._machine_host, DEFAULT_VIC_PORT))
            return str(probe.getsockname()[0])
        finally:
            probe.close()
