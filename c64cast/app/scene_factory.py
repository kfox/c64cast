"""Scene factory: the declarative ``[[scenes]]`` list -> live Scene objects.

`scenes_from_config` is the entry point: it validates every SceneCfg up
front (`validate_scene_cfg`, which fans out to the per-type `_validate_*`
helpers), then `build_scene` constructs each scene with its display mode,
overlays, effects, and audio source — dispatching to the mirror-image
`_build_<type>` helpers via the `_BUILDERS` table, then running the shared
epilogue. The `resolve_*` helpers coalesce the
tri-state config knobs (dither / color_match / cell_strategy / REU staging /
double-buffer / audio backend) into concrete per-scene values — doctor mode
calls them too, so a config is checked identically with and without hardware.

Split out of config.py: config owns the declarative model (dataclasses,
TOML loading, CLI merging), while this module imports the scene/display
runtime freely — the imports config.py had to defer to function level live
at module top here. Nothing in this module is read by the config-metadata
pipeline (introspect/schema/serializer/wizard read the dataclass metadata
in config.py), so the single source of truth is unmoved.
"""

from __future__ import annotations

import glob
import ipaddress
import logging
import math
import os
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from c64cast.audio.audio_source import (
    AudioFileSource,
    AudioSource,
    MicAudioSource,
    NullAudioSource,
    SidFileAudioSource,
)
from c64cast.audio.dac_curves import DAC_CURVE_CHOICES
from c64cast.audio.sampler import UltimateAudioSampler
from c64cast.hw.c64 import nmi_rate_safety
from c64cast.scenes import scenes as _scenes
from c64cast.scenes.effects import build_effect
from c64cast.scenes.generators import GenerativeSource, build_generator
from c64cast.scenes.overlays import build_overlay, paints_into_buffers, validate_for_scene
from c64cast.scenes.scenes import (
    BlankScene,
    LauncherScene,
    Scene,
    SlideshowScene,
    SourceScene,
    VideoScene,
    WebcamScene,
)
from c64cast.sid.asid_scene import AsidScene
from c64cast.sid.midi_scene import MidiScene
from c64cast.sid.sid_autoconfig import SID_MODEL_CHOICES, resolve_sid_model_cfg
from c64cast.sid.sid_host_emu import parse_sid_header, payload_overlaps_bank0_display
from c64cast.sid.songlengths import LengthsDB
from c64cast.sid.voice_scope import BITMAP_W as _SCOPE_BITMAP_W
from c64cast.sid.voice_scope import PERSISTENCE_NAMES, TIME_BASE_NAMES
from c64cast.sid.waveform import WaveformScene
from c64cast.video.dither import DITHER_METHODS
from c64cast.video.flicker import DEFAULT_TOLERANCE, FLICKER_TOLERANCES
from c64cast.video.modes import (
    BitmapDisplayMode,
    BlankDisplayMode,
    DisplayMode,
    HiresDisplayMode,
    MCMDisplayMode,
    MultiHiresDisplayMode,
    PETSCIIDisplayMode,
)
from c64cast.video.palette import CELL_STRATEGIES, COLOR_MATCH_MODES, resolve_color
from c64cast.video.video import WebcamSource, ensure_pyav
from c64cast.wled.wled_sink import WLEDSource

from . import paths
from .config import (
    _ASPECT_MODE_CHOICES,
    _AUDIO_SOURCE_CHOICES,
    _EFFECT_CHOICES,
    _EFFECT_SCENE_TYPES,
    _GENERATIVE_SOURCE_CHOICES,
    _INPUT_SOURCE_CHOICES,
    _MIDI_ACTION_CHOICES,
    _MIDI_CC_TYPE_CHOICES,
    _MIDI_MMC_COMMAND_CHOICES,
    _MIDI_VOICE_MODE_CHOICES,
    _MIDI_WAVEFORM_CHOICES,
    _MOD_SOURCE_CHOICES,
    LOOPBACK_HOSTS,
    ColorCfg,
    Config,
    ConfigError,
    ControlPlaneCfg,
    MidiControlCfg,
    SceneCfg,
    _is_valid_param_holder,
    scene_color,
)
from .orchestrator import resolve_orchestrator

if TYPE_CHECKING:
    from c64cast.audio.audio import AudioStreamer
    from c64cast.hw.backend import C64Backend

    from .quickcast import ResolvedMedia

log = logging.getLogger(__name__)

# Display modes that benefit from REU bank-swap double-buffering. Bitmap
# modes push a full 8000-byte frame every frame, so staging it off-screen and
# swapping $DD00 at vblank is what eliminates the single-buffer tearing that
# flashes the whole screen on scene cuts. Char modes (petscii/blank) are
# delta-cached small writes where staging is a net regression — so the "auto"
# setting leaves them on the host-DMA path. (mcm doesn't support staging.)
_REU_BITMAP_MODES = frozenset({"hires", "hires_edges", "mhires"})


def resolve_use_reu_staged(
    setting: bool | str,
    display: str,
    *,
    reu_available: bool,
    has_buffer_overlays: bool = False,
) -> bool:
    """Resolve the [video].use_reu_staged tri-state to a concrete bool for one
    scene's display mode.

    "auto" → True only for a bitmap display mode (see _REU_BITMAP_MODES) AND
    only when the hardware probe confirmed the REU is usable (reu_available) AND
    the scene has no buffer-painting (text) overlay. Such overlays fold fine
    high-contrast glyphs into the bitmap, and the REU bank-swap's mid-frame
    $DD00 swap (the ~9000-cycle REU→bank DMA runs the swap past vblank into the
    visible rows) makes bottom-row text shimmer; the host-DMA delta path renders
    it crisply. So a bitmap scene WITH text overlays resolves to host-DMA under
    auto — overlay-free bitmap video still gets the tear-free REU pipeline.

    Explicit true/false pass straight through (true forces REU even with text
    overlays — the caller has opted into the shimmer for tear-free cuts). The
    loader guarantees the only legal string is "auto"; any other string is
    treated as auto (False here) rather than silently truthy-True."""
    if isinstance(setting, str):
        if has_buffer_overlays:
            return False
        return reu_available and display in _REU_BITMAP_MODES
    return bool(setting)


def resolve_double_buffer(
    setting: bool | str,
    display: str,
    *,
    use_reu_staged: bool,
    backend_supports_reu: bool = False,
    has_buffer_overlays: bool = False,
    audio_reu_pump_active: bool = False,
) -> bool:
    """Resolve the [video].double_buffer tri-state to a concrete bool for one
    scene's display mode (the host-DMA page-flip path — see modes_irq.py
    HOSTDMA_SWAP_IRQ_HANDLER).

    Only bitmap modes have the two VIC banks to flip. It's mutually exclusive
    with REU staging (both drive $DD00), so a resolved use_reu_staged always
    wins.

    "auto" enables it where REU offers no tear-free alternative for the scene:
      * a backend with NO REU at all (the TeensyROM) — single-buffered host-DMA
        visibly tears there; or
      * a bitmap scene with a buffer-painting text overlay (has_buffer_overlays)
        on a REU backend — resolve_use_reu_staged turns the REU path OFF for
        these to dodge the bank-swap shimmer, which otherwise leaves them on
        single-buffer host-DMA that tears on scene cuts. The host-DMA double-
        buffer gives them tear-free frames AND crisp text (its swap IRQ does no
        in-IRQ DMA, so the $DD00 flip lands in vblank — no shimmer).
    Overlay-free bitmap video on a REU backend stays untouched (the REU path is
    the better tear-free option there). Explicit true/false pass through (still
    scoped to bitmap modes — true on a char mode is a no-op).

    Gated off when the scene runs the REU mic pump (audio_reu_pump_active): the
    host-DMA swap installs a plain $0314 raster IRQ (chains to $EA31) and the
    pump owns $0314 too, with no merged dispatcher for this pair (unlike the REU
    bank-swap path). Two $0314 owners would collide, so we stay single-buffer.
    Never reached on a no-REU backend — use_reu_pump is coerced off there."""
    if display not in _REU_BITMAP_MODES:
        return False
    if use_reu_staged:
        return False
    if audio_reu_pump_active:
        return False
    if isinstance(setting, str):  # "auto"
        return (not backend_supports_reu) or has_buffer_overlays
    return bool(setting)


# Display modes whose per-cell color lives in the screen matrix, which is the
# only memory the field-alternating page flip can re-point ($D018). See
# resolve_flicker_tolerance.
_FLICKER_DISPLAY_MODES = ("hires", "mhires")


def resolve_flicker_tolerance(
    setting: str,
    display: str,
    *,
    has_buffer_overlays: bool = False,
    audio_reu_pump_active: bool = False,
) -> str:
    """Resolve [color].flicker_tolerance for one scene's display mode (the
    field-alternating page flip — see modes_irq.FLICKER_SWAP_IRQ_HANDLER),
    returning "off" where blending cannot be honored.

    Opt-in, so there is no "auto" to resolve; this only decides where an
    explicit tolerance can actually be honored. Three gates, all structural:

      * the two bitmap modes only. What alternates is the screen matrix, which
        $D018 re-points every field, so a mode blends exactly the colors it
        keeps there: hires both nibbles of every cell, mhires its c1 and c2.
        mhires' c3 lives in color RAM at $D800 — not VIC-banked, not selected
        by $D018 — and its bg0 is the single $D021 register, so those two stay
        real colors (see modes/mhires.py). The char modes keep all their
        per-cell color in $D800 and so have nothing to alternate. This also
        excludes the fixed-2-color edges styling, which picks no color to
        blend: _build_display_mode reaches it through the separate
        "hires_edges" display name, never through "hires".
      * no buffer-painting text overlay. The second screen page sits at the
        $0C00 offset, which is also where overlays/big_text.py page-flips its
        own strip; the two cannot both own it.
      * not while the REU mic pump is running, which owns $0314.

    Blending brings its own bank-swapping double-buffer, so the caller clears
    both use_reu_staged and double_buffer when this returns anything but
    "off" — neither of those handlers carries the $D018 phase toggle, and all
    three want $0314."""
    if setting not in FLICKER_TOLERANCES:
        raise ValueError(
            f"[color].flicker_tolerance must be one of {tuple(FLICKER_TOLERANCES)}, got {setting!r}"
        )
    if setting == "off" or display not in _FLICKER_DISPLAY_MODES:
        return "off"
    return setting if not (has_buffer_overlays or audio_reu_pump_active) else "off"


def resolve_audio_backend(
    setting: str,
    *,
    supports_sampler: bool,
    sampler_available: bool,
) -> str:
    """Resolve the [audio].backend selector to a concrete ``"sampler"`` or
    ``"dac"`` for video-scene audio (mirrors resolve_use_reu_staged's pattern).

    The sampler is the U64 "Ultimate Audio" FPGA PCM path (sampler.py) — high
    fidelity, entirely off the C64 bus. ``supports_sampler`` is the backend
    capability (True on the Ultimate, False on TeensyROM); ``sampler_available``
    is the startup probe's verdict that the firmware exposes + routes it.

      * ``"auto"`` → ``"sampler"`` iff both are true, else ``"dac"``.
      * ``"sampler"`` → ``"sampler"`` iff both are true; otherwise logs a
        warning and degrades to ``"dac"`` (never silently silent).
      * ``"dac"`` → always ``"dac"`` (the lo-fi 4-bit $D418 path)."""
    if setting == "dac":
        return "dac"
    if supports_sampler and sampler_available:
        return "sampler"
    if setting == "sampler":
        log.warning(
            "[audio].backend = 'sampler' but the Ultimate Audio sampler is "
            "unavailable on this system (%s) — falling back to the 4-bit DAC. "
            "Enable 'Map Ultimate Audio $DF20-DFFF' (F2 -> C64 and Cartridge "
            "Settings) and set Vol Sampler L/R audible (F2 -> Audio Mixer), or "
            "set [audio].backend = 'dac' to silence this warning.",
            "no sampler support" if not supports_sampler else "feature not enabled",
        )
    return "dac"


def _build_display_mode(
    name: str,
    palette_mode: str = "percell",
    border: int | str = 0,
    background: int | str = 0,
    style: str = "default",
    use_reu_staged: bool = False,
    double_buffer: bool = False,
    audio_reu_pump_active: bool = False,
    color: ColorCfg | None = None,
    text_double_height: bool = False,
    dither_method: str = "none",
    cell_strategy: str = "frequency",
    flicker_tolerance: str = DEFAULT_TOLERANCE,
) -> DisplayMode:
    # border/background may be a C64 color name or an index; resolve to a plain
    # index here — the single point every scene's border/background flows
    # through — so the mode constructors (and callers) only ever see an int.
    border = resolve_color(border)
    background = resolve_color(background)
    # The whole [color] section is threaded through as one object; unpack the
    # static-shaping + forced-palette knobs the chromatic modes need here (a
    # single extraction point keeps the call sites to one `color=` kwarg).
    color = color if color is not None else ColorCfg()
    channel_boost = color.channel_boost
    hue_corrections = color.hue_corrections
    hue_corrections_replace = color.hue_corrections_replace_defaults
    force_palette = color.force_palette
    dither_strength = color.dither_strength
    # auto_fit_strength is applied mode-side now (the scenes install a
    # FULL-strength ColorFit and the mode lerps it by this factor at apply time)
    # so it's a live-tunable knob rather than frozen into the pre-scanned fit.
    # See DisplayMode._fit_for_apply + ColorFit.lerped.
    auto_fit_strength = color.auto_fit_strength
    # Resolve [color].color_match's "auto" against the concrete display mode —
    # the single point every mode's perceptual flag flows through.
    perceptual = resolve_color_match(color.color_match, name)
    if name == "hires_edges":
        return HiresDisplayMode(
            style="edges",
            use_reu_staged=use_reu_staged,
            double_buffer=double_buffer,
            audio_reu_pump_active=audio_reu_pump_active,
        )
    if name == "hires":
        return HiresDisplayMode(
            style="normal",
            use_reu_staged=use_reu_staged,
            double_buffer=double_buffer,
            audio_reu_pump_active=audio_reu_pump_active,
            dither_method=dither_method,
            dither_strength=dither_strength,
            perceptual=perceptual,
            cell_pick=color.hires_cell_pick,
            flicker_tolerance=flicker_tolerance,
            flicker_max_luma_delta=color.flicker_max_luma_delta,
            flicker_score_pairs=color.flicker_score_pairs,
        )
    if name == "petscii":
        return PETSCIIDisplayMode(
            style=style,
            use_reu_staged=use_reu_staged,
            channel_boost=channel_boost,
            hue_corrections=hue_corrections,
            hue_corrections_replace=hue_corrections_replace,
            perceptual=perceptual,
            auto_fit_strength=auto_fit_strength,
        )
    if name == "mcm":
        return MCMDisplayMode(
            palette_mode=palette_mode,
            channel_boost=channel_boost,
            hue_corrections=hue_corrections,
            hue_corrections_replace=hue_corrections_replace,
            force_palette=force_palette,
            dither_method=dither_method,
            dither_strength=dither_strength,
            perceptual=perceptual,
            auto_fit_strength=auto_fit_strength,
        )
    if name == "mhires":
        return MultiHiresDisplayMode(
            palette_mode=palette_mode,
            use_reu_staged=use_reu_staged,
            double_buffer=double_buffer,
            audio_reu_pump_active=audio_reu_pump_active,
            channel_boost=channel_boost,
            hue_corrections=hue_corrections,
            hue_corrections_replace=hue_corrections_replace,
            force_palette=force_palette,
            text_double_height=text_double_height,
            dither_method=dither_method,
            dither_strength=dither_strength,
            perceptual=perceptual,
            cell_strategy=cell_strategy,
            motion_smoothing=color.motion_smoothing,
            auto_fit_strength=auto_fit_strength,
            flicker_tolerance=flicker_tolerance,
            flicker_max_luma_delta=color.flicker_max_luma_delta,
            flicker_score_pairs=color.flicker_score_pairs,
        )
    if name == "blank":
        return BlankDisplayMode(border=border, background=background, use_reu_staged=use_reu_staged)
    raise ValueError(
        f"unknown display mode {name!r} (want: hires_edges, hires, petscii, mcm, mhires, blank)"
    )


