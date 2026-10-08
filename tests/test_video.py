"""Tests for the video module's pure helpers (no PyAV / no real file)."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import ExitStack, suppress
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import numpy as np
from _fakes import FakeAPI, FrozenClock

from c64cast.audio.audio import AudioStreamer
from c64cast.audio.audio_handlers import CHUNK_SIZE, PREBUFFER_CHUNKS
from c64cast.audio.audio_servo import HOST_DMA_SERVO_TARGET_GAP
from c64cast.audio.sampler import DEFAULT_LEAD_SECONDS, UltimateAudioSampler
from c64cast.audio.splice import FlushCut
from c64cast.control.transport import LoopPresetStore, timecode
from c64cast.hw.api import Ultimate64API
from c64cast.hw.c64 import RegionID
from c64cast.scenes import scenes, video_transport
from c64cast.scenes.scenes import VideoScene
from c64cast.video import video as video_mod
from c64cast.video.video import (
    AUDIO_DISCONTINUITY_S,
    DRY_FILL_MAX_PAST_NEWEST_S,
    DRY_FILL_MIN_LEAD_S,
    DRY_FILL_PAST_NEWEST_STEP_S,
    NORMALIZATION_MAX_GAIN,
    NORMALIZATION_TARGET_PEAK,
    SILENCE_PIECE_SAMPLES,
    AVFileSource,
    RemoteSeekStalled,
    _build_atempo_graph,
    _compute_normalization_gain,
    _ContainerCloser,
    _is_remote_url,
    _plan_decode_size,
    _SampleProgressTap,
    av_open,
    ensure_pyav,
    probe_container_title,
    scan_video_samples,
)


def _wait_until(predicate, limit_s: float = 5.0) -> bool:
    """Poll `predicate` until it holds or `limit_s` passes; its last answer."""
    deadline = time.monotonic() + limit_s
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


def _arm_locks(src: AVFileSource) -> AVFileSource:
    """The lock-side fields every `__new__` stub needs, in one place: the
    buffer lock, the condition that wakes a demux thread parked at EOF, and
    the thread-exited flag `finished` reads."""
    src._lock = threading.Lock()
    src._wake = threading.Condition(src._lock)
    src._demux_exited = False
    return src


def _signal_close(src: AVFileSource) -> None:
    """The half of `close()` a stub can run: set `_closed` and wake a demux
    thread parked at EOF, leaving the (fake) container and poll alone."""
    with src._lock:
        src._closed = True
        src._wake.notify_all()


def _demux_until_parked(src: AVFileSource) -> None:
    """Run the real `_demux_loop` until it parks at EOF waiting for a seek,
    then close it the way `close()` does and join it. A loop that never
    parks fails the test instead of hanging the run."""
    worker = threading.Thread(target=src._demux_loop, daemon=True)
    worker.start()
    try:
        if not _wait_until(lambda: src._eof or not worker.is_alive()):
            raise AssertionError("demux loop never reached EOF")
    finally:
        _signal_close(src)
        worker.join(5.0)
    if worker.is_alive():
        raise AssertionError("demux loop did not exit on close")


def _make_av_source_stub(frames: list[tuple[float, np.ndarray]], eof: bool) -> AVFileSource:
    """Build an AVFileSource without going through __init__ (which opens a
    real container via PyAV). Only the attributes touched by
    `current_frame` / `finished` are set; everything else stays unset."""
    src = AVFileSource.__new__(AVFileSource)
    src._video_buf = list(frames)
    _arm_locks(src)
    src._eof = eof
    src._pending_seek = None
    src._frames_taken, src._clock_read = 0, None
    return src


class SampleProgressTapTest(unittest.TestCase):
    """The pre-scan progress tap is just another accumulator: it counts
    add() calls against the planned sample total."""

    def test_reports_fraction_of_planned_samples(self):
        fractions: list[float] = []
        tap = _SampleProgressTap(4, fractions.append)
        for _ in range(3):
            tap.add(None)
        self.assertEqual(fractions, [0.25, 0.5, 0.75])

    def test_zero_total_cannot_divide_by_zero(self):
        fractions: list[float] = []
        tap = _SampleProgressTap(0, fractions.append)
        tap.add(None)
        self.assertEqual(fractions, [1.0])


class RemoteUrlTest(unittest.TestCase):
    def test_http_and_https_are_remote(self):
        self.assertTrue(_is_remote_url("http://example.com/a.mp4"))
        self.assertTrue(_is_remote_url("https://rr4.googlevideo.com/videoplayback?x=1"))

    def test_local_paths_are_not_remote(self):
        self.assertFalse(_is_remote_url("/home/user/assets/videos/clip.mp4"))
        self.assertFalse(_is_remote_url("assets/videos/clip.webm"))
        self.assertFalse(_is_remote_url("file:///tmp/clip.mp4"))


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class RemoteRefusalMessageTest(unittest.TestCase):
    """A signed stream URL is resolved once, when the playlist is built, and a
    looping show replays it until the signature expires — after which the raw
    `HTTPForbiddenError` says nothing about why an afternoon-long show suddenly
    stopped, or that a reload re-resolves it."""

    #: Every construction here passes the **third** argument. PyAV only appends
    #: a filename to an ``FFmpegError``'s string form when it was given one, so
    #: a two-argument fake makes `test_the_url_is_not_quoted_back` pass no
    #: matter what the code does — which is exactly how the leak this guards
    #: against shipped in the first place.
    def _refused(self, url, exc=None):
        import av
        import av.error

        err = exc or av.error.HTTPForbiddenError(403, "Server returned 403 Forbidden", url)
        with mock.patch.object(av, "open", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                av_open(url)
        return str(cm.exception)

    def test_the_fake_error_really_does_carry_the_url(self):
        # Guards the guard: if PyAV ever stops appending the filename, the
        # redaction tests below would start passing vacuously again.
        import av.error

        url = "https://cdn.example/clip.mp4?sig=abc"
        self.assertIn(url, str(av.error.HTTPForbiddenError(403, "403", url)))

    def test_a_remote_4xx_names_expiry_and_the_remedy(self):
        msg = self._refused("https://rr4.googlevideo.com/videoplayback?sig=stale")
        self.assertIn("expired signature", msg)
        self.assertIn("SIGHUP", msg)

    def test_the_url_is_not_quoted_back(self):
        # It can carry a signature or a credential, and every caller's own log
        # line already names the path it was opening.
        msg = self._refused("https://user:tok@cdn.example/clip.mp4?sig=abc")
        self.assertNotIn("tok", msg)
        self.assertNotIn("sig=abc", msg)
        self.assertNotIn("cdn.example", msg)
        self.assertIn("403 Forbidden", msg)

    def test_a_404_does_not_blame_an_expired_signature(self):
        # A pulled video, not a stale signature — reloading the playlist
        # re-resolves the same missing URL and points the operator nowhere.
        import av.error

        url = "https://cdn.example/gone.mp4?sig=abc"
        msg = self._refused(
            url, av.error.HTTPNotFoundError(404, "Server returned 404 Not Found", url)
        )
        self.assertIn("404 Not Found", msg)
        self.assertNotIn("expired signature", msg)
        self.assertNotIn("SIGHUP", msg)
        self.assertNotIn("sig=abc", msg)

    def test_a_local_path_is_left_alone(self):
        # No signature to expire, and the wrapper must not add the HTTP
        # reconnect options to a local open.
        import av

        with mock.patch.object(av, "open", return_value="container") as opened:
            self.assertEqual(av_open("/tmp/clip.mp4"), "container")
        opened.assert_called_once_with("/tmp/clip.mp4")

    def test_a_drive_letter_or_file_url_is_a_local_path(self):
        # FFmpeg reads both with its `file` protocol, so neither gets the
        # network bound — a one-letter "scheme" is a DOS drive, not a protocol.
        import av

        for path in ("C:\\clips\\clip.mp4", "file:/tmp/clip.mp4"):
            with self.subTest(path=path):
                with mock.patch.object(av, "open", return_value="container") as opened:
                    av_open(path)
                opened.assert_called_once_with(path)


class _StallingHttpServer:
    """A loopback server that accepts, sends `preamble`, then never writes
    again and never closes — the shape of a wedged CDN. `close()` releases
    every socket and joins the accept thread."""

    def __init__(self, preamble: bytes = b""):
        import socket

        self._preamble = preamble
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self._srv.settimeout(0.05)
        self.port = self._srv.getsockname()[1]
        self._held: list = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                continue
            self._held.append(conn)
            if self._preamble:
                conn.recv(4096)
                conn.sendall(self._preamble)

    def close(self):
        self._stop.set()
        self._thread.join(2.0)
        for conn in self._held:
            conn.close()
        self._srv.close()


def _wav_bytes(seconds: float = 2.0, rate: int = 8000) -> bytes:
    import struct

    data = b"\x00\x00" * int(seconds * rate)
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class RemoteStallBoundTest(unittest.TestCase):
    """A server that accepts and then goes silent must not hang the thread
    opening or reading it — that thread is the playlist's, for an audio-file
    scene's setup, and nothing else can unstick it."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch("c64cast.video.video._REMOTE_OPEN_TIMEOUT_S", 0.5))
        stack.enter_context(mock.patch("c64cast.video.video._REMOTE_READ_TIMEOUT_S", 0.5))

    def _bounded(self, fn, limit_s: float = 10.0):
        """Run `fn` on a worker and return what it raised (or its result). A
        worker still blocked after `limit_s` fails the test here rather than
        hanging the run; closing the server in cleanup then releases it."""
        box: list = []

        def run():
            try:
                box.append(fn())
            except Exception as e:  # noqa: BLE001 — the raise is the result
                box.append(e)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(limit_s)
        self.assertFalse(worker.is_alive(), f"still blocked after {limit_s}s")
        return box[0]

    def _server(self, preamble: bytes = b"") -> _StallingHttpServer:
        server = _StallingHttpServer(preamble)
        self.addCleanup(server.close)
        return server

    def test_a_silent_server_fails_the_open(self):
        import av.error

        server = self._server()
        outcome = self._bounded(lambda: av_open(f"http://127.0.0.1:{server.port}/tune.wav"))
        self.assertIsInstance(outcome, av.error.ExitError)

    def test_a_silent_peer_on_another_protocol_fails_the_open(self):
        import av.error

        # http(s) is not the only network protocol FFmpeg honors, and an
        # audio-file entry reaches av_open on its extension alone.
        server = self._server()
        outcome = self._bounded(lambda: av_open(f"tcp://127.0.0.1:{server.port}/tune.wav"))
        self.assertIsInstance(outcome, av.error.ExitError)

    def test_a_silent_rtsp_peer_is_bounded_by_ffmpegs_own_io_timeout(self):
        # The open bound is set out of reach, so only the per-IO timeout
        # (twice the 0.5 s read bound) can end this open inside the limit —
        # the same timeout that ends a seek nothing else interrupts.
        self.enterContext(mock.patch("c64cast.video.video._REMOTE_OPEN_TIMEOUT_S", 60.0))
        server = self._server()
        started = time.monotonic()
        outcome = self._bounded(lambda: av_open(f"rtsp://127.0.0.1:{server.port}/tune.wav"))
        elapsed = time.monotonic() - started
        self.assertIsInstance(outcome, Exception)
        # An open that fails before reaching the peer would pass the two
        # checks above without the timeout ever being exercised.
        self.assertTrue(server._held, "the open never reached the silent peer")
        # FFmpeg reports this timeout as InvalidDataError, so the type cannot
        # tell it from a bound passed in the wrong unit; the wait can.
        self.assertGreaterEqual(elapsed, 0.75, "the IO timeout fired far short of its bound")

    def test_a_stream_that_stalls_mid_body_fails_the_read(self):
        import av.error

        # Enough body that probing finishes and the open returns: the stall
        # has to land in demux, past the open bound, for this to test reads.
        body = _wav_bytes(30.0)
        head = (
            b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
        )
        server = self._server(head + body[:200_000])
        container = av_open(f"http://127.0.0.1:{server.port}/tune.wav")

        def drain():
            for _ in container.demux(container.streams.audio[0]):
                pass

        # Closed here, not from a cleanup: if the bound regresses, `_bounded`
        # fails with the worker still inside av_read_frame, and closing the
        # container under it would free the context it is reading. Left open,
        # the worker's own reference keeps it alive until the server's
        # cleanup releases the read.
        outcome = self._bounded(drain)
        container.close()
        self.assertIsInstance(outcome, av.error.ExitError)


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class ResamplerTailTest(unittest.TestCase):
    """A resampler holds back its filter's tail until flushed with None, so a
    decode that stops at the last packet drops the end of every track."""

    #: 0.4 s at 8 kHz, resampled to 44 kHz.
    EXPECTED = 17600

    def _wav(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "t.wav")
        Path(path).write_bytes(_wav_bytes(0.4, 8000))
        return path

    def test_decode_audio_full_keeps_the_tail(self):
        from c64cast.video.video import decode_audio_full

        self.assertEqual(decode_audio_full(self._wav(), 44000).size, self.EXPECTED)

    def _demux_source(self, pushed: list[int]) -> AVFileSource:
        import av

        src = AVFileSource.__new__(AVFileSource)
        src._audio_push = lambda arr: pushed.append(int(arr.size))
        src._audio_end = None
        src._audio_epoch = None
        src._audio_fed_s = None
        src._audio_trim = 0
        src._video_read_s = None
        src._audio_lag_s = 0.0
        src._audio_shift_s = 0.0
        src._audio_jump_warned = False
        src._dry_stall_level, src._dry_window = 0, None
        src._frames_taken, src._clock_read = 0, None
        src._pts_offset = None
        src._pts_anchor_target = 0.0
        src._tempo_scale = 1.0
        src.target_sr = 44000
        src._resampler = av.AudioResampler(format="s16", layout="mono", rate=44000)
        src._atempo_graph = None
        src._closed = False
        src._muted = False
        src._pending_seek = None
        src.audio_noise_gate = 0
        src.audio_gain = 1.0
        container = av.open(self._wav())
        self.addCleanup(container.close)
        src.container = container
        src.path = "t.wav"
        _arm_locks(src)
        src._eof = False
        return src

    def test_the_demux_path_flushes_the_tail_at_eof(self):
        pushed: list[int] = []
        src = self._demux_source(pushed)
        _demux_until_parked(src)
        self.assertTrue(src._eof)
        self.assertEqual(sum(pushed), self.EXPECTED)

    def test_a_demuxer_that_ends_by_raising_eof_still_flushes_the_tail(self):
        # Some demuxers end by raising EOFError instead of running dry; that
        # branch flushes too.
        pushed: list[int] = []
        src = self._demux_source(pushed)
        real = src.container

        class _RaisesAtEof:
            def demux(self, *streams):
                yield from real.demux(*streams)
                raise EOFError

        src.container = _RaisesAtEof()
        _demux_until_parked(src)
        self.assertTrue(src._eof)
        self.assertEqual(sum(pushed), self.EXPECTED)


