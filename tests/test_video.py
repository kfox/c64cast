"""Tests for the video module's pure helpers (no PyAV / no real file)."""

from __future__ import annotations

import threading
import unittest

import numpy as np

from c64cast.video import (
    NORMALIZATION_MAX_GAIN,
    NORMALIZATION_TARGET_PEAK,
    AVFileSource,
    _compute_normalization_gain,
    _plan_decode_size,
)


def _make_av_source_stub(frames: list[tuple[float, np.ndarray]], eof: bool) -> AVFileSource:
    """Build an AVFileSource without going through __init__ (which opens a
    real container via PyAV). Only the attributes touched by
    `current_frame` / `finished` are set; everything else stays unset."""
    src = AVFileSource.__new__(AVFileSource)
    src._video_buf = list(frames)
    src._lock = threading.Lock()
    src._eof = eof
    return src


class NormalizationGainTest(unittest.TestCase):
    def test_zero_peak_returns_unity(self):
        # Defensive: a fully-silent or unscannable file shouldn't divide-by-
        # zero and shouldn't get amplified into noise.
        self.assertEqual(_compute_normalization_gain(0), 1.0)

    def test_negative_peak_returns_unity(self):
        # np.abs().max() can't produce this, but the helper is public-ish
        # and shouldn't trust its caller.
        self.assertEqual(_compute_normalization_gain(-100), 1.0)

    def test_already_at_full_scale_returns_unity(self):
        # A source that already hits full scale would otherwise compute a
        # gain < 1 (= reduction), which would needlessly soften clean audio.
        self.assertEqual(_compute_normalization_gain(32767), 1.0)

    def test_already_above_target_returns_unity(self):
        # 90% target × 32767 ≈ 29490. A peak just above shouldn't reduce.
        self.assertEqual(_compute_normalization_gain(30000), 1.0)

    def test_vic20_shatner_case(self):
        # The bundled VIC-20/Shatner clip peaks at 4102 — the motivating
        # example for the whole feature. Expect ~7.2x.
        gain = _compute_normalization_gain(4102)
        self.assertAlmostEqual(gain, (0.9 * 32767) / 4102, places=4)
        self.assertGreater(gain, 7.0)
        self.assertLess(gain, 7.5)

    def test_c64_2026_ad_case(self):
        # The bundled C64 2026 ad peaks at 13637 — should be a gentle ~2.2x
        # boost, not pinned to max.
        gain = _compute_normalization_gain(13637)
        self.assertAlmostEqual(gain, (0.9 * 32767) / 13637, places=4)

    def test_near_silent_capped_at_max(self):
        # A barely-audible source (peak 100) would compute gain ~295x;
        # that'd amplify noise floor into the signal. Cap protects.
        gain = _compute_normalization_gain(100)
        self.assertEqual(gain, NORMALIZATION_MAX_GAIN)

    def test_max_gain_at_exact_threshold(self):
        # Sanity check: gain just under the cap doesn't trip the cap.
        threshold_peak = int((NORMALIZATION_TARGET_PEAK * 32767) / NORMALIZATION_MAX_GAIN) + 1
        gain = _compute_normalization_gain(threshold_peak)
        self.assertLess(gain, NORMALIZATION_MAX_GAIN)


