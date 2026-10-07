"""Lifecycle, worker-pacing, and bring-up/teardown coverage for AudioStreamer.

test_audio.py covers the sample tap + encode happy path; this module fills the
heavy-lift gaps the coverage backlog calls out: the real constructor, the worker
underrun/pacing paths (full + partial pad, prebuffer→strict-pace handoff, crash
guard), digi-boost, encode backpressure, the mic callback, input-device
resolution (against a fake sounddevice), and start/stop/position teardown.

No real U64 and no real sound device — FakeAPI plus a fake `sd` module.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import numpy as np
from _fakes import FakeAPI, FakeTime, FrozenClock, SleepDrivenClock, quiet_logging

from c64cast.audio import audio as audio_mod
from c64cast.audio import audio_rate as audio_rate_mod
from c64cast.audio.audio import AudioStreamer
from c64cast.audio.audio_handlers import (
    NEUTRAL_SAMPLE,
    PREBUFFER_CHUNKS,
    RING_BUFFER_ADDR,
    RING_BUFFER_END,
    SAMPLE_TAP_SIZE,
    STOMP_GUARD_BYTES,
    WORKER_JOIN_TIMEOUT_S,
    encode_floats_to_dac,
)
from c64cast.audio.audio_servo import (
    nmi_rate_step,
)
from c64cast.hw.api import Ultimate64API
from c64cast.hw.c64 import CIA1, CIA2, SID, VECTORS, cpu_clock, kernal_cia1_latch


def _make(**kw: Any) -> AudioStreamer:
    """Construct a real AudioStreamer (exercising __init__) over a FakeAPI."""
    api = cast(Ultimate64API, FakeAPI())
    return AudioStreamer(api, kw.pop("sample_rate", 8000), kw.pop("system", "NTSC"), **kw)


class _SupersedeOnStompRead(AudioStreamer):
    """An AudioStreamer whose worker is superseded the first time it reads a
    set pause request: the next scene's start_* landing between a call site's
    check of the flag and its stomp, deterministically."""

    @property  # type: ignore[override]
    def _stomp_requested(self) -> bool:
        requested = bool(self.__dict__.get("_stomp_flag", False))
        if requested and not self.__dict__.get("_tripped"):
            self.__dict__["_tripped"] = True
            self._worker_generation += 1  # the next scene's _start_worker
        return requested

    @_stomp_requested.setter
    def _stomp_requested(self, value: bool) -> None:
        self.__dict__["_stomp_flag"] = value


class _FlushDuringClaim(AudioStreamer):
    """An AudioStreamer whose worker is flushed, from another thread, between
    reading the flush epoch in ``_claim_ring_write`` and recording the chunk
    as in flight: the window a check made outside ``_count_lock`` leaves open.
    The flush gets 0.2 s to finish there; against the lock it blocks until the
    claim is done."""

    trigger_claim = 0
    _claims = 0

    def __init__(self, *a: Any, **kw: Any) -> None:
        self._epoch = 0
        self.flusher: threading.Thread | None = None
        self.anchors: list[float] = []
        super().__init__(*a, **kw)

    @property  # type: ignore[override]
    def _flush_epoch(self) -> int:
        in_claim = sys._getframe(1).f_code.co_name == "_claim_ring_write"
        if in_claim and threading.current_thread().name == "test-worker":
            self._claims += 1
            if self._claims == self.trigger_claim:
                value = self._epoch
                self.flusher = threading.Thread(
                    target=lambda: self.anchors.append(self.flush()), name="test-flusher"
                )
                self.flusher.start()
                self.flusher.join(0.2)
                return value
        return self._epoch

    @_flush_epoch.setter
    def _flush_epoch(self, value: int) -> None:
        self._epoch = value


def _make_worker_streamer(chunk_size: int = 32, sample_rate: int = 64000) -> AudioStreamer:
    """A streamer wired for fast, hardware-free worker runs: tiny chunks, a
    high sample rate (a ~2 ms pace period once the NMI latch clamps at the
    handler ceiling), and a stubbed NMI timer so the
    prebuffer→pace handoff runs without touching CIA registers."""
    s = _make(sample_rate=sample_rate)
    s.chunk_size = chunk_size
    s.nmi.start = lambda **kw: None  # type: ignore[method-assign]
    return s


def _written_stream(s: AudioStreamer) -> bytes:
    """Every byte the worker sent to the ring, in order.

    The worker splits each chunk into sub-NMI-period pieces, so no single write
    is the whole chunk any more. What the C64 sees is the concatenation, which
    is also the invariant worth asserting on — it survives any future change to
    the quantum.
    """
    return b"".join(data for _, data in cast(Any, s.api).writes)


def _run_worker(s: AudioStreamer, until, timeout: float = 2.0) -> threading.Thread:
    """Start the worker thread and spin until `until()` is true or timeout."""
    s.running = True
    t = threading.Thread(
        target=s._worker, args=(s._worker_generation,), daemon=True, name="test-worker"
    )
    t.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not until():
        time.sleep(0.005)
    s.running = False
    t.join(timeout=1.0)
    return t


class ConstructorTest(unittest.TestCase):
    def test_defaults(self):
        s = _make()
        self.assertEqual(s.sample_rate, 8000)
        self.assertEqual(s.system, "NTSC")
        self.assertTrue(s.dither_enabled)
        self.assertFalse(s.digi_boost)
        self.assertFalse(s.use_reu_pump)
        self.assertFalse(s.running)
        self.assertEqual(s.chunk_size, 1024)
        self.assertEqual(s._full_underruns, 0)
        self.assertEqual(s._partial_underruns, 0)
        self.assertEqual(s._queued_samples, 0)
        self.assertIsInstance(s.q, queue.Queue)
        self.assertIsNone(s._worker_thread)
        self.assertIsNone(s.mic_stream)
        self.assertFalse(s._reu_pump_armed)

    def test_flag_passthrough(self):
        s = _make(
            dither=False, digi_boost=True, use_reu_pump=True, sid_filter_cutoff=1200, system="PAL"
        )
        self.assertFalse(s.dither_enabled)
        self.assertTrue(s.digi_boost)
        self.assertTrue(s.use_reu_pump)
        self.assertEqual(s.sid_filter_cutoff, 1200)
        self.assertEqual(s.system, "PAL")


class WorkerPacingUnderrunTest(unittest.TestCase):
    def test_idle_no_data_no_nmi(self):
        # Empty queue, never prebuffered: the worker must spin on the
        # `n == 0 and not prebuffered → continue` path and write nothing.
        s = _make_worker_streamer()
        _run_worker(s, until=lambda: False, timeout=0.1)
        self.assertEqual(len(cast(Any, s.api).writes), 0)
        self.assertEqual(s._full_underruns, 0)

    def test_full_underrun_after_prebuffer(self):
        # Prebuffer exactly, then starve the queue: the worker arms NMI, flips
        # to strict pacing, and pads NEUTRAL chunks as full underruns.
        s = _make_worker_streamer(chunk_size=32)
        # Prebuffer with a NON-neutral value, so a NEUTRAL run in the stream can
        # only have come from an underrun pad and not from the prebuffer itself.
        for _ in range(PREBUFFER_CHUNKS):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        _run_worker(s, until=lambda: s._full_underruns >= 3)
        self.assertGreaterEqual(s._full_underruns, 1)
        stream = _written_stream(s)
        self.assertEqual(stream[: PREBUFFER_CHUNKS * 32], bytes([3] * PREBUFFER_CHUNKS * 32))
        self.assertIn(
            bytes([NEUTRAL_SAMPLE] * 32),
            stream[PREBUFFER_CHUNKS * 32 :],
            "expected a full NEUTRAL chunk after the prebuffer",
        )

    def test_partial_underrun_pads_tail(self):
        # A collect window closing on a sub-chunk blob pads the tail with
        # NEUTRAL and counts a partial, not full, underrun. The collect loop
        # runs `while n < chunk_size`, so whole chunks fill a window exactly
        # and the window after the prebuffer takes the 32 bytes, finds the
        # queue empty, and closes short — the branch under test, reached
        # without depending on a sleep landing inside a 1 ms window.
        s = _make_worker_streamer(chunk_size=64, sample_rate=64000)
        for _ in range(PREBUFFER_CHUNKS):
            s.q.put(bytes([1] * 64))
            s._queued_samples += 64
        half = s.chunk_size // 2
        s.q.put(bytes([2] * half))
        s._queued_samples += half

        # The padded chunk is the half blob then a NEUTRAL tail, asserted
        # against the reassembled stream because it reaches the ring as
        # several sub-NMI-period writes.
        expected = bytes([2] * half) + bytes([NEUTRAL_SAMPLE] * half)
        # Wait on the bytes, not the counter: the counter increments when the
        # short window closes, but the one-chunk pipeline drips that chunk out
        # on the following pass.
        _run_worker(s, until=lambda: expected in _written_stream(s), timeout=3.0)

        self.assertGreaterEqual(
            s._partial_underruns, 1, "expected at least one partial-pad underrun"
        )
        self.assertIn(expected, _written_stream(s), "partial chunk was not NEUTRAL-padded")

    def test_pads_after_end_input_are_not_underruns(self):
        # A file scene lives on after end_input() while the ring plays out.
        # The worker pads NEUTRAL behind the last sample, which is silence
        # after the track, not a producer stall: neither counter moves, for
        # the short tail chunk nor for the full pads after it.
        s = _make_worker_streamer(chunk_size=32)
        for _ in range(PREBUFFER_CHUNKS):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        s.q.put(bytes([4] * 16))
        s._queued_samples += 16
        s.end_input()
        played_out = (PREBUFFER_CHUNKS + 1 + 3) * 32
        _run_worker(s, until=lambda: len(_written_stream(s)) >= played_out, timeout=3.0)
        stream = _written_stream(s)
        self.assertGreaterEqual(len(stream), played_out, "fewer than 3 pad chunks landed")
        self.assertIn(bytes([4] * 16) + bytes([NEUTRAL_SAMPLE] * 16), stream)
        self.assertEqual(s._partial_underruns, 0, "the track's short tail counted as a stall")
        self.assertEqual(s._full_underruns, 0, "the play-out pads counted as a stall")

    def test_a_stall_before_end_input_stays_counted(self):
        s = _make_worker_streamer(chunk_size=32)
        for _ in range(PREBUFFER_CHUNKS):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        mark: list[int] = []

        def stalled_then_ended() -> bool:
            if not mark:
                if s._full_underruns < 1:
                    return False
                s.end_input()
                mark.append(len(_written_stream(s)))
            return len(_written_stream(s)) >= mark[0] + 3 * 32

        _run_worker(s, until=stalled_then_ended, timeout=3.0)
        self.assertTrue(mark, "the starved worker never counted an underrun")
        self.assertGreaterEqual(len(_written_stream(s)), mark[0] + 3 * 32)
        self.assertGreaterEqual(s._full_underruns, 1)

    def test_oversized_blob_carried_via_leftover(self):
        # A single blob bigger than chunk_size must split across writes through
        # the `leftover` carry, preserving byte order.
        s = _make_worker_streamer(chunk_size=16, sample_rate=64000)
        s.q.put(bytes(range(50)))
        s._queued_samples += 50
        _run_worker(s, until=lambda: len(cast(Any, s.api).writes) >= 4)
        body = b"".join(d for _, d in cast(Any, s.api).writes)
        self.assertGreaterEqual(len(body), 50)
        self.assertEqual(body[:50], bytes(range(50)))

    def test_ring_writes_stay_under_one_nmi_period(self):
        # A host DMAWRITE halts the 6510 for about one cycle per byte and CIA
        # #2 is edge-triggered, so a payload longer than one NMI period
        # swallows underflows that then never fire: every steady-state write
        # has to fit the quantum derived from the live latch.
        s = _make_worker_streamer(chunk_size=1024, sample_rate=12000)
        payload = bytes(range(256)) * 8
        for _ in range(PREBUFFER_CHUNKS + 2):
            s.q.put(payload)
            s._queued_samples += len(payload)

        _run_worker(s, until=lambda: len(cast(Any, s.api).writes) >= 40, timeout=3.0)

        quantum = s._halt_quantum()
        self.assertLess(quantum, (s.nmi.latch or s.nmi.compensated_latch()) + 1)
        # Prebuffer writes are deliberately unsplit (no NMI is consuming yet), so
        # only the writes past the prebuffer are held to the quantum.
        steady = cast(Any, s.api).writes[PREBUFFER_CHUNKS:]
        self.assertTrue(steady, "expected steady-state writes past the prebuffer")
        self.assertTrue(
            all(len(data) <= quantum for _, data in steady),
            f"a steady-state write exceeded the {quantum}-byte halt quantum",
        )
        stream = _written_stream(s)
        self.assertEqual(stream[: len(payload)], payload, "split lost or reordered bytes")

    def test_split_writes_are_contiguous_in_the_ring(self):
        # Each piece has to continue from the end of the one before it, or the
        # ring develops holes the NMI reads as stale audio.
        s = _make_worker_streamer(chunk_size=1024, sample_rate=12000)
        for _ in range(PREBUFFER_CHUNKS + 2):
            s.q.put(bytes([5] * 1024))
            s._queued_samples += 1024

        _run_worker(s, until=lambda: len(cast(Any, s.api).writes) >= 30, timeout=3.0)

        expect = None
        for addr_hex, data in cast(Any, s.api).writes:
            addr = int(addr_hex, 16)
            if expect is not None:
                self.assertEqual(addr, expect, "a ring write did not continue from the last")
            expect = addr + len(data)
            if expect >= audio_mod.RING_BUFFER_END:
                expect = audio_mod.RING_BUFFER_ADDR

    def test_halt_quantum_backs_off_to_the_write_rate_budget(self):
        # The quantum sets the write rate and the render thread shares the
        # socket, so asking for more writes than the link sustains starves
        # collection: a backend that advertises a ceiling has to raise the
        # quantum above the halt-derived size.
        s = _make(sample_rate=12000)
        halt_sized = s._halt_quantum()

        cast(Any, s.api).profile = SimpleNamespace(max_write_rate_hz=200.0)
        budgeted = s._halt_quantum()

        self.assertGreater(budgeted, halt_sized, "the budget did not raise the quantum")
        slots = -(-s.chunk_size // budgeted)
        writes_hz = slots / (s.chunk_size / s.effective_rate)
        self.assertLessEqual(writes_hz, 200.0 * audio_mod.AUDIO_WRITE_RATE_SHARE + 1.0)

    def test_unaffordable_link_collapses_to_one_write(self):
        # A backend too slow to carry the split degrades to a single write per
        # chunk on its own — splitting past what the link carries starves
        # collection, so the budget already expresses "cannot afford to split".
        s = _make_worker_streamer(chunk_size=64, sample_rate=64000)
        cast(Any, s.api).profile = SimpleNamespace(max_write_rate_hz=1.0)
        for _ in range(PREBUFFER_CHUNKS + 2):
            s.q.put(bytes([9] * 64))
            s._queued_samples += 64

        _run_worker(s, until=lambda: len(cast(Any, s.api).writes) >= PREBUFFER_CHUNKS + 2)

        self.assertEqual(s._halt_quantum(), s.chunk_size)
        self.assertTrue(
            all(len(data) == 64 for _, data in cast(Any, s.api).writes),
            "an unaffordable link still had its writes subdivided",
        )

    def test_slow_writes_are_counted_as_late_slots(self):
        # A sub-write that runs past its own slot leaves every later slot
        # already expired, so they fire back-to-back and the spread collapses
        # into the burst it was split to avoid. The bytes still land, in
        # order, with no underrun counted: this counter is the only signal.
        s = _make_worker_streamer(chunk_size=256, sample_rate=64000)
        real_write = cast(Any, s.api).write_memory_file

        def slow_write(addr: str, data: bytes) -> None:
            time.sleep(0.004)  # longer than a slot at this chunk period
            real_write(addr, data)

        cast(Any, s.api).write_memory_file = slow_write
        for _ in range(PREBUFFER_CHUNKS + 4):
            s.q.put(bytes([3] * 256))
            s._queued_samples += 256

        _run_worker(s, until=lambda: s._total_slots >= 8, timeout=3.0)

        self.assertGreater(s._total_slots, 0)
        self.assertGreater(s._late_slots, 0, "slow writes did not register as late slots")
        self.assertGreater(s._late_worst_window_s, 0.0)

    def test_prompt_writes_keep_the_schedule(self):
        # The negative control for the counter above: with writes that return
        # immediately the drip keeps its deadlines. Run on a virtual clock —
        # see SleepDrivenClock for why the host's scheduler cannot answer this.
        s = _make_worker_streamer(chunk_size=256, sample_rate=8000)
        for _ in range(PREBUFFER_CHUNKS + 8):
            s.q.put(bytes([3] * 256))
            s._queued_samples += 256

        clock = SleepDrivenClock()
        with (
            mock.patch.object(audio_mod, "time", clock),
            mock.patch.object(audio_rate_mod, "time", clock),
        ):
            _run_worker(s, until=lambda: s._total_slots >= 16, timeout=3.0)

        # A dead worker also reports zero late slots.
        self.assertGreaterEqual(s._total_slots, 16, "the worker did not finish the run")
        self.assertEqual(s._late_slots, 0, "a prompt write missed its slot on an ideal timeline")

    def test_consumer_rate_is_measured_with_the_adaptive_loop_off(self):
        # The rate estimate was computed inside the adaptive NMI-rate loop,
        # which is off by default, so the one quantity showing content-dependent
        # tick loss read zero in every log. Measuring is not steering.
        s = _make_worker_streamer(chunk_size=256, sample_rate=12000)
        s.nmi_rate_adaptive = False
        s.host_dma_servo = True
        s.nmi.started = True
        latch_before = s.nmi.latch
        base = audio_mod.RING_BUFFER_ADDR

        with mock.patch.object(s, "read_consumer_ptr", side_effect=[base, base + 1200]):
            s.servo.next_pace_increment(base + 4096, 0.1)
            time.sleep(0.05)
            s.servo.next_pace_increment(base + 4096 + 1200, 0.1)

        self.assertGreater(s.servo.r_rate_ema, 0.0, "consumer rate was not measured")
        self.assertGreater(s.servo.r_rate_max, 0.0)
        self.assertEqual(s.nmi.latch, latch_before, "observation must not steer the latch")

    def test_health_line_reports_window_deltas(self):
        # The health line places an onset in time, so it reports the window
        # rather than the session's running total.
        s = _make(sample_rate=12000)
        s.nmi.latch = 84
        s.servo.r_rate_ema = 11900.0
        with mock.patch.object(audio_mod, "AUDIO_HEALTH_LOG_INTERVAL_S", 0.0001):
            s._maybe_log_health(100.0)  # first call only marks the baseline
            s._full_underruns = 5
            s._late_slots = 9
            s._total_slots = 40
            with self.assertLogs(audio_mod.log, level="INFO") as first:
                s._maybe_log_health(101.0)
            s._full_underruns = 7
            with self.assertLogs(audio_mod.log, level="INFO") as second:
                s._maybe_log_health(102.0)

        self.assertIn("late=9/40", first.output[0])
        self.assertIn("under=5/0", first.output[0])
        self.assertIn("under=2/0", second.output[0])
        self.assertIn("late=0/0", second.output[0])

    def test_health_line_suppressed_inside_the_window(self):
        s = _make(sample_rate=12000)
        with mock.patch.object(audio_mod, "AUDIO_HEALTH_LOG_INTERVAL_S", 5.0):
            s._maybe_log_health(100.0)
            with mock.patch.object(audio_mod.log, "info") as info:
                s._maybe_log_health(102.0)
        info.assert_not_called()

    def test_prebuffer_partial_chunk_is_padded_to_the_grid(self):
        """A short collect during the PREBUFFER fill must be NEUTRAL-padded
        too, not written verbatim.

        Writing it raw advanced write_addr by the raw byte count, and from
        there every ring write sat off the chunk grid that RING_BUFFER_SIZE is
        an exact multiple of — so a later chunk straddled RING_BUFFER_END and
        its tail landed outside the ring, unplayed. Both wrap guards check the
        address only *after* the increment, which is why the straddling write
        goes out first. The partial counter stays consumption-phase-only: with
        no NMI reading yet, a slow prebuffer collect is not an underrun.
        """
        s = _make_worker_streamer(chunk_size=64, sample_rate=64000)
        half = s.chunk_size // 2
        s.q.put(bytes([2] * half))
        s._queued_samples += half
        expected = bytes([2] * half) + bytes([NEUTRAL_SAMPLE] * half)
        _run_worker(s, until=lambda: len(_written_stream(s)) >= s.chunk_size)
        stream = _written_stream(s)
        self.assertEqual(stream[: s.chunk_size], expected)
        self.assertEqual(len(stream) % s.chunk_size, 0, "ring writes left the chunk grid")
        self.assertEqual(s._partial_underruns, 0, "prebuffer pads are not underruns")

    def test_flushed_pending_chunk_leaves_no_ring_hole(self):
        """A transport splice that lands while the worker holds a pending chunk
        drops it — and must NEUTRAL-fill the span it was going to occupy.

        write_addr is advanced when the chunk is handed off and the next
        chunk's address is already past that span, so a bare discard left a
        hole nothing would ever write: the NMI replays a lap-old byte range as
        a stale echo, exactly where the splice design promises silence.
        Deterministic here because _maybe_log_health is called inside the
        handoff→next-iteration window the flush has to land in.
        """
        s = _make_worker_streamer(chunk_size=32, sample_rate=64000)
        for _ in range(PREBUFFER_CHUNKS):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        flushed: list[float] = []
        real_health = s._maybe_log_health

        def flush_once(now: float) -> None:
            if not flushed:
                flushed.append(now)
                s.flush()
            real_health(now)

        s._maybe_log_health = flush_once  # type: ignore[method-assign]
        hole_addr = RING_BUFFER_ADDR + PREBUFFER_CHUNKS * 32
        filled = (f"{hole_addr:04X}", bytes([NEUTRAL_SAMPLE] * 32))
        _run_worker(s, until=lambda: filled in cast(Any, s.api).writes, timeout=3.0)
        writes = cast(Any, s.api).writes
        self.assertIn(filled, writes, "the discarded chunk's ring span was never filled")
        # And every write, the fill included, tiles the ring contiguously.
        addr = RING_BUFFER_ADDR
        for key, data in writes:
            self.assertEqual(int(key, 16), addr)
            addr += len(data)
            if addr >= RING_BUFFER_END:
                addr = RING_BUFFER_ADDR

    def test_a_splice_during_a_ring_write_anchors_behind_that_chunk(self):
        """A chunk the worker is writing when the flush lands is in the ring
        ahead of every post-splice sample, so the anchor counts it. Read
        before it was counted as landed, the anchor came out one chunk early
        and the picture ran that far ahead of the sound until the next splice.
        Both the priming write and the paced drip are covered."""
        for flush_at in (1, PREBUFFER_CHUNKS + 1):
            with self.subTest(write=flush_at):
                s, anchor = self._flush_during_chunk(flush_at)
                landed = s._pushed_count - s._queued_samples
                self.assertEqual(round(anchor * s.effective_rate), landed)
                self.assertEqual(landed, 32 * flush_at, "the splice dropped a chunk it wrote")

    def _flush_during_chunk(self, chunk: int) -> tuple[AudioStreamer, float]:
        """Run a worker over a full queue, flushing during the first ring
        write of its ``chunk``-th chunk; return the streamer and the anchor."""
        s = _make_worker_streamer(chunk_size=32, sample_rate=64000)
        for _ in range(PREBUFFER_CHUNKS + 3):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
            s._pushed_count += 32
        api = cast(Any, s.api)
        real_write = api.write_memory_file
        anchors: list[float] = []

        def write_then_flush(addr, data):  # type: ignore[no-untyped-def]
            if len(_written_stream(s)) // 32 + 1 == chunk and not anchors:
                anchors.append(s.flush())
            real_write(addr, data)

        api.write_memory_file = write_then_flush
        _run_worker(s, until=lambda: s._queued_samples == 0, timeout=3.0)
        self.assertEqual(len(anchors), 1)
        return s, anchors[0]

    def test_a_flush_in_the_claim_window_still_counts_the_chunk(self):
        """The epoch check and the in-flight record are one step under
        ``_count_lock``. A flush landing between them would anchor without a
        chunk the worker then writes ahead of every post-splice sample."""
        for claim in (1, PREBUFFER_CHUNKS + 2):
            with self.subTest(claim=claim):
                self._flush_in_claim_window(claim)

    def _flush_in_claim_window(self, claim: int) -> None:
        api = cast(Any, _make_worker_streamer(chunk_size=32).api)
        s = _FlushDuringClaim(api, 64000, "NTSC")
        s.chunk_size = 32
        s.nmi.start = lambda **kw: None  # type: ignore[method-assign]
        s.trigger_claim = claim
        for _ in range(PREBUFFER_CHUNKS + 3):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
            s._pushed_count += 32
        _run_worker(s, until=lambda: s._queued_samples == 0 and bool(s.anchors))
        flusher = s.flusher
        assert flusher is not None
        flusher.join(2.0)
        self.assertFalse(flusher.is_alive())
        self.assertEqual(len(s.anchors), 1)
        landed = s._pushed_count - s._queued_samples
        self.assertEqual(round(s.anchors[0] * s.effective_rate), landed)
        self.assertGreater(landed, 0)

    def test_a_splice_after_a_chunk_lands_does_not_count_it_twice(self):
        """A landing clears the in-flight record. The worker next claims only
        after its collect, so a flush in between anchored a chunk late
        (the picture behind the sound) off a record that had already landed."""
        for chunks in (1, PREBUFFER_CHUNKS + 1):
            with self.subTest(landed_chunks=chunks):
                s, landed, anchor = self._flush_in("_collect_until", after_chunks=chunks)
                self.assertEqual(round(anchor * s.effective_rate), landed)

    def test_a_splice_with_a_chunk_handed_off_does_not_count_it(self):
        """A chunk handed off to the next iteration is claimed with 0 queued
        samples in flight: it is not in the ring until that iteration writes
        it, and a flush before then drops it. Counting it anchored a chunk
        late."""
        s, landed, anchor = self._flush_in("next_pace_increment", after_chunks=PREBUFFER_CHUNKS)
        self.assertEqual(round(anchor * s.effective_rate), landed)

    def _flush_in(self, name: str, *, after_chunks: int) -> tuple[AudioStreamer, int, float]:
        """Run a worker over a full queue, flushing at the first call of the
        worker's ``name`` once ``after_chunks`` chunks have landed; return the
        streamer, the landed count at that moment and the anchor."""
        s = _make_worker_streamer(chunk_size=32, sample_rate=64000)
        for _ in range(PREBUFFER_CHUNKS + 3):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
            s._pushed_count += 32
        flushes: list[tuple[int, float]] = []
        owner = s.servo if name == "next_pace_increment" else s
        real = getattr(owner, name)

        def flush_then_call(*a, **kw):  # type: ignore[no-untyped-def]
            # The worker's own call: a drip's interleaved collect runs before
            # its chunk is counted landed.
            from_worker = sys._getframe(1).f_code.co_name == "_worker"
            if from_worker and not flushes and len(_written_stream(s)) // 32 >= after_chunks:
                landed = s._pushed_count - s._queued_samples
                flushes.append((landed, s.flush()))
            return real(*a, **kw)

        with mock.patch.object(owner, name, flush_then_call):
            _run_worker(s, until=lambda: bool(flushes))
        self.assertEqual(len(flushes), 1, "the worker never reached the flush point")
        self.assertGreaterEqual(flushes[0][0], 32 * after_chunks)
        return s, *flushes[0]

    def test_worker_exits_when_its_generation_is_superseded(self):
        """stop()'s join is bounded, so a worker parked in a ring write can
        outlive it — and the next scene's start_* sets `running` back to True,
        which the orphan's own loop guard read as "keep going". Two workers
        then dripped into one ring with independent cursors. The generation
        captured at start is what makes the orphan leave instead.
        """
        s = _make_worker_streamer()
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            s._worker_generation += 1  # a later start_* claimed the ring
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "superseded worker kept running")
            self.assertTrue(s.running, "the shared flag is not what stopped it")
        finally:
            s.running = False
            t.join(timeout=1.0)

    def test_a_worker_superseded_during_its_pacing_read_acts_on_nothing_after_it(self):
        """The pacing read can park past stop()'s join like a ring write. A
        worker that gets its reading back after the next start_* must leave
        straight away: the stall resync and the health line after the read
        would otherwise act on the next session's ring and counters."""
        s = _make_worker_streamer()
        for _ in range(PREBUFFER_CHUNKS + 2):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        api = cast(Any, s.api)
        real_read = api.read_memory
        landed: list[int] = []

        def superseding_read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            if address == audio_mod.READ_PTR_LO_ADDR and length == 2:
                landed.append(len(api.writes))
                s._worker_generation += 1  # the next scene's _start_worker
                addr = RING_BUFFER_ADDR + 100
                return bytes([addr & 0xFF, addr >> 8])
            return real_read(address, length, timeout)

        api.read_memory = superseding_read
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(len(landed), 1)
        self.assertEqual(len(api.writes), landed[0], "wrote after its pacing read")
        self.assertEqual(s.servo.last_r_reading, -1, "noted the next session's R")
        self.assertEqual(s._health_last_log, 0.0, "opened the next session's health window")

    def _superseded_at_write(
        self, s: AudioStreamer, at: int
    ) -> tuple[list[tuple[int, int]], threading.Thread]:
        """Run the worker until its ``at``-th ring write (0-based), bumping the
        generation inside that write as the next scene's _start_worker would,
        and return ``(queue size, queued samples)`` as they stood then."""
        for _ in range(PREBUFFER_CHUNKS + 4):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        api = cast(Any, s.api)
        real_write = api.write_memory_file
        seen: list[tuple[int, int]] = []

        def superseding_write(addr, data):  # type: ignore[no-untyped-def]
            real_write(addr, data)
            if len(api.writes) == at + 1:
                s._worker_generation += 1  # the next scene's _start_worker
                seen.append((s.q.qsize(), s._queued_samples))

        api.write_memory_file = superseding_write
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        t.join(timeout=2.0)
        return seen, t

    def test_a_worker_superseded_during_its_collect_does_not_write_the_prebuffer(self):
        # The collect is the last step before the unsplit prebuffer write; a
        # worker superseded inside it while running is True again must not
        # land that chunk in the ring the next session is priming.
        s = _make_worker_streamer()
        for _ in range(PREBUFFER_CHUNKS + 2):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        real_collect = s._collect_until

        def superseding_collect(*args, **kwargs):  # type: ignore[no-untyped-def]
            out = real_collect(*args, **kwargs)
            s._worker_generation += 1  # the next scene's _start_worker
            return out

        s._collect_until = superseding_collect  # type: ignore[method-assign]
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(cast(Any, s.api).writes, [], "wrote into the next session's ring")

    def test_a_worker_superseded_during_its_stall_resync_skips_the_health_line(self):
        # A resync superseded in its R read returns None, as an unreadable R
        # does; the health line after it would reset the next session's window.
        s = _make_worker_streamer()
        for _ in range(PREBUFFER_CHUNKS + 2):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        # A pace increment far in the past reads as a stall past the lead.
        s.servo.next_pace_increment = lambda *a: -10.0  # type: ignore[method-assign]
        resyncs: list[float] = []

        def superseded_resync(lag, generation, w_head):  # type: ignore[no-untyped-def]
            resyncs.append(lag)
            s._worker_generation += 1  # the next scene's _start_worker
            return None

        health: list[float] = []
        s._resync_after_stall = superseded_resync  # type: ignore[method-assign]
        s._maybe_log_health = health.append  # type: ignore[method-assign]
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(len(resyncs), 1)
        self.assertEqual(health, [], "touched the next session's health window")

    def test_a_worker_superseded_during_a_drip_write_leaves_the_next_session_alone(self):
        # The steady state splits a chunk into several piece writes, any of
        # which can park past stop()'s join. The rest of the chunk would land
        # in the ring the next session is priming, and the collects between
        # pieces would take that session's audio from its queue.
        s = _make_worker_streamer()
        s._halt_quantum = lambda: 8  # type: ignore[method-assign]  # 4 pieces per chunk
        seen, t = self._superseded_at_write(s, PREBUFFER_CHUNKS)
        try:
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(cast(Any, s.api).writes), PREBUFFER_CHUNKS + 1)
        self.assertEqual((s.q.qsize(), s._queued_samples), seen[0])

    def test_a_worker_superseded_during_a_drip_write_collects_no_more(self):
        # The collect between drip pieces takes from the queue, which by then
        # is the next session's.
        s = _make_worker_streamer()
        s._halt_quantum = lambda: 8  # type: ignore[method-assign]  # 4 pieces per chunk
        real_collect = s._collect_until
        stale_collects: list[int] = []
        start_generation = s._worker_generation

        def spying_collect(*args, **kwargs):  # type: ignore[no-untyped-def]
            if s._worker_generation != start_generation:
                stale_collects.append(s._worker_generation)
            return real_collect(*args, **kwargs)

        s._collect_until = spying_collect  # type: ignore[method-assign]
        seen, t = self._superseded_at_write(s, PREBUFFER_CHUNKS)
        try:
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(stale_collects, [], "collected from the next session's queue")

    def test_a_worker_superseded_during_a_splice_fill_counts_nothing_landed(self):
        # The splice's NEUTRAL fill can park like any ring write; past it, the
        # landed-bytes accounting is the next session's.
        s = _make_worker_streamer()
        for _ in range(PREBUFFER_CHUNKS + 4):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32

        def splicing_pace(*args):  # type: ignore[no-untyped-def]
            s._flush_epoch += 1  # a flush() lands after the hand-off
            return 0.001

        s.servo.next_pace_increment = splicing_pace  # type: ignore[method-assign]
        real_fill = s._neutral_fill_ring
        real_landed = s._note_ring_landed
        landed: list[tuple[int, int]] = []
        landed_at_fill: list[int] = []

        def superseding_fill(addr: int, n: int) -> None:
            real_fill(addr, n)
            s._worker_generation += 1  # the next scene's _start_worker
            landed_at_fill.append(len(landed))

        def spying_landed(generation: int, n: int, pad: int) -> None:
            landed.append((n, pad))
            real_landed(generation, n, pad)

        s._neutral_fill_ring = superseding_fill  # type: ignore[method-assign]
        s._note_ring_landed = spying_landed  # type: ignore[method-assign]
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(len(landed_at_fill), 1, "never reached the splice fill")
        self.assertEqual(len(landed), landed_at_fill[0], "counted the fill as landed")

    def test_a_worker_superseded_during_its_last_prebuffer_write_does_not_arm(self):
        # Arming after a parked prebuffer write would start the NMI on a ring
        # the next session has not primed, and reset its servo and health.
        s = _make_worker_streamer()
        armed: list[bool] = []
        s.nmi.start = lambda **kw: armed.append(True)  # type: ignore[method-assign]
        s._health_last_log = 123.0
        seen, t = self._superseded_at_write(s, PREBUFFER_CHUNKS - 1)
        try:
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(armed, [], "armed the NMI for the next session")
        self.assertEqual(s._health_last_log, 123.0)
        self.assertEqual((s.q.qsize(), s._queued_samples), seen[0])

    def _superseded_in_a_wrapped_pause_stomp(self, *, in_pacing_read: bool) -> list[str]:
        """Run the worker with R near the ring's end, ask for the pause stomp
        inside its last prebuffer write or inside its first pacing read, and
        bump the generation inside the stomp's first write. Return every
        write's address from that one on."""
        s = _make_worker_streamer()
        for _ in range(PREBUFFER_CHUNKS + 4):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        api = cast(Any, s.api)
        r_offset = audio_mod.RING_BUFFER_SIZE - 1000
        r_addr = RING_BUFFER_ADDR + r_offset
        real_read = api.read_memory
        real_write = api.write_memory_file

        def read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            if address == audio_mod.READ_PTR_LO_ADDR and length == 2:
                if in_pacing_read and len(api.writes) == PREBUFFER_CHUNKS:
                    s._stomp_requested = True  # the playlist's pause
                return bytes([r_addr & 0xFF, r_addr >> 8])
            return real_read(address, length, timeout)

        first_span = f"{r_addr + STOMP_GUARD_BYTES:04X}"
        superseded_at: list[int] = []

        def write(addr, data):  # type: ignore[no-untyped-def]
            real_write(addr, data)
            if not in_pacing_read and len(api.writes) == PREBUFFER_CHUNKS:
                s._stomp_requested = True  # the playlist's pause
            if addr == first_span and not superseded_at:
                superseded_at.append(len(api.writes) - 1)
                s._worker_generation += 1  # the next scene's _start_worker

        api.read_memory = read
        api.write_memory_file = write
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(len(superseded_at), 1, "the pause stomp never ran")
        return [key for key, _ in api.writes[superseded_at[0] :]]

    def test_a_worker_superseded_in_its_priming_pause_stomp_skips_the_wrapped_write(self):
        # Asked during the last prebuffer write, the stomp runs on the next
        # iteration's own check, against the chunk in hand.
        writes = self._superseded_in_a_wrapped_pause_stomp(in_pacing_read=False)
        self.assertEqual(len(writes), 1, f"wrote into the next session's ring: {writes}")

    def test_a_worker_superseded_in_its_steady_pause_stomp_skips_the_wrapped_write(self):
        # Asked during the pacing read after a hand-off, the stomp runs from
        # the pending path, against the chunk about to go out.
        writes = self._superseded_in_a_wrapped_pause_stomp(in_pacing_read=True)
        self.assertEqual(len(writes), 1, f"wrote into the next session's ring: {writes}")

    def _superseded_as_it_reads_the_stomp_request(self, *, steady: bool) -> AudioStreamer:
        """Run the worker, set the pause request where one call site will see
        it next (``steady``: the pending path's; else the chunk-in-hand path's,
        on the first iteration after the arm), and supersede the worker at the
        moment that site reads the request. Return the streamer afterwards."""
        s = _make_worker_streamer()
        s.__class__ = _SupersedeOnStompRead
        for _ in range(PREBUFFER_CHUNKS + 4):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32

        def ask() -> None:
            if not s.__dict__.get("_asked"):
                s.__dict__["_asked"] = True
                s._stomp_requested = True  # the next session's pause

        if steady:

            def pace(*args):  # type: ignore[no-untyped-def]
                ask()  # after the hand-off, so the pending path sees it
                return 0.001

            s.servo.next_pace_increment = pace  # type: ignore[method-assign]
        else:
            s.nmi.start = lambda **kw: ask()  # type: ignore[method-assign]
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertTrue(s.__dict__.get("_tripped"), "no call site read the request")
        return s

    def test_a_superseded_worker_leaves_the_pending_paths_stomp_request(self):
        s = self._superseded_as_it_reads_the_stomp_request(steady=True)
        self.assertTrue(s._stomp_requested, "took the next session's pause request")

    def test_a_superseded_worker_leaves_the_chunk_in_hand_paths_stomp_request(self):
        s = self._superseded_as_it_reads_the_stomp_request(steady=False)
        self.assertTrue(s._stomp_requested, "took the next session's pause request")

    def test_a_worker_superseded_during_the_arm_leaves_the_next_session_alone(self):
        # The arm reads R and writes the CIA, so it can park as well; past it,
        # the servo reset and the health window are the next session's.
        s = _make_worker_streamer()
        for _ in range(PREBUFFER_CHUNKS + 2):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32

        def superseding_start(**kw):  # type: ignore[no-untyped-def]
            s._worker_generation += 1  # the next scene's _start_worker

        s.nmi.start = superseding_start  # type: ignore[method-assign]
        s._health_last_log = 123.0
        resets: list[int] = []
        s.servo.reset_for_consumer_start = resets.append  # type: ignore[method-assign]
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "superseded worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(resets, [], "reset the next session's servo")
        self.assertEqual(s._health_last_log, 123.0)

    def test_worker_crash_sets_not_running(self):
        # An exception in the DMA write must be caught, logged, and flip
        # running False so the main loop can detect the dead worker.
        s = _make_worker_streamer(chunk_size=8)
        s.q.put(bytes([7] * 8))
        s._queued_samples += 8

        def boom(addr: str, data: bytes) -> None:
            raise RuntimeError("dma exploded")

        cast(Any, s).api.write_memory_file = boom
        with self.assertLogs("c64cast.audio.audio", level="ERROR") as cm:
            s.running = True
            t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
            t.start()
            t.join(timeout=1.0)
        self.assertFalse(s.running)
        self.assertTrue(any("audio worker crashed" in m for m in cm.output))

    def test_a_superseded_worker_crashing_leaves_the_next_session_running(self):
        # A write parked on a stalled link usually raises when it gives up;
        # by then the next start_* owns `running`, and clearing it would
        # fence that session's worker out.
        s = _make_worker_streamer(chunk_size=8)
        s.q.put(bytes([7] * 8))
        s._queued_samples += 8

        def superseded_then_boom(addr: str, data: bytes) -> None:
            s._worker_generation += 1  # the next scene's _start_worker
            raise RuntimeError("dma exploded")

        cast(Any, s).api.write_memory_file = superseded_then_boom
        with self.assertLogs("c64cast.audio.audio", level="ERROR") as cm:
            s.running = True
            t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
            t.start()
            t.join(timeout=1.0)
        try:
            self.assertFalse(t.is_alive())
            self.assertTrue(s.running, "an orphan's crash stopped the next session")
        finally:
            s.running = False
        self.assertTrue(any("audio worker crashed" in m for m in cm.output))