class ProbeContainerTitleTest(unittest.TestCase):
    """A cheap header-only peek at a local file's own `title` tag — no real
    PyAV container, so av_open/ensure_pyav are faked."""

    class _FakeContainer:
        def __init__(self, metadata):
            self.metadata = metadata
            self.closed = False

        def close(self):
            self.closed = True

    def test_remote_url_skipped_without_touching_pyav(self):
        # Never even calls ensure_pyav/av_open — a probe here would be real
        # network I/O just to pick a display name.
        with (
            mock.patch("c64cast.video.video.ensure_pyav") as ensure,
            mock.patch("c64cast.video.video.av_open") as opener,
        ):
            self.assertIsNone(probe_container_title("https://example.com/clip.mp4"))
        ensure.assert_not_called()
        opener.assert_not_called()

    def test_missing_local_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(probe_container_title(os.path.join(tmp, "nope.mp4")))

    def test_pyav_unavailable_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            with mock.patch("c64cast.video.video.ensure_pyav", return_value=False):
                self.assertIsNone(probe_container_title(f.name))

    def test_open_failure_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            with (
                mock.patch("c64cast.video.video.ensure_pyav", return_value=True),
                mock.patch("c64cast.video.video.av_open", side_effect=OSError("bad container")),
            ):
                self.assertIsNone(probe_container_title(f.name))

    def test_title_tag_returned_and_container_closed(self):
        container = self._FakeContainer({"title": "Cool Clip"})
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            with (
                mock.patch("c64cast.video.video.ensure_pyav", return_value=True),
                mock.patch("c64cast.video.video.av_open", return_value=container),
            ):
                self.assertEqual(probe_container_title(f.name), "Cool Clip")
        self.assertTrue(container.closed)

    def test_no_title_tag_returns_none(self):
        container = self._FakeContainer({"encoder": "Lavf62.12.101"})
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            with (
                mock.patch("c64cast.video.video.ensure_pyav", return_value=True),
                mock.patch("c64cast.video.video.av_open", return_value=container),
            ):
                self.assertIsNone(probe_container_title(f.name))

    def test_blank_title_tag_returns_none(self):
        container = self._FakeContainer({"title": "   "})
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            with (
                mock.patch("c64cast.video.video.ensure_pyav", return_value=True),
                mock.patch("c64cast.video.video.av_open", return_value=container),
            ):
                self.assertIsNone(probe_container_title(f.name))


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
        # A gain just under the cap does not trip the cap.
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
        # Pre-EOF the stall protection is right: a consumed frame stays in the
        # buffer to re-emit if the audio clock stalls, rather than black-framing.
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
        # EOF is set but the audio clock is still behind, so only the
        # consumed-through range may be dropped, the chosen frame included.
        frames = [
            (t, np.full((2, 2, 3), int(t * 10), dtype=np.uint8)) for t in (0.0, 1.0, 2.0, 3.0)
        ]
        src = _make_av_source_stub(frames, eof=True)
        # Consume through PTS=1.0 (the second frame). PTS=2.0 and 3.0 are
        # ahead of the clock; they stay.
        chosen = src.current_frame(audio_position_s=1.5)
        assert chosen is not None
        self.assertEqual(chosen[0, 0, 0], 10)
        # With the EOF-aware drain the kept frame at index 1 triggers a full
        # drain only when it is also the LAST in the buffer; here it is not,
        # so normal trim applies and it stays as stall protection.
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
        # One read head shared by every demux() call, as a real container's
        # is: a pass that ends on a seek and the next pass read on from it.
        self._packets = iter(packets)

    def demux(self):
        return self._packets

    def seek(self, offset_us: int) -> None:
        # No-op by default (recording variants override this attribute
        # per-test); real PyAV would reposition the demux read head.
        pass


def _make_demux_source_stub(
    packets: list[_FakePacket],
    *,
    pending_seek: float | None = None,
    anchor: float = 0.0,
    decode_target: tuple[int, int] | None = None,
) -> AVFileSource:
    """The demux-loop stub shared by DemuxRebaseTest / TransportSeekTest /
    DemuxDecodeDownscaleTest: every attribute `_demux_loop` touches, set in
    ONE place so a new demux field can't silently miss a subset of the
    stubs. Stays on __new__ because the real __init__ opens a PyAV
    container — the one AVFileSource path a fake can't ride through."""
    src = AVFileSource.__new__(AVFileSource)
    src._closed = False
    src._pts_offset = None
    src._pts_anchor_target = anchor
    src._pending_seek = pending_seek
    src._muted = False
    src.video_time_base = 1.0  # 1 PTS tick == 1 second
    src._video_buf = []
    _arm_locks(src)
    src._eof = False
    src.max_video_buffer = 240
    src._resampler = None
    src._audio_push = None
    src._audio_end = None
    src._audio_epoch = None
    src._audio_fed_s = None
    src._audio_trim = 0
    src._video_read_s = None
    src._audio_lag_s = 0.0
    src._audio_shift_s = 0.0
    src._audio_jump_warned = False
    src._dry_stall_level, src._dry_window = 0, None
    src._frames_taken, src._clock_read = 0, None
    src._decode_target = decode_target
    src._decode_size = None
    src._decode_planned = False
    src._tempo_scale = 1.0
    src._atempo_graph = None
    src.last_frame_pts = 0.0
    src.path = "fake"
    src.target_sr = 8000
    src.a_stream = None
    src.container = _FakeContainer(packets)
    src._closer = _ContainerCloser(src.container)
    return src


def _make_emit_audio_stub(sink: list[np.ndarray], *, tempo_scale: float = 1.0) -> AVFileSource:
    """The `_emit_audio` stub shared by MuteLatchTest / EmitAudioSeekGuardTest /
    SeekPendingPropertyTest / AtempoTempoCompensationTest — every attribute
    the emit path reads, in one place (same rationale as
    `_make_demux_source_stub`)."""
    src = AVFileSource.__new__(AVFileSource)
    src.path = "test.mp4"
    src._closed = False
    src._muted = False
    src._pending_seek = None
    _arm_locks(src)
    src._audio_push = sink.append
    src._audio_epoch = None
    src._audio_trim = 0
    src._video_buf = []
    src.audio_noise_gate = 0
    src.audio_gain = 1.0
    src._tempo_scale = tempo_scale
    src._atempo_graph = None
    return src


class DemuxRebaseTest(unittest.TestCase):
    """`_demux_loop` rebases video PTS by the first decoded frame so a seeked
    source (frame PTS ~start_s) still starts at the from-0 playback clock.
    Driven with a fake container — no PyAV, no real file."""

    def _run_demux(self, frame_ptss: list[int]) -> list[float]:
        src = _make_demux_source_stub([_FakePacket([_FakeFrame(p)]) for p in frame_ptss])
        _demux_until_parked(src)
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
        return _make_emit_audio_stub(sink)

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

    def test_closed_source_drops_packets(self):
        # A demux thread outliving close()'s bounded join must not feed the
        # reused sampler that the scene's next setup() re-armed.
        sink: list[np.ndarray] = []
        src = self._stub(sink)
        src._closed = True
        src._emit_audio(np.array([1, 2, 3], dtype=np.int16))
        self.assertEqual(sink, [])


class TransportSeekTest(unittest.TestCase):
    """`request_seek`/`_apply_pending_seek` (MIDI live-tune Phase 2): a
    seek clears the stale pre-seek buffer immediately and re-anchors the
    demux thread's PTS rebase to land on the requested target_s instead of
    0 — the transport plan's "clock IS file position once touched" design.
    Driven with the same fake-container harness as DemuxRebaseTest — no
    PyAV, no real file."""

    def _make_src(self, frame_ptss, *, pending_seek=None, anchor=0.0) -> AVFileSource:
        return _make_demux_source_stub(
            [_FakePacket([_FakeFrame(p)]) for p in frame_ptss],
            pending_seek=pending_seek,
            anchor=anchor,
        )

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
        # The first packet fetched is whatever was in flight when the seek was
        # requested, and the real demux loop discards it and re-fetches from
        # the container's new position (see _apply_pending_seek). Modeled with
        # a throwaway packet, so the post-seek keyframes' rebased PTS must
        # land AT the seek target, not at 0 like an ordinary start_s seek.
        src = self._make_src([], pending_seek=30.0)
        stale = _FakePacket([_FakeFrame(999)])
        real = [_FakePacket([_FakeFrame(p)]) for p in (50, 51, 52)]
        src.container = _FakeContainer([stale, *real])
        _demux_until_parked(src)
        self.assertEqual([pts for pts, _ in src._video_buf], [30.0, 31.0, 32.0])
        self.assertIsNone(src._pending_seek, "pending seek must be consumed")
        self.assertEqual(src._pts_anchor_target, 30.0)

    def test_container_seek_called_with_microseconds(self):
        seeks: list[int] = []
        src = self._make_src([10], pending_seek=7.5)
        src.container.seek = seeks.append  # type: ignore[method-assign]
        _demux_until_parked(src)
        self.assertEqual(seeks, [7_500_000])

    def test_no_pending_seek_behaves_like_ordinary_start(self):
        src = self._make_src([0, 1, 2])
        _demux_until_parked(src)
        self.assertEqual([pts for pts, _ in src._video_buf], [0.0, 1.0, 2.0])

    def test_apply_pending_seek_returns_false_when_none_queued(self):
        src = self._make_src([0])
        self.assertFalse(src._apply_pending_seek())

    def test_a_close_during_the_seek_ends_the_loop_before_the_next_pass(self):
        # On a remote input the seek's worker closes the container once it
        # returns, so a pass opened after that would read a freed container.
        src = self._make_src([], pending_seek=3.0)
        src._demux_poll = None
        closed_at_demux: list[bool] = []
        packets = src.container.demux()
        src.container.demux = lambda: closed_at_demux.append(src._closed) or packets
        src.container.seek = lambda _offset: src.close()
        src.container.close = lambda: None
        src._demux_loop()
        self.assertEqual(closed_at_demux, [False])


class _StubSource:
    """Duck-types the bits of AVFileSource that VideoScene's transport
    surface and process_frame's loop-wrap logic touch, without any PyAV
    dependency — the source-side companion to `_make_video_scene_stub`."""

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
        self.accepts_seeks = True
        self.last_frame_pts = 0.0
        self.seeks: list[float] = []
        self.muted_calls: list[bool] = []
        # A non-None a_stream marks the source audio-bearing, which
        # VideoScene._touch_transport requires to resolve the resync path;
        # `events` records ordered request_seek/set_muted calls so a test can
        # pin the resume splice-then-unmute ordering.
        self.a_stream = a_stream
        self.seek_pending = False
        self._events = events
        self._frame = np.zeros((200, 320, 3), dtype=np.uint8)

    def request_seek(
        self,
        target_s: float,
        *,
        unmute: bool = False,
        on_request: Callable[[], object] | None = None,
    ) -> object:
        self.seeks.append(target_s)
        self.seek_pending = True
        if self._events is not None:
            self._events.append(("seek", target_s))
        if unmute:
            self.set_muted(False)
        return on_request() if on_request is not None else None

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


def _freeze_time(t: float) -> ExitStack:
    """Freeze `time` in BOTH namespaces the video clock reads it from —
    scenes (the wall_start_time epoch) and video_transport (the transport
    anchors) — so a call site moving between the two modules can never
    silently escape the freeze."""
    stack = ExitStack()
    clock = FrozenClock(t)
    stack.enter_context(mock.patch.object(scenes, "time", clock))
    stack.enter_context(mock.patch.object(video_transport, "time", clock))
    return stack


# resolve_file_spec passes URLs through with no existence check, which lets
# the stub builder run the real, I/O-free __init__ without a file on disk.
STUB_VIDEO_URL = "https://stub.invalid/clip.mp4"


def _make_video_scene_stub(source: _StubSource, *, start_s: float = 0.0) -> VideoScene:
    """Build a VideoScene through the REAL constructor (only setup() needs
    PyAV + a real AudioStreamer), then swap in the duck-typed source. A field
    added to __init__ can never silently miss this stub — the bug class
    PR #227 fixed for AudioStreamer fixtures."""
    scene = VideoScene(
        api=mock.MagicMock(),
        audio=None,
        display_mode=mock.MagicMock(),
        file=STUB_VIDEO_URL,
        start_s=start_s,
    )
    scene.source = source  # type: ignore[assignment]  # duck-typed stub, not a real AVFileSource
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
        scene.wall_start_time = 3.0
        with _freeze_time(10.0):
            self.assertAlmostEqual(scene.transport.clock_s(), 7.0)

    def test_touch_transport_freezes_current_reading_and_mutes(self):
        scene = self._scene()
        scene.wall_start_time = 4.0  # untouched clock would read 6.0
        with _freeze_time(10.0):
            scene.transport.touch()
        self.assertTrue(scene.transport.touched)
        self.assertAlmostEqual(scene.transport.wall_anchor_clock_s, 6.0)
        self.assertEqual(scene.source.muted_calls, [True])  # type: ignore[union-attr]

    def test_touch_transport_is_idempotent(self):
        scene = self._scene()
        with _freeze_time(10.0):
            scene.transport.touch()
            scene.transport.touch()
        # latched once only
        self.assertEqual(scene.source.muted_calls, [True])  # type: ignore[union-attr]

    def test_clock_free_runs_after_touch(self):
        scene = self._scene()
        with _freeze_time(10.0):
            scene.transport.touch()  # anchors at clock=10.0 (start_time=0)
        with _freeze_time(13.5):
            self.assertAlmostEqual(scene.transport.clock_s(), 13.5)

    def test_pause_freezes_clock(self):
        scene = self._scene()
        with _freeze_time(10.0):
            scene.transport.touch()
        with _freeze_time(15.0):
            scene.transport_pause()
            self.assertTrue(scene.transport.paused)
        frozen = scene.transport.wall_anchor_clock_s
        with _freeze_time(100.0):
            self.assertAlmostEqual(scene.transport.clock_s(), frozen)

    def test_resume_continues_from_frozen_value(self):
        scene = self._scene()
        with _freeze_time(10.0):
            scene.transport.touch()
        with _freeze_time(15.0):
            scene.transport_pause()
        frozen = scene.transport.wall_anchor_clock_s
        with _freeze_time(20.0):
            scene.transport_resume()
        self.assertFalse(scene.transport.paused)
        with _freeze_time(22.0):
            self.assertAlmostEqual(scene.transport.clock_s(), frozen + 2.0)

    def test_resume_without_pause_is_noop(self):
        scene = self._scene()
        with _freeze_time(10.0):
            scene.transport_resume()
        self.assertFalse(scene.transport.touched)

    def test_seek_reanchors_clock_and_calls_source(self):
        scene = self._scene()
        with _freeze_time(10.0):
            scene.transport_seek(42.0)
        self.assertEqual(scene.transport.wall_anchor_clock_s, 42.0)
        self.assertEqual(scene.source.seeks, [42.0])  # type: ignore[union-attr]
        self.assertTrue(scene.transport.touched)

    def test_seek_clamps_to_duration(self):
        scene = self._scene()  # duration=100.0
        with _freeze_time(10.0):
            scene.transport_seek(500.0)
        self.assertEqual(scene.transport.wall_anchor_clock_s, 100.0)

    def test_seek_clamps_negative_to_zero(self):
        scene = self._scene()
        with _freeze_time(10.0):
            scene.transport_seek(-20.0)
        self.assertEqual(scene.transport.wall_anchor_clock_s, 0.0)

    def test_toggle_pause_first_call_touches_and_pauses(self):
        scene = self._scene()
        with _freeze_time(10.0):
            scene.transport_toggle_pause()
        self.assertTrue(scene.transport.paused)
        with _freeze_time(11.0):
            scene.transport_toggle_pause()
        self.assertFalse(scene.transport.paused)


