"""Socket DMA client for the Ultimate 64.

The U64 firmware exposes a TCP server on port 64 that accepts a small
opcode protocol for direct DMA writes into C64 address space. Compared
to the REST API on port 80, the DMA protocol has two structural
advantages we exploit:

  * **Persistent socket.** Many commands share one TCP connection; the
    REST API forces ``Connection: close`` on every response, which means
    every PUT pays a fresh TCP handshake. Measured cost: 14 ms / 71
    writes/sec REST vs 5 ms / 200 writes/sec DMA.
  * **Tight wire format.** Each command is `<HH` opcode + length plus
    the payload. No HTTP headers, no JSON envelope.

The server's connection loop strictly serializes commands per
connection — one command read, dispatched, then the next. That FIFO
ordering is what lets ``flush()`` work: a trailing IDENTIFY round-trip
will only respond once every prior command has been processed.

Protocol reference: https://github.com/GideonZ/1541ultimate/blob/master/software/network/socket_dma.cc

This module covers only the opcodes needed by c64cast's write path
(DMAWRITE, IDENTIFY, AUTHENTICATE, plus RESET and KEYB for completeness).
The full opcode set is documented in [docs/caveats.md](../docs/caveats.md).
"""

from __future__ import annotations

import contextlib
import logging
import socket
import struct
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

DEFAULT_PORT = 64

# Opcode constants — see socket_dma.cc. We use a fraction of the full set.
CMD_KEYB = 0xFF03
CMD_RESET = 0xFF04
CMD_DMAWRITE = 0xFF06
CMD_REUWRITE = 0xFF07
CMD_IDENTIFY = 0xFF0E
CMD_AUTHENTICATE = 0xFF1F


class SocketDMAError(Exception):
    """Raised when the DMA service can't be reached, refuses authentication,
    or otherwise responds in a way that prevents normal operation. Caller
    (typically the CLI) is expected to surface a user-actionable message."""


