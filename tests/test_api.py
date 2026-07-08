"""Tests for Ultimate64API — specifically the rolling DMA latency window
and its formatters. Other parts of the API (write_region delta caching,
listener notifications) are exercised by the higher-level scene/mode
tests, not duplicated here. Wire-level protocol coverage lives in
test_socket_dma.py."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from c64cast.api import (
    _REINIT_PATCH_BANK,
    _REINIT_PATCH_INIT_HI,
    _REINIT_PATCH_INIT_LO,
    _REINIT_PATCH_IRQ_HI,
    _REINIT_PATCH_IRQ_LO,
    _REINIT_PATCH_SONG,
    _RELOCATED_STUB_OFFSET,
    _SID_PATCH_CTR_DEC_HI,
    _SID_PATCH_CTR_DEC_LO,
    _SID_PATCH_CTR_INIT_HI,
    _SID_PATCH_CTR_INIT_LO,
    _SID_PATCH_CTR_RELOAD_HI,
    _SID_PATCH_CTR_RELOAD_LO,
    _SID_PATCH_DIVIDER,
    _SID_PATCH_INIT_HI,
    _SID_PATCH_INIT_LO,
    _SID_PATCH_INITBANK,
    _SID_PATCH_IRQ_HI,
    _SID_PATCH_IRQ_LO,
    _SID_PATCH_PLAY_HI,
    _SID_PATCH_PLAY_LO,
    _SID_PATCH_PLAYBANK,
    _SID_PATCH_SONG,
    _SID_PATCH_SPIN_HI,
    _SID_PATCH_SPIN_LO,
    REINIT_STUB_ADDR,
    REINIT_STUB_TEMPLATE,
    SID_PLAYER_COUNTER_OFFSET,
    SID_PLAYER_DIVIDER_OFFSET,
    SID_PLAYER_IRQ_HANDLER_OFFSET,
    SID_PLAYER_MC_ADDR,
    SID_PLAYER_MC_TEMPLATE,
    SID_PLAYER_SPIN_OFFSET,
    ParsedPsid,
    Ultimate64API,
    _bank_for_addr_hi,
    _build_basic_sys_stub,
    _choose_player_layout,
    _find_free_layout,
    _init_bank_for,
    _layout_fits,
    _play_bank_for,
    _PlayerLayout,
    parse_psid_for_player,
)
from c64cast.c64 import CPU, VECTORS, VIC
from c64cast.socket_dma import SocketDMAError


class DmaLatencyTest(unittest.TestCase):
    def setUp(self):
        # Patch connect() so the constructor doesn't try to open a real
        # TCP socket. dmawrite/flush are also stubbed on the instance
        # below for tests that need to drive latency samples directly.
        patcher = patch("c64cast.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")

    def tearDown(self):
        with patch.object(self.api.socket_dma, "close"):
            self.api.close()

    def test_empty_summary(self):
        avg, p50, p95, mx, n = self.api.socket_dma.latency_summary()
        self.assertEqual((avg, p50, p95, mx, n), (0.0, 0.0, 0.0, 0.0, 0))
        self.assertIsNone(self.api.format_write_latency())

    def test_format_string(self):
        # Seed the window directly so we don't depend on a real socket.
        for _ in range(3):
            self.api.socket_dma._latencies.append(0.010)  # 10 ms
        line = self.api.format_write_latency()
        self.assertIsNotNone(line)
        assert line is not None  # narrow for type-checker
        for token in (
            "u64 dma latency",
            "n=3",
            "avg=10.0",
            "p50=10.0",
            "p95=10.0",
            "max=10.0",
            "ms",
        ):
            self.assertIn(token, line)

    def test_summary_percentiles(self):
        # 100 samples 1..100 ms — easy nearest-rank percentiles to verify.
        for i in range(1, 101):
            self.api.socket_dma._latencies.append(i / 1000.0)
        avg, p50, p95, mx, n = self.api.socket_dma.latency_summary()
        self.assertEqual(n, 100)
        self.assertAlmostEqual(avg, sum(range(1, 101)) / 100 / 1000.0)
        # nearest-rank: int(0.50 * 100) = 50 → sorted[50] = 51 ms.
        self.assertAlmostEqual(p50, 0.051)
        # int(0.95 * 100) = 95 → sorted[95] = 96 ms.
        self.assertAlmostEqual(p95, 0.096)
        self.assertAlmostEqual(mx, 0.100)


class DmaWriteErrorHandlingTest(unittest.TestCase):
    """_emit must absorb transient transport failures — both raw
    OSError from sendall AND SocketDMAError from a failed reconnect
    handshake (IDENTIFY/auth timeout) — so a brief U64 hiccup doesn't
    crash the active scene and abort the playlist."""

    def setUp(self):
        patcher = patch("c64cast.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")

    def tearDown(self):
        with patch.object(self.api.socket_dma, "close"):
            self.api.close()

    def test_oserror_is_absorbed_and_counted(self):
        with patch.object(self.api.socket_dma, "dmawrite", side_effect=TimeoutError("timed out")):
            self.api._emit(0xD020, b"\x0e")
        self.assertEqual(self.api.stats["errors"], 1)
        self.assertEqual(self.api.stats["writes"], 0)

    def test_socketdmaerror_from_reconnect_is_absorbed(self):
        # The production crash: send times out, transparent reconnect
        # attempt's IDENTIFY also times out and is re-raised as
        # SocketDMAError. _emit must NOT propagate it — the playlist
        # would otherwise tear down the current scene and advance.
        with patch.object(
            self.api.socket_dma, "dmawrite", side_effect=SocketDMAError("no reply to IDENTIFY")
        ):
            self.api._emit(0xD020, b"\x0e")
        self.assertEqual(self.api.stats["errors"], 1)
        self.assertEqual(self.api.stats["writes"], 0)

    def test_consecutive_errors_reset_on_success(self):
        # Drive a couple of failures then a success — the consecutive
        # counter feeds the escalating warning ladder, so a recovery
        # must reset it or the user sees stale "200 consecutive" alerts.
        with patch.object(self.api.socket_dma, "dmawrite", side_effect=SocketDMAError("boom")):
            self.api._emit(0xD020, b"\x0e")
            self.api._emit(0xD020, b"\x0e")
        self.assertEqual(self.api._consecutive_errors, 2)
        with patch.object(self.api.socket_dma, "dmawrite"):
            self.api._emit(0xD020, b"\x0e")
        self.assertEqual(self.api._consecutive_errors, 0)


class RunSidPlayerTest(unittest.TestCase):
    """Validation, header parsing, and MC-byte patching for run_sid_player.

    The C64-side player swaps the firmware's /v1/runners:sidplay UI for
    a tiny BASIC stub that SYSes a hand-rolled 6502 player at $C300.
    These tests cover the host-side parts of that — the contract is:
      * RSIDs are refused; PSIDs accepted.
      * load_addr inside the BASIC-stub window ($0801-$081F) is refused.
      * play_addr == 0 (INIT installs own IRQ) is refused.
      * The 5 patch bytes inside the MC template are written in the
        right slots: song-1, init lo/hi, play lo/hi.
    """

    def setUp(self):
        patcher = patch("c64cast.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")
        # Stub the wire-level write + flush + REST POST so the test runs
        # against in-process state only.
        # dma_writes tracks the SID-upload contract this class tests (payload
        # / player MC / re-INIT stub); the pre-flight blank_display() write to
        # $D011 (see _launch_sid_player) is recorded separately since it's
        # orthogonal to that contract and would otherwise shift every
        # index-based assertion below.
        self.dma_writes: list[tuple[int, bytes]] = []
        self.blank_writes: list[tuple[int, bytes]] = []

        def _fake_emit(addr, payload):
            if addr == VIC.D011_CONTROL_1:
                self.blank_writes.append((addr, bytes(payload)))
            else:
                self.dma_writes.append((addr, bytes(payload)))

        self.api._emit = _fake_emit  # type: ignore[method-assign]
        patch.object(self.api, "flush").start()
        self.posts: list[tuple[str, bytes]] = []

        def _fake_post(url, files=None, **_):
            payload = files["file"][1] if files else b""
            self.posts.append((url, bytes(payload)))

            class _R:
                def raise_for_status(self):
                    pass

            return _R()

        patch.object(self.api.session, "post", side_effect=_fake_post).start()
        # _tune_play_divider sleeps + REST-reads CIA #1 after run_sid_player.
        # No-op it here so the per-tune tests stay fast and don't accidentally
        # hit the (fake) network.
        patch.object(self.api, "_tune_play_divider", return_value=1).start()

    def tearDown(self):
        patch.stopall()
        with patch.object(self.api.socket_dma, "close"):
            self.api.close()

    # ---- header construction helper ----------------------------------
    @staticmethod
    def _make_sid(
        *,
        magic=b"PSID",
        load=0x1000,
        init=0x1003,
        play=0x1006,
        num_songs=4,
        start_song=1,
        payload_len=64,
    ):
        h = bytearray(124)
        h[0:4] = magic
        h[4:6] = (2).to_bytes(2, "big")
        # data_offset = 124 (v2 header)
        h[6:8] = (124).to_bytes(2, "big")
        h[8:10] = load.to_bytes(2, "big")
        h[10:12] = init.to_bytes(2, "big")
        h[12:14] = play.to_bytes(2, "big")
        h[14:16] = num_songs.to_bytes(2, "big")
        h[16:18] = start_song.to_bytes(2, "big")
        return bytes(h) + bytes(payload_len)

    # ---- validation --------------------------------------------------
    def test_rejects_rsid(self):
        with self.assertRaisesRegex(ValueError, "RSID"):
            self.api.run_sid_player(self._make_sid(magic=b"RSID"))

    def test_rejects_bad_magic(self):
        with self.assertRaisesRegex(ValueError, "not a SID file"):
            self.api.run_sid_player(b"NOPE" + bytes(120))

    def test_rejects_load_addr_overlapping_basic_stub(self):
        # $0801 is inside the BASIC stub window — would be clobbered.
        with self.assertRaisesRegex(ValueError, "BASIC SYS stub"):
            self.api.run_sid_player(self._make_sid(load=0x0801))
        with self.assertRaisesRegex(ValueError, "BASIC SYS stub"):
            self.api.run_sid_player(self._make_sid(load=0x081F))

    def test_accepts_load_addr_just_past_stub(self):
        # $0820 is the first acceptable load address.
        self.api.run_sid_player(self._make_sid(load=0x0820))
        # 3 DMA writes (payload + main MC + re-INIT stub) + 1 POST.
        self.assertEqual(len(self.dma_writes), 3)
        self.assertEqual(len(self.posts), 1)

    def test_mc_restores_master_volume_after_init(self):
        # The player MC must write $D418=$0F right after JSR init returns,
        # so the SID is audible regardless of whether an earlier
        # audio.stop() zeroed $D418 (clean cutoff for videos) or
        # whether INIT itself touched $D418. Verify the literal bytes
        # land at the documented offsets — a regression here would
        # silently mute the SID.
        self.api.run_sid_player(self._make_sid(load=0x0820))
        _, mc = self.dma_writes[1]
        # After JSR init the player restores the resting bank (LDA #$37 /
        # STA $01 at 14-17) THEN the master volume. Offsets 18-22: LDA #$0F
        # (A9 0F) ; STA $D418 (8D 18 D4).
        self.assertEqual(
            mc[18:23], b"\xa9\x0f\x8d\x18\xd4", "MC must restore $D418=$0F after the bank restore"
        )

    def test_rejects_play_addr_zero(self):
        with self.assertRaisesRegex(ValueError, "play_addr=0"):
            self.api.run_sid_player(self._make_sid(play=0))

    def test_rejects_out_of_range_song(self):
        with self.assertRaisesRegex(ValueError, "out of range"):
            self.api.run_sid_player(self._make_sid(num_songs=3), song=99)

    def test_rejects_tune_under_kernal_rom(self):
        # Code/data under KERNAL ROM ($E000-$FFFF) can't be exposed —
        # the player keeps KERNAL mapped for its $EA31 IRQ chain.
        with self.assertRaisesRegex(ValueError, "KERNAL ROM"):
            self.api.run_sid_player(self._make_sid(load=0xE000, init=0xE000, play=0xE003))

    # ---- CPU-port (memory bank) selection ----------------------------
    def test_bank_for_addr_hi_rule(self):
        # getBank rule mirrored from the U64 firmware (sidcommon.asm).
        self.assertEqual(_bank_for_addr_hi(0x10), CPU.PORT_DEFAULT)  # low RAM
        self.assertEqual(_bank_for_addr_hi(0x9F), CPU.PORT_DEFAULT)  # just below BASIC
        self.assertEqual(_bank_for_addr_hi(0xA0), CPU.PORT_BASIC_OUT)  # BASIC ROM
        self.assertEqual(_bank_for_addr_hi(0xBF), CPU.PORT_BASIC_OUT)
        self.assertEqual(_bank_for_addr_hi(0xC0), CPU.PORT_BASIC_OUT)  # $Cxxx -> $36
        self.assertEqual(_bank_for_addr_hi(0xD4), CPU.PORT_IO_OUT)  # I/O space
        self.assertEqual(_bank_for_addr_hi(0xE0), CPU.PORT_KERNAL_OUT)  # KERNAL ROM

    def test_init_play_bank_default_for_normal_tune(self):
        # A tune in ordinary low RAM keeps the default $37 for both banks.
        parsed = parse_psid_for_player(self._make_sid(load=0x1000, init=0x1003, play=0x1006))
        self.assertEqual(_init_bank_for(parsed), CPU.PORT_DEFAULT)
        self.assertEqual(_play_bank_for(parsed), CPU.PORT_DEFAULT)

    def test_init_bank_keys_on_load_end_not_load_start(self):
        # A tune loading from low RAM whose payload extends under BASIC ROM
        # gets $36 for init (load-end page), keyed on the END not the start.
        parsed = parse_psid_for_player(
            self._make_sid(load=0x9F00, init=0xC000, play=0xC003, payload_len=0x2000)
        )  # ends ~$BF00
        self.assertEqual(_init_bank_for(parsed), CPU.PORT_BASIC_OUT)
        self.assertEqual(_play_bank_for(parsed), CPU.PORT_BASIC_OUT)  # play $C0

    def test_under_basic_rom_tune_patches_both_bank_bytes(self):
        # Hyperion-2-like: init/play under BASIC ROM. Player MC carries $36
        # for BOTH the init-bank and play-bank slots; re-INIT stub carries
        # $36 for its init-bank slot.
        self.api.run_sid_player(self._make_sid(load=0xAE2A, init=0xAE2A, play=0xAE32))
        _, mc = self.dma_writes[1]
        _, stub = self.dma_writes[2]
        self.assertEqual(mc[_SID_PATCH_INITBANK], CPU.PORT_BASIC_OUT)
        self.assertEqual(mc[_SID_PATCH_PLAYBANK], CPU.PORT_BASIC_OUT)
        self.assertEqual(stub[_REINIT_PATCH_BANK], CPU.PORT_BASIC_OUT)
        # Each bank byte is consumed by an STA $01 immediately after it.
        self.assertEqual(mc[_SID_PATCH_INITBANK + 1 : _SID_PATCH_INITBANK + 3], bytes([0x85, 0x01]))
        self.assertEqual(mc[_SID_PATCH_PLAYBANK + 1 : _SID_PATCH_PLAYBANK + 3], bytes([0x85, 0x01]))

    def test_player_rests_at_default_bank_between_calls(self):
        # The resting bank is $37: restored right after JSR init and after
        # JSR play (LDA #$37 / STA $01 in both spots), even for an under-ROM
        # tune. This is what keeps tunes like Election from crashing.
        self.api.run_sid_player(self._make_sid(load=0xAE2A, init=0xAE2A, play=0xAE32))
        _, mc = self.dma_writes[1]
        # After JSR init (operand at _SID_PATCH_INIT_LO/_HI), bytes are
        # LDA #$37 / STA $01.
        after_init = _SID_PATCH_INIT_HI + 1
        self.assertEqual(mc[after_init : after_init + 4], bytes([0xA9, 0x37, 0x85, 0x01]))
        # After JSR play (operand at _SID_PATCH_PLAY_LO/_HI), same restore.
        after_play = _SID_PATCH_PLAY_HI + 1
        self.assertEqual(mc[after_play : after_play + 4], bytes([0xA9, 0x37, 0x85, 0x01]))

    # ---- MC patching -------------------------------------------------
    def test_mc_template_byte_offsets_round_trip(self):
        # Sanity: the named patch offsets land on the expected opcodes
        # (the address-bearing bytes themselves are 0x00 placeholders in
        # the template; they're filled per-tune by _build_player_mc).
        t = SID_PLAYER_MC_TEMPLATE
        # Leads with SEI then LDA #<initBank> / STA $01 (the CPU-port set).
        self.assertEqual(t[0], 0x78)  # SEI
        self.assertEqual(t[_SID_PATCH_INITBANK - 1], 0xA9)  # LDA #<initBank>
        self.assertEqual(t[_SID_PATCH_INITBANK], 0x37)  # default $37 seed
        self.assertEqual(
            t[_SID_PATCH_INITBANK + 1 : _SID_PATCH_INITBANK + 3], bytes([0x85, 0x01])
        )  # STA $01
        # IRQ handler leads with LDA #<playBank> / STA $01.
        self.assertEqual(t[_SID_PATCH_PLAYBANK - 1], 0xA9)  # LDA #<playBank>
        self.assertEqual(t[_SID_PATCH_PLAYBANK], 0x37)  # default seed
        self.assertEqual(
            t[_SID_PATCH_PLAYBANK + 1 : _SID_PATCH_PLAYBANK + 3], bytes([0x85, 0x01])
        )  # STA $01
        self.assertEqual(t[_SID_PATCH_SONG - 1], 0xA9)  # LDA #imm
        self.assertEqual(t[_SID_PATCH_INIT_LO - 1], 0x20)  # JSR
        self.assertEqual(t[_SID_PATCH_PLAY_LO - 1], 0x20)  # JSR (in IRQ)
        self.assertEqual(t[_SID_PATCH_IRQ_LO - 1], 0xA9)  # LDA #<irq
        self.assertEqual(t[_SID_PATCH_IRQ_HI - 1], 0xA9)  # LDA #>irq
        self.assertEqual(t[_SID_PATCH_SPIN_LO - 1], 0x4C)  # JMP <spin>
        # Tick-divider patch points: DEC counter / LDA #N / STA counter
        self.assertEqual(t[_SID_PATCH_CTR_INIT_LO - 1], 0x8D)  # STA abs
        self.assertEqual(t[_SID_PATCH_CTR_DEC_LO - 1], 0xCE)  # DEC abs
        self.assertEqual(t[_SID_PATCH_CTR_RELOAD_LO - 1], 0x8D)  # STA abs
        self.assertEqual(t[_SID_PATCH_DIVIDER - 1], 0xA9)  # LDA #N
        # Divider seed = 1 (chain-every-tick until host measures rate).
        self.assertEqual(t[_SID_PATCH_DIVIDER], 0x01)
        # Address-bearing offsets must derive from the chosen
        # player_base + these stable offset constants. _SID_PATCH_IRQ_*
        # points at LDA #imm operands, so the byte AT the offset is the
        # immediate value (= base + IRQ_HANDLER_OFFSET) once patched.
        self.assertEqual(SID_PLAYER_IRQ_HANDLER_OFFSET, 42)
        self.assertEqual(SID_PLAYER_SPIN_OFFSET, 39)
        self.assertEqual(SID_PLAYER_COUNTER_OFFSET, 72)
        self.assertEqual(SID_PLAYER_DIVIDER_OFFSET, 59)
        # Counter byte at the COUNTER_OFFSET position is seeded to 1.
        self.assertEqual(t[SID_PLAYER_COUNTER_OFFSET], 0x01)
        # Template length sanity — drift here usually means an offset
        # constant is stale.
        self.assertEqual(len(t), SID_PLAYER_COUNTER_OFFSET + 1)
        # Lean exit at offset 66: LDA $DC0D / JMP $EA81. Without the
        # $DC0D read the CIA #1 IRQ flag never clears and the IRQ
        # re-fires immediately; without $EA81 the CPU never returns.
        self.assertEqual(t[66:72], bytes([0xAD, 0x0D, 0xDC, 0x4C, 0x81, 0xEA]))
        # Chain path tail (offset 63-65) must chain to kernal $EA31.
        self.assertEqual(t[63:66], bytes([0x4C, 0x31, 0xEA]))

    def test_patched_mc_carries_song_init_and_play(self):
        self.api.run_sid_player(
            self._make_sid(load=0x2000, init=0x2003, play=0x2006, num_songs=8, start_song=1),
            song=5,
        )
        # The MC write is the second DMA call (payload first, MC second).
        addr, mc = self.dma_writes[1]
        self.assertEqual(addr, SID_PLAYER_MC_ADDR)
        self.assertEqual(mc[_SID_PATCH_SONG], 5 - 1)
        self.assertEqual(mc[_SID_PATCH_INIT_LO], 0x03)
        self.assertEqual(mc[_SID_PATCH_INIT_HI], 0x20)
        self.assertEqual(mc[_SID_PATCH_PLAY_LO], 0x06)
        self.assertEqual(mc[_SID_PATCH_PLAY_HI], 0x20)
        # Internal address slots resolve from the default layout's
        # player_base ($C300): irq = base + 42 = $C32A, spin = base + 39 = $C327.
        expected_irq = SID_PLAYER_MC_ADDR + SID_PLAYER_IRQ_HANDLER_OFFSET
        expected_spin = SID_PLAYER_MC_ADDR + SID_PLAYER_SPIN_OFFSET
        expected_counter = SID_PLAYER_MC_ADDR + SID_PLAYER_COUNTER_OFFSET
        self.assertEqual(mc[_SID_PATCH_IRQ_LO], expected_irq & 0xFF)
        self.assertEqual(mc[_SID_PATCH_IRQ_HI], (expected_irq >> 8) & 0xFF)
        self.assertEqual(mc[_SID_PATCH_SPIN_LO], expected_spin & 0xFF)
        self.assertEqual(mc[_SID_PATCH_SPIN_HI], (expected_spin >> 8) & 0xFF)
        # All three counter-address operands must point at the same byte
        # (the live counter at counter_addr); a desync would crash the
        # IRQ handler since DEC/STA would touch unrelated memory.
        for lo, hi in [
            (_SID_PATCH_CTR_INIT_LO, _SID_PATCH_CTR_INIT_HI),
            (_SID_PATCH_CTR_DEC_LO, _SID_PATCH_CTR_DEC_HI),
            (_SID_PATCH_CTR_RELOAD_LO, _SID_PATCH_CTR_RELOAD_HI),
        ]:
            self.assertEqual(mc[lo], expected_counter & 0xFF)
            self.assertEqual(mc[hi], (expected_counter >> 8) & 0xFF)

    def test_song_zero_picks_header_start_song(self):
        self.api.run_sid_player(
            self._make_sid(load=0x2000, init=0x2003, play=0x2006, num_songs=8, start_song=3),
            song=0,
        )
        _, mc = self.dma_writes[1]
        self.assertEqual(mc[_SID_PATCH_SONG], 3 - 1)

    def test_basic_stub_posted_targets_player_base(self):
        # After run_sid_player, the POSTed BASIC PRG's SYS argument must
        # be the same decimal address the player MC was uploaded to. If
        # they drift apart, BASIC would SYS into garbage.
        self.api.run_sid_player(self._make_sid(load=0x2000, init=0x2003, play=0x2006))
        self.assertEqual(len(self.posts), 1)
        _, prg = self.posts[0]
        # Find the SYS token (0x9E) and read the decimal digits after it.
        sys_idx = prg.index(b"\x9e")
        # Skip 0x9E + 0x20 (space), then ASCII digits, terminator 0x00.
        digits_end = prg.index(b"\x00", sys_idx)
        digits = prg[sys_idx + 2 : digits_end].decode("ascii")
        self.assertEqual(
            int(digits),
            SID_PLAYER_MC_ADDR,
            f"BASIC stub SYSes to {digits} but player MC was uploaded at {SID_PLAYER_MC_ADDR:#06x}",
        )

    def test_build_basic_sys_stub_round_trip(self):
        # The builder must produce a valid one-line `10 SYS <decimal>`
        # PRG for arbitrary addresses (the relocated-player path picks
        # non-default values).
        prg = _build_basic_sys_stub(0xC500)
        # Load address $0801.
        self.assertEqual(prg[:2], b"\x01\x08")
        # Next-line pointer: 0x0801 + 4 (ptr + line num) + 1 (SYS) + 1
        # (space) + len("50432") + 1 (EOL) = 0x0801 + 4 + 8 = 0x080D.
        self.assertEqual(prg[2:4], b"\x0d\x08")
        # Line number 10.
        self.assertEqual(prg[4:6], b"\x0a\x00")
        # SYS token + space + "50432" + EOL + end-of-program.
        self.assertEqual(prg[6:], b"\x9e\x2050432\x00\x00\x00")

    # ---- re-INIT stub (cue_song_reinit) ------------------------------
    def test_reinit_stub_uploaded_after_player_mc(self):
        # run_sid_player uploads the re-INIT stub as the 3rd DMA write
        # (after payload + main MC). cue_song_reinit later assumes it's
        # already in place — patching a non-existent stub would crash
        # the C64 on the next IRQ.
        self.api.run_sid_player(self._make_sid(load=0x2000, init=0x2003, play=0x2006))
        self.assertEqual(len(self.dma_writes), 3)
        addr, stub = self.dma_writes[2]
        self.assertEqual(addr, REINIT_STUB_ADDR)
        self.assertEqual(len(stub), len(REINIT_STUB_TEMPLATE))

    def test_reinit_stub_template_offsets(self):
        # Sanity: the named patch offsets land on the expected opcodes
        # (the address-bearing bytes are 0x00 placeholders in the template).
        t = REINIT_STUB_TEMPLATE
        # Leads with LDA #<bank> / STA $01 (no SEI — already in IRQ ctx).
        self.assertEqual(t[_REINIT_PATCH_BANK - 1], 0xA9)  # LDA #<bank>
        self.assertEqual(t[_REINIT_PATCH_BANK], 0x37)  # default seed
        self.assertEqual(
            t[_REINIT_PATCH_BANK + 1 : _REINIT_PATCH_BANK + 3], bytes([0x85, 0x01])
        )  # STA $01
        self.assertEqual(t[_REINIT_PATCH_SONG - 1], 0xA9)  # LDA #imm
        self.assertEqual(t[_REINIT_PATCH_INIT_LO - 1], 0x20)  # JSR
        self.assertEqual(t[_REINIT_PATCH_IRQ_LO - 1], 0xA9)  # LDA #<play
        self.assertEqual(t[_REINIT_PATCH_IRQ_HI - 1], 0xA9)  # LDA #>play
        # STA $0314 / STA $0315 sandwich the LDAs.
        self.assertEqual(
            t[_REINIT_PATCH_IRQ_LO + 1 : _REINIT_PATCH_IRQ_LO + 4], bytes([0x8D, 0x14, 0x03])
        )
        self.assertEqual(
            t[_REINIT_PATCH_IRQ_HI + 1 : _REINIT_PATCH_IRQ_HI + 4], bytes([0x8D, 0x15, 0x03])
        )
        # Tail must chain to the kernal IRQ at $EA31 — otherwise the
        # CPU would never return to the spin loop after re-INIT.
        self.assertEqual(t[-3:], bytes([0x4C, 0x31, 0xEA]), "stub must end with JMP $EA31")

    def test_reinit_stub_uploaded_restores_play_handler_vector(self):
        # The uploaded (patched) stub must re-install $0314/$0315 →
        # player_base + SID_PLAYER_IRQ_HANDLER_OFFSET so subsequent IRQ
        # ticks resume calling PLAY. If the embedded addr drifts from
        # the main player's IRQ entry, subsequent IRQs JMP into garbage.
        self.api.run_sid_player(self._make_sid(load=0x2000, init=0x2003, play=0x2006))
        _, stub = self.dma_writes[2]
        expected_irq = SID_PLAYER_MC_ADDR + SID_PLAYER_IRQ_HANDLER_OFFSET
        self.assertEqual(stub[_REINIT_PATCH_IRQ_LO], expected_irq & 0xFF)
        self.assertEqual(stub[_REINIT_PATCH_IRQ_HI], (expected_irq >> 8) & 0xFF)

    def test_reinit_stub_restores_master_volume(self):
        # The stub writes $D418=$0F after JSR init. Without this, a
        # PSID INIT that zeroes $D418 (some do) would leave the SID
        # silent until the user cycles again.
        t = REINIT_STUB_TEMPLATE
        # After JSR init the stub restores the resting bank ($37) at 13-16,
        # then the master volume. Bytes 17-21: LDA #$0F ; STA $D418.
        self.assertEqual(
            t[17:22], b"\xa9\x0f\x8d\x18\xd4", "stub must restore $D418=$0F after JSR init"
        )

    def test_reinit_stub_carries_song_and_init_at_upload(self):
        self.api.run_sid_player(
            self._make_sid(load=0x2000, init=0x2003, play=0x2006, num_songs=8, start_song=1),
            song=5,
        )
        _, stub = self.dma_writes[2]
        # Pre-seeded with the starting song so an immediate cue without
        # a song change replays the same INIT.
        self.assertEqual(stub[_REINIT_PATCH_SONG], 5 - 1)
        # init_addr matches the main player so cue_song_reinit only
        # needs to re-patch the song byte.
        self.assertEqual(stub[_REINIT_PATCH_INIT_LO], 0x03)
        self.assertEqual(stub[_REINIT_PATCH_INIT_HI], 0x20)

    def test_cue_song_reinit_patches_song_and_swaps_vector(self):
        # Bring the stub up via run_sid_player first (cue assumes it's
        # already in place at REINIT_STUB_ADDR).
        self.api.run_sid_player(
            self._make_sid(load=0x2000, init=0x2003, play=0x2006, num_songs=8, start_song=1)
        )
        # The 3 upload writes are already in self.dma_writes — index past
        # them so we only assert against the cue's writes.
        n_setup_writes = len(self.dma_writes)
        self.api.cue_song_reinit(7)

        cue_writes = self.dma_writes[n_setup_writes:]
        self.assertEqual(
            len(cue_writes),
            3,
            "cue must do 3 DMA writes: song patch + playBank restore + vector swap",
        )
        # First: 1-byte patch of REINIT_STUB_ADDR + _REINIT_PATCH_SONG.
        addr1, payload1 = cue_writes[0]
        self.assertEqual(addr1, REINIT_STUB_ADDR + _REINIT_PATCH_SONG)
        self.assertEqual(payload1, bytes([7 - 1]))
        # Second: 1-byte playBank restore to the tune's heuristic default
        # (no override passed → $37 for this $20xx-page tune).
        addr2, payload2 = cue_writes[1]
        self.assertEqual(addr2, SID_PLAYER_MC_ADDR + _SID_PATCH_PLAYBANK)
        self.assertEqual(payload2, bytes([CPU.PORT_DEFAULT]))
        # Third: 2-byte atomic vector swap to point at the stub.
        addr3, payload3 = cue_writes[2]
        self.assertEqual(addr3, VECTORS.IRQ)
        self.assertEqual(payload3, bytes([REINIT_STUB_ADDR & 0xFF, (REINIT_STUB_ADDR >> 8) & 0xFF]))

    def test_cue_song_reinit_play_bank_override_patches_player_mc(self):
        # A subtune that reads RAM under BASIC ROM needs $36; the override
        # must land on the player MC's playBank operand so PLAY of the new
        # subtune banks BASIC out (Times of Lore 2-11).
        self.api.run_sid_player(
            self._make_sid(load=0x2000, init=0x2003, play=0x2006, num_songs=8, start_song=1)
        )
        n_setup = len(self.dma_writes)
        self.api.cue_song_reinit(2, play_bank=CPU.PORT_BASIC_OUT)
        cue_writes = self.dma_writes[n_setup:]
        bank_addr, bank_payload = cue_writes[1]
        self.assertEqual(bank_addr, SID_PLAYER_MC_ADDR + _SID_PATCH_PLAYBANK)
        self.assertEqual(bank_payload, bytes([CPU.PORT_BASIC_OUT]))

    def test_cue_song_reinit_before_run_sid_player_raises(self):
        # Without a prior run_sid_player, there's no uploaded stub to
        # patch — calling cue would silently DMA into wherever a stale
        # default was, corrupting RAM. Must raise so the bug surfaces.
        with self.assertRaisesRegex(RuntimeError, "before run_sid_player"):
            self.api.cue_song_reinit(2)

    # ---- relocation -------------------------------------------------
    def test_relocates_player_when_payload_overlaps_default(self):
        # A SID that loads at $C200 and runs 0x800 bytes covers
        # $C200-$C9FF — overlapping the default player ($C300-$C322)
        # AND the default stub ($C400-$C419). The picker must relocate
        # both past the payload (page-aligned).
        sid = self._make_sid(load=0xC200, init=0xC200, play=0xC203, payload_len=0x800)
        self.api.run_sid_player(sid)

        # Default layout no longer used: the player + stub writes land at
        # non-default addresses.
        _, _ = self.dma_writes[0]  # SID payload
        player_addr, mc = self.dma_writes[1]
        stub_addr, stub = self.dma_writes[2]
        self.assertNotEqual(
            player_addr, SID_PLAYER_MC_ADDR, "player must relocate off $C300 when payload overlaps"
        )
        self.assertNotEqual(
            stub_addr, REINIT_STUB_ADDR, "stub must relocate off $C400 when payload overlaps"
        )
        # Both must land past the payload (or anywhere non-overlapping).
        payload_hi = 0xC200 + 0x800
        self.assertGreaterEqual(player_addr, payload_hi)
        self.assertGreaterEqual(stub_addr, payload_hi)
        # Both still below the I/O area at $D000.
        self.assertLess(player_addr + len(mc), 0xD000)
        self.assertLess(stub_addr + len(stub), 0xD000)
        # The MC's internal IRQ / spin patches must reflect the relocated
        # player_base, not the default $C300.
        expected_irq = player_addr + SID_PLAYER_IRQ_HANDLER_OFFSET
        self.assertEqual(mc[_SID_PATCH_IRQ_LO], expected_irq & 0xFF)
        self.assertEqual(mc[_SID_PATCH_IRQ_HI], (expected_irq >> 8) & 0xFF)
        expected_spin = player_addr + SID_PLAYER_SPIN_OFFSET
        self.assertEqual(mc[_SID_PATCH_SPIN_LO], expected_spin & 0xFF)
        self.assertEqual(mc[_SID_PATCH_SPIN_HI], (expected_spin >> 8) & 0xFF)
        # The re-INIT stub references the relocated player's IRQ handler too.
        self.assertEqual(stub[_REINIT_PATCH_IRQ_LO], expected_irq & 0xFF)
        self.assertEqual(stub[_REINIT_PATCH_IRQ_HI], (expected_irq >> 8) & 0xFF)
        # The BASIC SYS stub targets the relocated player_base.
        _, prg = self.posts[0]
        sys_idx = prg.index(b"\x9e")
        digits_end = prg.index(b"\x00", sys_idx)
        digits = prg[sys_idx + 2 : digits_end].decode("ascii")
        self.assertEqual(int(digits), player_addr)

    def test_relocated_cue_song_reinit_uses_relocated_stub_addr(self):
        # After relocation, cue_song_reinit must patch the *relocated*
        # stub address and point $0314/$0315 there — not the default
        # $C400 (which would dispatch into stale/garbage bytes).
        sid = self._make_sid(load=0xC200, init=0xC200, play=0xC203, payload_len=0x800)
        self.api.run_sid_player(sid)
        relocated_player = self.dma_writes[1][0]
        relocated_stub = self.dma_writes[2][0]
        n_setup = len(self.dma_writes)

        self.api.cue_song_reinit(3)
        cue_writes = self.dma_writes[n_setup:]
        self.assertEqual(len(cue_writes), 3)
        addr1, _ = cue_writes[0]
        addr2, _ = cue_writes[1]
        addr3, payload3 = cue_writes[2]
        self.assertEqual(addr1, relocated_stub + _REINIT_PATCH_SONG)
        # playBank restore lands on the relocated player MC, not $C300.
        self.assertEqual(addr2, relocated_player + _SID_PATCH_PLAYBANK)
        self.assertEqual(addr3, VECTORS.IRQ)
        self.assertEqual(payload3, bytes([relocated_stub & 0xFF, (relocated_stub >> 8) & 0xFF]))


class FootprintLayoutTest(unittest.TestCase):
    """Footprint-driven player relocation (the Beat_Dis fix).

    When the caller passes an `avoid` bitmap (the tune's RAM write
    footprint ∪ scene-reserved regions), the player must be placed in the
    largest hole free of avoid + payload + the $C000-$C2FF audio region —
    not crammed adjacent to the payload (where scratch-RAM tunes stomp it).
    """

    BUNDLE = _RELOCATED_STUB_OFFSET + len(REINIT_STUB_TEMPLATE)  # 95

    @staticmethod
    def _parsed(load=0x1000, size=0x100) -> ParsedPsid:
        return ParsedPsid(
            load_addr=load,
            init_addr=load,
            play_addr=load + 3,
            num_songs=1,
            start_song=1,
            song_to_play=1,
            payload=bytes(size),
        )

    @staticmethod
    def _avoid(*ranges) -> bytearray:
        a = bytearray(65536)
        for lo, hi in ranges:
            a[lo:hi] = b"\x01" * (hi - lo)
        return a

    def test_default_fast_path_when_clean(self):
        # Small tune at $1000, nothing near $C300 → keep the default layout.
        parsed = self._parsed(load=0x1000, size=0x100)
        layout = _choose_player_layout(parsed, self._avoid())
        self.assertEqual(layout.player_base, SID_PLAYER_MC_ADDR)
        self.assertEqual(layout.stub_base, REINIT_STUB_ADDR)

    def test_relocates_when_footprint_covers_default(self):
        # Tune footprint marks the default $C300 region as used → relocate.
        parsed = self._parsed(load=0x1000, size=0x100)
        avoid = self._avoid((0xC300, 0xC350))
        layout = _choose_player_layout(parsed, avoid)
        self.assertNotEqual(layout.player_base, SID_PLAYER_MC_ADDR)
        # Chosen region must be footprint-clean.
        end = layout.stub_base + len(REINIT_STUB_TEMPLATE)
        self.assertFalse(any(avoid[layout.player_base : end]))

    def test_beat_dis_shape_relocates_to_largest_hole(self):
        # Beat_Dis-like: payload $A000-$CBD4, tune writes scratch at
        # $CC00-$CC60 (right after payload). Free holes are $0820-$9FFF
        # (minus reserved) and $CC60-$CFFF. Largest-first → the low hole.
        parsed = self._parsed(load=0xA000, size=0xCBD4 - 0xA000)
        # Reserved display regions (as WaveformScene marks) + tune scratch.
        avoid = self._avoid(
            (0x0400, 0x07E8),
            (0x2000, 0x3F40),
            (0x4000, 0x6000),  # scene
            (0xCBFA, 0xCC55),
        )  # scratch
        layout = _choose_player_layout(parsed, avoid)
        # Largest free hole below the payload is $6000-$9FFF (16 KB) — bigger
        # than $0820-$1FFF (after bitmap/ring reserved). Expect $6000.
        self.assertEqual(layout.player_base, 0x6000)
        self.assertEqual(layout.stub_base, 0x6000 + _RELOCATED_STUB_OFFSET)

    def test_find_free_layout_prefers_largest_hole(self):
        # Two holes: a 200-byte one at $0900 and a 4 KB one at $5000.
        # Everything else blocked. Largest-first picks $5000.
        parsed = self._parsed(load=0x1000, size=0x10)
        avoid = bytearray(b"\x01" * 65536)
        avoid[0x0900 : 0x0900 + 200] = b"\x00" * 200
        avoid[0x5000:0x6000] = b"\x00" * 0x1000
        layout = _find_free_layout(parsed, avoid)
        self.assertEqual(layout.player_base, 0x5000)

    def test_layout_fits_rejects_avoid_overlap(self):
        parsed = self._parsed(load=0x1000, size=0x10)
        layout = _PlayerLayout(player_base=0x6000, stub_base=0x6000 + _RELOCATED_STUB_OFFSET)
        clean = self._avoid()
        self.assertTrue(_layout_fits(layout, parsed, clean))
        dirty = self._avoid((0x6010, 0x6020))  # inside the player MC
        self.assertFalse(_layout_fits(layout, parsed, dirty))

    def test_raises_when_no_hole_fits(self):
        parsed = self._parsed(load=0x1000, size=0x10)
        full = bytearray(b"\x01" * 65536)  # every byte occupied
        with self.assertRaisesRegex(ValueError, "no free slot"):
            _find_free_layout(parsed, full)

    def test_avoid_none_keeps_legacy_heuristic(self):
        # No avoid → adjacent-to-payload fallback (backward compatible).
        parsed = self._parsed(load=0xC200, size=0x800)  # overlaps default
        layout = _choose_player_layout(parsed, None)
        payload_hi = 0xC200 + 0x800
        self.assertGreaterEqual(layout.player_base, payload_hi)


class TunePlayDividerTest(unittest.TestCase):
    """`_tune_play_divider` samples CIA #1 Timer A to estimate the
    SID's reprogrammed PLAY rate, then patches the player MC's tick
    divider so kernal IRQ-tail work (SCNKEY + UDTIM + cursor blink)
    only runs every Nth tick. Without this, fast-PLAY tunes
    (Wizball-class, ~150 Hz) starve PLAY of cycles and distort.
    """

    def setUp(self):
        patcher = patch("c64cast.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")
        # Make the test fast: no settle sleep, no real CIA reads.
        patch("c64cast.api.time.sleep").start()
        patch.object(self.api, "flush").start()
        self.divider_writes: list[tuple[str, str]] = []

        def _fake_write(address, data_hex):
            self.divider_writes.append((address, data_hex))

        patch.object(self.api, "write_memory", side_effect=_fake_write).start()

    def tearDown(self):
        patch.stopall()
        with patch.object(self.api.socket_dma, "close"):
            self.api.close()

    def _set_latch(self, value: int):
        """Make read_memory(CIA1.TIMER_A_LO, 2) return `value` as 2 LE bytes."""
        buf = bytes([value & 0xFF, (value >> 8) & 0xFF])
        patch.object(self.api, "read_memory", return_value=buf).start()

    def test_no_layout_returns_1_without_writing(self):
        # _sid_player_layout is None until run_sid_player runs.
        self.assertEqual(self.api._tune_play_divider(), 1)
        self.assertEqual(self.divider_writes, [])

    def test_default_50hz_latch_divides_to_1(self):
        # Kernal-default PAL latch ~$4292 = 50 Hz PLAY → divider 1
        # (50 / 30 = 1, kernal chain every tick — no change from legacy).
        from c64cast.api import _PlayerLayout

        self.api._sid_player_layout = _PlayerLayout(
            player_base=SID_PLAYER_MC_ADDR, stub_base=REINIT_STUB_ADDR
        )
        self._set_latch(0x4292)
        n = self.api._tune_play_divider()
        self.assertEqual(n, 1)
        self.assertEqual(len(self.divider_writes), 1)
        addr, data = self.divider_writes[0]
        self.assertEqual(addr, f"{SID_PLAYER_MC_ADDR + SID_PLAYER_DIVIDER_OFFSET:04X}")
        self.assertEqual(data, "01")

    def test_fast_play_rate_divides_above_1(self):
        # Galway/Wizball-style ~151 Hz PLAY (latch ~$196E ≈ 6510 cycles).
        # rate ≈ 1e6 / 6510 ≈ 154 Hz; 154 / 30 = 5.
        from c64cast.api import _PlayerLayout

        self.api._sid_player_layout = _PlayerLayout(
            player_base=SID_PLAYER_MC_ADDR, stub_base=REINIT_STUB_ADDR
        )
        self._set_latch(0x196E)
        n = self.api._tune_play_divider()
        self.assertEqual(n, 5)
        _, data = self.divider_writes[0]
        self.assertEqual(data, "05")

    def test_divider_capped_at_max(self):
        # An absurd PLAY rate (latch=$0100, ~3900 Hz) must clamp to
        # _DIVIDER_MAX so a misread can't starve kernal services entirely.
        from c64cast.api import _PlayerLayout

        self.api._sid_player_layout = _PlayerLayout(
            player_base=SID_PLAYER_MC_ADDR, stub_base=REINIT_STUB_ADDR
        )
        self._set_latch(0x0100)
        n = self.api._tune_play_divider()
        self.assertEqual(n, self.api._DIVIDER_MAX)

    def test_read_failure_returns_1_without_patching(self):
        # A REST failure must NOT raise — the player keeps running with
        # whatever divider was already in place (template seeds 1).
        from c64cast.api import _PlayerLayout

        self.api._sid_player_layout = _PlayerLayout(
            player_base=SID_PLAYER_MC_ADDR, stub_base=REINIT_STUB_ADDR
        )
        patch.object(self.api, "read_memory", return_value=None).start()
        self.assertEqual(self.api._tune_play_divider(), 1)
        self.assertEqual(self.divider_writes, [])


class LaunchProgramTest(unittest.TestCase):
    """`launch_program` picks the firmware runner by extension and POSTs the
    file as multipart, re-raising failures so LauncherScene can advance."""

    def setUp(self):
        patcher = patch("c64cast.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")
        # flush()/invalidate_cache() touch the DMA socket; stub them.
        patch.object(self.api, "flush").start()
        patch.object(self.api, "invalidate_cache").start()
        self.post = patch.object(self.api.session, "post").start()
        self.post.return_value.raise_for_status.return_value = None

    def _write(self, tmp, name, data=b"\x01\x08"):
        import os

        p = os.path.join(tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_prg_uses_run_prg_endpoint(self):
        import tempfile

        from c64cast.c64 import U64_API

        with tempfile.TemporaryDirectory() as tmp:
            self.api.launch_program(self._write(tmp, "game.prg"))
        self.assertTrue(self.post.call_args.args[0].endswith(U64_API.RUN_PRG))

    def test_crt_uses_run_crt_endpoint_case_insensitive(self):
        import tempfile

        from c64cast.c64 import U64_API

        with tempfile.TemporaryDirectory() as tmp:
            self.api.launch_program(self._write(tmp, "cart.CRT"))
        self.assertTrue(self.post.call_args.args[0].endswith(U64_API.RUN_CRT))

    def test_unsupported_extension_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unsupported extension"):
                self.api.launch_program(self._write(tmp, "disk.d64"))
        self.post.assert_not_called()

    def test_post_failure_reraises(self):
        import tempfile

        import requests

        self.post.return_value.raise_for_status.side_effect = requests.HTTPError("boom")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(requests.HTTPError):
                self.api.launch_program(self._write(tmp, "game.prg"))


class PutConfigItemTest(unittest.TestCase):
    """put_config_item() issues the REST config-write the REU auto-provisioner
    relies on: PUT /v1/configs/<category>/<item>?value=<value>, spaces in the
    category/item path percent-encoded, value passed as a query param."""

    def setUp(self):
        patcher = patch("c64cast.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")
        self.put = patch.object(self.api.session, "put").start()
        self.addCleanup(patch.stopall)

    def test_builds_route_and_value_param(self):
        self.api.put_config_item("C64 and Cartridge Settings", "RAM Expansion Unit", "Enabled")
        self.put.assert_called_once()
        args, kwargs = self.put.call_args
        url = args[0] if args else kwargs["url"]
        # Spaces percent-encoded in BOTH path segments; value is a query param.
        self.assertEqual(
            url,
            "http://example.invalid/v1/configs/"
            "C64%20and%20Cartridge%20Settings/RAM%20Expansion%20Unit",
        )
        self.assertEqual(kwargs["params"], {"value": "Enabled"})

    def test_http_error_propagates(self):
        import requests

        self.put.return_value.raise_for_status.side_effect = requests.HTTPError("400")
        with self.assertRaises(requests.HTTPError):
            self.api.put_config_item("C64 and Cartridge Settings", "REU Size", "16 MB")


if __name__ == "__main__":
    unittest.main()
