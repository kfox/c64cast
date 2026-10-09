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
    READ_PTR_LO_ADDR,
    REU_AUDIO_DST_TRACKER_ADDR,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_CMD_FETCH_EXEC,
    REU_IRQ_HANDLER_TRACKED,
    REU_MIC_BASE,
    REU_MIC_BASE_HI,
    REU_MIC_BOOTSTRAP_BYTES,
    REU_MIC_PUMP_BODY_SUBROUTINE,
    REU_MIC_RING_LEAD,
    REU_MIC_RING_LEAD_MIN,
    REU_MIC_SIZE,
    REU_PUMP_BODY_SUBROUTINE_ADDR,
    REU_PUMP_CHUNK_SIZE,
    REU_PUMP_CIA1_LATCH_8KHZ,
    REU_PUMP_HANDLER_ADDR,
    REU_PUMP_HANDLER_STUB,
    REU_PUMP_TICK_COUNTER_ADDR,
    REU_UPLOAD_SLICE,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    RING_BUFFER_END_HI,
    RING_BUFFER_HI,
    RING_BUFFER_SIZE,
    mic_ring_lead_ok,
    mic_ring_seed,
)
from c64cast.audio.mic_lead import (
    MIC_LEAD_READ_TIMEOUT_S,
    MIC_LEAD_REANCHOR_GUARD,
    MicLeadServo,
    MicLeadShaper,
    MicRingGovernor,
    TrimWrite,
)
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
        # The chunked dispatchers JSR $C180 directly between REC
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
        # $C180 (chunked dispatcher JSR) or $C100 (fall-through) mid-install. The
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

    def test_trackers_seeded_to_mic_base_and_the_ring_lead(self):
        # src LO/MI/HI = REU_MIC_BASE, dst LO/HI = REU_MIC_RING_LEAD into the
        # ring, ahead of the NMI reader parked at its start. The pump reads
        # only these, so a wrong seed makes the first transfer read a bogus REU
        # offset or land outside the ring; a dst at the ring start itself put
        # the write head behind a reader that had already started (A-F3).
        s = self._start()
        fake = cast(FakeAPI, s.api)
        dst = RING_BUFFER_ADDR + REU_MIC_RING_LEAD
        expected = (
            f"{REU_MIC_BASE & 0xFF:02X}"
            f"{(REU_MIC_BASE >> 8) & 0xFF:02X}"
            f"{(REU_MIC_BASE >> 16) & 0xFF:02X}"
            f"{dst & 0xFF:02X}"
            f"{(dst >> 8) & 0xFF:02X}"
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


class _RingPointers:
    """A FakeAPI's view of the two pointers the ring-lead seed reads: the NMI
    reader R (fixed per test) and the pump's dst tracker W, taken from the last
    tracker write that landed plus ``pump_ran`` bytes (a pump that ran before
    the NMI armed). The first ``lose`` writes to the dst pair are overwritten
    by the pump's own tick, as a write between its load and store would be.
    The first ``glitch`` span reads after a dst write catch R in its ring-end
    carry, at $6000, outside the ring. The pump's src tracker reads
    ``src_per_read`` bytes further into the REU mic ring on every span read,
    as a pump that keeps consuming while the bring-up goes on."""

    def __init__(
        self,
        fake: FakeAPI,
        *,
        r: int,
        pump_ran: int = 0,
        lose: int = 0,
        glitch: int = 0,
        src_per_read: int = 0,
    ) -> None:
        self.src_per_read = src_per_read
        self.r = r
        self.pump_ran = pump_ran
        self.lose = lose
        self.glitch = glitch
        self.dst_writes: list[int] = []
        self.reads = 0
        self._real_write = fake.write_memory
        fake.write_memory = self._write  # type: ignore[method-assign]
        fake.read_memory = self._read  # type: ignore[method-assign]
        self._fake = fake

    def _w(self) -> int:
        mem = self._fake.memories
        seed = mem[f"{REU_AUDIO_SRC_TRACKER_ADDR:04X}"]  # src LO MI HI, dst LO HI
        w = int(seed[8:10] + seed[6:8], 16)
        pair = mem.get(f"{REU_AUDIO_DST_TRACKER_ADDR:04X}")
        if pair is not None:
            w = int(pair[2:4] + pair[0:2], 16)
        off = (w - RING_BUFFER_ADDR + self.pump_ran) % RING_BUFFER_SIZE
        return RING_BUFFER_ADDR + off

    def _write(self, addr, data_hex) -> None:
        if str(addr).upper() == f"{REU_AUDIO_DST_TRACKER_ADDR:04X}":
            self.dst_writes.append(int(data_hex[2:4] + data_hex[0:2], 16))
            self.pump_ran = 0
            if self.lose:
                self.lose -= 1
                return
        self._real_write(addr, data_hex)

    def _read(self, address, length, timeout=1.0):
        if address != READ_PTR_LO_ADDR:
            return None
        if length > 2:  # the seed's span read, not the NMI arm check's R read
            self.reads += 1
        raw = bytearray(length)
        w = self._w()
        off = REU_AUDIO_DST_TRACKER_ADDR - READ_PTR_LO_ADDR
        r = self.r
        if length > 2 and self.dst_writes and self.glitch:
            self.glitch -= 1
            r = RING_BUFFER_END
        raw[0:2] = r.to_bytes(2, "little")
        raw[off : off + 2] = w.to_bytes(2, "little")
        # The src tracker the install seeded, which the span read also checks.
        src_off = REU_AUDIO_SRC_TRACKER_ADDR - READ_PTR_LO_ADDR
        src = REU_MIC_BASE + self.src_per_read * self.reads
        raw[src_off : src_off + 3] = src.to_bytes(3, "little")
        return bytes(raw)

    def lead(self) -> int:
        return (self._w() - self.r) % RING_BUFFER_SIZE


class MicRingLeadSeedTest(unittest.TestCase):
    """AUD-2 A-F3: the bring-up chooses how far the pump's write head runs
    ahead of the NMI reader in the $4000 ring, instead of inheriting whatever
    the arm order left. On the solo path the reader is ~1 KB into the ring
    before the pump's first tick, so a pump seeded at the ring start trailed it
    and every sample waited most of a lap (~0.6 s at 12 kHz); under a
    dispatcher the pump ran first and led by a few hundred bytes, which the
    reader overtook into lap-old audio."""

    def _start(self, *, skip_irq_vector_hook: bool = False, **ring):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        ptrs = _RingPointers(fake, **ring)
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        # The lead servo's thread would share the fake's read; this class is
        # about the bring-up alone.
        cast(Any, s)._start_mic_lead_servo = lambda: None
        s._start_mic_for_reu_pump(device=-1, skip_irq_vector_hook=skip_irq_vector_hook)
        return s, ptrs

    def test_solo_pump_is_seeded_ahead_of_a_reader_already_into_the_ring(self):
        # R 125 ms into the ring when the $0314 patch starts the pump (the
        # finding measured >=963 B, ~80 ms; a slow confirm stretches it), past
        # what the install seed's lead absorbs. The pump ran a chunk since.
        r = RING_BUFFER_ADDR + 1500
        s, ptrs = self._start(r=r, pump_ran=REU_PUMP_CHUNK_SIZE)
        self.assertEqual(ptrs.dst_writes, [mic_ring_seed(r)])
        lead = ptrs.lead()
        self.assertGreaterEqual(lead, REU_MIC_RING_LEAD)
        self.assertLess(lead, REU_MIC_RING_LEAD + REU_PUMP_CHUNK_SIZE)

    def test_dispatcher_pump_that_ran_before_the_arm_is_reseeded(self):
        s, ptrs = self._start(skip_irq_vector_hook=True, r=RING_BUFFER_ADDR, pump_ran=1536)
        self.assertEqual(ptrs.dst_writes, [mic_ring_seed(RING_BUFFER_ADDR)])
        self.assertEqual(ptrs.lead(), REU_MIC_RING_LEAD)

    def test_a_lead_already_in_range_is_left_alone(self):
        # Every write is a chance for the pump's own tick to overwrite it. The
        # measured ~963 B solo arm-to-pump lag leaves the install seed's lead
        # at ~1.1 KB, in range.
        s, ptrs = self._start(r=RING_BUFFER_ADDR + 963)
        self.assertEqual(ptrs.dst_writes, [])
        self.assertEqual(ptrs.reads, 1)

    def test_a_seed_the_pump_overwrote_is_written_again(self):
        r = RING_BUFFER_ADDR + 1500
        s, ptrs = self._start(r=r, lose=1)
        self.assertEqual(ptrs.dst_writes, [mic_ring_seed(r)] * 2)
        self.assertTrue(mic_ring_lead_ok(ptrs.lead()))

    def test_a_read_that_fails_after_a_seed_is_retried(self):
        # Once a seed has gone out the tracker no longer holds the install
        # seed, so one torn read must not end the bring-up's measurement.
        r = RING_BUFFER_ADDR + 1500
        s, ptrs = self._start(r=r, lose=1, glitch=1)
        self.assertEqual(ptrs.reads, 4)
        self.assertEqual(ptrs.dst_writes, [mic_ring_seed(r)] * 2)
        self.assertTrue(mic_ring_lead_ok(ptrs.lead()))

    def test_a_seed_that_never_holds_is_a_warning(self):
        with self.assertLogs("c64cast.audio.audio", "WARNING") as cm:
            s, ptrs = self._start(r=RING_BUFFER_ADDR + 1500, lose=99)
        self.assertEqual(len(ptrs.dst_writes), audio_mod.TRACKED_PUMP_INSTALL_TRIES)
        self.assertTrue(any("lead over the NMI reads" in m for m in cm.output), cm.output)
        self.assertTrue(s.running, "an unseeded lead costs latency, not the scene's audio")

    def test_unreadable_pointers_keep_the_install_seed(self):
        s = _new_streamer()
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        cast(Any, s)._start_mic_lead_servo = lambda: None
        with self.assertLogs("c64cast.audio.audio", "INFO") as cm:
            s._start_mic_for_reu_pump(device=-1)
        self.assertTrue(any("stays at its install seed" in m for m in cm.output), cm.output)
        self.assertNotIn(f"{REU_AUDIO_DST_TRACKER_ADDR:04X}", cast(FakeAPI, s.api).memories)
        self.assertEqual(s._mic_reu_write_pos, REU_MIC_BOOTSTRAP_BYTES)

    def test_a_backend_without_reads_does_not_try(self):
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        fake.profile = dataclasses.replace(fake.profile, supports_read=False)
        ptrs = _RingPointers(fake, r=RING_BUFFER_ADDR + 963)
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        cast(Any, s)._start_mic_lead_servo = lambda: None
        s._start_mic_for_reu_pump(device=-1)
        self.assertEqual((ptrs.reads, ptrs.dst_writes), (0, []))
        self.assertEqual(s._mic_reu_write_pos, REU_MIC_BOOTSTRAP_BYTES)

    def test_a_slow_bring_up_starts_the_head_past_where_the_pump_has_got_to(self):
        # The pump consumes from its first tick, so the head is anchored at the
        # src tracker as the seed last read it, not at the ring start.
        r = RING_BUFFER_ADDR + 1500
        s, ptrs = self._start(r=r, lose=2, src_per_read=700)
        self.assertGreater(ptrs.reads, 1)
        self.assertEqual(
            s._mic_reu_write_pos,
            (700 * ptrs.reads + REU_MIC_BOOTSTRAP_BYTES) % REU_MIC_SIZE,
        )

    def test_a_head_anchored_near_the_ring_end_wraps(self):
        s, ptrs = self._start(r=RING_BUFFER_ADDR + 963, src_per_read=REU_MIC_SIZE - 100)
        self.assertEqual(s._mic_reu_write_pos, (REU_MIC_BOOTSTRAP_BYTES - 100) % REU_MIC_SIZE)

    def test_the_bring_up_log_states_both_stages_of_the_latency(self):
        with self.assertLogs("c64cast.audio.audio", "INFO") as cm:
            s, ptrs = self._start(r=RING_BUFFER_ADDR + 1500)
        line = next(m for m in cm.output if "host lead=" in m)
        lead = ptrs.lead()
        ms = round(1000 * (REU_MIC_BOOTSTRAP_BYTES + lead) / s.sample_rate)
        self.assertIn(f"C64 ring lead={lead}B", line)
        self.assertIn(f"({ms}ms latency)", line)


class MicRingSeedMathTest(unittest.TestCase):
    def test_seed_is_chunk_aligned_and_at_least_the_lead_ahead(self):
        for off in (
            0,
            1,
            127,
            128,
            963,
            RING_BUFFER_SIZE - REU_MIC_RING_LEAD,
            RING_BUFFER_SIZE - 1,
        ):
            r = RING_BUFFER_ADDR + off
            dst = mic_ring_seed(r)
            self.assertTrue(RING_BUFFER_ADDR <= dst < RING_BUFFER_END, hex(dst))
            self.assertEqual((dst - RING_BUFFER_ADDR) % REU_PUMP_CHUNK_SIZE, 0)
            lead = (dst - r) % RING_BUFFER_SIZE
            self.assertGreaterEqual(lead, REU_MIC_RING_LEAD, off)
            self.assertLess(lead, REU_MIC_RING_LEAD + REU_PUMP_CHUNK_SIZE, off)

    def test_lead_window(self):
        self.assertFalse(mic_ring_lead_ok(REU_MIC_RING_LEAD_MIN - 1))
        self.assertTrue(mic_ring_lead_ok(REU_MIC_RING_LEAD_MIN))
        self.assertTrue(mic_ring_lead_ok(REU_MIC_RING_LEAD + REU_PUMP_CHUNK_SIZE + 256))
        self.assertFalse(mic_ring_lead_ok(REU_MIC_RING_LEAD + REU_PUMP_CHUNK_SIZE + 257))
        # The old solo-path lead: W a lap behind R.
        self.assertFalse(mic_ring_lead_ok(RING_BUFFER_SIZE - 963))


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

    def test_the_entry_waits_out_an_in_flight_pump_after_the_mask(self):
        # A pump the dispatcher entered just before the mask landed is still
        # running $C100; the drain lets it leave before its code is replaced.
        s = _new_streamer()
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, 0x0000, 0)
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        with mock.patch.object(
            audio_mod.time, "sleep", side_effect=lambda dt: fake.ops.append(("sleep", dt))
        ):
            s._start_mic_for_reu_pump(device=-1, skip_irq_vector_hook=True)
        self.addCleanup(s._stop_mic_lead_servo)
        mask = self._index(fake, "write_memory", "DC0D", "7F")
        entry = self._index(fake, "write_memory_file", "C100")
        drain = fake.ops.index(("sleep", audio_mod.TRACKED_PUMP_ENTRY_DRAIN_S))
        self.assertIn(("flush",), fake.ops[mask:drain])
        self.assertLess(drain, entry)

    def test_a_lost_mask_holds_the_entry_back_until_a_mask_lands(self):
        # With the mask lost the dispatcher still JMPs to $C100, so writing
        # the entry then would replace code a pump may be running.
        s, fake, _ = self._start(lose=0xDC0D, times=1, skip_hook=True)
        self.assertTrue(s._reu_pump_armed)
        lost = fake.ops.index(("lost", "DC0D"))
        remask = next(
            i for i, o in enumerate(fake.ops) if i > lost and o == ("write_memory", "DC0D", "7F")
        )
        self.assertFalse(any(o[:2] == ("write_memory_file", "C100") for o in fake.ops[lost:remask]))

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

    def test_a_lost_entry_stub_restore_is_resent(self):
        # The link that lost every entry attempt can lose the stub restore
        # too; one lost restore would leave the tracked entry where the
        # dispatcher JMPs, with the kernal at a third of its rate.
        tries = audio_mod.TRACKED_PUMP_INSTALL_TRIES
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            _s, fake, _ = self._start(lose=REU_PUMP_HANDLER_ADDR, times=tries + 1, skip_hook=True)
        self.assertEqual(fake.mem_files["C100"], REU_PUMP_HANDLER_STUB)
        restore = self._index(fake, "write_memory_file", "C100")
        icr = [o for o in fake.ops[:restore] if o[:2] == ("write_memory", "DC0D")]
        self.assertEqual(icr[-1][2], "7F")

    def test_an_entry_stub_restore_that_never_lands_is_logged(self):
        with self.assertLogs("c64cast.audio.audio", level="ERROR") as cm:
            _s, fake, _ = self._start(lose=REU_PUMP_HANDLER_ADDR, skip_hook=True)
        self.assertTrue(any("pump entry stub restore" in m for m in cm.output), cm.output)
        icr = [o for o in fake.ops if o[:2] == ("write_memory", "DC0D")]
        self.assertEqual(icr[-1][2], "81")

    def test_a_lost_body_park_is_resent(self):
        # A body stage that never confirmed may have left a torn body where the
        # chunked dispatcher JSRs, so the RTS that parks it is confirmed too.
        tries = audio_mod.TRACKED_PUMP_INSTALL_TRIES
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            _s, fake, _ = self._start(
                lose=REU_PUMP_BODY_SUBROUTINE_ADDR, times=tries + 1, skip_hook=True
            )
        self.assertEqual(sum(1 for o in fake.ops if o == ("lost", "C180")), tries + 1)
        self.assertEqual(fake.memories["C180"], "60")

    def test_a_lost_cia1_unmask_is_resent(self):
        # Every entry and stub-restore attempt masks CIA #1, and a link that
        # lost all of those can lose the unmask too, which would leave the
        # kernal with no jiffy IRQ for the rest of the scene.
        lost = 2 * audio_mod.TRACKED_PUMP_INSTALL_TRIES + 1
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            _s, fake, _ = self._start(lose=0xDC0D, times=lost, skip_hook=True)
        self.assertEqual(sum(1 for o in fake.ops if o == ("lost", "DC0D")), lost)
        self.assertEqual(fake.memories["DC0D"], "81")

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

    def test_the_servo_reads_the_live_write_head_at_the_streamer_rate(self):
        s = _new_streamer(sample_rate=10000)
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        s._start_mic_for_reu_pump(device=-1)
        self.addCleanup(s.stop)
        lead = s._mic_lead
        assert lead is not None
        s._mic_reu_write_pos = 4321
        self.assertEqual(lead._write_pos(), 4321)
        self.assertEqual(lead._rate, 10000)

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

    def test_stop_summarizes_the_ring_governor(self):
        s = _new_streamer()
        gov = MicRingGovernor(
            write_latch=lambda latch: TrimWrite.DELIVERED,
            matched_latch=10879,
            sample_rate=12000,
        )
        gov.lead_min, gov.lead_max, gov.slow_min, gov.slow_max = 1900, 2300, 0.01, 0.06
        gov.latch = 11500
        s._mic_lead = MicLeadServo(
            read_memory=lambda *a, **k: None,
            write_pos=lambda: 0,
            sample_rate=12000,
            ring_governor=gov,
        )
        with self.assertLogs("c64cast.audio.audio", "INFO") as cm:
            s.stop()
        self.assertTrue(
            any("C64 ring lead 1900..2300 B" in m and "latch 11500" in m for m in cm.output),
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


class MicRingGovernorWiringTest(unittest.TestCase):
    """The streamer side of the #580 ring governor: it rides the lead servo
    with ``reu_pump_governor`` on, and its CIA #1 writes are fenced to the pump
    arm that created it."""

    def _start(self, *, governor: bool = True) -> AudioStreamer:
        s = new_streamer(dither=False, use_reu_pump=True, reu_pump_governor=governor)
        s._open_input_stream = lambda device, callback=None, *, sample_rate=None: _FakeStream()
        s._start_mic_for_reu_pump(device=-1)
        self.addCleanup(s.stop)
        return s

    def _governor(self, s: AudioStreamer) -> MicRingGovernor:
        lead = s._mic_lead
        assert lead is not None and lead.ring_governor is not None
        return lead.ring_governor

    def test_bring_up_attaches_a_governor_at_the_matched_latch(self):
        s = self._start()
        gov = self._governor(s)
        self.assertEqual(gov.latch, s._reu_cia1_latch_nominal)
        self.assertEqual(gov._matched, 10879)

    def test_with_the_governor_off_there_is_none(self):
        s = self._start(governor=False)
        assert s._mic_lead is not None
        self.assertIsNone(s._mic_lead.ring_governor)

    def test_a_trim_writes_the_pump_latch_while_armed(self):
        s = self._start()
        fake = cast(FakeAPI, s.api)
        self.assertIs(self._governor(s)._write_latch(11000), TrimWrite.DELIVERED)
        self.assertEqual(fake.memories["DC04"], _packed_latch(11000))

    def test_no_trim_lands_after_the_disarm_restores_the_kernal_latch(self):
        s = self._start()
        fake = cast(FakeAPI, s.api)
        write = self._governor(s)._write_latch
        s._disarm_reu_pump()
        self.assertIs(write(11000), TrimWrite.REFUSED)
        self.assertEqual(fake.memories["DC04"], _packed_latch(kernal_cia1_latch("NTSC")))

    def test_a_previous_arms_governor_cannot_trim_the_next_pump(self):
        s = self._start()
        stale = self._governor(s)._write_latch
        s.stop()
        s._start_mic_for_reu_pump(device=-1)
        self.assertIs(stale(11000), TrimWrite.REFUSED)
        self.assertIs(self._governor(s)._write_latch(11000), TrimWrite.DELIVERED)

    def test_a_trim_the_link_refused_is_not_flushed(self):
        # A flush over a link that refused the write warns outside the
        # backend's failure ladder, and the governor resends every second.
        s = self._start()
        fake = cast(FakeAPI, s.api)
        lose_writes_to(fake, CIA1.TIMER_A_LO, times=1)
        start = len(fake.ops)
        self.assertIs(self._governor(s)._write_latch(11000), TrimWrite.UNCONFIRMED)
        self.assertEqual(fake.ops[start:], [("lost", "DC04")])

    def test_a_trim_the_link_dropped_is_sent_again(self):
        # #602: the first trim is lost on the link. The second interval's
        # reading asks for the same latch, which the governor took as already
        # written, so the pump ran untrimmed until the output moved a step.
        s = self._start()
        servo = s._mic_lead
        assert servo is not None
        servo.stop()
        fake = cast(FakeAPI, s.api)
        r = RING_BUFFER_ADDR + 0x0A00
        # Two readings PI-equivalent at the same output: (kp + ki)·e1 on the
        # first, kp·e2 + ki·(e1 + e2) on the second, so e2 = e1·kp/(kp + ki).
        w = r + REU_MIC_RING_LEAD + 1100
        image = bytearray(0x10000)
        src = REU_MIC_BASE
        image[REU_AUDIO_SRC_TRACKER_ADDR : REU_AUDIO_SRC_TRACKER_ADDR + 5] = bytes(
            [src & 0xFF, (src >> 8) & 0xFF, src >> 16, w & 0xFF, w >> 8]
        )

        def set_r(addr: int) -> None:
            image[READ_PTR_LO_ADDR : READ_PTR_LO_ADDR + 2] = bytes([addr & 0xFF, addr >> 8])

        set_r(r)

        def read(address: int, length: int, timeout: float = 1.0) -> bytes | None:
            return bytes(image[address : address + length])

        # The servo took the backend's read when it was built.
        fake.read_memory = servo._read = read  # type: ignore[method-assign]
        lose_writes_to(fake, CIA1.TIMER_A_LO, times=1)
        waits: list[float] = []

        class _Stop:
            def wait(self, timeout: float) -> bool:
                waits.append(timeout)
                if len(waits) == 2:
                    set_r(r + 100)
                return len(waits) > 2

            def is_set(self) -> bool:
                return False

        real_stop = servo._stop
        servo._stop = _Stop()  # type: ignore[assignment]
        try:
            servo._run()
        finally:
            servo._stop = real_stop
        gov = self._governor(s)
        self.assertIn(("lost", "DC04"), fake.ops)
        self.assertNotEqual(gov.latch, s._reu_cia1_latch_nominal)
        self.assertEqual(fake.memories["DC04"], _packed_latch(gov.latch))

    def test_a_steady_interval_makes_one_read_for_both_loops(self):
        # #603: the governor's span read covers the src tracker, which the
        # lead servo also read twice an interval on its own: three REST reads
        # a second during playback, where REST polling risks wedging the U64.
        s = self._start()
        servo = s._mic_lead
        assert servo is not None
        servo.stop()
        fake = cast(FakeAPI, s.api)
        image = bytearray(0x10000)
        pump = [0]

        def advance() -> None:
            src = REU_MIC_BASE + pump[0] % REU_MIC_SIZE
            w = RING_BUFFER_ADDR + (REU_MIC_RING_LEAD + pump[0]) % RING_BUFFER_SIZE
            r = RING_BUFFER_ADDR + pump[0] % RING_BUFFER_SIZE
            image[READ_PTR_LO_ADDR : READ_PTR_LO_ADDR + 2] = r.to_bytes(2, "little")
            image[REU_AUDIO_SRC_TRACKER_ADDR : REU_AUDIO_SRC_TRACKER_ADDR + 5] = src.to_bytes(
                3, "little"
            ) + w.to_bytes(2, "little")
            s._mic_reu_write_pos = (REU_MIC_BOOTSTRAP_BYTES + pump[0]) % REU_MIC_SIZE

        reads: list[int] = []

        def read(address: int, length: int, timeout: float = 1.0) -> bytes | None:
            reads[-1] += 1
            return bytes(image[address : address + length])

        # The servo took the backend's read when it was built.
        fake.read_memory = servo._read = read  # type: ignore[method-assign]
        advance()

        class _Stop:
            def wait(self, timeout: float) -> bool:
                pump[0] += 12000
                advance()
                reads.append(0)
                return len(reads) > 6

            def is_set(self) -> bool:
                return False

        real_stop = servo._stop
        servo._stop = _Stop()  # type: ignore[assignment]
        try:
            servo._run()
        finally:
            servo._stop = real_stop
        # The first interval has nothing to check its reading against yet.
        self.assertEqual(reads[1:6], [1] * 5)
        gov = self._governor(s)
        self.assertEqual(gov.failed_reads, 0)
        self.assertEqual((gov.lead_min, gov.lead_max), (REU_MIC_RING_LEAD, REU_MIC_RING_LEAD))
        self.assertEqual(servo._fails, 0)
        self.assertEqual((servo.lead_min, servo.lead_max), (1600, 1600))

    def test_the_shared_read_spans_both_pointers_at_the_servo_timeout(self):
        s = self._start()
        servo = s._mic_lead
        assert servo is not None
        servo.stop()
        calls: list[tuple[int, int, float]] = []

        def read(address: int, length: int, timeout: float = 1.0) -> bytes | None:
            calls.append((address, length, timeout))
            return None

        servo._read = read
        self.assertIsNone(servo.tick())
        span = REU_AUDIO_DST_TRACKER_ADDR + 2 - READ_PTR_LO_ADDR
        self.assertEqual(calls, [(READ_PTR_LO_ADDR, span, MIC_LEAD_READ_TIMEOUT_S)])


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


if __name__ == "__main__":
    unittest.main()
