"""Tests for the REU-staged live-mic path (start_mic with use_reu_pump).

The host-side mechanism (REUWRITE wrap, callback encoding, host write
position tracking) is exercised directly. The C64-side IRQ handler is
EXECUTED on the repo's own 6502 (_fakes.run_irq_handler) so a
hand-assembled regression can't pass tests."""

from __future__ import annotations

import unittest
from typing import Any, cast
from unittest import mock

import numpy as np
from _fakes import FakeAPI, new_streamer, run_irq_handler

from c64cast.audio import audio as audio_mod
from c64cast.audio.audio import AudioStreamer
from c64cast.audio.audio_handlers import (
    NEUTRAL_SAMPLE,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_CMD_FETCH_EXEC,
    REU_IRQ_HANDLER_TRACKED,
    REU_MIC_BASE,
    REU_MIC_BASE_HI,
    REU_MIC_BOOTSTRAP_BYTES,
    REU_MIC_PUMP_BODY_SUBROUTINE,
    REU_MIC_SIZE,
    REU_PUMP_BODY_SUBROUTINE_ADDR,
    REU_PUMP_CHUNK_SIZE,
    REU_PUMP_CIA1_LATCH_8KHZ,
    REU_PUMP_HANDLER_ADDR,
    REU_PUMP_TICK_COUNTER_ADDR,
    REU_UPLOAD_SLICE,
    RING_BUFFER_ADDR,
    RING_BUFFER_END_HI,
    RING_BUFFER_HI,
)


def _new_streamer(use_reu_pump: bool = True, **overrides) -> AudioStreamer:
    """This file's defaults over the shared builder (dither + governor OFF,
    same rationale as test_reu_audio.py)."""
    return new_streamer(
        dither=False, use_reu_pump=use_reu_pump, reu_pump_governor=False, **overrides
    )


def _packed_latch(latch: int) -> str:
    """The CIA #1 Timer A latch as write_memory records it (LO then HI)."""
    return f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"


