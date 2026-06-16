"""Lifecycle, worker-pacing, and bring-up/teardown coverage for AudioStreamer.

test_audio.py covers the sample tap + encode happy path; this module fills the
heavy-lift gaps the coverage backlog calls out: the real constructor, the worker
underrun/pacing paths (full + partial pad, prebuffer→strict-pace handoff, crash
guard), digi-boost, encode backpressure, the mic callback, input-device
resolution (against a fake sounddevice), and start/stop/position teardown.

No real U64 and no real sound device — FakeAPI plus a fake `sd` module.
"""

from __future__ import annotations

import queue
import threading
import time
import unittest
from typing import Any, cast

import numpy as np
from _fakes import FakeAPI

from c64cast import audio as audio_mod
from c64cast.api import Ultimate64API
from c64cast.audio import (
    NEUTRAL_SAMPLE,
    PREBUFFER_CHUNKS,
    SAMPLE_TAP_SIZE,
    AudioStreamer,
    encode_floats_to_dac,
)
from c64cast.c64 import CIA2, SID


def _make(**kw: Any) -> AudioStreamer:
    """Construct a real AudioStreamer (exercising __init__) over a FakeAPI."""
    api = cast(Ultimate64API, FakeAPI())
    return AudioStreamer(api, kw.pop("sample_rate", 8000), kw.pop("system", "NTSC"), **kw)


def _make_worker_streamer(chunk_size: int = 32, sample_rate: int = 64000) -> AudioStreamer:
    """A streamer wired for fast, hardware-free worker runs: tiny chunks, a
    high sample rate (sub-ms pace period), and a stubbed NMI timer so the
    prebuffer→pace handoff runs without touching CIA registers."""
    s = _make(sample_rate=sample_rate)
    s.chunk_size = chunk_size
    s._start_nmi_timer = lambda: None  # type: ignore[method-assign]
    return s


