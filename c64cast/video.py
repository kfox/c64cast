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

from ._pollthread import PollThread
from .audio import DAC_VOLUME_SCALE, INT16_FULL_SCALE, INT16_MAX, INT16_MIN
from .palette import ColorFit, ColorFitAccumulator, ColorMap, ColorMapAccumulator

log = logging.getLogger(__name__)

# Peak-normalization for commercial-scene audio. The SID volume DAC is 4-bit;
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


# PyAV is imported lazily on first AVFileSource construction. On macOS the av
# wheel bundles a different libavdevice major version than the cv2 wheel, and
# eagerly importing both at startup triggers the Obj-C runtime's "duplicate
# class implementation" warnings. Deferring av until a commercial scene
# actually runs sidesteps the clash entirely when commercials aren't used.
av: Any = None
PYAV_AVAILABLE: bool | None = None  # tri-state: None = not yet probed


def _ensure_pyav() -> bool:
    """Import PyAV on demand; cache the result. Returns availability."""
    global av, PYAV_AVAILABLE
    if PYAV_AVAILABLE is not None:
        return PYAV_AVAILABLE
    try:
        import av as _av

        av = _av
        PYAV_AVAILABLE = True
    except ImportError:
        PYAV_AVAILABLE = False
    return PYAV_AVAILABLE


def decode_audio_full(path: str, target_sample_rate: int) -> np.ndarray:
    """Decode the entire audio track of ``path`` to mono int16 at
    ``target_sample_rate``. Returns a single contiguous np.ndarray.

    Blocking — call before scene paint starts. Used by the REU-staged audio
    path in CommercialScene where the whole track must be preloaded into
    REU before playback begins.

    Cost: ~100-200 ms for a 30-sec commercial via PyAV on this hardware.
    Raises RuntimeError if PyAV isn't available or there's no audio stream
    in the container.
    """
    if not _ensure_pyav():
        raise RuntimeError("PyAV not installed; install with `pip install c64cast[commercials]`")
    container = av.open(path)
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


def _scan_video_samples(path: str, accumulators: list[Any], max_samples: int = 120) -> bool:
    """Decode up to ``max_samples`` frames spread across ``path`` and feed each
    into every accumulator's ``.add(img_bgr)``.

    Stride comes from the stream's frame count when known, else a fixed decode
    stride. Blocking — call at scene setup, before playback. Returns True on a
    clean scan, False when PyAV is unavailable or decode failed (callers then
    treat each accumulator's result as None). Shared by the auto_fit and
    force_palette pre-scans so a source is decoded ONCE for both.
    """
    if not accumulators or not _ensure_pyav():
        return False
    try:
        container = av.open(path)
        try:
            v_stream = container.streams.video[0]
            v_stream.thread_type = "AUTO"
            total = v_stream.frames or 0
            stride = max(1, total // max_samples) if total else 5
            taken = 0
            for i, frame in enumerate(container.decode(v_stream)):
                if i % stride:
                    continue
                img = frame.to_ndarray(format="bgr24")
                for acc in accumulators:
                    acc.add(img)
                taken += 1
                if taken >= max_samples:
                    break
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
) -> tuple[ColorFit | None, ColorMap | None]:
    """Pre-scan a video once and derive the enabled per-source color stages.

    ``fit_strength`` not None enables the adaptive ColorFit ([color].auto_fit);
    ``map_colors``/``map_indices`` not None/empty enables the forced-palette
    ColorMap ([color].force_palette). Both stages share a single decode pass.
    Returns (ColorFit|None, ColorMap|None); a disabled or failed stage is None,
    so callers can unconditionally pass the results to set_color_fit /
    set_color_map. See palette.ColorFitAccumulator / palette.ColorMapAccumulator.
    """
    fit_acc = ColorFitAccumulator(strength=fit_strength) if fit_strength is not None else None
    map_acc = (
        ColorMapAccumulator(n_colors=map_colors or 16, indices=map_indices)
        if (map_colors is not None or map_indices)
        else None
    )
    accs = [a for a in (fit_acc, map_acc) if a is not None]
    if not _scan_video_samples(path, accs):
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

    def __init__(self, device: int):
        # -1 = system default camera. OpenCV doesn't have a portable "default"
        # sentinel of its own (passing -1 errors with "out device of bound"),
        # but index 0 is the platform default on every backend we target —
        # AVFoundation, V4L2, DSHOW, MSMF. Mirror the audio convention where
        # negative = default so the example config can use a single sentinel.
        index = 0 if device < 0 else device
        self.cap: cv2.VideoCapture | None = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open video device {device}")
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
    ):
        if not _ensure_pyav():
            raise RuntimeError(
                "PyAV not installed; install with `pip install c64cast[commercials]`"
            )

        self.path = path
        self.target_sr = target_sample_rate
        self.max_video_buffer = max_video_buffer

        self.container = av.open(path)
        self.v_stream = self.container.streams.video[0]
        self.a_stream = self.container.streams.audio[0] if self.container.streams.audio else None

        self.video_fps = float(self.v_stream.average_rate) if self.v_stream.average_rate else 30.0
        self.video_time_base = float(self.v_stream.time_base or 0)

        if self.a_stream is not None:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=target_sample_rate
            )
        else:
            self._resampler = None

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
        typically <1 s for a 60 s commercial. The playlist's interstitial
        already gives us several seconds of cover before a commercial paints
        its first frame."""
        peak = 0
        try:
            container = av.open(self.path)
            try:
                a_stream = container.streams.audio[0]
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

    def _demux_loop(self):
        # Differentiate "container hit EOF" (expected, info) from "decode blew
        # up mid-stream" (unexpected, log full traceback).
        try:
            for packet in self.container.demux():
                if self._closed:
                    return
                if packet.stream.type == "video":
                    for frame in packet.decode():
                        img = frame.to_ndarray(format="bgr24")
                        pts = (
                            float(frame.pts * self.video_time_base)
                            if frame.pts is not None
                            else 0.0
                        )
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
                            arr = resampled.to_ndarray().reshape(-1)
                            if self.audio_noise_gate > 0:
                                # Zero source-noise-floor samples BEFORE
                                # gain so the encoder doesn't jitter
                                # between NEUTRAL and ±1 at amplified
                                # noise levels.
                                arr = np.where(
                                    np.abs(arr) < self.audio_noise_gate, np.int16(0), arr
                                )
                            if self.audio_gain != 1.0:
                                arr = np.clip(
                                    arr.astype(np.float32) * self.audio_gain, INT16_MIN, INT16_MAX
                                ).astype(np.int16)
                            self._audio_push(arr.astype(np.int16, copy=False))
            log.debug("demux %s: EOF", self.path)
        except (EOFError, StopIteration):
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
            consumed_through = -1
            for i, (pts, img) in enumerate(self._video_buf):
                if pts <= audio_position_s:
                    chosen_img = img
                    consumed_through = i
                else:
                    break
            if chosen_img is None:
                return None
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