class _FakeSceneAudio:
    """Duck-types the AudioStreamer/UltimateAudioSampler slice VideoScene's
    resync path calls: a scriptable position_seconds() and a flush() that
    records its silence_output argument (and ordering when given an events
    list)."""

    def __init__(self, position: float = 0.0, events: list[tuple[str, object]] | None = None):
        self.sample_rate = 8000
        self._position = position
        self.ring_lead = 0.0
        self.use_reu_pump = False
        self.flush_calls: list[bool] = []
        self._events = events

    def position_seconds(self) -> float:
        return self._position

    def ring_lead_seconds(self) -> float:
        return self.ring_lead

    def cut(self) -> FlushCut:
        if self._events is not None:
            self._events.append(("cut", None))
        return FlushCut(1, self._position + self.ring_lead)

    def flush(self, *, silence_output: bool = False, cut: FlushCut | None = None) -> float:
        self.flush_calls.append(silence_output)
        if self._events is not None:
            self._events.append(("flush", silence_output))
        return (cut or self.cut()).anchor_s


class _FakeSamplerAudio(_FakeSceneAudio):
    """A `_FakeSceneAudio` that plays as a sampler whose re-anchors put the
    sound `lag` seconds behind its clock."""

    is_sampler = True

    def __init__(self, position: float = 0.0):
        super().__init__(position=position)
        self.lag = 0.0
        self.lag_read_at: list[float | None] = []

    def reanchor_lag_seconds(self, position: float | None = None) -> float:
        self.lag_read_at.append(position)
        return self.lag

    def flush(self, *, silence_output: bool = False, cut: FlushCut | None = None) -> float:
        # As the sampler's cut-over does: a splice clears the re-anchor lag.
        self.lag = 0.0
        return super().flush(silence_output=silence_output, cut=cut)


class EmitAudioSeekGuardTest(unittest.TestCase):
    """AVFileSource._emit_audio drops audio while a seek is pending (so stale
    pre-seek samples don't reach the consumer past the splice flush)."""

    def _stub(self, sink) -> AVFileSource:
        return _make_emit_audio_stub(sink)

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
        return _make_emit_audio_stub([])

    def test_false_when_none(self):
        self.assertFalse(self._src().seek_pending)

    def test_true_when_set(self):
        src = self._src()
        src._pending_seek = 5.0
        self.assertTrue(src.seek_pending)


def _splice_sinks() -> list[tuple[str, Any]]:
    """A DAC and a gated sampler, each accepting pushes, with no worker or
    writer to take from their queues."""
    dac = AudioStreamer(cast(Any, FakeAPI()), 8000, "NTSC")
    dac.running = True
    smp = UltimateAudioSampler(cast(Any, FakeAPI()), sample_rate=8000)
    smp._running = True
    smp._gate_time = time.monotonic()
    return [("dac", dac), ("sampler", smp)]


class SpliceKeepsPostSeekAudioTest(unittest.TestCase):
    """The demuxer can apply a splice's seek and push the target's first
    audio before the splice's flush runs. That audio is post-splice and plays
    from the anchor; the DAC's flush drained it, and the sampler had tagged
    it with the epoch the flush then retired, so either sink lost the start
    of the target, and a short post-seek pass lost all of it (#620)."""

    def _scene(self, audio: Any) -> tuple[VideoScene, AVFileSource, np.ndarray]:
        """A resync-path scene on a real demux stub feeding ``audio``, whose
        seek request is applied, and the target's first audio pushed, before
        the splice reaches its flush."""
        src = _make_emit_audio_stub([])
        src._audio_push = audio.push_samples
        src._audio_epoch = getattr(audio, "current_flush_epoch", None)
        src.a_stream = object()
        src.duration_s = 100.0
        target_audio = np.full(64, 1000, dtype=np.int16)
        request_seek = src.request_seek

        def seek_applied_before_the_flush(target_s: float, **kw: Any) -> Any:
            out = request_seek(target_s, **kw)
            with src._lock:
                src._pending_seek = None  # the demux thread applied it
            src._emit_audio(target_audio)
            return out

        src.request_seek = seek_applied_before_the_flush  # type: ignore[method-assign]
        scene = _make_video_scene_stub(cast(_StubSource, src))
        scene.audio = audio
        scene.transport.loop_audio = "on"
        scene.transport.touch()
        return scene, src, target_audio

    def test_the_dac_keeps_the_target_audio_pushed_before_its_flush(self):
        dac = AudioStreamer(cast(Any, FakeAPI()), 8000, "NTSC")
        dac.running = True  # accepts pushes; no worker, so nothing drains the queue
        scene, _, target_audio = self._scene(dac)
        scene.transport_seek(42.0)
        self.assertEqual(dac._queued_samples, target_audio.size, "the target's audio was dropped")
        self.assertEqual(dac._pushed_count, target_audio.size)
        epochs = [epoch for epoch, _ in list(dac.q.queue)]
        self.assertEqual(epochs, [dac._flush_epoch], "the target's audio was tagged stale")
        self.assertEqual(scene.transport.audio_anchor_pos, 0.0)

    def test_the_sampler_keeps_the_target_audio_pushed_before_its_flush(self):
        smp = UltimateAudioSampler(cast(Any, FakeAPI()), sample_rate=8000)
        smp._running = True
        smp._gate_time = time.monotonic()
        scene, _, _ = self._scene(smp)
        scene.transport_seek(42.0)
        epochs = [epoch for epoch, _ in list(smp._q.queue)]
        self.assertEqual(epochs, [smp._flush_epoch], "the target's audio was tagged stale")

    def test_the_sink_cuts_under_the_seek_lock_with_the_seek_pending(self):
        # Cut outside the lock, a pre-seek push between the two would carry
        # the new epoch and play after the splice, and post-seek audio pushed
        # before the cut would carry the old one and be dropped.
        src = _make_emit_audio_stub([])
        seen: list[tuple[bool, float | None]] = []
        src.request_seek(
            42.0, on_request=lambda: seen.append((src._lock.locked(), src._pending_seek))
        )
        self.assertEqual(seen, [(True, 42.0)])

    def test_a_cut_that_raises_still_wakes_a_demuxer_parked_at_eof(self):
        # The seek stays pending; unwoken, the parked demuxer never applies
        # it, and the picture holds on the buffer the request cleared.
        src = _make_emit_audio_stub([])
        src._pending_seek = None
        parked, woke = threading.Event(), threading.Event()

        def park() -> None:
            with src._lock:
                parked.set()
                while src._pending_seek is None:
                    if not src._wake.wait(timeout=2.0):
                        return
                woke.set()

        demux = threading.Thread(target=park)
        demux.start()
        self.addCleanup(demux.join)
        self.assertTrue(parked.wait(2.0))

        def cut() -> None:
            raise RuntimeError("link down")

        with self.assertRaises(RuntimeError):
            src.request_seek(42.0, on_request=cut)
        demux.join(3.0)
        self.assertTrue(woke.is_set(), "the parked demuxer was not woken")

    def test_the_audio_epoch_is_read_under_the_seek_lock(self):
        # Read outside it, a splice landing between the pending-seek check
        # and the read tags pre-seek audio with the post-splice epoch.
        held: list[bool] = []
        sink: list[np.ndarray] = []
        src = _make_emit_audio_stub(sink)
        src._audio_push = lambda arr, epoch=None: sink.append(arr)
        src._audio_epoch = lambda: held.append(src._lock.locked()) or 0
        src._emit_audio(np.array([1, 2, 3], dtype=np.int16))
        self.assertEqual((held, len(sink)), ([True], 1))

    def test_audio_decided_on_before_the_seek_and_pushed_after_it_is_dropped(self):
        # The push runs outside the lock: a seek requested while it is on
        # its way finds it carrying the epoch the seek's cut retires.
        for name, sink in _splice_sinks():
            with self.subTest(sink=name):
                src = _make_emit_audio_stub([])
                src._audio_epoch = sink.current_flush_epoch

                def push(
                    arr: np.ndarray,
                    *,
                    epoch: int | None = None,
                    sink: Any = sink,
                    src: AVFileSource = src,
                ) -> int:
                    src.request_seek(42.0, on_request=sink.cut)
                    return sink.push_samples(arr, epoch=epoch)

                src._audio_push = push
                src._emit_audio(np.full(64, 1000, dtype=np.int16))
                queued = sink._queued_samples if name == "dac" else sink._q.qsize()
                self.assertEqual(queued, 0, "pre-seek audio was kept past the cut")


def _aligned_stub(sink: list[np.ndarray], *, rate: int = 8000) -> AVFileSource:
    """An emit stub with the audio-timeline state `_align_audio_frame` and
    `_fill_dry_stretch` read, on a content timeline that starts at 0."""
    src = _make_emit_audio_stub(sink)
    src.target_sr = rate
    src._pts_offset = 0.0
    src._pts_anchor_target = 0.0
    src._audio_fed_s = None
    src._video_read_s = None
    src._audio_lag_s = 0.0
    src._audio_shift_s = 0.0
    src._audio_jump_warned = False
    src._dry_stall_level, src._dry_window = 0, None
    src._frames_taken, src._clock_read = 0, None
    src.video_fps = 30.0
    src._resampler = object()  # only tested for None: an audio stream is open
    return src


def _audio_frame(start_s: float | None, seconds: float, rate: int = 8000) -> Any:
    return SimpleNamespace(
        pts=None if start_s is None else round(start_s * rate),
        time_base=Fraction(1, rate),
        sample_rate=rate,
        samples=round(seconds * rate),
    )


