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

from c64cast.hw.socket_dma import (
    CMD_AUTHENTICATE,
    CMD_DMAWRITE,
    CMD_IDENTIFY,
    CMD_KEYB,
    CMD_RESET,
    CMD_REUWRITE,
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
        with patch("c64cast.hw.socket_dma.socket.create_connection", return_value=fake):
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
        # REUWRITE carries a 24-bit little-endian REU offset (3 bytes, not
        # the 16-bit C64 address DMAWRITE uses) before the data. Every REU
        # audio/video/mic preload rides on this command; elsewhere it's
        # only exercised through FakeSocketDMA, so the call is verified
        # but the bytes never were.
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
        # TimeoutError is an OSError subclass, so flush()'s except arm used
        # to re-raise with self._sock still assigned and an IDENTIFY reply
        # possibly still in flight — the next flush would then read that
        # stale reply as its own, permanently one reply behind.
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)

        def _timed_out_recv(n):
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
        # Each individual recv() arrives well inside io_timeout, but the
        # reply as a whole never finishes — the read must still give up
        # after one cumulative io_timeout rather than resetting the clock
        # on every byte.
        fake = FakeSocket([_IDENT_REPLY])
        c = _client_with(fake)
        c.io_timeout = 0.05

        class DribblingSocket(FakeSocket):
            # Every individual recv() is instant — a per-recv timeout alone
            # would never fire — but each delivers only one byte, so the
            # *sequence* of them takes longer than io_timeout overall.
            def recv(self, n):
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

        # Pre-load a second FakeSocket for the reconnect.
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
        # The original sendall fails, and so does the reconnect's own
        # handshake (fake2's first sendall is its IDENTIFY, not the
        # retried DMAWRITE) — that now surfaces as SocketDMAError, not a
        # raw OSError escaping past connect()'s documented contract.
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
                with self.assertRaises(OSError):
                    c.dmawrite(0xD020, b"\x0e")
        self.assertIsNone(c._sock)
        self.assertTrue(fake2.closed)

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
        fake1.fail_sendalls_remaining = 1  # provoke reconnect

        # Reconnect TCP succeeds; recv hangs (simulate by returning b"" so
        # _recv_exact_locked raises ConnectionError, OR by raising
        # TimeoutError directly). TimeoutError matches the real failure mode.
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

        threads = [threading.Thread(target=burst, args=(t,)) for t in range(N_THREADS)]
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
        for token in ("u64 dma latency", "n=3", "avg=5.0", "p50=5.0", "max=5.0", "ms"):
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
