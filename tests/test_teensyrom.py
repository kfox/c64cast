"""Tests for the TeensyROM+ transport framing + backend (Phase 1).

No hardware: a `LoopbackTransport` captures the exact bytes the client puts
on the wire and replies with queued tokens, so the framing (big-endian
tokens + fields), the ack handshake, the ReadC64Mem round-trip + read-
capability probe, and the PostFile/LaunchFile/reset workflows are pinned
against the firmware protocol.
"""

from __future__ import annotations

import os
import struct
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import replace
from unittest import mock

from _fakes import FakeTime, SleepDrivenClock, make_psid

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory
from c64cast.audio.audio import AudioStreamer
from c64cast.audio.audio_rate import nmi_sample_store
from c64cast.hw import api
from c64cast.hw import teensyrom_api as tr_api
from c64cast.hw import teensyrom_dma as tr_dma
from c64cast.hw.api import _DEFAULT_PLAYER_LAYOUT
from c64cast.hw.backend import (
    TEENSYROM_PROFILE,
    BackendCapabilityError,
    NmiSampleStore,
    make_backend,
)
from c64cast.hw.c64 import (
    DEFAULT_DAC_SAMPLE_RATE,
    LOCKED_BITMAP_DAC_SAMPLE_RATE,
    nmi_latch_for_rate,
)
from c64cast.hw.teensyrom_api import TeensyROMBackend
from c64cast.hw.teensyrom_dma import (
    DRIVE_SD,
    TOK_ACK,
    TOK_FAIL,
    TOK_FW_FULL,
    TOK_READ_C64_MEM,
    TOK_RETRY,
    SerialTransport,
    SpansProbe,
    TcpTransport,
    TRBusyError,
    TRClient,
    TRError,
    TRNak,
    TRSpansRefused,
    TRTransport,
    span_touches_cart_io,
)
from c64cast.scenes.scenes import VideoScene


class LoopbackTransport(TRTransport):
    """Records every byte sent; serves binary replies via recv_exact and
    unsolicited/text bytes via drain_text. `sent` is the concatenated wire
    output for assertions.

    Two buffers, matching how the client uses the transport: `_inbox` feeds
    recv_exact (binary reply tokens), `_stale` feeds drain_text (boot/menu
    chatter + text responses). Reply tokens are queued LITTLE-endian to match
    the firmware's SendU16 (LSB first)."""

    def __init__(self):
        self.sent = bytearray()
        self._inbox = bytearray()  # binary replies (recv_exact)
        self._stale = bytearray()  # unsolicited/text (drain_text)
        self.closed = False

    def queue_token(self, tok: int) -> None:
        # Firmware sends replies little-endian (LSB first).
        self._inbox += bytes([tok & 0xFF, (tok >> 8) & 0xFF])

    def queue_raw(self, data: bytes) -> None:
        self._inbox += data

    def queue_stale(self, data: bytes) -> None:
        self._stale += data

    def connect(self) -> None:
        pass

    def send_all(self, data: bytes) -> None:
        self.sent += data

    def recv_exact(self, n: int) -> bytes:
        if len(self._inbox) < n:
            # A real read that times out has consumed what did arrive.
            self._inbox.clear()
            raise TRError("loopback underflow")
        out = bytes(self._inbox[:n])
        del self._inbox[:n]
        return out

    def drain_text(self, quiet_s: float = 0.2) -> str:
        out = bytes(self._stale)
        self._stale.clear()
        return out.decode("ascii", errors="replace")

    def close(self) -> None:
        self.closed = True

    @property
    def description(self) -> str:
        return "loopback"


# Launch choreography acks: DeleteFile + PostFile 5, LaunchFile 2. One place for
# the count, so a wire change moves one number rather than every bring-up test.
_BRINGUP_LAUNCH_ACKS = 7


def _queue_bringup_acks(t: LoopbackTransport) -> None:
    """Ack the DeleteFile → PostFile → LaunchFile choreography that starts
    every clear-loop bring-up on read-capable firmware."""
    for _ in range(_BRINGUP_LAUNCH_ACKS):
        t.queue_token(TOK_ACK)


def _assert_token_order(test: unittest.TestCase, sent: bytes, *tokens: bytes) -> None:
    """Assert each wire token appears in `sent`, in the given order."""
    pos = -1
    for tok in tokens:
        idx = sent.find(tok, pos + 1)
        test.assertNotEqual(idx, -1, f"token {tok!r} missing (or out of order) after {pos}")
        pos = idx


class FramingTest(unittest.TestCase):
    def setUp(self):
        self.t = LoopbackTransport()
        self.client = TRClient(self.t)

    def test_write_segment_is_big_endian_with_ack(self):
        # token + addr + len are MSB-first.
        self.t.queue_token(TOK_ACK)
        self.client.write_segment(0xD020, b"\x0e")
        # 0x64FB, addr $D020, len 1, data 0x0E
        self.assertEqual(self.t.sent, bytes([0x64, 0xFB, 0xD0, 0x20, 0x00, 0x01, 0x0E]))

    def test_write_segment_nak_raises(self):
        self.t.queue_token(TOK_FAIL)
        with self.assertRaises(TRError):
            self.client.write_segment(0x0400, b"\x01\x02")

    def test_reply_token_parsed_little_endian(self):
        # Firmware SendU16 emits LSB first: Ack 0x64CC arrives as bytes CC 64.
        self.t.queue_raw(b"\xcc\x64")
        self.client.write_segment(0xD020, b"\x02")  # must NOT raise

    def test_big_endian_ack_bytes_are_rejected(self):
        # Bytes 64 CC parsed little-endian = 0xCC64, not an ack.
        self.t.queue_raw(b"\x64\xcc")
        with self.assertRaises(TRError):
            self.client.write_segment(0xD020, b"\x02")

    def test_read_segment_is_big_endian_with_ack_then_data(self):
        # ReadC64Mem: token 0x64FD + addr(BE) + len(BE), then the ack, then
        # `len` data bytes. $FFFC reads back the KERNAL reset vector (E2 FC).
        self.t.queue_token(TOK_ACK)
        self.t.queue_raw(b"\xe2\xfc")
        out = self.client.read_segment(0xFFFC, 2)
        self.assertEqual(self.t.sent, bytes([0x64, 0xFD, 0xFF, 0xFC, 0x00, 0x02]))
        self.assertEqual(out, b"\xe2\xfc")
        self.assertEqual(TOK_READ_C64_MEM, 0x64FD)

    def test_read_segment_nak_raises(self):
        self.t.queue_token(TOK_FAIL)
        with self.assertRaises(TRError):
            self.client.read_segment(0x028D, 1)

    def test_read_segment_ack_parsed_little_endian(self):
        # Ack 0x64CC arrives LSB-first (CC 64); a BE parse reads valid data as error.
        self.t.queue_raw(b"\xcc\x64")
        self.t.queue_raw(b"\x05")
        self.assertEqual(self.client.read_segment(0x028D, 1), b"\x05")

    def test_fw_check_on_connect(self):
        self.t.queue_token(TOK_FW_FULL)
        self.client.connect()
        self.assertEqual(self.client.firmware, "full")
        # connect sent exactly the FWCheck token (0x64E0).
        self.assertEqual(self.t.sent, bytes([0x64, 0xE0]))

    def test_post_file_framing(self):
        # ack(open), ack(header), ack(data)
        for _ in range(3):
            self.t.queue_token(TOK_ACK)
        data = b"ABCD"
        self.client.post_file(data, "c64cast/x.prg", DRIVE_SD)
        # token 0x64BB, then len(4 BE), checksum(2 BE)=sum&0xFFFF, storage, path\0
        expected = bytearray([0x64, 0xBB])
        expected += struct.pack(">I", len(data))
        expected += struct.pack(">H", sum(data) & 0xFFFF)
        expected += bytes([DRIVE_SD])
        expected += b"c64cast/x.prg\x00"
        expected += data
        self.assertEqual(self.t.sent, bytes(expected))

    def test_launch_file_framing(self):
        self.t.queue_token(TOK_ACK)  # open
        self.t.queue_token(TOK_ACK)  # launch
        self.client.launch_file("c64cast/x.prg", DRIVE_SD)
        expected = bytearray([0x64, 0x44, DRIVE_SD])
        expected += b"c64cast/x.prg\x00"
        self.assertEqual(self.t.sent, bytes(expected))

    def test_reset_drains_text_response(self):
        self.t.queue_stale(b"Reset cmd received\n")
        self.client.reset()
        self.assertEqual(self.t.sent, bytes([0x64, 0xEE]))

    def test_post_file_drains_stale_before_open(self):
        # An unsolicited boot token (GoodSID 0x9B81, LE 81 9b) must be drained
        # so it isn't misread as the PostFile open ack.
        self.t.queue_stale(b"\x81\x9b")
        for _ in range(3):
            self.t.queue_token(TOK_ACK)
        self.client.post_file(b"AB", "x.prg", DRIVE_SD)  # must NOT raise


_SPANS_TOK = bytes([0x64, 0xFC])
_MEM_TOK = bytes([0x64, 0xFB])


_LOCK_PROBE_HEADER = bytes([tr_dma.SPANS_FLAG_LOCK_TO_STORE, 0, 0, 0])
_LOCK_KNOWN = b"Want 1 to 64 spans"
_LOCK_UNKNOWN = b"Flags must be 0"


class _SpansLoopback(LoopbackTransport):
    """A loopback that can answer the four-byte span header with text, the way
    firmware does when it refuses a zero-span header (with or without the
    lock flag), and the token with text, the way it follows a FailToken with
    its reason; it records each send and each quiet window drain_text was
    asked to wait out, in order."""

    def __init__(self, on_header: bytes = b"", on_lock_header: bytes = b"", on_token: bytes = b""):
        super().__init__()
        self.on_header = on_header
        self.on_lock_header = on_lock_header
        self.on_token = on_token
        self.events: list[tuple[str, object]] = []
        self.quiet_waits: list[float] = []

    def send_all(self, data: bytes) -> None:
        self.events.append(("send", bytes(data)))
        super().send_all(data)
        if data == _SPANS_TOK:
            self.queue_stale(self.on_token)
        elif data == bytes(4):
            self.queue_stale(self.on_header)
        elif data == _LOCK_PROBE_HEADER:
            self.queue_stale(self.on_lock_header)

    def drain_text(self, quiet_s: float = 0.2) -> str:
        self.events.append(("quiet", quiet_s))
        self.quiet_waits.append(quiet_s)
        return super().drain_text(quiet_s)


_NEXT_FRAME = TRClient._u16(0x64FB) + bytes([0xD0, 0x20, 0x00, 0x01, 0x0E])


def _bitmap_tempo(b: TeensyROMBackend, multicolor: bool) -> float:
    return b.dac_pacing(bitmap=True, multicolor=multicolor).bitmap_tempo


def _dac_rate(b: TeensyROMBackend, *, bitmap: bool) -> int:
    return b.dac_pacing(bitmap=bitmap, multicolor=bitmap).sample_rate


def _frozen_clock(test: unittest.TestCase) -> SleepDrivenClock:
    """Bind a clock only sleeps advance over the module's `time`, so a resync's
    wait is exactly what the elapsed time since the last send leaves of it."""
    clock = SleepDrivenClock()
    patcher = mock.patch.object(tr_dma, "time", clock)
    patcher.start()
    test.addCleanup(patcher.stop)
    return clock


def _next_write_events(t: _SpansLoopback, c: TRClient) -> list[tuple[str, object]]:
    """What the next WriteC64Mem puts on the link: a resync's quiet window, if
    one runs, has to come before its frame."""
    t.events.clear()
    t.queue_token(TOK_ACK)
    c.write_segment(0xD020, b"\x0e")
    return list(t.events)


_RESYNCED = [("quiet", tr_dma._RESYNC_QUIET_S), ("send", _NEXT_FRAME)]
_IN_STEP = [("send", _NEXT_FRAME)]


