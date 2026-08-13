"""Tests for Ultimate64API — specifically the rolling DMA latency window
and its formatters. Other parts of the API (write_region delta caching,
listener notifications) are exercised by the higher-level scene/mode
tests, not duplicated here. Wire-level protocol coverage lives in
test_socket_dma.py."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from _fakes import make_psid

from c64cast.hw.api import (
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
    CHAR_ROM_DUMP_STUB_ADDR,
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
    build_char_rom_dump_stub,
    parse_psid_for_player,
)
from c64cast.hw.backend import BackendCapabilityError
from c64cast.hw.c64 import (
    CPU,
    VECTORS,
    VIC,
    cia1_latch_for_rate,
    frame_rate,
    kernal_cia1_latch,
)
from c64cast.hw.socket_dma import SocketDMAError


class DmaLatencyTest(unittest.TestCase):
    def setUp(self):
        # Patch connect() so the constructor doesn't try to open a real
        # TCP socket. dmawrite/flush are also stubbed on the instance
        # below for tests that need to drive latency samples directly.
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
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
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
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
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
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

    # ---- header construction helper (this file's defaults over the
    # shared PSID builder) ----------------------------------------------
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
        return make_psid(
            magic=magic,
            load=load,
            init=init,
            play=play,
            num_songs=num_songs,
            start_song=start_song,
            payload=bytes(payload_len),
        )

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

    # ---- U2+ emulated-SID snoop window -------------------------------
    def _u2plus(self):
        """Re-profile the API as a U2+ (emulated stereo SIDs, no U64 multi-SID
        surface) without rebuilding the whole fixture."""
        from dataclasses import replace

        self.api.profile = replace(self.api.profile, supports_emusid_mixer=True)

    def test_payload_in_the_snoop_window_warns_but_still_plays(self):
        # The U2+ takes SID writes off the cartridge port, which can't
        # distinguish them from writes to the RAM underneath — so a tune living
        # there is heard as register writes on the Ultimate's audio output.
        # A warning, never a refusal: the tune plays correctly, and the C64's
        # own output is unaffected.
        self._u2plus()
        with self.assertLogs("c64cast.hw.api", level="WARNING") as cm:
            self.api.run_sid_player(self._make_sid(load=0xD400, init=0xD400, play=0xD403))
        self.assertIn("emulated SIDs snoop", cm.records[0].getMessage())
        self.assertTrue(self.dma_writes)  # played, not refused

    def test_payload_clear_of_the_window_is_silent(self):
        self._u2plus()
        with self.assertNoLogs("c64cast.hw.api", level="WARNING"):
            self.api.run_sid_player(self._make_sid(load=0x2000, init=0x2003, play=0x2006))

    def test_no_warning_on_a_backend_without_emulated_sids(self):
        # A U64 (or TeensyROM) has no snooping emulation, so the same tune is
        # unremarkable there.
        with self.assertNoLogs("c64cast.hw.api", level="WARNING"):
            self.api.run_sid_player(self._make_sid(load=0xD400, init=0xD400, play=0xD403))

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
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")
        # Make the test fast: no settle sleep, no real CIA reads.
        patch("c64cast.hw.api.time.sleep").start()
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
        from c64cast.hw.api import _PlayerLayout

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
        from c64cast.hw.api import _PlayerLayout

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
        from c64cast.hw.api import _PlayerLayout

        self.api._sid_player_layout = _PlayerLayout(
            player_base=SID_PLAYER_MC_ADDR, stub_base=REINIT_STUB_ADDR
        )
        self._set_latch(0x0100)
        n = self.api._tune_play_divider()
        self.assertEqual(n, self.api._DIVIDER_MAX)

    def test_read_failure_returns_1_without_patching(self):
        # A REST failure must NOT raise — the player keeps running with
        # whatever divider was already in place (template seeds 1).
        from c64cast.hw.api import _PlayerLayout

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
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
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

        from c64cast.hw.c64 import U64_API

        with tempfile.TemporaryDirectory() as tmp:
            self.api.launch_program(self._write(tmp, "game.prg"))
        self.assertTrue(self.post.call_args.args[0].endswith(U64_API.RUN_PRG))

    def test_crt_uses_run_crt_endpoint_case_insensitive(self):
        import tempfile

        from c64cast.hw.c64 import U64_API

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
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
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


class ReadSideTest(unittest.TestCase):
    """The REST read surface (read_memory / probe / get_config_category /
    get_device_info / run_basic_clear_loop / reset): URL + params shape,
    the str-coercion contract on config/info maps, and which failures are
    swallowed (best-effort polling paths return None / log) versus
    propagated (config reads the caller must know about)."""

    def setUp(self):
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")
        self.get = patch.object(self.api.session, "get").start()
        self.get.return_value.raise_for_status.return_value = None
        self.addCleanup(patch.stopall)

    def test_read_memory_requests_hex_address_and_length(self):
        self.get.return_value.content = b"\x02"
        data = self.api.read_memory(0x028D, 1)
        self.assertEqual(data, b"\x02")
        _, kwargs = self.get.call_args
        self.assertEqual(kwargs["params"], {"address": "028D", "length": "1"})

    def test_read_memory_returns_none_on_transport_failure(self):
        # The pollers (keyboard, launcher) call this at 10 Hz; a dropped
        # read must come back as "couldn't tell", never an exception.
        import requests

        self.get.side_effect = requests.ConnectionError("down")
        self.assertIsNone(self.api.read_memory(0x028D, 1))

    def test_read_memory_returns_none_on_http_error(self):
        import requests

        self.get.return_value.raise_for_status.side_effect = requests.HTTPError("500")
        self.assertIsNone(self.api.read_memory(0x0400, 8))

    def test_probe_reports_status_and_swallows_failure(self):
        self.get.return_value.status_code = 200
        self.assertEqual(self.api.probe(), "HTTP 200")
        import requests

        self.get.side_effect = requests.ConnectionError("down")
        self.assertIsNone(self.api.probe())

    def test_get_config_category_unwraps_and_coerces_to_str(self):
        # The firmware's emit_store wraps the items under the category name
        # and mixes ints (value items) with strings (enum labels).
        self.get.return_value.json.return_value = {
            "Audio Mixer": {"Volume SID Left": 0, "Pan SID Left": "Left 3"},
        }
        got = self.api.get_config_category("Audio Mixer")
        self.assertEqual(got, {"Volume SID Left": "0", "Pan SID Left": "Left 3"})

    def test_get_config_category_unexpected_shape_returns_empty(self):
        self.get.return_value.json.return_value = ["not", "a", "dict"]
        self.assertEqual(self.api.get_config_category("Audio Mixer"), {})

    def test_get_config_category_propagates_http_error(self):
        # Config reads aren't fire-and-forget: AsidScene decides its socket
        # policy on the answer, so it must SEE the failure.
        import requests

        self.get.return_value.raise_for_status.side_effect = requests.HTTPError("500")
        with self.assertRaises(requests.HTTPError):
            self.api.get_config_category("Audio Mixer")

    def test_get_device_info_coerces_to_str(self):
        self.get.return_value.json.return_value = {"unique_id": "5D327C", "core": 137}
        self.assertEqual(self.api.get_device_info(), {"unique_id": "5D327C", "core": "137"})
        args, _ = self.get.call_args
        self.assertEqual(args[0], "http://example.invalid/v1/info")

    def test_describe_device_names_the_unit_and_its_firmware(self):
        # `product` is the only thing over this API that tells a U64 from a
        # U2+, and the two expose different config categories — so the
        # connect-time line has to carry it.
        self.get.return_value.json.return_value = {
            "product": "Ultimate II+",
            "unique_id": "5D327C",
            "firmware_version": "3.14d",
            "fpga_version": "122",
        }
        self.assertEqual(
            self.api.describe_device(), "Ultimate II+ 5D327C (firmware 3.14d, FPGA 122)"
        )

    def test_describe_device_omits_fields_the_device_did_not_report(self):
        self.get.return_value.json.return_value = {"product": "C64 Ultimate"}
        self.assertEqual(self.api.describe_device(), "C64 Ultimate")

    def test_describe_device_is_empty_when_the_device_wont_answer(self):
        # Firmware without /v1/info must cost a log line, not a crashed run.
        import requests

        self.get.side_effect = requests.ConnectionError("down")
        self.assertEqual(self.api.describe_device(), "")


# What GET /v1/configs lists on each device (live dumps, abridged to what the
# capability probe reads). The U2+ has a config API but none of the three
# multi-SID categories.
_U64_CATEGORIES = [
    "Audio Mixer",
    "SID Sockets Configuration",
    "UltiSID Configuration",
    "SID Addressing",
    "C64 and Cartridge Settings",
]
_U2PLUS_CATEGORIES = [
    "Audio Output Settings",
    "C64 and Cartridge Settings",
    "Network Settings",
]


class RefineCapabilitiesTest(unittest.TestCase):
    """The connect-time capability probe: category presence decides
    `supports_sid_config`, and every failure keeps the optimistic flags —
    a transient read error must never disable SID config on a healthy U64."""

    def setUp(self):
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")
        self.get = patch.object(self.api.session, "get").start()
        self.get.return_value.raise_for_status.return_value = None
        self.addCleanup(patch.stopall)

    def _refine_with(self, categories: object) -> None:
        self.get.return_value.json.return_value = {"categories": categories, "errors": []}
        self.api.refine_capabilities()

    def test_u64_category_list_keeps_the_sid_surface(self):
        self._refine_with(_U64_CATEGORIES)
        self.assertTrue(self.api.profile.supports_sid_config)
        self.assertFalse(self.api.profile.supports_emusid_mixer)

    def test_u2plus_category_list_revokes_the_sid_surface_with_one_line(self):
        with self.assertLogs("c64cast.hw.api", level="INFO") as cm:
            self._refine_with(_U2PLUS_CATEGORIES)
        self.assertFalse(self.api.profile.supports_sid_config)
        info_lines = [r for r in cm.records if r.levelname == "INFO"]
        self.assertEqual(len(info_lines), 1)
        self.assertIn("no multi-SID config surface", info_lines[0].getMessage())

    def test_u2plus_category_list_grants_the_emusid_surface(self):
        # The one line also points at the surface that IS available, so the
        # downgrade doesn't read as "no mixer control at all".
        with self.assertLogs("c64cast.hw.api", level="INFO") as cm:
            self._refine_with(_U2PLUS_CATEGORIES)
        self.assertTrue(self.api.profile.supports_emusid_mixer)
        self.assertIn("emulated stereo-SID", cm.records[0].getMessage())

    def test_no_surface_at_all_keeps_the_old_message(self):
        # A device with neither surface (no known hardware, but the probe
        # must not imply a mixer that isn't there).
        with self.assertLogs("c64cast.hw.api", level="INFO") as cm:
            self._refine_with(["C64 and Cartridge Settings"])
        self.assertFalse(self.api.profile.supports_sid_config)
        self.assertFalse(self.api.profile.supports_emusid_mixer)
        self.assertIn("mixer control are unavailable", cm.records[0].getMessage())

    def test_partial_surface_is_revoked(self):
        # All three categories make the surface; asid_sidmap's planners
        # write to each of them, so two out of three is still unusable.
        self._refine_with(["SID Addressing", "C64 and Cartridge Settings"])
        self.assertFalse(self.api.profile.supports_sid_config)

    def test_read_failure_keeps_optimism(self):
        import requests

        self.get.side_effect = requests.ConnectionError("down")
        self.api.refine_capabilities()
        self.assertTrue(self.api.profile.supports_sid_config)

    def test_unrecognized_shape_keeps_optimism(self):
        self._refine_with("not-a-list")
        self.assertTrue(self.api.profile.supports_sid_config)

    def test_already_revoked_still_probes_for_the_emusid_surface(self):
        # The old contract skipped the REST call on an already-revoked
        # profile; the emusid grant is evidence-based, so the read always
        # happens now — and a second refine is idempotent, no re-log.
        self.api.profile = replace(self.api.profile, supports_sid_config=False)
        self._refine_with(_U2PLUS_CATEGORIES)
        self.assertFalse(self.api.profile.supports_sid_config)
        self.assertTrue(self.api.profile.supports_emusid_mixer)

    def test_read_failure_keeps_emusid_conservative_false(self):
        import requests

        self.get.side_effect = requests.ConnectionError("down")
        self.api.refine_capabilities()
        self.assertFalse(self.api.profile.supports_emusid_mixer)

    def test_run_basic_clear_loop_posts_prg_and_swallows_failure(self):
        import requests

        from c64cast.hw.c64 import U64_API

        patch.object(self.api, "flush").start()
        patch.object(self.api, "invalidate_cache").start()
        post = patch.object(self.api.session, "post").start()
        post.return_value.raise_for_status.return_value = None
        self.api.run_basic_clear_loop()
        self.assertTrue(post.call_args.args[0].endswith(U64_API.RUN_PRG))
        self.assertIn("file", post.call_args.kwargs["files"])

        post.side_effect = requests.ConnectionError("down")
        with self.assertLogs("c64cast.hw.api", level="WARNING"):
            self.api.run_basic_clear_loop()  # best-effort — must not raise

    def test_reset_puts_even_when_pre_blank_fails(self):
        # The pre-reset display blank is best-effort; a dead DMA socket on
        # shutdown must not stop the REST reset from firing.
        patch.object(self.api, "blank_display", side_effect=OSError("dead socket")).start()
        put = patch.object(self.api.session, "put").start()
        self.api.reset()
        put.assert_called_once()
        self.assertEqual(put.call_args.args[0], self.api.reset_url)

    def test_reset_swallows_rest_failure(self):
        import requests

        patch.object(self.api, "blank_display").start()
        patch.object(self.api, "flush").start()
        put = patch.object(self.api.session, "put").start()
        put.side_effect = requests.ConnectionError("down")
        with self.assertLogs("c64cast.hw.api", level="WARNING"):
            self.api.reset()  # shutdown path — must not raise


class DumpCharRomTest(unittest.TestCase):
    """The shared dump orchestration on the Ultimate: upload the stub, SYS it
    via run_prg, wait for the completion flag, read the landing zone back.

    The flag poll is the part worth pinning — without it the read races the
    ~45 ms on-C64 copy, and `run_prg` returning tells us nothing about whether
    `SYS` has finished."""

    def setUp(self):
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")
        self.writes: list[tuple[int, bytes]] = []
        self.api._emit = lambda addr, payload: self.writes.append((addr, bytes(payload)))  # type: ignore[method-assign]
        patch.object(self.api, "flush").start()
        patch.object(self.api, "run_basic_clear_loop").start()
        self.posts: list[tuple[str, bytes]] = []

        def _fake_post(url, files=None, **_):
            self.posts.append((url, bytes(files["file"][1]) if files else b""))

            class _R:
                def raise_for_status(self):
                    pass

            return _R()

        patch.object(self.api.session, "post", side_effect=_fake_post).start()
        self.rom = bytes(range(256)) * 16  # 4096 bytes; content is irrelevant here
        # Flag reads answer "not yet" twice, then done — proving the poll loop.
        self.flag_reads = 0

        def _fake_read(address, length, timeout=1.0):
            if length == 1:
                self.flag_reads += 1
                return b"\xff" if self.flag_reads > 2 else b"\x00"
            return self.rom

        patch.object(self.api, "read_memory", side_effect=_fake_read).start()
        patch("c64cast.hw.api.time.sleep").start()
        # The deadline is real wall clock, and with sleep stubbed out the
        # never-signals case would busy-spin the full production budget.
        patch("c64cast.hw.api._CHAR_ROM_FLAG_TIMEOUT_S", 0.2).start()

    def tearDown(self):
        patch.stopall()
        with patch.object(self.api.socket_dma, "close"):
            self.api.close()

    def test_uploads_the_stub_and_sys_es_it(self):
        self.api.dump_char_rom()
        stub_writes = [w for w in self.writes if w[0] == CHAR_ROM_DUMP_STUB_ADDR]
        self.assertEqual(len(stub_writes), 1)
        self.assertEqual(stub_writes[0][1], build_char_rom_dump_stub(irq_exit=False))
        # The kick is a `10 SYS <stub addr>` PRG posted to run_prg.
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][1], _build_basic_sys_stub(CHAR_ROM_DUMP_STUB_ADDR))

    def test_waits_for_the_completion_flag_before_reading(self):
        self.assertEqual(self.api.dump_char_rom(), self.rom)
        self.assertEqual(self.flag_reads, 3, "must poll until the stub signals")

    def test_restores_the_clear_loop_the_kick_reset(self):
        self.api.dump_char_rom()
        self.api.run_basic_clear_loop.assert_called_once()  # type: ignore[attr-defined]

    def test_clear_loop_is_restored_even_when_the_dump_fails(self):
        patch.object(self.api, "read_memory", return_value=None).start()
        with self.assertRaises(RuntimeError):
            self.api.dump_char_rom()
        self.api.run_basic_clear_loop.assert_called_once()  # type: ignore[attr-defined]

    def test_a_stub_that_never_signals_raises(self):
        patch.object(self.api, "read_memory", return_value=b"\x00").start()
        with self.assertRaises(RuntimeError) as ctx:
            self.api.dump_char_rom()
        self.assertIn("never signaled", str(ctx.exception))

    def test_a_short_read_back_raises(self):
        def _short(address, length, timeout=1.0):
            return b"\xff" if length == 1 else b"\x00" * 100

        patch.object(self.api, "read_memory", side_effect=_short).start()
        with self.assertRaises(RuntimeError) as ctx:
            self.api.dump_char_rom()
        self.assertIn("100", str(ctx.exception))

    def test_refused_without_read_support(self):
        self.api.profile = replace(self.api.profile, supports_read=False)
        with self.assertRaises(BackendCapabilityError):
            self.api.dump_char_rom()


if __name__ == "__main__":
    unittest.main()


class ParsedPsidTimingTest(unittest.TestCase):
    """parse_psid_for_player decodes the two header fields the PLAY-rate lock
    depends on: the clock flag and the per-subtune speed word."""

    def test_clock_flag_round_trips(self):
        from c64cast.hw.api import parse_psid_for_player

        for label in ("PAL", "NTSC", "PAL+NTSC"):
            parsed = parse_psid_for_player(make_psid(clock=label))
            self.assertEqual(parsed.clock, label)
        # No clock bits set at all is the header's "unknown", not None.
        self.assertEqual(parse_psid_for_player(make_psid()).clock, "?")

    def test_clock_table_matches_the_sid_module(self):
        # hw must not import sid, so the table is duplicated. Pin them together.
        from c64cast.hw.api import _PSID_CLOCK_TABLE
        from c64cast.sid.sid_host_emu import _CLOCK_TABLE

        self.assertEqual(_PSID_CLOCK_TABLE, _CLOCK_TABLE)

    def test_speed_word_is_per_subtune(self):
        from c64cast.hw.api import parse_psid_for_player

        # Subtunes 1 and 3 CIA-timed (bits 0 and 2), 2 and 4 vsync.
        sid = make_psid(num_songs=4, speed=0b0101)
        for song, vsync in ((1, False), (2, True), (3, False), (4, True)):
            parsed = parse_psid_for_player(sid, song=song)
            self.assertIs(parsed.song_is_vsync(), vsync, f"song {song}")

    def test_songs_past_32_reuse_the_top_bit(self):
        from c64cast.hw.api import parse_psid_for_player

        parsed = parse_psid_for_player(make_psid(num_songs=40, speed=1 << 31), song=40)
        self.assertFalse(parsed.song_is_vsync())

    def test_speed_defaults_to_all_vsync(self):
        from c64cast.hw.api import parse_psid_for_player

        self.assertTrue(parse_psid_for_player(make_psid()).song_is_vsync())


class SidPlayRateTest(unittest.TestCase):
    """The PLAY-rate lock: a vsync tune's PLAY rides the kernal jiffy IRQ,
    which is ~60 Hz on BOTH standards — so a PAL tune runs ~19.7% fast unless
    CIA #1 Timer A is reprogrammed. `_apply_play_rate` does that, after INIT
    (the Ultimate's run_prg kick soft-resets and the KERNAL reloads the latch,
    so a pre-kick write would not survive)."""

    def setUp(self):
        patcher = patch("c64cast.hw.socket_dma.SocketDMAClient.connect", autospec=True)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.api = Ultimate64API("http://example.invalid")
        patch.object(self.api, "flush").start()
        self.writes: list[tuple[str, str]] = []
        patch.object(
            self.api,
            "write_memory",
            side_effect=lambda a, d: self.writes.append((a, d)),
        ).start()

    def tearDown(self):
        patch.stopall()
        with patch.object(self.api.socket_dma, "close"):
            self.api.close()

    def _load(
        self,
        *,
        clock: str | None = None,
        speed: int = 0,
        play_rate: str | float | None = "auto",
        system: str = "NTSC",
        song: int = 1,
    ):
        from c64cast.hw.api import parse_psid_for_player

        self.api.profile = replace(self.api.profile, system=system)
        sid = make_psid(clock=clock, speed=speed, num_songs=4)
        self.api._sid_parsed = parse_psid_for_player(sid, song=song)
        self.api._sid_play_rate = play_rate

    # A sampled latch that says "the kernal default is still in place" — i.e.
    # INIT did not reprogram Timer A. The sample is the max of 8 reads of a
    # free-running down-counter, so it sits somewhat below the true latch.
    def _kernal_sample(self, system="NTSC"):
        return int(kernal_cia1_latch(system) * 0.97)

    def test_pal_vsync_tune_is_retuned_to_the_pal_frame_rate(self):
        self._load(clock="PAL")
        rate = self.api._apply_play_rate(self._kernal_sample())
        assert rate is not None
        self.assertAlmostEqual(rate, frame_rate("PAL"), places=1)
        # Latch computed against the MACHINE's clock (NTSC here), for the
        # TUNE's rate — that is what makes tempo right on either machine.
        self.assertEqual(
            self.writes, [("DC04", _latch_hex(cia1_latch_for_rate(frame_rate("PAL"), "NTSC")))]
        )

    def test_pal_tune_on_a_pal_machine_uses_the_pal_clock(self):
        self._load(clock="PAL", system="PAL")
        self.api._apply_play_rate(self._kernal_sample("PAL"))
        self.assertEqual(
            self.writes, [("DC04", _latch_hex(cia1_latch_for_rate(frame_rate("PAL"), "PAL")))]
        )

    def test_ntsc_tune_on_an_ntsc_machine_is_a_near_noop(self):
        self._load(clock="NTSC")
        rate = self.api._apply_play_rate(self._kernal_sample())
        assert rate is not None
        self.assertAlmostEqual(rate, frame_rate("NTSC"), places=1)

    def test_cia_timed_subtune_is_never_touched(self):
        self._load(clock="PAL", speed=0b0001, song=1)
        self.assertIsNone(self.api._apply_play_rate(self._kernal_sample()))
        self.assertEqual(self.writes, [])

    def test_off_leaves_the_kernal_rate_alone(self):
        self._load(clock="PAL", play_rate="off")
        self.assertIsNone(self.api._apply_play_rate(self._kernal_sample()))
        self.assertEqual(self.writes, [])

    def test_none_leaves_the_kernal_rate_alone(self):
        self._load(clock="PAL", play_rate=None)
        self.assertIsNone(self.api._apply_play_rate(self._kernal_sample()))
        self.assertEqual(self.writes, [])

    def test_an_explicit_rate_pins_every_vsync_tune(self):
        # This is how you keep hearing PAL tunes at NTSC speed on purpose.
        self._load(clock="PAL", play_rate=59.826)
        rate = self.api._apply_play_rate(self._kernal_sample())
        assert rate is not None
        self.assertAlmostEqual(rate, 59.826, places=1)

    def test_an_ambiguous_clock_flag_gets_no_opinion(self):
        for label in ("PAL+NTSC", "?", None):
            with self.subTest(clock=label):
                self._load(clock=label)
                self.assertIsNone(self.api._apply_play_rate(self._kernal_sample()))

    def test_a_multispeed_latch_overrides_a_lying_vsync_flag(self):
        # Flagged vsync, but INIT left Timer A at half the kernal latch — a 2x
        # multispeed. Trust the machine over the metadata.
        self._load(clock="PAL")
        sampled = kernal_cia1_latch("NTSC") // 2
        self.assertIsNone(self.api._apply_play_rate(sampled))
        self.assertEqual(self.writes, [])

    def test_a_failed_write_degrades_instead_of_raising(self):
        self._load(clock="PAL")
        patch.object(self.api, "write_memory", side_effect=RuntimeError("boom")).start()
        self.assertIsNone(self.api._apply_play_rate(self._kernal_sample()))

    def test_teardown_only_writes_back_over_a_latch_we_set(self):
        self._load(clock="PAL")
        self.api.restore_kernal_play_rate()
        self.assertEqual(self.writes, [])
        self.api._apply_play_rate(self._kernal_sample())
        self.writes.clear()
        self.api.restore_kernal_play_rate()
        self.assertEqual(self.writes, [("DC04", _latch_hex(kernal_cia1_latch("NTSC")))])
        # Idempotent: a second teardown writes nothing more.
        self.writes.clear()
        self.api.restore_kernal_play_rate()
        self.assertEqual(self.writes, [])

    def test_vsync_rate_defaults_to_the_kernal_jiffy_not_the_frame_rate(self):
        # The bug this whole feature exists for: PLAY is NOT once per frame.
        self.api.profile = replace(self.api.profile, system="PAL")
        self.assertAlmostEqual(self.api.sid_vsync_play_rate_hz(), 60.0, places=1)
        self.assertNotAlmostEqual(self.api.sid_vsync_play_rate_hz(), frame_rate("PAL"), places=1)


def _latch_hex(latch: int) -> str:
    """The write_memory payload for a CIA #1 Timer A latch (lo byte, hi byte)."""
    return f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"
