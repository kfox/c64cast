"""Tests for the REU-staged live-mic path (start_mic with use_reu_pump).

The host-side mechanism (REUWRITE wrap, callback encoding, host write
position tracking) is exercised directly. The C64-side IRQ handler is
EXECUTED on the repo's own 6502 (_fakes.run_irq_handler) so a
hand-assembled regression can't pass tests."""

from __future__ import annotations

import dataclasses
import unittest
from typing import Any, cast
from unittest import mock

import numpy as np
from _fakes import (
    FakeAPI,
    lose_reu_writes_to,
    lose_writes_to,
    new_streamer,
    run_irq_handler,
    written_addresses,
)

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
    REU_PUMP_HANDLER_STUB,
    REU_PUMP_TICK_COUNTER_ADDR,
    REU_UPLOAD_SLICE,
    RING_BUFFER_ADDR,
    RING_BUFFER_END_HI,
    RING_BUFFER_HI,
)
from c64cast.audio.mic_lead import MIC_LEAD_REANCHOR_GUARD, MicLeadServo, MicLeadShaper
from c64cast.hw.c64 import CIA1, KERNAL, VECTORS, kernal_cia1_latch


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

    def test_src_carries_into_its_middle_byte_mid_ring(self):
        src = REU_MIC_BASE + 0x2FFF - REU_PUMP_CHUNK_SIZE + 1
        run = self._run(src=src)
        self.assertEqual(self._src(run), REU_MIC_BASE + 0x3000)

    def test_stores_only_to_the_rec_the_trackers_the_counter_and_the_stack(self):
        # A store aimed one page off still leaves the trackers looking right
        # when the src wrap rewrites them, so the footprint is what shows it.
        t = REU_AUDIO_SRC_TRACKER_ADDR
        allowed = (
            set(range(0xDF01, 0xDF09))
            | set(range(t, t + 5))
            | {REU_PUMP_TICK_COUNTER_ADDR}
            | set(range(0x0100, 0x0200))
        )
        last_chunk_dst = (RING_BUFFER_END_HI << 8) - REU_PUMP_CHUNK_SIZE
        last_chunk_src = REU_MIC_BASE + REU_MIC_SIZE - REU_PUMP_CHUNK_SIZE
        for src in (REU_MIC_BASE + 0x2F80, last_chunk_src):
            for dst in (RING_BUFFER_ADDR, last_chunk_dst):
                with self.subTest(src=hex(src), dst=hex(dst)):
                    self.assertLessEqual(written_addresses(self._run(src=src, dst=dst)), allowed)

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
        self.addCleanup(s._stop_mic_lead_servo)
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
        # Device validation is not under test, and must not query the host's.
        s._resolve_input_device = lambda device: (device, "fake")  # type: ignore[method-assign]
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
        s._resolve_input_device = lambda device: (device, "fake")  # type: ignore[method-assign]
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