class SocketDMAClient:
    """One-connection client. Not multi-process safe — each process should
    open its own. Within a process, ``dmawrite()`` and ``flush()`` are
    thread-safe via an internal lock that serializes writes on the wire so
    multi-byte commands from different threads can't interleave.

    The lifecycle is: ``connect()`` once at construction (called by the
    caller, not the constructor, so failures are easier to surface),
    ``dmawrite()`` / ``flush()`` repeatedly, ``close()`` at shutdown. A
    failed sendall triggers exactly one transparent reconnect-and-retry;
    a second failure is raised to the caller."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        password: str | None = None,
        connect_timeout: float = 5.0,
        io_timeout: float = 2.0,
    ):
        self.host = host
        self.port = port
        self.password = password or None  # treat "" same as None
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout

        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        # Per-sendall latency window. 256 samples ≈ 5s at 50 writes/s,
        # which matches the typical --profile-interval. Held by the same
        # lock as the socket itself; readers in latency_summary() snapshot
        # under that lock.
        self._latencies: deque[float] = deque(maxlen=256)
        self.product = "(not yet identified)"

    # ---- connect / close --------------------------------------------------

    def connect(self) -> None:
        """Open the TCP socket and complete the handshake.

        Raises ``SocketDMAError`` on connection refused (service disabled
        on the U64), auth rejection, or unexpected IDENTIFY response."""
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        # Caller must hold self._lock.
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except ConnectionRefusedError as e:
            raise SocketDMAError(
                f"connection refused at {self.host}:{self.port}. The U64 "
                f"Ultimate DMA Service is probably disabled. Enable it at "
                f"F2 Menu -> Network Settings -> Ultimate DMA Service."
            ) from e
        except OSError as e:
            raise SocketDMAError(f"could not connect to {self.host}:{self.port}: {e}") from e
        sock.settimeout(self.io_timeout)
        # Disable Nagle so 7-byte DMAWRITE commands ship immediately
        # instead of waiting for the kernel to coalesce — Nagle would
        # add ~40 ms of accidental latency on every write.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

        # If auth or identify fails after self._sock is assigned, close
        # and clear the socket so the next reconnect attempt starts from
        # a clean slate — otherwise we'd leave a half-open socket whose
        # next sendall might block on the unanswered IDENTIFY in the
        # server's per-connection FIFO.
        try:
            if self.password is not None:
                self._authenticate_locked()
            # IDENTIFY both validates the connection and captures the U64
            # product string for diagnostic logging.
            self.product = self._identify_locked()
        except Exception:
            self._close_locked()
            raise
        log.info("socket dma: connected to %s:%d (%s)", self.host, self.port, self.product)

    def _authenticate_locked(self) -> None:
        assert self._sock is not None
        assert self.password is not None
        payload = self.password.encode("utf-8")
        self._send_cmd_locked(CMD_AUTHENTICATE, payload)
        try:
            reply = self._recv_exact_locked(1)
        except OSError as e:
            raise SocketDMAError(
                "authentication failed — socket closed before reply. "
                "Server may have throttled too many bad attempts."
            ) from e
        if reply != b"\x01":
            raise SocketDMAError(
                "authentication rejected. Check [ultimate64] dma_password "
                "or the C64CAST_DMA_PASSWORD env var."
            )

    def _identify_locked(self) -> str:
        assert self._sock is not None
        self._send_cmd_locked(CMD_IDENTIFY, b"")
        try:
            length = self._recv_exact_locked(1)[0]
            payload = self._recv_exact_locked(length)
        except TimeoutError as e:
            # TCP accept succeeded but the server never answered IDENTIFY.
            # Most common cause: the U64's "Command Interface" toggle is OFF
            # (F2 → Network Settings → Command Interface → Enabled). That
            # toggle gates the DMA command dispatcher even when the listening
            # socket stays open. Password mismatch usually closes the socket
            # rather than hanging, but mention it as a secondary possibility.
            raise SocketDMAError(
                "no reply to IDENTIFY from the U64 Socket DMA service. "
                "Check that BOTH 'Ultimate DMA Service' AND 'Command "
                "Interface' are enabled in F2 → Network Settings. If a "
                "network password is set on the U64, also configure "
                "dma_password."
            ) from e
        except OSError as e:
            raise SocketDMAError(
                f"IDENTIFY round-trip failed: {e}. The DMA service may have "
                "closed the connection — check F2 → Network Settings → "
                "Ultimate DMA Service and Command Interface are both "
                "enabled."
            ) from e
        return payload.decode("utf-8", errors="replace")

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    # ---- low-level wire I/O ----------------------------------------------

    def _send_cmd_locked(self, opcode: int, payload: bytes) -> None:
        """Write one full command. Caller holds self._lock so commands
        don't interleave across threads."""
        assert self._sock is not None
        header = struct.pack("<HH", opcode, len(payload))
        self._sock.sendall(header + payload)

    def _recv_exact_locked(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed mid-read")
            buf.extend(chunk)
        return bytes(buf)

    def _send_with_reconnect(self, opcode: int, payload: bytes) -> None:
        """sendall + one transparent reconnect-and-retry on OSError. Used
        by the public command methods so a transient network blip or a
        U64 reboot doesn't crash the pipeline.

        If a previous reconnect attempt failed mid-handshake (auth or
        IDENTIFY), self._sock will be None — try to reconnect first
        before attempting the send."""
        with self._lock:
            t0 = time.perf_counter()
            if self._sock is None:
                # _connect_locked() raises SocketDMAError on its own failures;
                # we let that propagate.
                self._connect_locked()
            try:
                self._send_cmd_locked(opcode, payload)
            except OSError as e:
                log.warning("socket dma: send failed (%s) — reconnecting", e)
                self._close_locked()
                self._connect_locked()
                # Retry the original command exactly once.
                self._send_cmd_locked(opcode, payload)
            self._latencies.append(time.perf_counter() - t0)

    # ---- public command surface ------------------------------------------

    def dmawrite(self, addr: int, data: bytes) -> None:
        """Write ``data`` to C64 address ``addr`` via hardware DMA.

        ``addr`` is the C64 bus address (0x0000-0xFFFF). I/O space writes
        (e.g. ``0xD020``) take effect immediately at the VIC/SID. No
        response — the call returns as soon as the kernel has accepted
        the bytes for transmission; TCP backpressure provides natural
        rate limiting if the server can't keep up."""
        payload = struct.pack("<H", addr) + data
        self._send_with_reconnect(CMD_DMAWRITE, payload)

    def reuwrite(self, reu_offset: int, data: bytes) -> None:
        """Write ``data`` directly into FPGA-mapped REU SRAM at 24-bit
        ``reu_offset`` (0..0xFFFFFF). Unlike ``dmawrite()``, this path does
        NOT halt the C64 bus — the U64 firmware implements REUWRITE as a
        simple ``*(uint8_t *)(REU_MEMORY_BASE + offs) = buf[i]`` ARM-side
        memcpy. Use for bulk preload (audio buffers, large data tables) when
        the destination can be reached later via the REU's REC ($DF00-$DF0A)
        DMA mechanism. Requires REU to be enabled in F2 → C64 and Cartridge
        Settings on the U64."""
        addr_bytes = bytes([reu_offset & 0xFF, (reu_offset >> 8) & 0xFF, (reu_offset >> 16) & 0xFF])
        self._send_with_reconnect(CMD_REUWRITE, addr_bytes + data)

    def reset(self) -> None:
        """C64 reset. Provided for completeness; the higher-level
        Ultimate64API uses the REST reset endpoint instead because the
        sync semantics are simpler there (no DMA-then-disconnect race)."""
        self._send_with_reconnect(CMD_RESET, b"")

    def keyb(self, ascii_bytes: bytes) -> None:
        """Inject keystrokes into the kernal keyboard buffer ($0277) and
        set the count at $00C6. Equivalent to the REST + BASIC `RUN\\r`
        injection. Up to 10 bytes (the kernal buffer size); the server
        clamps."""
        self._send_with_reconnect(CMD_KEYB, ascii_bytes)

    def flush(self) -> None:
        """Wait for the server to drain every previously-issued command.

        Implementation: a single IDENTIFY round-trip. Because the server
        processes the per-connection command stream strictly in order
        (see socket_dma.cc inner ``while(1)``), the IDENTIFY reply
        arrives only after every prior DMAWRITE has been executed."""
        with self._lock:
            t0 = time.perf_counter()
            if self._sock is None:
                # A previous reconnect attempt failed mid-handshake; the
                # socket is gone. Re-establish it before sending IDENTIFY.
                self._connect_locked()
            try:
                self._send_cmd_locked(CMD_IDENTIFY, b"")
                length = self._recv_exact_locked(1)[0]
                self._recv_exact_locked(length)
            except OSError:
                # Don't transparently retry flush(): callers use it as a
                # sync barrier before a REST runner call; surfacing the
                # failure lets them decide whether to abort. The caller
                # owns the log message — duplicating it here would emit
                # two WARNINGs for the same event.
                raise
            self._latencies.append(time.perf_counter() - t0)

    # ---- diagnostics -----------------------------------------------------

    def latency_summary(self) -> tuple[float, float, float, float, int]:
        """``(avg, p50, p95, max, n)`` in seconds over the rolling window.
        Empty window returns all zeros."""
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
        """One-line summary for the profile-emit log line. Returns
        ``None`` when no samples have been recorded yet."""
        avg, p50, p95, mx, n = self.latency_summary()
        if n == 0:
            return None
        return (
            f"u64 dma latency: n={n} avg={avg * 1000:.1f} "
            f"p50={p50 * 1000:.1f} p95={p95 * 1000:.1f} "
            f"max={mx * 1000:.1f} ms"
        )