class SpansFramingTest(unittest.TestCase):
    def setUp(self):
        _frozen_clock(self)
        self.t = _SpansLoopback()
        self.c = TRClient(self.t)

    def test_token_header_span_list_then_payload(self):
        for _ in range(3):
            self.t.queue_token(TOK_ACK)
        self.c.write_spans([(0x4000, b"\x01\x02"), (0x5000, b"\x03")], 32, 40)
        self.assertEqual(
            bytes(self.t.sent),
            _SPANS_TOK
            + bytes([0, 32, 40, 2])
            + bytes([0x40, 0x00, 0x00, 0x02, 0x50, 0x00, 0x00, 0x01])
            + b"\x01\x02\x03",
        )

    def test_bus_held_at_the_token_is_a_refusal_with_nothing_more_sent(self):
        self.t.queue_token(TOK_FAIL)
        self.t.queue_stale(b"REU in use")
        with self.assertRaisesRegex(TRSpansRefused, "REU in use"):
            self.c.write_spans([(0x4000, b"\x01")], 32, 40)
        self.assertEqual(bytes(self.t.sent), _SPANS_TOK)
        # Nothing is owed, so the write that carries it instead goes at once.
        self.assertEqual(_next_write_events(self.t, self.c), _IN_STEP)

    def test_an_unexpected_token_reply_closes_the_command_with_a_zero_header(self):
        self.t.queue_token(0x1234)
        with self.assertRaisesRegex(TRError, "unexpected reply") as cm:
            self.c.write_spans([(0x4000, b"\x01")], 32, 40)
        self.assertNotIsInstance(cm.exception, TRSpansRefused)
        self.assertEqual(bytes(self.t.sent), _SPANS_TOK + bytes(4))
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_lost_token_reply_closes_the_command_with_a_zero_header(self):
        with self.assertRaises(TRError):
            self.c.write_spans([(0x4000, b"\x01")], 32, 40)
        self.assertEqual(bytes(self.t.sent), _SPANS_TOK + bytes(4))
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_failed_landing_waits_out_the_discard_without_sending_more(self):
        self.t.queue_token(TOK_ACK)
        self.t.queue_token(TOK_ACK)
        self.t.queue_token(TOK_FAIL)
        with self.assertRaises(TRError) as cm:
            self.c.write_spans([(0x4000, b"\x01\x02")], 32, 40)
        self.assertNotIsInstance(cm.exception, TRSpansRefused)
        self.assertTrue(bytes(self.t.sent).endswith(b"\x01\x02"))
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_refused_span_list_waits_out_the_discard(self):
        self.t.queue_token(TOK_ACK)
        self.t.queue_token(TOK_FAIL)
        with self.assertRaises(TRError):
            self.c.write_spans([(0x4000, b"\x01\x02")], 32, 40)
        self.assertFalse(bytes(self.t.sent).endswith(b"\x01\x02"))
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_send_the_transport_could_not_finish_still_recovers(self):
        class _Fails(_SpansLoopback):
            def send_all(self, data: bytes) -> None:
                if data == b"\x01\x02":
                    raise TimeoutError("sendall timed out")
                super().send_all(data)

        t = _Fails()
        t.queue_token(TOK_ACK)
        t.queue_token(TOK_ACK)
        c = TRClient(t)
        with self.assertRaises(OSError):
            c.write_spans([(0x4000, b"\x01\x02")], 32, 40)
        self.assertEqual(_next_write_events(t, c), _RESYNCED)

    def test_a_token_the_transport_could_not_send_closes_the_command(self):
        class _Fails(_SpansLoopback):
            def send_all(self, data: bytes) -> None:
                if data == _SPANS_TOK:
                    raise TimeoutError("sendall timed out")
                super().send_all(data)

        t = _Fails()
        c = TRClient(t)
        with self.assertRaises(OSError):
            c.write_spans([(0x4000, b"\x01")], 32, 40)
        self.assertEqual(bytes(t.sent), bytes(4))
        self.assertEqual(_next_write_events(t, c), _RESYNCED)

    def test_span_count_and_header_fields_are_bounded_before_the_wire(self):
        for spans, slice_bytes, gap in (
            ([], 32, 40),
            ([(0x4000, b"\x00")] * 65, 32, 40),
            ([(0x4000, b"\x00")], 256, 40),
            ([(0x4000, b"\x00")], 32, 256),
        ):
            with self.assertRaises(ValueError):
                self.c.write_spans(spans, slice_bytes, gap)
        self.assertEqual(bytes(self.t.sent), b"")

    def test_probe_closes_an_acked_token_with_a_zero_header(self):
        self.t.queue_token(TOK_ACK)
        self.assertIs(self.c.probe_spans(), SpansProbe.READY)
        self.assertEqual(bytes(self.t.sent), _SPANS_TOK + bytes(4))

    def test_probe_reports_a_held_bus_with_nothing_sent_after_the_token(self):
        self.t.queue_token(TOK_FAIL)
        self.t.on_token = b"C64 is paused\r\n"
        self.assertIs(self.c.probe_spans(), SpansProbe.HELD)
        self.assertEqual(bytes(self.t.sent), _SPANS_TOK)
        self.assertEqual(_next_write_events(self.t, self.c), _IN_STEP)

    def test_probe_reads_support_from_the_refusal_when_chatter_hid_the_reply(self):
        t = _SpansLoopback(on_header=b"Want 1 to 64 spans")
        t.queue_raw(b"TR")  # boot chatter where the reply token should be
        self.assertIs(TRClient(t).probe_spans(), SpansProbe.READY)

    def test_probe_reads_a_held_bus_from_its_text_when_chatter_hid_the_reply(self):
        for reason in (b"C64 is paused", b"REU emulation is running"):
            t = _SpansLoopback(on_header=b"\x7f\x9b" + reason)
            t.queue_raw(b"TR")
            self.assertIs(TRClient(t).probe_spans(), SpansProbe.HELD, reason)

    def test_probe_on_firmware_without_the_command(self):
        t = _SpansLoopback(on_header=b"Unk cmd: 0000")
        t.queue_raw(b"Un")  # the text answer, read as a token
        self.assertIs(TRClient(t).probe_spans(), SpansProbe.ABSENT)

    def test_a_locked_write_flags_the_header_and_names_the_store_first(self):
        for _ in range(3):
            self.t.queue_token(TOK_ACK)
        self.c.write_spans([(0x4000, b"\x01\x02")], 33, 170, lock_store=0xD418)
        self.assertEqual(
            bytes(self.t.sent),
            _SPANS_TOK
            + bytes([tr_dma.SPANS_FLAG_LOCK_TO_STORE, 33, 170, 2])
            + bytes([0xD4, 0x18, 0x00, 0x00, 0x40, 0x00, 0x00, 0x02])
            + b"\x01\x02",
        )

    def test_the_store_entry_takes_one_of_the_span_list_places(self):
        with self.assertRaises(ValueError):
            self.c.write_spans([(0x4000, b"\x00")] * 64, 32, 40, lock_store=0xD418)
        self.assertEqual(bytes(self.t.sent), b"")

    def test_lock_probe_finds_the_flag_from_the_span_count_refusal(self):
        t = _SpansLoopback(on_lock_header=_LOCK_KNOWN)
        t.queue_token(TOK_ACK)
        c = TRClient(t)
        self.assertTrue(c.probe_spans_lock())
        self.assertEqual(bytes(t.sent), _SPANS_TOK + _LOCK_PROBE_HEADER)
        self.assertEqual(_next_write_events(t, c), _IN_STEP)

    def test_lock_probe_on_firmware_that_refuses_the_flag_stays_in_step(self):
        t = _SpansLoopback(on_lock_header=_LOCK_UNKNOWN)
        t.queue_token(TOK_ACK)
        c = TRClient(t)
        self.assertFalse(c.probe_spans_lock())
        self.assertEqual(_next_write_events(t, c), _IN_STEP)

    def test_lock_probe_refused_at_its_token_sends_nothing_after_it(self):
        t = _SpansLoopback(on_lock_header=_LOCK_KNOWN, on_token=b"REU emulation is running\r\n")
        t.queue_token(TOK_FAIL)
        c = TRClient(t)
        self.assertFalse(c.probe_spans_lock())
        self.assertEqual(bytes(t.sent), _SPANS_TOK)
        self.assertEqual(_next_write_events(t, c), _IN_STEP)

    def test_lock_probe_the_firmware_timed_out_on_resyncs(self):
        t = _SpansLoopback(on_token=b"Timeout!\n")
        t.queue_token(TOK_FAIL)
        c = TRClient(t)
        with self.assertRaisesRegex(TRError, "timed out on the token"):
            c.probe_spans_lock()
        self.assertEqual(bytes(t.sent), _SPANS_TOK)
        self.assertEqual(_next_write_events(t, c), _RESYNCED)

    def test_lock_probe_answered_by_neither_refusal_resyncs(self):
        t = _SpansLoopback(on_lock_header=b"Timeout!")
        t.queue_token(TOK_ACK)
        c = TRClient(t)
        self.assertFalse(c.probe_spans_lock())
        self.assertEqual(_next_write_events(t, c), _RESYNCED)

    def test_sample_watch_framing_and_counts(self):
        self.t.queue_token(TOK_ACK)
        self.t.queue_raw(struct.pack("<IQIIII", 12000, 600_000_000, 120_000, 50, 2, 600_000_000))
        stats = self.c.sample_watch(0xD418)
        self.assertEqual(bytes(self.t.sent), bytes([0x64, 0xF9, 1, 0xD4, 0x18]))
        self.assertEqual(stats.intervals, 12000)
        self.assertEqual(stats.interval_cycles, 600_000_000)
        self.assertEqual(stats.longest_cycles, 120_000)
        self.assertEqual((stats.locked_slices, stats.unlocked_slices), (50, 2))
        self.assertAlmostEqual(stats.lost_ticks(1 / 12100), 100.0)

    def test_sample_watch_stop_sends_mode_zero(self):
        self.t.queue_token(TOK_ACK)
        self.t.queue_raw(bytes(28))
        self.c.sample_watch(None)
        self.assertEqual(bytes(self.t.sent), bytes([0x64, 0xF9, 0, 0, 0]))

    def test_sample_watch_refused_for_a_held_snoop_is_a_nak(self):
        self.t.queue_token(TOK_FAIL)
        self.t.queue_stale(b"Bus snoop in use")
        with self.assertRaisesRegex(TRNak, "Bus snoop in use"):
            self.c.sample_watch(0xD418)

    def test_cart_io_overlap_bounds(self):
        self.assertFalse(span_touches_cart_io(0xDD00, 0x100))
        self.assertTrue(span_touches_cart_io(0xDD00, 0x101))
        self.assertTrue(span_touches_cart_io(0xDFFF, 1))
        self.assertFalse(span_touches_cart_io(0xE000, 0x100))


