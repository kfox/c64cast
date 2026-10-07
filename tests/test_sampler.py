"""Unit tests for the Ultimate Audio FPGA PCM sampler (c64cast/audio/sampler.py) and
its config/provisioning integration. No hardware: a recording fake backend
stands in for the U64, and the hw_provision REST queries are mocked."""

from __future__ import annotations

import random
import threading
import time
import unittest
from collections.abc import Callable
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

    delivery_epoch = 0

    def __init__(self) -> None:
        self.reu_writes: list[tuple[int, int]] = []  # (offset, length)
        self.reu_data: list[tuple[int, bytes]] = []  # (offset, payload)
        self.reg_writes: list[tuple[str, tuple[int, ...]]] = []
        self.mem_writes: list[tuple[str, str]] = []
        self.flushes = 0
        self.audible_writes = 0  # REU writes carrying anything but silence
        self.delivery_epoch = 0

    def reu_write(self, offset: int, data: bytes) -> None:
        self.reu_writes.append((offset, len(data)))
        self.reu_data.append((offset, bytes(data)))
        if any(data):
            self.audible_writes += 1

    def reu_bytes(self, offset: int, length: int) -> bytes:
        """What the REU holds at ``offset`` after every write so far, with
        bytes no write reached reading 0xFF, so a test reads where audio landed rather
        than which write carried it."""
        out = bytearray(b"\xff" * length)
        for at, data in self.reu_data:
            lo, hi = max(at, offset), min(at + len(data), offset + length)
            if lo < hi:
                out[lo - offset : hi - offset] = data[lo - at : hi - at]
        return bytes(out)

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


def _signal_on_put(smp: s.UltimateAudioSampler) -> threading.Event:
    """An event set when anything next calls ``smp._q.put``, just before the
    put itself runs."""
    parked = threading.Event()
    full_put = smp._q.put

    def put(*a: Any, **kw: Any) -> None:
        parked.set()
        full_put(*a, **kw)

    smp._q.put = put  # type: ignore[method-assign]
    return parked


def _outlasting(wait_s: float, sample_rate: int = 8000) -> np.ndarray:
    """A tone that plays on past a test's wait for it to reach the ring. The
    writer drops audio whose slot the wall-clock read head has passed, so a
    shorter one made that slot, not the wait, the writer's budget: half a
    second pushed after start() is all late about 0.3 s later."""
    return np.full(int(sample_rate * (wait_s + 1.0)), 8000, dtype=np.int16)


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

    def test_the_eof_clamp_lets_a_reanchored_track_be_heard_to_its_end(self):
        # A re-anchor plays the track's last sample its lag past the pushed
        # total; clamped at the total, the heard position stopped that short.
        from c64cast.audio.audio_source import heard_seconds

        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp._running = True
        smp._gate_time = time.monotonic() - 100.0
        smp._pushed_samples = smp.sample_rate  # ~1 s of audio pushed
        lag_bytes = int(0.25 * smp._actual_rate) * smp.bps
        smp._reanchor_lag = (lag_bytes, (), 0)  # every hold crossed
        smp.mark_eof()
        lag = lag_bytes / smp.bps / smp._actual_rate
        total = smp._pushed_samples / smp._actual_rate
        self.assertAlmostEqual(smp.position_seconds(), total + lag, places=6)
        self.assertAlmostEqual(heard_seconds(smp), total, places=4)

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
        smp.push_samples(_outlasting(2.0))
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


class _LossyGateOffBackend(_FailingBackend):
    """A link that stays down for REU writes and also loses the writer
    thread's next ``lost`` register writes (negative: all of them) the way
    ``_emit`` loses one: without raising, moving ``delivery_epoch``."""

    def __init__(self, lost: int) -> None:
        super().__init__(failures=-1)
        self.lost = lost

    def write_memory(self, address: str, data_hex: str) -> None:
        if self.lost and threading.current_thread().name == "uaudio-writer":
            self.lost -= 1
            self.delivery_epoch += 1
            return
        super().write_memory(address, data_hex)


class _StallingGateOffBackend(_FailingBackend):
    """A link that stays down for REU writes, where the writer thread's
    gate-off sits in the transport (a dial to a machine that is switched off)
    until ``release`` is set, and then lands."""

    def __init__(self) -> None:
        super().__init__(failures=-1)
        self.stalled = threading.Event()
        self.release = threading.Event()

    def write_memory(self, address: str, data_hex: str) -> None:
        if threading.current_thread().name == "uaudio-writer" and not self.release.is_set():
            self.stalled.set()
            self.release.wait(5.0)
        super().write_memory(address, data_hex)


class SamplerGaveUpSurvivorTest(unittest.TestCase):
    """A writer that gave up on the link and is still in its gate-off when
    stop()'s join runs out: the next activation goes ahead, and that gate-off
    cannot land after the new gate-on."""

    def test_the_next_activation_starts_and_its_gate_on_lands_last(self):
        api = _StallingGateOffBackend()
        self.addCleanup(api.release.set)
        smp = _make(api, sample_rate=8000, bits=8, lead_seconds=0.2, prebuffer_seconds=0.01)
        with (
            mock.patch.object(s, "WRITER_GIVE_UP_S", 0.1),
            mock.patch.object(s, "WRITER_BACKOFF_MAX_S", 0.01),
            quiet_logging(),
        ):
            smp.start(prebuffer_timeout=0.01)
            self.assertTrue(api.stalled.wait(3.0), "the writer never sent its gate-off")
            survivor = smp._writer
            assert survivor is not None
            self.addCleanup(survivor.stop)
            survivor._join_timeout = 0.05
            smp.stop()
            self.assertTrue(survivor.is_running(), "the survivor did not outlive the join")
            smp.arm()  # refused before: the lap played silent
            starter = threading.Thread(target=smp.start, kwargs={"prebuffer_timeout": 0.01})
            starter.start()
            time.sleep(0.1)
            api.release.set()
            starter.join(3.0)
            self.assertFalse(starter.is_alive(), "start() never gated the channel on")
            survivor.stop()
            self.assertFalse(survivor.is_running())
            last = [d for a, d in api.mem_writes if a == "DF20"][-1]
            smp.stop()
        self.assertNotEqual(last, "00", "the retired writer's gate-off landed after the gate-on")

    def test_a_retired_writer_sends_no_gate_off_after_the_next_gate_on(self):
        # Every gate-off reaches the machine but reads as lost, so the writer
        # keeps retrying across stop() and the next start().
        api = _UnconfirmedGateOffBackend()
        smp = _make(api, sample_rate=8000, bits=8, lead_seconds=0.2, prebuffer_seconds=0.01)
        with (
            mock.patch.object(s, "WRITER_GIVE_UP_S", 0.1),
            mock.patch.object(s, "WRITER_BACKOFF_MAX_S", 0.01),
            quiet_logging(),
        ):
            smp.start(prebuffer_timeout=0.01)
            deadline = time.monotonic() + 3.0
            while api.delivery_epoch < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(api.delivery_epoch, 3, "the gate-off was not retried")
            survivor = smp._writer
            assert survivor is not None
            self.addCleanup(survivor.stop)
            smp.stop()
            with mock.patch.object(s, "WRITER_GIVE_UP_S", 60.0):
                smp.start(prebuffer_timeout=0.01)
                gate_on = len(api.mem_writes)
                time.sleep(0.1)
                after = [w for w in api.mem_writes[gate_on:] if w == ("DF20", "00")]
                smp.stop()
        self.assertEqual(after, [], "a retired writer gated the new activation off")


class _UnconfirmedGateOffBackend(_FailingBackend):
    """A link that stays down for REU writes, and on which every writer-thread
    register write lands but is counted lost (``delivery_epoch`` moves)."""

    def __init__(self) -> None:
        super().__init__(failures=-1)

    def write_memory(self, address: str, data_hex: str) -> None:
        super().write_memory(address, data_hex)
        if threading.current_thread().name == "uaudio-writer":
            self.delivery_epoch += 1