_songlengths_cache: dict[str, LengthsDB | None] = {}
_AUTODETECT_SONGLENGTHS_ROOT = "assets/sids"


class _Unset:
    pass


_UNSET: Any = _Unset()
_songlengths_autodetected: str | None | Any = _UNSET


def _autodetect_songlengths_path(root: str = _AUTODETECT_SONGLENGTHS_ROOT) -> str | None:
    """Best-effort discovery of an unpacked HVSC's SongLengths.md5 under
    ``assets/sids`` (see assets/sids/README.md), for when
    ``[playlist].songlengths_file`` is left unset. Checks the two layouts an
    HVSC unpack actually produces before falling back to a full scan for a
    nonstandard placement. Memoized (including the "not found" result) since
    an HVSC tree is tens of thousands of files."""
    global _songlengths_autodetected
    if _songlengths_autodetected is not _UNSET:
        return _songlengths_autodetected
    found: str | None = None
    for candidate in (
        os.path.join(root, "C64Music", "DOCUMENTS", "Songlengths.md5"),
        os.path.join(root, "DOCUMENTS", "Songlengths.md5"),
    ):
        if os.path.isfile(candidate):
            found = candidate
            break
    else:
        if os.path.isdir(root):
            matches = sorted(
                os.path.join(dirpath, name)
                for dirpath, _dirnames, filenames in os.walk(root)
                for name in filenames
                if name.lower() == "songlengths.md5"
            )
            found = matches[0] if matches else None
    _songlengths_autodetected = found
    return found


def _load_songlengths(path: str | None) -> LengthsDB | None:
    """Memoized load of the HVSC SongLengths database. If ``path`` is unset
    (None — the field's default), auto-detects an unpacked HVSC under
    ``assets/sids``; an explicit empty string opts out of auto-detection.
    Returns None if no path is configured/detected or the file is
    missing/unreadable."""
    if path is None:
        path = _autodetect_songlengths_path()
        if path is None:
            return None
        log.info("playlist.songlengths_file not set; auto-detected HVSC database at %s", path)
    elif not path:
        return None
    else:
        path = paths.expand_user(path)
    if path in _songlengths_cache:
        return _songlengths_cache[path]
    try:
        db = LengthsDB.load(path)
    except FileNotFoundError:
        log.warning(
            "playlist.songlengths_file %s not found; waveform scenes will use default duration",
            path,
        )
        db = None
    except Exception:
        log.exception("failed to load songlengths %s", path)
        db = None
    _songlengths_cache[path] = db
    return db


def _attach_overlays(
    scene: Scene, overlay_dicts: list[dict[str, Any]], audio: AudioStreamer | None
) -> None:
    """Build overlay instances from config dicts and attach to scene.

    Validates that each overlay accepts the scene's display mode (e.g.
    REQUIRES_PETSCII). Raises with a clear error on first failure so
    misconfiguration is caught at load time, not 5 frames into the run."""
    for ov_cfg in overlay_dicts:
        ov = build_overlay(ov_cfg, audio)
        validate_for_scene(ov, scene.display_mode)
        scene.overlays.append(ov)


# Truthy stand-in for an AudioStreamer; used by `validate_scene_cfg` so the
# REQUIRES_AUDIO gate in `build_overlay` mirrors what `build_scene` would see
# at runtime when `[audio].enabled = true`. Overlay constructors only store
# the audio reference (they call into it at process_frame, not __init__), so
# a bare object satisfies validation without needing real audio hardware.
_AUDIO_SENTINEL: Any = object()


class MediaNotChosen(ValueError):
    """A scene that names no media at all, on a host whose default directory
    for that media is empty or absent.

    Fatal to a run like any other resolve failure — a scene with nothing to
    play cannot play. It carries its own type because it is the one failure
    that means *not yet* rather than *wrong*: it is the exact state a scene is
    in the moment the console adds it, before there is a form to name the file
    on. `config_store` excuses it while a show is being built, and nothing
    else. Anything the user actually typed — a bad glob, a path with a typo —
    stays a plain ValueError and stays fatal everywhere."""


def _resolve_file_spec_or_explain(
    s: SceneCfg, default_dir: str, exts: tuple[str, ...], *, label: str, drop_hint: str
) -> None:
    """Resolve the scene's `file` spec at validate time, defaulting to
    `default_dir` when unset — and mutating `s.file` to the resolved default
    so `build_scene` downstream (and the doctor/heartbeat) sees it. The scene
    re-resolves at each setup() so a directory's contents can change between
    iterations; resolving here just catches bad globs / empty dirs / typos at
    load time.

    On a resolve failure with the default still in place, raise the friendly
    "no `file =` set / drop one in the dir" guidance; otherwise re-raise
    resolve_file_spec's error verbatim. Shared by the video / waveform /
    slideshow / launcher branches, which differ only in dir, extensions, and
    the file-kind hint."""
    if not s.file:
        s.file = default_dir
    try:
        resolve_file_spec(s.file, exts, label=label)
    except ValueError as e:
        if s.file == default_dir:
            raise MediaNotChosen(
                f"{label} scene: no `file =` set and the default directory "
                f"{default_dir!r} is missing or empty. Drop {drop_hint} into "
                f'{default_dir}/ or set `file = "path"` on the scene '
                f"(comma-separated paths/dirs/globs accepted)."
            ) from e
        raise


def resolve_scene_display(display: str | None, scene_type: str) -> str:
    """Resolve a SceneCfg `display` value's per-scene-type default.

    Unset (`None`) resolves to `"mhires"` for video and wled scenes (the
    richest bitmap mode, suited to arbitrary film/photo/streamed-pixel content
    — matches quick playback's default, see quickcast._DEFAULT_VIDEO_DISPLAY)
    and `"hires_edges"` everywhere else (tuned for live webcam Canny-edge
    stylization, the historical global default). Any explicit value passes
    through unchanged. Slideshow has its own `_resolve_slideshow_display`
    (also handles `"random"`); this helper is for webcam/video/generative/wled
    and doctor's uniform per-scene reporting."""
    if display is not None:
        return display
    return "mhires" if scene_type in ("video", "wled") else "hires_edges"


def _display_mode_for_scene(
    display: str | None,
    s: SceneCfg,
    cfg: Config,
    *,
    reu_available: bool = False,
    backend_supports_reu: bool = False,
    force_host_dma: bool = False,
) -> DisplayMode:
    """Build the standard video display mode for a scene, centralizing the
    palette/border/background/style/REU/color kwarg cluster shared by the
    webcam, video, and slideshow paths (both the validate and build
    passes). `display` is passed explicitly because slideshow resolves
    "random" to a concrete mode first; an unset (`None`) `display` is
    resolved here via `resolve_scene_display`.

    `reu_available` resolves the [video].use_reu_staged tri-state (see
    resolve_use_reu_staged). The validate passes leave it False — auto then
    resolves to host-DMA, which is fine because the validation mode is a
    throwaway used only for overlay-compat checks (they don't depend on the
    staging flag). build_scene threads the real probe result.

    REU-staged video push (opt-in via [video].use_reu_staged): PETSCII and
    Blank honor the flag with single-buffer host-triggered REU→main DMAs (no
    IRQ install — coexists with REU audio cleanly today). Hires and
    MultiHires honor it with double-buffer + a C64-side raster IRQ at $0314
    that swaps $DD00 at vblank; when the scene also opts into REU audio, the
    bank-swap install picks a MERGED dispatcher whose non-raster branch JMPs
    the audio pump at $C100 so both IRQ sources (raster vblank + CIA #1
    jiffy) are serviced through one $0314 hook. MCM doesn't yet support
    use_reu_staged (separate future-work).

    `force_host_dma` hard-disables REU staging regardless of
    [video].use_reu_staged (including an explicit `= true`, which otherwise
    bypasses the auto path). Used for SID-audio scenes: the SID player owns the
    $0314 IRQ for PLAY, so the display must not install the bank-swap raster IRQ
    at the same vector."""
    display = resolve_scene_display(display, s.type)
    color = scene_color(cfg, s)
    has_buffer_overlays = any(
        paints_into_buffers(ov.get("type", "")) for ov in s.overlays if isinstance(ov, dict)
    )
    use_reu_staged = (
        False
        if force_host_dma
        else resolve_use_reu_staged(
            cfg.video.use_reu_staged,
            display,
            reu_available=reu_available,
            has_buffer_overlays=has_buffer_overlays,
        )
    )
    # Host-DMA double-buffer (no-REU backends). Also disabled by force_host_dma:
    # like the REU path it installs a $0314 raster IRQ, which would collide with
    # the SID player's PLAY IRQ on a SID-audio scene.
    double_buffer = (
        False
        if force_host_dma
        else resolve_double_buffer(
            cfg.video.double_buffer,
            display,
            use_reu_staged=use_reu_staged,
            backend_supports_reu=backend_supports_reu,
            has_buffer_overlays=has_buffer_overlays,
            audio_reu_pump_active=cfg.audio.use_reu_pump,
        )
    )
    # Flicker blend needs the $D018 phase toggle, which neither of the other two
    # swap handlers carries — so where it engages it takes over the double-buffer
    # slot and pushes REU staging aside, extending the mutual exclusion those two
    # already have. force_host_dma gates it for the same reason it gates the
    # others: a SID-audio scene's player owns $0314.
    flicker_tolerance = (
        "off"
        if force_host_dma
        else resolve_flicker_tolerance(
            color.flicker_tolerance,
            display,
            has_buffer_overlays=has_buffer_overlays,
            audio_reu_pump_active=cfg.audio.use_reu_pump,
        )
    )
    if flicker_tolerance != "off":
        use_reu_staged = False
        double_buffer = False
    return _build_display_mode(
        display,
        palette_mode=s.palette_mode,
        border=s.border,
        background=s.background,
        style=s.style,
        use_reu_staged=use_reu_staged,
        double_buffer=double_buffer,
        audio_reu_pump_active=cfg.audio.use_reu_pump,
        color=color,
        text_double_height=s.text_double_height,
        dither_method=resolve_dither_method(color.dither, s.type),
        cell_strategy=resolve_cell_strategy(color.cell_strategy, s.type),
        flicker_tolerance=flicker_tolerance,
    )


def _validate_blank(s: SceneCfg, cfg: Config) -> DisplayMode:
    # "hires_edges" is accepted alongside the real default (None) as a
    # historical quirk: it was SceneCfg's literal global default before
    # display became per-type-resolved, and blank ignores the value anyway
    # (always builds BlankDisplayMode below).
    if s.display not in (None, "blank", "hires_edges"):
        raise ValueError(f"blank scene must use display = 'blank', got {s.display!r}")
    return _build_display_mode(
        "blank",
        border=s.border,
        background=s.background,
        use_reu_staged=resolve_use_reu_staged(
            cfg.video.use_reu_staged, "blank", reu_available=False
        ),
    )


def _is_single_url_spec(spec: str | None) -> bool:
    """True if a `file =` spec is exactly one http(s) URL (not a comma-joined
    multi-spec). Single URLs are the form quick playback and configs resolve
    via yt-dlp; dir/glob/multi specs stay on the local-file path."""
    if not spec:
        return False
    s = spec.strip()
    return s.lower().startswith(("http://", "https://")) and "," not in s


def _validate_video(s: SceneCfg, cfg: Config) -> DisplayMode:
    _resolve_file_spec_or_explain(
        s, DEFAULT_VIDEO_DIR, VIDEO_EXTS, label="video", drop_hint="a video"
    )
    # Offline URL sanity (runs in --doctor too): a single URL that yt-dlp must
    # resolve (a YouTube/etc. page, not a direct media link) needs the `yt`
    # extra. Flag it now instead of failing at playback with a cryptic ffmpeg
    # "Invalid data found" when PyAV tries to open the page as a media file.
    if s.file is not None and _is_single_url_spec(s.file):
        # Deferred: quickcast imports this module's *_EXTS at top level,
        # so a top-level import here would be a cycle.
        from .quickcast import _ytdlp_available, url_needs_ytdlp

        if url_needs_ytdlp(s.file.strip()) and not _ytdlp_available():
            raise ValueError(
                f"video: {s.file!r} is a URL that needs yt-dlp to resolve, but the "
                "`yt` extra isn't installed. Install it (`uv tool install --force 'c64cast[all]'`), "
                "or use a direct media URL / local file."
            )
    if s.duration_s is not None:
        raise ValueError(
            "video scene does not accept `duration_s` — the scene "
            "runs until the video file ends. Remove the field from the "
            "config; use a [[scenes]] timeout via a different scene type "
            "if you want a hard cap."
        )
    if s.start_s is not None and s.start_s < 0:
        raise ValueError(f"video: start_s must be >= 0, got {s.start_s!r}")
    return _display_mode_for_scene(s.display, s, cfg)


def _validate_scope_knobs(s: SceneCfg, label: str) -> None:
    """Validate the shared VoiceScopeRenderer knobs (time_base / auto_cycles /
    persistence / scroll_columns) used by both waveform and midi scenes. Mirrors
    the constructor checks so doctor mode (no scene instance) catches them too."""
    if s.time_base not in TIME_BASE_NAMES:
        raise ValueError(
            f"{label}: time_base must be one of {tuple(TIME_BASE_NAMES)}, got {s.time_base!r}"
        )
    if s.auto_cycles <= 0:
        raise ValueError(f"{label}: auto_cycles must be > 0, got {s.auto_cycles!r}")
    if s.persistence not in PERSISTENCE_NAMES:
        raise ValueError(
            f"{label}: persistence must be one of {tuple(PERSISTENCE_NAMES)}, got {s.persistence!r}"
        )
    sc = s.scroll_columns
    if isinstance(sc, list):
        if len(sc) != 3 or not all(isinstance(x, int) for x in sc):
            raise ValueError(f"{label}: scroll_columns list must have 3 ints, got {sc!r}")
        if any(x < 0 or x > _SCOPE_BITMAP_W for x in sc):
            raise ValueError(
                f"{label}: scroll_columns entries must be in 0..{_SCOPE_BITMAP_W}, got {sc!r}"
            )
    elif isinstance(sc, int):
        if sc < 0 or sc > _SCOPE_BITMAP_W:
            raise ValueError(f"{label}: scroll_columns must be in 0..{_SCOPE_BITMAP_W}, got {sc!r}")
    else:
        raise ValueError(
            f"{label}: scroll_columns must be an int or list of 3 ints, got {type(sc).__name__}"
        )


