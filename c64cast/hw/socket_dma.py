"""Socket DMA client for the Ultimate 64.

The U64 firmware exposes a TCP server on port 64 speaking a small opcode
protocol — `<HH` opcode + length, then the payload — for direct DMA writes
into C64 address space, over one persistent connection. The server's
connection loop strictly serializes commands per connection, which is what
lets ``flush()`` work: a trailing IDENTIFY round-trip only responds once every
prior command has been processed.

Only the opcodes c64cast's write path needs are covered here (DMAWRITE,
REUWRITE, IDENTIFY, AUTHENTICATE, plus RESET, KEYB and the VIC stream pair).

The full set is in the firmware's own protocol table:
https://github.com/GideonZ/1541ultimate/blob/master/software/network/socket_dma.cc

For the transport measurements, see docs/caveats.md and
docs/architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import socket
import struct
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

DEFAULT_PORT = 64

CMD_KEYB = 0xFF03
CMD_RESET = 0xFF04
CMD_DMAWRITE = 0xFF06
CMD_REUWRITE = 0xFF07
CMD_IDENTIFY = 0xFF0E
CMD_AUTHENTICATE = 0xFF1F
# `#ifdef U64` in the firmware: Ultimate 64 only (a U2+ has no VIC to stream).
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

#: How long the connection may sit idle before the next command first proves
#: it is still open with an IDENTIFY round trip. From firmware 3.15 the
#: Ultimate closes a DMA connection that has sent it nothing for one second
#: (`socket_dma_set_timeouts` sets SO_RCVTIMEO, and a timed-out `recv` ends
#: the connection loop), and a command that crosses that close on the wire is
#: lost without an error. Half the timeout leaves the round trip, at about
#: 2.5 ms, a margin wider than any scheduling hiccup between check and send.
IDLE_VERIFY_AFTER_S = 0.5

#: How long after the last command a FIN must arrive to count as the server's
#: own orderly close. The server sends a FIN only once it has read every byte
#: sent to it; a command that crossed the close draws a reset one round trip
#: after it was sent, and the Ultimate is on the LAN, at about 2.5 ms per
#: IDENTIFY round trip, so by this point that reset would already be here.
FIN_SETTLE_S = 0.1

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
    opening a fresh connection nobody owns.

    Before each command the client checks that the server has not closed
    the connection: a non-blocking peek for a FIN or a reset on every
    command, and an IDENTIFY round trip when nothing has been sent for
    ``idle_verify_after_s``. Either finding redials before the command
    goes out, so a server that drops an idle connection (firmware 3.15+)
    costs a reconnect rather than the command. ``reconnect_count`` counts
    every implicit redial since construction.

    A redial for any other reason (a reset, an early FIN, a stray byte, an
    unanswered idle IDENTIFY, a failed send) abandons a connection whose
    unconfirmed commands may never have run, so the next ``flush()``
    raises ``ConnectionError`` once instead of reporting them drained."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        password: str | None = None,
        connect_timeout: float = 5.0,
        io_timeout: float = 2.0,
        idle_verify_after_s: float = IDLE_VERIFY_AFTER_S,
    ):
        self.host = host
        self.port = port
        self.password = password or None
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        self.idle_verify_after_s = idle_verify_after_s

        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        # Per-sendall latency window; 256 samples ≈ 5 s at 50 writes/s, which
        # matches the typical --profile-interval. Guarded by the socket's own
        # lock. t0 is taken right before the send that actually goes out, never
        # before a reconnect, so reconnect cost never lands in this window.
        self._latencies: deque[float] = deque(maxlen=256)
        self.product = "(not yet identified)"
        # Both make an implicit reconnect (from dmawrite/flush finding
        # self._sock is None) refuse instead of redialing; see the class
        # docstring. Cleared only by an explicit connect().
        self._auth_rejected = False
        self._closed = False
        # monotonic() of the last command the server is known to have
        # received or will receive before going idle; see _ensure_live_locked.
        self._last_send = 0.0
        # True while a command has gone out on this connection since the
        # last answered IDENTIFY; see _redial_locked.
        self._unconfirmed = False
        # Why a connection holding unconfirmed commands was abandoned in a
        # way that may have dropped them, until flush() reports it.
        self._maybe_lost: str | None = None
        self.reconnect_count = 0

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
        class docstring. Used by every implicit reconnect, and counts each
        one that succeeds in ``reconnect_count``; ``connect()`` calls
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
        self.reconnect_count += 1

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
        # No Nagle: it would add ~40 ms to every 7-byte DMAWRITE.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

        # A failure after self._sock is assigned must close and clear it: a
        # half-open socket's next sendall can block on the unanswered IDENTIFY
        # still in the server's per-connection FIFO.
        try:
            if self.password is not None:
                self._authenticate_locked()
            self.product = self._identify_locked()
        except Exception:
            self._close_locked()
            raise
        self._last_send = time.monotonic()
        self._unconfirmed = False
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

    def _identify_roundtrip_locked(self) -> bytes:
        """Send IDENTIFY and return the reply's payload."""
        self._send_cmd_locked(CMD_IDENTIFY, b"")
        length = self._recv_exact_locked(1)[0]
        return self._recv_exact_locked(length)

    def _identify_locked(self) -> str:
        assert self._sock is not None
        try:
            payload = self._identify_roundtrip_locked()
        except TimeoutError as e:
            # A TCP accept with no IDENTIFY reply is usually the U64's
            # "Command Interface" toggle being OFF: it gates the DMA command
            # dispatcher even while the listening socket stays open.
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
        # bytes of it, and is logged verbatim here and by callers — filter to
        # printable characters and cap it so it can't forge --log-file lines.
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
            # Restore the steady-state per-call timeout so the next command's
            # sendall doesn't inherit this read's remaining deadline.
            self._sock.settimeout(self.io_timeout)
        return bytes(buf)

    def _redial_locked(self, reason: str, *, benign: bool = False) -> None:
        """Close the current socket and open a fresh one.

        Unless ``benign``, commands sent on the old connection since its
        last answered IDENTIFY may never have run, and the next ``flush()``
        raises once for them; the round trips on the new connection say
        nothing about the old one."""
        log.debug("socket dma: %s — reconnecting", reason)
        if self._unconfirmed and not benign and self._maybe_lost is None:
            self._maybe_lost = reason
        self._close_locked()
        self._reconnect_locked()

    def _peer_gone_locked(self) -> tuple[str, bool] | None:
        """``(why, benign)`` when the server can no longer be reached on
        this socket, or ``None`` if the kernel has seen nothing from it.

        A zero-timeout ``MSG_PEEK``: a pending FIN reads as ``b""`` and a
        reset raises, and both mean the next command would be lost or
        refused. The server sends nothing unprompted, so a pending byte
        means a reply this client never read; the stream is out of step
        and is redialed too.

        Only a FIN seen ``FIN_SETTLE_S`` or more after the last command is
        ``benign``: the server closed after reading every byte sent to it,
        as the firmware 3.15 idle close does, and any reset a lost command
        drew would have arrived by then. A reset means the server discarded
        bytes this client sent; an earlier FIN may still be followed by
        one. A reset that follows a FIN peeks as ``b""`` on Linux, which
        checks for the FIN before the error, so ``SO_ERROR`` is read too."""
        assert self._sock is not None
        self._sock.settimeout(0.0)
        try:
            pending = self._sock.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            return None
        except OSError as e:
            return f"connection reset while idle ({e})", False
        finally:
            self._sock.settimeout(self.io_timeout)
        if not pending:
            err = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err:
                return f"connection reset after the server closed it ({os.strerror(err)})", False
            quiet_for = time.monotonic() - self._last_send
            return "server closed the connection", quiet_for >= FIN_SETTLE_S
        log.warning("socket dma: unsolicited byte %r from the server — redialing", pending)
        return "unsolicited data on the connection", False

    def _idle_check_locked(self) -> str | None:
        """An IDENTIFY round trip; ``None`` if it was answered.

        Sent only after ``idle_verify_after_s`` of silence, where the server
        may close the connection while a command is in flight. A reply
        proves the connection is open and restarts the server's idle timer,
        so the command that follows has the whole timeout to arrive."""
        try:
            self._identify_roundtrip_locked()
        except OSError as e:
            return f"idle connection did not answer IDENTIFY ({e})"
        self._last_send = time.monotonic()
        self._unconfirmed = False
        return None

    def _ensure_live_locked(self) -> None:
        """Leave ``self._sock`` on a connection the next command will reach,
        redialing when it is missing or the server has closed it."""
        if self._sock is None:
            # A previous reconnect attempt failed mid-handshake.
            self._reconnect_locked()
            return
        gone = self._peer_gone_locked()
        if gone is None and time.monotonic() - self._last_send >= self.idle_verify_after_s:
            reason = self._idle_check_locked()
            if reason is not None:
                gone = reason, False
        if gone is not None:
            self._redial_locked(gone[0], benign=gone[1])

    def _send_with_reconnect(self, opcode: int, payload: bytes) -> None:
        """sendall + one transparent reconnect-and-retry on OSError. Used
        by the public command methods so a transient network blip or a
        U64 reboot doesn't crash the pipeline.

        ``_ensure_live_locked`` runs first, so a connection the server
        has already closed is redialed before the command rather than
        losing it. The first failure logs at debug: it
        self-heals here and never reaches the escalating failure ladder in
        backend.py's `_note_emit_failure`, so logging it at warning would be
        the *only* place that event is visible, at the wrong level. A
        failure that survives the retry is the one worth a warning, since
        by then the caller is about to see the exception anyway."""
        with self._lock:
            self._ensure_live_locked()
            try:
                t0 = time.perf_counter()
                self._send_cmd_locked(opcode, payload)
            except OSError as e:
                self._redial_locked(f"send failed ({e})")
                try:
                    t0 = time.perf_counter()
                    self._send_cmd_locked(opcode, payload)
                except OSError as e2:
                    # A partially-sent command left on the socket would
                    # misframe the next one.
                    self._close_locked()
                    log.warning(
                        "socket dma: send failed again after reconnect (%s) — giving up", e2
                    )
                    raise
            self._latencies.append(time.perf_counter() - t0)
            self._last_send = time.monotonic()
            self._unconfirmed = True

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
        # max(1, ...): a positive sub-tick request must round up, not down onto
        # 0, which the firmware reads as unbounded.
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
        arrives only after every prior DMAWRITE has been executed.

        A connection redialed since the last ``flush()`` may have taken
        commands with it (see ``_redial_locked``); then this raises
        ``ConnectionError`` instead, once, without sending the IDENTIFY.
        Any raise from here answers for the commands issued before it, so
        the pending loss is cleared whichever way this call fails."""
        with self._lock:
            try:
                self._flush_locked()
            finally:
                self._maybe_lost = None

    def _flush_locked(self) -> None:
        self._ensure_live_locked()
        if self._maybe_lost is not None:
            raise ConnectionError(
                f"socket dma: {self._maybe_lost}; commands sent before the "
                "reconnect may not have reached the server"
            )
        try:
            t0 = time.perf_counter()
            self._identify_roundtrip_locked()
        except OSError:
            # No transparent retry: callers use flush() as a sync barrier
            # before a REST runner and own the log message. Close, though —
            # an unconsumed IDENTIFY reply may still be in flight (a
            # TimeoutError is an OSError), and the next flush()/command
            # would read it as its own, permanently one reply behind.
            self._close_locked()
            raise
        self._latencies.append(time.perf_counter() - t0)
        self._last_send = time.monotonic()
        self._unconfirmed = False

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
        """One-line summary for the profile-emit log line, with the
        cumulative ``reconnect_count``. Returns ``None`` when no samples
        have been recorded yet."""
        avg, p50, p95, mx, n = self.latency_summary()
        if n == 0:
            return None
        return (
            f"u64 dma latency: n={n} avg={avg * 1000:.1f} "
            f"p50={p50 * 1000:.1f} p95={p95 * 1000:.1f} "
            f"max={mx * 1000:.1f} ms reconnects={self.reconnect_count}"
        )