class SlicingBackendTest(unittest.TestCase):
    _MODE = "on"

    def _backend(
        self,
        mode: str | None = None,
        *,
        supported: bool = True,
        held: bool = False,
        slice_bytes: int = 32,
        gap_us: int = 40,
        lock: bool = False,
    ):
        mode = mode or self._MODE
        t = _SpansLoopback(
            on_header=b"" if supported else b"Unk cmd: 0000",
            on_lock_header=_LOCK_KNOWN if lock else _LOCK_UNKNOWN,
        )
        t.queue_token(TOK_FW_FULL)
        t.queue_token(TOK_ACK)
        t.queue_raw(b"\xe2\xfc")
        if mode != "off":
            if supported:
                t.queue_token(TOK_FAIL if held else TOK_ACK)
                if mode in ("auto", "locked"):
                    t.queue_token(TOK_FAIL if held else TOK_ACK)
            else:
                t.queue_raw(b"Un")
        b = TeensyROMBackend(
            t,
            profile=replace(TEENSYROM_PROFILE),
            storage="sd",
            dma_slicing=mode,
            dma_slice_bytes=slice_bytes,
            dma_slice_gap_us=gap_us,
        )
        self.connect_sent = bytes(t.sent)
        t.sent.clear()
        return b, t

    def _write(self, b, t, addr: str, size: int, acks: int) -> bytes:
        for _ in range(acks):
            t.queue_token(TOK_ACK)
        b.write_memory_file(addr, bytes(size))
        return bytes(t.sent)

    def test_no_nmi_consumer_keeps_writes_on_writec64mem(self):
        b, t = self._backend()
        sent = self._write(b, t, "4000", 256, 1)
        self.assertTrue(sent.startswith(_MEM_TOK))
        self.assertNotIn(_SPANS_TOK, sent)

    def test_nmi_consumer_slices_a_write_longer_than_one_slice(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        sent = self._write(b, t, "4000", 33, 3)
        self.assertTrue(sent.startswith(_SPANS_TOK + bytes([0, 32, 40, 1])))
        self.assertEqual(b.stats["errors"], 0)

    def test_a_write_of_one_slice_or_less_stays_on_writec64mem(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        sent = self._write(b, t, "4000", 32, 1)
        self.assertTrue(sent.startswith(_MEM_TOK))

    def test_the_consumer_stopping_returns_writes_to_writec64mem(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        b.note_nmi_consumer(False)
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))

    def test_cart_io_stays_on_writec64mem(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        self.assertTrue(self._write(b, t, "DE00", 64, 1).startswith(_MEM_TOK))

    def test_sliced_commands_are_capped_at_the_segment_size(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        size = tr_dma.SPANS_SEGMENT_BYTES * 2 + 33
        sent = self._write(b, t, "4000", size, 9)
        self.assertEqual(sent.count(_SPANS_TOK + bytes([0, 32, 40, 1])), 3)
        self.assertEqual(b.stats["writes"], 3)

    def test_a_tail_of_one_slice_or_less_goes_on_writec64mem(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        size = tr_dma.SPANS_SEGMENT_BYTES + 5
        sent = self._write(b, t, "4000", size, 4)
        self.assertEqual(sent.count(_SPANS_TOK), 1)
        self.assertTrue(sent.endswith(_MEM_TOK + bytes([0x44, 0x00, 0x00, 0x05]) + bytes(5)))
        self.assertEqual(b.stats["errors"], 0)

    def test_slice_zero_halts_per_segment_on_writec64mem(self):
        # A whole-span command halts as long as a WriteC64Mem of the same
        # segment, so only the segment cap is left to apply.
        b, t = self._backend(slice_bytes=0)
        b.note_nmi_consumer(True)
        self.assertNotIn(_SPANS_TOK, self._write(b, t, "4000", 2, 1))
        t.sent.clear()
        sent = self._write(b, t, "6000", tr_dma.SPANS_SEGMENT_BYTES * 2, 2)
        self.assertNotIn(_SPANS_TOK, sent)
        self.assertEqual(sent.count(_MEM_TOK), 2)
        self.assertIn(_MEM_TOK + bytes([0x64, 0x00, 0x04, 0x00]), sent)

    def test_a_refused_segment_is_carried_by_writec64mem(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        t.queue_token(TOK_FAIL)
        t.queue_token(TOK_ACK)
        b.write_memory_file("4000", bytes(64))
        sent = bytes(t.sent)
        self.assertEqual(sent[:4], _SPANS_TOK + _MEM_TOK)
        self.assertEqual(sent[4:6], bytes([0x40, 0x00]))
        self.assertEqual(b.stats["errors"], 0)
        self.assertEqual(b.stats["writes"], 1)

    def test_a_refusal_carries_the_rest_of_the_write_on_writec64mem(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        t.queue_token(TOK_FAIL)
        t.queue_token(TOK_ACK)
        t.queue_token(TOK_ACK)
        t.quiet_waits.clear()
        b.write_memory_file("4000", bytes(tr_dma.SPANS_SEGMENT_BYTES * 3))
        sent = bytes(t.sent)
        self.assertEqual(sent.count(_SPANS_TOK), 1)
        self.assertEqual(sent.count(_MEM_TOK + bytes([0x44, 0x00])), 1)
        self.assertEqual(t.quiet_waits.count(0.15), 1)
        self.assertEqual(b.stats["errors"], 0)

    def _refuse_then_carry(self, b, t, addr: str = "4000") -> None:
        """One write whose WriteC64Spans is refused at its token and carried
        by WriteC64Mem."""
        t.queue_token(TOK_FAIL)
        t.queue_token(TOK_ACK)
        b.write_memory_file(addr, bytes(64))

    def test_a_refusal_is_remembered_across_writes(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        clock = [100.0]
        with mock.patch.object(tr_api.time, "monotonic", lambda: clock[0]):
            self._refuse_then_carry(b, t)
            t.sent.clear()
            t.quiet_waits.clear()
            clock[0] += tr_api.SPANS_REFUSED_BACKOFF_MIN_S / 2
            sent = self._write(b, t, "4000", 256, 1)
        self.assertNotIn(_SPANS_TOK, sent)
        self.assertNotIn(0.15, t.quiet_waits)
        self.assertEqual(b.stats["errors"], 0)

    def test_the_back_off_asks_again_and_doubles_while_the_bus_stays_held(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        clock = [100.0]
        low = tr_api.SPANS_REFUSED_BACKOFF_MIN_S
        with mock.patch.object(tr_api.time, "monotonic", lambda: clock[0]):
            self._refuse_then_carry(b, t)
            clock[0] += low
            t.sent.clear()
            self._refuse_then_carry(b, t)
            self.assertTrue(bytes(t.sent).startswith(_SPANS_TOK), "asked again after the back-off")
            clock[0] += 2 * low - 0.01
            t.sent.clear()
            self.assertNotIn(_SPANS_TOK, self._write(b, t, "4000", 256, 1))
            clock[0] += 0.01
            t.sent.clear()
            self._refuse_then_carry(b, t)
            self.assertTrue(bytes(t.sent).startswith(_SPANS_TOK), "asked again after twice it")

    def test_the_back_off_is_capped(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        clock = [100.0]
        with mock.patch.object(tr_api.time, "monotonic", lambda: clock[0]):
            for _ in range(8):
                self._refuse_then_carry(b, t)
                clock[0] += tr_api.SPANS_REFUSED_BACKOFF_MAX_S
            t.sent.clear()
            self._refuse_then_carry(b, t)
        self.assertTrue(bytes(t.sent).startswith(_SPANS_TOK))

    def test_an_accepted_ask_resumes_slicing_and_resets_the_back_off(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        clock = [100.0]
        low = tr_api.SPANS_REFUSED_BACKOFF_MIN_S
        with mock.patch.object(tr_api.time, "monotonic", lambda: clock[0]):
            self._refuse_then_carry(b, t)
            clock[0] += low
            self._refuse_then_carry(b, t)
            clock[0] += 2 * low
            t.sent.clear()
            with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO") as cm:
                sent = self._write(b, t, "4000", 64, 3)
            self.assertTrue(sent.startswith(_SPANS_TOK + bytes([0, 32, 40, 1])))
            self.assertTrue(any("slicing resumed" in m for m in cm.output))
            t.sent.clear()
            self.assertTrue(self._write(b, t, "4000", 64, 3).startswith(_SPANS_TOK))
            self._refuse_then_carry(b, t)
            clock[0] += low
            t.sent.clear()
            self._refuse_then_carry(b, t)
        self.assertTrue(bytes(t.sent).startswith(_SPANS_TOK), "back-off restarted from its floor")

    def test_an_outcome_is_recorded_before_another_thread_asks(self):
        # The audio thread's ask is refused, and the render thread's write,
        # claimed while the bus was free, comes up behind it. Render must not
        # ask before audio's refusal is recorded: its acceptance, recorded
        # first, would be undone by that refusal, and once the refusal is on
        # record render carries its write on WriteC64Mem without asking.
        b, t = self._backend()
        b.note_nmi_consumer(True)
        write_spans = b.tr.write_spans
        note_spans_refused = b._note_spans_refused
        render_done = threading.Event()

        def render_write():
            b.write_memory_file("6000", bytes(64))
            render_done.set()

        render = threading.Thread(target=render_write)

        def audio_refused(*a, **kw):
            b.tr.write_spans = write_spans
            # Audio's refusal, then its WriteC64Mem and render's three replies.
            t.queue_token(TOK_FAIL)
            for _ in range(4):
                t.queue_token(TOK_ACK)
            try:
                write_spans(*a, **kw)
            finally:
                render.start()

        def audio_records_late(*a, **kw):
            render_done.wait(0.3)
            note_spans_refused(*a, **kw)

        b.tr.write_spans = audio_refused
        b._note_spans_refused = audio_records_late
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"):
            b.write_memory_file("4000", bytes(64))
            render.join(5)
        self.assertEqual(bytes(t.sent).count(_SPANS_TOK), 1)
        self.assertIn(_MEM_TOK + bytes([0x60, 0x00, 0x00, 0x40]), bytes(t.sent))
        self.assertTrue(b._spans_held)
        self.assertEqual(b._spans_backoff_s, 2 * tr_api.SPANS_REFUSED_BACKOFF_MIN_S)
        self.assertEqual(b.stats["errors"], 0)

    def test_a_write_already_under_way_stops_asking_once_another_thread_is_refused(self):
        # The render thread's two-segment write was accepted for its first
        # segment; the audio thread's ask was refused before its second.
        b, t = self._backend()
        b.note_nmi_consumer(True)
        clear_spans_refusal = b._clear_spans_refusal

        def then_audio_is_refused():
            was_held = clear_spans_refusal()
            b._note_spans_refused("audio")
            return was_held

        b._clear_spans_refusal = then_audio_is_refused
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"):
            sent = self._write(b, t, "4000", tr_dma.SPANS_SEGMENT_BYTES * 2, 4)
        self.assertEqual(sent.count(_SPANS_TOK), 1)
        self.assertIn(_MEM_TOK + bytes([0x44, 0x00, 0x04, 0x00]), sent)
        self.assertEqual(b._spans_backoff_s, 2 * tr_api.SPANS_REFUSED_BACKOFF_MIN_S)
        self.assertEqual(b.stats["errors"], 0)

    def test_a_write_that_passed_the_first_look_before_a_refusal_does_not_ask(self):
        # _emit's unlocked first look let the audio thread's write through
        # while the bus was free; the render thread's refusal was recorded
        # before it took the lock to ask.
        b, t = self._backend()
        b.note_nmi_consumer(True)
        backing_off = b._spans_backing_off
        looks = []

        def then_render_is_refused():
            looks.append(backing_off())
            if len(looks) == 1:
                with b._spans_lock:
                    b._note_spans_refused("render")
            return looks[-1]

        b._spans_backing_off = then_render_is_refused
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"):
            sent = self._write(b, t, "4000", 64, 1)
        self.assertEqual(looks, [False, True])
        self.assertNotIn(_SPANS_TOK, sent)
        self.assertEqual(b._spans_backoff_s, 2 * tr_api.SPANS_REFUSED_BACKOFF_MIN_S)
        self.assertEqual(b.stats["errors"], 0)

    def test_a_write_waiting_on_another_threads_ask_does_not_ask_after_its_refusal(self):
        # The audio thread's ask held the lock while the render thread's
        # write waited on it, and was refused.
        b, t = self._backend()
        b.note_nmi_consumer(True)
        lock = b._spans_lock

        class RefusedWhileWaiting:
            def __enter__(self):
                lock.acquire()
                b._note_spans_refused("audio")

            def __exit__(self, *exc):
                lock.release()

        with (
            mock.patch.object(b, "_spans_lock", RefusedWhileWaiting()),
            self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"),
        ):
            sent = self._write(b, t, "4000", 64, 1)
        self.assertNotIn(_SPANS_TOK, sent)
        self.assertTrue(sent.startswith(_MEM_TOK))
        self.assertEqual(b._spans_backoff_s, 2 * tr_api.SPANS_REFUSED_BACKOFF_MIN_S)
        self.assertEqual(b.stats["errors"], 0)

    def test_the_first_refusal_is_logged_once(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        clock = [100.0]
        with mock.patch.object(tr_api.time, "monotonic", lambda: clock[0]):
            with self.assertLogs("c64cast.hw.teensyrom_api", level="DEBUG") as cm:
                self._refuse_then_carry(b, t)
                clock[0] += tr_api.SPANS_REFUSED_BACKOFF_MIN_S
                self._refuse_then_carry(b, t)
        refusals = [r for r in cm.records if "asking again in" in r.getMessage()]
        self.assertEqual([r.levelname for r in refusals], ["INFO", "DEBUG"])

    def test_a_bus_held_at_connect_starts_on_writec64mem(self):
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO") as cm:
            b, t = self._backend(held=True)
        self.assertTrue(any("bus held" in m for m in cm.output))
        b.note_nmi_consumer(True)
        self.assertNotIn(_SPANS_TOK, self._write(b, t, "4000", 256, 1))
        self.assertEqual(_bitmap_tempo(b, True), 0.88)

    def test_a_reset_ends_a_remembered_refusal(self):
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"):
            b, t = self._backend(held=True)
        t.queue_stale(b"Reset cmd received\n")
        b.reset()
        self.assertEqual(_bitmap_tempo(b, True), 0.97)
        b.note_nmi_consumer(True)
        t.sent.clear()
        self.assertTrue(self._write(b, t, "4000", 64, 3).startswith(_SPANS_TOK))

    def test_a_reset_during_a_refused_ask_ends_the_refusal(self):
        # The render thread's ask was refused on the link before the reset,
        # and the reset finished while the refusal waited to be recorded.
        b, t = self._backend()
        b.note_nmi_consumer(True)
        reset_sent = threading.Event()
        b.tr.reset = reset_sent.set
        resetter = threading.Thread(target=b.reset)

        def refused_across_a_reset(*a, **kw):
            resetter.start()
            # Timed from the reset reaching the link, not from start(): a
            # resetter slow to start would clear after this refusal even with
            # no lock in reset(), and the test would pass.
            reset_sent.wait(5)
            resetter.join(0.3)
            raise tr_dma.TRSpansRefused("TR WriteC64Spans refused: C64 is paused")

        b.tr.write_spans = refused_across_a_reset
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"):
            self._write(b, t, "4000", 64, 1)
            resetter.join(5)
        self.assertFalse(b._spans_held)
        self.assertEqual(b._spans_backoff_s, tr_api.SPANS_REFUSED_BACKOFF_MIN_S)

    def test_a_bus_held_at_connect_at_slice_0_puts_off_no_ask(self):
        # At slice 0 no WriteC64Spans is sent, so there is nothing to ask again.
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO") as cm:
            b, _ = self._backend(held=True, slice_bytes=0)
        self.assertFalse(any("asking again" in m for m in cm.output))
        self.assertFalse(b._spans_held)

    def test_bitmap_tempo_follows_a_refusal_and_its_end(self):
        b, t = self._backend()
        b.note_nmi_consumer(True)
        clock = [100.0]
        with mock.patch.object(tr_api.time, "monotonic", lambda: clock[0]):
            self._refuse_then_carry(b, t)
            self.assertEqual(_bitmap_tempo(b, True), 0.88)
            self.assertEqual(_bitmap_tempo(b, False), 0.89)
            clock[0] += tr_api.SPANS_REFUSED_BACKOFF_MIN_S
            self._write(b, t, "4000", 64, 3)
        self.assertEqual(_bitmap_tempo(b, True), 0.97)

    def test_the_measured_slice_shape_is_the_config_default(self):
        # The config help and the books promise 0.97 at the default shape.
        tr = cfgmod.TeensyromCfg()
        self.assertEqual((tr.dma_slice_bytes, tr.dma_slice_gap_us), tr_api._SLICED_TEMPO_SHAPE)

    def test_bitmap_tempo_at_an_unmeasured_slice_shape_is_the_unsliced_figure(self):
        for slice_bytes, gap_us in ((64, 40), (32, 20), (255, 0)):
            with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO") as cm:
                b, _ = self._backend(slice_bytes=slice_bytes, gap_us=gap_us)
            self.assertTrue(any("was not measured" in m for m in cm.output))
            self.assertEqual(_bitmap_tempo(b, True), 0.88, (slice_bytes, gap_us))
            self.assertEqual(_bitmap_tempo(b, False), 0.89, (slice_bytes, gap_us))

    def test_off_never_probes(self):
        b, t = self._backend("off")
        self.assertNotIn(_SPANS_TOK, self.connect_sent)
        b.note_nmi_consumer(True)
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))

    def test_auto_on_firmware_without_the_command_stays_unsliced(self):
        b, t = self._backend("auto", supported=False)
        b.note_nmi_consumer(True)
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))

    def test_on_warns_when_the_firmware_lacks_the_command(self):
        with self.assertLogs("c64cast.hw.teensyrom_api", level="WARNING") as cm:
            self._backend("on", supported=False)
        self.assertTrue(any("lacks WriteC64Spans" in m for m in cm.output))

    def test_connect_log_names_the_command_the_writes_go_on(self):
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO") as cm:
            self._backend()
        self.assertTrue(any("WriteC64Spans, 32-byte slices" in m for m in cm.output))
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO") as cm:
            self._backend(slice_bytes=0)
        writes = [m for m in cm.output if "TR writes:" in m]
        self.assertEqual(len(writes), 1)
        self.assertIn("WriteC64Mem in 1024-byte halts", writes[0])
        self.assertNotIn("WriteC64Spans", writes[0])

    def test_bitmap_tempo_follows_the_write_path(self):
        sliced, _ = self._backend()
        self.assertEqual(_bitmap_tempo(sliced, True), 0.97)
        self.assertEqual(_bitmap_tempo(sliced, False), 0.97)
        plain, _ = self._backend("off")
        self.assertEqual(_bitmap_tempo(plain, True), 0.88)
        self.assertEqual(_bitmap_tempo(plain, False), 0.89)

    def test_bitmap_tempo_at_slice_zero_is_the_unsliced_figure(self):
        # Each command is then one 1 KiB halt, as costly to the NMI player
        # per byte as WriteC64Mem's.
        whole, _ = self._backend(slice_bytes=0)
        self.assertEqual(_bitmap_tempo(whole, True), 0.88)
        self.assertEqual(_bitmap_tempo(whole, False), 0.89)


_STORE = NmiSampleStore(addr=0xD418, period_cycles=85, period_s=85 / 1022727, free_cycles=25)
# 25 free cycles less the firmware's 5 per slice; two 83.1 us periods to wait.
_LOCKED_HEADER = _SPANS_TOK + bytes([tr_dma.SPANS_FLAG_LOCK_TO_STORE, 20, 167])
_WATCH_TOK = bytes([0x64, 0xF9])


class LockedSlicingBackendTest(unittest.TestCase):
    connect_sent: bytes
    _MODE = "auto"
    _backend = SlicingBackendTest._backend
    _write = SlicingBackendTest._write

    def _locked(self, mode: str = "auto"):
        b, t = self._backend(mode, lock=True)
        t.queue_token(TOK_ACK)
        t.queue_raw(bytes(28))
        b.note_nmi_consumer(True, _STORE)
        self.assertEqual(bytes(t.sent), _WATCH_TOK + bytes([1, 0xD4, 0x18]))
        t.sent.clear()
        return b, t

    def test_bitmap_tempo_is_real_time_while_the_lock_holds(self):
        b, _ = self._locked()
        self.assertEqual(_bitmap_tempo(b, True), 1.0)
        self.assertEqual(_bitmap_tempo(b, False), 1.0)

    def test_bitmap_tempo_falls_back_when_the_snoop_is_held(self):
        b, t = self._backend(lock=True)
        t.queue_token(TOK_FAIL)
        t.queue_stale(b"Bus snoop in use")
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"):
            b.note_nmi_consumer(True, _STORE)
        self.assertEqual(_bitmap_tempo(b, True), 0.88)

    def test_bitmap_tempo_falls_back_when_no_slice_fits_the_period(self):
        b, _ = self._backend(lock=True)
        b.note_nmi_consumer(True, replace(_STORE, free_cycles=12))
        self.assertEqual(_bitmap_tempo(b, True), 0.88)

    def test_bitmap_tempo_falls_back_after_a_locked_write_is_refused(self):
        b, t = self._locked()
        t.queue_token(TOK_ACK)
        t.queue_token(TOK_FAIL)
        t.queue_stale(b"Bus snoop in use")
        t.queue_token(TOK_ACK)
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"):
            b.write_memory_file("4000", bytes(64))
        self.assertEqual(_bitmap_tempo(b, True), 0.88)

    def test_bitmap_scenes_default_to_the_locked_rate_before_any_consumer(self):
        b, _ = self._backend(lock=True)
        self.assertEqual(_dac_rate(b, bitmap=True), LOCKED_BITMAP_DAC_SAMPLE_RATE)
        self.assertEqual(_dac_rate(b, bitmap=False), DEFAULT_DAC_SAMPLE_RATE)

    def test_an_unset_rate_arms_the_locked_bitmap_latch_only_for_bitmap_scenes(self):
        b, _ = self._backend(lock=True)
        st = AudioStreamer(b, None, "NTSC")
        st.set_scene_rate(_dac_rate(b, bitmap=True), "mhires")
        self.assertEqual(st.nmi.nominal_latch(), nmi_latch_for_rate(10000, "NTSC"))
        st.set_scene_rate(_dac_rate(b, bitmap=False), "petscii")
        self.assertEqual(st.nmi.nominal_latch(), nmi_latch_for_rate(12000, "NTSC"))

    def _lose_the_snoop(self, b: TeensyROMBackend, t: _SpansLoopback) -> None:
        t.queue_token(TOK_FAIL)
        t.queue_stale(b"Bus snoop in use")
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"):
            b.note_nmi_consumer(True, _STORE)

    def _win_the_snoop(self, b: TeensyROMBackend, t: _SpansLoopback) -> None:
        t.queue_token(TOK_ACK)
        t.queue_raw(bytes(28))
        b.note_nmi_consumer(True, _STORE)

    def _set_up_after(
        self, b: TeensyROMBackend, t: _SpansLoopback, flip: Callable[[], None]
    ) -> tuple[AudioStreamer, VideoScene, mock.MagicMock]:
        """An mhires video with an unset tempo and sample rate, built, then
        `flip` changes the lock state, then it is set up."""
        st = AudioStreamer(b, None, "NTSC")
        clip_dir = tempfile.TemporaryDirectory()
        self.addCleanup(clip_dir.cleanup)
        clip = os.path.join(clip_dir.name, "clip.mp4")
        with open(clip, "wb") as f:
            f.write(b"\x00")
        cfg = cfgmod.Config()
        cfg.audio.backend = "dac"
        s = cfgmod.SceneCfg(type="video", display="mhires", file=clip)
        scene = scene_factory.build_scene(s, cfg, b, st, None)
        assert isinstance(scene, VideoScene)
        scene._setup_progress = False
        flip()
        with (
            mock.patch("c64cast.scenes.scenes.ensure_pyav", return_value=True),
            mock.patch("c64cast.scenes.scenes.AVFileSource") as source_cls,
            mock.patch.object(scene.display_mode, "setup"),
            mock.patch.object(b, "dac_pacing", wraps=b.dac_pacing) as pacing,
        ):
            source_cls.return_value.a_stream = None
            with self.assertLogs("c64cast", level="INFO"):
                scene.setup()
        self.assertEqual(pacing.call_count, 1)
        return st, scene, source_cls

    def test_a_lock_lost_between_build_and_setup_paces_the_scene_unlocked(self):
        b, t = self._backend(lock=True)
        st, scene, source_cls = self._set_up_after(b, t, lambda: self._lose_the_snoop(b, t))
        self.assertEqual(st.sample_rate, DEFAULT_DAC_SAMPLE_RATE)
        self.assertEqual(scene.tempo_scale, 0.88)
        self.assertEqual(source_cls.call_args.kwargs["tempo_scale"], 0.88)
        self.assertTrue(scene.tempo_follow)

    def test_a_lock_won_between_build_and_setup_paces_the_scene_locked(self):
        b, t = self._backend(lock=True)
        self._lose_the_snoop(b, t)
        st, scene, source_cls = self._set_up_after(b, t, lambda: self._win_the_snoop(b, t))
        self.assertEqual(st.sample_rate, LOCKED_BITMAP_DAC_SAMPLE_RATE)
        self.assertEqual(scene.tempo_scale, 1.0)
        self.assertEqual(source_cls.call_args.kwargs["tempo_scale"], 1.0)
        self.assertFalse(scene.tempo_follow)

    def test_bitmap_rate_is_the_default_without_the_lock(self):
        for mode, lock in (("auto", False), ("on", True)):
            b, _ = self._backend(mode, lock=lock)
            self.assertEqual(_dac_rate(b, bitmap=True), DEFAULT_DAC_SAMPLE_RATE, mode)

    def test_bitmap_rate_falls_back_when_the_snoop_is_held(self):
        b, t = self._backend(lock=True)
        t.queue_token(TOK_FAIL)
        t.queue_stale(b"Bus snoop in use")
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO"):
            b.note_nmi_consumer(True, _STORE)
        self.assertEqual(_dac_rate(b, bitmap=True), DEFAULT_DAC_SAMPLE_RATE)

    def test_auto_starts_the_watch_and_locks_writes_to_the_store(self):
        b, t = self._locked()
        sent = self._write(b, t, "4000", 256, 3)
        self.assertTrue(sent.startswith(_LOCKED_HEADER + bytes([2, 0xD4, 0x18, 0, 0])))
        self.assertEqual(b.stats["errors"], 0)

    def test_locked_mode_locks_too(self):
        b, t = self._locked("locked")
        self.assertTrue(self._write(b, t, "4000", 256, 3).startswith(_LOCKED_HEADER))

    def test_a_short_write_goes_locked_rather_than_on_writec64mem(self):
        b, t = self._locked()
        self.assertTrue(self._write(b, t, "4000", 2, 3).startswith(_LOCKED_HEADER))

    def test_on_never_asks_for_the_lock(self):
        b, t = self._backend("on", lock=True)
        self.assertNotIn(_LOCK_PROBE_HEADER, self.connect_sent)
        b.note_nmi_consumer(True, _STORE)
        self.assertNotIn(_WATCH_TOK, bytes(t.sent))
        sent = self._write(b, t, "4000", 33, 3)
        self.assertTrue(sent.startswith(_SPANS_TOK + bytes([0, 32, 40, 1])))

    def test_firmware_without_the_lock_writes_unsliced(self):
        b, t = self._backend(lock=False)
        b.note_nmi_consumer(True, _STORE)
        self.assertNotIn(_WATCH_TOK, bytes(t.sent))
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))
        self.assertEqual(_bitmap_tempo(b, True), 0.88)

    def test_locked_mode_on_firmware_without_the_lock_warns(self):
        with self.assertLogs("c64cast.hw.teensyrom_api", level="WARNING") as cm:
            self._backend("locked", lock=False)
        self.assertIn("the firmware lacks the flag, or REU emulation or a pause", cm.output[0])

    def test_auto_mode_on_firmware_without_the_lock_does_not_warn(self):
        with self.assertNoLogs("c64cast.hw.teensyrom_api", level="WARNING"):
            self._backend("auto", lock=False)

    def test_locked_mode_with_the_bus_held_at_connect_blames_the_hold(self):
        with self.assertLogs("c64cast.hw.teensyrom_api", level="WARNING") as cm:
            b, _ = self._backend("locked", held=True, lock=True)
        self.assertFalse(b._lock_capable)
        self.assertEqual(len(cm.output), 1)
        self.assertIn("held the bus at connect", cm.output[0])
        self.assertNotIn("firmware lacks", cm.output[0])

    def test_locked_mode_with_a_failed_lock_probe_names_the_failure(self):
        failed = mock.patch.object(
            tr_dma.TRClient, "probe_spans_lock", side_effect=TRError("no reply")
        )
        with failed, self.assertLogs("c64cast.hw.teensyrom_api", level="WARNING") as cm:
            self._backend("locked", lock=True)
        self.assertIn("the lock-to-store probe failed (no reply)", cm.output[0])

    def test_a_consumer_with_no_store_writes_unsliced(self):
        b, t = self._backend(lock=True)
        b.note_nmi_consumer(True)
        self.assertNotIn(_WATCH_TOK, bytes(t.sent))
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))

    def test_a_period_too_short_to_fit_a_slice_writes_unsliced(self):
        b, t = self._backend(lock=True)
        b.note_nmi_consumer(True, replace(_STORE, free_cycles=12))
        self.assertNotIn(_WATCH_TOK, bytes(t.sent))
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))

    def test_a_held_bus_snoop_writes_unsliced(self):
        b, t = self._backend(lock=True)
        t.queue_token(TOK_FAIL)
        t.queue_stale(b"Bus snoop in use")
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO") as cm:
            b.note_nmi_consumer(True, _STORE)
        self.assertIn("Bus snoop in use", cm.output[0])
        t.sent.clear()
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))

    def test_the_consumer_stopping_stops_the_watch(self):
        b, t = self._locked()
        t.queue_token(TOK_ACK)
        t.queue_raw(bytes(28))
        b.note_nmi_consumer(False)
        self.assertEqual(bytes(t.sent), _WATCH_TOK + bytes([0, 0, 0]))
        t.sent.clear()
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))

    def test_a_refused_locked_write_drops_the_lock_and_lands_unsliced(self):
        b, t = self._locked()
        t.queue_token(TOK_ACK)
        t.queue_token(TOK_FAIL)
        t.queue_stale(b"Bus snoop in use")
        t.queue_token(TOK_ACK)
        with self.assertLogs("c64cast.hw.teensyrom_api", level="INFO") as cm:
            b.write_memory_file("4000", bytes(64))
        self.assertIn("refused a locked write", cm.output[0])
        sent = bytes(t.sent)
        self.assertEqual(sent.count(_LOCKED_HEADER), 1)
        self.assertTrue(sent.endswith(_MEM_TOK + bytes([0x40, 0x00, 0x00, 0x40]) + bytes(64)))
        self.assertEqual(b.stats["errors"], 0)
        t.sent.clear()
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))

    def test_a_locked_write_failing_for_another_reason_keeps_the_lock(self):
        b, t = self._locked()
        t.queue_token(TOK_ACK)
        t.queue_token(TOK_FAIL)
        t.queue_stale(b"No memory to stage the payload")
        b.write_memory_file("4000", bytes(64))
        self.assertEqual(b.stats["errors"], 1)
        self.assertNotIn(_MEM_TOK, bytes(t.sent))
        self.assertEqual(_bitmap_tempo(b, True), 1.0)
        t.sent.clear()
        self.assertTrue(self._write(b, t, "4000", 256, 3).startswith(_LOCKED_HEADER))

    def test_a_retuned_period_resizes_the_locked_slices(self):
        b, t = self._locked()
        b.note_nmi_period(replace(_STORE, period_cycles=75, period_s=75 / 1022727, free_cycles=16))
        sent = self._write(b, t, "4000", 256, 3)
        self.assertTrue(
            sent.startswith(_SPANS_TOK + bytes([tr_dma.SPANS_FLAG_LOCK_TO_STORE, 11, 147]))
        )

    def test_the_slice_at_12_khz_leaves_the_6510_room_for_the_handler(self):
        # A cycle simulation of the handler, the KERNAL hop and the badlines
        # overloads the 6510 past about 26 bytes on NTSC and 24 on PAL: NMIs
        # nest and the stack wraps.
        for system, slice_bytes in (("NTSC", 20), ("PAL", 18)):
            with self.subTest(system=system):
                store = nmi_sample_store(nmi_latch_for_rate(12000, system), system)
                lock = TeensyROMBackend._lock_for(store)
                assert lock is not None
                self.assertEqual(lock[1], slice_bytes)

    def test_a_retuned_period_with_no_lock_stays_unsliced(self):
        b, t = self._backend(lock=True)
        b.note_nmi_period(_STORE)
        b.note_nmi_consumer(True)
        self.assertTrue(self._write(b, t, "4000", 256, 1).startswith(_MEM_TOK))