def _validate_waveform(s: SceneCfg, cfg: Config) -> DisplayMode:
    _resolve_file_spec_or_explain(
        s, DEFAULT_WAVEFORM_DIR, SID_EXTS, label="waveform", drop_hint="a .sid"
    )
    _validate_scope_knobs(s, "waveform")
    # WaveformScene is bitmap-only — the SceneCfg `display` field is
    # ignored for this scene type. Synthesize a hires display_mode so
    # overlay compatibility checks fire against what the scene will
    # actually paint.
    return _build_display_mode("hires")


def _validate_midi(s: SceneCfg) -> DisplayMode:
    if len(s.midi_adsr) != 4:
        raise ValueError(f"midi scene midi_adsr must have 4 entries, got {s.midi_adsr!r}")
    if s.midi_voice_mode not in _MIDI_VOICE_MODE_CHOICES:
        raise ValueError(
            f"midi scene midi_voice_mode must be one of {_MIDI_VOICE_MODE_CHOICES}, "
            f"got {s.midi_voice_mode!r}"
        )
    if len(s.midi_voice_waveforms) > 3:
        raise ValueError(
            f"midi scene midi_voice_waveforms takes at most 3 entries (one per voice), "
            f"got {len(s.midi_voice_waveforms)}"
        )
    for spec in s.midi_voice_waveforms:
        tokens = [t.strip().lower() for t in str(spec).split("+") if t.strip()]
        if not tokens or any(t not in _MIDI_WAVEFORM_CHOICES for t in tokens):
            raise ValueError(
                f"midi scene midi_voice_waveforms entry {spec!r} must be one or a "
                f"'+'-combo of {_MIDI_WAVEFORM_CHOICES}"
            )
    if s.midi_voice_mode == "multitimbral":
        chans = s.midi_voice_channels[:3]
        if any(not 1 <= c <= 16 for c in chans):
            raise ValueError(
                f"midi scene midi_voice_channels must be MIDI channels 1..16, "
                f"got {s.midi_voice_channels!r}"
            )
        if len(set(chans)) != len(chans):
            raise ValueError(
                f"midi scene midi_voice_channels must be unique, got {s.midi_voice_channels!r}"
            )
    _validate_scope_knobs(s, "midi")
    # MidiScene is bitmap-only (hires oscilloscope) — the SceneCfg `display`
    # field is ignored. Synthesize a hires display_mode so overlay
    # compatibility validates against what the scene will actually paint
    # (and PETSCII overlays are rejected, as on a waveform scene).
    return _build_display_mode("hires")


def _validate_asid(s: SceneCfg) -> DisplayMode:
    # AsidScene carries the SID state in the stream, so it has no synth knobs
    # to validate — only the shared oscilloscope knobs. Like MidiScene it's
    # bitmap-only (hires), so synthesize a hires display_mode for overlay
    # compatibility (PETSCII overlays rejected).
    _validate_scope_knobs(s, "asid")
    if not (1 <= s.asid_max_sids <= 8):
        raise ValueError(f"asid: asid_max_sids must be in 1..8, got {s.asid_max_sids!r}")
    if s.asid_buffered_player not in ("auto", "on", "off"):
        raise ValueError(
            f"asid: asid_buffered_player must be auto|on|off, got {s.asid_buffered_player!r}"
        )
    return _build_display_mode("hires")


def _validate_slideshow(s: SceneCfg, cfg: Config) -> DisplayMode:
    _resolve_file_spec_or_explain(
        s, DEFAULT_SLIDESHOW_DIR, PICTURE_EXTS, label="slideshow", drop_hint="a .jpg/.png"
    )
    if s.image_duration_s <= 0:
        raise ValueError(f"slideshow: image_duration_s must be > 0, got {s.image_duration_s!r}")
    if s.aspect_mode not in _ASPECT_MODE_CHOICES:
        raise ValueError(
            f"slideshow: aspect_mode must be one of {_ASPECT_MODE_CHOICES}, got {s.aspect_mode!r}"
        )
    # Resolve "random" to a concrete mode for overlay-compat validation.
    # The actual scene re-resolves at every setup() so single-scene loops
    # get a fresh mode per iteration.
    display = _resolve_slideshow_display(s.display)
    if display == "blank":
        raise ValueError(
            "slideshow scene cannot use display = 'blank' (no place "
            "to paint the image — pick mhires/hires/hires_edges/mcm/"
            "petscii, or use display = 'random')."
        )
    return _display_mode_for_scene(display, s, cfg)


def _validate_generative(s: SceneCfg, cfg: Config) -> DisplayMode:
    if s.source not in _GENERATIVE_SOURCE_CHOICES:
        raise ValueError(
            f"generative scene `source` must be one of {_GENERATIVE_SOURCE_CHOICES}, "
            f"got {s.source!r}"
        )
    if s.display == "blank":
        raise ValueError(
            "generative scene cannot use display = 'blank' (there'd be nothing "
            "to quantize the generated frame). Pick mhires/hires/hires_edges/"
            "mcm/petscii."
        )
    if s.display == "random":
        raise ValueError(
            "generative scene does not support display = 'random' (only slideshow "
            "does). Pick a concrete mode."
        )
    if s.audio_source not in _AUDIO_SOURCE_CHOICES:
        raise ValueError(
            f"generative scene `audio_source` must be one of {_AUDIO_SOURCE_CHOICES}, "
            f"got {s.audio_source!r}"
        )
    if s.audio_source == "sid":
        # A SID source drives the chip directly; the DAC-path `audio` toggle is
        # meaningless for it (it plays regardless of [audio].enabled). Reject an
        # explicit per-scene `audio` rather than silently ignoring it.
        if s.audio is not None:
            raise ValueError(
                "generative scene with audio_source = 'sid' must not set `audio` — "
                "the SID plays on the chip regardless of the DAC/mic path. Remove "
                "`audio` (use audio_source = 'mic'/'none' for the live-mic path)."
            )
        # Resolve the .sid spec (default to the SID dir, like waveform) and
        # validate the first candidate's payload against the FIXED bank-0
        # display — a SID source can't relocate, so a bitmap display + a tune
        # that loads over $2000 is a hard conflict. setup() does the
        # authoritative per-pick check; this is the load-time fast-fail.
        _resolve_file_spec_or_explain(
            s, DEFAULT_WAVEFORM_DIR, SID_EXTS, label="generative sid audio", drop_hint="a .sid"
        )
        display = resolve_scene_display(s.display, s.type)
        mode = _display_mode_for_scene(display, s, cfg, force_host_dma=True)
        _check_first_sid_clears_display(s, mode, display)
        return mode
    if s.audio_source == "file":
        # Decode an audio file to the DAC + analyzer. `file` is required (no
        # default dir); resolve it now so a bad path/glob fails at load time. The
        # scene re-resolves at each setup() (a dir/glob random-picks per play).
        if not s.file:
            raise ValueError(
                'generative scene with audio_source = "file" needs `file = "..."` '
                "(an audio file, or a directory/glob of them)."
            )
        resolve_file_spec(s.file, AUDIO_EXTS, label="generative file audio")
        if not cfg.audio.enabled or s.audio is False:
            # The file streams to the C64's audio output (the off-bus sampler on a
            # sampler-capable U64, else the 4-bit DAC) and the analyzer taps that
            # same path, so with audio off there's neither playback nor
            # reactivity. Warn, don't fail (mirrors the mic/listen guidance).
            log.warning(
                "generative scene: audio_source = 'file' but audio is off "
                "(%s) — the file won't play or drive the visuals. Enable [audio] to "
                "hear the track and make the visuals react.",
                "this scene sets audio = false" if s.audio is False else "[audio].enabled is false",
            )
    if s.audio_source == "mic" and s.reactive and (not cfg.audio.enabled or s.audio is False):
        # The analyzer taps the mic callback, so no capture ⇒ no features. Warn
        # rather than fail: `reactive` defaults True, so a user who only wanted
        # silent generative visuals shouldn't have to turn it off explicitly.
        log.warning(
            "generative scene: audio_source = 'mic' with reactive = true, but the "
            "mic never runs (%s) — the visuals will stay time-driven. Enable "
            "[audio] to make them react to the input.",
            "this scene sets audio = false" if s.audio is False else "[audio].enabled is false",
        )
    if s.audio_source == "listen":
        # Listen-only exists solely to drive the visuals from the input, so
        # reactive = false leaves it opening nothing. And its capture still needs
        # the shared streamer, i.e. [audio].enabled (the per-scene `audio` DAC
        # toggle is irrelevant — listen never feeds the DAC). Warn, don't fail.
        if not s.reactive:
            log.warning(
                "generative scene: audio_source = 'listen' with reactive = false — "
                "listen captures the input only to drive the visuals, so with "
                "reactive off it opens nothing (silent, time-driven). Use "
                "audio_source = 'none' for a plain silent scene."
            )
        elif not cfg.audio.enabled:
            log.warning(
                "generative scene: audio_source = 'listen' with reactive = true, "
                "but [audio].enabled is false — the capture subsystem is off, so "
                "the visuals will stay time-driven. Enable [audio] (listen still "
                "produces no C64 audio) to make them react to the input."
            )
    # mic / listen / none: standard frame-source display (REU staging allowed).
    return _display_mode_for_scene(s.display, s, cfg)


def _check_first_sid_clears_display(s: SceneCfg, mode: DisplayMode, display: str) -> None:
    """Load-time guard: confirm the first resolvable .sid candidate's payload
    clears the (fixed bank-0) display regions. Best-effort fast-fail — a
    multi-entry pool may have other candidates, so this only raises when the
    first one parses and demonstrably conflicts (setup() does the authoritative
    per-pick check with bounded retry). Missing/unparseable files are left for
    setup() to surface."""
    assert s.file is not None  # set by _resolve_file_spec_or_explain above
    candidates = resolve_file_spec(s.file, SID_EXTS, label="generative sid audio")
    if not candidates:
        return
    path = candidates[0]
    try:
        with open(path, "rb") as f:
            sid_bytes = f.read()
        parse_sid_header(sid_bytes)  # magic / length
    except (OSError, ValueError):
        return  # let setup() report a real load error
    conflict = payload_overlaps_bank0_display(sid_bytes, is_bitmapped=mode.is_bitmapped)
    if conflict is not None:
        lo, hi = conflict
        region = "hires bitmap" if lo == 0x2000 else "screen RAM"
        raise ValueError(
            f"generative sid audio: {os.path.basename(path)}'s payload overlaps the "
            f"{display} display's {region} (${lo:04X}-${hi:04X}); a SID source "
            f"can't relocate the bank-0 display. Use a char display (petscii/mcm — "
            f"they reserve only $0400) or a SID that loads above ${hi:04X}."
        )


def _validate_wled(s: SceneCfg, cfg: Config) -> DisplayMode:
    """WLED pixel-sink scene: a virtual LED matrix streamed to over the LAN.

    Needs a quantizing display (there's a real BGR frame to render), so reject
    blank/random exactly like generative. Bounds the matrix dimensions — a sink
    presents `sink_width`×`sink_height` pixels the sender must match; absurd
    sizes are a config error, not a runtime surprise."""
    if s.display == "blank":
        raise ValueError(
            "wled scene cannot use display = 'blank' (there'd be nothing to "
            "quantize the streamed frame). Pick mhires/hires/hires_edges/mcm/petscii."
        )
    if s.display == "random":
        raise ValueError(
            "wled scene does not support display = 'random' (only slideshow does). "
            "Pick a concrete mode."
        )
    for label, value in (("sink_width", s.sink_width), ("sink_height", s.sink_height)):
        if not 1 <= value <= 1024:
            raise ValueError(f"wled scene {label} must be 1..1024, got {value!r}")
    for label, port in (("sink_ddp_port", s.sink_ddp_port), ("sink_wled_port", s.sink_wled_port)):
        if not 1 <= port <= 65535:
            raise ValueError(f"wled scene {label} must be 1..65535, got {port!r}")
    if s.sink_ddp_port == s.sink_wled_port:
        raise ValueError(
            f"wled scene sink_ddp_port and sink_wled_port must differ, both are {s.sink_ddp_port!r}"
        )
    for addr in s.sink_allow:
        try:
            ipaddress.ip_address(addr)
        except ValueError:
            raise ValueError(
                f"wled scene sink_allow entry {addr!r} is not a valid IP address"
            ) from None
    return _display_mode_for_scene(s.display, s, cfg)


def _validate_launcher(s: SceneCfg) -> None:
    """Self-contained launcher validation. The launched program owns the
    whole machine (VIC/SID/CIAs), so a launcher carries no display mode and
    no overlays — this validates and resolves any orchestrator itself, and
    `validate_scene_cfg` returns immediately after calling it (the shared
    overlay-compat loop assumes a real `mode`, which this scene never has)."""
    _resolve_file_spec_or_explain(
        s, DEFAULT_PROGRAM_DIR, PROGRAM_EXTS, label="launcher", drop_hint="a .prg/.crt"
    )
    if s.input_source not in _INPUT_SOURCE_CHOICES:
        raise ValueError(
            f"launcher: input_source must be one of {_INPUT_SOURCE_CHOICES}, got {s.input_source!r}"
        )
    if s.max_duration_s is not None and s.max_duration_s <= 0:
        raise ValueError(f"launcher: max_duration_s must be > 0, got {s.max_duration_s!r}")
    if s.min_duration_s < 0:
        raise ValueError(f"launcher: min_duration_s must be >= 0, got {s.min_duration_s!r}")
    # `display` is unset by default on SceneCfg; reject any explicit value
    # since the program — not c64cast — drives the VIC.
    if s.display is not None:
        raise ValueError(
            "launcher scene does not use `display` — the launched "
            "program owns the VIC. Remove the field from the scene."
        )
    if s.overlays:
        raise ValueError(
            "launcher scene cannot carry overlays — the launched program "
            "owns screen + color RAM, so overlays would be overwritten."
        )
    if s.orchestrate:
        resolve_orchestrator(s)


