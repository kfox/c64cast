"""Tests for the TeensyROM+ transport framing + backend (Phase 1).

No hardware: a `LoopbackTransport` captures the exact bytes the client puts
on the wire and replies with queued tokens, so the framing (big-endian
tokens + fields), the ack handshake, and the PostFile/LaunchFile/reset
workflows are pinned against the firmware protocol.
"""

from __future__ import annotations

import struct
import unittest
from dataclasses import replace

from c64cast import config as cfgmod
from c64cast.backend import TEENSYROM_PROFILE, BackendCapabilityError, make_backend
from c64cast.teensyrom_api import TeensyROMBackend
from c64cast.teensyrom_dma import (
    DRIVE_SD,
    TOK_ACK,
    TOK_FAIL,
    TOK_FW_FULL,
    TRClient,
    TRError,
    TRTransport,
)


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

    # test helpers
    def queue_token(self, tok: int) -> None:
        # Firmware sends replies little-endian (LSB first).
        self._inbox += bytes([tok & 0xFF, (tok >> 8) & 0xFF])

    def queue_raw(self, data: bytes) -> None:
        self._inbox += data

    def queue_stale(self, data: bytes) -> None:
        self._stale += data

    # TRTransport
    def connect(self) -> None:
        pass

    def send_all(self, data: bytes) -> None:
        self.sent += data

    def recv_exact(self, n: int) -> bytes:
        if len(self._inbox) < n:
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


class FramingTest(unittest.TestCase):
    def setUp(self):
        self.t = LoopbackTransport()
        self.client = TRClient(self.t)

    def test_write_segment_is_big_endian_with_ack(self):
        # The single most important contract: token + addr + len are MSB-first.
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
        # The pre-fix bug: bytes 64 CC parsed little-endian = 0xCC64, not an ack.
        self.t.queue_raw(b"\x64\xcc")
        with self.assertRaises(TRError):
            self.client.write_segment(0xD020, b"\x02")

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


class BackendTest(unittest.TestCase):
    def _backend(self):
        t = LoopbackTransport()
        t.queue_token(TOK_FW_FULL)  # consumed by connect()'s fw_check
        b = TeensyROMBackend(t, profile=replace(TEENSYROM_PROFILE), storage="sd")
        return b, t

    def test_emit_chunks_large_writes(self):
        b, t = self._backend()
        # A payload larger than MAX_SEGMENT_BYTES splits into multiple acked
        # segments; queue one ack per expected segment.
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

    def test_read_memory_is_unsupported(self):
        b, _ = self._backend()
        self.assertFalse(b.profile.supports_read)
        with self.assertRaises(BackendCapabilityError):
            b.read_memory(0x028D, 1)

    def test_sid_player_unsupported_in_phase1(self):
        b, _ = self._backend()
        with self.assertRaises(BackendCapabilityError):
            b.run_sid_player(b"PSID")

    def test_bring_up_dmas_spin_stub_then_deletes_uploads_launches(self):
        # Bring-up DMAs the spin MC to $C000 (one WriteC64Mem segment), then
        # DeleteFile -> PostFile -> LaunchFile the SYS stub. Acks: 1 (spin) +
        # 2 (delete) + 3 (post) + 2 (launch) = 8.
        b, t = self._backend()
        # 1 (spin MC) + 2 (delete) + 3 (post) + 2 (launch) + 1 (screen clear).
        for _ in range(9):
            t.queue_token(TOK_ACK)
        b.run_basic_clear_loop()
        sent = bytes(t.sent)
        # Tokens in order: 0x64FB (spin write), 0x64CF, 0x64BB, 0x6444.
        i_write = sent.find(b"\x64\xfb")
        i_del = sent.find(b"\x64\xcf")
        i_post = sent.find(b"\x64\xbb")
        i_launch = sent.find(b"\x64\x44")
        self.assertNotEqual(i_write, -1)
        self.assertLess(i_write, i_del)
        self.assertLess(i_del, i_post)
        self.assertLess(i_post, i_launch)
        # The spin MC is written to $C000 (BE address bytes C0 00).
        self.assertEqual(sent[i_write : i_write + 4], b"\x64\xfb\xc0\x00")

    def test_bring_up_tolerates_missing_file_on_delete(self):
        # First-ever run: the file doesn't exist, so DeleteFile NAKs; bring-up
        # must ignore that and still PostFile + LaunchFile.
        b, t = self._backend()
        t.queue_token(TOK_ACK)  # spin MC write
        t.queue_token(TOK_ACK)  # delete open
        t.queue_token(TOK_FAIL)  # delete body -> file not found (ignored)
        for _ in range(3):
            t.queue_token(TOK_ACK)  # post open/header/data
        for _ in range(2):
            t.queue_token(TOK_ACK)  # launch open/body
        t.queue_token(TOK_ACK)  # post-launch screen clear
        b.run_basic_clear_loop()
        self.assertIn(b"\x64\x44", bytes(t.sent))  # launch still happened

    def test_semantic_helpers_are_pure_writes(self):
        # silence_sid / disable_case_switch are inherited from the buffered
        # base and work on any write-capable backend.
        b, t = self._backend()
        for _ in range(10):
            t.queue_token(TOK_ACK)
        b.disable_case_switch()  # $0291 = $80
        # last write_segment frame ends with the value byte 0x80
        self.assertEqual(b.stats["writes"], 1)


class MakeBackendTest(unittest.TestCase):
    def test_serial_requires_port(self):
        cfg = cfgmod.Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "serial"
        cfg.teensyrom.serial_port = None
        with self.assertRaises(ValueError):
            make_backend(cfg)

    def test_tcp_requires_host(self):
        cfg = cfgmod.Config()
        cfg.hardware.backend = "teensyrom"
        cfg.teensyrom.transport = "tcp"
        cfg.teensyrom.host = None
        with self.assertRaises(ValueError):
            make_backend(cfg)


if __name__ == "__main__":
    unittest.main()
