"""Scene state machine: a DisplayMode, an optional audio path, and a source.

See docs/architecture/scenes.md#scenespy--scene-state-machine.
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

import cv2
import numpy as np

from c64cast._pollthread import PollThread
from c64cast._teardown import run_teardown_steps
from c64cast.app.profiler import get_profiler
from c64cast.audio.audio import AudioStreamer
from c64cast.audio.audio_handlers import (
    INT16_FULL_SCALE,
    REU_PUMP_CHUNK_SIZE_HEAVY_BUS,
    encode_floats_to_dac,
)
from c64cast.audio.sampler import UltimateAudioSampler
from c64cast.control.transport import make_loop_preset_store, timecode
from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import CIA1, SCREEN
from c64cast.video.modes import BitmapDisplayMode, DisplayMode
from c64cast.video.palette import ColorFitAccumulator, ColorMapAccumulator
from c64cast.video.rolling_palette import RollingForcePalette
from c64cast.video.video import (
    AVFileSource,
    WebcamSource,
    _compute_normalization_gain,
    decode_audio_full,
    ensure_pyav,
    prescan_source_color,
    probe_container_title,
)

from .bitmap_text import glyphs_to_mask, load_glyphs
from .setup_progress import SegmentedProgress, make_setup_bar
from .video_transport import VideoTransportControls

if TYPE_CHECKING:
    from c64cast.app.config import AudioCfg, ColorCfg
    from c64cast.app.quickcast import ResolvedMedia
    from c64cast.app.scene_factory import DisplayWiring
    from c64cast.audio.audio_source import AudioSource

    from .effects import FrameEffect
    from .frame_source import FrameSource
    from .modulation import MusicModulation
    from .overlays import Overlay

log = logging.getLogger(__name__)

SceneAudio = AudioStreamer | UltimateAudioSampler

_C64_ASPECT = 320 / 200

# Opening frames folded into the online ColorFitAccumulator before the derived
# fit freezes — ~2 s at 24 fps.
ONLINE_FIT_WARMUP_FRAMES = 48

AV_LAG_LOG_INTERVAL_S = 2.0

# Defined here, not in scene_factory (which imports this module); scene_factory
# re-exports them to the app layer.
VIDEO_EXTS = (".mp4", ".avi", ".mkv", ".mov", ".webm", ".m4v")
SID_EXTS = (".sid",)
PICTURE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
PROGRAM_EXTS = (".prg", ".crt")
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus")


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
    palette cell (black by default → C64 index 0)."""
    h, w = img.shape[:2]
    ar = w / h if h else target_ratio
    if ar > target_ratio:
        new_h = round(w / target_ratio)
        pad = max(0, new_h - h)
        top = pad // 2
        return cv2.copyMakeBorder(img, top, pad - top, 0, 0, cv2.BORDER_CONSTANT, value=pad_color)
    if ar < target_ratio:
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


def _blit_c64_text(
    img: np.ndarray,
    text: str,
    *,
    width_frac: float,
    vpos: str,
    margin_y_frac: float,
) -> np.ndarray:
    """Composite `text`, rendered from the real C64 character ROM, into a COPY
    of the BGR frame `img` (the caller's `img` may be a view onto a shared source
    buffer, so we never mutate it in place).

    The glyphs come from :func:`bitmap_text.load_glyphs` (the uppercase charset
    :mod:`c64cast.hw.char_rom` resolves, with a builtin fallback), so the
    pre-quantization overlays share the same font the on-C64 renderers use. The
    8×8 cells are nearest-neighbor upscaled by an integer factor chosen so the
    block spans ~`width_frac` of the frame width regardless of source resolution
    — that keeps the text legible through the downscale to 160/320-wide C64
    output and preserves the blocky, authentically-C64 pixel edges (no
    anti-aliasing). White glyph pixels over a black halo read on any background.
    `vpos` is "top" or "bottom"; the block is left-aligned at a 2% margin and
    inset `margin_y_frac` of the height from the chosen edge."""
    out = img.copy()
    if not text:
        return out
    h, w = out.shape[:2]
    mask = glyphs_to_mask(load_glyphs(), text)
    gw = mask.shape[1]
    scale = max(1, int(round((width_frac * w) / max(gw, 1))))
    up = np.repeat(np.repeat(mask, scale, axis=0), scale, axis=1)
    bh, bw = up.shape
    x0 = max(0, int(0.02 * w))
    y0 = h - bh - int(margin_y_frac * h) if vpos == "bottom" else int(margin_y_frac * h)
    y0 = max(0, y0)
    region = out[y0 : y0 + bh, x0 : x0 + bw]
    rh, rw = region.shape[:2]
    if rh == 0 or rw == 0:
        return out
    glyph = up[:rh, :rw]
    k = scale
    kernel = np.ones((2 * k + 1, 2 * k + 1), dtype=np.uint8)
    halo = cv2.dilate(glyph, kernel).astype(bool)
    region[halo] = 0
    region[glyph.astype(bool)] = 255
    return out


def _annotate_frame_number(img: np.ndarray, label: str) -> np.ndarray:
    """Draw `label` (timecode + frame #) into the top-left corner of a BGR
    frame using the C64 character ROM font, returning an annotated COPY.

    Drawn before quantization, so it works on any display mode (the digits
    become part of the quantized bitmap). Spans ~55% of the frame width so it
    survives the downscale to 160/320-wide C64 output legibly. Diagnostic aid
    only (see [debug].frame_numbers). See :func:`_blit_c64_text`."""
    return _blit_c64_text(img, label, width_frac=0.55, vpos="top", margin_y_frac=0.10)


class OsdState:
    """A brief on-screen message for live performance (a knob sweep's new value,
    a mode change) — the visible feedback for the MIDI/WLED live-tune controls.

    Thread-safe: control threads (the MIDI reader, the WLED server) call
    :meth:`post`; the render thread calls :meth:`current` once per frame. A post
    supersedes any earlier one and shows for `duration_s`, then clears itself.

    Two independent gates: `enabled` is the static setting
    (``[midi_control].osd = "off"``), stamped on by
    ``scene_factory.build_scene``; `suppressed` is the run-level override that
    performance mode (``Playlist.performance_mode``) sets. Ask :attr:`visible`,
    not either gate, whenever the question is "is the OSD up?" — see
    docs/architecture/control.md#the-osd. `position` is "top" or "bottom".
    Rendered pre-quantization via :func:`_annotate_osd`, so it works on every
    display mode, exactly like the ``--frame-numbers`` debug label."""

    __slots__ = ("_lock", "_text", "_expires_at", "position", "enabled", "suppressed")

    def __init__(
        self, position: str = "bottom", enabled: bool = True, *, suppressed: bool = False
    ) -> None:
        self._lock = threading.Lock()
        self._text = ""
        self._expires_at = 0.0
        self.position = position
        self.enabled = enabled
        self.suppressed = suppressed

    @property
    def visible(self) -> bool:
        """Whether a post would be shown at all — both gates open."""
        return self.enabled and not self.suppressed

    def post(self, text: str, duration_s: float = 2.5) -> None:
        """Show `text` for `duration_s` seconds (supersedes any current message).
        A no-op when the OSD is disabled or suppressed, so callers needn't
        check first — which is what lets performance mode silence every poster
        (live-tune, effect bypass, the transport engine) from one flag."""
        if not self.visible:
            return
        with self._lock:
            self._text = text
            self._expires_at = time.monotonic() + duration_s

    def current(self) -> str | None:
        """The message to draw this frame, or None when disabled / suppressed /
        expired / never posted. Cheap enough to call every frame."""
        if not self.visible:
            return None
        with self._lock:
            if self._text and time.monotonic() < self._expires_at:
                return self._text
            return None