class SlicingConfigTest(unittest.TestCase):
    def test_slicing_is_locked_or_off_by_default(self):
        # Unlocked slices drop bitmap frames that are on screen only briefly,
        # so only a slice locked to the sample store is on without asking.
        self.assertEqual(cfgmod.Config().teensyrom.dma_slicing, "auto")

    def test_slice_fields_are_bounded_to_a_header_byte(self):
        for name in ("dma_slice_bytes", "dma_slice_gap_us"):
            for bad in (-1, 256, True, 3.0):
                cfg = cfgmod.Config()
                setattr(cfg.teensyrom, name, bad)
                with self.assertRaisesRegex(ValueError, name):
                    cfgmod.validate_sections(cfg)
            cfg = cfgmod.Config()
            setattr(cfg.teensyrom, name, 255)
            cfgmod.validate_sections(cfg)

    def test_make_backend_passes_the_slicing_settings(self):
        cfg = cfgmod.Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "serial"
        cfg.teensyrom.serial_port = "/dev/ttyACM0"
        cfg.teensyrom.dma_slicing = "on"
        cfg.teensyrom.dma_slice_bytes = 64
        cfg.teensyrom.dma_slice_gap_us = 20
        captured: dict[str, object] = {}

        def fake_backend(transport, **kw):
            captured.update(kw)
            return object()

        with mock.patch("c64cast.hw.teensyrom_api.TeensyROMBackend", side_effect=fake_backend):
            make_backend(cfg)
        self.assertEqual(
            (captured["dma_slicing"], captured["dma_slice_bytes"], captured["dma_slice_gap_us"]),
            ("on", 64, 20),
        )


class ErrorSurfacingTest(unittest.TestCase):
    """The firmware sends a trailing text line after a rejected command
    (e.g. "Busy!", "Not enough room"). `_expect_ack` now surfaces it in the
    raised error instead of discarding it, and promotes "Busy!" to TRBusyError.
    Exercised via write_segment (no pre-command drain to interfere)."""

    def setUp(self):
        self.t = LoopbackTransport()
        self.client = TRClient(self.t)

    def _refuse_open(self, tok: int, text: str):
        """Answer a file command's token with `tok` and `text`, after the
        command's own pre-drain has found nothing."""
        self.t.queue_token(tok)
        return mock.patch.object(self.t, "drain_text", side_effect=["", text])

    def test_busy_text_raises_trbusyerror(self):
        for name, call in (
            ("post_file", lambda: self.client.post_file(b"AB", "x.prg")),
            ("delete_file", lambda: self.client.delete_file("x.prg")),
        ):
            self.setUp()  # one subtest's failure must not leave the next out of step
            with self.subTest(name), self._refuse_open(TOK_FAIL, "Busy!"):
                with self.assertRaises(TRBusyError) as cm:
                    call()
                self.assertIn("busy", str(cm.exception).lower())
                self.assertFalse(self.client._out_of_step)

    def test_busy_on_a_command_served_while_busy_is_out_of_step(self):
        # WriteC64Mem is served while a program runs, so a Busy! there is a
        # stray command's reply and the write's own ack is still to come.
        self.t.queue_token(TOK_FAIL)
        self.t.queue_stale(b"Busy!\r\n")
        with self.assertRaises(TRError) as cm:
            self.client.write_segment(0xD020, b"\x01")
        self.assertNotIsInstance(cm.exception, TRNak)
        self.assertTrue(self.client._out_of_step)

    def test_busy_after_a_file_command_opened_is_out_of_step(self):
        self.t.queue_token(TOK_ACK)
        self.t.queue_token(TOK_FAIL)
        with mock.patch.object(self.t, "drain_text", side_effect=["", "Busy!"]):
            with self.assertRaises(TRError) as cm:
                self.client.post_file(b"AB", "x.prg")
        self.assertNotIsInstance(cm.exception, TRNak)
        self.assertTrue(self.client._out_of_step)

    def test_timeout_outranks_busy(self):
        with self._refuse_open(TOK_FAIL, "Timeout!Error receiving data!Busy!"):
            with self.assertRaises(TRError) as cm:
                self.client.post_file(b"AB", "x.prg")
        self.assertNotIsInstance(cm.exception, TRNak)
        self.assertIn("timed out", str(cm.exception))
        self.assertTrue(self.client._out_of_step)

    def test_busy_with_a_token_that_is_no_refusal_is_out_of_step(self):
        with self._refuse_open(0x3412, "Busy!"):
            with self.assertRaises(TRError) as cm:
                self.client.post_file(b"AB", "x.prg")
        self.assertNotIsInstance(cm.exception, TRNak)
        self.assertIn("unexpected reply 0x3412", str(cm.exception))
        self.assertTrue(self.client._out_of_step)

    def test_retry_token_is_a_nak(self):
        self.t.queue_token(TOK_RETRY)
        with self.assertRaises(TRNak) as cm:
            self.client.write_segment(0xD020, b"\x01")
        self.assertIn("RetryToken (0x9B7E)", str(cm.exception))
        self.assertFalse(self.client._out_of_step)

    def test_fail_text_is_surfaced_in_message(self):
        self.t.queue_token(TOK_FAIL)
        self.t.queue_stale(b"Not enough room\r\n")
        with self.assertRaises(TRError) as cm:
            self.client.write_segment(0xD020, b"\x01")
        self.assertNotIsInstance(cm.exception, TRBusyError)
        self.assertIn("Not enough room", str(cm.exception))

    def test_fail_without_text_has_no_dangling_separator(self):
        self.t.queue_token(TOK_FAIL)  # no trailing text queued
        with self.assertRaises(TRError) as cm:
            self.client.write_segment(0xD020, b"\x01")
        self.assertNotIn("—", str(cm.exception))


class PathValidationTest(unittest.TestCase):
    """delete_file/post_file/launch_file all frame the path via _encode_path,
    which must raise TRError (not a bare UnicodeEncodeError, not a wire
    desync) for a non-ASCII path or one with an embedded NUL — and must do
    so before anything reaches the wire."""

    def setUp(self):
        self.t = LoopbackTransport()
        self.client = TRClient(self.t)

    def test_delete_file_rejects_non_ascii_path_without_touching_the_wire(self):
        with self.assertRaises(TRError):
            self.client.delete_file("café.prg")
        self.assertEqual(self.t.sent, b"")

    def test_post_file_rejects_embedded_nul_without_touching_the_wire(self):
        with self.assertRaises(TRError):
            self.client.post_file(b"AB", "a\x00b.prg")
        self.assertEqual(self.t.sent, b"")

    def test_launch_file_rejects_non_ascii_path_without_touching_the_wire(self):
        with self.assertRaises(TRError):
            self.client.launch_file("日本語.prg")
        self.assertEqual(self.t.sent, b"")

    def test_plain_ascii_path_is_unaffected(self):
        self.t.queue_token(TOK_ACK)  # open
        self.t.queue_token(TOK_ACK)  # launch
        self.client.launch_file("c64cast/x.prg")  # must not raise