class PitchCompensationLatchTest(unittest.TestCase):
    """set_nmi_latch_for_mode converts a playback-rate multiplier into a CIA #2
    Timer A latch. The relationship is *inverse* (NMI period = latch+1), so a
    >1.0 (faster) multiplier MUST shrink the latch — these tests pin that
    direction so the historic latch×multiplier inversion can't return."""

    def _started(self, **kw: Any) -> AudioStreamer:
        # host_dma_servo defaults on; fake a running worker + a started timer
        # at the nominal latch so the guard passes and a change writes through.
        s = _make(**kw)
        s._worker_thread = cast(Any, object())  # truthy → guard passes
        s.nmi.started = True  # timer already armed
        s.nmi.latch = s.nmi.nominal_latch()  # at nominal
        return s

    def _latch_write(self, s: AudioStreamer) -> int | None:
        """The value last written to CIA #2 Timer A LO/HI, or None."""
        regs = cast(Any, s.api).regs
        key = f"{CIA2.TIMER_A_LO:04X}"
        if key not in regs:
            return None
        lo, hi = regs[key]
        return lo | (hi << 8)

    def test_speedup_multiplier_shrinks_latch(self):
        s = self._started()
        nominal = s.nmi.nominal_latch()  # NTSC@8kHz → 127 (period 128)
        s.set_nmi_latch_for_mode("mhires", {"mhires": 1.1575})
        # period = round(128 / 1.1575) = 111 → latch 110, strictly below nominal.
        self.assertEqual(s.nmi.latch, 110)
        self.assertLess(s.nmi.latch, nominal)  # faster rate ⇒ smaller latch
        self.assertEqual(self._latch_write(s), 110)

    def test_no_multiplier_arms_past_the_handler_budget(self):
        # 1.2 at 12 kHz NTSC asked for latch 70 (a 71-cycle period), under the
        # 75-cycle safe minimum the adaptive loop is already held to.
        s = self._started(sample_rate=12000)
        with self.assertLogs("c64cast.audio.audio", level="WARNING") as cm:
            s.set_nmi_latch_for_mode("mhires", {"mhires": 1.2})
        self.assertIn("latch 70", cm.output[0])
        self.assertEqual(s.nmi.latch, s.nmi.ceiling_latch())
        self.assertEqual(self._latch_write(s), s.nmi.ceiling_latch())

    def test_a_vanishing_multiplier_arms_the_slowest_latch(self):
        # 1e-320 is positive, so it loads, but (nominal+1)/1e-320 overflows to
        # inf and round() raised OverflowError on the playlist thread.
        s = self._started(sample_rate=12000)
        with self.assertLogs("c64cast.audio.audio", level="WARNING"):
            s.set_nmi_latch_for_mode("mhires", {"mhires": 1e-320})
        self.assertEqual(s.nmi.latch, 0xFFFF)
        self.assertEqual(self._latch_write(s), 0xFFFF)

    def test_a_nonpositive_multiplier_is_refused(self):
        # -1.0 used to arm latch 1 (an NMI every 2 cycles); 0.0 divided by zero.
        for mult in (0.0, -1.0):
            with self.subTest(mult=mult):
                s = self._started()
                with self.assertRaises(ValueError):
                    s.set_nmi_latch_for_mode("mhires", {"mhires": mult})

    def test_slowdown_multiplier_grows_latch(self):
        s = self._started()
        nominal = s.nmi.nominal_latch()
        s.set_nmi_latch_for_mode("petscii", {"petscii": 0.8})
        # period = round(128 / 0.8) = 160 → latch 159, above nominal.
        self.assertEqual(s.nmi.latch, 159)
        self.assertGreater(s.nmi.latch, nominal)

    def test_unity_multiplier_no_write(self):
        s = self._started()
        s.set_nmi_latch_for_mode("blank", {"blank": 1.0})
        self.assertEqual(s.nmi.latch, s.nmi.nominal_latch())
        self.assertIsNone(self._latch_write(s))  # unchanged ⇒ no bus traffic

    def test_unknown_mode_defaults_to_unity(self):
        s = self._started()
        s.set_nmi_latch_for_mode("hires_edges", {"hires": 1.1})  # no exact key
        self.assertEqual(s.nmi.latch, s.nmi.nominal_latch())  # 1.0 fallback
        self.assertIsNone(self._latch_write(s))

    def test_no_op_without_servo(self):
        s = self._started(host_dma_servo=False)
        s.set_nmi_latch_for_mode("mhires", {"mhires": 1.1575})
        self.assertIsNone(self._latch_write(s))

    def test_no_op_without_worker(self):
        s = self._started()
        s._worker_thread = None
        s.set_nmi_latch_for_mode("mhires", {"mhires": 1.1575})
        self.assertIsNone(self._latch_write(s))

    def test_multiplier_is_sticky_until_timer_starts(self):
        # set_nmi_latch_for_mode runs at scene setup BEFORE the worker
        # prebuffers and arms the timer, so it stashes the multiplier and
        # _start_nmi_timer applies it; otherwise the timer start would clobber
        # the compensation back to nominal.
        s = _make()
        s._worker_thread = cast(Any, object())
        self.assertFalse(s.nmi.started)
        s.set_nmi_latch_for_mode("mhires", {"mhires": 1.1575})
        self.assertIsNone(self._latch_write(s))  # deferred, not written
        self.assertAlmostEqual(s.nmi.pitch_multiplier, 1.1575)

        s.nmi.start(adaptive=s.nmi_rate_adaptive)  # worker arms the timer
        self.assertTrue(s.nmi.started)
        self.assertEqual(s.nmi.latch, 110)  # compensation applied
        self.assertEqual(self._latch_write(s), 110)

    def test_stop_clears_pitch_state(self):
        s = self._started()
        s.set_nmi_latch_for_mode("mhires", {"mhires": 1.1575})
        s.running = True
        s._worker_thread = None  # no real thread to join in this unit test
        s.stop()
        self.assertFalse(s.nmi.started)
        self.assertAlmostEqual(s.nmi.pitch_multiplier, 1.0)