def _annotate_osd(img: np.ndarray, text: str, position: str = "bottom") -> np.ndarray:
    """Draw the OSD `text` into the top or bottom of a BGR frame using the C64
    character ROM font, returning an annotated COPY (the caller's `img` may be a
    view onto a shared source buffer; see _annotate_frame_number). Spans ~85% of
    the frame width regardless of source resolution — legible through the
    downscale to 160/320-wide C64 output, and shrinks a long ``param.name value``
    line to fit rather than clipping it. Pre-quantization, so it works on any
    display mode. See :func:`_blit_c64_text`."""
    vpos = "top" if position == "top" else "bottom"
    return _blit_c64_text(img, text, width_frac=0.85, vpos=vpos, margin_y_frac=0.06)


class Scene:
    # Consulted by the Playlist's ensemble audio lock before setup; ignored
    # entirely in single-system mode.
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
        self.osd = OsdState()
        self.is_done = False
        self.duration_s: float = 30.0
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self.prev_frame: np.ndarray | None = None
        # Later overlays paint on top of earlier ones.
        self.overlays: list[Overlay] = []
        # None = the Playlist's system default (60 NTSC / 50 PAL).
        self.target_fps: float | None = None
        self.show_frame_numbers: bool = False
        # None = the global/source-aware default.
        self.pre_emphasis: float | None = None
        self.effects: list[FrameEffect] = []
        # ClockModulationSource over the playlist's beat grid, injected by
        # Playlist.safe_setup; None until a playlist owns the scene.
        self.clock_modulation: Any = None
        # The SceneCfg this scene was built from. Any, because an Orchestrator
        # import would cycle; the consumers know the real types.
        self._cfg: Any = None
        # Which `[[scenes]]` block the scene came from, or None when no block
        # named it. An index, not the SceneCfg above, because the live-tune
        # save-back re-reads the config file and needs the block's address.
        self.cfg_index: int | None = None
        self._orchestrator: Any = None
        self._is_conductor: bool = False
        self._system_index: int = 0

    def bind_orchestrator(self, orch: Any, *, conductor: bool, index: int) -> None:
        """Stamp the cross-ensemble orchestrator role onto this scene, before
        setup() runs, so participating overlays (big_text) can find it. The
        playlist's ensemble coordinator calls this for conductor and follower
        scenes alike; `index` is this system's left-to-right position in the
        ensemble (span-mode orchestrators slice global content with it)."""
        self._orchestrator = orch
        self._is_conductor = conductor
        self._system_index = index

    def clear_orchestrator(self) -> None:
        """Drop the orchestrator stamp at teardown. The same Scene instance is
        reused across loop iterations, and a stale stamp would make the next
        conductor install short-circuit."""
        self._orchestrator = None
        self._is_conductor = False

    @property
    def effect(self) -> FrameEffect | None:
        """Back-compat single-effect accessor over the `effects` chain: reads the
        first layer (or None). Setting it to a FrameEffect makes it the sole
        layer; setting None clears the chain."""
        return self.effects[0] if self.effects else None

    @effect.setter
    def effect(self, value: FrameEffect | None) -> None:
        self.effects = [value] if value is not None else []

    def competes_for_audio_lock(self) -> bool:
        """Whether THIS instance contends for the ensemble audio slot.

        `WANTS_AUDIO_LOCK` declares the capability at the class level;
        instances opt out when their audio is actually disabled (e.g. a muted
        video). SID-driving scenes (waveform/midi) output through the chip
        regardless of the AudioStreamer, so they don't override this."""
        return self.WANTS_AUDIO_LOCK

    def features(self) -> MusicModulation | None:
        """A live music-feature snapshot for this scene, or None when the scene
        has no music source. Consumed by process-wide reactive sinks that run
        outside the render loop (e.g. the WLED audio-sync broadcaster). Default:
        no music. SID-driven scenes override to expose their emulator state."""
        return None

    @property
    def wled_label(self) -> str:
        """Stable, human label for the WLED effect list + preset-name defaults.
        Defaults to the scene name. Scenes whose `name` tracks a *randomized*
        asset (e.g. a waveform scene picking a random SID from a pool, whose
        `name` becomes the currently-loaded tune) override this to a stable
        pool-level label — so the WLED effect dropdown doesn't churn as the asset
        rotates, and a saved preset doesn't falsely promise the one asset that
        happened to be loaded when it was saved (recall re-picks a different
        one)."""
        return self.name

    def prepare_next(self) -> None:
        """Called by the Playlist right before the interstitial that
        precedes this scene is built. Randomized scenes override this to
        pick their file now so the "UP NEXT" card shows the real upcoming
        content (and so the pick isn't deferred to setup(), which runs
        after the card is already on screen). Default: no-op."""

    def setup(self) -> None:
        self.is_done = False
        self.prev_frame = None
        for eff in self.effects:
            eff.reset()
        # Before the subclass brings audio up: mic start and the video
        # pre-encode both read the updated DSP params.
        if self.audio is not None:
            self.audio.set_pre_emphasis(self.pre_emphasis)
        if self.display_mode is not None:
            self.display_mode.setup(self.api)
        mode_name = type(self.display_mode).__name__ if self.display_mode is not None else "none"
        fps_str = f"{self.target_fps:.0f}fps" if self.target_fps else "auto-fps"
        overlay_names = [getattr(ov, "name", type(ov).__name__) for ov in self.overlays]
        ov_str = ", ".join(overlay_names) if overlay_names else "no overlays"
        dur_str = "unbounded" if math.isinf(self.duration_s) else f"{self.duration_s:.1f}s"
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

    def _apply_osd(self, img: np.ndarray) -> np.ndarray:
        """Annotate `img` with the current OSD message, if any (an annotated
        copy), else return it unchanged. The frame-bearing scenes call this right
        before quantization, after the frame-number debug label. VideoScene
        handles OSD inline instead (it must also bust its identity-skip on an OSD
        change), so it doesn't use this helper."""
        text = self.osd.current()
        return _annotate_osd(img, text, self.osd.position) if text else img

    def process_frame(self, current_time: float) -> bool:
        raise NotImplementedError

    def teardown(self) -> None:
        # First, so a subclass's audio.stop() latency does not land on top of a
        # raster IRQ the mode still has hooked at $0314 (HiresDisplayMode with
        # use_reu_staged; a no-op for every other mode).
        if self.display_mode is not None:
            try:
                self.display_mode.teardown(self.api)
            except Exception:
                log.exception("display_mode.teardown failed; continuing")