class DrainAfterCommandTest(unittest.TestCase):
    """drain_after_command is the locked replacement for reaching into
    self.transport directly after a command's own lock is released (the
    _settle_after_launch finding)."""

    def test_drain_after_command_holds_the_lock(self):
        t = LoopbackTransport()
        client = TRClient(t)
        seen_locked = []
        orig = t.drain_text

        def spy(quiet_s: float = 0.2) -> str:
            seen_locked.append(client._lock.locked())
            return orig(quiet_s)

        t.drain_text = spy  # type: ignore[method-assign]
        client.drain_after_command(0.05)
        self.assertEqual(seen_locked, [True])

    def test_drain_after_command_returns_the_transport_text(self):
        t = LoopbackTransport()
        client = TRClient(t)
        t.queue_stale(b"hello")
        self.assertEqual(client.drain_after_command(), "hello")


class BackendTest(unittest.TestCase):
    def _backend(self, read: bool = True):
        t = LoopbackTransport()
        t.queue_token(TOK_FW_FULL)  # consumed by connect()'s fw_check
        # __init__ probes ReadC64Mem (a 2-byte $FFFC read): read=True feeds a
        # successful round-trip, read=False a NAK that downgrades supports_read.
        if read:
            t.queue_token(TOK_ACK)  # probe ack
            t.queue_raw(b"\xe2\xfc")  # 2 ROM bytes ($FFFC)
        else:
            t.queue_token(TOK_FAIL)  # probe NAK -> supports_read False
        b = TeensyROMBackend(t, profile=replace(TEENSYROM_PROFILE), storage="sd")
        return b, t

    def test_emit_chunks_large_writes(self):
        b, t = self._backend()
        # A payload over MAX_SEGMENT_BYTES splits into acked segments — one ack each.
        n_segments = 3
        size = b.tr.MAX_SEGMENT_BYTES * (n_segments - 1) + 5
        for _ in range(n_segments):
            t.queue_token(TOK_ACK)
        b.write_memory_file("4000", bytes(size))
        self.assertEqual(b.stats["writes"], n_segments)
        self.assertEqual(b.stats["bytes"], size)

    def test_emit_absorbs_transport_error(self):
        b, t = self._backend()
        t.queue_token(TOK_FAIL)  # first segment NAK
        # Must not raise — a blip shouldn't crash the playlist.
        b.write_memory("d020", "0e")
        self.assertEqual(b.stats["errors"], 1)

    def test_read_probe_sets_supports_read(self):
        # Answering the probe keeps supports_read True, so cli.py builds the poller.
        b, _ = self._backend(read=True)
        self.assertTrue(b.profile.supports_read)

    def test_read_probe_downgrades_on_old_firmware(self):
        # A NAK reports read-free, so callers fall back to the control plane.
        b, _ = self._backend(read=False)
        self.assertFalse(b.profile.supports_read)

    def test_a_refused_read_probe_leaves_the_link_to_resync(self):
        # Old firmware reads the unknown token's four argument bytes as commands of
        # its own and replies to each; the refusal alone leaves the link in step.
        b, t = self._backend(read=False)
        t.queue_stale(b"Unk cmd\n")
        t.queue_token(TOK_ACK)
        b.write_memory("d020", "0e")
        self.assertEqual(b.stats["errors"], 0)
        self.assertEqual(t.drain_text(), "")

    def test_read_memory_round_trip(self):
        b, t = self._backend()
        t.queue_token(TOK_ACK)
        t.queue_raw(b"\x42")  # the byte living at $028D
        out = b.read_memory(0x028D, 1)
        self.assertEqual(out, b"\x42")
        # The ReadC64Mem frame is the tail, after the connect + probe prefix.
        self.assertTrue(bytes(t.sent).endswith(b"\x64\xfd\x02\x8d\x00\x01"))

    def test_read_memory_returns_none_on_nak(self):
        # keyboard.py + the menu poller need None (never a raise) for "couldn't tell".
        b, t = self._backend()
        t.queue_token(TOK_FAIL)
        self.assertIsNone(b.read_memory(0x028D, 1))

    def test_read_memory_returns_none_on_timeout(self):
        b, _ = self._backend()
        # No reply queued -> the transport underflows; read_memory swallows it.
        self.assertIsNone(b.read_memory(0x028D, 1))

    def test_reu_write_unsupported(self):
        # The TR has no REUWRITE opcode, so reu_write stays on the ABC's raising
        # default and the experimental REU paths gate on it.
        b, _ = self._backend()
        self.assertFalse(b.profile.supports_reu)
        with self.assertRaises(BackendCapabilityError):
            b.reu_write(0, b"\x00")

    @staticmethod
    def _make_sid(*, load=0x1000, init=0x1003, play=0x1006, num_songs=1, payload_len=64):
        """This file's defaults over the shared PSID builder."""
        return make_psid(
            load=load, init=init, play=play, num_songs=num_songs, payload=bytes(payload_len)
        )

    def test_run_sid_player_loads_then_vector_swaps(self):
        # Default (defer_audio False): DMA payload + player MC + re-INIT stub, then a
        # pure-DMA $0314/$0315 swap to the stub, which the next kernal IRQ runs. No
        # reset, no LaunchFile, no PostFile — started exactly like a subtune cue.
        b, t = self._backend(read=True)
        for _ in range(4):  # 3 blob writes + 1 vector-swap write
            t.queue_token(TOK_ACK)
        # _verify_player_irq reads $0314 once after the swap; the re-INIT stub
        # has restored it to the player handler ($C300 + 42 = $C32A, LE wire).
        t.queue_token(TOK_ACK)
        t.queue_raw(b"\x2a\xc3")
        with mock.patch.object(b, "_tune_play_divider", return_value=1):
            b.run_sid_player(self._make_sid())
        sent = bytes(t.sent)
        self.assertIn(b"\x64\xfb\x10\x00", sent)  # payload DMA to $1000
        # $0314 vector-swap to the re-INIT stub at the default $C400 (len 2,
        # data = $C400 little-endian = 00 C4).
        self.assertIn(b"\x64\xfb\x03\x14\x00\x02\x00\xc4", sent)
        self.assertNotIn(b"\x64\x44", sent)  # no LaunchFile (no boot)
        self.assertNotIn(b"\x64\xee", sent)  # no ResetC64
        self.assertNotIn(b"\x64\xbb", sent)  # no PostFile

    def test_run_sid_player_defers_audio_until_begin(self):
        # defer_audio=True (WaveformScene) loads the player but leaves it silent — no
        # $0314 swap, no divider tune — so the caller paints the scope first;
        # begin_sid_audio then does the swap that starts INIT/PLAY.
        b, t = self._backend(read=True)
        for _ in range(3):  # 3 blob writes only
            t.queue_token(TOK_ACK)
        with mock.patch.object(b, "_tune_play_divider", return_value=1) as tune:
            b.run_sid_player(self._make_sid(), defer_audio=True)
        loaded = bytes(t.sent)
        self.assertIn(b"\x64\xfb\x10\x00", loaded)  # payload DMA'd...
        self.assertNotIn(b"\x64\xfb\x03\x14", loaded)  # ...but NO $0314 swap yet
        tune.assert_not_called()  # divider not tuned until audio starts
        self.assertIsNone(b.sid_audio_start_time())

        # Now release audio: the deferred $0314 swap fires.
        t.queue_token(TOK_ACK)  # vector-swap write
        t.queue_token(TOK_ACK)  # _verify_player_irq read
        t.queue_raw(b"\x2a\xc3")
        before = bytes(t.sent)
        with mock.patch.object(b, "_tune_play_divider", return_value=1) as tune2:
            b.begin_sid_audio()
        swapped = bytes(t.sent)[len(before) :]
        self.assertIn(b"\x64\xfb\x03\x14\x00\x02\x00\xc4", swapped)  # swap to $C400
        tune2.assert_called_once()
        self.assertIsNotNone(b.sid_audio_start_time())

    def test_run_sid_player_gated_on_read_support(self):
        # The vector-swap launch needs the IRQ-enabled idle (cycle-clean firmware,
        # proxied by supports_read). The older spin-stub idle masks IRQs, so the swap
        # would never fire — raise rather than play silently.
        b, _ = self._backend(read=False)
        with self.assertRaises(BackendCapabilityError):
            b.run_sid_player(self._make_sid())

    def test_tune_play_divider_reads_cia1_on_tr(self):
        # A CIA #1 Timer A latch of $4000 (~61 Hz PLAY) → divider 2, patched at the
        # player MC. On read-free firmware the auto-tune degrades to N=1.
        b, t = self._backend(read=True)
        b._sid_player_layout = _DEFAULT_PLAYER_LAYOUT
        # One ack + 2 data bytes per latch sample; count from the constant.
        for _ in range(b._DIVIDER_LATCH_SAMPLES):
            t.queue_token(TOK_ACK)
            t.queue_raw(b"\x00\x40")  # $4000 little-endian on the wire
        t.queue_token(TOK_ACK)  # divider write
        with mock.patch.object(api, "time", FakeTime(sleep=None)):
            n = b._tune_play_divider()
        self.assertEqual(n, 2)
        # Divider byte patched at player_base + DIVIDER_OFFSET ($C300+59=$C33B).
        self.assertIn(b"\x64\xfb\xc3\x3b\x00\x01\x02", bytes(t.sent))

    def test_cue_song_reinit_on_tr_is_pure_dma(self):
        # SHIFT-driven subtune re-INIT is a pure-DMA vector swap (no reset / no
        # PostFile): patch the stub's song byte + swap $0314/$0315 to the stub.
        b, t = self._backend(read=True)
        b._sid_player_layout = _DEFAULT_PLAYER_LAYOUT
        b._sid_player_default_play_bank = None
        for _ in range(4):
            t.queue_token(TOK_ACK)
        with mock.patch.object(b, "_tune_play_divider", return_value=1):
            b.cue_song_reinit(2)
        sent = bytes(t.sent)
        self.assertIn(b"\x64\xfb\x03\x14", sent)  # IRQ vector swap at $0314
        self.assertNotIn(b"\x64\xee", sent)  # no reset
        self.assertNotIn(b"\x64\xbb", sent)  # no PostFile

    def test_bring_up_clear_loop_when_read_supported(self):
        # Cycle-clean firmware (supports_read True) launches the IRQ-enabled BASIC
        # clear-loop: DeleteFile → PostFile → LaunchFile, then a DMA screen clear to
        # wipe the loader's "RUNNING.."/READY text. No BLNSW ($00CC) write — the clear
        # loop is what stops the cursor, and poking $CC never held. No spin MC and no
        # $D011 blanking: DEN-off would hang the DMA. Acks: 5 + 2 + 1 probe + 1 clear.
        b, t = self._backend(read=True)
        _queue_bringup_acks(t)
        t.queue_token(TOK_ACK)  # CURLIN probe read
        t.queue_raw(b"\x14\x00")  # ...CURLIN = line 20 -> already in the GOTO loop
        t.queue_token(TOK_ACK)
        b.run_basic_clear_loop()
        sent = bytes(t.sent)
        self.assertNotIn(b"\x64\xfb\xc0\x00", sent)  # no spin MC ($C000)
        self.assertNotIn(b"\x64\xfb\xd0\x11", sent)  # display never blanked ($D011)
        self.assertIn(b"\x64\xfb\x04\x00", sent)  # screen-clear to $0400
        self.assertNotIn(b"\x64\xfb\x00\xcc", sent)  # BLNSW ($00CC) never written
        _assert_token_order(self, sent, b"\x64\xcf", b"\x64\xbb", b"\x64\x44")

    def test_bring_up_repairs_a_clear_loop_that_did_not_run(self):
        # The usual TR outcome (HW-measured): LaunchFile leaves the program body at
        # $0801 with its link pointer zeroed, so BASIC sees an empty program, RUN
        # returns to READY, and the machine sits in the editor's input-wait loop —
        # which copies NDX into BLNSW every pass, so no write to $CC stops the cursor.
        # Bring-up detects that via CURLIN and repairs: re-DMA, fix VARTAB, type RUN.
        b, t = self._backend(read=True)
        _queue_bringup_acks(t)
        t.queue_token(TOK_ACK)  # CURLIN probe read
        t.queue_raw(b"\x00\x00")  # ...CURLIN $0000 -> BASIC is at READY
        for _ in range(4):
            t.queue_token(TOK_ACK)  # program body, VARTAB, KEYD, NDX
        t.queue_token(TOK_ACK)  # CURLIN re-probe read
        t.queue_raw(b"\x14\x00")  # ...CURLIN = line 20: the loop is running
        t.queue_token(TOK_ACK)  # screen clear
        with mock.patch.object(tr_api, "time", FakeTime(sleep=None)):
            b.run_basic_clear_loop()
        sent = bytes(t.sent)
        self.assertIn(b"\x64\xfb\x08\x01", sent)  # program body re-DMA'd to $0801
        self.assertIn(b"\x64\xfb\x00\x2d", sent)  # VARTAB fixed ($2D)
        self.assertIn(b"\x64\xfb\x02\x77", sent)  # RUN typed into KEYD ($0277)
        self.assertIn(b"RUN\r", sent)
        self.assertIn(b"\x64\xfb\x00\xc6", sent)  # NDX ($C6) set to the count

    def test_bring_up_does_not_type_into_a_running_clear_loop(self):
        # Typing RUN into a program that IS looping leaves keystrokes in KEYD, where
        # the keyboard poller reads them as menu input (RETURN is a nav code).
        b, t = self._backend(read=True)
        _queue_bringup_acks(t)
        t.queue_token(TOK_ACK)  # CURLIN probe read
        t.queue_raw(b"\x14\x00")  # ...CURLIN = line 20 -> already looping
        t.queue_token(TOK_ACK)
        with mock.patch.object(tr_api, "time", FakeTime(sleep=None)):
            b.run_basic_clear_loop()
        sent = bytes(t.sent)
        self.assertNotIn(b"RUN\r", sent)
        self.assertNotIn(b"\x64\xfb\x02\x77", sent)  # nothing typed into KEYD

    def test_basic_is_at_ready_reads_curlin(self):
        # HW-measured on a TeensyROM+, both states off the same machine: at READY
        # CURLIN reads $0000, running the clear loop it reads $0014 (line 20). The
        # "$FF in the high byte means direct mode" shorthand matched neither state, so
        # every probe answered "running". Accept both spellings of "no line".
        for hi, lo, expected in (
            (0x00, 0x00, True),  # measured at READY
            (0x00, 0x14, False),  # measured running the clear loop, line 20
            (0xFF, 0x00, True),  # the documented direct-mode marker
            (0xFF, 0xFF, True),
            (0x00, 0x0A, False),  # any other real line number
        ):
            with self.subTest(curlin=f"${hi:02X}{lo:02X}"):
                b, t = self._backend(read=True)
                t.queue_token(TOK_ACK)
                t.queue_raw(bytes([lo, hi]))
                self.assertIs(b._basic_is_at_ready(), expected)

    def test_basic_is_at_ready_is_none_when_unreadable(self):
        # An unreadable probe must not be mistaken for either state.
        b, t = self._backend(read=True)
        t.queue_token(TOK_FAIL)
        self.assertIsNone(b._basic_is_at_ready())

    def test_bring_up_drains_the_loaders_console_text_before_probing(self):
        # LaunchFile acks and then streams its own console text back over the same
        # link, including a C64 reset. HW-measured: with only a fixed sleep, the next
        # reply misaligned with that text and came back as the ASCII of
        # "...mote Launch:", so the CURLIN probe failed and the repair was skipped.
        b, t = self._backend(read=True)
        t.queue_stale(b"Remote Launch:\rF: clearloop.prg\rResetting C64\r")
        _queue_bringup_acks(t)
        t.queue_token(TOK_ACK)  # CURLIN probe read
        t.queue_raw(b"\x00\x00")  # ...CURLIN $0000 -> BASIC is at READY, repair fires
        for _ in range(4):
            t.queue_token(TOK_ACK)  # program body, VARTAB, KEYD, NDX
        t.queue_token(TOK_ACK)  # CURLIN re-probe read
        t.queue_raw(b"\x14\x00")  # ...CURLIN = line 20: the loop is running
        t.queue_token(TOK_ACK)  # screen clear
        with mock.patch.object(tr_api, "time", FakeTime(sleep=None)):
            b.run_basic_clear_loop()
        self.assertEqual(bytes(t._stale), b"")  # chatter consumed, not left to desync
        self.assertIn(b"RUN\r", bytes(t.sent))  # ...so the repair actually ran

    def test_bring_up_spin_stub_when_read_unsupported(self):
        # Old firmware (supports_read False) falls back to the spin stub: DMA the spin
        # MC to $C000, then DeleteFile → PostFile → LaunchFile the SYS stub, then
        # DMA-clear the screen. Acks: 1 + 5 + 2 + 1 = 9.
        b, t = self._backend(read=False)
        t.queue_token(TOK_ACK)  # spin MC write
        _queue_bringup_acks(t)
        t.queue_token(TOK_ACK)  # screen clear
        b.run_basic_clear_loop()
        sent = bytes(t.sent)
        # Tokens in order: 0x64FB (spin write), 0x64CF, 0x64BB, 0x6444.
        _assert_token_order(self, sent, b"\x64\xfb", b"\x64\xcf", b"\x64\xbb", b"\x64\x44")
        # The spin MC is written to $C000 (BE address bytes C0 00).
        i_write = sent.find(b"\x64\xfb")
        self.assertEqual(sent[i_write : i_write + 4], b"\x64\xfb\xc0\x00")

    def test_pause_idle_clears_screen_keeping_display_on(self):
        # A pause on TR must not reset — a reset lands at the TeensyROM menu, freezing
        # $028D and stranding the resume-hold — and must not turn the VIC display off:
        # DEN=0 removes badlines, hanging the cycle-clean DMA (HW-confirmed; wedges
        # the TR on firmware before v0.9). So it clears screen RAM with DEN on: WriteC64Mem to $0400, and no
        # ResetC64, no $D011, no $00CC (the running clear loop holds the cursor off).
        b, t = self._backend(read=True)
        t.queue_token(TOK_ACK)  # screen-clear write (<4096 -> 1 seg)
        b.pause_idle()
        sent = bytes(t.sent)
        self.assertNotIn(b"\x64\xee", sent)  # no ResetC64 anywhere
        self.assertNotIn(b"\x64\xfb\xd0\x11", sent)  # no $D011 write (display stays on)
        self.assertIn(b"\x64\xfb\x04\x00", sent)  # WriteC64Mem to $0400 (screen clear)
        self.assertNotIn(b"\x64\xfb\x00\xcc", sent)  # BLNSW ($00CC) never written

    def test_bring_up_tolerates_missing_file_on_delete(self):
        # First-ever run: the file doesn't exist, so DeleteFile NAKs; bring-up
        # must ignore that and still PostFile + LaunchFile. (Clear-loop path.)
        b, t = self._backend(read=True)
        t.queue_token(TOK_ACK)  # clear-loop delete open
        t.queue_token(TOK_FAIL)  # clear-loop delete body -> file not found (ignored)
        for _ in range(3):
            t.queue_token(TOK_ACK)  # clear-loop post open/header/data
        for _ in range(2):
            t.queue_token(TOK_ACK)  # launch open/body
        t.queue_token(TOK_ACK)  # CURLIN probe read
        t.queue_raw(b"\x14\x00")  # ...line 20 -> loop running, no repair needed
        t.queue_token(TOK_ACK)  # screen clear
        b.run_basic_clear_loop()
        self.assertIn(b"\x64\x44", bytes(t.sent))  # launch still happened

    def test_semantic_helpers_are_pure_writes(self):
        # silence_sid / disable_case_switch come from the buffered base and work on
        # any write-capable backend.
        b, t = self._backend()
        for _ in range(10):
            t.queue_token(TOK_ACK)
        b.disable_case_switch()  # $0291 = $80
        self.assertEqual(b.stats["writes"], 1)

    def test_probe_returns_the_status_line(self):
        b, t = self._backend()
        t.queue_stale(b"TR ready\r\n")
        self.assertEqual(b.probe(), "TR ready")

    def test_probe_returns_none_on_an_empty_reply_instead_of_faking_liveness(self):
        # ping()'s drain_text never raises on a timeout, so a TR that sends back zero
        # bytes must report "couldn't tell", not a fabricated firmware-label success.
        b, _ = self._backend()
        self.assertIsNone(b.probe())

    def test_describe_device_omits_serial_number_when_the_transport_has_none(self):
        # LoopbackTransport doesn't override TRTransport.serial_number, so the
        # ABC's default (None) applies -- same as a TCP-attached board.
        b, _t = self._backend()
        self.assertEqual(b.describe_device(), "TeensyROM+ (full firmware, loopback)")

    def test_settle_after_launch_goes_through_the_client_lock(self):
        # _settle_after_launch runs after launch_file() released the lock, so it must
        # reacquire via drain_after_command rather than touching self.tr.transport —
        # a concurrent keyboard-poller read_memory() could otherwise steal the bytes.
        b, t = self._backend()
        seen_locked = []
        orig = t.drain_text

        def spy(quiet_s: float = 0.2) -> str:
            seen_locked.append(b.tr._lock.locked())
            return orig(quiet_s)

        t.drain_text = spy  # type: ignore[method-assign]
        b._settle_after_launch()
        self.assertEqual(seen_locked, [True])


