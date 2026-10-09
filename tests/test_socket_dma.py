"""Tests for the Socket DMA client.

A small in-process FakeSocket replaces `socket.create_connection` so we
can drive the protocol without an actual U64 on the network. The fake
records every sendall byte for wire-format assertions and serves a
scripted sequence of recv replies for round-trip flows (IDENTIFY,
AUTHENTICATE)."""

from __future__ import annotations

import contextlib
import errno
import logging
import socket
import struct
import threading
import time
import unittest
from collections import deque
from collections.abc import Iterator
from unittest.mock import patch

from c64cast.hw.backend import LinkError
from c64cast.hw.socket_dma import (
    CMD_AUTHENTICATE,
    CMD_DMAWRITE,
    CMD_IDENTIFY,
    CMD_KEYB,
    CMD_RESET,
    CMD_REUWRITE,
    CommandsMayBeLostError,
    SocketDMAClient,
    SocketDMAError,
)

_IDENT_REPLY = b"\x16*** Ultimate 64-II ***"  # 0x16 = 22 = len(payload)


class FakeSocket:
    """Stand-in for a real TCP socket. sendall accumulates bytes into
    `sent`; recv pops from a scripted `replies` deque (each entry is a
    bytes blob; recv returns up to the requested length). Setting
    `fail_sendalls_remaining` causes the next N sendalls to raise
    BrokenPipeError before succeeding — used to test the
    reconnect-and-retry path.

    A non-blocking ``MSG_PEEK`` (the client's per-command liveness check)
    reports only what the test sets: ``peer_closed`` reads as a FIN,
    ``peer_reset`` raises, ``unsolicited`` is a pending byte, and otherwise
    nothing is pending. ``so_error`` is what ``SO_ERROR`` reads, so a FIN
    with it set is a reset that arrived after the FIN, as Linux reports it. Scripted replies stay invisible to it, since the
    fake has no notion of a reply arriving only after its request."""

    def __init__(self, replies: list[bytes] | None = None):
        self.sent = bytearray()
        # Replies are returned in order; each FakeSocket instance scripts
        # one connection's worth of responses.
        self._replies: deque[bytes] = deque(replies or [])
        self.fail_sendalls_remaining = 0
        self.closed = False
        self.timeout = None
        self.sockopts: list[tuple] = []
        self.peer_closed = False
        self.peer_reset = False
        self.so_error = 0
        self.unsolicited = b""

    def settimeout(self, t):
        self.timeout = t

    def setsockopt(self, level, opt, val):
        self.sockopts.append((level, opt, val))

    def getsockopt(self, level, opt):
        assert (level, opt) == (socket.SOL_SOCKET, socket.SO_ERROR)
        return self.so_error

    def sendall(self, data: bytes) -> None:
        if self.fail_sendalls_remaining > 0:
            self.fail_sendalls_remaining -= 1
            raise BrokenPipeError("scripted failure")
        self.sent.extend(data)

    def recv(self, n: int, flags: int = 0) -> bytes:
        if flags & socket.MSG_PEEK:
            assert self.timeout == 0.0, "the liveness peek must not block"
            if self.peer_reset:
                raise ConnectionResetError("scripted reset")
            if self.peer_closed:
                return b""
            if self.unsolicited:
                return self.unsolicited[:n]
            raise BlockingIOError("nothing pending")
        if not self._replies:
            return b""
        head = self._replies[0]
        if len(head) <= n:
            self._replies.popleft()
            return head
        out, self._replies[0] = head[:n], head[n:]
        return out

    def shutdown(self, _how) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _client_with(
    fake: FakeSocket, *, password: str | None = None, connect: bool = True
) -> SocketDMAClient:
    """Build a client whose `socket.create_connection` returns `fake`.
    Set connect=False if the test wants to drive the connect() flow itself."""
    c = SocketDMAClient("test-host", port=64, password=password)
    if connect:
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake):
            c.connect()
    return c


