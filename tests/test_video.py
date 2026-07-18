"""Tests for the video module's pure helpers (no PyAV / no real file)."""

from __future__ import annotations

import math
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from c64cast import scenes
from c64cast.scenes import VideoScene, _timecode
from c64cast.transport import LoopPresetStore
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

    def seek(self, offset_us: int) -> None:
        # No-op by default (recording variants override this attribute
        # per-test); real PyAV would reposition the demux read head.
        pass


class DemuxRebaseTest(unittest.TestCase):
    """`_demux_loop` rebases video PTS by the first decoded frame so a seeked
    source (frame PTS ~start_s) still starts at the from-0 playback clock.
    Driven with a fake container — no PyAV, no real file."""

    def _run_demux(self, frame_ptss: list[int]) -> list[float]:
        src = AVFileSource.__new__(AVFileSource)
        src._closed = False
        src._pts_offset = None
        src._pts_anchor_target = 0.0
        src._pending_seek = None
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


class MuteLatchTest(unittest.TestCase):
    """`set_muted` (MIDI live-tune Phase 2's transport escape valve) drops
    every packet `_emit_audio` would otherwise pass to the consumer."""

    def _stub(self, sink) -> AVFileSource:
        src = AVFileSource.__new__(AVFileSource)
        src._muted = False
        src._pending_seek = None
        src._audio_push = sink.append
        src.audio_noise_gate = 0
        src.audio_gain = 1.0
        return src

    def test_muted_drops_packets(self):
        sink: list[np.ndarray] = []
        src = self._stub(sink)
        src.set_muted(True)
        src._emit_audio(np.array([1, 2, 3], dtype=np.int16))
        self.assertEqual(sink, [])

    def test_unmuted_passes_through(self):
        sink: list[np.ndarray] = []
        src = self._stub(sink)
        src._emit_audio(np.array([1, 2, 3], dtype=np.int16))
        self.assertEqual(len(sink), 1)

    def test_unmute_resumes(self):
        sink: list[np.ndarray] = []
        src = self._stub(sink)
        src.set_muted(True)
        src.set_muted(False)
        src._emit_audio(np.array([1], dtype=np.int16))
        self.assertEqual(len(sink), 1)


class TransportSeekTest(unittest.TestCase):
    """`request_seek`/`_apply_pending_seek` (MIDI live-tune Phase 2): a
    seek clears the stale pre-seek buffer immediately and re-anchors the
    demux thread's PTS rebase to land on the requested target_s instead of
    0 — the transport plan's "clock IS file position once touched" design.
    Driven with the same fake-container harness as DemuxRebaseTest — no
    PyAV, no real file."""

    def _make_src(self, frame_ptss, *, pending_seek=None, anchor=0.0) -> AVFileSource:
        src = AVFileSource.__new__(AVFileSource)
        src._closed = False
        src._pts_offset = None
        src._pts_anchor_target = anchor
        src._pending_seek = pending_seek
        src._muted = False
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
        src.target_sr = 8000
        src.a_stream = None
        src.container = _FakeContainer([_FakePacket([_FakeFrame(p)]) for p in frame_ptss])
        return src

    def test_request_seek_clears_buffer_and_queues_target(self):
        src = self._make_src([0, 1, 2])
        src._video_buf = [(0.0, np.zeros((2, 2, 3), dtype=np.uint8))]
        src.request_seek(12.5)
        self.assertEqual(src._pending_seek, 12.5)
        self.assertEqual(src._video_buf, [])

    def test_request_seek_clamps_negative_to_zero(self):
        src = self._make_src([0])
        src.request_seek(-3.0)
        self.assertEqual(src._pending_seek, 0.0)

    def test_pending_seek_rebases_pts_to_target_not_zero(self):
        # The very first packet fetched is whatever was "in flight" when the
        # seek was requested (stale, pre-seek) — the real demux loop always
        # discards it and re-fetches from the container's new position (see
        # _apply_pending_seek's docstring). Model that here with a throwaway
        # first packet, then the real post-seek keyframes (~50s onward):
        # their rebased PTS must land AT the seek target (30s), not at 0
        # like an ordinary start_s seek would.
        src = self._make_src([], pending_seek=30.0)
        stale = _FakePacket([_FakeFrame(999)])
        real = [_FakePacket([_FakeFrame(p)]) for p in (50, 51, 52)]
        src.container = _FakeContainer([stale, *real])
        src._demux_loop()
        self.assertEqual([pts for pts, _ in src._video_buf], [30.0, 31.0, 32.0])
        self.assertIsNone(src._pending_seek, "pending seek must be consumed")
        self.assertEqual(src._pts_anchor_target, 30.0)

    def test_container_seek_called_with_microseconds(self):
        seeks: list[int] = []
        src = self._make_src([10], pending_seek=7.5)
        src.container.seek = seeks.append  # type: ignore[method-assign]
        src._demux_loop()
        self.assertEqual(seeks, [7_500_000])

    def test_no_pending_seek_behaves_like_ordinary_start(self):
        src = self._make_src([0, 1, 2])
        src._demux_loop()
        self.assertEqual([pts for pts, _ in src._video_buf], [0.0, 1.0, 2.0])

    def test_apply_pending_seek_returns_false_when_none_queued(self):
        src = self._make_src([0])
        self.assertFalse(src._apply_pending_seek())