class _FakeSerial:
    """Minimal stand-in for pyserial's Serial, enough for SerialTransport's
    recv_exact/drain_text. `chunks` is popped one per read() call; once
    exhausted, read() returns b"" (idle, matching a real .timeout expiry).
    Each read() sleeps `delay_s` on `clock`, the host's when none is given."""

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        delay_s: float = 0.0,
        clock: SleepDrivenClock | None = None,
    ):
        self.timeout = 2.0
        self.timeouts_seen: list[float] = []
        self._chunks = list(chunks or [])
        self._delay_s = delay_s
        self._sleep = time.sleep if clock is None else clock.sleep

    @property
    def in_waiting(self) -> int:
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, n: int) -> bytes:
        self.timeouts_seen.append(self.timeout)
        if self._delay_s:
            self._sleep(self._delay_s)
        return self._chunks.pop(0) if self._chunks else b""

    def write(self, data: bytes) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class _BlockingFakeSerial(_FakeSerial):
    """Reads the way pyserial does: read(n) returns at once when n bytes are
    waiting, and otherwise waits out the whole timeout first."""

    def __init__(self, chunks: list[bytes], clock: SleepDrivenClock):
        super().__init__(chunks, clock=clock)

    def read(self, n: int) -> bytes:
        self.timeouts_seen.append(self.timeout)
        if self.in_waiting < n:
            self._sleep(self.timeout)
        return self._chunks.pop(0) if self._chunks else b""


class SerialTransportDeadlineTest(unittest.TestCase):
    """SerialTransport.recv_exact/drain_text against a fake pyserial object —
    no real hardware. The deadline tests run the transport's clock from the
    fake's reads, so they count reads rather than time them."""

    def test_drain_text_overrides_ser_timeout_to_quiet_s_and_restores_it(self):
        # Without this, read(64) blocks up to the fixed io_timeout (2s) waiting for
        # any bytes, so the common "nothing to drain" case stalls 2s.
        transport = SerialTransport("COM_FAKE")
        fake = _FakeSerial()
        transport._ser = fake
        result = transport.drain_text(quiet_s=0.05)
        self.assertEqual(result, "")
        self.assertTrue(fake.timeouts_seen)
        self.assertTrue(all(t == 0.05 for t in fake.timeouts_seen))
        self.assertEqual(fake.timeout, 2.0)  # restored afterward

    def test_drain_text_stops_one_quiet_window_after_the_last_byte(self):
        clock = SleepDrivenClock()
        transport = SerialTransport("COM_FAKE")
        transport._ser = _BlockingFakeSerial([b"Timeout!\n"], clock)
        start = clock.monotonic()
        with mock.patch.object(tr_dma, "time", clock):
            self.assertEqual(transport.drain_text(quiet_s=1.0), "Timeout!")
        self.assertAlmostEqual(clock.monotonic() - start, 1.0)

    def test_drain_text_strips_control_characters(self):
        transport = SerialTransport("COM_FAKE")
        transport._ser = _FakeSerial([b"AB\x07CD\x1b[31m"])
        self.assertEqual(transport.drain_text(quiet_s=0.05), "ABCD[31m")

    def test_drain_text_hard_cap_stops_a_device_that_never_goes_quiet(self):
        clock = SleepDrivenClock()
        transport = SerialTransport("COM_FAKE")
        fake = _FakeSerial([b"x"] * 10_000, delay_s=0.01, clock=clock)
        transport._ser = fake
        with (
            mock.patch.object(tr_dma, "time", clock),
            mock.patch("c64cast.hw.teensyrom_dma._DRAIN_MAX_S", 0.05),
            self.assertLogs("c64cast.hw.teensyrom_dma", level="WARNING"),
        ):
            transport.drain_text(quiet_s=10.0)  # would never go quiet on its own
        # The cap is a handful of reads on the fake's clock; uncapped, the
        # drain reads all 10,000 chunks.
        self.assertLess(len(fake.timeouts_seen), 10)
        self.assertTrue(transport.drain_capped)

    def test_drain_text_byte_cap_is_reported_even_when_the_text_sanitizes_away(self):
        transport = SerialTransport("COM_FAKE")
        transport._ser = _FakeSerial([b"\x00" * 64] * 100)
        with (
            mock.patch("c64cast.hw.teensyrom_dma._DRAIN_MAX_BYTES", 128),
            self.assertLogs("c64cast.hw.teensyrom_dma", level="WARNING"),
        ):
            self.assertEqual(transport.drain_text(quiet_s=10.0), "")
        self.assertTrue(transport.drain_capped)

    def test_a_drain_that_goes_quiet_is_not_reported_as_capped(self):
        transport = SerialTransport("COM_FAKE")
        transport._ser = _FakeSerial([b"ok"])
        transport.drain_capped = True  # left over from an earlier drain
        self.assertEqual(transport.drain_text(quiet_s=0.05), "ok")
        self.assertFalse(transport.drain_capped)

    def test_recv_exact_times_out_on_a_byte_at_a_time_trickle(self):
        # Checking the deadline only when read() returns nothing means a link that
        # delivers >=1 byte per call never trips it, holding TRClient._lock forever.
        clock = SleepDrivenClock()
        transport = SerialTransport("COM_FAKE", io_timeout=0.05)
        fake = _FakeSerial([b"x"] * 10_000, delay_s=0.01, clock=clock)
        transport._ser = fake
        with mock.patch.object(tr_dma, "time", clock), self.assertRaises(TRError):
            transport.recv_exact(1000)
        self.assertLess(len(fake.timeouts_seen), 10)

    def test_serial_number_delegates_to_usb_serial_number(self):
        transport = SerialTransport("/dev/cu.usbmodemABC")
        with mock.patch("c64cast.hw.teensyrom_dma.usb_serial_number", return_value="XYZ123") as m:
            self.assertEqual(transport.serial_number, "XYZ123")
        m.assert_called_once_with("/dev/cu.usbmodemABC")


class _FakeSocket:
    """Minimal stand-in for a connected socket, enough for TcpTransport's
    recv_exact/drain_text. Each recv() sleeps `delay_s` on `clock`, the host's
    when none is given."""

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        delay_s: float = 0.0,
        clock: SleepDrivenClock | None = None,
    ):
        self._timeout: float | None = None
        self._chunks = list(chunks or [])
        self._delay_s = delay_s
        self._sleep = time.sleep if clock is None else clock.sleep
        self.recvs = 0

    def gettimeout(self) -> float | None:
        return self._timeout

    def settimeout(self, value: float | None) -> None:
        self._timeout = value

    def recv(self, n: int) -> bytes:
        self.recvs += 1
        if self._delay_s:
            self._sleep(self._delay_s)
        if not self._chunks:
            raise TimeoutError("no more fake data")
        return self._chunks.pop(0)


class TcpTransportDeadlineTest(unittest.TestCase):
    """TcpTransport.recv_exact/drain_text against a fake socket."""

    def test_drain_text_strips_control_characters(self):
        transport = TcpTransport("host")
        transport._sock = _FakeSocket([b"AB\x07CD\x1b[31m"])  # type: ignore[assignment]
        self.assertEqual(transport.drain_text(quiet_s=0.05), "ABCD[31m")

    def test_drain_text_hard_cap_stops_a_device_that_never_goes_quiet(self):
        clock = SleepDrivenClock()
        transport = TcpTransport("host")
        sock = _FakeSocket([b"x"] * 10_000, delay_s=0.01, clock=clock)
        transport._sock = sock  # type: ignore[assignment]
        with (
            mock.patch.object(tr_dma, "time", clock),
            mock.patch("c64cast.hw.teensyrom_dma._DRAIN_MAX_S", 0.05),
            self.assertLogs("c64cast.hw.teensyrom_dma", level="WARNING"),
        ):
            transport.drain_text(quiet_s=10.0)  # would never go quiet on its own
        self.assertLess(sock.recvs, 10)
        self.assertTrue(transport.drain_capped)

    def test_drain_text_byte_cap_is_reported_even_when_the_text_sanitizes_away(self):
        transport = TcpTransport("host")
        transport._sock = _FakeSocket([b"\x00" * 64] * 100)  # type: ignore[assignment]
        with (
            mock.patch("c64cast.hw.teensyrom_dma._DRAIN_MAX_BYTES", 128),
            self.assertLogs("c64cast.hw.teensyrom_dma", level="WARNING"),
        ):
            self.assertEqual(transport.drain_text(quiet_s=10.0), "")
        self.assertTrue(transport.drain_capped)

    def test_a_drain_that_goes_quiet_is_not_reported_as_capped(self):
        transport = TcpTransport("host")
        transport._sock = _FakeSocket([b"ok"])  # type: ignore[assignment]
        transport.drain_capped = True  # left over from an earlier drain
        self.assertEqual(transport.drain_text(quiet_s=0.05), "ok")
        self.assertFalse(transport.drain_capped)

    def test_recv_exact_times_out_on_a_byte_at_a_time_trickle(self):
        # Without a tracked deadline, every recv() gets a fresh per-call timeout
        # budget, so a trickling peer never trips anything.
        clock = SleepDrivenClock()
        transport = TcpTransport("host", io_timeout=0.05)
        sock = _FakeSocket([b"x"] * 10_000, delay_s=0.01, clock=clock)
        transport._sock = sock  # type: ignore[assignment]
        with mock.patch.object(tr_dma, "time", clock), self.assertRaises(TRError):
            transport.recv_exact(1000)
        self.assertLess(sock.recvs, 10)

    def test_has_no_serial_number(self):
        transport = TcpTransport("host")
        self.assertIsNone(transport.serial_number)