class AVFileSourceEOFTest(unittest.TestCase):
    """Regression: pre-fix, `current_frame` kept the last-consumed frame in
    `_video_buf` as stall protection. That kept the buffer at size-1 forever
    after demux EOF, so `finished` (which checks `_eof and not _video_buf`)
    never flipped True. VideoScene.process_frame kept returning True,
    the playlist never advanced, and the audio worker padded NEUTRAL for
    minutes (visible in audio logs as a sustained `writes=4/s bytes=4KiB/s`
    streak after the demux EOF debug line). Fix: when EOF is observed AND
    the consumed index is the last buffered frame, drain the buffer
    entirely so `finished` can flip on the next check."""

    def test_kept_frame_persists_before_eof(self):
        # Pre-EOF, the stall-protection IS the right behavior: consuming a
        # frame leaves it in the buffer in case the audio clock stalls and
        # we need to re-emit it (avoids black-framing the display).
        a = np.zeros((4, 4, 3), dtype=np.uint8)
        b = np.ones((4, 4, 3), dtype=np.uint8) * 100
        src = _make_av_source_stub([(0.0, a), (1.0, b)], eof=False)
        chosen = src.current_frame(audio_position_s=1.5)
        self.assertIs(chosen, b)
        # Last chosen frame stays in the buffer for stall re-emit.
        self.assertEqual(len(src._video_buf), 1)
        self.assertFalse(src.finished, "demux still running → not finished")

    def test_drains_last_frame_when_eof(self):
        # Post-EOF, the kept-frame logic becomes a trap. Once we've consumed
        # the last buffered frame, drop it too so `finished` can fire.
        a = np.zeros((4, 4, 3), dtype=np.uint8)
        b = np.ones((4, 4, 3), dtype=np.uint8) * 100
        src = _make_av_source_stub([(0.0, a), (1.0, b)], eof=True)
        chosen = src.current_frame(audio_position_s=1.5)
        self.assertIs(
            chosen,
            b,
            "the last consumed frame must still be returned to "
            "the caller (one final paint), but not retained",
        )
        self.assertEqual(
            len(src._video_buf),
            0,
            "buffer must be drained when EOF + last frame consumed, so `finished` can flip",
        )
        self.assertTrue(src.finished, "EOF + drained buffer = done")

    def test_partial_consume_at_eof_keeps_unconsumed_frames(self):
        # EOF was set but the audio clock is still behind some frames. The
        # unconsumed frames must NOT be dropped — only the consumed-through
        # range (including the chosen one, since EOF means no more coming).
        frames = [
            (t, np.full((2, 2, 3), int(t * 10), dtype=np.uint8)) for t in (0.0, 1.0, 2.0, 3.0)
        ]
        src = _make_av_source_stub(frames, eof=True)
        # Consume through PTS=1.0 (the second frame). PTS=2.0 and 3.0 are
        # ahead of the clock; they stay.
        chosen = src.current_frame(audio_position_s=1.5)
        assert chosen is not None
        self.assertEqual(chosen[0, 0, 0], 10)
        # Pre-fix: kept index 1 ([10, 20, 30]). With the EOF-aware drain,
        # the kept frame at index 1 only triggers full-drain when it's also
        # the LAST in the buffer; here it isn't, so normal trim applies and
        # the chosen frame stays as stall protection.
        remaining = [f[1][0, 0, 0] for f in src._video_buf]
        self.assertEqual(
            remaining,
            [10, 20, 30],
            "unconsumed future frames must survive partial consume even after EOF",
        )
        self.assertFalse(src.finished, "still frames ahead of clock → not finished")


class _FakeFrame:
    def __init__(self, pts: int, width: int = 3840, height: int = 2160):
        self.pts = pts
        self.width = width
        self.height = height
        self.reformat_calls: list[tuple[int, int, str]] = []

    def to_ndarray(self, format: str | None = None):  # noqa: A002 - PyAV's kwarg name
        # Full-res convert path returns a frame at the native size.
        return np.zeros((self.height, self.width, 3), dtype=np.uint8)

    def reformat(self, width: int, height: int, format: str):  # noqa: A002 - PyAV kwarg
        # Mimic PyAV: yuv→bgr + downscale in one pass, yielding a new frame
        # at the requested size whose to_ndarray() reflects it.
        self.reformat_calls.append((width, height, format))
        return _FakeFrame(self.pts, width=width, height=height)


class _FakeStream:
    type = "video"


class _FakePacket:
    def __init__(self, frames: list[_FakeFrame]):
        self.stream = _FakeStream()
        self._frames = frames

    def decode(self):
        return self._frames


class _FakeContainer:
    def __init__(self, packets: list[_FakePacket]):
        self._packets = packets

    def demux(self):
        return iter(self._packets)


class DemuxRebaseTest(unittest.TestCase):
    """`_demux_loop` rebases video PTS by the first decoded frame so a seeked
    source (frame PTS ~start_s) still starts at the from-0 playback clock.
    Driven with a fake container — no PyAV, no real file."""

    def _run_demux(self, frame_ptss: list[int]) -> list[float]:
        src = AVFileSource.__new__(AVFileSource)
        src._closed = False
        src._pts_offset = None
        src.video_time_base = 1.0  # 1 PTS tick == 1 second
        src._video_buf = []
        src._lock = threading.Lock()
        src.max_video_buffer = 240
        src._resampler = None
        src._audio_push = None
        src._decode_target = None
        src._decode_size = None
        src._decode_planned = False
        src.last_frame_pts = 0.0
        src.path = "fake"
        src.container = _FakeContainer([_FakePacket([_FakeFrame(p)]) for p in frame_ptss])
        src._demux_loop()
        return [pts for pts, _ in src._video_buf]

    def test_seeked_source_rebases_to_zero(self):
        # Frame PTS ~100s (post-seek) must rebase so the buffer starts at 0.
        self.assertEqual(self._run_demux([100, 101, 102]), [0.0, 1.0, 2.0])

    def test_no_seek_unchanged(self):
        # First frame already at 0 → offset 0 → buffer unchanged.
        self.assertEqual(self._run_demux([0, 1, 2]), [0.0, 1.0, 2.0])