class _StubSource:
    """Duck-types the bits of AVFileSource that VideoScene's transport
    surface and process_frame's loop-wrap logic touch, without any PyAV
    dependency — mirrors this file's AVFileSource.__new__ stub pattern one
    layer up."""

    def __init__(
        self,
        *,
        duration: float | None = None,
        video_fps: float = 30.0,
        a_stream: object | None = None,
        events: list[tuple[str, object]] | None = None,
    ):
        self.duration_s = duration
        self.video_fps = video_fps
        self.finished = False
        self.last_frame_pts = 0.0
        self.seeks: list[float] = []
        self.muted_calls: list[bool] = []
        # Phase 4 resync surface: a non-None a_stream marks the source as
        # audio-bearing (VideoScene._touch_transport requires it to resolve the
        # resync path); seek_pending mirrors AVFileSource.seek_pending. `events`
        # (when supplied) records the ordered request_seek/set_muted calls so a
        # test can pin the resume splice-then-unmute ordering.
        self.a_stream = a_stream
        self.seek_pending = False
        self._events = events
        self._frame = np.zeros((200, 320, 3), dtype=np.uint8)

    def request_seek(self, target_s: float) -> None:
        self.seeks.append(target_s)
        self.seek_pending = True
        if self._events is not None:
            self._events.append(("seek", target_s))

    def set_muted(self, muted: bool) -> None:
        self.muted_calls.append(muted)
        if self._events is not None:
            self._events.append(("muted", muted))

    def close(self) -> None:
        pass

    def current_frame(self, clock_s: float) -> np.ndarray | None:
        return self._frame

    @property
    def video_buffer_depth(self) -> int:
        return 0


def _make_video_scene_stub(source: _StubSource, *, start_s: float = 0.0) -> VideoScene:
    """Build a VideoScene without going through __init__/setup() (which need
    PyAV + a real AudioStreamer) — mirrors test_ensemble_audio_lock.py's
    `VideoScene.__new__(VideoScene)` pattern, filling in exactly the
    attributes the transport surface + process_frame's loop-wrap/frame-number
    paths touch."""
    scene = VideoScene.__new__(VideoScene)
    scene.source = source  # type: ignore[assignment]  # duck-typed stub, not a real AVFileSource
    scene.audio = None
    scene._start_time = 0.0
    scene.osd = scenes.OsdState()
    scene.start_s = start_s
    scene.show_frame_numbers = False
    scene._last_rendered_img = None
    scene._last_osd_shown = None
    scene._online_fit = None
    scene._online_fit_frames = 0
    scene.display_mode = mock.MagicMock()
    scene.overlays = []
    scene.api = mock.MagicMock()
    scene._av_lag_min = math.inf
    scene._av_lag_max = -math.inf
    scene._av_lag_sum = 0.0
    scene._av_lag_count = 0
    scene._av_buf_min = math.inf
    scene._av_last_log_t = 0.0
    scene._transport_touched = False
    scene._paused = False
    scene._wall_anchor_clock_s = 0.0
    scene._wall_anchor_time = 0.0
    # Phase 4 resync state (defaults keep the mute/wall path for audio=None
    # stubs, so the existing Phase-2 clock tests are unchanged).
    scene._tempo_scale = 1.0
    scene._loop_audio = "on"
    scene._transport_resync = False
    scene._audio_anchor_clock_s = 0.0
    scene._audio_anchor_pos = 0.0
    scene._loop_a = None
    scene._loop_b = None
    scene._loop_state = "none"
    scene._record_border_active = False
    scene._loop_store = None
    return scene