class TrackedPumpDeliveryTest(unittest.TestCase):
    """_install_tracked_pump confirms each stage (trackers, body, entry)
    delivered before starting the next. A lost tracker write followed by a
    body and entry that land would run the body on stale trackers, whose
    first DMA goes to whatever address the dst tracker held — over the body
    itself, or upward through zero page."""

    def _start(self, *, lose: int | None = None, times: int | None = None, skip_hook: bool):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, lose if lose is not None else 0x0000, 0 if lose is None else times)
        opened: list[int] = []

        def open_stream(device, callback=None, *, sample_rate=None):
            opened.append(device)
            return _FakeStream()

        s._open_input_stream = open_stream
        s._start_mic_for_reu_pump(device=-1, skip_irq_vector_hook=skip_hook)
        self.addCleanup(s._stop_mic_lead_servo)
        return s, fake, opened

    @staticmethod
    def _index(fake: FakeAPI, *op) -> int:
        return next(i for i, o in enumerate(fake.ops) if o[: len(op)] == op)

    def test_each_stage_is_flushed_before_the_next_starts(self):
        for skip_hook in (False, True):
            with self.subTest(skip_hook=skip_hook):
                _s, fake, _ = self._start(skip_hook=skip_hook)
                tracker = self._index(fake, "write_memory", "C200")
                body = self._index(fake, "write_memory_file", "C180")
                entry = self._index(fake, "write_memory_file", "C100")
                self.assertIn(("flush",), fake.ops[tracker:body])
                self.assertIn(("flush",), fake.ops[body:entry])
                self.assertIn(("flush",), fake.ops[entry:])

    def test_a_lost_tracker_write_is_resent_before_the_body(self):
        s, fake, opened = self._start(lose=REU_AUDIO_SRC_TRACKER_ADDR, times=1, skip_hook=True)
        lost = self._index(fake, "lost", "C200")
        resent = self._index(fake, "write_memory", "C200")
        body = self._index(fake, "write_memory_file", "C180")
        self.assertLess(lost, resent)
        self.assertLess(resent, body)
        self.assertTrue(s._reu_pump_armed)
        self.assertEqual(opened, [-1])

    def test_trackers_that_never_land_abort_the_bring_up(self):
        for skip_hook in (False, True):
            with self.subTest(skip_hook=skip_hook):
                with self.assertLogs("c64cast.audio.audio", level="ERROR") as cm:
                    s, fake, opened = self._start(
                        lose=REU_AUDIO_SRC_TRACKER_ADDR, skip_hook=skip_hook
                    )
                self.assertTrue(any("plays without audio" in m for m in cm.output), cm.output)
                self.assertEqual(
                    sum(1 for o in fake.ops if o == ("lost", "C200")),
                    audio_mod.TRACKED_PUMP_INSTALL_TRIES,
                )
                # Neither the body nor the entry went up, and $C180 is parked on
                # an RTS for a dispatcher that JSRs it.
                self.assertNotIn(
                    REU_MIC_PUMP_BODY_SUBROUTINE,
                    [o[2] for o in fake.ops if o[:2] == ("write_memory_file", "C180")],
                )
                self.assertFalse(any(o[:2] == ("write_memory_file", "C100") for o in fake.ops))
                self.assertEqual(fake.memories["C180"], "60")
                # Nothing armed, no mic stream, and the NMI bring-up undone.
                self.assertFalse(s._reu_pump_armed)
                self.assertFalse(s.running)
                self.assertEqual(opened, [])
                self.assertNotIn("0314", fake.regs)
                self.assertEqual(fake.regs["DD0D"][0], 0x7F)
                self.assertEqual(fake.nmi_consumer_notes[-1], False)

    def test_dispatcher_entry_upload_is_bracketed_by_a_cia1_mask(self):
        _s, fake, _ = self._start(skip_hook=True)
        mask = self._index(fake, "write_memory", "DC0D", "7F")
        entry = self._index(fake, "write_memory_file", "C100")
        unmask = self._index(fake, "write_memory", "DC0D", "81")
        self.assertLess(mask, entry)
        self.assertLess(entry, unmask)
        # The mask is confirmed delivered before the entry goes up.
        self.assertIn(("flush",), fake.ops[mask:entry])

    def test_solo_path_leaves_cia1_alone(self):
        # $0314 is hooked last on the solo path, so $C100 is unreachable while
        # the entry goes up and masking would only cost keyboard ticks.
        _s, fake, _ = self._start(skip_hook=False)
        self.assertFalse(any(o[:2] == ("write_memory", "DC0D") for o in fake.ops))

    def test_a_failed_dispatcher_install_leaves_cia1_unmasked(self):
        # The entry stage masks CIA #1, so a body or an entry that never lands
        # must not leave the keyboard scan and the pump's fall-through dead.
        for lost in (REU_PUMP_BODY_SUBROUTINE_ADDR, REU_PUMP_HANDLER_ADDR):
            with self.subTest(lost=f"${lost:04X}"):
                with self.assertLogs("c64cast.audio.audio", level="ERROR"):
                    s, fake, _ = self._start(lose=lost, skip_hook=True)
                self.assertFalse(s._reu_pump_armed)
                icr = [o for o in fake.ops if o[:2] == ("write_memory", "DC0D")]
                self.assertEqual(icr[-1][2], "81")

    def test_a_resent_entry_goes_up_under_a_fresh_mask(self):
        # The first attempt's unmask lands even though its entry did not, so
        # the retry has to mask again before the entry replaces the stub.
        s, fake, _ = self._start(lose=REU_PUMP_HANDLER_ADDR, times=1, skip_hook=True)
        self.assertTrue(s._reu_pump_armed)
        entries = [
            i
            for i, o in enumerate(fake.ops)
            if o == ("lost", "C100") or o[:2] == ("write_memory_file", "C100")
        ]
        self.assertEqual(len(entries), 2)
        for i in entries:
            icr = [o for o in fake.ops[:i] if o[:2] == ("write_memory", "DC0D")]
            self.assertEqual(icr[-1][2], "7F")

    def test_an_entry_that_never_lands_parks_the_body(self):
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            _s, fake, _ = self._start(lose=REU_PUMP_HANDLER_ADDR, skip_hook=True)
        park = max(i for i, o in enumerate(fake.ops) if o == ("write_memory", "C180", "60"))
        body = self._index(fake, "write_memory_file", "C180")
        self.assertLess(body, park)

    def test_an_unconfirmed_dispatcher_entry_is_put_back_to_the_stub(self):
        # Every attempt's entry may have landed with only the epoch moving, so
        # the park puts the installer's stub back where the dispatcher JMPs.
        tries = audio_mod.TRACKED_PUMP_INSTALL_TRIES
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            _s, fake, _ = self._start(lose=REU_PUMP_HANDLER_ADDR, times=tries, skip_hook=True)
        self.assertEqual(fake.mem_files["C100"], REU_PUMP_HANDLER_STUB)

    @staticmethod
    def _vector_writes(fake: FakeAPI) -> list[tuple]:
        return [o[2] for o in fake.ops if o[:2] == ("write_regs", "0314")]

    def test_a_latch_that_never_lands_aborts_before_the_vector_patch(self):
        # The pump code is in place by then, so the abort parks it and puts the
        # kernal's CIA #1 rate back, and $0314 is never pointed at $C100.
        tries = audio_mod.TRACKED_PUMP_INSTALL_TRIES
        for skip_hook in (False, True):
            with self.subTest(skip_hook=skip_hook):
                with self.assertLogs("c64cast.audio.audio", level="ERROR"):
                    s, fake, opened = self._start(
                        lose=CIA1.TIMER_A_LO, times=tries, skip_hook=skip_hook
                    )
                self.assertFalse(s._reu_pump_armed)
                self.assertFalse(s.running)
                self.assertEqual(opened, [])
                self.assertEqual(fake.memories["C180"], "60")
                self.assertEqual(fake.memories["DC04"], _packed_latch(kernal_cia1_latch("NTSC")))
                self.assertEqual(self._vector_writes(fake), [])
                # Under a dispatcher, which keeps JMPing to $C100, the tracked
                # entry goes back to the installer's JMP $EA31 stub.
                expected = REU_PUMP_HANDLER_STUB if skip_hook else REU_IRQ_HANDLER_TRACKED
                self.assertEqual(fake.mem_files["C100"], expected)

    def test_a_vector_patch_that_never_confirms_is_restored_to_the_kernal(self):
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s, fake, opened = self._start(
                lose=VECTORS.IRQ, times=audio_mod.TRACKED_PUMP_INSTALL_TRIES, skip_hook=False
            )
        self.assertEqual(
            self._vector_writes(fake)[-1], (KERNAL.IRQ_HANDLER & 0xFF, KERNAL.IRQ_HANDLER >> 8)
        )
        self.assertFalse(s._reu_pump_armed)
        self.assertEqual(opened, [])
        self.assertEqual(fake.memories["C180"], "60")


