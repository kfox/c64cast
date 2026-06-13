"""TeensyROM+ (TR) control client: framing for the TR remote protocol over
either USB serial or raw TCP.

The TR firmware exposes a small token protocol (see SensoriumEmbedded/
TeensyROM `Source/Teensy/SerUSBIO.ino` + `FileTransfer.ino`). Unlike the
Ultimate's split socket-DMA/REST design, ONE byte stream carries every
command, and every command is acknowledged (`AckToken 0x64CC` / `FailToken
0x9B7F`). c64cast uses this subset:

  * **WriteC64Mem `0x64FB`** — sequential DMA write into C64 address space.
    The hot path; carries all rendering + audio programming. (Present in the
    author's test build; may not be in older shipping firmware.)
  * **Reset `0x64EE`** — reset the C64 (boots to the TR menu). Responds with
    a text line, not a binary ack.
  * **PostFile `0x64BB`** — upload a file to SD/USB. Requires the TR menu to
    be the active handler.
  * **LaunchFile `0x6444`** — launch a file already on storage.
  * **FWCheck `0x64E0` / Ping `0x6455`** — liveness + firmware-type probe.

**Wire byte order is asymmetric** (confirmed on TeensyROM+ v0.7.2.4 + in the
firmware source):
  * **Host -> TR (commands):** 16-bit tokens + every multi-byte field are
    big-endian / MSB-first — `inVal = (inVal<<8) | read()` in the dispatcher,
    `GetUInt` reads the high byte first.
  * **TR -> host (replies):** tokens are little-endian / LSB-first —
    `SendU16` writes `val & 0xff` then `val >> 8`. So an Ack (0x64CC) arrives
    on the wire as `CC 64`; parsing it big-endian yields 0xCC64 and makes a
    successful write look like an error.
We therefore send big-endian and parse replies little-endian. The smoke test
after wiring: write one byte to `$D020` and confirm the border colour changes
(a byte-swapped *address* would land in RAM and leave the border untouched).

The TR also emits unsolicited status text/tokens around reset + menu
transitions (e.g. a GoodSID token after boot); `TRClient._drain_stale` clears
those before control commands so they aren't misread as an ack.

Two transports share the framing via the `TRTransport` byte-I/O interface:
`SerialTransport` (pyserial, the `tr` extra) and `TcpTransport` (stdlib
socket, port 2112). pyserial is imported lazily so the module loads without
the extra installed.
"""
from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from abc import ABC, abstractmethod
from collections import deque

log = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 2112
DEFAULT_BAUD = 2_000_000      # 2 Mbaud 8N1, per the firmware author

# ---- protocol tokens (Common_Defs.h) --------------------------------------
TOK_WRITE_C64_MEM = 0x64FB
TOK_RESET_C64     = 0x64EE
TOK_LAUNCH_FILE   = 0x6444
TOK_POST_FILE     = 0x64BB
TOK_DELETE_FILE   = 0x64CF
TOK_PING          = 0x6455
TOK_FW_CHECK      = 0x64E0
TOK_ACK           = 0x64CC
TOK_FAIL          = 0x9B7F
TOK_RETRY         = 0x9B7E
TOK_FW_MINIMAL    = 0x64E1
TOK_FW_FULL       = 0x64E2

# PostFile / LaunchFile storage selector (RegMenuTypes). PostFile supports
# USB + SD only; Teensy is launch-target-only.
DRIVE_USB    = 0
DRIVE_SD     = 1
# (RegMenuTypes also defines Teensy = 2, but it's launch-target-only — never a
# PostFile destination — so c64cast has no use for it.)