def validate_nmi_sample_rate(cfg: Config) -> None:
    """Guard [audio].sample_rate against the NMI handler's cycle budget.

    Raises ConfigError when the configured rate would overrun the $D418 DAC NMI
    handler on the target system (NMIs queue → pitch drop); logs a warning for
    rates inside the entry-latency margin. Thin pass-through to
    `c64.nmi_rate_safety` so the rule lives in one place (shared with --doctor).
    No-op when audio is disabled.

    Runs before hardware opens (see `validate_configs`'s docstring), so
    `[ultimate64].system` may still be the unresolved "auto" — assume NTSC,
    matching that field's own documented fallback and `hw_provision.
    resolve_system`'s convention, rather than falling through to PAL the way
    `c64.py`'s helpers used to for any non-"NTSC" string."""
    if not cfg.audio.enabled:
        return
    system = "NTSC" if cfg.ultimate64.system.upper() == "AUTO" else cfg.ultimate64.system
    level, message = nmi_rate_safety(system, cfg.audio.sample_rate)
    if level == "error":
        raise ConfigError(f"[audio].sample_rate: {message}")
    if level == "warn":
        log.warning("[audio].sample_rate: %s", message)


def validate_sampler_cfg(cfg: Config) -> None:
    """Guard the Ultimate Audio sampler settings ([audio].sampler_bits /
    sampler_sample_rate). Raises ConfigError on an unusable value. No-op when
    audio is disabled; the rate is only *used* when [audio].backend resolves to
    the sampler, but validating unconditionally keeps a typo from lurking until
    the backend is selected. The ring is length-independent (streaming), so
    there is no per-clip overflow check — see sampler.py."""
    if not cfg.audio.enabled:
        return
    if cfg.audio.sampler_bits not in (8, 16):
        raise ConfigError(f"[audio].sampler_bits must be 8 or 16, got {cfg.audio.sampler_bits}")
    if not 1000 <= cfg.audio.sampler_sample_rate <= 48000:
        raise ConfigError(
            "[audio].sampler_sample_rate must be 1000..48000 Hz, got "
            f"{cfg.audio.sampler_sample_rate}"
        )


def validate_dac_curve_cfg(cfg: Config) -> None:
    """Guard [audio].dac_curve: reject an unknown curve name and the
    dac_curve + digi_boost combination (both commandeer the 3 SID voices for
    different DAC schemes). No-op when audio is disabled."""
    if not cfg.audio.enabled:
        return
    if cfg.audio.dac_curve not in DAC_CURVE_CHOICES:
        raise ConfigError(
            f"[audio].dac_curve must be one of {', '.join(DAC_CURVE_CHOICES)}, "
            f"got {cfg.audio.dac_curve!r}"
        )
    # An EXPLICIT non-linear curve conflicts with digi_boost (both park the 3 SID
    # voices as DC sources for different DAC schemes). "auto" is not a conflict:
    # it yields to digi_boost by resolving to linear (see
    # dac_curve_resolve.resolve_dac_curve_for_backend).
    if cfg.audio.dac_curve in ("mahoney_ultisid", "calibrated") and cfg.audio.digi_boost:
        raise ConfigError(
            "[audio].dac_curve and [audio].digi_boost are mutually exclusive "
            "(both park the SID voices as DC sources for different DAC schemes). "
            "Set digi_boost = false to use the Mahoney curve."
        )


def validate_sid_model_cfg(cfg: Config) -> None:
    """Guard [ultimate64].sid_model: reject an unknown value."""
    if cfg.ultimate64.sid_model not in SID_MODEL_CHOICES:
        raise ConfigError(
            f"[ultimate64].sid_model must be one of {', '.join(SID_MODEL_CHOICES)}, "
            f"got {cfg.ultimate64.sid_model!r}"
        )


def validate_dac_bitmap_tempo_cfg(cfg: Config) -> None:
    """Guard the bitmap+DAC tempo-compensation fractions ([audio].
    dac_bitmap_tempo_hires / _mhires): each must be 0.5..1.0. The lower bound is
    atempo's single-stage floor — content is time-compressed by 1/value, and
    atempo only spans 0.5..2.0 per stage, so value < 0.5 → factor > 2.0 can't be
    realized in one filter. 1.0 = compensation off. No-op when audio is
    disabled."""
    if not cfg.audio.enabled:
        return
    for name, value in (
        ("dac_bitmap_tempo_hires", cfg.audio.dac_bitmap_tempo_hires),
        ("dac_bitmap_tempo_mhires", cfg.audio.dac_bitmap_tempo_mhires),
    ):
        if not 0.5 <= value <= 1.0:
            raise ConfigError(
                f"[audio].{name} must be 0.5..1.0 (observed playback-speed "
                f"fraction; 1.0 = off), got {value}"
            )


DITHER_CHOICES: tuple[str, ...] = ("auto", *DITHER_METHODS)

# Scene types whose source is effectively static once composed (a slideshow
# holds one image for its whole dwell time), so the expensive floyd-steinberg/
# atkinson per-pixel loop is a one-time cost, not a per-frame one. Everything
# else `resolve_dither_method` sees is a motion scene.
_STATIC_DITHER_SCENE_TYPES = frozenset({"slideshow"})


def resolve_dither_method(dither_setting: str, scene_type: str) -> str:
    """Resolve [color].dither's `"auto"` to a concrete dither.DITHER_METHODS
    value for a given scene type; an explicit non-auto value passes through
    unchanged (a user may force floyd-steinberg/atkinson, or the older
    'ordered' Bayer method, on a motion scene and accept the caveats — see
    docs/caveats.md).

    `"auto"` picks the best method that's actually USEFUL for the scene, not
    merely a safe default: static scenes (slideshow) get floyd-steinberg,
    the highest-quality method, since it's composed once and cost is a
    non-issue; everything else (video/webcam/generative — anything that
    recomposes every frame) gets blue_noise, the best method that stays
    realtime (vectorized) and temporally stable (its fixed tiling means the
    same pixel position always dithers the same way, so it doesn't add
    frame-to-frame shimmer the way independently-diffused frames would) —
    strictly better than 'ordered' (Bayer) at the same cost, since it drops
    Bayer's visible cross-hatch/grid structure without giving up either
    property (see dither.py's module docstring)."""
    if dither_setting != "auto":
        return dither_setting
    return "floyd-steinberg" if scene_type in _STATIC_DITHER_SCENE_TYPES else "blue_noise"


def effective_colors(cfg: Config) -> list[tuple[str, ColorCfg]]:
    """Every distinct effective [color] section `cfg` resolves to: the global
    section, plus one per scene whose ``[scenes.color]`` overrides it — each
    labeled for use in a ConfigError/report message.

    The four ``validate_*_cfg`` guards below (and doctor's per-aspect probes)
    loop this instead of reading ``cfg.color`` directly, so a bad value inside
    a scene override surfaces the same as a bad global value, naming the scene
    it came from. `scene_color` raises a plain `ValueError` for a bad
    `force_palette_colors` override (it's also called at load time, outside
    any ConfigError-only handler); re-raised here as `ConfigError` so the
    session/doctor callers that only catch `ConfigError` around these guards
    don't see an unhandled exception."""
    out: list[tuple[str, ColorCfg]] = [("[color]", cfg.color)]
    for i, s in enumerate(cfg.scenes):
        if s.color:
            label = f"[[scenes]][{i}].color"
            try:
                out.append((label, scene_color(cfg, s)))
            except ValueError as e:
                raise ConfigError(f"{label}: {e}") from e
    return out


def dither_cfg_error(label: str, color: ColorCfg) -> str | None:
    """Range-check one resolved [color] section's dither/dither_strength;
    returns the ConfigError message, or None if `color` is fine.

    Split out of `validate_dither_cfg` so doctor's per-scene report can check
    one scene at a time (via `effective_colors`) without a bad override in
    scene N hiding the resolution report for every other scene — see
    `validate_dither_cfg` for the fail-fast form of the same check."""
    if color.dither not in DITHER_CHOICES:
        return f"{label}.dither must be one of {', '.join(DITHER_CHOICES)}, got {color.dither!r}"
    if not 0.0 <= color.dither_strength <= 2.0:
        return f"{label}.dither_strength must be 0..2.0, got {color.dither_strength}"
    return None


def validate_dither_cfg(cfg: Config) -> None:
    """Guard dither/dither_strength on [color] and every scene override:
    reject an unknown method name or an out-of-range strength."""
    for label, color in effective_colors(cfg):
        err = dither_cfg_error(label, color)
        if err:
            raise ConfigError(err)


def motion_smoothing_cfg_error(label: str, color: ColorCfg) -> str | None:
    """Range-check one resolved [color] section's motion_smoothing (0..1);
    returns the ConfigError message, or None if `color` is fine. See
    `dither_cfg_error` for why this is split from `validate_motion_smoothing_cfg`."""
    if not 0.0 <= color.motion_smoothing <= 1.0:
        return f"{label}.motion_smoothing must be 0..1.0, got {color.motion_smoothing}"
    return None


def validate_motion_smoothing_cfg(cfg: Config) -> None:
    """Guard motion_smoothing on [color] and every scene override: reject an
    out-of-range value (0..1)."""
    for label, color in effective_colors(cfg):
        err = motion_smoothing_cfg_error(label, color)
        if err:
            raise ConfigError(err)


COLOR_MATCH_CHOICES: tuple[str, ...] = ("auto", *COLOR_MATCH_MODES)

# Display modes whose "auto" color_match resolves to perceptual (CIE-Lab). These
# are the modes that make a genuine nearest-of-16 color decision; the perceptual
# metric picks the color the eye calls closest and needs no channel_boost /
# gray-penalty bias (see palette.quantize_distances_for). Modes not listed
# ("blank", "hires_edges") pick no colors, so the setting is a no-op there and
# auto resolves to rgb (harmless).
_COLOR_MATCH_AUTO_PERCEPTUAL: frozenset[str] = frozenset({"mcm", "mhires", "hires", "petscii"})


def resolve_color_match(color_match_setting: str, display_mode_name: str) -> bool:
    """Resolve [color].color_match to a perceptual bool for a display mode.

    An explicit 'perceptual'/'rgb' passes through; 'auto' picks perceptual for
    the quantizing modes (see _COLOR_MATCH_AUTO_PERCEPTUAL) and rgb otherwise."""
    if color_match_setting == "perceptual":
        return True
    if color_match_setting == "rgb":
        return False
    return display_mode_name in _COLOR_MATCH_AUTO_PERCEPTUAL


def color_match_cfg_error(label: str, color: ColorCfg) -> str | None:
    """Check one resolved [color] section's color_match choice; returns the
    ConfigError message, or None if `color` is fine. See `dither_cfg_error`
    for why this is split from `validate_color_match_cfg`."""
    if color.color_match not in COLOR_MATCH_CHOICES:
        return (
            f"{label}.color_match must be one of {', '.join(COLOR_MATCH_CHOICES)}, "
            f"got {color.color_match!r}"
        )
    return None


def validate_color_match_cfg(cfg: Config) -> None:
    """Guard color_match on [color] and every scene override: reject an
    unknown value."""
    for label, color in effective_colors(cfg):
        err = color_match_cfg_error(label, color)
        if err:
            raise ConfigError(err)


CELL_STRATEGY_CHOICES: tuple[str, ...] = ("auto", *CELL_STRATEGIES)

# Scene types whose composed frame is effectively static (a slideshow holds one
# image for its whole dwell), so the costlier error-min cell strategy is a
# one-time cost and worth its better reconstruction. Everything else recomposes
# every frame, where frequency's temporal stability (it ranks the EMA-smoothed
# histogram) avoids per-frame slot churn. Mirrors _STATIC_DITHER_SCENE_TYPES.
_STATIC_CELL_STRATEGY_SCENE_TYPES = frozenset({"slideshow"})


def resolve_cell_strategy(cell_strategy_setting: str, scene_type: str) -> str:
    """Resolve [color].cell_strategy's `"auto"` to a concrete CELL_STRATEGIES
    value for a scene type; an explicit value passes through unchanged.

    `"auto"` picks error-min for static scenes (slideshow — composed once, so the
    C(K,3)-per-cell search cost is paid once for the best reconstruction) and
    frequency for motion scenes (video/webcam/generative), whose per-frame
    recompose makes frequency's temporal stability the right default."""
    if cell_strategy_setting != "auto":
        return cell_strategy_setting
    return "error-min" if scene_type in _STATIC_CELL_STRATEGY_SCENE_TYPES else "frequency"


def cell_strategy_cfg_error(label: str, color: ColorCfg) -> str | None:
    """Check one resolved [color] section's cell_strategy choice; returns the
    ConfigError message, or None if `color` is fine. See `dither_cfg_error`
    for why this is split from `validate_cell_strategy_cfg`."""
    if color.cell_strategy not in CELL_STRATEGY_CHOICES:
        return (
            f"{label}.cell_strategy must be one of {', '.join(CELL_STRATEGY_CHOICES)}, "
            f"got {color.cell_strategy!r}"
        )
    return None


def validate_cell_strategy_cfg(cfg: Config) -> None:
    """Guard cell_strategy on [color] and every scene override: reject an
    unknown value."""
    for label, color in effective_colors(cfg):
        err = cell_strategy_cfg_error(label, color)
        if err:
            raise ConfigError(err)


def validate_control_cfg(control_cfg: ControlPlaneCfg) -> None:
    """Guard [control]: refuse an unauthenticated plane on a network address.

    Takes the already-resolved ControlPlaneCfg (`loaded.master_control` — the
    section is process-wide like [midi_control], not per-system-cascaded), so
    what is checked is what `session.start_services` will actually bind.
    No-op when disabled, and on loopback, where the port is reachable only by
    someone who already has a shell on this machine.

    [web] needs no equivalent: that surface generates a token rather than
    offering an open mode at all."""
    if not control_cfg.enabled or control_cfg.token or control_cfg.allow_unauthenticated:
        return
    if control_cfg.host in LOOPBACK_HOSTS:
        return
    raise ConfigError(
        f"[control].host is {control_cfg.host!r} with no token — anything that can "
        "reach the port could drive the run (pause, skip, launch clips, reload "
        "configs). Set C64CAST_CONTROL_TOKEN in the environment, or [control].token "
        "in the config; on a network you trust, set "
        "[control].allow_unauthenticated = true to keep it open."
    )