class ConnectAndIdentifyTest(unittest.TestCase):
    def test_connect_without_password_sends_identify_only(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        # No AUTHENTICATE — first 4 bytes are the IDENTIFY command header.
        self.assertEqual(fake.sent[:4], struct.pack("<HH", CMD_IDENTIFY, 0))
        self.assertEqual(c.product, "*** Ultimate 64-II ***")

    def test_connect_refused_raises_socketdmaerror(self):
        c = SocketDMAClient("test-host", port=64)
        with patch(
            "c64cast.hw.socket_dma.socket.create_connection", side_effect=ConnectionRefusedError()
        ):
            with self.assertRaises(SocketDMAError) as ctx:
                c.connect()
        self.assertIn("Ultimate DMA Service", str(ctx.exception))

    def test_connect_with_password_sends_authenticate_first(self):
        # Reply: AUTHENTICATE ack (0x01) then IDENTIFY length+payload.
        fake = FakeSocket([b"\x01", _IDENT_REPLY])
        c = _client_with(fake, password="hunter2")
        auth_header = struct.pack("<HH", CMD_AUTHENTICATE, len("hunter2"))
        self.assertEqual(fake.sent[:4], auth_header)
        self.assertEqual(bytes(fake.sent[4:11]), b"hunter2")
        ident_header = struct.pack("<HH", CMD_IDENTIFY, 0)
        self.assertEqual(fake.sent[11:15], ident_header)
        self.assertEqual(c.product, "*** Ultimate 64-II ***")

    def test_auth_rejected_raises(self):
        fake = FakeSocket([b"\x00"])  # 0 = rejected
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake):
            c = SocketDMAClient("test-host", port=64, password="wrong")
            with self.assertRaises(SocketDMAError) as ctx:
                c.connect()
        self.assertIn("authentication rejected", str(ctx.exception))

    def test_password_that_is_not_utf8_raises_without_echoing_it(self):
        # A non-UTF-8 byte in C64CAST_DMA_PASSWORD reaches os.environ as a
        # lone surrogate (surrogateescape on POSIX).
        fake = FakeSocket([b"\x01", _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake):
            c = SocketDMAClient("test-host", port=64, password="h\udce4nter2")
            with self.assertRaises(SocketDMAError) as ctx:
                c.connect()
        message = str(ctx.exception)
        self.assertNotIn("\udce4", message)
        self.assertNotIn("position", message)
        self.assertNotIn("nter2", message)
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertEqual(bytes(fake.sent), b"")
        self.assertTrue(fake.closed)

    def test_empty_password_treated_as_none(self):
        # password="" should NOT trigger AUTHENTICATE — same as None.
        fake = FakeSocket([_IDENT_REPLY])
        _client_with(fake, password="")
        self.assertEqual(fake.sent[:4], struct.pack("<HH", CMD_IDENTIFY, 0))


class WireEncodingTest(unittest.TestCase):
    """Spot-check the exact bytes on the wire for each command type.
    Regressions here would silently corrupt every U64 write."""

    def setUp(self):
        self.fake = FakeSocket([_IDENT_REPLY])
        self.client = _client_with(self.fake)
        # Drop the connect-time IDENTIFY bytes so assertions start at the command
        # each test issues.
        self.connect_len = len(self.fake.sent)

    def _new(self) -> bytes:
        return bytes(self.fake.sent[self.connect_len :])

    def test_dmawrite_border_color(self):
        # The exact bytes a $D020 border write to color $0E should produce.
        self.client.dmawrite(0xD020, b"\x0e")
        self.assertEqual(
            self._new(),
            b"\x06\xff\x03\x00\x20\xd0\x0e",
            "DMAWRITE bytes don't match — wire format regression!",
        )

    def test_dmawrite_multi_byte_payload(self):
        # Multi-byte payload → length field includes addr (2) + data.
        self.client.dmawrite(0x0400, b"ABC")
        expected = (
            struct.pack("<HH", CMD_DMAWRITE, 5)  # 2 addr + 3 data
            + struct.pack("<H", 0x0400)
            + b"ABC"
        )
        self.assertEqual(self._new(), expected)

    def test_reset_encoding(self):
        self.client.reset()
        self.assertEqual(self._new(), struct.pack("<HH", CMD_RESET, 0))

    def test_keyb_encoding(self):
        self.client.keyb(b"RUN\r")
        expected = struct.pack("<HH", CMD_KEYB, 4) + b"RUN\r"
        self.assertEqual(self._new(), expected)

    def test_keyb_over_ten_bytes_raises_without_touching_the_wire(self):
        # The firmware does NOT clamp this (see the docstring) — the client
        # must, or a write past the kernal buffer reaches $0291 and beyond.
        with self.assertRaisesRegex(SocketDMAError, "10"):
            self.client.keyb(b"01234567890")
        self.assertEqual(self._new(), b"")

    def test_reuwrite_encoding(self):
        # REUWRITE carries a 24-bit little-endian REU offset (3 bytes, not the
        # 16-bit C64 address DMAWRITE uses) before the data. Every REU preload rides
        # on it, but elsewhere only FakeSocketDMA sees the call, never the bytes.
        self.client.reuwrite(0x012345, b"\xaa\xbb")
        expected = (
            struct.pack("<HH", CMD_REUWRITE, 5)  # 3 offset + 2 data
            + b"\x45\x23\x01"  # 0x012345 little-endian, 24-bit
            + b"\xaa\xbb"
        )
        self.assertEqual(
            self._new(),
            expected,
            "REUWRITE bytes don't match — wire format regression!",
        )

    def test_reuwrite_offset_uses_all_24_bits(self):
        # The top byte must survive: a 16-bit truncation would alias every
        # REU bank onto the first 64 KiB and silently corrupt the preload.
        self.client.reuwrite(0xFEDCBA, b"\x01")
        expected = struct.pack("<HH", CMD_REUWRITE, 4) + b"\xba\xdc\xfe" + b"\x01"
        self.assertEqual(self._new(), expected)


class FlushTest(unittest.TestCase):
    def test_flush_issues_identify_roundtrip(self):
        # Two IDENTIFY replies: one for connect, one for flush.
        fake = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        c = _client_with(fake)
        before = len(fake.sent)
        c.flush()
        flushed = bytes(fake.sent[before:])
        self.assertEqual(flushed, struct.pack("<HH", CMD_IDENTIFY, 0))

    def test_flush_timeout_closes_the_socket_instead_of_leaving_it_desynced(self):
        # TimeoutError is an OSError subclass, so flush()'s except arm re-raised
        # with self._sock still assigned and an IDENTIFY reply possibly in flight —
        # the next flush read that stale reply as its own, permanently one behind.
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)

        def _timed_out_recv(n, flags=0):
            if flags & socket.MSG_PEEK:
                raise BlockingIOError("nothing pending")
            raise TimeoutError("timed out")

        fake.recv = _timed_out_recv  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            c.flush()
        self.assertIsNone(c._sock)


class VicstreamOnValidationTest(unittest.TestCase):
    """vicstream_on's watchdog encoding: 0 is the documented 'unbounded'
    sentinel, so rounding must never land there by accident."""

    def setUp(self):
        self.fake = FakeSocket([_IDENT_REPLY])
        self.client = _client_with(self.fake)
        self.connect_len = len(self.fake.sent)

    def _ticks(self) -> int:
        (ticks,) = struct.unpack(
            "<H", bytes(self.fake.sent[self.connect_len + 4 : self.connect_len + 6])
        )
        return ticks

    def test_zero_stays_the_unbounded_sentinel(self):
        self.client.vicstream_on("1.2.3.4:9", stop_after_s=0.0)
        self.assertEqual(self._ticks(), 0)

    def test_sub_tick_duration_rounds_up_to_one_tick_not_down_to_unbounded(self):
        self.client.vicstream_on("1.2.3.4:9", stop_after_s=0.001)
        self.assertEqual(self._ticks(), 1)

    def test_negative_duration_raises_rather_than_silently_going_unbounded(self):
        with self.assertRaises(ValueError):
            self.client.vicstream_on("1.2.3.4:9", stop_after_s=-1.0)
        self.assertEqual(bytes(self.fake.sent[self.connect_len :]), b"")