class AlignedAudioTest(unittest.TestCase):
    """The demuxer feeds audio on the picture's timeline (#606). The sink's
    clock counts samples played and the picture follows it, so audio fed
    back to back put a sound that starts late, or comes back after a gap,
    up to a whole video buffer ahead of its picture, and a stretch with no
    audio stopped the clock with the demuxer waiting on a full buffer."""

    RATE = 8000

    def _fed(self, sink: list[np.ndarray]) -> tuple[int, bool]:
        """Samples fed, and whether every one was silence."""
        return sum(a.size for a in sink), all(not a.any() for a in sink)

    def test_a_sound_that_starts_late_is_preceded_by_silence_up_to_its_start(self):
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._align_audio_frame(_audio_frame(2.0, 0.5))
        self.assertEqual(self._fed(sink), (2 * self.RATE, True))
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 2.5)

    def test_a_frame_within_the_tolerance_follows_on(self):
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._audio_fed_s = 1.0
        src._align_audio_frame(_audio_frame(1.02, 0.5))
        self.assertEqual(sink, [])
        # The 20 ms it starts late was not fed, so the audio fed ends 0.5 s
        # on, and the next frame's gap carries those 20 ms.
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 1.5)

    def test_gaps_too_small_to_fill_one_by_one_are_filled_once_they_add_up(self):
        # Every other 1024-sample frame missing: a 21.3 ms hole each time,
        # under the tolerance. Each hole was forgotten once the frame after
        # it was placed, and the sound ran ahead of its picture without
        # bound: 2.1 s after 4.3 s of content.
        rate = 48000
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink, rate=rate)
        seconds = 1024 / rate
        sound = 0
        for i in range(0, 200, 2):
            src._align_audio_frame(_audio_frame(i * seconds, seconds, rate))
            sound += 1024
        fed = sound + sum(a.size for a in sink)
        self.assertAlmostEqual(cast(float, src._audio_fed_s), fed / rate, delta=1e-6)
        # The last frame starts where all that was fed puts it, to within
        # the tolerance.
        self.assertAlmostEqual((fed - 1024) / rate, 198 * seconds, delta=0.03)

    def test_overlaps_too_small_to_trim_one_by_one_are_trimmed_once_they_add_up(self):
        # Each frame's timestamp 10 ms short of the one before's end: the
        # sound fell behind its picture by 10 ms a frame.
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        trimmed = 0
        for i in range(100):
            src._align_audio_frame(_audio_frame(i * 0.09, 0.1))
            trimmed += src._audio_trim
            src._audio_trim = 0
        self.assertEqual(sink, [])
        played = 100 * self.RATE // 10 - trimmed
        self.assertAlmostEqual(played / self.RATE, 99 * 0.09 + 0.1, delta=0.03)
        self.assertAlmostEqual(cast(float, src._audio_fed_s), played / self.RATE, delta=1e-6)

    def test_a_frame_with_no_timestamp_follows_on(self):
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._audio_fed_s = 3.0
        src._align_audio_frame(_audio_frame(None, 0.5))
        self.assertEqual((sink, src._audio_trim), ([], 0))
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 3.5)

    def test_the_part_of_a_frame_the_audio_fed_already_covers_is_trimmed(self):
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._audio_fed_s = 2.0
        src._align_audio_frame(_audio_frame(1.5, 1.0))
        self.assertEqual(src._audio_trim, self.RATE // 2)
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 2.5)
        pcm = np.arange(1, self.RATE + 1, dtype=np.int16)
        src._emit_resampled(SimpleNamespace(to_ndarray=lambda: pcm.reshape(1, -1)))
        self.assertEqual(len(sink), 1)
        np.testing.assert_array_equal(sink[0], pcm[self.RATE // 2 :])
        self.assertEqual(src._audio_trim, 0)

    def test_a_sound_that_starts_after_its_picture_keeps_that_distance(self):
        # The picture's first timestamp sets the origin both streams share;
        # an origin per stream pulled the sound to the front.
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._pts_offset = None
        self.assertEqual(src._content_time(5.0), 0.0)
        src._align_audio_frame(_audio_frame(7.0, 0.5))
        self.assertEqual(self._fed(sink), (2 * self.RATE, True))

    def test_a_sound_read_before_its_picture_sets_the_shared_origin(self):
        # Left to the picture, the origin put a sound read first that far
        # behind silence.
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._pts_offset = None
        src._align_audio_frame(_audio_frame(10.0, 0.5))
        self.assertEqual(sink, [])
        self.assertEqual(src._content_time(12.0), 2.0)

    def test_a_full_buffer_with_no_audio_is_filled_short_of_its_newest_frame(self):
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._fill_dry_stretch(0.0, 8.0)
        self.assertEqual(self._fed(sink), (round(7.5 * self.RATE), True))
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 7.5)

    def test_the_fill_keeps_back_as_far_as_the_file_writes_audio_late(self):
        # Audio the file has been seen to write 2 s behind its picture may
        # still come for that stretch; filled, it would be trimmed away.
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._audio_lag_s = 2.0
        src._fill_dry_stretch(0.0, 8.0)
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 5.97)

    def test_a_stalled_picture_takes_the_sinks_lead_past_its_oldest_frame(self):
        # In a buffer spanning less than the sink holds back, a fill short of
        # the newest frame never brings the clock to the oldest. The first
        # level stays within the frames read; each one past it, the sink
        # holding back more than the buffer spans, goes a step past the newest.
        step = DRY_FILL_PAST_NEWEST_STEP_S
        for level, expected in ((0, 0.5), (1, 1.0), (2, 1.0 + step), (3, 1.0 + 2 * step)):
            with self.subTest(level=level):
                sink: list[np.ndarray] = []
                src = _aligned_stub(sink)
                src._dry_stall_level = level
                src._fill_dry_stretch(0.0, 1.0)
                self.assertAlmostEqual(cast(float, src._audio_fed_s), expected)

    def test_the_stall_lead_stops_growing_at_its_ceiling(self):
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._dry_stall_level = 1000
        src._fill_dry_stretch(0.0, 1.0)
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 1.0 + DRY_FILL_MAX_PAST_NEWEST_S)

    def test_a_held_picture_stops_raising_the_stall_at_its_ceiling(self):
        at_ceiling = 1 + round(DRY_FILL_MAX_PAST_NEWEST_S / DRY_FILL_PAST_NEWEST_STEP_S)
        src = _aligned_stub([])
        src._dry_stall_level, src._dry_window = at_ceiling, (0.0, 0, 0.0)
        src._watch_dry_pace(2.0, 1.0, 0, (1.5, 0.0))
        self.assertEqual(src._dry_stall_level, at_ceiling)

    def test_no_fill_while_the_audio_fed_reaches_the_target(self):
        # Within the tolerance short of the target, and past it: the fill
        # feeds nothing and leaves where the audio fed ends alone.
        for fed in (7.49, 9.0):
            with self.subTest(fed=fed):
                sink: list[np.ndarray] = []
                src = _aligned_stub(sink)
                src._audio_fed_s = fed
                src._fill_dry_stretch(0.0, 8.0)
                self.assertEqual((sink, src._audio_fed_s), ([], fed))

    def test_no_fill_without_an_audio_stream(self):
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._resampler = None
        src._fill_dry_stretch(0.0, 8.0)
        self.assertEqual((sink, src._audio_fed_s), ([], None))

    def _enqueue_blocked(
        self, *, take: int, clock: float | None = 0.0, stamps: tuple[float, float] = (0.0, 1.0)
    ) -> AVFileSource:
        """Block `_enqueue_frame` on a full buffer of two frames at `stamps`
        for about 3 s of a clock that steps 0.3 s a reading, the consumer
        taking `take` frames a reading and reading the clock at `clock` (None:
        not reading it at all) at 30 fps. Real time is 9 frames a reading."""
        src = _aligned_stub([])
        img = np.zeros((2, 2, 3), dtype=np.uint8)
        src.max_video_buffer = 2
        src._video_buf = [(stamps[0], img), (stamps[1], img)]
        calls = [0]

        def fill(_oldest: float, _newest: float) -> None:
            calls[0] += 1
            src._frames_taken += take
            if clock is not None:
                src._clock_read = (1e9, clock)
            if calls[0] >= 10:
                src._closed = True

        src._fill_dry_stretch = fill  # type: ignore[method-assign]
        with mock.patch.object(video_mod, "time", FrozenClock(0.0, "monotonic", 0.3, sleep=None)):
            self.assertFalse(src._enqueue_frame(2.0, img))
        return src

    def test_a_picture_held_on_one_frame_raises_the_stall_each_second(self):
        # About 3 s held: a step each DRY_FILL_STALL_S, so a lead too short
        # for the sink grows until it is not.
        self.assertEqual(self._enqueue_blocked(take=0)._dry_stall_level, 2)

    def test_a_picture_crawling_through_its_buffer_takes_the_lead_within_it(self):
        # A fill that keeps the clock just short of what the sink holds back
        # drains a frame now and then: no one frame waits long, and the
        # picture plays at a fraction of its speed. The clock moves, so the
        # lead within the frames read is enough, and going past the newest
        # would trim a returning sound for nothing.
        self.assertEqual(self._enqueue_blocked(take=1)._dry_stall_level, 1)

    def test_a_picture_playing_in_real_time_does_not_raise_the_stall(self):
        self.assertEqual(self._enqueue_blocked(take=9)._dry_stall_level, 0)

    def test_pace_is_counted_in_frames_not_read_off_the_stamps(self):
        # Tempo compensation scales the stamps (0.5 is a valid setting) and a
        # file can step them back; the frames the consumer takes are the
        # pace either way. 80 % of real time is not a stall.
        src = self._enqueue_blocked(take=7)
        self.assertEqual(src._dry_stall_level, 0)

    def test_a_consumer_that_stops_reading_the_clock_does_not_raise_the_stall(self):
        # A stalled render takes no frames, but the sink's clock is not what
        # held it: a lead taken for it would only trim the next sound.
        self.assertEqual(self._enqueue_blocked(take=0, clock=None)._dry_stall_level, 0)

    def test_a_clock_past_the_next_frame_does_not_raise_the_stall(self):
        # The sink's clock has reached the next frame: the picture will move
        # as soon as the consumer takes it.
        self.assertEqual(self._enqueue_blocked(take=0, clock=1.0)._dry_stall_level, 0)

    def test_the_consumer_counts_the_frames_it_takes_and_its_clock_reads(self):
        # What `_watch_dry_pace` judges by: the frames taken off the buffer,
        # and where the clock was when last read, even short of any frame.
        img = np.zeros((2, 2, 3), dtype=np.uint8)
        for eof, taken in ((False, 2), (True, 3)):
            with self.subTest(eof=eof):
                src = _make_av_source_stub([(0.0, img), (1.0, img), (2.0, img)], eof=eof)
                with mock.patch.object(video_mod, "time", FrozenClock(7.0, "monotonic")):
                    src.current_frame(2.5)
                self.assertEqual((src._frames_taken, src._clock_read), (taken, (7.0, 2.5)))
        src = _make_av_source_stub([(1.0, img)], eof=False)
        with mock.patch.object(video_mod, "time", FrozenClock(7.0, "monotonic")):
            self.assertIsNone(src.current_frame(0.5))
        self.assertEqual((src._frames_taken, src._clock_read), (0, (7.0, 0.5)))

    def test_a_clock_held_short_of_an_oldest_frame_stamped_after_the_next_raises_the_stall(self):
        # A file whose stamps step back: the oldest frame (5.0) is still due
        # and the clock is held short of it, past the next one's stamp (4.0).
        src = self._enqueue_blocked(take=0, clock=4.5, stamps=(5.0, 4.0))
        self.assertEqual(src._dry_stall_level, 2)

    def test_a_clock_moving_short_of_the_next_frame_does_not_go_past_the_newest(self):
        # A low frame rate, or a sparse stretch of a variable one, takes no
        # frame in a window while its clock runs: not a held picture, and a
        # step past the newest would trim the next sound for nothing.
        src = _aligned_stub([])
        src._dry_stall_level, src._dry_window = 1, (0.0, 0, 0.2)
        src._watch_dry_pace(1.5, 2.0, 0, (1.4, 1.6))
        self.assertEqual((src._dry_stall_level, src._dry_window), (1, (1.5, 0, 1.6)))

    def test_a_clock_read_before_the_window_does_not_raise_the_stall(self):
        src = _aligned_stub([])
        src._dry_window = (1.0, 0, 0.0)
        src._watch_dry_pace(2.0, 1.0, 0, (0.5, 0.0))
        self.assertEqual((src._dry_stall_level, src._dry_window), (0, (2.0, 0, 0.0)))

    def test_no_stall_is_judged_without_an_audio_sink(self):
        # Nothing reads the level then: a paused REU-pump scene, or a file
        # with no audio stream.
        src = _aligned_stub([])
        src._audio_push = None
        src._watch_dry_pace(0.0, 1.0, 0, (0.0, 0.0))
        src._watch_dry_pace(5.0, 1.0, 0, (4.0, 0.0))
        self.assertEqual((src._dry_stall_level, src._dry_window), (0, None))

    def test_audio_coming_again_releases_the_stall(self):
        src = _aligned_stub([])
        src._dry_stall_level, src._dry_window = 2, (5.0, 1, 0.0)
        src._align_audio_frame(_audio_frame(0.0, 0.1))
        self.assertEqual((src._dry_stall_level, src._dry_window), (0, None))

    def test_a_seek_starts_the_audio_timeline_over(self):
        # Kept, the fed position of the old pass put the target's first audio
        # behind it: trimmed away on a seek back, or behind silence on one
        # forward.
        src = _make_demux_source_stub([], pending_seek=3.0)
        src._audio_fed_s, src._audio_trim = 40.0, 123
        src._video_read_s, src._dry_stall_level, src._dry_window = 41.0, 2, (5.0, 1, 0.0)
        src._audio_shift_s, src._audio_jump_warned = 1e6, True
        self.assertTrue(src._apply_pending_seek())
        self.assertEqual(
            (
                src._audio_fed_s,
                src._audio_trim,
                src._video_read_s,
                src._dry_stall_level,
                src._dry_window,
            ),
            (None, 0, None, 0, None),
        )
        # A jump the old pass followed on from is no part of the new pass's
        # timeline, and the new pass warns of its own.
        self.assertEqual((src._audio_shift_s, src._audio_jump_warned), (0.0, False))

    def _jumped(
        self, frames: list[tuple[float, float]], at: float
    ) -> tuple[AVFileSource, list[np.ndarray], int]:
        """Align ``frames`` (start, length) after audio and picture both
        reached ``at``. Return the source, what it fed, and how many
        warnings it logged."""
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._audio_fed_s = src._video_read_s = at
        with self.assertLogs("c64cast.video.video", level="WARNING") as logs:
            for start, length in frames:
                src._align_audio_frame(_audio_frame(start, length))
        return src, sink, len(logs.records)

    def test_an_audio_timestamp_far_past_the_picture_follows_on(self):
        # Fed as silence up to its stamp, one packet at 1e6 s held the scene
        # for that long at the sink's pace.
        src, sink, warned = self._jumped([(1e6, 0.1)], 2.0)
        self.assertEqual((sink, warned), ([], 1))
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 2.1)
        self.assertEqual(src._audio_lag_s, 0.0)

    def test_a_walk_of_jumps_each_under_the_bound_feeds_no_more_than_the_bound(self):
        # Each packet 9.9 s past the last: a bound measured from the audio
        # fed is never tripped, and the silence adds up without end.
        walk = [(2.0 + 9.9 * k, 0.01) for k in range(1, 200)]
        _, sink, warned = self._jumped(walk, 2.0)
        self.assertLessEqual(sum(a.size for a in sink), AUDIO_DISCONTINUITY_S * self.RATE)
        self.assertEqual(warned, 1, "warned more than once a pass")

    def test_an_audio_timestamp_far_behind_the_audio_fed_follows_on(self):
        # Trimmed against the audio fed, every frame after it was cut whole
        # and the rest of the pass was mute.
        src, sink, warned = self._jumped([(10.0, 0.5), (10.5, 0.5)], 100.0)
        self.assertEqual((sink, src._audio_trim, warned), ([], 0, 1))
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 101.0)
        self.assertEqual(src._audio_lag_s, 0.0)

    def test_a_sound_the_picture_has_been_read_up_to_still_waits_for_it(self):
        # A sound that starts long after its picture is no discontinuity:
        # the picture is read up to it first.
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink)
        src._video_read_s = 40.0
        src._align_audio_frame(_audio_frame(40.0, 0.5))
        self.assertEqual(self._fed(sink), (40 * self.RATE, True))
        self.assertEqual(src._audio_shift_s, 0.0)