def validate_midi_control_cfg(midi_cfg: MidiControlCfg) -> None:
    """Guard [midi_control]: jump_transition choice, broadcast_channel
    range, and every cc_map entry's shape. Takes the already-resolved
    MidiControlCfg (loaded.master_midi_control in ensemble mode, else
    cfgs[0].midi_control — see cli.py) rather than a whole Config, since
    [midi_control] is process-wide like [control], not per-system-cascaded.
    No-op when disabled."""
    if not midi_cfg.enabled:
        return
    if midi_cfg.jump_transition not in ("cut", "interstitial"):
        raise ConfigError(
            "[midi_control].jump_transition must be 'cut' or 'interstitial', "
            f"got {midi_cfg.jump_transition!r}"
        )
    if midi_cfg.osd not in ("bottom", "top", "off"):
        raise ConfigError(
            f"[midi_control].osd must be 'bottom', 'top', or 'off', got {midi_cfg.osd!r}"
        )
    if midi_cfg.loop_audio not in ("on", "mute"):
        raise ConfigError(
            f"[midi_control].loop_audio must be 'on' or 'mute', got {midi_cfg.loop_audio!r}"
        )
    if not 1 <= midi_cfg.broadcast_channel <= 16:
        raise ConfigError(
            f"[midi_control].broadcast_channel must be 1..16, got {midi_cfg.broadcast_channel}"
        )
    if not isinstance(midi_cfg.controller_profile, str) or not midi_cfg.controller_profile:
        raise ConfigError(
            "[midi_control].controller_profile must be a non-empty string "
            f"('auto', 'off', or a profile name), got {midi_cfg.controller_profile!r}"
        )
    for i, entry in enumerate(midi_cfg.cc_map):
        if not isinstance(entry, dict):
            raise ConfigError(f"[midi_control].cc_map[{i}] must be a table, got {entry!r}")
        kind = entry.get("type")
        if kind not in _MIDI_CC_TYPE_CHOICES:
            raise ConfigError(
                f"[midi_control].cc_map[{i}].type must be one of "
                f"{', '.join(_MIDI_CC_TYPE_CHOICES)}, got {kind!r}"
            )
        number = entry.get("number")
        if not isinstance(number, int) or not 0 <= number <= 127:
            raise ConfigError(f"[midi_control].cc_map[{i}].number must be 0..127, got {number!r}")
        if kind == "mmc" and number not in _MIDI_MMC_COMMAND_CHOICES:
            raise ConfigError(
                f"[midi_control].cc_map[{i}] type 'mmc' number must be one of "
                f"{sorted(_MIDI_MMC_COMMAND_CHOICES)} (an MMC command byte), got {number!r}"
            )
        action = entry.get("action")
        if action not in _MIDI_ACTION_CHOICES:
            raise ConfigError(
                f"[midi_control].cc_map[{i}].action must be one of "
                f"{', '.join(_MIDI_ACTION_CHOICES)}, got {action!r}"
            )
        if action == "jump" and not isinstance(entry.get("scene"), int):
            raise ConfigError(f"[midi_control].cc_map[{i}] action 'jump' needs an int 'scene'")
        if action == "param":
            target = entry.get("target")
            if (
                not isinstance(target, str)
                or "." not in target
                or not _is_valid_param_holder(target.split(".", 1)[0])
            ):
                raise ConfigError(
                    f"[midi_control].cc_map[{i}] action 'param' needs a string 'target' "
                    "of the form 'effect.<name>', 'source.<name>', 'scene.<name>', "
                    "'mode.<name>', or a layer-addressed 'fx<N>.<name>' / "
                    f"'effect[<N>].<name>', got {target!r}"
                )
        if action == "transport.jog":
            mode = entry.get("mode")
            if mode is not None and mode not in ("abs", "rel"):
                raise ConfigError(
                    f"[midi_control].cc_map[{i}] action 'transport.jog' mode must be "
                    f"'abs' or 'rel', got {mode!r}"
                )
        if action in ("loop_slot", "clip_launch", "look_save", "look_recall"):
            slot = entry.get("slot")
            if not isinstance(slot, int) or isinstance(slot, bool) or slot < 1:
                raise ConfigError(
                    f"[midi_control].cc_map[{i}] action {action!r} needs an int 'slot' "
                    f">= 1, got {slot!r}"
                )
        if action == "fx_toggle":
            # Effect layer index is 0-based (fx0 is the first layer), so >= 0.
            slot = entry.get("slot")
            if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
                raise ConfigError(
                    f"[midi_control].cc_map[{i}] action 'fx_toggle' needs an int 'slot' "
                    f">= 0 (0-based effect layer index), got {slot!r}"
                )


def _has_sid_scene(cfg: Config) -> bool:
    """True if the playlist has any SID-driven scene the WLED audio-sync
    broadcaster could source features from: a waveform scene, or a generative
    scene whose audio_source is a SID file."""
    return any(
        s.type == "waveform" or (s.type == "generative" and s.audio_source == "sid")
        for s in cfg.scenes
    )


# [wled] endpoint defaults, per direction. Broadcast targets WLED's Audio Sync
# multicast group; listen binds the Mode-1 JSON API on all interfaces so the LAN
# can reach it (the mDNS SRV record carries the real port for app discovery).
WLED_BROADCAST_DEFAULT_HOST = "239.0.0.1"
WLED_BROADCAST_DEFAULT_PORT = 11988
WLED_LISTEN_DEFAULT_HOST = "0.0.0.0"
WLED_LISTEN_DEFAULT_PORT = 8080

_WLED_DISABLED_TOKENS = frozenset({"", "disabled"})
_WLED_ENABLED_TOKEN = "enabled"


def parse_wled_endpoint(
    value: str | None, default_host: str, default_port: int, *, field_name: str
) -> tuple[bool, str, int]:
    """Decode a combined `[wled]` on/off+endpoint value into (enabled, host, port).

    Grammar (see WledCfg): None / "disabled" → off; "enabled" → on with the
    passed defaults; otherwise "[host][:port]" → on, where a bare "HOST" (no
    colon) sets only the host and a leading ":PORT" sets only the port. Missing
    parts fall back to the defaults. Raises ConfigError on a non-integer or
    out-of-range port. Pure — safe to call from resolvers and doctor."""
    if value is None:
        return (False, default_host, default_port)
    token = value.strip()
    low = token.lower()
    if low in _WLED_DISABLED_TOKENS:
        return (False, default_host, default_port)
    if low == _WLED_ENABLED_TOKEN:
        return (True, default_host, default_port)
    host, sep, port_str = token.rpartition(":")
    if not sep:
        # No colon: the whole value is a host override.
        return (True, token, default_port)
    host = host or default_host
    if not port_str:
        return (True, host, default_port)
    try:
        port = int(port_str)
    except ValueError as e:
        raise ConfigError(f"{field_name}: bad port {port_str!r} in {value!r}") from e
    if not 1 <= port <= 65535:
        raise ConfigError(f"{field_name}: port must be 1..65535, got {port}")
    return (True, host, port)


def resolve_wled_broadcast(cfg: Config) -> tuple[bool, str, int]:
    """(enabled, host, port) for the Mode 3 audio-sync broadcast target."""
    return parse_wled_endpoint(
        cfg.wled.broadcast,
        WLED_BROADCAST_DEFAULT_HOST,
        WLED_BROADCAST_DEFAULT_PORT,
        field_name="[wled].broadcast",
    )


def resolve_wled_listen(cfg: Config) -> tuple[bool, str, int]:
    """(enabled, host, port) for the Mode 1 virtual-WLED-device JSON API bind."""
    return parse_wled_endpoint(
        cfg.wled.listen,
        WLED_LISTEN_DEFAULT_HOST,
        WLED_LISTEN_DEFAULT_PORT,
        field_name="[wled].listen",
    )


def validate_wled_cfg(cfg: Config) -> None:
    """Guard [wled] (both directions). Parse each endpoint (raising on a bad
    host:port), bound the broadcast rate, and warn — don't fail — when broadcast
    is enabled with no SID-driven scene to source features from (nothing would
    go out). Mode 1 (listen) needs no SID scene. No-op when both are off."""
    broadcast_on, _, _ = resolve_wled_broadcast(cfg)
    resolve_wled_listen(cfg)  # parse for validation side effect (raises on bad)
    if not 1.0 <= cfg.wled.rate_hz <= 120.0:
        raise ConfigError(f"[wled].rate_hz must be 1..120, got {cfg.wled.rate_hz}")
    if not isinstance(cfg.wled.broadcast_tempo_fallback, bool):
        raise ConfigError(
            "[wled].broadcast_tempo_fallback must be true/false, got "
            f"{cfg.wled.broadcast_tempo_fallback!r}"
        )
    if broadcast_on and cfg.wled.broadcast_tempo_fallback:
        # The tempo fallback keeps a non-SID scene lit, so the "nothing to
        # broadcast" warning below no longer applies — the grid supplies packets.
        return
    if broadcast_on and not _has_sid_scene(cfg):
        log.warning(
            "[wled] broadcast enabled but no SID-driven scene (waveform, or "
            "generative with audio_source = 'sid') in the playlist — nothing "
            "will be broadcast."
        )


# Every whole-Config validator in this module, in the order a run applies
# them. `session.validate_configs` iterates this rather than naming them one
# by one: the hand-written list had already fallen a validator behind
# (`validate_wled_cfg` reached `--doctor` and no actual run), and the symptom
# of the next omission is a mid-show failure instead of a pre-hardware
# rejection. tests/test_scene_factory_validators.py holds the tuple to a
# partition of the module's `validate_*(cfg: Config)` callables.
#
# The two validators that take a *section* rather than a whole Config
# (`validate_control_cfg`, `validate_midi_control_cfg`) are deliberately not
# here: [control] and [midi_control] are process-wide, so they are checked
# once against the master, not once per system.
PER_SYSTEM_VALIDATORS: tuple[Callable[[Config], None], ...] = (
    validate_nmi_sample_rate,
    validate_sampler_cfg,
    validate_dac_curve_cfg,
    validate_dac_bitmap_tempo_cfg,
    validate_sid_model_cfg,
    validate_dither_cfg,
    validate_color_match_cfg,
    validate_cell_strategy_cfg,
    validate_motion_smoothing_cfg,
    validate_wled_cfg,
)


def validate_scene_cfg(s: SceneCfg, cfg: Config, *, audio_enabled: bool) -> None:
    """Pre-construction validation for a SceneCfg.

    Runs every check that `build_scene` would surface at load time, without
    instantiating a Scene. Safe to call without api/audio/source — used by
    `doctor.validate_load_result` to collect all configuration errors in one
    pass instead of failing fast on the first one.

    Raises ValueError (display-mode parse, required fields, overlay
    compatibility) or OrchestratorError (orchestrate=true with no
    claiming subclass). The constructor-only webcam check (`source is None`)
    lives in `_build_webcam` — doctor mode runs without a source and must
    not be tripped by it.

    Per-type checks live in `_validate_<type>` helpers, each returning the
    display mode the scene will paint (so the shared overlay-compat loop can
    validate against it). Launcher is the exception — it owns the VIC, so it
    self-validates (including its orchestrator) and we return immediately."""
    # Per-scene pixel effect(s): validated up front (before the launcher early
    # return) so it's caught on every type. Only frame-bearing scenes support
    # them. The single `effect` and the `effects` chain are mutually exclusive
    # (one authoring style per scene — see build_scene's chain construction).
    if s.effect is not None and s.effects:
        raise ValueError(
            "set either `effect` (single) or `effects` (chain), not both — got "
            f"effect={s.effect!r} and effects={s.effects!r}"
        )
    effect_layers = s.effects if s.effects else ([s.effect] if s.effect is not None else [])
    if effect_layers:
        for name in effect_layers:
            if name not in _EFFECT_CHOICES:
                raise ValueError(f"effect must be one of {_EFFECT_CHOICES} or unset, got {name!r}")
        if s.type not in _EFFECT_SCENE_TYPES:
            raise ValueError(
                f"effect is not supported on {s.type!r} scenes (they don't render a "
                f"video frame). Supported: {tuple(sorted(_EFFECT_SCENE_TYPES))}."
            )
    if s.mod_source not in _MOD_SOURCE_CHOICES:
        raise ValueError(f"mod_source must be one of {_MOD_SOURCE_CHOICES}, got {s.mod_source!r}")

    # [scenes.color] only means anything on a scene that paints a frame — the
    # same set `effect`/`effects` are scoped to above.
    if s.color and s.type not in _EFFECT_SCENE_TYPES:
        raise ValueError(
            f"color is not supported on {s.type!r} scenes (they don't render a "
            f"video frame). Supported: {tuple(sorted(_EFFECT_SCENE_TYPES))}."
        )

    # start_s is a video-only start offset (the only scene whose source has a
    # seekable timeline). Reject it elsewhere rather than silently ignoring it.
    if s.start_s is not None and s.type != "video":
        raise ValueError(
            f"start_s is only supported on video scenes, not {s.type!r}. "
            "Remove the field (it would be a silent no-op here)."
        )

    # duration_s = 0 is the "run forever" sentinel; negatives are a typo.
    # (Video rejects any duration_s below in _validate_video.)
    if s.duration_s is not None and s.duration_s < 0:
        raise ValueError(f"duration_s must be >= 0 (0 = run forever), got {s.duration_s!r}")

    if s.type == "webcam":
        mode = _display_mode_for_scene(s.display, s, cfg)
    elif s.type == "blank":
        mode = _validate_blank(s, cfg)
    elif s.type == "video":
        mode = _validate_video(s, cfg)
    elif s.type == "waveform":
        mode = _validate_waveform(s, cfg)
    elif s.type == "midi":
        mode = _validate_midi(s)
    elif s.type == "asid":
        mode = _validate_asid(s)
    elif s.type == "slideshow":
        mode = _validate_slideshow(s, cfg)
    elif s.type == "generative":
        mode = _validate_generative(s, cfg)
    elif s.type == "wled":
        mode = _validate_wled(s, cfg)
    elif s.type == "launcher":
        _validate_launcher(s)
        return
    else:
        raise ValueError(
            f"unknown scene type {s.type!r} "
            "(known: webcam, blank, video, waveform, midi, asid, "
            "slideshow, launcher, generative, wled). Note: scrolling_text is now "
            "an overlay — attach it via [[scenes.overlays]]."
        )

    audio_proxy = _AUDIO_SENTINEL if audio_enabled else None
    for ov_cfg in s.overlays:
        ov = build_overlay(ov_cfg, audio_proxy)
        validate_for_scene(ov, mode)

    if s.orchestrate:
        resolve_orchestrator(s)


def _half_system_rate(system: str) -> float:
    """Half the VIC refresh rate (25 PAL / 30 NTSC) — the default frame-push
    cap for a bitmap scene without digitized audio."""
    return 25.0 if system.upper() == "PAL" else 30.0


