"""Unit tests for the Ultimate Audio FPGA PCM sampler (c64cast/audio/sampler.py) and
its config/provisioning integration. No hardware: a recording fake backend
stands in for the U64, and the hw_provision REST queries are mocked."""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any, cast
from unittest import mock

import numpy as np
import requests
from _fakes import quiet_logging

from c64cast.app import config as cfgmod
from c64cast.app import scene_factory
from c64cast.audio import sampler as s
from c64cast.hw import hw_provision


class PureHelperTest(unittest.TestCase):
    def test_the_mapped_io_page_matches_the_channel_register_files(self):
        # ULTIMATE_AUDIO.IO_BASE/IO_END is what the SID planners and the PSID
        # header decoder refuse to place a chip on (hw.c64.RESERVED_IO_WINDOWS), so
        # it must stay the range this register spec occupies: the firmware switch
        # is "Map Ultimate Audio $DF20-DFFF" and seven 32-byte files fill it.
        from c64cast.hw.c64 import ULTIMATE_AUDIO

        self.assertEqual(s.SAMPLER_IO_BASE, ULTIMATE_AUDIO.IO_BASE)
        self.assertEqual(
            s.SAMPLER_IO_BASE + s.SAMPLER_NUM_CHANNELS * s.SAMPLER_CHANNEL_STRIDE - 1,
            ULTIMATE_AUDIO.IO_END,
        )
        self.assertEqual(s.channel_base(s.SAMPLER_NUM_CHANNELS - 1), 0xDFE0)

    def test_divider_table_matches_doc(self):
        # round(6.25 MHz / rate); 44100 -> 142 is the documented value.
        self.assertEqual(s.divider_for_rate(44100), 142)
        self.assertEqual(s.divider_for_rate(48000), 130)
        self.assertEqual(s.divider_for_rate(8000), 781)
        self.assertEqual(s.divider_for_rate(16000), 391)

    def test_divider_rejects_nonpositive(self):
        with self.assertRaises(ValueError):
            s.divider_for_rate(0)

    def test_actual_rate_roundtrips(self):
        div = s.divider_for_rate(44100)
        self.assertAlmostEqual(s.actual_rate_for_divider(div), 6_250_000 / 142, places=2)

    def test_ref_clock_calibration(self):
        # A per-unit calibrated reference clock shifts BOTH the divider and the
        # actual rate together, keeping the resample target matched to what the
        # FPGA clocks out. A lower ref picks a smaller divider (audio sped up).
        ref = 6_120_000
        div = s.divider_for_rate(44100, ref)
        self.assertEqual(div, 139)  # round(6_120_000 / 44100)
        self.assertAlmostEqual(s.actual_rate_for_divider(div, ref), ref / 139, places=2)
        # Default arg still the nominal design value.
        self.assertEqual(s.divider_for_rate(44100), s.divider_for_rate(44100, 6_250_000))

    def test_bytes_per_sample(self):
        self.assertEqual(s.bytes_per_sample(8), 1)
        self.assertEqual(s.bytes_per_sample(16), 2)
        with self.assertRaises(ValueError):
            s.bytes_per_sample(24)

    def test_pack_pcm_8bit_is_signed(self):
        arr = np.array([0, 32767, -32768, 256, -256], dtype=np.int16)
        out = np.frombuffer(s.pack_pcm(arr, 8), dtype=np.int8)
        self.assertEqual(list(out), [0, 127, -128, 1, -1])

    def test_pack_pcm_16bit_is_le(self):
        self.assertEqual(list(s.pack_pcm(np.array([1, -1], dtype=np.int16), 16)), [1, 0, 255, 255])

    def test_pack_pcm_rejects_bad_bits(self):
        with self.assertRaises(ValueError):
            s.pack_pcm(np.array([0], dtype=np.int16), 12)

    def test_control_byte_bits(self):
        self.assertEqual(s.control_byte(gate=True, repeat=True, bits=16), 0x13)
        self.assertEqual(s.control_byte(gate=True, bits=8), 0x01)
        self.assertEqual(s.control_byte(gate=False, repeat=True, bits=8), 0x02)
        self.assertEqual(s.control_byte(gate=True, interrupt=True, bits=16), 0x15)

    def test_channel_base(self):
        self.assertEqual(s.channel_base(0), 0xDF20)
        self.assertEqual(s.channel_base(1), 0xDF40)
        self.assertEqual(s.channel_base(6), 0xDFE0)
        with self.assertRaises(ValueError):
            s.channel_base(7)

    def test_channel_register_writes_layout(self):
        writes = dict(
            s.channel_register_writes(
                reu_offset=0x200000,
                length=0x100000,
                divider=142,
                volume=63,
                pan=7,
                repeat=True,
                repeat_a=0,
                repeat_b=0x100000,
            )
        )
        # Start address = $01000000 + REU offset, big-endian.
        self.assertEqual(writes[s.REG_START], [0x01, 0x20, 0x00, 0x00])
        self.assertEqual(writes[s.REG_LENGTH], [0x10, 0x00, 0x00])
        self.assertEqual(writes[s.REG_RATE], [0x00, 0x8E])  # 142
        self.assertEqual(writes[s.REG_VOLUME], [0x3F])
        self.assertEqual(writes[s.REG_PAN], [0x07])
        self.assertEqual(writes[s.REG_REPEAT_A], [0x00, 0x00, 0x00])
        self.assertEqual(writes[s.REG_REPEAT_B], [0x10, 0x00, 0x00])

    def test_register_writes_omit_repeat_when_off(self):
        writes = dict(
            s.channel_register_writes(
                reu_offset=0,
                length=100,
                divider=142,
                volume=63,
                pan=7,
                repeat=False,
                repeat_a=0,
                repeat_b=0,
            )
        )
        self.assertNotIn(s.REG_REPEAT_A, writes)
        self.assertNotIn(s.REG_REPEAT_B, writes)


class _FakeBackend:
    """Records the writes a UltimateAudioSampler issues (reu_write / write_regs /
    write_memory / flush). No socket, no REST."""

    def __init__(self) -> None:
        self.reu_writes: list[tuple[int, int]] = []  # (offset, length)
        self.reg_writes: list[tuple[str, tuple[int, ...]]] = []
        self.mem_writes: list[tuple[str, str]] = []
        self.flushes = 0
        self.audible_writes = 0  # REU writes carrying anything but silence

    def reu_write(self, offset: int, data: bytes) -> None:
        self.reu_writes.append((offset, len(data)))
        if any(data):
            self.audible_writes += 1

    def write_regs(self, base_addr: str, *values: int) -> None:
        self.reg_writes.append((base_addr.upper(), values))

    def write_memory(self, address: str, data_hex: str) -> None:
        self.mem_writes.append((address.upper(), data_hex.upper()))

    def flush(self) -> None:
        self.flushes += 1


def _make(api: _FakeBackend, **kw) -> s.UltimateAudioSampler:
    """Build a sampler against the recording fake (cast like the audio tests'
    `cast(Ultimate64API, FakeAPI())` — the fake duck-types the write surface)."""
    return s.UltimateAudioSampler(cast(Any, api), **kw)


