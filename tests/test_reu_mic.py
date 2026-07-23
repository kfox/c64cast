"""Tests for the REU-staged live-mic path (start_mic with use_reu_pump).

The host-side mechanism (REUWRITE wrap, callback encoding, host write
position tracking) is exercised directly. The C64-side IRQ handler bytes
get the same shape verification as the video REU pump handler so a
hand-assembled regression can't pass tests."""

from __future__ import annotations

import queue
import threading
import unittest
from typing import cast

import numpy as np
from _fakes import FakeAPI

from c64cast.api import Ultimate64API
from c64cast.audio import (
    NEUTRAL_SAMPLE,
    REU_MIC_BASE,
    REU_MIC_BASE_HI,
    REU_MIC_BOOTSTRAP_BYTES,
    REU_MIC_END_HI,
    REU_MIC_IRQ_HANDLER,
    REU_MIC_SIZE,
    REU_MIC_SRC_TRACKER_ADDR,
    REU_PUMP_CHUNK_SIZE,
    REU_PUMP_CIA1_LATCH,
    REU_PUMP_HANDLER_ADDR,
    REU_UPLOAD_SLICE,
    RING_BUFFER_ADDR,
    RING_BUFFER_END_HI,
    RING_BUFFER_HI,
    SAMPLE_TAP_SIZE,
    AudioStreamer,
)


def _new_streamer(use_reu_pump: bool = True) -> AudioStreamer:
    s = AudioStreamer.__new__(AudioStreamer)
    s.api = cast(Ultimate64API, FakeAPI())
    s.sample_rate = 8000
    s.system = "NTSC"
    s.q = queue.Queue(maxsize=256)
    s._queued_samples = 0
    s._max_queued_samples = 16384
    s.running = False
    s.chunk_size = 1024
    s.sensitivity = 1.0
    s.noise_gate = 0.05
    s.mic_stream = None
    s._worker_thread = None
    s._pushed_count = 0
    s._tap_buf = np.zeros(SAMPLE_TAP_SIZE, dtype=np.float32)
    s._tap_write = 0
    s._tap_lock = threading.Lock()
    s.dither_enabled = False
    s.digi_boost = False
    s.dac_curve_name = "linear"
    s._dac_curve = None
    s._neutral_byte = NEUTRAL_SAMPLE
    s.sid_filter_cutoff = 0
    s.use_reu_pump = use_reu_pump
    s.reu_pump_governor = False
    s._reu_pump_armed = False
    s._reu_pump_start_time = 0.0
    s._reu_pump_total_samples = 0
    s._mic_reu_write_pos = 0
    s._full_underruns = 0
    s._partial_underruns = 0
    s.host_dma_servo = False
    s._servo_integ = 0.0
    s._servo_gap_min = -1
    s._servo_gap_max = -1
    s._servo_gap_last = -1
    s._nmi_latch = 0
    s._pitch_multiplier = 1.0
    s._nmi_timer_started = False
    # Phase 4 transport-flush state (read by _worker's iteration top).
    s._flush_epoch = 0
    s._count_lock = threading.Lock()
    s._stomp_requested = False
    return s


