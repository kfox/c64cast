"""Tests for the audio sample tap + worker queue (no real U64, no real
sound device)."""

from __future__ import annotations

import queue
import threading
import unittest
from typing import Any, cast

import numpy as np
from _fakes import FakeAPI

from c64cast.api import Ultimate64API
from c64cast.audio import SAMPLE_TAP_SIZE, AudioStreamer


def _new_streamer(monkeypatch_api=True) -> AudioStreamer:
    s = AudioStreamer.__new__(AudioStreamer)
    s.api = cast(Ultimate64API, FakeAPI())
    s.sample_rate = 8000
    s.system = "NTSC"
    # New queue shape: bytes-blob per item, with a separate sample-count
    # counter for backpressure (q.qsize() now counts blobs, not samples).
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
    s.dither_enabled = True
    s.digi_boost = False
    s.sid_filter_cutoff = 0
    s._full_underruns = 0
    s._partial_underruns = 0
    # Host-DMA servo off → worker stays open-loop (no R reads) for these
    # deterministic worker-path tests.
    s.host_dma_servo = False
    s._servo_integ = 0.0
    s._servo_gap_min = -1
    s._servo_gap_max = -1
    s._servo_gap_last = -1
    # Fixed-ratio resampler state (mirrors __init__): 1.0 = passthrough.
    s._resample_ratio = 1.0
    s._resample_phase = 0.0
    s._resample_prev_tail = np.zeros(0, dtype=np.float32)
    return s


def _drain_queue_to_samples(q: queue.Queue[tuple[bytes, int]]) -> list[int]:
    """Pop every blob from `q` and concatenate into a list of sample bytes.

    Tests below used to do `s.q.get()` per sample; now each get returns a
    (payload, src_weight) tuple — the payload is the bytes blob of N samples.
    This helper bridges that for the simple cases."""
    out: list[int] = []
    while not q.empty():
        payload, _weight = q.get()
        out.extend(payload)
    return out


class SampleTapTest(unittest.TestCase):
    def test_push_then_get(self):
        s = _new_streamer()
        s._push_to_tap(np.array([0.5, -0.5, 0.25], dtype=np.float32))
        out = s.get_recent_samples(5)
        self.assertEqual(list(out), [0.0, 0.0, 0.5, -0.5, 0.25])

    def test_wrap_around(self):
        s = _new_streamer()
        # Push more than tap size to force a wrap.
        big = np.linspace(-1, 1, SAMPLE_TAP_SIZE + 100, dtype=np.float32)
        s._push_to_tap(big)
        # Should hold the *last* SAMPLE_TAP_SIZE samples.
        out = s.get_recent_samples(SAMPLE_TAP_SIZE)
        self.assertEqual(len(out), SAMPLE_TAP_SIZE)
        # First few samples of `out` should match the corresponding tail of `big`.
        np.testing.assert_allclose(out, big[-SAMPLE_TAP_SIZE:], rtol=1e-5)

    def test_empty_push_noop(self):
        s = _new_streamer()
        s._push_to_tap(np.array([], dtype=np.float32))
        self.assertEqual(s._tap_write, 0)


class EncodeAndEnqueueTest(unittest.TestCase):
    def test_mid_scale_maps_to_7(self):
        # 0.0 input → (0 + 1) * 7.5 = 7.5 → uint8 truncates to 7 = NEUTRAL_SAMPLE.
        s = _new_streamer()
        n = s._encode_and_enqueue(np.array([0.0] * 4, dtype=np.float32))
        self.assertEqual(n, 4)
        values = _drain_queue_to_samples(s.q)
        # With dither on (default in streamer helper), allow ±1 around 7.
        self.assertTrue(all(v in (6, 7, 8) for v in values), values)

    def test_full_scale_maps_to_15_and_0(self):
        # +1.0 → 15; -1.0 → 0.
        s = _new_streamer()
        s._encode_and_enqueue(np.array([1.0, -1.0], dtype=np.float32))
        blob, _ = s.q.get()
        # TPDF dither can shift full-scale by ±1 LSB pre-clip.
        self.assertIn(blob[0], (14, 15))
        self.assertIn(blob[1], (0, 1))

    def test_dither_suppressed_for_exact_zero_input(self):
        # Suppression of dither at floats == 0 is a load-bearing property
        # of _encode_and_enqueue — mic / AVFileSource noise gates zero the
        # noise floor; dither must not re-introduce noise there.
        s = _new_streamer()
        s._encode_and_enqueue(np.zeros(1024, dtype=np.float32))
        blob, _ = s.q.get()
        self.assertTrue(
            all(v == 7 for v in blob),
            f"exact-zero input should encode to NEUTRAL_SAMPLE=7 "
            f"unchanged by dither; got {set(blob)}",
        )

    def test_dither_disabled_gives_deterministic_quantize(self):
        # With dither off, a constant non-zero input must map to a single
        # bit-exact value — no random offset.
        s = _new_streamer()
        s.dither_enabled = False
        # 0.5 input → (0.5 + 1) * 7.5 = 11.25 → uint8 truncates to 11.
        s._encode_and_enqueue(np.full(64, 0.5, dtype=np.float32))
        blob, _ = s.q.get()
        self.assertTrue(
            all(v == 11 for v in blob),
            f"dither off + constant input should encode to a single value; got {set(blob)}",
        )

    def test_drops_when_queue_full_without_block(self):
        s = _new_streamer()
        # Saturate the sample-count cap directly (backpressure is by
        # sample count now, not q.full()).
        s._queued_samples = s._max_queued_samples
        n = s._encode_and_enqueue(np.zeros(100, dtype=np.float32), block_on_full=False)
        self.assertEqual(n, 0, "drop-on-full path should push 0 samples")