def _frame_push_default_fps(
    mode: DisplayMode,
    has_digitized_audio: bool,
    system: str,
    *,
    off_bus_audio: bool = False,
    always_fresh: bool = False,
) -> float | None:
    """Default ``target_fps`` for a frame-pushing scene that can stream the
    4-bit ``$D418`` digitized-audio DAC (video / live webcam / generative-mic).

    Bitmap modes (hires/mhires) push a full ~9-10 KB frame every frame; each
    DMA write halts the C64 bus, and when the digitized-audio DAC is *also*
    streaming, the combined halt load tears the picture at the system rate.
    So a bitmap scene streaming digitized audio caps at **20 fps** (both NTSC
    and PAL), and a bitmap scene without it at **half** the system rate
    (30 NTSC / 25 PAL).

    ``always_fresh`` marks a source that renders a NEW frame every tick and so
    has no dedup to fall back on — generative (the generator runs per tick) and
    live webcam (every grab differs), as against ``VideoScene``, which re-pushes
    only on a new source frame. For those, "char modes are cheap" stops holding
    once the DAC is streaming: mcm rewrites screen + color RAM every tick, and
    at the system rate that traffic shares the one DMA socket with the audio
    ring writes and jitters the NMI service. Audibly: an mcm generative scene
    with DAC audio at 60 fps is a noisy mess and is clean at 20 (HW 2026-07-25).
    So an always-fresh scene streaming digitized audio takes the same **20 fps**
    cap in char modes as in bitmap ones. Without the DAC there is nothing to
    protect, and off-bus sampler audio does not contend at all — both keep the
    playlist system default.

    Otherwise char modes (petscii/mcm/blank) are cheap — a ~1 KB delta-cached
    screen — so they keep the playlist system default; this returns ``None``
    for them and the caller leaves ``target_fps`` unset.

    ``off_bus_audio`` is the Ultimate Audio FPGA PCM sampler (see sampler.py):
    audio streams straight from REU with zero SID/``$D418``/NMI/CPU, so it does
    NOT compete with frame uploads for the bus, and its presence forces the
    tear-free REU-staged (bank-swap) video path — whose frame uploads are
    bus-clean REUWRITEs, not CPU-halting host DMA. Both the audio-competition
    cap (20) and the host-DMA tear cap (half-rate) therefore lift, so this
    returns the **system rate** (60 NTSC / 50 PAL) as the poll *ceiling* only.
    Because ``VideoScene`` dedups (it re-pushes only on a new source frame —
    see scenes.py), this ceiling makes the *effective* push rate equal the
    source video's own fps: a 24 fps clip pushes 24/s (every frame, none
    dropped, no wasted re-pushes), a 30 fps clip 30/s, a 60 fps clip 60/s.
    I.e. sampler bitmap video plays at the source rate, capped at the VIC
    refresh — no artificial cap. HW-verified on .64 (audio stayed clean at a
    real 60/s push; see ``reference_ultimate_audio_sampler`` fps A/B). Beats
    ``has_digitized`` when both could apply.

    Worth revisiting the DAC/muted caps once the firmware no longer halts the
    CPU on DMA writes (see ``u64ii_firmware_build`` / ``u64_zero_halt_dma_path``).
    """
    if has_digitized_audio and (mode.is_bitmapped or always_fresh):
        return 20.0
    if not mode.is_bitmapped:
        return None
    if off_bus_audio:
        return 50.0 if system.upper() == "PAL" else 60.0
    return _half_system_rate(system)


@dataclass(frozen=True)
class _SceneBuildContext:
    """Everything ``build_scene`` hands a per-type ``_build_<type>`` helper.

    One frozen bundle instead of threading eight parameters through ten
    builders. ``s`` is the scene being built; the rest is the run-wide
    context (build_scene's docstring says what each field means)."""

    s: SceneCfg
    cfg: Config
    api: C64Backend
    audio: AudioStreamer | None
    source: WebcamSource | None
    is_ensemble: bool
    reu_available: bool
    sampler_available: bool

    @property
    def backend_supports_reu(self) -> bool:
        """Whether THIS backend has an REU at all (capability, not "REU
        enabled" — that's ``reu_available``). Resolves the
        [video].double_buffer "auto" host-DMA page-flip path on no-REU
        backends (the TeensyROM). See resolve_double_buffer."""
        return self.api.profile.supports_reu

    @property
    def color(self) -> ColorCfg:
        """The effective [color] section for ``s`` — the global section with
        ``s.color``'s authored overrides applied. What every builder passes
        to its Scene/DisplayMode constructors instead of ``cfg.color``."""
        return scene_color(self.cfg, self.s)

    def display_mode(self, display: str | None) -> DisplayMode:
        """The scene's display mode for ``display``, with the probe verdicts
        threaded through — the call every frame-bearing builder makes."""
        return _display_mode_for_scene(
            display,
            self.s,
            self.cfg,
            reu_available=self.reu_available,
            backend_supports_reu=self.backend_supports_reu,
        )


def _resolve_live_audio(ctx: _SceneBuildContext, name: str, label: str) -> AudioStreamer | None:
    """The DAC streamer a live-input scene (webcam / blank / generative mic)
    should carry, or None for silence.

    Default: follow global [audio].enabled. When ``ctx.audio`` is None the
    streamer wasn't constructed (global is off) so the scene runs silent;
    when it's a real streamer, the scene picks it up. Set ``audio = false``
    per-scene to opt out even when the global is on. In ensemble mode live
    scenes never hold the audio spotlight, so audio is always suppressed —
    with a log line when the scene explicitly opted in."""
    scene_audio = None if ctx.s.audio is False else ctx.audio
    if ctx.is_ensemble and scene_audio is not None:
        if ctx.s.audio is True:
            log.info(
                "[%s] %s: audio suppressed in ensemble mode "
                "(live scenes never hold the audio spotlight)",
                name,
                label,
            )
        scene_audio = None
    return scene_audio


def _resolve_sampler_audio(ctx: _SceneBuildContext) -> UltimateAudioSampler | None:
    """A per-scene Ultimate Audio sampler when [audio].backend resolves to
    it, else None (the caller keeps the shared 4-bit DAC streamer).

    On a sampler-capable U64 with the Ultimate Audio sampler available, video
    and generative-file scenes swap the shared ``$D418`` DAC for a per-scene
    UltimateAudioSampler (high fidelity, off the C64 bus — see sampler.py).
    It satisfies the same scene-facing audio contract (sample_rate /
    position_seconds / push_samples / stop), so scenes drive it
    polymorphically; mic/webcam scenes keep the shared DAC."""
    backend = resolve_audio_backend(
        ctx.cfg.audio.backend,
        supports_sampler=ctx.api.profile.supports_sampler,
        sampler_available=ctx.sampler_available,
    )
    if backend != "sampler":
        return None
    return UltimateAudioSampler(
        ctx.api,
        sample_rate=ctx.cfg.audio.sampler_sample_rate,
        bits=ctx.cfg.audio.sampler_bits,
        ref_clock_hz=ctx.cfg.audio.sampler_clock_hz,
    )


def _video_tempo_scale(cfg: Config, mode: DisplayMode, *, dac_audio: bool) -> float:
    """Bitmap + ``$D418``-DAC tempo compensation factor (1.0 = none).

    On the host-DMA 4-bit DAC path over a bitmap mode, heavy REU bank-swap
    bitmap writes bias the audio servo and time-stretch playback ~1/s SLOW at
    correct pitch. Pre-compress the content by 1/s (audio time-compress +
    video PTS × s) so it nets to real time. Gated OFF (1.0) for the off-bus
    sampler, the REU pump, char modes, and muted scenes — none of which
    stretch (``dac_audio`` False covers sampler and muted)."""
    if not dac_audio or cfg.audio.use_reu_pump or not isinstance(mode, BitmapDisplayMode):
        return 1.0
    if isinstance(mode, MultiHiresDisplayMode):
        return cfg.audio.dac_bitmap_tempo_mhires
    return cfg.audio.dac_bitmap_tempo_hires


def _resolve_video_source(
    s: SceneCfg,
) -> tuple[str, float | None, str | None, ResolvedMedia | None]:
    """The video scene's (file_spec, start_s, name, resolved) after URL
    resolution. `resolved` is the full ResolvedMedia (None for a local file) —
    the caller stashes its uploader/license/webpage_url onto the scene for
    recording_metadata to read.

    A single media URL (YouTube et al.) is resolved here — the ONE resolution
    path shared with quick playback — so config-driven videos accept URLs
    too. Its t=/start= timestamp folds into start_s (an explicit start_s
    wins), and the resolved title becomes the scene name (an explicit name
    wins). Local files / dir / glob / multi specs are untouched."""
    assert s.file is not None  # narrowed by validate_scene_cfg
    file_spec = s.file
    start_s = s.start_s
    name = s.name
    resolved = None
    if _is_single_url_spec(s.file):
        # Deferred: cycle with quickcast (see _validate_video).
        from .quickcast import resolve_video_url

        resolved = resolve_video_url(s.file.strip())
        file_spec = resolved.stream_url
        if start_s is None:
            start_s = resolved.start_s
        if name is None:
            name = resolved.title
    return file_spec, start_s, name, resolved


def _build_webcam(ctx: _SceneBuildContext) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    if ctx.source is None:
        raise ValueError(
            "webcam scene declared but no WebcamSource was provided — "
            "this should have been caught at cli.py startup"
        )
    display = resolve_scene_display(s.display, s.type)
    mode = ctx.display_mode(display)
    name = s.name or f"Webcam {display}"
    scene_audio = _resolve_live_audio(ctx, name, "live webcam scene")
    scene = WebcamScene(ctx.api, scene_audio, mode, ctx.source, cfg.audio, name, color=ctx.color)
    if s.target_fps is None:
        # always_fresh: every camera grab differs, so there is no dedup —
        # a char mode still repaints the whole screen each tick and, with
        # mic audio on the DAC, contends with the ring writes.
        fps = _frame_push_default_fps(
            mode,
            scene_audio is not None,
            cfg.ultimate64.system,
            always_fresh=True,
        )
        if fps is not None:
            scene.target_fps = fps
    return scene


def _build_blank(ctx: _SceneBuildContext) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    mode = _build_display_mode(
        "blank",
        border=s.border,
        background=s.background,
        use_reu_staged=resolve_use_reu_staged(
            cfg.video.use_reu_staged, "blank", reu_available=ctx.reu_available
        ),
    )
    name = s.name or "Blank"
    scene_audio = _resolve_live_audio(ctx, name, "live blank scene")
    return BlankScene(ctx.api, scene_audio, mode, cfg.audio, name)


def _build_video(ctx: _SceneBuildContext) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    mode = ctx.display_mode(s.display)
    # Default: audio ON for videos (it's part of the file). The user can mute
    # one with `audio = false`. Widened because it may hold the per-scene
    # off-bus sampler instead of the shared DAC streamer (see
    # _resolve_sampler_audio).
    video_audio: AudioStreamer | UltimateAudioSampler | None = (
        None if s.audio is False else ctx.audio
    )
    using_sampler = False
    if video_audio is not None:
        sampler = _resolve_sampler_audio(ctx)
        if sampler is not None:
            video_audio = sampler
            using_sampler = True
    # `using_sampler` False with audio present means the DAC path.
    has_dac_audio = video_audio is not None and not using_sampler
    file_spec, start_s, video_name, resolved = _resolve_video_source(s)
    scene = VideoScene(
        ctx.api,
        video_audio,
        mode,
        file_spec,
        prepend_alignment_marker=(cfg.audio.source_alignment_marker and cfg.audio.use_reu_pump),
        color=ctx.color,
        start_s=start_s or 0.0,
        tempo_scale=_video_tempo_scale(cfg, mode, dac_audio=has_dac_audio),
        loop_audio=cfg.midi_control.loop_audio,
        setup_progress=cfg.video.setup_progress_bar,
    )
    if video_name:
        scene.name = video_name
    # Stashed for recording_metadata._video_source — never read by playback
    # itself, only by the SCENE_CONFIG_JSON snapshot at scene start.
    scene.source_info = resolved
    if s.target_fps is None:
        # The sampler plays entirely off the C64 bus, so it neither imposes
        # the 4-bit DAC's bitmap fps cap (the DAC's NMI + ring DMAWRITEs
        # compete with frame uploads for the bus) nor the muted half-rate
        # cap (its REU-staged frame uploads are bus-clean, not host DMA).
        # So sampler bitmap video uncaps to the system rate (60/50) — and
        # because VideoScene dedups, the effective push rate then equals the
        # source video's fps (24fps clip → 24/s, etc.). DAC video stays 20;
        # muted bitmap stays 30/25. See _frame_push_default_fps.
        fps = _frame_push_default_fps(
            mode, has_dac_audio, cfg.ultimate64.system, off_bus_audio=using_sampler
        )
        if fps is not None:
            scene.target_fps = fps
    return scene


def _resolve_sid_play_rate(cfg: Config) -> str | float | None:
    """`[ultimate64].sid_play_rate`, forced off when something else in the run
    owns CIA #1 Timer A.

    The REU audio pump reprograms Timer A to its own matched rate
    (audio.py's `_arm_reu_pump`). Retuning the same timer for the SID's PLAY
    rate would have the two overwrite each other, so the pump — which is
    load-bearing for audio continuity — wins and the tempo correction is
    dropped with a note rather than left to fight."""
    if cfg.ultimate64.sid_play_rate in (None, "off"):
        return None
    if cfg.audio.use_reu_pump:
        log.info(
            "[ultimate64].sid_play_rate is ignored while [audio].use_reu_pump "
            "is on — the REU pump owns CIA #1 Timer A. Vsync tunes play at the "
            "kernal jiffy rate (PAL tunes ~20%% fast)."
        )
        return None
    return cfg.ultimate64.sid_play_rate


def _build_waveform(ctx: _SceneBuildContext) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    # If duration_s is unset AND a songlengths DB is configured, let the
    # WaveformScene look up the true length. Explicit duration_s wins over
    # the DB.
    db = _load_songlengths(cfg.playlist.songlengths_file)
    assert s.file is not None  # narrowed by validate_scene_cfg
    scene = WaveformScene(
        ctx.api,
        ctx.audio,
        file=s.file,
        song=s.song,
        duration_s=s.duration_s,
        target_fps=s.target_fps,
        system=cfg.ultimate64.system,
        color_mode=s.color_mode,
        voice_colors=s.voice_colors or None,
        waveform_colors=s.waveform_colors or None,
        time_base=s.time_base,
        auto_cycles=s.auto_cycles,
        persistence=s.persistence,
        scroll_columns=s.scroll_columns,
        songlengths_db=db,
        sid_model=resolve_sid_model_cfg(cfg),
        sid_panning=cfg.ultimate64.sid_panning,
        sid_volume=cfg.ultimate64.sid_volume,
        sid_play_rate=_resolve_sid_play_rate(cfg),
    )
    if s.name:
        scene.name = s.name
    return scene


def _build_slideshow(ctx: _SceneBuildContext) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    display = _resolve_slideshow_display(s.display)
    mode = ctx.display_mode(display)
    assert s.file is not None  # narrowed by validate_scene_cfg
    # Pass the *original* display spec (may be "random") so the scene can
    # re-resolve at each setup() for fresh variety in single-scene loops. The
    # build kwargs travel along so the scene can rebuild without re-plumbing
    # through `scene._cfg`. The REU staging setting is handed over as the raw
    # tri-state + the probe verdict (not the resolved bool), so a
    # `display = "random"` rebuild re-decides staging per concrete mode each
    # setup().
    return SlideshowScene(
        ctx.api,
        mode,
        s.file,
        image_duration_s=s.image_duration_s,
        display_spec=s.display,
        palette_mode=s.palette_mode,
        border=s.border,
        background=s.background,
        style=s.style,
        use_reu_staged=cfg.video.use_reu_staged,
        double_buffer=cfg.video.double_buffer,
        reu_available=ctx.reu_available,
        backend_supports_reu=ctx.backend_supports_reu,
        audio_reu_pump_active=cfg.audio.use_reu_pump,
        color=ctx.color,
        text_double_height=s.text_double_height,
        aspect_mode=s.aspect_mode,
    )