class ReuCoercionTest(unittest.TestCase):
    """cli._coerce_reu_for_backend drops REU-staged opt-ins on a no-REU backend
    so they never reach reu_write (which raises on the TeensyROM)."""

    @staticmethod
    def _cfg(*, pump: bool, staged: bool | str) -> cfgmod.Config:
        cfg = cfgmod.Config()
        cfg.hardware.backend = "teensyrom"
        cfg.audio.use_reu_pump = pump
        cfg.video.use_reu_staged = staged
        return cfg

    @staticmethod
    def _backend(*, supports_reu: bool) -> TeensyROMBackend:
        t = LoopbackTransport()
        t.queue_token(TOK_FW_FULL)  # connect's fw_check
        t.queue_token(TOK_ACK)  # read-probe ack
        t.queue_raw(b"\xe2\xfc")  # read-probe data
        profile = replace(TEENSYROM_PROFILE, supports_reu=supports_reu)
        return TeensyROMBackend(t, profile=profile, storage="sd")

    def test_no_reu_backend_coerces_opt_ins_off(self):
        from c64cast.app.cli import _coerce_reu_for_backend

        cfg = self._cfg(pump=True, staged=True)
        with self.assertLogs("c64cast", level="WARNING"):
            _coerce_reu_for_backend(cfg, self._backend(supports_reu=False))
        self.assertFalse(cfg.audio.use_reu_pump)
        self.assertFalse(cfg.video.use_reu_staged)

    def test_no_reu_backend_leaves_auto_staged_alone(self):
        # "auto" self-heals elsewhere; the coercion only touches explicit true.
        from c64cast.app.cli import _coerce_reu_for_backend

        cfg = self._cfg(pump=False, staged="auto")
        _coerce_reu_for_backend(cfg, self._backend(supports_reu=False))
        self.assertEqual(cfg.video.use_reu_staged, "auto")

    def test_reu_backend_unchanged(self):
        from c64cast.app.cli import _coerce_reu_for_backend

        cfg = self._cfg(pump=True, staged=True)
        _coerce_reu_for_backend(cfg, self._backend(supports_reu=True))
        self.assertTrue(cfg.audio.use_reu_pump)
        self.assertIs(cfg.video.use_reu_staged, True)


class _FakePort:
    """Minimal stand-in for pyserial's ListPortInfo (duck-typed by the matcher)."""

    def __init__(self, device, vid=None, pid=None, product=None, description=None):
        self.device = device
        self.vid = vid
        self.pid = pid
        self.product = product
        self.description = description


class TRSerialAutodetectTest(unittest.TestCase):
    """Cross-platform serial-device auto-detection via pyserial's list_ports
    (no hardware; fake ListPortInfo objects, comports() patched)."""

    # A TeensyROM as it enumerates on Windows: correct (VID, PID) but the
    # generic usbser.sys driver exposes no product string.
    WINDOWS = _FakePort(
        "COM19", vid=0x16C0, pid=0x0489, product=None, description="USB Serial Device (COM19)"
    )
    # As it enumerates on macOS/Linux, where the USB product string surfaces.
    MACOS = _FakePort("/dev/cu.usbmodem1234", vid=0x16C0, pid=0x0489, product="TeensyROM")
    OTHER = _FakePort("COM3", vid=0x1234, pid=0x5678, product="Some Modem")

    def test_matches_by_vid_pid_when_product_missing(self):
        from c64cast.hw.teensyrom_dma import _is_teensyrom_port

        self.assertTrue(_is_teensyrom_port(self.WINDOWS))

    def test_matches_by_product_name_fallback(self):
        from c64cast.hw.teensyrom_dma import _is_teensyrom_port

        # Product string alone identifies it even if the (VID, PID) is unknown.
        self.assertTrue(_is_teensyrom_port(_FakePort("COM7", product="TeensyROM v1")))

    def test_rejects_non_teensyrom_port(self):
        from c64cast.hw.teensyrom_dma import _is_teensyrom_port

        self.assertFalse(_is_teensyrom_port(self.OTHER))

    def _patch_comports(self, ports):
        # Patch the isolated enumeration helper (not serial.tools.list_ports)
        # so these tests don't require the 'tr' extra / pyserial in CI.
        return mock.patch("c64cast.hw.teensyrom_dma._list_comports", return_value=ports)

    def test_autodetect_returns_matching_device(self):
        from c64cast.hw import teensyrom_dma

        with self._patch_comports([self.OTHER, self.WINDOWS]):
            self.assertEqual(teensyrom_dma.autodetect_serial_port(), "COM19")

    def test_autodetect_returns_none_when_no_match(self):
        from c64cast.hw import teensyrom_dma

        with self._patch_comports([self.OTHER]):
            self.assertIsNone(teensyrom_dma.autodetect_serial_port())

    def test_autodetect_warns_and_picks_lowest_on_multiple_boards(self):
        from c64cast.hw import teensyrom_dma

        second = _FakePort("COM20", vid=0x16C0, pid=0x0489)
        with self._patch_comports([second, self.WINDOWS]):
            with self.assertLogs("c64cast.hw.teensyrom_dma", level="WARNING"):
                self.assertEqual(teensyrom_dma.autodetect_serial_port(), "COM19")


class MakeBackendTest(unittest.TestCase):
    def test_serial_requires_port_when_autodetect_fails(self):
        # Patch auto-detect to None so the test is deterministic on a Mac that
        # actually has a TR attached.
        cfg = cfgmod.Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "serial"
        cfg.teensyrom.serial_port = None
        with (
            mock.patch("c64cast.hw.teensyrom_dma.autodetect_serial_port", return_value=None),
            self.assertRaises(ValueError),
        ):
            make_backend(cfg)

    def test_serial_auto_detects_device_when_unset(self):
        cfg = cfgmod.Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "serial"
        cfg.teensyrom.serial_port = None
        captured: dict[str, str] = {}

        def fake_backend(transport, *, profile, storage, **_slicing):
            captured["port"] = transport.port
            return object()

        with (
            mock.patch(
                "c64cast.hw.teensyrom_dma.autodetect_serial_port",
                return_value="/dev/cu.usbmodemAUTO1",
            ),
            mock.patch("c64cast.hw.teensyrom_api.TeensyROMBackend", side_effect=fake_backend),
            self.assertLogs("c64cast.hw.backend", level="INFO"),
        ):
            make_backend(cfg)
        self.assertEqual(captured["port"], "/dev/cu.usbmodemAUTO1")
        # The resolved device is written back onto the config: resolve_calibration_key
        # only looks up the board's USB serial number when serial_port is set, so two
        # TR+ boards on one host would share one "tr-serial-auto" calibration file.
        self.assertEqual(cfg.teensyrom.serial_port, "/dev/cu.usbmodemAUTO1")

    def test_explicit_serial_port_skips_autodetect(self):
        # An explicit serial_port wins — auto-detect is never consulted.
        cfg = cfgmod.Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "serial"
        cfg.teensyrom.serial_port = "/dev/ttyACM0"
        captured: dict[str, str] = {}

        def fake_backend(transport, *, profile, storage, **_slicing):
            captured["port"] = transport.port
            return object()

        with (
            mock.patch(
                "c64cast.hw.teensyrom_dma.autodetect_serial_port",
                side_effect=AssertionError("auto-detect must not run when serial_port is set"),
            ),
            mock.patch("c64cast.hw.teensyrom_api.TeensyROMBackend", side_effect=fake_backend),
        ):
            make_backend(cfg)
        self.assertEqual(captured["port"], "/dev/ttyACM0")

    def test_tcp_requires_host(self):
        cfg = cfgmod.Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "tcp"
        cfg.teensyrom.host = None
        with self.assertRaises(ValueError):
            make_backend(cfg)


class _CutLoopback(_SpansLoopback):
    """A loopback whose next send can be cut part way, by an interrupt (the
    signal handler raising out of a blocked write) or by a transport error,
    and whose next read can be cut by an interrupt."""

    def __init__(self):
        super().__init__()
        self.cut_next_send: type[BaseException] | None = None
        self.cut_next_recv = False

    def send_all(self, data: bytes) -> None:
        if self.cut_next_send is not None:
            exc, self.cut_next_send = self.cut_next_send, None
            super().send_all(data[:3])
            raise exc
        super().send_all(data)

    def recv_exact(self, n: int) -> bytes:
        if self.cut_next_recv:
            self.cut_next_recv = False
            raise KeyboardInterrupt
        return super().recv_exact(n)


class CutCommandTest(unittest.TestCase):
    """A command that fails part way can leave the firmware owed bytes, or a
    reply on its way, so the next command waits out the firmware's timeout
    and discard before it sends anything."""

    def setUp(self):
        self.clock = _frozen_clock(self)
        self.t = _CutLoopback()
        self.c = TRClient(self.t)

    def _cut_write(self, exc: type[BaseException] = KeyboardInterrupt) -> None:
        self.t.cut_next_send = exc
        with self.assertRaises(exc):
            self.c.write_segment(0xD020, b"\x0e")

    def test_a_write_cut_mid_send_resyncs_before_the_next_command(self):
        self._cut_write()
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_write_a_transport_error_cut_mid_send_resyncs_before_the_next_command(self):
        self._cut_write(OSError)
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_read_cut_before_its_reply_resyncs_before_the_next_command(self):
        self.t.queue_token(TOK_ACK)
        self.t.cut_next_recv = True
        with self.assertRaises(KeyboardInterrupt):
            self.c.read_segment(0x0400, 4)
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_reply_that_timed_out_resyncs_before_the_next_command(self):
        # The reply may still arrive, and the next command would read it.
        with self.assertRaises(TRError):
            self.c.write_segment(0xD020, b"\x0e")  # no ack queued: underflow
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_read_data_that_timed_out_resyncs_before_the_next_command(self):
        self.t.queue_token(TOK_ACK)
        self.t.queue_raw(b"\x01")  # one of the four data bytes
        with self.assertRaises(TRError):
            self.c.read_segment(0x0400, 4)
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_malformed_reply_resyncs_before_the_next_command(self):
        self.t.queue_token(0x1234)
        with self.assertRaisesRegex(TRError, "unexpected reply"):
            self.c.write_segment(0xD020, b"\x0e")
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_refusal_does_not_add_a_resync(self):
        # The firmware sends FailToken once it is done with the command.
        self.t.queue_token(TOK_FAIL)
        with self.assertRaises(TRError):
            self.c.delete_file("x.prg")
        self.assertEqual(_next_write_events(self.t, self.c), _IN_STEP)

    def test_every_command_resyncs_after_a_send_cut_short(self):
        commands = {
            "write_segment": lambda: self.c.write_segment(0x4000, b"\x01\x02\x03\x04"),
            "write_spans": lambda: self.c.write_spans([(0x4000, b"\x01\x02")], 32, 40),
            "read_segment": lambda: self.c.read_segment(0x4000, 4),
            "reset": self.c.reset,
            "ping": self.c.ping,
            "delete_file": lambda: self.c.delete_file("c64cast/x.prg"),
            "post_file": lambda: self.c.post_file(b"ABCD", "c64cast/x.prg"),
            "launch_file": lambda: self.c.launch_file("c64cast/x.prg"),
            "probe_spans": self.c.probe_spans,
        }
        for name, command in commands.items():
            with self.subTest(command=name):
                self.t.cut_next_send = OSError
                with self.assertRaises(OSError):
                    command()
                self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_firmware_check_that_timed_out_resyncs_before_the_next_command(self):
        self.c.connect()  # no FWCheck reply queued: underflow
        self.assertEqual(self.c.firmware, "unknown")
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_spans_header_that_did_not_go_out_is_waited_out_from_the_token(self):
        # The firmware waits out a stalled header for 5.1 s after the token,
        # so a command sent 1.6 s after it would be read as that header.
        class _HeaderCut(_CutLoopback):
            def send_all(self, data: bytes) -> None:
                super().send_all(data)
                if data == TRClient._u16(tr_dma.TOK_WRITE_C64_SPANS):
                    self.cut_next_send = OSError

        cases = {
            "write_spans": (TOK_ACK, OSError, lambda c: c.write_spans([(0x4000, b"\x01")], 32, 40)),
            "_close_spans_token": (
                0x1234,
                TRError,
                lambda c: c.write_spans([(0x4000, b"\x01")], 32, 40),
            ),
            "probe_spans": (TOK_ACK, OSError, lambda c: c.probe_spans()),
        }
        for name, (reply, exc, command) in cases.items():
            with self.subTest(site=name):
                t = _HeaderCut()
                c = TRClient(t)
                t.queue_token(reply)
                with self.assertRaises(exc):
                    command(c)
                events = _next_write_events(t, c)
                self.assertEqual(events[1:], _IN_STEP)
                self.assertAlmostEqual(events[0][1], tr_dma._SPANS_HEADER_WAIT_S)  # type: ignore[arg-type]

    def test_the_wait_runs_from_the_last_byte_sent(self):
        class _SlowReply(_CutLoopback):
            def __init__(self, clock: SleepDrivenClock, delay_s: float):
                super().__init__()
                self.clock, self.delay_s = clock, delay_s

            def recv_exact(self, n: int) -> bytes:
                self.clock.sleep(self.delay_s)
                return super().recv_exact(n)

        for delay_s, quiet_s in ((1.0, tr_dma._RESYNC_QUIET_S - 1.0), (5.0, 0.2)):
            with self.subTest(delay_s=delay_s):
                t = _SlowReply(self.clock, delay_s)
                c = TRClient(t)
                with self.assertRaises(TRError):
                    c.write_segment(0xD020, b"\x0e")
                t.delay_s = 0.0
                events = _next_write_events(t, c)
                self.assertEqual(events[1:], _IN_STEP)
                self.assertAlmostEqual(events[0][1], quiet_s)  # type: ignore[arg-type]

    def test_a_link_that_stays_silent_is_judged_dead_until_it_answers(self):
        for _ in range(2):  # the first resync hears nothing, then fails again
            self._cut_write(OSError)
        # Judged dead: the next resync waits only the short quiet.
        self._cut_write(OSError)
        self.assertEqual(self.t.quiet_waits, [tr_dma._RESYNC_QUIET_S, tr_dma._RESYNC_DEAD_QUIET_S])
        # The device answering a resync ends it.
        self.t.queue_stale(b"Timeout!")
        self._cut_write(OSError)
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_completed_command_ends_the_dead_judgment(self):
        for _ in range(2):
            self._cut_write(OSError)
        # This resync waits the short quiet, and its command then completes.
        self.assertEqual(
            _next_write_events(self.t, self.c),
            [("quiet", tr_dma._RESYNC_DEAD_QUIET_S), ("send", _NEXT_FRAME)],
        )
        self._cut_write(OSError)
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_cut_resync_is_retried_when_it_is_cut_too(self):
        self._cut_write()
        with mock.patch.object(self.t, "drain_text", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.c.write_segment(0xD020, b"\x0e")
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_the_resync_runs_once(self):
        self._cut_write()
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)
        self.assertEqual(_next_write_events(self.t, self.c), _IN_STEP)

    def test_a_dead_link_still_waits_out_the_firmware_byte_timeout_from_the_last_send(self):
        for _ in range(2):
            self._cut_write(OSError)
        # Judged dead; the wait still runs from the last byte sent, so a
        # command right after the cut waits out the firmware's 500 ms per byte.
        self.clock.sleep(0.3)
        events = _next_write_events(self.t, self.c)
        self.assertEqual(events[1:], _IN_STEP)
        self.assertAlmostEqual(events[0][1], tr_dma._RESYNC_DEAD_QUIET_S - 0.3)  # type: ignore[arg-type]
        self.assertGreater(tr_dma._RESYNC_DEAD_QUIET_S, 0.5)

    def test_a_dead_link_waits_out_the_discard_after_a_spans_cut_past_its_token(self):
        for _ in range(2):
            self._cut_write(OSError)  # judged dead
        real_recv = self.t.recv_exact

        def ack_then_cut_the_header(n: int) -> bytes:
            self.t.cut_next_send = OSError
            return real_recv(n)

        self.t.queue_token(TOK_ACK)
        with mock.patch.object(self.t, "recv_exact", side_effect=ack_then_cut_the_header):
            with self.assertRaises(OSError):
                self.c.write_spans([(0x4000, b"\x01\x02")], 32, 40)
        # The firmware's wait for the header outlasts the dead link's short wait.
        events = _next_write_events(self.t, self.c)
        self.assertEqual(events[1:], _IN_STEP)
        self.assertAlmostEqual(events[0][1], tr_dma._SPANS_HEADER_WAIT_S)  # type: ignore[arg-type]
        # Once waited out, a dead judgment is back to the short wait.
        for _ in range(3):
            self._cut_write(OSError)
        self.assertEqual(self.t.quiet_waits[-1], tr_dma._RESYNC_DEAD_QUIET_S)

    def test_a_waited_out_spans_discard_is_not_waited_again(self):
        for _ in range(2):
            self._cut_write(OSError)  # judged dead
        real_recv = self.t.recv_exact

        def ack_then_cut_the_header(n: int) -> bytes:
            self.t.cut_next_send = OSError
            return real_recv(n)

        self.t.queue_token(TOK_ACK)
        with mock.patch.object(self.t, "recv_exact", side_effect=ack_then_cut_the_header):
            with self.assertRaises(OSError):
                self.c.write_spans([(0x4000, b"\x01\x02")], 32, 40)
        self._cut_write(OSError)  # its resync waits out the discard, then fails too
        self._cut_write(OSError)
        self.assertAlmostEqual(self.t.quiet_waits[-2], tr_dma._SPANS_HEADER_WAIT_S)
        self.assertEqual(self.t.quiet_waits[-1], tr_dma._RESYNC_DEAD_QUIET_S)

    def test_an_interrupt_after_a_silent_resync_does_not_judge_the_link_dead(self):
        with self.assertRaises(TRError):
            self.c.write_segment(0xD020, b"\x0e")  # no reply: out of step
        self._cut_write(KeyboardInterrupt)  # its resync hears nothing
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_only_the_failing_commands_own_send_counts_toward_a_dead_judgment(self):
        self._cut_write(OSError)  # a send failed, but its resync was not silent yet
        with self.assertRaises(TRError):
            self.c.write_segment(0xD020, b"\x0e")  # silent resync, then no reply
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_reply_that_timed_out_after_a_silent_resync_does_not_judge_the_link_dead(self):
        for _ in range(2):
            with self.assertRaises(TRError):
                self.c.write_segment(0xD020, b"\x0e")  # no reply
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_firmware_timeout_refusal_resyncs_before_the_next_command(self):
        # The firmware gave up on bytes the host sent, which may still arrive.
        fail = bytes([TOK_FAIL & 0xFF, TOK_FAIL >> 8])
        commands = {
            "write_segment": lambda: self.c.write_segment(0x4000, b"\x01\x02"),
            "read_segment": lambda: self.c.read_segment(0x4000, 4),
        }
        for name, command in commands.items():
            with self.subTest(command=name):
                self.t.queue_token(TOK_FAIL)
                self.t.queue_stale(b"Timeout!\n" + fail + b"Error receiving data!")
                with self.assertRaisesRegex(TRError, "timed out receiving") as cm:
                    command()
                self.assertNotIsInstance(cm.exception, tr_dma.TRNak)
                self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_spans_token_the_firmware_timed_out_on_resyncs(self):
        self.t.queue_token(TOK_FAIL)
        self.t.queue_stale(b"Timeout!\n")
        with self.assertRaisesRegex(TRError, "timed out on the token") as cm:
            self.c.write_spans([(0x4000, b"\x01")], 32, 40)
        self.assertNotIsInstance(cm.exception, tr_dma.TRNak)
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_spans_probe_the_firmware_timed_out_on_resyncs(self):
        self.t.queue_token(TOK_FAIL)
        self.t.on_token = b"Timeout!\n"
        with self.assertRaisesRegex(TRError, "timed out on the token"):
            self.c.probe_spans()
        self.assertEqual(bytes(self.t.sent), _SPANS_TOK)
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_an_acked_spans_probe_with_a_firmware_timeout_still_finds_support(self):
        # Only firmware carrying WriteC64Spans acks its token.
        self.t.queue_token(TOK_ACK)
        self.t.on_header = b"Timeout!\n" + tr_dma._SPANS_COUNT_REFUSAL.encode()
        self.assertIs(self.c.probe_spans(), SpansProbe.READY)
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_firmware_check_answered_with_anything_else_resyncs(self):
        self.t.queue_token(TOK_FAIL)
        self.t.queue_stale(b"Timeout!\n")
        self.c.connect()
        self.assertEqual(self.c.firmware, "unknown")
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_spans_probe_whose_replies_went_unheard_resyncs(self):
        # The refusal the zero header owes may still be on its way.
        for name, token in (("token reply timed out", None), ("ack, no refusal", TOK_ACK)):
            with self.subTest(name):
                if token is not None:
                    self.t.queue_token(token)
                self.c.probe_spans()
                self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_spans_probe_answered_by_chatter_without_the_refusal_resyncs(self):
        # The firmware may act on the token after its chatter, and its ack and
        # the zero header's refusal may still be on their way.
        self.t.queue_raw(b"TR")
        self.assertIs(self.c.probe_spans(), SpansProbe.ABSENT)
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_spans_probe_that_heard_the_refusal_stays_in_step(self):
        self.t.on_header = tr_dma._SPANS_COUNT_REFUSAL.encode()
        for name, token in (("token reply timed out", None), ("ack", TOK_ACK)):
            with self.subTest(name):
                if token is not None:
                    self.t.queue_token(token)
                self.assertIs(self.c.probe_spans(), SpansProbe.READY)
                self.assertEqual(_next_write_events(self.t, self.c), _IN_STEP)

    def test_a_spans_probe_answered_by_a_held_bus_stays_in_step(self):
        self.t.queue_token(TOK_FAIL)
        self.assertIs(self.c.probe_spans(), SpansProbe.HELD)
        self.assertEqual(_next_write_events(self.t, self.c), _IN_STEP)

    def test_a_text_reply_that_went_unheard_resyncs(self):
        commands = {"ping": self.c.ping, "reset": self.c.reset}
        for name, command in commands.items():
            with self.subTest(command=name):
                command()  # nothing queued: the drain heard no reply
                self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_text_reply_that_came_stays_in_step(self):
        for name, command, text in (
            ("ping", self.c.ping, b"TeensyROM is ready"),
            ("reset", self.c.reset, b"Reset cmd received"),
        ):
            with self.subTest(command=name):
                self.t.queue_stale(text)
                command()
                self.assertEqual(_next_write_events(self.t, self.c), _IN_STEP)

    def test_an_interrupt_awaiting_the_spans_token_reply_closes_the_command(self):
        # The firmware's wait for a header outlasts the resync, so the zero
        # header has to go out on every exit.
        self.t.cut_next_recv = True
        with self.assertRaises(KeyboardInterrupt):
            self.c.write_spans([(0x4000, b"\x01")], 32, 40)
        self.assertEqual(bytes(self.t.sent), _SPANS_TOK + bytes(4))
        self.assertEqual(_next_write_events(self.t, self.c), _RESYNCED)

    def test_a_spans_probe_whose_token_reply_read_failed_still_sends_the_zero_header(self):
        for name, exc in (("interrupt", KeyboardInterrupt), ("transport error", OSError)):
            with self.subTest(name):
                t = _CutLoopback()
                c = TRClient(t)
                with mock.patch.object(t, "recv_exact", side_effect=exc):
                    with self.assertRaises(exc):
                        c.probe_spans()
                self.assertEqual(bytes(t.sent), _SPANS_TOK + bytes(4))
                self.assertEqual(_next_write_events(t, c), _RESYNCED)

    def test_both_transports_resync_after_a_cut_write(self):
        class _CutSerial(_FakeSerial):
            def write(self, data: bytes) -> None:
                raise KeyboardInterrupt

        class _CutSocket(_FakeSocket):
            def sendall(self, data: bytes) -> None:
                raise KeyboardInterrupt

        serial_t = SerialTransport("COM_FAKE")
        serial_t._ser = _CutSerial()
        tcp_t = TcpTransport("host")
        tcp_t._sock = _CutSocket()  # type: ignore[assignment]
        for transport in (serial_t, tcp_t):
            with self.subTest(transport=transport.description):
                client = TRClient(transport)
                with self.assertRaises(KeyboardInterrupt):
                    client.write_segment(0xD020, b"\x0e")
                with (
                    mock.patch.object(transport, "drain_text", return_value="") as drain,
                    mock.patch.object(transport, "send_all"),
                    mock.patch.object(
                        transport, "recv_exact", return_value=bytes([TOK_ACK & 0xFF, TOK_ACK >> 8])
                    ),
                ):
                    client.write_segment(0xD020, b"\x0e")
                drain.assert_called_once_with(tr_dma._RESYNC_QUIET_S)