class WorkerBatchingTest(unittest.TestCase):
    def test_worker_drains_chunk_in_one_post(self):
        s = _new_streamer()
        s.running = True
        s.chunk_size = 64
        # Pre-load the queue with one full chunk's worth as a single blob.
        s.q.put((bytes([7] * 64), 64))
        s._queued_samples = 64
        # Run the worker briefly.
        t = threading.Thread(target=s._worker, daemon=True)
        t.start()
        # Wait for the chunk to be flushed.
        import time

        for _ in range(50):
            if cast(Any, s.api).writes:
                break
            time.sleep(0.01)
        s.running = False
        t.join(timeout=1.0)
        # First write should contain at least one POST with a chunk.
        self.assertGreater(
            len(cast(Any, s.api).writes), 0, "worker should have posted at least one chunk"
        )
        # Each posted chunk is up to chunk_size bytes.
        for _addr, data in cast(Any, s.api).writes:
            self.assertLessEqual(len(data), s.chunk_size)

    def test_worker_splits_oversized_blob_across_chunks(self):
        """A single blob larger than chunk_size should be split across
        multiple uploads via the `leftover` carry."""
        s = _new_streamer()
        s.running = True
        s.chunk_size = 16
        # 50 samples = 3 full chunks + 2 leftover.
        s.q.put((bytes(range(50)), 50))
        s._queued_samples = 50
        t = threading.Thread(target=s._worker, daemon=True)
        t.start()
        import time

        for _ in range(50):
            if len(cast(Any, s.api).writes) >= 4:
                break
            time.sleep(0.01)
        s.running = False
        t.join(timeout=1.0)
        writes = cast(Any, s.api).writes
        # Reassembled body across the first 4 writes should equal the
        # original 50 sample bytes (the 4th write fills to chunk_size with
        # NEUTRAL pad once underrun kicks in after prebuffer; we only
        # check the first 50 bytes).
        body = b"".join(data for _, data in writes)
        self.assertGreaterEqual(len(body), 50)
        self.assertEqual(body[:50], bytes(range(50)))

    def test_worker_paces_post_prebuffer_writes_to_nmi_rate(self):
        """Regression for the burst-write bug.

        Pre-load a deep queue and run the worker for a fixed wall-clock
        window. After the 3-chunk prebuffer fires the NMI timer, the
        worker must throttle to one write per chunk_size / sample_rate.
        Before this fix the worker drained the queue as fast as DMA would
        let it (~one write per ~5 ms), lapping the ring buffer and
        overwriting real audio with neutral pads."""
        import time

        s = _new_streamer()
        s.running = True
        s.chunk_size = 64
        s.sample_rate = 8000  # → chunk_period = 8 ms
        # Stub the NMI timer start — FakeAPI is happy with the regs writes
        # but we don't care about them for this test.
        s._start_nmi_timer = lambda: None  # type: ignore[method-assign]
        # 100 full chunks of real audio queued ahead — far more than the
        # worker can ship in the test window if it actually paces itself.
        for _ in range(100):
            s.q.put((bytes(range(64)), 64))
            s._queued_samples += 64

        t = threading.Thread(target=s._worker, daemon=True)
        t.start()
        time.sleep(0.080)  # 80 ms wall clock
        s.running = False
        t.join(timeout=1.0)

        writes = cast(Any, s.api).writes
        # Paced budget: 3 prebuffer + ~10 paced (80 ms / 8 ms) = ~13. The
        # generous cap absorbs scheduler jitter without re-admitting the
        # unbounded-burst regression (which produced 40+ writes in this
        # window).
        self.assertLess(
            len(writes), 25, f"worker wrote {len(writes)} chunks in 80 ms — pacing regression?"
        )
        # Sanity: prebuffer must have fired (≥ 3 writes) so we're actually
        # exercising the paced post-prebuffer path.
        self.assertGreaterEqual(len(writes), 3)


if __name__ == "__main__":
    unittest.main()