def _maybe_start_rolling_palette(
    scene: Scene, color: ColorCfg | None, display_mode: DisplayMode | None
) -> RollingForcePalette | None:
    """Start a live rolling force_palette for a scene that can't pre-scan (webcam,
    wled sink, generative), when `[color].force_palette` is on AND the mode
    actually applies it (`_force_palette`, i.e. mcm/mhires). Returns the started
    driver, or None when it doesn't apply (a no-op on char/blank/hires modes)."""
    if color is None or not getattr(display_mode, "_force_palette", False):
        return None
    from c64cast.app.config import resolved_force_palette

    n_colors, indices = resolved_force_palette(color)
    fp = RollingForcePalette(n_colors=n_colors, indices=indices)
    fp.start()
    log.info("%s: live rolling force_palette (adapts to the source over ~30s)", scene.name)
    return fp


def _apply_rolling_palette(
    fp: RollingForcePalette | None, display_mode: DisplayMode | None, frame: np.ndarray
) -> None:
    """Feed the clean frame to the rolling driver and install any newly baked
    map. Called each frame before quantization; cheap (the k-means runs on the
    driver's worker thread, not here)."""
    if fp is None or display_mode is None:
        return
    fp.submit_frame(frame)
    cmap = fp.poll_colormap()
    if cmap is not None:
        display_mode.set_color_map(cmap)
        log.debug("rolling force_palette → %d colors %s", len(cmap.indices), list(cmap.indices))


class WebcamScene(Scene):
    """Live webcam scene optimized for low latency.

    Reads each camera frame and pushes it straight through to the display
    mode — no delay buffer. Audio is attached whenever the global
    [audio].enabled is on; a per-scene `audio = false` opts back out (see
    config.SceneCfg.audio). When attached, mic capture runs independently
    of the video (no sync).

    See docs/architecture/scenes.md#webcamscene--tuned-for-latency.
    """

    def __init__(
        self,
        api: C64Backend,
        audio: AudioStreamer | None,
        display_mode: DisplayMode,
        source: WebcamSource,
        audio_cfg: AudioCfg,
        name: str,
        color: ColorCfg | None = None,
    ):
        super().__init__(api, audio, display_mode, name)
        self.source = source
        self.audio_cfg = audio_cfg
        self.start_time = 0.0
        self._frame_count = 0
        self._color = color
        self._rolling_fp: RollingForcePalette | None = None

    def setup(self) -> None:
        super().setup()
        self.start_time = time.time()
        self._rolling_fp = _maybe_start_rolling_palette(self, self._color, self.display_mode)
        # The mic path is always the 4-bit DAC streamer; the sampler is a
        # video-only backend.
        if isinstance(self.audio, AudioStreamer):
            # A mode that installs the bank-swap merged dispatcher at $0314
            # owns that vector, so the mic REU pump must skip its own hook.
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
            # Before annotation, so the rolling force_palette stats the shown
            # picture and not the debug digits.
            _apply_rolling_palette(self._rolling_fp, self.display_mode, img)
            if self.show_frame_numbers:
                self._frame_count += 1
                label = f"{timecode(current_time - self.start_time)} f{self._frame_count}"
                img = _annotate_frame_number(img, label)
            img = self._apply_osd(img)
            assert self.display_mode is not None
            _render_with_overlays(
                self.display_mode, self.api, img, self.overlays, current_time, self
            )
        return True

    def teardown(self) -> None:
        fp, self._rolling_fp = self._rolling_fp, None
        steps: list[tuple[str, Callable[[], object]]] = [("base teardown", super().teardown)]
        if fp is not None:
            steps.append(("rolling palette stop", fp.stop))
        if self.audio:
            steps.append(("audio stop", self.audio.stop))
        run_teardown_steps(log, type(self).__name__, steps)


def _effect_modulation(
    scene: Scene, eff: FrameEffect, audio_mod: MusicModulation | None
) -> MusicModulation | None:
    """The MusicModulation snapshot a layer should react to, per its
    `mod_source`: "audio" → the scene's feed, "clock" → the beat grid (via
    `scene.clock_modulation`, None-safe), "off" (or anything else) → None (never
    react — the byte-stable baseline)."""
    src = getattr(eff, "mod_source", "audio")
    if src == "audio":
        return audio_mod
    if src == "clock":
        clock = scene.clock_modulation
        return clock.features() if clock is not None else None
    return None