class VideoSceneClockTest(unittest.TestCase):
    """VideoScene._clock_s()/_touch_transport (MIDI live-tune Phase 2): before
    transport is touched, the clock is unchanged (audio-position, or wall-
    clock from _start_time when unmuted with no audio streamer); once
    touched, it becomes a self-owned wall-clock anchor that freezes on pause
    and re-anchors on seek — see design decisions 1/2 of the transport plan."""

    def _scene(self, **kw) -> VideoScene:
        return _make_video_scene_stub(_StubSource(duration=100.0), **kw)

    def test_untouched_uses_wall_clock_from_start_time(self):
        scene = self._scene()
        scene._start_time = 3.0
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            self.assertAlmostEqual(scene._clock_s(), 7.0)

    def test_touch_transport_freezes_current_reading_and_mutes(self):
        scene = self._scene()
        scene._start_time = 4.0  # untouched clock would read 6.0
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene._touch_transport()
        self.assertTrue(scene._transport_touched)
        self.assertAlmostEqual(scene._wall_anchor_clock_s, 6.0)
        self.assertEqual(scene.source.muted_calls, [True])  # type: ignore[union-attr]

    def test_touch_transport_is_idempotent(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene._touch_transport()
            scene._touch_transport()
        # latched once only
        self.assertEqual(scene.source.muted_calls, [True])  # type: ignore[union-attr]

    def test_clock_free_runs_after_touch(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene._touch_transport()  # anchors at clock=10.0 (start_time=0)
        with mock.patch.object(scenes.time, "time", return_value=13.5):
            self.assertAlmostEqual(scene._clock_s(), 13.5)

    def test_pause_freezes_clock(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene._touch_transport()
        with mock.patch.object(scenes.time, "time", return_value=15.0):
            scene.transport_pause()
            self.assertTrue(scene._paused)
        frozen = scene._wall_anchor_clock_s
        with mock.patch.object(scenes.time, "time", return_value=100.0):
            self.assertAlmostEqual(scene._clock_s(), frozen)

    def test_resume_continues_from_frozen_value(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene._touch_transport()
        with mock.patch.object(scenes.time, "time", return_value=15.0):
            scene.transport_pause()
        frozen = scene._wall_anchor_clock_s
        with mock.patch.object(scenes.time, "time", return_value=20.0):
            scene.transport_resume()
        self.assertFalse(scene._paused)
        with mock.patch.object(scenes.time, "time", return_value=22.0):
            self.assertAlmostEqual(scene._clock_s(), frozen + 2.0)

    def test_resume_without_pause_is_noop(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene.transport_resume()
        self.assertFalse(scene._transport_touched)

    def test_seek_reanchors_clock_and_calls_source(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene.transport_seek(42.0)
        self.assertEqual(scene._wall_anchor_clock_s, 42.0)
        self.assertEqual(scene.source.seeks, [42.0])  # type: ignore[union-attr]
        self.assertTrue(scene._transport_touched)

    def test_seek_clamps_to_duration(self):
        scene = self._scene()  # duration=100.0
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene.transport_seek(500.0)
        self.assertEqual(scene._wall_anchor_clock_s, 100.0)

    def test_seek_clamps_negative_to_zero(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene.transport_seek(-20.0)
        self.assertEqual(scene._wall_anchor_clock_s, 0.0)

    def test_toggle_pause_first_call_touches_and_pauses(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene.transport_toggle_pause()
        self.assertTrue(scene._paused)
        with mock.patch.object(scenes.time, "time", return_value=11.0):
            scene.transport_toggle_pause()
        self.assertFalse(scene._paused)


class _FakeSceneAudio:
    """Duck-types the AudioStreamer/UltimateAudioSampler slice VideoScene's
    resync path calls: a scriptable position_seconds() and a flush() that
    records its silence_output argument (and ordering when given an events
    list)."""

    def __init__(self, position: float = 0.0, events: list[tuple[str, object]] | None = None):
        self.sample_rate = 8000
        self._position = position
        self.use_reu_pump = False
        self.flush_calls: list[bool] = []
        self._events = events

    def position_seconds(self) -> float:
        return self._position

    def flush(self, *, silence_output: bool = False) -> None:
        self.flush_calls.append(silence_output)
        if self._events is not None:
            self._events.append(("flush", silence_output))


class EmitAudioSeekGuardTest(unittest.TestCase):
    """AVFileSource._emit_audio drops audio while a seek is pending (so stale
    pre-seek samples don't reach the consumer past the splice flush)."""

    def _stub(self, sink) -> AVFileSource:
        src = AVFileSource.__new__(AVFileSource)
        src._muted = False
        src._pending_seek = None
        src._lock = threading.Lock()
        src._audio_push = sink.append
        src.audio_noise_gate = 0
        src.audio_gain = 1.0
        return src

    def test_drops_while_seek_pending(self):
        sink: list[np.ndarray] = []
        src = self._stub(sink)
        src._pending_seek = 12.0
        src._emit_audio(np.array([1, 2, 3], dtype=np.int16))
        self.assertEqual(sink, [])

    def test_passes_after_seek_cleared(self):
        sink: list[np.ndarray] = []
        src = self._stub(sink)
        src._pending_seek = None
        src._emit_audio(np.array([1, 2, 3], dtype=np.int16))
        self.assertEqual(len(sink), 1)

    def test_mute_still_wins_over_pending(self):
        sink: list[np.ndarray] = []
        src = self._stub(sink)
        src._muted = True
        src._pending_seek = None
        src._emit_audio(np.array([1], dtype=np.int16))
        self.assertEqual(sink, [])


class SeekPendingPropertyTest(unittest.TestCase):
    def _src(self) -> AVFileSource:
        src = AVFileSource.__new__(AVFileSource)
        src._lock = threading.Lock()
        src._pending_seek = None
        return src

    def test_false_when_none(self):
        self.assertFalse(self._src().seek_pending)

    def test_true_when_set(self):
        src = self._src()
        src._pending_seek = 5.0
        self.assertTrue(src.seek_pending)


class VideoSceneSpliceTest(unittest.TestCase):
    """VideoScene's Phase 4 audio-resync transport path (loop_audio="on"):
    audio-anchored clock, the _splice primitive, and the tempo_scale domain
    seam. Mirrors VideoSceneClockTest's stub harness with a real audio object
    and an audio-bearing source (a_stream set)."""

    def _resync_scene(
        self, *, position: float = 0.0, tempo_scale: float = 1.0, events=None
    ) -> tuple[VideoScene, _StubSource, _FakeSceneAudio]:
        source = _StubSource(duration=100.0, a_stream=object(), events=events)
        audio = _FakeSceneAudio(position=position, events=events)
        scene = _make_video_scene_stub(source)
        scene.audio = audio  # type: ignore[assignment]
        scene._tempo_scale = tempo_scale
        scene._loop_audio = "on"
        return scene, source, audio

    def test_touch_resolves_resync_and_does_not_mute(self):
        scene, source, _ = self._resync_scene(position=7.0)
        scene._touch_transport()
        self.assertTrue(scene._transport_resync)
        self.assertEqual(source.muted_calls, [])  # NOT muted
        self.assertAlmostEqual(scene._audio_anchor_pos, 7.0)

    def test_touch_with_mute_setting_is_verbatim_phase2(self):
        scene, source, _ = self._resync_scene(position=7.0)
        scene._loop_audio = "mute"
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene._touch_transport()
        self.assertFalse(scene._transport_resync)
        self.assertEqual(source.muted_calls, [True])

    def test_on_without_audio_falls_back_to_mute(self):
        scene = _make_video_scene_stub(_StubSource(duration=100.0))  # audio=None
        scene._loop_audio = "on"
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene._touch_transport()
        self.assertFalse(scene._transport_resync)
        self.assertEqual(scene.source.muted_calls, [True])  # type: ignore[union-attr]

    def test_on_without_audio_stream_falls_back_to_mute(self):
        # audio present, but the source carries no audio stream (a_stream None).
        source = _StubSource(duration=100.0, a_stream=None)
        scene = _make_video_scene_stub(source)
        scene.audio = _FakeSceneAudio()  # type: ignore[assignment]
        scene._loop_audio = "on"
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene._touch_transport()
        self.assertFalse(scene._transport_resync)
        self.assertEqual(source.muted_calls, [True])

    def test_seek_splices(self):
        scene, source, audio = self._resync_scene(position=3.0)
        scene.transport_seek(42.0)
        self.assertEqual(source.seeks, [42.0])  # request_seek fired
        self.assertEqual(audio.flush_calls, [False])  # plain flush (not silence)
        self.assertAlmostEqual(scene._audio_anchor_clock_s, 42.0)  # tempo 1.0

    def test_clock_tracks_audio_delta_not_wall(self):
        scene, _, audio = self._resync_scene(position=0.0)
        scene._touch_transport()  # anchor_clock=0, anchor_pos=0
        audio._position = 5.0
        # Wall time is irrelevant on the resync path — only the audio delta.
        with mock.patch.object(scenes.time, "time", return_value=999.0):
            self.assertAlmostEqual(scene._clock_s(), 5.0)

    def test_pause_freezes_and_silences(self):
        scene, source, audio = self._resync_scene(position=10.0)
        scene._touch_transport()
        scene.transport_pause()
        self.assertTrue(scene._paused)
        self.assertEqual(audio.flush_calls, [True])  # silence_output
        self.assertIn(True, source.muted_calls)
        # Clock frozen at the paused position regardless of further audio motion.
        audio._position = 99.0
        self.assertAlmostEqual(scene._clock_s(), 10.0)

    def test_resume_splices_back_then_unmutes(self):
        events: list[tuple[str, object]] = []
        scene, _, _ = self._resync_scene(position=10.0, events=events)
        scene._touch_transport()
        scene.transport_pause()
        events.clear()
        scene.transport_resume()
        self.assertFalse(scene._paused)
        # Order is load-bearing: splice (request_seek then flush) BEFORE unmute.
        self.assertEqual(
            [e[0] for e in events],
            ["seek", "flush", "muted"],
        )
        self.assertEqual(events[-1], ("muted", False))

    def test_loop_wrap_splices_once_while_seek_pending(self):
        scene, source, _ = self._resync_scene(position=0.0)
        scene._touch_transport()
        scene._loop_a = 0.0
        scene._loop_b = 5.0
        scene._loop_state = "active"
        source.finished = True  # force the wrap path every frame
        scene.process_frame(0.0)
        self.assertEqual(source.seeks, [0.0])  # fired once
        # request_seek set seek_pending; the next frame must NOT re-fire.
        scene.process_frame(0.0)
        self.assertEqual(source.seeks, [0.0])

    def test_tempo_scale_anchor_and_wrap(self):
        # The §3 hotspot: internal clock is scaled (s×content), the transport
        # surface is content seconds. s=0.88.
        scene, source, audio = self._resync_scene(position=0.0, tempo_scale=0.88)
        scene.transport_seek(100.0)
        self.assertAlmostEqual(scene._audio_anchor_clock_s, 88.0)  # 100 × 0.88
        self.assertAlmostEqual(scene.transport_position(), 100.0)  # back to content
        # Loop B stored in content seconds (10) wraps when clock ≥ 8.8.
        scene._loop_a = 0.0
        scene._loop_b = 10.0
        scene._loop_state = "active"
        scene._audio_anchor_clock_s = 0.0
        scene._audio_anchor_pos = 0.0
        source.seeks.clear()
        source.seek_pending = False  # transport_seek(100) set it; clear for wrap
        source.finished = False
        audio._position = 9.0  # clock 9.0 ≥ 8.8 → wrap
        scene.process_frame(0.0)
        self.assertEqual(source.seeks, [0.0])

    def test_tempo_scale_frame_label_in_content_domain(self):
        # Frame-number label must report CONTENT seconds, not the scaled clock
        # (an inverted conversion would double the tempo error into the label).
        scene, source, audio = self._resync_scene(position=0.0, tempo_scale=0.88)
        scene._touch_transport()
        scene._audio_anchor_clock_s = 88.0  # content 100 at s=0.88
        scene._audio_anchor_pos = 0.0
        audio._position = 0.0  # clock = 88.0
        scene.show_frame_numbers = True
        source.video_fps = 30.0
        labels: list[str] = []
        with (
            mock.patch.object(
                scenes, "_annotate_frame_number", lambda img, lbl: labels.append(lbl) or img
            ),
            mock.patch.object(scenes, "_render_with_overlays"),
            mock.patch.object(scenes, "_crop_to_aspect", side_effect=lambda x: x),
        ):
            scene.process_frame(0.0)
        self.assertTrue(labels, "frame-number label was not rendered")
        self.assertTrue(labels[0].startswith(_timecode(100.0)), labels[0])

    def test_mute_path_wrap_compare_unscaled(self):
        # Guard: on the mute path tempo_scale must NOT scale the loop-B compare.
        scene = _make_video_scene_stub(_StubSource(duration=100.0))  # audio=None
        scene._loop_audio = "mute"
        scene._tempo_scale = 0.88
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            scene._touch_transport()
        scene._loop_a = 0.0
        scene._loop_b = 10.0
        scene._loop_state = "active"
        scene._wall_anchor_clock_s = 9.0  # unscaled clock 9.0
        scene._wall_anchor_time = 10.0
        scene.source.finished = False  # type: ignore[union-attr]
        scene.source._frame = None  # type: ignore[union-attr]  # pre-roll → no render path
        with mock.patch.object(scenes.time, "time", return_value=10.0):
            # content compare: 9.0 < 10.0 → NO wrap. A wrongly scaled threshold
            # (8.8) would wrap here.
            scene.process_frame(0.0)
        self.assertEqual(scene.source.seeks, [])  # type: ignore[union-attr]


class VideoSceneLoopToggleTest(unittest.TestCase):
    """transport_loop_toggle's 3-state cycle (mark A -> mark B + active ->
    clear), and the red-border feedback it shares with the Record/Stop pair
    (MIDI live-tune Phase 3) since both drive the same _loop_a/_loop_b/
    _loop_state machine."""

    def _scene(self) -> VideoScene:
        return _make_video_scene_stub(_StubSource(duration=100.0))

    def test_three_state_cycle(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=5.0):
            scene.transport_loop_toggle()
        self.assertEqual(scene._loop_state, "armed")
        self.assertEqual(scene._loop_a, 5.0)
        self.assertIsNone(scene._loop_b)

        with mock.patch.object(scenes.time, "time", return_value=8.0):
            scene.transport_loop_toggle()
        self.assertEqual(scene._loop_state, "active")
        self.assertEqual(scene._loop_a, 5.0)
        self.assertEqual(scene._loop_b, 8.0)

        with mock.patch.object(scenes.time, "time", return_value=9.0):
            scene.transport_loop_toggle()
        self.assertEqual(scene._loop_state, "none")
        self.assertIsNone(scene._loop_a)
        self.assertIsNone(scene._loop_b)

    def test_first_press_reddens_border_second_clears_it(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=5.0):
            scene.transport_loop_toggle()
        self.assertTrue(scene._record_border_active)
        scene.api.write_regs.assert_called_with("d020", 2)  # type: ignore[attr-defined]

        with mock.patch.object(scenes.time, "time", return_value=8.0):
            scene.transport_loop_toggle()
        self.assertFalse(scene._record_border_active)
        scene.api.write_regs.assert_called_with("d020", 0)  # type: ignore[attr-defined]


class VideoSceneRecordStopTest(unittest.TestCase):
    """transport_record (arm) / transport_stop (close loop / pause / quit-
    signal) — the Record/Stop entry point into the same state machine
    transport_loop_toggle drives (MIDI live-tune Phase 3)."""

    def _scene(self) -> VideoScene:
        return _make_video_scene_stub(_StubSource(duration=100.0))

    def test_record_arms_and_reddens_border(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=3.0):
            scene.transport_record()
        self.assertEqual(scene._loop_state, "armed")
        self.assertEqual(scene._loop_a, 3.0)
        self.assertTrue(scene._record_border_active)
        scene.api.write_regs.assert_called_with("d020", 2)  # type: ignore[attr-defined]

    def test_record_is_noop_when_already_armed(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=3.0):
            scene.transport_record()
        with mock.patch.object(scenes.time, "time", return_value=9.0):
            scene.transport_record()
        self.assertEqual(scene._loop_a, 3.0)  # unchanged by the second call

    def test_stop_while_armed_closes_loop_and_clears_border(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=3.0):
            scene.transport_record()
        with mock.patch.object(scenes.time, "time", return_value=7.0):
            quit_requested = scene.transport_stop()
        self.assertFalse(quit_requested)
        self.assertEqual(scene._loop_state, "active")
        self.assertEqual(scene._loop_b, 7.0)
        self.assertFalse(scene._record_border_active)
        scene.api.write_regs.assert_called_with("d020", 0)  # type: ignore[attr-defined]

    def test_stop_while_playing_pauses(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=1.0):
            quit_requested = scene.transport_stop()
        self.assertFalse(quit_requested)
        self.assertTrue(scene._paused)

    def test_stop_while_already_paused_requests_quit(self):
        scene = self._scene()
        with mock.patch.object(scenes.time, "time", return_value=1.0):
            scene.transport_stop()  # first press: pauses
        with mock.patch.object(scenes.time, "time", return_value=2.0):
            quit_requested = scene.transport_stop()  # second press: quit
        self.assertTrue(quit_requested)


class VideoSceneLoopSlotTest(unittest.TestCase):
    """transport_loop_slot: plain press recalls (or whole-file default),
    Stop-held saves, Record-held clears (MIDI live-tune Phase 3)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _scene(self) -> tuple[VideoScene, LoopPresetStore]:
        scene = _make_video_scene_stub(_StubSource(duration=100.0))
        store = LoopPresetStore(Path(self._tmp.name) / "loop.json", video_ref="clip.mp4", size=123)
        scene._loop_store = store
        return scene, store

    def test_save_persists_current_loop(self):
        scene, store = self._scene()
        scene._loop_a = 10.0
        scene._loop_b = 20.0
        scene.transport_loop_slot(1, save=True, clear=False)
        self.assertEqual(store.load(), {"1": {"a": 10.0, "b": 20.0}})

    def test_save_with_no_current_loop_is_noop(self):
        scene, store = self._scene()
        scene.transport_loop_slot(1, save=True, clear=False)
        self.assertEqual(store.load(), {})

    def test_clear_deletes_slot(self):
        scene, store = self._scene()
        store.save(2, 1.0, 2.0)
        scene.transport_loop_slot(2, save=False, clear=True)
        self.assertEqual(store.load(), {})

    def test_plain_press_recalls_stored_slot_and_seeks(self):
        scene, store = self._scene()
        store.save(3, 12.0, 34.0)
        scene.transport_loop_slot(3, save=False, clear=False)
        self.assertEqual(scene._loop_a, 12.0)
        self.assertEqual(scene._loop_b, 34.0)
        self.assertEqual(scene._loop_state, "active")
        self.assertEqual(scene.source.seeks, [12.0])  # type: ignore[union-attr]

    def test_plain_press_on_empty_slot_loops_whole_file(self):
        scene, _store = self._scene()
        scene.transport_loop_slot(9, save=False, clear=False)
        self.assertEqual(scene._loop_a, 0.0)
        self.assertIsNone(scene._loop_b)
        self.assertEqual(scene._loop_state, "active")

    def test_recall_resumes_if_paused(self):
        scene, store = self._scene()
        store.save(1, 5.0, None)
        scene._paused = True
        scene._transport_touched = True
        scene.transport_loop_slot(1, save=False, clear=False)
        self.assertFalse(scene._paused)


class VideoSceneRecordBorderTeardownTest(unittest.TestCase):
    def test_teardown_restores_border_when_left_armed(self):
        scene = _make_video_scene_stub(_StubSource(duration=100.0))
        scene.audio = None
        scene._av_lag_count = 0
        with mock.patch.object(scenes.time, "time", return_value=1.0):
            scene.transport_record()
        self.assertTrue(scene._record_border_active)
        scene.teardown()
        self.assertFalse(scene._record_border_active)
        scene.api.write_regs.assert_called_with("d020", 0)  # type: ignore[attr-defined]

    def test_teardown_is_noop_when_never_armed(self):
        scene = _make_video_scene_stub(_StubSource(duration=100.0))
        scene.audio = None
        scene._av_lag_count = 0
        scene.teardown()
        scene.api.write_regs.assert_not_called()  # type: ignore[attr-defined]


class VideoSceneProcessFrameLoopTest(unittest.TestCase):
    """process_frame's EOF check + loop-wrap: an active A/B loop neither
    ends the scene at EOF nor at reaching B — it seeks back to A instead."""

    def test_wraps_to_a_when_clock_reaches_b(self):
        source = _StubSource(duration=100.0)
        scene = _make_video_scene_stub(source)
        scene._transport_touched = True
        scene._loop_state = "active"
        scene._loop_a = 5.0
        scene._loop_b = 10.0
        scene._wall_anchor_clock_s = 10.0
        scene._wall_anchor_time = 0.0
        with mock.patch.object(scenes.time, "time", return_value=0.0):
            still_active = scene.process_frame(current_time=0.0)
        self.assertTrue(still_active)
        self.assertEqual(source.seeks, [5.0])
        self.assertEqual(scene._wall_anchor_clock_s, 5.0)

    def test_wraps_to_a_when_source_hits_eof_before_b(self):
        source = _StubSource(duration=100.0)
        source.finished = True
        scene = _make_video_scene_stub(source)
        scene._transport_touched = True
        scene._loop_state = "active"
        scene._loop_a = 5.0
        scene._loop_b = 50.0  # clock hasn't reached B yet
        scene._wall_anchor_clock_s = 20.0
        scene._wall_anchor_time = 0.0
        with mock.patch.object(scenes.time, "time", return_value=0.0):
            still_active = scene.process_frame(current_time=0.0)
        self.assertTrue(still_active)
        self.assertEqual(source.seeks, [5.0])

    def test_finished_without_active_loop_ends_scene(self):
        source = _StubSource(duration=100.0)
        source.finished = True
        scene = _make_video_scene_stub(source)
        self.assertFalse(scene.process_frame(current_time=0.0))

    def test_finished_with_armed_but_not_active_loop_ends_scene(self):
        # "armed" (only A marked) must not suppress the EOF check — only
        # "active" (both A and B marked) does.
        source = _StubSource(duration=100.0)
        source.finished = True
        scene = _make_video_scene_stub(source)
        scene._loop_state = "armed"
        scene._loop_a = 5.0
        self.assertFalse(scene.process_frame(current_time=0.0))


class VideoSceneFrameNumberLabelTest(unittest.TestCase):
    """show_frame_numbers' file-position label must not double-count
    start_s once transport has re-anchored the clock to an absolute file
    position (see design decision 2 of the transport plan)."""

    def _run(self, scene: VideoScene) -> str:
        captured: dict[str, str] = {}

        def fake_annotate(img, label):
            captured["label"] = label
            return img

        with (
            mock.patch.object(scenes, "_annotate_frame_number", side_effect=fake_annotate),
            mock.patch.object(scenes, "_render_with_overlays"),
            mock.patch.object(scenes.time, "time", return_value=0.0),
        ):
            scene.process_frame(current_time=0.0)
        return captured["label"]

    def test_untouched_adds_start_s(self):
        source = _StubSource(duration=None)
        scene = _make_video_scene_stub(source, start_s=50.0)
        scene.show_frame_numbers = True
        label = self._run(scene)
        # clock_s reads 0.0 (untouched, no audio -> wall-from-start_time,
        # both zero); start_s(50) is added back for the true file offset.
        self.assertIn(_timecode(50.0), label)

    def test_touched_does_not_double_count_start_s(self):
        source = _StubSource(duration=None)
        scene = _make_video_scene_stub(source, start_s=50.0)
        scene.show_frame_numbers = True
        scene._transport_touched = True
        scene._wall_anchor_clock_s = 80.0  # already an absolute file position
        scene._wall_anchor_time = 0.0
        label = self._run(scene)
        self.assertIn(_timecode(80.0), label)
        self.assertNotIn(_timecode(130.0), label)  # the double-counted (wrong) value


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
        src._pts_anchor_target = 0.0
        src._pending_seek = None
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
        src._muted = False
        src._pending_seek = None
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


@unittest.skipUnless(_ensure_pyav(), "PyAV not installed")
class DurationSTest(unittest.TestCase):
    """`AVFileSource.duration_s` (MIDI live-tune Phase 2 — absolute-jog
    mapping and seek/loop clamping) reads the container's real duration at
    construction. Uses the synthetic fixture, so a real (if tiny) decode."""

    def _make(self) -> str:
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        _write_synthetic_video(path, seconds=3, fps=30)
        return path

    def test_duration_matches_encoded_length(self):
        path = self._make()
        src = AVFileSource(path, target_sample_rate=8000, scan_audio_peak=False)
        try:
            self.assertIsNotNone(src.duration_s)
            assert src.duration_s is not None
            self.assertAlmostEqual(src.duration_s, 3.0, delta=0.5)
        finally:
            src.close()


if __name__ == "__main__":
    unittest.main()