def _run_worker(s: AudioStreamer, until, timeout: float = 2.0) -> threading.Thread:
    """Start the worker thread and spin until `until()` is true or timeout."""
    s.running = True
    t = threading.Thread(target=s._worker, daemon=True, name="test-worker")
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
        # Prebuffer exactly, then starve the queue: the worker should arm NMI,
        # flip to strict pacing, and pad NEUTRAL chunks counted as full
        # underruns.
        s = _make_worker_streamer(chunk_size=32)
        for _ in range(PREBUFFER_CHUNKS):
            s.q.put(bytes([NEUTRAL_SAMPLE] * 32))
            s._queued_samples += 32
        _run_worker(s, until=lambda: s._full_underruns >= 3)
        self.assertGreaterEqual(s._full_underruns, 1)
        writes = cast(Any, s.api).writes
        # The post-prebuffer underrun chunks are all-NEUTRAL, full chunk_size.
        neutral_chunks = [d for _, d in writes if d == bytes([NEUTRAL_SAMPLE] * 32)]
        self.assertTrue(neutral_chunks, "expected NEUTRAL underrun chunks")

    def test_partial_underrun_pads_tail(self):
        # Feed sub-chunk blobs slower than the pace deadline so each collect
        # window closes with a partial chunk → NEUTRAL tail pad.
        s = _make_worker_streamer(chunk_size=64, sample_rate=64000)
        for _ in range(PREBUFFER_CHUNKS):
            s.q.put(bytes([1] * 64))
            s._queued_samples += 64

        stop = threading.Event()

        def trickle() -> None:
            # Half-chunk blobs, one per ~pace period, so a full chunk rarely
            # assembles within a single collect window.
            period = s.chunk_size / s.sample_rate
            while not stop.is_set():
                s.q.put(bytes([2] * (s.chunk_size // 2)))
                s._queued_samples += s.chunk_size // 2
                time.sleep(period)

        feeder = threading.Thread(target=trickle, daemon=True)
        feeder.start()
        try:
            _run_worker(s, until=lambda: s._partial_underruns >= 1, timeout=3.0)
        finally:
            stop.set()
            feeder.join(timeout=1.0)
        self.assertGreaterEqual(
            s._partial_underruns, 1, "expected at least one partial-pad underrun"
        )

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

    def test_worker_crash_sets_not_running(self):
        # An exception in the DMA write must be caught, logged, and flip
        # running False so the main loop can detect the dead worker.
        s = _make_worker_streamer(chunk_size=8)
        s.q.put(bytes([7] * 8))
        s._queued_samples += 8

        def boom(addr: str, data: bytes) -> None:
            raise RuntimeError("dma exploded")

        cast(Any, s).api.write_memory_file = boom
        with self.assertLogs("c64cast.audio", level="ERROR") as cm:
            s.running = True
            t = threading.Thread(target=s._worker, daemon=True)
            t.start()
            t.join(timeout=1.0)
        self.assertFalse(s.running)
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
        s._nmi_timer_started = True  # timer already armed
        s._nmi_latch = s._nmi_latch_value()  # at nominal
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
        nominal = s._nmi_latch_value()  # NTSC@8kHz → 127 (period 128)
        s.set_nmi_latch_for_mode("mhires", {"mhires": 1.1575})
        # period = round(128 / 1.1575) = 111 → latch 110, strictly below nominal.
        self.assertEqual(s._nmi_latch, 110)
        self.assertLess(s._nmi_latch, nominal)  # faster rate ⇒ smaller latch
        self.assertEqual(self._latch_write(s), 110)

    def test_slowdown_multiplier_grows_latch(self):
        s = self._started()
        nominal = s._nmi_latch_value()
        s.set_nmi_latch_for_mode("petscii", {"petscii": 0.8})
        # period = round(128 / 0.8) = 160 → latch 159, above nominal.
        self.assertEqual(s._nmi_latch, 159)
        self.assertGreater(s._nmi_latch, nominal)

    def test_unity_multiplier_no_write(self):
        s = self._started()
        s.set_nmi_latch_for_mode("blank", {"blank": 1.0})
        self.assertEqual(s._nmi_latch, s._nmi_latch_value())
        self.assertIsNone(self._latch_write(s))  # unchanged ⇒ no bus traffic

    def test_unknown_mode_defaults_to_unity(self):
        s = self._started()
        s.set_nmi_latch_for_mode("hires_edges", {"hires": 1.1})  # no exact key
        self.assertEqual(s._nmi_latch, s._nmi_latch_value())  # 1.0 fallback
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
        # The real ordering: set_nmi_latch_for_mode runs at scene setup BEFORE
        # the worker prebuffers and arms the timer. It must stash the multiplier
        # (no write yet) and _start_nmi_timer must then apply it — otherwise the
        # timer start would clobber the compensation back to nominal.
        s = _make()
        s._worker_thread = cast(Any, object())
        self.assertFalse(s._nmi_timer_started)
        s.set_nmi_latch_for_mode("mhires", {"mhires": 1.1575})
        self.assertIsNone(self._latch_write(s))  # deferred, not written
        self.assertAlmostEqual(s._pitch_multiplier, 1.1575)

        cast(Any, s)._start_nmi_timer()  # worker arms the timer
        self.assertTrue(s._nmi_timer_started)
        self.assertEqual(s._nmi_latch, 110)  # compensation applied
        self.assertEqual(self._latch_write(s), 110)

    def test_stop_clears_pitch_state(self):
        s = self._started()
        s.set_nmi_latch_for_mode("mhires", {"mhires": 1.1575})
        s.running = True
        s._worker_thread = None  # no real thread to join in this unit test
        s.stop()
        self.assertFalse(s._nmi_timer_started)
        self.assertAlmostEqual(s._pitch_multiplier, 1.0)


class NmiRateSafetyTest(unittest.TestCase):
    """The NMI sample-rate guard (c64.nmi_rate_safety) + its config wiring.

    The handler completes in <=81 cycles (badline worst case); a sample period
    shorter than that queues NMIs and drops pitch. PAL's slower clock = tighter
    ceiling than NTSC."""

    def test_default_rate_is_safe_both_standards(self):
        from c64cast.c64 import nmi_rate_safety
        from c64cast.config import AudioCfg

        self.assertEqual(AudioCfg().sample_rate, 10500)
        for system in ("NTSC", "PAL"):
            self.assertEqual(nmi_rate_safety(system, 10500)[0], "ok")

    def test_legacy_and_candidate_rates_ok(self):
        from c64cast.c64 import nmi_rate_safety

        self.assertEqual(nmi_rate_safety("NTSC", 8000)[0], "ok")
        self.assertEqual(nmi_rate_safety("NTSC", 11025)[0], "ok")  # NTSC headroom
        self.assertEqual(nmi_rate_safety("PAL", 10500)[0], "ok")

    def test_overrun_is_error(self):
        from c64cast.c64 import nmi_rate_safety

        for system in ("NTSC", "PAL"):
            level, msg = nmi_rate_safety(system, 16000)
            self.assertEqual(level, "error")
            self.assertIn("queue", msg.lower())

    def test_marginal_rate_warns(self):
        from c64cast.c64 import nmi_rate_safety

        # 12000 → period ~85 (NTSC) / ~82 (PAL): above the 81-cycle handler but
        # inside the 88-cycle entry-latency margin → warn, not error.
        for system in ("NTSC", "PAL"):
            self.assertEqual(nmi_rate_safety(system, 12000)[0], "warn")

    def test_pal_ceiling_below_ntsc(self):
        from c64cast.c64 import max_safe_sample_rate

        self.assertLess(max_safe_sample_rate("PAL"), max_safe_sample_rate("NTSC"))

    def test_nonpositive_rate_is_error(self):
        from c64cast.c64 import nmi_rate_safety

        self.assertEqual(nmi_rate_safety("NTSC", 0)[0], "error")

    def test_config_validate_raises_on_overrun_when_audio_enabled(self):
        import dataclasses

        from c64cast.config import Config, ConfigError, validate_nmi_sample_rate

        cfg = Config()
        cfg = dataclasses.replace(
            cfg, audio=dataclasses.replace(cfg.audio, enabled=True, sample_rate=16000)
        )
        with self.assertRaises(ConfigError):
            validate_nmi_sample_rate(cfg)

    def test_config_validate_noop_when_audio_disabled(self):
        import dataclasses

        from c64cast.config import Config, validate_nmi_sample_rate

        cfg = Config()  # audio disabled by default
        cfg = dataclasses.replace(
            cfg, audio=dataclasses.replace(cfg.audio, enabled=False, sample_rate=16000)
        )
        validate_nmi_sample_rate(cfg)  # must not raise

    def test_config_validate_passes_default(self):
        from c64cast.config import Config, validate_nmi_sample_rate

        validate_nmi_sample_rate(Config())  # default 10500, no raise


class DigiBoostTest(unittest.TestCase):
    def test_enable_writes_all_voices(self):
        s = _make(digi_boost=True)
        with self.assertLogs("c64cast.audio", level="INFO"):
            s._enable_digi_boost()
        api = cast(Any, s.api)
        # One control byte (write_memory) per voice at its CONTROL register.
        for v in range(SID.N_VOICES):
            ctrl = f"{SID.voice_base(v) + SID.OFF_CONTROL:04X}"
            self.assertIn(ctrl, api.memories)

    def test_disable_releases_gate_each_voice(self):
        s = _make(digi_boost=True)
        s._disable_digi_boost()
        api = cast(Any, s.api)
        for v in range(SID.N_VOICES):
            ctrl = f"{SID.voice_base(v) + SID.OFF_CONTROL:04X}"
            self.assertEqual(api.memories[ctrl], "40")  # SID_GATE_OFF

    def test_disable_swallows_write_errors(self):
        s = _make(digi_boost=True)

        def boom(addr: str, data_hex: str) -> None:
            raise RuntimeError("write failed")

        cast(Any, s).api.write_memory = boom
        with self.assertLogs("c64cast.audio", level="DEBUG"):
            s._disable_digi_boost()  # must not raise


class EncodeBackpressureTest(unittest.TestCase):
    def test_block_on_full_times_out_to_zero(self):
        s = _make()
        s.running = True
        s._queued_samples = s._max_queued_samples  # saturate the soft cap
        orig = audio_mod.QUEUE_PUT_TIMEOUT_S
        audio_mod.QUEUE_PUT_TIMEOUT_S = 0.001  # keep the spin loop instant
        try:
            n = s._encode_and_enqueue(np.zeros(64, dtype=np.float32), block_on_full=True)
        finally:
            audio_mod.QUEUE_PUT_TIMEOUT_S = orig
        self.assertEqual(n, 0)

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

    def test_empty_input_returns_zero(self):
        s = _make()
        self.assertEqual(s._encode_and_enqueue(np.array([], dtype=np.float32)), 0)


class EncodeDacTest(unittest.TestCase):
    def test_explicit_rng_dither_is_reproducible(self):
        # The offline pre-encode path passes a seeded Generator; same seed →
        # identical codes (exercises the rng-provided dither branch).
        floats = np.linspace(-0.9, 0.9, 64, dtype=np.float32)
        a = encode_floats_to_dac(floats, dither=True, rng=np.random.default_rng(7))
        b = encode_floats_to_dac(floats, dither=True, rng=np.random.default_rng(7))
        np.testing.assert_array_equal(a, b)
        self.assertEqual(a.dtype, np.uint8)


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

    def query_devices(self, idx: Any = None, kind: Any = None) -> dict[str, Any]:
        if idx is None:
            return self._devices[self.default.device[0]]
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

    def test_output_only_device_falls_back(self):
        fake = _FakeSD(
            [
                {"name": "default mic", "max_input_channels": 1},
                {"name": "speaker only", "max_input_channels": 0},
            ],
            0,
        )
        self._patch_sd(fake)
        s = _make()
        with self.assertLogs("c64cast.audio", level="WARNING"):
            dev, name = s._resolve_input_device(1)
        self.assertEqual(dev, 0)  # fell back to default input
        self.assertEqual(name, "default mic")

    def test_open_stream_channel_fallback(self):
        # channels=1 rejected, native channels=2 accepted.
        fake = _FakeSD([{"name": "stereo mic", "max_input_channels": 2}], 0, reject_channels={1})
        self._patch_sd(fake)
        s = _make()
        with self.assertLogs("c64cast.audio", level="INFO"):
            stream = s._open_input_stream(0)
        self.assertIsInstance(stream, _FakeStream)
        self.assertEqual(cast(Any, stream).kw["channels"], 2)

    def test_open_stream_all_channels_rejected_raises(self):
        # Every candidate channel count is rejected by PortAudio → the final
        # "could not open mic" RuntimeError (debug logs per attempt, no warning).
        fake = _FakeSD([{"name": "fussy mic", "max_input_channels": 2}], 0, reject_channels={1, 2})
        self._patch_sd(fake)
        s = _make()
        with self.assertLogs("c64cast.audio", level="DEBUG"):
            with self.assertRaises(RuntimeError):
                s._open_input_stream(0)

    def test_resolve_query_failure_falls_back(self):
        # query_devices raising for the requested device → fall back to default.
        class _RaisingSD(_FakeSD):
            def query_devices(self, idx=None, kind=None):
                if idx == 5:
                    raise RuntimeError("no such device")
                return super().query_devices(idx, kind)

        fake = _RaisingSD([{"name": "default mic", "max_input_channels": 1}], 0)
        self._patch_sd(fake)
        s = _make()
        with self.assertLogs("c64cast.audio", level="WARNING"):
            dev, name = s._resolve_input_device(5)
        self.assertEqual(dev, 0)

    def test_open_stream_no_usable_device_raises(self):
        fake = _FakeSD([{"name": "dead", "max_input_channels": 0}], 0)
        self._patch_sd(fake)
        s = _make()
        # Resolution warns about the unusable device before the open raises;
        # capture the warning so it doesn't leak to the test console.
        with self.assertLogs("c64cast.audio", level="WARNING"):
            with self.assertRaises(RuntimeError):
                s._open_input_stream(0)

    def test_start_mic_without_sounddevice_warns(self):
        self._orig_avail = audio_mod.AUDIO_AVAILABLE
        audio_mod.AUDIO_AVAILABLE = False
        self.addCleanup(lambda: setattr(audio_mod, "AUDIO_AVAILABLE", self._orig_avail))
        s = _make()
        with self.assertLogs("c64cast.audio", level="WARNING"):
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

    def test_position_seconds_host_dma(self):
        s = _make()
        s._pushed_count = 8000
        s._queued_samples = 0
        self.assertAlmostEqual(s.position_seconds(), 1.0, places=3)
        # Still-queued samples are not yet "consumed".
        s._queued_samples = 4000
        self.assertAlmostEqual(s.position_seconds(), 0.5, places=3)

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
        s.reset_position()
        self.assertEqual(s._pushed_count, 0)

    def test_stop_teardown_writes_and_logs_clean(self):
        s = _make()
        s.start_for_external_source()
        with self.assertLogs("c64cast.audio", level="INFO") as cm:
            s.stop()
        self.assertFalse(s.running)
        api = cast(Any, s.api)
        self.assertEqual(api.memories.get("D418"), "00")  # SID muted
        self.assertIsNone(s._worker_thread)
        self.assertEqual(s._queued_samples, 0)
        self.assertTrue(any("clean session" in m for m in cm.output))

    def test_stop_reports_underruns(self):
        s = _make()
        s._full_underruns = 2
        s._partial_underruns = 5
        with self.assertLogs("c64cast.audio", level="WARNING") as cm:
            s.stop()
        self.assertTrue(any("2 full + 5 partial" in m for m in cm.output))
        # Counters reset for the next session.
        self.assertEqual(s._full_underruns, 0)
        self.assertEqual(s._partial_underruns, 0)

    def test_stop_swallows_teardown_write_errors(self):
        s = _make()

        def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("teardown write failed")

        cast(Any, s).api.write_regs = boom
        with self.assertLogs("c64cast.audio", level="DEBUG"):
            s.stop()  # must not raise

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
            def stop(self):
                raise RuntimeError("mic stop failed")

            def close(self):
                raise RuntimeError("mic close failed")

        s.mic_stream = cast(Any, _BadStream())
        with self.assertLogs("c64cast.audio", level="DEBUG"):
            s.stop()  # must not raise
        self.assertIsNone(s.mic_stream)

    def test_disarm_reu_pump_swallows_errors(self):
        s = _make()
        s._reu_pump_armed = True

        def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("vector restore failed")

        cast(Any, s).api.write_regs = boom
        with self.assertLogs("c64cast.audio", level="DEBUG"):
            s._disarm_reu_pump()  # must not raise
        self.assertFalse(s._reu_pump_armed)

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


if __name__ == "__main__":
    unittest.main()