class PlanDecodeSizeTest(unittest.TestCase):
    """`_plan_decode_size` picks the smallest even decode size whose post-crop
    dims still exceed the display target by DECODE_HEADROOM, never upscaling."""

    def test_4k_to_hires_downscales(self):
        # 3840×2160 (16:9) for a 320×200 (1.6) target. Crop trims width, so the
        # height axis binds: decoded height ≥ 2×200 = 400 → 712×400.
        plan = _plan_decode_size(3840, 2160, 320, 200)
        assert plan is not None
        dw, dh = plan
        self.assertEqual((dw, dh), (712, 400))
        # Post-crop both axes exceed the target with headroom.
        crop_w = dh * (320 / 200)
        self.assertGreaterEqual(crop_w, 320 * 2 - 1)
        self.assertGreaterEqual(dh, 200 * 2)

    def test_anamorphic_mhires_honors_height_axis(self):
        # MHires target (160, 200): height (200) > width (160). A width-only cap
        # would under-decode height and force an upscale; the planner must keep
        # decoded height ≥ 2×200.
        plan = _plan_decode_size(3840, 2160, 160, 200)
        assert plan is not None
        self.assertGreaterEqual(plan[1], 200 * 2)

    def test_even_dimensions(self):
        plan = _plan_decode_size(1920, 1080, 320, 200)
        assert plan is not None
        dw, dh = plan
        self.assertEqual(dw % 2, 0)
        self.assertEqual(dh % 2, 0)

    def test_source_already_small_returns_none(self):
        # A source at/below the needed resolution must not be upscaled.
        self.assertIsNone(_plan_decode_size(320, 200, 320, 200))
        self.assertIsNone(_plan_decode_size(400, 300, 320, 200))

    def test_degenerate_dims_return_none(self):
        self.assertIsNone(_plan_decode_size(0, 100, 320, 200))
        self.assertIsNone(_plan_decode_size(100, 100, 0, 200))


class DemuxDecodeDownscaleTest(unittest.TestCase):
    """The demux loop reformats (downscales during decode) when a decode target
    is set, and falls back to the full-res convert when it isn't."""

    def _run(self, decode_target, src_w=3840, src_h=2160):
        src = AVFileSource.__new__(AVFileSource)
        src._closed = False
        src._pts_offset = None
        src.video_time_base = 1.0
        src._video_buf = []
        src._lock = threading.Lock()
        src.max_video_buffer = 240
        src._resampler = None
        src._audio_push = None
        src._decode_target = decode_target
        src._decode_size = None
        src._decode_planned = False
        src.last_frame_pts = 0.0
        src.path = "fake"
        frame = _FakeFrame(0, width=src_w, height=src_h)
        src.container = _FakeContainer([_FakePacket([frame])])
        src._demux_loop()
        return src, frame

    def test_reformats_to_planned_size(self):
        src, frame = self._run(decode_target=(320, 200))
        # Planner ran and produced a sub-source size → reformat was used.
        self.assertIsNotNone(src._decode_size)
        self.assertEqual(len(frame.reformat_calls), 1)
        w, h, fmt = frame.reformat_calls[0]
        self.assertEqual((w, h), src._decode_size)
        self.assertEqual(fmt, "bgr24")
        # Buffered frame carries the downscaled dimensions, not the 4K source.
        _, img = src._video_buf[0]
        self.assertEqual(img.shape[:2], (h, w))

    def test_no_target_uses_full_res_convert(self):
        src, frame = self._run(decode_target=None)
        self.assertIsNone(src._decode_size)
        self.assertEqual(frame.reformat_calls, [])
        _, img = src._video_buf[0]
        self.assertEqual(img.shape[:2], (2160, 3840))

    def test_small_source_skips_reformat(self):
        # Source already ≤ target → planner returns None → full-res convert.
        src, frame = self._run(decode_target=(320, 200), src_w=320, src_h=200)
        self.assertIsNone(src._decode_size)
        self.assertEqual(frame.reformat_calls, [])


class CurrentFrameTelemetryTest(unittest.TestCase):
    """`current_frame` records the displayed frame's PTS and `video_buffer_depth`
    reports occupancy — the inputs to VideoScene's A/V-lag telemetry."""

    def test_last_frame_pts_tracks_chosen_frame(self):
        frames = [(0.0, np.zeros((2, 2, 3), np.uint8)), (1.0, np.ones((2, 2, 3), np.uint8))]
        src = _make_av_source_stub(frames, eof=False)
        src.current_frame(audio_position_s=1.5)
        self.assertEqual(src.last_frame_pts, 1.0)

    def test_buffer_depth(self):
        frames = [(float(t), np.zeros((2, 2, 3), np.uint8)) for t in range(3)]
        src = _make_av_source_stub(frames, eof=False)
        self.assertEqual(src.video_buffer_depth, 3)


if __name__ == "__main__":
    unittest.main()
