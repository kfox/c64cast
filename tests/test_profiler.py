"""Tests for the profiling harness."""
from __future__ import annotations

import logging
import time
import unittest

from c64cast.profiler import (
    FrameProfiler,
    NullProfiler,
    _Stats,
    get_profiler,
    set_profiler,
)


class StatsTest(unittest.TestCase):

    def test_empty(self):
        s = _Stats()
        self.assertEqual(s.count(), 0)
        self.assertEqual(s.summary(), (0.0, 0.0, 0.0, 0.0))

    def test_summary_on_known_input(self):
        s = _Stats(capacity=10)
        for v in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
            s.add(float(v))
        avg, p50, p95, mx = s.summary()
        self.assertAlmostEqual(avg, 5.5)
        # nearest-rank: int(0.5 * 10) = 5 → sorted[5] = 6.
        self.assertEqual(p50, 6.0)
        # int(0.95 * 10) = 9 → sorted[9] = 10.
        self.assertEqual(p95, 10.0)
        self.assertEqual(mx, 10.0)

    def test_capacity_evicts(self):
        s = _Stats(capacity=3)
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            s.add(v)
        # Only the last 3 survive.
        self.assertEqual(s.count(), 3)
        avg, _, _, mx = s.summary()
        self.assertAlmostEqual(avg, (3 + 4 + 5) / 3)
        self.assertEqual(mx, 5.0)


class FrameProfilerTest(unittest.TestCase):

    def test_frame_records_total(self):
        p = FrameProfiler(interval=10.0)
        with p.frame("scene-a"):
            time.sleep(0.001)
        bucket = p._bucket("scene-a", "frame_total")
        self.assertEqual(bucket.count(), 1)
        self.assertGreater(bucket.summary()[0], 0.0)

    def test_stage_accumulates_under_frame(self):
        p = FrameProfiler(interval=10.0)
        with p.frame("s"):
            with p.stage("compose"):
                time.sleep(0.001)
            with p.stage("push"):
                time.sleep(0.001)
        self.assertEqual(p._bucket("s", "compose").count(), 1)
        self.assertEqual(p._bucket("s", "push").count(), 1)

    def test_repeated_stage_in_one_frame_sums(self):
        p = FrameProfiler(interval=10.0)
        with p.frame("s"):
            with p.stage("overlay_compose"):
                time.sleep(0.001)
            with p.stage("overlay_compose"):
                time.sleep(0.001)
        # One frame → one sample, but the elapsed should reflect both opens.
        bucket = p._bucket("s", "overlay_compose")
        self.assertEqual(bucket.count(), 1)
        self.assertGreater(bucket.summary()[3], 0.0015)  # sum exceeds either alone

    def test_stage_outside_frame_records_nothing(self):
        p = FrameProfiler(interval=10.0)
        with p.stage("compose"):
            time.sleep(0.0005)
        # No bucket should have been created since _cur_scene was None.
        self.assertEqual(p._stats, {})

    def test_record_counts(self):
        p = FrameProfiler(interval=10.0)
        with p.frame("s"):
            p.record_counts(writes=12, bytes_=4096)
        self.assertEqual(p._bucket("s", "writes").summary()[0], 12.0)
        self.assertEqual(p._bucket("s", "bytes").summary()[0], 4096.0)

    def test_emit_if_due_respects_interval(self):
        p = FrameProfiler(interval=10.0)
        log = logging.getLogger("test_profile")
        with self.assertLogs(log, level="INFO") as cap:
            with p.frame("s"):
                p.record_counts(1, 100)
            # First call sets baseline, returns False, emits nothing.
            self.assertFalse(p.emit_if_due(now=100.0, log=log))
            # Within the interval: still nothing.
            self.assertFalse(p.emit_if_due(now=105.0, log=log))
            # Past the interval: one summary per scene, returns True.
            self.assertTrue(p.emit_if_due(now=111.0, log=log))
            # Force at least one log record so assertLogs doesn't raise on
            # the no-emit path (it requires >=1 record).
            log.info("sentinel")
        emitted = [r for r in cap.output if "profile[s]" in r]
        self.assertEqual(len(emitted), 1)
        self.assertIn("frame avg=", emitted[0])
        self.assertIn("writes/frame avg=1", emitted[0])

    def test_emit_format_includes_known_stages(self):
        p = FrameProfiler(interval=0.001)
        log = logging.getLogger("test_profile_fmt")
        with p.frame("scene-x"):
            with p.stage("cpu_render"):
                time.sleep(0.001)
            with p.stage("compose"):
                pass
            with p.stage("push"):
                pass
            with p.stage("wait"):
                pass
            p.record_counts(writes=5, bytes_=2048)
        # Step the clock past the interval.
        p._last_emit = time.time() - 1.0
        with self.assertLogs(log, level="INFO") as cap:
            p.emit_if_due(now=time.time(), log=log)
        line = cap.output[0]
        for token in ("profile[scene-x]", "frame ", "cpu_render ",
                      "compose ", "push ", "wait ",
                      "writes/frame", "bytes/frame"):
            self.assertIn(token, line)


class NullProfilerTest(unittest.TestCase):

    def test_is_default(self):
        # Module-level default: get_profiler() returns a NullProfiler.
        # Save+restore in case a prior test mutated the global.
        prev = get_profiler()
        try:
            set_profiler(NullProfiler())
            self.assertIsInstance(get_profiler(), NullProfiler)
            self.assertFalse(get_profiler().enabled)
        finally:
            set_profiler(prev)

    def test_methods_dont_raise(self):
        n = NullProfiler()
        with n.frame("anything"):
            with n.stage("compose"):
                pass
            n.record_counts(10, 200)
        # NullProfiler.emit_if_due always returns False — callers chain off it.
        self.assertFalse(n.emit_if_due(time.time(), logging.getLogger("null")))


if __name__ == "__main__":
    unittest.main()