def _build_generative(ctx: _SceneBuildContext) -> Scene:
    # The three arms differ in how audio reaches the C64: "sid" plays through
    # the real SID chip via the player IRQ, "listen" analyzes live input with
    # no C64 output, and mic/file/none ride the live DAC-or-sampler path.
    s = ctx.s
    gen = build_generator(s.source)
    name = s.name or f"Generative {s.source}"
    if s.audio_source == "sid":
        return _build_generative_sid(ctx, gen, name)
    if s.audio_source == "listen":
        return _build_generative_listen(ctx, gen, name)
    return _build_generative_live(ctx, gen, name)


def _build_generative_sid(ctx: _SceneBuildContext, gen: GenerativeSource, name: str) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    # Force host-DMA: the SID player owns the $0314 IRQ for PLAY, so the
    # display must NOT install the REU bank-swap raster IRQ (it would
    # collide). The SID drives the chip directly — no DAC streamer, plays
    # regardless of [audio].enabled, and is NOT subject to the ensemble
    # live-mic suppression (it legitimately holds the audio spotlight;
    # wants_audio_lock=True gates the slot). The scene's base audio stays
    # None.
    mode = _display_mode_for_scene(s.display, s, cfg, force_host_dma=True)
    assert s.file is not None  # narrowed by _validate_generative
    audio_src = SidFileAudioSource(
        ctx.api,
        s.file,
        song=s.song,
        display_mode=mode,
        system=cfg.ultimate64.system,
        reactive=s.reactive,
        sid_model=resolve_sid_model_cfg(cfg),
        sid_panning=cfg.ultimate64.sid_panning,
        sid_volume=cfg.ultimate64.sid_volume,
        sid_play_rate=_resolve_sid_play_rate(cfg),
    )
    scene = SourceScene(ctx.api, None, mode, gen, audio_src, name, color=ctx.color)
    # Bitmap displays push a full ~9-10 KB frame via host DMAWRITE; at full
    # system rate that competes with the SID player's per-frame PLAY IRQ for
    # the bus. Default such scenes to half-rate (like WaveformScene) for
    # safety; a char display stays full-rate, and an explicit target_fps
    # (applied in build_scene's epilogue) still wins.
    if s.target_fps is None and mode.is_bitmapped:
        scene.target_fps = _half_system_rate(cfg.ultimate64.system)
    return scene


def _build_generative_listen(ctx: _SceneBuildContext, gen: GenerativeSource, name: str) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    mode = ctx.display_mode(s.display)
    # Listen-only: analyze the live input for reactive visuals with NO C64
    # audio output. It drives neither the DAC nor the SID, so it is never
    # ensemble-suppressed and ignores the per-scene `audio` DAC toggle — it
    # just needs the shared streamer to own the input + analysis sink. The
    # SourceScene gets no DAC audio (None): the analyzer taps pre-DSP, so
    # per-scene pre-emphasis is irrelevant, and there is no DAC stream to
    # frame-cap against.
    audio_src: AudioSource
    if ctx.audio is not None and s.reactive:
        audio_src = MicAudioSource(
            ctx.audio,
            cfg.audio,
            display_mode=mode,
            reactive=True,
            listen_only=True,
            features_cfg=cfg.audio_features,
        )
    else:
        # No streamer ([audio] off) or reactive = false → silence.
        audio_src = NullAudioSource()
    return SourceScene(ctx.api, None, mode, gen, audio_src, name, color=ctx.color)


def _build_generative_live(ctx: _SceneBuildContext, gen: GenerativeSource, name: str) -> Scene:
    # mic / file / none: the live-frame audio path. Like webcam/blank, a live
    # mic source is suppressed in ensemble mode.
    s, cfg = ctx.s, ctx.cfg
    mode = ctx.display_mode(s.display)
    scene_audio = _resolve_live_audio(ctx, name, "generative scene")
    audio_src: AudioSource
    file_audio_src: AudioFileSource | None = None
    # The audio object the SourceScene carries as its base `.audio` (the
    # set_pre_emphasis hook + overlay sample tap). Defaults to the shared
    # 4-bit DAC streamer; the file path may swap it for a per-scene off-bus
    # sampler (below), so it must widen to either.
    scene_base_audio: AudioStreamer | UltimateAudioSampler | None = scene_audio
    # True when the file path decodes into the Ultimate Audio sampler rather
    # than the $D418 DAC — lifts the DAC bitmap fps caps.
    file_uses_sampler = False
    if s.audio_source == "mic" and scene_audio is not None:
        audio_src = MicAudioSource(
            scene_audio,
            cfg.audio,
            display_mode=mode,
            reactive=s.reactive,
            features_cfg=cfg.audio_features,
        )
    elif s.audio_source == "file" and scene_audio is not None:
        # Decode a music file to the C64's audio AND analyze it — the same
        # analyzer the mic path uses, sourced from a file. The backend
        # resolves exactly like a video scene: on a sampler-capable U64 with
        # the Ultimate Audio sampler available (backend = auto/sampler),
        # decode into the off-bus 16-bit sampler instead of the 4-bit $D418
        # DAC. The DAC path is intrinsically staticky here — its NMI service
        # is jittered by every host-DMA RAM write and it quantizes to ~6-7
        # bits — so a decoded track is barely recognizable regardless of
        # display mode (HW-measured 2026-07-24); the sampler is immune (no
        # $D418/NMI/CPU). Falls back to the DAC on TeensyROM, when the
        # sampler is unavailable, or backend = "dac".
        assert s.file is not None  # narrowed by _validate_generative
        file_audio_obj: AudioStreamer | UltimateAudioSampler = scene_audio
        sampler = _resolve_sampler_audio(ctx)
        if sampler is not None:
            file_audio_obj = sampler
            file_uses_sampler = True
        scene_base_audio = file_audio_obj
        file_audio_src = AudioFileSource(
            file_audio_obj,
            s.file,
            reactive=s.reactive,
            features_cfg=cfg.audio_features,
        )
        audio_src = file_audio_src
    else:
        # "none", or "mic"/"file" with audio disabled → silence.
        audio_src = NullAudioSource()
    scene = SourceScene(ctx.api, scene_base_audio, mode, gen, audio_src, name, color=ctx.color)
    # Size a file-audio scene to the track so `c64cast tune.mp3` plays the
    # whole song then advances/loops (an explicit duration_s still wins,
    # applied in build_scene's duration-resolution epilogue).
    if file_audio_src is not None and s.duration_s is None and file_audio_src.duration_s:
        scene.duration_s = file_audio_src.duration_s
    # A mic/file-source generative scene is digitized-audio-capable like
    # webcam/video, so a bitmap display caps its frame push: 20 fps while the
    # 4-bit DAC streams (its NMI + ring DMAWRITEs compete with frame
    # uploads), half the system rate (30/25) otherwise. The off-bus sampler
    # frees the *audio* from the bus, but NOT the video: a generative source
    # renders a fresh frame every tick (no VideoScene-style dedup), so
    # uncapping to the system rate would push 60 real mhires frames/s of REU
    # bank-swap traffic — which starves the sampler's own REU writes (audible
    # static) and overloads the bus (C64-side visual crash, HW 2026-07-24).
    # So a sampler-routed file scene keeps the muted 30/25 bitmap cap:
    # off-bus audio, no on-bus digi, no uncap. The "none" source drives no
    # audio, so it keeps the playlist default.
    #
    # That same no-dedup property (always_fresh) is why the DAC's 20 fps cap
    # now applies in CHAR modes too, not just bitmap ones — a 60 fps mcm
    # generative scene repaints screen + color RAM every tick over the socket
    # the audio ring shares. Sampler-routed audio is off-bus and keeps the
    # high default.
    if s.target_fps is None and s.audio_source in ("mic", "file"):
        fps = _frame_push_default_fps(
            mode,
            scene_audio is not None and not file_uses_sampler,
            cfg.ultimate64.system,
            always_fresh=True,
        )
        if fps is not None:
            scene.target_fps = fps
    return scene


def _build_wled(ctx: _SceneBuildContext) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    # A network pixel sink: the frame arrives over UDP, no audio, no SID.
    # It's just another FrameSource behind the SourceScene seam — the display
    # mode quantizes the received BGR frame to the C64 unchanged.
    mode = ctx.display_mode(s.display)
    wled_source = WLEDSource(
        s.sink_width,
        s.sink_height,
        ddp_port=s.sink_ddp_port,
        wled_port=s.sink_wled_port,
        sender_allowlist=frozenset(s.sink_allow) if s.sink_allow else None,
    )
    name = s.name or "WLED sink"
    scene = SourceScene(ctx.api, None, mode, wled_source, NullAudioSource(), name, color=ctx.color)
    # Bitmap displays push a full ~9-10 KB frame per update; default to half
    # rate like the other frame scenes (an explicit target_fps, applied in
    # build_scene's epilogue, still wins).
    if s.target_fps is None and mode.is_bitmapped:
        scene.target_fps = _half_system_rate(cfg.ultimate64.system)
    return scene


def _build_launcher(ctx: _SceneBuildContext) -> Scene:
    s = ctx.s
    assert s.file is not None  # narrowed by validate_scene_cfg
    # No audio streamer: the launched program drives the real SID directly.
    # No display mode / overlays: it owns the VIC.
    return LauncherScene(
        ctx.api,
        s.file,
        input_source=s.input_source,
        reset_before_launch=s.reset_before_launch,
        min_duration_s=s.min_duration_s,
        max_duration_s=(math.inf if s.max_duration_s is None else s.max_duration_s),
        bypass_audio_lock=s.bypass_audio_lock,
        name=s.name,
    )


def _build_midi(ctx: _SceneBuildContext) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    a, d, sus, r = s.midi_adsr
    return MidiScene(
        ctx.api,
        ctx.audio,
        port=s.midi_port,
        waveform=s.midi_waveform,
        voice_waveforms=s.midi_voice_waveforms or None,
        voice_mode=s.midi_voice_mode,
        voice_channels=s.midi_voice_channels or None,
        program_change=s.midi_program_change,
        adsr=(a, d, sus, r),
        pulse_width=s.midi_pulse_width,
        filter_cutoff=s.midi_filter_cutoff,
        filter_resonance=s.midi_filter_resonance,
        filter_mode=s.midi_filter_mode,
        master_volume=s.midi_master_volume,
        voice_colors=s.voice_colors or None,
        color_mode=s.color_mode,
        waveform_colors=s.waveform_colors or None,
        time_base=s.time_base,
        auto_cycles=s.auto_cycles,
        persistence=s.persistence,
        scroll_columns=s.scroll_columns,
        target_fps=s.target_fps,
        system=cfg.ultimate64.system,
        name=s.name or "MIDI",
    )


def _build_asid(ctx: _SceneBuildContext) -> Scene:
    s, cfg = ctx.s, ctx.cfg
    return AsidScene(
        ctx.api,
        ctx.audio,
        port=s.asid_port,
        voice_colors=s.voice_colors or None,
        color_mode=s.color_mode,
        waveform_colors=s.waveform_colors or None,
        time_base=s.time_base,
        auto_cycles=s.auto_cycles,
        persistence=s.persistence,
        scroll_columns=s.scroll_columns,
        target_fps=s.target_fps,
        system=cfg.ultimate64.system,
        multi_sid=s.asid_multi_sid,
        max_sids=s.asid_max_sids,
        buffered_player=s.asid_buffered_player,
        sid_panning=cfg.ultimate64.sid_panning,
        sid_volume=cfg.ultimate64.sid_volume,
        name=s.name or "ASID",
    )


# One builder per scene type, mirroring the _validate_<type> helpers that
# validate_scene_cfg fans out to. validate_scene_cfg (always called first in
# build_scene) rejects unknown types, so the lookup can't miss. Keep in sync
# with config.SCENE_TYPES — tests/test_config.py holds the two to the same
# set.
_BUILDERS: dict[str, Callable[[_SceneBuildContext], Scene]] = {
    "webcam": _build_webcam,
    "blank": _build_blank,
    "video": _build_video,
    "waveform": _build_waveform,
    "midi": _build_midi,
    "asid": _build_asid,
    "slideshow": _build_slideshow,
    "launcher": _build_launcher,
    "generative": _build_generative,
    "wled": _build_wled,
}