def _apply_effect_chain(
    scene: Scene, frame: np.ndarray, t: float, audio_mod: MusicModulation | None
) -> np.ndarray:
    """Run the scene's effect chain over `frame`, layer by layer, in order.
    Disabled layers (bypass) are skipped as exact identity; each enabled layer
    gets the snapshot its `mod_source` selects. A layer that raises is dropped
    from the chain (disabled) and the pre-layer frame carries on — one bad
    effect never kills the scene. Iterates a copy of the list so a failing
    layer can be removed without disturbing the loop."""
    for eff in list(scene.effects):
        if not getattr(eff, "enabled", True):
            continue
        try:
            frame = eff.apply(frame, t, _effect_modulation(scene, eff, audio_mod))
        except Exception:
            log.exception(
                "effect %r failed on %r — disabling",
                getattr(eff, "name", eff),
                scene.name,
            )
            if eff in scene.effects:
                scene.effects.remove(eff)
    return frame


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

    The per-scene pixel effect *chain* (scene.effects), if any, transforms the
    source frame here — before downscale/quantization — each layer in order, so
    it applies uniformly to every frame-bearing scene. A disabled layer
    (`enabled = False`, a live bypass toggle) is skipped (exact identity). Each
    layer's `mod_source` selects which `MusicModulation` snapshot drives it:
    "audio" gets the scene's feed (`modulation`, None on non-reactive scenes),
    "clock" gets the beat grid (`scene.clock_modulation`), "off" gets None.
    Skipped entirely when there's no frame (Blank). A failing layer disables
    itself (dropped from the chain) rather than killing the scene."""
    prof = get_profiler()
    if frame is not None and scene.effects:
        frame = _apply_effect_chain(scene, frame, t, modulation)
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
            if getattr(ov, "disabled", False):
                continue
            try:
                ov.compose(buffers, scene, t)
            except Exception:
                log.exception(
                    "overlay %r compose failed on %r — disabling",
                    getattr(ov, "name", ov),
                    scene.name,
                )
                ov.disabled = True
    # Cached at full brightness so a freeze+dim fade-out can re-push without
    # re-composing; apply_fade never mutates the cached buffers.
    display_mode.last_buffers = buffers
    if display_mode.fade_alpha < 1.0 or display_mode.user_dim < 1.0:
        buffers = display_mode.apply_fade(buffers)
    with prof.stage("push"):
        display_mode.push(api, buffers)


class SourceScene(Scene):
    """Composable scene: a FrameSource × an AudioSource × a display mode.

    Read a frame from the source at the scene clock, run the scene's effect
    chain (inside _render_with_overlays), quantize via the display mode, push —
    overlays compose on top. The source decides the scene's lifetime: infinite
    sources (generative art) run until `duration_s`; a finite source ends the
    scene when it reports `finished`.

    Audio is delegated to the AudioSource building block, chosen independently
    of the video source. The base `audio` reference is still passed so the
    shared streamer's per-scene pre-emphasis hook works; the AudioSource owns
    start/stop.
    """

    def __init__(
        self,
        api: C64Backend,
        audio: SceneAudio | None,
        display_mode: DisplayMode,
        source: FrameSource,
        audio_source: AudioSource,
        name: str,
        color: ColorCfg | None = None,
    ):
        super().__init__(api, audio, display_mode, name)
        self.source = source
        self.audio_source = audio_source
        self.start_time = 0.0
        self._frame_count = 0
        self._color = color
        self._rolling_fp: RollingForcePalette | None = None

    def competes_for_audio_lock(self) -> bool:
        return self.audio_source.wants_audio_lock

    def features(self) -> MusicModulation | None:
        return self.audio_source.features()

    def setup(self) -> None:
        super().setup()
        self.start_time = time.time()
        self.source.setup()
        # The playlist does not wrap setup() in try/except, so a raise from a
        # source that rejects its content would crash the run loop. Self-abort
        # instead and let the playlist advance.
        try:
            self.audio_source.setup()
        except Exception:
            log.exception(
                "scene %r: audio source %s failed to start — aborting scene",
                self.name,
                type(self.audio_source).__name__,
            )
            self.is_done = True
        # A SID audio source kicks its player through run_prg, which re-inits
        # the machine to text mode and clobbers the VIC registers super().setup()
        # just wrote; re-assert the display after it, against invalidated cache.
        if (
            not self.is_done
            and self.display_mode is not None
            and getattr(self.audio_source, "resets_display", False)
        ):
            self.api.invalidate_cache()
            self.display_mode.setup(self.api)
        # After any display re-assert above, so it installs onto the settled mode.
        if not self.is_done:
            self._rolling_fp = _maybe_start_rolling_palette(self, self._color, self.display_mode)

    def process_frame(self, current_time: float) -> bool:
        # A generative FrameSource is never `finished`, so an is_done set by a
        # failed setup() has to end the scene here or it plays silent for the
        # full duration.
        if self.is_done:
            return False
        if self.source.finished:
            return False
        if (current_time - self.start_time) >= self.duration_s:
            return False
        modulation = self.audio_source.features()
        frame = self.source.read(current_time - self.start_time, modulation)
        if frame is not None:
            _apply_rolling_palette(self._rolling_fp, self.display_mode, frame)
            if self.show_frame_numbers:
                self._frame_count += 1
                label = f"{timecode(current_time - self.start_time)} f{self._frame_count}"
                frame = _annotate_frame_number(frame, label)
            frame = self._apply_osd(frame)
            assert self.display_mode is not None
            _render_with_overlays(
                self.display_mode, self.api, frame, self.overlays, current_time, self, modulation
            )
        return True

    def teardown(self) -> None:
        fp, self._rolling_fp = self._rolling_fp, None
        steps: list[tuple[str, Callable[[], object]]] = [("base teardown", super().teardown)]
        if fp is not None:
            steps.append(("rolling palette stop", fp.stop))
        steps += [
            ("audio source teardown", self.audio_source.teardown),
            ("source teardown", self.source.teardown),
        ]
        run_teardown_steps(log, type(self).__name__, steps)


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
        if isinstance(self.audio, AudioStreamer):
            self.audio.start_mic(
                self.audio_cfg.device, self.audio_cfg.mic_sensitivity, self.audio_cfg.noise_gate
            )

    def process_frame(self, current_time: float) -> bool:
        # Paints past duration_s too: the Playlist's overlay busy-defer flips
        # is_done back to False, and a scene that stopped rendering there would
        # freeze the screen mid-message.
        assert self.display_mode is not None
        _render_with_overlays(self.display_mode, self.api, None, self.overlays, current_time, self)
        return (current_time - self.start_time) < self.duration_s

    def teardown(self) -> None:
        super().teardown()
        if self.audio:
            self.audio.stop()


class MediaFileMixin:
    """Shared `file =` spec plumbing for the media-file scenes (slideshow /
    video / launcher). Owns candidate resolution, the random per-setup pick,
    and the interstitial pre-pick; a concrete scene sets
    ``MEDIA_EXTS``/``MEDIA_LABEL`` and provides the annotated instance
    attrs."""

    MEDIA_EXTS: ClassVar[tuple[str, ...]] = ()
    MEDIA_LABEL: ClassVar[str] = ""

    file_spec: str
    filepath: str
    name: str
    _prepared: bool

    def _resolve_candidates(self) -> list[str]:
        from c64cast.app.scene_factory import resolve_file_spec

        return resolve_file_spec(self.file_spec, self.MEDIA_EXTS, label=self.MEDIA_LABEL)

    def _display_name_for(self, filepath: str) -> str:
        """The name to show for a picked file: its basename by default.
        Overridden by scene types that can read a real name out of the file
        itself (VideoScene: a container `title` tag) rather than settling
        for the filename."""
        return _display_name(filepath)

    def _initial_scene_name(self, candidates: list[str]) -> str:
        """The build-time scene name: the file's basename for a single-entry
        pool, the raw spec for a multi-entry one (the picked file's basename
        gets prefixed at each setup)."""
        if len(candidates) == 1:
            return f"{self.MEDIA_LABEL.title()}: {self._display_name_for(candidates[0])}"
        return f"{self.MEDIA_LABEL.title()}: {self.file_spec}"

    def _pick_filepath(self) -> bool:
        """Re-resolve the spec (directories rescan between iterations so
        newly dropped files become eligible), pick a random candidate, and
        refresh self.name to the picked file (extension stripped) so the
        interstitial card + heartbeat log show it. Returns False if the spec
        no longer resolves to anything."""
        try:
            candidates = self._resolve_candidates()
        except ValueError as e:
            log.error(
                "%s: file spec %r failed to resolve at setup: %s",
                self.MEDIA_LABEL,
                self.file_spec,
                e,
            )
            return False
        self.filepath = random.choice(candidates)
        self.name = f"{self.MEDIA_LABEL.title()}: {self._display_name_for(self.filepath)}"
        if len(candidates) > 1:
            log.info(
                "%s: picked %s from %d candidates",
                self.MEDIA_LABEL,
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


class SlideshowScene(MediaFileMixin, Scene):
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

    MEDIA_EXTS = PICTURE_EXTS
    MEDIA_LABEL = "slideshow"

    def __init__(
        self,
        api: C64Backend,
        display_mode: DisplayMode,
        file: str,
        *,
        image_duration_s: float = 5.0,
        display_spec: str | None = "mhires",
        wiring: DisplayWiring | None = None,
        color: ColorCfg | None = None,
        aspect_mode: str = "crop",
    ):
        from c64cast.app.config import ColorCfg
        from c64cast.app.scene_factory import DisplayWiring

        self.file_spec = file
        self.image_duration_s = float(image_duration_s)
        self._aspect_mode = aspect_mode
        # Drives the display-mode shaping at construction and the per-slide
        # color fit / forced-palette remap recomputed in _advance_image.
        self._color = color if color is not None else ColorCfg()
        # May be "random" — re-resolved at every setup() so single-scene loops
        # get a fresh display mode per iteration.
        self.display_spec = display_spec
        # Stashed whole: it carries the raw [video] tri-states plus the probe
        # verdicts, so a `display = "random"` re-pick can re-decide REU staging
        # / double-buffer / flicker against its own concrete mode.
        self._display_wiring = wiring if wiring is not None else DisplayWiring(color=self._color)
        candidates = self._resolve_candidates()
        super().__init__(api, None, display_mode, self._initial_scene_name(candidates))
        self._shuffle_bag: list[str] = []
        self._current_path: str | None = None
        self._current_img: np.ndarray | None = None
        self._image_start: float = 0.0
        self.start_time: float = 0.0
        self._prepared = False

    def _maybe_rebuild_display_mode(self) -> None:
        """When display_spec is "random", pick a fresh concrete mode and
        rebuild the DisplayMode. Tears down the previous one cleanly. No-op
        otherwise.

        The wiring goes back through the factory's own
        `build_wired_display_mode`, so this cannot drift from the mode the
        factory built at load time. Only `has_buffer_overlays` is recomputed,
        because overlays are attached after construction."""
        if self.display_spec != "random":
            return
        from c64cast.app.scene_factory import (
            _resolve_slideshow_display,
            build_wired_display_mode,
        )

        new_name = _resolve_slideshow_display(self.display_spec)
        old = self.display_mode
        if old is not None:
            try:
                old.teardown(self.api)
            except Exception:
                log.exception("slideshow: prior display_mode teardown failed; continuing")
        self.display_mode = build_wired_display_mode(
            new_name,
            replace(
                self._display_wiring,
                has_buffer_overlays=any(
                    getattr(ov, "PAINTS_INTO_BUFFERS", False) for ov in self.overlays
                ),
            ),
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
                self._shuffle_bag = [p for p in self._shuffle_bag if p != path]
                if not self._shuffle_bag:
                    # Whole pool consumed and nothing decoded — bail rather
                    # than loop forever.
                    self.is_done = True
                    return
                continue
            self._current_path = path
            self._current_img = _apply_aspect(img, self._aspect_mode)
            if self.display_mode is not None:
                c = self._color
                if c.auto_fit:
                    # Full strength: the display mode lerps it by
                    # [color].auto_fit_strength at apply time.
                    fit_acc = ColorFitAccumulator(strength=1.0)
                    fit_acc.add(self._current_img)
                    self.display_mode.set_color_fit(fit_acc.result())
                if c.force_palette:
                    from c64cast.app.config import resolved_force_palette

                    n_colors, indices = resolved_force_palette(c)
                    map_acc = ColorMapAccumulator(n_colors=n_colors, indices=indices)
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
        if self._prepared:
            self._prepared = False
        elif not self._pick_first_image():
            super().setup()
            self.is_done = True
            return
        super().setup()
        # prepare_next may have loaded the slide seconds ago, during the
        # interstitial, so its _image_start would short-change the first slide.
        self.start_time = time.time()
        self._image_start = time.time()
        # super().setup() just cleared is_done, so a failed decode above has to
        # re-assert it.
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
                f"{timecode(current_time - self.start_time)} "
                f"{os.path.basename(self._current_path or '')}"
            )
            img = _annotate_frame_number(img, label)
        img = self._apply_osd(img)
        assert self.display_mode is not None
        _render_with_overlays(self.display_mode, self.api, img, self.overlays, current_time, self)
        return True


class VideoScene(MediaFileMixin, Scene):
    """PyAV-driven A/V playback with audio-master sync.

    The demuxer runs on its own thread, pushing resampled audio straight into
    the AudioStreamer queue. process_frame() asks the AudioStreamer for the
    current playback position and picks the latest video frame whose PTS ≤
    that position. Frames behind get dropped; frames ahead wait. Drift can't
    accumulate because the audio clock IS the reference.
    """

    WANTS_AUDIO_LOCK = True
    MEDIA_EXTS = VIDEO_EXTS
    MEDIA_LABEL = "video"

    def competes_for_audio_lock(self) -> bool:
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
        tempo_scale: float = 1.0,
        loop_audio: str = "on",
        setup_progress: bool = True,
    ):
        """`file` is a comma-separated `resolve_file_spec` spec (or a single
        literal path — the spec grammar treats one path as a one-entry
        pool). The candidate pool is resolved here once; each `setup()`
        re-resolves so a directory's contents can change between scene
        repeats. Single-entry pools stay deterministic."""
        self.file_spec = file
        # Resolve once so a bad spec raises at construction; picked again at
        # each setup().
        candidates = self._resolve_candidates()
        super().__init__(api, audio, display_mode, self._initial_scene_name(candidates))
        # True when prepare_next() has already chosen this iteration's file, so
        # setup() consumes that pick instead of re-rolling.
        self._prepared = False
        # The deterministic single-entry case, so a caller introspecting before
        # setup() sees a real path; multi-entry pools overwrite it in setup().
        self.filepath = candidates[0]
        self.source: AVFileSource | None = None
        self.wall_start_time = 0.0
        # The resolved URL's yt-dlp attribution (None for a local file). Set
        # post-construction by scene_factory._build_video; read by
        # recording_metadata._video_source, nowhere in playback itself.
        self.source_info: ResolvedMedia | None = None
        # Where playback begins. AVFileSource seeks + rebases PTS to it; quick
        # playback derives it from a URL's t=/start= timestamp.
        self.start_s = max(0.0, start_s)
        # Bitmap + $D418-DAC tempo compensation (1.0 = off): AVFileSource
        # time-compresses the audio by 1/tempo_scale and scales video PTS by
        # tempo_scale, canceling the bitmap+DAC slowdown. Resolved in
        # config.build_scene.
        self.tempo_scale = tempo_scale
        self._last_rendered_img: np.ndarray | None = None
        # The OSD text baked into the last rendered frame; compared each tick so
        # a post or expiry busts the identity-skip for one render.
        self._last_osd_shown: str | None = None
        # A/V-lag telemetry: per-displayed-frame audio_clock − frame PTS.
        self._av_lag_min = math.inf
        self._av_lag_max = -math.inf
        self._av_lag_sum = 0.0
        self._av_lag_count = 0
        self._av_buf_min = math.inf
        self._av_last_log_t = 0.0
        # None = no online fit (pre-scanned or disabled).
        self._online_fit: ColorFitAccumulator | None = None
        self._online_fit_frames = 0
        # Capture-anchor marker (audio_marker.py). Only honored on the REU
        # pre-encode path.
        self.prepend_alignment_marker = prepend_alignment_marker
        self._setup_progress = setup_progress
        # Drives the display-mode shaping and the per-video color fit /
        # forced-palette remap installed at setup() from a one-shot pre-scan.
        from c64cast.app.config import ColorCfg

        self._color = color if color is not None else ColorCfg()
        # Lifetime is video-driven; math.inf disables the base-class duration
        # timer, and the config layer rejects a user-supplied `duration_s`.
        self.duration_s = math.inf
        # DJ transport collaborator; reset in setup() so a repeated or looped
        # scene starts fresh.
        self.transport = VideoTransportControls(self, loop_audio=loop_audio)

    def _display_name_for(self, filepath: str) -> str:
        """Prefer the file's own container `title` tag over its bare
        filename, when the file is local and actually carries one — the
        cheap header-only peek in video.probe_container_title (no frame
        decode, so this stays fast enough to run before every "UP NEXT"
        interstitial). Falls back to the filename otherwise, and always for
        a URL — its real title comes from yt-dlp instead, and probing a stream
        here would mean network I/O just to pick a display name."""
        return probe_container_title(filepath) or super()._display_name_for(filepath)

    def setup(self) -> None:
        if self._prepared:
            self._prepared = False
        elif not self._pick_filepath():
            super().setup()
            self.is_done = True
            return
        super().setup()
        # Back onto the audio-master clock, untouched, rather than inheriting a
        # prior run's pause/seek/loop/mute.
        self.transport.reset()
        self.transport.loop_store = make_loop_preset_store(self.filepath)
        if not ensure_pyav():
            log.warning(
                "PyAV unavailable; video scene cannot play %s "
                "(install with `uv tool install --force 'c64cast[all]'`)",
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
        # Created after the mode's setup() cleared the screen; the mode's first
        # frame push wipes it.
        bar = make_setup_bar(self.api, self.display_mode) if self._setup_progress else None
        progress = (
            SegmentedProgress(self._setup_segments(), bar.show)
            if bar is not None
            else SegmentedProgress.off()
        )

        sr = int(round(self.audio.effective_rate)) if self.audio else 8000
        # The peak scan only matters on the non-REU audible path: a muted scene
        # never pushes, and the REU path pre-encodes with its own gain. Skipping
        # it removes a full audio decode from setup.
        will_push_audio = self.audio is not None and not getattr(self.audio, "use_reu_pump", False)
        # The only resolution the display mode consumes (≤320×200); AVFileSource
        # downscales to it during decode rather than converting the full source
        # frame. See video._plan_decode_size.
        decode_target = getattr(self.display_mode, "frame_target_size", None)
        try:
            self.source = AVFileSource(
                self.filepath,
                target_sample_rate=sr,
                scan_audio_peak=will_push_audio,
                start_s=self.start_s,
                decode_target_size=decode_target,
                tempo_scale=self.tempo_scale,
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
        progress.complete("open")

        c = self._color
        # force_palette needs a blocking pre-scan: its k-means false-color map
        # must be fixed before the first frame or the mapping shifts
        # mid-playback. auto_fit on its own converges online instead.
        self._online_fit = None
        self._online_fit_frames = 0
        self._av_lag_min = math.inf
        self._av_lag_max = -math.inf
        self._av_lag_sum = 0.0
        self._av_lag_count = 0
        self._av_buf_min = math.inf
        self._av_last_log_t = 0.0
        if self.display_mode is not None:
            if c.force_palette:
                from c64cast.app.config import resolved_force_palette

                # One pre-scan pass derives the map, and the fit too since it
                # is already decoding.
                map_colors, map_indices = resolved_force_palette(c)
                fit, cmap = prescan_source_color(
                    self.filepath,
                    # Full strength: the mode lerps it by
                    # [color].auto_fit_strength at apply time.
                    fit_strength=1.0 if c.auto_fit else None,
                    map_colors=map_colors,
                    map_indices=map_indices,
                    decode_target_size=decode_target,
                    on_progress=progress.reporter("prescan"),
                )
                progress.complete("prescan")
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
                # Start neutral and converge during playback; see process_frame
                # and ONLINE_FIT_WARMUP_FRAMES.
                self.display_mode.set_color_fit(None)
                self.display_mode.set_color_map(None)
                # Full strength: the mode lerps it by
                # [color].auto_fit_strength at apply time.
                self._online_fit = ColorFitAccumulator(strength=1.0)
                log.info(
                    "video: auto-fit converging online over first %d frames",
                    ONLINE_FIT_WARMUP_FRAMES,
                )
            else:
                self.display_mode.set_color_fit(None)
                self.display_mode.set_color_map(None)

        has_audio = (self.source.a_stream is not None) and (self.audio is not None)
        if has_audio and isinstance(self.audio, UltimateAudioSampler):
            # The demuxer starts FIRST: the sampler's start() blocks collecting
            # a prebuffer, and push_samples accepts data before the ring is
            # gated, so starting the sampler first waits out the whole prebuffer
            # timeout on silence. AudioFileSource.setup keeps the same order.
            self.source.start(audio_push=self.audio.push_samples)
            self.audio.start()
            progress.complete("audio-start")
        elif has_audio and getattr(self.audio, "use_reu_pump", False):
            # audio_push=None makes the demuxer skip audio decode entirely: the
            # REU path has already pre-encoded the whole track, and decoding it
            # again would compete with video decode on the same thread and lag
            # the picture at scene start.
            assert isinstance(self.audio, AudioStreamer)
            audio_4bit = self._preencode_audio_for_reu()
            progress.complete("encode")
            # Bitmap modes push ~300 KB/sec of host DMAWRITE, halting the bus in
            # long bursts that cost the NMI ~50 % of its ticks; the default
            # chunk (128) then over-produces ~2× and overflows the audio ring in
            # ~2 sec. Char modes carry no bitmap traffic and keep the default.
            chunk = (
                REU_PUMP_CHUNK_SIZE_HEAVY_BUS
                if isinstance(self.display_mode, BitmapDisplayMode)
                else None
            )
            # A mode that installs the bank-swap dispatcher at $0314 (the
            # merged variant JMPs to $C100 on non-raster IRQs) owns that vector,
            # and its installer has already pre-uploaded a JMP $EA31 stub at
            # $C100 covering the gap until real audio bytes land there.
            skip_hook = bool(getattr(self.display_mode, "audio_reu_pump_active", False))
            self.audio.start_for_reu_staged(
                audio_4bit,
                chunk_size=chunk,
                skip_irq_vector_hook=skip_hook,
                on_progress=progress.reporter("upload"),
            )
            self.source.start(audio_push=None)
        elif has_audio:
            assert isinstance(self.audio, AudioStreamer)
            self.audio.start_for_external_source()
            self.source.start(audio_push=self.audio.push_samples)
        else:
            self.source.start(audio_push=None)
        progress.finish()
        self.wall_start_time = time.time()

    def _setup_segments(self) -> list[tuple[str, float]]:
        """The SegmentedProgress weights for this scene's blocking setup
        steps: coarse relative costs, decided from config before the
        container opens. Only prescan and upload report real denominators;
        the rest jump to done via complete(), and a segment that never runs
        (e.g. the file turns out to have no audio stream) is absorbed by
        finish()."""
        segments = [("open", 1.0)]
        if self.display_mode is not None and self._color.force_palette:
            segments.append(("prescan", 3.0))
        if isinstance(self.audio, UltimateAudioSampler):
            segments.append(("audio-start", 1.0))
        elif self.audio is not None and getattr(self.audio, "use_reu_pump", False):
            segments.extend((("encode", 1.0), ("upload", 4.0)))
        return segments

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
        assert isinstance(self.audio, AudioStreamer)
        # The REU pump's CIA #1 latch derives from the same nominal as the NMI
        # consumer, so it drains at the *achieved* rate; pre-encoding at the
        # requested one would play the clip off-speed.
        sr = int(round(self.audio.effective_rate))
        int16 = decode_audio_full(self.filepath, sr)
        if int16.size == 0:
            log.warning("video: empty audio track after decode; REU pump will play silence")
            return b""
        peak = int(np.abs(int16).max())
        gain = _compute_normalization_gain(peak)
        if gain != 1.0:
            int16 = np.clip(int16.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
        log.info("video: REU pre-encode peak=%d → gain=%.2fx (%d samples)", peak, gain, int16.size)
        floats = int16.astype(np.float32) / INT16_FULL_SCALE
        # The whole track at once, matching the per-chunk DSP the host-DMA path
        # applies in _encode_and_enqueue.
        floats = self.audio.process_offline_dsp(floats)
        # An explicit Generator, so this offline pass does not perturb the
        # global RNG state the realtime callbacks draw from.
        rng = np.random.default_rng() if self.audio.dither_enabled else None
        vol = encode_floats_to_dac(
            floats, dither=self.audio.dither_enabled, rng=rng, curve=self.audio.dac_curve
        )
        # ndarray.tobytes() is typed Any by the stubs; the wrap is for mypy.
        encoded = bytes(vol.tobytes())
        if getattr(self, "prepend_alignment_marker", False):
            from c64cast.audio.audio_marker import MARKER_DURATION_S, synthesize_marker_4bit

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

    # TransportSession getattr-probes these transport_* names on whatever
    # scene is current, so the duck-typed contract lives here even though the
    # state machine is video_transport.VideoTransportControls.
    def transport_pause(self) -> None:
        self.transport.pause()

    def transport_resume(self) -> None:
        self.transport.resume()

    def transport_toggle_pause(self) -> None:
        self.transport.toggle_pause()

    def transport_seek(self, target_s: float) -> None:
        self.transport.seek(target_s)

    def transport_loop_toggle(self) -> None:
        self.transport.loop_toggle()

    def transport_record(self) -> None:
        self.transport.record()

    def transport_stop(self) -> bool:
        return self.transport.stop()

    def transport_loop_slot(self, slot: int, *, save: bool, clear: bool) -> None:
        self.transport.loop_slot(slot, save=save, clear=clear)

    def transport_position(self) -> float:
        return self.transport.position()

    def transport_duration(self) -> float | None:
        return self.transport.duration()

    def transport_is_paused(self) -> bool:
        return self.transport.is_paused()

    def transport_loop_info(self) -> dict[str, float | str | None]:
        return self.transport.loop_info()

    def transport_loop_slots(self) -> list[int]:
        return self.transport.loop_slots()

    def process_frame(self, current_time: float) -> bool:
        # A source at EOF under an active A/B loop is about to wrap to A below,
        # so it does not end the scene.
        if self.source is None or (self.source.finished and self.transport.loop_state != "active"):
            # Clamps a sampler's position_seconds() to the pushed total; a
            # no-op for the DAC streamer, and idempotent.
            if self.audio is not None:
                mark_eof = getattr(self.audio, "mark_eof", None)
                if callable(mark_eof):
                    mark_eof()
            return False

        tr = self.transport
        clock_s = tr.clock_s()
        if tr.loop_state == "active" and tr.loop_a is not None:
            # loop_b is stored in content seconds; the clock is in the scaled
            # domain on the resync tempo path, so compare against the scaled B.
            at_b = tr.loop_b is not None and clock_s >= tr.content_to_clock(tr.loop_b)
            if at_b or self.source.finished:
                # On the resync path a source.finished wrap re-fires every frame
                # until the demuxer clears _eof, and each re-fire would drop the
                # first fresh post-A audio; flush and seek A exactly once.
                if not (tr.resync and self.source.seek_pending):
                    tr.seek(tr.loop_a)
                return True
        img = self.source.current_frame(clock_s)
        if img is None:
            return True  # still pre-rolling
        # AVFileSource.current_frame returns the SAME ndarray object between
        # PTS boundaries, and the playlist polls faster (50/60 Hz) than source
        # video runs (24-30 fps). Re-pushing those identical pixels doubles the
        # ~10 ms per-frame mhires/hires REU bus halt to ~60 % of the time, which
        # AM-modulates the SID DAC at the playlist rate — an audible 60 Hz buzz,
        # measured via Cam Link envelope FFT 2026-05-26. An OSD post or expiry
        # busts the skip for one render so the message appears or clears.
        osd_now = self.osd.current()
        new_source = img is not self._last_rendered_img
        if not new_source and osd_now == self._last_osd_shown:
            return True
        # A/V-lag accounting and the rolling auto_fit accumulator count real
        # frames, so an OSD-only re-render must not advance them.
        if new_source:
            self._last_rendered_img = img
            self._record_av_lag(clock_s, current_time)
        img = _crop_to_aspect(img)
        # Before annotation, so the debug digits stay out of the
        # contrast/saturation stats.
        if (
            new_source
            and self._online_fit is not None
            and self._online_fit_frames < ONLINE_FIT_WARMUP_FRAMES
            and self.display_mode is not None
        ):
            self._online_fit.add(img)
            self._online_fit_frames += 1
            self.display_mode.set_color_fit(self._online_fit.result())
        if self.show_frame_numbers:
            fps = self.source.video_fps or 30.0
            # clock_s is rebased to 0 at start_s, so add it back for the true
            # offset into the file — unless transport has been touched, past
            # which clock_s is already an absolute file position.
            file_s = tr.clock_to_content(clock_s) if tr.touched else clock_s + self.start_s
            label = f"{timecode(file_s)} f{int(round(file_s * fps))}"
            img = _annotate_frame_number(img, label)
        if osd_now:
            img = _annotate_osd(img, osd_now, self.osd.position)
        self._last_osd_shown = osd_now
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
        real time."""
        assert self.source is not None
        lag = clock_s - self.source.last_frame_pts
        depth = self.source.video_buffer_depth
        self._av_lag_min = min(self._av_lag_min, lag)
        self._av_lag_max = max(self._av_lag_max, lag)
        self._av_lag_sum += lag
        self._av_lag_count += 1
        self._av_buf_min = min(self._av_buf_min, depth)
        # clock/wall is how fast the master clock advances against real time:
        # ~1.0 is real-time, and below that is the host-DMA servo under-draining
        # the ring under bitmap DMA load. Tempo compensation pre-compresses
        # content, not the drain clock, so the figure is unmoved by it and
        # scripts/diags/mhires_tempo_clock_ab.py calibrates
        # [audio].dac_bitmap_tempo_* against it.
        wall = current_time - self.wall_start_time
        clock_wall = clock_s / wall if wall > 0 else 0.0
        if log.isEnabledFor(logging.DEBUG) and (
            current_time - self._av_last_log_t >= AV_LAG_LOG_INTERVAL_S
        ):
            self._av_last_log_t = current_time
            log.debug(
                "video A/V lag: now=%+.0fms (min=%+.0f avg=%+.0f max=%+.0f) "
                "buf=%d clock/wall=%.4f over %d frames",
                lag * 1000,
                self._av_lag_min * 1000,
                (self._av_lag_sum / self._av_lag_count) * 1000,
                self._av_lag_max * 1000,
                depth,
                clock_wall,
                self._av_lag_count,
            )

    def teardown(self) -> None:
        src, self.source = self.source, None
        steps: list[tuple[str, Callable[[], object]]] = [
            ("base teardown", super().teardown),
            # Idempotent, so a scene interrupted mid-record never leaves a red
            # border lingering into the next one.
            ("record border restore", partial(self.transport.set_record_border, False)),
        ]
        # Ahead of the audio stop: that zeroes `position_seconds()`, which is
        # what this summary's clock/wall gauge divides.
        steps.append(("A/V lag summary", self._log_av_lag_summary))
        if self.audio:
            steps.append(("audio stop", self.audio.stop))
        # Behind the audio stop: the close bounded-joins the demux thread, which
        # parks in the sampler's push_samples until the sampler itself stops.
        if src is not None:
            steps.append(("source close", src.close))
        steps.append(("identity-skip cache reset", self._reset_identity_skip_cache))
        run_teardown_steps(log, type(self).__name__, steps)

    def _reset_identity_skip_cache(self) -> None:
        # Nothing resets these in setup(), so a stale value would reach lap 2
        # and suppress its first OSD repaint.
        self._last_rendered_img = None
        self._last_osd_shown = None

    def _log_av_lag_summary(self) -> None:
        if not self._av_lag_count:
            return
        wall = time.time() - self.wall_start_time
        clock_wall = self.transport.clock_s() / wall if wall > 0 else 0.0
        log.info(
            "video A/V lag summary: min=%+.0f avg=%+.0f max=%+.0f ms, "
            "min buffer depth=%d, clock/wall=%.4f over %d displayed frames",
            self._av_lag_min * 1000,
            (self._av_lag_sum / self._av_lag_count) * 1000,
            self._av_lag_max * 1000,
            int(self._av_buf_min),
            clock_wall,
            self._av_lag_count,
        )