class AlignedAudioBranchesTest(unittest.TestCase):
    """The branches of the aligned-audio path that `AlignedAudioTest` leaves
    to the plain case: tempo compensation, a trim longer than one frame, a
    stop in the middle of a gap, a frame that names no rate, and the buffer's
    scaled timestamps."""

    RATE = 8000

    def _graph_stub(self, sink: list[np.ndarray], tempo_scale: float = 0.5) -> AVFileSource:
        src = _aligned_stub(sink, rate=self.RATE)
        src._tempo_scale = tempo_scale
        src._atempo_graph = _build_atempo_graph(self.RATE, tempo_scale)
        return src

    def test_silence_goes_through_the_atempo_graph_under_tempo_compensation(self):
        # Fed raw, the gap would play at twice the length the compensated
        # picture gives it, and the sound would fall behind its picture.
        sink: list[np.ndarray] = []
        src = self._graph_stub(sink)
        src._feed_silence(10 * self.RATE)
        src._flush_atempo()
        fed = sum(a.size for a in sink)
        self.assertAlmostEqual(fed / (10 * self.RATE), 0.5, delta=0.03)
        self.assertTrue(all(not a.any() for a in sink))

    def test_a_trimmed_frame_goes_through_the_atempo_graph_under_tempo_compensation(self):
        sink: list[np.ndarray] = []
        src = self._graph_stub(sink)
        # A trim this long moves the output well past the graph's tolerance,
        # so a trim that is counted but not cut fails here too.
        trim = 4 * self.RATE
        src._audio_trim = trim
        pcm = np.full(10 * self.RATE, 700, dtype=np.int16)
        src._emit_resampled(SimpleNamespace(to_ndarray=lambda: pcm.reshape(1, -1)))
        src._flush_atempo()
        fed = sum(a.size for a in sink)
        self.assertAlmostEqual(fed, 0.5 * (pcm.size - trim), delta=800)
        self.assertEqual(src._audio_trim, 0)

    def test_a_trim_longer_than_one_resampled_frame_carries_to_the_next(self):
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink, rate=self.RATE)
        src._audio_trim = 1500
        pcm = np.arange(1, 3001, dtype=np.int16)
        for i in range(3):
            piece = pcm[i * 1000 : (i + 1) * 1000]
            src._emit_resampled(SimpleNamespace(to_ndarray=lambda p=piece: p.reshape(1, -1)))
        np.testing.assert_array_equal(np.concatenate(sink), pcm[1500:])
        self.assertEqual(src._audio_trim, 0)

    def test_a_frame_wholly_behind_the_fed_audio_loses_only_its_own_length(self):
        # Timestamps 3 s back must not trim 3 s of the frames after it, nor
        # pull the fed position back with them.
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink, rate=self.RATE)
        src._audio_fed_s = 4.0
        src._align_audio_frame(_audio_frame(1.0, 0.5, self.RATE))
        self.assertEqual(src._audio_trim, self.RATE // 2)
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 4.0)

    def test_a_gap_is_fed_in_pieces_no_larger_than_the_piece_size(self):
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink, rate=self.RATE)
        src._feed_silence(2 * SILENCE_PIECE_SAMPLES + 100)
        self.assertEqual([a.size for a in sink], [SILENCE_PIECE_SAMPLES] * 2 + [100])

    def test_silence_stops_between_pieces_on_a_close_or_a_pending_seek(self):
        for name in ("close", "seek"):
            with self.subTest(stop=name):
                src = _aligned_stub([], rate=self.RATE)
                pieces: list[int] = []

                def emit(
                    arr: np.ndarray,
                    src: AVFileSource = src,
                    name: str = name,
                    pieces: list[int] = pieces,
                ) -> None:
                    pieces.append(arr.size)
                    if name == "close":
                        src._closed = True
                    else:
                        src._pending_seek = 5.0

                src._emit_audio = emit  # type: ignore[method-assign]
                src._feed_silence(5 * SILENCE_PIECE_SAMPLES)
                self.assertEqual(pieces, [SILENCE_PIECE_SAMPLES])

    def test_a_frame_that_names_no_rate_is_taken_at_the_target_rate(self):
        for rate in (None, 0):
            with self.subTest(sample_rate=rate):
                sink: list[np.ndarray] = []
                src = _aligned_stub(sink, rate=self.RATE)
                frame = SimpleNamespace(
                    pts=2 * self.RATE,
                    time_base=Fraction(1, self.RATE),
                    sample_rate=rate,
                    samples=self.RATE // 2,
                )
                src._align_audio_frame(frame)
                self.assertEqual(sum(a.size for a in sink), 2 * self.RATE)
                self.assertAlmostEqual(cast(float, src._audio_fed_s), 2.5)

    def test_the_fill_reads_the_buffer_s_scaled_timestamps_as_content_time(self):
        # The buffer holds picture timestamps already scaled by tempo_scale:
        # 4.0 in it is 8.0 s of the file at 0.5.
        sink: list[np.ndarray] = []
        src = _aligned_stub(sink, rate=self.RATE)
        src._tempo_scale = 0.5
        src._fill_dry_stretch(0.0, 4.0)
        self.assertAlmostEqual(cast(float, src._audio_fed_s), 7.5)
        self.assertEqual(sum(a.size for a in sink), round(7.5 * self.RATE))

    def test_the_picture_read_position_is_kept_in_content_time(self):
        src = _aligned_stub([], rate=self.RATE)
        src._tempo_scale = 0.5
        src.max_video_buffer = 4
        self.assertTrue(src._enqueue_frame(4.0, np.zeros((2, 2, 3), dtype=np.uint8)))
        self.assertAlmostEqual(cast(float, src._video_read_s), 8.0)
        # A packet the file wrote 2 s of content behind that picture.
        src._align_audio_frame(_audio_frame(6.0, 0.1, self.RATE))
        self.assertAlmostEqual(src._audio_lag_s, 2.0)

    def test_the_stall_lead_exceeds_what_each_sink_holds_back(self):
        # A retune of either sink that grows its hold-back past the first
        # step of the stall lead costs every stall at its default settings
        # another second before the picture moves. The DAC holds back its
        # prebuffer and a ring lead the servo steers toward its target gap;
        # 8 kHz is the slowest rate it has shipped at (12 kHz is the default).
        dac_s = (PREBUFFER_CHUNKS * CHUNK_SIZE + HOST_DMA_SERVO_TARGET_GAP) / 8000
        self.assertGreater(DRY_FILL_MIN_LEAD_S, dac_s)
        self.assertGreater(DRY_FILL_MIN_LEAD_S, DEFAULT_LEAD_SECONDS)


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
        scene.tempo_scale = tempo_scale
        scene.transport.loop_audio = "on"
        return scene, source, audio

    def test_touch_resolves_resync_and_does_not_mute(self):
        scene, source, _ = self._resync_scene(position=7.0)
        scene.transport.touch()
        self.assertTrue(scene.transport.resync)
        self.assertEqual(source.muted_calls, [])  # NOT muted
        self.assertEqual(scene.transport.audio_anchor_pos, 7.0)

    def test_touch_with_mute_setting_is_verbatim_phase2(self):
        scene, source, _ = self._resync_scene(position=7.0)
        scene.transport.loop_audio = "mute"
        with _freeze_time(10.0):
            scene.transport.touch()
        self.assertFalse(scene.transport.resync)
        self.assertEqual(source.muted_calls, [True])

    def test_on_without_audio_falls_back_to_mute(self):
        scene = _make_video_scene_stub(_StubSource(duration=100.0))  # audio=None
        scene.transport.loop_audio = "on"
        with _freeze_time(10.0):
            scene.transport.touch()
        self.assertFalse(scene.transport.resync)
        self.assertEqual(scene.source.muted_calls, [True])  # type: ignore[union-attr]

    def test_on_without_audio_stream_falls_back_to_mute(self):
        # audio present, but the source carries no audio stream (a_stream None).
        source = _StubSource(duration=100.0, a_stream=None)
        scene = _make_video_scene_stub(source)
        scene.audio = _FakeSceneAudio()  # type: ignore[assignment]
        scene.transport.loop_audio = "on"
        with _freeze_time(10.0):
            scene.transport.touch()
        self.assertFalse(scene.transport.resync)
        self.assertEqual(source.muted_calls, [True])

    def test_seek_splices(self):
        scene, source, audio = self._resync_scene(position=3.0)
        scene.transport_seek(42.0)
        self.assertEqual(source.seeks, [42.0])  # request_seek fired
        self.assertEqual(audio.flush_calls, [False])  # plain flush (not silence)
        self.assertAlmostEqual(scene.transport.audio_anchor_clock_s, 42.0)  # tempo 1.0

    def test_seek_waits_out_the_ring_lead(self):
        # The flush keeps the ring's unplayed lead, so the target is heard that
        # much later: the clock sits that far below the target until it is.
        scene, _, audio = self._resync_scene(position=3.0)
        audio.ring_lead = 0.34
        scene.transport.touch()
        scene.transport_seek(42.0)
        self.assertAlmostEqual(scene.transport.clock_s(), 42.0 - 0.34)
        audio._position = 3.34
        self.assertAlmostEqual(scene.transport.clock_s(), 42.0)

    def test_position_reports_the_seek_target_while_the_ring_lead_plays_out(self):
        # A held FF seeks to position() + delta every tick; a position that
        # read one ring lead below the last target would scrub backward.
        scene, _, audio = self._resync_scene(position=3.0)
        audio.ring_lead = 0.34
        scene.transport.touch()
        scene.transport_seek(42.0)
        self.assertAlmostEqual(scene.transport_position(), 42.0)
        scene.transport_seek(scene.transport_position() + 0.02)
        self.assertAlmostEqual(scene.transport_position(), 42.02)
        audio._position = 3.34 + 1.0
        self.assertAlmostEqual(scene.transport_position(), 43.02)

    def test_pause_inside_the_ring_lead_freezes_at_the_seek_target(self):
        scene, source, audio = self._resync_scene(position=3.0)
        audio.ring_lead = 0.34
        scene.transport.touch()
        scene.transport_seek(42.0)
        audio._position = 3.1
        scene.transport_pause()
        self.assertAlmostEqual(scene.transport_position(), 42.0)
        scene.transport_resume()
        self.assertEqual(source.seeks[-1], 42.0)

    def test_untouched_clock_follows_a_sampler_s_reanchored_sound(self):
        # A sampler that re-anchored late audio plays it that far behind its
        # clock; read raw, the picture ran ahead of the sound by that much.
        scene = _make_video_scene_stub(_StubSource(duration=100.0))
        audio = _FakeSamplerAudio(position=10.0)
        audio.lag = 0.25
        scene.audio = audio  # type: ignore[assignment]
        self.assertAlmostEqual(scene.transport.clock_s(), 9.75)
        self.assertEqual(audio.lag_read_at, [10.0])  # taken at that position's head

    def test_resync_clock_follows_a_sampler_s_reanchored_sound(self):
        scene, source, _ = self._resync_scene(position=4.0)
        audio = _FakeSamplerAudio(position=4.0)
        scene.audio = audio  # type: ignore[assignment]
        audio.lag = 0.1
        scene.transport.touch()  # anchor_clock = heard 3.9, anchor_pos = heard 3.9
        self.assertAlmostEqual(scene.transport.clock_s(), 3.9)
        audio._position = 9.0
        audio.lag = 0.6  # a re-anchor since the touch
        self.assertAlmostEqual(scene.transport.clock_s(), 8.4)

    def test_a_splice_anchors_on_the_sampler_s_raw_clock(self):
        # The splice's flush clears the lag, so the heard position is the raw
        # clock from then on; anchored on the heard position from before the
        # flush, the picture reached the target that lag ahead of its sound.
        scene, _, _ = self._resync_scene(position=10.0)
        audio = _FakeSamplerAudio(position=10.0)
        scene.audio = audio  # type: ignore[assignment]
        audio.ring_lead = 0.15
        audio.lag = 0.3
        scene.transport.touch()
        scene.transport_seek(42.0)
        self.assertEqual(audio.lag, 0.0)  # the fake's flush ran
        self.assertAlmostEqual(scene.transport.clock_s(), 42.0 - 0.15)
        audio._position = 10.15  # the target's first sample is heard
        self.assertAlmostEqual(scene.transport.clock_s(), 42.0)

    def test_clock_tracks_audio_delta_not_wall(self):
        scene, _, audio = self._resync_scene(position=0.0)
        scene.transport.touch()  # anchor_clock=0, anchor_pos=0
        audio._position = 5.0
        # Wall time is irrelevant on the resync path — only the audio delta.
        with _freeze_time(999.0):
            self.assertAlmostEqual(scene.transport.clock_s(), 5.0)

    def test_pause_freezes_and_silences(self):
        scene, source, audio = self._resync_scene(position=10.0)
        scene.transport.touch()
        scene.transport_pause()
        self.assertTrue(scene.transport.paused)
        self.assertEqual(audio.flush_calls, [True])  # silence_output
        self.assertIn(True, source.muted_calls)
        # Clock frozen at the paused position regardless of further audio motion.
        audio._position = 99.0
        self.assertAlmostEqual(scene.transport.clock_s(), 10.0)

    def test_resume_requests_the_seek_then_unmutes_then_flushes(self):
        # Order is load-bearing. The seek request first arms the pending-seek
        # guard against pre-seek audio, and the sink's cut is taken with it;
        # the unmute before the flush lets the target's first audio through
        # even when the demuxer decodes it while the flush is still running.
        events: list[tuple[str, object]] = []
        scene, _, _ = self._resync_scene(position=10.0, events=events)
        scene.transport.touch()
        scene.transport_pause()
        events.clear()
        scene.transport_resume()
        self.assertFalse(scene.transport.paused)
        self.assertEqual(
            events, [("seek", 10.0), ("muted", False), ("cut", None), ("flush", False)]
        )

    def test_audio_the_demuxer_decodes_during_the_resume_flush_reaches_the_sink(self):
        # Unmuting only after the flush dropped that audio at the source, so
        # the stream started past its target at the anchor: on hardware the
        # sound ran 50-200 ms ahead of the picture after every resume.
        scene, _, audio = self._resync_scene(position=10.0)
        sink: list[np.ndarray] = []
        scene.transport.touch()  # resolves the resync path on the stub source
        demux = _make_emit_audio_stub(sink)
        demux._video_buf = []
        scene.source = demux  # type: ignore[assignment]
        scene.transport_pause()
        self.assertTrue(demux._muted)

        def flush_while_the_demuxer_seeks(
            *, silence_output: bool = False, cut: FlushCut | None = None
        ) -> float:
            demux._pending_seek = None  # the demux thread applied the seek
            demux._emit_audio(np.array([1, 2, 3], dtype=np.int16))
            return 10.0

        audio.flush = flush_while_the_demuxer_seeks  # type: ignore[method-assign]
        scene.transport_resume()
        self.assertEqual(len(sink), 1, "the seek target's first audio was dropped")

    def test_a_seek_shows_its_target_frame_through_the_ring_lead(self):
        # A held FF re-seeks before each hold ends; frames chosen by the held
        # clock would leave the screen blank until release.
        scene, source, audio = self._resync_scene(position=3.0)
        audio.ring_lead = 0.34
        asked: list[float] = []
        # None skips the render, which the stub scene cannot do.
        source.current_frame = lambda clock_s: asked.append(clock_s)  # type: ignore[method-assign]
        scene.transport.touch()
        scene.transport_seek(42.0)
        scene.process_frame(0.0)
        self.assertAlmostEqual(asked[-1], 42.0)
        audio._position = 3.34 + 1.0
        scene.process_frame(0.0)
        self.assertAlmostEqual(asked[-1], 43.0)

    def test_a_frame_shown_through_the_hold_is_not_counted_as_lag(self):
        # The held clock still reads the pre-splice audio, so measuring the
        # target frame against it logs the hold's length as lag on every seek.
        scene, source, audio = self._resync_scene(position=3.0)
        audio.ring_lead = 0.34
        scene._av_lag_count = 0
        scene.transport.touch()
        scene.transport_seek(42.0)
        source.last_frame_pts = 42.0
        source._frame = np.zeros((200, 320, 3), dtype=np.uint8)
        with (
            mock.patch.object(scenes, "_render_with_overlays"),
            mock.patch.object(scenes, "_crop_to_aspect", side_effect=lambda x: x),
        ):
            scene.process_frame(0.0)
            self.assertEqual(scene._av_lag_count, 0)
            audio._position = 3.34 + 1.0
            source.last_frame_pts = 43.0
            source._frame = np.zeros((200, 320, 3), dtype=np.uint8)
            scene.process_frame(0.0)
        self.assertEqual(scene._av_lag_count, 1)
        self.assertAlmostEqual(scene._av_lag_min, 0.0)

    def test_a_running_clock_outside_a_hold_is_counted_as_lag(self):
        # The sampler's position is a wall clock, so no two reads agree.
        scene, source, audio = self._resync_scene(position=3.0)
        reads = iter(3.0 + 0.001 * i for i in range(100))
        audio.position_seconds = lambda: next(reads)  # type: ignore[method-assign]
        scene._av_lag_count = 0
        source.last_frame_pts = 3.0
        source._frame = np.zeros((200, 320, 3), dtype=np.uint8)
        with (
            mock.patch.object(scenes, "_render_with_overlays"),
            mock.patch.object(scenes, "_crop_to_aspect", side_effect=lambda x: x),
        ):
            scene.process_frame(0.0)
            scene.transport.touch()
            source._frame = np.zeros((200, 320, 3), dtype=np.uint8)
            scene.process_frame(0.0)
        self.assertEqual(scene._av_lag_count, 2)

    def test_a_frame_shown_through_the_hold_is_labeled_with_its_own_time(self):
        scene, source, audio = self._resync_scene(position=3.0)
        audio.ring_lead = 0.34
        scene.show_frame_numbers = True
        scene.transport.touch()
        scene.transport_seek(42.0)
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
        self.assertTrue(labels[0].startswith(timecode(42.0)), labels[0])

    def test_loop_wrap_splices_once_while_seek_pending(self):
        scene, source, _ = self._resync_scene(position=0.0)
        scene.transport.touch()
        scene.transport.loop_a = 0.0
        scene.transport.loop_b = 5.0
        scene.transport.loop_state = "active"
        source.finished = True  # force the wrap path every frame
        scene.process_frame(0.0)
        self.assertEqual(source.seeks, [0.0])  # fired once
        # request_seek set seek_pending; the next frame must NOT re-fire.
        scene.process_frame(0.0)
        self.assertEqual(source.seeks, [0.0])

    def test_tempo_scale_anchor_and_wrap(self):
        # The internal clock is scaled (s × content); the transport surface is
        # content seconds. s=0.88.
        scene, source, audio = self._resync_scene(position=0.0, tempo_scale=0.88)
        scene.transport_seek(100.0)
        self.assertAlmostEqual(scene.transport.audio_anchor_clock_s, 88.0)  # 100 × 0.88
        self.assertAlmostEqual(scene.transport_position(), 100.0)  # back to content
        # Loop B stored in content seconds (10) wraps when clock ≥ 8.8.
        scene.transport.loop_a = 0.0
        scene.transport.loop_b = 10.0
        scene.transport.loop_state = "active"
        scene.transport.audio_anchor = (0.0, 0.0)
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
        scene.transport.touch()
        scene.transport.audio_anchor = (88.0, 0.0)  # content 100 at s=0.88
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
        self.assertTrue(labels[0].startswith(timecode(100.0)), labels[0])

    def test_mute_path_wrap_compare_unscaled(self):
        # Guard: on the mute path tempo_scale must NOT scale the loop-B compare.
        scene = _make_video_scene_stub(_StubSource(duration=100.0))  # audio=None
        scene.transport.loop_audio = "mute"
        scene.tempo_scale = 0.88
        with _freeze_time(10.0):
            scene.transport.touch()
        scene.transport.loop_a = 0.0
        scene.transport.loop_b = 10.0
        scene.transport.loop_state = "active"
        scene.transport.wall_anchor_clock_s = 9.0  # unscaled clock 9.0
        scene.transport.wall_anchor_time = 10.0
        scene.source.finished = False  # type: ignore[union-attr]
        scene.source._frame = None  # type: ignore[union-attr]  # pre-roll → no render path
        with _freeze_time(10.0):
            # content compare: 9.0 < 10.0 → NO wrap. A wrongly scaled threshold
            # (8.8) would wrap here.
            scene.process_frame(0.0)
        self.assertEqual(scene.source.seeks, [])  # type: ignore[union-attr]