def build_scene(
    s: SceneCfg,
    cfg: Config,
    api: C64Backend,
    audio: AudioStreamer | None,
    source: WebcamSource | None,
    *,
    is_ensemble: bool = False,
    reu_available: bool = False,
    sampler_available: bool = False,
) -> Scene:
    """Build a single Scene from a SceneCfg.

    Extracted from `scenes_from_config` so the playlist's broadcast
    interrupt machinery (see Playlist.EnsembleCoordinator.handle_broadcast_interrupt) can
    spin up follower scenes one at a time without re-iterating cfg.scenes.

    Needs the surrounding `Config` for context fields (ultimate64.system
    for SID timing, audio for streamer defaults, playlist.songlengths_file
    for waveform durations) that aren't on SceneCfg itself.

    `is_ensemble=True` forces live-scene (webcam, blank) audio off so the
    mic capture can't compete with the one system holding the ensemble
    audio lock for that scheduling window. Audio-bearing scene types
    (video, waveform, midi) still receive the streamer — the lock
    arbitrates which one actually drives the SID at any moment.

    `reu_available` is the startup probe's verdict on whether the U64's REU
    is enabled; it resolves the [video].use_reu_staged "auto" setting (see
    resolve_use_reu_staged). Callers that build scenes without a live probe
    (validation, doctor) leave it False so auto degrades to host-DMA.

    `sampler_available` is the probe's verdict on whether the U64's Ultimate
    Audio sampler is exposed + routed; it resolves [audio].backend for video
    scenes (see resolve_audio_backend). False without a probe → DAC.

    Per-type construction lives in the `_build_<type>` helpers dispatched
    through `_BUILDERS`, mirroring the `_validate_<type>` split; the epilogue
    below applies what every scene gets — duration resolution, an explicit
    target_fps, the effect chain, overlays, and the debug/OSD/pre-emphasis
    stamps."""
    validate_scene_cfg(s, cfg, audio_enabled=audio is not None)

    ctx = _SceneBuildContext(
        s=s,
        cfg=cfg,
        api=api,
        audio=audio,
        source=source,
        is_ensemble=is_ensemble,
        reu_available=reu_available,
        sampler_available=sampler_available,
    )
    scene = _BUILDERS[s.type](ctx)

    # Duration resolution. `scene.duration_s = math.inf` means "run until
    # stopped" (the scene never auto-advances).
    #   * explicit duration_s == 0 → the "run forever" sentinel (any type);
    #   * explicit duration_s  > 0 → honored verbatim;
    #   * unset (None): webcam/blank default to infinite in a SINGLE-scene
    #     playlist ("leave the camera running"), but keep the base 30 s in a
    #     multi-scene playlist so the rotation still advances — an infinite
    #     live scene never becomes is_done and would wedge the playlist. Every
    #     other type keeps the default already set above (video's video-driven
    #     math.inf, waveform's song-length, etc.).
    # Video scenes set their own math.inf in __init__ and reject explicit
    # duration_s in _validate_video, so leave them untouched here.
    if s.type != "video":
        # A single configured scene stays single-scene: interleave_videos is
        # skipped for a 1-scene playlist (see scenes_from_config), so the
        # scene count is the whole story here.
        single_scene_playlist = len(cfg.scenes) <= 1
        if s.duration_s is not None:
            scene.duration_s = math.inf if s.duration_s == 0 else s.duration_s
        elif s.type in ("webcam", "blank") and single_scene_playlist:
            scene.duration_s = math.inf
    if s.target_fps is not None:
        scene.target_fps = float(s.target_fps)
    # Per-scene pixel effect chain (validated frame-bearing + mutually-exclusive
    # in validate_scene_cfg). Applied in order in scenes._render_with_overlays.
    # `effects` (the chain) wins if set; otherwise the legacy single `effect`
    # becomes a one-layer chain. Each layer inherits the scene's mod_source so a
    # clock/audio choice drives the whole stack uniformly.
    effect_names = s.effects if s.effects else ([s.effect] if s.effect is not None else [])
    if effect_names:
        chain = []
        for name in effect_names:
            eff = build_effect(name)
            eff.mod_source = s.mod_source
            chain.append(eff)
        scene.effects = chain
    _attach_overlays(scene, s.overlays, audio)
    # Debug aid: source-bearing scenes draw the playback timecode + frame
    # number into each frame (pre-quantization). Harmless no-op on scenes
    # without a video frame (waveform/launcher/midi ignore the flag).
    scene.show_frame_numbers = cfg.debug.frame_numbers
    # Live-tune OSD placement ([midi_control].osd): "top"/"bottom" position, or
    # "off" to disable. Stamped here (like show_frame_numbers) so every built
    # scene honors the setting; the OsdState stays invisible until a live-tune
    # control posts to it.
    scene.osd.enabled = cfg.midi_control.osd != "off"
    scene.osd.position = (
        cfg.midi_control.osd if cfg.midi_control.osd in ("top", "bottom") else "bottom"
    )
    # Per-scene pre-emphasis cascade: explicit scene value wins; otherwise fall
    # back to the global [dsp].pre_emphasis (which may itself be None = source-
    # aware auto). The audio-bearing scenes apply this to the shared streamer at
    # setup() via audio.set_pre_emphasis; other scene types ignore it.
    scene.pre_emphasis = s.pre_emphasis if s.pre_emphasis is not None else cfg.dsp.pre_emphasis
    # Stamp the source SceneCfg on the instance so the playlist's
    # orchestrator wiring (and overlays that need access to the
    # declarative cfg) can find it without re-iterating cfg.scenes.
    scene._cfg = s
    return scene


def scenes_from_config(
    cfg: Config,
    api: C64Backend,
    audio: AudioStreamer | None,
    source: WebcamSource | None,
    *,
    is_ensemble: bool = False,
    reu_available: bool = False,
    sampler_available: bool = False,
) -> list[Scene]:
    """Build the playlist scene list from cfg.scenes.

    Interleaves videos between scenes when ``cfg.playlist.interleave_videos``
    is true and the videos directory contains video files (and PyAV is available).

    Scenes marked `follower_only = true` are skipped here — they exist only
    to be picked up as follower overrides during a cross-system broadcast
    (via `Orchestrator.follower_scene_cfg_for`, which reads `cfg.scenes`
    directly and still finds them by name).

    `is_ensemble` propagates to `build_scene` so live scenes (webcam,
    blank) are forced silent under ensemble coordination — see
    `build_scene` for the rationale.

    `reu_available` propagates to `build_scene` to resolve the
    [video].use_reu_staged "auto" setting (see resolve_use_reu_staged).

    `sampler_available` propagates to `build_scene` to resolve the
    [audio].backend selector for video scenes (see resolve_audio_backend)."""
    # Validate follower-only scenes here too — they're built lazily at
    # broadcast time via `build_follower_scene`, so without this call a
    # bad cfg would only surface mid-broadcast. (build_scene below runs
    # validate_scene_cfg internally for the scenes that DO build now.)
    for s in cfg.scenes:
        if s.follower_only:
            validate_scene_cfg(s, cfg, audio_enabled=audio is not None)

    base: list[Scene] = []
    for index, s in enumerate(cfg.scenes):
        if s.follower_only:
            continue
        scene = build_scene(
            s,
            cfg,
            api,
            audio,
            source,
            is_ensemble=is_ensemble,
            reu_available=reu_available,
            sampler_available=sampler_available,
        )
        # The scene's address in the *file*, so a live-tune save-back can write a
        # per-scene knob (palette_mode) into the block it came from. Counted over
        # cfg.scenes rather than over this list, so a follower-only scene earlier
        # in the file doesn't shift everything after it — and stamped only here,
        # which leaves it None on every scene the config did not name.
        scene.cfg_index = index
        base.append(scene)

    if not base:
        # Sensible default if user gave us no scenes at all. No audio —
        # live video defaults to silent so it can run at full speed.
        if source is None:
            raise ValueError(
                "no scenes configured and no WebcamSource available — "
                "configure at least one scene or attach a webcam"
            )
        base.append(
            WebcamScene(
                api, None, HiresDisplayMode(style="edges"), source, cfg.audio, "Live Hi-Res Edges"
            )
        )
        # The sole scene when nothing is configured — leave it running.
        base[-1].duration_s = math.inf

    if not cfg.playlist.interleave_videos:
        return base
    if len(base) <= 1:
        # Single-scene playlists run in Playlist's single-scene mode (no
        # interstitials, loop forever). Interleaving a video would silently
        # promote it to a 2-scene multi-scene playlist — surprising. Skip.
        if _gather_videos(cfg.playlist.videos_dir):
            log.info(
                "interleave_videos skipped: single-scene playlist "
                "(loops the one scene; no place to insert videos)"
            )
        return base

    video_files = _gather_videos(cfg.playlist.videos_dir)
    if not video_files:
        return base
    if not ensure_pyav():
        log.warning(
            "Found %d video files but PyAV is not installed; skipping videos.", len(video_files)
        )
        return base

    interleaved: list[Scene] = []
    video_idx = 0
    for built in base:
        interleaved.append(built)
        if not isinstance(built, VideoScene):
            vid_mode = HiresDisplayMode(style="edges")
            vid_scene = VideoScene(
                api,
                audio,
                vid_mode,
                video_files[video_idx],
                prepend_alignment_marker=(
                    cfg.audio.source_alignment_marker and cfg.audio.use_reu_pump
                ),
                setup_progress=cfg.video.setup_progress_bar,
            )
            # These are built directly (not via build_scene), so apply the
            # same bitmap frame-push cap: 20 fps with audio (this hires_edges
            # video streams the digitized DAC), half rate when muted.
            fps = _frame_push_default_fps(vid_mode, audio is not None, cfg.ultimate64.system)
            if fps is not None:
                vid_scene.target_fps = fps
            interleaved.append(vid_scene)
            video_idx = (video_idx + 1) % len(video_files)
    return interleaved


# The media-extension tuples live with the scene classes (scenes.scenes,
# whose MediaFileMixin subclasses carry them as MEDIA_EXTS) and are
# re-exported here — quickcast, wizard, and the CLI import them from this
# module, the app layer's scene surface.
VIDEO_EXTS = _scenes.VIDEO_EXTS
SID_EXTS = _scenes.SID_EXTS
PICTURE_EXTS = _scenes.PICTURE_EXTS
PROGRAM_EXTS = _scenes.PROGRAM_EXTS
AUDIO_EXTS = _scenes.AUDIO_EXTS

# Default `file =` value for scenes that don't set one. The scene picks a
# random file from the directory at each setup() (same as an explicit
# directory spec). Missing/empty default dirs surface as a clear
# validate-time error pointing the user at the dir to populate or the
# `file =` field to override.
DEFAULT_VIDEO_DIR = "assets/videos"
DEFAULT_WAVEFORM_DIR = "assets/sids"
DEFAULT_SLIDESHOW_DIR = "assets/pictures"
DEFAULT_PROGRAM_DIR = "assets/programs"

# Display modes the slideshow can pick from when `display = "random"`. Blank
# is excluded (no video source); bitmap + char modes all accept a BGR frame.
SLIDESHOW_RANDOM_DISPLAYS = ("mhires", "hires", "hires_edges", "mcm", "petscii")


def _resolve_slideshow_display(spec: str | None) -> str:
    """Resolve a slideshow scene's `display` config value:

    * Unset (`None`) or the explicit value `"hires_edges"` (tuned for live
      webcam Canny-edge stylization, not stills) resolves to `"mhires"` —
      stills benefit most from per-cell color picking. Users wanting plain
      bitmap output can set `display = "hires"` explicitly.
    * `"random"` picks one of `SLIDESHOW_RANDOM_DISPLAYS` at random; this
      runs at every setup() so single-scene loops get fresh variety.
    * Any other value passes through unchanged.
    """
    if spec is None or spec == "hires_edges":
        return "mhires"
    if spec == "random":
        return random.choice(SLIDESHOW_RANDOM_DISPLAYS)
    return spec


def _gather_videos(directory: str) -> list[str]:
    directory = paths.expand_user(directory)
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, f) for f in os.listdir(directory) if f.lower().endswith(VIDEO_EXTS)
    )


_GLOB_CHARS = re.compile(r"[*?\[]")


def missing_media(spec: str) -> list[str]:
    """The entries of a `file =` spec that name a local path which isn't there.

    :func:`resolve_file_spec` lets a literal path through unchecked on purpose
    — media can appear between load and playback, and a spec may legitimately
    name a file on another machine in an ensemble — so nothing notices until
    the scene builds, seconds into a run with the link open and the C64 already
    reset. A caller that would rather say so first asks here and *warns*;
    turning this into a refusal would break the two cases the pass-through
    exists for.

    URLs are skipped (not local), and so are globs and empty entries, which
    ``resolve_file_spec`` already fails loudly on."""
    out: list[str] = []
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry or entry.lower().startswith(("http://", "https://")):
            continue
        expanded = paths.expand_user(entry)
        if _GLOB_CHARS.search(expanded):
            continue
        if not os.path.exists(expanded):
            out.append(entry)
    return out


def resolve_file_spec(spec: str, extensions: tuple[str, ...], *, label: str) -> list[str]:
    """Resolve a comma-separated `file =` spec to a sorted, unique list of
    concrete file paths.

    Each comma-separated entry is one of:
      * a literal file path — included as-is (extension-checked).
      * a directory path — every file inside whose extension is in
        `extensions` is included (non-recursive; mirrors `_gather_videos`).
        Exception: for the waveform scene (`label="waveform"`), the default
        SID directory (`DEFAULT_WAVEFORM_DIR`, `assets/sids`) is walked
        recursively instead — an unpacked HVSC archive is a deep tree, and
        this is the directory the waveform scene falls back to when `file`
        is unset, so it should work out of the box. Any other directory
        (or a non-default entry mixed into the same spec) stays shallow;
        write `**/*.sid` explicitly for recursion elsewhere.
      * a glob pattern (containing `*`, `?`, or `[`) — expanded via
        `glob.glob`; matches whose extension is in `extensions` are kept.
        A `**` segment recurses into subdirectories (e.g.
        `assets/sids/**/*.sid` finds a whole HVSC tree), matching zero or
        more directory levels.

    Whitespace around commas is stripped. Empty entries (e.g. a trailing
    comma) are ignored. Raises ValueError when the spec resolves to zero
    files or when a literal-path entry has the wrong extension — the
    `label` (e.g. "video" / "waveform") is woven into the message so
    `validate_scene_cfg` surfaces an actionable error.

    Returns paths sorted lexically for stable test/log output. The
    *random* pick across the returned list is the caller's responsibility
    (done at scene setup so re-setup re-picks)."""
    if not spec:
        raise ValueError(f"{label}: file spec is empty")

    matches: set[str] = set()
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            continue
        is_url = entry.lower().startswith(("http://", "https://"))
        if not is_url:
            # A TOML file has no shell to expand a leading `~/…` the way one
            # does for a CLI argument, and glob/os.path treat `~` as a literal
            # directory name — so this has to happen here or the entry matches
            # nothing. URLs are kept off the path helpers entirely.
            entry = paths.expand_user(entry)
        if is_url:
            # A URL (e.g. a direct media link, or a yt-dlp-resolved stream URL
            # from quickcast). Pass through untouched — URLs have no meaningful
            # local extension and must not be globbed or existence-checked;
            # AVFileSource opens http(s) directly via PyAV.
            matches.add(entry)
        elif os.path.isfile(entry):
            # An existing file wins over glob interpretation — filenames with
            # `[`/`]`/`*`/`?` (e.g. YouTube-style `name [videoid].mp4`) would
            # otherwise be mistaken for glob patterns and match nothing.
            if not entry.lower().endswith(extensions):
                raise ValueError(
                    f"{label}: {entry!r} doesn't match expected extension {extensions}"
                )
            matches.add(entry)
        elif _GLOB_CHARS.search(entry):
            # recursive=True only changes behavior for `**` segments; ordinary
            # `*`/`?`/`[...]` patterns are unaffected (backward-compatible).
            hits = [
                p
                for p in glob.glob(entry, recursive=True)
                if os.path.isfile(p) and p.lower().endswith(extensions)
            ]
            if not hits:
                # A glob with zero hits is almost always a typo — louder
                # than silently shrinking the candidate pool.
                raise ValueError(
                    f"{label}: glob {entry!r} matched no files with extension {extensions}"
                )
            matches.update(hits)
        elif os.path.isdir(entry):
            if label == "waveform" and os.path.normpath(entry) == os.path.normpath(
                DEFAULT_WAVEFORM_DIR
            ):
                hits = [
                    os.path.join(dirpath, f)
                    for dirpath, _dirnames, filenames in os.walk(entry)
                    for f in filenames
                    if f.lower().endswith(extensions)
                ]
            else:
                hits = [
                    os.path.join(entry, f)
                    for f in os.listdir(entry)
                    if os.path.isfile(os.path.join(entry, f)) and f.lower().endswith(extensions)
                ]
            if not hits:
                raise ValueError(
                    f"{label}: directory {entry!r} contains no files with extension {extensions}"
                )
            matches.update(hits)
        else:
            # Literal path. Don't require it to exist yet — the scene's
            # setup() reports a clear "file not found" if it disappears
            # between config load and playback. But DO catch extension
            # mismatches now (those are typos, not transient issues).
            if not entry.lower().endswith(extensions):
                raise ValueError(
                    f"{label}: {entry!r} doesn't match expected extension {extensions}"
                )
            matches.add(entry)

    if not matches:
        raise ValueError(f"{label}: file spec {spec!r} resolved to no files")
    return sorted(matches)
