"""Video input sources.

* `WebcamSource` -- always-on shared camera broker: one background grab thread
  owns the VideoCapture and hands copies of the latest frame to any number of
  consumers (the webcam scene + the vision controller).
* `AVFileSource` -- PyAV demuxer for file playback. Splits one container into
  audio (pushed straight through to AudioStreamer) and video (queued with PTS).
  Consumers select the next video frame by passing the current audio clock
  position to `current_frame()`, which drops anything behind and returns the
  newest frame whose PTS is ≤ the clock. This is the audio-master sync model.

See docs/architecture/video-color.md#videopy--webcamsource-shared-broker--avfilesource-pyav.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar

import cv2
import numpy as np

from c64cast._native_io import silence_native_stderr
from c64cast._pollthread import PollThread
from c64cast.audio.audio_handlers import DAC_VOLUME_SCALE, INT16_FULL_SCALE, INT16_MAX, INT16_MIN

from .palette import ColorFit, ColorFitAccumulator, ColorMap, ColorMapAccumulator, FrameSampler

_T = TypeVar("_T")

# Audio is fed toward the sink on the picture's timeline: a packet that starts
# later than the audio fed so far is preceded by silence, and one that starts
# earlier is trimmed. Within this much either way it is fed as it is, so the
# few samples a resampler holds back or a muxer rounds do not split a packet.
AUDIO_ALIGN_TOLERANCE_S = 0.03
# An audio timestamp this far past the newest picture read, or this far behind
# the audio fed, is a discontinuity in the file's timestamps rather than a
# silence it means: the frame follows on and the rest of the pass is shifted
# by the jump. Unbounded, one packet stamped hours ahead fed that much silence
# at the sink's real-time pace, and the scene never ended; one stamped behind
# had every later frame trimmed whole. A muxer interleaves its streams well
# inside this, while a sound that starts late or comes back after a gap has
# its picture read up to it first, so only a stamp the picture never reaches
# trips it. Measured from the picture rather than from the audio fed, so a
# file that steps each packet a little under the bound past the last still
# never has its silence take the audio fed more than the bound past the
# picture.
AUDIO_DISCONTINUITY_S = 30.0
# A full video buffer with no audio coming is fed silence up to its newest
# frame less this much, or less the furthest the file has been seen to write
# audio behind its picture if that is more: audio for anything earlier would
# already have been read.
DRY_FILL_INTERLEAVE_S = 0.5
# ... and, once the oldest frame has waited `DRY_FILL_STALL_S` for the clock,
# never less than this far past it. Each sink holds audio back before its
# clock moves (the DAC's 6 x 1024 B prebuffer plus a ring lead of at most
# 4096 B: 0.85 s at its 12 kHz default, 1.28 s at 8 kHz; the sampler's 1.0 s
# lead), so in a buffer spanning less than that plus the allowance, a
# fill short of the newest frame never brings the clock to the oldest. Only a
# stall takes this lead: a file that writes its audio late in a short buffer
# otherwise has it covered by silence and trimmed.
DRY_FILL_MIN_LEAD_S = 1.5
DRY_FILL_STALL_S = 1.0
# Silence goes out in pieces no larger than this, so each one fits the sink's
# backpressure bound and a seek or close is seen between them.
SILENCE_PIECE_SAMPLES = 1024


def place_audio_frame(
    start_s: float, duration_s: float, fed_s: float, rate: int
) -> tuple[int, int, float]:
    """Where a decoded audio frame starting at ``start_s`` and lasting
    ``duration_s`` goes, given audio fed so far up to ``fed_s`` (seconds on
    one timeline) at ``rate`` samples per second: the silence to feed ahead
    of it and the samples to drop from its front, both counted at ``rate``,
    and where the audio fed then ends.

    That end advances by what is fed, never to the frame's own timestamp: a
    frame within `AUDIO_ALIGN_TOLERANCE_S` follows on, and its gap stays in
    the next frame's, so gaps and overlaps each too small to correct add up
    until one is corrected rather than drifting the sound from its picture
    without bound."""
    gap = start_s - fed_s
    if gap > AUDIO_ALIGN_TOLERANCE_S:
        silence = round(gap * rate)
        return silence, 0, fed_s + silence / rate + duration_s
    if gap < -AUDIO_ALIGN_TOLERANCE_S:
        trim = round(min(-gap, duration_s) * rate)
        return 0, trim, fed_s + duration_s - trim / rate
    return 0, 0, fed_s + duration_s


log = logging.getLogger(__name__)

# Peak-normalization for video-scene audio. The SID volume DAC is 4-bit and
# `(float + 1) * 7.5` puts samples within ±0.067 of zero on NEUTRAL_SAMPLE, so
# a source peaking below ~30% of int16 full scale plays as silence-with-clicks.
# MAX_GAIN caps the boost so a near-silent file isn't amplified into noise.
NORMALIZATION_TARGET_PEAK = 0.9
NORMALIZATION_MAX_GAIN = 16.0

# Half-width of the NEUTRAL band in int16 sample units: (sample/32768 + 1) * 7.5
# is in [7, 8) iff |sample| < 32768 * 0.5 / 7.5, so any post-gain |sample| below
# this encodes to NEUTRAL_SAMPLE (=7). Pre-gain gate = NEUTRAL_BAND_INT16 / gain.
NEUTRAL_BAND_INT16 = INT16_FULL_SCALE * 0.5 / DAC_VOLUME_SCALE

# FFmpeg http/https protocol reconnect options, applied to remote URLs only
# (`av_open`) because FFmpeg warns about unrecognized options on a local path.
# A CDN drop mid-stream otherwise surfaces as `OSError: [Errno 5] Input/output
# error` out of `container.demux()` and ends playback.
#   reconnect                  reconnect on connection close / EOF
#   reconnect_streamed         reconnect for non-seekable (streamed) inputs
#   reconnect_on_network_error reconnect on any network error mid-stream
#   reconnect_delay_max        cap the exponential backoff (seconds)
_HTTP_RECONNECT_OPTIONS = {
    "reconnect": "1",
    "reconnect_streamed": "1",
    "reconnect_on_network_error": "1",
    "reconnect_delay_max": "5",
}

# Remote inputs only, passed as PyAV's `timeout=(open, read)`. Without them a
# server that accepts the connection and then stops answering blocks the
# opening thread forever: FFmpeg's own socket timeouts default to none, and
# the reconnect options above fire on an error or EOF, never on silence. The
# read bound applies per blocking read, so a slow-but-flowing stream is fine,
# and it sits well above the reconnect backoff cap so a reconnect completes.
_REMOTE_OPEN_TIMEOUT_S = 20.0
_REMOTE_READ_TIMEOUT_S = 30.0


def _protocol_options(path: str) -> dict[str, str]:
    """FFmpeg's own per-IO ``rw_timeout`` (microseconds) for a network input.

    PyAV's bound is an interrupt callback it arms only inside ``demux()``, so
    a seek `_seek` abandons has no bound of its own and would hold its
    worker, socket and container for as long as the server holds the
    connection. Twice the read bound, so inside ``demux()`` PyAV's bound still
    fires first and playback behaves as before.

    The RTSP demuxer opens its control connection without the caller's
    protocol options, so ``rw_timeout`` never reaches it; it takes the same
    bound as its own ``timeout`` option instead."""
    bound = str(int(_REMOTE_READ_TIMEOUT_S * 2 * 1_000_000))
    options = {"rw_timeout": bound}
    if path.lower().startswith(("rtsp://", "rtsps://")):
        options["timeout"] = bound
    return options


def _is_remote_url(path: str) -> bool:
    """True for http(s) inputs, which get the FFmpeg reconnect options."""
    return path.startswith(("http://", "https://"))


# FFmpeg's own protocol test (`url_find_protocol`): a run of these characters
# followed by `:` names a protocol, except a single letter, which is a DOS
# drive. Everything else, and an explicit `file:`, opens as a local file.
_FFMPEG_PROTOCOL_PREFIX = re.compile(r"^([A-Za-z0-9+.-]+):")


def _is_local_file(path: str) -> bool:
    """True when FFmpeg opens `path` with its `file` protocol.

    The open/read bound is keyed to this rather than to `_is_remote_url`
    because http(s) is not the only network protocol FFmpeg honors: an
    audio-file entry such as ``tcp://host:port/x.wav`` or ``rtsp://…`` passes
    `resolve_file_spec` on its extension alone, has no existence check, and
    blocks on a silent peer exactly as http does. Anything this does not
    recognize as local is bounded, which is the safe direction."""
    m = _FFMPEG_PROTOCOL_PREFIX.match(path)
    return m is None or len(m.group(1)) == 1 or m.group(1) == "file"


def _remote_refusal_message(e: Any) -> str:
    """The operator-facing text for a remote 4xx, carrying no URL.

    ``str(e)`` cannot be used here, which is the whole reason this is a
    function: PyAV appends the filename it was opening to an ``FFmpegError``'s
    string form, so interpolating the exception quotes the signed URL —
    signature, token and all — into a message that gets logged and shown.
    ``strerror`` is the same explanatory text with the filename left off.

    The expiry advice is limited to the two statuses a stale signature
    actually answers with. A 404 is a pulled video and a 429 is rate limiting;
    sending the operator to reload the playlist for either points them at the
    wrong thing."""
    detail = e.strerror or type(e).__name__
    if not isinstance(e, (av.error.HTTPUnauthorizedError, av.error.HTTPForbiddenError)):
        return f"the media server refused this stream ({detail})."
    return (
        f"the media server refused this stream ({detail}). For a URL resolved from a "
        "page (YouTube and friends), an expired signature is the likeliest cause: the "
        "stream URL is resolved once when the playlist is built, and a long show "
        "replays it. Reload the playlist to re-resolve it — SIGHUP, or POST /reload "
        "on the control plane."
    )


def av_open(path: str):
    """`av.open` wrapper that injects the HTTP reconnect options for remote
    URLs so a transient CDN drop mid-stream resumes instead of crashing the
    demuxer, and bounds the open and every later demux read
    (`_REMOTE_OPEN_TIMEOUT_S` / `_REMOTE_READ_TIMEOUT_S`) so a stalled server
    raises instead of hanging the playlist. Any other network protocol gets
    the same bound without the http-only reconnect options; local files
    (`_is_local_file`) open unchanged.

    A remote 4xx is re-raised naming the likeliest cause, because the raw
    ``HTTPForbiddenError`` is unreadable on the one shape it usually means.
    `scene_factory._resolve_video_source` resolves a page URL through yt-dlp
    at **build** time and `scenes_from_config` runs once at startup, so a
    playlist holds whatever signed stream URL it got then and replays it on
    every loop pass — and those signatures expire (YouTube's in a few hours).
    A looping show that ran fine all afternoon starts 403ing, with nothing
    saying why. `SIGHUP` reloads the playlist, which rebuilds the scenes and
    re-resolves the URL, so there is a remedy worth naming.

    The wording is built by :func:`_remote_refusal_message`, which is where
    the URL is kept out of it."""
    if _is_local_file(path):
        return av.open(path)
    if not _is_remote_url(path):
        # A non-http network protocol: no reconnect options (they are
        # http-only), but the same bound on a peer that goes silent.
        return av.open(
            path,
            options=_protocol_options(path),
            timeout=(_REMOTE_OPEN_TIMEOUT_S, _REMOTE_READ_TIMEOUT_S),
        )
    try:
        return av.open(
            path,
            options={**_HTTP_RECONNECT_OPTIONS, **_protocol_options(path)},
            timeout=(_REMOTE_OPEN_TIMEOUT_S, _REMOTE_READ_TIMEOUT_S),
        )
    except av.error.HTTPClientError as e:
        raise RuntimeError(_remote_refusal_message(e)) from e


class RemoteSeekStalled(RuntimeError):
    """A seek on a network input got no answer within the read bound.

    The container's close now waits on the abandoned seek: the worker still
    inside FFmpeg closes it once the seek returns and the owner has released
    it — closing it any sooner would free the context that read is using."""


class _ContainerCloser:
    """Closes a container once its owner has released it and no `_seek`
    worker is inside it, whichever comes last.

    An owner whose close can run while a seek is in flight on another thread
    (`AVFileSource.close` against the demux thread's transport seek) releases
    through this rather than closing directly, because that seek can outlive
    any join the owner is willing to wait for."""

    def __init__(self, container: Any) -> None:
        self._container = container
        self._lock = threading.Lock()
        self._seeks = 0
        self._released = False
        self._closed = False

    def enter(self) -> None:
        with self._lock:
            self._seeks += 1

    def leave(self) -> None:
        with self._lock:
            self._seeks -= 1
        self._close_if_done()

    def release(self) -> None:
        with self._lock:
            self._released = True
        self._close_if_done()

    def _close_if_done(self) -> None:
        with self._lock:
            if self._closed or not self._released or self._seeks:
                return
            self._closed = True
        try:
            self._container.close()
        except Exception as e:
            log.debug("container close: %s", e)


def _seek(
    container: Any,
    path: str,
    offset: int,
    closer: _ContainerCloser | None = None,
    **kwargs: Any,
) -> None:
    """``container.seek`` bounded by `_REMOTE_READ_TIMEOUT_S` for any input
    `av_open` bounds (see `_is_local_file`), raising `RemoteSeekStalled` past
    it.

    PyAV arms its read bound only while a ``demux()`` generator is live, and
    times a seek inside one against that generator's last read, so the bound
    cannot come from PyAV: a seek outside one waits on a server that stops
    answering the range request it opens until FFmpeg's own IO timeout
    (`_protocol_options`, twice the read bound per IO, retried by the
    reconnect options on an http(s) input) gives up, and one inside a generator
    held open by a paused scene fails at once against a healthy server. The
    seek runs on a worker instead, which the caller abandons on a stall.

    ``closer`` is the owner's `_ContainerCloser` when another thread may close
    the container while this seek runs; the owner then releases it as usual.
    Without one, a stall releases the container here, so the caller must not
    touch it again."""
    if _is_local_file(path):
        container.seek(offset, **kwargs)
        return
    owned = closer is None
    guard = closer if closer is not None else _ContainerCloser(container)
    outcome: list[BaseException | None] = []
    guard.enter()

    def run() -> None:
        try:
            container.seek(offset, **kwargs)
            result: BaseException | None = None
        except BaseException as e:  # noqa: BLE001 — handed to the caller
            result = e
        outcome.append(result)
        guard.leave()

    worker = threading.Thread(target=run, name="av-seek", daemon=True)
    worker.start()
    worker.join(_REMOTE_READ_TIMEOUT_S)
    if not outcome:
        if owned:
            guard.release()
        raise RemoteSeekStalled(
            f"the media server did not answer a seek within {_REMOTE_READ_TIMEOUT_S:g} s"
        )
    if outcome[0] is not None:
        raise outcome[0]


def probe_container_title(path: str) -> str | None:
    """A cheap, header-only peek at a local file's own `title` tag (PyAV
    parses container metadata without decoding any frames) — lets
    VideoScene prefer a file's real name over its bare filename on the
    "UP NEXT" interstitial card, which is built from `prepare_next()`
    before `setup()` opens the file for real playback.

    Local files only — a URL's real title already comes from yt-dlp,
    resolved before the scene even exists (see
    scene_factory._resolve_video_source); probing a remote stream here would
    add real network I/O just to pick a display name. Returns None on
    anything going wrong (missing/corrupt/unsupported file, PyAV
    unavailable) — that's setup()'s problem to report moments later, not
    this best-effort peek's."""
    if _is_remote_url(path) or not ensure_pyav() or not os.path.exists(path):
        return None
    try:
        container = av_open(path)
    except Exception:
        return None
    try:
        title = container.metadata.get("title")
    except Exception:
        return None
    finally:
        container.close()
    return title.strip() if title and title.strip() else None


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
# kept, in each axis, after the center-crop, so the mode's INTER_AREA resize
# stays a pure downscale with area-averaging headroom. Decode is capped at the
# source size regardless.
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
    # Even dimensions: chroma-subsampled source codecs want them and swscale
    # warns otherwise.
    dw = max(2, round(src_w * scale / 2) * 2)
    dh = max(2, round(src_h * scale / 2) * 2)
    return dw, dh


# PyAV is imported lazily on first AVFileSource construction. On macOS the av
# wheel bundles a different libavdevice major version than the cv2 wheel, so as
# av's libavdevice loads on top of cv2's the Obj-C runtime prints a one-time
# "Class AVFFrameReceiver/AVFAudioReceiver is implemented in both ..." warning
# to fd 2 — duplicated AVFoundation device classes, harmless here since neither
# file-decode path touches the avfoundation input device. Hence the
# silence_native_stderr wrapper on the import.
av: Any = None
PYAV_AVAILABLE: bool | None = None  # tri-state: None = not yet probed


def ensure_pyav() -> bool:
    """Import PyAV on demand; cache the result. Returns availability."""
    global av, PYAV_AVAILABLE
    if PYAV_AVAILABLE is not None:
        return PYAV_AVAILABLE
    try:
        # fd-level mute: the objc warning is printed by the runtime as the
        # native libs load, below Python.
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
    single-stage 0.5..2.0 range. Requires PyAV (`ensure_pyav()` first)."""
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


def decode_audio_full(
    path: str,
    target_sample_rate: int,
    *,
    origin_s: float | None = None,
    max_samples: int | None = None,
) -> np.ndarray:
    """Decode the entire audio track of ``path`` to mono int16 at
    ``target_sample_rate``. Returns a single contiguous np.ndarray.

    Each frame is placed on the track's timeline the way the demuxer places
    the audio it feeds (`place_audio_frame`): silence where the file has no
    sound, overlaps trimmed. Sample 0 is ``origin_s`` on the stream
    timestamps — the picture's origin, from
    `AVFileSource.pin_timeline_origin` — or, without one, the first audio
    timestamp.

    ``max_samples`` caps the result, and decoding stops once it is reached:
    the silence a placed frame is preceded by grows with the timestamp a file
    claims, so without a cap a single packet stamped far ahead asks for an
    allocation that size.

    Blocking — call before scene paint starts. Used by the REU-staged audio
    path in VideoScene where the whole track must be preloaded into
    REU before playback begins.

    Cost: ~100-200 ms for a 30-sec video via PyAV on this hardware.
    Raises RuntimeError if PyAV isn't available or there's no audio stream
    in the container.
    """
    if not ensure_pyav():
        raise RuntimeError(
            "PyAV not installed; install with `uv tool install --force 'c64cast[all]'`"
        )
    container = av_open(path)
    try:
        if not container.streams.audio:
            raise RuntimeError(f"no audio stream in {path}")
        a_stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=target_sample_rate)
        chunks: list[np.ndarray] = []
        room = max_samples if max_samples is not None else sys.maxsize
        fed = 0.0
        trim = 0
        frames = (f for packet in container.demux(a_stream) for f in packet.decode())
        # The trailing None flushes the filter tail the resampler holds back.
        for frame in itertools.chain(frames, [None]):
            if room <= 0:
                break
            if frame is not None:
                rate = frame.sample_rate or target_sample_rate
                duration = frame.samples / rate if rate else 0.0
                start = fed
                if frame.pts is not None and frame.time_base is not None:
                    pts_s = float(frame.pts * frame.time_base)
                    if origin_s is None:
                        origin_s = pts_s
                    start = pts_s - origin_s
                silence, cut, fed = place_audio_frame(start, duration, fed, target_sample_rate)
                trim += cut
                silence = min(silence, room)
                if silence:
                    chunks.append(np.zeros(silence, dtype=np.int16))
                    room -= silence
            for resampled in resampler.resample(frame):
                arr = resampled.to_ndarray().reshape(-1)
                cut = min(trim, arr.size)
                trim -= cut
                arr = arr[cut : cut + room]
                if arr.size:
                    chunks.append(arr.astype(np.int16, copy=False))
                    room -= arr.size
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
    path: str,
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
        # Interval midpoints, so the last target is not at/after EOF, where it
        # can decode to nothing.
        target_s = (n + 0.5) / max_samples * duration_s
        ts = start_time + int(target_s / time_base)
        _seek(container, path, ts, stream=v_stream)  # backward=True (default) → keyframe ≤ ts
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


class _SampleProgressTap:
    """An accumulator that only counts: reports ``n / total`` to ``notify``
    per sampled frame, so a caller can show scan progress without the
    sampling functions knowing anything beyond their accumulator list."""

    def __init__(self, total: int, notify: Callable[[float], None]):
        self._total = max(1, total)
        self._notify = notify
        self._n = 0

    def add(self, img: Any) -> None:
        self._n += 1
        self._notify(self._n / self._total)


def scan_video_samples(
    path: str,
    accumulators: list[Any],
    max_samples: int = 120,
    decode_target_size: tuple[int, int] | None = None,
    on_progress: Callable[[float], None] | None = None,
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

    ``on_progress`` (fraction 0..1) is called per sampled frame. The sequential
    fallback may sample fewer than ``max_samples`` frames, so the fraction can
    end short of 1.0 — callers mark their own completion.
    """
    if not accumulators or not ensure_pyav():
        return False
    if on_progress is not None:
        accumulators = [*accumulators, _SampleProgressTap(max_samples, on_progress)]
    try:
        container = av_open(path)
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
                            path,
                            v_stream,
                            accumulators,
                            max_samples,
                            duration_s,
                            decode_target_size,
                        )
                        > 0
                    )
                except RemoteSeekStalled:
                    # The abandoned seek owns the container now.
                    container = None
                    raise
                except Exception as e:
                    # Non-seekable source: fall through to sequential decode.
                    log.debug(
                        "seek-sampled pre-scan of %s failed (%s); using sequential decode",
                        path,
                        e,
                    )
            if not seeked:
                # Re-open to reset any partial seek state.
                container.close()
                container = av_open(path)
                v_stream = container.streams.video[0]
                v_stream.thread_type = "AUTO"
                _decode_sample_frames(
                    container, v_stream, accumulators, max_samples, decode_target_size
                )
        finally:
            if container is not None:
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
    frames: FrameSampler | None = None,
    decode_target_size: tuple[int, int] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> tuple[ColorFit | None, ColorMap | None]:
    """Pre-scan a video once and derive the enabled per-source color stages.

    ``fit_strength`` not None enables the adaptive ColorFit ([color].auto_fit);
    ``map_colors``/``map_indices`` not None/empty enables the forced-palette
    ColorMap ([color].force_palette). Both stages share a single decode pass,
    and so does ``frames``, which keeps the sampled frames for a derivation that
    needs the fit first ([color].hardware_palette).
    ``decode_target_size`` downscales sampled frames during decode (see
    scan_video_samples). Returns (ColorFit|None, ColorMap|None); a disabled or
    failed stage is None, so callers can unconditionally pass the results to
    set_color_fit / set_color_map. A failed scan also empties ``frames``, so
    nothing is fitted to, or kept from, a partial pass. See
    palette.ColorFitAccumulator / palette.ColorMapAccumulator.
    """
    fit_acc = ColorFitAccumulator(strength=fit_strength) if fit_strength is not None else None
    map_acc = (
        ColorMapAccumulator(n_colors=map_colors or 16, indices=map_indices)
        if (map_colors is not None or map_indices)
        else None
    )
    accs = [a for a in (fit_acc, map_acc, frames) if a is not None]
    if not scan_video_samples(
        path, accs, decode_target_size=decode_target_size, on_progress=on_progress
    ):
        if frames is not None:
            frames.frames.clear()
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
    contention — see [c64cast/control/vision.py](../control/vision.py).

    Returning the latest grabbed frame (rather than blocking for the next one)
    also keeps the live-webcam path low-latency: a consumer always gets the
    freshest available frame and stale ones are simply overwritten.
    """

    def __init__(self, device: int | str):
        # -1 = system default camera, mirroring the audio convention. OpenCV has
        # no portable default sentinel (passing -1 errors with "out device of
        # bound"), but index 0 is the platform default on AVFoundation, V4L2,
        # DSHOW and MSMF alike.
        #
        # A string-resolved device also carries the backend it was enumerated
        # against, since the enumerated index is only valid for that
        # apiPreference; an int device resolves to backend=None and the
        # single-arg CAP_ANY open.
        from c64cast.control import camera

        index, backend = camera.resolve_camera_index(device)
        self.cap: cv2.VideoCapture | None = (
            cv2.VideoCapture(index, backend) if backend is not None else cv2.VideoCapture(index)
        )
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open video device {device!r} (cv2 index {index})")
        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None
        # manual=True: the grab loop blocks in cap.read() at the device frame
        # rate, so it paces itself.
        self._poll = PollThread(self._grab_loop, name="webcam-grab", manual=True, join_timeout=1.0)
        self._poll.start()

    def _grab_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            cap = self.cap
            if cap is None:
                break
            ok, img = cap.read()
            if not ok:
                # Transient device hiccup: keep the last good frame and back off
                # rather than spinning.
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

    def release(self) -> None:
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
        if not ensure_pyav():
            raise RuntimeError(
                "PyAV not installed; install with `uv tool install --force 'c64cast[all]'`"
            )

        self.path = path
        self.target_sr = target_sample_rate
        self.max_video_buffer = max_video_buffer
        # The display mode's frame_target_size (or None). When set, the demux
        # loop downscales each frame to a headroom multiple of it during the
        # yuv→bgr swscale pass instead of converting the full source frame; the
        # size is planned once from the first frame (_plan_decode_size).
        self._decode_target = decode_target_size
        self._decode_size: tuple[int, int] | None = None
        self._decode_planned = False
        # PTS (rebased, seconds) of the frame current_frame() last returned —
        # the A/V-lag telemetry in VideoScene reads it as displayed_frame_pts.
        self.last_frame_pts: float = 0.0
        # Seconds into the source to begin playback (0 = from the start). The
        # container seeks to the keyframe at/just-before it and frame PTS rebase
        # to ~0 (_pts_offset), so VideoScene's from-0 playback clock lines up.
        self.start_s = max(0.0, start_s)
        # From the first decoded video frame, so later PTS rebase to a ~0 origin.
        # None until that frame arrives; re-derived after a transport seek so the
        # rebased domain lands on the seek target instead of 0.
        self._pts_offset: float | None = None
        # Where the next _pts_offset derivation rebases to: 0.0 for ordinary
        # start-of-file playback, or a landed transport seek's target_s. The
        # arithmetic is in _demux_loop.
        self._pts_anchor_target: float = 0.0
        # Transport. Guarded by self._lock alongside _video_buf: a pending seek
        # is a target_s float or None, consumed by the demux thread between
        # passes (_demux_loop), never inside a live demux(). set_muted latches
        # audio off once a scene's transport is touched.
        self._pending_seek: float | None = None
        self._muted = False

        self.container = av_open(path)
        self.v_stream = self.container.streams.video[0]
        self.a_stream = self.container.streams.audio[0] if self.container.streams.audio else None

        self.video_fps = float(self.v_stream.average_rate) if self.v_stream.average_rate else 30.0
        self.video_time_base = float(self.v_stream.time_base or 0)
        # Source duration in seconds, for absolute-jog mapping and seek/loop
        # clamping. None when the container does not report one.
        self.duration_s: float | None = (
            self.container.duration / 1_000_000 if self.container.duration else None
        )

        # Before any demux, so there is no decoder state to flush.
        # Whole-container seek in AV_TIME_BASE units (microseconds); backward
        # lands on the keyframe <= target, so playback starts at most one GOP
        # early. Audio packets interleave near the same byte offset, so A/V stay
        # aligned once video PTS are rebased.
        if self.start_s > 0:
            # A stall raises out of here, leaving the container to the
            # abandoned seek (RemoteSeekStalled).
            _seek(self.container, path, int(self.start_s * 1_000_000))
            log.info("av %s: seek to start_s=%.3fs", os.path.basename(self.path), self.start_s)

        if self.a_stream is not None:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=target_sample_rate
            )
        else:
            self._resampler = None

        # Bitmap + $D418-DAC tempo compensation. On the host-DMA 4-bit DAC path
        # over a bitmap display mode, heavy REU bank-swap writes bias the audio
        # servo and stretch playback to ~1/tempo_scale at correct pitch, so the
        # content is pre-compressed by the inverse factor: audio through an
        # `atempo` graph at 1/tempo_scale, video PTS × tempo_scale. 1.0 is a
        # no-op. atempo spans 0.5..2.0 per stage, which is why
        # validate_dac_bitmap_tempo_cfg holds tempo_scale ≥ 0.5.
        #
        # See docs/architecture/video-color.md#bitmap--d418-dac-tempo-compensation-tempo_scale.
        self._tempo_scale = tempo_scale
        # av.filter.Graph when tempo compensation is active, else None. Typed Any
        # because `av` is a lazily-imported module-global (`av: Any`), so
        # `av.filter.Graph` is not usable as a static type here.
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
        # Wakes a demux thread parked at EOF (_await_seek_after_eof) when a
        # seek is requested or the source closes.
        self._wake = threading.Condition(self._lock)
        self._eof = False
        # Set when the demux thread has returned for good (closed or crashed),
        # so a seek requested after that cannot hold `finished` off forever.
        self._demux_exited = False
        # close() releases the container through this, so a transport seek
        # still inside FFmpeg closes it when it returns (RemoteSeekStalled).
        self._closer = _ContainerCloser(self.container)
        self._closed = False
        self._demux_poll: PollThread | None = None
        self._audio_push: Callable[..., object] | None = None
        self._audio_end: Callable[[], object] | None = None
        self._audio_epoch: Callable[[], int] | None = None
        # Where on the content timeline (rebased, unscaled seconds) the audio
        # this pass has fed toward the sink ends, sound and silence alike; None
        # until the pass feeds its first. Demux-thread state, reset by a seek.
        # See `_align_audio_frame`.
        self._audio_fed_s: float | None = None
        # Resampled samples still to drop from the front of the next audio:
        # the part of a packet that overlaps audio already fed.
        self._audio_trim = 0
        # Content time of the newest decoded picture this pass, reset by a
        # seek; and the furthest behind it the file has written a packet of
        # audio, kept for the file. See `_fill_dry_stretch`.
        self._video_read_s: float | None = None
        self._audio_lag_s = 0.0
        # How far this pass's audio timestamps are shifted to follow on past
        # a discontinuity, and whether it has been warned of; reset by a seek.
        # See `_audio_frame_start`.
        self._audio_shift_s = 0.0
        self._audio_jump_warned = False
        # Set once the picture has stalled on a dry stretch, until audio
        # comes again or a seek: the fill keeps its lead over the sink's
        # buffering for the rest of that stretch, instead of stalling again
        # each time a frame drains.
        self._dry_stalled = False

        # Unity gain when there is no audio stream or the scan fails.
        self.audio_gain: float = 1.0
        # Pre-gain noise gate threshold, for the dither-on path only, where the
        # dither would otherwise paint hiss across the amplified noise floor.
        # With dither off there is no jitter to suppress and the hard per-sample
        # gate slices every zero-crossing of a quiet tone, which is heard as
        # "many short segments stitched together" — so it is off by default and
        # callers pass source_noise_gate_enabled=True alongside dither. Zero =
        # no gate.
        self.audio_noise_gate: int = 0
        # The peak scan is a full end-to-end audio decode (~0.5 s on a 2.5-min
        # clip) whose only product is audio_gain, so a caller that will not push
        # audio, or computes its own gain (the REU pre-encode path), passes
        # scan_audio_peak=False and keeps gain at unity.
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
            container = av_open(self.path)
            try:
                a_stream = container.streams.audio[0]
                # With a start_s seek, normalize over [start_s, end] only, so
                # the gain reflects what is actually heard.
                if self.start_s > 0:
                    try:
                        _seek(container, self.path, int(self.start_s * 1_000_000))
                    except RemoteSeekStalled:
                        container = None
                        raise
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
                if container is not None:
                    container.close()
        except RemoteSeekStalled as e:
            log.warning("av %s: audio peak scan skipped (%s); using unity gain", self.path, e)
            return 0
        except Exception:
            log.exception("av %s: audio peak scan failed; using unity gain", self.path)
            return 0
        return peak

    def start(
        self,
        audio_push: Callable[..., object] | None,
        audio_end: Callable[[], object] | None = None,
        audio_epoch: Callable[[], int] | None = None,
    ):
        """Start the demuxer thread. ``audio_push=None`` skips audio decode
        entirely — used by the REU-staged audio path where the soundtrack
        has already been pre-decoded into REU and the demuxer shouldn't
        waste CPU decoding + resampling audio just to discard it.

        ``audio_end`` is the sink's ``end_input``, called after the last push
        of every pass that reaches EOF with no seek pending, and when the
        demuxer crashes. Both sinks wait for a prebuffer before
        they play, and a clip whose audio is shorter than it never fills one.
        A seek after EOF starts pushing again, and the sink's next accepted
        push reopens its input, so the call is safe under an A/B loop.

        ``audio_epoch`` is the sink's ``current_flush_epoch``. Each push is
        tagged with the epoch read under the lock `request_seek` takes the
        sink's cut under, so a push decided before a seek is requested is
        dropped and one decided after it is applied is kept, however soon the
        demuxer gets there (see `request_seek`)."""
        self._audio_push = audio_push
        self._audio_end = audio_end
        self._audio_epoch = audio_epoch
        # The loop's stop signal is self._closed, read by the seek/emit paths
        # too, so the PollThread event goes unused; the poll supplies only the
        # daemon-thread start/join lifecycle.
        self._demux_poll = PollThread(
            lambda stop: self._demux_loop(), name="av-demux", manual=True, join_timeout=1.0
        )
        self._demux_poll.start()

    def request_seek(
        self,
        target_s: float,
        *,
        unmute: bool = False,
        on_request: Callable[[], _T] | None = None,
    ) -> _T | None:
        """Ask the demux thread to seek to `target_s` (absolute seconds from
        file start) at its next opportunity. Coalescing is natural: rapid
        repeated calls (RW/FF ticking, jog) just overwrite the single pending
        slot — the demux thread performs however many real seeks it has
        cycles for. Clears the buffered (stale, pre-seek) frames immediately
        so a caller reading `current_frame` right after doesn't get one.

        ``on_request`` runs under the same lock, with the seek pending, and
        its result is returned: the splice takes the audio sink's cut there
        (`FlushCut`). Every push the demuxer decides on reads the sink's epoch
        under that lock (`_emit_audio`), so none can straddle the cut: one
        decided before it carries the retired epoch, and the demuxer can
        apply the seek and push the target's audio only after it. Taken
        after this returned, the cut retired that audio too whenever the
        demuxer got there first. ``unmute`` lifts `set_muted` in the same
        critical section, for a resume, so no packet of the old position
        reaches the sink between the two."""
        target_s = max(0.0, target_s)
        with self._lock:
            self._pending_seek = target_s
            self._video_buf.clear()
            if unmute:
                self._muted = False
            try:
                return on_request() if on_request is not None else None
            finally:
                # The seek stands whether or not the cut raised (the splice
                # falls back to a fresh anchor), so a demuxer parked at EOF
                # still has to be woken to apply it.
                self._wake.notify_all()

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

    @property
    def accepts_seeks(self) -> bool:
        """False once the demux thread has returned for good (closed or
        crashed): a seek requested after that is never applied, so an A/B loop
        wrap at EOF cannot restart playback."""
        with self._lock:
            return not self._demux_exited

    def _emit_audio(self, arr: np.ndarray) -> None:
        """Apply the noise gate + normalization gain to a mono int16 sample
        array and hand it to the audio consumer. Shared by the direct path and
        the atempo-compensated path."""
        # Drop audio decoded from the stale pre-seek read position while a seek
        # is pending, or it plays after the splice's flush. A chunk that passes
        # this check just before a seek is requested carries the epoch read
        # with it, which the cut taken in request_seek retires, and the sink
        # drops it. A closed source pushes nothing: a demux thread that
        # outlived close()'s bounded join would otherwise feed a reused
        # sampler that the scene's next setup() has re-armed.
        with self._lock:
            push = self._audio_push
            if self._closed or push is None or self._muted or self._pending_seek is not None:
                return
            epoch = self._audio_epoch() if self._audio_epoch is not None else None
        if self.audio_noise_gate > 0:
            # Zero source-noise-floor samples before gain, or the encoder jitters
            # between NEUTRAL and ±1 at amplified noise levels.
            arr = np.where(np.abs(arr) < self.audio_noise_gate, np.int16(0), arr)
        if self.audio_gain != 1.0:
            arr = np.clip(arr.astype(np.float32) * self.audio_gain, INT16_MIN, INT16_MAX).astype(
                np.int16
            )
        if epoch is None:
            push(arr.astype(np.int16, copy=False))
        else:
            push(arr.astype(np.int16, copy=False), epoch=epoch)

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
            # In the same critical section that retires the request, or
            # `finished` could see neither a pending seek nor a live pass.
            self._eof = False
        _seek(self.container, self.path, int(target * 1_000_000), closer=self._closer)
        if self.a_stream is not None:
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=self.target_sr)
        if self._atempo_graph is not None:
            self._atempo_graph = _build_atempo_graph(self.target_sr, self._tempo_scale)
        self._pts_offset = None
        self._pts_anchor_target = target
        self._audio_fed_s = None
        self._audio_trim = 0
        self._video_read_s = None
        self._dry_stalled = False
        self._audio_shift_s = 0.0
        self._audio_jump_warned = False
        log.info("av %s: transport seek to %.3fs", os.path.basename(self.path), target)
        return True

    def _plan_decode(self, frame: Any) -> None:
        """Plan the decode downscale once, from the first frame's real
        dimensions. _decode_size None = source already small enough (or no
        target) → plain full-res convert."""
        self._decode_planned = True
        if self._decode_target is None:
            return
        self._decode_size = _plan_decode_size(frame.width, frame.height, *self._decode_target)
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

    def _frame_to_bgr(self, frame: Any) -> np.ndarray:
        """Decoded frame → BGR ndarray, at the planned decode size when one
        applies (yuv→bgr + downscale in one swscale pass — cheap, vs a
        full-res bgr buffer + a separate cv2.resize)."""
        if not self._decode_planned:
            self._plan_decode(frame)
        if self._decode_size is not None:
            return frame.reformat(
                width=self._decode_size[0],
                height=self._decode_size[1],
                format="bgr24",
            ).to_ndarray()
        return frame.to_ndarray(format="bgr24")

    def _rebase_pts(self, frame: Any) -> float:
        """Rebase a frame's PTS so the first decoded frame sits at
        ~_pts_anchor_target (0.0 for ordinary start_s playback; a transport
        seek's target_s once one has landed — see _apply_pending_seek). With a
        start_s seek the raw PTS are ~start_s; the playback clock (audio
        samples / wall-clock) starts at 0, so without this current_frame()
        would find no frame <= 0 for start_s seconds. Offset is captured from
        the first frame (the keyframe the seek landed on), so the
        no-transport-seek path is unchanged (anchor 0.0, offset == first PTS,
        rebased ~0). Then the bitmap+DAC tempo compensation: compress the
        video timeline by tempo_scale so it stays in lock-step with the
        1/tempo_scale-compressed audio (both then net to real time under the
        ~tempo_scale drain-clock slowdown). No-op when tempo_scale == 1.0."""
        pts = float(frame.pts * self.video_time_base) if frame.pts is not None else 0.0
        pts = self._content_time(pts)
        if self._tempo_scale != 1.0:
            pts *= self._tempo_scale
        return pts

    def pin_timeline_origin(self) -> float | None:
        """Fix the content timeline's origin (stream seconds) before `start`,
        for a caller that places the audio itself: the REU-staged preload,
        which gets no audio from this demuxer to set it. The origin is the
        earliest start either stream reports, where the demuxer's own would
        be the pass's first timestamp. None, pinning nothing, under a
        ``start_s`` seek (the preload decodes from the file's start) or when
        neither stream reports a start."""
        if self.start_s > 0:
            return None
        starts = [
            float(s.start_time * s.time_base)
            for s in (self.v_stream, self.a_stream)
            if s is not None and s.start_time is not None and s.time_base is not None
        ]
        if not starts:
            return None
        origin = min(starts)
        self._pts_offset = origin - self._pts_anchor_target
        return origin

    def _content_time(self, pts_s: float) -> float:
        """A stream timestamp (seconds) on the content timeline: rebased,
        unscaled. The pass's first timestamp from either stream sets the
        offset, so audio and picture share one origin; a sound that starts
        after its picture keeps that distance instead of being pulled to the
        front."""
        if self._pts_offset is None:
            self._pts_offset = pts_s - self._pts_anchor_target
        return pts_s - self._pts_offset

    def _enqueue_frame(self, pts: float, img: np.ndarray) -> bool:
        """Append (pts, img) to the video buffer, blocking while it is full.
        Returns False only when the source closed mid-wait (stop demuxing).

        Backpressure rationale: the old behavior (silent-drop oldest frames)
        was a safety net under host-DMA mode, where AudioStreamer's
        push_samples blocking throttle keeps the demuxer at real-time and the
        buffer never filled in practice. In REU mode there's no audio
        backpressure (audio is pre-decoded and lives in REU), so the demuxer
        would race ahead, fill the buffer, and start dropping the EARLIEST
        frames — leaving current_frame() with no frames at PTS ≤ the audio
        clock for several seconds. User-visible symptom: video freezes early
        in playback for a long time, then "catches up" near the end. Blocking
        the demuxer until the consumer drains is correct in both modes;
        host-DMA just doesn't hit the wait."""
        self._video_read_s = pts / (self._tempo_scale or 1.0)
        waiting_on: float | None = None
        since = 0.0
        while True:
            if self._closed:
                return False
            with self._lock:
                if self._pending_seek is not None:
                    # A seek landed while blocked on a full buffer, so this
                    # frame predates it. The pass ends at its next packet
                    # and _demux_loop applies the seek between passes.
                    return True
                if len(self._video_buf) < self.max_video_buffer:
                    self._video_buf.append((pts, img))
                    return True
                oldest, newest = self._video_buf[0][0], self._video_buf[-1][0]
            now = time.monotonic()
            if oldest != waiting_on:
                waiting_on, since = oldest, now
            elif now - since >= DRY_FILL_STALL_S:
                self._dry_stalled = True
            self._fill_dry_stretch(oldest, newest)
            time.sleep(0.005)

    def _fill_dry_stretch(self, oldest_pts: float, newest_pts: float) -> None:
        """The video buffer is full, and the sink's clock is what drains it.
        With no audio coming for the buffered stretch (a sound that ended, or
        starts late, or has a gap longer than the buffer), that clock stops
        and the demuxer waits on it for good. Feed silence up to
        `DRY_FILL_INTERLEAVE_S` short of the newest buffered frame (or as far
        short as this file has written audio behind its picture); once the
        oldest frame has waited `DRY_FILL_STALL_S` (`_dry_stalled`, until
        audio comes again), at least `DRY_FILL_MIN_LEAD_S` past the oldest;
        never past the newest.
        The clock then runs on through the buffer. Audio that does come
        is aligned as usual: later than the fill, it follows it; inside it,
        the covered part is trimmed. A no-op without an audio sink, or while
        the audio fed already reaches that far."""
        if self._resampler is None or self._audio_push is None:
            return
        scale = self._tempo_scale or 1.0
        oldest, newest = oldest_pts / scale, newest_pts / scale
        margin = max(DRY_FILL_INTERLEAVE_S, self._audio_lag_s + AUDIO_ALIGN_TOLERANCE_S)
        target = newest - margin
        if self._dry_stalled:
            target = max(target, oldest + DRY_FILL_MIN_LEAD_S)
        target = min(target, newest)
        fed = self._audio_fed_s if self._audio_fed_s is not None else self._pts_anchor_target
        silence, _, self._audio_fed_s = place_audio_frame(target, 0.0, fed, self.target_sr)
        self._feed_silence(silence)

    def _decode_audio_packet(self, packet: Any) -> None:
        """Resample an audio packet and emit it — through the atempo graph
        when tempo compensation is on (time-compress, pitch-preserving; the
        graph buffers, so one input frame yields 0..N output frames)."""
        assert self._resampler is not None  # caller checks (audio-branch gate)
        for frame in packet.decode():
            self._align_audio_frame(frame)
            for resampled in self._resampler.resample(frame):
                self._emit_resampled(resampled)

    def _align_audio_frame(self, frame: Any) -> None:
        """Place a decoded audio frame on the picture's timeline before it is
        fed. The sink's clock counts the samples it plays, and the picture
        follows that clock, so audio fed back to back drifts from the picture
        by every stretch the file leaves silent: a sound that starts late, or
        comes back after a gap, played as soon as it was read, up to a whole
        video buffer early. A frame that starts after the audio fed so far is
        preceded by silence up to its start, and one that starts before it
        (audio a dry-stretch fill already covered, or a muxer's overlap) loses
        that overlap. A frame with no timestamp follows on."""
        self._dry_stalled = False
        rate = frame.sample_rate or self.target_sr
        duration = frame.samples / rate if rate else 0.0
        fed = self._audio_fed_s if self._audio_fed_s is not None else self._pts_anchor_target
        if frame.pts is None or frame.time_base is None:
            start = fed
        else:
            start = self._audio_frame_start(float(frame.pts * frame.time_base), fed)
            if self._video_read_s is not None:
                self._audio_lag_s = max(self._audio_lag_s, self._video_read_s - start)
        silence, trim, self._audio_fed_s = place_audio_frame(start, duration, fed, self.target_sr)
        self._audio_trim += trim
        self._feed_silence(silence)

    def _audio_frame_start(self, pts_s: float, fed_s: float) -> float:
        """Where on the content timeline an audio frame stamped ``pts_s``
        (stream seconds) starts, given the audio fed so far ends at
        ``fed_s``. A stamp past `AUDIO_DISCONTINUITY_S` beyond the newest
        picture read (or the pass's anchor, before one is read), or that far
        behind the audio fed, is a jump in the file's timestamps: the frame
        follows on, and the pass's later frames are shifted by the same jump."""
        start = self._content_time(pts_s) - self._audio_shift_s
        horizon = self._video_read_s if self._video_read_s is not None else self._pts_anchor_target
        ahead = start - fed_s > AUDIO_ALIGN_TOLERANCE_S and start - horizon > AUDIO_DISCONTINUITY_S
        behind = fed_s - start > AUDIO_DISCONTINUITY_S
        if not (ahead or behind):
            return start
        jump = start - fed_s
        self._audio_shift_s += jump
        if not self._audio_jump_warned:
            self._audio_jump_warned = True
            log.warning(
                "av %s: audio timestamps jump %+.1fs at %.1fs; following on from the audio "
                "before them (further jumps this pass are not logged)",
                os.path.basename(self.path),
                jump,
                fed_s,
            )
        return fed_s

    def _feed_silence(self, samples: int) -> None:
        """Feed ``samples`` (at ``target_sr``) of content-timeline silence the
        way audio goes, through the atempo graph when tempo compensation is
        on, in pieces a sink's backpressure takes one at a time. Stops early
        on a seek or a close, which retire it anyway."""
        remaining = samples
        while remaining > 0 and not self._closed and not self.seek_pending:
            n = min(remaining, SILENCE_PIECE_SAMPLES)
            remaining -= n
            zeros = np.zeros(n, dtype=np.int16)
            if self._atempo_graph is not None:
                frame = av.AudioFrame.from_ndarray(
                    zeros.reshape(1, -1), format="s16", layout="mono"
                )
                frame.sample_rate = self.target_sr
                self._atempo_graph.push(frame)
                self._drain_atempo()
            else:
                self._emit_audio(zeros)

    def _emit_resampled(self, resampled: Any) -> None:
        if self._audio_trim:
            arr = resampled.to_ndarray().reshape(-1)
            cut = min(self._audio_trim, arr.size)
            self._audio_trim -= cut
            arr = arr[cut:]
            if not arr.size:
                return
            if self._atempo_graph is None:
                self._emit_audio(arr)
                return
            resampled = av.AudioFrame.from_ndarray(arr.reshape(1, -1), format="s16", layout="mono")
            resampled.sample_rate = self.target_sr
        if self._atempo_graph is not None:
            self._atempo_graph.push(resampled)
            self._drain_atempo()
        else:
            self._emit_audio(resampled.to_ndarray().reshape(-1))

    def _flush_resampler(self) -> None:
        """At EOF, emit the filter tail the resampler holds back until it is
        flushed (a few milliseconds per track), ahead of `_flush_atempo` so the
        tail is time-compressed with the rest. A transport seek rebuilds the
        resampler, so flushing this one does not strand a later loop pass."""
        if self._resampler is None or self._audio_push is None or self._closed:
            return
        try:
            for resampled in self._resampler.resample(None):
                self._emit_resampled(resampled)
        except (av.error.FFmpegError, ValueError) as e:
            log.debug("demux: resampler flush failed: %s", e)

    def _demux_loop(self):
        """One demux pass per seek target: a pass that reaches EOF parks the
        thread until a seek or close() arrives, and a seek starts the next
        pass from the target. The demuxer reads ahead of playback, so it
        reaches EOF while the scene still has seconds to show — and a seek
        made in that window (a resume, an A/B loop wrap, a jog back) would be
        lost with nothing left to apply it."""
        # "Container hit EOF" is expected and logs debug; a mid-stream decode
        # failure logs a full traceback and ends the thread.
        try:
            while (end := self._demux_pass()) != "closed":
                if end == "eof":
                    self._flush_resampler()
                    self._flush_atempo()
                    self._end_audio_input()
                    log.debug("demux %s: EOF", self.path)
                    if not self._await_seek_after_eof():
                        return
                self._apply_pending_seek()
        except RemoteSeekStalled as e:
            log.error("demux %s: %s; ending playback", self.path, e)
        except Exception:
            log.exception("demux %s crashed", self.path)
            # The crash is this input's end too: a clip that pushed less than
            # the sink's prebuffer before it would otherwise never be played.
            self._end_audio_input()
        finally:
            with self._lock:
                self._eof = True
                self._demux_exited = True

    def _end_audio_input(self) -> None:
        """Tell the sink this pass pushed its last sample (see `start`). Not
        when a seek is already pending: that pass is superseded and the next
        one ends the input at its own EOF, while a call here can land after
        the splice's cut and mark the post-seek input ended. The check and
        the call share `_lock` with `request_seek`, because a seek landing
        between them is that same late call: the cut that reopens the input
        is taken under that lock, so a call that wins the lock lands before
        it, and a post-seek pass's end, after it, is kept."""
        with self._lock:
            self._end_audio_input_locked()

    def _end_audio_input_locked(self) -> None:
        if (
            self._audio_end is None
            or self._audio_push is None
            or self._closed
            or self._pending_seek is not None
        ):
            return
        self._audio_end()

    def _demux_pass(self) -> Literal["eof", "seek", "closed"]:
        """Demux from the container's current position until EOF, a pending
        seek, or close. A seek ends the pass, and `_demux_loop` applies it
        between passes, with no `demux()` generator live: once a generator
        has read EOF it yields only flush packets, which would drain the
        decoders the seek just reset, and a seek inside one is timed against
        its last read (see `_seek`). The packet in flight when the seek
        arrived was read from the old position and is dropped."""
        if self._closed:
            # close() may have landed during the seek that ended the last
            # pass, and that seek's worker has since closed the container.
            return "closed"
        packets = self.container.demux()
        try:
            for packet in packets:
                if self._closed:
                    return "closed"
                if self.seek_pending:
                    return "seek"
                if packet.stream.type == "video":
                    for frame in packet.decode():
                        img = self._frame_to_bgr(frame)
                        if not self._enqueue_frame(self._rebase_pts(frame), img):
                            return "closed"
                elif (
                    packet.stream.type == "audio"
                    and self._resampler is not None
                    and self._audio_push is not None
                ):
                    self._decode_audio_packet(packet)
        except (EOFError, StopIteration):
            pass
        finally:
            close = getattr(packets, "close", None)
            if close is not None:
                close()
        # A seek requested after the last packet would otherwise park with it.
        return "seek" if self.seek_pending else "eof"

    def _await_seek_after_eof(self) -> bool:
        """Mark EOF and park until a seek is requested (True; `_demux_loop`
        applies it) or the source closes (False)."""
        with self._lock:
            self._eof = True
            while self._pending_seek is None and not self._closed:
                self._wake.wait()
            return not self._closed

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
            # Telemetry: VideoScene logs audio_position_s - last_frame_pts as
            # the A/V lag. Up to one frame interval is healthy; a growing lag
            # means the decoder is falling behind the audio-master clock.
            self.last_frame_pts = chosen_pts
            # The chosen frame normally stays in the buffer so a clock stall
            # cannot black-frame the display. After demux EOF that would trap
            # the buffer at size 1 forever, so `finished` never fires and the
            # audio worker pads NEUTRAL indefinitely — hence draining it once
            # EOF is observed and the last buffered frame has been consumed.
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
        """EOF with nothing left to show — unless a seek is still waiting
        for a live demux thread to restart it."""
        with self._lock:
            seek_outstanding = self._pending_seek is not None and not self._demux_exited
            return self._eof and not self._video_buf and not seek_outstanding

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._wake.notify_all()
        if self._demux_poll is not None:
            self._demux_poll.stop()
            self._demux_poll = None
        self._closer.release()