class VideoSceneIdentitySkipTest(unittest.TestCase):
    """process_frame skips a render when the source hands back the same frame
    object (a pause, or polling faster than the video's frame rate), except
    when a write may have been lost since the last render (c64cast#531)."""

    def _renders(self, scene: VideoScene, n: int) -> int:
        with (
            mock.patch.object(scenes, "_render_with_overlays") as render,
            mock.patch.object(scenes, "_crop_to_aspect", side_effect=lambda x: x),
        ):
            for _ in range(n):
                scene.process_frame(0.0)
        return render.call_count

    def test_a_held_frame_renders_once(self):
        scene = _make_video_scene_stub(_StubSource())
        api = cast(mock.MagicMock, scene.api)
        api.delivery_epoch = 0
        self.assertEqual(self._renders(scene, 3), 1)

    def test_a_lost_write_repaints_a_held_frame_once(self):
        scene = _make_video_scene_stub(_StubSource())
        api = cast(mock.MagicMock, scene.api)
        api.delivery_epoch = 0
        self._renders(scene, 1)
        api.delivery_epoch = 1
        self.assertEqual(self._renders(scene, 3), 1)

    def test_a_frame_the_dead_link_dropped_is_rendered_again(self):
        # A held frame whose push raised never reached the machine, so the
        # identity skip must not count it as shown (c64cast#583).
        from c64cast.hw.socket_dma import SocketDMAError

        scene = _make_video_scene_stub(_StubSource())
        api = cast(mock.MagicMock, scene.api)
        api.delivery_epoch = 0
        with (
            mock.patch.object(scenes, "_render_with_overlays", side_effect=SocketDMAError("down")),
            mock.patch.object(scenes, "_crop_to_aspect", side_effect=lambda x: x),
            self.assertRaises(SocketDMAError),
        ):
            scene.process_frame(0.0)
        self.assertEqual(self._renders(scene, 3), 1)


class VideoSceneLoopToggleTest(unittest.TestCase):
    """transport_loop_toggle's 3-state cycle (mark A -> mark B + active ->
    clear), and the red-border feedback it shares with the Record/Stop pair
    (MIDI live-tune Phase 3) since both drive the same _loop_a/_loop_b/
    _loop_state machine."""

    def _scene(self) -> VideoScene:
        return _make_video_scene_stub(_StubSource(duration=100.0))

    def test_three_state_cycle(self):
        scene = self._scene()
        with _freeze_time(5.0):
            scene.transport_loop_toggle()
        self.assertEqual(scene.transport.loop_state, "armed")
        self.assertEqual(scene.transport.loop_a, 5.0)
        self.assertIsNone(scene.transport.loop_b)

        with _freeze_time(8.0):
            scene.transport_loop_toggle()
        self.assertEqual(scene.transport.loop_state, "active")
        self.assertEqual(scene.transport.loop_a, 5.0)
        self.assertEqual(scene.transport.loop_b, 8.0)

        with _freeze_time(9.0):
            scene.transport_loop_toggle()
        self.assertEqual(scene.transport.loop_state, "none")
        self.assertIsNone(scene.transport.loop_a)
        self.assertIsNone(scene.transport.loop_b)

    def test_first_press_reddens_border_second_clears_it(self):
        scene = self._scene()
        with _freeze_time(5.0):
            scene.transport_loop_toggle()
        self.assertTrue(scene.transport.record_border_active)
        scene.api.write_regs.assert_called_with("d020", 2)  # type: ignore[attr-defined]

        with _freeze_time(8.0):
            scene.transport_loop_toggle()
        self.assertFalse(scene.transport.record_border_active)
        scene.api.write_regs.assert_called_with("d020", 0)  # type: ignore[attr-defined]
        # So a petscii/blank border that is not black comes back on the next push.
        scene.api.invalidate_region.assert_called_with(RegionID.VIC_D020)  # type: ignore[attr-defined]


class VideoSceneRecordStopTest(unittest.TestCase):
    """transport_record (arm) / transport_stop (close loop / pause / quit-
    signal) — the Record/Stop entry point into the same state machine
    transport_loop_toggle drives (MIDI live-tune Phase 3)."""

    def _scene(self) -> VideoScene:
        return _make_video_scene_stub(_StubSource(duration=100.0))

    def test_record_arms_and_reddens_border(self):
        scene = self._scene()
        with _freeze_time(3.0):
            scene.transport_record()
        self.assertEqual(scene.transport.loop_state, "armed")
        self.assertEqual(scene.transport.loop_a, 3.0)
        self.assertTrue(scene.transport.record_border_active)
        scene.api.write_regs.assert_called_with("d020", 2)  # type: ignore[attr-defined]

    def test_record_is_noop_when_already_armed(self):
        scene = self._scene()
        with _freeze_time(3.0):
            scene.transport_record()
        with _freeze_time(9.0):
            scene.transport_record()
        self.assertEqual(scene.transport.loop_a, 3.0)  # unchanged by the second call

    def test_stop_while_armed_closes_loop_and_clears_border(self):
        scene = self._scene()
        with _freeze_time(3.0):
            scene.transport_record()
        with _freeze_time(7.0):
            quit_requested = scene.transport_stop()
        self.assertFalse(quit_requested)
        self.assertEqual(scene.transport.loop_state, "active")
        self.assertEqual(scene.transport.loop_b, 7.0)
        self.assertFalse(scene.transport.record_border_active)
        scene.api.write_regs.assert_called_with("d020", 0)  # type: ignore[attr-defined]

    def test_stop_while_playing_pauses(self):
        scene = self._scene()
        with _freeze_time(1.0):
            quit_requested = scene.transport_stop()
        self.assertFalse(quit_requested)
        self.assertTrue(scene.transport.paused)

    def test_stop_while_already_paused_requests_quit(self):
        scene = self._scene()
        with _freeze_time(1.0):
            scene.transport_stop()  # first press: pauses
        with _freeze_time(2.0):
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
        scene.transport.loop_store = store
        return scene, store

    def test_save_persists_current_loop(self):
        scene, store = self._scene()
        scene.transport.loop_a = 10.0
        scene.transport.loop_b = 20.0
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
        self.assertEqual(scene.transport.loop_a, 12.0)
        self.assertEqual(scene.transport.loop_b, 34.0)
        self.assertEqual(scene.transport.loop_state, "active")
        self.assertEqual(scene.source.seeks, [12.0])  # type: ignore[union-attr]

    def test_plain_press_on_empty_slot_loops_whole_file(self):
        scene, _store = self._scene()
        scene.transport_loop_slot(9, save=False, clear=False)
        self.assertEqual(scene.transport.loop_a, 0.0)
        self.assertIsNone(scene.transport.loop_b)
        self.assertEqual(scene.transport.loop_state, "active")

    def test_recall_resumes_if_paused(self):
        scene, store = self._scene()
        store.save(1, 5.0, None)
        scene.transport.paused = True
        scene.transport.touched = True
        scene.transport_loop_slot(1, save=False, clear=False)
        self.assertFalse(scene.transport.paused)