class SamplerWriterFailureTest(unittest.TestCase):
    TONE = np.full(256, 8000, dtype=np.int16)
    WAIT_S = 3.0

    def _started(self, api: _FakeBackend) -> s.UltimateAudioSampler:
        smp = _make(api, sample_rate=8000, bits=8, lead_seconds=0.2, prebuffer_seconds=0.01)

        def quiet_stop() -> None:
            with quiet_logging():  # idle-writer pads are timing, not the subject
                smp.stop()

        self.addCleanup(quiet_stop)
        smp.start(prebuffer_timeout=0.01)
        return smp

    def _wait(self, cond: Any) -> bool:
        deadline = time.monotonic() + self.WAIT_S
        while not cond() and time.monotonic() < deadline:
            time.sleep(0.01)
        return bool(cond())

    def test_a_failed_write_is_retried_not_fatal(self):
        api = _FailingBackend(failures=2)
        with self.assertLogs("c64cast.audio.sampler", level="WARNING") as logs:
            smp = self._started(api)
            api.audible_writes = 0
            smp.push_samples(_outlasting(self.WAIT_S))
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
        smp._q.put((smp._flush_epoch, b""))  # a full queue nothing drains
        smp._q.put = mock.Mock(  # type: ignore[method-assign]
            side_effect=AssertionError("the producer parked on a dead sampler")
        )
        self.assertEqual(smp.push_samples(self.TONE), 0)

    def test_a_gate_off_lost_to_the_outage_is_sent_until_it_lands(self):
        # The gate-off travels the link that failed. Sent once and lost, the
        # ring looped stale audio after the link came back, under a scene that
        # now survives the outage.
        api = _LossyGateOffBackend(lost=3)
        with (
            mock.patch.object(s, "WRITER_GIVE_UP_S", 0.1),
            mock.patch.object(s, "WRITER_BACKOFF_MAX_S", 0.01),
            self.assertLogs("c64cast.audio.sampler", level="INFO") as logs,
        ):
            smp = self._started(api)
            self.assertTrue(
                self._wait(lambda: ("DF20", "00") in api.mem_writes), "the gate-off never landed"
            )
        self.assertEqual(api.lost, 0)
        self.assertTrue(smp._failed)
        self.assertTrue(any("retrying until the link answers" in m for m in logs.output))
        self.assertTrue(any("channel gated off" in m for m in logs.output), logs.output)

    def test_a_gate_off_the_link_lost_is_not_flushed(self):
        # Ultimate64API.flush logs a warning per failure; flushing a retry the
        # link already lost logged two a second for the whole outage.
        api = _LossyGateOffBackend(lost=-1)
        writer_flushes = []
        api.flush = lambda: writer_flushes.append(threading.current_thread().name)  # type: ignore[method-assign]
        with (
            mock.patch.object(s, "WRITER_GIVE_UP_S", 0.1),
            mock.patch.object(s, "WRITER_BACKOFF_MAX_S", 0.01),
            self.assertLogs("c64cast.audio.sampler", level="WARNING") as logs,
        ):
            self._started(api)
            self.assertTrue(
                self._wait(lambda: api.delivery_epoch >= 5), "the gate-off was not retried"
            )
        self.assertTrue(any("retrying until the link answers" in m for m in logs.output))
        self.assertNotIn("uaudio-writer", writer_flushes)

    def test_push_samples_reports_what_it_accepted(self):
        # An audio-file scene waits for the sink's clock to reach what it
        # accepted, so a refused chunk must not be reported as taken.
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8)
        self.assertEqual(smp.push_samples(self.TONE), len(self.TONE))
        smp._failed = True
        self.assertEqual(smp.push_samples(self.TONE), 0)
        smp._failed = False
        smp._stopped = True
        self.assertEqual(smp.push_samples(self.TONE), 0)

    def test_a_producer_parked_when_the_writer_gives_up_is_released(self):
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8, queue_max_chunks=1)
        smp._q.put((smp._flush_epoch, b""))
        parked = _signal_on_put(smp)
        t = threading.Thread(target=smp.push_samples, args=(self.TONE,))
        self.addCleanup(t.join, 1.0)
        self.addCleanup(setattr, smp, "_stopped", True)
        t.start()
        self.assertTrue(parked.wait(2.0), "the producer never reached the full queue")
        smp._failed = True
        t.join(timeout=2.0)
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
        self.assertEqual(api.reu_bytes(0x200000 + consumed + margin, 16), b"\x01" * 16)
        self.assertEqual(api.audible_writes, 1, api.reu_writes)
        self.assertEqual(smp._late_bytes, 16)
        self.assertEqual(
            api.reu_bytes(0x200000 + consumed, margin),
            bytes(margin),
            "the skipped span was not blanked",
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

    def _hold_floor(self, smp: s.UltimateAudioSampler) -> int:
        """The lowest lead at which a partial quantum is held: past the hold's
        deadline (HOLD_GUARD_S over the write floor), whatever its size and
        however long ago the last write was."""
        return smp._hold_deadline + smp.bps

    def test_a_real_time_producer_is_coalesced_below_the_low_watermark(self):
        # A live stream never builds a queue backlog: each pass finds one
        # frame. A splice or a re-anchor leaves it below the low watermark,
        # and while the lead has the slack the writer still waits for a whole
        # quantum, without padding meanwhile.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=4.0, ring_size=0x10000)
        pos = self._hold_floor(smp)
        self.assertLess(pos, smp._lead_panic)
        self._place(smp, pos)
        frames = 0
        while (frames + 1) * 20 < smp._write_quantum:
            smp._q.put((smp._flush_epoch, b"\x01" * 20))
            frames += 1
            self.assertFalse(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [], "a frame was written, or a pad, while held")
        smp._q.put((smp._flush_epoch, b"\x01" * 20))
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [(smp.ring_base + pos, 20 * (frames + 1))])

    def test_the_hold_is_decided_on_the_read_head_after_the_queue_wait(self):
        # The producer stalled with a partial quantum held. The pass waits on
        # the empty queue while the reader moves on; held again on the lead
        # from before that wait, the gather would be written late.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=4.0, ring_size=0x10000)
        consumed = [0]
        smp._read_consumed_bytes = lambda: consumed[0]  # type: ignore[method-assign]
        pos = self._hold_floor(smp)
        self._place(smp, pos)
        smp._carry = (smp._flush_epoch, memoryview(b"\x01" * 20))

        def stalled_get(*_a: Any, **_kw: Any) -> Any:
            consumed[0] += smp.bps
            raise s.queue.Empty

        smp._q.get = stalled_get  # type: ignore[method-assign]
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [(smp.ring_base + pos, 20)])

    def test_at_the_holds_deadline_a_partial_quantum_is_written(self):
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=4.0, ring_size=0x10000)
        pos = self._hold_floor(smp) - smp.bps
        self._place(smp, pos)
        smp._q.put((smp._flush_epoch, b"\x01" * 20))
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [(smp.ring_base + pos, 20)])

    def test_a_partial_quantum_is_held_to_the_deadline_not_to_a_forecast(self):
        # Too little slack for the rest of the quantum to arrive at real time
        # before the deadline is no reason to write it now: a producer
        # slightly slower than real time spent the last of its cushion that
        # way, one REU write per 2.5 ms frame.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=4.0, ring_size=0x10000)
        self.assertGreater(smp._write_quantum - 20, 2 * smp.bps)
        self._place(smp, self._hold_floor(smp) + smp.bps)
        smp._q.put((smp._flush_epoch, b"\x01" * 20))
        self.assertFalse(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [])
        self.assertIsNotNone(smp._carry)

    def test_the_write_interval_floor_never_holds_an_on_time_gather_late(self):
        # The writer has just written, and a partial gather is held one byte
        # past the hold's deadline. Its queue waits pass real time; the floor
        # must have run out by the end of the first, inside HOLD_GUARD_S, or
        # the next wait takes the gather past the write floor.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=4.0, ring_size=0x10000)
        consumed = [0]
        smp._read_consumed_bytes = lambda: consumed[0]  # type: ignore[method-assign]
        smp._last_write_head = 0
        pos = self._hold_floor(smp)
        self._place(smp, pos)
        smp._carry = (smp._flush_epoch, memoryview(b"\x01" * 20))

        def waiting_get(block: bool = True, timeout: float | None = None) -> Any:
            if block and timeout:
                consumed[0] += int(timeout * smp._actual_rate) * smp.bps
            raise s.queue.Empty

        smp._q.get = waiting_get  # type: ignore[method-assign]
        for _ in range(5):
            if smp._writer_step(smp._writer_gen):
                break
        self.assertEqual(api.reu_writes, [(smp.ring_base + pos, 20)])
        self.assertEqual(smp._late_bytes, 0)

    def test_partial_writes_are_spaced_by_the_write_interval(self):
        # In every state a partial quantum waits out MIN_WRITE_INTERVAL_S
        # after the previous audio write: here the lead is already at the
        # write floor, as right after a splice.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=4.0, ring_size=0x10000)
        consumed = [0]
        smp._read_consumed_bytes = lambda: consumed[0]  # type: ignore[method-assign]
        smp._last_write_head = 0
        self._place(smp, smp._flush_margin + smp._write_interval)
        smp._q.put((smp._flush_epoch, b"\x01" * 20))
        self.assertFalse(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [])
        consumed[0] = smp._write_interval
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(
            api.reu_writes, [(smp.ring_base + smp._flush_margin + smp._write_interval, 20)]
        )

    def test_arm_forgets_the_last_activations_write_head(self):
        # The read head restarts at 0 with the next gate. Timed from a head
        # the last activation left, the floor held a partial gather until the
        # new head passed it: a clip shorter than a quantum was never written.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=4.0, ring_size=0x10000)
        smp._last_write_head = 60 * int(smp._actual_rate) * smp.bps
        smp.arm()
        smp._running = True
        self._place(smp, smp._flush_margin)
        smp._q.put((smp._flush_epoch, b"\x01" * 20))
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [(smp.ring_base + smp._flush_margin, 20)])

    def test_a_whole_quantum_does_not_wait_out_the_write_interval(self):
        # A producer catching up fills quanta faster than the floor's pace;
        # holding them would only turn its backlog late.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=4.0, ring_size=0x10000)
        smp._last_write_head = 0
        smp._q.put((smp._flush_epoch, b"\x01" * smp._write_quantum))
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(api.reu_writes, [(smp.ring_base + smp._flush_margin, smp._write_quantum)])

    def test_only_a_held_carry_waits_on_the_queue(self):
        # Right after a splice the anchor sits on the write floor: a carry
        # there is written, and waiting on an empty queue first would turn the
        # wait's worth of it late.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=4.0, ring_size=0x10000)
        smp._carry = (smp._flush_epoch, memoryview(b"\x01" * 20))
        blocking: list[Any] = []

        def empty_get(block: bool = True, timeout: float | None = None) -> Any:
            if block:
                blocking.append(timeout)
            raise s.queue.Empty

        smp._q.get = empty_get  # type: ignore[method-assign]
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertEqual(blocking, [], "the writer waited on the queue behind a due carry")
        self.assertEqual(api.reu_writes, [(smp.ring_base + smp._flush_margin, 20)])
        # A carry that is held still makes the pass's one bounded wait, so a
        # writer holding a partial quantum does not spin.
        self._place(smp, self._hold_floor(smp))
        smp._carry = (smp._flush_epoch, memoryview(b"\x01" * 20))
        self.assertFalse(smp._writer_step(smp._writer_gen))
        self.assertEqual(blocking, [0.02])

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

    def test_a_pad_after_end_input_still_pads_but_is_not_an_underrun(self):
        # A file scene lives on after end_input() while the ring plays out,
        # and the lead falls through the watermark behind the last sample.
        api = _FakeBackend()
        smp = self._idle_reader(api, lead_seconds=1.0)
        self._place(smp, smp._lead_panic)
        smp.end_input()
        self.assertTrue(smp._writer_step(smp._writer_gen))
        self.assertGreater(smp._written, smp._lead_panic, "the play-out was not padded")
        self.assertEqual(smp._underrun_pads, 0)

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