class ReuMicIrqHandlerTest(unittest.TestCase):
    """The mic IRQ handler is hand-assembled with two BCC displacements
    that must land on instruction boundaries, and the src-side reload
    uses a main-RAM tracker instead of $DF06 read-back (which the U64's
    REU returns as garbage). Pin the byte shape so a regression on either
    constraint trips a test."""

    def test_handler_length_is_known(self):
        # The handler is bigger than the video pump because it
        # reloads src from a 3-byte tracker each trigger and increments
        # the tracker (instead of trusting $DF06 auto-increment + read-back).
        self.assertEqual(len(REU_MIC_IRQ_HANDLER), 102)

    def test_src_wrap_bcc_lands_on_dst_wrap_block(self):
        # BCC +15 at offset 64 → offset 81 (start of dst wrap block,
        # LDA $DF03 absolute = 0xAD opcode). Wrong offset here lands
        # mid-instruction and stomps either REU regs or the tracker.
        self.assertEqual(REU_MIC_IRQ_HANDLER[64], 0x90)  # BCC
        self.assertEqual(REU_MIC_IRQ_HANDLER[65], 0x0F)  # +15
        self.assertEqual(REU_MIC_IRQ_HANDLER[81], 0xAD)  # LDA absolute opcode

    def test_dst_wrap_bcc_lands_on_trailing_pla(self):
        # BCC +10 at offset 86 → PLA at offset 98 (opcode 0x68). Same
        # constraint as the video pump's BCC; pinned to catch any
        # future edit to the dst-wrap block that doesn't recompute the
        # displacement.
        self.assertEqual(REU_MIC_IRQ_HANDLER[86], 0x90)  # BCC
        self.assertEqual(REU_MIC_IRQ_HANDLER[87], 0x0A)  # +10
        self.assertEqual(REU_MIC_IRQ_HANDLER[98], 0x68)  # PLA

    def test_handler_ends_in_jmp_kernal_irq(self):
        # JMP $EA31 — chains keyboard scan + jiffy clock so the C= /
        # SHIFT / CTRL poller keeps working.
        self.assertEqual(REU_MIC_IRQ_HANDLER[-3:], bytes([0x4C, 0x31, 0xEA]))

    def test_src_reload_reads_main_ram_tracker_not_DF06(self):
        # The src reload sequence (offsets 11-28) loads from the main-RAM
        # tracker into $DF04/$DF05/$DF06. Verify the LDA absolute reads
        # are aimed at the tracker, NOT at $DF06 (which would re-introduce
        # the read-back garbage bug). All three LDA opcodes = 0xAD;
        # operand low byte = tracker offset; operand high byte = tracker page.
        # LDA tracker_lo at offset 11:
        self.assertEqual(REU_MIC_IRQ_HANDLER[11], 0xAD)
        self.assertEqual(REU_MIC_IRQ_HANDLER[12], REU_MIC_SRC_TRACKER_ADDR & 0xFF)
        self.assertEqual(REU_MIC_IRQ_HANDLER[13], REU_MIC_SRC_TRACKER_ADDR >> 8)
        # LDA tracker_hi at offset 23:
        self.assertEqual(REU_MIC_IRQ_HANDLER[23], 0xAD)
        self.assertEqual(REU_MIC_IRQ_HANDLER[24], (REU_MIC_SRC_TRACKER_ADDR + 2) & 0xFF)

    def test_src_wrap_check_uses_mic_ring_end(self):
        # The wrap check at offset 59-63 reads tracker_hi and compares
        # against REU_MIC_END_HI. Wrong value either never wraps (silent
        # runaway past the ring) or wraps too early (truncates the ring).
        self.assertEqual(REU_MIC_IRQ_HANDLER[62], 0xC9, "CMP immediate opcode")
        self.assertEqual(REU_MIC_IRQ_HANDLER[63], REU_MIC_END_HI)

    def test_src_wrap_resets_tracker_to_mic_ring_base(self):
        # On wrap, the handler writes REU_MIC_BASE_HI to tracker_hi (not
        # to $DF06 directly). The host's _push_mic_to_reu wraps its own
        # write-position by the same modulus, so the two stay aligned.
        self.assertEqual(REU_MIC_IRQ_HANDLER[66], 0xA9, "LDA immediate opcode")
        self.assertEqual(REU_MIC_IRQ_HANDLER[67], REU_MIC_BASE_HI)
        # STA tracker_hi at offset 68:
        self.assertEqual(REU_MIC_IRQ_HANDLER[68], 0x8D)
        self.assertEqual(REU_MIC_IRQ_HANDLER[69], (REU_MIC_SRC_TRACKER_ADDR + 2) & 0xFF)

    def test_dst_wrap_uses_audio_ring(self):
        # The dst wrap reads $DF03 (this side IS reliable on the U64's REU)
        # and compares against RING_BUFFER_END_HI. On wrap, resets dst to
        # RING_BUFFER_ADDR. Catches a future audio-ring relocation that
        # doesn't propagate into this handler.
        self.assertEqual(REU_MIC_IRQ_HANDLER[85], RING_BUFFER_END_HI)
        self.assertEqual(REU_MIC_IRQ_HANDLER[89], RING_BUFFER_HI)

    def test_handler_does_not_read_DF06(self):
        # Whole-handler invariant: $DF06 is WRITTEN (during src reload)
        # but never READ. Reading $DF06 was the original bug that caused
        # silent audio — the U64's REU returns garbage in the upper bits
        # of the src_hi register read-back.
        for i in range(len(REU_MIC_IRQ_HANDLER) - 2):
            # 6502 absolute LDA = 0xAD; check no LDA absolute reads $DF06.
            if REU_MIC_IRQ_HANDLER[i] == 0xAD:
                addr_lo = REU_MIC_IRQ_HANDLER[i + 1]
                addr_hi = REU_MIC_IRQ_HANDLER[i + 2]
                self.assertNotEqual(
                    (addr_lo, addr_hi),
                    (0x06, 0xDF),
                    f"handler reads $DF06 at offset {i} — "
                    "the read-back is garbage; use the "
                    "main-RAM tracker instead",
                )