class StreamerTest(unittest.TestCase):
    def test_init_resolves_rate_and_ring(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16, ring_size=4097)
        self.assertEqual(smp.bps, 2)
        self.assertEqual(smp._divider, 142)
        self.assertEqual(smp.sample_rate, round(6_250_000 / 142))
        # Ring frame-aligned (even for 16-bit).
        self.assertEqual(smp.ring_size % 2, 0)
        self.assertTrue(smp.is_sampler)

    def test_write_wrapped_splits_at_ring_boundary(self):
        api = _FakeBackend()
        smp = _make(api, sample_rate=44100, bits=8, ring_base=0x200000, ring_size=16)
        smp._write_wrapped(10, b"ABCDEF")  # 6 bytes from pos 10 in a 16-byte ring
        # 6 bytes at base+10, then 0 wrap... 10+6=16 exactly, no wrap.
        self.assertEqual(api.reu_writes, [(0x200000 + 10, 6)])
        api.reu_writes.clear()
        smp._write_wrapped(12, b"ABCDEF")  # crosses: 4 at +12, 2 at +0
        self.assertEqual(api.reu_writes, [(0x200000 + 12, 4), (0x200000, 2)])

    def test_position_seconds_zero_before_start(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        self.assertEqual(smp.position_seconds(), 0.0)

    def test_position_seconds_tracks_wallclock(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp._running = True
        smp._gate_time = time.monotonic() - 2.0
        self.assertAlmostEqual(smp.position_seconds(), 2.0, delta=0.2)

    def test_position_clamps_after_eof(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp._running = True
        smp._gate_time = time.monotonic() - 100.0
        smp._pushed_samples = smp.sample_rate  # ~1 s of audio pushed
        smp.mark_eof()
        self.assertAlmostEqual(smp.position_seconds(), 1.0, delta=0.1)

    def test_read_consumed_bytes_is_frame_aligned(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp._running = True
        smp._gate_time = time.monotonic() - 1.0
        consumed = smp._read_consumed_bytes()
        self.assertEqual(consumed % smp.bps, 0)
        self.assertGreater(consumed, 0)

    def test_start_programs_the_divider_of_the_configured_clock(self):
        # The resample target (sample_rate) and the programmed divider must come
        # from the same clock; at the shipped 6.16 MHz, 44.1 kHz is divider 140,
        # where the 6.25 MHz design value would program 142 and play 1.4 % slow.
        api = _FakeBackend()
        smp = _make(api, sample_rate=44100, bits=16, ref_clock_hz=s.SAMPLER_REF_CLOCK_DEFAULT)
        smp.start(prebuffer_timeout=0.01)
        with quiet_logging():  # the idle writer's pads are not the subject
            smp.stop()
        self.assertEqual(smp.sample_rate, 44000)
        rate_reg = f"{s.SAMPLER_IO_BASE + s.REG_RATE:04X}"
        self.assertIn((rate_reg, (0, 140)), api.reg_writes)

    def test_start_prefills_and_gates_then_stop_gates_off(self):
        api = _FakeBackend()
        smp = _make(
            api, sample_rate=44100, bits=16, ring_base=0x200000, ring_size=8192, lead_seconds=0.01
        )
        # Prime the queue so the prebuffer returns immediately (no 2 s block).
        smp.push_samples(np.zeros(2048, dtype=np.int16))
        smp.start(prebuffer_timeout=0.1)
        try:
            # Prefill wrote the ring (NEUTRAL) before gating.
            self.assertTrue(api.reu_writes)
            # Control register at $DF20 was written with gate+repeat+mode16 (0x13).
            gate_writes = [v for a, v in api.mem_writes if a == "DF20"]
            self.assertIn("13", gate_writes)
            self.assertTrue(smp._running)
        finally:
            smp.stop()
        # Gate-off wrote $DF20 = 00.
        self.assertEqual(api.mem_writes[-1], ("DF20", "00"))
        self.assertFalse(smp._running)

    def test_prebuffer_target_decoupled_from_lead(self):
        # The runtime lead (1.0 s default) is deeper than the startup prebuffer
        # (0.5 s), so playback starts promptly while the writer keeps a cushion
        # deep enough for a 4K clip's decode stalls.
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        self.assertLess(smp._prebuffer_target, smp._lead_target)
        self.assertAlmostEqual(smp._prebuffer_target / smp._lead_target, 0.5, delta=0.05)

    def test_prebuffer_clamped_to_lead_target(self):
        # A prebuffer configured larger than the lead can't exceed the runtime
        # depth (the writer never targets less than it seeds).
        smp = _make(
            _FakeBackend(), sample_rate=44100, bits=16, lead_seconds=0.2, prebuffer_seconds=1.0
        )
        self.assertEqual(smp._prebuffer_target, smp._lead_target)

    def test_get_recent_samples_returns_pushed(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp.push_samples(np.ones(100, dtype=np.int16) * 16384)
        recent = smp.get_recent_samples(50)
        self.assertEqual(recent.shape, (50,))
        self.assertTrue(np.all(recent > 0.4))

    def test_set_pre_emphasis_is_noop(self):
        # Scene.setup calls this on the audio object regardless of backend;
        # on the sampler it must neither raise nor touch the hardware.
        api = _FakeBackend()
        smp = _make(api, sample_rate=44100, bits=16)
        smp.set_pre_emphasis(0.9)
        self.assertEqual(
            (api.reu_writes, api.reg_writes, api.mem_writes, api.flushes), ([], [], [], 0)
        )


def _run_steps(test: unittest.TestCase, smp: s.UltimateAudioSampler) -> None:
    """Writer steps on the test's thread until a patched queue ends the run by
    clearing _running; bounded, so a writer that stops asking for data fails
    the test instead of spinning in its sleep branch."""
    for _ in range(200):
        if not smp._running:
            return
        smp._writer_step(smp._writer_gen)
    test.fail("the writer stopped draining the queue")


class SamplerReuseTest(unittest.TestCase):
    """A scene builds its sampler once and a looping playlist sets the scene up
    again, so a second activation of the same object has to play."""

    TONE = np.full(1024, 8000, dtype=np.int16)

    def _sampler(self, api: _FakeBackend) -> s.UltimateAudioSampler:
        smp = _make(api, sample_rate=8000, bits=8, lead_seconds=0.2, prebuffer_seconds=0.05)
        self.addCleanup(smp.stop)
        return smp

    def _lap(self, smp: s.UltimateAudioSampler, api: _FakeBackend, *, arm: bool = True) -> int:
        """One activation the way the scenes drive it: arm, let the producer
        push, start, play a moment, stop. Returns the audible REU writes."""
        if arm:
            smp.arm()
        for _ in range(4):
            smp.push_samples(self.TONE)
        api.audible_writes = 0
        smp.start(prebuffer_timeout=0.1)
        time.sleep(0.1)
        smp.stop()
        return api.audible_writes

    def test_a_second_activation_plays(self):
        api = _FakeBackend()
        smp = self._sampler(api)
        self.assertGreater(self._lap(smp, api), 0)
        self.assertGreater(self._lap(smp, api), 0, "the second activation streamed only silence")

    def test_start_arms_a_stopped_sampler_its_caller_did_not_arm(self):
        api = _FakeBackend()
        smp = self._sampler(api)
        self._lap(smp, api)
        api.audible_writes = 0
        smp.start(prebuffer_timeout=0.01)
        for _ in range(4):  # more than the start-up slot the reader passes
            smp.push_samples(self.TONE)
        deadline = time.monotonic() + 2.0
        while api.audible_writes == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(api.audible_writes, 0, "start() left the stop latch set")

    def test_arm_clears_what_the_last_activation_left(self):
        api = _FakeBackend()
        smp = self._sampler(api)
        self._lap(smp, api)
        smp.mark_eof()
        smp._output_silenced = True
        smp._underrun_pads = 3
        smp._q.put((0, b"stale"))
        epoch = smp._flush_epoch
        smp.arm()
        self.assertFalse(smp._stopped)
        self.assertFalse(smp._eof)
        self.assertFalse(smp._output_silenced)
        self.assertEqual(
            (smp._pushed_samples, smp._written, smp._content_pos, smp._underrun_pads), (0, 0, 0, 0)
        )
        self.assertEqual((smp._lead_min, smp._lead_max), (None, None))
        self.assertTrue(smp._q.empty())
        self.assertGreater(smp._flush_epoch, epoch)
        self.assertFalse(np.any(smp.get_recent_samples(s.SAMPLE_TAP_SIZE)))

    def test_an_analyzer_failing_again_after_arm_is_logged_again(self):
        def broken(_floats: np.ndarray) -> None:
            raise ValueError("analyzer broke")

        smp = self._sampler(_FakeBackend())
        for _lap in range(2):
            smp.arm()
            smp.analysis_sink = broken  # the scene reinstalls it every activation
            with self.assertLogs("c64cast.audio.sampler", "ERROR") as logs:
                smp.push_samples(self.TONE)
            self.assertIn("analysis sink failed", logs.output[0])
            self.assertIsNone(smp.analysis_sink)
            smp.stop()


class _WedgingBackend(_FakeBackend):
    """A backend whose next writer-thread REU write blocks until released,
    the way a REUWRITE does on a stalled link."""

    def __init__(self) -> None:
        super().__init__()
        self.wedge_next = False
        self.wedged = threading.Event()
        self.release = threading.Event()

    def reu_write(self, offset: int, data: bytes) -> None:
        if self.wedge_next and threading.current_thread().name == "uaudio-writer":
            self.wedge_next = False
            self.wedged.set()
            self.release.wait(5.0)
        super().reu_write(offset, data)


class SamplerWriterSurvivorTest(unittest.TestCase):
    """A writer that outlives stop()'s bounded join must not run beside the
    next activation's writer."""

    def _wedged_and_stopped(self) -> tuple[s.UltimateAudioSampler, _WedgingBackend]:
        api = _WedgingBackend()
        smp = _make(api, sample_rate=8000, bits=8, lead_seconds=0.2, prebuffer_seconds=0.05)
        self.addCleanup(smp.stop)
        self.addCleanup(api.release.set)
        smp.start(prebuffer_timeout=0.01)
        assert smp._writer is not None
        smp._writer._join_timeout = 0.05
        api.wedge_next = True
        smp.push_samples(np.full(256, 8000, dtype=np.int16))
        self.assertTrue(api.wedged.wait(2.0), "the writer never reached its REU write")
        # "c64cast", not the poll thread alone: whether the wedged write was an
        # underrun pad (which this stop() also reports) is timing.
        with self.assertLogs("c64cast", level="WARNING") as logs:
            smp.stop()
        self.assertTrue(any("did not stop" in m for m in logs.output), logs.output)
        # Runs first among the cleanups (LIFO): the cleanup stop() joins the
        # released survivor with a real bound, not the 50 ms that made it a
        # survivor, so a slow worker cannot log "did not stop" between the dots.
        self.addCleanup(setattr, smp._writer, "_join_timeout", 2.0)
        return smp, api

    def test_a_surviving_writer_stays_fenced(self):
        smp, api = self._wedged_and_stopped()
        self.assertIsNotNone(smp._writer, "the survivor was forgotten")
        with self.assertRaisesRegex(RuntimeError, "previous writer"):
            smp.arm()
        with self.assertRaisesRegex(RuntimeError, "previous writer"):
            smp.start(prebuffer_timeout=0.01)

    def test_a_released_survivor_writes_nothing_more_and_frees_the_fence(self):
        smp, api = self._wedged_and_stopped()
        writer = smp._writer
        assert writer is not None
        api.release.set()
        deadline = time.monotonic() + 2.0
        while writer.is_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(writer.is_running())
        smp.arm()  # the fence lifts once the survivor has exited
        self.assertIsNone(smp._writer)

    def test_a_generation_retired_while_waiting_on_the_queue_writes_nothing(self):
        # stop() and the next start() both land while the writer sits in
        # q.get(); the check under _io_lock is what keeps its chunk out.
        api = _FakeBackend()
        smp = _make(api, sample_rate=8000, bits=8)
        smp._running = True
        gen = smp._writer_gen
        real_get = smp._q.get

        def get_across_a_restart(*a: Any, **kw: Any) -> Any:
            smp._writer_gen += 2  # a stop() and a start() happened meanwhile
            smp._q.get = real_get  # type: ignore[method-assign]
            return smp._flush_epoch, b"\x40" * 64

        smp._q.get = get_across_a_restart  # type: ignore[method-assign]
        smp._read_consumed_bytes = lambda: 0  # type: ignore[method-assign]
        smp._writer_loop(gen)
        self.assertEqual(api.reu_writes, [])
        self.assertEqual(smp._written, 0)

    def test_start_refuses_a_running_sampler(self):
        api = _FakeBackend()
        smp = _make(api, sample_rate=8000, bits=8, prebuffer_seconds=0.01)

        def quiet_stop() -> None:
            # Whether the idle writer padded first is timing; the report is
            # asserted by test_stop_reports_underruns_once.
            with quiet_logging():
                smp.stop()

        self.addCleanup(quiet_stop)
        smp.start(prebuffer_timeout=0.01)
        with self.assertRaisesRegex(RuntimeError, "already started"):
            smp.start(prebuffer_timeout=0.01)

    def test_stop_reports_underruns_once(self):
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8)
        smp._underrun_pads = 2
        with self.assertLogs("c64cast.audio.sampler", level="WARNING") as logs:
            smp.stop()
        self.assertIn("2 underrun pads", logs.output[0])
        with self.assertNoLogs("c64cast.audio.sampler", level="WARNING"):
            smp.stop()


class _FailingBackend(_FakeBackend):
    """REU writes from the writer thread raise while ``failures`` is nonzero
    (counted down per raise; -1 = forever), the way a write fails when the
    link is still down after socket_dma's one redial."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def reu_write(self, offset: int, data: bytes) -> None:
        if self.failures and threading.current_thread().name == "uaudio-writer":
            if self.failures > 0:
                self.failures -= 1
            raise ConnectionError("send failed again after reconnect")
        super().reu_write(offset, data)


class SamplerWriterFailureTest(unittest.TestCase):
    TONE = np.full(256, 8000, dtype=np.int16)

    def _started(self, api: _FakeBackend) -> s.UltimateAudioSampler:
        smp = _make(api, sample_rate=8000, bits=8, lead_seconds=0.2, prebuffer_seconds=0.01)

        def quiet_stop() -> None:
            with quiet_logging():  # idle-writer pads are timing, not the subject
                smp.stop()

        self.addCleanup(quiet_stop)
        smp.start(prebuffer_timeout=0.01)
        return smp

    def _wait(self, cond: Any, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while not cond() and time.monotonic() < deadline:
            time.sleep(0.01)
        return bool(cond())

    def test_a_failed_write_is_retried_not_fatal(self):
        api = _FailingBackend(failures=2)
        with self.assertLogs("c64cast.audio.sampler", level="WARNING") as logs:
            smp = self._started(api)
            api.audible_writes = 0
            for _ in range(16):
                smp.push_samples(self.TONE)
            self.assertTrue(self._wait(lambda: api.audible_writes > 0), "the writer died")
        self.assertTrue(any("ring write failed" in m for m in logs.output), logs.output)
        assert smp._writer is not None
        self.assertTrue(smp._writer.is_running())

    def test_a_dead_link_gates_the_channel_off_and_stops_parking_the_producer(self):
        api = _FailingBackend(failures=-1)
        with mock.patch.object(s, "WRITER_GIVE_UP_S", 0.1):
            with self.assertLogs("c64cast.audio.sampler", level="ERROR"):
                smp = self._started(api)
                self.assertTrue(self._wait(lambda: smp._failed), "the writer never gave up")
        self.assertEqual(api.mem_writes[-1], ("DF20", "00"), "the channel still loops stale audio")
        smp._q = s.queue.Queue(maxsize=1)
        smp._q.put((smp._flush_epoch, b""))
        t0 = time.monotonic()
        smp.push_samples(self.TONE)  # a full queue nothing drains
        self.assertLess(time.monotonic() - t0, 0.05, "the producer parked on a dead sampler")

    def test_a_producer_parked_when_the_writer_gives_up_is_released(self):
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8, queue_max_chunks=1)
        smp._q.put((smp._flush_epoch, b""))
        t = threading.Thread(target=smp.push_samples, args=(self.TONE,))
        self.addCleanup(t.join, 1.0)
        self.addCleanup(setattr, smp, "_stopped", True)
        t.start()
        time.sleep(0.02)  # parked in put(timeout=0.1)
        smp._failed = True
        t.join(timeout=0.5)
        self.assertFalse(t.is_alive(), "the producer stays parked on a sampler that gave up")

    def test_a_write_head_the_reader_passed_skips_ahead_of_it(self):
        api = _FakeBackend()
        smp = _make(api, sample_rate=2000, bits=8, ring_base=0x200000, ring_size=4096)
        smp._running = True
        consumed = 1000
        smp._read_consumed_bytes = lambda: consumed  # type: ignore[method-assign]
        smp._written = 200  # 800 bytes behind the reader
        margin = smp._flush_margin
        # A chunk whose first 16 bytes' slot is already within the guard.
        smp._content_pos = consumed + margin - 16
        items = [(smp._flush_epoch, b"\x01" * 32)]

        def get(*_a: Any, **_kw: Any) -> Any:
            if not items:
                smp._running = False
                raise s.queue.Empty
            return items.pop(0)

        smp._q.get = get  # type: ignore[method-assign]
        _run_steps(self, smp)
        audible = [w for w in api.reu_writes if w == (0x200000 + consumed + margin, 16)]
        self.assertEqual(len(audible), 1, api.reu_writes)
        self.assertEqual(smp._late_bytes, 16)
        self.assertIn(
            (0x200000 + consumed, margin), api.reu_writes, "the skipped span was not blanked"
        )


class SamplerWriteSizingTest(unittest.TestCase):
    """Write count is the lever on the Ultimate's link, and no single chunk may
    push the write head past the lead target or a write past one slice."""

    def _idle_reader(self, api: _FakeBackend, **kw: Any) -> s.UltimateAudioSampler:
        smp = _make(api, sample_rate=8000, bits=8, **kw)
        smp._running = True
        smp._read_consumed_bytes = lambda: 0  # type: ignore[method-assign]
        # The first writable byte: anything nearer the reader is late.
        self._place(smp, smp._flush_margin)
        return smp

    @staticmethod
    def _place(smp: s.UltimateAudioSampler, pos: int) -> None:
        smp._written = smp._content_pos = pos

    def _drain(self, smp: s.UltimateAudioSampler) -> None:
        """Writer steps until it has nothing left to write for this lead."""
        for _ in range(500):
            if smp._lead_target - smp._written < smp._write_quantum:
                return
            if smp._q.empty() and smp._carry is None:
                return
            if not smp._writer_step(smp._writer_gen) and smp._q.empty():
                return  # what is left is held for a whole quantum

    def test_the_write_quantum_is_the_links_free_payload(self):
        from c64cast.hw.backend import ULTIMATE_PROFILE

        free = ULTIMATE_PROFILE.free_payload_bytes()
        self.assertTrue(2000 <= free <= 2200, free)  # the measured ~2.1 KB knee
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        self.assertEqual(smp._write_quantum, free - free % 2)

    def test_tiny_chunks_are_coalesced(self):
        # 2.5 ms Opus frames: 20 bytes each at 8 kHz/8-bit.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=1.0, queue_max_chunks=512)
        for _ in range(300):
            smp._q.put((smp._flush_epoch, b"\x01" * 20))
        self._drain(smp)
        writes = api.audible_writes
        self.assertGreater(writes, 0)
        self.assertLessEqual(writes, -(-6000 // smp._write_quantum), api.reu_writes)

    def test_a_real_time_producer_is_coalesced_above_the_low_watermark(self):
        # A live stream never builds a queue backlog: each pass finds one
        # frame. Above the watermark the writer waits for a whole quantum.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=1.0)
        self._place(smp, smp._lead_panic + smp.bps)
        frames = 0
        while (frames + 1) * 20 < smp._write_quantum:
            smp._q.put((smp._flush_epoch, b"\x01" * 20))
            frames += 1
            self.assertFalse(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [])
        smp._q.put((smp._flush_epoch, b"\x01" * 20))
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(
            api.reu_writes, [(smp.ring_base + smp._lead_panic + smp.bps, 20 * (frames + 1))]
        )

    def test_at_the_low_watermark_a_partial_quantum_is_written(self):
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=1.0)
        self._place(smp, smp._lead_panic)
        smp._q.put((smp._flush_epoch, b"\x01" * 20))
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [(smp.ring_base + smp._lead_panic, 20)])

    def test_an_oversized_chunk_is_split_and_stops_at_the_lead_target(self):
        api = _FakeBackend()
        smp = self._idle_reader(api, ring_size=0x20000, lead_seconds=4.0)
        big = b"\x01" * (3 * s.REU_WRITE_SLICE)
        smp._q.put((smp._flush_epoch, big))
        self._drain(smp)
        self.assertLessEqual(max(n for _, n in api.reu_writes), s.REU_WRITE_SLICE)
        self.assertLessEqual(smp._written, smp._lead_target)
        assert smp._carry is not None
        self.assertEqual(
            smp._written - smp._flush_margin + len(smp._carry[1]), len(big), "samples were dropped"
        )

    def test_an_oversized_prebuffer_is_written_no_deeper_than_the_lead(self):
        api = _FakeBackend()
        smp = _make(api, sample_rate=8000, bits=8, ring_size=0x8000, lead_seconds=1.0)
        self.addCleanup(smp.stop)
        big = np.full(0x20000, 8000, dtype=np.int16)  # 128 KiB of 8-bit PCM, 4 rings
        smp.push_samples(big)
        # The read head held at the gate: a reader that moved a quantum before
        # the assertions ran would let the writer take more of the carry.
        smp._read_consumed_bytes = lambda: 0  # type: ignore[method-assign]
        smp.start(prebuffer_timeout=0.05)
        self.assertEqual(smp._written, smp._lead_target)
        assert smp._carry is not None
        self.assertEqual(smp._written + len(smp._carry[1]), len(big))

    def test_less_room_than_a_quantum_waits_instead_of_writing_a_sliver(self):
        # In steady state the reader frees a few hundred bytes between passes;
        # writing each sliver is what made 400 writes a second.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=1.0)
        self._place(smp, smp._lead_target - smp._write_quantum // 2)
        smp._q.put((smp._flush_epoch, b"\x01" * 4096))
        self.assertFalse(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [])

    def test_an_empty_queue_pads_only_below_the_low_watermark(self):
        # A pad inserts silence, so a briefly empty queue above the watermark
        # waits for the producer instead.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=1.0)
        self._place(smp, smp._lead_target - smp._write_quantum)
        self.assertGreater(smp._written, smp._lead_panic)
        self.assertFalse(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [])
        self._place(smp, smp._lead_panic)
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(smp._underrun_pads, 1)

    def test_the_lead_never_exceeds_half_the_ring(self):
        # Write-ahead deeper than half the ring could lap the reader.
        smp = _make(
            _FakeBackend(), sample_rate=44100, bits=16, ring_size=0x10000, lead_seconds=10.0
        )
        self.assertEqual(smp._lead_target, 0x8000)

    def test_a_chunk_whose_write_fails_is_retried(self):
        api = _FailingBackend(failures=1)
        smp = self._idle_reader(api)
        smp._q.put((smp._flush_epoch, b"\x01" * 64))
        with mock.patch("threading.current_thread") as current:
            current.return_value.name = "uaudio-writer"
            with self.assertRaises(ConnectionError):
                smp._writer_step(smp._writer_gen)
            smp._writer_step(smp._writer_gen)
        self.assertIn((0x200000 + smp._flush_margin, 64), api.reu_writes)
        self.assertEqual(smp._written, smp._flush_margin + 64)


class SamplerArmedBeforeProducerTest(unittest.TestCase):
    """Both scene setups arm the sampler before their producer can push into
    it; a push into a still-stopped sampler is dropped."""

    def test_video_scene_arms_before_the_demuxer_starts(self):
        from c64cast.scenes.scenes import VideoScene

        audio = mock.MagicMock(spec=s.UltimateAudioSampler)
        audio.sample_rate = 44000
        order = mock.MagicMock()
        order.attach_mock(audio.arm, "arm")
        order.attach_mock(audio.start, "start")
        with (
            mock.patch("c64cast.scenes.scenes.ensure_pyav", return_value=True),
            mock.patch("c64cast.scenes.scenes.AVFileSource") as source_cls,
        ):
            order.attach_mock(source_cls.return_value.start, "source_start")
            scene = VideoScene(
                api=mock.MagicMock(),
                audio=audio,
                display_mode=mock.MagicMock(),
                file="https://stub.invalid/clip.mp4",
                setup_progress=False,
            )
            scene.setup()
        names = [c[0] for c in order.mock_calls if c[0] in ("arm", "source_start", "start")]
        self.assertEqual(names, ["arm", "source_start", "start"])

    def test_video_scene_plays_silent_when_the_sampler_refuses_to_arm(self):
        # The playlist does not catch a setup() raise, so a refusal (the last
        # writer outlived stop()) must not escape and end the whole run.
        from c64cast.scenes.scenes import VideoScene

        audio = mock.MagicMock(spec=s.UltimateAudioSampler)
        audio.sample_rate = 44000
        audio.arm.side_effect = RuntimeError("writer still running")
        with (
            mock.patch("c64cast.scenes.scenes.ensure_pyav", return_value=True),
            mock.patch("c64cast.scenes.scenes.AVFileSource") as source_cls,
        ):
            scene = VideoScene(
                api=mock.MagicMock(),
                audio=audio,
                display_mode=mock.MagicMock(),
                file="https://stub.invalid/clip.mp4",
                setup_progress=False,
            )
            with self.assertLogs("c64cast.scenes.scenes", "ERROR") as logs:
                scene.setup()
            source_cls.return_value.start.assert_called_once_with(audio_push=None)
            audio.start.assert_not_called()
            self.assertIn("playing", logs.output[0])
            self.assertIsNone(scene.audio)
            scene.teardown()
        self.assertIs(scene.audio, audio, "teardown did not restore the set-aside sampler")

    @unittest.skipUnless(
        __import__("c64cast.video.video", fromlist=["ensure_pyav"]).ensure_pyav(),
        "PyAV (video extra) not installed",
    )
    def test_file_source_arms_before_the_decoder_starts(self):
        import os
        import tempfile
        import wave

        from c64cast.audio.audio_source import AudioFileSource

        tune = os.path.join(tempfile.mkdtemp(), "tune.wav")
        with wave.open(tune, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)
        audio = mock.MagicMock(is_sampler=True)
        order: list[str] = []
        audio.arm.side_effect = lambda: order.append("arm")
        source = AudioFileSource(audio, tune, reactive=False)
        with mock.patch.object(
            AudioFileSource, "_start_decode_thread", side_effect=lambda: order.append("decode")
        ):
            source.setup()
        self.assertEqual(order, ["arm", "decode"])


class SamplerFlushTests(unittest.TestCase):
    def _running(self, api: _FakeBackend, *, rate: int = 2000, ring: int = 4096, consumed: int = 0):
        smp = _make(api, sample_rate=rate, bits=8, ring_base=0x200000, ring_size=ring)
        smp._running = True
        smp._read_consumed_bytes = lambda: consumed  # type: ignore[method-assign]
        return smp

    def _margin(self, smp: s.UltimateAudioSampler) -> int:
        return int(s.FLUSH_GUARD_S * smp._actual_rate) * smp.bps

    def test_flush_rewrites_lead_with_neutral(self):
        api = _FakeBackend()
        smp = self._running(api, consumed=100)
        margin = self._margin(smp)
        smp._written = 100 + margin + 500  # 500 bytes of lead past the margin
        api.reu_writes.clear()
        smp.flush()
        # Exactly [consumed+margin, old_written) rewritten, no wrap.
        self.assertEqual(api.reu_writes, [(0x200000 + 100 + margin, 500)])
        self.assertEqual(smp._written, 100 + margin)

    def test_flush_wraps_ring_boundary(self):
        api = _FakeBackend()
        smp = self._running(api, ring=512, consumed=0)
        margin = self._margin(smp)  # 300 at rate 2000
        smp._written = margin + 400  # rewrite region [300, 700) wraps 512
        api.reu_writes.clear()
        smp.flush()
        start = margin % 512
        first = 512 - start
        self.assertEqual(
            api.reu_writes,
            [(0x200000 + start, first), (0x200000, 400 - first)],
        )

    def test_flush_resets_written_and_clears_eof(self):
        smp = self._running(_FakeBackend(), consumed=100)
        smp._written = 100 + self._margin(smp) + 200
        smp._eof = True
        smp.flush()
        self.assertEqual(smp._written, 100 + self._margin(smp))
        self.assertEqual(smp._content_pos, 100 + self._margin(smp))
        self.assertFalse(smp._eof)
        self.assertEqual(smp._flush_epoch, 1)

    def test_ring_lead_is_the_flush_margin(self):
        # The transport anchors a splice at position_seconds() + this; the
        # first post-splice sample lands one margin past the read head.
        smp = self._running(_FakeBackend(), consumed=0)
        self.assertAlmostEqual(
            smp.ring_lead_seconds(), self._margin(smp) / smp.bps / smp._actual_rate, places=9
        )
        self.assertAlmostEqual(smp.ring_lead_seconds(), s.FLUSH_GUARD_S, delta=0.001)

    def test_post_splice_audio_lands_where_the_transport_anchored_it(self):
        # The demuxer takes a while to deliver post-seek audio. Its first
        # sample belongs one ring lead after the splice; whatever arrives after
        # its slot is dropped rather than shifting everything behind it later.
        api = _FakeBackend()
        consumed = [1000]
        smp = _make(api, sample_rate=2000, bits=8, ring_base=0x200000, ring_size=0x4000)
        smp._running = True
        smp._read_consumed_bytes = lambda: consumed[0]  # type: ignore[method-assign]
        smp._written = smp._content_pos = 1000 + 1500  # a 1500-byte lead
        anchor = consumed[0] + round(smp.ring_lead_seconds() * smp._actual_rate) * smp.bps
        smp.flush()
        consumed[0] += 100  # the demuxer's seek latency
        api.reu_writes.clear()
        self._drive_writer(smp, [(smp._flush_epoch, bytes(range(1, 201)))])
        # Sample k of the new stream sits at anchor + k: the first 100 were late.
        self.assertIn((0x200000 + anchor + 100, 100), api.reu_writes, api.reu_writes)
        self.assertEqual(smp._content_pos, anchor + 200)

    def test_an_underrun_pad_is_overwritten_by_the_data_that_follows_it(self):
        api = _FakeBackend()
        smp = _make(api, sample_rate=2000, bits=8, ring_base=0x200000, ring_size=0x4000)
        smp._running = True
        smp._read_consumed_bytes = lambda: 0  # type: ignore[method-assign]
        start = smp._flush_margin
        smp._written = smp._content_pos = start
        self.assertTrue(smp._pad_underrun(smp._writer_gen))
        padded_to = smp._written
        self.assertGreater(padded_to, start)
        self.assertEqual(smp._content_pos, start, "the pad moved the audio timeline")
        api.reu_writes.clear()
        self._drive_writer(smp, [(smp._flush_epoch, b"\x01" * 64)])
        self.assertIn((0x200000 + start, 64), api.reu_writes)
        self.assertEqual(smp._written, padded_to)

    def test_flush_keeps_audio_pushed_after_its_bump(self):
        # The demuxer can apply the seek and push between flush()'s epoch bump
        # and the end of the call; that chunk is the start of the seek target.
        api = _FakeBackend()
        smp = self._running(api, consumed=0)
        stale = smp._flush_epoch
        smp._q.put((stale, b"\x01" * 32))
        smp._q.put((stale + 1, b"\x02" * 32))  # tagged with the epoch flush() bumps to
        smp.flush()
        self.assertEqual(smp._flush_epoch, stale + 1)
        api.reu_writes.clear()
        api.audible_writes = 0
        self._drive_writer(smp, [smp._q.get_nowait(), smp._q.get_nowait()])
        self.assertEqual(
            api.audible_writes, 1, "the post-splice chunk was lost, or the stale one written"
        )

    def test_flush_bumps_the_epoch_without_waiting_for_a_write(self):
        smp = self._running(_FakeBackend(), consumed=0)
        epoch = smp._flush_epoch
        t = threading.Thread(target=smp.flush)
        with smp._io_lock:  # the writer, mid REU write
            t.start()
            deadline = time.monotonic() + 1.0
            while smp._flush_epoch == epoch and time.monotonic() < deadline:
                time.sleep(0.005)
            bumped = smp._flush_epoch != epoch
        t.join(timeout=1.0)
        self.assertTrue(bumped, "flush() waited on the writer before retiring the queue")

    def test_flush_noop_when_not_running(self):
        api = _FakeBackend()
        smp = _make(api, sample_rate=2000, bits=8)
        smp._running = False
        api.reu_writes.clear()
        smp.flush()
        self.assertEqual(api.reu_writes, [])
        self.assertEqual(smp._flush_epoch, 0)

    def test_flush_position_unchanged(self):
        smp = self._running(_FakeBackend(), consumed=100)
        smp._gate_time = time.monotonic() - 3.0
        smp._written = 100 + self._margin(smp) + 50
        before = smp.position_seconds()
        smp.flush()
        self.assertAlmostEqual(smp.position_seconds(), before, delta=0.05)

    def test_lead_below_margin_blanks_skip_region(self):
        api = _FakeBackend()
        smp = self._running(api, consumed=1000)
        margin = self._margin(smp)
        smp._written = 1100  # lead = 100 < margin → new_written > old_written
        api.reu_writes.clear()
        smp.flush()
        # Blanks the [old_written, consumed+margin) lap-stale skip region.
        self.assertEqual(api.reu_writes, [(0x200000 + 1100, (1000 + margin) - 1100)])
        self.assertEqual(smp._written, 1000 + margin)

    def test_push_after_flush_stale_epoch_dropped(self):
        # A push parked in the Full-retry loop when the flush epoch advances must
        # drop its chunk (return before the put) and NOT count it toward
        # _pushed_samples. Keeping the queue full means the put never succeeds, so
        # the loop re-checks the epoch on each Full timeout and bails; with a
        # real flush() that case is test_a_producer_parked_across_a_flush_puts_nothing.
        api = _FakeBackend()
        smp = _make(api, sample_rate=2000, bits=8, queue_max_chunks=1)
        smp._q.put((0, b"x"))  # fill and keep full

        def push():
            smp.push_samples(np.zeros(50, dtype=np.int16))

        t = threading.Thread(target=push)

        def release() -> None:
            # A pusher that never saw the epoch move would otherwise spin in
            # the Full-retry loop past the end of the test.
            smp._stopped = True
            t.join(timeout=1.0)

        self.addCleanup(release)
        t.start()
        time.sleep(0.02)  # let it park in the Full-retry loop
        smp._flush_epoch += 1  # a concurrent flush bumped the epoch
        t.join(timeout=1.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(smp._pushed_samples, 0)  # dropped, not counted

    def _drive_writer(
        self, smp: s.UltimateAudioSampler, items: list[Any], hook: Any = None
    ) -> None:
        """Run the writer loop synchronously over ``items``, then end it.
        ``hook`` runs inside each get, i.e. after the dequeue and before the
        write, where a concurrent flush() can land."""

        def get(*_a: Any, **_kw: Any) -> Any:
            if not items:
                smp._running = False
                raise s.queue.Empty
            if hook is not None:
                hook()
            return items.pop(0)

        smp._q.get = get  # type: ignore[method-assign]
        _run_steps(self, smp)

    def test_a_producer_parked_across_a_flush_puts_nothing(self):
        # The producer is parked on a full queue when the splice lands. flush()
        # does not drain, so its put stays blocked until the epoch check gives
        # it up, and the stale chunk ahead of it is dropped by the writer.
        api = _FakeBackend()
        smp = self._running(api, consumed=0)
        parked = threading.Event()

        class _SignalingQueue(s.queue.Queue):  # type: ignore[type-arg]
            def put(self, *a: Any, **kw: Any) -> None:
                parked.set()
                super().put(*a, **kw)

        smp._q = _SignalingQueue(maxsize=1)
        smp._q.put((smp._flush_epoch, b"\x01" * 32))
        parked.clear()
        t = threading.Thread(target=smp.push_samples, args=(np.full(50, 8000, dtype=np.int16),))

        def release() -> None:
            smp._stopped = True
            t.join(timeout=1.0)

        self.addCleanup(release)
        t.start()
        self.assertTrue(parked.wait(1.0), "the producer never reached its put")
        smp.flush()
        t.join(timeout=1.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(smp._pushed_samples, 0, "a pre-splice chunk counted toward EOF")
        self.assertEqual(smp._q.qsize(), 1, "the parked put went through")
        api.reu_writes.clear()
        api.audible_writes = 0
        self._drive_writer(smp, [smp._q.get_nowait()])
        self.assertEqual(api.audible_writes, 0, "the pre-splice chunk was written after the cut")

    def test_a_flush_between_dequeue_and_write_drops_the_chunk(self):
        api = _FakeBackend()
        smp = self._running(api, consumed=0)

        def flush_lands() -> None:
            smp._flush_epoch += 1

        self._drive_writer(smp, [(smp._flush_epoch, b"\x01" * 32)], hook=flush_lands)
        self.assertEqual(api.audible_writes, 0)

    def test_a_current_chunk_is_written(self):
        api = _FakeBackend()
        smp = self._running(api, consumed=0)
        smp._written = smp._content_pos = smp._flush_margin
        self._drive_writer(smp, [(smp._flush_epoch, b"\x01" * 32)])
        self.assertGreater(api.audible_writes, 0)

    def test_the_prebuffer_skips_stale_chunks(self):
        smp = _make(_FakeBackend(), sample_rate=2000, bits=8)
        smp._q.put((smp._flush_epoch - 1, b"\x01" * 8))
        smp._q.put((smp._flush_epoch, b"\x02" * 8))
        self.assertEqual(smp._collect_prebuffer(16, 0.05), b"\x02" * 8)

    def test_flush_rewrites_nothing_behind_the_reader_and_at_most_a_ring(self):
        # A writer stalled for minutes leaves _written far behind the reader;
        # the rewrite must not grow with the stall.
        api = _FakeBackend()
        smp = self._running(api, ring=512, consumed=100_000)
        smp._written = 0
        api.reu_writes.clear()
        smp.flush()
        self.assertLessEqual(sum(n for _, n in api.reu_writes), 512)
        self.assertEqual(api.reu_writes[0][0], 0x200000 + 100_000 % 512)

    def test_flush_rewrites_at_most_a_ring_of_lead(self):
        api = _FakeBackend()
        smp = self._running(api, ring=512, consumed=0)
        smp._written = 5000
        api.reu_writes.clear()
        smp.flush()
        self.assertLessEqual(sum(n for _, n in api.reu_writes), 512)

    def test_silence_output_writes_volume_zero_then_restores(self):
        api = _FakeBackend()
        smp = self._running(api, consumed=0)
        smp._written = 0
        # $DF21 = channel 0 base ($DF20) + REG_VOLUME (1).
        smp.flush(silence_output=True)
        self.assertIn(("DF21", "00"), api.mem_writes)
        self.assertTrue(smp._output_silenced)
        api.mem_writes.clear()
        smp.flush()  # resume's plain flush restores the channel volume
        self.assertIn(("DF21", f"{smp._volume & 0x3F:02X}"), api.mem_writes)
        self.assertFalse(smp._output_silenced)


class ResolveAudioBackendTest(unittest.TestCase):
    def test_auto_picks_sampler_when_available(self):
        self.assertEqual(
            scene_factory.resolve_audio_backend(
                "auto", supports_sampler=True, sampler_available=True
            ),
            "sampler",
        )

    def test_auto_falls_back_to_dac(self):
        self.assertEqual(
            scene_factory.resolve_audio_backend(
                "auto", supports_sampler=True, sampler_available=False
            ),
            "dac",
        )
        self.assertEqual(
            scene_factory.resolve_audio_backend(
                "auto", supports_sampler=False, sampler_available=False
            ),
            "dac",
        )

    def test_dac_is_forced(self):
        self.assertEqual(
            scene_factory.resolve_audio_backend(
                "dac", supports_sampler=True, sampler_available=True
            ),
            "dac",
        )

    def test_explicit_sampler_warns_and_falls_back(self):
        with self.assertLogs("c64cast.app.scene_factory", level="WARNING"):
            got = scene_factory.resolve_audio_backend(
                "sampler", supports_sampler=False, sampler_available=False
            )
        self.assertEqual(got, "dac")

    def test_explicit_sampler_succeeds_when_available(self):
        self.assertEqual(
            scene_factory.resolve_audio_backend(
                "sampler", supports_sampler=True, sampler_available=True
            ),
            "sampler",
        )


class ValidateSamplerCfgTest(unittest.TestCase):
    def _cfg(self, *, bits=16, rate=44100, enabled=True):
        cfg = cfgmod.Config()
        cfg.audio.enabled = enabled
        cfg.audio.sampler_bits = bits
        cfg.audio.sampler_sample_rate = rate
        return cfg

    def test_valid_passes(self):
        scene_factory.validate_sampler_cfg(self._cfg())  # no raise

    def test_bad_bits_rejected(self):
        with self.assertRaises(cfgmod.ConfigError):
            scene_factory.validate_sampler_cfg(self._cfg(bits=12))

    def test_out_of_range_rate_rejected(self):
        with self.assertRaises(cfgmod.ConfigError):
            scene_factory.validate_sampler_cfg(self._cfg(rate=96000))
        with self.assertRaises(cfgmod.ConfigError):
            scene_factory.validate_sampler_cfg(self._cfg(rate=10))

    def test_skipped_when_audio_disabled(self):
        # Even an invalid value is ignored when audio is off.
        scene_factory.validate_sampler_cfg(self._cfg(bits=99, enabled=False))


class _FakeProfile:
    def __init__(self, supports_sampler: bool = True, supports_config: bool = True) -> None:
        self.supports_sampler = supports_sampler
        self.supports_config = supports_config


class _FakeRestApi:
    """Category-aware fake: read_sampler_config GETs two config sections, so
    session.get must return the right one per URL.

    ``absent_answer`` is how the firmware answers a GET for a category it does
    not register: ``"200"`` (before 3.15, and C64 Ultimate 1.1.0) is HTTP 200
    with only the errors array; ``"404"`` (3.15 on) is HTTP 404 with a JSON
    error naming the category. ``mixer_category=None`` registers no mixer
    category at all. ``master`` is the mixer category's ``Vol Master`` label
    (firmware 3.15+); None leaves the item out, as 3.14e and C64 Ultimate
    1.1.0 do."""

    def __init__(
        self,
        *,
        present: bool = True,
        map_status: str = "Enabled",
        vol_l: str = " 0 dB",
        vol_r: str = " 0 dB",
        mixer_category: str | None = "Audio Mixer",
        absent_answer: str = "200",
        supports_sampler: bool = True,
        supports_config: bool = True,
        master: str | None = None,
        put_error: Exception | None = None,
        get_error: Exception | None = None,
        error_categories: set[str] | None = None,
    ) -> None:
        self.base_url = "http://fake"
        self.profile = _FakeProfile(supports_sampler, supports_config)
        self.put_calls: list[tuple[str, str, str]] = []
        self._put_error = put_error
        cart: dict[str, str] = {}
        mixer: dict[str, str] = {}
        if present:
            cart["Map Ultimate Audio $DF20-DFFF"] = map_status
            mixer["Vol Sampler L"] = vol_l
            mixer["Vol Sampler R"] = vol_r
        if master is not None:
            mixer["Vol Master"] = master
        self._sections = {"C64 and Cartridge Settings": cart}
        if mixer_category is not None:
            self._sections[mixer_category] = mixer
        self.session = mock.MagicMock()

        def _get(url, timeout=3.0):
            from urllib.parse import unquote

            if get_error is not None:
                raise get_error
            cat = unquote(url.split("/v1/configs/")[-1])
            if error_categories and cat in error_categories:
                raise requests.Timeout(f"read timeout on {cat}")
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.raise_for_status = mock.MagicMock()
            body: dict[str, object] = {"errors": []}
            if cat in self._sections:
                body[cat] = self._sections[cat]
            elif absent_answer == "404":
                resp.status_code = 404
                body["errors"] = [f"No configuration category matches '{cat}'."]
                resp.raise_for_status.side_effect = requests.HTTPError("404 Client Error")
            resp.json.return_value = body
            return resp

        self.session.get.side_effect = _get

    def put_config_item(
        self, category: str, item: str, value: str, *, timeout: float = 3.0
    ) -> None:
        if self._put_error is not None:
            raise self._put_error
        self.put_calls.append((category, item, value))


def _video_cfg(*, backend="auto", enabled=True, skip_probe=False):
    cfg = cfgmod.Config()
    cfg.audio.enabled = enabled
    cfg.audio.backend = backend
    cfg.debug.skip_probe = skip_probe
    cfg.scenes = [cfgmod.SceneCfg(type="video", file="x.mp4")]
    return cfg


class SamplerAvailabilityTest(unittest.TestCase):
    def test_available_when_mapped_and_audible(self):
        self.assertIs(hw_provision.sampler_is_available(_FakeRestApi()), True)

    def test_unavailable_when_map_disabled(self):
        self.assertIs(hw_provision.sampler_is_available(_FakeRestApi(map_status="Disabled")), False)

    def test_unavailable_when_muted(self):
        self.assertIs(
            hw_provision.sampler_is_available(_FakeRestApi(vol_l="OFF", vol_r="OFF")), False
        )

    def test_audible_when_one_channel_on(self):
        self.assertIs(hw_provision.sampler_is_available(_FakeRestApi(vol_r="OFF")), True)

    def test_unavailable_when_feature_absent(self):
        self.assertIs(hw_provision.sampler_is_available(_FakeRestApi(present=False)), False)

    def test_none_on_query_failure(self):
        api = _FakeRestApi(get_error=requests.Timeout("read timeout"))
        self.assertIsNone(hw_provision.sampler_is_available(api))


def _u2plus_fake(**kwargs: Any) -> _FakeRestApi:
    """A U2+-shaped config store: the Sampler mixer channels live in
    "Audio Output Settings" and there is no "Audio Mixer" category at all."""
    return _FakeRestApi(mixer_category="Audio Output Settings", **kwargs)


class SamplerMixerCategoryTest(unittest.TestCase):
    """The category carrying "Vol Sampler L/R" differs across the Ultimate
    family (U64 "Audio Mixer" vs U2+ "Audio Output Settings");
    read_sampler_config must resolve the one this device actually carries and
    every mixer PUT + restore key must follow it. Runs once per way the
    firmware answers for the category a device does not have."""

    ABSENT_ANSWER = "200"

    def _fake(self, **kwargs: Any) -> _FakeRestApi:
        return _FakeRestApi(absent_answer=self.ABSENT_ANSWER, **kwargs)

    def _u2plus(self, **kwargs: Any) -> _FakeRestApi:
        return _u2plus_fake(absent_answer=self.ABSENT_ANSWER, **kwargs)

    def test_u64_resolves_audio_mixer(self):
        state = hw_provision.read_sampler_config(self._fake())
        self.assertIs(state.present, True)
        self.assertEqual(state.mixer_category, "Audio Mixer")

    def test_u2plus_resolves_audio_output_settings(self):
        state = hw_provision.read_sampler_config(self._u2plus())
        self.assertIs(state.present, True)
        self.assertIs(state.map_enabled, True)
        self.assertEqual(state.mixer_category, "Audio Output Settings")
        self.assertEqual(state.volumes, {"Vol Sampler L": " 0 dB", "Vol Sampler R": " 0 dB"})

    def test_u2plus_available(self):
        self.assertIs(hw_provision.sampler_is_available(self._u2plus()), True)

    def test_absent_everywhere_is_false_not_none(self):
        state = hw_provision.read_sampler_config(self._fake(present=False))
        self.assertIs(state.present, False)
        self.assertIsNone(state.mixer_category)

    def test_no_mixer_category_at_all_is_false_not_none(self):
        # A plain U2: neither candidate mixer category is registered, so both
        # reads answer "absent" and the sampler is absent, not unreadable.
        state = hw_provision.read_sampler_config(self._fake(present=False, mixer_category=None))
        self.assertIs(state.present, False)
        self.assertIsNone(state.mixer_category)

    def test_first_category_error_still_resolves_second(self):
        api = self._u2plus(error_categories={"Audio Mixer"})
        state = hw_provision.read_sampler_config(api)
        self.assertIs(state.present, True)
        self.assertEqual(state.mixer_category, "Audio Output Settings")

    def test_error_with_no_fields_found_is_cant_tell(self):
        # A U64-shaped store whose mixer read fails: the fields were never seen
        # AND a query failed, so "absent" cannot be told from "unreadable" — that
        # must stay None, not False.
        api = self._fake(error_categories={"Audio Mixer"})
        self.assertIsNone(hw_provision.read_sampler_config(api).present)

    def test_u2plus_provision_unmutes_into_resolved_category(self):
        api = self._u2plus(vol_l="OFF", vol_r="OFF")
        restore = hw_provision.provision_sampler(api, _video_cfg())
        assert restore is not None
        unmutes = [c for c in api.put_calls if c[0] == "Audio Output Settings"]
        self.assertEqual(len(unmutes), 2)
        self.assertEqual([c for c in api.put_calls if c[0] == "Audio Mixer"], [])

    def test_u2plus_master_resolves_audio_output_settings(self):
        master = hw_provision.read_master_volume(self._u2plus(master="OFF"))
        self.assertEqual(master, hw_provision.MasterVolume("OFF", "Audio Output Settings", False))

    def test_master_absent_everywhere_is_not_a_failure(self):
        master = hw_provision.read_master_volume(self._fake())
        self.assertEqual(master, hw_provision.MasterVolume(None, None, False))

    def test_u2plus_restore_targets_resolved_category(self):
        api = self._u2plus(map_status="Disabled", vol_l="OFF")
        restore = hw_provision.provision_sampler(api, _video_cfg())
        api.put_calls.clear()
        hw_provision.restore_sampler(api, restore)
        self.assertIn(
            ("C64 and Cartridge Settings", "Map Ultimate Audio $DF20-DFFF", "Disabled"),
            api.put_calls,
        )
        self.assertIn(("Audio Output Settings", "Vol Sampler L", "OFF"), api.put_calls)


class SamplerMixerCategory404Test(SamplerMixerCategoryTest):
    """The same cases against firmware 3.15, which answers 404 with a JSON
    error for a category the device does not register."""

    ABSENT_ANSWER = "404"


class WantsSamplerTest(unittest.TestCase):
    def test_wants_with_auto_and_video(self):
        wants, reasons = hw_provision.wants_sampler(_video_cfg(backend="auto"))
        self.assertTrue(wants)
        self.assertTrue(reasons)

    def test_wants_with_explicit_sampler(self):
        self.assertTrue(hw_provision.wants_sampler(_video_cfg(backend="sampler"))[0])

    def test_not_wanted_with_dac(self):
        self.assertFalse(hw_provision.wants_sampler(_video_cfg(backend="dac"))[0])

    def test_not_wanted_without_audio(self):
        self.assertFalse(hw_provision.wants_sampler(_video_cfg(enabled=False))[0])

    def test_not_wanted_without_video_scene(self):
        cfg = cfgmod.Config()
        cfg.audio.enabled = True
        cfg.scenes = [cfgmod.SceneCfg(type="waveform", file="t.sid")]
        self.assertFalse(hw_provision.wants_sampler(cfg)[0])


class ProvisionSamplerTest(unittest.TestCase):
    def test_noop_when_already_enabled(self):
        api = _FakeRestApi(map_status="Enabled", vol_l=" 0 dB", vol_r=" 0 dB")
        self.assertIsNone(hw_provision.provision_sampler(api, _video_cfg()))
        self.assertEqual(api.put_calls, [])

    def test_enables_map_when_disabled(self):
        api = _FakeRestApi(map_status="Disabled")
        restore = hw_provision.provision_sampler(api, _video_cfg())
        self.assertIsNotNone(restore)
        self.assertIn(
            ("C64 and Cartridge Settings", "Map Ultimate Audio $DF20-DFFF", "Enabled"),
            api.put_calls,
        )
        # Restore maps the composite key back to "Disabled".
        assert restore is not None
        self.assertIn("Disabled", restore.values())

    def test_unmutes_when_off(self):
        api = _FakeRestApi(vol_l="OFF", vol_r="OFF")
        restore = hw_provision.provision_sampler(api, _video_cfg())
        assert restore is not None
        unmutes = [c for c in api.put_calls if c[0] == "Audio Mixer"]
        self.assertEqual(len(unmutes), 2)
        self.assertEqual(list(restore.values()).count("OFF"), 2)

    def test_skipped_on_no_sampler_backend(self):
        api = _FakeRestApi(supports_sampler=False, map_status="Disabled")
        self.assertIsNone(hw_provision.provision_sampler(api, _video_cfg()))
        self.assertEqual(api.put_calls, [])

    def test_skipped_under_skip_probe(self):
        api = _FakeRestApi(map_status="Disabled")
        self.assertIsNone(hw_provision.provision_sampler(api, _video_cfg(skip_probe=True)))
        self.assertEqual(api.put_calls, [])

    def test_skipped_when_backend_dac(self):
        api = _FakeRestApi(map_status="Disabled")
        self.assertIsNone(hw_provision.provision_sampler(api, _video_cfg(backend="dac")))
        self.assertEqual(api.put_calls, [])

    def test_restore_puts_originals_back(self):
        api = _FakeRestApi(map_status="Disabled", vol_l="OFF", vol_r=" 0 dB")
        restore = hw_provision.provision_sampler(api, _video_cfg())
        api.put_calls.clear()
        hw_provision.restore_sampler(api, restore)
        # Map restored to Disabled, the muted channel back to OFF.
        self.assertIn(
            ("C64 and Cartridge Settings", "Map Ultimate Audio $DF20-DFFF", "Disabled"),
            api.put_calls,
        )
        self.assertIn(("Audio Mixer", "Vol Sampler L", "OFF"), api.put_calls)

    def test_restore_noop_on_none(self):
        api = _FakeRestApi()
        hw_provision.restore_sampler(api, None)  # must not raise
        self.assertEqual(api.put_calls, [])


class MasterVolumeAudibilityTest(unittest.TestCase):
    """Firmware 3.15's Vol Master multiplies every source, so the sampler is
    audible only when the master is not OFF; without the item (3.14e, C64
    Ultimate 1.1.0) the master is unity and the verdict is the channels'."""

    def test_master_absent_reads_none_and_stays_available(self):
        api = _FakeRestApi()
        self.assertIsNone(hw_provision.read_sampler_config(api).master)
        self.assertIs(hw_provision.sampler_is_available(api), True)

    def test_master_at_unity_is_available(self):
        api = _FakeRestApi(master=" 0 dB")
        self.assertEqual(hw_provision.read_sampler_config(api).master, " 0 dB")
        self.assertIs(hw_provision.sampler_is_available(api), True)

    def test_master_trimmed_is_available(self):
        self.assertIs(hw_provision.sampler_is_available(_FakeRestApi(master="-42 dB")), True)

    def test_master_off_is_unavailable(self):
        self.assertIs(hw_provision.sampler_is_available(_FakeRestApi(master="OFF")), False)

    def test_master_off_on_u2plus_is_unavailable(self):
        self.assertIs(hw_provision.sampler_is_available(_u2plus_fake(master="OFF")), False)


def _waveform_cfg(*, enabled: bool = False) -> cfgmod.Config:
    cfg = cfgmod.Config()
    cfg.audio.enabled = enabled
    cfg.scenes = [cfgmod.SceneCfg(type="waveform", file="t.sid")]
    return cfg


class WantsAudioTest(unittest.TestCase):
    def test_audio_enabled_wants_it(self):
        wants, reasons = hw_provision.wants_audio(_video_cfg())
        self.assertTrue(wants)
        self.assertIn("[audio].enabled = true", reasons)

    def test_sid_scene_wants_it_with_audio_disabled(self):
        for stype in ("waveform", "midi", "asid", "launcher"):
            cfg = cfgmod.Config()
            cfg.audio.enabled = False
            cfg.scenes = [cfgmod.SceneCfg(type=stype)]
            with self.subTest(stype=stype):
                self.assertEqual(hw_provision.wants_audio(cfg), (True, [f"{stype} scene(s)"]))

    def test_generative_sid_wants_it_with_audio_disabled(self):
        cfg = cfgmod.Config()
        cfg.audio.enabled = False
        cfg.scenes = [cfgmod.SceneCfg(type="generative", audio_source="sid")]
        self.assertTrue(hw_provision.wants_audio(cfg)[0])

    def test_muted_video_run_does_not(self):
        self.assertEqual(hw_provision.wants_audio(_video_cfg(enabled=False)), (False, []))


class ProvisionMasterVolumeTest(unittest.TestCase):
    def test_raises_off_to_unity_and_restores_off(self):
        api = _FakeRestApi(master="OFF")
        restore = hw_provision.provision_master_volume(api, _video_cfg())
        self.assertEqual(api.put_calls, [("Audio Mixer", "Vol Master", " 0 dB")])
        api.put_calls.clear()
        hw_provision.restore_master_volume(api, restore)
        self.assertEqual(api.put_calls, [("Audio Mixer", "Vol Master", "OFF")])

    def test_raises_on_u2plus_into_its_category(self):
        api = _u2plus_fake(master="OFF", absent_answer="404")
        hw_provision.provision_master_volume(api, _waveform_cfg())
        self.assertEqual(api.put_calls, [("Audio Output Settings", "Vol Master", " 0 dB")])

    def test_leaves_a_trimmed_master_alone(self):
        for level in (" 0 dB", "-12 dB", "-42 dB", "+6 dB"):
            api = _FakeRestApi(master=level)
            with self.subTest(level=level):
                self.assertIsNone(hw_provision.provision_master_volume(api, _video_cfg()))
                self.assertEqual(api.put_calls, [])

    def test_absent_master_writes_nothing(self):
        api = _FakeRestApi()
        self.assertIsNone(hw_provision.provision_master_volume(api, _video_cfg()))
        self.assertEqual(api.put_calls, [])

    def test_skipped_without_audio(self):
        api = _FakeRestApi(master="OFF")
        self.assertIsNone(hw_provision.provision_master_volume(api, _video_cfg(enabled=False)))
        self.assertEqual(api.put_calls, [])

    def test_skipped_under_skip_probe(self):
        api = _FakeRestApi(master="OFF")
        self.assertIsNone(hw_provision.provision_master_volume(api, _video_cfg(skip_probe=True)))
        self.assertEqual(api.put_calls, [])

    def test_skipped_without_config_api(self):
        api = _FakeRestApi(master="OFF", supports_config=False)
        self.assertIsNone(hw_provision.provision_master_volume(api, _video_cfg()))
        self.assertEqual(api.put_calls, [])

    def test_unreadable_mixer_warns_and_writes_nothing(self):
        api = _FakeRestApi(master="OFF", get_error=requests.Timeout("read timeout"))
        with self.assertLogs("c64cast.hw.hw_provision", level="WARNING") as cm:
            self.assertIsNone(hw_provision.provision_master_volume(api, _video_cfg()))
        self.assertIn("Vol Master", cm.output[0])
        self.assertEqual(api.put_calls, [])

    def test_failed_put_warns_and_leaves_nothing_to_restore(self):
        api = _FakeRestApi(master="OFF", put_error=requests.ConnectionError("down"))
        with self.assertLogs("c64cast.hw.hw_provision", level="WARNING") as cm:
            self.assertIsNone(hw_provision.provision_master_volume(api, _video_cfg()))
        self.assertIn("could not raise Vol Master", cm.output[0])


class WantsReuCouplingTest(unittest.TestCase):
    """The sampler streams its ring out of REU SDRAM, so a sampler run must
    pull the REU into wants_reu (provisioning + the doctor REU probe)."""

    def test_sampler_makes_wants_reu_true(self):
        wants, reasons = hw_provision.wants_reu(_video_cfg(backend="auto"))
        self.assertTrue(wants)
        self.assertTrue(any("sampler" in r for r in reasons))

    def test_dac_video_does_not_want_reu(self):
        self.assertFalse(hw_provision.wants_reu(_video_cfg(backend="dac"))[0])


if __name__ == "__main__":
    unittest.main()