class _LateReplyLink(TRTransport):
    """A link on a clock only sleeps advance. Each send queues its reply at a
    virtual arrival time, recv_exact waits up to io_timeout for the next one,
    and drain_text returns once quiet_s passes with nothing arriving."""

    io_timeout = 2.0

    def __init__(self, clock: SleepDrivenClock):
        self.clock = clock
        self.replies: list[tuple[float, bytes]] = []
        self.answers: list[tuple[float, int]] = []  # (delay, token) per send

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    @property
    def description(self) -> str:
        return "late-reply"

    def send_all(self, data: bytes) -> None:
        delay, tok = self.answers.pop(0)
        self.replies.append((self.clock.monotonic() + delay, bytes([tok & 0xFF, tok >> 8])))
        self.replies.sort()

    def _arrived(self) -> bytes:
        now = self.clock.monotonic()
        out = b"".join(b for at, b in self.replies if at <= now)
        self.replies = [(at, b) for at, b in self.replies if at > now]
        return out

    def recv_exact(self, n: int) -> bytes:
        if self.replies and self.replies[0][0] - self.clock.monotonic() <= self.io_timeout:
            self.clock.sleep(max(0.0, self.replies[0][0] - self.clock.monotonic()))
            now = self.clock.monotonic()
            got = b"".join(b for at, b in self.replies if at <= now)[:n]
            if len(got) == n:
                self.replies.pop(0)
                return got
        self.clock.sleep(self.io_timeout)
        raise TRError("read timed out")

    def drain_text(self, quiet_s: float = 0.2) -> str:
        got = self._arrived()
        while self.replies and self.replies[0][0] - self.clock.monotonic() <= quiet_s:
            self.clock.sleep(max(0.0, self.replies[0][0] - self.clock.monotonic()))
            got += self._arrived()
        self.clock.sleep(quiet_s)
        return got.decode("ascii", errors="replace")


class LateSpansReplyTest(unittest.TestCase):
    """A WriteC64Spans job may run past io_timeout on a bus that stalls its
    slices, so its reply can come after the client gave up; no later command
    may read it as its own."""

    def test_a_spans_reply_later_than_io_timeout_is_not_read_by_a_later_command(self):
        clock = _frozen_clock(self)
        t = _LateReplyLink(clock)
        c = TRClient(t)
        late_s = tr_dma._SPANS_JOB_CEILING_S - 0.5  # inside the firmware's bound
        t.answers = [(0.0, TOK_ACK), (0.0, TOK_ACK), (late_s, TOK_ACK)]
        with self.assertRaises(TRError):
            c.write_spans([(0x4000, b"\x01" * 1024)], 32, 40)
        t.answers = [(0.0, TOK_ACK)]
        c.write_segment(0xD020, b"\x0e")
        clock.sleep(late_s)  # idle long enough for the late reply to have come
        t.answers = [(0.0, TOK_FAIL)]
        # This command's own reply is a refusal; read as the late ack, it
        # would report a write that never landed as done.
        with self.assertRaises(tr_dma.TRNak):
            c.write_segment(0xD020, b"\x0e")

    def test_a_drain_cut_by_its_cap_is_followed_by_another_until_the_reply_is_due(self):
        clock = _frozen_clock(self)
        t = _LateReplyLink(clock)
        c = TRClient(t)
        t.answers = [(0.0, TOK_ACK), (0.0, TOK_ACK), (60.0, TOK_ACK)]
        with self.assertRaises(TRError):
            c.write_spans([(0x4000, b"\x01" * 1024)], 32, 40)
        drains: list[float] = []

        def chatter_until_the_cap(quiet_s: float = 0.2) -> str:
            # Bytes keep coming, so drain_text returns at its hard cap.
            drains.append(quiet_s)
            clock.sleep(min(quiet_s, tr_dma._DRAIN_MAX_S - 1.0))
            return "chatter" if len(drains) == 1 else ""

        t.replies.clear()
        t.answers = [(0.0, TOK_ACK)]
        with mock.patch.object(t, "drain_text", side_effect=chatter_until_the_cap):
            c.write_segment(0xD020, b"\x0e")
        # The first drain is asked for what the job's ceiling leaves after the
        # timed-out read, and the second for what the capped first one left.
        cap_cut = tr_dma._DRAIN_MAX_S - 1.0
        self.assertEqual(len(drains), 2)
        self.assertAlmostEqual(drains[0], tr_dma._SPANS_JOB_CEILING_S - t.io_timeout)
        self.assertAlmostEqual(drains[1], drains[0] - cap_cut)


class _FloodingLoopback(_SpansLoopback):
    """A loopback whose next `floods` drains run to drain_text's hard cap, the
    way a device that never goes quiet makes them, and return nothing a
    sanitized drain would keep."""

    def __init__(self, clock: SleepDrivenClock):
        super().__init__()
        self.clock = clock
        self.floods = 0

    def drain_text(self, quiet_s: float = 0.2) -> str:
        text = super().drain_text(quiet_s)
        self.drain_capped = self.floods > 0
        if self.drain_capped:
            self.floods -= 1
            self.clock.sleep(tr_dma._DRAIN_MAX_S)
        else:
            self.clock.sleep(quiet_s)
        return text


class ResyncCapTest(unittest.TestCase):
    """A resync drain that its hard cap ended is not silence: past the wait,
    the device is still sending, and nothing may go out."""

    def setUp(self):
        self.clock = _frozen_clock(self)
        self.t = _FloodingLoopback(self.clock)
        self.c = TRClient(self.t)
        with self.assertRaises(TRError):
            self.c.write_segment(0xD020, b"\x0e")  # no reply: out of step

    def test_a_device_still_sending_past_the_wait_fails_the_command_unsent(self):
        self.t.floods = 10
        self.t.events.clear()
        self.t.queue_token(TOK_ACK)
        with self.assertRaisesRegex(TRError, "never went quiet"):
            self.c.write_segment(0xD020, b"\x0e")
        self.assertEqual(
            self.t.events,
            [("quiet", tr_dma._RESYNC_QUIET_S), ("quiet", tr_dma._RESYNC_MIN_QUIET_S)],
        )
        # Still out of step: once the device goes quiet, the next command
        # resyncs before it sends.
        self.t.floods = 0
        self.assertEqual(
            _next_write_events(self.t, self.c),
            [("quiet", tr_dma._RESYNC_MIN_QUIET_S), ("send", _NEXT_FRAME)],
        )

    def test_a_drain_capped_inside_the_wait_is_followed_by_one_that_hears_quiet(self):
        self.t.floods = 1
        self.assertEqual(
            _next_write_events(self.t, self.c),
            [
                ("quiet", tr_dma._RESYNC_QUIET_S),
                ("quiet", tr_dma._RESYNC_MIN_QUIET_S),
                ("send", _NEXT_FRAME),
            ],
        )


class CappedReplyDrainTest(unittest.TestCase):
    """A reply drain that its hard cap ended got its line while the device was
    still sending, so the next command resyncs before it sends."""

    def setUp(self):
        self.t = _FloodingLoopback(_frozen_clock(self))
        self.c = TRClient(self.t)
        self.t.floods = 1

    def test_reset(self):
        self.t.queue_stale(b"Reset cmd received\n")
        self.c.reset()
        self.assertEqual(
            _next_write_events(self.t, self.c),
            [("quiet", tr_dma._RESYNC_MIN_QUIET_S), ("send", _NEXT_FRAME)],
        )

    def test_ping(self):
        self.t.queue_stale(b"TeensyROM ready\n")
        self.assertEqual(self.c.ping(), "TeensyROM ready")
        self.assertEqual(
            _next_write_events(self.t, self.c),
            [("quiet", tr_dma._RESYNC_MIN_QUIET_S), ("send", _NEXT_FRAME)],
        )


if __name__ == "__main__":
    unittest.main()