class TransportOsdBoundaryTest(unittest.TestCase):
    """The engine's OSD line goes over the *audience* output, so it carries
    transport **state** — what the picture is now doing — and not confirmation
    that a control was pressed. A save or a clear changes a file on disk and
    nothing on screen; the console sees it via `loop_slots` in every state
    frame, and the log records it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _scene(self) -> tuple[VideoScene, LoopPresetStore]:
        scene = _make_video_scene_stub(_StubSource(duration=100.0))
        store = LoopPresetStore(Path(self._tmp.name) / "loop.json", video_ref="clip.mp4", size=123)
        scene.transport.loop_store = store
        return scene, store

    def test_a_save_posts_no_osd(self):
        scene, _store = self._scene()
        scene.transport.loop_a = 10.0
        scene.transport.loop_b = 20.0
        with self.assertLogs("c64cast.scenes.video_transport", level="INFO"):
            scene.transport_loop_slot(1, save=True, clear=False)
        self.assertIsNone(scene.osd.current())

    def test_a_save_with_no_loop_posts_no_osd(self):
        scene, _store = self._scene()
        with self.assertLogs("c64cast.scenes.video_transport", level="INFO"):
            scene.transport_loop_slot(1, save=True, clear=False)
        self.assertIsNone(scene.osd.current())

    def test_a_clear_posts_no_osd(self):
        scene, store = self._scene()
        store.save(2, 1.0, 2.0)
        with self.assertLogs("c64cast.scenes.video_transport", level="INFO"):
            scene.transport_loop_slot(2, save=False, clear=True)
        self.assertIsNone(scene.osd.current())

    def test_a_recall_does_post_osd(self):
        # Recall changes what is playing, so the state that follows it belongs
        # on the audience screen — this is the boundary, not an exception.
        scene, store = self._scene()
        store.save(3, 12.0, 34.0)
        scene.transport_loop_slot(3, save=False, clear=False)
        self.assertEqual(scene.osd.current(), "LOOP 3")

    def test_arming_and_looping_still_post_state(self):
        scene, _store = self._scene()
        scene.transport_loop_toggle()
        self.assertIsNotNone(scene.osd.current())
        self.assertTrue((scene.osd.current() or "").startswith("LOOP A"))
        scene.transport_loop_toggle()
        self.assertTrue((scene.osd.current() or "").startswith("LOOP "))
        scene.transport_loop_toggle()
        self.assertEqual(scene.osd.current(), "LOOP OFF")


class VideoSceneRecordBorderTeardownTest(unittest.TestCase):
    def test_teardown_restores_border_when_left_armed(self):
        scene = _make_video_scene_stub(_StubSource(duration=100.0))
        scene.audio = None
        scene._av_lag_count = 0
        with _freeze_time(1.0):
            scene.transport_record()
        self.assertTrue(scene.transport.record_border_active)
        scene.teardown()
        self.assertFalse(scene.transport.record_border_active)
        scene.api.write_regs.assert_called_with("d020", 0)  # type: ignore[attr-defined]

    def test_teardown_is_noop_when_never_armed(self):
        scene = _make_video_scene_stub(_StubSource(duration=100.0))
        scene.audio = None
        scene._av_lag_count = 0
        scene.teardown()
        scene.api.write_regs.assert_not_called()  # type: ignore[attr-defined]


class VideoSceneEndsAudioInputTest(unittest.TestCase):
    """setup() hands the demuxer the sink's `end_input` along with its
    `push_samples` on both push paths, so a clip shorter than the sink's
    prebuffer still starts it."""

    def _setup(self, audio) -> mock.MagicMock:
        scene = VideoScene(
            api=mock.MagicMock(),
            audio=audio,
            display_mode=mock.MagicMock(frame_target_size=None),
            file=STUB_VIDEO_URL,
            setup_progress=False,
        )
        with (
            mock.patch.object(scenes, "ensure_pyav", return_value=True),
            mock.patch.object(scenes, "AVFileSource") as source_cls,
        ):
            scene.setup()
        return source_cls.return_value

    def test_the_dac_path_passes_end_input_and_its_epoch(self):
        from c64cast.audio.audio import AudioStreamer

        audio = mock.MagicMock(spec=AudioStreamer, effective_rate=8000.0, use_reu_pump=False)
        source = self._setup(audio)
        source.start.assert_called_once_with(
            audio_push=audio.push_samples,
            audio_end=audio.end_input,
            audio_epoch=audio.current_flush_epoch,
        )

    def test_the_sampler_path_passes_end_input_and_its_epoch(self):
        from c64cast.audio.sampler import UltimateAudioSampler

        audio = mock.MagicMock(spec=UltimateAudioSampler, effective_rate=8000.0)
        source = self._setup(audio)
        source.start.assert_called_once_with(
            audio_push=audio.push_samples,
            audio_end=audio.end_input,
            audio_epoch=audio.current_flush_epoch,
        )


class EndAudioInputSeekGuardTest(unittest.TestCase):
    """A pass that reaches EOF ends the sink's input, except when a seek is
    already pending: the splice's flush may have run, and an end marked after
    it would cut the post-seek input."""

    def _source(self, *, pending_seek: float | None) -> tuple[AVFileSource, list[bool]]:
        ended: list[bool] = []
        src = _make_emit_audio_stub([])
        src._audio_end = lambda: ended.append(True)
        src._pending_seek = pending_seek
        return src, ended

    def test_eof_ends_the_input(self):
        src, ended = self._source(pending_seek=None)
        src._end_audio_input()
        self.assertEqual(ended, [True])

    def test_a_pending_seek_holds_the_end_back(self):
        src, ended = self._source(pending_seek=1.0)
        src._end_audio_input()
        self.assertEqual(ended, [])

    def test_the_end_is_called_under_the_seek_lock(self):
        # Checked and called outside the lock, a request_seek + flush landing
        # between the two would still get the end marked after the flush.
        held: list[bool] = []
        src, _ = self._source(pending_seek=None)
        src._audio_end = lambda: held.append(src._lock.locked())
        src._end_audio_input()
        self.assertEqual(held, [True])

    def test_a_closed_source_does_not_end_the_input(self):
        # The sink outlives the source: a demux thread that outlived
        # close()'s join would otherwise end the next activation's input.
        src, ended = self._source(pending_seek=None)
        src._closed = True
        src._end_audio_input()
        self.assertEqual(ended, [])

    def test_an_end_after_the_cut_survives_the_flush(self):
        # The post-seek pass can reach EOF and end the input between
        # request_seek and the splice's flush. A flush that reopened the
        # input left nothing to end it again.
        for name, sink in _splice_sinks():
            with self.subTest(sink=name):
                src = _make_emit_audio_stub([])
                src._audio_end = sink.end_input
                cut = src.request_seek(1.0, on_request=sink.cut)
                src._pending_seek = None  # the demux thread applied the seek
                src._end_audio_input()
                sink.flush(cut=cut)
                self.assertTrue(sink._input_ended)

    def test_the_cut_reopens_an_input_the_pre_seek_pass_ended(self):
        for name, sink in _splice_sinks():
            with self.subTest(sink=name):
                src = _make_emit_audio_stub([])
                src._audio_end = sink.end_input
                src._end_audio_input()
                src.request_seek(1.0, on_request=sink.cut)
                self.assertFalse(sink._input_ended)

    def test_a_demux_crash_ends_the_input(self):
        # Nothing more is pushed after a crash, so a clip that pushed less
        # than the prebuffer before it is played only if the input ends.
        class _CrashingContainer:
            def demux(self):
                raise RuntimeError("decode failed")

        ended: list[bool] = []
        src = _make_demux_source_stub([])
        src.container = _CrashingContainer()
        src._audio_push = lambda arr: None
        src._audio_end = lambda: ended.append(True)
        with self.assertLogs("c64cast.video.video", level="ERROR") as logs:
            src._demux_loop()
        self.assertIn("crashed", logs.output[0])
        self.assertEqual(ended, [True])
        self.assertTrue(src._demux_exited)


class VideoSceneProcessFrameLoopTest(unittest.TestCase):
    """process_frame's EOF check + loop-wrap: an active A/B loop neither
    ends the scene at EOF nor at reaching B — it seeks back to A instead."""

    def test_wraps_to_a_when_clock_reaches_b(self):
        source = _StubSource(duration=100.0)
        scene = _make_video_scene_stub(source)
        scene.transport.touched = True
        scene.transport.loop_state = "active"
        scene.transport.loop_a = 5.0
        scene.transport.loop_b = 10.0
        scene.transport.wall_anchor_clock_s = 10.0
        scene.transport.wall_anchor_time = 0.0
        with _freeze_time(0.0):
            still_active = scene.process_frame(current_time=0.0)
        self.assertTrue(still_active)
        self.assertEqual(source.seeks, [5.0])
        self.assertEqual(scene.transport.wall_anchor_clock_s, 5.0)

    def test_wraps_to_a_when_source_hits_eof_before_b(self):
        source = _StubSource(duration=100.0)
        source.finished = True
        scene = _make_video_scene_stub(source)
        scene.transport.touched = True
        scene.transport.loop_state = "active"
        scene.transport.loop_a = 5.0
        scene.transport.loop_b = 50.0  # clock hasn't reached B yet
        scene.transport.wall_anchor_clock_s = 20.0
        scene.transport.wall_anchor_time = 0.0
        with _freeze_time(0.0):
            still_active = scene.process_frame(current_time=0.0)
        self.assertTrue(still_active)
        self.assertEqual(source.seeks, [5.0])

    def test_eof_with_a_dead_demux_thread_ends_a_looping_scene(self):
        # Nothing is left to apply the wrap's seek, so wrapping would hold
        # the last frame, re-requesting A every tick, until the loop is cleared.
        source = _StubSource(duration=100.0)
        source.finished = True
        source.accepts_seeks = False
        scene = _make_video_scene_stub(source)
        scene.transport.touched = True
        scene.transport.loop_state = "active"
        scene.transport.loop_a = 5.0
        scene.transport.loop_b = 50.0
        scene.transport.wall_anchor_clock_s = 20.0
        scene.transport.wall_anchor_time = 0.0
        with _freeze_time(0.0):
            still_active = scene.process_frame(current_time=0.0)
        self.assertFalse(still_active)
        self.assertEqual(source.seeks, [])

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
        scene.transport.loop_state = "armed"
        scene.transport.loop_a = 5.0
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
            _freeze_time(0.0),
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
        self.assertIn(timecode(50.0), label)

    def test_untouched_reads_the_clock_back_into_file_time_under_tempo_compensation(self):
        # Under the DAC+bitmap tempo scale the clock runs in the scaled
        # domain: 8.8 s of it at 0.88 is 10 s into the file past start_s.
        source = _StubSource(duration=None)
        scene = _make_video_scene_stub(source, start_s=50.0)
        scene.show_frame_numbers = True
        scene.tempo_scale = 0.88
        scene.wall_start_time = -8.8
        label = self._run(scene)
        self.assertEqual(label, f"{timecode(60.0)} f{round(60.0 * 30)}")

    def test_a_jog_before_the_touch_steps_from_the_file_position(self):
        # The first FF/RW step and the web console read position() before
        # anything has touched transport; at 8.8 s of a 0.88-scaled clock past
        # start_s=50, +10 lands at 70, not at 18.8.
        source = _StubSource(duration=200.0)
        scene = _make_video_scene_stub(source, start_s=50.0)
        scene.tempo_scale = 0.88
        scene.wall_start_time = -8.8
        with _freeze_time(0.0):
            self.assertAlmostEqual(scene.transport_position(), 60.0)
            scene.transport_seek(scene.transport_position() + 10.0)
        self.assertEqual(len(source.seeks), 1)
        self.assertAlmostEqual(source.seeks[0], 70.0)

    def test_touched_does_not_double_count_start_s(self):
        source = _StubSource(duration=None)
        scene = _make_video_scene_stub(source, start_s=50.0)
        scene.show_frame_numbers = True
        scene.transport.touched = True
        scene.transport.wall_anchor_clock_s = 80.0  # already an absolute file position
        scene.transport.wall_anchor_time = 0.0
        label = self._run(scene)
        self.assertIn(timecode(80.0), label)
        self.assertNotIn(timecode(130.0), label)  # the double-counted (wrong) value


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
        frame = _FakeFrame(0, width=src_w, height=src_h)
        src = _make_demux_source_stub([_FakePacket([frame])], decode_target=decode_target)
        _demux_until_parked(src)
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


@unittest.skipUnless(ensure_pyav(), "PyAV (video extra) not installed")
class AtempoTempoCompensationTest(unittest.TestCase):
    """Bitmap+DAC tempo compensation: the atempo graph time-compresses audio
    (pitch-preserving) by 1/tempo_scale, so the emitted sample count is ≈
    tempo_scale × the input count. Drives the real AVFileSource emit path
    (_drain_atempo / _flush_atempo / _emit_audio) through a __new__ stub so no
    container/file is needed."""

    SR = 8000

    def _stub(self, tempo_scale: float, sink) -> AVFileSource:
        src = _make_emit_audio_stub(sink, tempo_scale=tempo_scale)
        src._atempo_graph = _build_atempo_graph(self.SR, tempo_scale)
        return src

    def _feed(self, src: AVFileSource, total_samples: int, frame_len: int = 1024) -> None:
        import av

        rng = np.random.default_rng(0)
        pts = 0
        for _ in range(0, total_samples, frame_len):
            arr = rng.integers(-2000, 2000, frame_len).astype(np.int16).reshape(1, -1)
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
        # _emit_audio must still apply normalization gain when routing
        # through the graph.
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


@unittest.skipUnless(ensure_pyav(), "PyAV not installed")
class ScanVideoSamplesTest(unittest.TestCase):
    """`scan_video_samples` seek-samples across a source's whole timeline."""

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
        self.assertTrue(scan_video_samples(path, [acc], max_samples=30))
        self.assertGreater(len(acc.means), 0)
        seen = {self._dominant(m) for m in acc.means}
        self.assertEqual(seen, {"r", "g", "b"})

    def test_missing_file_returns_false(self):
        acc = _RecordingAcc()
        with self.assertLogs("c64cast.video.video", level="WARNING"):
            self.assertFalse(scan_video_samples("/no/such/file.mp4", [acc]))
        self.assertEqual(acc.means, [])

    def test_empty_accumulators_short_circuit(self):
        self.assertFalse(scan_video_samples(self._make(), []))

    def test_falls_back_to_sequential_when_seek_fails(self):
        # A non-seekable source (seek raises) must still get sampled via the
        # sequential-decode fallback — and still span the whole timeline.
        import unittest.mock as mock

        from c64cast.video import video

        path = self._make()
        acc = _RecordingAcc()
        with mock.patch.object(video, "_seek_sample_frames", side_effect=OSError("not seekable")):
            self.assertTrue(scan_video_samples(path, [acc], max_samples=30))
        self.assertGreater(len(acc.means), 0)
        seen = {self._dominant(m) for m in acc.means}
        self.assertEqual(seen, {"r", "g", "b"})


@unittest.skipUnless(ensure_pyav(), "PyAV not installed")
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


@unittest.skipUnless(ensure_pyav(), "PyAV not installed")
class SeekAfterEofTest(unittest.TestCase):
    """The demuxer reads up to `max_video_buffer` frames ahead of playback, so
    near the end of a clip it reaches EOF while the scene still has frames to
    show — and a paused scene's frozen clock lets it run all the way there.
    A seek requested after that (a resume, an A/B loop wrap, a jog back) has
    to restart it at the target rather than end the scene."""

    def _started_at_eof(self) -> AVFileSource:
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        _write_synthetic_video(path, seconds=3, fps=30)
        src = AVFileSource(path, target_sample_rate=8000, scan_audio_peak=False)
        self.addCleanup(src.close)
        src.start(audio_push=None)
        # 90 frames fit the read-ahead buffer, so the demuxer reaches EOF
        # with no consumer at all.
        self.assertTrue(_wait_until(lambda: src._eof), "demuxer never reached EOF")
        return src

    def test_a_seek_after_eof_is_applied(self):
        src = self._started_at_eof()
        src.request_seek(2.0)
        self.assertTrue(
            _wait_until(lambda: src.video_buffer_depth > 0), "the seek was never applied"
        )
        img = src.current_frame(2.0)
        assert img is not None
        # The PTS rebase stamps any first frame with the target, so the
        # content decides: 2.0 s opens the blue third (BGR).
        self.assertGreater(img[..., 0].mean(), 200)
        self.assertLess(img[..., 2].mean(), 60)

    def test_a_pending_seek_after_eof_is_not_finished(self):
        # The scene polls `finished` every tick, and the request clears the
        # buffer at once: between the request and the demuxer applying it,
        # an EOF source with an empty buffer must not read as done.
        src = _make_av_source_stub([], eof=True)
        src.request_seek(1.0)
        self.assertFalse(src.finished)

    def test_a_seek_no_thread_will_apply_does_not_hold_finished_off(self):
        # A demux thread that crashed or closed has nothing left to apply it.
        src = _make_av_source_stub([], eof=True)
        src._demux_exited = True
        src.request_seek(1.0)
        self.assertTrue(src.finished)
        self.assertFalse(src.accepts_seeks)

    def test_a_scene_seeking_back_after_eof_keeps_playing(self):
        src = self._started_at_eof()
        scene = _make_video_scene_stub(_StubSource(duration=3.0))
        scene.source = src
        with mock.patch.object(scenes, "_render_with_overlays"), _freeze_time(0.0):
            scene.transport_seek(1.0)
            self.assertTrue(scene.process_frame(current_time=0.0), "the seek ended the scene")
            self.assertTrue(_wait_until(lambda: src.video_buffer_depth > 0))
            self.assertTrue(scene.process_frame(current_time=0.0))
        self.assertAlmostEqual(src.last_frame_pts, 1.0, delta=0.25)

    class _LiveDemuxContainer(_FakeContainer):
        """Records, at each seek, whether a demux() generator was still live."""

        def __init__(self, packets: list[_FakePacket], flush: _FakePacket | None = None):
            super().__init__(packets)
            self._flush = flush if flush is not None else _FakePacket([])
            self.reading = False
            self.seeks: list[bool] = []

        def demux(self):
            self.reading = True
            try:
                yield from self._packets
                yield self._flush  # PyAV's trailing flush packet
            finally:
                self.reading = False

        def seek(self, offset_us: int) -> None:
            self.seeks.append(self.reading)

    def _run_demux_loop(self, src: AVFileSource) -> None:
        worker = threading.Thread(target=src._demux_loop, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5.0)
        self.addCleanup(_signal_close, src)

    def test_a_seek_after_eof_is_applied_with_no_demux_live(self):
        # PyAV times a seek inside a live demux() against that generator's
        # last read, which a paused scene leaves stale; `_seek` bounds it
        # instead, between passes.
        src = _make_demux_source_stub([])
        container = self._LiveDemuxContainer([_FakePacket([_FakeFrame(0)])])
        src.container = container
        self._run_demux_loop(src)
        self.assertTrue(_wait_until(lambda: src._eof), "demux loop never reached EOF")
        src.request_seek(0.0)
        self.assertTrue(_wait_until(lambda: container.seeks), "the seek was never applied")
        self.assertEqual(container.seeks, [False])

    def test_a_seek_landing_on_the_flush_packet_is_applied_with_no_demux_live(self):
        # A seek requested while the last packet decodes is still applied,
        # rather than parked with the EOF.
        src = _make_demux_source_stub([])

        class _SeekingPacket(_FakePacket):
            requested = False

            def decode(self):
                if not self.requested:  # once: every pass yields this packet
                    self.requested = True
                    src.request_seek(0.0)
                return []

        container = self._LiveDemuxContainer(
            [_FakePacket([_FakeFrame(0)])], flush=_SeekingPacket([])
        )
        src.container = container
        self._run_demux_loop(src)
        self.assertTrue(_wait_until(lambda: container.seeks), "the seek was never applied")
        self.assertEqual(container.seeks, [False])

    def test_the_restarted_pass_reaches_eof_again(self):
        src = self._started_at_eof()
        src.request_seek(2.5)
        self.assertTrue(_wait_until(lambda: src.video_buffer_depth > 0))
        self.assertTrue(_wait_until(lambda: src._eof))
        while src.current_frame(10.0) is not None:
            pass
        self.assertTrue(src.finished)