class ClosedAndAuthRejectedTest(unittest.TestCase):
    """close() and a rejected password are both terminal: a later write
    must not silently re-dial (and, for a rejected password, re-offer the
    cleartext credential) — it should raise until connect() is called."""

    def test_write_after_close_raises_instead_of_reopening(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        c.close()
        with self.assertRaises(SocketDMAError):
            c.dmawrite(0xD020, b"\x0e")

    def test_connect_after_close_reopens_normally(self):
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        c.close()
        fake2 = FakeSocket([_IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            c.connect()
        c.dmawrite(0xD020, b"\x0e")  # does not raise

    def test_rejected_auth_is_not_retried_on_the_next_write(self):
        fake1 = FakeSocket([b"\x00"])  # AUTHENTICATE rejected
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake1):
            c = SocketDMAClient("test-host", port=64, password="wrong")
            with self.assertRaises(SocketDMAError):
                c.connect()
        # A second connect() attempt (e.g. from a real reconnect elsewhere)
        # would re-offer the same cleartext password — the next implicit
        # write must refuse instead of trying.
        with patch("c64cast.hw.socket_dma.socket.create_connection") as create:
            with self.assertRaisesRegex(SocketDMAError, "not be retried"):
                c.dmawrite(0xD020, b"\x0e")
        create.assert_not_called()

    def test_explicit_connect_clears_the_rejected_auth_state(self):
        fake1 = FakeSocket([b"\x00"])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake1):
            c = SocketDMAClient("test-host", port=64, password="wrong")
            with self.assertRaises(SocketDMAError):
                c.connect()
        fake2 = FakeSocket([b"\x01", _IDENT_REPLY])  # correct password this time
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            c.connect()
        c.dmawrite(0xD020, b"\x0e")  # does not raise


class WireBoundsTest(unittest.TestCase):
    def test_oversized_payload_raises_socketdmaerror_not_struct_error(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        with self.assertRaises(SocketDMAError):
            c.dmawrite(0x0400, b"\x00" * 70000)


class IdentifySanitizationTest(unittest.TestCase):
    def test_control_bytes_are_stripped_and_length_is_capped(self):
        dirty = "X" * 80 + "\n\r\x1b[31mFAKE ERROR"
        reply = dirty.encode("utf-8")
        fake = FakeSocket([bytes([len(reply)]) + reply])
        c = _client_with(fake)
        self.assertNotIn("\n", c.product)
        self.assertNotIn("\r", c.product)
        self.assertLessEqual(len(c.product), 64)


class CumulativeReadDeadlineTest(unittest.TestCase):
    def test_a_dribbling_peer_times_out_after_one_io_timeout_total_not_per_byte(self):
        # Each individual recv() arrives well inside io_timeout, but the reply as a
        # whole never finishes — the read must give up after one cumulative
        # io_timeout rather than resetting the clock on every byte.
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        c.io_timeout = 0.05

        class DribblingSocket(FakeSocket):
            # Every recv() lands inside io_timeout, so a per-recv timeout alone
            # never fires, but each delivers one byte and the sequence overruns.
            def recv(self, n, flags=0):
                if flags & socket.MSG_PEEK:
                    return super().recv(n, flags)
                time.sleep(0.02)
                return b"\x05"

        # Swap the already-connected socket directly — flush() only
        # reconnects when self._sock is None, and it isn't here.
        c._sock = DribblingSocket([])  # type: ignore[assignment]
        with self.assertRaises(TimeoutError):
            c.flush()


class ReconnectTest(unittest.TestCase):
    def test_dmawrite_reconnects_on_broken_pipe(self):
        # connect: serves IDENTIFY. First sendall after connect fails;
        # reconnect serves IDENTIFY again; retry succeeds.
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.fail_sendalls_remaining = 1  # the next sendall will throw

        fake2 = FakeSocket([_IDENT_REPLY])
        # The reconnect path logs at debug (it self-heals here, so it never
        # reaches backend.py's escalating failure ladder) — capture it (so
        # it doesn't spam stderr) and verify the expected message.
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG") as cap:
                c.dmawrite(0xD020, b"\x0e")
        self.assertTrue(
            any("send failed (scripted failure) — reconnecting" in line for line in cap.output),
            f"expected reconnect-debug log, got: {cap.output!r}",
        )

        # fake1's failed sendall didn't append anything.
        self.assertEqual(len(fake1.sent), struct.pack("<HH", CMD_IDENTIFY, 0).__len__())
        # fake2 received the IDENTIFY (re-handshake) AND the retried DMAWRITE.
        self.assertIn(b"\x06\xff\x03\x00\x20\xd0\x0e", bytes(fake2.sent))
        self.assertTrue(fake1.closed)

    def test_second_failure_propagates(self):
        # The original sendall fails, and so does the reconnect's own handshake
        # (fake2's first sendall is its IDENTIFY, not the retried DMAWRITE) — that
        # surfaces as SocketDMAError, not a raw OSError past connect()'s contract.
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.fail_sendalls_remaining = 1

        fake2 = FakeSocket([_IDENT_REPLY])
        fake2.fail_sendalls_remaining = 1
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG") as cap:
                with self.assertRaises(SocketDMAError):
                    c.dmawrite(0xD020, b"\x0e")
        self.assertTrue(
            any("send failed (scripted failure) — reconnecting" in line for line in cap.output),
            f"expected reconnect-debug log, got: {cap.output!r}",
        )

    def test_second_failure_on_the_retried_command_itself_closes_the_socket(self):
        # Reconnect succeeds, but the retried DMAWRITE (not the handshake)
        # fails too — the socket must be closed rather than left assigned
        # mid-command, or the next write on it would be misframed.
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.fail_sendalls_remaining = 1

        # Reconnect's own IDENTIFY succeeds; the retried DMAWRITE is the
        # *second* sendall on fake2, so let the first (IDENTIFY) through.
        fake2 = FakeSocket([_IDENT_REPLY])

        class FailSecondSendSocket(FakeSocket):
            def __init__(self, replies):
                super().__init__(replies)
                self._sends = 0

            def sendall(self, data: bytes) -> None:
                self._sends += 1
                if self._sends == 2:
                    raise BrokenPipeError("scripted failure")
                super().sendall(data)

        fake2 = FailSecondSendSocket([_IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                with self.assertRaises(SocketDMAError) as raised:
                    c.dmawrite(0xD020, b"\x0e")
        # A link error, not a bare OSError: the render loop skips a frame on
        # one and would end the scene on the other.
        self.assertIsInstance(raised.exception, LinkError)
        self.assertIsInstance(raised.exception.__cause__, BrokenPipeError)
        self.assertIsNone(c._sock)
        self.assertTrue(fake2.closed)

    def test_reconnect_identify_timeout_clears_socket_and_next_call_reconnects(self):
        # Repro of the production crash: a send times out, reconnect succeeds at the
        # TCP layer but the U64 never answers the post-handshake IDENTIFY (Command
        # Interface stalled). The first dmawrite raises SocketDMAError and clears
        # self._sock, so the next one reconnects fresh instead of blocking.
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.fail_sendalls_remaining = 1  # provoke reconnect

        # Reconnect TCP succeeds but recv hangs. TimeoutError matches the real
        # failure mode; returning b"" would raise ConnectionError instead.
        class TimeoutOnRecvSocket(FakeSocket):
            def recv(self, n):
                raise TimeoutError("timed out")

        fake2 = TimeoutOnRecvSocket([])

        # Reconnect #2 (for the next dmawrite): clean IDENTIFY this time.
        fake3 = FakeSocket([_IDENT_REPLY])

        with patch("c64cast.hw.socket_dma.socket.create_connection", side_effect=[fake2, fake3]):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                with self.assertRaises(SocketDMAError):
                    c.dmawrite(0xD020, b"\x0e")
            # The half-open socket must be cleaned up, or the next call trips
            # `assert self._sock is not None` or blocks on the unanswered IDENTIFY.
            self.assertIsNone(c._sock)
            self.assertTrue(fake2.closed)

            _expire_redial_backoff(c)
            c.dmawrite(0xD020, b"\x0e")
        self.assertIn(b"\x06\xff\x03\x00\x20\xd0\x0e", bytes(fake3.sent))
        self.assertEqual(c.reconnect_count, 1)  # the failed handshake is not counted


class ThreadSafetyTest(unittest.TestCase):
    def test_two_threads_dont_interleave_commands(self):
        # Without the lock held across sendall, two threads could write half of one
        # command and half of another; with it, the stream decomposes cleanly.
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)

        N_PER_THREAD = 50
        N_THREADS = 4

        def burst(thread_idx: int):
            for i in range(N_PER_THREAD):
                # A 4-byte payload identifiable per thread, so ordering is auditable.
                c.dmawrite(0xC800, bytes([thread_idx, i & 0xFF, 0xAA, 0x55]))

        threads = [threading.Thread(target=burst, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stream = bytes(fake.sent)
        # Skip the connect-time IDENTIFY (4 bytes header + 0 payload).
        i = 4
        parsed = 0
        while i < len(stream):
            opcode, length = struct.unpack("<HH", stream[i : i + 4])
            i += 4
            self.assertEqual(opcode, CMD_DMAWRITE)
            self.assertEqual(length, 6)  # 2 addr + 4 data
            i += length
            parsed += 1
        self.assertEqual(i, len(stream), "wire bytes don't end on a command boundary")
        self.assertEqual(parsed, N_PER_THREAD * N_THREADS)


class LatencyTest(unittest.TestCase):
    def test_latency_summary_empty(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        # Connect's IDENTIFY round-trip went through _identify_locked,
        # which doesn't touch _latencies; so the window is empty here.
        self.assertEqual(c.latency_summary(), (0.0, 0.0, 0.0, 0.0, 0))
        self.assertIsNone(c.format_latency())

    def test_latency_summary_populates(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        # Seed the rolling window directly — this exercises the math, not real
        # wall-clock sendall costs.
        for v in [0.001, 0.002, 0.003, 0.004, 0.005]:
            c._latencies.append(v)
        avg, p50, p95, mx, n = c.latency_summary()
        self.assertEqual(n, 5)
        self.assertAlmostEqual(avg, 0.003)
        self.assertEqual(mx, 0.005)

    def test_format_latency_includes_expected_tokens(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        for _ in range(3):
            c._latencies.append(0.005)
        line = c.format_latency()
        self.assertIsNotNone(line)
        assert line is not None
        for token in ("u64 dma latency", "n=3", "avg=5.0", "p50=5.0", "max=5.0", "ms"):
            self.assertIn(token, line)

    def test_dmawrite_records_latency(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        t0 = time.perf_counter()
        c.dmawrite(0xD020, b"\x0e")
        self.assertGreater(c.latency_summary()[4], 0)  # n > 0
        avg = c.latency_summary()[0]
        self.assertGreaterEqual(avg, 0.0)
        self.assertLess(avg, time.perf_counter() - t0 + 0.1)


def _read_exact_or_none(conn: socket.socket, n: int, stop: threading.Event) -> bytes | None:
    """Read ``n`` bytes from a 50 ms-polled socket; ``None`` on EOF or stop."""
    buf = bytearray()
    while len(buf) < n:
        if stop.is_set():
            return None
        try:
            chunk = conn.recv(n - len(buf))
        except TimeoutError:
            continue
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _expire_redial_backoff(c: SocketDMAClient) -> None:
    """Let the next implicit redial dial, as if the backoff after a failed
    one had run out; see RedialBackoffTest."""
    c._redial_not_before = 0.0


class _LoopbackDMAServer:
    """A loopback stand-in for the firmware's DMA task, so the liveness
    checks meet a real kernel's FIN and RST rather than a scripted fake.

    One connection at a time, as the firmware serves them; IDENTIFY is
    answered and DMAWRITEs are recorded. With ``idle_close_s`` set, a
    connection that has sent nothing for that long is closed, as firmware
    3.15a does after one second; ``None`` keeps it open, as 3.14e and C64
    Ultimate 1.1.0 do. ``idle_closed`` is set each time that happens.

    With ``drop_after_writes`` set, the first connection is closed once that
    many DMAWRITEs have run and the next command is waiting unread, so the
    kernel answers with a reset and that command is lost, as when the
    service is switched off or the machine restarts. ``dropped`` is set
    when that happens."""

    def __init__(self, idle_close_s: float | None, drop_after_writes: int | None = None):
        self.idle_close_s = idle_close_s
        self.drop_after_writes = drop_after_writes
        self.dropped = threading.Event()
        self.writes: list[tuple[int, bytes]] = []
        self.accepted = 0
        self.idle_closed = threading.Event()
        self._stop = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.05)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="LoopbackDMAServer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._listener.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            self.accepted += 1
            with conn:
                self._serve(conn)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(0.05)
        last = time.monotonic()
        while not self._stop.is_set():
            if self.idle_close_s is not None and time.monotonic() - last >= self.idle_close_s:
                conn.close()
                self.idle_closed.set()
                return
            try:
                first = conn.recv(1)
            except TimeoutError:
                continue
            if not first:
                return
            rest = _read_exact_or_none(conn, 3, self._stop)
            if rest is None:
                return
            opcode, length = struct.unpack("<HH", first + rest)
            payload = _read_exact_or_none(conn, length, self._stop) if length else b""
            if payload is None:
                return
            if opcode == CMD_IDENTIFY:
                conn.sendall(_IDENT_REPLY)
            elif opcode == CMD_DMAWRITE:
                self.writes.append((struct.unpack("<H", payload[:2])[0], payload[2:]))
                if len(self.writes) == self.drop_after_writes and not self.dropped.is_set():
                    self._close_with_the_next_command_unread(conn)
                    return
            last = time.monotonic()

    def _close_with_the_next_command_unread(self, conn: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn.recv(1, socket.MSG_PEEK)
            except TimeoutError:
                continue
            break
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()
        self.dropped.set()


class IdleCloseTest(unittest.TestCase):
    """c64cast#520: firmware 3.15 closes a DMA connection idle for one
    second, and the first command after that used to vanish — the kernel
    accepted the bytes and the Ultimate answered them with a reset."""

    def _serve(self, idle_close_s: float | None) -> _LoopbackDMAServer:
        server = _LoopbackDMAServer(idle_close_s)
        self.addCleanup(server.stop)
        return server

    def _client(self, server: _LoopbackDMAServer, idle_verify_after_s: float) -> SocketDMAClient:
        c = SocketDMAClient("127.0.0.1", port=server.port, idle_verify_after_s=idle_verify_after_s)
        c.connect()
        self.addCleanup(c.close)
        return c

    def _connect_quietly(self, server, idle_verify_after_s):
        with self.assertLogs("c64cast.hw.socket_dma", level="INFO"):
            return self._client(server, idle_verify_after_s)

    def test_the_first_write_after_an_idle_close_arrives(self):
        # The verify threshold sits past the gap, so only the FIN peek can
        # catch the close — remove it and the second write is lost.
        server = self._serve(idle_close_s=0.2)
        c = self._connect_quietly(server, idle_verify_after_s=60.0)
        c.dmawrite(0x0400, b"\x01")
        self.assertTrue(server.idle_closed.wait(2.0))
        time.sleep(0.05)  # let the FIN reach this side of the loopback
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG") as cap:
            c.dmawrite(0x0401, b"\x02")
            c.flush()
        self.assertEqual(server.writes, [(0x0400, b"\x01"), (0x0401, b"\x02")])
        self.assertEqual(c.reconnect_count, 1)
        self.assertEqual(server.accepted, 2)
        self.assertTrue(any("server closed the connection" in line for line in cap.output))

    def test_flush_after_an_idle_close_succeeds_instead_of_raising(self):
        server = self._serve(idle_close_s=0.2)
        c = self._connect_quietly(server, idle_verify_after_s=60.0)
        self.assertTrue(server.idle_closed.wait(2.0))
        time.sleep(0.05)
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
            c.flush()
        self.assertEqual(c.reconnect_count, 1)

    def test_a_gap_just_inside_the_timeout_keeps_the_connection(self):
        # Past the verify threshold but short of the server's timeout: the
        # IDENTIFY round trip finds the connection open, so no redial.
        server = self._serve(idle_close_s=1.0)
        c = self._connect_quietly(server, idle_verify_after_s=0.1)
        c.dmawrite(0x0400, b"\x01")
        time.sleep(0.25)
        c.dmawrite(0x0401, b"\x02")
        c.flush()
        self.assertEqual(server.writes, [(0x0400, b"\x01"), (0x0401, b"\x02")])
        self.assertEqual(c.reconnect_count, 0)
        self.assertEqual(server.accepted, 1)

    def test_a_reset_with_a_write_unread_fails_the_next_flush_once(self):
        # A real kernel reset for a command the server never read: the
        # redial before the flush must not let the barrier pass.
        server = _LoopbackDMAServer(idle_close_s=None, drop_after_writes=1)
        self.addCleanup(server.stop)
        c = self._connect_quietly(server, idle_verify_after_s=60.0)
        c.dmawrite(0x0400, b"\x01")
        c.dmawrite(0x0401, b"\x02")
        self.assertTrue(server.dropped.wait(2.0))
        time.sleep(0.05)  # let the reset reach this side of the loopback
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
            with self.assertRaises(ConnectionError):
                c.flush()
            c.flush()
        self.assertEqual(server.writes, [(0x0400, b"\x01")])

    def test_a_server_without_the_timeout_never_sees_a_redial(self):
        # Firmware 3.14e and C64 Ultimate 1.1.0 keep an idle connection open;
        # the checks there cost a round trip and nothing else.
        server = self._serve(idle_close_s=None)
        c = self._connect_quietly(server, idle_verify_after_s=0.1)
        for i in range(3):
            time.sleep(0.15)
            c.dmawrite(0x0400 + i, bytes([i]))
        c.flush()
        self.assertEqual(server.writes, [(0x0400 + i, bytes([i])) for i in range(3)])
        self.assertEqual(c.reconnect_count, 0)
        self.assertEqual(server.accepted, 1)


class LivenessCheckTest(unittest.TestCase):
    """The pre-command checks, against the scripted fake: each way the
    server can be gone redials before the command, never after it."""

    _WRITE = b"\x06\xff\x03\x00\x20\xd0\x0e"

    def _redial_and_write(self, fake1: FakeSocket, c: SocketDMAClient) -> FakeSocket:
        fake2 = FakeSocket([_IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            c.dmawrite(0xD020, b"\x0e")
        self.assertNotIn(self._WRITE, bytes(fake1.sent))
        self.assertIn(self._WRITE, bytes(fake2.sent))
        self.assertTrue(fake1.closed)
        self.assertEqual(c.reconnect_count, 1)
        return fake2

    def test_a_pending_fin_redials_before_the_write(self):
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.peer_closed = True
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
            self._redial_and_write(fake1, c)

    def test_a_pending_reset_redials_before_the_write(self):
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.peer_reset = True
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG") as cap:
            self._redial_and_write(fake1, c)
        self.assertTrue(any("connection reset while idle" in line for line in cap.output))

    def test_unsolicited_data_redials_and_warns(self):
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.unsolicited = b"\x16"
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG") as cap:
            self._redial_and_write(fake1, c)
        self.assertTrue(
            any(r.levelname == "WARNING" and "unsolicited" in r.getMessage() for r in cap.records)
        )

    def test_an_idle_connection_that_fails_identify_redials_before_the_write(self):
        # The race the peek cannot see: the server closes after the peek but
        # before the command arrives. After an idle gap the IDENTIFY round
        # trip goes first, so it is the IDENTIFY that meets the close.
        fake1 = FakeSocket([_IDENT_REPLY])  # nothing left: the verify reads EOF
        c = _client_with(fake1)
        c._last_send -= c.idle_verify_after_s
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG") as cap:
            self._redial_and_write(fake1, c)
        self.assertTrue(any("did not answer IDENTIFY" in line for line in cap.output))

    def test_an_idle_connection_that_answers_identify_is_kept(self):
        fake = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        c = _client_with(fake)
        c._last_send -= c.idle_verify_after_s
        before = len(fake.sent)
        c.dmawrite(0xD020, b"\x0e")
        self.assertEqual(
            bytes(fake.sent[before:]), struct.pack("<HH", CMD_IDENTIFY, 0) + self._WRITE
        )
        self.assertEqual(c.reconnect_count, 0)

    def test_a_busy_connection_sends_no_identify(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        before = len(fake.sent)
        for _ in range(3):
            c.dmawrite(0xD020, b"\x0e")
        self.assertEqual(bytes(fake.sent[before:]), self._WRITE * 3)

    def test_steady_writes_spanning_the_threshold_send_no_identify(self):
        # Idle is measured from the last command, not from connect: four
        # writes 0.2 s apart span 0.8 s, and none of them is a gap.
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        before = len(fake.sent)
        for _ in range(4):
            time.sleep(c.idle_verify_after_s * 0.4)
            c.dmawrite(0xD020, b"\x0e")
        self.assertEqual(bytes(fake.sent[before:]), self._WRITE * 4)

    def test_format_latency_reports_the_reconnect_count(self):
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.peer_closed = True
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
            self._redial_and_write(fake1, c)
        line = c.format_latency()
        assert line is not None
        self.assertIn("reconnects=1", line)


class LostCommandReportTest(unittest.TestCase):
    """A redial that may have dropped commands already sent must not let
    the next flush() report them drained: callers such as
    ``Ultimate64API._flush_or_raise`` refuse a launch on a failed flush."""

    def _writing_client(self) -> tuple[FakeSocket, SocketDMAClient]:
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        c.dmawrite(0xD020, b"\x0e")
        return fake, c

    def _assert_flush_reports_a_loss(self, c: SocketDMAClient) -> None:
        # The new connection would answer the flush's IDENTIFY, so only the
        # loss report can make it raise.
        with self.assertRaisesRegex(ConnectionError, "may not have reached the server"):
            c.flush()

    def _flush_after_redial(self, c: SocketDMAClient) -> FakeSocket:
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                self._assert_flush_reports_a_loss(c)
        return fake2

    def test_a_reset_after_a_write_fails_the_next_flush_once(self):
        fake1, c = self._writing_client()
        fake1.peer_reset = True
        fake2 = self._flush_after_redial(c)
        c.flush()
        self.assertFalse(fake2.closed)

    def test_the_loss_report_still_drains_the_new_connection(self):
        # api.flush() only logs the raise, and its callers then poll
        # registers over REST on the strength of it: the writes issued on
        # the new connection must have drained before the report goes out.
        fake1, c = self._writing_client()
        fake1.peer_reset = True
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.dmawrite(0xD021, b"\x00")
                before = len(fake2.sent)
                self._assert_flush_reports_a_loss(c)
        self.assertEqual(bytes(fake2.sent[before:]), struct.pack("<HH", CMD_IDENTIFY, 0))
        self.assertEqual(len(fake2._replies), 0)

    def test_a_new_connection_starts_with_nothing_unconfirmed(self):
        fake1, c = self._writing_client()
        fake1.peer_reset = True
        fake2 = self._flush_after_redial(c)
        fake2.peer_reset = True
        fake3 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake3):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.flush()
        self.assertEqual(c.reconnect_count, 2)

    def test_an_early_fin_after_a_write_fails_the_next_flush(self):
        fake1, c = self._writing_client()
        fake1.peer_closed = True
        self._flush_after_redial(c)

    def test_a_stray_byte_after_a_write_fails_the_next_flush(self):
        fake1, c = self._writing_client()
        fake1.unsolicited = b"\x16"
        self._flush_after_redial(c)

    def test_an_unanswered_idle_identify_after_a_write_fails_the_next_flush(self):
        _, c = self._writing_client()
        c._last_send -= c.idle_verify_after_s
        self._flush_after_redial(c)

    def test_a_failed_send_after_a_write_fails_the_next_flush(self):
        fake1, c = self._writing_client()
        fake1.fail_sendalls_remaining = 1
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.dmawrite(0xD021, b"\x00")
                self._assert_flush_reports_a_loss(c)

    def test_an_answered_identify_on_the_new_connection_does_not_clear_it(self):
        fake1, c = self._writing_client()
        fake1.peer_reset = True
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.dmawrite(0xD021, b"\x00")
                c._last_send -= c.idle_verify_after_s
                c.dmawrite(0xD021, b"\x01")  # idle IDENTIFY, answered
                self._assert_flush_reports_a_loss(c)
        self.assertEqual(c.reconnect_count, 1)

    def test_a_flush_that_fails_otherwise_consumes_the_report(self):
        fake1, c = self._writing_client()
        fake1.peer_reset = True
        with patch(
            "c64cast.hw.socket_dma.socket.create_connection",
            side_effect=ConnectionRefusedError("scripted"),
        ):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                with self.assertRaises(SocketDMAError):
                    c.flush()
        _expire_redial_backoff(c)
        fake3 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake3):
            with self.assertLogs("c64cast.hw.socket_dma", level="INFO"):
                c.flush()

    def test_the_idle_close_fin_keeps_flush_quiet(self):
        fake1, c = self._writing_client()
        fake1.peer_closed = True
        c._last_send -= c.idle_verify_after_s
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG") as cap:
                c.flush()
        self.assertEqual(c.reconnect_count, 1)
        # Routine on 3.15 after every pause: nothing at the default INFO level.
        self.assertEqual([r.getMessage() for r in cap.records if r.levelno >= logging.INFO], [])

    def test_a_redial_that_may_have_lost_writes_logs_its_connect_at_info(self):
        fake1, c = self._writing_client()
        fake1.peer_reset = True
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="INFO") as cap:
                self._assert_flush_reports_a_loss(c)
        self.assertTrue(any("connected to" in r.getMessage() for r in cap.records))

    def test_a_reset_after_the_idle_close_fin_fails_the_next_flush(self):
        # A write that crossed the idle close draws a reset after the FIN;
        # Linux then peeks b"" and leaves the reset in SO_ERROR.
        fake1, c = self._writing_client()
        fake1.peer_closed = True
        fake1.so_error = errno.EPIPE
        c._last_send -= c.idle_verify_after_s
        self._flush_after_redial(c)

    def test_an_answered_idle_identify_confirms_the_writes_before_it(self):
        # The idle IDENTIFY is answered, then the write after it fails to
        # send: the write is retried on the new connection, and the one
        # before the IDENTIFY was confirmed, so nothing is owed a report.
        class FailThirdSendSocket(FakeSocket):
            sends = 0

            def sendall(self, data: bytes) -> None:
                self.sends += 1
                if self.sends == 4:  # connect IDENTIFY, write, idle IDENTIFY, write
                    raise BrokenPipeError("scripted failure")
                super().sendall(data)

        fake1 = FailThirdSendSocket([_IDENT_REPLY, _IDENT_REPLY])
        c = _client_with(fake1)
        c.dmawrite(0xD020, b"\x0e")
        c._last_send -= c.idle_verify_after_s
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.dmawrite(0xD021, b"\x00")
                c.flush()
        self.assertEqual(c.reconnect_count, 1)

    def test_a_redial_with_nothing_unconfirmed_keeps_flush_quiet(self):
        fake1, c = self._writing_client()
        fake1._replies.append(_IDENT_REPLY)
        c.flush()
        fake1.peer_reset = True
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.flush()
        self.assertEqual(c.reconnect_count, 1)


def _on_another_thread(fn) -> BaseException | None:
    """Run ``fn`` on a thread of its own, joined before returning; the
    exception it raised, if any."""
    raised: list[BaseException] = []

    def run() -> None:
        try:
            fn()
        except BaseException as e:
            raised.append(e)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return raised[0] if raised else None


@contextlib.contextmanager
def _live_thread_that(first, then=None) -> Iterator[list[BaseException]]:
    """Run ``first`` on a thread that stays alive through the ``with`` body,
    then runs ``then`` and ends; yields the exceptions the two raised. A thread
    that has ended is no longer a writer anyone can charge."""
    raised: list[BaseException] = []
    first_done = threading.Event()
    release = threading.Event()

    def run() -> None:
        try:
            first()
            first_done.set()
            release.wait()
            if then is not None:
                then()
        except BaseException as e:
            raised.append(e)
        finally:
            first_done.set()

    t = threading.Thread(target=run)
    t.start()
    first_done.wait()
    try:
        yield raised
    finally:
        release.set()
        t.join()


class PerThreadLossTest(unittest.TestCase):
    """A lost command is charged to the threads that sent it. Another
    thread's flush must neither hear of it nor consume the report."""

    def _lost_after_main_wrote(self) -> tuple[SocketDMAClient, FakeSocket]:
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        c.dmawrite(0xD020, b"\x0e")
        fake1.peer_reset = True
        return c, FakeSocket([_IDENT_REPLY, _IDENT_REPLY, _IDENT_REPLY])

    def test_a_flush_on_a_thread_that_sent_nothing_does_not_consume_the_loss(self):
        c, fake2 = self._lost_after_main_wrote()
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                other = _on_another_thread(c.flush)
                self.assertIsNone(other)
                with self.assertRaisesRegex(ConnectionError, "may not have reached the server"):
                    c.flush()

    def test_a_live_senders_loss_stays_pending_for_its_own_flush(self):
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY, _IDENT_REPLY])
        with _live_thread_that(lambda: c.dmawrite(0xD020, b"\x0e"), c.flush) as raised:
            fake1.peer_reset = True
            with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
                with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                    c.flush()
                    self.assertEqual(c.thread_loss_count(), 0)
        self.assertEqual(len(raised), 1)
        self.assertIsInstance(raised[0], ConnectionError)
        self.assertIn("may not have reached the server", str(raised[0]))

    def test_the_loss_is_charged_to_the_sender_only(self):
        c, fake2 = self._lost_after_main_wrote()
        other_counts: list[int] = []
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                _on_another_thread(lambda: (c.flush(), other_counts.append(c.thread_loss_count())))
        self.assertEqual(other_counts, [0])
        self.assertEqual(c.thread_loss_count(), 1)
        self.assertEqual(c.possible_loss_count, 1)

    def test_every_thread_that_sent_on_the_lost_connection_is_charged(self):
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY, _IDENT_REPLY])
        with _live_thread_that(lambda: c.dmawrite(0xD020, b"\x0e"), c.flush) as raised:
            c.dmawrite(0xD021, b"\x00")
            fake1.peer_reset = True
            with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
                with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                    with self.assertRaises(ConnectionError):
                        c.flush()
        self.assertEqual(c.thread_loss_count(), 1)
        self.assertEqual(c.possible_loss_count, 1)
        self.assertEqual([type(e) for e in raised], [type(CommandsMayBeLostError())])

    def test_a_thread_that_wrote_only_after_the_redial_is_not_charged(self):
        c, fake2 = self._lost_after_main_wrote()
        counts: list[int] = []
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.check_for_loss()
                _on_another_thread(
                    lambda: (
                        c.dmawrite(0xD021, b"\x00"),
                        c.flush(),
                        counts.append(c.thread_loss_count()),
                    )
                )
        self.assertEqual(counts, [0])

    def test_a_loss_is_reported_once_to_the_sender(self):
        c, fake2 = self._lost_after_main_wrote()
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                with self.assertRaises(ConnectionError):
                    c.flush()
                c.flush()
        self.assertEqual(c.thread_loss_count(), 1)


class PossibleLossCountTest(unittest.TestCase):
    """c64cast#531: ``possible_loss_count`` is what tells ``write_region``'s
    dirty cache that bytes it recorded as sent may never have run."""

    def _writing_client(self) -> tuple[FakeSocket, SocketDMAClient]:
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        c.dmawrite(0xD020, b"\x0e")
        return fake, c

    def _write_on_a_new_connection(self, c: SocketDMAClient) -> FakeSocket:
        fake2 = FakeSocket([_IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.dmawrite(0xD021, b"\x00")
        return fake2

    def test_a_reset_after_a_write_counts_one_loss(self):
        fake1, c = self._writing_client()
        fake1.peer_reset = True
        self._write_on_a_new_connection(c)
        self.assertEqual(c.possible_loss_count, 1)

    def test_a_failed_send_after_a_write_counts_one_loss(self):
        fake1, c = self._writing_client()
        fake1.fail_sendalls_remaining = 1
        self._write_on_a_new_connection(c)
        self.assertEqual(c.possible_loss_count, 1)

    def test_the_idle_close_fin_counts_no_loss(self):
        fake1, c = self._writing_client()
        fake1.peer_closed = True
        c._last_send -= c.idle_verify_after_s
        self._write_on_a_new_connection(c)
        self.assertEqual(c.reconnect_count, 1)
        self.assertEqual(c.possible_loss_count, 0)

    def test_a_redial_with_nothing_unconfirmed_counts_no_loss(self):
        fake1, c = self._writing_client()
        fake1._replies.append(_IDENT_REPLY)
        c.flush()
        fake1.peer_reset = True
        self._write_on_a_new_connection(c)
        self.assertEqual(c.reconnect_count, 1)
        self.assertEqual(c.possible_loss_count, 0)

    def test_an_unanswered_flush_after_a_write_counts_one_loss(self):
        # Ultimate64API.flush() only logs this raise, so the count is the
        # one place a cache can learn of it.
        _, c = self._writing_client()
        with self.assertRaises(ConnectionError):
            c.flush()  # no IDENTIFY reply scripted: "socket closed mid-read"
        self.assertEqual(c.possible_loss_count, 1)

    def test_an_unanswered_flush_with_nothing_unconfirmed_counts_no_loss(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        with self.assertRaises(ConnectionError):
            c.flush()
        self.assertEqual(c.possible_loss_count, 0)

    def test_check_for_loss_finds_a_reset_without_sending(self):
        # A static picture sends nothing; the check must still find the
        # dropped connection rather than wait for the next command.
        fake1, c = self._writing_client()
        fake1.peer_reset = True
        sent = len(fake1.sent)
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
            self.assertEqual(c.check_for_loss(), 1)
        self.assertEqual(len(fake1.sent), sent)
        self.assertTrue(fake1.closed)
        fake2 = FakeSocket([_IDENT_REPLY, _IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="INFO"):
                with self.assertRaisesRegex(ConnectionError, "may not have reached"):
                    c.flush()
        self.assertEqual(c.possible_loss_count, 1)

    def test_check_for_loss_leaves_the_idle_close_for_the_next_command(self):
        fake1, c = self._writing_client()
        fake1.peer_closed = True
        c._last_send -= c.idle_verify_after_s
        self.assertEqual(c.check_for_loss(), 0)
        self.assertFalse(fake1.closed)

    def test_check_for_loss_with_nothing_unconfirmed_does_not_peek(self):
        fake1, c = self._writing_client()
        fake1._replies.append(_IDENT_REPLY)
        c.flush()
        fake1.peer_reset = True
        self.assertEqual(c.check_for_loss(), 0)
        self.assertFalse(fake1.closed)

    def test_check_for_loss_does_not_wait_for_a_busy_connection(self):
        fake1, c = self._writing_client()
        fake1.peer_reset = True
        with c._lock:
            self.assertEqual(c.check_for_loss(), 0)
        self.assertFalse(fake1.closed)


class DirtyCacheAfterALossTest(unittest.TestCase):
    """c64cast#531 end to end: a real kernel reset drops a write the cache
    recorded, and the next frame of an unchanged picture must resend it."""

    def test_an_unchanged_frame_after_a_dropped_write_is_resent_in_full(self):
        from c64cast.hw.api import Ultimate64API

        server = _LoopbackDMAServer(idle_close_s=None, drop_after_writes=2)
        self.addCleanup(server.stop)
        with self.assertLogs("c64cast.hw.socket_dma", level="INFO"):
            api = Ultimate64API("http://127.0.0.1", dma_port=server.port)
        self.addCleanup(api.close)
        frame = [(0x0400, b"\x01" * 4, 1), (0x0800, b"\x02" * 4, 2), (0x0C00, b"\x03" * 4, 3)]
        for addr, data, rid in frame:
            api.write_region(addr, data, region_id=rid)
        self.assertTrue(server.dropped.wait(2.0))
        time.sleep(0.05)  # let the reset reach this side of the loopback
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
            for addr, data, rid in frame:
                self.assertEqual(api.write_region(addr, data, region_id=rid), 4)
            # The flush reports the loss; Ultimate64API.flush only logs it.
            with self.assertLogs("c64cast.hw.api", level="WARNING"):
                api.flush()
        self.assertEqual(server.writes, [(a, d) for a, d, _ in frame[:2] + frame])


class RedialBackoffTest(unittest.TestCase):
    """#542: during an outage every write used to dial, and a dial to a
    switched-off machine holds the connection lock for connect_timeout."""

    def _client_after_a_failed_redial(self) -> tuple[SocketDMAClient, list[int]]:
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.peer_reset = True
        dials: list[int] = []

        def refuse(*_a, **_k):
            dials.append(1)
            raise ConnectionRefusedError("scripted")

        refusing = patch("c64cast.hw.socket_dma.socket.create_connection", side_effect=refuse)
        refusing.start()
        self.addCleanup(refusing.stop)
        with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
            with self.assertRaises(SocketDMAError):
                c.dmawrite(0xD020, b"\x0e")
        self.assertEqual(len(dials), 1)
        return c, dials

    def test_writes_inside_the_backoff_fail_without_dialing(self):
        c, dials = self._client_after_a_failed_redial()
        for _ in range(5):
            with self.assertRaisesRegex(SocketDMAError, "next attempt in"):
                c.dmawrite(0xD020, b"\x0e")
        with self.assertRaisesRegex(SocketDMAError, "next attempt in"):
            c.flush()
        self.assertEqual(len(dials), 1)

    def test_the_backoff_doubles_up_to_its_cap(self):
        c, dials = self._client_after_a_failed_redial()
        waits = [c._redial_backoff_s]
        for _ in range(8):
            _expire_redial_backoff(c)
            with self.assertRaises(SocketDMAError):
                c.dmawrite(0xD020, b"\x0e")
            waits.append(c._redial_backoff_s)
        self.assertEqual(waits, [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0, 8.0])
        self.assertEqual(len(dials), 9)

    def test_a_successful_redial_clears_the_backoff(self):
        c, _ = self._client_after_a_failed_redial()
        _expire_redial_backoff(c)
        fake2 = FakeSocket([_IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="INFO"):
                c.dmawrite(0xD020, b"\x0e")
        self.assertEqual((c._redial_backoff_s, c._redial_not_before), (0.0, 0.0))

    def test_an_explicit_connect_ignores_the_backoff(self):
        c, _ = self._client_after_a_failed_redial()
        fake2 = FakeSocket([_IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="INFO"):
                c.connect()
                c.dmawrite(0xD020, b"\x0e")
        self.assertIn(b"\x06\xff\x03\x00\x20\xd0\x0e", bytes(fake2.sent))
        # A later drop redials at once rather than inside the stale window.
        self.assertEqual((c._redial_backoff_s, c._redial_not_before), (0.0, 0.0))

    def test_a_failed_write_inside_the_backoff_still_moves_the_delivery_epoch(self):
        from c64cast.hw.api import Ultimate64API

        with patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True):
            api = Ultimate64API("http://example.invalid")
        self.addCleanup(api.session.close)
        api.socket_dma._redial_not_before = time.monotonic() + 60.0
        before = api.delivery_epoch
        with self.assertLogs("c64cast.hw.backend", level="DEBUG"):
            api.write_region(0x0400, bytes(40), region_id=1)
        self.assertGreater(api.delivery_epoch, before)


class _CutSocket(FakeSocket):
    """A connection whose next send or read is cut part way by an interrupt,
    the way the signal handler raises out of a blocked sendall or recv."""

    def __init__(self, replies: list[bytes] | None = None):
        super().__init__(replies)
        self.cut_next_send = False
        self.cut_next_recv = False

    def sendall(self, data: bytes) -> None:
        if self.cut_next_send:
            self.cut_next_send = False
            self.sent.extend(data[:3])
            raise KeyboardInterrupt
        super().sendall(data)

    def recv(self, n: int, flags: int = 0) -> bytes:
        if self.cut_next_recv and not flags & socket.MSG_PEEK:
            self.cut_next_recv = False
            out = super().recv(1)
            if out:
                raise KeyboardInterrupt
            return out
        return super().recv(n, flags)


class CutCommandTest(unittest.TestCase):
    """A command cut part way leaves the server reading the next one as its
    remainder, so the connection is abandoned and the next command redials."""

    _WRITE = b"\x06\xff\x03\x00\x20\xd0\x0e"

    def _next_write_redials(self, fake1: _CutSocket, c: SocketDMAClient) -> FakeSocket:
        self.assertIsNone(c._sock)
        self.assertTrue(fake1.closed)
        sent_before = bytes(fake1.sent)
        fake2 = FakeSocket([_IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.dmawrite(0xD020, b"\x0e")
        self.assertEqual(bytes(fake1.sent), sent_before)
        self.assertTrue(bytes(fake2.sent).endswith(self._WRITE))
        return fake2

    def test_a_write_cut_mid_send_abandons_the_connection(self):
        fake1 = _CutSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.cut_next_send = True
        with self.assertRaises(KeyboardInterrupt):
            c.dmawrite(0xD020, b"\x0e")
        fake2 = self._next_write_redials(fake1, c)
        # The cut write may not have run, so the next flush says so once.
        self.assertEqual(c.possible_loss_count, 1)
        fake2._replies.extend([_IDENT_REPLY, _IDENT_REPLY])
        with self.assertRaises(ConnectionError):
            c.flush()
        c.flush()

    def test_a_flush_cut_mid_reply_abandons_the_connection(self):
        fake1 = _CutSocket([_IDENT_REPLY, _IDENT_REPLY])
        c = _client_with(fake1)
        fake1.cut_next_recv = True
        with self.assertRaises(KeyboardInterrupt):
            c.flush()
        self._next_write_redials(fake1, c)
        # Nothing went out unconfirmed, so nothing counts as lost.
        self.assertEqual(c.possible_loss_count, 0)

    def test_a_handshake_cut_mid_reply_leaves_no_socket_behind(self):
        fake1 = _CutSocket([_IDENT_REPLY])
        fake1.cut_next_recv = True
        c = _client_with(fake1, connect=False)
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake1):
            with self.assertRaises(KeyboardInterrupt):
                c.connect()
        self.assertIsNone(c._sock)
        self.assertTrue(fake1.closed)

    def test_a_flush_cut_between_send_and_reply_abandons_the_connection(self):
        # The interrupt lands after IDENTIFY went out and before its read
        # began, so neither wire call saw it; the reply is still owed.
        fake1 = _CutSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        with patch.object(c, "_recv_exact_locked", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                c.flush()
        self._next_write_redials(fake1, c)
        self.assertEqual(c.possible_loss_count, 0)

    def test_a_cut_identify_send_counts_no_loss(self):
        fake1 = _CutSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.cut_next_send = True
        with self.assertRaises(KeyboardInterrupt):
            c.flush()
        fake2 = self._next_write_redials(fake1, c)
        self.assertEqual(c.possible_loss_count, 0)
        fake2._replies.append(_IDENT_REPLY)
        c.flush()

    def test_a_handshake_cut_between_send_and_reply_closes_the_socket(self):
        fake1 = _CutSocket([b"\x01", _IDENT_REPLY])
        c = _client_with(fake1, password="hunter2", connect=False)
        with (
            patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake1),
            patch.object(c, "_recv_exact_locked", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            c.connect()
        self.assertIsNone(c._sock)
        self.assertTrue(fake1.closed)

    def test_an_os_error_keeps_its_own_handling(self):
        # The send-failure path still redials and retries on the same call.
        fake1 = _CutSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.fail_sendalls_remaining = 1
        fake2 = FakeSocket([_IDENT_REPLY])
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake2):
            with self.assertLogs("c64cast.hw.socket_dma", level="DEBUG"):
                c.dmawrite(0xD020, b"\x0e")
        self.assertTrue(bytes(fake2.sent).endswith(self._WRITE))


if __name__ == "__main__":
    unittest.main()