class LauncherScene(MediaFileMixin, Scene):
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
    MEDIA_EXTS = PROGRAM_EXTS
    MEDIA_LABEL = "launcher"

    def competes_for_audio_lock(self) -> bool:
        # The launched program outputs through the real SID whatever self.audio
        # is, so the scene contends unless `bypass_audio_lock` opts it out —
        # which is what lets several systems each run their own launcher.
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
        self.file_spec = file
        # Resolve once so a bad spec raises at construction; re-resolved at each
        # setup() so a newly dropped file becomes eligible.
        candidates = self._resolve_candidates()
        super().__init__(api, None, None, name or self._initial_scene_name(candidates))
        self.input_source = input_source
        self.reset_before_launch = reset_before_launch
        self.bypass_audio_lock = bool(bypass_audio_lock)
        self.min_duration_s = float(min_duration_s)
        self.poll_interval_s = float(poll_interval_s)
        self.launch_grace_s = float(launch_grace_s)
        self.max_duration_s = float(max_duration_s)
        # Nothing is rendered, so a low cap keeps host overhead negligible.
        self.target_fps = 4.0
        self.filepath: str = candidates[0]
        self.start_time = 0.0
        # Written by the poll thread, read by process_frame; hence the lock.
        self._last_input_t = 0.0
        self._input_lock = threading.Lock()
        self._baseline: bytes | None = None
        self._poll = PollThread(self._input_loop, name="launcher-input-poll", manual=True)
        self._prepared = False

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
        # A `.crt` in particular expects a reset to take effect.
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
        if (current_time - self.start_time) >= self.max_duration_s:
            return False
        if (current_time - self.start_time) < self.min_duration_s:
            return True
        with self._input_lock:
            last_input = self._last_input_t
        # duration_s is an idle timeout here, not a runtime.
        return (current_time - last_input) < self.duration_s

    def teardown(self) -> None:
        # The reset is mandatory for a `.crt` — `run_crt` leaves the cartridge
        # active — so it is a guarded step of its own, out of reach of a
        # RuntimeError from the input poll's join.
        run_teardown_steps(
            log,
            type(self).__name__,
            [
                ("input poll stop", self._poll.stop),
                ("base teardown", super().teardown),
                ("program reset", self.api.reset),
            ],
        )

    def _read_snapshot(self) -> bytes | None:
        """Read the configured input registers. Returns None on a failed
        read (caller ignores it — don't reset the idle clock on a glitch).
        Never reads $028D, so the app's modifier keys are excluded."""
        parts: list[bytes] = []
        if self.input_source in ("cia", "auto"):
            cia = self.api.read_memory(self._CIA_BASE, 2)
            if cia is None:
                return None
            # The upper bits carry keyboard-scan / serial state that churns
            # independently of player input.
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
