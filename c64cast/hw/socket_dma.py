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
import math
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
# `#ifdef U64` in the firmware: these exist on an Ultimate 64 and not on an
# Ultimate II+, which has no VIC of its own to stream.
CMD_VICSTREAM_ON = 0xFF20
CMD_VICSTREAM_OFF = 0xFF30

#: The firmware's FreeRTOS tick, from its `configTICK_RATE_HZ` — the unit the
#: VIC stream's auto-stop duration is counted in. 5 ms, so the uint16 the
#: command carries tops out a little over five minutes.
STREAM_TICK_S = 1.0 / 200.0

#: keyb()'s client-side bound. The firmware's KEYB handler (socket_dma.cc)
#: does a raw DMA_RAW_WRITE at $0277 of the announced length with no clamp
#: of its own — a write past the kernal's 10-byte keyboard buffer reaches
#: $0291 (the case-switch flag) and beyond. Enforced here since nothing on
#: the wire does.
_KEYB_MAX_BYTES = 10

#: `_send_cmd_locked`'s wire header packs the payload length into a uint16;
#: a longer payload would raise a bare `struct.error` instead of the
#: `SocketDMAError` every caller of this module is documented to expect.
_MAX_COMMAND_PAYLOAD = 0xFFFF


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
    a second failure is raised to the caller.

    A rejected password is sticky: once the server refuses AUTHENTICATE,
    later writes stop trying to reconnect (and stop re-offering the
    cleartext credential to whatever now answers ``host:port``) until
    ``connect()`` is called again explicitly. ``close()`` is terminal the
    same way — a write after ``close()`` raises rather than silently
    opening a fresh connection nobody owns."""

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
        # under that lock. t0 is always taken right before the send that
        # actually goes out, never before a reconnect it might have needed
        # first, so a reconnect's connect+auth+IDENTIFY cost never lands in
        # this window.
        self._latencies: deque[float] = deque(maxlen=256)
        self.product = "(not yet identified)"
        # See the class docstring: both make an implicit reconnect (from
        # dmawrite/flush finding self._sock is None) refuse instead of
        # redialing. Cleared only by an explicit connect().
        self._auth_rejected = False
        self._closed = False

    # ---- connect / close --------------------------------------------------

    def connect(self) -> None:
        """Open the TCP socket and complete the handshake.

        Raises ``SocketDMAError`` on connection refused (service disabled
        on the U64), auth rejection, or unexpected IDENTIFY response.

        An explicit call — clears both the sticky auth-rejected state and
        the closed state described in the class docstring, so this is also
        how a caller retries after fixing the password or reopens after
        ``close()``."""
        with self._lock:
            self._closed = False
            self._auth_rejected = False
            self._connect_locked()

    def _reconnect_locked(self) -> None:
        """``_connect_locked()``, but refuses instead of redialing when the
        client was closed or a previous AUTHENTICATE was rejected — see the
        class docstring. Used by the implicit-reconnect paths (dmawrite /
        flush finding ``self._sock is None``); ``connect()`` calls
        ``_connect_locked()`` directly since it's the one place these
        states are meant to be cleared."""
        if self._closed:
            raise SocketDMAError("socket dma: client was closed; call connect() to reopen")
        if self._auth_rejected:
            raise SocketDMAError(
                "socket dma: authentication was rejected on a previous attempt "
                "and will not be retried automatically — fix [ultimate64] "
                "dma_password / C64CAST_DMA_PASSWORD and call connect() "
                "explicitly"
            )
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
        try:
            self._send_cmd_locked(CMD_AUTHENTICATE, payload)
            reply = self._recv_exact_locked(1)
        except OSError as e:
            raise SocketDMAError(
                "authentication failed — socket closed before reply. "
                "Server may have throttled too many bad attempts."
            ) from e
        if reply != b"\x01":
            self._auth_rejected = True
            raise SocketDMAError(
                "authentication rejected. Check [ultimate64] dma_password "
                "or the C64CAST_DMA_PASSWORD env var."
            )

    def _identify_locked(self) -> str:
        assert self._sock is not None
        try:
            self._send_cmd_locked(CMD_IDENTIFY, b"")
            length = self._recv_exact_locked(1)[0]
            payload = self._recv_exact_locked(length)
        except TimeoutError as e:
            # TCP accept succeeded but the server never answered IDENTIFY.
            # Most common cause: the U64's "Command Interface" toggle is OFF
            # (menu → F2 → Memory Configuration → Command Interface →
            # Enabled). That toggle gates the DMA command dispatcher even when
            # the listening socket stays open. Password mismatch usually closes
            # the socket rather than hanging, but mention it as a secondary
            # possibility.
            raise SocketDMAError(
                "no reply to IDENTIFY from the U64 Socket DMA service. "
                "Check that 'Ultimate DMA Service' (F2 → Network Settings) "
                "AND 'Command Interface' (F2 → Memory Configuration) are "
                "both enabled. If a network password is set on the U64, also "
                "configure dma_password."
            ) from e
        except OSError as e:
            raise SocketDMAError(
                f"IDENTIFY round-trip failed: {e}. The DMA service may have "
                "closed the connection — check that 'Ultimate DMA Service' "
                "(F2 → Network Settings) and 'Command Interface' (F2 → "
                "Memory Configuration) are both enabled."
            ) from e
        # The IDENTIFY payload is whatever answers on host:port, up to 255
        # bytes of it — filter to printable characters and cap the length so
        # it can't inject forged lines into --log-file output (this string
        # is logged verbatim below and again by callers).
        text = payload.decode("utf-8", errors="replace")
        return "".join(c for c in text if c.isprintable())[:64]

    def close(self) -> None:
        with self._lock:
            self._close_locked()
            self._closed = True

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
        if len(payload) > _MAX_COMMAND_PAYLOAD:
            raise SocketDMAError(
                f"command payload {len(payload)} bytes exceeds the "
                f"{_MAX_COMMAND_PAYLOAD}-byte wire length field"
            )
        header = struct.pack("<HH", opcode, len(payload))
        self._sock.sendall(header + payload)

    def _recv_exact_locked(self, n: int) -> bytes:
        """Read exactly `n` bytes, bounded by one `io_timeout` total rather
        than one per `recv()` call — a peer that dribbles the reply back
        slower than `io_timeout` but never idle for a full `io_timeout`
        would otherwise keep this loop (and the process-wide lock it runs
        under) spinning indefinitely."""
        assert self._sock is not None
        deadline = time.monotonic() + self.io_timeout
        buf = bytearray()
        try:
            while len(buf) < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for {n} bytes ({len(buf)} received)")
                self._sock.settimeout(remaining)
                chunk = self._sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("socket closed mid-read")
                buf.extend(chunk)
        finally:
            # Restore the socket's steady-state per-call timeout so the next
            # command's sendall doesn't inherit whatever sliver of time was
            # left on this read's cumulative deadline.
            self._sock.settimeout(self.io_timeout)
        return bytes(buf)

    def _send_with_reconnect(self, opcode: int, payload: bytes) -> None:
        """sendall + one transparent reconnect-and-retry on OSError. Used
        by the public command methods so a transient network blip or a
        U64 reboot doesn't crash the pipeline.

        If a previous reconnect attempt failed mid-handshake (auth or
        IDENTIFY), self._sock will be None — try to reconnect first
        before attempting the send. The first failure logs at debug: it
        self-heals here and never reaches the escalating failure ladder in
        backend.py's `_note_emit_failure`, so logging it at warning would be
        the *only* place that event is visible, at the wrong level. A
        failure that survives the retry is the one worth a warning, since
        by then the caller is about to see the exception anyway."""
        with self._lock:
            if self._sock is None:
                self._reconnect_locked()
            try:
                t0 = time.perf_counter()
                self._send_cmd_locked(opcode, payload)
            except OSError as e:
                log.debug("socket dma: send failed (%s) — reconnecting", e)
                self._close_locked()
                self._reconnect_locked()
                try:
                    # Retry the original command exactly once.
                    t0 = time.perf_counter()
                    self._send_cmd_locked(opcode, payload)
                except OSError as e2:
                    # Don't leave the retry's partially-sent command on a
                    # socket we're about to hand back to the caller as
                    # failed — the next command on it would be misframed.
                    self._close_locked()
                    log.warning(
                        "socket dma: send failed again after reconnect (%s) — giving up", e2
                    )
                    raise
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
        injection.

        Enforces the 10-byte kernal buffer bound client-side: the firmware
        does NOT clamp it (socket_dma.cc's KEYB handler is a raw
        DMA_RAW_WRITE at $0277 of the announced length; a write past 10
        bytes reaches $0291, the case-switch flag this module manages
        elsewhere, and beyond)."""
        if len(ascii_bytes) > _KEYB_MAX_BYTES:
            raise SocketDMAError(
                f"keyb() payload is {len(ascii_bytes)} bytes; the kernal "
                f"keyboard buffer holds at most {_KEYB_MAX_BYTES}"
            )
        self._send_with_reconnect(CMD_KEYB, ascii_bytes)

    def vicstream_on(self, destination: str, *, stop_after_s: float = 0.0) -> None:
        """Start the machine's own VIC stream to ``destination`` (``host:port``).

        The FPGA sends the composite pixel stream straight out of the Ethernet
        MAC as UDP — no C64 cycles, no bus contention, and nothing on the C64
        side that a running show could disturb. See
        :mod:`c64cast.hw.vic_stream` for the packet format.

        ``stop_after_s`` arms the firmware's own timer, which is why it is worth
        passing: this stream is a couple of megabytes a second, and a host that
        is SIGKILLed never gets to send the OFF. A watchdog that the *machine*
        counts down is the only kind that survives its listener dying, so
        callers re-arm it while somebody is still watching rather than asking
        for an unbounded stream. 0 (the default) means unbounded — pass an
        explicit positive value to actually bound the stream. A negative
        value raises rather than silently mapping onto the unbounded
        sentinel.

        Only an Ultimate 64 answers this (the firmware compiles it under
        ``#ifdef U64``); an Ultimate II+ has no VIC to stream and ignores the
        command, so the caller checks ``profile.supports_video_stream``."""
        if stop_after_s < 0:
            raise ValueError(f"stop_after_s must be >= 0, got {stop_after_s}")
        # max(1, ...) so any positive-but-sub-tick request (< 2.5 ms) rounds
        # up to the shortest bounded stream rather than down onto 0 — which
        # the firmware reads as unbounded, the exact opposite of the ask.
        ticks = 0 if stop_after_s == 0 else min(0xFFFF, max(1, round(stop_after_s / STREAM_TICK_S)))
        # The firmware NUL-terminates the name itself at the command length, so
        # the destination goes on the wire bare.
        payload = struct.pack("<H", ticks) + destination.encode("ascii")
        self._send_with_reconnect(CMD_VICSTREAM_ON, payload)

    def vicstream_off(self) -> None:
        """Stop the VIC stream. Idempotent — the firmware clears an already
        clear enable bit without complaint."""
        self._send_with_reconnect(CMD_VICSTREAM_OFF, b"")

    def flush(self) -> None:
        """Wait for the server to drain every previously-issued command.

        Implementation: a single IDENTIFY round-trip. Because the server
        processes the per-connection command stream strictly in order
        (see socket_dma.cc inner ``while(1)``), the IDENTIFY reply
        arrives only after every prior DMAWRITE has been executed."""
        with self._lock:
            if self._sock is None:
                # A previous reconnect attempt failed mid-handshake; the
                # socket is gone. Re-establish it before sending IDENTIFY.
                self._reconnect_locked()
            try:
                t0 = time.perf_counter()
                self._send_cmd_locked(CMD_IDENTIFY, b"")
                length = self._recv_exact_locked(1)[0]
                self._recv_exact_locked(length)
            except OSError:
                # Don't transparently retry flush(): callers use it as a
                # sync barrier before a REST runner call; surfacing the
                # failure lets them decide whether to abort. The caller
                # owns the log message — duplicating it here would emit
                # two WARNINGs for the same event. But an unconsumed
                # IDENTIFY reply may still be in flight on this socket (a
                # TimeoutError is an OSError subclass), and every other
                # round-trip in this class treats that as grounds to close:
                # otherwise the *next* flush()/command reads that stale
                # reply as its own, permanently one reply behind.
                self._close_locked()
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
        # Nearest-rank percentile: ceil(q*n) - 1, not int(q*n) — the latter
        # is one rank high for small n (at n=20 it puts p95 at index 19,
        # identical to max, in the first few seconds of a run).
        p50 = snap[min(n - 1, max(0, math.ceil(0.50 * n) - 1))]
        p95 = snap[min(n - 1, max(0, math.ceil(0.95 * n) - 1))]
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