class ReuMicPumpTest(unittest.TestCase):
    """The mic pump — REU_IRQ_HANDLER_TRACKED at $C100 calling
    REU_MIC_PUMP_BODY_SUBROUTINE at $C180 — EXECUTED on the repo's own 6502
    (see _fakes.run_irq_handler) instead of pinning instruction offsets: a
    wrong branch displacement JAMs a real C64, and the constraints these
    guard (the REC addresses come from the main-RAM trackers, never from
    $DF02-$DF06, which the bank-swap and screen-push DMAs rewrite and whose
    $DF06 read-back is garbage on the U64) are exactly what running it proves."""

    def _run(self, *, src: int, dst: int = RING_BUFFER_ADDR, extra_seed=None):
        t = REU_AUDIO_SRC_TRACKER_ADDR
        seed = {
            t + 0: src & 0xFF,
            t + 1: (src >> 8) & 0xFF,
            t + 2: (src >> 16) & 0xFF,
            t + 3: dst & 0xFF,
            t + 4: (dst >> 8) & 0xFF,
            REU_PUMP_TICK_COUNTER_ADDR: 1,
        }
        seed.update(extra_seed or {})
        return run_irq_handler(
            REU_IRQ_HANDLER_TRACKED,
            addr=REU_PUMP_HANDLER_ADDR,
            seed=seed,
            images={REU_PUMP_BODY_SUBROUTINE_ADDR: REU_MIC_PUMP_BODY_SUBROUTINE},
        )

    def _src(self, run) -> int:
        t = REU_AUDIO_SRC_TRACKER_ADDR
        ram = run.memory.ram
        return ram[t] | (ram[t + 1] << 8) | (ram[t + 2] << 16)

    def _dst(self, run) -> int:
        t = REU_AUDIO_SRC_TRACKER_ADDR
        ram = run.memory.ram
        return ram[t + 3] | (ram[t + 4] << 8)

    def test_pumps_one_chunk_from_the_main_ram_trackers(self):
        src = REU_MIC_BASE + 0x1234
        dst = RING_BUFFER_ADDR + 0x0400
        run = self._run(src=src, dst=dst)
        ram = run.memory.ram
        self.assertEqual(ram[0xDF07], REU_PUMP_CHUNK_SIZE & 0xFF)
        self.assertEqual(ram[0xDF08], (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF)
        self.assertEqual(
            [ram[0xDF04], ram[0xDF05], ram[0xDF06]],
            [src & 0xFF, (src >> 8) & 0xFF, (src >> 16) & 0xFF],
        )
        self.assertEqual([ram[0xDF02], ram[0xDF03]], [dst & 0xFF, dst >> 8])
        self.assertEqual(ram[0xDF01], REU_CMD_FETCH_EXEC)
        # Both trackers advanced one chunk; the entry chained the kernal IRQ
        # tail (keyboard scan + jiffy clock keep working) with a balanced stack.
        self.assertEqual(self._src(run), src + REU_PUMP_CHUNK_SIZE)
        self.assertEqual(self._dst(run), dst + REU_PUMP_CHUNK_SIZE)
        self.assertEqual(run.exit_pc, 0xEA31)
        self.assertEqual(run.mpu.sp, 0xFF, "entry and body must balance the stack")

    def test_rec_registers_left_by_a_video_dma_are_ignored(self):
        # #551: a bank-swap REC DMA (or a REU screen push) between ticks leaves
        # $DF02-$DF06 pointing at video staging and screen RAM. The mic pump
        # must DMA from the trackers regardless.
        stale = {0xDF02: 0x00, 0xDF03: 0xD8, 0xDF04: 0x40, 0xDF05: 0x1F, 0xDF06: 0xE0}
        src = REU_MIC_BASE + 0x0800
        run = self._run(src=src, dst=RING_BUFFER_ADDR, extra_seed=stale)
        ram = run.memory.ram
        self.assertEqual([ram[0xDF02], ram[0xDF03]], [RING_BUFFER_ADDR & 0xFF, RING_BUFFER_HI])
        self.assertEqual(
            [ram[0xDF04], ram[0xDF05], ram[0xDF06]],
            [src & 0xFF, (src >> 8) & 0xFF, (src >> 16) & 0xFF],
        )

    def test_src_wraps_to_mic_ring_base_at_ring_end(self):
        # The last chunk of the mic ring must reset the tracker to the ring
        # base — the host's _push_mic_to_reu wraps its write position by the
        # same modulus, so the two stay aligned.
        run = self._run(src=REU_MIC_BASE + REU_MIC_SIZE - REU_PUMP_CHUNK_SIZE)
        self.assertEqual(self._src(run), REU_MIC_BASE)

    def test_src_below_ring_end_is_not_wrapped(self):
        src = REU_MIC_BASE + REU_MIC_SIZE - 2 * REU_PUMP_CHUNK_SIZE
        run = self._run(src=src)
        self.assertEqual(self._src(run), src + REU_PUMP_CHUNK_SIZE)

    def test_dst_tracker_wraps_to_the_audio_ring(self):
        last_chunk_dst = (RING_BUFFER_END_HI << 8) - REU_PUMP_CHUNK_SIZE
        run = self._run(src=REU_MIC_BASE, dst=last_chunk_dst)
        self.assertEqual(self._dst(run), RING_BUFFER_ADDR)

    def test_both_trackers_wrap_on_the_same_tick(self):
        # The src wrap follows the body's dst wrap; a displacement that skipped
        # one when the other fired would show up only here.
        last_chunk_dst = (RING_BUFFER_END_HI << 8) - REU_PUMP_CHUNK_SIZE
        run = self._run(src=REU_MIC_BASE + REU_MIC_SIZE - REU_PUMP_CHUNK_SIZE, dst=last_chunk_dst)
        self.assertEqual(self._src(run), REU_MIC_BASE)
        self.assertEqual(self._dst(run), RING_BUFFER_ADDR)

    def test_chunked_dispatcher_call_returns_to_its_caller(self):
        # The chunked mhires dispatcher JSRs $C180 directly between REC
        # families, so the body must RTS rather than chain to the kernal.
        caller = bytes(
            [
                0x20,
                REU_PUMP_BODY_SUBROUTINE_ADDR & 0xFF,
                REU_PUMP_BODY_SUBROUTINE_ADDR >> 8,  # JSR $C180
                0x4C,
                0x31,
                0xEA,  # JMP $EA31
            ]
        )
        t = REU_AUDIO_SRC_TRACKER_ADDR
        seed = {t + 2: REU_MIC_BASE_HI, t + 4: RING_BUFFER_HI}
        run = run_irq_handler(
            caller,
            addr=0xC000,
            seed=seed,
            images={REU_PUMP_BODY_SUBROUTINE_ADDR: REU_MIC_PUMP_BODY_SUBROUTINE},
        )
        self.assertEqual(run.exit_pc, 0xEA31)
        self.assertEqual(run.mpu.sp, 0xFF)
        self.assertEqual(run.memory.ram[0xDF01], REU_CMD_FETCH_EXEC)

    def test_pump_never_reads_the_rec_address_registers(self):
        # Reading $DF02-$DF06 back is what #551 and the $DF06-garbage silence
        # both came from; the trackers are the only source of truth. Scans for
        # every absolute-mode read opcode with a REC address operand.
        absolute_reads = {0xAD, 0xAE, 0xAC, 0xCD, 0x6D, 0xED, 0x2D, 0x0D, 0x4D, 0x2C}
        for code, name in (
            (REU_IRQ_HANDLER_TRACKED, "REU_IRQ_HANDLER_TRACKED"),
            (REU_MIC_PUMP_BODY_SUBROUTINE, "REU_MIC_PUMP_BODY_SUBROUTINE"),
        ):
            for i in range(len(code) - 2):
                if code[i] in absolute_reads and code[i + 2] == 0xDF:
                    self.assertNotIn(
                        code[i + 1], range(0x02, 0x07), f"{name} reads $DF{code[i + 1]:02X} at {i}"
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
        self.assertEqual(s._mic_reu_write_pos, 256 - 64)

    def test_empty_write_is_noop(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s._push_mic_to_reu(b"")
        self.assertEqual(fake.socket_dma.reuwrites, [])
        self.assertEqual(s._mic_reu_write_pos, 0)

    def test_pushed_count_advances(self):
        # In REU mic mode position_seconds() tracks _pushed_count.
        s = _new_streamer()
        s._push_mic_to_reu(b"\x07" * 256)
        self.assertEqual(s._pushed_count, 256)


class StartMicForReuPumpTest(unittest.TestCase):
    """Verify the bring-up sequence for the REU mic pump. The actual
    sounddevice InputStream open is unreachable without real audio
    hardware, so we monkey-patch _open_input_stream to a no-op."""

    def _start(self, *, skip_irq_vector_hook: bool = False, **overrides):
        s = _new_streamer(**overrides)
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        s._start_mic_for_reu_pump(device=-1, skip_irq_vector_hook=skip_irq_vector_hook)
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

    def test_tracked_entry_lands_at_c100_and_mic_body_at_c180(self):
        s = self._start()
        fake = cast(FakeAPI, s.api)
        self.assertEqual(fake.mem_files[f"{REU_PUMP_HANDLER_ADDR:04X}"], REU_IRQ_HANDLER_TRACKED)
        self.assertEqual(
            fake.mem_files[f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}"], REU_MIC_PUMP_BODY_SUBROUTINE
        )

    def test_install_order_is_trackers_then_body_then_entry(self):
        # Under a bank-swap dispatcher that owns $0314, a CIA #1 tick can reach
        # $C180 (chunked mhires JSR) or $C100 (fall-through) mid-install. The
        # body must never run on unseeded trackers, and the entry must never
        # JSR a body that is not there yet.
        s = self._start(skip_irq_vector_hook=True)
        fake = cast(FakeAPI, s.api)

        def first(kind: str, addr: int) -> int:
            key = f"{addr:04X}"
            return next(
                i for i, op in enumerate(fake.ops) if op[0] == kind and op[1].upper() == key
            )

        tracker = first("write_memory", REU_AUDIO_SRC_TRACKER_ADDR)
        body = first("write_memory_file", REU_PUMP_BODY_SUBROUTINE_ADDR)
        entry = first("write_memory_file", REU_PUMP_HANDLER_ADDR)
        self.assertLess(tracker, body)
        self.assertLess(body, entry)

    def test_trackers_seeded_to_mic_base_and_ring_start(self):
        # src LO/MI/HI = REU_MIC_BASE, dst LO/HI = RING_BUFFER_ADDR. The pump
        # reads only these, so a wrong seed makes the first transfer read a
        # bogus REU offset or land outside the ring.
        s = self._start()
        fake = cast(FakeAPI, s.api)
        expected = (
            f"{REU_MIC_BASE & 0xFF:02X}"
            f"{(REU_MIC_BASE >> 8) & 0xFF:02X}"
            f"{(REU_MIC_BASE >> 16) & 0xFF:02X}"
            f"{RING_BUFFER_ADDR & 0xFF:02X}"
            f"{(RING_BUFFER_ADDR >> 8) & 0xFF:02X}"
        )
        self.assertEqual(fake.memories[f"{REU_AUDIO_SRC_TRACKER_ADDR:04X}"], expected)

    def test_tick_counter_seeded_to_one(self):
        s = self._start()
        fake = cast(FakeAPI, s.api)
        self.assertEqual(fake.memories[f"{REU_PUMP_TICK_COUNTER_ADDR:04X}"], "01")

    def test_cia1_latch_is_derived_from_the_live_nmi_rate(self):
        """The mic pump's latch must be the matched pump period for the
        configured sample_rate, exactly as the video path derives it — writing
        the historical 8 kHz constant here asked the pump for 85/128 of the
        bytes NMI eats at the 12 kHz default, so the ring under-filled and NMI
        re-read a lap-old span. The fixture used to pin sample_rate=8000, the
        one rate at which the constant and the derivation agree."""
        s = self._start()
        fake = cast(FakeAPI, s.api)
        # 12 kHz NTSC: NMI latch 84 → period 85 → 128 x 85 - 1 = 10879.
        self.assertEqual(fake.memories["DC04"], _packed_latch(10879))
        self.assertEqual(s._reu_cia1_latch_nominal, 10879)
        self.assertNotEqual(fake.memories["DC04"], _packed_latch(REU_PUMP_CIA1_LATCH_8KHZ))

    def test_cia1_latch_at_8khz_is_the_historical_value(self):
        """The other end of the same derivation: at 8 kHz it still produces
        chunk x 128 - 1, which is what REU_PUMP_CIA1_LATCH_8KHZ records."""
        s = self._start(sample_rate=8000)
        fake = cast(FakeAPI, s.api)
        self.assertEqual(fake.memories["DC04"], _packed_latch(REU_PUMP_CIA1_LATCH_8KHZ))

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
        # Host writes start REU_MIC_BOOTSTRAP_BYTES ahead of the pump's
        # initial read position, giving the mic ~200 ms of slack before
        # underrun.
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
        # AUDIO_AVAILABLE is a module global; without sounddevice installed
        # the function early-returns and the branch cannot be observed.
        from c64cast.audio import audio as audio_mod

        if not audio_mod.AUDIO_AVAILABLE:
            self.skipTest("sounddevice not installed in this environment")
        s.start_mic(device=5, sensitivity=1.0, noise_gate=0.0)
        self.assertEqual(called, [5], "REU path not taken when use_reu_pump=True")

    def test_host_path_is_taken_when_flag_unset(self):
        s = _new_streamer(use_reu_pump=False)
        from c64cast.audio import audio as audio_mod

        if not audio_mod.AUDIO_AVAILABLE:
            self.skipTest("sounddevice not installed in this environment")
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
        # sounddevice signals input over/underflow via `status`; like the
        # host-DMA _mic_callback, drop the buffer rather than feed stale samples.
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s.running = True
        samples = np.zeros((256, 1), dtype=np.float32)
        s._mic_callback_reu(samples, 256, None, "input overflow")
        self.assertEqual(fake.socket_dma.reuwrites, [])


if __name__ == "__main__":
    unittest.main()


class PushMicToReuFailureTest(unittest.TestCase):
    """The mic pump REUWRITEs straight from the PortAudio callback, which has
    no worker and so none of the worker's telemetry. An exception leaving a
    sounddevice callback kills mic audio for the rest of the scene and its
    traceback goes to stderr via PortAudio rather than to the logger, so the
    link failure is caught, counted, and logged once here."""

    def _failing(self) -> AudioStreamer:
        s = _new_streamer()

        def boom(off: int, data: bytes) -> None:
            raise RuntimeError("link down")

        cast(Any, s).api.reu_write = boom
        return s

    def test_first_failure_is_logged_and_counted(self):
        s = self._failing()
        with self.assertLogs("c64cast.audio.audio", level="ERROR") as cm:
            s._push_mic_to_reu(b"\x07" * 128)
        self.assertEqual(s._mic_reu_write_errors, 1)
        self.assertTrue(any("REU write failed" in m for m in cm.output), cm.output)
        self.assertEqual(s._mic_reu_write_pos, 0)
        self.assertEqual(s._pushed_count, 0)

    def test_later_failures_are_counted_without_re_logging(self):
        s = self._failing()
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s._push_mic_to_reu(b"\x07" * 128)
        with mock.patch.object(audio_mod.log, "exception") as exc:
            for _ in range(5):
                s._push_mic_to_reu(b"\x07" * 128)
        exc.assert_not_called()
        self.assertEqual(s._mic_reu_write_errors, 6)

    def test_stop_reports_the_run_total(self):
        s = self._failing()
        s._mic_reu_write_errors = 3
        s.running = True
        with self.assertLogs("c64cast.audio.audio", level="WARNING") as cm:
            s.stop()
        self.assertTrue(any("REU write failures this run" in m for m in cm.output), cm.output)
        self.assertEqual(s._mic_reu_write_errors, 0)


class PushMicToReuNormalizationTest(unittest.TestCase):
    """Both branches store (pos + n) mod ring. The wrapping branch used to
    store a bare `n - split`, which only stays in range while one block is
    under a ring's worth past the head; outside that the write head is left
    OUTSIDE the ring and every later call slices with a negative split."""

    def test_block_larger_than_the_ring_keeps_the_head_inside_it(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        s._mic_reu_write_pos = REU_MIC_SIZE - 16
        s._push_mic_to_reu(b"\x07" * (2 * REU_MIC_SIZE + 32))
        self.assertLess(s._mic_reu_write_pos, REU_MIC_SIZE)
        self.assertGreaterEqual(s._mic_reu_write_pos, 0)
        for off, _data in fake.socket_dma.reuwrites:
            self.assertGreaterEqual(off, REU_MIC_BASE)
            self.assertLess(off, REU_MIC_BASE + REU_MIC_SIZE)