class SamplerLateReanchorTest(unittest.TestCase):
    """Audio whose slot has passed is dropped, so a producer catching up after a
    stall lines up again. One that stays late for LATE_REANCHOR_S is not
    catching up, and is re-anchored past the read head instead of left silent."""

    def setUp(self) -> None:
        self.api = _FakeBackend()
        self.consumed = 0
        smp = _make(self.api, sample_rate=2000, bits=8, ring_base=0x200000, ring_size=0x4000)
        smp._running = True
        smp._read_consumed_bytes = lambda: self.consumed  # type: ignore[method-assign]
        self.smp = smp

    def _write(self, n: int) -> bool:
        smp = self.smp
        return smp._write_payload(smp._writer_gen, smp._flush_epoch, b"\x01" * n)

    def test_a_producer_that_stays_late_is_reanchored_and_keeps_playing(self):
        # The prebuffer timed out (nothing anchored past 0) and the stream
        # turned up 3 s later at real time: without a re-anchor every chunk
        # would be dropped for the rest of the scene.
        smp = self.smp
        self.consumed = 3 * int(smp._actual_rate)
        started = self.consumed
        with self.assertLogs("c64cast.audio.sampler", "WARNING") as logs:
            while self.consumed - started < smp._late_reanchor_bytes:
                self.assertFalse(self._write(40))
                self.consumed += 40
            self.assertEqual(self.api.audible_writes, 0)
            slot = smp._content_pos  # where the dropped audio left the next sample
            self.assertTrue(self._write(40))
        self.assertIn("re-anchored", logs.output[0])
        self.assertIn("arrived late for 0.5 s", logs.output[0])
        anchor = self.consumed + smp._reanchor_lead
        self.assertGreaterEqual(smp._reanchor_lead, smp._flush_margin)
        at = 0x200000 + anchor % smp.ring_size
        self.assertEqual(sum(self.api.reu_writes[-1]), at + 40)
        self.assertEqual(self.api.reu_bytes(at, 40), b"\x01" * 40)
        for _ in range(50):  # real time from here: nothing more is dropped
            self.consumed += 40
            self.assertTrue(self._write(40))
        self.assertEqual(smp._content_pos, anchor + 51 * 40)
        self.assertEqual(smp._reanchors, 1)
        # The sound now ends this far past the length the producer delivered.
        dropped_writes = -(-smp._late_reanchor_bytes // 40)
        delivered = (dropped_writes + 51) * 40
        self.assertAlmostEqual(
            smp.content_lag_seconds,
            (smp._content_pos - delivered) / smp.bps / smp._actual_rate,
            places=9,
        )
        # Every sample from here plays that far past its slot: the sound's lag
        # behind position_seconds(), which a file source's analyzer subtracts.
        lag = (anchor - slot) / smp.bps / smp._actual_rate
        self.assertGreater(lag, 3.0)
        self.assertAlmostEqual(smp.reanchor_lag_seconds(), lag)

    def test_the_heard_sample_holds_at_the_moved_slot_until_the_anchor(self):
        # Between a re-anchor and its anchor the reader plays nothing current,
        # so the sample heard holds at the slot the audio was moved from;
        # stepping straight back by the shift replayed audio the analyzer
        # had already read, which was dropped late and never heard.
        smp = self.smp
        self.consumed = 3 * int(smp._actual_rate)
        started = self.consumed
        with self.assertLogs("c64cast.audio.sampler", "WARNING"):
            while self.consumed - started < smp._late_reanchor_bytes:
                self._write(40)
                self.consumed += 40
            slot = smp._content_pos
            self.assertTrue(self._write(40))
        anchor = self.consumed + smp._reanchor_lead

        def heard() -> int:
            lag = round(smp.reanchor_lag_seconds() * smp._actual_rate) * smp.bps
            return self.consumed - lag

        self.assertEqual(heard(), slot)
        self.consumed = anchor - smp.bps
        self.assertEqual(heard(), slot)
        self.consumed = anchor + 40
        self.assertEqual(heard(), slot + 40)

    def _heard(self, pushed: int) -> int:
        # What a file source's analysis tap hands the analyzer: the heard
        # sample, clamped to what was pushed (8-bit, so bytes are samples).
        smp = self.smp
        lag = round(smp.reanchor_lag_seconds() * smp._actual_rate) * smp.bps
        return min(self.consumed - lag, pushed)

    def _reanchor_late(self) -> None:
        smp = self.smp
        self.consumed = 3 * int(smp._actual_rate)
        started = self.consumed
        with self.assertLogs("c64cast.audio.sampler", "WARNING"):
            while self.consumed - started < smp._late_reanchor_bytes:
                self._write(40)
                self.consumed += 40
            self.assertTrue(self._write(40))

    def test_a_reanchor_past_the_moved_slot_holds_what_was_already_heard(self):
        # The head is past the moved slot and the producer has audio queued
        # behind the writer: the tap already handed the analyzer samples past
        # the slot, so holding at the slot replayed them.
        smp = self.smp
        self.consumed = 3 * int(smp._actual_rate)
        started = self.consumed
        with self.assertLogs("c64cast.audio.sampler", "WARNING"):
            while self.consumed - started < smp._late_reanchor_bytes:
                self._write(40)
                self.consumed += 40
            smp._pushed_samples = smp._content_pos + 3000
            pushed = smp._pushed_samples
            before = self._heard(pushed)
            self.assertTrue(self._write(40))
        # The producer keeps pushing from here, so the tap no longer clamps.
        seen = [self._heard(10**9)]
        anchor = self.consumed + smp._reanchor_lead
        while self.consumed < anchor + 4000:
            self.consumed += 37
            seen.append(self._heard(10**9))
        self.assertEqual(seen[0], before)
        self.assertEqual(seen, sorted(seen), "the heard sample stepped back")
        self.assertGreater(seen[-1], before)

    def test_a_sticky_reanchor_before_the_last_anchor_keeps_its_hold(self):
        # A producer too slow to fill even the flush margin re-anchors again
        # while the head is still short of the last anchor; that hold's
        # uncrossed part must not come in at once.
        smp = self.smp
        self._reanchor_late()
        first_anchor = self.consumed + smp._reanchor_lead
        self.consumed = first_anchor - 10
        before = self._heard(10**9)
        with self.assertLogs("c64cast.audio.sampler", "DEBUG"):
            self.assertTrue(self._write(40))
        self.assertEqual(smp._reanchors, 2)
        seen = [self._heard(10**9)]
        while self.consumed < smp._content_pos + 1000:
            self.consumed += 7
            seen.append(self._heard(10**9))
        self.assertEqual(seen[0], before)
        self.assertEqual(seen, sorted(seen), "the heard sample stepped back")

    def test_a_sticky_reanchor_inside_a_hold_past_its_anchor_keeps_it_flat(self):
        # A re-anchor with the head past the moved slot holds the heard
        # sample past its anchor. A sticky one landing inside that hold
        # overlapped it, so the head's progress came off twice: the heard
        # sample jumped ahead, then ran backward.
        self._reanchor_inside_a_hold_past_its_anchor(head_past_slot=10)

    def test_a_sticky_reanchor_short_of_its_slot_ends_the_older_hold_there(self):
        # The same, with the head still short of the slot the second
        # re-anchor moves: the older hold is cut where the new one starts
        # rather than dropped, and kept whole it overlapped the new hold.
        self._reanchor_inside_a_hold_past_its_anchor(head_past_slot=-10)

    def _reanchor_inside_a_hold_past_its_anchor(self, head_past_slot: int) -> None:
        smp = self.smp
        self.consumed = 3 * int(smp._actual_rate)
        started = self.consumed
        with self.assertLogs("c64cast.audio.sampler", "WARNING"):
            while self.consumed - started < smp._late_reanchor_bytes:
                self._write(40)
                self.consumed += 40
            smp._pushed_samples = 10**9  # far ahead: the tap never clamps
            before = self._heard(10**9)
            self.assertTrue(self._write(40))
        # The producer stalls until the head is near or past what it wrote.
        self.assertLess(-head_past_slot, smp._flush_margin)  # still late
        self.consumed = smp._content_pos + head_past_slot
        with self.assertLogs("c64cast.audio.sampler", "DEBUG"):
            self.assertTrue(self._write(40))
        self.assertEqual(smp._reanchors, 2)
        seen = []
        end = smp._content_pos + 10000
        while self.consumed < end:
            seen.append(self._heard(10**9))
            self.consumed += 20
        self.assertEqual(seen[0], before)
        self.assertEqual(seen, sorted(seen), "the heard sample stepped back")
        self.assertGreater(seen[-1], before)

    def test_a_splice_clears_the_reanchor_lag(self):
        smp = self.smp
        self._reanchor_late()
        self.consumed += 2 * smp._reanchor_lead
        self.assertGreater(smp.reanchor_lag_seconds(), 0.0)
        smp.flush()
        self.assertEqual(smp.reanchor_lag_seconds(), 0.0)

    def test_arm_clears_the_reanchor_lag(self):
        smp = self.smp
        self._reanchor_late()
        self.consumed += 2 * smp._reanchor_lead
        self.assertGreater(smp.reanchor_lag_seconds(), 0.0)
        smp.arm()
        self.assertEqual(smp.reanchor_lag_seconds(), 0.0)

    def test_a_reanchor_published_after_the_lag_read_s_head_does_not_step_back(self):
        # The reader reads the head, then a re-anchor further on lands before
        # it reads the lag. Inside a hold, that lag taken at the earlier head
        # stepped the heard sample back by the distance between the two.
        smp = self.smp
        self.consumed = 3 * int(smp._actual_rate)
        started = self.consumed
        with self.assertLogs("c64cast.audio.sampler", "WARNING"):
            while self.consumed - started < smp._late_reanchor_bytes:
                self._write(40)
                self.consumed += 40
            smp._pushed_samples = 10**9  # far ahead: the tap never clamps
            self.assertTrue(self._write(40))
        self.consumed = smp._content_pos + 10  # inside the hold, still late
        head = self.consumed
        held = self._heard(10**9)

        def head_then_reanchor() -> int:
            smp._read_consumed_bytes = lambda: self.consumed  # type: ignore[method-assign]
            self.consumed += 30
            with self.assertLogs("c64cast.audio.sampler", "DEBUG"):
                self.assertTrue(self._write(40))
            self.assertEqual(smp._reanchors, 2)
            return head

        smp._read_consumed_bytes = head_then_reanchor  # type: ignore[method-assign]
        lag = round(smp.reanchor_lag_seconds() * smp._actual_rate) * smp.bps
        self.assertEqual(head - lag, held, "the heard sample stepped back")

    def test_a_lag_read_while_a_reanchor_is_in_flight_waits_for_it(self):
        # The writer has read the head it re-anchors at and not yet published
        # the lag; a reader whose head is past the writer's took the old lag,
        # heard audio the new one then held it short of, and stepped back.
        smp = self.smp
        self._reanchor_late()
        smp._pushed_samples = 10**9  # far ahead: the tap never clamps
        self.consumed = smp._content_pos + 10  # past the hold, still late
        heard: list[int] = []

        def read() -> None:
            lag = round(smp.reanchor_lag_seconds() * smp._actual_rate) * smp.bps
            heard.append(self.consumed - lag)

        # A daemon: a reader left spinning by a window that never closes fails
        # this test rather than holding the test process open at exit.
        reader = threading.Thread(target=read, daemon=True)
        compute = smp._lag_after_reanchor

        def reanchor_with_a_read_in_flight(consumed: int, c: int, shift: int) -> Any:
            self.consumed = consumed + 20  # the reader's head, past the writer's
            reader.start()
            reader.join(0.2)  # a reader that does not wait has read by now
            return compute(consumed, c, shift)

        smp._lag_after_reanchor = reanchor_with_a_read_in_flight  # type: ignore[method-assign]
        with self.assertLogs("c64cast.audio.sampler", "DEBUG"):
            self.assertTrue(self._write(40))
        reader.join(5.0)
        self.assertFalse(reader.is_alive())
        self.assertEqual(heard, [self._heard(10**9)], "the heard sample stepped back")

    def test_a_lag_read_at_a_given_position_takes_that_position_s_head(self):
        # AudioFileSource subtracts the lag from a position_seconds() it read
        # first; inside a hold, a lag read at the head as of later stepped the
        # heard sample back by however far the head had moved in between.
        smp = self.smp
        self._reanchor_late()
        position = self.consumed / smp._actual_rate
        held = self.consumed - round(smp.reanchor_lag_seconds() * smp._actual_rate)
        self.consumed += 25  # still inside the hold
        lag = smp.reanchor_lag_seconds(position)
        sample = 1 / smp._actual_rate
        self.assertAlmostEqual(position - lag, held * sample, delta=1.5 * sample)
        self.assertAlmostEqual(lag, smp.reanchor_lag_seconds() - 25 * sample, delta=1.5 * sample)

    def test_the_lag_window_covers_the_writer_s_head_and_not_its_log_line(self):
        # A reader whose head was read past the writer's head must find the
        # window open, so the head read is inside it; and a reader must not
        # wait out the log line, which may block on a handler's I/O.
        smp = self.smp
        at_head: list[int] = []

        def head() -> int:
            at_head.append(smp._lag_seq & 1)
            return self.consumed

        smp._read_consumed_bytes = head  # type: ignore[method-assign]
        at_log: list[int] = []
        self.consumed = 3 * int(smp._actual_rate)
        started = self.consumed
        with mock.patch.object(
            s.log, "log", side_effect=lambda *a: at_log.append(smp._lag_seq & 1)
        ):
            while self.consumed - started < smp._late_reanchor_bytes:
                self._write(40)
                self.consumed += 40
            self.assertTrue(self._write(40))
        self.assertEqual(smp._reanchors, 1)
        self.assertEqual(at_log, [0], "the re-anchor's log line ran inside the window")
        self.assertEqual(set(at_head), {1}, "a writer head read fell outside the window")
        self.assertEqual(smp._lag_seq & 1, 0)

    def test_a_stopped_sampler_has_no_reanchor_lag(self):
        # Its position_seconds() is 0, and the lag taken at the last
        # re-anchor's head put the heard sample there instead.
        smp = self.smp
        self._reanchor_late()
        self.consumed += 2 * smp._reanchor_lead
        self.assertGreater(smp.reanchor_lag_seconds(), 0.0)
        smp._running = False
        self.assertEqual(smp.reanchor_lag_seconds(), 0.0)
        self.assertEqual(smp.reanchor_lag_seconds(0.0), 0.0)

    def test_a_producer_catching_up_lines_up_without_a_reanchor(self):
        # A decoder with a backlog after a stall: its late chunks are dropped
        # and the rest land at their own slots, so sync is unchanged.
        smp = self.smp
        self.consumed = 2000
        started = self.consumed
        chunks = 0
        while not self._write(100):  # 10x real time
            chunks += 1
            self.consumed += 10
        self.assertGreater(chunks, 0)
        self.assertLess(self.consumed - started, smp._late_reanchor_bytes)
        for _ in range(50):
            chunks += 1
            self.consumed += 10
            self.assertTrue(self._write(100))
        self.assertEqual(smp._reanchors, 0)
        # Every chunk sits at its original slot: the audio did not shift.
        self.assertEqual(smp._content_pos, (chunks + 1) * 100)

    def test_a_failed_ring_write_restarts_the_late_run(self):
        # The link went down while the audio was turning late, and the retries
        # backed off for a whole window. The backlog behind it drops through at
        # once when the link returns, so the outage must not count as a run.
        smp = self.smp
        self.consumed = 1000
        smp._written = smp._content_pos = self.consumed  # late, straddling the floor
        with mock.patch.object(self.api, "reu_write", side_effect=OSError("link down")):
            with self.assertRaises(OSError):
                self._write(smp._flush_margin + 100)
        self.consumed += smp._late_reanchor_bytes
        self.assertFalse(self._write(100))  # late: dropped, and a new run begins
        self.assertEqual(smp._reanchors, 0)

    def test_a_reanchored_write_stops_at_the_lead_target(self):
        # The payload was sized for the room the late anchor left; written whole
        # from the re-anchor it would pass the lead target, so its tail is carried.
        smp = self.smp
        self.consumed = 5000
        start = self.consumed
        self.assertFalse(self._write(50))  # late: a window begins
        while self.consumed - start < smp._late_reanchor_bytes - 50:
            self.consumed += 50  # real time: the lateness holds
            self.assertFalse(self._write(50))
        self.consumed += 50
        with self.assertLogs("c64cast.audio.sampler", "WARNING"):
            self.assertTrue(self._write(smp._lead_target))
        self.assertEqual(smp._content_pos, self.consumed + smp._lead_target)
        assert smp._carry is not None
        self.assertEqual(len(smp._carry[1]), smp._reanchor_lead)

    def test_a_lead_with_no_room_past_the_reanchor_still_writes(self):
        # A lead at the write floor re-anchors to the lead target itself, so
        # nothing fits under it; carrying the whole payload would re-anchor
        # it to no room again on every pass, and the ring would never be fed.
        smp = _make(
            self.api,
            sample_rate=2000,
            bits=8,
            ring_base=0x200000,
            ring_size=0x4000,
            lead_seconds=0.15,
        )
        smp._running = True
        smp._read_consumed_bytes = lambda: self.consumed  # type: ignore[method-assign]
        self.smp = smp
        self.assertEqual(smp._reanchor_lead, smp._lead_target)
        smp._reanchor_sticky = True
        self.consumed = 5000
        with self.assertLogs("c64cast.audio.sampler", "WARNING"):
            self.assertTrue(self._write(40))
        self.assertIsNone(smp._carry)
        self.assertEqual(smp._content_pos, self.consumed + smp._reanchor_lead + 40)

    def test_an_on_time_write_ends_the_late_run(self):
        smp = self.smp
        self.consumed = 1000
        smp._written = smp._content_pos = self.consumed + smp._flush_margin - 10
        self.assertTrue(self._write(100))  # 10 bytes late: a run begins
        self.assertTrue(self._write(100))  # on time
        # Late again, past the window from the first run's start: a new run.
        self.consumed += smp._late_reanchor_bytes + 1000
        self.assertFalse(self._write(100))
        self.assertEqual(smp._reanchors, 0)

    def _reanchor_once(self) -> None:
        smp = self.smp
        self.consumed = max(self.consumed, smp._content_pos) + 3 * int(smp._actual_rate)
        # The WARNING is once per activation; later re-anchors log at DEBUG.
        level = "WARNING" if smp._reanchors == 0 else "DEBUG"
        target = smp._reanchors + 1
        with self.assertLogs("c64cast.audio.sampler", level):
            while smp._reanchors < target:
                self._write(40)
                self.consumed += 40

    def test_after_a_reanchor_late_audio_is_reanchored_at_once(self):
        # A producer shown slower than real time plays late, as before
        # anchoring, rather than losing another window every cycle.
        smp = self.smp
        self._reanchor_once()
        self.consumed = smp._content_pos  # the cushion used up
        with self.assertLogs("c64cast.audio.sampler", "DEBUG") as logs:
            self.assertTrue(self._write(40))
        # No window was waited out, so the log line does not claim one.
        self.assertIn("arrived late again", logs.output[0])
        self.assertEqual(smp._reanchors, 2)
        self.assertEqual(smp._content_pos, self.consumed + smp._reanchor_lead + 40)

    def test_the_content_lag_adds_up_over_reanchors(self):
        # A producer that stays slow is re-anchored again and again, and each
        # one moves the sound further behind the clock: the end an audio-file
        # scene waits out is past all of them, not only the latest.
        smp = self.smp
        self._reanchor_once()
        first = smp.content_lag_seconds
        self.assertGreater(first, 0.0)
        before = smp._content_pos
        self.consumed = before + 400  # the cushion used up, and then some
        with self.assertLogs("c64cast.audio.sampler", "DEBUG"):
            self.assertTrue(self._write(40))
        self.assertEqual(smp._reanchors, 2)
        shift = self.consumed + smp._reanchor_lead - before
        self.assertAlmostEqual(
            smp.content_lag_seconds, first + shift / smp.bps / smp._actual_rate, places=9
        )

    def _fail_reanchors(self, retries: int) -> None:
        # A link outage under a producer already shown slow: every retry is
        # late past the anchor the last one moved to, and re-anchors at once.
        smp = self.smp
        with mock.patch.object(self.api, "reu_write", side_effect=OSError("link down")):
            for _ in range(retries):
                self.consumed = max(self.consumed, smp._content_pos) + 400
                with self.assertNoLogs("c64cast.audio.sampler"), self.assertRaises(OSError):
                    self._write(40)
                smp._carry = None  # the writer's next pass takes the carry back

    def test_a_reanchor_whose_write_failed_counts_once_it_lands(self):
        # An outage's failed re-anchors do not count or log as re-anchors
        # until one lands, and then they land as one.
        smp = self.smp
        self._reanchor_once()
        before_lag, before_pos = smp.content_lag_seconds, smp._content_pos
        self._fail_reanchors(20)
        self.assertEqual(smp._reanchors, 1)
        self.consumed = smp._content_pos + 400
        with self.assertLogs("c64cast.audio.sampler", "DEBUG") as logs:
            self.assertTrue(self._write(40))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("(re-anchor 2)", logs.output[0])
        self.assertEqual(smp._reanchors, 2)
        shift = self.consumed + smp._reanchor_lead - before_pos
        self.assertAlmostEqual(
            smp.content_lag_seconds, before_lag + shift / smp.bps / smp._actual_rate, places=9
        )

    def test_a_pending_reanchor_counts_in_the_content_lag_until_the_writer_gives_up(self):
        # The writer retries at the pending anchor, so once the link is back
        # the sound lags by it. Left out of the lag, an audio-file scene's
        # end read the clock as caught up and cut the scene mid-outage. Once
        # the writer gives up nothing more lands, and the scene would sit the
        # pending shift out on silence, so only what landed counts.
        smp = self.smp
        self._reanchor_once()
        before_lag, before_pos = smp.content_lag_seconds, smp._content_pos
        self._fail_reanchors(20)
        assert smp._unlanded_reanchor is not None
        pending = smp._content_pos - before_pos
        self.assertEqual(smp._unlanded_reanchor[0], pending)
        self.assertAlmostEqual(
            smp.content_lag_seconds, before_lag + pending / smp.bps / smp._actual_rate, places=9
        )
        smp._failed = True
        self.assertEqual(smp.content_lag_seconds, before_lag)

    def test_a_reanchor_given_up_inside_its_hold_leaves_the_landed_lag_whole(self):
        # The writer gives up with the read head still inside the hold of the
        # latest re-anchor, whose write never landed. The part of its shift
        # the head has not crossed is already out of the lag; taking the
        # whole shift off on top took that part twice, and the heard position
        # ran ahead of the sound that did land by as much.
        smp = self.smp
        self._reanchor_once()
        landed = smp.content_lag_seconds
        self._fail_reanchors(3)
        uncrossed = (smp._content_pos - self.consumed) / smp.bps / smp._actual_rate
        self.assertGreater(uncrossed, 0.0)  # inside the latest hold
        self.assertAlmostEqual(
            smp.reanchor_lag_seconds(), smp.content_lag_seconds - uncrossed, places=9
        )
        smp._failed = True
        self.assertAlmostEqual(smp.reanchor_lag_seconds(), landed, places=9)
        self.consumed = smp._content_pos + 40  # and past it
        self.assertAlmostEqual(smp.reanchor_lag_seconds(), landed, places=9)

    def test_a_reanchor_given_up_inside_a_landed_hold_leaves_that_hold_in_the_lag(self):
        # A sticky re-anchor can come up to a flush margin before the landed
        # one's anchor, with the head still inside that one's hold. Given up,
        # the lag is the landed one's as it was: the head has not crossed the
        # rest of its hold, so the heard sample must not jump past it.
        smp = self.smp
        self._reanchor_once()
        hold_end = smp._reanchor_lag[1][-1][1]
        self.consumed = smp._content_pos - smp._flush_margin + smp.bps  # late by one sample
        self.assertLess(self.consumed, hold_end)
        before = smp.reanchor_lag_seconds()
        self.assertLess(before, smp.content_lag_seconds)  # inside the landed hold
        with mock.patch.object(self.api, "reu_write", side_effect=OSError("link down")):
            with self.assertNoLogs("c64cast.audio.sampler"), self.assertRaises(OSError):
                self._write(40)
        smp._carry = None
        self.assertIsNotNone(smp._unlanded_reanchor)
        smp._failed = True
        self.assertAlmostEqual(smp.reanchor_lag_seconds(), before, places=9)
        self.consumed = hold_end + 40
        self.assertAlmostEqual(smp.reanchor_lag_seconds(), smp.content_lag_seconds, places=9)

    def test_a_given_up_lag_is_taken_at_its_own_head_for_an_earlier_position(self):
        # A position read before the head the lag was worked out at gets the
        # sample heard at that head, given up or not: taken at the earlier
        # position, the given-up lag put the heard sample behind what a fresh
        # read had already reported.
        smp = self.smp
        self._reanchor_once()
        self._fail_reanchors(3)
        smp._failed = True
        step = 40 / smp.bps / smp._actual_rate
        earlier = (self.consumed - 40 + 0.5) / smp._actual_rate  # 40 samples before the head
        fresh = smp.reanchor_lag_seconds()
        self.assertAlmostEqual(smp.reanchor_lag_seconds(earlier), fresh - step, places=9)

    def _hook_lag_fields(self, on_get: Any = None, on_set: Any = None) -> None:
        # content_lag_seconds reads the lag and the pending re-anchor without
        # _io_lock, so the writer can land a re-anchor between its reads, or
        # the reader can read between the writer's stores. A subclass whose
        # fields run a hook on each access puts the other thread there
        # deterministically.
        smp = self.smp

        def field(name: str) -> property:
            def get(obj: Any) -> Any:
                value = obj.__dict__[name]
                if on_get is not None:
                    on_get()
                return value

            def put(obj: Any, value: Any) -> None:
                obj.__dict__[name] = value
                if on_set is not None:
                    on_set()

            return property(get, put)

        cls = type(smp)
        fields = ("_reanchor_lag", "_unlanded_reanchor")
        smp.__class__ = type("Hooked", (cls,), {name: field(name) for name in fields})
        self.addCleanup(setattr, smp, "__class__", cls)

    def _pending_reanchor(self) -> float:
        """A re-anchor left pending by an outage; returns the content lag
        it lands at, which a lock-free reader must never read short of."""
        smp = self.smp
        self._reanchor_once()
        self._fail_reanchors(3)
        assert smp._unlanded_reanchor is not None
        return smp.content_lag_seconds

    def test_a_reader_between_the_landing_writes_does_not_miss_the_shift(self):
        smp = self.smp
        lands_at = self._pending_reanchor()
        seen: list[float] = []
        self._hook_lag_fields(on_set=lambda: seen.append(smp.content_lag_seconds))
        self.consumed = smp._content_pos - smp._flush_margin  # on time at the anchor
        with self.assertLogs("c64cast.audio.sampler", "DEBUG"):
            self.assertTrue(self._write(40))
        self.assertIsNone(smp._unlanded_reanchor)
        # The lag took the shift in when the re-anchor was made, so landing
        # stores only the pending re-anchor's clearing: one read after it.
        self.assertEqual(len(seen), 1)
        self.assertGreaterEqual(min(seen), lands_at)
        self.assertAlmostEqual(smp.content_lag_seconds, lands_at, places=9)

    def test_a_landing_between_the_readers_reads_does_not_hide_the_shift(self):
        smp = self.smp
        lands_at = self._pending_reanchor()
        landings: list[int] = []

        def land_once() -> None:
            if not landings:
                landings.append(1)
                smp._land_reanchor()

        self._hook_lag_fields(on_get=land_once)
        with self.assertLogs("c64cast.audio.sampler", "DEBUG"):
            read = smp.content_lag_seconds  # the writer lands after its first read
        self.assertIsNone(smp._unlanded_reanchor)
        self.assertGreaterEqual(read, lands_at)

    def test_a_retried_reanchor_rewrites_the_slots_its_failed_write_reached(self):
        # The re-anchored write split at the ring's end and only its first
        # slice landed. Retried before the reader nears that anchor, it goes
        # back there: re-anchored afresh a back-off later, past slots the
        # underrun pads had already claimed, the reader played the landed
        # head and then the same audio again from the new anchor.
        smp = self.smp
        self._reanchor_once()
        before_lag, before_pos = smp.content_lag_seconds, smp._content_pos
        ring = smp.ring_size
        anchor = ((before_pos + 1000 + smp._reanchor_lead) // ring + 1) * ring - 20
        self.consumed = anchor - smp._reanchor_lead
        smp._written = self.consumed + smp._lead_target  # underrun pads ran ahead
        data = bytes(range(1, 41))
        land = self.api.reu_write
        calls = []

        def second_slice_fails(offset: int, chunk: bytes) -> None:
            calls.append(offset)
            if len(calls) == 2:
                raise OSError("link down")
            land(offset, chunk)

        with mock.patch.object(self.api, "reu_write", side_effect=second_slice_fails):
            with self.assertRaises(OSError):
                smp._write_payload(smp._writer_gen, smp._flush_epoch, data)
        self.assertEqual(self.api.reu_bytes(smp.ring_base + anchor % ring, 20), data[:20])
        smp._carry = None  # the writer's next pass takes the carry back
        self.consumed += 10  # one short back-off later
        with self.assertLogs("c64cast.audio.sampler", "DEBUG"):
            self.assertTrue(smp._write_payload(smp._writer_gen, smp._flush_epoch, data))
        self.assertEqual(smp._content_pos, anchor + 40)
        self.assertEqual(self.api.reu_bytes(smp.ring_base + anchor % ring, 20), data[:20])
        self.assertEqual(self.api.reu_bytes(smp.ring_base, 20), data[20:])
        self.assertEqual(smp._reanchors, 2)
        self.assertAlmostEqual(
            smp.content_lag_seconds,
            before_lag + (anchor - before_pos) / smp.bps / smp._actual_rate,
            places=9,
        )

    def test_a_splice_or_arm_clears_the_content_lag(self):
        # A splice anchors the next audio afresh, and arm() starts an
        # activation whose clock and content both begin at zero.
        smp = self.smp
        self._reanchor_once()
        smp.flush()
        self.assertEqual(smp.content_lag_seconds, 0.0)
        self._reanchor_once()
        smp.arm()
        self.assertEqual(smp.content_lag_seconds, 0.0)

    def test_a_splice_or_arm_drops_a_reanchor_whose_write_never_landed(self):
        # A re-anchor waits for a write to land before it counts. A splice or
        # arm() anchors the audio afresh, so the first write after it lands at
        # the new anchor and must not count the old, abandoned one.
        smp = self.smp
        for cut in (smp.flush, smp.arm):
            self._reanchor_once()
            reanchors = smp._reanchors
            self.consumed = max(self.consumed, smp._content_pos) + 400
            with mock.patch.object(self.api, "reu_write", side_effect=OSError("link down")):
                with self.assertRaises(OSError):
                    self._write(40)
            self.assertIsNotNone(smp._unlanded_reanchor)
            cut()
            if cut == smp.arm:
                reanchors = 0
                smp._written = smp._content_pos = self.consumed + smp._flush_margin
            with self.assertNoLogs("c64cast.audio.sampler"):
                self.assertTrue(self._write(40))
            self.assertEqual(smp._reanchors, reanchors)
            self.assertEqual(smp.content_lag_seconds, 0.0)

    def test_a_splice_or_arm_clears_the_immediate_reanchor(self):
        smp = self.smp
        self._reanchor_once()
        smp.flush()
        self.consumed += smp._reanchor_lead
        self.assertFalse(self._write(40))  # dropped: a fresh window
        self.assertEqual(smp._reanchors, 1)
        self._reanchor_once()
        smp.arm()
        self.consumed = 5000
        self.assertFalse(self._write(40))
        self.assertEqual(smp._reanchors, 0)

    def test_a_producer_stall_between_late_writes_restarts_the_window(self):
        # The last chunk before a stall lands a little late; the next arrives
        # after the stall. Nothing was dropped in between, so the backlog
        # burst that follows gets a whole window to catch up in.
        smp = self.smp
        self.consumed = 1000
        smp._written = smp._content_pos = self.consumed + smp._flush_margin
        self.assertTrue(self._write(10))  # playing on time until now
        self.consumed += 20
        self.assertTrue(self._write(100))  # 10 bytes late: a window begins
        self.consumed += 2 * smp._late_reanchor_bytes  # the stall
        self.assertFalse(self._write(100))
        self.assertEqual(smp._reanchors, 0)

    def test_a_bursty_producer_that_stays_late_is_still_reanchored(self):
        # A segmented live stream that fell seconds behind: each segment's
        # frames arrive in a burst the writer drops in no read-head time, then
        # nothing until the next segment. A burst ends no closer than the one
        # before it; timing each burst on its own would leave the scene
        # silent for good. Two whole bursts are what show it.
        smp = self.smp
        self.consumed = 3 * int(smp._actual_rate)
        for _ in range(2):
            for _ in range(10):  # a segment's burst, dropped whole
                self.assertFalse(self._write(40))
            self.consumed += 2 * smp._late_reanchor_bytes  # the next segment
        with self.assertLogs("c64cast.audio.sampler", "WARNING") as logs:
            self.assertTrue(self._write(40))
        self.assertEqual(smp._reanchors, 1)
        # Late since the first burst, two segments ago: not one window.
        self.assertIn("arrived late for 2.0 s", logs.output[0])

    def test_a_late_window_spans_no_earlier_activation(self):
        # The gap test reads the latest attempt's read-head position, which
        # arm() resets with the read head itself.
        smp = self.smp
        smp._last_try = 10 * smp._late_reanchor_bytes
        smp._prev_start = smp._burst_start = (5, 5)
        smp.arm()
        self.assertEqual(
            (smp._last_try, smp._burst_start, smp._prev_start, smp._late_ref),
            (None, None, None, None),
        )

    def test_a_splice_restarts_the_late_window(self):
        # The demuxer's re-seek delay makes the first post-splice audio late;
        # that is a fresh run, not a continuation of the one before the splice.
        smp = self.smp
        self.consumed = 1000
        self.assertFalse(self._write(100))
        self.consumed += smp._late_reanchor_bytes
        smp.flush()
        self.consumed += 100
        self.assertTrue(self._write(400))
        self.assertEqual(smp._reanchors, 0)

    def _writes_after_a_reanchor(self, quanta: int, *, late: bool) -> int:
        """Audible writes for ``quanta`` write quanta of a real-time 10-byte
        frame feed, after it stayed late long enough to be re-anchored. With
        ``late``, every other frame from then on arrives HOLD_GUARD_S behind
        its schedule."""
        api = _FakeBackend()
        smp = _make(api, sample_rate=2000, bits=8, ring_size=0x10000, lead_seconds=4.0)
        smp._running = True
        behind = [0]
        smp._read_consumed_bytes = lambda: self.consumed + behind[0]  # type: ignore[method-assign]
        self.consumed = 3 * int(smp._actual_rate)

        def frame() -> None:
            smp._q.put((smp._flush_epoch, b"\x01" * 10))
            smp._writer_step(smp._writer_gen)
            self.consumed += 10

        with self.assertLogs("c64cast.audio.sampler", "WARNING"):
            while smp._reanchors == 0:
                frame()
        before = api.audible_writes
        for n in range(quanta * smp._write_quantum // 10):
            behind[0] = smp._hold_guard if late and n % 2 else 0
            frame()
        self.assertEqual(smp._reanchors, 1)
        return api.audible_writes - before

    def test_a_reanchored_real_time_producer_is_coalesced(self):
        # The re-anchor leaves a live stream enough slack over the write floor
        # to be held to whole quanta, not written one small frame at a time.
        self.assertLessEqual(self._writes_after_a_reanchor(10, late=False), 11)

    def test_a_reanchored_producer_whose_frames_arrive_late_is_coalesced(self):
        # A live stream's frames do not arrive on the sample: one a little
        # behind its schedule must not tip the hold into writing it alone.
        self.assertLessEqual(self._writes_after_a_reanchor(10, late=True), 11)

    def test_the_lead_summary_is_what_the_ring_holds_ahead_of_the_reader(self):
        # Paused or late, the audio's anchor falls far behind the reader while
        # pads keep the ring itself ahead; the summary reports the ring.
        smp = self.smp
        self.consumed = 5000
        smp._content_pos = 0
        smp._written = self.consumed + 800
        smp._writer_step(smp._writer_gen)
        with self.assertLogs("c64cast.audio.sampler", "INFO") as logs:
            smp.stop()
        self.assertTrue(any("lead min=800 max=800" in m for m in logs.output), logs.output)

    def test_repeated_reanchors_are_summarized_at_stop_and_cleared_by_arm(self):
        smp = self.smp
        smp._reanchors = 3
        with self.assertLogs("c64cast.audio.sampler", "WARNING") as logs:
            smp.stop()
        self.assertTrue(any("re-anchored late audio 3 times" in m for m in logs.output))
        smp._reanchors = 3
        smp.arm()
        self.assertEqual(smp._reanchors, 0)


class _ScenarioLink(_FakeBackend):
    """The matrix's fake link: counts REU writes (in total and per sim
    second) and the non-silent bytes they carry, and fails every write
    inside ``outage`` (sim seconds)."""

    def __init__(self, clock: list[float], outage: tuple[float, float] | None) -> None:
        super().__init__()
        self.clock = clock
        self.outage = outage
        self.writes = 0
        self.per_second: dict[int, int] = {}
        self.audible = 0

    def reu_write(self, offset: int, data: bytes) -> None:
        if self.outage is not None and self.outage[0] <= self.clock[0] < self.outage[1]:
            raise ConnectionError("link down")
        self.writes += 1
        second = int(self.clock[0])
        self.per_second[second] = self.per_second.get(second, 0) + 1
        self.audible += len(data) - data.count(0)


class _ScenarioQueue:
    """The sampler's queue without its blocking wait: on the fake clock a
    20 ms wait is time that does not pass."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def put(self, item: Any, *_a: Any, **_kw: Any) -> None:
        self.items.append(item)

    def get(self, *_a: Any, **_kw: Any) -> Any:
        if not self.items:
            raise s.queue.Empty
        return self.items.pop(0)

    get_nowait = get

    def empty(self) -> bool:
        return not self.items

    def qsize(self) -> int:
        return len(self.items)


class _NoSleep:
    """Stands in for the sampler module's ``time``: its sleeps do not pass
    the fake clock, and the writer's monotonic is that clock."""

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock

    def sleep(self, _s: float) -> None:
        pass

    def monotonic(self) -> float:
        return self.clock[0]


def _run_scenario(
    rate_of: Any,
    *,
    seconds: float,
    sample_rate: int = 8000,
    bits: int = 16,
    lead_seconds: float = 1.0,
    prebuffer_s: float = 0.5,
    frame_s: float = 0.01,
    outage: tuple[float, float] | None = None,
    splice_at: float | None = None,
    tail_s: float = 4.0,
    waits_for_frames: bool = False,
) -> dict[str, float]:
    """Drive the real sampler's writer on a fake clock against a fake link.

    ``rate_of(t, produced_s, lateness_s)`` is the producer's speed (1.0 is
    real time) given the sim time, the audio it has produced, and how late
    the audio's anchor is. The writer steps until idle every ``frame_s``,
    and a step that raises (an outage) is retried on the next tick, as the
    writer loop's back-off would. With ``waits_for_frames`` it does not step
    on an empty queue with nothing carried, as on hardware, where the
    queue's 20 ms wait outlasts a real-time producer's next frame; stepped,
    it pads, and a pad ahead of the write floor hides what a late write
    blanks. Returns the re-anchor count, the lag of
    the audio behind its anchor (ms), and over the last ``tail_s`` the share
    of real time the ring got audio for and the REU writes per second; the
    most REU writes in any whole second of the run; and the audio still
    queued or carried, unwritten, at the end (ms); and the audio dropped as
    late over the whole run (ms)."""
    clock = [0.0]
    link = _ScenarioLink(clock, outage)
    smp = _make(link, sample_rate=sample_rate, bits=bits, lead_seconds=lead_seconds)
    rate = smp._actual_rate
    bps = smp.bps
    smp._running = True
    smp._read_consumed_bytes = lambda: int(clock[0] * rate) * bps  # type: ignore[method-assign]
    pre = int(prebuffer_s * rate) * bps
    smp._written = smp._content_pos = pre
    q = _ScenarioQueue()
    smp._q = q  # type: ignore[assignment]
    frame = max(1, int(frame_s * rate))
    produced = 0
    pending = 0.0
    base_shift = 0
    tail_from = seconds - tail_s
    tail_writes = tail_audible = None
    ticks = int(round(seconds / frame_s))
    with mock.patch.object(s, "time", _NoSleep(clock)):
        for tick in range(ticks):
            t = tick * frame_s
            lateness_s = (
                (smp._read_consumed_bytes() + smp._flush_margin - smp._content_pos) / bps / rate
            )
            pending += rate_of(t, produced / rate, lateness_s) * frame
            while pending >= frame:
                pending -= frame
                q.put((smp._flush_epoch, b"\x01" * (frame * bps)))
                produced += frame
            for _ in range(200):
                if waits_for_frames and q.empty() and smp._carry is None:
                    break
                try:
                    wrote = smp._writer_step(smp._writer_gen)
                except ConnectionError:
                    break
                if not wrote and q.empty():
                    break
            clock[0] = (tick + 1) * frame_s
            if splice_at is not None and abs(clock[0] - splice_at) < frame_s / 2:
                smp.flush()
                base_shift = smp._content_pos - (pre + produced * bps)
            if tail_writes is None and clock[0] >= tail_from:
                tail_writes, tail_audible = link.writes, link.audible
    assert tail_writes is not None and tail_audible is not None
    held = sum(len(item[1]) for item in q.items) + (len(smp._carry[1]) if smp._carry else 0)
    delivered = produced * bps - held
    return {
        "reanchors": smp._reanchors,
        "lag_ms": (smp._content_pos - (pre + delivered) - base_shift) / bps / rate * 1000,
        "audible": (link.audible - tail_audible) / (tail_s * rate * bps),
        "writes_s": (link.writes - tail_writes) / tail_s,
        "peak_writes_s": max(
            (n for sec, n in link.per_second.items() if sec + 1 <= seconds),
            default=0,
        ),
        "held_ms": held / bps / rate * 1000,
        "dropped_ms": smp._late_bytes / bps / rate * 1000,
    }


def _stall(start: float, length: float, then: Any) -> Any:
    def rate_of(t: float, prod: float, late: float) -> float:
        if t < start:
            return 1.0
        if t < start + length:
            return 0.0
        return then(t, prod, late)

    return rate_of


def _until_caught_up(fast: float) -> Any:
    # A decoder with a backlog: fast until it has produced up to the wall
    # clock (the prebuffer was produced ahead of it), then real time.
    return lambda t, prod, late: fast if prod < t else 1.0


def _segments(burst: float) -> Any:
    """A live stream that stalled 3 s and resumed segmented: ``burst``/10 s
    of audio fetched in a 0.1 s burst every 2 s, so it stays seconds
    behind (at 20x it never gains; above that it gains slowly)."""

    def rate_of(t: float, prod: float, late: float) -> float:
        if t < 2.0:
            return 1.0
        if t < 5.0:
            return 0.0
        return burst if (t - 5.0) % 2.0 < 0.1 else 0.0

    return rate_of


def _hiccups(t: float, prod: float, late: float) -> float:
    # A decoder that stalls 1.2 s every 4 s and catches up at 3x between.
    if t < 2.0:
        return 1.0
    if (t - 2.0) % 4.0 < 1.2:
        return 0.0
    return 3.0 if prod < t else 1.0


def _chunk_dropped_before_a_stall(t: float, prod: float, late: float) -> float:
    # A stall long enough to make the audio late, a moment at 0.5x whose
    # chunks are dropped whole, a 1.2 s stall, then the backlog at 10x.
    if t < 3.0:
        return 1.0
    if t < 4.3:
        return 0.0
    if t < 4.4:
        return 0.5
    if t < 5.6:
        return 0.0
    return 10.0 if prod < t else 1.0


def _live_seek(delay: float, jitter_s: float, seed: int) -> Any:
    """A live stream on the wall clock, each frame up to ``jitter_s`` late
    (seeded, so a row replays the same jitter), seeked at 2 s: it resumes
    ``delay`` later, behind the anchor by that delay for good."""

    def rate_of(t: float, prod: float, late: float) -> float:
        if 2.0 <= t < 2.0 + delay:
            return 0.0
        behind = delay if t >= 2.0 else 0.0
        jitter = random.Random(seed * 1_000_003 + round(t * 1e4)).uniform(0.0, jitter_s)
        return 4.0 if prod < t - behind - jitter else 0.0

    return rate_of


def _late_once_then_near(stall_s: float, stays_late: bool) -> Any:
    """Real time with its anchor 6-9 ms ahead of the write floor (run it
    from a 0.16 s prebuffer), one write 11 ms late after a 20 ms stall at
    2 s, then a ``stall_s`` stall at 12 s, after which it catches straight
    up or, with ``stays_late``, stays that far behind."""

    def rate_of(t: float, prod: float, late: float) -> float:
        if 2.0 <= t < 2.02 or 12.0 <= t < 12.0 + stall_s:
            return 0.0
        behind = stall_s if stays_late and t >= 12.0 else 0.0
        return 4.0 if prod < t - behind else 0.0

    return rate_of


def _behind_then(speed: float) -> Any:
    """Stalls at 3 s until 0.35 s late, then decodes at ``speed`` until
    caught up, then at real time."""
    resumed = [False]

    def rate_of(t: float, prod: float, late: float) -> float:
        if t < 3.0:
            resumed[0] = False
            return 1.0
        if not resumed[0]:
            if late < 0.35:
                return 0.0
            resumed[0] = True
        return speed if prod < t else 1.0

    return rate_of


class SamplerScenarioMatrixTest(unittest.TestCase):
    """The lateness-trend re-anchor rule, run end to end through the real
    sampler on a fake clock and a fake link. Each row is a producer the
    rule has to answer; the outcome is what a listener would get."""

    # name: (rate_of, run kwargs, expectations). Expectations: reanchors as
    # an exact count or a (min, max) range, lag_ms as a (min, max) range,
    # audible as a minimum share, writes_s as a (min, max) range, held_ms as
    # a maximum, dropped_ms as a maximum. Every row also holds its busiest whole second, splices and
    # first late windows included, to PEAK_WRITES_S: the link carries about
    # 200 writes/s, shared with the picture.
    PEAK_WRITES_S = 60

    ROWS: dict[str, tuple[Any, dict[str, Any], dict[str, Any]]] = {
        "0.95x decoder": (
            lambda t, prod, late: 0.95,
            {"seconds": 20.0, "tail_s": 8.0},
            {"reanchors": (1, 99), "lag_ms": (300, 2000), "audible": 0.9},
        ),
        "live 1.0x after a 2 s stall": (
            _stall(3.0, 2.0, lambda t, prod, late: 1.0),
            {"seconds": 12.0},
            {"reanchors": 1, "lag_ms": (500, 2500), "audible": 0.95},
        ),
        "prebuffer timeout start": (
            lambda t, prod, late: 0.0 if t < 3.0 else 1.0,
            {"seconds": 10.0, "prebuffer_s": 0.0},
            {"reanchors": 1, "lag_ms": (2000, 4500), "audible": 0.95},
        ),
        "8x burst after a 2 s stall": (
            _stall(3.0, 2.0, _until_caught_up(8.0)),
            {"seconds": 12.0},
            {"reanchors": 0, "lag_ms": (0, 0), "audible": 0.95},
        ),
        "1.5x decoder 0.35 s behind": (
            _behind_then(1.5),
            {"seconds": 12.0},
            {"reanchors": 0, "lag_ms": (0, 0), "audible": 0.95},
        ),
        # The same from a 1.0 s lead: the hold carries its partial quantum
        # through LATE_REANCHOR_S of the stall, so the gather written short as
        # it lets go is the attempt after a gap, and must not start a burst
        # there either (it re-anchored this decoder, 0.51 s lag).
        "1.5x decoder 0.35 s behind, from a 1.0 s lead": (
            _behind_then(1.5),
            {"seconds": 12.0, "prebuffer_s": 1.0},
            {"reanchors": 0, "lag_ms": (0, 0), "audible": 0.95},
        ),
        # Catching up 100 ms a window, lined up in 1.75 s: inside
        # LATE_CATCHUP_S by only 12 ms a window, so a pace test that took the
        # write interval's 20 ms off the gain re-anchored it (0.44 s lag).
        "1.2x decoder 0.35 s behind": (
            _behind_then(1.2),
            {"seconds": 12.0},
            {"reanchors": 0, "lag_ms": (0, 0), "audible": 0.95},
        ),
        # Catching up, but 50 ms a window: it would drop everything for
        # 3.5 s before lining up, so it is re-anchored. At 44.1 kHz that gain
        # is two write quanta, so slicing jitter is not what decides it.
        "1.1x decoder 0.35 s behind": (
            _behind_then(1.1),
            {"seconds": 12.0, "sample_rate": 44100},
            {"reanchors": 1, "audible": 0.95},
        ),
        "chunk before a 1.2 s stall dropped whole, then 10x": (
            _chunk_dropped_before_a_stall,
            {"seconds": 12.0},
            {"reanchors": 0, "lag_ms": (0, 0), "audible": 0.95},
        ),
        "segmented bursty stream seconds behind": (
            _segments(20.0),
            {"seconds": 24.0, "tail_s": 6.0},
            {"reanchors": (1, 2), "audible": 0.9},
        ),
        # Each burst starts 50 ms closer (two write quanta at 44.1 kHz): at
        # that pace it would take a minute to line up, so it is re-anchored.
        "segmented stream gaining 50 ms a burst": (
            _segments(20.5),
            {"seconds": 24.0, "tail_s": 6.0, "sample_rate": 44100},
            {"reanchors": (1, 2), "audible": 0.9},
        ),
        # Late after every stall but on time between them: each stall is a
        # fresh case, not the next burst of a stream that stays behind.
        "1.2 s stall every 4 s, caught up between": (
            _hiccups,
            {"seconds": 24.0, "tail_s": 8.0},
            {"reanchors": 0, "lag_ms": (0, 0), "audible": 0.6},
        ),
        "1.5 s link outage with retries": (
            lambda t, prod, late: 1.0,
            {"seconds": 12.0, "outage": (3.0, 4.5)},
            {"reanchors": 0, "lag_ms": (0, 0), "audible": 0.95},
        ),
        "shallow lead (0.15 s at 2 kHz/8-bit)": (
            lambda t, prod, late: 0.0 if t < 1.0 else 1.0,
            {
                "seconds": 6.0,
                "sample_rate": 2000,
                "bits": 8,
                "lead_seconds": 0.15,
                "prebuffer_s": 0.0,
                "tail_s": 3.0,
            },
            # The lead target is the write floor itself, so every write is
            # late and re-anchored; what the row pins is that audio lands.
            {"reanchors": (1, 99), "audible": 0.45},
        ),
        # Slightly slow live streams in 2.5 ms frames: each re-anchor's
        # cushion runs out at the shortfall, and the writes must stay whole
        # quanta while it does (they fell to one per frame, ~400/s, for the
        # last ~50 ms of every cushion).
        "0.99x live after a 1.5 s stall": (
            lambda t, prod, late: 0.0 if 3.0 <= t < 4.5 else 0.99,
            {"seconds": 26.0, "sample_rate": 44100, "frame_s": 0.0025, "tail_s": 16.0},
            {"reanchors": (2, 99), "audible": 0.95},
        ),
        "0.97x live": (
            lambda t, prod, late: 0.97,
            {"seconds": 20.0, "sample_rate": 44100, "frame_s": 0.0025, "tail_s": 10.0},
            {"reanchors": (1, 99), "audible": 0.95},
        ),
        "2.5 ms frames at real time": (
            lambda t, prod, late: 1.0,
            {"seconds": 6.0, "sample_rate": 44100, "frame_s": 0.0025, "tail_s": 3.0},
            {"reanchors": 0, "lag_ms": (0, 0), "audible": 0.95, "writes_s": (30, 50)},
        ),
        "2.5 ms frames at real time, after a splice": (
            lambda t, prod, late: 1.0,
            {
                "seconds": 6.0,
                "sample_rate": 44100,
                "frame_s": 0.0025,
                "splice_at": 2.0,
                "tail_s": 3.0,
            },
            {"audible": 0.95, "writes_s": (30, 50)},
        ),
        "2.5 ms frames at 8 kHz/8-bit, after a splice": (
            lambda t, prod, late: 1.0,
            {
                "seconds": 6.0,
                "sample_rate": 8000,
                "bits": 8,
                "frame_s": 0.0025,
                "splice_at": 2.0,
                "tail_s": 3.0,
            },
            {"audible": 0.95},
        ),
        # A first late window, before re-anchoring is sticky: frames that
        # land partly late each went out on their own (~210 writes/s).
        "1.0x live after a 0.33 s stall": (
            lambda t, prod, late: 0.0 if 3.0 <= t < 3.33 else 1.0,
            {"seconds": 10.0, "sample_rate": 44100, "frame_s": 0.0025},
            {"reanchors": (0, 1), "audible": 0.95},
        ),
        "0.9x live": (
            lambda t, prod, late: 0.9,
            {"seconds": 16.0, "sample_rate": 44100, "frame_s": 0.0025, "tail_s": 8.0},
            {"reanchors": (1, 99), "audible": 0.85},
        ),
        # Re-anchored once, then an 80 ms hiccup every second, caught up at
        # 2x: the cushion absorbs it, so there is no second re-anchor.
        "80 ms hiccups each second after a 2 s stall": (
            lambda t, prod, late: (
                0.0
                if 3.0 <= t < 5.0 or (t > 6.0 and t % 1.0 < 0.08)
                else (2.0 if t > 6.0 and t % 1.0 < 0.2 else 1.0)
            ),
            {"seconds": 16.0, "sample_rate": 44100, "frame_s": 0.0025},
            {"reanchors": 1, "audible": 0.95},
        ),
        # A jittered live stream seeked: its writes at the write floor swing
        # by up to the floor's interval. Read as catching up, or as on time,
        # that swing restarted the window, and the stream dropped about 1 s
        # before it was re-anchored.
        "seeked live, 20 ms jitter, resumed 30 ms late": (
            _live_seek(0.03, 0.02, 0),
            {
                "seconds": 4.5,
                "frame_s": 0.0025,
                "splice_at": 2.0,
                "tail_s": 1.0,
                "sample_rate": 44100,
            },
            {"reanchors": 1, "audible": 0.95, "dropped_ms": 550},
        ),
        "seeked live, 50 ms jitter, resumed at once": (
            _live_seek(0.0, 0.05, 2),
            {
                "seconds": 4.5,
                "frame_s": 0.0025,
                "splice_at": 2.0,
                "tail_s": 1.0,
                "sample_rate": 44100,
            },
            {"reanchors": 1, "audible": 0.95, "dropped_ms": 550},
        ),
        "seeked live, 50 ms jitter, resumed 30 ms late": (
            _live_seek(0.03, 0.05, 1),
            {
                "seconds": 4.5,
                "frame_s": 0.0025,
                "splice_at": 2.0,
                "tail_s": 1.0,
                "sample_rate": 44100,
            },
            {"reanchors": 1, "audible": 0.95, "dropped_ms": 550},
        ),
        # The same seek with the writer waiting on its queue, as on hardware:
        # each late gather's dropped head was blanked in a write of its own,
        # 72 REU writes in the second of the seek.
        "seeked live, 20 ms jitter, the writer waiting for frames": (
            _live_seek(0.0, 0.02, 0),
            {
                "seconds": 4.5,
                "frame_s": 0.0025,
                "splice_at": 2.0,
                "tail_s": 1.0,
                "sample_rate": 44100,
                "waits_for_frames": True,
            },
            {"reanchors": 1, "audible": 0.95, "dropped_ms": 550},
        ),
        "seeked live, 50 ms jitter, resumed at once (8 kHz/8-bit)": (
            _live_seek(0.0, 0.05, 1),
            {
                "seconds": 4.5,
                "frame_s": 0.0025,
                "splice_at": 2.0,
                "tail_s": 1.0,
                "sample_rate": 8000,
                "bits": 8,
            },
            {"reanchors": 1, "audible": 0.95, "dropped_ms": 550},
        ),
        # One late write, 10 s of writes 6-9 ms on time, then one 4 ms late:
        # the window the first opened must not still be open, or the second
        # re-anchors at once (0.2 s of lag).
        "one late write, 10 s just on time, then one more": (
            _late_once_then_near(0.009, stays_late=False),
            {"seconds": 16.0, "prebuffer_s": 0.16, "tail_s": 2.0},
            {"reanchors": 0, "lag_ms": (0, 0), "audible": 0.95},
        ),
        # The end of a stream after a re-anchor: the last partial gather is
        # written where it belongs, not re-anchored past a gap.
        "end of stream after a re-anchor": (
            lambda t, prod, late: 0.0 if 3.0 <= t < 5.0 or t >= 8.013 else 1.0,
            {"seconds": 10.0, "sample_rate": 44100, "frame_s": 0.0025, "tail_s": 1.0},
            {"reanchors": 1, "lag_ms": (1700, 1900), "audible": 0.0, "held_ms": 0.0},
        ),
    }

    def test_a_window_does_not_outlive_its_lateness_in_the_warning(self):
        # One late write at 2 s, then 10 s just on time, then late for good
        # at 12 s: the WARNING times the lateness from 12 s, not from 2 s.
        with self.assertLogs("c64cast.audio.sampler", "WARNING") as logs:
            got = _run_scenario(
                _late_once_then_near(0.3, stays_late=True),
                seconds=16.0,
                prebuffer_s=0.16,
                tail_s=2.0,
            )
        self.assertEqual(got["reanchors"], 1, got)
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn("arrived late for 0.5 s", logs.output[0])

    def run_row(self, name: str) -> dict[str, float]:
        rate_of, kwargs, _ = self.ROWS[name]
        with quiet_logging():
            return _run_scenario(rate_of, **kwargs)

    def test_each_producer_gets_the_outcome_it_should(self):
        for name, (_, _, expect) in self.ROWS.items():
            with self.subTest(name):
                got = self.run_row(name)
                want = expect.get("reanchors")
                if isinstance(want, tuple):
                    self.assertTrue(want[0] <= got["reanchors"] <= want[1], got)
                elif want is not None:
                    self.assertEqual(got["reanchors"], want, got)
                if "lag_ms" in expect:
                    lo, hi = expect["lag_ms"]
                    self.assertTrue(lo - 1 <= got["lag_ms"] <= hi + 1, got)
                self.assertGreaterEqual(got["audible"], expect["audible"], got)
                if "writes_s" in expect:
                    lo, hi = expect["writes_s"]
                    self.assertTrue(lo <= got["writes_s"] <= hi, got)
                if "held_ms" in expect:
                    self.assertLessEqual(got["held_ms"], expect["held_ms"], got)
                if "dropped_ms" in expect:
                    self.assertLessEqual(got["dropped_ms"], expect["dropped_ms"], got)
                self.assertLessEqual(got["peak_writes_s"], self.PEAK_WRITES_S, got)


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
        self.assertEqual(api.reu_bytes(0x200000 + anchor + 100, 100), bytes(range(101, 201)))
        self.assertEqual(api.audible_writes, 1, api.reu_writes)
        self.assertEqual(smp._content_pos, anchor + 200)

    def test_the_splice_anchor_is_the_read_head_when_flush_is_called(self):
        # Resume's flush restores the volume, and every flush waits for the
        # writer's REU write to release _io_lock. The read head moves on
        # meanwhile; the transport anchored the picture before either.
        api = _FakeBackend()
        consumed = [1000]
        smp = _make(api, sample_rate=2000, bits=8, ring_base=0x200000, ring_size=0x4000)
        smp._running = True
        smp._read_consumed_bytes = lambda: consumed[0]  # type: ignore[method-assign]
        smp._written = smp._content_pos = 1000 + 1500
        smp._output_silenced = True
        anchor = consumed[0] + round(smp.ring_lead_seconds() * smp._actual_rate) * smp.bps

        def slow_volume_write(_value: int) -> None:
            consumed[0] += 60

        smp._write_volume = slow_volume_write  # type: ignore[method-assign]
        smp.flush()
        self.assertEqual(smp._content_pos, anchor)
        self.assertEqual(smp._written, consumed[0] + smp._flush_margin)

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

    def test_audio_pushed_before_the_cut_over_is_written_at_the_anchor(self):
        # The demuxer pushes the seek target while flush() is still writing the
        # volume, and the writer reaches the lock first. Written then, the chunk
        # would land in the old lead and be blanked by the cut-over.
        api = _FakeBackend()
        smp = self._running(api, consumed=0)
        margin = self._margin(smp)
        smp._written = smp._content_pos = margin + 300
        smp._output_silenced = True  # resume: flush() restores the volume first

        left_queued: list[int] = []

        def writer_runs_during_the_volume_write(_value: int) -> None:
            smp._q.put((smp._flush_epoch, b"\x01" * 1024))
            smp._writer_step(smp._writer_gen)
            left_queued.append(smp._q.qsize())

        smp._write_volume = writer_runs_during_the_volume_write  # type: ignore[method-assign]
        smp.flush()
        api.reu_writes.clear()
        api.audible_writes = 0
        for _ in range(3):  # the chunk fills the lead, so the writer then idles
            smp._writer_step(smp._writer_gen)
        self.assertGreater(api.audible_writes, 0, "the seek target's first audio was lost")
        self.assertEqual(api.reu_writes[0][0], 0x200000 + margin)
        self.assertEqual(left_queued, [1], "the writer dequeued with a cut-over pending")

    def test_current_audio_waits_for_a_pending_cut_over(self):
        # flush() bumped after the writer's pass began, and its cut-over has
        # not run: the chunk is carried, not written into the doomed lead.
        api = _FakeBackend()
        smp = self._running(api, consumed=0)
        smp._flush_epoch += 1
        data = b"\x01" * 64
        self.assertFalse(smp._write_payload(smp._writer_gen, smp._flush_epoch, data))
        self.assertEqual(api.audible_writes, 0)
        assert smp._carry is not None
        self.assertEqual((smp._carry[0], bytes(smp._carry[1])), (smp._flush_epoch, data))

    def test_a_flush_that_fails_releases_the_writer(self):
        smp = self._running(_FakeBackend(), consumed=0)
        smp._output_silenced = True

        def link_down(_value: int) -> None:
            raise OSError("link down")

        smp._write_volume = link_down  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            smp.flush()
        self.assertEqual(smp._cut_epoch, smp._flush_epoch)

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
        parked = _signal_on_put(smp)

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
        self.assertTrue(parked.wait(2.0), "the producer never reached the full queue")
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
        tapped: list[np.ndarray] = []
        smp.analysis_sink = tapped.append
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
        # A file source reads the analysis tap at the slot each sample was
        # written for, which a dropped chunk never takes.
        self.assertEqual(tapped, [], "a dropped chunk entered the analysis tap")
        # A drain would free the slot and let the parked put through, which
        # leaves one item queued as well: the item itself tells them apart.
        self.assertEqual(
            [item for _, item in smp._q.queue], [b"\x01" * 32], "the parked put went through"
        )
        api.reu_writes.clear()
        api.audible_writes = 0
        self._drive_writer(smp, [smp._q.get_nowait()])
        self.assertEqual(api.audible_writes, 0, "the pre-splice chunk was written after the cut")

    def test_a_chunk_a_flush_overtakes_after_its_put_is_not_accepted(self):
        # The put went through, but the splice bumped the epoch before
        # push_samples counted it, so the writer drops the chunk on its stale
        # tag. Taken as accepted, it put a file source's length past audio
        # that never plays; counted, the EOF ceiling; tapped, a window the
        # analyzer reads at a slot the chunk never takes.
        api = _FakeBackend()
        smp = self._running(api, consumed=0)

        class _SplicedAfterPut(s.queue.Queue):  # type: ignore[type-arg]
            def put(self, *a: Any, **kw: Any) -> None:
                super().put(*a, **kw)
                smp._flush_epoch += 1  # flush()'s bump, just after the put

        smp._q = _SplicedAfterPut(maxsize=4)
        tapped: list[np.ndarray] = []
        smp.analysis_sink = tapped.append
        self.assertEqual(smp.push_samples(np.full(50, 8000, dtype=np.int16)), 0)
        self.assertEqual(smp._pushed_samples, 0)
        self.assertEqual(tapped, [])

    def test_a_flush_between_dequeue_and_write_drops_the_chunk(self):
        api = _FakeBackend()
        smp = self._running(api, consumed=0)

        def flush_lands() -> None:
            smp.flush()

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
    def _cfg(self, *, bits=16, rate=44100, enabled=True, clock=None):
        cfg = cfgmod.Config()
        cfg.audio.enabled = enabled
        cfg.audio.sampler_bits = bits
        cfg.audio.sampler_sample_rate = rate
        if clock is not None:
            cfg.audio.sampler_clock_hz = clock
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

    def test_clock_range_edges_pass_and_slips_are_rejected(self):
        lo, hi = scene_factory.SAMPLER_CLOCK_HZ_RANGE
        for ok in (lo, 6_160_000, 6_250_000, hi):
            scene_factory.validate_sampler_cfg(self._cfg(clock=ok))  # no raise
        # Zero leaves a 0 Hz rate to divide by; the others are unit slips.
        for bad in (0, -6_160_000, 6160, lo - 1, hi + 1, 61_600_000):
            with self.subTest(clock=bad), self.assertRaises(cfgmod.ConfigError):
                scene_factory.validate_sampler_cfg(self._cfg(clock=bad))

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


class _OrderedBackend(_FakeBackend):
    """A `_FakeBackend` that also keeps every call in one ordered list, so a
    test can check what was flushed before what."""

    def __init__(self) -> None:
        super().__init__()
        self.ops: list[tuple[Any, ...]] = []

    def reu_write(self, offset: int, data: bytes) -> None:
        super().reu_write(offset, data)
        self.ops.append(("reu", offset))

    def write_regs(self, base_addr: str, *values: int) -> None:
        super().write_regs(base_addr, *values)
        self.ops.append(("regs", base_addr.upper(), values))

    def write_memory(self, address: str, data_hex: str) -> None:
        super().write_memory(address, data_hex)
        self.ops.append(("mem", address.upper(), data_hex.upper()))

    def flush(self) -> None:
        super().flush()
        self.ops.append(("flush",))


class SamplerStartProgramTest(unittest.TestCase):
    def test_start_programs_the_whole_channel_before_its_gate(self):
        api = _OrderedBackend()
        ring = 8192
        smp = _make(
            api, sample_rate=8000, bits=8, volume=40, pan=3, ring_base=0x200000, ring_size=ring
        )
        smp.start(prebuffer_timeout=0.01)
        with quiet_logging():  # the idle writer's pads are not the subject
            smp.stop()
        base = s.SAMPLER_IO_BASE
        regs = {o[1]: o[2] for o in api.ops if o[0] == "regs"}
        self.assertEqual(regs[f"{base + s.REG_VOLUME:04X}"], (40,))
        self.assertEqual(regs[f"{base + s.REG_PAN:04X}"], (3,))
        self.assertEqual(regs[f"{base + s.REG_REPEAT_B:04X}"], (0x00, 0x20, 0x00))  # 8192 BE
        ctrl = f"{s.control_byte(gate=True, repeat=True, bits=8):02X}"
        gate = api.ops.index(("mem", f"{base:04X}", ctrl))
        last_reg = max(i for i, o in enumerate(api.ops) if o[0] == "regs")
        first_reg = min(i for i, o in enumerate(api.ops) if o[0] == "regs")
        last_prefill = max(i for i, o in enumerate(api.ops[:first_reg]) if o[0] == "reu")
        # The prefill lands before any register goes out, and every register
        # before the gate starts the FPGA reading.
        self.assertIn(("flush",), api.ops[last_prefill:first_reg])
        self.assertIn(("flush",), api.ops[last_reg:gate])


class _LossyBringUpBackend(_FakeBackend):
    """Loses the bring-up's REU writes (those not from the writer thread)
    that ``lose`` picks, ``times`` of them (-1 = every one): nothing lands and
    ``delivery_epoch`` moves, the way a lossy redial drops a write."""

    def __init__(self, lose: Callable[[bytes], bool], times: int) -> None:
        super().__init__()
        self.lose = lose
        self.times = times

    def reu_write(self, offset: int, data: bytes) -> None:
        on_writer = threading.current_thread().name == "uaudio-writer"
        if self.times and not on_writer and self.lose(data):
            self.times -= 1 if self.times > 0 else 0
            self.delivery_epoch += 1
            return
        super().reu_write(offset, data)


class SamplerBringUpDeliveryTest(unittest.TestCase):
    """The ring the FPGA loops over is seeded once, at start(). A slice of it
    lost to the link stays whatever the REU last held there, which on a ring
    the previous scene used is its audio, until the writer passes it."""

    RING = 8192
    BASE = 0x200000

    def _start(self, api: _FakeBackend, pushed: np.ndarray | None = None) -> s.UltimateAudioSampler:
        smp = _make(api, sample_rate=8000, bits=8, ring_base=self.BASE, ring_size=self.RING)
        if pushed is not None:
            smp.push_samples(pushed)
            smp.end_input()
        smp.start(prebuffer_timeout=0.01)

        def stop() -> None:
            with quiet_logging():  # the idle writer's pads are not the subject
                smp.stop()

        self.addCleanup(stop)
        return smp

    def test_a_lost_prefill_slice_is_sent_again(self):
        api = _LossyBringUpBackend(lambda data: True, times=1)
        smp = self._start(api)
        self.assertNotIn(0xFF, api.reu_bytes(self.BASE, self.RING))
        self.assertTrue(smp._running)

    def test_a_prefill_that_never_lands_is_logged_and_playback_goes_on(self):
        api = _LossyBringUpBackend(lambda data: True, times=-1)
        with self.assertLogs("c64cast.audio.sampler", "WARNING") as logs:
            smp = self._start(api)
        self.assertIn("ring prefill was not confirmed", logs.output[0])
        self.assertTrue(smp._running)

    def test_a_lost_prebuffer_write_is_sent_again(self):
        tone = np.full(512, 8000, dtype=np.int16)
        api = _LossyBringUpBackend(lambda data: any(data), times=1)
        self._start(api, pushed=tone)
        head = api.reu_bytes(self.BASE, tone.size)
        self.assertEqual(set(head), set(s.pack_pcm(tone, 8)))


class SamplerPushTest(unittest.TestCase):
    TONE = np.full(256, 8000, dtype=np.int16)

    def test_the_dsp_shapes_what_is_queued(self):
        class _Mute:
            active = True

            def process(self, x: np.ndarray) -> np.ndarray:
                return x * 0.0

        smp = _make(_FakeBackend(), sample_rate=8000, bits=8, dsp=cast(Any, _Mute()))
        smp.push_samples(self.TONE)
        _, pack = smp._q.get_nowait()
        self.assertEqual(pack, bytes(len(self.TONE)))

    def test_the_eof_clamp_counts_what_was_pushed(self):
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8)
        for _ in range(4):
            smp.push_samples(self.TONE)
        smp._running = True
        smp._gate_time = time.monotonic() - 100.0
        smp.mark_eof()
        self.assertAlmostEqual(smp.position_seconds(), 1024 / smp._actual_rate, places=6)

    def test_a_stopped_sampler_feeds_nothing_to_the_tap(self):
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8)
        smp.stop()
        smp.push_samples(self.TONE)
        self.assertFalse(np.any(smp.get_recent_samples(256)))

    def test_stop_releases_the_queued_audio(self):
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8)
        smp.push_samples(self.TONE)
        smp.stop()
        self.assertTrue(smp._q.empty())

    def test_stop_does_not_raise_when_the_gate_off_write_fails(self):
        api = _FakeBackend()

        def boom(address: str, data_hex: str) -> None:
            raise ConnectionError("link down")

        api.write_memory = boom  # type: ignore[method-assign]
        smp = _make(api, sample_rate=8000, bits=8)
        smp.stop()
        self.assertFalse(smp._running)

    def test_the_tap_reads_back_across_its_wrap(self):
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8)
        smp.push_samples(np.full(s.SAMPLE_TAP_SIZE - 10, -16384, dtype=np.int16))
        smp.push_samples(np.full(30, 16384, dtype=np.int16))
        np.testing.assert_allclose(smp.get_recent_samples(30), 0.5, rtol=1e-3)

    def test_the_read_head_counts_bytes_not_samples(self):
        smp = _make(_FakeBackend(), sample_rate=44100, bits=16)
        smp._running = True
        smp._gate_time = 100.0
        # A pinned clock: one second after the gate, however long a loaded
        # host takes between these lines.
        with mock.patch.object(s.time, "monotonic", return_value=101.0):
            consumed = smp._read_consumed_bytes()
        self.assertAlmostEqual(consumed / smp.bps / smp._actual_rate, 1.0, delta=0.01)