class _RangeHttpServer:
    """A loopback HTTP server that honors `Range` for `body` on its first
    `serve` connections (every one when None) and accepts every later one
    without ever answering: the shape of a server that goes silent once the
    client seeks. `close()` releases every socket and joins its threads."""

    def __init__(self, body: bytes, serve: int | None = None):
        import socket

        self._body = body
        self._serve_n = serve
        self.requests = 0
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self._srv.settimeout(0.05)
        self.port = self._srv.getsockname()[1]
        self._held: list = []
        self._senders: list[threading.Thread] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/clip.mkv"

    def _serve(self):
        import re

        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                continue
            self._held.append(conn)
            self.requests += 1
            if self._serve_n is not None and self.requests > self._serve_n:
                continue
            conn.settimeout(2.0)
            try:
                request = conn.recv(4096)
            except OSError:
                continue
            # Blocking from here: a client that stops reading for a while (a
            # paused scene) must find the rest of the body still coming.
            conn.settimeout(None)
            m = re.search(rb"[Rr]ange: bytes=(\d+)-", request)
            start = int(m.group(1)) if m else 0
            part = self._body[start:]
            head = (
                b"HTTP/1.1 206 Partial Content\r\nConnection: close\r\nAccept-Ranges: bytes\r\n"
                + f"Content-Range: bytes {start}-{len(self._body) - 1}/{len(self._body)}\r\n".encode()
                + f"Content-Length: {len(part)}\r\n\r\n".encode()
            )
            sender = threading.Thread(target=self._send, args=(conn, head + part), daemon=True)
            self._senders.append(sender)
            sender.start()

    def _send(self, conn, data: bytes) -> None:
        with suppress(OSError):
            conn.sendall(data)

    def close(self):
        self._stop.set()
        self._thread.join(2.0)
        for conn in self._held:
            conn.close()
        for sender in self._senders:
            sender.join(2.0)
        self._srv.close()


def _write_av_mkv(path: str, *, seconds: int = 6, fps: int = 30, rate: int = 8000) -> None:
    """A Matroska clip with noise video (so it is too large to arrive in one
    read) and a tone on a PCM audio stream. Matroska keeps its seek index at
    the end of the file, so every seek in it opens a new range request."""
    import av

    rng = np.random.default_rng(0)
    container = av.open(path, "w", format="matroska")
    try:
        video = container.add_stream("mpeg4", rate=fps)
        video.width, video.height = 64, 64
        video.pix_fmt = "yuv420p"
        video.gop_size = 6
        video.bit_rate = 4_000_000
        audio = container.add_stream("pcm_s16le", rate=rate)
        audio.layout = "mono"
        for _ in range(seconds * fps):
            img = rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)
            for packet in video.encode(av.VideoFrame.from_ndarray(img, "rgb24")):
                container.mux(packet)
        t = np.arange(seconds * rate) / rate
        pcm = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
        frame.sample_rate = rate
        frame.pts = 0
        for packet in audio.encode(frame):
            container.mux(packet)
        for stream in (video, audio):
            for packet in stream.encode():
                container.mux(packet)
    finally:
        container.close()


@unittest.skipUnless(ensure_pyav(), "PyAV not installed")
class RemoteSeekBoundTest(unittest.TestCase):
    """A seek on a remote input opens a new range request, which PyAV bounds
    only while a `demux()` generator is live, and then against the clock of
    that generator's last read. Every seek has to fail within the read bound
    when the server goes silent — the start_s seek, the peak scan's, the
    color pre-scan's and the demux thread's own — and none may fail a
    healthy server because that clock went stale while playback was paused."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch("c64cast.video.video._REMOTE_OPEN_TIMEOUT_S", 0.5))
        stack.enter_context(mock.patch("c64cast.video.video._REMOTE_READ_TIMEOUT_S", 0.5))
        # The reconnect backoff would keep an abandoned seek redialing the
        # closed server for seconds, past the thread sandbox's grace.
        stack.enter_context(mock.patch("c64cast.video.video._HTTP_RECONNECT_OPTIONS", {}))
        fd, path = tempfile.mkstemp(suffix=".mkv")
        os.close(fd)
        self.addCleanup(os.remove, path)
        _write_av_mkv(path)
        self.local = path

    def _server(self, serve: int | None) -> str:
        """A server for the clip; opening it takes the first connection."""
        server = _RangeHttpServer(Path(self.local).read_bytes(), serve)
        self.addCleanup(server.close)
        return server.url

    def _bounded(self, fn, limit_s: float = 10.0):
        """`fn`'s result or what it raised; a worker still blocked after
        `limit_s` fails the test (closing the server releases it)."""
        box: list = []

        def run():
            try:
                box.append(fn())
            except Exception as e:  # noqa: BLE001 — the raise is the result
                box.append(e)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(limit_s)
        self.assertFalse(worker.is_alive(), f"still blocked after {limit_s}s")
        return box[0]

    def test_a_stalled_start_s_seek_fails_the_open(self):
        url = self._server(serve=1)
        outcome = self._bounded(
            lambda: AVFileSource(url, target_sample_rate=8000, scan_audio_peak=False, start_s=3.0)
        )
        self.assertIsInstance(outcome, RemoteSeekStalled)

    def test_an_abandoned_seek_returns_while_the_server_holds_the_socket(self):
        # Nothing interrupts the seek a caller gave up on, so only FFmpeg's
        # own IO timeout ends it; without one its worker, socket and
        # container last as long as the server keeps the connection open.
        url = self._server(serve=1)
        with self.assertRaises(RemoteSeekStalled):
            AVFileSource(url, target_sample_rate=8000, scan_audio_peak=False, start_s=3.0)
        self.assertTrue(
            _wait_until(
                lambda: not any(t.name == "av-seek" for t in threading.enumerate()), limit_s=10.0
            ),
            "the abandoned seek is still blocked",
        )

    def test_a_stalled_peak_scan_seek_falls_back_to_unity_gain(self):
        src = AVFileSource(self.local, target_sample_rate=8000, scan_audio_peak=False)
        self.addCleanup(src.close)
        src.path = self._server(serve=1)
        src.start_s = 3.0
        with self.assertLogs("c64cast.video.video", level="WARNING") as logs:
            self.assertEqual(self._bounded(src._scan_audio_peak), 0)
        # A read that fails after the seek also falls back to unity gain, but
        # as "failed": only the seek's own bound logs "skipped".
        self.assertTrue(any("peak scan skipped" in m for m in logs.output), logs.output)

    def test_a_stalled_color_prescan_seek_skips_the_scan(self):
        url = self._server(serve=1)
        with self.assertLogs("c64cast.video.video", level="WARNING") as logs:
            self.assertIs(self._bounded(lambda: scan_video_samples(url, [_RecordingAcc()])), False)
        # The sequential fallback's reopen fails against the same server, so
        # only the message tells the seek's bound from that open's.
        self.assertTrue(any("did not answer a seek" in m for m in logs.output), logs.output)

    def test_a_stalled_transport_seek_ends_the_source(self):
        src = AVFileSource(self._server(serve=1), target_sample_rate=8000, scan_audio_peak=False)
        self.addCleanup(src.close)
        with self.assertLogs("c64cast.video.video", level="ERROR") as logs:
            src.start(audio_push=None)
            self.assertTrue(_wait_until(lambda: src._eof), "demuxer never reached EOF")
            src.request_seek(3.0)
            self.assertTrue(_wait_until(lambda: src.finished), "a stalled seek hung the source")
        # A read that stalls after the seek ends the source too, as a crash.
        self.assertTrue(any("ending playback" in m for m in logs.output), logs.output)

    def test_close_during_a_transport_seek_leaves_the_container_to_it(self):
        # close() joins the demux thread for 1 s, well inside the read bound,
        # so it returns while the seek is still inside FFmpeg.
        self.enterContext(mock.patch("c64cast.video.video._REMOTE_READ_TIMEOUT_S", 5.0))
        server = _RangeHttpServer(Path(self.local).read_bytes(), serve=1)
        self.addCleanup(server.close)
        src = AVFileSource(server.url, target_sample_rate=8000, scan_audio_peak=False)
        real = src.container
        closed_by: list[str] = []

        def close():
            closed_by.append(threading.current_thread().name)
            real.close()

        src._closer._container = mock.Mock(close=close)
        with self.assertLogs("c64cast", level="WARNING") as logs:
            src.start(audio_push=None)
            self.assertTrue(_wait_until(lambda: src._eof), "demuxer never reached EOF")
            src.request_seek(3.0)
            self.assertTrue(
                _wait_until(lambda: any(t.name == "av-seek" for t in threading.enumerate()))
            )
            src.close()
            self.assertEqual(closed_by, [], "close() freed the container under a live seek")
            server.close()
            self.assertTrue(_wait_until(lambda: closed_by), "the seek never closed the container")
            self.assertTrue(_wait_until(lambda: src._demux_exited))
        self.assertEqual(closed_by, ["av-seek"])
        self.assertTrue(any("did not stop" in m for m in logs.output))

    def test_a_seek_after_a_long_pause_reaches_a_healthy_server(self):
        # Blocked on a full buffer, the demux thread holds its generator open
        # past the read bound, as a paused scene's does.
        src = AVFileSource(self._server(serve=None), target_sample_rate=8000, scan_audio_peak=False)
        self.addCleanup(src.close)
        src.max_video_buffer = 5
        src.start(audio_push=None)
        self.assertTrue(_wait_until(lambda: src.video_buffer_depth == 5))
        time.sleep(1.0)
        src.request_seek(3.0)
        self.assertTrue(
            _wait_until(lambda: src.video_buffer_depth > 0), "the seek never reached the server"
        )
        self.assertFalse(src.finished)


class SpliceAnchorTest(unittest.TestCase):
    """A resync splice anchors the clock where the sink's flush put the
    target's first sample, read once and after the flush has cleared any
    end-of-stream clamp."""

    def _touched(self, audio: Any) -> VideoScene:
        scene = _make_video_scene_stub(_StubSource(duration=100.0, a_stream=object()))
        scene.audio = audio
        scene.transport.loop_audio = "on"
        scene.transport.touch()
        self.assertTrue(scene.transport.resync)
        return scene

    def test_a_splice_after_the_samplers_eof_anchors_on_its_unclamped_clock(self):
        # mark_eof clamps the sampler's clock to the pushed total while the
        # wall runs on, and the flush clears the clamp: an anchor read before
        # the flush put the picture the clamp's overrun ahead of the sound.
        smp = UltimateAudioSampler(
            cast(Any, mock.MagicMock()), sample_rate=2000, bits=8, ring_size=0x4000
        )
        smp._running = True
        smp._gate_time = time.monotonic() - 5.0
        smp._pushed_samples = 2000  # 1 s pushed, 5 s on the wall
        smp.mark_eof()
        scene = self._touched(smp)
        scene.transport_seek(0.5)
        self.assertAlmostEqual(scene.transport.clock_s(), 0.5 - smp.ring_lead_seconds(), delta=0.05)

    def test_a_console_poll_during_the_flush_reads_the_target(self):
        # The web console reads position() off the playlist thread; read
        # against the previous anchor, a poll during the flush showed the
        # target plus everything heard since then.
        audio = _FakeSceneAudio(position=0.0)
        scene = self._touched(audio)
        audio._position = 40.0
        polls: list[float] = []
        flush = audio.flush

        def flush_while_polled(
            *, silence_output: bool = False, cut: FlushCut | None = None
        ) -> float:
            polls.append(scene.transport.position())
            return flush(silence_output=silence_output, cut=cut)

        with mock.patch.object(audio, "flush", side_effect=flush_while_polled):
            scene.transport_seek(5.0)
        self.assertEqual(polls, [5.0])

    def test_a_console_poll_after_the_samplers_cut_over_reads_the_target(self):
        # The cut-over clears mark_eof's clamp before flush() returns. Held on
        # an estimate read while the clamp stood, a poll there read the target
        # plus the clamp's overrun.
        smp = UltimateAudioSampler(
            cast(Any, mock.MagicMock()), sample_rate=2000, bits=8, ring_size=0x4000
        )
        smp._running = True
        smp._gate_time = time.monotonic() - 5.0
        smp._pushed_samples = 2000  # 1 s pushed, 5 s on the wall
        smp.mark_eof()
        scene = self._touched(smp)
        polls: list[float] = []
        cut_over = smp._cut_over

        def cut_over_then_poll(*args: Any, **kwargs: Any) -> None:
            cut_over(*args, **kwargs)
            polls.append(scene.transport.position())

        with mock.patch.object(smp, "_cut_over", side_effect=cut_over_then_poll):
            scene.transport_seek(0.5)
        self.assertEqual(polls, [0.5])

    def test_a_flush_that_raises_leaves_the_clock_running(self):
        # Held at the target until the flush returns, the clock would stay
        # held for good after a flush that never does.
        audio = _FakeSceneAudio(position=0.0)
        scene = self._touched(audio)
        audio._position = 40.0
        with (
            mock.patch.object(audio, "flush", side_effect=RuntimeError("link down")),
            self.assertRaises(RuntimeError),
        ):
            scene.transport_seek(5.0)
        audio._position = 41.0
        self.assertAlmostEqual(scene.transport.clock_s(), 6.0 - audio.ring_lead)

    def test_a_cut_that_raises_leaves_the_clock_running(self):
        # The cut runs inside the seek request (a link read on the sampler):
        # raising there must take the same fallback as a raising flush.
        audio = _FakeSceneAudio(position=0.0)
        scene = self._touched(audio)
        audio._position = 40.0
        with (
            mock.patch.object(audio, "cut", side_effect=RuntimeError("link down")),
            self.assertRaises(RuntimeError),
        ):
            scene.transport_seek(5.0)
        audio._position = 41.0
        self.assertAlmostEqual(scene.transport.clock_s(), 6.0 - audio.ring_lead)

    def test_a_console_poll_during_a_resume_reads_the_paused_position(self):
        # Unpaused before the splice stored its anchor, the clock read the
        # paused position plus everything heard since the previous anchor.
        audio = _FakeSceneAudio(position=0.0)
        scene = self._touched(audio)
        audio._position = 10.0
        scene.transport.pause()
        audio._position = 70.0
        polls: list[float] = []
        flush = audio.flush

        def flush_while_polled(
            *, silence_output: bool = False, cut: FlushCut | None = None
        ) -> float:
            polls.append(scene.transport.position())
            return flush(silence_output=silence_output, cut=cut)

        with mock.patch.object(audio, "flush", side_effect=flush_while_polled):
            scene.transport.resume()
        self.assertEqual(polls, [10.0])
        self.assertFalse(scene.transport.is_paused())

    def test_a_dac_splice_anchors_behind_the_chunk_in_flight(self):
        # 1000 bytes landed and a 400-byte chunk on its way to the ring: the
        # target's first sample is heard at 1400. A position and a lead read
        # separately also paired two landed counts when a chunk landed
        # between the reads.
        dac = AudioStreamer(cast(Ultimate64API, FakeAPI()), 8000, "NTSC")
        scene = self._touched(dac)
        dac._pushed_count, dac._queued_samples, dac._in_flight_samples = 1800, 800, 400
        scene.transport_seek(2.0)
        pos = scene.transport.audio_anchor_pos
        assert pos is not None
        self.assertEqual(round(pos * dac.effective_rate, 6), 1400)


if __name__ == "__main__":
    unittest.main()