class PushMicToReuTest(unittest.TestCase):
    """Verify _push_mic_to_reu wraps correctly at REU_MIC_SIZE so the
    C64-side pump always reads a contiguous stream."""

    def test_simple_write_advances_position(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s._mic_reu_write_pos = 0
        s._push_mic_to_reu(b"\x00" * 128)
        self.assertEqual(len(fake.socket_dma.reuwrites), 1)
        off, data = fake.socket_dma.reuwrites[0]
        self.assertEqual(off, REU_MIC_BASE)
        self.assertEqual(len(data), 128)
        self.assertEqual(s._mic_reu_write_pos, 128)

    def test_write_at_ring_end_does_not_wrap(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s._mic_reu_write_pos = REU_MIC_SIZE - 128
        s._push_mic_to_reu(b"\xaa" * 128)
        # Single write, position wraps back to 0.
        self.assertEqual(len(fake.socket_dma.reuwrites), 1)
        off, data = fake.socket_dma.reuwrites[0]
        self.assertEqual(off, REU_MIC_BASE + REU_MIC_SIZE - 128)
        self.assertEqual(s._mic_reu_write_pos, 0)

    def test_write_straddling_end_splits(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s._mic_reu_write_pos = REU_MIC_SIZE - 64
        s._push_mic_to_reu(bytes(range(128)) + bytes(range(128)))
        # Two writes: tail piece at end of ring, head piece at start.
        self.assertEqual(len(fake.socket_dma.reuwrites), 2)
        off1, data1 = fake.socket_dma.reuwrites[0]
        off2, data2 = fake.socket_dma.reuwrites[1]
        self.assertEqual(off1, REU_MIC_BASE + REU_MIC_SIZE - 64)
        self.assertEqual(len(data1), 64)
        self.assertEqual(off2, REU_MIC_BASE)
        self.assertEqual(len(data2), 256 - 64)
        # Wrapped position lands at (256 - 64) past the ring start.
        self.assertEqual(s._mic_reu_write_pos, 256 - 64)

    def test_empty_write_is_noop(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s._push_mic_to_reu(b"")
        self.assertEqual(fake.socket_dma.reuwrites, [])
        self.assertEqual(s._mic_reu_write_pos, 0)

    def test_pushed_count_advances(self):
        # position_seconds() in REU mic mode reads _pushed_count to track
        # how much real audio has been captured.
        s = _new_streamer()
        s._push_mic_to_reu(b"\x07" * 256)
        self.assertEqual(s._pushed_count, 256)


class StartMicForReuPumpTest(unittest.TestCase):
    """Verify the bring-up sequence for the REU mic pump. The actual
    sounddevice InputStream open is unreachable without real audio
    hardware, so we monkey-patch _open_input_stream to a no-op."""

    def _start(self):
        s = _new_streamer()
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        s._start_mic_for_reu_pump(device=-1)
        return s

    def test_reu_ring_is_prefilled_with_neutral(self):
        s = self._start()
        fake = cast(FakeAPI, s.api)
        # First N REUWRITEs are the NEUTRAL prefill — one per 32 KB slice.
        prefill_writes = fake.socket_dma.reuwrites[: REU_MIC_SIZE // REU_UPLOAD_SLICE]
        self.assertEqual(len(prefill_writes), REU_MIC_SIZE // REU_UPLOAD_SLICE)
        for off, data in prefill_writes:
            self.assertGreaterEqual(off, REU_MIC_BASE)
            self.assertLess(off, REU_MIC_BASE + REU_MIC_SIZE)
            self.assertTrue(
                all(b == NEUTRAL_SAMPLE for b in data), "REU mic prefill must be NEUTRAL_SAMPLE"
            )

    def test_handler_lands_at_c100(self):
        s = self._start()
        fake = cast(FakeAPI, s.api)
        key = f"{REU_PUMP_HANDLER_ADDR:04X}"
        self.assertIn(key, fake.mem_files)
        self.assertEqual(fake.mem_files[key], REU_MIC_IRQ_HANDLER)

    def test_reu_dest_starts_at_audio_ring(self):
        # DF02 (C64 dst LO) + DF03 (HI) → packed two-byte payload pointing
        # at RING_BUFFER_ADDR. write_memory stores the hex string under the
        # uppercase address key.
        s = self._start()
        fake = cast(FakeAPI, s.api)
        expected = f"{RING_BUFFER_ADDR & 0xFF:02X}{(RING_BUFFER_ADDR >> 8) & 0xFF:02X}"
        self.assertEqual(fake.memories["DF02"], expected)

    def test_main_ram_tracker_seeded_to_mic_base(self):
        # The 3-byte src tracker at REU_MIC_SRC_TRACKER_ADDR ($C200) gets
        # seeded with REU_MIC_BASE at bring-up; the handler reads it
        # (not $DF06) on every IRQ. If this seed is wrong, the first
        # transfer reads from a bogus REU offset.
        s = self._start()
        fake = cast(FakeAPI, s.api)
        expected = (
            f"{REU_MIC_BASE & 0xFF:02X}"
            f"{(REU_MIC_BASE >> 8) & 0xFF:02X}"
            f"{(REU_MIC_BASE >> 16) & 0xFF:02X}"
        )
        key = f"{REU_MIC_SRC_TRACKER_ADDR:04X}"
        self.assertEqual(fake.memories[key], expected)

    def test_reu_length_matches_chunk_size(self):
        s = self._start()
        fake = cast(FakeAPI, s.api)
        expected = f"{REU_PUMP_CHUNK_SIZE & 0xFF:02X}{(REU_PUMP_CHUNK_SIZE >> 8) & 0xFF:02X}"
        self.assertEqual(fake.memories["DF07"], expected)

    def test_cia1_latch_is_reprogrammed(self):
        # Same matched-rate latch as the video REU path.
        s = self._start()
        fake = cast(FakeAPI, s.api)
        expected = f"{REU_PUMP_CIA1_LATCH & 0xFF:02X}{(REU_PUMP_CIA1_LATCH >> 8) & 0xFF:02X}"
        self.assertEqual(fake.memories["DC04"], expected)

    def test_irq_vector_patched_to_handler(self):
        s = self._start()
        fake = cast(FakeAPI, s.api)
        self.assertEqual(
            fake.regs["0314"], (REU_PUMP_HANDLER_ADDR & 0xFF, (REU_PUMP_HANDLER_ADDR >> 8) & 0xFF)
        )

    def test_pump_armed_state_set(self):
        s = self._start()
        self.assertTrue(s.running)
        self.assertTrue(s._reu_pump_armed)

    def test_mic_write_pos_starts_at_bootstrap_offset(self):
        # Bootstrap latency: host writes start REU_MIC_BOOTSTRAP_BYTES
        # ahead of the pump's initial read position (0), giving the mic
        # ~200 ms of slack before underrun. Smoke-test the constant +
        # the initial assignment.
        s = self._start()
        self.assertEqual(s._mic_reu_write_pos, REU_MIC_BOOTSTRAP_BYTES)
        self.assertGreater(REU_MIC_BOOTSTRAP_BYTES, 0)


class _FakeStream:
    """Stand-in for sounddevice.InputStream so _start_mic_for_reu_pump
    can run without real audio hardware."""

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


class StartMicBranchesOnReuFlagTest(unittest.TestCase):
    """start_mic must dispatch to the REU path when use_reu_pump=True."""

    def test_reu_path_is_taken_when_flag_set(self):
        s = _new_streamer(use_reu_pump=True)
        called: list[int] = []
        s._start_mic_for_reu_pump = lambda device, **_kwargs: called.append(device)  # type: ignore[method-assign]
        # Patch sd availability so we get past the import guard. AUDIO_AVAILABLE
        # is a module global; if sounddevice isn't installed in the test env
        # the function early-returns and we can't observe the branch — skip.
        from c64cast import audio as audio_mod

        if not audio_mod.AUDIO_AVAILABLE:
            self.skipTest("sounddevice not installed in this environment")
        s.start_mic(device=5, sensitivity=1.0, noise_gate=0.0)
        self.assertEqual(called, [5], "REU path not taken when use_reu_pump=True")

    def test_host_path_is_taken_when_flag_unset(self):
        s = _new_streamer(use_reu_pump=False)
        from c64cast import audio as audio_mod

        if not audio_mod.AUDIO_AVAILABLE:
            self.skipTest("sounddevice not installed in this environment")
        # Re-route the heavy bits: pretend mic open succeeded.
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()  # type: ignore[method-assign]
        called_reu: list[int] = []
        s._start_mic_for_reu_pump = lambda device: called_reu.append(device)  # type: ignore[method-assign]
        s.start_mic(device=5, sensitivity=1.0, noise_gate=0.0)
        self.assertEqual(called_reu, [], "REU path taken when use_reu_pump=False")
        # Worker thread started — the existing host-DMA path's tell.
        self.assertIsNotNone(s._worker_thread)
        # Stop it cleanly so the test doesn't leak a thread.
        s.stop()


class MicCallbackReuTest(unittest.TestCase):
    """The REU mic callback must encode + REUWRITE without queueing."""

    def test_callback_writes_to_reu(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.running = True
        s._mic_reu_write_pos = 0
        # Build a fake 256-sample mono float buffer.
        samples = np.full((256, 1), 0.5, dtype=np.float32)
        s._mic_callback_reu(samples, 256, None, None)
        self.assertEqual(len(fake.socket_dma.reuwrites), 1)
        off, data = fake.socket_dma.reuwrites[0]
        self.assertEqual(off, REU_MIC_BASE)
        self.assertEqual(len(data), 256)
        # Each byte in [0, 15] (4-bit DAC clamp).
        self.assertTrue(all(0 <= b <= 15 for b in data))

    def test_callback_drops_when_not_running(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.running = False
        samples = np.zeros((256, 1), dtype=np.float32)
        s._mic_callback_reu(samples, 256, None, None)
        self.assertEqual(fake.socket_dma.reuwrites, [])

    def test_callback_drops_on_xrun_status(self):
        # sounddevice signals input over/underflow via `status`. Mirrors
        # the host-DMA _mic_callback: drop the buffer rather than feed
        # potentially-stale samples.
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.running = True
        samples = np.zeros((256, 1), dtype=np.float32)
        s._mic_callback_reu(samples, 256, None, "input overflow")
        self.assertEqual(fake.socket_dma.reuwrites, [])


if __name__ == "__main__":
    unittest.main()