class TRError(Exception):
    """Raised when the TR can't be reached, a command is NAK'd (FailToken),
    or a reply is malformed/times out. The CLI surfaces a user-actionable
    message (parallel to socket_dma.SocketDMAError on the Ultimate side)."""


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------
class TRTransport(ABC):
    """Byte-level link to a TR. Implementations guarantee `recv_exact` blocks
    until exactly n bytes arrive or the io timeout elapses (the ack handshake
    depends on it)."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def send_all(self, data: bytes) -> None: ...

    @abstractmethod
    def recv_exact(self, n: int) -> bytes: ...

    @abstractmethod
    def drain_text(self, quiet_s: float = 0.2) -> str:
        """Read whatever bytes are currently available (e.g. a text status
        line from Reset/Ping), stopping after `quiet_s` of silence. Best
        effort — never raises on timeout, returns what it got."""
        ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def description(self) -> str: ...


class SerialTransport(TRTransport):
    """USB-serial link via pyserial (the ``tr`` extra). 2 Mbaud 8N1."""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD,
                 io_timeout: float = 2.0):
        self.port = port
        self.baud = baud
        self.io_timeout = io_timeout
        self._ser: object | None = None

    def connect(self) -> None:
        try:
            import serial  # lazy: only needed for the serial transport
        except ImportError as e:
            raise TRError(
                "pyserial is not installed — install the 'tr' extra "
                "(uv sync --extra tr) to use the TeensyROM serial "
                "transport.") from e
        try:
            self._ser = serial.Serial(
                self.port, baudrate=self.baud, bytesize=8,
                parity="N", stopbits=1,
                timeout=self.io_timeout, write_timeout=self.io_timeout)
        except serial.SerialException as e:
            raise TRError(
                f"could not open serial port {self.port!r}: {e}. Check the "
                "USB cable to the TR's micro-USB-B port and the port name.") from e

    def send_all(self, data: bytes) -> None:
        assert self._ser is not None
        self._ser.write(data)   # type: ignore[attr-defined]
        self._ser.flush()       # type: ignore[attr-defined]

    def recv_exact(self, n: int) -> bytes:
        assert self._ser is not None
        buf = bytearray()
        deadline = time.monotonic() + self.io_timeout
        while len(buf) < n:
            chunk = self._ser.read(n - len(buf))  # type: ignore[attr-defined]
            if chunk:
                buf.extend(chunk)
            elif time.monotonic() > deadline:
                raise TRError(
                    f"serial read timed out ({len(buf)}/{n} bytes)")
        return bytes(buf)

    def drain_text(self, quiet_s: float = 0.2) -> str:
        assert self._ser is not None
        buf = bytearray()
        deadline = time.monotonic() + quiet_s
        while time.monotonic() < deadline:
            chunk = self._ser.read(64)  # type: ignore[attr-defined]
            if chunk:
                buf.extend(chunk)
                deadline = time.monotonic() + quiet_s
        return buf.decode("ascii", errors="replace")

    def close(self) -> None:
        if self._ser is not None:
            with contextlib.suppress(Exception):
                self._ser.close()  # type: ignore[attr-defined]
            self._ser = None

    @property
    def description(self) -> str:
        return f"serial {self.port}@{self.baud}"


class TcpTransport(TRTransport):
    """Raw-TCP link to the TR's TCP listener (port 2112). No extra dep."""

    def __init__(self, host: str, port: int = DEFAULT_TCP_PORT,
                 connect_timeout: float = 5.0, io_timeout: float = 2.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout)
        except OSError as e:
            raise TRError(
                f"could not connect to {self.host}:{self.port}: {e}. Enable "
                "'Enable TCP Listener' in the TR setup and reboot it.") from e
        sock.settimeout(self.io_timeout)
        # No Nagle — small ack-gated commands must ship immediately.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

    def send_all(self, data: bytes) -> None:
        assert self._sock is not None
        self._sock.sendall(data)

    def recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except TimeoutError as e:
                raise TRError(
                    f"tcp read timed out ({len(buf)}/{n} bytes)") from e
            if not chunk:
                raise TRError("tcp socket closed mid-read")
            buf.extend(chunk)
        return bytes(buf)

    def drain_text(self, quiet_s: float = 0.2) -> str:
        assert self._sock is not None
        buf = bytearray()
        prev = self._sock.gettimeout()
        self._sock.settimeout(quiet_s)
        try:
            while True:
                try:
                    chunk = self._sock.recv(64)
                except (TimeoutError, OSError):
                    break
                if not chunk:
                    break
                buf.extend(chunk)
        finally:
            self._sock.settimeout(prev)
        return buf.decode("ascii", errors="replace")

    def close(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    @property
    def description(self) -> str:
        return f"tcp {self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class TRClient:
    """Frames TR protocol commands over a `TRTransport`. Thread-safe: a lock
    serializes each command's send+ack so render and audio threads can't
    interleave on the wire (mirrors SocketDMAClient's per-command mutex).

    Every memory write is acknowledged, so there is no separate flush step —
    by the time `write_segment` returns, the TR has executed the write."""

    # Cap a single WriteC64Mem segment. The 16-bit length field allows 65535,
    # but bounding segment size keeps a transient failure cheap to retry and
    # keeps the ack cadence regular. write_memory_file splits larger blobs.
    MAX_SEGMENT_BYTES = 4096

    def __init__(self, transport: TRTransport):
        self.transport = transport
        self._lock = threading.Lock()
        self._latencies: deque[float] = deque(maxlen=256)
        self.firmware = "unknown"   # "full" | "minimal" | "unknown"

    # ---- connect / close -------------------------------------------------
    def connect(self) -> None:
        """Open the transport and best-effort probe the firmware type.

        The transport opening is the real liveness gate (it raises TRError on
        failure). The FWCheck probe is best-effort — a minimal/older build
        may not answer it, which must not fail the connection."""
        self.transport.connect()
        # Clear any banner/boot chatter left in the buffer before probing.
        self._drain_stale()
        try:
            self.firmware = self._fw_check_unlocked()
        except TRError:
            self.firmware = "unknown"
        log.info("teensyrom: connected via %s (firmware: %s)",
                 self.transport.description, self.firmware)

    def close(self) -> None:
        with self._lock:
            self.transport.close()

    # ---- low-level framing -----------------------------------------------
    # IMPORTANT asymmetry, confirmed on hardware (TeensyROM+ v0.7.2.4) and in
    # the firmware source: the TR *reads* command tokens + multi-byte fields
    # MSB-first (dispatcher `(inVal<<8)|read()`, `GetUInt`), but *sends* its
    # replies LSB-first (`SendU16` writes `val&0xff` then `val>>8`). So we
    # send big-endian and parse replies little-endian. Getting the reply
    # parse wrong makes a real Ack (0x64CC, on the wire `CC 64`) read as
    # 0xCC64 and look like an error even though the write executed.
    @staticmethod
    def _u16(value: int) -> bytes:
        """16-bit big-endian (MSB first) — command tokens + fields."""
        return bytes([(value >> 8) & 0xFF, value & 0xFF])

    @staticmethod
    def _u32(value: int) -> bytes:
        return bytes([(value >> 24) & 0xFF, (value >> 16) & 0xFF,
                      (value >> 8) & 0xFF, value & 0xFF])

    def _read_token(self) -> int:
        """Read a 16-bit reply token. Replies are little-endian (LSB first)."""
        lo, hi = self.transport.recv_exact(2)
        return lo | (hi << 8)

    def _drain_stale(self, quiet_s: float = 0.3) -> None:
        """Discard any unsolicited bytes sitting in the receive buffer before
        a control command. The TR emits boot/menu chatter (e.g. a GoodSID
        token after reset) that would otherwise be misread as the next
        command's ack. NOT used on the write_segment hot path — steady-state
        writing produces exactly one ack per command."""
        stale = self.transport.drain_text(quiet_s)
        if stale:
            log.debug("teensyrom: drained %d stale bytes before command: %r",
                      len(stale), stale[:32])

    def _expect_ack(self, what: str) -> None:
        tok = self._read_token()
        if tok == TOK_ACK:
            return
        # Resync before raising: a rejected command is often followed by an
        # error text line (e.g. "Failed to ensure directory"), and a stale
        # read offset would otherwise make every subsequent ack misalign and
        # cascade. Drain trailing bytes so the next command starts clean.
        self.transport.drain_text(0.15)
        if tok == TOK_FAIL:
            raise TRError(f"{what}: TR returned FailToken (0x9B7F)")
        if tok == TOK_RETRY:
            raise TRError(f"{what}: TR returned RetryToken (0x9B7E)")
        raise TRError(f"{what}: unexpected reply 0x{tok:04X} (expected ack)")

    def _fw_check_unlocked(self) -> str:
        # Caller (connect) is single-threaded at this point.
        self.transport.send_all(self._u16(TOK_FW_CHECK))
        tok = self._read_token()
        if tok == TOK_FW_FULL:
            return "full"
        if tok == TOK_FW_MINIMAL:
            return "minimal"
        return "unknown"

    # ---- public commands -------------------------------------------------
    def write_segment(self, addr: int, data: bytes) -> None:
        """One WriteC64Mem command: token + addr(BE) + len(BE) + data, then
        read and verify the ack. Raises TRError on NAK/timeout."""
        frame = (self._u16(TOK_WRITE_C64_MEM)
                 + self._u16(addr & 0xFFFF)
                 + self._u16(len(data))
                 + data)
        with self._lock:
            t0 = time.perf_counter()
            self.transport.send_all(frame)
            self._expect_ack(f"WriteC64Mem ${addr:04X}")
            self._latencies.append(time.perf_counter() - t0)

    def reset(self) -> None:
        """Reset the C64 (boots to the TR menu). The firmware answers with a
        text line ("Reset cmd received"), NOT a binary ack — drain it so it
        doesn't pollute the next command's reply."""
        with self._lock:
            self.transport.send_all(self._u16(TOK_RESET_C64))
            line = self.transport.drain_text()
            if "Reset" not in line:
                log.debug("teensyrom reset: unexpected response %r", line)

    def delete_file(self, path: str, drive: int = DRIVE_SD) -> None:
        """Delete `path` from storage. Workflow: token -> ack ->
        storage(1)+path\\0 -> ack/fail. Raises TRError if the file doesn't
        exist (callers that delete-before-write should ignore that)."""
        payload = bytes([drive & 0xFF]) + path.encode("ascii") + b"\x00"
        with self._lock:
            self._drain_stale()
            self.transport.send_all(self._u16(TOK_DELETE_FILE))
            self._expect_ack("DeleteFile (open)")
            self.transport.send_all(payload)
            self._expect_ack(f"DeleteFile {path!r}")

    def post_file(self, data: bytes, dest_path: str,
                  drive: int = DRIVE_SD) -> None:
        """Upload `data` to `dest_path` on the given storage (DRIVE_SD /
        DRIVE_USB). Requires the TR menu to be the active handler, else the
        firmware replies "Busy!". Checksum = sum(bytes) mod 2^16.

        NOTE: the firmware REFUSES to overwrite — PostFile to an existing path
        returns FailToken ("File already exists."). Callers that re-upload the
        same path must delete_file() first.

        Workflow: token -> ack -> len(4)+cksum(2)+storage(1)+path\\0 ->
        ack/fail -> file data -> ack/fail.
        """
        checksum = sum(data) & 0xFFFF
        path_bytes = dest_path.encode("ascii") + b"\x00"
        header = (self._u32(len(data)) + self._u16(checksum)
                  + bytes([drive & 0xFF]) + path_bytes)
        with self._lock:
            self._drain_stale()   # clear post-reset/menu boot chatter
            self.transport.send_all(self._u16(TOK_POST_FILE))
            self._expect_ack("PostFile (open)")
            self.transport.send_all(header)
            self._expect_ack(f"PostFile header {dest_path!r}")
            self.transport.send_all(data)
            self._expect_ack(f"PostFile data {dest_path!r}")

    def launch_file(self, path: str, drive: int = DRIVE_SD) -> None:
        """Launch a file already present on storage. Workflow: token -> ack ->
        drive(1)+path\\0 -> ack -> C64 launches."""
        path_bytes = bytes([drive & 0xFF]) + path.encode("ascii") + b"\x00"
        with self._lock:
            self._drain_stale()   # clear post-reset/menu boot chatter
            self.transport.send_all(self._u16(TOK_LAUNCH_FILE))
            self._expect_ack("LaunchFile (open)")
            self.transport.send_all(path_bytes)
            self._expect_ack(f"LaunchFile {path!r}")

    def ping(self) -> str:
        """Liveness check. The firmware answers with a text status line."""
        with self._lock:
            self.transport.send_all(self._u16(TOK_PING))
            return self.transport.drain_text().strip()

    def flush(self) -> None:
        """No-op: every write is acked, so by the time write_segment returns
        the TR has already executed it. Present for API parity with the
        Ultimate's DMA-flush barrier."""
        return

    # ---- diagnostics -----------------------------------------------------
    def latency_summary(self) -> tuple[float, float, float, float, int]:
        with self._lock:
            snap = list(self._latencies)
        n = len(snap)
        if n == 0:
            return 0.0, 0.0, 0.0, 0.0, 0
        snap.sort()
        avg = sum(snap) / n
        p50 = snap[min(n - 1, int(0.50 * n))]
        p95 = snap[min(n - 1, int(0.95 * n))]
        return avg, p50, p95, snap[-1], n

    def format_latency(self) -> str | None:
        avg, p50, p95, mx, n = self.latency_summary()
        if n == 0:
            return None
        return (f"tr write latency: n={n} avg={avg * 1000:.1f} "
                f"p50={p50 * 1000:.1f} p95={p95 * 1000:.1f} "
                f"max={mx * 1000:.1f} ms")
