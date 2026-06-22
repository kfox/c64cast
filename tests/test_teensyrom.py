"""Tests for the TeensyROM+ transport framing + backend (Phase 1).

No hardware: a `LoopbackTransport` captures the exact bytes the client puts
on the wire and replies with queued tokens, so the framing (big-endian
tokens + fields), the ack handshake, the ReadC64Mem round-trip + read-
capability probe, and the PostFile/LaunchFile/reset workflows are pinned
against the firmware protocol.
"""

from __future__ import annotations

import struct
import unittest
from dataclasses import replace
from unittest import mock

from c64cast import config as cfgmod
from c64cast.api import _DEFAULT_PLAYER_LAYOUT
from c64cast.backend import TEENSYROM_PROFILE, BackendCapabilityError, make_backend
from c64cast.teensyrom_api import TeensyROMBackend
from c64cast.teensyrom_dma import (
    DRIVE_SD,
    TOK_ACK,
    TOK_FAIL,
    TOK_FW_FULL,
    TOK_READ_C64_MEM,
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

    def test_read_segment_is_big_endian_with_ack_then_data(self):
        # ReadC64Mem: token 0x64FD + addr(BE) + len(BE), then the ack, then
        # `len` data bytes. $FFFC reads back the KERNAL reset vector (E2 FC).
        self.t.queue_token(TOK_ACK)
        self.t.queue_raw(b"\xe2\xfc")
        out = self.client.read_segment(0xFFFC, 2)
        self.assertEqual(self.t.sent, bytes([0x64, 0xFD, 0xFF, 0xFC, 0x00, 0x02]))
        self.assertEqual(out, b"\xe2\xfc")
        # Sanity: the token constant matches the wire bytes.
        self.assertEqual(TOK_READ_C64_MEM, 0x64FD)

    def test_read_segment_nak_raises(self):
        self.t.queue_token(TOK_FAIL)
        with self.assertRaises(TRError):
            self.client.read_segment(0x028D, 1)

    def test_read_segment_ack_parsed_little_endian(self):
        # Ack 0x64CC arrives LSB-first (CC 64); a big-endian parse would make
        # a valid read look like an error before the data is even read.
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


class BackendTest(unittest.TestCase):
    def _backend(self, read: bool = True):
        t = LoopbackTransport()
        t.queue_token(TOK_FW_FULL)  # consumed by connect()'s fw_check
        # __init__ probes ReadC64Mem (a 2-byte $FFFC read). Feed a successful
        # round-trip (read=True) so supports_read stays set, or a NAK
        # (read=False) so it downgrades to the old read-free behaviour.
        if read:
            t.queue_token(TOK_ACK)  # probe ack
            t.queue_raw(b"\xe2\xfc")  # 2 ROM bytes ($FFFC)
        else:
            t.queue_token(TOK_FAIL)  # probe NAK -> supports_read False
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

    def test_read_probe_sets_supports_read(self):
        # A firmware that answers the ReadC64Mem probe keeps supports_read True
        # (so cli.py builds the keyboard poller).
        b, _ = self._backend(read=True)
        self.assertTrue(b.profile.supports_read)

    def test_read_probe_downgrades_on_old_firmware(self):
        # A firmware that NAKs the probe is honestly reported as read-free,
        # so callers fall back to the control plane instead of polling forever.
        b, _ = self._backend(read=False)
        self.assertFalse(b.profile.supports_read)

    def test_read_memory_round_trip(self):
        b, t = self._backend()
        t.queue_token(TOK_ACK)
        t.queue_raw(b"\x42")  # the byte living at $028D
        out = b.read_memory(0x028D, 1)
        self.assertEqual(out, b"\x42")
        # The ReadC64Mem command (token + addr BE + len BE) is the tail of the
        # wire output, after the connect + probe prefix.
        self.assertTrue(bytes(t.sent).endswith(b"\x64\xfd\x02\x8d\x00\x01"))

    def test_read_memory_returns_none_on_nak(self):
        # keyboard.py + the menu poller depend on None (never a raise) meaning
        # "couldn't tell" so a blip doesn't crash the playlist.
        b, t = self._backend()
        t.queue_token(TOK_FAIL)
        self.assertIsNone(b.read_memory(0x028D, 1))

    def test_read_memory_returns_none_on_timeout(self):
        b, _ = self._backend()
        # No reply queued -> the transport underflows; read_memory swallows it.
        self.assertIsNone(b.read_memory(0x028D, 1))

    def test_reu_write_unsupported(self):
        # The TR has no REUWRITE opcode (supports_reu False); reu_write stays on
        # the ABC's raising default so the experimental REU paths gate on it.
        b, _ = self._backend()
        self.assertFalse(b.profile.supports_reu)
        with self.assertRaises(BackendCapabilityError):
            b.reu_write(0, b"\x00")

    # ---- SID player -------------------------------------------------------
    @staticmethod
    def _make_sid(*, load=0x1000, init=0x1003, play=0x1006, num_songs=1, payload_len=64):
        """Minimal valid PSID v2 header + payload (mirrors tests/test_api.py)."""
        h = bytearray(124)
        h[0:4] = b"PSID"
        h[4:6] = (2).to_bytes(2, "big")  # version
        h[6:8] = (124).to_bytes(2, "big")  # data offset
        h[8:10] = load.to_bytes(2, "big")
        h[10:12] = init.to_bytes(2, "big")
        h[12:14] = play.to_bytes(2, "big")
        h[14:16] = num_songs.to_bytes(2, "big")
        h[16:18] = (1).to_bytes(2, "big")  # start song
        return bytes(h) + bytes(payload_len)

    def test_run_sid_player_loads_then_vector_swaps(self):
        # Default (defer_audio False): DMA payload + player MC + re-INIT stub,
        # then a pure-DMA $0314/$0315 vector-swap to the re-INIT stub (which the
        # next kernal IRQ runs to JSR init + install the PLAY handler). NO reset,
        # NO LaunchFile, NO PostFile mid-stream — the player is started exactly
        # like a subtune cue, over the running IRQ-enabled clear-loop.
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
        # defer_audio=True (WaveformScene): run_sid_player loads the player but
        # leaves it SILENT — no $0314 swap, no divider tune — so the caller can
        # paint the scope first. begin_sid_audio then does the vector-swap that
        # actually starts INIT/PLAY. This is the "waveforms before audio" path.
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
        # The vector-swap launch needs the IRQ-enabled idle (cycle-clean fw,
        # proxied by supports_read). On older firmware the spin-stub idle masks
        # IRQs, so the swap would never fire — run_sid_player raises rather than
        # play silently.
        b, _ = self._backend(read=False)
        with self.assertRaises(BackendCapabilityError):
            b.run_sid_player(self._make_sid())

    def test_tune_play_divider_reads_cia1_on_tr(self):
        # Reads now work on TR, so the PLAY-rate divider auto-tune runs for real
        # (it degraded to N=1 on the read-free TR). Feed a CIA #1 Timer A latch
        # of $4000 (~61 Hz PLAY) -> divider 2, patched at the player MC.
        b, t = self._backend(read=True)
        b._sid_player_layout = _DEFAULT_PLAYER_LAYOUT
        for _ in range(8):  # 8 latch samples: ack + 2 data bytes each
            t.queue_token(TOK_ACK)
            t.queue_raw(b"\x00\x40")  # $4000 little-endian on the wire
        t.queue_token(TOK_ACK)  # divider write
        with mock.patch("c64cast.api.time.sleep"):
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
        # Cycle-clean firmware (supports_read True) launches the IRQ-enabled
        # BASIC clear-loop: DeleteFile -> PostFile -> LaunchFile, then (like the
        # spin path) DMA-clears the screen + suppresses the cursor blink to wipe
        # the loader "RUNNING.."/READY/cursor text TR LaunchFile leaves behind.
        # NO spin MC write to $C000 and NO $D011 blanking (the display stays on
        # — DEN-off would hang the DMA). The SID player needs no pre-uploaded
        # stub anymore (it starts via a pure-DMA $0314 swap), so bring-up is just
        # the clear-loop. Acks: 5 (delete+post clear-loop) + 2 (launch) +
        # 1 (screen clear) + 1 (cursor suppress) = 9.
        b, t = self._backend(read=True)
        for _ in range(9):
            t.queue_token(TOK_ACK)
        b.run_basic_clear_loop()
        sent = bytes(t.sent)
        self.assertNotIn(b"\x64\xfb\xc0\x00", sent)  # no spin MC ($C000)
        self.assertNotIn(b"\x64\xfb\xd0\x11", sent)  # display never blanked ($D011)
        self.assertIn(b"\x64\xfb\x04\x00", sent)  # screen-clear to $0400
        self.assertIn(b"\x64\xfb\x00\xcc", sent)  # cursor-blink suppress ($00CC)
        # The clear-loop PRG was deleted-then-posted-then-launched.
        i_del = sent.find(b"\x64\xcf")
        i_post = sent.find(b"\x64\xbb")
        i_launch = sent.find(b"\x64\x44")
        self.assertNotEqual(i_del, -1)
        self.assertLess(i_del, i_post)
        self.assertLess(i_post, i_launch)

    def test_bring_up_spin_stub_when_read_unsupported(self):
        # Old firmware (supports_read False) falls back to the spin stub: DMA
        # the spin MC to $C000, then DeleteFile -> PostFile -> LaunchFile the
        # SYS stub, then DMA-clear the screen. Acks: 1 (spin) +
        # 5 (delete+post spin stub) + 2 (launch) + 1 (screen clear) = 9.
        b, t = self._backend(read=False)
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

    def test_pause_idle_clears_screen_keeping_display_on(self):
        # On TR a pause must (1) NOT reset — a reset lands at the TeensyROM
        # menu, freezing $028D and stranding the resume-hold; and (2) NOT turn
        # the VIC display off — DEN=0 removes badlines, hanging the cycle-clean
        # DMA so reads/writes time out (HW-confirmed, wedges the TR). So it
        # clears screen RAM (spaces) + suppresses the cursor blink, with DEN
        # left ON. Asserts: WriteC64Mem to $0400 (clear) + $00CC (cursor), and
        # NO ResetC64 and NO $D011 (blank) write.
        b, t = self._backend(read=True)
        t.queue_token(TOK_ACK)  # screen-clear write (<4096 -> 1 seg)
        t.queue_token(TOK_ACK)  # cursor-blink suppress write
        b.pause_idle()
        sent = bytes(t.sent)
        self.assertNotIn(b"\x64\xee", sent)  # no ResetC64 anywhere
        self.assertNotIn(b"\x64\xfb\xd0\x11", sent)  # no $D011 write (display stays on)
        self.assertIn(b"\x64\xfb\x04\x00", sent)  # WriteC64Mem to $0400 (screen clear)
        self.assertIn(b"\x64\xfb\x00\xcc", sent)  # cursor-blink suppress ($00CC)

    def test_bring_up_tolerates_missing_file_on_delete(self):
        # First-ever run: the file doesn't exist, so DeleteFile NAKs; bring-up
        # must ignore that and still PostFile + LaunchFile. (Clear-loop path.)
        b, t = self._backend(read=True)
        t.queue_token(TOK_ACK)  # clear-loop delete open
        t.queue_token(TOK_FAIL)  # clear-loop delete body -> file not found (ignored)
        for _ in range(3):
            t.queue_token(TOK_ACK)  # clear-loop post open/header/data
        for _ in range(5):
            t.queue_token(TOK_ACK)  # SID stub delete (2) + post (3)
        for _ in range(2):
            t.queue_token(TOK_ACK)  # launch open/body
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
        from c64cast.cli import _coerce_reu_for_backend

        cfg = self._cfg(pump=True, staged=True)
        with self.assertLogs("c64cast", level="WARNING"):
            _coerce_reu_for_backend(cfg, self._backend(supports_reu=False))
        self.assertFalse(cfg.audio.use_reu_pump)
        self.assertFalse(cfg.video.use_reu_staged)

    def test_no_reu_backend_leaves_auto_staged_alone(self):
        # "auto" self-heals elsewhere; the coercion only touches explicit true.
        from c64cast.cli import _coerce_reu_for_backend

        cfg = self._cfg(pump=False, staged="auto")
        _coerce_reu_for_backend(cfg, self._backend(supports_reu=False))
        self.assertEqual(cfg.video.use_reu_staged, "auto")

    def test_reu_backend_unchanged(self):
        from c64cast.cli import _coerce_reu_for_backend

        cfg = self._cfg(pump=True, staged=True)
        _coerce_reu_for_backend(cfg, self._backend(supports_reu=True))
        self.assertTrue(cfg.audio.use_reu_pump)
        self.assertIs(cfg.video.use_reu_staged, True)


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