class _RFakeAPI(FakeAPI):
    """FakeAPI serving the NMI read pointer R from a scripted sequence.

    Each read of ``READ_PTR_LO_ADDR`` pops the next ring *offset* from
    ``r_offsets``; the last entry repeats once the list runs out. That lets a
    test say "R frozen for the first two verify windows, then moving" — the
    machine-specific dropped-CIA-write case this whole path exists for.
    """

    def __init__(self, r_offsets: list[int]) -> None:
        super().__init__()
        self.r_offsets = r_offsets
        self.r_reads = 0

    def read_memory(self, address, length, timeout=1.0):  # type: ignore[no-untyped-def]
        if address == audio_mod.READ_PTR_LO_ADDR and length == 2:
            offset = self.r_offsets[min(self.r_reads, len(self.r_offsets) - 1)]
            self.r_reads += 1
            addr = audio_mod.RING_BUFFER_ADDR + offset
            return bytes([addr & 0xFF, (addr >> 8) & 0xFF])
        return super().read_memory(address, length, timeout)


class NmiArmVerifyTest(unittest.TestCase):
    """_start_nmi_timer verifies the arm actually took by watching R move.

    The two CIA #2 writes and the `$0318` vector ride a transport whose `_emit`
    absorbs a failed write, so a dropped one used to leave R frozen and the whole
    session silent (and fast, the servo chasing a dead reader) with nothing said.
    """

    def setUp(self) -> None:
        # The real 30 ms verify window would make five attempts a 150 ms test.
        patcher = mock.patch.object(audio_rate_mod, "NMI_ARM_VERIFY_DELAY_S", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _streamer(self, api: Any) -> AudioStreamer:
        return AudioStreamer(cast(Ultimate64API, api), 8000, "NTSC")

    def _arm_count(self, api: Any) -> int:
        """How many times the ICR enable+start pair was written (= arms)."""
        key = f"{CIA2.ICR:04X}"
        armed = (audio_rate_mod.CIA2_ICR_ENABLE_TIMER_A_NMI, audio_rate_mod.CIA2_TIMER_A_CONTINUOUS)
        return sum(1 for op in api.ops if op[0] == "write_regs" and op[1] == key and op[2] == armed)

    def test_moving_r_arms_once(self):
        api = _RFakeAPI([0, 240])  # R advanced within the verify window
        s = self._streamer(api)
        with self.assertNoLogs(audio_rate_mod.log, level="WARNING"):
            s.nmi.start(adaptive=s.nmi_rate_adaptive)
        self.assertEqual(self._arm_count(api), 1)
        self.assertEqual(s.nmi.arm_attempts, 1)

    def test_frozen_then_moving_retries(self):
        api = _RFakeAPI([0, 0, 0, 240])  # two dropped arms, then it takes
        s = self._streamer(api)
        with self.assertLogs(audio_rate_mod.log, level="WARNING") as cm:
            s.nmi.start(adaptive=s.nmi_rate_adaptive)
        self.assertEqual(self._arm_count(api), 3)
        self.assertEqual(s.nmi.arm_attempts, 3)
        self.assertEqual(len(cm.records), 1)
        self.assertIn("3 attempts", cm.output[0])

    def test_frozen_throughout_gives_up_loudly(self):
        api = _RFakeAPI([0])  # R never moves, whatever we write
        s = self._streamer(api)
        with self.assertLogs(audio_rate_mod.log, level="WARNING") as cm:
            s.nmi.start(adaptive=s.nmi_rate_adaptive)
        self.assertEqual(self._arm_count(api), audio_rate_mod.NMI_ARM_MAX_ATTEMPTS)
        self.assertEqual(s.nmi.arm_attempts, audio_rate_mod.NMI_ARM_MAX_ATTEMPTS)
        self.assertEqual(len(cm.records), 1)
        self.assertIn("never started", cm.output[0])

    def test_arm_relands_the_nmi_vector(self):
        # A dropped $0318 write leaves the KERNAL handler installed, and its
        # #$7F → $DD0D kills CIA #2 interrupts — same frozen R. So the retry has
        # to re-land the vector, not just the CIA registers.
        api = _RFakeAPI([0, 0, 240])
        s = self._streamer(api)
        with self.assertLogs(audio_rate_mod.log, level="WARNING"):
            s.nmi.start(adaptive=s.nmi_rate_adaptive)
        key = f"{audio_mod.VECTORS.NMI:04X}"
        vector_writes = [op for op in api.ops if op[0] == "write_regs" and op[1] == key]
        self.assertEqual(len(vector_writes), 2)  # one per arm
        expected = (audio_mod.NMI_ROUTINE_ADDR & 0xFF, audio_mod.NMI_ROUTINE_ADDR >> 8)
        self.assertEqual(vector_writes[-1][2], expected)

    def test_unreadable_backend_arms_once_without_waiting(self):
        # A backend that can't read R (TR on older firmware) can't be verified.
        # It must keep the old behavior exactly — one arm, no retry latency.
        api = FakeAPI()  # read_memory → None for the read pointer
        s = self._streamer(api)
        # The sleep under test is audio_rate.NmiTimer.start's, not one of
        # audio.py's: scoping the patch to audio_mod makes the assertion
        # below vacuous.
        with mock.patch.object(audio_rate_mod, "time", FakeTime(sleep=mock.MagicMock())) as faked:
            with self.assertNoLogs(audio_rate_mod.log, level="WARNING"):
                s.nmi.start(adaptive=s.nmi_rate_adaptive)
        self.assertEqual(self._arm_count(api), 1)
        self.assertEqual(s.nmi.arm_attempts, 1)
        faked.sleep.assert_not_called()

    def test_stop_clears_arm_state(self):
        api = _RFakeAPI([0, 240])
        s = self._streamer(api)
        s.nmi.start(adaptive=s.nmi_rate_adaptive)
        s.running = True
        s._worker_thread = None
        s.stop()
        self.assertEqual(s.nmi.arm_attempts, 0)


class NmiStallWatchdogTest(unittest.TestCase):
    """The servo already read R every chunk and would see it stall instantly;
    it just never said so. A consumer killed mid-session now warns once."""

    def _servo_streamer(self, r_offset: int) -> tuple[AudioStreamer, Any]:
        api = _RFakeAPI([r_offset])  # R pinned — a dead consumer
        s = AudioStreamer(cast(Ultimate64API, api), 8000, "NTSC", host_dma_servo=True)
        return s, api

    def test_frozen_r_warns_once_per_session(self):
        s, _ = self._servo_streamer(0)
        write_addr = audio_mod.RING_BUFFER_ADDR + 4096
        with self.assertLogs(audio_rate_mod.log, level="WARNING") as cm:
            for _ in range(audio_rate_mod.NMI_STALL_WARN_CHUNKS + 8):
                s.servo.next_pace_increment(write_addr, 0.064)
        self.assertEqual(len(cm.records), 1)  # once, not once per chunk
        self.assertIn("stalled", cm.output[0])

    def test_moving_r_never_warns(self):
        api = _RFakeAPI(list(range(0, 4000, 240)))  # R advancing normally
        s = AudioStreamer(cast(Ultimate64API, api), 8000, "NTSC", host_dma_servo=True)
        write_addr = audio_mod.RING_BUFFER_ADDR + 4096
        with self.assertNoLogs(audio_rate_mod.log, level="WARNING"):
            for _ in range(audio_rate_mod.NMI_STALL_WARN_CHUNKS + 8):
                s.servo.next_pace_increment(write_addr, 0.064)

    def test_stop_rearms_the_warning(self):
        s, _ = self._servo_streamer(0)
        write_addr = audio_mod.RING_BUFFER_ADDR + 4096
        with self.assertLogs(audio_rate_mod.log, level="WARNING"):
            for _ in range(audio_rate_mod.NMI_STALL_WARN_CHUNKS + 1):
                s.servo.next_pace_increment(write_addr, 0.064)
        s.running = True
        s._worker_thread = None
        s.stop()
        self.assertFalse(s.servo.stall_warned)
        self.assertEqual(s.servo.r_stall_chunks, 0)


class SlowReadPointerTest(unittest.TestCase):
    """The R read sits inside the paced loop. One slower than the time the
    loop can spare is not paced by, and stops the servo reading for a backoff
    instead of being paid for again every chunk."""

    CHUNK_PERIOD = 0.085

    def _streamer(self, read_s: float) -> tuple[AudioStreamer, SleepDrivenClock, mock.MagicMock]:
        clock = SleepDrivenClock()
        s = AudioStreamer(cast(Ultimate64API, FakeAPI()), 12000, "NTSC", host_dma_servo=True)
        self.read_s = read_s

        def read() -> int:
            clock.sleep(self.read_s)
            return audio_mod.RING_BUFFER_ADDR

        reader = mock.MagicMock(side_effect=read)
        patcher = mock.patch.object(audio_rate_mod, "time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        reader_patch = mock.patch.object(s, "read_consumer_ptr", reader)
        reader_patch.start()
        self.addCleanup(reader_patch.stop)
        return s, clock, reader

    def test_slow_read_is_not_paced_by_and_holds_off(self):
        s, clock, reader = self._streamer(read_s=0.1)
        write_addr = audio_mod.RING_BUFFER_ADDR + 1000  # far off target: servo would act
        with self.assertLogs(audio_rate_mod.log, level="WARNING") as cm:
            period = s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        self.assertEqual(period, self.CHUNK_PERIOD)
        self.assertEqual(s.servo.gap_last, -1, "a late R reached the gap servo")
        self.assertIn("holding the pace correction", cm.output[0])
        # Inside the holdoff the servo does not read at all.
        clock.sleep(0.5)
        s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        self.assertEqual(reader.call_count, 1)

    def test_holdoff_doubles_while_reads_stay_slow_and_resets_on_a_prompt_one(self):
        s, clock, _ = self._streamer(read_s=0.1)
        write_addr = audio_mod.RING_BUFFER_ADDR + 4096
        holdoffs = []
        with self.assertLogs(audio_rate_mod.log, level="DEBUG"):
            for _ in range(5):
                s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
                holdoffs.append(s.servo.read_holdoff_s)
                clock.sleep(s.servo.read_holdoff_s)
        self.assertEqual(holdoffs, [1.0, 2.0, 4.0, 8.0, 8.0])
        self.assertEqual(s.servo.slow_reads, 5)
        self.read_s = 0.01
        s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        self.read_s = 0.1
        with self.assertLogs(audio_rate_mod.log, level="DEBUG"):
            s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        self.assertEqual(s.servo.read_holdoff_s, 1.0, "a prompt read did not reset the backoff")

    def test_slow_read_holds_the_standing_correction(self):
        # The integral term is the bus-halt drift the servo has learned; a
        # bare chunk_period would drop it and let W walk off R again.
        s, clock, _ = self._streamer(read_s=0.1)
        s.servo.integ = 20000.0
        held = audio_rate_mod.servo_hold_period(20000.0, chunk_period=self.CHUNK_PERIOD)
        self.assertGreater(held, self.CHUNK_PERIOD)
        with self.assertLogs(audio_rate_mod.log, level="WARNING"):
            self.assertEqual(s.servo.next_pace_increment(0x4000, self.CHUNK_PERIOD), held)
        clock.sleep(0.2)
        self.assertEqual(s.servo.next_pace_increment(0x4000, self.CHUNK_PERIOD), held)

    def test_prompt_read_paces(self):
        s, _, _ = self._streamer(read_s=0.01)
        write_addr = audio_mod.RING_BUFFER_ADDR + 1000
        with self.assertNoLogs(audio_rate_mod.log, level="WARNING"):
            period = s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        self.assertNotEqual(period, self.CHUNK_PERIOD)
        self.assertEqual(s.servo.gap_last, 1000)

    def test_a_new_consumer_start_drops_the_holdoff(self):
        # The next scene's consumer must not inherit the last one's backoff.
        s, _, reader = self._streamer(read_s=0.1)
        write_addr = audio_mod.RING_BUFFER_ADDR + 4096
        with self.assertLogs(audio_rate_mod.log, level="WARNING"):
            s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        s.servo.reset_for_consumer_start(2048)
        # Still slow, so the backoff it arms shows what it doubled from: a
        # prompt read would zero read_holdoff_s itself and hide a stale one.
        with self.assertLogs(audio_rate_mod.log, level="DEBUG"):
            s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        self.assertEqual(reader.call_count, 2, "the old holdoff skipped the new consumer's read")
        self.assertEqual(
            s.servo.read_holdoff_s, 1.0, "the new consumer's backoff doubled the old one"
        )

    def test_the_slow_read_warning_rearms_after_stop(self):
        s, _, _ = self._streamer(read_s=0.1)
        write_addr = audio_mod.RING_BUFFER_ADDR + 4096
        with self.assertLogs(audio_rate_mod.log, level="WARNING"):
            s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        s.servo.reset_after_stop()
        s.servo.reset_for_consumer_start(2048)
        with self.assertLogs(audio_rate_mod.log, level="WARNING") as cm:
            s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
        self.assertIn("1 slow so far", cm.output[0])

    def test_a_pacing_read_returning_to_the_next_session_leaves_its_servo_alone(self):
        # A worker parked in this read can outlive stop() and the next
        # start_*. Its reading, slow or prompt, then belongs to nobody: it
        # must not warn into the next scene, spend that scene's warning, arm
        # or end its backoff, or reach the gap servo and the watchdog.
        for read_s in (0.1, 0.01):
            with self.subTest(read_s=read_s):
                s, _, _ = self._streamer(read_s)
                s.servo.read_holdoff_s = 4.0
                s.servo.integ = 20000.0
                held = audio_rate_mod.servo_hold_period(20000.0, chunk_period=self.CHUNK_PERIOD)
                with self.assertNoLogs("c64cast.audio", level="DEBUG"):
                    period = s.servo.next_pace_increment(
                        audio_mod.RING_BUFFER_ADDR + 1000, self.CHUNK_PERIOD, lambda: False
                    )
                self.assertEqual(period, held)
                servo = s.servo
                self.assertEqual(
                    (servo.slow_reads, servo.slow_read_warned, servo.read_holdoff_s),
                    (0, False, 4.0),
                )
                self.assertEqual((servo.gap_last, servo.last_r_reading), (-1, -1))
                self.assertEqual(servo.integ, 20000.0)

    def test_a_dead_consumer_behind_a_slow_server_still_warns(self):
        # Every reading is slow, so none is paced by, but R frozen across
        # them is still a stalled consumer the watchdog has to report.
        s, clock, _ = self._streamer(read_s=0.1)
        write_addr = audio_mod.RING_BUFFER_ADDR + 4096
        read_at = []
        with self.assertLogs(audio_rate_mod.log, level="DEBUG") as cm:
            for _ in range(audio_rate_mod.NMI_STALL_WARN_CHUNKS + 1):
                s.servo.next_pace_increment(write_addr, self.CHUNK_PERIOD)
                read_at.append(clock.monotonic())
                clock.sleep(s.servo.read_holdoff_s)
        self.assertTrue(s.servo.stall_warned)
        stalled = [line for line in cm.output if "NMI consumer stalled" in line]
        self.assertEqual(len(stalled), 1)
        # The readings were a backoff apart, so the warning gives the time R
        # stood still (tens of seconds here), not a count of chunks.
        self.assertIn(f"for {read_at[-1] - read_at[0]:.1f} s", stalled[0])


class _StallingConsumerAPI(FakeAPI):
    """A FakeAPI with an NMI consumer behind it, on the test's virtual clock.

    R advances at the streamer's effective rate from the moment the consumer
    starts, and every ring byte remembers whether it has been played since it
    was written, so a write over one that has not is counted. Each DMA write
    costs a few ms of virtual time and one of them, past ``stall_at``, blocks
    for ``stall_s`` (a link that blocked or redialed)."""

    WRITE_S = 0.0052
    READ_S = 0.008

    def __init__(
        self,
        clock: SleepDrivenClock,
        stall_at: float,
        stall_s: float,
        consumer_scale: float = 1.0,
    ) -> None:
        super().__init__()
        # The consumer's rate as a fraction of effective_rate: bus halts make
        # a real one run slow, which an open-loop worker never corrects.
        self.consumer_scale = consumer_scale
        self.profile = dataclasses.replace(self.profile, max_write_rate_hz=200.0)
        self.clock = clock
        self.stall_at = stall_at
        self.stall_s = stall_s
        self.streamer: AudioStreamer | None = None
        self.t0: float | None = None
        self.consumed = 0
        self.unplayed = bytearray(audio_mod.RING_BUFFER_SIZE)
        self.overwritten = 0
        # (when, bytes) for each write that landed on audio not yet played.
        self.overwrites: list[tuple[float, int]] = []
        self.write_times: list[float] = []
        self.stalled_until: float | None = None

    def _consume(self) -> None:
        if self.t0 is None or self.streamer is None:
            return
        rate = self.streamer.effective_rate * self.consumer_scale
        target = int((self.clock.monotonic() - self.t0) * rate)
        while self.consumed < target:
            self.unplayed[self.consumed % audio_mod.RING_BUFFER_SIZE] = 0
            self.consumed += 1

    def _spend(self, seconds: float) -> None:
        self._consume()
        self.clock.sleep(seconds)
        self._consume()

    def write_memory_file(self, addr, data):  # type: ignore[no-untyped-def]
        cost = self.WRITE_S
        due = self.t0 is not None and self.clock.monotonic() - self.t0 >= self.stall_at
        if due and self.stalled_until is None:
            cost = self.stall_s
            self.stalled_until = self.clock.monotonic() + cost
        self._spend(cost)
        base = int(addr, 16) - audio_mod.RING_BUFFER_ADDR
        hit = 0
        for i in range(len(data)):
            a = (base + i) % audio_mod.RING_BUFFER_SIZE
            hit += self.unplayed[a]
            self.unplayed[a] = 1
        self.overwritten += hit
        if hit:
            self.overwrites.append((self.clock.monotonic(), hit))
        self.write_times.append(self.clock.monotonic())
        super().write_memory_file(addr, data)

    def write_regs(self, base, *vals):  # type: ignore[no-untyped-def]
        self._spend(self.WRITE_S)
        super().write_regs(base, *vals)

    def read_memory(self, address, length, timeout=1.0):  # type: ignore[no-untyped-def]
        self._spend(self.READ_S)
        if address == audio_mod.READ_PTR_LO_ADDR and length == 2 and self.t0 is not None:
            r = audio_mod.RING_BUFFER_ADDR + self.consumed % audio_mod.RING_BUFFER_SIZE
            return bytes([r & 0xFF, r >> 8])
        return super().read_memory(address, length, timeout)


class _AlwaysFullQueue:
    """A decoder that is always ahead: every get returns a blob at once."""

    def get(self, timeout=None):  # type: ignore[no-untyped-def]
        return bytes([3] * 512)

    get_nowait = get


class StallResyncTest(unittest.TestCase):
    """A DMA link that blocks for longer than the ring lead leaves the worker
    far behind its absolute schedule. Catching that up sprinted writes at the
    link's limit until W lapped R and overwrote audio not yet played; the
    worker now re-anchors W ahead of R and restarts the schedule instead."""

    RUN_S = 4.0

    def _run(
        self, stall_s: float, *, servo: bool = True, consumer_scale: float = 1.0
    ) -> tuple[AudioStreamer, _StallingConsumerAPI]:
        clock = SleepDrivenClock()
        api = _StallingConsumerAPI(
            clock, stall_at=1.0, stall_s=stall_s, consumer_scale=consumer_scale
        )
        s = AudioStreamer(cast(Ultimate64API, api), 12000, "NTSC")
        s.host_dma_servo = servo
        api.streamer = s
        s.q = cast(Any, _AlwaysFullQueue())

        def start(**_kw: Any) -> None:
            s.nmi.latch = s.nmi.nominal_latch()
            s.nmi.started = True
            api.t0 = clock.monotonic()

        s.nmi.start = start  # type: ignore[method-assign]
        real_write = api.write_memory_file

        def write(addr, data):  # type: ignore[no-untyped-def]
            real_write(addr, data)
            if api.t0 is not None and clock.monotonic() - api.t0 > self.RUN_S:
                s.running = False

        api.write_memory_file = write  # type: ignore[method-assign]
        with (
            mock.patch.object(audio_mod, "time", clock),
            mock.patch.object(audio_rate_mod, "time", clock),
        ):
            s.running = True
            s._worker(s._worker_generation)
        return s, api

    def test_a_long_stall_resyncs_instead_of_lapping(self):
        with self.assertLogs(audio_mod.log, level="WARNING") as cm:
            s, api = self._run(stall_s=1.5)
        self.assertTrue(s.running is False and api.stalled_until is not None)
        self.assertIn("stalled", cm.output[0])
        self.assertIn("Re-anchored", cm.output[0])
        # The splice drops at most the rest of the chunk that was in the air,
        # not seconds of audio the consumer had yet to play.
        self.assertLessEqual(api.overwritten, s.chunk_size)
        # And no sprint: the second after the stall writes at the steady rate,
        # not the link's limit (~94/s steady here; it was ~160/s).
        assert api.stalled_until is not None
        after = [t for t in api.write_times if 0 < t - api.stalled_until <= 1.0]
        self.assertLess(len(after), 110)

    def test_reanchor_is_on_the_chunk_grid_at_least_the_lead_ahead(self):
        size, base = audio_mod.RING_BUFFER_SIZE, audio_mod.RING_BUFFER_ADDR
        lead = audio_mod.HOST_DMA_SERVO_TARGET_GAP
        for r in (0, 1, 1023, 1024, 4095, 4096, 7000, size - 1):
            anchor = audio_mod.stall_reanchor(base + r, 1024)
            self.assertEqual((anchor - base) % 1024, 0, r)
            self.assertTrue(base <= anchor < base + size, r)
            self.assertTrue(lead <= (anchor - base - r) % size < lead + 1024, r)

    def _backlogged(self, api: FakeAPI, *, live: bool) -> AudioStreamer:
        s = AudioStreamer(cast(Ultimate64API, api), 12000, "NTSC")
        if live:
            s.mic_stream = object()
        for _ in range(4):
            s.q.put(bytes([3] * 512))
        s._pushed_count = s._queued_samples = 2048
        # As the worker that calls it sees the streamer: running, and its own
        # generation current.
        s.running = True
        return s

    def test_a_live_backlog_is_dropped_at_the_resync(self):
        s = self._backlogged(_RFakeAPI([100]), live=True)
        with self.assertLogs(audio_mod.log, level="WARNING") as cm:
            anchor = s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
        self.assertIsNotNone(anchor)
        self.assertTrue(s.q.empty())
        self.assertEqual((s._pushed_count, s._queued_samples), (0, 0))
        self.assertIn(f"dropped {2048 / s.effective_rate:.2f} s of live input", cm.output[0])

    def test_a_decoded_backlog_is_kept_at_the_resync(self):
        s = self._backlogged(_RFakeAPI([100]), live=False)
        with self.assertLogs(audio_mod.log, level="WARNING"):
            s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(s.q.qsize(), 4)
        self.assertEqual(s._queued_samples, 2048)

    def test_the_clock_never_steps_back_across_a_resync(self):
        # The re-anchor NEUTRAL-fills a fresh lead. Everything landed before
        # the stall has played, so the clock reaches the landed count, and it
        # holds there while the content behind the pad lands and is played.
        # Writing the lead as a bare tail pad stepped the clock ≈0.31 s
        # forward and then back once content landed behind it.
        s = self._backlogged(_RFakeAPI([100]), live=False)
        generation = s._worker_generation
        chunk = s.chunk_size
        rate = s.effective_rate

        def land(n: int) -> None:
            s._pushed_count += n
            s._note_ring_landed(generation, n, 0)

        for _ in range(8):
            land(chunk)
        s.servo.ring_lead = float(chunk)  # a gap shorter than the new lead
        positions = [s.position_seconds()]
        with self.assertLogs(audio_mod.log, level="WARNING"):
            anchor = s._resync_after_stall(1.5, generation, audio_mod.RING_BUFFER_ADDR)
        self.assertIsNotNone(anchor)
        landed = s._pushed_count - s._queued_samples
        at_resync = s.position_seconds()
        positions.append(at_resync)
        for _ in range(int(s.servo.ring_lead) // chunk + 3):
            land(chunk)
            positions.append(s.position_seconds())
        self.assertEqual(positions, sorted(positions), "the clock stepped back")
        self.assertAlmostEqual(at_resync, landed / rate)
        self.assertGreater(positions[-1], at_resync, "the clock stalled past the pad")

    def test_no_r_read_inside_a_holdoff_armed_by_a_read_too_slow_to_anchor_on(self):
        # The holdoff says reads are slower than the anchor can stand right
        # now; another one would block the worker that long again and come
        # back with an R that moved on.
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        s.servo.read_holdoff_until = audio_rate_mod.time.monotonic() + 60.0
        s.servo.last_slow_read_s = 0.4
        with self.assertLogs(audio_mod.log, level="WARNING") as cm:
            self.assertIsNone(
                s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
            )
        self.assertEqual(api.r_reads, 0)
        self.assertEqual(api.writes, [], "re-anchored without a read of R")
        self.assertIn("could not be re-anchored", cm.output[0])

    def test_a_read_too_slow_to_pace_by_is_still_anchored_on(self):
        # 0.1 s is over the servo's ≈43 ms pacing budget, so its backoff is
        # running, but R moves ≈1.2 KB in that time against a 4 KB lead: the
        # anchor is still ahead of the live R. Falling back to snap-only here
        # left W a near-lap behind R, and the ring lapped later in the scene.
        clock = SleepDrivenClock()
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        s.servo.read_holdoff_until = clock.monotonic() + 60.0
        s.servo.last_slow_read_s = 0.1
        real_read = api.read_memory

        def slow_read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            clock.sleep(0.1)
            return real_read(address, length, timeout)

        api.read_memory = slow_read  # type: ignore[method-assign]
        with (
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs("c64cast.audio", level="WARNING") as cm,
        ):
            anchor = s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(api.r_reads, 1)
        self.assertEqual(
            anchor, audio_mod.stall_reanchor(audio_mod.RING_BUFFER_ADDR + 100, s.chunk_size)
        )
        self.assertTrue(any("Re-anchored" in line for line in cm.output))

    def test_a_prompt_read_inside_a_backoff_ends_it(self):
        # The re-anchor reads through a backoff armed by a read it can still
        # anchor on. When that read comes back prompt, the server is fast
        # again, and the servo must not sit out the rest of a hold of up to
        # 8 s with its pace correction frozen.
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        s.servo.read_holdoff_until = audio_rate_mod.time.monotonic() + 60.0
        s.servo.read_holdoff_s = 8.0
        s.servo.last_slow_read_s = 0.1
        with self.assertLogs("c64cast.audio", level="WARNING"):
            s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(api.r_reads, 1)
        self.assertFalse(s.servo.reads_held_off(), "a prompt read left the servo held off")
        self.assertEqual(s.servo.read_holdoff_s, 0.0)

    def test_a_slow_r_read_is_not_anchored_on(self):
        # R read over budget: by the time it returns the consumer has moved
        # on by up to the read's duration, and at 0.4 s that is more than the
        # lead the anchor would put between them.
        clock = SleepDrivenClock()
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        real_read = api.read_memory

        def slow_read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            clock.sleep(0.4)
            return real_read(address, length, timeout)

        api.read_memory = slow_read  # type: ignore[method-assign]
        with (
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs("c64cast.audio", level="WARNING") as cm,
        ):
            self.assertIsNone(
                s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
            )
            self.assertTrue(s.servo.reads_held_off(), "a slow read did not arm the backoff")
            # The backoff that read armed is one the next stall must not read
            # through: it would block the worker another 0.4 s for nothing.
            self.assertIsNone(
                s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
            )
        self.assertEqual(api.r_reads, 1)
        self.assertEqual(api.writes, [], "re-anchored on a slow read of R")
        self.assertTrue(any("could not be re-anchored" in line for line in cm.output))
        self.assertTrue(any("holding the pace correction" in line for line in cm.output))

    def test_a_slow_r_read_with_the_servo_off_claims_no_pace_correction(self):
        # Servo off, the re-anchor is the only reader of R: the slow-read
        # warning must not say a pace correction is being held.
        clock = SleepDrivenClock()
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        s.host_dma_servo = False
        real_read = api.read_memory

        def slow_read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            clock.sleep(0.4)
            return real_read(address, length, timeout)

        api.read_memory = slow_read  # type: ignore[method-assign]
        with (
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs(audio_rate_mod.log, level="WARNING") as cm,
            self.assertLogs(audio_mod.log, level="WARNING"),
        ):
            self.assertIsNone(
                s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
            )
        self.assertIn("not reading it again", cm.output[0])
        self.assertNotIn("pace correction", cm.output[0])

    def test_with_the_servo_off_a_read_within_the_anchor_budget_is_not_slow(self):
        # Servo off, there is no pacing budget: a 0.1 s read is anchored on, so
        # it must neither warn that it was over a budget nor arm a backoff that
        # claims R is not read again.
        clock = SleepDrivenClock()
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        s.host_dma_servo = False
        real_read = api.read_memory

        def slow_read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            clock.sleep(0.1)
            return real_read(address, length, timeout)

        api.read_memory = slow_read  # type: ignore[method-assign]
        with (
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs("c64cast.audio", level="WARNING") as cm,
        ):
            anchor = s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
            self.assertFalse(s.servo.reads_held_off(), "an anchored-on read armed the backoff")
        self.assertEqual(
            anchor, audio_mod.stall_reanchor(audio_mod.RING_BUFFER_ADDR + 100, s.chunk_size)
        )
        self.assertFalse(any("over the" in line for line in cm.output), cm.output)

    def _superseded_during(self, op: str, r_offset: int = 100) -> tuple[AudioStreamer, _RFakeAPI]:
        """A live backlogged streamer whose worker is replaced by a stop() and
        a restart while it is parked in ``op`` (the R read, or a stomp write),
        as when that call outlives stop()'s bounded join."""
        api = _RFakeAPI([r_offset])
        s = self._backlogged(api, live=True)
        generation = s._worker_generation
        real = getattr(api, op)

        def superseding(*args, **kwargs):  # type: ignore[no-untyped-def]
            out = real(*args, **kwargs)
            s._worker_generation += 1  # the next scene's _start_worker
            return out

        setattr(api, op, superseding)
        s._note_ring_landed(generation, 512, 77)
        s.servo.ring_lead = 1234.0
        self.assertIsNone(s._resync_after_stall(1.5, generation, audio_mod.RING_BUFFER_ADDR))
        return s, api

    def test_a_worker_superseded_during_the_r_read_leaves_the_next_session_alone(self):
        s, api = self._superseded_during("read_memory")
        self.assertEqual(api.writes, [], "stomped the next session's ring")
        self.assertEqual(s.q.qsize(), 4, "drained the next session's mic queue")
        self.assertEqual(
            (s._ring_landed_total, list(s._ring_pads), s.servo.ring_lead),
            (512, [(512, 77)], 1234.0),
        )

    def test_a_worker_superseded_during_the_stomp_leaves_the_clock_state_alone(self):
        s, _ = self._superseded_during("write_memory_file")
        self.assertEqual(
            (s._ring_landed_total, list(s._ring_pads), s.servo.ring_lead),
            (512, [(512, 77)], 1234.0),
        )

    def test_a_worker_superseded_during_a_wrapped_stomp_skips_its_second_write(self):
        # R this far into the ring puts the stomp span across RING_BUFFER_END,
        # so its second write lands at RING_BUFFER_ADDR — where the next
        # session's prebuffer starts.
        r_offset = audio_mod.RING_BUFFER_SIZE - 1000
        s, api = self._superseded_during("write_memory_file", r_offset)
        anchor = audio_mod.stall_reanchor(audio_mod.RING_BUFFER_ADDR + r_offset, s.chunk_size)
        self.assertEqual(
            len(audio_mod.stomp_spans(audio_mod.RING_BUFFER_ADDR + r_offset, anchor)), 2
        )
        self.assertEqual(len(api.writes), 1, "stomped the next session's prebuffer")

    def test_a_worker_superseded_during_a_wrapped_pause_stomp_skips_its_second_write(self):
        # The pause fast mute stomps the same span shape from the same worker,
        # and its first write can park past stop()'s join just the same.
        r_offset = audio_mod.RING_BUFFER_SIZE - 1000
        api = _RFakeAPI([r_offset])
        s = self._backlogged(api, live=False)
        generation = s._worker_generation
        real_write = api.write_memory_file

        def superseding_write(addr, data):  # type: ignore[no-untyped-def]
            real_write(addr, data)
            s._worker_generation += 1  # the next scene's _start_worker

        api.write_memory_file = superseding_write  # type: ignore[method-assign]
        write_addr = audio_mod.RING_BUFFER_ADDR + 2 * s.chunk_size
        r_addr = audio_mod.RING_BUFFER_ADDR + r_offset
        self.assertEqual(len(audio_mod.stomp_spans(r_addr, write_addr)), 2)
        s._stomp_ring(write_addr, lambda: not s._superseded(generation))
        self.assertEqual(len(api.writes), 1, "stomped the next session's prebuffer")

    def test_a_superseded_worker_leaves_the_pause_stomp_request_alone(self):
        # Once a later start_* has run, a pause that set the flag is the next
        # session's: an orphan that took it would read R, write nothing, and
        # leave that pause un-muted.
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        generation = s._worker_generation
        s._worker_generation += 1  # the next scene's _start_worker
        s._stomp_requested = True  # the next scene's pause
        s._stomp_ring(audio_mod.RING_BUFFER_ADDR, lambda: not s._superseded(generation))
        self.assertTrue(s._stomp_requested, "took the next session's stomp request")
        self.assertEqual((api.r_reads, api.writes), (0, []))

    def test_a_current_worker_takes_the_pause_stomp_request(self):
        # The request is cleared only inside _stomp_ring now; a current worker
        # that left it set would stomp the ring on every chunk after a pause.
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        s._stomp_requested = True
        s._stomp_ring(audio_mod.RING_BUFFER_ADDR + 2048, lambda: True)
        self.assertFalse(s._stomp_requested, "left the pause request set")
        self.assertEqual(api.r_reads, 1)

    def _r_read_returning_after_supersession(self, read_s: float) -> AudioStreamer:
        """A resync whose R read takes ``read_s`` and returns after a stop()
        and the next start_* have replaced its worker, with the servo state
        that next session set up."""
        clock = SleepDrivenClock()
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        generation = s._worker_generation
        s.servo.read_holdoff_s = 4.0
        real_read = api.read_memory

        def superseding_read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            clock.sleep(read_s)
            s._worker_generation += 1  # the next scene's _start_worker
            return real_read(address, length, timeout)

        api.read_memory = superseding_read  # type: ignore[method-assign]
        with (
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertNoLogs("c64cast.audio", level="DEBUG"),
        ):
            self.assertIsNone(s._resync_after_stall(1.5, generation, audio_mod.RING_BUFFER_ADDR))
        self.assertEqual(api.r_reads, 1)
        return s

    def test_a_slow_r_read_returning_to_the_next_session_leaves_its_backoff_alone(self):
        # No warning in the next scene, no holdoff it never asked for, and its
        # own first slow read still warns.
        servo = self._r_read_returning_after_supersession(0.4).servo
        self.assertFalse(servo.reads_held_off())
        self.assertEqual(
            (servo.slow_reads, servo.slow_read_warned, servo.last_slow_read_s), (0, False, 0.0)
        )
        self.assertEqual(servo.read_holdoff_s, 4.0)

    def test_a_prompt_r_read_returning_to_the_next_session_leaves_its_backoff_alone(self):
        servo = self._r_read_returning_after_supersession(0.0).servo
        self.assertEqual(servo.read_holdoff_s, 4.0, "reset the next session's backoff")

    def test_a_stopped_worker_does_not_resync(self):
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        s.running = False
        self.assertIsNone(
            s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
        )
        self.assertEqual(api.writes, [])

    def test_an_unreadable_r_still_warns_and_does_not_reanchor(self):
        s = self._backlogged(FakeAPI(), live=False)
        with self.assertLogs(audio_mod.log, level="WARNING") as cm:
            self.assertIsNone(
                s._resync_after_stall(1.5, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
            )
        self.assertIn("could not be re-anchored", cm.output[0])

    def test_a_short_stall_is_caught_up_without_a_resync(self):
        with self.assertNoLogs(audio_mod.log, level="WARNING"):
            _, api = self._run(stall_s=0.2)
        self.assertEqual(api.overwritten, 0)

    def test_a_stall_inside_a_lead_grown_past_the_target_is_ridden_out(self):
        # Servo off, a consumer 15 % slow: the open-loop lead grows from the
        # ≈4 KiB it starts at to ≈5.9 KiB by the stall. 0.42 s of stall is
        # past the 4096 B trigger but R is still ≈1.5 KiB short of W, so a
        # re-anchor would NEUTRAL-fill that unplayed span. The ring is left
        # alone instead. (Later the drift laps the ring, as open loop always does;
        # only the half second after the stall is this test's business.)
        with self.assertNoLogs(audio_mod.log, level="WARNING"):
            _, api = self._run(stall_s=0.42, servo=False, consumer_scale=0.85)
        assert api.stalled_until is not None
        after = sum(n for t, n in api.overwrites if t <= api.stalled_until + 0.5)
        self.assertEqual(after, 0)
        # Topped back up to the target lead, no more and no less: the half
        # second after writes ≈66, against ≈46 for a bare snap (W left
        # ≈1.5 KiB ahead) and ≈84 for catching up the whole stall, which
        # from a lead grown past the target drives W toward a lap.
        catch_up = [t for t in api.write_times if 0 < t - api.stalled_until <= 0.5]
        self.assertTrue(56 <= len(catch_up) <= 76, len(catch_up))

    def test_a_stall_inside_the_lead_refills_all_of_the_gap_to_the_target(self):
        # Same open-loop run: R is ≈1.6 KiB short of W when the stall ends.
        # The refill owes TARGET_GAP - gap, all of it: 0.3 s on, W−R is
        # ≈4.8 KiB (the target plus the slow consumer's drift), where owing
        # half the shortfall left it ≈3.5 KiB, under the target for good.
        # W and R both start at RING_BUFFER_ADDR, so bytes landed less bytes
        # played is W−R after each write.
        leads: list[tuple[float, int]] = []
        written = [0]
        real_write = _StallingConsumerAPI.write_memory_file

        def tracking(api: _StallingConsumerAPI, addr: str, data: bytes) -> None:
            real_write(api, addr, data)
            written[0] += len(data)
            leads.append((api.clock.monotonic(), written[0] - api.consumed))

        with mock.patch.object(_StallingConsumerAPI, "write_memory_file", tracking):
            api, outcomes = self._judged_run(0.42, servo=False, consumer_scale=0.85)
        self.assertEqual(len(outcomes), 1, outcomes)
        self.assertIsInstance(outcomes[0], audio_mod.StallInsideLead)
        assert api.stalled_until is not None
        lead = [g for t, g in leads if t <= api.stalled_until + 0.3][-1]
        target = audio_mod.HOST_DMA_SERVO_TARGET_GAP
        self.assertTrue(target <= lead <= target + 1024, lead)

    def _judged_run(
        self, stall_s: float, *, servo: bool, consumer_scale: float
    ) -> tuple[_StallingConsumerAPI, list[object]]:
        """``_run``, also returning what each stall resync answered."""
        real = AudioStreamer._resync_after_stall
        outcomes: list[object] = []

        def judged(s: AudioStreamer, *args: Any) -> object:
            outcome = real(s, *args)
            outcomes.append(outcome)
            return outcome

        with mock.patch.object(AudioStreamer, "_resync_after_stall", judged), quiet_logging():
            _, api = self._run(stall_s=stall_s, servo=servo, consumer_scale=consumer_scale)
        return api, outcomes

    def test_a_stall_inside_the_lead_is_judged_once_not_every_catch_up_iteration(self):
        # Consumer 20 % slow, W still ahead after the stall. Keeping the old
        # schedule left every catch-up iteration over the trigger, each judged
        # again with a lag that no longer measured what R ate: servo on, a
        # 0.5 s stall was judged six times; servo off, a 0.55 s one five
        # times, the last passing for a lap and re-anchoring with W still
        # ahead (3344 B of unplayed audio NEUTRAL-filled).
        for servo, stall_s in ((True, 0.5), (False, 0.55)):
            with self.subTest(servo=servo, stall_s=stall_s):
                api, outcomes = self._judged_run(stall_s, servo=servo, consumer_scale=0.8)
                self.assertEqual(len(outcomes), 1, outcomes)
                self.assertIsInstance(outcomes[0], audio_mod.StallInsideLead)
                assert api.stalled_until is not None
                after = sum(n for t, n in api.overwrites if t <= api.stalled_until + 0.5)
                self.assertEqual(after, 0)

    def test_a_w_a_few_hundred_bytes_ahead_of_r_is_left_alone(self):
        # Servo on, consumer 20 % slow, 0.55 s stall: W is ≈690 B ahead of R
        # when R is read. A whole-chunk slack counted that as lapped and
        # NEUTRAL-filled the 656 B of it R had not yet played; past a
        # quarter-chunk slack it is inside the lead, and nothing is lost.
        api, outcomes = self._judged_run(0.55, servo=True, consumer_scale=0.8)
        self.assertEqual(len(outcomes), 1, outcomes)
        self.assertIsInstance(outcomes[0], audio_mod.StallInsideLead)
        assert api.stalled_until is not None
        after = sum(n for t, n in api.overwrites if t <= api.stalled_until + 0.5)
        self.assertEqual(after, 0)

    def test_a_stall_that_lapped_is_still_reanchored_with_a_slow_consumer(self):
        with self.assertLogs(audio_mod.log, level="WARNING") as cm:
            _, api = self._run(stall_s=1.5, servo=False, consumer_scale=0.85)
        self.assertIn("Re-anchored", cm.output[0])
        assert api.stalled_until is not None
        after = sum(n for t, n in api.overwrites if t <= api.stalled_until + 0.5)
        self.assertLessEqual(after, 1024)

    def test_a_stall_just_past_the_lead_is_judged_against_the_landed_head(self):
        # Servo off: 0.4 s of stall carries R a few hundred bytes past the
        # ≈4.1 KiB open-loop lead. Judged against write_addr, a chunk past the
        # last byte that landed, R reads as still short of W, the worker
        # catches up instead of re-anchoring, and the catch-up overwrites
        # ≈1.7 KiB of audio not yet played.
        with self.assertLogs(audio_mod.log, level="WARNING") as cm:
            s, api = self._run(stall_s=0.4, servo=False)
        self.assertIn("Re-anchored", cm.output[0])
        assert api.stalled_until is not None
        after = sum(n for t, n in api.overwrites if t <= api.stalled_until + 0.5)
        self.assertLessEqual(after, s.chunk_size // 4)

    def test_the_resync_leaves_a_ring_r_has_not_reached_alone(self):
        # R at offset 100 with W 2000 B ahead of it after 0.4 s (4800 B) of
        # stall: the lead was 6800 B, and W is still ahead.
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        clock = SleepDrivenClock()
        with (
            mock.patch.object(audio_mod, "time", clock),
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs(audio_mod.log, level="DEBUG") as cm,
        ):
            result = s._resync_after_stall(
                0.4, s._worker_generation, audio_mod.RING_BUFFER_ADDR + 2100
            )
        self.assertEqual(result, audio_mod.StallInsideLead(2000))
        self.assertEqual(api.writes, [], "NEUTRAL-filled audio R has not played")
        self.assertEqual(s.q.qsize(), 4, "dropped a decoded backlog")
        self.assertTrue(all(r.levelno < logging.WARNING for r in cm.records), cm.output)
        self.assertFalse(any("live input" in line for line in cm.output), cm.output)
        # The refill burst is a disturbance the adaptive rate loop sits out.
        self.assertGreater(s.servo.warmup_until, clock.monotonic())

    def test_a_live_backlog_is_dropped_when_r_has_not_reached_w_too(self):
        # Same stall inside the lead, from a mic: played late, its backlog
        # would add that much latency for the rest of the session.
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=True)
        clock = SleepDrivenClock()
        with (
            mock.patch.object(audio_mod, "time", clock),
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs(audio_mod.log, level="DEBUG") as cm,
        ):
            result = s._resync_after_stall(
                0.4, s._worker_generation, audio_mod.RING_BUFFER_ADDR + 2100
            )
        self.assertEqual(result, audio_mod.StallInsideLead(2000))
        self.assertEqual(api.writes, [], "NEUTRAL-filled audio R has not played")
        self.assertTrue(s.q.empty())
        self.assertEqual((s._pushed_count, s._queued_samples), (0, 0))
        # The four 512-byte blobs _backlogged queued, in seconds of playback.
        # Lost mic audio is audible, so the line is a WARNING, as on the
        # re-anchor path, and the only record the stall logs.
        dropped = f"dropped {2048 / s.effective_rate:.2f} s of live input"
        self.assertEqual(len(cm.records), 1, cm.output)
        self.assertEqual(cm.records[0].levelno, logging.WARNING, cm.output)
        self.assertIn(dropped, cm.output[0])

    def test_a_second_live_drop_inside_the_lead_is_throttled(self):
        # The drop shares the re-anchor's throttle: a stall that recurs
        # within its interval adds no second WARNING.
        api = _RFakeAPI([100, 100])
        s = self._backlogged(api, live=True)
        clock = SleepDrivenClock()
        with (
            mock.patch.object(audio_mod, "time", clock),
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs(audio_mod.log, level="DEBUG") as cm,
        ):
            for _ in range(2):
                s.q.put(b"\x80" * 512)
                s._queued_samples += 512
                s._pushed_count += 1
                s._resync_after_stall(0.4, s._worker_generation, audio_mod.RING_BUFFER_ADDR + 2100)
        warnings = [r for r in cm.records if r.levelno >= logging.WARNING]
        self.assertEqual(len(warnings), 1, cm.output)

    def test_a_w_ahead_by_less_than_r_moves_while_read_counts_as_lapped(self):
        # 0.1 s of read carries R 1200 B on: a W 1400 B ahead when R was
        # sampled is within the slack of it by the time the answer is used.
        clock = SleepDrivenClock()
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        real_read = api.read_memory

        def slow_read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            clock.sleep(0.1)
            return real_read(address, length, timeout)

        api.read_memory = slow_read  # type: ignore[method-assign]
        with (
            mock.patch.object(audio_mod, "time", clock),
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs("c64cast.audio", level="WARNING") as cm,
        ):
            result = s._resync_after_stall(
                0.4, s._worker_generation, audio_mod.RING_BUFFER_ADDR + 1500
            )
        self.assertEqual(result, audio_mod.stall_reanchor(audio_mod.RING_BUFFER_ADDR + 100, 1024))
        self.assertTrue(any("Re-anchored" in line for line in cm.output))

    def test_the_gap_left_inside_the_lead_is_net_of_r_travel_during_the_read(self):
        # W 3000 B ahead when R was sampled, 0.1 s of read, 0.2 s of stall
        # (an old lead of ≈6.6 KiB): what the worker tops up to the target
        # lead is measured from where R is now.
        clock = SleepDrivenClock()
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        real_read = api.read_memory

        def slow_read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            clock.sleep(0.1)
            return real_read(address, length, timeout)

        api.read_memory = slow_read  # type: ignore[method-assign]
        with (
            mock.patch.object(audio_mod, "time", clock),
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs("c64cast.audio", level="WARNING"),  # the slow read's
        ):
            result = s._resync_after_stall(
                0.2, s._worker_generation, audio_mod.RING_BUFFER_ADDR + 3100
            )
        assert isinstance(result, audio_mod.StallInsideLead)
        self.assertAlmostEqual(result.gap, 3000 - 0.1 * s.effective_rate, delta=2)

    def test_the_resync_reanchors_once_r_has_passed_w(self):
        # Same R, but W 100 B *behind* it: R passed W, and the ring it plays
        # until W gets back ahead is a lap old.
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        with self.assertLogs(audio_mod.log, level="WARNING"):
            result = s._resync_after_stall(0.4, s._worker_generation, audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(result, audio_mod.stall_reanchor(audio_mod.RING_BUFFER_ADDR + 100, 1024))

    def test_stall_lapped_splits_at_a_ring_of_gap_plus_stall(self):
        base, size = audio_mod.RING_BUFFER_ADDR, audio_mod.RING_BUFFER_SIZE
        r = base + 100
        self.assertFalse(audio_mod.stall_lapped(r, r + 1000, 4800, 0))
        self.assertFalse(audio_mod.stall_lapped(r, r + 1000, size - 1001, 0))
        self.assertTrue(audio_mod.stall_lapped(r, r + 1000, size - 1000, 0))
        self.assertTrue(audio_mod.stall_lapped(r, r - 100, 4800, 0))
        # Several laps: any gap, W is long behind.
        self.assertTrue(audio_mod.stall_lapped(r, r + 1000, 3 * size, 0))
        # A gap under the slack R covers while being read counts as lapped.
        self.assertFalse(audio_mod.stall_lapped(r, r + 1000, 4800, 1000))
        self.assertTrue(audio_mod.stall_lapped(r, r + 1000, 4800, 1001))

    def test_a_read_that_outlasts_the_old_lead_is_still_a_lap(self):
        # A live lead of ≈2.2 KiB, then a 0.05 s stall and a 0.25 s read
        # (≈3.6 KiB of consumption): R passed W during the read. Counting
        # only the stall as consumption, the ≈6.8 KiB gap read as W still
        # ahead, by ≈3.8 KiB once the read's travel came off.
        clock = SleepDrivenClock()
        api = _RFakeAPI([100])
        s = self._backlogged(api, live=False)
        real_read = api.read_memory

        def slow_read(address, length, timeout=1.0):  # type: ignore[no-untyped-def]
            clock.sleep(0.25)
            return real_read(address, length, timeout)

        api.read_memory = slow_read  # type: ignore[method-assign]
        w_head = audio_mod.RING_BUFFER_ADDR + (100 + 1024 + 600 - 3000) % audio_mod.RING_BUFFER_SIZE
        with (
            mock.patch.object(audio_mod, "time", clock),
            mock.patch.object(audio_rate_mod, "time", clock),
            self.assertLogs("c64cast.audio", level="WARNING") as cm,
        ):
            result = s._resync_after_stall(0.05, s._worker_generation, w_head)
        self.assertNotIsInstance(result, audio_mod.StallInsideLead)
        self.assertTrue(any("Re-anchored" in line for line in cm.output))


class NmiRateSafetyTest(unittest.TestCase):
    """The NMI sample-rate guard (c64.nmi_rate_safety) + its config wiring.

    The handler completes in <=68 cycles (HW-measured 2026-07-02, badline worst
    case); a sample period shorter than that queues NMIs and drops pitch. PAL's
    slower clock = tighter ceiling than NTSC. See [[project-nmi-rate-intelligibility]]."""

    def test_default_rate_is_safe_both_standards(self):
        from c64cast.app.config import AudioCfg
        from c64cast.hw.c64 import nmi_rate_safety

        self.assertEqual(AudioCfg().sample_rate, 12000)
        for system in ("NTSC", "PAL"):
            self.assertEqual(nmi_rate_safety(system, 12000)[0], "ok")

    def test_legacy_and_candidate_rates_ok(self):
        from c64cast.hw.c64 import nmi_rate_safety

        self.assertEqual(nmi_rate_safety("NTSC", 8000)[0], "ok")
        self.assertEqual(nmi_rate_safety("NTSC", 11025)[0], "ok")  # NTSC headroom
        self.assertEqual(nmi_rate_safety("PAL", 10500)[0], "ok")

    def test_overrun_is_error(self):
        from c64cast.hw.c64 import nmi_rate_safety

        for system in ("NTSC", "PAL"):
            level, msg = nmi_rate_safety(system, 16000)
            self.assertEqual(level, "error")
            self.assertIn("queue", msg.lower())

    def test_a_rate_inside_the_margin_is_refused(self):
        from c64cast.hw.c64 import nmi_rate_safety

        # 14000 → period ~73 (NTSC) / ~70 (PAL): above the 68-cycle handler
        # onset but inside the 75-cycle margin. That used to load with a
        # warning, and the timer then armed the 75-cycle ceiling instead, so
        # the request could only ever play at a rate nobody asked for.
        for system in ("NTSC", "PAL"):
            level, msg = nmi_rate_safety(system, 14000)
            self.assertEqual(level, "error")
            self.assertIn("margin", msg)

    def test_the_safety_rule_matches_the_latch_the_timer_arms(self):
        from c64cast.hw.c64 import max_safe_sample_rate, nmi_rate_safety

        # A rate is accepted exactly when its nearest-grid latch is one the
        # timer arms unclamped, so load and NmiTimer agree at the boundary.
        for system in ("NTSC", "PAL"):
            for rate in range(
                max_safe_sample_rate(system) - 50, max_safe_sample_rate(system) + 150
            ):
                with self.subTest(system=system, rate=rate):
                    s = _make(sample_rate=rate, system=system)
                    unclamped = s.nmi.requested_latch() == s.nmi.nominal_latch()
                    self.assertEqual(nmi_rate_safety(system, rate)[0] == "ok", unclamped)

    def test_a_rate_too_slow_for_the_16_bit_latch_is_refused(self):
        from c64cast.hw.c64 import nmi_rate_safety

        # 15 Hz NTSC wants latch 68181, which the register pair truncated.
        level, msg = nmi_rate_safety("NTSC", 15)
        self.assertEqual(level, "error")
        self.assertIn("16-bit", msg)
        self.assertEqual(nmi_rate_safety("NTSC", 16)[0], "ok")

    def test_the_armed_latch_stays_inside_the_handler_budget_and_16_bits(self):
        # nominal_latch is what effective_rate, the REU pump latch and the
        # adaptive clamp all derive from, so holding it holds all of them.
        # 14000 NTSC wants latch 72 (under the 74 ceiling); 15 Hz wants 68181,
        # which the two-register write used to truncate to 2645 (386 Hz).
        for rate, want in ((14000, 74), (15, 0xFFFF)):
            with self.subTest(rate=rate):
                s = _make(sample_rate=rate)
                self.assertEqual(s.nmi.nominal_latch(), want)
                self.assertAlmostEqual(s.effective_rate, 1022727 / (want + 1), places=6)

    def test_a_clamped_rate_warns_when_armed(self):
        s = _make(sample_rate=14000)
        api = cast(Any, s.api)
        api.read_memory = lambda *a, **k: None  # unverifiable: arm once, no sleep
        with self.assertLogs(audio_rate_mod.log, level="WARNING") as cm:
            s.nmi.start(adaptive=True)
        self.assertIn("latch 72", cm.output[0])
        self.assertEqual(s.nmi.latch, s.nmi.ceiling_latch())

    def test_pal_ceiling_below_ntsc(self):
        from c64cast.hw.c64 import max_safe_sample_rate

        self.assertLess(max_safe_sample_rate("PAL"), max_safe_sample_rate("NTSC"))

    def test_nonpositive_rate_is_error(self):
        from c64cast.hw.c64 import nmi_rate_safety

        self.assertEqual(nmi_rate_safety("NTSC", 0)[0], "error")

    def test_config_validate_raises_on_overrun_when_audio_enabled(self):
        import dataclasses

        from c64cast.app.config import Config, ConfigError
        from c64cast.app.scene_factory import validate_nmi_sample_rate

        cfg = Config()
        cfg = dataclasses.replace(
            cfg, audio=dataclasses.replace(cfg.audio, enabled=True, sample_rate=16000)
        )
        with self.assertRaises(ConfigError):
            validate_nmi_sample_rate(cfg)

    def test_config_validate_noop_when_audio_disabled(self):
        import dataclasses

        from c64cast.app.config import Config
        from c64cast.app.scene_factory import validate_nmi_sample_rate

        cfg = Config()  # audio disabled by default
        cfg = dataclasses.replace(
            cfg, audio=dataclasses.replace(cfg.audio, enabled=False, sample_rate=16000)
        )
        validate_nmi_sample_rate(cfg)  # must not raise

    def test_config_validate_passes_default(self):
        from c64cast.app.config import Config
        from c64cast.app.scene_factory import validate_nmi_sample_rate

        validate_nmi_sample_rate(Config())  # default 12000, no raise


class NmiRateAdaptiveStepTest(unittest.TestCase):
    """The pure adaptive-rate control step (`audio_servo.nmi_rate_step`) + its wiring.

    Drives the measured consumer rate toward target by stepping the CIA #2 latch.
    Rate/latch are inverse, so R too slow → SMALLER latch (faster). NTSC@10500:
    nominal_latch=96 (period 97), ceiling_latch=74 (period 75, the measured
    handler budget). See [[project-nmi-rate-intelligibility]] / [[project-hostdma-servo-pitch-compensation]]."""

    NOMINAL = 96  # _nmi_latch_value() for NTSC @ 10500
    CEILING = 74  # NMI_SAFE_MIN_PERIOD_CYCLES (75) - 1
    TARGET = 10500.0

    def _step(self, r_rate: float, latch: int) -> int:
        return nmi_rate_step(
            r_rate,
            latch,
            nominal_latch=self.NOMINAL,
            ceiling_latch=self.CEILING,
            target_rate=self.TARGET,
        )

    def test_too_slow_shrinks_latch(self):  # the sign pin
        out = self._step(9456.0, self.NOMINAL)  # ~9.9% slow
        self.assertLess(out, self.NOMINAL)  # faster NMI ⇒ smaller latch

    def test_too_fast_grows_latch(self):
        out = self._step(10800.0, 90)  # consumer above target
        self.assertGreater(out, 90)  # slower NMI ⇒ larger latch

    def test_deadband_holds(self):
        # within ~1% of target (< 1.3% deadband) ⇒ no change (no limit cycle)
        self.assertEqual(self._step(10440.0, 92), 92)

    def test_fixed_point_at_target(self):
        self.assertEqual(self._step(self.TARGET, 92), 92)

    def test_fine_zone_single_step(self):
        # 1.9% error (deadband < e < coarse 3%) ⇒ exactly one latch step
        out = self._step(10300.0, 92)
        self.assertEqual(abs(out - 92), 1)

    def test_coarse_zone_bigger_step_but_capped(self):
        out = self._step(9456.0, self.NOMINAL)  # ~9.9% ⇒ capped coarse step (4)
        self.assertEqual(self.NOMINAL - out, 4)

    def test_clamp_at_ceiling(self):
        # huge error near the ceiling must never push past it (overrun guard)
        self.assertEqual(self._step(5000.0, self.CEILING + 1), self.CEILING)

    def test_clamp_at_nominal(self):
        # too-fast at nominal must not exceed nominal (can only slow back to it)
        self.assertEqual(self._step(11500.0, self.NOMINAL), self.NOMINAL)

    def test_nonpositive_rate_no_change(self):
        self.assertEqual(self._step(0.0, 92), 92)
        self.assertEqual(self._step(-1.0, 92), 92)

    def test_adaptive_mode_disables_static_multiplier(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s._worker_thread = cast(Any, object())
        s.nmi.started = True
        s.nmi.latch = s.nmi.nominal_latch()  # nominal
        s.set_nmi_latch_for_mode("mhires", {"mhires": 1.1575})
        # Adaptive ignores the static multiplier and re-seeds the latch to the
        # mode seed (here the ceiling), not the static-multiplier latch (110).
        self.assertEqual(s.nmi.pitch_multiplier, 1.0)
        self.assertEqual(s.nmi.mode, "mhires")
        self.assertEqual(s.nmi.latch, s.nmi.ceiling_latch())

    def test_loop_retunes_latch_when_slow(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.nmi.started = True
        s.nmi.latch = s.nmi.nominal_latch()  # 96
        s.servo.r_rate_ema = 9456.0  # ~9.9% slow, pre-seeded
        s.servo.last_r_addr = -1  # skip the EMA update this call (use the seed)
        decide_every = max(1, round(s.sample_rate / s.chunk_size))
        s.servo.loop_chunk_count = decide_every - 1  # next call triggers a decision
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(s.nmi.latch, 92)  # 96 - capped coarse step 4
        regs = cast(Any, s.api).regs[f"{CIA2.TIMER_A_LO:04X}"]
        self.assertEqual(regs[0] | (regs[1] << 8), 92)

    def test_loop_exits_acquisition_on_settle(self):
        # When a decision needs no change (R at target ⇒ deadband), the fast
        # acquisition phase flips off so steady-state uses the gentle fine loop.
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.nmi.started = True
        s.nmi.latch = s.nmi.nominal_latch()
        s.servo.r_rate_ema = 10500.0  # already at target → step returns no change
        s.servo.last_r_addr = -1
        s.servo.loop_chunk_count = audio_rate_mod.NMI_RATE_LOOP_ACQUIRE_DECIDE_CHUNKS - 1
        self.assertTrue(s.servo.loop_acquiring)
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR)
        self.assertFalse(s.servo.loop_acquiring)  # settled → fine loop

    def test_seed_bitmap_mode_near_ceiling(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        self.assertEqual(s.nmi.seed_latch_for_mode("mhires"), s.nmi.ceiling_latch())
        self.assertEqual(s.nmi.seed_latch_for_mode("hires"), s.nmi.ceiling_latch())

    def test_seed_char_mode_at_nominal(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        self.assertEqual(s.nmi.seed_latch_for_mode("petscii"), s.nmi.nominal_latch())
        self.assertEqual(
            s.nmi.seed_latch_for_mode(None), s.nmi.nominal_latch()
        )  # unknown → nominal

    def test_seed_prefers_learned_value(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.nmi.learned_latch["mhires"] = 90
        self.assertEqual(s.nmi.seed_latch_for_mode("mhires"), 90)  # learned beats the class default

    def test_start_timer_arms_at_mode_seed(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.nmi.mode = "mhires"
        s.nmi.start(adaptive=s.nmi_rate_adaptive)
        self.assertEqual(s.nmi.latch, s.nmi.ceiling_latch())  # no glide-up from nominal

    def test_settle_records_learned_latch(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.nmi.started = True
        s.nmi.mode = "mhires"
        s.nmi.latch = 88
        s.servo.r_rate_ema = 10500.0  # at target → settles without a change
        s.servo.last_r_addr = -1
        s.servo.loop_chunk_count = audio_rate_mod.NMI_RATE_LOOP_ACQUIRE_DECIDE_CHUNKS - 1
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(s.nmi.learned_latch["mhires"], 88)

    def _converge(self, s: AudioStreamer) -> None:
        """One adaptive decision with R at target, so the loop settles."""
        s.servo.warmup_until = 0.0
        s.servo.r_rate_ema = s.nmi.effective_rate
        s.servo.last_r_addr = -1
        s.servo.loop_chunk_count = audio_rate_mod.NMI_RATE_LOOP_ACQUIRE_DECIDE_CHUNKS - 1
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR)

    def test_stop_forgets_the_display_mode(self):
        # A scene with no display_mode never calls set_nmi_latch_for_mode, so a
        # mode that outlived stop() would seed it from mhires's learned latch and
        # then overwrite that cache entry with its own converged latch.
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s._worker_thread = cast(Any, object())
        s.nmi.started = True
        s.nmi.latch = s.nmi.nominal_latch()
        s.set_nmi_latch_for_mode("mhires")
        self._converge(s)
        self.assertEqual(s.nmi.learned_latch, {"mhires": s.nmi.ceiling_latch()})
        s._worker_thread = None
        s.stop()
        # The next scene: no display_mode, adaptive start.
        s.nmi.start(adaptive=s.nmi_rate_adaptive)
        self.assertEqual(s.nmi.latch, s.nmi.nominal_latch())
        s.nmi.latch = 90  # wherever this scene's load settles
        self._converge(s)
        self.assertEqual(s.nmi.learned_latch, {"mhires": s.nmi.ceiling_latch()})

    def test_loop_discards_torn_read(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.servo.last_r_addr = audio_mod.RING_BUFFER_ADDR
        s.servo.last_r_time = time.monotonic() - 0.1
        s.servo.r_rate_ema = -1.0
        # a half-ring forward jump = a torn self-modify read, not real advance
        torn = audio_mod.RING_BUFFER_ADDR + audio_mod.RING_BUFFER_SIZE // 2 + 16
        s.servo.update_rate_loop(torn)
        self.assertEqual(s.servo.r_rate_ema, -1.0)  # estimate left unseeded

    def test_loop_discards_a_reading_across_a_ring_wrap(self):
        # A 1.5 s link stall: R really advanced 1.5 s of samples, a lap and a
        # bit, and the bit is all the modulo shows. Accepted, it reads as a
        # consumer at a seventh of its rate and the loop speeds the NMI up.
        s = _make(sample_rate=12000, nmi_rate_adaptive=True)
        s.nmi.latch = s.nmi.nominal_latch()
        s.servo.last_r_addr = audio_mod.RING_BUFFER_ADDR
        s.servo.last_r_time = time.monotonic() - 1.5
        s.servo.r_rate_ema = -1.0
        advanced = round(1.5 * s.effective_rate) % audio_mod.RING_BUFFER_SIZE
        self.assertLess(advanced, audio_mod.RING_BUFFER_SIZE // 2)  # passes the torn guard
        s.servo.observe_r_rate(audio_mod.RING_BUFFER_ADDR + advanced)
        self.assertEqual(s.servo.r_rate_ema, -1.0)
        self.assertEqual(s.servo.r_rate_min, -1.0)

    def test_wrap_bound_uses_the_fastest_latch_armed_since_the_last_reading(self):
        # A bitmap -> char mode change at 6 kHz reseeds the latch from the
        # ceiling to nominal mid-interval. R ran at the ceiling's rate for the
        # whole 0.65 s, a lap and a bit, but half a ring at nominal takes 0.68 s:
        # judged by the latch armed now, the bit passed as a 1 kHz consumer.
        s = _make(sample_rate=6000, nmi_rate_adaptive=True)
        s.nmi.write_latch(s.nmi.ceiling_latch())
        fast_rate = cpu_clock(s.system) / (s.nmi.ceiling_latch() + 1)
        s.servo.observe_r_rate(audio_mod.RING_BUFFER_ADDR)  # baseline, at the ceiling
        s.servo.last_r_time = time.monotonic() - 0.65
        s.nmi.write_latch(s.nmi.nominal_latch())
        self.assertGreater(s.servo.max_unambiguous_dt(s.nmi.latch), 0.65)  # the old bound
        advanced = round(0.65 * fast_rate) % audio_mod.RING_BUFFER_SIZE
        self.assertLess(advanced, audio_mod.RING_BUFFER_SIZE // 2)
        s.servo.observe_r_rate(audio_mod.RING_BUFFER_ADDR + advanced)
        self.assertEqual(s.servo.r_rate_ema, -1.0)
        self.assertEqual(s.servo.r_rate_min, -1.0)

    def test_loop_seeds_rate_on_valid_read(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.servo.last_r_addr = audio_mod.RING_BUFFER_ADDR
        s.servo.last_r_time = time.monotonic() - 0.1  # ~0.1 s ago
        s.servo.r_rate_ema = -1.0
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR + 1000)  # ~1000 B in ~0.1 s
        self.assertGreater(s.servo.r_rate_ema, 0.0)  # seeded to ~10 kB/s (timing-slop)

    # ---- warm-up gate ----
    def _slow_r_primed(self) -> AudioStreamer:
        """A streamer primed so the next _update_nmi_rate_loop call WOULD step the
        latch (slow R, past the decide cadence) absent any warm-up hold."""
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.nmi.started = True
        s.nmi.latch = s.nmi.nominal_latch()  # nominal
        s.servo.r_rate_ema = 9456.0  # ~9.9% slow → coarse step
        s.servo.last_r_addr = -1  # skip the EMA update this call (use the pre-seed)
        s.servo.loop_chunk_count = max(1, round(s.sample_rate / s.chunk_size)) - 1
        return s

    def test_warmup_holds_latch(self):
        # Within the warm-up window the loop must NOT move the latch, even with a
        # slow R that would otherwise step it (the start/seek transient hold).
        s = self._slow_r_primed()
        s.servo.warmup_until = time.monotonic() + 5.0  # warm-up in effect
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(s.nmi.latch, s.nmi.nominal_latch())  # unchanged

    def test_warmup_still_updates_ema(self):
        # The EMA keeps warming during warm-up so the first post-warm-up decision
        # acts on a settled estimate rather than re-seeding off one sample.
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.nmi.started = True
        s.servo.warmup_until = time.monotonic() + 5.0
        s.servo.last_r_addr = audio_mod.RING_BUFFER_ADDR
        s.servo.last_r_time = time.monotonic() - 0.1
        s.servo.r_rate_ema = -1.0
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR + 1000)
        self.assertGreater(s.servo.r_rate_ema, 0.0)  # measured + seeded despite the hold

    def test_acts_after_warmup(self):
        # Past the warm-up deadline the same slow R steps the latch (gate released).
        s = self._slow_r_primed()
        s.servo.warmup_until = time.monotonic() - 0.01  # warm-up elapsed
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(s.nmi.latch, 92)  # 96 - capped coarse step 4

    def test_note_playback_disturbance_rearms_warmup(self):
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        before = time.monotonic()
        s.note_playback_disturbance()
        self.assertGreaterEqual(
            s.servo.warmup_until, before + audio_mod.NMI_RATE_LOOP_WARMUP_S - 0.05
        )

    def test_disturbance_then_held(self):
        # End-to-end: a disturbance arms warm-up, which then holds a would-be step.
        s = self._slow_r_primed()
        s.note_playback_disturbance()
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(s.nmi.latch, s.nmi.nominal_latch())  # held by the re-arm

    def test_a_timer_not_yet_started_is_not_stepped(self):
        # The worker can read R before the NMI arms (a prebuffer that has not
        # filled yet); stepping then would write a latch over a timer start()
        # is about to arm at its own seed.
        s = self._slow_r_primed()
        s.nmi.started = False
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(s.nmi.latch, s.nmi.nominal_latch())
        self.assertNotIn(f"{CIA2.TIMER_A_LO:04X}", cast(Any, s.api).regs)

    def test_the_loop_targets_the_armed_rate_not_the_requested_one(self):
        # PAL at 11 kHz arms latch 89, 0.48% under the request. An R 1.2% under
        # that armed rate is inside the deadband and converged; judged against
        # sample_rate it is 1.67% slow, and the loop would walk the latch off
        # nominal by the quantization error alone.
        s = _make(sample_rate=11000, system="PAL", nmi_rate_adaptive=True)
        s.nmi.started = True
        s.nmi.latch = s.nmi.nominal_latch()
        self.assertLess(s.nmi.effective_rate, 0.996 * s.sample_rate)
        s.servo.warmup_until = 0.0
        s.servo.r_rate_ema = s.nmi.effective_rate * (1 - 0.012)
        s.servo.last_r_addr = -1
        s.servo.loop_chunk_count = audio_rate_mod.NMI_RATE_LOOP_ACQUIRE_DECIDE_CHUNKS - 1
        s.servo.update_rate_loop(audio_mod.RING_BUFFER_ADDR)
        self.assertEqual(s.nmi.latch, s.nmi.nominal_latch())
        self.assertFalse(s.servo.loop_acquiring)

    def test_the_seed_holds_a_learned_latch_past_nominal_to_nominal(self):
        # A learned latch slower than nominal would arm the scene below
        # sample_rate and hold it there through the whole warm-up; the seed
        # keeps to the [ceiling, nominal] range the loop itself steps in.
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.nmi.learned_latch["petscii"] = s.nmi.nominal_latch() + 20
        self.assertEqual(s.nmi.seed_latch_for_mode("petscii"), s.nmi.nominal_latch())

    def test_the_rate_reads_across_the_ring_end_as_a_forward_advance(self):
        # R wrapped past the ring's last byte between two readings: 500 bytes
        # before the end, then 500 after the start, is 1000 bytes forward.
        s = _make(sample_rate=10500, nmi_rate_adaptive=True)
        s.nmi.latch = s.nmi.nominal_latch()
        ring_end = audio_mod.RING_BUFFER_ADDR + audio_mod.RING_BUFFER_SIZE
        s.servo.last_r_addr = ring_end - 500
        s.servo.last_r_time = time.monotonic() - 0.1
        s.servo.r_rate_ema = -1.0
        s.servo.observe_r_rate(audio_mod.RING_BUFFER_ADDR + 500)
        # 1000 B over at least the 0.1 s set above, and well under a second. The
        # ceiling carries 0.1 % for float noise (dt is a hair over 0.1 s, so the
        # exact 10000 bound is met only to the last ulp); an unwrapped read is
        # negative, far outside both bounds.
        self.assertGreater(s.servo.r_rate_ema, 1000 / 1.0)
        self.assertLess(s.servo.r_rate_ema, 1000 / 0.1 * 1.001)


class DigiBoostTest(unittest.TestCase):
    def test_enable_writes_all_voices(self):
        s = _make(digi_boost=True)
        with self.assertLogs("c64cast.audio.audio", level="INFO"):
            s._enable_digi_boost()
        api = cast(Any, s.api)
        # One control byte (write_memory) per voice at its CONTROL register.
        for v in range(SID.N_VOICES):
            ctrl = f"{SID.voice_base(v) + SID.OFF_CONTROL:04X}"
            self.assertIn(ctrl, api.memories)

    def test_disable_releases_gate_each_voice(self):
        s = _make(digi_boost=True)
        s._release_sid_gates()
        api = cast(Any, s.api)
        for v in range(SID.N_VOICES):
            ctrl = f"{SID.voice_base(v) + SID.OFF_CONTROL:04X}"
            self.assertEqual(api.memories[ctrl], "40")  # SID_GATE_OFF

    def test_disable_swallows_write_errors(self):
        s = _make(digi_boost=True)

        def boom(addr: str, data_hex: str) -> None:
            raise RuntimeError("write failed")

        cast(Any, s).api.write_memory = boom
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s._release_sid_gates()  # must not raise

    def test_one_failed_voice_does_not_starve_the_others(self):
        s = _make(digi_boost=True)
        api = cast(Any, s.api)
        write_memory = api.write_memory
        first_voice = f"{SID.voice_base(0) + SID.OFF_CONTROL:04X}"

        def boom(addr: str, data_hex: str) -> None:
            if addr == first_voice:
                raise RuntimeError("write failed")
            write_memory(addr, data_hex)

        api.write_memory = boom
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s._release_sid_gates()
        self.assertNotIn(first_voice, api.memories)
        for v in range(1, SID.N_VOICES):
            ctrl = f"{SID.voice_base(v) + SID.OFF_CONTROL:04X}"
            self.assertEqual(api.memories[ctrl], "40")


class EncodeBackpressureTest(unittest.TestCase):
    def test_block_on_full_times_out_to_zero(self):
        s = _make()
        s.running = True
        s._queued_samples = s._max_queued_samples  # saturate the soft cap
        with mock.patch.object(s, "_backpressure_wait_s", return_value=0.0):
            n = s._encode_and_enqueue(np.zeros(64, dtype=np.float32), block_on_full=True)
        self.assertEqual(n, 0)

    def test_a_decoder_ahead_of_real_time_loses_no_blob_at_startup(self):
        # A decoder fills the queue at once, then waits for room behind a
        # worker that frees it a chunk at a time, one chunk late: the first
        # wait outlasted QUEUE_PUT_TIMEOUT_S and the blob was dropped. 1114
        # samples is a 4096-sample 44.1 kHz WAV packet resampled to 12 kHz.
        s = _make(sample_rate=12000)
        s.nmi.start = lambda **kw: None  # type: ignore[method-assign]
        s.host_dma_servo = False
        blob = np.zeros(1114, dtype=np.int16)
        # Past the prebuffer the worker takes and the soft cap the queue holds.
        prebuffer = PREBUFFER_CHUNKS * s.chunk_size
        pushes = (s._max_queued_samples + prebuffer) // blob.size + 3
        with quiet_logging():
            s.start_for_external_source()
            try:
                accepted = [s.push_samples(blob) for _ in range(pushes)]
            finally:
                s.stop()
        self.assertEqual(accepted, [blob.size] * pushes)

    def test_block_on_full_succeeds_when_capacity_frees(self):
        s = _make()
        s.running = True
        # Under the sample cap → the put path runs (block_on_full timeout arm).
        n = s._encode_and_enqueue(np.zeros(64, dtype=np.float32), block_on_full=True)
        self.assertEqual(n, 64)
        self.assertEqual(s._queued_samples, 64)

    def test_queue_full_on_nowait_returns_zero(self):
        s = _make()
        s.running = True
        s.q = queue.Queue(maxsize=1)
        s.q.put(b"\x07")  # fill the single blob slot
        s._queued_samples = 0  # but keep the sample cap clear
        n = s._encode_and_enqueue(np.zeros(8, dtype=np.float32), block_on_full=False)
        self.assertEqual(n, 0)
        self.assertEqual((s._queued_samples, s._pushed_count), (0, 0))

    def test_a_flush_that_drains_a_blob_just_put_keeps_the_landed_count(self):
        # Drained before its count was added, the blob's subtract clamped at
        # zero and the late add left a queued count the ring never consumes,
        # with the landed count dropped by the blob.
        s = _make()
        s.running = True
        s._pushed_count = 1000  # everything pushed so far has landed
        anchors: list[float] = []

        class FlushAfterPut(queue.Queue):  # type: ignore[type-arg]
            def put(self, item, block=True, timeout=None):  # type: ignore[no-untyped-def]
                super().put(item, block, timeout)
                if not anchors:
                    anchors.append(s.flush())

        s.q = FlushAfterPut(maxsize=s.q.maxsize)
        self.assertEqual(s._encode_and_enqueue(np.zeros(200, dtype=np.float32), True), 200)
        self.assertEqual(round(anchors[0] * s.effective_rate), 1000)
        self.assertEqual((s._pushed_count, s._queued_samples, s.q.qsize()), (1000, 0, 0))

    def test_empty_input_returns_zero(self):
        s = _make()
        self.assertEqual(s._encode_and_enqueue(np.array([], dtype=np.float32)), 0)

    def test_blob_bigger_than_the_whole_cap_is_still_admitted(self):
        """The gate tests queued + n against the cap, so a blob larger than the
        cap on its own could never clear it however empty the queue got: the
        caller burned the timeout and returned 0 for that call and every one
        after it — permanent silence, from a method whose docstring promises
        graceful throttling. An empty queue admits it once."""
        s = _make()
        s.running = True
        n = s._max_queued_samples + 1
        self.assertEqual(s._encode_and_enqueue(np.zeros(n, dtype=np.float32), True), n)


class EncodeDacTest(unittest.TestCase):
    def test_explicit_rng_dither_is_reproducible(self):
        # The offline pre-encode path passes a seeded Generator; same seed →
        # identical codes (exercises the rng-provided dither branch).
        floats = np.linspace(-0.9, 0.9, 64, dtype=np.float32)
        a = encode_floats_to_dac(floats, dither=True, rng=np.random.default_rng(7))
        b = encode_floats_to_dac(floats, dither=True, rng=np.random.default_rng(7))
        np.testing.assert_array_equal(a, b)
        self.assertEqual(a.dtype, np.uint8)

    def test_dither_without_a_generator_is_refused(self):
        # The fallback this replaces drew from numpy's process-wide RNG, which
        # no caller owns and no run can reproduce. Refusing is the noisy half.
        floats = np.linspace(-0.9, 0.9, 64, dtype=np.float32)
        with self.assertRaises(ValueError):
            encode_floats_to_dac(floats, dither=True)

    def test_no_generator_is_fine_without_dither(self):
        floats = np.linspace(-0.9, 0.9, 64, dtype=np.float32)
        self.assertEqual(encode_floats_to_dac(floats, dither=False).dtype, np.uint8)


class DitherSeedTest(unittest.TestCase):
    """The realtime dither is a sequence the streamer owns: same seed, same
    bytes; different seed, different bytes."""

    def _chunks(self, seed: int) -> list[bytes]:
        s = _make(dither=True, dither_seed=seed)
        floats = np.linspace(-0.9, 0.9, 256, dtype=np.float32)
        return [s._encode_dac(floats).tobytes() for _ in range(3)]

    def test_same_seed_is_byte_identical(self):
        self.assertEqual(self._chunks(4242), self._chunks(4242))

    def test_different_seeds_differ(self):
        self.assertNotEqual(self._chunks(4242), self._chunks(9001))

    def test_successive_chunks_advance_the_sequence(self):
        # Same input three times: identical output would mean the generator was
        # being rebuilt (or reseeded) per chunk rather than drawn from.
        a, b, c = self._chunks(4242)
        self.assertNotEqual(a, b)
        self.assertNotEqual(b, c)

    def test_undithered_encoding_ignores_the_seed(self):
        floats = np.linspace(-0.9, 0.9, 256, dtype=np.float32)
        lo = _make(dither=False, dither_seed=1)._encode_dac(floats)
        hi = _make(dither=False, dither_seed=2)._encode_dac(floats)
        np.testing.assert_array_equal(lo, hi)

    def test_seed_is_logged_when_dither_is_on(self):
        # Logging it is what makes a dithered capture reproducible after the
        # fact — the seed has to reach the run's log, not just the object.
        with self.assertLogs("c64cast.audio.audio", "INFO") as cm:
            s = _make(dither=True, dither_seed=777)
        self.assertIn("777", "\n".join(cm.output))
        self.assertEqual(s.dither_seed, 777)

    def test_seed_is_not_logged_when_dither_is_off(self):
        with self.assertNoLogs("c64cast.audio.audio", "INFO"):
            _make(dither=False)

    def test_unseeded_streamers_get_different_seeds(self):
        seeds = {_make(dither=False).dither_seed for _ in range(8)}
        self.assertEqual(len(seeds), 8)


class SampleTapWrapTest(unittest.TestCase):
    def test_split_write_across_buffer_end(self):
        # Write head near the end so a sub-tap push wraps the ring (the
        # two-slice branch in _push_to_tap, distinct from the >= tap-size case).
        s = _make()
        s._tap_write = SAMPLE_TAP_SIZE - 3
        s._push_to_tap(np.array([0.1, 0.2, 0.3, 0.4, 0.5], dtype=np.float32))
        out = s.get_recent_samples(5)
        np.testing.assert_allclose(out, [0.1, 0.2, 0.3, 0.4, 0.5], rtol=1e-5)
        self.assertEqual(s._tap_write, 2)


class MicCallbackTest(unittest.TestCase):
    def test_status_flag_drops_frame(self):
        s = _make()
        s.running = True
        s._mic_callback(np.ones((10, 1), dtype=np.float32), 10, None, status="overflow")
        self.assertEqual(s._queued_samples, 0)

    def test_not_running_drops_frame(self):
        s = _make()
        s.running = False
        s._mic_callback(np.ones((10, 1), dtype=np.float32), 10, None, None)
        self.assertEqual(s._queued_samples, 0)

    def test_enqueues_gated_stereo_downmix(self):
        s = _make()
        s.running = True
        s.sensitivity = 1.0
        s.noise_gate = 0.05
        # Stereo input above the gate → downmixed + enqueued.
        indata = np.full((32, 2), 0.5, dtype=np.float32)
        s._mic_callback(indata, 32, None, None)
        self.assertEqual(s._queued_samples, 32)


class AnalysisSinkFailureTest(unittest.TestCase):
    """The session shares one streamer across scenes, and each reactive source
    installs its own analyzer on it, so each installed sink's first failure
    is logged."""

    def test_a_reinstalled_analyzer_failing_again_is_logged_again(self):
        def broken(_floats: np.ndarray) -> None:
            raise ValueError("analyzer broke")

        s = _make()
        for _lap in range(2):
            s.analysis_sink = broken  # the source installs it every setup()
            with self.assertLogs("c64cast.audio.audio", "ERROR") as logs:
                s._push_to_analysis(np.zeros(8, dtype=np.float32))
            self.assertIn("analysis sink failed", logs.output[0])
            self.assertIsNone(s.analysis_sink)


class ListenOnlyCaptureTest(unittest.TestCase):
    """start_listen: analysis-only capture — no NMI, no worker, no DAC/SID
    writes. The samples reach the analysis sink and stop there."""

    def _patch_sd(self, fake: Any) -> None:
        orig_sd, orig_avail = audio_mod.sd, audio_mod.AUDIO_AVAILABLE
        audio_mod.sd = fake
        audio_mod.AUDIO_AVAILABLE = True
        self.addCleanup(lambda: setattr(audio_mod, "sd", orig_sd))
        self.addCleanup(lambda: setattr(audio_mod, "AUDIO_AVAILABLE", orig_avail))

    def test_listen_callback_feeds_only_the_analysis_sink(self):
        s = _make()
        s.running = True
        s.sensitivity = 2.0
        pushed: list[np.ndarray] = []
        s.analysis_sink = pushed.append
        s._listen_callback(np.full((16, 1), 0.25, dtype=np.float32), 16, None, None)
        # Reached the sink, scaled by sensitivity; never queued for the DAC.
        self.assertEqual(len(pushed), 1)
        np.testing.assert_allclose(pushed[0], 0.5)
        self.assertEqual(s._queued_samples, 0)

    def test_listen_callback_drops_when_not_running(self):
        s = _make()
        s.running = False
        pushed: list[np.ndarray] = []
        s.analysis_sink = pushed.append
        s._listen_callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
        self.assertEqual(pushed, [])

    def test_start_listen_opens_analysis_only_at_the_given_rate(self):
        fake = _FakeSD([{"name": "line", "max_input_channels": 2}], 0)
        self._patch_sd(fake)
        s = _make(sample_rate=8000)
        try:
            s.start_listen(0, 1.0, sample_rate=44100)
            self.assertTrue(s.running)
            self.assertTrue(s._listen_mode)
            # Opened once, at the listen rate, with the listen callback — and no
            # worker thread was spun up (the DAC path's tell).
            self.assertEqual(len(fake.created), 1)
            self.assertEqual(fake.created[0]["samplerate"], 44100)
            self.assertEqual(fake.created[0]["callback"], s._listen_callback)
            self.assertIsNone(s._worker_thread)
        finally:
            s.stop()

    def test_start_listen_defaults_to_the_dac_rate(self):
        fake = _FakeSD([{"name": "line", "max_input_channels": 1}], 0)
        self._patch_sd(fake)
        s = _make(sample_rate=8000)
        try:
            s.start_listen(0, 1.0)
            self.assertEqual(fake.created[0]["samplerate"], 8000)
        finally:
            s.stop()

    def test_stop_after_listen_skips_dac_teardown(self):
        fake = _FakeSD([{"name": "line", "max_input_channels": 2}], 0)
        self._patch_sd(fake)
        s = _make()
        s.start_listen(0, 1.0)
        # The listen branch of stop() must not run the DAC/REU teardown at all.
        disarmed: list[bool] = []
        s._disarm_reu_pump = lambda: disarmed.append(True)  # type: ignore[method-assign]
        s.stop()
        self.assertEqual(disarmed, [])
        self.assertFalse(s.running)
        self.assertFalse(s._listen_mode)
        self.assertIsNone(s.mic_stream)

    def test_start_listen_by_name_resolves_and_opens(self):
        # Regression: a device *name* must be resolved to an int before the
        # "device=%d" log line — otherwise %d on a str raises TypeError and the
        # scene aborts (caught on real hardware with -D "Cam Link").
        fake = _FakeSD(
            [
                {"name": "Built-in Mic", "max_input_channels": 1},
                {"name": "Cam Link 4K", "max_input_channels": 2},
            ],
            0,
        )
        self._patch_sd(fake)
        s = _make(sample_rate=8000)
        try:
            with self.assertLogs("c64cast.audio.audio", level="INFO"):
                s.start_listen("Cam Link", 1.0, sample_rate=44100)
            self.assertTrue(s.running)
            # Opened against the name-resolved index (Cam Link → 1).
            self.assertEqual(fake.created[0]["device"], 1)
        finally:
            s.stop()

    def test_start_listen_without_sounddevice_warns(self):
        orig_avail = audio_mod.AUDIO_AVAILABLE
        audio_mod.AUDIO_AVAILABLE = False
        self.addCleanup(lambda: setattr(audio_mod, "AUDIO_AVAILABLE", orig_avail))
        s = _make()
        with self.assertLogs("c64cast.audio.audio", level="WARNING"):
            s.start_listen(0, 1.0)
        self.assertFalse(s.running)


# --- fake sounddevice for input-device resolution ------------------------


class _FakePortAudioError(Exception):
    pass


class _FakeStream:
    def __init__(self, **kw: Any):
        self.kw = kw
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeDefault:
    def __init__(self, default_input: int):
        # PortAudio's sd.default.device is an (input, output) pair; -1 stands
        # in for "no output device" (the code only ever reads index 0).
        self.device: list[int] = [default_input, -1]


class _FakeSD:
    PortAudioError = _FakePortAudioError

    def __init__(
        self,
        devices: list[dict[str, Any]],
        default_input: int,
        reject_channels: set[int] | None = None,
    ):
        self._devices = devices
        self.default = _FakeDefault(default_input)
        self.reject_channels = reject_channels or set()
        self.created: list[dict[str, Any]] = []

    def query_devices(self, idx: Any = None, kind: Any = None) -> Any:
        # Real sounddevice returns the full DeviceList on a no-arg call (what
        # resolve_audio_input_device iterates) and a single dict when indexed.
        if idx is None:
            return list(self._devices)
        return self._devices[idx]

    def InputStream(self, **kw: Any) -> _FakeStream:
        if kw.get("channels") in self.reject_channels:
            raise _FakePortAudioError("invalid channels")
        self.created.append(kw)
        return _FakeStream(**kw)


class InputDeviceResolutionTest(unittest.TestCase):
    def _patch_sd(self, fake: _FakeSD) -> None:
        self._orig_sd = audio_mod.sd
        self._orig_avail = audio_mod.AUDIO_AVAILABLE
        audio_mod.sd = fake
        audio_mod.AUDIO_AVAILABLE = True
        self.addCleanup(self._restore_sd)

    def _restore_sd(self) -> None:
        audio_mod.sd = self._orig_sd
        audio_mod.AUDIO_AVAILABLE = self._orig_avail

    def test_negative_device_uses_default(self):
        fake = _FakeSD([{"name": "mic", "max_input_channels": 1}], 0)
        self._patch_sd(fake)
        s = _make()
        dev, name = s._resolve_input_device(-1)
        self.assertEqual(dev, 0)
        self.assertEqual(name, "mic")

    def test_valid_device_with_inputs(self):
        fake = _FakeSD(
            [
                {"name": "speaker", "max_input_channels": 0},
                {"name": "usb mic", "max_input_channels": 2},
            ],
            1,
        )
        self._patch_sd(fake)
        s = _make()
        dev, name = s._resolve_input_device(1)
        self.assertEqual(dev, 1)
        self.assertEqual(name, "usb mic")

    def test_output_only_device_is_refused(self):
        fake = _FakeSD(
            [
                {"name": "default mic", "max_input_channels": 1},
                {"name": "speaker only", "max_input_channels": 0},
            ],
            0,
        )
        self._patch_sd(fake)
        s = _make()
        # Never the default input in its place: on a laptop that is the
        # built-in microphone.
        with self.assertRaises(audio_mod.AudioInputDeviceError):
            s._resolve_input_device(1)

    def test_open_stream_channel_fallback(self):
        # channels=1 rejected, native channels=2 accepted.
        fake = _FakeSD([{"name": "stereo mic", "max_input_channels": 2}], 0, reject_channels={1})
        self._patch_sd(fake)
        s = _make()
        with self.assertLogs("c64cast.audio.audio", level="INFO"):
            stream = s._open_input_stream(0)
        self.assertIsInstance(stream, _FakeStream)
        self.assertEqual(cast(Any, stream).kw["channels"], 2)

    def test_open_stream_all_channels_rejected_raises(self):
        # Every candidate channel count is rejected by PortAudio → the final
        # "could not open mic" RuntimeError (debug logs per attempt, no warning).
        fake = _FakeSD([{"name": "fussy mic", "max_input_channels": 2}], 0, reject_channels={1, 2})
        self._patch_sd(fake)
        s = _make()
        with self.assertLogs("c64cast.audio.audio", level="DEBUG"):
            with self.assertRaises(RuntimeError):
                s._open_input_stream(0)

    def test_resolve_query_failure_is_refused(self):
        # query_devices raising for the requested device → refused, not the default.
        class _RaisingSD(_FakeSD):
            def query_devices(self, idx=None, kind=None):
                if idx == 5:
                    raise RuntimeError("no such device")
                return super().query_devices(idx, kind)

        fake = _RaisingSD([{"name": "default mic", "max_input_channels": 1}], 0)
        self._patch_sd(fake)
        s = _make()
        with self.assertRaises(audio_mod.AudioInputDeviceError):
            s._resolve_input_device(5)

    def test_open_stream_no_usable_device_raises(self):
        fake = _FakeSD([{"name": "dead", "max_input_channels": 0}], 0)
        self._patch_sd(fake)
        s = _make()
        with self.assertRaises(RuntimeError):
            s._open_input_stream(0)

    def test_start_mic_refuses_an_unmatched_name_before_starting_anything(self):
        fake = _FakeSD([{"name": "MacBook Pro Microphone", "max_input_channels": 1}], 0)
        self._patch_sd(fake)
        s = _make()
        with self.assertRaises(audio_mod.AudioInputDeviceError):
            s.start_mic("Scarlett", 1.0, 0.05)
        self.assertFalse(s.running)
        self.assertEqual(fake.created, [])

    def test_a_mic_scene_with_an_unmatched_name_plays_silent_not_the_laptop_mic(self):
        # Webcam and blank scenes keep their picture, log why there is no
        # sound, and open no input at all.
        from types import SimpleNamespace

        from c64cast.scenes import scenes

        fake = _FakeSD([{"name": "MacBook Pro Microphone", "max_input_channels": 1}], 0)
        self._patch_sd(fake)
        s = _make()
        scene = SimpleNamespace(audio=s, display_mode=None, name="cam")
        cfg = SimpleNamespace(device="Scarlett", mic_sensitivity=1.0, noise_gate=0.05)
        with self.assertLogs("c64cast.scenes.scenes", level="ERROR") as logs:
            scenes._start_scene_mic(cast(Any, scene), cast(Any, cfg))
        self.assertIn("Scarlett", logs.output[0])
        self.assertFalse(s.running)
        self.assertEqual(fake.created, [])

    def test_start_mic_refuses_an_output_only_index_before_starting_anything(self):
        fake = _FakeSD(
            [
                {"name": "MacBook Pro Microphone", "max_input_channels": 1},
                {"name": "MacBook Pro Speakers", "max_input_channels": 0},
            ],
            0,
        )
        self._patch_sd(fake)
        s = _make()
        with self.assertRaises(audio_mod.AudioInputDeviceError):
            s.start_mic(1, 1.0, 0.05)
        self.assertFalse(s.running)
        self.assertIsNone(s._worker_thread)
        self.assertEqual(fake.created, [])

    def test_start_listen_refuses_an_unmatched_name(self):
        fake = _FakeSD([{"name": "MacBook Pro Microphone", "max_input_channels": 1}], 0)
        self._patch_sd(fake)
        s = _make()
        with self.assertRaises(audio_mod.AudioInputDeviceError):
            s.start_listen("Scarlett", 1.0)
        self.assertFalse(s.running)
        self.assertEqual(fake.created, [])

    def test_start_mic_without_sounddevice_warns(self):
        self._orig_avail = audio_mod.AUDIO_AVAILABLE
        audio_mod.AUDIO_AVAILABLE = False
        self.addCleanup(lambda: setattr(audio_mod, "AUDIO_AVAILABLE", self._orig_avail))
        s = _make()
        with self.assertLogs("c64cast.audio.audio", level="WARNING"):
            s.start_mic(0, 1.0, 0.05)
        self.assertFalse(s.running)


class LifecycleTest(unittest.TestCase):
    def test_start_external_source_brings_up_worker(self):
        s = _make()
        try:
            s.start_for_external_source()
            self.assertTrue(s.running)
            self.assertIsNotNone(s._worker_thread)
            # NMI routine + neutral ring were uploaded.
            api = cast(Any, s.api)
            self.assertIn("C020", api.mem_files)
            self.assertIn("4000", api.mem_files)
        finally:
            s.stop()

    def test_push_samples_enqueues(self):
        s = _make()
        s.running = True
        s.push_samples(np.array([0, 16384, -16384], dtype=np.int16))
        self.assertEqual(s._queued_samples, 3)

    def test_push_samples_after_stop_does_not_refill_the_queue(self):
        s = _make()
        s.running = True
        s.push_samples(np.array([0, 16384, -16384], dtype=np.int16))
        self.assertEqual(s._queued_samples, 3)
        s.stop()
        s.push_samples(np.array([0, 16384, -16384], dtype=np.int16))
        self.assertEqual(s._queued_samples, 0)
        self.assertTrue(s.q.empty())

    def test_position_seconds_reads_what_is_heard(self):
        # A landed sample is heard one ring lead later, and nothing is heard
        # before the consumer starts.
        s = _make()
        s._pushed_count = 8000
        s._queued_samples = 0
        self.assertEqual(s.position_seconds(), 0.0)
        self.assertAlmostEqual(s.ring_lead_seconds(), 8000 / s.effective_rate, places=6)
        s.servo.ring_lead = 2000.0
        self.assertAlmostEqual(s.position_seconds(), 6000 / s.effective_rate, places=6)
        self.assertAlmostEqual(s.ring_lead_seconds(), 2000 / s.effective_rate, places=6)
        s.servo.reset_after_stop()
        self.assertEqual(s.position_seconds(), 0.0)

    def test_a_splice_during_the_prebuffer_waits_out_what_already_landed(self):
        # The pre-splice prebuffer plays first once the consumer starts, so the
        # splice's anchor, position + lead, has to sit past it on both sides of
        # the start.
        s = _make()
        s._pushed_count = 3072
        s._queued_samples = 1024
        landed = 2048 / s.effective_rate
        self.assertEqual(s.position_seconds(), 0.0)
        self.assertAlmostEqual(s.ring_lead_seconds(), landed, places=6)
        s.servo.reset_for_consumer_start(2048)
        self.assertAlmostEqual(s.position_seconds() + s.ring_lead_seconds(), landed, places=6)

    def test_a_splice_just_after_the_start_leaves_out_the_prebuffer_pad(self):
        # The consumer starts behind a prebuffer whose last chunk was padded,
        # so the seeded lead exceeds the landed content and position_seconds()
        # reads 0; the anchor still has to be the landed content, not the pad
        # past it.
        s = _make()
        s._pushed_count = 2036
        s._queued_samples = 0
        s.servo.reset_for_consumer_start(2048)
        self.assertEqual(s.position_seconds(), 0.0)
        self.assertAlmostEqual(
            s.position_seconds() + s.ring_lead_seconds(), 2036 / s.effective_rate, places=6
        )

    def test_the_prebuffer_lead_leaves_out_a_padded_chunks_pad(self):
        # position_seconds() counts content only, so the clock already lags by
        # a prebuffer pad; a lead that also counted the pad would hold the
        # picture that far past the splice's first sample.
        s = _make_worker_streamer(chunk_size=32)
        before: list[float] = []
        seeds: list[int] = []
        seed = s.servo.reset_for_consumer_start

        def capture_then_seed(ring_lead: int) -> None:
            before.append(s.ring_lead_seconds())
            seeds.append(ring_lead)
            seed(ring_lead)

        s.servo.reset_for_consumer_start = capture_then_seed  # type: ignore[method-assign]
        s.host_dma_servo = False
        s.start_for_external_source()

        def stop_quietly() -> None:
            # The run summary stop() logs is asserted by the underrun tests.
            with quiet_logging():
                s.stop()

        self.addCleanup(stop_quietly)
        s.push_samples(np.zeros(20, dtype=np.int16))
        deadline = time.monotonic() + 5.0
        while s._pushed_count - s._queued_samples < 20 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(s._pushed_count - s._queued_samples, 20)
        s.push_samples(np.zeros(32 * 6, dtype=np.int16))
        while not seeds and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(len(seeds), 1)
        pad = 32 - 20
        self.assertAlmostEqual(before[0], (seeds[0] - pad) / s.effective_rate, places=9)

    def test_position_seconds_starts_at_zero_after_the_prebuffer(self):
        # The prebuffer lands before the consumer starts, so it is all lead.
        # The clock is read inside the start, on the worker: once the producer
        # runs dry the worker pads the ring and the clock rightly moves on.
        s = _make_worker_streamer(chunk_size=32)
        started = threading.Event()
        seed = s.servo.reset_for_consumer_start
        at_start: list[float] = []

        def seed_then_signal(ring_lead: int) -> None:
            seed(ring_lead)
            at_start.append(s.position_seconds())
            started.set()

        s.servo.reset_for_consumer_start = seed_then_signal  # type: ignore[method-assign]
        s.host_dma_servo = False
        s.start_for_external_source()
        self.addCleanup(s.stop)
        s.push_samples(np.zeros(32 * 6, dtype=np.int16))
        self.assertTrue(started.wait(5.0))
        self.assertEqual(s.servo.ring_lead, 32 * 6)
        self.assertEqual(at_start, [0.0])

    def test_position_seconds_reaches_the_end_once_the_producer_runs_dry(self):
        # Past the last sample the worker pads the ring, so the gap holds while
        # nothing in it is content. The clock has to reach the end anyway: a
        # video ends only when it reaches its last frame's PTS.
        s = _make_worker_streamer(chunk_size=32)
        s.host_dma_servo = False
        total = 32 * 6 + 20
        # The underrun summary stop() logs is asserted by the underrun tests.
        with quiet_logging():
            s.start_for_external_source()
            try:
                s.push_samples(np.zeros(total, dtype=np.int16))
                deadline = time.monotonic() + 5.0
                while s._full_underruns < 12 and time.monotonic() < deadline:
                    time.sleep(0.001)
                underruns = s._full_underruns
                position = s.position_seconds()
            finally:
                s.stop()
        self.assertGreaterEqual(underruns, 12)
        self.assertAlmostEqual(position, total / s.effective_rate, places=6)

    def _drive_ring(
        self, s: AudioStreamer, plan: str, chunk: int, gap: int
    ) -> list[tuple[float, int]]:
        """Land one ``chunk`` per plan letter and play ``chunk`` bytes after
        it, holding the ring gap at ``gap`` the way the servo does: ``c``
        lands content, ``p`` a whole pad chunk, ``s`` content with a short pad
        tail. Returns (clock in bytes, content bytes heard) after each step,
        with the smoothed gap taken as exact."""
        ring: list[bool] = []
        heard = 0
        for _ in range(gap // chunk):
            ring += [True] * chunk
            s._pushed_count += chunk
            s._note_ring_landed(s._worker_generation, chunk, 0)
        out = []
        for kind in plan:
            content = {"c": chunk, "p": 0, "s": chunk // 3}[kind]
            ring += [True] * content + [False] * (chunk - content)
            s._note_ring_landed(s._worker_generation, chunk, chunk - content)
            s._pushed_count += content
            played, ring = ring[:chunk], ring[chunk:]
            heard += sum(played)
            s.servo.ring_lead = float(len(ring))
            out.append((s.position_seconds() * s.effective_rate, heard))
        return out

    def test_content_after_a_pad_stretch_does_not_step_the_clock_back(self):
        # A producer that runs dry and resumes leaves pad in the ring ahead of
        # the resumed content. Nothing past the pad is heard until the pad has
        # played, so the clock holds and then moves on; counting that pad as
        # content once content landed behind it stepped the clock back by it.
        s = _make(sample_rate=12000)
        steps = self._drive_ring(s, "cccccc" + "pppppp" + "ccccccc", chunk=1024, gap=4096)
        for clock, heard in steps:
            self.assertAlmostEqual(clock, heard, places=6)
        clocks = [clock for clock, _ in steps]
        self.assertEqual(clocks, sorted(clocks))

    def test_a_short_pad_is_taken_out_where_it_sits(self):
        # A producer late by less than a chunk leaves a short pad between two
        # stretches of content: it is in the gap until it plays, wherever more
        # content lands behind it.
        s = _make(sample_rate=12000)
        steps = self._drive_ring(s, "ccscsccspcccccc", chunk=32, gap=128)
        for clock, heard in steps:
            self.assertAlmostEqual(clock, heard, places=6)

    def _started_on(self, clock: FrozenClock, plan: str) -> AudioStreamer:
        """A streamer whose consumer started on ``clock`` with a 4096-byte
        gap, then one 1024-byte chunk landed per plan letter (``c`` content,
        ``p`` pad), the gap read just after the last landing."""
        s = _make(sample_rate=12000)
        s.chunk_size = 1024
        with mock.patch.object(audio_mod, "time", clock):
            for _ in range(4):
                s._pushed_count += 1024
                s._note_ring_landed(s._worker_generation, 1024, 0)
            s.servo.reset_for_consumer_start(4096)
            s._mark_ring_clock()
            for kind in plan:
                pad = 1024 if kind == "p" else 0
                s._note_ring_landed(s._worker_generation, 1024, pad)
                s._pushed_count += 1024 - pad
            s.servo.ring_lead = 4096.0
        return s

    def test_the_clock_moves_between_landings(self):
        # The landed count moves a chunk at a time. Read only at landings, the
        # clock held for a chunk period and then jumped a chunk, and an
        # analyzer reading the file's tap at it skipped whole windows.
        clock = FrozenClock(100.0, "monotonic")
        s = self._started_on(clock, "c")
        period = 1024 / s.effective_rate
        with mock.patch.object(audio_mod, "time", clock):
            at_landing = s.position_seconds()
            self.assertAlmostEqual(at_landing, 1024 / s.effective_rate, places=9)
            clock.advance(period / 4)
            self.assertAlmostEqual(s.position_seconds(), at_landing + period / 4, places=9)
            clock.advance(period / 2)
            self.assertAlmostEqual(s.position_seconds(), at_landing + 3 * period / 4, places=9)

    def test_the_clock_between_landings_stops_at_the_next_chunk(self):
        # A link that stalls lands nothing: the clock runs on for at most the
        # chunk the next landing would have counted, then holds.
        clock = FrozenClock(100.0, "monotonic")
        s = self._started_on(clock, "c")
        with mock.patch.object(audio_mod, "time", clock):
            at_landing = s.position_seconds()
            clock.advance(5.0)
            self.assertAlmostEqual(
                s.position_seconds(), at_landing + 1024 / s.effective_rate, places=9
            )

    def test_each_landing_restarts_the_clock_between_landings(self):
        # The gap is read just after a landing, so it already holds what played
        # before it; counting that again from an older start runs the clock
        # ahead of the sound.
        clock = FrozenClock(100.0, "monotonic")
        s = self._started_on(clock, "")
        period = 1024 / s.effective_rate
        with mock.patch.object(audio_mod, "time", clock):
            clock.advance(period / 2)
            s._note_ring_landed(s._worker_generation, 1024, 0)
            s._pushed_count += 1024
            self.assertAlmostEqual(s.position_seconds(), period, places=9)
            clock.advance(period / 4)
            self.assertAlmostEqual(s.position_seconds(), period + period / 4, places=9)

    def test_the_worker_starts_the_clock_between_landings_with_the_consumer(self):
        # Without the start the clock never runs between landings at all: a
        # landing restarts it only once the consumer has started it.
        s = _make_worker_streamer(chunk_size=32)
        s.host_dma_servo = False
        s.start_for_external_source()
        self.addCleanup(s.stop)
        s.push_samples(np.zeros(32 * 6, dtype=np.int16))
        deadline = time.monotonic() + 5.0
        while s._ring_landed_at is None and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertIsNotNone(s._ring_landed_at)

    def test_the_clock_between_landings_does_not_move_over_pad(self):
        # Pad at the front of the gap is what the NMI plays next, and content
        # landed behind it is not heard until it has played.
        clock = FrozenClock(100.0, "monotonic")
        s = self._started_on(clock, "ppppc")
        with mock.patch.object(audio_mod, "time", clock):
            at_landing = s.position_seconds()
            clock.advance(1024 / s.effective_rate)
            self.assertEqual(s.position_seconds(), at_landing)

    def test_a_fractional_gap_does_not_move_the_clock_over_pad(self):
        # The smoothed gap is fractional; in fractional bytes the pad under a
        # partial span netted a few ULPs short of the span, and the clock read
        # that much content past the pad as heard. Read at the term itself: at
        # a clock of a third of a second those ULPs round away, and early in a
        # scene they did not.
        clock = FrozenClock(100.0, "monotonic")
        s = self._started_on(clock, "ppppc")
        with mock.patch.object(audio_mod, "time", clock):
            for _ in range(20):
                clock.advance(0.037 * 1024 / s.effective_rate)
                with s._ring_pad_lock:
                    self.assertEqual(s._played_since_landing(4095.7), 0.0)

    def test_the_clock_between_landings_stops_at_the_landed_content(self):
        # With pad across a fractional gap's front, the content in the gap is
        # counted from the fractional front and the content played since the
        # landing from a whole byte, and they came out a few ULPs apart: once
        # the interpolation reached the last content byte, the clock read
        # past everything landed.
        clock = FrozenClock(100.0, "monotonic")
        s = _make(sample_rate=12000)
        s.chunk_size = 1024
        with mock.patch.object(audio_mod, "time", clock):
            s._pushed_count += 64
            s._note_ring_landed(s._worker_generation, 64, 0)
            s.servo.reset_for_consumer_start(64)
            s._mark_ring_clock()
            s._pushed_count += 1024 - 600
            s._note_ring_landed(s._worker_generation, 1024, 600)
            s._pushed_count += 300
            s._note_ring_landed(s._worker_generation, 300, 0)
            s.servo.ring_lead = 301.4
            clock.advance(1.0)
            consumed, heard = s._host_clock_bytes()
        self.assertEqual(heard, consumed)

    def test_a_widening_smoothed_gap_does_not_walk_the_clock_back(self):
        # The gap is an EMA, so it can grow by more than what landed between
        # two reads; the clock holds rather than reporting less than it did.
        s = _make()
        s._pushed_count = 8000
        s.servo.ring_lead = 2000.0
        self.assertAlmostEqual(s.position_seconds(), 6000 / s.effective_rate, places=6)
        s.servo.ring_lead = 3000.0
        self.assertAlmostEqual(s.position_seconds(), 6000 / s.effective_rate, places=6)

    def test_a_new_activation_starts_its_clock_from_zero(self):
        # The high-water mark belongs to one activation: a reused streamer
        # would otherwise hold the next one's clock at the last one's end.
        s = _make()
        s._pushed_count = 8000
        s.servo.ring_lead = 0.0
        self.assertAlmostEqual(s.position_seconds(), 8000 / s.effective_rate, places=6)
        s.reset_position()
        s._pushed_count = 100
        self.assertAlmostEqual(s.position_seconds(), 100 / s.effective_rate, places=6)

    def test_a_reset_during_a_clock_read_still_starts_the_clock_from_zero(self):
        # A reset landing between the read of the landed count and the floor
        # update must not leave the old activation's position as the new
        # one's floor.
        s = _make()
        s._pushed_count = 8000
        s.servo.ring_lead = 0.0
        count_lock = s._count_lock
        resetter = threading.Thread(target=s.reset_position)

        class ResetOnRelease:
            def __enter__(self) -> None:
                count_lock.acquire()

            def __exit__(self, *exc: object) -> None:
                count_lock.release()
                if resetter.ident is None:
                    resetter.start()
                    # Long enough for an unfenced reset to finish here; a
                    # fenced one waits on the clock read instead.
                    resetter.join(0.2)

        s._count_lock = ResetOnRelease()  # type: ignore[assignment]
        s.position_seconds()
        resetter.join(5.0)
        self.assertFalse(resetter.is_alive())
        s._count_lock = count_lock
        s._pushed_count = 100
        self.assertAlmostEqual(s.position_seconds(), 100 / s.effective_rate, places=6)

    def test_a_landing_from_a_superseded_worker_is_not_recorded(self):
        # A worker that outlived stop()'s join returns from its ring write
        # after the next start: its chunk is not in the new activation's ring,
        # so recording it would put pad in the gap that is not there.
        s = _make()
        stale = s._worker_generation
        with mock.patch.object(threading.Thread, "start"):
            s._start_worker()
        s._note_ring_landed(stale, 32, 32)
        self.assertEqual(s._ring_landed_total, 0)
        self.assertEqual(len(s._ring_pads), 0)
        s._note_ring_landed(s._worker_generation, 32, 32)
        self.assertEqual(s._ring_landed_total, 32)

    def test_a_superseded_worker_does_not_move_the_counts(self):
        # The landing is only half the clock: consumed = pushed - queued, and a
        # late return that still subtracted its chunk from the next
        # activation's queued count would put the clock that chunk early.
        s = _make()
        stale = s._worker_generation
        with mock.patch.object(threading.Thread, "start"):
            s._start_worker()
        s._pushed_count, s._queued_samples = 100, 40
        s._consume_queued(32, generation=stale)
        s._discard_unpushed(32, generation=stale)
        self.assertEqual((s._pushed_count, s._queued_samples), (100, 40))
        s._consume_queued(32, generation=s._worker_generation)
        self.assertEqual((s._pushed_count, s._queued_samples), (100, 8))

    def test_stop_retires_the_worker_it_gave_up_on(self):
        # start_listen and the REU starts set running back to True without
        # starting a worker, so no _start_worker bump fences an orphan that
        # outlived stop()'s join: stop() itself has to.
        s = _make_worker_streamer()
        orphan = s._worker_generation
        s.stop()
        s.running = True  # what start_listen / start_for_reu_staged do
        t = threading.Thread(target=s._worker, args=(orphan,), daemon=True)
        t.start()
        try:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "the orphan resumed under the next start")
        finally:
            s.running = False
            t.join(timeout=1.0)

    def test_a_retired_worker_takes_nothing_from_the_next_activations_queue(self):
        # Its discard is fenced out of the counts, so a blob it pulled off the
        # queue after its stalled write returned would stay counted as queued
        # for the rest of the next activation, holding the clock back.
        s = _make_worker_streamer()
        api = cast(Any, s.api)
        real_write = api.write_memory_file
        parked, release = threading.Event(), threading.Event()
        arm = [False]

        def stalling_write(addr: str, data: bytes) -> None:
            if arm[0]:
                arm[0] = False
                parked.set()
                release.wait(2.0)
            real_write(addr, data)

        api.write_memory_file = stalling_write

        def push(blob: bytes) -> None:
            s.q.put(blob)
            with s._count_lock:
                s._queued_samples += len(blob)
                s._pushed_count += len(blob)

        for _ in range(PREBUFFER_CHUNKS + 4):
            push(bytes([NEUTRAL_SAMPLE]) * s.chunk_size)
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            # Arm only after an underrun: with everything pushed up front, that
            # proves every later collect comes back empty, so the parked write's
            # iteration holds no full chunk and has to collect again after the
            # release. Arming on an empty queue alone could park the write of an
            # iteration already holding a full chunk, which skips the collect
            # and lets an unguarded _collect_until pass.
            deadline = time.monotonic() + 2.0
            while s._full_underruns < 1 and time.monotonic() < deadline:
                time.sleep(0.002)
            self.assertGreaterEqual(s._full_underruns, 1, "the worker never ran dry")
            arm[0] = True
            self.assertTrue(parked.wait(2.0), "the worker never reached a ring write")
            # stop() minus its join: retire, splice, clear the counts.
            with s._ring_pad_lock:
                s._worker_generation += 1
            s._flush_epoch += 1
            s._drain_queue_samples()
            s._pushed_count = s._queued_samples = 0
            s.running = True  # the next activation
            for _ in range(4):
                push(bytes([NEUTRAL_SAMPLE]) * s.chunk_size)
            release.set()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())
            in_queue = sum(len(b) for b in list(s.q.queue))
            self.assertEqual(s._queued_samples, in_queue)
        finally:
            release.set()
            s.running = False
            t.join(timeout=1.0)

    def _park_in_ring_write(self, s: AudioStreamer, park_on: int, fail: bool = False):
        """Stall the worker's ``park_on``-th ring write (1-based) until released;
        ``fail`` makes that write raise once released. Returns (parked, release)."""
        api = cast(Any, s.api)
        real_write = api.write_memory_file
        parked, release = threading.Event(), threading.Event()
        count = [0]

        def stalling_write(addr: str, data: bytes) -> None:
            count[0] += 1
            if count[0] == park_on:
                parked.set()
                release.wait(2.0)
                if fail:
                    raise RuntimeError("late write failed")
            real_write(addr, data)

        api.write_memory_file = stalling_write
        return parked, release

    def test_a_retired_worker_does_not_start_the_nmi_after_its_prebuffer(self):
        # The write completing the prebuffer returns after a stop(): starting
        # the NMI and resetting the servo there would reprogram the timer and
        # wipe the servo under whatever activation came next.
        s = _make_worker_streamer()
        nmi_starts: list[dict[str, Any]] = []
        servo_resets: list[int] = []
        s.nmi.start = lambda **kw: nmi_starts.append(kw)  # type: ignore[method-assign]
        s.servo.reset_for_consumer_start = servo_resets.append  # type: ignore[method-assign]
        parked, release = self._park_in_ring_write(s, park_on=PREBUFFER_CHUNKS)
        for _ in range(PREBUFFER_CHUNKS):
            s.q.put(bytes([NEUTRAL_SAMPLE]) * s.chunk_size)
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            self.assertTrue(parked.wait(2.0), "the worker never reached its last prebuffer write")
            with s._ring_pad_lock:
                s._worker_generation += 1  # stop() retired it
            release.set()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())
            self.assertEqual(nmi_starts, [])
            self.assertEqual(servo_resets, [])
        finally:
            release.set()
            s.running = False
            t.join(timeout=1.0)

    def test_a_retired_workers_late_crash_leaves_the_next_activation_running(self):
        # Its write fails only after a stop() and the next start_*: clearing
        # `running` then would stop the activation that owns the ring now.
        s = _make_worker_streamer()
        parked, release = self._park_in_ring_write(s, park_on=1, fail=True)
        s.q.put(bytes([NEUTRAL_SAMPLE]) * s.chunk_size)
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            self.assertTrue(parked.wait(2.0), "the worker never reached a ring write")
            with s._ring_pad_lock:
                s._worker_generation += 1  # stop() retired it
            s.running = True  # the next activation
            with self.assertLogs("c64cast.audio.audio", level="ERROR") as cm:
                release.set()
                t.join(timeout=2.0)
            self.assertFalse(t.is_alive())
            self.assertTrue(any("audio worker crashed" in m for m in cm.output))
            self.assertTrue(s.running, "a retired worker's crash stopped the next activation")
        finally:
            release.set()
            s.running = False
            t.join(timeout=1.0)

    def test_the_worker_records_the_pad_before_it_counts_the_landing(self):
        # Content landing behind a dry tail: a reader between the two steps
        # must not pair the new landed count with the old pad record, which would
        # put the clock a ring of pad past anything heard.
        s = _make_worker_streamer(chunk_size=32)
        s.host_dma_servo = False
        total = 32 * 6 + 20
        seen: list[float] = []
        consume = s._consume_queued

        def consume_then_read(n: int, *, generation: int) -> None:
            consume(n, generation=generation)
            if n:
                seen.append(s.position_seconds())

        s._consume_queued = consume_then_read  # type: ignore[method-assign]
        with quiet_logging():
            s.start_for_external_source()
            try:
                s.push_samples(np.zeros(total, dtype=np.int16))
                deadline = time.monotonic() + 5.0
                while s._full_underruns < 12 and time.monotonic() < deadline:
                    time.sleep(0.001)
                underruns = s._full_underruns
                seen.clear()
                s.push_samples(np.zeros(32, dtype=np.int16))
                while not seen and time.monotonic() < deadline:
                    time.sleep(0.001)
            finally:
                s.stop()
        self.assertGreaterEqual(underruns, 12)
        self.assertTrue(seen)
        self.assertLessEqual(seen[0], total / s.effective_rate)

    def test_position_seconds_reads_the_landed_count_before_the_pad_record(self):
        # The worker records a landing's pad before it counts the landing, so
        # a reader that took the pad record first and the count after could
        # pair a window of pad with content landed behind it.
        s = _make()
        s.servo.ring_lead = 192.0
        s._pushed_count = 1032
        s._queued_samples = 32
        for _ in range(10):
            s._note_ring_landed(s._worker_generation, 32, 32)
        unplayed_pad = s._unplayed_pad

        def pad_then_land(lead: float) -> float:
            pad = unplayed_pad(lead)
            # Not _note_ring_landed or _consume_queued: the caller holds
            # _ring_pad_lock, and either lock taken here would block where the
            # per-test cap cannot interrupt it.
            s._ring_landed_total += 32
            s._queued_samples -= 32
            return pad

        s._unplayed_pad = pad_then_land  # type: ignore[method-assign]
        self.assertLessEqual(s.position_seconds(), 1000 / s.effective_rate)

    def test_position_seconds_is_not_torn_by_a_worker_discard(self):
        # The worker drops a pre-splice chunk with the paired subtract while
        # the video thread reads the clock; a read pairing the old pushed
        # count with the new queued count would lead by that chunk.
        class TearOnRead(AudioStreamer):
            @property
            def _pushed_count(self) -> int:
                pushed: int = self.__dict__["_pushed_raw"]
                if self._count_lock.acquire(blocking=False):
                    self._queued_samples -= 32
                    self.__dict__["_pushed_raw"] = pushed - 32
                    self._count_lock.release()
                return pushed

            @_pushed_count.setter
            def _pushed_count(self, value: int) -> None:
                self.__dict__["_pushed_raw"] = value

        s = _make()
        s.__class__ = TearOnRead
        s._pushed_count = 1032
        s._queued_samples = 32
        s.servo.ring_lead = 0.0
        self.assertLessEqual(s.position_seconds(), 1000 / s.effective_rate)

    def test_position_seconds_host_dma(self):
        # The divisor is effective_rate — the rate the CIA latch actually
        # yields — not the requested sample_rate. At 8 kHz NTSC that is
        # 7990.05 Hz, so 8000 consumed samples is 1.0012 s of real time, and
        # asserting a flat 1.0 here would be asserting the old 0.12% error.
        s = _make()
        s._pushed_count = 8000
        s._queued_samples = 0
        s.servo.ring_lead = 0.0
        self.assertAlmostEqual(s.position_seconds(), 8000 / s.effective_rate, places=6)
        self.assertAlmostEqual(s.position_seconds(), 1.00124, places=5)
        # Still-queued samples are not yet "consumed".
        s = _make()
        s._pushed_count = 8000
        s._queued_samples = 4000
        s.servo.ring_lead = 0.0
        self.assertAlmostEqual(s.position_seconds(), 4000 / s.effective_rate, places=6)

    def test_position_seconds_zero_rate(self):
        s = _make()
        s.sample_rate = 0
        self.assertEqual(s.position_seconds(), 0.0)

    def test_position_seconds_reu_pump_clamped(self):
        s = _make()
        s._reu_pump_armed = True
        s._reu_pump_total_samples = 8000  # 1.0 s of source
        s._reu_pump_start_time = time.monotonic() - 100.0  # long past
        # Clamped to total source length, not the 100 s of wall clock.
        self.assertAlmostEqual(s.position_seconds(), 1.0, places=2)

    def test_reset_position(self):
        s = _make()
        s._pushed_count = 1234
        s._in_flight_samples = 32
        s.reset_position()
        self.assertEqual((s._pushed_count, s._in_flight_samples), (0, 0))

    def test_stop_teardown_writes_and_logs_clean(self):
        s = _make()
        s.start_for_external_source()
        s._total_slots = 1
        with self.assertLogs("c64cast.audio.audio", level="INFO") as cm:
            s.stop()
        self.assertFalse(s.running)
        api = cast(Any, s.api)
        self.assertEqual(api.memories.get("D418"), "00")  # SID muted
        self.assertIsNone(s._worker_thread)
        self.assertEqual(s._queued_samples, 0)
        self.assertTrue(any("clean run" in m for m in cm.output))

    def test_the_backend_hears_the_nmi_player_stop(self):
        # A TR+ slices its writes only while this is on, so a stop that skipped
        # the note would leave every later write at a third the throughput.
        s = _make()
        s.start_for_external_source()
        api = cast(Any, s.api)
        s._total_slots = 1
        with self.assertLogs("c64cast.audio.audio", level="INFO"):
            s.stop()
        self.assertEqual(api.nmi_consumer_notes[-1], False)

    def _notes_around_the_arm(self, readable: bool) -> tuple[list[Any], list[Any]]:
        """Consumer notes taken at upload, then across the arm, each paired
        with the CIA #2 ICR value the fake held when it fired."""
        s = _make()
        api = cast(Any, s.api)
        seen: list[tuple[bool, Any]] = []
        api.note_nmi_consumer = lambda active: seen.append(
            (active, api.regs.get(f"{CIA2.ICR:04X}"))
        )
        s._upload_nmi_and_buffers()
        at_upload = list(seen)
        reads = iter([0x4000, 0x4010])
        with (
            mock.patch.object(audio_rate_mod, "NMI_ARM_VERIFY_DELAY_S", 0.0),
            mock.patch.object(s, "read_consumer_ptr", lambda: next(reads) if readable else None),
        ):
            s.nmi.start(adaptive=False)
        return at_upload, seen[len(at_upload) :]

    def test_the_backend_hears_the_nmi_player_start_only_once_it_is_armed(self):
        # Noted at upload, a TR+ sliced the whole prebuffer for an NMI that was
        # not running yet. The note has to land after the CIA #2 arm write,
        # whether the arm was verified or could not be.
        armed = (audio_rate_mod.CIA2_ICR_ENABLE_TIMER_A_NMI, audio_rate_mod.CIA2_TIMER_A_CONTINUOUS)
        for readable in (False, True):
            with self.subTest(readable=readable):
                at_upload, at_arm = self._notes_around_the_arm(readable)
                self.assertEqual(at_upload, [])
                self.assertEqual(at_arm, [(True, armed)])

    def test_a_consumer_that_never_started_is_not_noted(self):
        # R frozen through every retry means no NMI is consuming, so the link
        # has nothing to spare and must keep its full-speed writes.
        s = _make()
        api = cast(Any, s.api)
        s._upload_nmi_and_buffers()
        with (
            mock.patch.object(audio_rate_mod, "NMI_ARM_VERIFY_DELAY_S", 0.0),
            mock.patch.object(s, "read_consumer_ptr", lambda: 0x4000),
            self.assertLogs("c64cast.audio.audio_rate", level="WARNING") as cm,
        ):
            s.nmi.start(adaptive=False)
        self.assertTrue(any("never started" in m for m in cm.output))
        self.assertEqual(api.nmi_consumer_notes, [])

    def test_stop_reports_underruns(self):
        s = _make()
        s._total_slots = 1
        s._full_underruns = 2
        s._partial_underruns = 5
        with self.assertLogs("c64cast.audio.audio", level="WARNING") as cm:
            s.stop()
        self.assertTrue(any("2 full + 5 partial" in m for m in cm.output))
        # Counters reset for the next run.
        self.assertEqual(s._full_underruns, 0)
        self.assertEqual(s._partial_underruns, 0)

    def test_second_stop_reports_nothing(self):
        # stop() runs at scene teardown and again at session teardown. The
        # second call has cleared counters, so an unconditional summary would
        # follow the real numbers with a flat contradiction of them.
        s = _make()
        s._total_slots = 1
        s._full_underruns = 3
        with self.assertLogs("c64cast.audio.audio", level="WARNING"):
            s.stop()
        with self.assertNoLogs("c64cast.audio.audio", level="INFO"):
            s.stop()

    def test_stop_swallows_teardown_write_errors(self):
        s = _make()
        api = cast(Any, s.api)
        write_regs = api.write_regs
        calls = 0

        def boom(*a: Any, **k: Any) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("teardown write failed")
            write_regs(*a, **k)

        api.write_regs = boom
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s.stop()  # must not raise
        self.assertEqual(api.memories["D418"], "00")
        self.assertIn(f"{VECTORS.NMI:04X}", api.regs)

    def test_stop_orders_the_nmi_vector_then_the_mute_then_the_bias_release(self):
        # Both positions are load-bearing; see stop()'s order comment.
        s = _make(digi_boost=True)
        s.running = True
        s.stop()
        api = cast(Any, s.api)
        gates = {f"{SID.voice_base(v) + SID.OFF_CONTROL:04X}" for v in range(SID.N_VOICES)}
        watched = (f"{VECTORS.NMI:04X}", "D418", *gates)
        order = [op[1] for op in api.ops if op[1] in watched]
        self.assertEqual(order, [f"{VECTORS.NMI:04X}", "D418", *sorted(gates)], f"ops={api.ops}")

    def test_stop_drains_leftover_queue(self):
        s = _make()
        s.q.put(b"\x07\x07")
        s._queued_samples = 2
        s.stop()
        self.assertTrue(s.q.empty())
        self.assertEqual(s._queued_samples, 0)

    def test_stop_swallows_mic_close_errors(self):
        s = _make()

        class _BadStream:
            def __init__(self) -> None:
                self.closed = False

            def stop(self):
                raise RuntimeError("mic stop failed")

            def close(self):
                self.closed = True
                raise RuntimeError("mic close failed")

        stream = _BadStream()
        s.mic_stream = cast(Any, stream)
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s.stop()  # must not raise
        self.assertTrue(stream.closed)
        self.assertIsNone(s.mic_stream)

    def test_disarm_reu_pump_swallows_errors(self):
        s = _make()
        s._reu_pump_armed = True

        def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("vector restore failed")

        cast(Any, s).api.write_regs = boom
        with self.assertLogs("c64cast.audio.audio", level="ERROR"):
            s._disarm_reu_pump()  # must not raise
        self.assertFalse(s._reu_pump_armed)
        # The latch restore sits behind the failing vector restore.
        self.assertIn(f"{CIA1.TIMER_A_LO:04X}", cast(Any, s.api).memories)

    def test_disarm_reu_pump_noop_when_unarmed(self):
        s = _make()
        s._reu_pump_armed = False
        s._disarm_reu_pump()  # early-return path, no writes
        self.assertEqual(len(cast(Any, s.api).ops), 0)

    def test_close_delegates_to_stop(self):
        s = _make()
        s.start_for_external_source()
        s.close()
        self.assertFalse(s.running)
        self.assertIsNone(s._worker_thread)

    def test_stop_disables_digi_boost(self):
        s = _make(digi_boost=True)
        s.start_for_external_source()
        s.stop()
        api = cast(Any, s.api)
        # Gate-off control byte written for every voice during teardown.
        for v in range(SID.N_VOICES):
            ctrl = f"{SID.voice_base(v) + SID.OFF_CONTROL:04X}"
            self.assertEqual(api.memories.get(ctrl), "40")


class _StuckThread:
    """A worker thread that survives a bounded join (the stalled-link case)."""

    name = "audio-worker"

    def join(self, timeout: float | None = None) -> None:
        return None

    def is_alive(self) -> bool:
        return True


class StopWorkerJoinTest(unittest.TestCase):
    """stop() joins the worker with a bounded timeout, so it has to say
    something when the worker is still alive afterwards: the counters cleared
    right below the join are still being mutated, and nothing else in the
    process reports it."""

    def test_warns_when_the_worker_outlives_the_join(self):
        s = _make()
        s.running = True
        s._worker_thread = cast(Any, _StuckThread())
        with self.assertLogs("c64cast.audio.audio", level="WARNING") as cm:
            s.stop()
        self.assertTrue(any("did not exit within" in m for m in cm.output), cm.output)
        self.assertGreater(WORKER_JOIN_TIMEOUT_S, 0.0)
        self.assertIsNone(s._worker_thread)

    def test_an_orphan_stays_fenced_out_by_a_start_that_starts_no_worker(self):
        # The REU-pump and listen-only starts set running back to True without
        # a _start_worker, so only stop() can retire the orphan's generation.
        s = _make_worker_streamer()
        for _ in range(PREBUFFER_CHUNKS + 4):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        api = cast(Any, s.api)
        real_write = api.write_memory_file
        parked = threading.Event()
        release = threading.Event()
        after_release: list[str] = []

        def parking_write(addr, data):  # type: ignore[no-untyped-def]
            if not parked.is_set():
                parked.set()
                release.wait(2.0)
                return
            after_release.append(addr)
            real_write(addr, data)

        api.write_memory_file = parking_write
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        s._worker_thread = t
        t.start()
        try:
            self.assertTrue(parked.wait(2.0), "worker never reached its first write")
            with (
                mock.patch.object(audio_mod, "WORKER_JOIN_TIMEOUT_S", 0.05),
                self.assertLogs("c64cast.audio.audio", level="WARNING") as cm,
            ):
                s.stop()
            self.assertTrue(any("did not exit within" in m for m in cm.output), cm.output)
            s.running = True  # what start_for_reu_staged and start_listen do
            release.set()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "orphan kept running under the next start")
        finally:
            release.set()
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(after_release, [], "orphan wrote into the next session's ring")

    def test_silent_when_the_worker_exited(self):
        s = _make()
        s.running = True
        s._worker_thread = cast(
            Any, SimpleNamespace(join=lambda timeout: None, is_alive=lambda: False)
        )
        with mock.patch.object(audio_mod.log, "warning") as warn:
            s.stop()
        warn.assert_not_called()


class DacBiasTeardownTest(unittest.TestCase):
    """stop() releases the SID gates whichever DAC bias set them: digi-boost,
    or the Mahoney environment a dac_curve installs (the default "auto"
    resolves to one), and only after the $D418 mute."""

    @staticmethod
    def _controls() -> list[str]:
        return [f"{SID.voice_base(v) + SID.OFF_CONTROL:04X}" for v in range(SID.N_VOICES)]

    def test_a_curve_only_stop_releases_every_gate_after_the_mute(self):
        s = _make(dac_table=bytes(range(256)))
        s.stop()
        ops = cast(Any, s.api).ops
        mute = ops.index(("write_memory", "D418", "00"))
        for ctrl in self._controls():
            self.assertGreater(ops.index(("write_memory", ctrl, "40")), mute)  # SID_GATE_OFF

    def test_a_linear_stop_leaves_the_voices_alone(self):
        s = _make()
        s.stop()
        controls = set(self._controls())
        written = [o for o in cast(Any, s.api).ops if o[0] == "write_memory" and o[1] in controls]
        self.assertEqual(written, [])


class ReuPumpDisarmTest(unittest.TestCase):
    def test_disarm_puts_back_the_kernal_cia1_latch_and_flushes_after_it(self):
        s = _make(system="NTSC")
        s._reu_pump_armed = True
        api = cast(Any, s.api)
        api.flush = lambda timeout=5.0: api.ops.append(("flush",))
        s._disarm_reu_pump()
        latch = kernal_cia1_latch("NTSC")
        self.assertEqual(api.memories["DC04"], f"{latch & 0xFF:02X}{latch >> 8:02X}")
        latch_write = max(i for i, o in enumerate(api.ops) if o[:2] == ("write_memory", "DC04"))
        self.assertIn(("flush",), api.ops[latch_write:])


class RingLeadArmedPumpTest(unittest.TestCase):
    def test_an_armed_reu_pump_reports_no_host_ring_lead(self):
        # The C64 pump fills the ring itself; the host-DMA landed count says
        # nothing about what is ahead of R there.
        s = _make()
        s._pushed_count = 8000
        s._queued_samples = 0
        self.assertGreater(s.ring_lead_seconds(), 0.0)
        s._reu_pump_armed = True
        self.assertEqual(s.ring_lead_seconds(), 0.0)


class SpliceFillLandedTest(unittest.TestCase):
    def test_a_splice_fill_lands_as_pad(self):
        # The NEUTRAL fill a splice writes over the chunk it discarded is in
        # the ring like any chunk: counted landed, all of it pad, or the DAC
        # clock and the next splice's anchor fall a chunk behind.
        s = _make_worker_streamer()
        for _ in range(PREBUFFER_CHUNKS + 4):
            s.q.put(bytes([3] * 32))
            s._queued_samples += 32
        spliced: list[bool] = []

        def splicing_pace(*args):  # type: ignore[no-untyped-def]
            if not spliced:
                spliced.append(True)
                s._flush_epoch += 1  # a flush() lands after the hand-off
            return 0.001

        s.servo.next_pace_increment = splicing_pace  # type: ignore[method-assign]
        real_fill = s._neutral_fill_ring
        real_landed = s._note_ring_landed
        fills: list[tuple[int, int]] = []
        landed: list[tuple[int, int]] = []

        def spying_fill(addr: int, n: int) -> None:
            real_fill(addr, n)
            fills.append((len(landed), n))

        def spying_landed(generation: int, n: int, pad: int) -> None:
            landed.append((n, pad))
            real_landed(generation, n, pad)
            if fills:
                s.running = False

        s._neutral_fill_ring = spying_fill  # type: ignore[method-assign]
        s._note_ring_landed = spying_landed  # type: ignore[method-assign]
        s.running = True
        t = threading.Thread(target=s._worker, args=(s._worker_generation,), daemon=True)
        t.start()
        try:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "worker kept running")
        finally:
            s.running = False
            t.join(timeout=1.0)
        self.assertEqual(len(fills), 1, "never reached the splice fill")
        at, n = fills[0]
        self.assertEqual(landed[at : at + 1], [(n, n)])


class DacEncodeLockTest(unittest.TestCase):
    def test_the_dither_generator_is_drawn_from_under_its_lock(self):
        s = _make()
        held: list[bool] = []

        def spying_encode(*args, **kwargs):  # type: ignore[no-untyped-def]
            held.append(s._dither_lock.locked())
            return encode_floats_to_dac(*args, **kwargs)

        with mock.patch.object(audio_mod, "encode_floats_to_dac", spying_encode):
            s._encode_dac(np.zeros(16, dtype=np.float32))
        self.assertEqual(held, [True])


if __name__ == "__main__":
    unittest.main()