class SamplerWriterTelemetryTest(unittest.TestCase):
    def test_lead_min_and_max_span_every_step(self):
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8)
        smp._running = True
        smp._written = 1000
        smp._content_pos = smp._lead_target + 1000  # no room: each step only measures
        heads = iter([900, 950, 800])
        smp._read_consumed_bytes = lambda: next(heads)  # type: ignore[method-assign]
        with mock.patch.object(s.time, "sleep"):
            for _ in range(3):
                smp._writer_step(smp._writer_gen)
        self.assertEqual((smp._lead_min, smp._lead_max), (50, 200))


class SamplerWriterBackoffTest(unittest.TestCase):
    def test_the_backoff_doubles_while_failing_and_restarts_after_a_recovery(self):
        smp = _make(_FakeBackend(), sample_rate=8000, bits=8)
        smp._running = True
        script = iter(["fail", "fail", "fail", "ok", "fail", "end"])

        def step(gen: int) -> bool:
            what = next(script)
            if what == "fail":
                raise ConnectionError("link down")
            if what == "end":
                smp._running = False
            return what == "ok"

        smp._writer_step = step  # type: ignore[method-assign]
        with (
            mock.patch.object(s.time, "sleep") as sleep,
            self.assertLogs("c64cast.audio.sampler", level="INFO"),
        ):
            smp._writer_loop(smp._writer_gen)
        lo = s.WRITER_BACKOFF_MIN_S
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [lo, 2 * lo, 4 * lo, lo])


if __name__ == "__main__":
    unittest.main()
