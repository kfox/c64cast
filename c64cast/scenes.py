"""Scene state machine.

Scenes own a DisplayMode and (optionally) an AudioStreamer + video source.
The Playlist drives them: setup() → process_frame()* → teardown().

Each scene also carries a list of `Overlay`s (see overlays/) which the
Playlist runs around the scene's lifecycle. The scene itself is oblivious
to overlays — they're a Playlist-level concern.

Two scene families:

* Live (webcam): WebcamScene. Optimized for low latency — reads each
  frame and pushes it straight through. Audio follows the global
  [audio].enabled flag (overridable per-scene with `audio = false`);
  when on, the mic feed runs uncorrelated to the video (no sync delay).
  The Playlist's deadline-based frame dropping handles congestion; no
  per-scene backpressure check needed under DMA.

* Recorded (file): VideoScene. Uses PyAV for A/V demux with a shared
  PTS clock — the video reader picks frames against the audio playback
  position rather than wall-clock-from-start, so drift can't accumulate.
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from ._pollthread import PollThread
from .audio import (
    INT16_FULL_SCALE,
    REU_PUMP_CHUNK_SIZE_HEAVY_BUS,
    AudioStreamer,
    encode_floats_to_dac,
)
from .backend import C64Backend
from .c64 import CIA1, SCREEN
from .modes import BitmapDisplayMode, DisplayMode
from .palette import ColorFitAccumulator, ColorMapAccumulator
from .profiler import get_profiler
from .sampler import UltimateAudioSampler
from .video import (
    AVFileSource,
    WebcamSource,
    _compute_normalization_gain,
    _ensure_pyav,
    decode_audio_full,
    prescan_source_color,
)

if TYPE_CHECKING:
    from .audio_source import AudioSource
    from .config import AudioCfg, ColorCfg
    from .effects import FrameEffect
    from .frame_source import FrameSource
    from .modulation import MusicModulation
    from .overlays import Overlay

log = logging.getLogger(__name__)

# A scene's audio object is either the shared 4-bit DAC streamer or (video
# scenes on a sampler-capable U64) the Ultimate Audio FPGA sampler. Both
# satisfy the scene-facing contract (sample_rate / position_seconds / stop /
# push_samples / set_pre_emphasis); backend-specific bring-up branches narrow
# via isinstance.
SceneAudio = AudioStreamer | UltimateAudioSampler

_C64_ASPECT = 320 / 200

# Rolling-window auto_fit: how many opening frames to fold into the online
# ColorFitAccumulator before freezing the derived fit. The accumulator is
# additive, so the fit converges and stabilises over this window (~2s at
# 24 fps) — replacing the old blocking full-source pre-scan with a brief
# on-screen settle and no startup pause. See VideoScene.setup.
ONLINE_FIT_WARMUP_FRAMES = 48

# How often (seconds) VideoScene emits the live A/V-lag debug line while a
# video plays under -vv. The per-scene summary is logged once at teardown
# (info, visible at -v). See VideoScene._log_av_lag.
AV_LAG_LOG_INTERVAL_S = 2.0


def _crop_to_aspect(img: np.ndarray, target_ratio: float = _C64_ASPECT) -> np.ndarray:
    """Center-*crop* to ``target_ratio`` (fill/cover): trims the long axis so
    the image fills the frame edge-to-edge, losing the cropped margins. The
    default aspect handling for every source (and the only one webcam/video
    use)."""
    h, w = img.shape[:2]
    ar = w / h if h else target_ratio
    if ar > target_ratio:
        new_w = int(h * target_ratio)
        x0 = (w - new_w) // 2
        return img[:, x0 : x0 + new_w]
    if ar < target_ratio:
        new_h = int(w / target_ratio)
        y0 = (h - new_h) // 2
        return img[y0 : y0 + new_h, :]
    return img


def _fit_to_aspect(
    img: np.ndarray,
    target_ratio: float = _C64_ASPECT,
    pad_color: tuple[int, int, int] = (0, 0, 0),
) -> np.ndarray:
    """Letterbox/pillarbox to ``target_ratio`` (contain): scale nothing, just
    pad the short axis with ``pad_color`` bars so the *whole* image is visible.
    The inverse trade-off to ``_crop_to_aspect`` — nothing is lost, but bars
    appear. The bars are a single solid color so they quantize to one stable
    palette cell (black by default → C64 index 0); for a still slideshow that's
    flicker-free, which is why fit-mode is exposed there and not on video (where
    per-frame bg0 churn would shimmer — see project_slideshow_aspect_fit)."""
    h, w = img.shape[:2]
    ar = w / h if h else target_ratio
    if ar > target_ratio:
        # Wider than target → keep full width, pad top/bottom.
        new_h = round(w / target_ratio)
        pad = max(0, new_h - h)
        top = pad // 2
        return cv2.copyMakeBorder(img, top, pad - top, 0, 0, cv2.BORDER_CONSTANT, value=pad_color)
    if ar < target_ratio:
        # Taller than target → keep full height, pad left/right.
        new_w = round(h * target_ratio)
        pad = max(0, new_w - w)
        left = pad // 2
        return cv2.copyMakeBorder(img, 0, 0, left, pad - left, cv2.BORDER_CONSTANT, value=pad_color)
    return img


def _apply_aspect(
    img: np.ndarray,
    aspect_mode: str = "crop",
    target_ratio: float = _C64_ASPECT,
) -> np.ndarray:
    """Dispatch a source frame through the configured aspect handling before
    the display mode downscales it to the C64 resolution:

    * ``"crop"`` (default) — center-crop to fill (today's universal behavior).
    * ``"fit"``  — letterbox/pillarbox so the whole image shows, padded black.
    * ``"stretch"`` — no aspect handling; the mode's resize distorts to fill.
    """
    if aspect_mode == "fit":
        return _fit_to_aspect(img, target_ratio)
    if aspect_mode == "stretch":
        return img
    return _crop_to_aspect(img, target_ratio)


def _display_name(path: str) -> str:
    """Basename without its file extension, for scene-name display."""
    return os.path.splitext(os.path.basename(path))[0]


def _annotate_frame_number(img: np.ndarray, label: str) -> np.ndarray:
    """Draw `label` (timecode + frame #) into the top-left corner of a BGR
    frame, returning an annotated COPY (the caller's `img` may be a view onto
    a shared source buffer — mutating it in place would corrupt the cache).

    Drawn before quantization, so it works on any display mode (the digits
    become part of the quantized bitmap). The font scale is derived from the
    frame width so the label spans ~55% of the width regardless of source
    resolution — that survives the downscale to 160/320-wide C64 output
    legibly. White text with a black outline reads on any background. This
    is a diagnostic aid only (see [debug].frame_numbers)."""
    out = img.copy()
    h, w = out.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, _th), _ = cv2.getTextSize(label, font, 1.0, 1)
    scale = (0.55 * w) / max(tw, 1)
    thick = max(2, int(round(scale * 2)))
    org = (int(0.02 * w), int(0.10 * h) + int(scale * 12))
    cv2.putText(out, label, org, font, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(out, label, org, font, scale, (255, 255, 255), thick, cv2.LINE_AA)
    return out


def _timecode(seconds: float) -> str:
    """Format seconds as M:SS for the frame-number debug overlay."""
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


class Scene:
    # Subclasses that drive audible content (video PyAV, native
    # sidplay, MIDI → SID) flip this to True so the Playlist consults
    # the ensemble audio lock before setup. Live scenes (webcam, blank)
    # leave it False — in ensemble mode their audio is suppressed at
    # build time so they have nothing to coordinate. Single-system mode
    # ignores this flag entirely (no ensemble → no lock to consult).
    WANTS_AUDIO_LOCK: bool = False

    def __init__(
        self,
        api: C64Backend,
        audio: SceneAudio | None,
        display_mode: DisplayMode | None,
        name: str,
    ):
        self.api = api
        self.audio = audio
        self.display_mode = display_mode
        self.name = name
        self.is_done = False
        self.duration_s: float = 30.0
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self.prev_frame: np.ndarray | None = None
        # Populated by config.scenes_from_config(); Playlist runs them around
        # setup/process_frame/teardown. Order matters — later overlays paint
        # on top of earlier ones.
        self.overlays: list[Overlay] = []
        # Per-scene framerate cap. None = use the Playlist's default (60 for
        # NTSC / 50 for PAL). Bitmap scenes that can't sustain full system fps
        # over HTTP set this to something achievable (24-30 typical) so the
        # natural-pace + drop logic in Playlist doesn't waste CPU trying.
        self.target_fps: float | None = None
        # Debug aid (set by config.build_scene from [debug].frame_numbers):
        # source-bearing scenes draw the timecode + frame number into each
        # frame before quantization. See _annotate_frame_number.
        self.show_frame_numbers: bool = False
        # Set by config.build_scene: per-scene pre-emphasis (float, or None =
        # global/source-aware default). Applied to the shared AudioStreamer in
        # setup() for audio-bearing scenes; ignored when the scene has no audio.
        self.pre_emphasis: float | None = None
        # Optional per-scene pixel effect (None = no effect). Set by
        # config.build_scene from [[scenes]].effect. Applied to the source
        # frame in _render_with_overlays before the display mode quantizes, so
        # it works on any frame-bearing scene (webcam/video/slideshow/
        # generative). Self-rendered bitmap scenes (waveform/midi) bypass that
        # helper, so the effect doesn't apply to them. Reset in setup() so a
        # looping/re-entered scene starts with clean effect state.
        self.effect: FrameEffect | None = None
        # Set by config.build_scene to the SceneCfg the scene was built
        # from — the playlist's orchestrator wiring reads this (and the
        # rest are populated for conductor/follower roles by the
        # playlist / cli ensemble plumbing). All Any because Orchestrator
        # imports would create cycles; the consumers (overlays, playlist)
        # know the real types.
        self._cfg: Any = None
        self._orchestrator: Any = None
        self._is_conductor: bool = False
        self._system_index: int = 0

    def competes_for_audio_lock(self) -> bool:
        """Whether THIS instance contends for the ensemble audio slot.
        `WANTS_AUDIO_LOCK` declares the capability at the class level;
        instances opt out when their audio is actually disabled (e.g. a
        muted video), so a silent scene is selectable like any
        non-audio scene. SID-driving scenes (waveform/midi) always
        compete — they output through the chip regardless of the
        AudioStreamer, so they don't override this."""
        return self.WANTS_AUDIO_LOCK

    def prepare_next(self) -> None:
        """Called by the Playlist right before the interstitial that
        precedes this scene is built. Randomized scenes override this to
        pick their file now so the "UP NEXT" card shows the real upcoming
        content (and so the pick isn't deferred to setup(), which runs
        after the card is already on screen). Default: no-op."""

    def setup(self) -> None:
        self.is_done = False
        self.prev_frame = None
        # Clear any inter-frame effect state so a looping or re-entered scene
        # doesn't ghost a trail from the previous iteration.
        if self.effect is not None:
            self.effect.reset()
        # Apply this scene's pre-emphasis to the shared streamer before the
        # subclass brings audio up (mic start / video pre-encode read the
        # updated DSP params). No-op when the scene has no audio.
        if self.audio is not None:
            self.audio.set_pre_emphasis(self.pre_emphasis)
        if self.display_mode is not None:
            self.display_mode.setup(self.api)
        mode_name = type(self.display_mode).__name__ if self.display_mode is not None else "none"
        fps_str = f"{self.target_fps:.0f}fps" if self.target_fps else "auto-fps"
        overlay_names = [getattr(ov, "name", type(ov).__name__) for ov in self.overlays]
        ov_str = ", ".join(overlay_names) if overlay_names else "no overlays"
        # VideoScene sets duration_s = math.inf because its lifetime
        # is video-driven; format that distinctly instead of "inf.0s".
        dur_str = "video-driven" if math.isinf(self.duration_s) else f"{self.duration_s:.1f}s"
        log.info(
            "scene %r: mode=%s duration=%s %s [%s]", self.name, mode_name, dur_str, fps_str, ov_str
        )

    def apply_smoothing(self, img: np.ndarray) -> np.ndarray:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = self.clahe.apply(l)
        img = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

        if self.prev_frame is None or self.prev_frame.shape != img.shape:
            self.prev_frame = img.astype(np.float32)
        else:
            alpha = 0.6
            self.prev_frame = cv2.addWeighted(
                img.astype(np.float32), alpha, self.prev_frame, 1 - alpha, 0
            )
            img = self.prev_frame.astype(np.uint8)
        return img

    def process_frame(self, current_time: float) -> bool:
        raise NotImplementedError

    def teardown(self) -> None:
        # Give the display mode a chance to undo any C64-side state that
        # outlives the scene boundary — currently only HiresDisplayMode
        # with use_reu_staged, which leaves a raster IRQ hooked at $0314.
        # Default DisplayMode.teardown is a no-op, so this is harmless
        # for every other mode. Runs FIRST so subclasses with audio.stop()
        # don't pile teardown latency on top of an IRQ that's still
        # firing into a half-installed handler.
        if self.display_mode is not None:
            try:
                self.display_mode.teardown(self.api)
            except Exception:
                log.exception("display_mode.teardown failed; continuing")


class WebcamScene(Scene):
    """Live webcam scene optimized for low latency.

    Reads each camera frame and pushes it straight through to the display
    mode — no delay buffer. Audio is attached whenever the global
    [audio].enabled is on; a per-scene `audio = false` opts back out (see
    config.SceneCfg.audio). When attached, mic capture runs independently
    of the video (no sync).

    Congestion handling lives in the Playlist's deadline-based frame
    dropper; no per-scene queue-fill check needed since DMA writes go
    over a persistent socket without an in-process queue.
    """

    def __init__(
        self,
        api: C64Backend,
        audio: AudioStreamer | None,
        display_mode: DisplayMode,
        source: WebcamSource,
        audio_cfg: AudioCfg,
        name: str,
    ):
        super().__init__(api, audio, display_mode, name)
        self.source = source
        self.audio_cfg = audio_cfg
        self.start_time = 0.0
        self._frame_count = 0

    def setup(self) -> None:
        super().setup()
        self.start_time = time.time()
        # The webcam mic path is always the 4-bit DAC streamer (the sampler is a
        # video-only backend), so narrow to AudioStreamer for start_mic.
        if isinstance(self.audio, AudioStreamer):
            # Mirror VideoScene: when the display mode installs the
            # bank-swap merged dispatcher at $0314 (audio_reu_pump_active
            # flag on a bitmap mode with use_reu_staged), the mic REU pump
            # install must skip its own $0314 hook.
            skip_hook = bool(getattr(self.display_mode, "audio_reu_pump_active", False))
            self.audio.start_mic(
                self.audio_cfg.device,
                self.audio_cfg.mic_sensitivity,
                self.audio_cfg.noise_gate,
                skip_irq_vector_hook=skip_hook,
            )

    def _read_frame(self) -> np.ndarray | None:
        img = self.source.read()
        if img is None:
            return None
        img = self.apply_smoothing(img)
        img = _crop_to_aspect(img)
        return cv2.flip(img, 1)

    def process_frame(self, current_time: float) -> bool:
        if (current_time - self.start_time) >= self.duration_s:
            return False
        img = self._read_frame()
        if img is not None:
            if self.show_frame_numbers:
                self._frame_count += 1
                label = f"{_timecode(current_time - self.start_time)} f{self._frame_count}"
                img = _annotate_frame_number(img, label)
            assert self.display_mode is not None
            _render_with_overlays(
                self.display_mode, self.api, img, self.overlays, current_time, self
            )
        return True

    def teardown(self) -> None:
        super().teardown()
        if self.audio:
            self.audio.stop()


def _render_with_overlays(
    display_mode: DisplayMode,
    api: C64Backend,
    frame: np.ndarray | None,
    overlays: list[Overlay],
    t: float,
    scene: Scene,
    modulation: MusicModulation | None = None,
) -> None:
    """Compose the frame, let buffer-painting overlays mutate it, then push.

    For display modes that support compose(): build screen+color buffers,
    invoke each compose-based overlay's compose() to paint into them, then
    push once. Single combined write per frame — no scene/overlay flicker
    from interleaved writes.

    For display modes that don't (bitmap modes): fall back to render().
    Bitmap overlays don't paint into screen/color RAM, so there's nothing
    to compose.

    `frame` may be None for BlankDisplayMode (no video input). The base
    DisplayMode.compose/render signature requires a real array, so callers
    that hit the None branch supply an empty placeholder — Blank's compose
    ignores its arg, and bitmap modes never get a None frame in practice.

    A per-scene pixel effect (scene.effect), if present, transforms the source
    frame here — before downscale/quantization — so it applies uniformly to
    every frame-bearing scene. `modulation` (a music-feature snapshot, None on
    non-reactive scenes) is handed to the effect so reactive effects can react
    to the beat. Skipped when there's no frame (Blank). A failing effect disables
    itself rather than killing the scene."""
    prof = get_profiler()
    if frame is not None and scene.effect is not None:
        try:
            frame = scene.effect.apply(frame, t, modulation)
        except Exception:
            log.exception(
                "effect %r failed on %r — disabling",
                getattr(scene.effect, "name", scene.effect),
                scene.name,
            )
            scene.effect = None
    frame_arg = frame if frame is not None else np.empty(0, dtype=np.uint8)
    if not display_mode.supports_compose:
        with prof.stage("render"):
            display_mode.render(api, frame_arg)
        return
    with prof.stage("compose"):
        buffers = display_mode.compose(frame_arg)
    with prof.stage("overlay_compose"):
        for ov in overlays:
            if not getattr(ov, "PAINTS_INTO_BUFFERS", False):
                continue
            if getattr(ov, "_disabled", False):
                continue
            try:
                ov.compose(buffers, scene, t)
            except Exception:
                log.exception(
                    "overlay %r compose failed on %r — disabling",
                    getattr(ov, "name", ov),
                    scene.name,
                )
                ov._disabled = True
    with prof.stage("push"):
        display_mode.push(api, buffers)


class SourceScene(Scene):
    """Composable scene: a FrameSource × an AudioSource × a display mode.

    Generalizes the live-frame pattern (WebcamScene/SlideshowScene): read a
    frame from the source at the scene clock, optionally run the scene's pixel
    effect (applied inside _render_with_overlays), quantize via the display
    mode, push — overlays compose on top. The source decides the scene's
    lifetime: infinite sources (generative art) run until `duration_s`; a
    finite source ends the scene when it reports `finished`.

    Audio is delegated to the AudioSource building block (silence, live mic,
    and — later — SID playback / sampled streaming), chosen independently of
    the video source. The base `audio` reference is still passed so the shared
    streamer's per-scene pre-emphasis hook works; the AudioSource owns
    start/stop.
    """

    def __init__(
        self,
        api: C64Backend,
        audio: AudioStreamer | None,
        display_mode: DisplayMode,
        source: FrameSource,
        audio_source: AudioSource,
        name: str,
    ):
        super().__init__(api, audio, display_mode, name)
        self.source = source
        self.audio_source = audio_source
        self.start_time = 0.0
        self._frame_count = 0

    def competes_for_audio_lock(self) -> bool:
        # The audio building block decides: a mic/silent source doesn't claim
        # the ensemble SID spotlight; a future SID-playback source would.
        return self.audio_source.wants_audio_lock

    def setup(self) -> None:
        super().setup()
        self.start_time = time.time()
        self.source.setup()
        # The audio source can fail on real content (a SID source rejects an
        # RSID / a tune that loads too low / one whose payload clobbers the
        # display). The playlist does NOT wrap setup() in try/except, so a
        # raise here would crash the run loop — instead self-abort like
        # VideoScene: log, flip is_done, and let the playlist advance.
        try:
            self.audio_source.setup()
        except Exception:
            log.exception(
                "scene %r: audio source %s failed to start — aborting scene",
                self.name,
                type(self.audio_source).__name__,
            )
            self.is_done = True
        # A SID audio source kicks its player via the firmware's run_prg, which
        # re-inits the machine to text mode — clobbering the VIC mode the display
        # configured in super().setup() (which runs BEFORE the audio source). A
        # bitmap display (mhires/hires) would then render its $0400 colour-nibble
        # bytes as PETSCII. Re-assert the display AFTER the player, the same order
        # WaveformScene uses. invalidate_cache first so the next frame fully
        # repaints against the player-disturbed RAM.
        if (
            not self.is_done
            and self.display_mode is not None
            and getattr(self.audio_source, "resets_display", False)
        ):
            self.api.invalidate_cache()
            self.display_mode.setup(self.api)

    def process_frame(self, current_time: float) -> bool:
        # setup() flips is_done when the audio source failed to start (e.g. a
        # SID source whose tune run_sid_player refuses). The generative
        # FrameSource's `finished` is always False, so without this guard the
        # playlist's `is_done = not still_active` would clobber the abort and
        # play silent video for the full duration. Honor it like a finished
        # source so the scene tears down and the playlist advances.
        if self.is_done:
            return False
        if self.source.finished:
            return False
        if (current_time - self.start_time) >= self.duration_s:
            return False
        # Music-reactive scenes: the audio source exposes a live feature snapshot
        # (None when it has no feature stream), which the source reads to
        # modulate its frame. Non-reactive sources ignore it.
        modulation = self.audio_source.features()
        frame = self.source.read(current_time - self.start_time, modulation)
        if frame is not None:
            if self.show_frame_numbers:
                self._frame_count += 1
                label = f"{_timecode(current_time - self.start_time)} f{self._frame_count}"
                frame = _annotate_frame_number(frame, label)
            assert self.display_mode is not None
            _render_with_overlays(
                self.display_mode, self.api, frame, self.overlays, current_time, self, modulation
            )
        return True

    def teardown(self) -> None:
        # Display teardown first (unhook any IRQ), then stop audio + source —
        # mirrors WebcamScene so audio.stop() latency doesn't pile on a still-
        # firing IRQ.
        super().teardown()
        try:
            self.audio_source.teardown()
        finally:
            self.source.teardown()


class BlankScene(Scene):
    """A scene with no video input — just a blank canvas for overlays.

    Pairs with BlankDisplayMode (configurable border + background). Useful
    as a stage for title cards, big-text scrollers, RSS tickers, etc.,
    where a webcam feed would just compete with the overlays.
    """

    def __init__(
        self,
        api: C64Backend,
        audio: AudioStreamer | None,
        display_mode: DisplayMode,
        audio_cfg: AudioCfg,
        name: str,
    ):
        super().__init__(api, audio, display_mode, name)
        self.audio_cfg = audio_cfg
        self.start_time = 0.0

    def setup(self) -> None:
        super().setup()
        self.start_time = time.time()
        # Only start mic capture if the scene opted in *and* the global
        # audio is enabled — same model as WebcamScene. Always the DAC streamer.
        if isinstance(self.audio, AudioStreamer):
            self.audio.start_mic(
                self.audio_cfg.device, self.audio_cfg.mic_sensitivity, self.audio_cfg.noise_gate
            )

    def process_frame(self, current_time: float) -> bool:
        # Always paint — the Playlist's busy-defer flips is_done back to
        # False when an overlay (e.g. big_text) reports busy, and a scene
        # that stopped rendering past duration_s would freeze the screen
        # mid-message until the next teardown+setup cycle.
        assert self.display_mode is not None
        _render_with_overlays(self.display_mode, self.api, None, self.overlays, current_time, self)
        return (current_time - self.start_time) < self.duration_s

    def teardown(self) -> None:
        super().teardown()
        if self.audio:
            self.audio.stop()


class SlideshowScene(Scene):
    """Cycle through still images for the scene's duration.

    File spec mirrors VideoScene's grammar (comma-separated paths,
    directories, and globs — see `resolve_file_spec`). Each `setup()`
    re-resolves so directory contents can change between iterations. A
    shuffle-and-walk picker guarantees every image in the pool gets shown
    before any repeats; the first pick after a reshuffle is swapped with
    the second when the pool has more than one entry, so the same image
    never appears twice back-to-back across reshuffle boundaries.

    Per-image timing is controlled by `image_duration_s`; total scene
    runtime by the base-class `duration_s`. The two are independent —
    cycling stops when `duration_s` expires regardless of how many images
    have been shown.

    No audio (silent like BlankScene). No CLAHE / temporal EMA — the
    webcam smoothing pipeline blends consecutive frames, which would
    produce ugly cross-fades between unrelated stills.
    """

    def __init__(
        self,
        api: C64Backend,
        display_mode: DisplayMode,
        file: str,
        *,
        image_duration_s: float = 5.0,
        display_spec: str = "mhires",
        palette_mode: str = "percell",
        border: int = 0,
        background: int = 0,
        style: str = "default",
        use_reu_staged: bool | str = "auto",
        double_buffer: bool | str = "auto",
        reu_available: bool = False,
        backend_supports_reu: bool = False,
        audio_reu_pump_active: bool = False,
        color: ColorCfg | None = None,
        text_double_height: bool = False,
        aspect_mode: str = "crop",
    ):
        from .config import PICTURE_EXTS, ColorCfg, resolve_file_spec

        self.file_spec = file
        self.image_duration_s = float(image_duration_s)
        # How each image is fit to the C64 aspect before the display mode
        # downscales it: "crop" (center-crop to fill — the default everywhere),
        # "fit" (letterbox/pillarbox so the whole image shows), or "stretch"
        # (no aspect handling — the mode's resize distorts to fill). See
        # _apply_aspect.
        self._aspect_mode = aspect_mode
        # The whole [color] section travels as one object — it drives both the
        # display-mode shaping (channel_boost / hue_corrections, applied at
        # construction) AND the per-image stages installed here at setup time:
        # the adaptive color fit ([color].auto_fit) and the forced-palette remap
        # ([color].force_palette), both recomputed per slide in _advance_image so
        # every photo is optimized while staying stable within a slide.
        self._color = color if color is not None else ColorCfg()
        # Original spec (may be "random") — re-resolved at every setup()
        # so single-scene loops get a fresh display mode per iteration.
        self.display_spec = display_spec
        # Stash the build kwargs so setup() can rebuild a fresh display
        # mode when display_spec == "random" without re-plumbing through
        # SceneCfg/Config. Stored as individual attrs so mypy can keep
        # the original types (a dict[str, object] would erase them).
        self._palette_mode = palette_mode
        self._border = border
        self._background = background
        self._style = style
        self._text_double_height = text_double_height
        # Stored as the raw tri-state setting + the probe verdict (not a
        # resolved bool) so a `display = "random"` rebuild can re-decide REU
        # staging per concrete mode each setup() — auto stages bitmap picks
        # but leaves char-mode picks on host-DMA. See config.resolve_use_reu_staged.
        self._reu_staged_setting = use_reu_staged
        self._reu_available = reu_available
        # Host-DMA double-buffer (no-REU backends): stored as the raw tri-state +
        # the backend's REU capability so a `display = "random"` rebuild can
        # re-decide it per concrete mode each setup(). See config.resolve_double_buffer.
        self._double_buffer_setting = double_buffer
        self._backend_supports_reu = backend_supports_reu
        self._audio_reu_pump_active = audio_reu_pump_active
        candidates = resolve_file_spec(file, PICTURE_EXTS, label="slideshow")
        if len(candidates) == 1:
            scene_name = f"Slideshow: {_display_name(candidates[0])}"
        else:
            scene_name = f"Slideshow: {file}"
        super().__init__(api, None, display_mode, scene_name)
        self._shuffle_bag: list[str] = []
        self._current_path: str | None = None
        self._current_img: np.ndarray | None = None
        self._image_start: float = 0.0
        self.start_time: float = 0.0
        # True when prepare_next() has already loaded the opening slide (and
        # updated self.name); setup() then skips the re-pick. See
        # VideoScene._prepared for the full rationale.
        self._prepared = False

    def _resolve_candidates(self) -> list[str]:
        from .config import PICTURE_EXTS, resolve_file_spec

        return resolve_file_spec(self.file_spec, PICTURE_EXTS, label="slideshow")

    def _maybe_rebuild_display_mode(self) -> None:
        """When display_spec is "random", pick a fresh concrete mode and
        rebuild the DisplayMode. Tears down the previous one cleanly. No-op
        otherwise."""
        if self.display_spec != "random":
            return
        from .config import (
            _build_display_mode,
            _resolve_slideshow_display,
            resolve_double_buffer,
            resolve_use_reu_staged,
        )

        new_name = _resolve_slideshow_display(self.display_spec)
        old = self.display_mode
        if old is not None:
            try:
                old.teardown(self.api)
            except Exception:
                log.exception("slideshow: prior display_mode teardown failed; continuing")
        reu_staged = resolve_use_reu_staged(
            self._reu_staged_setting,
            new_name,
            reu_available=self._reu_available,
            # Text overlays fold into the bitmap; under auto they prefer the
            # crisp host-DMA path over the REU bank-swap (which shimmers fine
            # glyphs). See config.resolve_use_reu_staged.
            has_buffer_overlays=any(
                getattr(ov, "PAINTS_INTO_BUFFERS", False) for ov in self.overlays
            ),
        )
        self.display_mode = _build_display_mode(
            new_name,
            palette_mode=self._palette_mode,
            border=self._border,
            background=self._background,
            style=self._style,
            use_reu_staged=reu_staged,
            double_buffer=resolve_double_buffer(
                self._double_buffer_setting,
                new_name,
                use_reu_staged=reu_staged,
                backend_supports_reu=self._backend_supports_reu,
            ),
            audio_reu_pump_active=self._audio_reu_pump_active,
            color=self._color,
            text_double_height=self._text_double_height,
        )
        log.info("slideshow: display = random → %s", new_name)

    def _advance_image(self) -> None:
        """Pop the next image from the shuffle bag; reshuffle when empty."""
        try:
            candidates = self._resolve_candidates()
        except ValueError as e:
            log.error("slideshow: file spec %r failed to resolve at advance: %s", self.file_spec, e)
            self.is_done = True
            return
        while True:
            if not self._shuffle_bag:
                self._shuffle_bag = list(candidates)
                random.shuffle(self._shuffle_bag)
                # No-immediate-repeat across reshuffle boundaries.
                if len(self._shuffle_bag) > 1 and self._shuffle_bag[0] == self._current_path:
                    self._shuffle_bag[0], self._shuffle_bag[1] = (
                        self._shuffle_bag[1],
                        self._shuffle_bag[0],
                    )
            path = self._shuffle_bag.pop(0)
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            if img is None:
                log.warning("slideshow: failed to decode %s; skipping", path)
                # Drop it from future candidates this iteration.
                self._shuffle_bag = [p for p in self._shuffle_bag if p != path]
                if not self._shuffle_bag:
                    # Whole pool consumed and nothing decoded — bail rather
                    # than infinite-loop.
                    self.is_done = True
                    return
                continue
            self._current_path = path
            self._current_img = _apply_aspect(img, self._aspect_mode)
            if self.display_mode is not None:
                # Per-image color stages: build the enabled accumulators, feed
                # the one cropped image, install both results. None clears any
                # stale state from the previous slide.
                c = self._color
                if c.auto_fit:
                    fit_acc = ColorFitAccumulator(strength=c.auto_fit_strength)
                    fit_acc.add(self._current_img)
                    self.display_mode.set_color_fit(fit_acc.result())
                if c.force_palette:
                    map_acc = ColorMapAccumulator(
                        n_colors=c.force_palette_colors, indices=c.force_palette_indices or None
                    )
                    map_acc.add(self._current_img)
                    self.display_mode.set_color_map(map_acc.result())
            self.name = f"Slideshow: {_display_name(path)}"
            self._image_start = time.time()
            log.info(
                "slideshow: showing %s (%d remaining in bag)",
                os.path.basename(path),
                len(self._shuffle_bag),
            )
            return

    def _pick_first_image(self) -> bool:
        """Reset the shuffle bag and load the opening slide (updating
        self.name to it, extension stripped). Returns False if the file
        spec no longer resolves to anything."""
        self._maybe_rebuild_display_mode()
        # Reset shuffle state so each scene entry starts a fresh pass.
        self._shuffle_bag = []
        self._current_path = None
        self._current_img = None
        try:
            self._resolve_candidates()
        except ValueError as e:
            log.error("slideshow: file spec %r failed to resolve at setup: %s", self.file_spec, e)
            return False
        self._advance_image()
        return True

    def prepare_next(self) -> None:
        """Load the opening slide now so the preceding interstitial shows
        the actual first image rather than the directory spec / the last
        slide of the previous run. setup() consumes this pick."""
        if self._pick_first_image():
            self._prepared = True

    def setup(self) -> None:
        # Consume a prepare_next() pick if present; else pick now
        # (single-scene loops / pause-resume skip prepare_next).
        if self._prepared:
            self._prepared = False
        elif not self._pick_first_image():
            super().setup()
            self.is_done = True
            return
        super().setup()
        # Re-anchor timers to now: prepare_next may have loaded the slide
        # seconds ago during the interstitial, so _image_start (stamped by
        # _advance_image then) would short-change the first slide.
        self.start_time = time.time()
        self._image_start = time.time()
        # _advance_image (in _pick_first_image) may have failed to decode
        # any image; super().setup() just cleared is_done, so re-assert it.
        if self._current_img is None:
            self.is_done = True

    def process_frame(self, current_time: float) -> bool:
        if (current_time - self.start_time) >= self.duration_s:
            return False
        if self._current_img is None:
            return False
        if (current_time - self._image_start) >= self.image_duration_s:
            self._advance_image()
            if self.is_done or self._current_img is None:
                return False
        img = self._current_img
        if self.show_frame_numbers:
            label = (
                f"{_timecode(current_time - self.start_time)} "
                f"{os.path.basename(self._current_path or '')}"
            )
            img = _annotate_frame_number(img, label)
        assert self.display_mode is not None
        _render_with_overlays(self.display_mode, self.api, img, self.overlays, current_time, self)
        return True


class VideoScene(Scene):
    """PyAV-driven A/V playback with audio-master sync.

    The demuxer runs on its own thread, pushing resampled audio straight into
    the AudioStreamer queue. process_frame() asks the AudioStreamer for the
    current playback position and picks the latest video frame whose PTS ≤
    that position. Frames behind get dropped; frames ahead wait. Drift can't
    accumulate because the audio clock IS the reference.
    """

    WANTS_AUDIO_LOCK = True

    def competes_for_audio_lock(self) -> bool:
        # A muted video (audio = false, or global [audio].enabled
        # off) has self.audio is None and produces no sound — it doesn't
        # need the ensemble audio slot and should be selectable like any
        # non-audio scene.
        return self.WANTS_AUDIO_LOCK and self.audio is not None

    def __init__(
        self,
        api: C64Backend,
        audio: SceneAudio | None,
        display_mode: DisplayMode,
        file: str,
        prepend_alignment_marker: bool = False,
        color: ColorCfg | None = None,
        start_s: float = 0.0,
    ):
        """`file` is a comma-separated `resolve_file_spec` spec (or a single
        literal path — the spec grammar treats one path as a one-entry
        pool). The candidate pool is resolved here once; each `setup()`
        re-resolves so a directory's contents can change between scene
        repeats. Single-entry pools stay deterministic."""
        from .config import VIDEO_EXTS, resolve_file_spec

        self.file_spec = file
        # Initial resolution so __init__ raises on bad specs (mirrors the
        # validate_scene_cfg check; also covers auto-interleaved scenes
        # built without going through validate). Picked again at setup.
        candidates = resolve_file_spec(file, VIDEO_EXTS, label="video")
        # Display the spec in the scene name when the pool has multiple
        # entries — the picked file's basename gets prefixed at setup.
        if len(candidates) == 1:
            scene_name = f"Video: {_display_name(candidates[0])}"
        else:
            scene_name = f"Video: {file}"
        super().__init__(api, audio, display_mode, scene_name)
        # True when prepare_next() has already chosen this iteration's file
        # (and updated self.name) so setup() consumes that pick instead of
        # re-rolling. Reset to False each setup so single-scene loops /
        # pause-resume (which skip prepare_next) still pick fresh.
        self._prepared = False
        # `filepath` is the currently-chosen path (set at each setup()).
        # Initialise to the deterministic single-entry case so callers
        # introspecting before setup see a real path; multi-entry pools
        # overwrite this in setup().
        self.filepath = candidates[0]
        self.source: AVFileSource | None = None
        self._start_time = 0.0
        # Seconds into the file to begin playback (0 = from the start). Passed
        # to AVFileSource at setup(), which seeks + rebases PTS. Quick playback
        # derives this from a URL's t=/start= timestamp.
        self.start_s = max(0.0, start_s)
        self._last_rendered_img: np.ndarray | None = None
        # A/V-lag telemetry (reset each setup): per-displayed-frame
        # audio_clock - displayed_frame_pts. Small + (≤ one frame interval) is
        # healthy; a growing + means the decoder is falling behind the
        # audio-master clock (the 4K-decode-bound symptom). See _log_av_lag.
        self._av_lag_min = math.inf
        self._av_lag_max = -math.inf
        self._av_lag_sum = 0.0
        self._av_lag_count = 0
        self._av_buf_min = math.inf
        self._av_last_log_t = 0.0
        # Rolling-window auto_fit state (set up in setup() when [color].auto_fit
        # is on without force_palette). None = no online fit (pre-scanned or
        # disabled). See ONLINE_FIT_WARMUP_FRAMES.
        self._online_fit: ColorFitAccumulator | None = None
        self._online_fit_frames = 0
        # Capture-anchor marker (see audio_marker.py + AudioCfg.source_
        # alignment_marker). Only honored on the REU pre-encode path.
        self.prepend_alignment_marker = prepend_alignment_marker
        # The whole [color] section travels as one object. It drives both the
        # display-mode shaping (channel_boost / hue_corrections) AND the
        # per-video stages installed at setup() from a one-shot pre-scan of the
        # picked file: the adaptive color fit ([color].auto_fit) and the
        # forced-palette remap ([color].force_palette). See prescan_source_color.
        from .config import ColorCfg

        self._color = color if color is not None else ColorCfg()
        # Lifetime is video-driven: `process_frame` returns False when the
        # source signals `finished`, advancing the playlist. math.inf
        # disables the base-class duration timer (a finite duration_s would
        # truncate playback partway through long files); the config layer
        # rejects any user-supplied `duration_s` so this stays consistent.
        self.duration_s = math.inf

    def _resolve_candidates(self) -> list[str]:
        from .config import VIDEO_EXTS, resolve_file_spec

        return resolve_file_spec(self.file_spec, VIDEO_EXTS, label="video")

    def _pick_filepath(self) -> bool:
        """Re-resolve the spec (directories rescan between iterations so
        newly dropped files become eligible), pick a random candidate, and
        refresh self.name to the picked file (extension stripped) so the
        interstitial card + heartbeat log show it. Returns False if the spec
        no longer resolves to anything."""
        try:
            candidates = self._resolve_candidates()
        except ValueError as e:
            log.error("video: file spec %r failed to resolve at setup: %s", self.file_spec, e)
            return False
        self.filepath = random.choice(candidates)
        self.name = f"Video: {_display_name(self.filepath)}"
        if len(candidates) > 1:
            log.info(
                "video: picked %s from %d candidates",
                os.path.basename(self.filepath),
                len(candidates),
            )
        return True

    def prepare_next(self) -> None:
        """Pick the upcoming file now so the preceding interstitial shows
        the real filename instead of the directory spec / a stale prior
        pick. setup() consumes this pick (skips re-rolling)."""
        if self._pick_filepath():
            self._prepared = True

    def setup(self) -> None:
        # Consume a prepare_next() pick if there is one; otherwise pick now
        # (single-scene loops and pause/resume re-setup skip prepare_next).
        # On a resolve failure the base-class setup still runs but is_done
        # flips immediately.
        if self._prepared:
            self._prepared = False
        elif not self._pick_filepath():
            super().setup()
            self.is_done = True
            return
        super().setup()
        if not _ensure_pyav():
            log.warning(
                "PyAV unavailable; video scene cannot play %s "
                "(install with `pip install c64cast[video]`)",
                self.filepath,
            )
            self.is_done = True
            return
        is_url = self.filepath.lower().startswith(("http://", "https://"))
        if not is_url and not os.path.exists(self.filepath):
            log.error(
                "video: file not found: %s — check the path in "
                "your config or the [playlist].videos_dir contents",
                self.filepath,
            )
            self.is_done = True
            return
        sr = self.audio.sample_rate if self.audio else 8000
        # The peak scan only matters when AVFileSource will push audio with
        # per-frame gain — i.e. the non-REU audible path. A muted scene
        # (self.audio is None) never pushes; the REU path pre-encodes audio
        # with its own gain and tells the demuxer to skip audio. Skipping the
        # scan in those cases removes a full audio decode from setup.
        will_push_audio = self.audio is not None and not getattr(self.audio, "use_reu_pump", False)
        # The only resolution the display mode consumes (≤320×200). Passed to
        # AVFileSource so it downscales each frame DURING decode instead of
        # converting the full source frame — the supply-side fix for video
        # lagging audio on heavy/4K clips. See video._plan_decode_size.
        decode_target = getattr(self.display_mode, "frame_target_size", None)
        try:
            self.source = AVFileSource(
                self.filepath,
                target_sample_rate=sr,
                scan_audio_peak=will_push_audio,
                start_s=self.start_s,
                decode_target_size=decode_target,
            )
        except PermissionError as e:
            log.error("video: permission denied opening %s (%s)", self.filepath, e)
            self.is_done = True
            return
        except Exception as e:
            log.error(
                "video: failed to open %s (%s) — file may be corrupt or in an unsupported codec",
                self.filepath,
                e,
            )
            self.is_done = True
            return

        c = self._color
        # Per-video color stages. force_palette needs a blocking pre-scan (its
        # k-means false-color map must be fixed before the first frame, or the
        # mapping would shift mid-playback); auto_fit on its own does NOT —
        # it converges online over a warmup window, which removes the pre-scan
        # decode (the bulk of the startup pause) from the common path.
        self._online_fit = None
        self._online_fit_frames = 0
        # Reset A/V-lag telemetry so a looped/re-entered scene starts clean.
        self._av_lag_min = math.inf
        self._av_lag_max = -math.inf
        self._av_lag_sum = 0.0
        self._av_lag_count = 0
        self._av_buf_min = math.inf
        self._av_last_log_t = 0.0
        if self.display_mode is not None:
            if c.force_palette:
                # One pre-scan pass derives the map (and the fit, since it's
                # already decoding). None clears stale state from a prior file.
                fit, cmap = prescan_source_color(
                    self.filepath,
                    fit_strength=c.auto_fit_strength if c.auto_fit else None,
                    map_colors=c.force_palette_colors,
                    map_indices=(c.force_palette_indices or None),
                    decode_target_size=decode_target,
                )
                self.display_mode.set_color_fit(fit)
                self.display_mode.set_color_map(cmap)
                if fit is not None:
                    log.info("video: auto-fit %s", fit)
                if cmap is not None:
                    log.info(
                        "video: forced palette → %d colors %s",
                        len(cmap.indices),
                        list(cmap.indices),
                    )
            elif c.auto_fit:
                # Rolling/online auto_fit: start neutral, converge during
                # playback (see process_frame + ONLINE_FIT_WARMUP_FRAMES).
                self.display_mode.set_color_fit(None)
                self.display_mode.set_color_map(None)
                self._online_fit = ColorFitAccumulator(strength=c.auto_fit_strength)
                log.info(
                    "video: auto-fit converging online over first %d frames",
                    ONLINE_FIT_WARMUP_FRAMES,
                )
            else:
                self.display_mode.set_color_fit(None)
                self.display_mode.set_color_map(None)

        has_audio = (self.source.a_stream is not None) and (self.audio is not None)
        if has_audio and isinstance(self.audio, UltimateAudioSampler):
            # Ultimate Audio FPGA sampler path: bring up the streaming REU ring
            # (prefill + gate the A↔B loop), then feed the demuxer's decoded
            # int16 straight to its push_samples. No SID/$D418/NMI bring-up —
            # the FPGA plays from REU off the C64 bus. position_seconds()/stop()
            # are polymorphic, so _clock_s() + teardown() are unchanged.
            self.audio.start()
            self.source.start(audio_push=self.audio.push_samples)
        elif has_audio and getattr(self.audio, "use_reu_pump", False):
            # REU-staged path: pre-decode entire audio track, 4-bit encode
            # with the same gain/dither pipeline as the host-DMA path uses
            # per sample, then upload to REU. Video frames still come from
            # the AVFileSource demuxer thread; pass audio_push=None so the
            # demuxer SKIPS audio decode entirely (otherwise it competes
            # with video decode in the same thread for CPU, causing
            # noticeable video lag at scene start until the demuxer catches up).
            assert isinstance(self.audio, AudioStreamer)  # DAC streamer (not sampler)
            audio_4bit = self._preencode_audio_for_reu()
            # Bitmap display modes (hires/mhires) push ~300 KB/sec via host
            # DMAWRITE which halts the C64 bus ~30 % of the time. NMI service
            # drops from 8 kHz to ~5 kHz under that load — the default REU
            # pump chunk size = 128 then over-produces by ~2× and overflows
            # the audio ring in ~2 sec (audible as accelerating distortion +
            # static). Smaller chunk keeps the production rate close to the
            # bus-halt-reduced consumption rate.
            # Chunk-size choice for the REU audio pump:
            #   * Bitmap modes via host-DMAWRITE (use_reu_staged=False):
            #     bus is halted in long unpredictable bursts as the host
            #     dumps 8K bitmap + 1K screen + 1K color. NMI loses ~50%
            #     of ticks. HEAVY_BUS chunk (80) keeps production matched
            #     to the reduced consumption rate.
            #   * Bitmap modes via REU bank-swap (use_reu_staged=True):
            #     the bus halt moves to a single ~10ms vblank-aligned
            #     event per source frame (~30 Hz). NMI still loses ticks
            #     during the halt, but the halt is deterministic and
            #     bounded. The default chunk (128) is matched to nominal
            #     NMI rate; production/consumption stay balanced because
            #     halts block both proportionally.
            #   * Char modes (default else branch): no bitmap traffic at
            #     all, default chunk fine.
            chunk = (
                REU_PUMP_CHUNK_SIZE_HEAVY_BUS
                if isinstance(self.display_mode, BitmapDisplayMode)
                else None
            )
            # When the display mode also installs the bank-swap dispatcher
            # at $0314 (the merged variant JMPs to $C100 on non-raster
            # IRQs), the audio install must skip its own $0314 hook so it
            # doesn't clobber the dispatcher. The dispatcher's installer
            # already pre-uploaded a JMP $EA31 stub at $C100 covering the
            # gap until this method writes real audio bytes there.
            skip_hook = bool(getattr(self.display_mode, "audio_reu_pump_active", False))
            self.audio.start_for_reu_staged(
                audio_4bit, chunk_size=chunk, skip_irq_vector_hook=skip_hook
            )
            self.source.start(audio_push=None)
        elif has_audio:
            assert isinstance(self.audio, AudioStreamer)  # DAC streamer (not sampler)
            self.audio.start_for_external_source()
            self.source.start(audio_push=self.audio.push_samples)
        else:
            self.source.start(audio_push=None)
        self._start_time = time.time()

    def _preencode_audio_for_reu(self) -> bytes:
        """Decode the entire audio track to mono int16, apply the same
        peak-normalization gain AVFileSource would, then 4-bit encode for
        the SID DAC. Returns bytes ready for AudioStreamer.start_for_reu_staged.

        Matches the encoding pipeline of AudioStreamer._encode_and_enqueue
        + AVFileSource's peak-normalization, so audio levels are identical
        whether REU mode is on or off.

        If ``self.prepend_alignment_marker`` is True, a 100 ms chirp from
        audio_marker.py is prepended to the encoded bytes — plays as a
        brief blip at scene start, then real content begins. Used to
        anchor Cam Link captures to a known source-timeline-zero for
        cross-capture comparison."""
        # The REU-pump pre-encode is a DAC-streamer-only path (the sampler
        # streams 16-bit PCM through its own ring); narrow to AudioStreamer.
        assert isinstance(self.audio, AudioStreamer)
        sr = self.audio.sample_rate
        # Decode full audio to int16 mono at sample rate.
        int16 = decode_audio_full(self.filepath, sr)
        if int16.size == 0:
            log.warning("video: empty audio track after decode; REU pump will play silence")
            return b""
        # Apply the same peak-normalization gain AVFileSource computes.
        peak = int(np.abs(int16).max())
        gain = _compute_normalization_gain(peak)
        if gain != 1.0:
            int16 = np.clip(int16.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
        log.info("video: REU pre-encode peak=%d → gain=%.2fx (%d samples)", peak, gain, int16.size)
        # Float → 4-bit DAC code via the shared encoder (identical math to the
        # mic paths). Use an explicit Generator so this offline pass doesn't
        # perturb the global RNG state the realtime callbacks draw from.
        floats = int16.astype(np.float32) / INT16_FULL_SCALE
        # Apply the host DSP chain (compressor/limiter/expander/pre-emphasis)
        # over the whole track so REU-staged video audio matches the
        # host-DMA path, which applies the same DSP per chunk in
        # _encode_and_enqueue. No-op when [dsp].enabled is false.
        floats = self.audio.process_offline_dsp(floats)
        rng = np.random.default_rng() if self.audio.dither_enabled else None
        vol = encode_floats_to_dac(floats, dither=self.audio.dither_enabled, rng=rng)
        # numpy.ndarray.tobytes() returns Any per the stubs; cast for strict
        # mypy. Runtime guarantee: ndarray.tobytes() returns bytes.
        encoded = bytes(vol.tobytes())
        if getattr(self, "prepend_alignment_marker", False):
            from .audio_marker import MARKER_DURATION_S, synthesize_marker_4bit

            marker = synthesize_marker_4bit(sr)
            log.info(
                "video: prepending %d-byte alignment marker "
                "(%.0f ms chirp) — source content shifts to %.0fms",
                len(marker),
                MARKER_DURATION_S * 1000,
                MARKER_DURATION_S * 1000,
            )
            encoded = marker + encoded
        return encoded

    def _clock_s(self) -> float:
        if self.audio and self.audio.sample_rate:
            return self.audio.position_seconds()
        return time.time() - self._start_time

    def process_frame(self, current_time: float) -> bool:
        if self.source is None or self.source.finished:
            # Tell a sampler the source is exhausted so position_seconds()
            # clamps to the pushed total (no-op for the DAC streamer). Idempotent.
            if self.audio is not None:
                mark_eof = getattr(self.audio, "mark_eof", None)
                if callable(mark_eof):
                    mark_eof()
            return False

        clock_s = self._clock_s()
        img = self.source.current_frame(clock_s)
        if img is None:
            return True  # still pre-rolling
        # Source video is typically 24-30 fps; the playlist polls at the
        # system rate (50/60 Hz). AVFileSource.current_frame returns the
        # SAME ndarray object across calls between PTS boundaries — so
        # without this skip, we re-quantize + re-DMA identical pixels on
        # every other playlist tick. On mhires/hires REU the per-frame
        # bus-halt is ~10 ms (8K bitmap + 1K screen + 1K color via REC
        # DMA); doubling it to 60/s halts the bus ~60 % of the time and
        # AM-modulates the SID DAC at the playlist rate (audible 60 Hz
        # buzz, verified via Cam Link envelope FFT 2026-05-26). Identity
        # check is exact and cheap; overlays still tick because the
        # Playlist runs overlay.process_frame separately afterwards.
        if img is self._last_rendered_img:
            return True
        self._last_rendered_img = img
        self._record_av_lag(clock_s, current_time)
        img = _crop_to_aspect(img)
        # Rolling-window auto_fit: fold the clean (pre-annotation) frame into
        # the accumulator and refresh the derived fit until the warmup window
        # closes, then freeze. Feeding before annotation keeps the debug
        # digits out of the contrast/saturation stats.
        if (
            self._online_fit is not None
            and self._online_fit_frames < ONLINE_FIT_WARMUP_FRAMES
            and self.display_mode is not None
        ):
            self._online_fit.add(img)
            self._online_fit_frames += 1
            self.display_mode.set_color_fit(self._online_fit.result())
        if self.show_frame_numbers:
            fps = self.source.video_fps or 30.0
            label = f"{_timecode(clock_s)} f{int(round(clock_s * fps))}"
            img = _annotate_frame_number(img, label)
        assert self.display_mode is not None
        _render_with_overlays(self.display_mode, self.api, img, self.overlays, current_time, self)
        return True

    def _record_av_lag(self, clock_s: float, current_time: float) -> None:
        """Accumulate the A/V lag (audio clock − displayed-frame PTS) for the
        just-selected frame and emit a live debug line at most every
        AV_LAG_LOG_INTERVAL_S. The teardown summary reports the min/avg/max.

        Lag is artifact-free (software-side, no capture): small + lag ≤ one
        source-frame interval is healthy frame selection; a lag that climbs
        while the decode buffer sits near 0 is the decoder failing to keep
        real time (project_av_sync_decode_bound). Cheap — no allocation."""
        assert self.source is not None
        lag = clock_s - self.source.last_frame_pts
        depth = self.source.video_buffer_depth
        self._av_lag_min = min(self._av_lag_min, lag)
        self._av_lag_max = max(self._av_lag_max, lag)
        self._av_lag_sum += lag
        self._av_lag_count += 1
        self._av_buf_min = min(self._av_buf_min, depth)
        if log.isEnabledFor(logging.DEBUG) and (
            current_time - self._av_last_log_t >= AV_LAG_LOG_INTERVAL_S
        ):
            self._av_last_log_t = current_time
            log.debug(
                "video A/V lag: now=%+.0fms (min=%+.0f avg=%+.0f max=%+.0f) buf=%d over %d frames",
                lag * 1000,
                self._av_lag_min * 1000,
                (self._av_lag_sum / self._av_lag_count) * 1000,
                self._av_lag_max * 1000,
                depth,
                self._av_lag_count,
            )

    def teardown(self) -> None:
        super().teardown()
        if self._av_lag_count:
            log.info(
                "video A/V lag summary: min=%+.0f avg=%+.0f max=%+.0f ms, "
                "min buffer depth=%d over %d displayed frames",
                self._av_lag_min * 1000,
                (self._av_lag_sum / self._av_lag_count) * 1000,
                self._av_lag_max * 1000,
                int(self._av_buf_min),
                self._av_lag_count,
            )
        if self.source:
            self.source.close()
            self.source = None
        if self.audio:
            self.audio.stop()
        self._last_rendered_img = None


class LauncherScene(Scene):
    """Launch a native C64 program and hand the machine over to it.

    Resets the U64, then uploads + runs a `.prg` (firmware run_prg) or `.crt`
    cartridge (run_crt), chosen by file extension. Once launched the program
    owns the VIC, SID, and CIAs — c64cast stops painting; this scene only
    polls for player input and times out.

    Duration model: `duration_s` is an *idle timeout*. It counts down from
    launch and is reset whenever the player provides input, so an actively-
    played game stays up while an untouched demo runs for the full
    `duration_s` before the playlist advances. `min_duration_s` is a floor
    (the scene can't advance before it elapses, even if idle); the optional
    `max_duration_s` is a hard ceiling (advance regardless of input).

    Input detection deliberately excludes the modifier keys c64cast already
    scans (Commodore / SHIFT / CTRL at $028D) — those drive pause/skip/cycle
    and must not count as "player active". The detector polls one of:

      * "cia"    — CIA1 $DC00/$DC01 joystick bits (up/down/left/right/fire,
                   active-low). Works regardless of whether the program keeps
                   the kernal IRQ, but reads can race the program's own
                   keyboard-matrix scan (best-effort; see docs/caveats.md).
      * "kernal" — kernal scratch $00C5 (last key) + $00C6 (buffer length).
                   Clean, but only live while the kernal IRQ runs (BASIC
                   games / kernal-friendly demos); blind once a program
                   installs its own IRQ.
      * "auto"   — both signals OR'd together.
      * "none"   — no input polling; pure `duration_s` timer (for demos).

    Audio: the program drives the real SID directly, so this scene carries no
    AudioStreamer (built with audio=None) but still WANTS_AUDIO_LOCK so it
    coordinates the ensemble slot like the SID/MIDI scenes.
    """

    WANTS_AUDIO_LOCK = True

    def competes_for_audio_lock(self) -> bool:
        # The launched program outputs through the real SID regardless of
        # self.audio (which is always None here), so — like waveform/midi —
        # it normally contends for the ensemble slot. `bypass_audio_lock`
        # opts out: the scene then never claims or waits on the slot, so
        # several systems can run interactive launchers at once and each
        # player hears their own game.
        return self.WANTS_AUDIO_LOCK and not self.bypass_audio_lock

    # Bytes to read for each input source (contiguous so one read covers both).
    _CIA_BASE = CIA1.PORT_A  # $DC00, reads $DC00+$DC01
    _KERNAL_BASE = SCREEN.LAST_KEY  # $00C5, reads $00C5+$00C6

    def __init__(
        self,
        api: C64Backend,
        file: str,
        *,
        input_source: str = "cia",
        reset_before_launch: bool = True,
        min_duration_s: float = 0.0,
        max_duration_s: float = math.inf,
        bypass_audio_lock: bool = False,
        poll_interval_s: float = 0.1,
        launch_grace_s: float = 1.5,
        name: str | None = None,
    ):
        from .config import PROGRAM_EXTS, resolve_file_spec

        self.file_spec = file
        # Resolve once so __init__ raises on a bad spec (mirrors
        # validate_scene_cfg; also covers any scene built without validation).
        # Re-resolved at each setup() so a dropped file becomes eligible.
        candidates = resolve_file_spec(file, PROGRAM_EXTS, label="launcher")
        if name:
            scene_name = name
        elif len(candidates) == 1:
            scene_name = f"Launcher: {_display_name(candidates[0])}"
        else:
            scene_name = f"Launcher: {file}"
        super().__init__(api, None, None, scene_name)
        self.input_source = input_source
        self.reset_before_launch = reset_before_launch
        self.bypass_audio_lock = bool(bypass_audio_lock)
        self.min_duration_s = float(min_duration_s)
        self.poll_interval_s = float(poll_interval_s)
        self.launch_grace_s = float(launch_grace_s)
        # Hard ceiling; inf = no cap.
        self.max_duration_s = float(max_duration_s)
        # Nothing is rendered, so a low cap keeps host overhead negligible.
        self.target_fps = 4.0
        self.filepath: str = candidates[0]
        self.start_time = 0.0
        # Idle clock: last time input was observed. Guarded because the poll
        # thread writes it and process_frame reads it.
        self._last_input_t = 0.0
        self._input_lock = threading.Lock()
        self._baseline: bytes | None = None
        self._poll = PollThread(self._input_loop, name="launcher-input-poll", manual=True)
        # True when prepare_next() already picked this iteration's file.
        self._prepared = False

    def _resolve_candidates(self) -> list[str]:
        from .config import PROGRAM_EXTS, resolve_file_spec

        return resolve_file_spec(self.file_spec, PROGRAM_EXTS, label="launcher")

    def _pick_filepath(self) -> bool:
        """Re-resolve the spec (dirs rescan between iterations), pick a random
        candidate, refresh self.name. Returns False if nothing resolves."""
        try:
            candidates = self._resolve_candidates()
        except ValueError as e:
            log.error("launcher: file spec %r failed to resolve at setup: %s", self.file_spec, e)
            return False
        self.filepath = random.choice(candidates)
        self.name = f"Launcher: {_display_name(self.filepath)}"
        if len(candidates) > 1:
            log.info(
                "launcher: picked %s from %d candidates",
                os.path.basename(self.filepath),
                len(candidates),
            )
        return True

    def prepare_next(self) -> None:
        """Pick the upcoming program now so the preceding interstitial shows
        the real filename. setup() consumes this pick."""
        if self._pick_filepath():
            self._prepared = True

    def setup(self) -> None:
        if self._prepared:
            self._prepared = False
        elif not self._pick_filepath():
            super().setup()
            self.is_done = True
            return
        super().setup()
        if not os.path.exists(self.filepath):
            log.error(
                "launcher: file not found: %s — check the path in your "
                "config or the assets/programs/ contents",
                self.filepath,
            )
            self.is_done = True
            return
        now = time.time()
        self.start_time = now
        with self._input_lock:
            self._last_input_t = now
        self._baseline = None
        # A fresh machine state avoids inheriting whatever the prior scene
        # left in RAM/VIC; .crt especially expects a reset to take effect.
        if self.reset_before_launch:
            self.api.reset()
        try:
            self.api.launch_program(self.filepath)
        except Exception as e:
            log.error("launcher: failed to launch %s (%s)", self.filepath, e)
            self.is_done = True
            return
        if self.input_source != "none":
            self._poll.start()

    def process_frame(self, current_time: float) -> bool:
        # Hard ceiling wins regardless of input.
        if (current_time - self.start_time) >= self.max_duration_s:
            return False
        # Floor: never advance before min_duration_s, even when idle.
        if (current_time - self.start_time) < self.min_duration_s:
            return True
        with self._input_lock:
            last_input = self._last_input_t
        # Idle timeout: advance (return False) when no input for duration_s.
        return (current_time - last_input) < self.duration_s

    def teardown(self) -> None:
        self._poll.stop()
        super().teardown()
        # Clear the program (mandatory for .crt, which run_crt leaves active)
        # so the next scene paints onto a clean machine.
        self.api.reset()

    # -- input polling --------------------------------------------------

    def _read_snapshot(self) -> bytes | None:
        """Read the configured input registers. Returns None on a failed
        read (caller ignores it — don't reset the idle clock on a glitch).
        Never reads $028D, so the app's modifier keys are excluded."""
        parts: list[bytes] = []
        if self.input_source in ("cia", "auto"):
            cia = self.api.read_memory(self._CIA_BASE, 2)
            if cia is None:
                return None
            # Mask to the joystick bits on both ports; the upper bits carry
            # keyboard-scan / serial state that churns independently of input.
            parts.append(bytes(b & CIA1.JOY_MASK for b in cia))
        if self.input_source in ("kernal", "auto"):
            kern = self.api.read_memory(self._KERNAL_BASE, 2)
            if kern is None:
                return None
            parts.append(kern)
        return b"".join(parts)

    def _input_loop(self, stop: threading.Event) -> None:
        """Manual poll loop. After a grace window (so the program's INIT
        churn doesn't seed a bogus baseline), snapshot a baseline, then reset
        the idle clock whenever a later read deviates from it."""
        if stop.wait(self.launch_grace_s):
            return
        self._baseline = self._read_snapshot()
        while not stop.wait(self.poll_interval_s):
            snap = self._read_snapshot()
            if snap is None:
                continue
            if self._baseline is None:
                self._baseline = snap
                continue
            if snap != self._baseline:
                with self._input_lock:
                    self._last_input_t = time.time()
