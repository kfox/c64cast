"""Tests for the video module's pure helpers (no PyAV / no real file)."""

from __future__ import annotations

import os
import threading
import unittest

import numpy as np

from c64cast.video import (
    NORMALIZATION_MAX_GAIN,
    NORMALIZATION_TARGET_PEAK,
    AVFileSource,
    _build_atempo_graph,
    _compute_normalization_gain,
    _ensure_pyav,
    _is_remote_url,
    _plan_decode_size,
    _scan_video_samples,
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


class RemoteUrlTest(unittest.TestCase):
    def test_http_and_https_are_remote(self):
        self.assertTrue(_is_remote_url("http://example.com/a.mp4"))
        self.assertTrue(_is_remote_url("https://rr4.googlevideo.com/videoplayback?x=1"))

    def test_local_paths_are_not_remote(self):
        self.assertFalse(_is_remote_url("/Users/kfox/assets/videos/clip.mp4"))
        self.assertFalse(_is_remote_url("assets/videos/clip.webm"))
        self.assertFalse(_is_remote_url("file:///tmp/clip.mp4"))


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
        src._tempo_scale = 1.0
        src._atempo_graph = None
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
        src._tempo_scale = 1.0
        src._atempo_graph = None
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


@unittest.skipUnless(_ensure_pyav(), "PyAV (video extra) not installed")
class AtempoTempoCompensationTest(unittest.TestCase):
    """Bitmap+DAC tempo compensation: the atempo graph time-compresses audio
    (pitch-preserving) by 1/tempo_scale, so the emitted sample count is ≈
    tempo_scale × the input count. Drives the real AVFileSource emit path
    (_drain_atempo / _flush_atempo / _emit_audio) through a __new__ stub so no
    container/file is needed."""

    SR = 8000

    def _stub(self, tempo_scale: float, sink) -> AVFileSource:
        src = AVFileSource.__new__(AVFileSource)
        src.path = "test.mp4"
        src._closed = False
        src.audio_gain = 1.0
        src.audio_noise_gate = 0
        src._tempo_scale = tempo_scale
        src._audio_push = sink.append
        src._atempo_graph = _build_atempo_graph(self.SR, tempo_scale)
        return src

    def _feed(self, src: AVFileSource, total_samples: int, frame_len: int = 1024) -> None:
        import av

        pts = 0
        for _ in range(0, total_samples, frame_len):
            arr = np.random.randint(-2000, 2000, frame_len).astype(np.int16).reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(arr, format="s16", layout="mono")
            frame.sample_rate = self.SR
            frame.pts = pts
            pts += frame_len
            src._atempo_graph.push(frame)
            src._drain_atempo()

    def test_output_length_matches_tempo_scale(self):
        for s in (0.88, 0.75, 0.5):
            sink: list[np.ndarray] = []
            src = self._stub(s, sink)
            n_in = 400_000  # large enough that atempo's fixed tail is negligible
            self._feed(src, n_in)
            src._flush_atempo()
            n_out = sum(a.size for a in sink)
            ratio = n_out / n_in
            self.assertAlmostEqual(ratio, s, delta=0.01, msg=f"tempo_scale={s}: ratio {ratio:.4f}")

    def test_flush_emits_buffered_tail(self):
        # Without the EOF flush, the last atempo-buffered frames are lost.
        sink: list[np.ndarray] = []
        src = self._stub(0.88, sink)
        self._feed(src, 40_000)
        pre_flush = sum(a.size for a in sink)
        src._flush_atempo()
        post_flush = sum(a.size for a in sink)
        self.assertGreater(post_flush, pre_flush)

    def test_gain_applied_on_compensated_path(self):
        # _emit_audio must still apply normalization gain when routing through
        # the graph (the refactor moved gain/gate into the shared helper).
        sink: list[np.ndarray] = []
        src = self._stub(0.88, sink)
        src.audio_gain = 2.0
        arr = np.full(4096, 1000, np.int16)
        src._emit_audio(arr)
        self.assertTrue(all((a == 2000).all() for a in sink))


class _RecordingAcc:
    """Minimal accumulator: records the mean BGR of every frame fed to it, so a
    test can see which parts of a source's timeline the scan actually sampled."""

    def __init__(self) -> None:
        self.means: list[tuple[float, float, float]] = []

    def add(self, img_bgr: np.ndarray) -> None:
        b, g, r = (float(img_bgr[..., c].mean()) for c in range(3))
        self.means.append((b, g, r))


def _write_synthetic_video(path: str, *, seconds: int = 3, fps: int = 30) -> None:
    """Encode a 3-segment video (red → green → blue thirds) so a scan's sampled
    frames reveal which timeline regions it visited. Small GOP so the seek path
    has several keyframes to land on."""
    import av

    container = av.open(path, "w")
    try:
        stream = container.add_stream("mpeg4", rate=fps)
        stream.width, stream.height = 64, 64
        stream.pix_fmt = "yuv420p"
        stream.gop_size = 6
        total = seconds * fps
        colors_rgb = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]  # red, green, blue thirds
        for i in range(total):
            rgb = colors_rgb[min(2, i * 3 // total)]
            img = np.empty((64, 64, 3), dtype=np.uint8)
            img[..., 0], img[..., 1], img[..., 2] = rgb
            frame = av.VideoFrame.from_ndarray(img, "rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():  # flush the encoder
            container.mux(packet)
    finally:
        container.close()


@unittest.skipUnless(_ensure_pyav(), "PyAV not installed")
class ScanVideoSamplesTest(unittest.TestCase):
    """`_scan_video_samples` seek-samples across a source's whole timeline."""

    def _make(self) -> str:
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        _write_synthetic_video(path)
        return path

    @staticmethod
    def _dominant(mean: tuple[float, float, float]) -> str:
        b, g, r = mean
        return "rgb"[int(np.argmax([r, g, b]))]

    def test_samples_span_whole_timeline(self):
        # Seek sampling should visit every third of the file (red/green/blue),
        # not just the head — the whole point of even-spaced timestamps.
        path = self._make()
        acc = _RecordingAcc()
        self.assertTrue(_scan_video_samples(path, [acc], max_samples=30))
        self.assertGreater(len(acc.means), 0)
        seen = {self._dominant(m) for m in acc.means}
        self.assertEqual(seen, {"r", "g", "b"})

    def test_missing_file_returns_false(self):
        acc = _RecordingAcc()
        with self.assertLogs("c64cast.video", level="WARNING"):
            self.assertFalse(_scan_video_samples("/no/such/file.mp4", [acc]))
        self.assertEqual(acc.means, [])

    def test_empty_accumulators_short_circuit(self):
        self.assertFalse(_scan_video_samples(self._make(), []))

    def test_falls_back_to_sequential_when_seek_fails(self):
        # A non-seekable source (seek raises) must still get sampled via the
        # sequential-decode fallback — and still span the whole timeline.
        import unittest.mock as mock

        from c64cast import video

        path = self._make()
        acc = _RecordingAcc()
        with mock.patch.object(video, "_seek_sample_frames", side_effect=OSError("not seekable")):
            self.assertTrue(_scan_video_samples(path, [acc], max_samples=30))
        self.assertGreater(len(acc.means), 0)
        seen = {self._dominant(m) for m in acc.means}
        self.assertEqual(seen, {"r", "g", "b"})


if __name__ == "__main__":
    unittest.main()
