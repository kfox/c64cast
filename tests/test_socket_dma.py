"""Tests for the Socket DMA client.

A small in-process FakeSocket replaces `socket.create_connection` so we
can drive the protocol without an actual U64 on the network. The fake
records every sendall byte for wire-format assertions and serves a
scripted sequence of recv replies for round-trip flows (IDENTIFY,
AUTHENTICATE)."""
from __future__ import annotations

import struct
import threading
import time
import unittest
from collections import deque
from unittest.mock import patch

from c64cast.socket_dma import (
    CMD_AUTHENTICATE,
    CMD_DMAWRITE,
    CMD_IDENTIFY,
    CMD_KEYB,
    CMD_RESET,
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
    reconnect-and-retry path."""

    def __init__(self, replies: list[bytes] | None = None):
        self.sent = bytearray()
        # Replies are returned in order; each FakeSocket instance scripts
        # one connection's worth of responses.
        self._replies: deque[bytes] = deque(replies or [])
        self.fail_sendalls_remaining = 0
        self.closed = False
        self.timeout = None
        self.sockopts: list[tuple] = []

    # The SocketDMAClient configures the socket; record what it does.
    def settimeout(self, t):
        self.timeout = t

    def setsockopt(self, level, opt, val):
        self.sockopts.append((level, opt, val))

    def sendall(self, data: bytes) -> None:
        if self.fail_sendalls_remaining > 0:
            self.fail_sendalls_remaining -= 1
            raise BrokenPipeError("scripted failure")
        self.sent.extend(data)

    def recv(self, n: int) -> bytes:
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


def _client_with(fake: FakeSocket, *, password: str | None = None,
                 connect: bool = True) -> SocketDMAClient:
    """Build a client whose `socket.create_connection` returns `fake`.
    Set connect=False if the test wants to drive the connect() flow itself."""
    c = SocketDMAClient("test-host", port=64, password=password)
    if connect:
        with patch("c64cast.socket_dma.socket.create_connection",
                   return_value=fake):
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
        with patch("c64cast.socket_dma.socket.create_connection",
                   side_effect=ConnectionRefusedError()):
            with self.assertRaises(SocketDMAError) as ctx:
                c.connect()
        self.assertIn("Ultimate DMA Service", str(ctx.exception))

    def test_connect_with_password_sends_authenticate_first(self):
        # Reply: AUTHENTICATE ack (0x01) then IDENTIFY length+payload.
        fake = FakeSocket([b"\x01", _IDENT_REPLY])
        c = _client_with(fake, password="hunter2")
        # First command on the wire should be AUTHENTICATE with the password.
        auth_header = struct.pack("<HH", CMD_AUTHENTICATE, len("hunter2"))
        self.assertEqual(fake.sent[:4], auth_header)
        self.assertEqual(bytes(fake.sent[4:11]), b"hunter2")
        # Then IDENTIFY.
        ident_header = struct.pack("<HH", CMD_IDENTIFY, 0)
        self.assertEqual(fake.sent[11:15], ident_header)
        self.assertEqual(c.product, "*** Ultimate 64-II ***")

    def test_auth_rejected_raises(self):
        fake = FakeSocket([b"\x00"])  # 0 = rejected
        with patch("c64cast.socket_dma.socket.create_connection",
                   return_value=fake):
            c = SocketDMAClient("test-host", port=64, password="wrong")
            with self.assertRaises(SocketDMAError) as ctx:
                c.connect()
        self.assertIn("authentication rejected", str(ctx.exception))

    def test_empty_password_treated_as_none(self):
        # password="" should NOT trigger AUTHENTICATE — same as None.
        fake = FakeSocket([_IDENT_REPLY])
        _client_with(fake, password="")
        # Only IDENTIFY on the wire — no AUTHENTICATE.
        self.assertEqual(fake.sent[:4], struct.pack("<HH", CMD_IDENTIFY, 0))


class WireEncodingTest(unittest.TestCase):
    """Spot-check the exact bytes on the wire for each command type.
    Regressions here would silently corrupt every U64 write."""

    def setUp(self):
        self.fake = FakeSocket([_IDENT_REPLY])
        self.client = _client_with(self.fake)
        # Drop the connect-time IDENTIFY bytes so subsequent assertions
        # are positioned at the start of the per-test command.
        self.connect_len = len(self.fake.sent)

    def _new(self) -> bytes:
        return bytes(self.fake.sent[self.connect_len:])

    def test_dmawrite_border_color(self):
        # The exact bytes a $D020 border write to color $0E should produce.
        self.client.dmawrite(0xD020, b"\x0e")
        self.assertEqual(
            self._new(),
            b"\x06\xff\x03\x00\x20\xd0\x0e",
            "DMAWRITE bytes don't match — wire format regression!")

    def test_dmawrite_multi_byte_payload(self):
        # Multi-byte payload → length field includes addr (2) + data.
        self.client.dmawrite(0x0400, b"ABC")
        expected = (struct.pack("<HH", CMD_DMAWRITE, 5)  # 2 addr + 3 data
                    + struct.pack("<H", 0x0400) + b"ABC")
        self.assertEqual(self._new(), expected)

    def test_reset_encoding(self):
        self.client.reset()
        self.assertEqual(self._new(), struct.pack("<HH", CMD_RESET, 0))

    def test_keyb_encoding(self):
        self.client.keyb(b"RUN\r")
        expected = struct.pack("<HH", CMD_KEYB, 4) + b"RUN\r"
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


class ReconnectTest(unittest.TestCase):

    def test_dmawrite_reconnects_on_broken_pipe(self):
        # connect: serves IDENTIFY. First sendall after connect fails;
        # reconnect serves IDENTIFY again; retry succeeds.
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.fail_sendalls_remaining = 1  # the next sendall will throw

        # Pre-load a second FakeSocket for the reconnect.
        fake2 = FakeSocket([_IDENT_REPLY])
        # The reconnect path logs a WARNING — capture it (so it doesn't
        # spam stderr) and verify the expected message was emitted.
        with patch("c64cast.socket_dma.socket.create_connection",
                   return_value=fake2):
            with self.assertLogs("c64cast.socket_dma", level="WARNING") as cap:
                c.dmawrite(0xD020, b"\x0e")
        self.assertTrue(
            any("send failed (scripted failure) — reconnecting" in line
                for line in cap.output),
            f"expected reconnect-warning log, got: {cap.output!r}",
        )

        # fake1's failed sendall didn't append anything.
        self.assertEqual(len(fake1.sent),
                         struct.pack("<HH", CMD_IDENTIFY, 0).__len__())
        # fake2 received the IDENTIFY (re-handshake) AND the retried DMAWRITE.
        self.assertIn(b"\x06\xff\x03\x00\x20\xd0\x0e", bytes(fake2.sent))
        self.assertTrue(fake1.closed)

    def test_second_failure_propagates(self):
        # Both the original and the retry fail — the OSError should escape.
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.fail_sendalls_remaining = 1

        fake2 = FakeSocket([_IDENT_REPLY])
        fake2.fail_sendalls_remaining = 1
        with patch("c64cast.socket_dma.socket.create_connection",
                   return_value=fake2):
            with self.assertLogs("c64cast.socket_dma", level="WARNING") as cap:
                with self.assertRaises(OSError):
                    c.dmawrite(0xD020, b"\x0e")
        self.assertTrue(
            any("send failed (scripted failure) — reconnecting" in line
                for line in cap.output),
            f"expected reconnect-warning log, got: {cap.output!r}",
        )

    def test_reconnect_identify_timeout_clears_socket_and_next_call_reconnects(self):
        # Repro of the production crash: a send times out, reconnect
        # succeeds at the TCP layer but the U64 doesn't reply to the
        # post-handshake IDENTIFY (e.g. the Command Interface stalled).
        # The first dmawrite should raise SocketDMAError; self._sock
        # must be cleared so the *next* dmawrite reconnects fresh
        # rather than asserting on a missing socket or blocking forever
        # on the half-open one.
        fake1 = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake1)
        fake1.fail_sendalls_remaining = 1   # provoke reconnect

        # Reconnect TCP succeeds; recv hangs (simulate by returning b"" so
        # _recv_exact_locked raises ConnectionError, OR by raising
        # TimeoutError directly). TimeoutError matches the real failure mode.
        class TimeoutOnRecvSocket(FakeSocket):
            def recv(self, n):
                raise TimeoutError("timed out")
        fake2 = TimeoutOnRecvSocket([])

        # Reconnect #2 (for the next dmawrite): clean IDENTIFY this time.
        fake3 = FakeSocket([_IDENT_REPLY])

        with patch("c64cast.socket_dma.socket.create_connection",
                   side_effect=[fake2, fake3]):
            with self.assertLogs("c64cast.socket_dma", level="WARNING"):
                with self.assertRaises(SocketDMAError):
                    c.dmawrite(0xD020, b"\x0e")
            # The half-open socket must be cleaned up — otherwise the
            # next call would either trip the `assert self._sock is not
            # None` or block on the unanswered IDENTIFY still in the
            # server's FIFO.
            self.assertIsNone(c._sock)
            self.assertTrue(fake2.closed)

            # Second dmawrite reconnects via fake3 and succeeds.
            c.dmawrite(0xD020, b"\x0e")
        self.assertIn(b"\x06\xff\x03\x00\x20\xd0\x0e", bytes(fake3.sent))


class ThreadSafetyTest(unittest.TestCase):

    def test_two_threads_dont_interleave_commands(self):
        # If the lock weren't held across sendall, threads could write
        # half of one command + half of another, producing a corrupted
        # stream. With the lock, the recorded bytes must decompose
        # cleanly into N well-formed commands.
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)

        N_PER_THREAD = 50
        N_THREADS = 4

        def burst(thread_idx: int):
            for i in range(N_PER_THREAD):
                # 4-byte payload that's unambiguously identifiable per
                # thread so we can audit ordering later.
                c.dmawrite(0xC800, bytes([thread_idx, i & 0xFF, 0xAA, 0x55]))

        threads = [threading.Thread(target=burst, args=(t,))
                   for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Parse the recorded stream as a sequence of complete commands.
        stream = bytes(fake.sent)
        # Skip the connect-time IDENTIFY (4 bytes header + 0 payload).
        i = 4
        parsed = 0
        while i < len(stream):
            opcode, length = struct.unpack("<HH", stream[i:i + 4])
            i += 4
            self.assertEqual(opcode, CMD_DMAWRITE)
            self.assertEqual(length, 6)  # 2 addr + 4 data
            i += length
            parsed += 1
        self.assertEqual(i, len(stream),
                         "wire bytes don't end on a command boundary")
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
        # Seed the rolling window directly — exercising the math, not
        # real wall-clock sendall costs.
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
        for token in ("u64 dma latency", "n=3", "avg=5.0",
                      "p50=5.0", "max=5.0", "ms"):
            self.assertIn(token, line)

    def test_dmawrite_records_latency(self):
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        t0 = time.perf_counter()
        c.dmawrite(0xD020, b"\x0e")
        self.assertGreater(c.latency_summary()[4], 0)  # n > 0
        # Sample should be a sensible non-negative number not larger
        # than wall time of the test so far.
        avg = c.latency_summary()[0]
        self.assertGreaterEqual(avg, 0.0)
        self.assertLess(avg, time.perf_counter() - t0 + 0.1)


if __name__ == "__main__":
    unittest.main()
