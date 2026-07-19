"""Video input sources.

* `WebcamSource` -- always-on shared camera broker: one background grab thread
  owns the VideoCapture and hands copies of the latest frame to any number of
  consumers (the webcam scene + the vision controller).
* `AVFileSource` -- PyAV demuxer for file playback. Splits one container into
  audio (pushed straight through to AudioStreamer) and video (queued with PTS).
  Consumers select the next video frame by passing the current audio clock
  position to `current_frame()`, which drops anything behind and returns the
  newest frame whose PTS is ≤ the clock. This is the audio-master sync model.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import cv2
import numpy as np

from ._native_io import silence_native_stderr
from ._pollthread import PollThread
from .audio import DAC_VOLUME_SCALE, INT16_FULL_SCALE, INT16_MAX, INT16_MIN
from .palette import ColorFit, ColorFitAccumulator, ColorMap, ColorMapAccumulator

log = logging.getLogger(__name__)

# Peak-normalization for video-scene audio. The SID volume DAC is 4-bit;
# `(float + 1) * 7.5` puts samples within ±0.067 of zero on NEUTRAL_SAMPLE,
# so a source peaking below ~30% of int16 full scale plays as silence-with-
# clicks. AVFileSource pre-scans each file and scales every pushed frame to
# bring the peak to TARGET_PEAK × int16-max. MAX_GAIN caps the boost so a
# near-silent file isn't amplified into pure noise.
NORMALIZATION_TARGET_PEAK = 0.9
NORMALIZATION_MAX_GAIN = 16.0

# Half-width of the NEUTRAL band in int16 sample units: any post-gain
# |sample| below this rounds to NEUTRAL_SAMPLE (=7) after encoding, so it
# contributes nothing audible — it just adds quantization-jitter noise as
# the encoder flips between values 6/7/8 around the source noise floor.
# Mathematically: (sample/32768 + 1) * 7.5 in [7, 8) iff |sample| < 32768
# * 0.5 / 7.5. Pre-gain gate threshold = NEUTRAL_BAND_INT16 / gain.
NEUTRAL_BAND_INT16 = INT16_FULL_SCALE * 0.5 / DAC_VOLUME_SCALE

# FFmpeg http/https protocol reconnect options. A resolved YouTube stream is a
# single progressive googlevideo CDN URL; the CDN throttles (see the `cps=` and
# `ratebypass` query params) and drops the connection mid-stream, which surfaces
# as `OSError: [Errno 5] Input/output error` out of `container.demux()` and
# kills playback. These make FFmpeg transparently re-establish the HTTP
# connection and resume from the current byte offset instead of erroring out:
#   reconnect                  reconnect on connection close / EOF
#   reconnect_streamed         reconnect for non-seekable (streamed) inputs
#   reconnect_on_network_error reconnect on any network error mid-stream
#   reconnect_delay_max        cap the exponential backoff (seconds)
# Applied only to remote URLs (see `_av_open`) — harmless for local files but
# these are http-protocol-only options, so we scope them to avoid FFmpeg
# warning about unrecognized options on a file:// / plain-path input.
_HTTP_RECONNECT_OPTIONS = {
    "reconnect": "1",
    "reconnect_streamed": "1",
    "reconnect_on_network_error": "1",
    "reconnect_delay_max": "5",
}


def _is_remote_url(path: str) -> bool:
    """True for http(s) inputs, which get the FFmpeg reconnect options."""
    return path.startswith(("http://", "https://"))


def _av_open(path: str):
    """`av.open` wrapper that injects the HTTP reconnect options for remote
    URLs so a transient CDN drop mid-stream resumes instead of crashing the
    demuxer. Local paths open unchanged."""
    if _is_remote_url(path):
        return av.open(path, options=_HTTP_RECONNECT_OPTIONS)
    return av.open(path)


def _compute_normalization_gain(
    peak_int16: int,
    target_peak: float = NORMALIZATION_TARGET_PEAK,
    max_gain: float = NORMALIZATION_MAX_GAIN,
) -> float:
    """Map a measured int16 peak to a multiplicative gain. Returns 1.0 for
    zero/negative peaks (defensive) and for peaks already at-or-above the
    target. Caps at max_gain so near-silent input doesn't get boosted into
    noise."""
    if peak_int16 <= 0:
        return 1.0
    gain = (target_peak * INT16_MAX) / peak_int16
    return min(max(gain, 1.0), max_gain)


# The C64 display aspect every video frame is center-cropped to before the
# display mode downscales it (must match scenes._C64_ASPECT / _crop_to_aspect —
# kept local to avoid a scenes→video import cycle).
_CROP_ASPECT = 320 / 200
# How much bigger than the display mode's final target the decode output is
# kept, in each axis, after the center-crop — so the mode's INTER_AREA resize
# stays a pure downscale (never an upscale) with area-averaging headroom. 2.0
# is the classic supersample-then-area-average margin; the result is quantized
# to ≤16 colors / ≤320px anyway, so more is wasted decode and less risks
# softness on a source already near the target. Decode is capped at the source
# size regardless (never upscales the source).
DECODE_HEADROOM = 2.0


def _plan_decode_size(
    src_w: int,
    src_h: int,
    target_w: int,
    target_h: int,
    headroom: float = DECODE_HEADROOM,
) -> tuple[int, int] | None:
    """Plan a decode ``(width, height)`` for a source frame that the scene will
    center-crop to ``_CROP_ASPECT`` and the display mode will then downscale to
    ``(target_w, target_h)``.

    Returns the smallest even (w, h) — preserving the source aspect — whose
    *post-crop* dimensions still exceed ``(target_w, target_h)`` by ``headroom``
    in both axes, so the final resize is a pure INTER_AREA downscale. Returns
    None when the source is already small enough that no downscale helps (the
    planned size would meet or exceed the source), so the caller keeps the plain
    full-resolution yuv→bgr convert.

    The point: a 4K source frame decoded to a full-res BGR buffer then
    cv2.resize-d to 320px costs ~40 ms/frame of host convert+resize — over the
    ~33 ms budget at 30 fps — which starves the audio-master video clock and
    makes playback lag + drift on clips the box can't decode in real time. Doing
    the downscale *inside* the swscale pass (av.VideoFrame.reformat) drops that
    to ~4 ms/frame: the conversion and every downstream op work on a ~640px
    frame instead of 4K. See AVFileSource._demux_loop and
    project_av_sync_decode_bound.
    """
    if src_w <= 0 or src_h <= 0 or target_w <= 0 or target_h <= 0:
        return None
    # Cropped source dimensions at _CROP_ASPECT — mirrors scenes._crop_to_aspect
    # so the headroom math reflects the pixels that actually survive the crop.
    ar = src_w / src_h
    if ar > _CROP_ASPECT:  # wider than 1.6 → crop trims width
        crop_w, crop_h = src_h * _CROP_ASPECT, float(src_h)
    elif ar < _CROP_ASPECT:  # taller than 1.6 → crop trims height
        crop_w, crop_h = float(src_w), src_w / _CROP_ASPECT
    else:
        crop_w, crop_h = float(src_w), float(src_h)
    scale = headroom * max(target_w / crop_w, target_h / crop_h)
    if scale >= 1.0:
        return None  # source already at/below the resolution the mode needs
    # Round to even dimensions (chroma-subsampled source codecs want even
    # dims; swscale warns otherwise) and never below 2px.
    dw = max(2, round(src_w * scale / 2) * 2)
    dh = max(2, round(src_h * scale / 2) * 2)
    return dw, dh


# PyAV is imported lazily on first AVFileSource construction. On macOS the av
# wheel bundles a different libavdevice major version than the cv2 wheel, so the
# moment av's libavdevice loads on top of cv2's, the Obj-C runtime prints a
# one-time "Class AVFFrameReceiver/AVFAudioReceiver is implemented in both ..."
# warning to fd 2 (it's the duplicated AVFoundation device classes; harmless,
# neither file-decode path touches the avfoundation input device). Deferring av
# until a video scene actually runs keeps that import off the startup path when
# videos aren't used, and the import itself is wrapped in silence_native_stderr
# so the warning never reaches the user even when a video does run.
av: Any = None
PYAV_AVAILABLE: bool | None = None  # tri-state: None = not yet probed


def _ensure_pyav() -> bool:
    """Import PyAV on demand; cache the result. Returns availability."""
    global av, PYAV_AVAILABLE
    if PYAV_AVAILABLE is not None:
        return PYAV_AVAILABLE
    try:
        # fd-level mute: the duplicate-libavdevice objc warning is printed by
        # the runtime as the native libs load during import, below Python.
        with silence_native_stderr():
            import av as _av

        av = _av
        PYAV_AVAILABLE = True
    except ImportError:
        PYAV_AVAILABLE = False
    return PYAV_AVAILABLE


def _build_atempo_graph(target_sample_rate: int, tempo_scale: float):
    """Build a one-stage `atempo` filter graph that time-compresses mono/s16
    audio (pitch-preserving) by ``1 / tempo_scale``. Fed the s16/mono/
    target_sample_rate frames the AVFileSource resampler already produces, so
    the abuffer format is fixed. Used by the bitmap+DAC tempo-compensation path
    (see AVFileSource + the `video.py` note in docs/architecture.md).

    Callers keep ``tempo_scale`` in (0, 1) (validate_dac_bitmap_tempo_cfg bounds
    it to 0.5..1.0), so ``1/tempo_scale`` lands in (1.0, 2.0] — inside atempo's
    single-stage 0.5..2.0 range. Requires PyAV (`_ensure_pyav()` first)."""
    graph = av.filter.Graph()
    abuffer = graph.add(
        "abuffer",
        sample_rate=str(target_sample_rate),
        sample_fmt="s16",
        channel_layout="mono",
        time_base=f"1/{target_sample_rate}",
    )
    atempo = graph.add("atempo", f"{1.0 / tempo_scale:.6f}")
    sink = graph.add("abuffersink")
    abuffer.link_to(atempo)
    atempo.link_to(sink)
    graph.configure()
    return graph


def decode_audio_full(path: str, target_sample_rate: int) -> np.ndarray:
    """Decode the entire audio track of ``path`` to mono int16 at
    ``target_sample_rate``. Returns a single contiguous np.ndarray.

    Blocking — call before scene paint starts. Used by the REU-staged audio
    path in VideoScene where the whole track must be preloaded into
    REU before playback begins.

    Cost: ~100-200 ms for a 30-sec video via PyAV on this hardware.
    Raises RuntimeError if PyAV isn't available or there's no audio stream
    in the container.
    """
    if not _ensure_pyav():
        raise RuntimeError("PyAV not installed; install with `pip install c64cast[video]`")
    container = _av_open(path)
    try:
        if not container.streams.audio:
            raise RuntimeError(f"no audio stream in {path}")
        a_stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=target_sample_rate)
        chunks: list[np.ndarray] = []
        for packet in container.demux(a_stream):
            for frame in packet.decode():
                for resampled in resampler.resample(frame):
                    arr = resampled.to_ndarray().reshape(-1)
                    if arr.size:
                        chunks.append(arr.astype(np.int16, copy=False))
    finally:
        container.close()
    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(chunks)


def _frame_to_scan_bgr(frame: Any, decode_size: tuple[int, int] | None) -> np.ndarray:
    """Convert one decoded frame to a BGR ndarray for the accumulators,
    downscaling DURING the yuv→bgr swscale pass when a decode size is planned."""
    if decode_size is not None:
        return frame.reformat(
            width=decode_size[0], height=decode_size[1], format="bgr24"
        ).to_ndarray()
    return frame.to_ndarray(format="bgr24")


def _source_duration_s(container: Any, v_stream: Any) -> float | None:
    """Playback duration of ``v_stream`` in seconds, or None when unknown.

    Prefers the stream's own duration (stream time_base units); falls back to
    the container duration (AV_TIME_BASE microseconds). None on a live/unbounded
    stream where neither is set — the caller then decodes sequentially."""
    if v_stream.duration is not None and v_stream.time_base is not None:
        return float(v_stream.duration * v_stream.time_base)
    if container.duration is not None:
        return float(container.duration) / float(av.time_base)
    return None


def _seek_sample_frames(
    container: Any,
    v_stream: Any,
    accumulators: list[Any],
    max_samples: int,
    duration_s: float,
    decode_target_size: tuple[int, int] | None,
) -> int:
    """Seek to ``max_samples`` evenly spaced timestamps across ``duration_s``,
    decode one frame at each, feed the accumulators. Returns the number of
    frames actually sampled (0 = seeking produced nothing → caller falls back).

    Seeks land on the keyframe at/before each target (``backward``), which is
    exactly right for color statistics — they're distribution-based, so a
    keyframe near each timestamp represents that region as well as an exact
    frame would, and keyframe-only seeking is the point: it makes the scan
    roughly constant-time regardless of file length or codec (a full-decode
    scan of a long/4K source is decode-bound and scales with the whole file)."""
    time_base = v_stream.time_base
    start_time = v_stream.start_time or 0
    decode_size: tuple[int, int] | None = None
    planned = False
    taken = 0
    for n in range(max_samples):
        # Sample interval midpoints so the last target isn't at/after EOF (which
        # can decode to nothing); spread across the open interval [0, duration).
        target_s = (n + 0.5) / max_samples * duration_s
        ts = start_time + int(target_s / time_base)
        container.seek(ts, stream=v_stream)  # backward=True (default) → keyframe ≤ ts
        frame = next(container.decode(v_stream), None)
        if frame is None:
            continue
        if not planned:
            planned = True
            if decode_target_size is not None:
                decode_size = _plan_decode_size(frame.width, frame.height, *decode_target_size)
        img = _frame_to_scan_bgr(frame, decode_size)
        for acc in accumulators:
            acc.add(img)
        taken += 1
    return taken


def _decode_sample_frames(
    container: Any,
    v_stream: Any,
    accumulators: list[Any],
    max_samples: int,
    decode_target_size: tuple[int, int] | None,
) -> None:
    """Sequential-decode fallback: stride through decoded frames and feed up to
    ``max_samples`` into the accumulators. Used when the source can't seek or
    has no known duration (live streams). Decode-bound, but the only option for
    a non-seekable source. Stride comes from the frame count when known."""
    total = v_stream.frames or 0
    stride = max(1, total // max_samples) if total else 5
    taken = 0
    decode_size: tuple[int, int] | None = None
    planned = False
    for i, frame in enumerate(container.decode(v_stream)):
        if i % stride:
            continue
        if not planned:
            planned = True
            if decode_target_size is not None:
                decode_size = _plan_decode_size(frame.width, frame.height, *decode_target_size)
        img = _frame_to_scan_bgr(frame, decode_size)
        for acc in accumulators:
            acc.add(img)
        taken += 1
        if taken >= max_samples:
            break


def _scan_video_samples(
    path: str,
    accumulators: list[Any],
    max_samples: int = 120,
    decode_target_size: tuple[int, int] | None = None,
) -> bool:
    """Sample up to ``max_samples`` frames spread across ``path`` and feed each
    into every accumulator's ``.add(img_bgr)``.

    Prefers **seek-sampled** collection (`_seek_sample_frames`): jump to evenly
    spaced timestamps and decode one keyframe each, so the scan is roughly
    constant-time regardless of length/codec. Falls back to a sequential decode
    (`_decode_sample_frames`) when the source has no known duration (a live
    stream) or seeking yields nothing (non-seekable). Blocking — call at scene
    setup, before playback. Returns True on a clean scan, False when PyAV is
    unavailable or decode failed (callers then treat each accumulator's result
    as None). Shared by the auto_fit and force_palette pre-scans so a source is
    decoded ONCE for both.

    ``decode_target_size`` (the display mode's frame_target_size) downscales
    each sampled frame DURING the yuv→bgr swscale pass (same win as playback —
    see _plan_decode_size). Color statistics are distribution-based, so the
    downscaled frame yields the same fit/palette as the full-res one at a
    fraction of the cost.
    """
    if not accumulators or not _ensure_pyav():
        return False
    try:
        container = _av_open(path)
        try:
            v_stream = container.streams.video[0]
            v_stream.thread_type = "AUTO"
            duration_s = _source_duration_s(container, v_stream)
            seeked = False
            if duration_s is not None and duration_s > 0:
                try:
                    seeked = (
                        _seek_sample_frames(
                            container,
                            v_stream,
                            accumulators,
                            max_samples,
                            duration_s,
                            decode_target_size,
                        )
                        > 0
                    )
                except Exception as e:
                    # Non-seekable source (some network streams / odd codecs):
                    # log and fall through to a sequential decode below.
                    log.debug(
                        "seek-sampled pre-scan of %s failed (%s); using sequential decode",
                        path,
                        e,
                    )
            if not seeked:
                # Re-open to reset any partial seek state, then decode in order.
                container.close()
                container = _av_open(path)
                v_stream = container.streams.video[0]
                v_stream.thread_type = "AUTO"
                _decode_sample_frames(
                    container, v_stream, accumulators, max_samples, decode_target_size
                )
        finally:
            container.close()
    except Exception as e:
        log.warning("color pre-scan of %s failed (%s); skipping", path, e)
        return False
    return True


def prescan_source_color(
    path: str,
    *,
    fit_strength: float | None = None,
    map_colors: int | None = None,
    map_indices: list[int] | None = None,
    decode_target_size: tuple[int, int] | None = None,
) -> tuple[ColorFit | None, ColorMap | None]:
    """Pre-scan a video once and derive the enabled per-source color stages.

    ``fit_strength`` not None enables the adaptive ColorFit ([color].auto_fit);
    ``map_colors``/``map_indices`` not None/empty enables the forced-palette
    ColorMap ([color].force_palette). Both stages share a single decode pass.
    ``decode_target_size`` downscales sampled frames during decode (see
    _scan_video_samples). Returns (ColorFit|None, ColorMap|None); a disabled or
    failed stage is None, so callers can unconditionally pass the results to
    set_color_fit / set_color_map. See palette.ColorFitAccumulator /
    palette.ColorMapAccumulator.
    """
    fit_acc = ColorFitAccumulator(strength=fit_strength) if fit_strength is not None else None
    map_acc = (
        ColorMapAccumulator(n_colors=map_colors or 16, indices=map_indices)
        if (map_colors is not None or map_indices)
        else None
    )
    accs = [a for a in (fit_acc, map_acc) if a is not None]
    if not _scan_video_samples(path, accs, decode_target_size=decode_target_size):
        return None, None
    return (fit_acc.result() if fit_acc else None, map_acc.result() if map_acc else None)


def prescan_color_fit(path: str, *, strength: float = 1.0) -> ColorFit | None:
    """Back-compat thin wrapper over `prescan_source_color` (auto_fit only)."""
    fit, _ = prescan_source_color(path, fit_strength=strength)
    return fit


class WebcamSource:
    """Always-on shared camera broker.

    A single `cv2.VideoCapture` can only be pull-read by one consumer — every
    `.read()` consumes the next frame off the device and concurrent reads from
    two threads aren't safe. So instead of letting each consumer pull the
    device directly, one background grab thread owns the capture, continuously
    reads the newest frame, and hands out independent *copies* to any number of
    consumers via `read()`. That lets the webcam scene (when active) and the
    vision controller (always) share a single physical camera with no
    contention — see [c64cast/vision.py](c64cast/vision.py).

    Returning the latest grabbed frame (rather than blocking for the next one)
    also keeps the live-webcam path low-latency: a consumer always gets the
    freshest available frame and stale ones are simply overwritten.
    """

    def __init__(self, device: int | str):
        # `device` is either an int cv2 index or a string matched to a camera by
        # name substring / USB VID:PID (see camera.resolve_camera_index). -1 =
        # system default camera: OpenCV has no portable "default" sentinel of its
        # own (passing -1 errors with "out device of bound"), but index 0 is the
        # platform default on every backend we target — AVFoundation, V4L2,
        # DSHOW, MSMF. Mirror the audio convention where negative = default.
        #
        # A string-resolved device also carries the backend it was enumerated
        # against (the enumerated index is only valid for that apiPreference); an
        # int device resolves to backend=None so we keep the historical
        # single-arg CAP_ANY open, byte-identical for existing configs.
        from . import camera

        index, backend = camera.resolve_camera_index(device)
        self.cap: cv2.VideoCapture | None = (
            cv2.VideoCapture(index, backend) if backend is not None else cv2.VideoCapture(index)
        )
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open video device {device!r} (cv2 index {index})")
        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None
        # manual=True: the grab loop blocks in cap.read() at the device frame
        # rate, so it paces itself — no fixed period needed.
        self._poll = PollThread(self._grab_loop, name="webcam-grab", manual=True, join_timeout=1.0)
        self._poll.start()

    def _grab_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            cap = self.cap
            if cap is None:
                break
            ok, img = cap.read()
            if not ok:
                # Transient read failure (device hiccup): keep the last good
                # frame, back off briefly so we don't spin a hot loop.
                stop.wait(0.01)
                continue
            with self._lock:
                self._latest = img

    def read(self) -> np.ndarray | None:
        """Return an independent copy of the most recent grabbed frame.

        Copies so concurrent consumers can't race the grab thread's in-place
        overwrite or each other's downstream mutations (flip/crop/smoothing)."""
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def release(self):
        self._poll.stop()
        with self._lock:
            self._latest = None
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class AVFileSource:
    """PyAV-backed demuxer with shared PTS."""

    def __init__(
        self,
        path: str,
        target_sample_rate: int,
        max_video_buffer: int = 240,
        source_noise_gate_enabled: bool = False,
        scan_audio_peak: bool = True,
        start_s: float = 0.0,
        decode_target_size: tuple[int, int] | None = None,
        tempo_scale: float = 1.0,
    ):
        if not _ensure_pyav():
            raise RuntimeError("PyAV not installed; install with `pip install c64cast[video]`")

        self.path = path
        self.target_sr = target_sample_rate
        self.max_video_buffer = max_video_buffer
        # The display mode's frame_target_size (or None). When set, the demux
        # loop downscales each frame to a small headroom multiple of it DURING
        # the yuv→bgr swscale pass instead of converting the full source frame
        # — the supply-side fix for video lagging audio on heavy/4K clips (the
        # full-res convert+resize alone overran the per-frame budget). The
        # actual decode size is planned once from the first frame's real
        # dimensions (see _demux_loop + _plan_decode_size).
        self._decode_target = decode_target_size
        self._decode_size: tuple[int, int] | None = None
        self._decode_planned = False
        # PTS (rebased, seconds) of the frame current_frame() last returned —
        # the A/V-lag telemetry in VideoScene reads it as displayed_frame_pts.
        self.last_frame_pts: float = 0.0
        # Seconds into the source to begin playback (0 = from the start). When
        # set, the container is sought to the keyframe at/just-before this time
        # and frame PTS are rebased to ~0 (see _pts_offset + _demux_loop) so the
        # from-0 playback clock in VideoScene still lines up.
        self.start_s = max(0.0, start_s)
        # Captured from the first decoded video frame so every later frame's PTS
        # can be rebased to a ~0 origin. None until that first frame arrives.
        # After a transport seek this is re-derived so the rebased PTS domain
        # lands on the seek target instead of 0 (see request_seek).
        self._pts_offset: float | None = None
        # Where the NEXT self._pts_offset derivation should rebase to: 0.0 for
        # ordinary start-of-file playback, or a transport seek's target_s once
        # one has landed (request_seek/_apply_pending_seek). See the PTS-rebase
        # comment in _demux_loop for the arithmetic.
        self._pts_anchor_target: float = 0.0
        # Transport (MIDI live-tune Phase 2). Guarded by self._lock alongside
        # _video_buf: a pending seek is a (target_s) float or None; the demux
        # thread consumes it at the top of the packet loop and inside the
        # backpressure wait. set_muted latches audio off permanently once a
        # scene's transport is touched (VideoScene._touch_transport).
        self._pending_seek: float | None = None
        self._muted = False

        self.container = _av_open(path)
        self.v_stream = self.container.streams.video[0]
        self.a_stream = self.container.streams.audio[0] if self.container.streams.audio else None

        self.video_fps = float(self.v_stream.average_rate) if self.v_stream.average_rate else 30.0
        self.video_time_base = float(self.v_stream.time_base or 0)
        # Transport (Phase 2): source duration in seconds, for absolute-jog
        # mapping and seek/loop clamping. None if the container doesn't
        # report one (some streams/formats omit it).
        self.duration_s: float | None = (
            self.container.duration / 1_000_000 if self.container.duration else None
        )

        # Seek before any demux so there's no decoder state to flush. Whole-
        # container seek in AV_TIME_BASE units (microseconds); backward=True
        # (the default) lands on the keyframe <= target, so playback starts at
        # most one GOP early. Audio packets are interleaved near the same byte
        # offset, so A/V stay aligned once video PTS are rebased.
        if self.start_s > 0:
            self.container.seek(int(self.start_s * 1_000_000))
            log.info("av %s: seek to start_s=%.3fs", os.path.basename(self.path), self.start_s)

        if self.a_stream is not None:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=target_sample_rate
            )
        else:
            self._resampler = None

        # Bitmap + $D418-DAC tempo compensation. On the host-DMA 4-bit DAC path
        # over a bitmap display mode, heavy REU bank-swap bitmap writes bias the
        # audio servo and time-stretch playback ~1/tempo_scale SLOW at correct
        # pitch (a pitch-preserving stretch — the ring under-fills and the NMI
        # re-reads samples). We pre-compress the content by the inverse factor so
        # the system's own stretch nets to real time: audio time-compressed
        # pitch-preserving by 1/tempo_scale via an `atempo` filter graph (fed the
        # existing s16/mono/target_sr resampler output), and video PTS × tempo_
        # scale (in _demux_loop). tempo_scale == 1.0 (sampler / REU pump / char /
        # muted, per config.build_scene's gate) is a no-op — no graph, PTS
        # untouched. atempo spans 0.5..2.0/stage; validate_dac_bitmap_tempo_cfg
        # keeps tempo_scale ≥ 0.5 so 1/tempo_scale ≤ 2.0 fits one stage.
        self._tempo_scale = tempo_scale
        # av.filter.Graph when tempo compensation is active, else None. Typed
        # Any because `av` is a lazily-imported module-global (`av: Any`), so
        # `av.filter.Graph` isn't usable as a static type here.
        self._atempo_graph: Any = None
        if self.a_stream is not None and tempo_scale < 1.0:
            self._atempo_graph = _build_atempo_graph(target_sample_rate, tempo_scale)
            log.info(
                "av %s: bitmap+DAC tempo compensation ON (s=%.4f → atempo=%.4f)",
                os.path.basename(self.path),
                tempo_scale,
                1.0 / tempo_scale,
            )

        # PTS-sorted decoded video frames: (pts_seconds, BGR np.ndarray)
        self._video_buf: list[tuple[float, np.ndarray]] = []
        self._lock = threading.Lock()
        self._eof = False
        self._closed = False
        self._demux_thread: threading.Thread | None = None
        self._audio_push: Callable[[np.ndarray], None] | None = None

        # Audio peak-normalization. Pre-scan the audio so each clip plays at
        # a consistent level — the 4-bit SID DAC has no usable dynamic range
        # for sources peaking below ~30% of int16 full scale. Unity gain when
        # there's no audio stream or the scan fails.
        self.audio_gain: float = 1.0
        # Pre-gain noise gate threshold. Any input |sample| below this would
        # round to NEUTRAL_SAMPLE after gain + encode in the DITHER-ON path,
        # where the dither would otherwise paint hiss across the noise floor
        # the gain has amplified. With dither off (now the default), there's
        # no jitter to suppress and this hard per-sample gate becomes pure
        # damage — every zero-crossing of a quiet tone gets sliced out,
        # leaving the waveform punched full of holes. User-perceived result:
        # "many short segments stitched together." Disabled by default;
        # callers pass source_noise_gate_enabled=True to re-enable when
        # pairing with dither-on. Zero = no gate.
        self.audio_noise_gate: int = 0
        # The peak scan is a full end-to-end audio decode (~0.5s on a 2.5-min
        # clip) whose only product is audio_gain. Skip it entirely when the
        # caller won't push audio (muted scene) or computes its own gain (the
        # REU pre-encode path) — `scan_audio_peak=False` keeps gain at unity
        # and removes that decode from scene setup, shaving the startup pause.
        if self.a_stream is not None and scan_audio_peak:
            peak = self._scan_audio_peak()
            self.audio_gain = _compute_normalization_gain(peak)
            if source_noise_gate_enabled and self.audio_gain > 1.0:
                self.audio_noise_gate = int(NEUTRAL_BAND_INT16 / self.audio_gain)
            gate_str = f"±{self.audio_noise_gate}" if self.audio_noise_gate else "off"
            log.info(
                "av %s: audio peak=%d → gain=%.2fx, noise gate %s",
                os.path.basename(self.path),
                peak,
                self.audio_gain,
                gate_str,
            )

    def _scan_audio_peak(self) -> int:
        """Decode the audio stream end-to-end via a throwaway container +
        resampler (the main one is positioned for playback) and return the
        peak abs int16 value across all samples. Returns 0 if the stream is
        empty or decoding fails — caller treats 0 as "no normalization."

        Cost: one extra full-decode of audio packets per scene setup,
        typically <1 s for a 60 s video. The playlist's interstitial
        already gives us several seconds of cover before a video paints
        its first frame."""
        peak = 0
        try:
            container = _av_open(self.path)
            try:
                a_stream = container.streams.audio[0]
                # Match the played portion: with a start_s seek, normalize over
                # [start_s, end] only — both more correct (gain reflects what's
                # heard) and cheaper (no decoding the skipped head on long clips).
                if self.start_s > 0:
                    container.seek(int(self.start_s * 1_000_000))
                resampler = av.AudioResampler(format="s16", layout="mono", rate=self.target_sr)
                for packet in container.demux(a_stream):
                    for frame in packet.decode():
                        for resampled in resampler.resample(frame):
                            arr = resampled.to_ndarray().reshape(-1)
                            if arr.size:
                                local_peak = int(np.abs(arr).max())
                                if local_peak > peak:
                                    peak = local_peak
            finally:
                container.close()
        except Exception:
            log.exception("av %s: audio peak scan failed; using unity gain", self.path)
            return 0
        return peak

    def start(self, audio_push: Callable[[np.ndarray], None] | None):
        """Start the demuxer thread. ``audio_push=None`` skips audio decode
        entirely — used by the REU-staged audio path where the soundtrack
        has already been pre-decoded into REU and the demuxer shouldn't
        waste CPU decoding + resampling audio just to discard it."""
        self._audio_push = audio_push
        self._demux_thread = threading.Thread(target=self._demux_loop, daemon=True, name="av-demux")
        self._demux_thread.start()

    def request_seek(self, target_s: float) -> None:
        """Ask the demux thread to seek to `target_s` (absolute seconds from
        file start) at its next opportunity. Coalescing is natural: rapid
        repeated calls (RW/FF ticking, jog) just overwrite the single pending
        slot — the demux thread performs however many real seeks it has
        cycles for. Clears the buffered (stale, pre-seek) frames immediately
        so a caller reading `current_frame` right after doesn't get one."""
        target_s = max(0.0, target_s)
        with self._lock:
            self._pending_seek = target_s
            self._video_buf.clear()

    def set_muted(self, muted: bool) -> None:
        """Latch (or unlatch) audio output. While muted, `_emit_audio` drops
        every packet before it reaches the consumer — nothing already queued
        downstream (AudioStreamer / UltimateAudioSampler) is retracted."""
        self._muted = muted

    @property
    def seek_pending(self) -> bool:
        """True while a requested transport seek has not yet been applied by the
        demux thread. VideoScene's resync loop-wrap uses this to avoid re-firing
        transport_seek(A) every frame (each re-fire would flush the first fresh
        post-A audio) until the demuxer clears the pending slot."""
        with self._lock:
            return self._pending_seek is not None

    def _emit_audio(self, arr: np.ndarray) -> None:
        """Apply the noise gate + normalization gain to a mono int16 sample
        array and hand it to the audio consumer. Shared by the direct path and
        the atempo-compensated path."""
        # Drop audio decoded from the stale pre-seek read position while a seek
        # is pending — otherwise it reaches the consumer and plays after the
        # splice's flush. The unlocked _pending_seek read is racy but benign: the
        # AudioStreamer/sampler flush epoch closes the residual one-blob window
        # (a chunk that slips through here right as the seek lands is discarded
        # consumer-side by the epoch check).
        if self._audio_push is None or self._muted or self._pending_seek is not None:
            return
        if self.audio_noise_gate > 0:
            # Zero source-noise-floor samples BEFORE gain so the encoder doesn't
            # jitter between NEUTRAL and ±1 at amplified noise levels.
            arr = np.where(np.abs(arr) < self.audio_noise_gate, np.int16(0), arr)
        if self.audio_gain != 1.0:
            arr = np.clip(arr.astype(np.float32) * self.audio_gain, INT16_MIN, INT16_MAX).astype(
                np.int16
            )
        self._audio_push(arr.astype(np.int16, copy=False))

    def _drain_atempo(self) -> None:
        """Pull every time-compressed frame the atempo graph can currently
        produce and emit it. BlockingIOError = "no frame ready yet" (need more
        input); EOFError = graph fully drained after the EOS push."""
        assert self._atempo_graph is not None
        while True:
            try:
                out = self._atempo_graph.pull()
            except (av.error.BlockingIOError, av.error.EOFError):
                return
            self._emit_audio(out.to_ndarray().reshape(-1))

    def _flush_atempo(self) -> None:
        """At EOF, signal end-of-stream to the atempo graph and drain the
        compressed tail still buffered in the filter (otherwise the last
        fraction of a second is lost). No-op when tempo compensation is off, the
        consumer has gone away, or the scene was torn down mid-stream."""
        if self._atempo_graph is None or self._audio_push is None or self._closed:
            return
        try:
            self._atempo_graph.push(None)
            self._drain_atempo()
        except (av.error.EOFError, av.error.BlockingIOError):
            pass

    def _apply_pending_seek(self) -> bool:
        """Demux-thread-only: if a transport seek is pending, perform it —
        re-seek the container, rebuild per-seek decoder state (resampler,
        atempo graph), and re-anchor the PTS rebase so the next frame's PTS
        lands on the requested target (design decision 2 of the transport
        plan: the clock IS file position once transport is touched — no
        separate file_offset_s bookkeeping). Returns True if a seek was
        applied."""
        with self._lock:
            target = self._pending_seek
            self._pending_seek = None
        if target is None:
            return False
        self.container.seek(int(target * 1_000_000))
        if self.a_stream is not None:
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=self.target_sr)
        if self._atempo_graph is not None:
            self._atempo_graph = _build_atempo_graph(self.target_sr, self._tempo_scale)
        self._eof = False
        self._pts_offset = None
        self._pts_anchor_target = target
        log.info("av %s: transport seek to %.3fs", os.path.basename(self.path), target)
        return True

    def _demux_loop(self):
        # Differentiate "container hit EOF" (expected, info) from "decode blew
        # up mid-stream" (unexpected, log full traceback).
        try:
            for packet in self.container.demux():
                if self._closed:
                    return
                if self._apply_pending_seek():
                    continue
                if packet.stream.type == "video":
                    for frame in packet.decode():
                        # Plan the decode downscale once, from the first frame's
                        # real dimensions. _decode_size None = source already
                        # small enough (or no target) → plain full-res convert.
                        if not self._decode_planned:
                            self._decode_planned = True
                            if self._decode_target is not None:
                                self._decode_size = _plan_decode_size(
                                    frame.width, frame.height, *self._decode_target
                                )
                                if self._decode_size is not None:
                                    log.info(
                                        "av %s: decoding %dx%d→%dx%d (display target %dx%d)",
                                        os.path.basename(self.path),
                                        frame.width,
                                        frame.height,
                                        self._decode_size[0],
                                        self._decode_size[1],
                                        self._decode_target[0],
                                        self._decode_target[1],
                                    )
                        if self._decode_size is not None:
                            # yuv→bgr + downscale in one swscale pass (cheap),
                            # vs a full-res bgr buffer + a separate cv2.resize.
                            img = frame.reformat(
                                width=self._decode_size[0],
                                height=self._decode_size[1],
                                format="bgr24",
                            ).to_ndarray()
                        else:
                            img = frame.to_ndarray(format="bgr24")
                        pts = (
                            float(frame.pts * self.video_time_base)
                            if frame.pts is not None
                            else 0.0
                        )
                        # Rebase PTS so the first decoded frame sits at
                        # ~_pts_anchor_target (0.0 for ordinary start_s
                        # playback; a transport seek's target_s once one has
                        # landed — see _apply_pending_seek). With a start_s
                        # seek the raw PTS are ~start_s; the playback clock
                        # (audio samples / wall-clock) starts at 0, so without
                        # this current_frame() would find no frame <= 0 for
                        # start_s seconds. Offset is captured from the first
                        # frame (the keyframe the seek landed on), so the
                        # no-transport-seek path is unchanged (anchor 0.0,
                        # offset == first PTS, rebased ~0).
                        if self._pts_offset is None:
                            self._pts_offset = pts - self._pts_anchor_target
                        pts -= self._pts_offset
                        # Bitmap+DAC tempo compensation: compress the video
                        # timeline by tempo_scale so it stays in lock-step with
                        # the 1/tempo_scale-compressed audio (both then net to
                        # real time under the ~tempo_scale drain-clock slowdown).
                        # No-op when tempo_scale == 1.0.
                        if self._tempo_scale != 1.0:
                            pts *= self._tempo_scale
                        # Backpressure: wait if the buffer is at capacity.
                        # The old behavior (silent-drop oldest frames) was a
                        # safety net under host-DMA mode, where AudioStreamer's
                        # push_samples blocking throttle keeps the demuxer at
                        # real-time and the buffer never filled in practice.
                        # In REU mode there's no audio backpressure (audio is
                        # pre-decoded and lives in REU), so the demuxer would
                        # race ahead, fill the buffer, and start dropping the
                        # EARLIEST frames — leaving current_frame() with no
                        # frames at PTS ≤ the audio clock for several seconds.
                        # User-visible symptom: video freezes early in playback
                        # for a long time, then "catches up" near the end.
                        # Blocking the demuxer until consumer drains is correct
                        # in both modes; host-DMA just doesn't hit the wait.
                        while True:
                            if self._closed:
                                return
                            with self._lock:
                                if self._pending_seek is not None:
                                    # A seek landed while we were blocked on a
                                    # full buffer — this decoded frame predates
                                    # it and would corrupt the post-seek buffer.
                                    # Abandon it; the outer loop's top-of-packet
                                    # check applies the seek on the next packet.
                                    break
                                if len(self._video_buf) < self.max_video_buffer:
                                    self._video_buf.append((pts, img))
                                    break
                            time.sleep(0.005)
                elif (
                    packet.stream.type == "audio"
                    and self._resampler is not None
                    and self._audio_push is not None
                ):
                    for frame in packet.decode():
                        for resampled in self._resampler.resample(frame):
                            if self._atempo_graph is not None:
                                # Time-compress (pitch-preserving) through the
                                # atempo graph before emit. The graph buffers, so
                                # one input frame yields 0..N output frames.
                                self._atempo_graph.push(resampled)
                                self._drain_atempo()
                            else:
                                self._emit_audio(resampled.to_ndarray().reshape(-1))
            self._flush_atempo()
            log.debug("demux %s: EOF", self.path)
        except (EOFError, StopIteration):
            self._flush_atempo()
            log.debug("demux %s: EOF", self.path)
        except Exception:
            log.exception("demux %s crashed", self.path)
        finally:
            self._eof = True

    def current_frame(self, audio_position_s: float) -> np.ndarray | None:
        """Return the latest video frame whose PTS ≤ audio_position_s.

        Returns None if no frame is ready yet (still pre-rolling).
        Frames at or behind the chosen one are dropped from the buffer.
        """
        with self._lock:
            if not self._video_buf:
                return None
            chosen_img: np.ndarray | None = None
            chosen_pts = 0.0
            consumed_through = -1
            for i, (pts, img) in enumerate(self._video_buf):
                if pts <= audio_position_s:
                    chosen_img = img
                    chosen_pts = pts
                    consumed_through = i
                else:
                    break
            if chosen_img is None:
                return None
            # Telemetry: the displayed frame's PTS. VideoScene logs
            # audio_position_s - last_frame_pts as the A/V lag — small +
            # (≤ one frame interval) is healthy; a growing + means the decoder
            # is falling behind the audio-master clock (the 4K-decode-bound
            # symptom). See VideoScene.process_frame / project_av_sync_decode_bound.
            self.last_frame_pts = chosen_pts
            # Normally we keep the chosen frame in the buffer so a clock
            # stall doesn't black-frame the display ("keep the chosen one
            # in case the clock stalls and we need to re-emit it"). After
            # demux EOF that stall protection becomes a trap: the buffer
            # stays size-1 forever, `finished` never fires, the scene
            # never ends, and the audio worker pads NEUTRAL indefinitely.
            # When EOF has been observed AND we've consumed through the
            # last buffered frame, drain it too so `finished` can flip.
            if self._eof and consumed_through == len(self._video_buf) - 1:
                self._video_buf.clear()
            elif consumed_through > 0:
                del self._video_buf[:consumed_through]
            return chosen_img

    @property
    def video_buffer_depth(self) -> int:
        """Number of decoded frames waiting ahead of the consumer. Read by the
        A/V-lag telemetry: a depth that stays near 0 while the lag grows is the
        decoder-can't-keep-up signature."""
        with self._lock:
            return len(self._video_buf)

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._eof and not self._video_buf

    def close(self) -> None:
        self._closed = True
        if self._demux_thread:
            self._demux_thread.join(timeout=1.0)
            self._demux_thread = None
        try:
            self.container.close()
        except Exception as e:
            log.debug("container close: %s", e)