class _PendingAnchor:
    """A lead servo with one re-anchor waiting and a fixed drop fraction."""

    def __init__(self, anchor: int | None, drop_frac: float = 0.0) -> None:
        self.anchor = anchor
        self.drop_frac = drop_frac

    def take_reanchor(self) -> int | None:
        anchor, self.anchor = self.anchor, None
        return anchor


class MicLeadServoWiringTest(unittest.TestCase):
    """The streamer side of the #560 lead servo: the callback applies its
    re-anchor and drop fraction, and bring-up/teardown own its thread."""

    def _streamer(self, anchor: int | None, drop_frac: float = 0.0) -> AudioStreamer:
        s = _new_streamer()
        s.running = True
        s._mic_reu_write_pos = 5000
        cast(Any, s)._mic_lead = _PendingAnchor(anchor, drop_frac)
        s._mic_shaper = MicLeadShaper(s.sample_rate)
        return s

    def test_a_reanchor_restarts_the_head_past_the_pump_behind_a_neutral_fill(self):
        anchor = REU_MIC_SIZE - 100
        s = self._streamer(anchor=anchor)
        fake = cast(FakeAPI, s.api)
        s._mic_callback_reu(np.full((256, 1), 0.5, dtype=np.float32), 256, None, None)
        # The fill covers the pump's estimated position itself, so the first
        # bytes it reads after the re-anchor are NEUTRAL, not the lap-old ring.
        fill = REU_MIC_BOOTSTRAP_BYTES + MIC_LEAD_REANCHOR_GUARD
        start = (anchor - MIC_LEAD_REANCHOR_GUARD) % REU_MIC_SIZE
        self.assertEqual(fake.socket_dma.reuwrites[0][0], REU_MIC_BASE + start)
        data = b"".join(chunk for _, chunk in fake.socket_dma.reuwrites)
        self.assertEqual(data[:fill], bytes([NEUTRAL_SAMPLE]) * fill)
        self.assertEqual(
            s._mic_reu_write_pos, (anchor + REU_MIC_BOOTSTRAP_BYTES + 256) % REU_MIC_SIZE
        )

    def test_a_failed_reanchor_fill_leaves_the_head_where_it_was(self):
        # Moving the head before a write that then fails would drop the fill
        # and leave the next block a chunk or two past the pump.
        s = self._streamer(anchor=REU_MIC_SIZE - 100)

        def boom(off: int, data: bytes) -> None:
            raise RuntimeError("link down")

        cast(Any, s).api.reu_write = boom
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s._mic_callback_reu(np.full((256, 1), 0.5, dtype=np.float32), 256, None, None)
        self.assertEqual(s._mic_reu_write_pos, 5000)

    def test_without_a_reanchor_the_head_continues(self):
        s = self._streamer(anchor=None)
        fake = cast(FakeAPI, s.api)
        s._mic_callback_reu(np.full((256, 1), 0.5, dtype=np.float32), 256, None, None)
        self.assertEqual(fake.socket_dma.reuwrites[0][0], REU_MIC_BASE + 5000)
        self.assertEqual(s._mic_reu_write_pos, 5256)

    def test_the_drop_fraction_shortens_what_is_written(self):
        s = self._streamer(anchor=None, drop_frac=0.02)
        for _ in range(40):
            s._mic_callback_reu(np.full((256, 1), 0.5, dtype=np.float32), 256, None, None)
        written = (s._mic_reu_write_pos - 5000) % REU_MIC_SIZE
        self.assertAlmostEqual(written / (40 * 256), 0.98, delta=0.002)

    def test_a_block_the_shaper_holds_back_writes_nothing(self):
        s = self._streamer(anchor=None, drop_frac=0.35)
        fake = cast(FakeAPI, s.api)
        shaper = s._mic_shaper
        assert shaper is not None
        held = False
        for _ in range(8):
            before = len(fake.socket_dma.reuwrites)
            s._mic_callback_reu(np.full((256, 1), 0.5, dtype=np.float32), 256, None, None)
            if len(shaper._held) and len(fake.socket_dma.reuwrites) == before:
                held = True
        self.assertTrue(held)

    def test_bring_up_starts_the_servo_and_stop_ends_it(self):
        s = _new_streamer()
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        s._start_mic_for_reu_pump(device=-1)
        lead = s._mic_lead
        assert lead is not None
        thread = lead._thread
        assert thread is not None and thread.is_alive()
        s.stop()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(s._mic_lead)
        self.assertIsNone(s._mic_shaper)

    def test_a_backend_without_reads_runs_open_loop_and_says_so(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        fake.profile = dataclasses.replace(fake.profile, supports_read=False)
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        with self.assertLogs("c64cast.audio.audio", "WARNING") as cm:
            s._start_mic_for_reu_pump(device=-1)
        self.assertTrue(any("lead servo is off" in m for m in cm.output), cm.output)
        self.assertIsNone(s._mic_lead)
        self.assertIsNotNone(s._mic_shaper)

    def test_stop_summarizes_a_run_the_servo_measured(self):
        s = _new_streamer()
        servo = MicLeadServo(
            read_memory=lambda *a, **k: None, write_pos=lambda: 0, sample_rate=12000
        )
        servo.lead_min, servo.lead_max, servo.reanchors = 1400, 1800, 2
        servo.reanchors_dropped = 1
        s._mic_lead = servo
        s._mic_shaper = MicLeadShaper(12000)
        with self.assertLogs("c64cast.audio.audio", "INFO") as cm:
            s.stop()
        self.assertTrue(
            any("lead 1400..1800 B, 2 re-anchor(s) (1 dropped unclaimed)" in m for m in cm.output),
            cm.output,
        )

    def test_stop_ends_the_servo_loop_before_the_teardown(self):
        # A teardown stalled past the claim window would otherwise have the
        # servo drop an unclaimed re-anchor at WARNING during a normal stop.
        s = _new_streamer()
        servo = MicLeadServo(
            read_memory=lambda *a, **k: None, write_pos=lambda: 0, sample_rate=12000
        )
        s._mic_lead = servo
        seen: list[bool] = []
        cast(Any, s)._disarm_reu_pump = lambda: seen.append(servo._stop.is_set())
        s.stop()
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()


class MicRingPrefillDeliveryTest(unittest.TestCase):
    """The NEUTRAL prefill of the REU mic ring is confirmed per slice: a lost
    slice would have the pump play stale FPGA SRAM, which can be loud."""

    def _start(self, lose: int, times: int | None):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        lose_reu_writes_to(fake, lose, times)
        opened: list[int] = []

        def open_stream(device, callback=None, *, sample_rate=None):
            opened.append(device)
            return _FakeStream()

        s._open_input_stream = open_stream
        s._start_mic_for_reu_pump(device=-1)
        self.addCleanup(s._stop_mic_lead_servo)
        return s, fake, opened

    def test_a_lost_slice_is_resent(self):
        s, fake, opened = self._start(REU_MIC_BASE + REU_UPLOAD_SLICE, 1)
        self.assertTrue(s._reu_pump_armed)
        self.assertEqual(opened, [-1])
        landed = dict(fake.socket_dma.reuwrites)
        self.assertEqual(
            landed[REU_MIC_BASE + REU_UPLOAD_SLICE], bytes([NEUTRAL_SAMPLE]) * REU_UPLOAD_SLICE
        )

    def test_a_slice_that_never_lands_aborts_the_bring_up(self):
        with self.assertLogs("c64cast.audio.audio", level="ERROR") as cm:
            s, fake, opened = self._start(REU_MIC_BASE + REU_UPLOAD_SLICE, None)
        self.assertTrue(any("plays without audio" in m for m in cm.output), cm.output)
        self.assertFalse(s._reu_pump_armed)
        self.assertFalse(s.running)
        self.assertEqual(opened, [])
        self.assertNotIn(f"{REU_PUMP_HANDLER_ADDR:04X}", fake.mem_files)
        self.assertNotIn("0314", fake.regs)
