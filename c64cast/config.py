"""Config loading and CLI merging.

Defaults live in the dataclasses below. A TOML file (default search path:
``./c64cast.toml``, override with ``--config PATH``) can override any
of them, and CLI args in turn override the config file. The precedence
is: built-in defaults < config file < CLI flags.

`scenes_from_config` is the factory that turns the declarative ``[[scenes]]``
list into real Scene instances. Lives here rather than in scenes.py so the
display-mode registry doesn't create an import cycle.
"""

from __future__ import annotations

import argparse
import difflib
import glob
import logging
import math
import os
import random
import re
import tomllib
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

from .c64 import nmi_rate_safety
from .dsp import DSPParams

if TYPE_CHECKING:
    from .audio import AudioStreamer
    from .backend import C64Backend
    from .modes import DisplayMode
    from .scenes import Scene
    from .songlengths import LengthsDB
    from .video import WebcamSource

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enum-ish value vocabularies
# ---------------------------------------------------------------------------
# Surfaced to `--describe` and the JSON schema as the valid `choices` for a
# field. These mirror the authoritative constants in the heavy runtime modules
# (modes.PALETTE_MODES, petscii_styles.STYLE_NAMES, waveform.TIME_BASE_NAMES,
# …) but are duplicated here so config.py stays import-light (no numpy / cv2
# pulled in just to load a TOML). tests/test_introspect.py asserts each list
# stays in sync with its source of truth, so the duplication can't drift.
_SYSTEM_CHOICES = ("NTSC", "PAL")
# Mirrors backend.BACKENDS; duplicated here so config.py stays import-light
# (it doesn't pull in api.py). tests/test_introspect.py asserts they match.
_BACKEND_CHOICES = ("ultimate", "teensyrom")
_TR_TRANSPORT_CHOICES = ("serial", "tcp")
_TR_STORAGE_CHOICES = ("sd", "usb")
_DISPLAY_CHOICES = ("hires_edges", "hires", "petscii", "mcm", "mhires", "blank", "random")
_PALETTE_MODE_CHOICES = ("percell", "cheap", "vivid", "grayscale")
_STYLE_CHOICES = (
    "default",
    "halftone",
    "random_glyph",
    "letter_rain",
    "neon",
    "inverse_pop",
    "hatch",
    "color_only",
    "random",
)
_TIME_BASE_CHOICES = ("wallclock", "auto")
_PERSISTENCE_CHOICES = ("off", "short", "medium", "long", "random")
_COLOR_MODE_CHOICES = ("per_voice", "per_waveform")
_MIDI_WAVEFORM_CHOICES = ("triangle", "sawtooth", "pulse", "noise")
_MIDI_FILTER_MODE_CHOICES = ("lowpass", "bandpass", "highpass")
_BACKGROUND_CHOICES = (
    "starfield",
    "petscii_bars",
    "raster_bars",
    "checker",
    "nature",
    "city",
    "none",
    "random",
)
_INPUT_SOURCE_CHOICES = ("cia", "kernal", "auto", "none")

# The scene types (mirrors validate_scene_cfg). Used by the introspection
# layer's `applies_to` filtering; declared here so SceneCfg metadata can name
# them symbolically.
SCENE_TYPES = ("webcam", "blank", "video", "waveform", "midi", "slideshow", "launcher")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
#
# Every overridable field carries `metadata={"help": ...}` (plus optional
# "choices" and, on SceneCfg, "applies_to"). That metadata is the single
# source of truth the introspection layer (introspect.py) renders into
# `--describe`, `--list-*`, `--compat`, and the JSON schema — so the docs
# can't drift from the code. Deep design/rationale comments stay as ordinary
# comments (maintainer-facing); `help` text is concise and author-facing.
#
# NOTE: metadata is written as `field(default=..., metadata={...})` *inlined*
# in each class body, not via a helper — mypy's dataclass plugin only
# recognizes a literal `dataclasses.field(...)` call when deciding a field has
# a default. A wrapper would make every field look required.


@dataclass
class HardwareCfg:
    # Selects the hardware abstraction backend (see backend.make_backend).
    # "ultimate" = Ultimate 64 / Ultimate II+ over socket DMA + REST.
    # "teensyrom" = TeensyROM+ over the token protocol ([teensyrom] section).
    # Defaults to "ultimate" so existing configs are unaffected.
    backend: str = field(
        default="ultimate",
        metadata={"help": "Hardware backend family driving the C64.", "choices": _BACKEND_CHOICES},
    )


@dataclass
class TeensyromCfg:
    # Connection + storage settings for the TeensyROM+ backend
    # ([hardware].backend = "teensyrom"). Ignored by the Ultimate backend.
    transport: str = field(
        default="serial",
        metadata={
            "help": "TR control link: USB serial or raw TCP (port 2112).",
            "choices": _TR_TRANSPORT_CHOICES,
        },
    )
    serial_port: str | None = field(
        default=None,
        metadata={
            "help": "Serial device for transport=serial "
            "(e.g. /dev/tty.usbmodem* or COM3). Required for serial."
        },
    )
    baud: int = field(
        default=2_000_000,
        metadata={"help": "Serial baud rate (TR uses full USB bandwidth; 2 Mbaud 8N1)."},
    )
    host: str | None = field(
        default=None,
        metadata={
            "help": 'TR IP address for transport=tcp (find via CCGMS "ATC" or '
            "RTC sync). Required for tcp."
        },
    )
    tcp_port: int = field(
        default=2112, metadata={"help": "TR TCP listener port (firmware default 2112)."}
    )
    storage: str = field(
        default="sd",
        metadata={
            "help": "Where helper PRGs are uploaded + launched from.",
            "choices": _TR_STORAGE_CHOICES,
        },
    )


@dataclass
class Ultimate64Cfg:
    url: str = field(
        default="http://ultimate-64-ii.lan",
        metadata={"help": "Base URL of the Ultimate 64 (REST + DMA host)."},
    )
    system: str = field(
        default="NTSC",
        metadata={
            "help": "Target video system timing (affects frame rate + SID PLAY rate).",
            "choices": _SYSTEM_CHOICES,
        },
    )
    # See docs/usage.md for how to enable the DMA service on the U64 itself.
    dma_port: int = field(
        default=64,
        metadata={"help": "TCP port of the U64 Ultimate DMA Service (firmware default 64)."},
    )
    # Precedence: C64CAST_DMA_PASSWORD env var > this field > none. The env
    # var override is applied at merge_cli() time so the same TOML can be
    # committed to a public repo without leaking the password.
    dma_password: str | None = field(
        default=None,
        metadata={
            "help": "U64 network password, if set. Prefer the C64CAST_DMA_PASSWORD "
            "env var over committing it here."
        },
    )


@dataclass
class VideoCfg:
    device: int = field(
        default=-1,
        metadata={"help": "Webcam device index; -1 = system default camera (cv2 index 0)."},
    )
    # REU-staged video push. Bitmap frames (hires/mhires) are staged into
    # REU SRAM off-screen and swapped into the displayed bank by an atomic
    # $DD00 flip at vblank (double-buffer — kills the single-buffer tearing
    # that flashes the whole screen on scene cuts). Char-mode screens
    # (petscii, blank) are single-buffer-staged: the 1000-byte $0400 screen
    # is REUWRITE'd then dropped in via one REU→main DMA. Color RAM at $D800
    # always stays on the delta-cached DMAWRITE path (it isn't VIC-banked).
    #
    # Tri-state — true | false | "auto" (default):
    #   * "auto" enables staging ONLY for bitmap modes (where double-buffer
    #     fixes tearing and the bulk transfer wins) and ONLY when the
    #     startup probe confirms the U64's REU is Enabled. Char modes stay on
    #     the host-DMA path under auto — the delta cache makes staging a net
    #     regression there (a full 1000-byte REU→main DMA every frame vs
    #     "only the changed cells"). Falls back to false whenever REU can't be
    #     confirmed (--skip-probe, REU disabled, or the probe query fails), so
    #     video never silently freezes on a box without a (enabled) REU.
    #   * true forces staging on for every mode that supports it.
    #   * false forces it off everywhere.
    # Resolution is per-scene at build time (config.resolve_use_reu_staged),
    # so a `display = "random"` slideshow re-decides per concrete mode.
    # Pairs cleanly with [audio].use_reu_pump on any scene (the bank-swap
    # installer picks a merged $0314 dispatcher that services both IRQ
    # sources). MCM doesn't support staging yet (separate future-work).
    use_reu_staged: bool | str = field(
        default="auto",
        metadata={
            "help": 'REU bank-swap double-buffer for video push. "auto" (default) '
            "stages bitmap modes (hires/mhires) when the startup probe finds "
            "the U64's REU enabled, leaving char modes on the cheaper "
            "host-DMA path; true forces it on for every mode, false off. "
            "auto silently falls back to host-DMA when REU isn't confirmed."
        },
    )


@dataclass
class AudioCfg:
    enabled: bool = field(
        default=False,
        metadata={
            "help": "Master switch for SID audio streaming (the 4-bit $D418 DAC). "
            "Also enabled by the -A CLI flag."
        },
    )
    device: int = field(
        default=-1, metadata={"help": "Audio input device index; -1 = system default microphone."}
    )
    sample_rate: int = field(
        default=10500,
        metadata={
            "help": "Audio sample rate in Hz fed to the SID DAC. Default 10500 lifts "
            "the Nyquist to ~5.25 kHz so fricatives/sibilants survive (8000 lost "
            "them); HW-verified safe on NTSC + PAL with no NMI handler overrun. NTSC "
            "can go to ~11025; keep PAL <= ~10500. Rates that overrun the handler are "
            "rejected at load (see c64.nmi_rate_safety)."
        },
    )
    mic_sensitivity: float = field(
        default=1.5, metadata={"help": "Microphone input gain multiplier."}
    )
    noise_gate: float = field(
        default=0.05, metadata={"help": "Mic level below which input is squelched to silence."}
    )
    # A/B tested on a real 6581: dither-off sounds slightly cleaner (the added
    # hiss outweighs the buzz reduction at 4 bits). Flip on if your hardware
    # or source material disagrees.
    dither: bool = field(
        default=False,
        metadata={
            "help": "TPDF dither on the 4-bit quantization step. Default off; flip on "
            "for smoother hiss on already-noisy sources."
        },
    )
    # See CLAUDE.md [audio].digi_boost for the full rationale. Essential on
    # 8580s and emulated SIDs; on a 6581 it just raises output level.
    digi_boost: bool = field(
        default=False,
        metadata={
            "help": "EXPERIMENTAL: lock SID voices to a DC pulse so the ADSR D/As bias "
            "the master mixer, raising $D418 playback level."
        },
    )
    # 11-bit cutoff maps roughly 0→200 Hz … 2047→20 kHz on a 6581, but the
    # mapping is non-linear and varies per chip. Start ~1500 and tune by ear.
    sid_filter_cutoff: int = field(
        default=0,
        metadata={
            "help": "SID low-pass cutoff for the PWM carrier voice (0 = disabled). "
            "Attenuates the carrier above the audio band."
        },
    )
    # See CLAUDE.md [audio].use_reu_pump. Eliminates the host-DMA 'gurgling'
    # artifact on real hardware by streaming from REU SRAM instead.
    use_reu_pump: bool = field(
        default=False,
        metadata={
            "help": "EXPERIMENTAL: stream video/mic audio from a REU ring "
            "(bus-clean) instead of per-write host DMA. Requires REU enabled."
        },
    )
    # See CLAUDE.md [audio].use_reu_pump. The C64-side pump (CIA #1 rate) and
    # the NMI reader free-run open-loop; video DMA bus-halts throttle the NMI
    # reader below nominal so the pump out-produces it and laps the ring every
    # ~15-23s = audible echo. The governor lives in the pump's own IRQ handler:
    # it reads the NMI read pointer and skips a chunk whenever the write head
    # is too far ahead, self-throttling to the consumer with zero host bus
    # writes. Default on per "prefer best quality"; only relevant when
    # use_reu_pump is set. Off = open-loop (original drift/echo) for A/B.
    reu_pump_governor: bool = field(
        default=True,
        metadata={
            "help": "C64-side rate governor for the REU audio pump: the pump IRQ "
            "skips a chunk when its write head outruns the reader, stopping "
            "drift/echo with no host writes. Only active with use_reu_pump."
        },
    )
    # The host-DMA worker paces ring writes to wall-clock, so the write head W
    # advances at exactly sample_rate while the NMI reader R loses ~4% of its
    # ticks to video DMA bus-halts → W laps the ring every ~26s = echo. The
    # servo reads R once per chunk and runs a PI controller on the worker's
    # sleep so the gap locks near half a ring. Pure host-side timing (no C64
    # writes). Default on per "prefer best quality"; off = open-loop for A/B.
    host_dma_servo: bool = field(
        default=True,
        metadata={
            "help": "Closed-loop pacing for the host-DMA audio worker (mic / "
            "videos): reads the C64 NMI read pointer and adjusts the "
            "producer's software pace so the ring write head holds a fixed "
            "gap behind the reader, stopping the ~26s drift/echo. Pure "
            "host-side timing, no C64 writes. Not the REU pump path."
        },
    )
    # See c64cast.audio_marker for the find-marker analysis helper. Only the
    # REU-pump path injects the marker; host-DMA scenes are unmarked.
    source_alignment_marker: bool = field(
        default=False,
        metadata={
            "help": "DEBUG/CAPTURE ONLY: prepend a 100 ms chirp to REU audio as a "
            "capture-alignment anchor. Turn OFF for production listening."
        },
    )
    # ---- host-DMA servo pitch compensation ----------------------------------
    # The host-DMA audio servo (eliminates echo) locks playback to the C64 NMI
    # consumer rate R. R runs slightly below the nominal sample rate because the
    # display steals NMI ticks (VIC badlines + any host-DMA video writes +, for
    # REU-staged bitmap modes, the per-frame bank-swap raster IRQ), so playback
    # comes out a touch slow (pitch + speed down together). Each multiplier below
    # is a **playback-rate multiplier** for one display mode: >1.0 speeds playback
    # up to cancel the slowdown, 1.0 = no change. The AudioStreamer applies it by
    # decimating the source by a fixed ratio = 1/multiplier (host-side resampling
    # before the 4-bit encode) — NOT by speeding the NMI up. A fixed ratio set per
    # scene avoids both the firmware DMA/badline wedge risk of a faster NMI and the
    # over-correction of chasing the read-pointer R (which reads biased-low under
    # bus load — an R-driven resampler played ~10% fast, capture-verified). It is a
    # best-fit dial: it can't track content-dependent DMA load, so heavy-motion
    # bitmap may still drift; tune by ear per system. `hires_edges` scenes use
    # pitch_mult_hires (same VIC fetch). Defaults are U64-II NTSC starting points
    # (default use_reu_staged="auto" — bitmap video over the bus-clean REU
    # bank-swap, so the residual loss is small). Values are backend- and
    # standard-coupled: PAL (50fps → fewer halts/sec) and the lower-latency TR+
    # backend want their own ears-on values; override per system.
    pitch_mult_petscii: float = field(
        default=1.00,
        metadata={
            "help": "Host-side resample playback-rate multiplier for PETSCII mode "
            "(light char-mode load; 1.0 = none/passthrough. U64-II NTSC: 1.0)."
        },
    )
    pitch_mult_hires: float = field(
        default=1.02,
        metadata={
            "help": "Host-side resample playback-rate multiplier for Hires / "
            "Hires-edges modes (REU-staged bitmap; bank-swap IRQ residual). "
            "U64-II NTSC starting point 1.02; tune by ear, override for TR+ / PAL."
        },
    )
    pitch_mult_mhires: float = field(
        default=1.02,
        metadata={
            "help": "Host-side resample playback-rate multiplier for MultiHires "
            "mode (REU-staged bitmap + host-DMA $D800 color RAM). U64-II NTSC "
            "starting point 1.02; tune by ear, override for TR+ / PAL."
        },
    )
    pitch_mult_mcm: float = field(
        default=1.00,
        metadata={
            "help": "Host-side resample playback-rate multiplier for MCM mode "
            "(char-based, light load; U64-II NTSC: 1.0/passthrough)."
        },
    )
    pitch_mult_blank: float = field(
        default=1.00,
        metadata={
            "help": "Host-side resample playback-rate multiplier for Blank mode "
            "(no video input; 1.0 = none/passthrough)."
        },
    )


@dataclass
class VisionCfg:
    """Camera-as-input: hand-gesture control via MediaPipe HandLandmarker.

    See [c64cast/vision.py](c64cast/vision.py). Needs the `vision` extra
    (mediapipe) + a downloaded HandLandmarker model. The camera is shared with
    any webcam scene through the WebcamSource broker, so no second device is
    needed; gestures work over any scene (blank/video/waveform/webcam)."""

    enabled: bool = field(
        default=False,
        metadata={
            "help": "Enable webcam hand-gesture control (pinch=pause/resume, "
            "swipe=skip, open-hand=cycle). Needs the 'vision' extra."
        },
    )
    model_path: str = field(
        default="assets/models/hand_landmarker.task",
        metadata={
            "help": "Path to the MediaPipe HandLandmarker .task model bundle "
            "(download separately; see assets/models/README.md)."
        },
    )
    num_hands: int = field(default=1, metadata={"help": "Max hands the tracker detects per frame."})
    min_detection_confidence: float = field(
        default=0.7,
        metadata={
            "help": "Minimum confidence to detect a hand (0..1). Raise it if your "
            "torso/face occasionally register as a phantom hand."
        },
    )
    min_tracking_confidence: float = field(
        default=0.5,
        metadata={"help": "Minimum confidence to keep tracking a hand across frames (0..1)."},
    )
    poll_interval_s: float = field(
        default=0.066,
        metadata={"help": "Seconds between gesture-recognition ticks (~0.066 = 15 Hz)."},
    )
    pinch_threshold: float = field(
        default=0.05,
        metadata={"help": "Thumb-index normalized distance below which a pinch registers."},
    )
    swipe_velocity: float = field(
        default=0.4,
        metadata={
            "help": "Wrist horizontal speed (frame-widths/sec) that triggers a skip. "
            "HW-tuned: deliberate swipes peak ~0.5-1.1, drift stays < ~0.2."
        },
    )
    gesture_cooldown_s: float = field(
        default=1.0, metadata={"help": "Minimum seconds between fired gesture events (debounce)."}
    )
    gesture_dwell_s: float = field(
        default=0.4,
        metadata={
            "help": "Seconds a pose (pinch / open hand) must be held STILL before it "
            "fires (0 = first frame). With the stillness gate this rejects "
            "busy/moving hands and poses passing through on the way to a "
            "swipe. Swipe (motion) ignores it."
        },
    )
    hold_threshold_s: float = field(
        default=3.0, metadata={"help": "Seconds a pinch must be held while paused to resume."}
    )
    mirror: bool = field(
        default=True,
        metadata={
            "help": "Mirror the frame before tracking so swipe direction matches "
            "the mirrored webcam view."
        },
    )


@dataclass
class InterstitialCfg:
    duration_s: float = field(
        default=4.0, metadata={"help": "How long the 'UP NEXT' interstitial shows between scenes."}
    )
    text_color: str = field(
        default="rainbow",
        metadata={"help": "Interstitial text color: a C64 color name, 'rainbow', or 'random'."},
    )
    background: str = field(
        default="random",
        metadata={
            "help": "Animated parallax background style behind the interstitial text.",
            "choices": _BACKGROUND_CHOICES,
        },
    )


@dataclass
class PlaylistCfg:
    videos_dir: str = field(
        default="assets/videos",
        metadata={"help": "Directory of videos to interleave between scenes."},
    )
    interleave_videos: bool = field(
        default=True,
        metadata={
            "help": "Insert a video from videos_dir after each scene (multi-scene playlists "
            "only; ignored in single-scene mode)."
        },
    )
    songlengths_file: str | None = field(
        default=None,
        metadata={
            "help": "Path to an HVSC Songlengths.md5 file; gives waveform scenes their "
            "true duration when duration_s is unset."
        },
    )
    # See CLAUDE.md 'Playlist loop control' for single- vs multi-scene behavior.
    loop: bool = field(
        default=True,
        metadata={
            "help": "Loop the playlist after the last scene (--no-loop exits after one "
            "pass; useful for 'play one video and quit')."
        },
    )


@dataclass
class SceneCfg:
    type: str = field(default="webcam", metadata={"help": "Scene kind.", "choices": SCENE_TYPES})
    display: str = field(
        default="hires_edges",
        metadata={
            "help": "VIC-II display mode. waveform and midi are bitmap-only (both "
            "ignore this); slideshow also accepts 'random'.",
            "choices": _DISPLAY_CHOICES,
            "applies_to": ("webcam", "blank", "video", "slideshow"),
        },
    )
    name: str | None = field(
        default=None,
        metadata={"help": "Display name (shown in interstitials/logs; ensemble match key)."},
    )
    # None = scene-type default (30s for webcam/blank, songlengths-or-30s for
    # waveform/midi). Video scenes reject any value (video-driven).
    duration_s: float | None = field(
        default=None,
        metadata={
            "help": "Seconds before auto-advance. Unset = scene-type default. "
            "Video scenes reject this (they run until the file ends). "
            "For launcher this is the idle timeout (reset by player input).",
            "applies_to": ("webcam", "blank", "waveform", "midi", "slideshow", "launcher"),
        },
    )
    # See resolve_file_spec for the comma-separated path/dir/glob grammar.
    file: str | None = field(
        default=None,
        metadata={
            "help": "Asset spec (comma-separated paths/dirs/globs). Videos for "
            "video, .sid for waveform, images for slideshow, "
            ".prg/.crt for launcher.",
            "applies_to": ("video", "waveform", "slideshow", "launcher"),
        },
    )
    image_duration_s: float = field(
        default=5.0,
        metadata={
            "help": "Per-image dwell time before advancing (total runtime is duration_s).",
            "applies_to": ("slideshow",),
        },
    )
    target_fps: float | None = field(
        default=None,
        metadata={
            "help": "Per-scene frame-rate cap; unset = playlist default (60/50), "
            "except waveform scenes which default to half rate (30/25) to "
            "stay under the DMA ceiling."
        },
    )
    # None = follow global [audio].enabled; False forces off; True is a no-op
    # when the global is off. waveform/midi ignore this (they drive the SID).
    audio: bool | None = field(
        default=None,
        metadata={
            "help": "Per-scene audio override. Unset follows [audio].enabled; "
            "false mutes this scene only.",
            "applies_to": ("webcam", "blank", "video"),
        },
    )
    # None = use global [dsp].pre_emphasis (which itself may be source-aware
    # auto); a number overrides it for this scene. Only meaningful when
    # [dsp].enabled and the scene has audio.
    pre_emphasis: float | None = field(
        default=None,
        metadata={
            "help": "Per-scene HF pre-emphasis (0 = off, ~0.3-0.7 typical; "
            "brightens speech). Unset = global [dsp].pre_emphasis / "
            "source-aware default. Needs [dsp].enabled + scene audio.",
            "applies_to": ("webcam", "blank", "video"),
        },
    )
    # waveform-specific kwargs — passed straight through to WaveformScene.
    song: int = field(
        default=0,
        metadata={"help": "SID subtune index to play (0-based).", "applies_to": ("waveform",)},
    )
    color_mode: str = field(
        default="per_voice",
        metadata={
            "help": "Oscilloscope coloring: fixed per voice, or by current waveform type.",
            "choices": _COLOR_MODE_CHOICES,
            "applies_to": ("waveform", "midi"),
        },
    )
    voice_colors: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Per-voice trace colors (C64 color names) for color_mode=per_voice.",
            "applies_to": ("waveform", "midi"),
        },
    )
    waveform_colors: dict[str, str] = field(
        default_factory=dict,
        metadata={
            "help": "Per-waveform-type colors (e.g. pulse=cyan) for color_mode=per_waveform.",
            "applies_to": ("waveform", "midi"),
        },
    )
    time_base: str = field(
        default="wallclock",
        metadata={
            "help": "Scope time window: 'wallclock' (1 row = 1 frame) or 'auto' "
            "(per-voice window sized so auto_cycles cycles fit).",
            "choices": _TIME_BASE_CHOICES,
            "applies_to": ("waveform", "midi"),
        },
    )
    auto_cycles: float = field(
        default=4.0,
        metadata={
            "help": "Complete cycles per render window when time_base = 'auto'.",
            "applies_to": ("waveform", "midi"),
        },
    )
    persistence: str = field(
        default="off",
        metadata={
            "help": "Trace decay/trail length ('off' redraws each frame).",
            "choices": _PERSISTENCE_CHOICES,
            "applies_to": ("waveform", "midi"),
        },
    )
    # Scalar broadcasts to all 3 voices; a list of 3 assigns per voice.
    scroll_columns: int | list[int] = field(
        default=0,
        metadata={
            "help": "FIFO-scroll the strip left by N columns/frame (0 = redraw). "
            "Int or a list of 3 per-voice ints.",
            "applies_to": ("waveform", "midi"),
        },
    )
    # MIDI scene kwargs.
    midi_port: str | None = field(
        default=None,
        metadata={
            "help": "MIDI input port name substring; unset = first available port.",
            "applies_to": ("midi",),
        },
    )
    midi_waveform: str = field(
        default="pulse",
        metadata={
            "help": "SID waveform for MIDI notes.",
            "choices": _MIDI_WAVEFORM_CHOICES,
            "applies_to": ("midi",),
        },
    )
    midi_adsr: list[int] = field(
        default_factory=lambda: [0, 8, 12, 8],
        metadata={
            "help": "ADSR envelope as [attack, decay, sustain, release] (4 nibbles 0..15).",
            "applies_to": ("midi",),
        },
    )
    midi_pulse_width: int = field(
        default=2048,
        metadata={
            "help": "SID pulse width (0..4095) when midi_waveform = 'pulse'. "
            "Swept live by CC1 (mod wheel).",
            "applies_to": ("midi",),
        },
    )
    midi_filter_cutoff: int = field(
        default=2047,
        metadata={
            "help": "SID filter cutoff (0..2047); all voices are routed through "
            "the filter. Default open (neutral lowpass); swept live by CC74.",
            "applies_to": ("midi",),
        },
    )
    midi_filter_resonance: int = field(
        default=0,
        metadata={
            "help": "SID filter resonance (0..15) for MIDI notes; swept live by CC71.",
            "applies_to": ("midi",),
        },
    )
    midi_filter_mode: str = field(
        default="lowpass",
        metadata={
            "help": "SID filter mode for MIDI notes.",
            "choices": _MIDI_FILTER_MODE_CHOICES,
            "applies_to": ("midi",),
        },
    )
    midi_master_volume: int = field(
        default=15,
        metadata={
            "help": "SID master volume nibble (0..15) for MIDI notes; CC7.",
            "applies_to": ("midi",),
        },
    )
    # See CLAUDE.md modes.py for the per-mode palette_mode semantics.
    palette_mode: str = field(
        default="percell",
        metadata={
            "help": "VIC-II slot-allocation strategy for mcm/mhires display (ignored "
            "by other modes): percell (default), cheap, vivid, grayscale. "
            "Color shaping (channel boost + hue corrections, e.g. the purple "
            "rescue) is the global [color] section, applied to every mode.",
            "choices": _PALETTE_MODE_CHOICES,
            "applies_to": ("webcam", "video", "slideshow"),
        },
    )
    style: str = field(
        default="default",
        metadata={
            "help": "PETSCII glyph/color style (only when display = 'petscii'); "
            "'random' picks one at setup.",
            "choices": _STYLE_CHOICES,
            "applies_to": ("webcam", "video", "slideshow"),
        },
    )
    border: int = field(
        default=0,
        metadata={"help": "Border palette index 0..15 (blank scenes).", "applies_to": ("blank",)},
    )
    background: int = field(
        default=0,
        metadata={
            "help": "Background palette index 0..15 (blank scenes).",
            "applies_to": ("blank",),
        },
    )
    # Launcher scene kwargs.
    input_source: str = field(
        default="cia",
        metadata={
            "help": "What counts as player input to reset the idle timeout: "
            "'cia' (joystick bits at $DC00/$DC01), 'kernal' ($00C5/$00C6, "
            "only live while the kernal IRQ runs), 'auto' (both), or "
            "'none' (pure timer, for demos). Never counts C=/SHIFT/CTRL.",
            "choices": _INPUT_SOURCE_CHOICES,
            "applies_to": ("launcher",),
        },
    )
    max_duration_s: float | None = field(
        default=None,
        metadata={
            "help": "Hard ceiling in seconds — advance regardless of input. "
            "Unset = no cap (a continuously-played game runs forever).",
            "applies_to": ("launcher",),
        },
    )
    min_duration_s: float = field(
        default=0.0,
        metadata={
            "help": "Floor in seconds before the idle timeout can advance the "
            "scene, even if no input is seen.",
            "applies_to": ("launcher",),
        },
    )
    reset_before_launch: bool = field(
        default=True,
        metadata={
            "help": "Reset the U64 before launching for a clean machine state.",
            "applies_to": ("launcher",),
        },
    )
    bypass_audio_lock: bool = field(
        default=False,
        metadata={
            "help": "Ensemble: don't contend for the exclusive audio slot — the "
            "launched program drives its own SID concurrently, so several "
            "people can play (and hear) their own games at once. No effect "
            "single-system.",
            "applies_to": ("launcher",),
        },
    )
    # Free-form dicts; each overlay class validates its own kwargs.
    overlays: list[dict[str, Any]] = field(
        default_factory=list,
        metadata={"help": "List of overlay tables ([[scenes.overlays]]); see --list-overlays."},
    )
    # See CLAUDE.md ensemble coordination for orchestrate/follower_only.
    orchestrate: bool = field(
        default=False,
        metadata={
            "help": "Ensemble: make this system the conductor and broadcast this scene "
            "to all others (requires name; ignored single-system)."
        },
    )
    follower_only: bool = field(
        default=False,
        metadata={
            "help": "Ensemble: exclude from normal rotation; used only as a broadcast "
            "follower override (requires name; excludes orchestrate)."
        },
    )


@dataclass
class DebugCfg:
    verbose: int = field(
        default=0, metadata={"help": "Log verbosity (0 = INFO; 1+ = DEBUG). CLI: -v / -vv."}
    )
    heartbeat: float = field(
        default=10.0, metadata={"help": "Seconds between health heartbeat log lines (0 disables)."}
    )
    skip_probe: bool = field(
        default=False, metadata={"help": "Skip the startup U64 reachability probe."}
    )
    log_file: str | None = field(
        default=None,
        metadata={"help": "Also mirror log output to this file (useful for headless runs)."},
    )
    # Zero overhead when off (every hook resolves to a no-op NullProfiler).
    profile: bool = field(
        default=False,
        metadata={"help": "Emit per-scene frame-timing summaries (render/compose/push/wait)."},
    )
    profile_interval: float = field(
        default=10.0, metadata={"help": "Seconds between profiler summary lines."}
    )
    # Diagnostic aid for video flicker/flash investigation — draws the
    # playback timecode + source frame number into each rendered frame
    # (before quantization) so an on-screen range maps onto a known frame.
    frame_numbers: bool = field(
        default=False,
        metadata={
            "help": "Overlay the playback timecode + source frame number on "
            "video/slideshow/webcam frames (debug aid for "
            "locating flashing/flickering frames)."
        },
    )


@dataclass
class PreviewCfg:
    """Local pygame window mirroring what the U64 displays. Off by default
    since it requires the `pygame` optional dep."""

    enabled: bool = field(
        default=False,
        metadata={
            "help": "Open a local pygame window mirroring the U64 display "
            "(requires the 'preview' extra)."
        },
    )
    fps: int = field(default=30, metadata={"help": "Preview window refresh rate."})
    scale: int = field(
        default=3, metadata={"help": "Integer pixel scale factor for the preview window."}
    )
    charset_path: str | None = field(
        default="assets/roms/characters.901225-01.bin",
        metadata={"help": "C64 character ROM used to render char modes in the preview."},
    )


@dataclass
class RecordingCfg:
    """Capture the rendered display to a video file. Uses cv2.VideoWriter,
    so all you need is the `opencv-python` core dep."""

    enabled: bool = field(
        default=False,
        metadata={"help": "Record the rendered display to a video file (cv2.VideoWriter)."},
    )
    path: str = field(default="recording.mp4", metadata={"help": "Output video file path."})
    fps: int = field(default=30, metadata={"help": "Recording frame rate."})
    scale: int = field(
        default=2, metadata={"help": "Integer pixel scale factor for the recording."}
    )
    fourcc: str = field(
        default="mp4v", metadata={"help": "FourCC codec code passed to cv2.VideoWriter."}
    )


@dataclass
class ColorCfg:
    """Global pre-quantization color shaping, applied to every chromatic
    display mode (mcm, mhires, petscii) regardless of palette_mode.

    Two stages run before nearest-palette quantization: a per-channel gain
    (channel_boost) and a set of hue-band corrections. The C64's only purple
    (index 4) is a bright magenta, so dark real-world violets quantize to
    gray/blue and never to purple; the built-in default ships a single
    "purple_rescue" hue band that snaps + boosts the violet→magenta range to
    recover it. User bands extend the defaults unless replace is set."""

    channel_boost: list[float] = field(
        default_factory=list,
        metadata={
            "help": "Per-channel pre-quantize gain [blue, green, red] (OpenCV BGR "
            "order). Empty = built-in default [1.3, 1.2, 1.0] (blue/green "
            "lift toward C64-friendly hues; red left neutral)."
        },
    )
    hue_corrections: list[dict[str, Any]] = field(
        default_factory=list,
        metadata={
            "help": "List of [[color.hue_corrections]] bands applied before "
            "quantize (keys: hue_lo_deg, hue_hi_deg, sat_thresh, "
            "val_thresh, sat_mult, val_mult, hue_target_deg, name). "
            "Empty = built-in purple rescue only."
        },
    )
    hue_corrections_replace_defaults: bool = field(
        default=False,
        metadata={
            "help": "If true, user hue_corrections REPLACE the built-in defaults "
            "instead of extending them."
        },
    )
    auto_fit: bool = field(
        default=True,
        metadata={
            "help": "Per-source adaptive color fit for video + slideshow "
            "scenes: pre-scan the source and stretch its contrast + "
            "saturation to fill the C64 gamut (faithful — hue preserved). "
            "Ignored by webcam scenes (can't pre-scan)."
        },
    )
    auto_fit_strength: float = field(
        default=1.0,
        metadata={
            "help": "Strength of the auto_fit transform, 0..1 (1 = full, 0 = off). "
            "Lerps the derived stretch toward identity."
        },
    )
    force_palette: bool = field(
        default=False,
        metadata={
            "help": "EXTREME forced-palette remap for video + slideshow "
            "scenes (mcm/mhires): pre-scan the source, k-means it into N "
            "clusters, and map each cluster to a DISTINCT C64 color so all "
            "N colors are used. Deliberate false-color (NOT faithful) — "
            "off by default; also reachable via the SHIFT cycle's "
            "'percell+forced' stop once enabled."
        },
    )
    force_palette_colors: int = field(
        default=16,
        metadata={
            "help": "Number of distinct C64 colors to spread the source across "
            "when force_palette is on (2..16). Ignored when "
            "force_palette_indices is set."
        },
    )
    force_palette_indices: list[int] = field(
        default_factory=list,
        metadata={
            "help": "Explicit C64 palette index whitelist (0..15) for "
            "force_palette; its length sets the color count and overrides "
            "force_palette_colors. Empty = use all 16 (or the count)."
        },
    )


@dataclass
class DSPCfg:
    """Host-side audio DSP applied to float samples BEFORE the 4-bit $D418 DAC
    quantization (see c64cast/dsp.py). The DAC has ~24 dB of usable range;
    these stages make the signal use it — even out dynamics (compressor +
    limiter), lift quiet mic input (AGC), brighten speech (pre-emphasis), and
    clean the noise floor without the chatter of a hard gate (expander with
    hysteresis). All stages are off until enabled. Defaults are tuned for the
    4-bit DAC. Orthogonal to [audio].dither (which is the quantization step
    itself) and to the REU pump (which is the transport)."""

    enabled: bool = field(
        default=True,
        metadata={
            "help": "Master switch for the host-side audio DSP chain (ON by "
            "default — the 4-bit DAC needs it). Set false for the legacy "
            "linear encode + hard mic gate."
        },
    )
    pre_emphasis: float | None = field(
        default=None,
        metadata={
            "help": "High-frequency boost amount; y[n]=x+amt*(x-x[-1]). Brightens "
            "speech for intelligibility. Unset = source-aware default (mic "
            "0.7 / line 0.6); a number forces that amount for all sources; "
            "0 disables. Per-scene [[scenes]].pre_emphasis overrides this."
        },
    )
    expander: bool = field(
        default=True,
        metadata={
            "help": "Downward expander with hysteresis (replaces the hard noise "
            "gate when DSP is enabled). Attenuates below the threshold."
        },
    )
    expander_threshold_db: float = field(
        default=-45.0, metadata={"help": "Level below which the expander attenuates (dBFS)."}
    )
    expander_ratio: float = field(
        default=2.0,
        metadata={"help": "Expansion ratio (>1; larger = more attenuation below thresh)."},
    )
    expander_hysteresis_db: float = field(
        default=6.0,
        metadata={
            "help": "Gap (dB) below the open threshold before the gate closes — "
            "prevents chatter on signal hovering at the threshold."
        },
    )
    expander_floor_db: float = field(
        default=-60.0, metadata={"help": "Maximum attenuation the expander applies (dB)."}
    )
    expander_attack_ms: float = field(
        default=5.0, metadata={"help": "Expander gain open (attack) time constant in ms."}
    )
    expander_release_ms: float = field(
        default=80.0, metadata={"help": "Expander gain close (release) time constant in ms."}
    )
    compress: bool = field(
        default=True,
        metadata={
            "help": "Soft-knee feed-forward compressor + makeup gain — the main "
            "win for fitting program dynamics into 4 bits."
        },
    )
    comp_threshold_db: float = field(
        default=-18.0, metadata={"help": "Compression threshold (dBFS); above this, gain reduces."}
    )
    comp_ratio: float = field(
        default=3.0, metadata={"help": "Compression ratio (>=1; e.g. 3 = 3:1 above threshold)."}
    )
    comp_knee_db: float = field(
        default=6.0,
        metadata={"help": "Soft-knee width in dB around the threshold (0 = hard knee)."},
    )
    comp_attack_ms: float = field(
        default=5.0, metadata={"help": "Compressor attack time constant in ms."}
    )
    comp_release_ms: float = field(
        default=120.0, metadata={"help": "Compressor release time constant in ms."}
    )
    comp_makeup_auto: bool = field(
        default=True,
        metadata={
            "help": "Auto-compute makeup gain so threshold-level signal exits near "
            "unity. Set false to use comp_makeup_db explicitly."
        },
    )
    comp_makeup_db: float = field(
        default=0.0, metadata={"help": "Explicit makeup gain (dB) when comp_makeup_auto is false."}
    )
    limiter: bool = field(
        default=True,
        metadata={"help": "Fast peak limiter / brickwall ceiling — final safety stage."},
    )
    limiter_ceiling: float = field(
        default=0.95,
        metadata={"help": "Limiter output ceiling, linear 0..1 (just under full scale)."},
    )
    limiter_release_ms: float = field(
        default=50.0, metadata={"help": "Limiter gain recovery (release) time constant in ms."}
    )
    agc: bool = field(
        default=False,
        metadata={
            "help": "Automatic gain control for the MIC path only (line/video "
            "audio is already peak-normalized). Slow gain toward a target. "
            "EXPERIMENTAL: being level-based it can boost a sustained noise "
            "floor during long pauses — best on clean mics, or pair with the "
            "expander / raise agc_noise_floor_db above the floor."
        },
    )
    agc_target_db: float = field(default=-18.0, metadata={"help": "AGC target RMS level (dBFS)."})
    agc_max_gain_db: float = field(
        default=24.0, metadata={"help": "Maximum AGC gain/attenuation magnitude (dB)."}
    )
    agc_time_ms: float = field(
        default=300.0,
        metadata={"help": "AGC adaptation time constant in ms (larger = slower/steadier)."},
    )
    agc_noise_floor_db: float = field(
        default=-60.0,
        metadata={
            "help": "Below this input RMS, AGC holds gain instead of amplifying the noise floor."
        },
    )

    def to_params(self) -> DSPParams:
        """Build the pure dsp.DSPParams the AudioDSP chain consumes. Maps the
        auto/explicit makeup split onto DSPParams' single optional field."""
        return DSPParams(
            enabled=self.enabled,
            pre_emphasis=self.pre_emphasis,
            expander=self.expander,
            expander_threshold_db=self.expander_threshold_db,
            expander_ratio=self.expander_ratio,
            expander_hysteresis_db=self.expander_hysteresis_db,
            expander_floor_db=self.expander_floor_db,
            expander_attack_ms=self.expander_attack_ms,
            expander_release_ms=self.expander_release_ms,
            compress=self.compress,
            comp_threshold_db=self.comp_threshold_db,
            comp_ratio=self.comp_ratio,
            comp_knee_db=self.comp_knee_db,
            comp_attack_ms=self.comp_attack_ms,
            comp_release_ms=self.comp_release_ms,
            comp_makeup_db=(None if self.comp_makeup_auto else self.comp_makeup_db),
            limiter=self.limiter,
            limiter_ceiling=self.limiter_ceiling,
            limiter_release_ms=self.limiter_release_ms,
            agc=self.agc,
            agc_target_db=self.agc_target_db,
            agc_max_gain_db=self.agc_max_gain_db,
            agc_time_ms=self.agc_time_ms,
            agc_noise_floor_db=self.agc_noise_floor_db,
        )


@dataclass
class ControlPlaneCfg:
    """FastAPI control plane. Off by default; requires the `control` extra."""

    enabled: bool = field(
        default=False,
        metadata={
            "help": "Run the HTTP control plane (pause/resume/skip/reload); "
            "requires the 'control' extra."
        },
    )
    host: str = field(
        default="127.0.0.1", metadata={"help": "Bind address for the control-plane HTTP server."}
    )
    port: int = field(
        default=8765, metadata={"help": "Bind port for the control-plane HTTP server."}
    )


@dataclass
class SystemEntryCfg:
    """One system in an ensemble — name plus the path to its per-system
    standalone TOML. The path is resolved relative to the master TOML's
    directory at load time."""

    name: str
    config: str


@dataclass
class EnsembleCfg:
    """Multi-system runtime config. The presence of [ensemble] in a master
    TOML is what switches the loader into multi-system mode.

    `systems` is ordered left-to-right, matching the physical screen
    arrangement. Order is load-bearing for span-mode orchestrators (e.g.
    BigTextSpan scrolls right-to-left, so the rightmost system is the
    conductor and the leftmost is where the message scrolls off)."""

    systems: list[SystemEntryCfg] = field(default_factory=list)


@dataclass
class Config:
    hardware: HardwareCfg = field(default_factory=HardwareCfg)
    teensyrom: TeensyromCfg = field(default_factory=TeensyromCfg)
    ultimate64: Ultimate64Cfg = field(default_factory=Ultimate64Cfg)
    video: VideoCfg = field(default_factory=VideoCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    vision: VisionCfg = field(default_factory=VisionCfg)
    interstitial: InterstitialCfg = field(default_factory=InterstitialCfg)
    playlist: PlaylistCfg = field(default_factory=PlaylistCfg)
    scenes: list[SceneCfg] = field(default_factory=list)
    debug: DebugCfg = field(default_factory=DebugCfg)
    preview: PreviewCfg = field(default_factory=PreviewCfg)
    recording: RecordingCfg = field(default_factory=RecordingCfg)
    color: ColorCfg = field(default_factory=ColorCfg)
    dsp: DSPCfg = field(default_factory=DSPCfg)
    control: ControlPlaneCfg = field(default_factory=ControlPlaneCfg)
    # Set only on the master Config produced by load_master(). Per-system
    # Configs in the returned list always have ensemble = None.
    ensemble: EnsembleCfg | None = None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = "c64cast.toml"


class ConfigError(Exception):
    """Raised by `load()` when the config file is missing or unparseable.
    The message is already formatted for end-user display (multi-line, no
    traceback needed); cli.py prints it via `log.error("%s", e)` and exits."""

    pass


_TOML_POS_RE = re.compile(r"^(?P<msg>.*) \(at line (?P<line>\d+), column (?P<col>\d+)\)$")


def _format_toml_error(path: str, err: tomllib.TOMLDecodeError) -> str:
    """Build a friendly multi-line error showing the offending line and a
    caret under the column. Python 3.14+ exposes .lineno/.colno/.msg/.doc
    on TOMLDecodeError; on 3.11-3.13 we parse them out of str(err) and
    re-read the file for the source line."""
    lineno = getattr(err, "lineno", None)
    colno = getattr(err, "colno", None)
    msg = getattr(err, "msg", None)
    doc = getattr(err, "doc", None)
    if lineno is None or colno is None or msg is None:
        m = _TOML_POS_RE.match(str(err))
        if m:
            msg = m.group("msg")
            lineno = int(m.group("line"))
            colno = int(m.group("col"))
    if not doc:
        try:
            with open(path, encoding="utf-8") as f:
                doc = f.read()
        except OSError:
            doc = ""
    if msg is None:
        msg = str(err)
    out = [f"Could not parse config file {path}:"]
    if lineno is not None and colno is not None:
        out.append(f"  line {lineno}, column {colno}: {msg}")
        lines = doc.splitlines()
        if 0 < lineno <= len(lines):
            offending = lines[lineno - 1]
            caret = " " * (colno - 1) + "^"
            out.append(f"    {offending}")
            out.append(f"    {caret}")
    else:
        out.append(f"  {msg}")
    return "\n".join(out)


def _apply_section(dc: Any, data: dict[str, Any], section_name: str) -> None:
    """Overwrite dc fields with values from a TOML section dict, dropping
    unknown keys with a warning so typos don't pass silently."""
    valid = {f.name for f in fields(dc)}
    for k, v in data.items():
        if k not in valid:
            close = difflib.get_close_matches(k, valid, n=1)
            suggestion = f" — did you mean {close[0]!r}?" if close else ""
            log.warning("[%s] unknown config key %r%s — ignored", section_name, k, suggestion)
            continue
        setattr(dc, k, v)


def _validate_use_reu_staged(video: VideoCfg) -> None:
    """The tri-state [video].use_reu_staged accepts only a bool or the literal
    string "auto". Catch a typo (e.g. "true"/"on"/"yes") at load time with a
    clear message instead of letting a stray truthy string silently force
    staging on."""
    v = video.use_reu_staged
    if isinstance(v, bool):
        return
    if v != "auto":
        raise ValueError(f'[video].use_reu_staged must be true, false, or "auto", got {v!r}')


def _validate_force_palette(color: ColorCfg) -> None:
    """Range-check the [color].force_palette knobs at load/doctor time so a bad
    value surfaces before the playlist runs, not mid-stream at pre-scan."""
    if not (2 <= color.force_palette_colors <= 16):
        raise ValueError(
            f"color.force_palette_colors must be in 2..16, got {color.force_palette_colors}"
        )
    idx = color.force_palette_indices
    if idx:
        if not (2 <= len(idx) <= 16):
            raise ValueError(f"color.force_palette_indices must list 2..16 entries, got {len(idx)}")
        for i in idx:
            if not (0 <= int(i) <= 15):
                raise ValueError(
                    f"color.force_palette_indices entries must be C64 palette "
                    f"indices 0..15, got {i}"
                )


def load(path: str | None) -> Config:
    """Load a Config from a TOML file path, or from the default search path
    if `path` is None, or return defaults if neither exists.

    `path` semantics:
      - None  → look for ./c64cast.toml; missing is fine.
      - str   → load that file; missing raises ConfigError.

    Parse failures (TOML syntax errors, missing file when path is given)
    raise `ConfigError` with a message formatted for end-user display."""
    cfg = Config()
    if path is None:
        if not os.path.exists(DEFAULT_CONFIG_PATH):
            return cfg
        path = DEFAULT_CONFIG_PATH
        log.info("loading default config %s", path)
    else:
        log.info("loading config %s", path)

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"Config file not found: {path}") from e
    except PermissionError as e:
        raise ConfigError(f"Could not read config file {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(_format_toml_error(path, e)) from e

    for section, dc in (
        ("hardware", cfg.hardware),
        ("teensyrom", cfg.teensyrom),
        ("ultimate64", cfg.ultimate64),
        ("video", cfg.video),
        ("audio", cfg.audio),
        ("vision", cfg.vision),
        ("interstitial", cfg.interstitial),
        ("playlist", cfg.playlist),
        ("debug", cfg.debug),
        ("preview", cfg.preview),
        ("recording", cfg.recording),
        ("dsp", cfg.dsp),
        ("control", cfg.control),
    ):
        if section in data:
            _apply_section(dc, data[section], section)

    _validate_use_reu_staged(cfg.video)

    # [color] is handled separately from the scalar section loop because it
    # carries a list-of-tables field (hue_corrections) that must be pulled out
    # before _apply_section, same as [[scenes.overlays]] below.
    if "color" in data:
        raw_color = dict(data["color"])
        raw_hc = raw_color.pop("hue_corrections", [])
        _apply_section(cfg.color, raw_color, "color")
        for hc in raw_hc:
            if not isinstance(hc, dict):
                raise ValueError(f"color.hue_corrections entry must be a table, got {hc!r}")
            cfg.color.hue_corrections.append(dict(hc))
        _validate_force_palette(cfg.color)

    for raw in data.get("scenes", []):
        sc = SceneCfg()
        # Pull overlays out before _apply_section so we keep the original
        # dicts intact (each overlay class validates its own kwargs).
        raw_overlays = raw.pop("overlays", [])
        _apply_section(sc, raw, "scenes")
        for ov_raw in raw_overlays:
            if not isinstance(ov_raw, dict):
                raise ValueError(f"scenes.overlays entry must be a table, got {ov_raw!r}")
            sc.overlays.append(dict(ov_raw))
        if sc.orchestrate and not sc.name:
            raise ConfigError(
                f"[[scenes]] in {path}: scenes with `orchestrate = true` "
                'must declare a `name = "..."` — the name is the '
                "cross-system match key followers use to look up their "
                "own version of this scene in their per-system playlist."
            )
        if sc.follower_only and not sc.name:
            raise ConfigError(
                f"[[scenes]] in {path}: scenes with `follower_only = true` "
                'must declare a `name = "..."` — the name is what the '
                "conductor's orchestrate=true scene matches to find this "
                "follower override."
            )
        if sc.follower_only and sc.orchestrate:
            raise ConfigError(
                f"[[scenes]] in {path}: scenes cannot have both "
                "`follower_only = true` and `orchestrate = true` — "
                "follower_only marks a scene that *receives* broadcasts; "
                "orchestrate marks one that *initiates* them."
            )
        cfg.scenes.append(sc)

    return cfg


def _parse_ensemble_section(data: dict[str, Any]) -> EnsembleCfg:
    """Build EnsembleCfg from a raw [ensemble] table. Validates that each
    entry in `systems` is a table with non-empty `name` + `config` strings
    and that names are unique."""
    raw_systems = data.get("systems")
    if not isinstance(raw_systems, list) or not raw_systems:
        raise ConfigError(
            "[ensemble] requires a non-empty `systems` array, e.g.:\n"
            "  systems = [\n"
            '      { name = "left",  config = "left.toml"  },\n'
            '      { name = "right", config = "right.toml" },\n'
            "  ]"
        )
    entries: list[SystemEntryCfg] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_systems):
        if not isinstance(raw, dict):
            raise ConfigError(f"[ensemble].systems[{i}] must be a table, got {raw!r}")
        name = raw.get("name")
        cfg_path = raw.get("config")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"[ensemble].systems[{i}] needs a non-empty string `name`")
        if not isinstance(cfg_path, str) or not cfg_path:
            raise ConfigError(
                f"[ensemble].systems[{i}] ({name!r}) needs a non-empty "
                "string `config` (relative path to the per-system TOML)"
            )
        if name in seen:
            raise ConfigError(f"[ensemble].systems: duplicate system name {name!r}")
        seen.add(name)
        entries.append(SystemEntryCfg(name=name, config=cfg_path))
    return EnsembleCfg(systems=entries)


# Sections that inherit master defaults, paired with the field names within
# each section that should NEVER cascade (e.g. ultimate64.url is per-system
# only — every U64 has its own IP, no sensible global default).
#
# Sections deliberately omitted from this list:
#   [[scenes]] — playlists are per-system by nature; sharing scenes across
#                systems is what the [ensemble] orchestrate hook is for, not
#                a side-effect of config cascading.
#   [video]    — device index identifies a physical capture device.
#   [control]  — there is one control plane shared across the ensemble (see
#                control_plane refactor), wired from the master config.
_CASCADE_SECTIONS: tuple[tuple[str, frozenset[str]], ...] = (
    ("hardware", frozenset()),
    # serial_port + host are per-system (each TR has its own device/IP),
    # so they never inherit a master default — like ultimate64.url.
    ("teensyrom", frozenset({"serial_port", "host"})),
    ("ultimate64", frozenset({"url"})),
    ("audio", frozenset()),
    ("interstitial", frozenset()),
    ("playlist", frozenset()),
    ("debug", frozenset()),
    ("preview", frozenset()),
    ("recording", frozenset()),
    ("color", frozenset()),
)


def apply_master_defaults(defaults: Config, sys_cfg: Config) -> Config:
    """Cascade master-TOML defaults into a per-system Config.

    For each cascaded section, fields that the per-system file left at the
    dataclass default inherit the master's value (when the master itself
    set something other than the dataclass default). Fields the per-system
    file explicitly set keep their values.

    Approximation worth knowing about: "the user explicitly set this field"
    is detected as "the field value differs from a fresh blank instance".
    A user who explicitly sets `verbose = 0` in their per-system TOML looks
    identical to "didn't set it" — if the master sets `verbose = 2`, the
    per-system 0 gets overwritten. This is the price of TOML not telling
    us which keys were present in the source file. The fix in practice is
    "if you want to override a master default with the dataclass default,
    set the master to the dataclass default too" — usually a non-issue.

    Returns the same `sys_cfg` instance (mutated in place)."""
    for section_name, skip_fields in _CASCADE_SECTIONS:
        master_section = getattr(defaults, section_name)
        sys_section = getattr(sys_cfg, section_name)
        blank = type(sys_section)()
        for f in fields(sys_section):
            if f.name in skip_fields:
                continue
            blank_val = getattr(blank, f.name)
            master_val = getattr(master_section, f.name)
            sys_val = getattr(sys_section, f.name)
            if sys_val == blank_val and master_val != blank_val:
                setattr(sys_section, f.name, master_val)
    return sys_cfg


@dataclass
class LoadResult:
    """Wrapped return type of load_master().

    Carries the per-system Configs, their names, the absolute paths they
    were loaded from (so SIGHUP-reload can re-read each per-system TOML
    without re-parsing the master), and the `is_ensemble` flag so the
    caller doesn't have to infer it from `len(cfgs) > 1` (an [ensemble]
    with a single system entry still runs through the multi-system code
    path).

    In single-system mode: `cfgs = [the_one_config]`, `names = ["system"]`,
    `paths = [args.config or None]`, `is_ensemble = False`.
    `master_control` holds the master TOML's [control] section (in
    single-system mode this is just the loaded config's [control])."""

    cfgs: list[Config]
    names: list[str]
    paths: list[str | None]
    is_ensemble: bool
    master_control: ControlPlaneCfg


def load_master(path: str | None) -> LoadResult:
    """Single entry point for cli.py. Routes to single- or multi-system mode
    based on whether the TOML has an `[ensemble]` table.

    Returns a `LoadResult` with `cfgs` length ≥ 1. When [ensemble] is
    absent the result holds the single loaded Config with `name="system"`
    and `is_ensemble=False`; the existing single-system code paths read
    unchanged. When [ensemble] is present, every per-system file is
    loaded and the master's other sections cascade in via
    `apply_master_defaults`."""
    if path is None:
        if not os.path.exists(DEFAULT_CONFIG_PATH):
            cfg = Config()
            return LoadResult(
                cfgs=[cfg],
                names=["system"],
                paths=[None],
                is_ensemble=False,
                master_control=cfg.control,
            )
        path = DEFAULT_CONFIG_PATH

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"Config file not found: {path}") from e
    except PermissionError as e:
        raise ConfigError(f"Could not read config file {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(_format_toml_error(path, e)) from e

    if "ensemble" not in raw:
        cfg = load(path)
        return LoadResult(
            cfgs=[cfg],
            names=["system"],
            paths=[path],
            is_ensemble=False,
            master_control=cfg.control,
        )

    log.info("loading ensemble master %s", path)
    ensemble = _parse_ensemble_section(raw["ensemble"])

    if "scenes" in raw:
        log.warning(
            "[%s] ensemble master contains [[scenes]] — ignored "
            "(scenes belong in per-system configs, not the master)",
            path,
        )

    defaults = Config()
    for section, dc in (
        ("ultimate64", defaults.ultimate64),
        ("video", defaults.video),
        ("audio", defaults.audio),
        ("interstitial", defaults.interstitial),
        ("playlist", defaults.playlist),
        ("debug", defaults.debug),
        ("preview", defaults.preview),
        ("recording", defaults.recording),
        ("control", defaults.control),
    ):
        if section in raw:
            _apply_section(dc, raw[section], section)

    _validate_use_reu_staged(defaults.video)

    # [color] master defaults — handled separately for the list-of-tables
    # field, mirroring load() above.
    if "color" in raw:
        raw_color = dict(raw["color"])
        raw_hc = raw_color.pop("hue_corrections", [])
        _apply_section(defaults.color, raw_color, "color")
        for hc in raw_hc:
            if not isinstance(hc, dict):
                raise ValueError(f"color.hue_corrections entry must be a table, got {hc!r}")
            defaults.color.hue_corrections.append(dict(hc))
        _validate_force_palette(defaults.color)

    master_dir = os.path.dirname(os.path.abspath(path))
    cfgs: list[Config] = []
    paths: list[str | None] = []
    for entry in ensemble.systems:
        sub_path = entry.config
        if not os.path.isabs(sub_path):
            sub_path = os.path.join(master_dir, sub_path)
        sys_cfg = load(sub_path)
        sys_cfg = apply_master_defaults(defaults, sys_cfg)
        # Per-system Configs never carry ensemble metadata themselves —
        # only the master TOML does. (Belt and braces: load() never sets
        # ensemble either since it doesn't know about [ensemble].)
        sys_cfg.ensemble = None
        cfgs.append(sys_cfg)
        paths.append(sub_path)
    _warn_audio_only_ensemble(cfgs, [e.name for e in ensemble.systems])
    return LoadResult(
        cfgs=cfgs,
        names=[e.name for e in ensemble.systems],
        paths=paths,
        is_ensemble=True,
        master_control=defaults.control,
    )


_AUDIO_BEARING_SCENE_TYPES = frozenset({"video", "waveform", "midi", "launcher"})


def _scene_contends_for_audio(s: SceneCfg) -> bool:
    """Whether a scene cfg will actually contend for the ensemble audio
    slot at runtime — mirrors Scene.competes_for_audio_lock(). A muted
    video (`audio = false`) produces no sound and falls through
    like a non-audio scene, so it doesn't count. waveform/midi have no
    per-scene audio override (they drive the SID directly), so they
    always count."""
    if s.type not in _AUDIO_BEARING_SCENE_TYPES:
        return False
    # A muted video falls through like a non-audio scene.
    if s.type == "video" and s.audio is False:
        return False
    # A launcher with bypass_audio_lock never waits on the slot (it plays
    # its own SID concurrently), so it doesn't contend either.
    return not (s.type == "launcher" and s.bypass_audio_lock)


def _warn_audio_only_ensemble(cfgs: list[Config], names: list[str]) -> None:
    """Emit a load-time WARNING for any per-system playlist composed
    entirely of audio-bearing scene types. In ensemble mode only one
    system can hold the audio slot at a time; if a system has nothing
    else to fall back to, it will sit and wait whenever the slot is
    held elsewhere instead of advancing to a non-audio scene. Not a
    hard error — single-scene audio-bearing playlists are still
    meaningful (e.g. a system dedicated to looping a SID tune) — but
    the user should know it's a contention footgun."""
    for cfg, name in zip(cfgs, names, strict=True):
        if not cfg.scenes:
            continue
        if all(_scene_contends_for_audio(s) for s in cfg.scenes):
            log.warning(
                "[%s] every scene in this system's playlist needs the "
                "ensemble audio slot — when another system holds it, "
                "this playlist will idle until the slot frees instead "
                "of falling through to a non-audio scene",
                name,
            )


# Mapping argparse dest → (config section attr, field name). Used by
# merge_cli to know which CLI flags map onto which config fields.
CLI_TO_CFG = {
    "backend": ("hardware", "backend"),
    "tr_transport": ("teensyrom", "transport"),
    "tr_serial_port": ("teensyrom", "serial_port"),
    "tr_host": ("teensyrom", "host"),
    "url": ("ultimate64", "url"),
    "system": ("ultimate64", "system"),
    "dma_port": ("ultimate64", "dma_port"),
    "device": ("video", "device"),
    "audio": ("audio", "enabled"),
    "audio_device": ("audio", "device"),
    "sample_rate": ("audio", "sample_rate"),
    "mic_sensitivity": ("audio", "mic_sensitivity"),
    "noise_gate": ("audio", "noise_gate"),
    "vision": ("vision", "enabled"),
    "vision_model": ("vision", "model_path"),
    "videos": ("playlist", "videos_dir"),
    "loop": ("playlist", "loop"),
    "verbose": ("debug", "verbose"),
    "heartbeat": ("debug", "heartbeat"),
    "skip_probe": ("debug", "skip_probe"),
    "log_file": ("debug", "log_file"),
    "profile": ("debug", "profile"),
    "profile_interval": ("debug", "profile_interval"),
    "frame_numbers": ("debug", "frame_numbers"),
}


def merge_cli(cfg: Config, args: argparse.Namespace) -> Config:
    """For each CLI option whose value is not None, overwrite the matching
    config field. Argparse must use ``default=None`` for every overridable
    option (so "user didn't pass it" is distinguishable from "user passed
    the default").

    Also folds in the C64CAST_DMA_PASSWORD env var as the final layer of
    precedence (env > config > default) so the U64 network password can be
    supplied without putting it in a checked-in TOML file."""
    for dest, (section, key) in CLI_TO_CFG.items():
        if not hasattr(args, dest):
            continue
        val = getattr(args, dest)
        if val is None:
            continue
        setattr(getattr(cfg, section), key, val)
    env_pw = os.environ.get("C64CAST_DMA_PASSWORD")
    if env_pw is not None:
        cfg.ultimate64.dma_password = env_pw
    return cfg


# ---------------------------------------------------------------------------
# Scene factory
# ---------------------------------------------------------------------------

# Display modes that benefit from REU bank-swap double-buffering. Bitmap
# modes push a full 8000-byte frame every frame, so staging it off-screen and
# swapping $DD00 at vblank is what eliminates the single-buffer tearing that
# flashes the whole screen on scene cuts. Char modes (petscii/blank) are
# delta-cached small writes where staging is a net regression — so the "auto"
# setting leaves them on the host-DMA path. (mcm doesn't support staging.)
_REU_BITMAP_MODES = frozenset({"hires", "hires_edges", "mhires"})


def resolve_use_reu_staged(setting: bool | str, display: str, *, reu_available: bool) -> bool:
    """Resolve the [video].use_reu_staged tri-state to a concrete bool for one
    scene's display mode.

    "auto" → True only for a bitmap display mode (see _REU_BITMAP_MODES) AND
    only when the hardware probe confirmed the REU is usable (reu_available).
    Explicit true/false pass straight through. The loader guarantees the only
    legal string is "auto"; any other string is treated as auto (False here)
    rather than silently truthy-True."""
    if isinstance(setting, str):
        return reu_available and display in _REU_BITMAP_MODES
    return bool(setting)


def _build_display_mode(
    name: str,
    palette_mode: str = "percell",
    border: int = 0,
    background: int = 0,
    style: str = "default",
    use_reu_staged: bool = False,
    audio_reu_pump_active: bool = False,
    color: ColorCfg | None = None,
) -> DisplayMode:
    # Imported inside the function to keep config.py importable in test
    # contexts that stub out the heavy modules.
    from .modes import (
        BlankDisplayMode,
        HiresDisplayMode,
        MCMDisplayMode,
        MultiHiresDisplayMode,
        PETSCIIDisplayMode,
    )

    # The whole [color] section is threaded through as one object; unpack the
    # static-shaping + forced-palette knobs the chromatic modes need here (a
    # single extraction point keeps the call sites to one `color=` kwarg).
    color = color if color is not None else ColorCfg()
    channel_boost = color.channel_boost
    hue_corrections = color.hue_corrections
    hue_corrections_replace = color.hue_corrections_replace_defaults
    force_palette = color.force_palette
    if name == "hires_edges":
        return HiresDisplayMode(
            style="edges",
            use_reu_staged=use_reu_staged,
            audio_reu_pump_active=audio_reu_pump_active,
        )
    if name == "hires":
        return HiresDisplayMode(
            style="normal",
            use_reu_staged=use_reu_staged,
            audio_reu_pump_active=audio_reu_pump_active,
        )
    if name == "petscii":
        return PETSCIIDisplayMode(
            style=style,
            use_reu_staged=use_reu_staged,
            channel_boost=channel_boost,
            hue_corrections=hue_corrections,
            hue_corrections_replace=hue_corrections_replace,
        )
    if name == "mcm":
        return MCMDisplayMode(
            palette_mode=palette_mode,
            channel_boost=channel_boost,
            hue_corrections=hue_corrections,
            hue_corrections_replace=hue_corrections_replace,
            force_palette=force_palette,
        )
    if name == "mhires":
        return MultiHiresDisplayMode(
            palette_mode=palette_mode,
            use_reu_staged=use_reu_staged,
            audio_reu_pump_active=audio_reu_pump_active,
            channel_boost=channel_boost,
            hue_corrections=hue_corrections,
            hue_corrections_replace=hue_corrections_replace,
            force_palette=force_palette,
        )
    if name == "blank":
        return BlankDisplayMode(border=border, background=background, use_reu_staged=use_reu_staged)
    raise ValueError(
        f"unknown display mode {name!r} (want: hires_edges, hires, petscii, mcm, mhires, blank)"
    )


_songlengths_cache: dict[str, LengthsDB | None] = {}


def _load_songlengths(path: str | None) -> LengthsDB | None:
    """Memoized load of the HVSC SongLengths database. Returns None if no
    path is configured or the file is missing/unreadable."""
    if not path:
        return None
    if path in _songlengths_cache:
        return _songlengths_cache[path]
    try:
        from .songlengths import LengthsDB

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
    from .overlays import build_overlay, validate_for_scene

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
            raise ValueError(
                f"{label} scene: no `file =` set and the default directory "
                f"{default_dir!r} is missing or empty. Drop {drop_hint} into "
                f'{default_dir}/ or set `file = "path"` on the scene '
                f"(comma-separated paths/dirs/globs accepted)."
            ) from e
        raise


def _display_mode_for_scene(
    display: str, s: SceneCfg, cfg: Config, *, reu_available: bool = False
) -> DisplayMode:
    """Build the standard video display mode for a scene, centralizing the
    palette/border/background/style/REU/color kwarg cluster shared by the
    webcam, video, and slideshow paths (both the validate and build
    passes). `display` is passed explicitly because slideshow resolves
    "random" to a concrete mode first.

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
    use_reu_staged (separate future-work)."""
    return _build_display_mode(
        display,
        palette_mode=s.palette_mode,
        border=s.border,
        background=s.background,
        style=s.style,
        use_reu_staged=resolve_use_reu_staged(
            cfg.video.use_reu_staged, display, reu_available=reu_available
        ),
        audio_reu_pump_active=cfg.audio.use_reu_pump,
        color=cfg.color,
    )


def _validate_blank(s: SceneCfg, cfg: Config) -> DisplayMode:
    if s.display not in ("blank", "hires_edges"):
        raise ValueError(f"blank scene must use display = 'blank', got {s.display!r}")
    return _build_display_mode(
        "blank",
        border=s.border,
        background=s.background,
        use_reu_staged=resolve_use_reu_staged(
            cfg.video.use_reu_staged, "blank", reu_available=False
        ),
    )


def _validate_video(s: SceneCfg, cfg: Config) -> DisplayMode:
    _resolve_file_spec_or_explain(
        s, DEFAULT_VIDEO_DIR, VIDEO_EXTS, label="video", drop_hint="a video"
    )
    if s.duration_s is not None:
        raise ValueError(
            "video scene does not accept `duration_s` — the scene "
            "runs until the video file ends. Remove the field from the "
            "config; use a [[scenes]] timeout via a different scene type "
            "if you want a hard cap."
        )
    return _display_mode_for_scene(s.display, s, cfg)


def _validate_scope_knobs(s: SceneCfg, label: str) -> None:
    """Validate the shared VoiceScopeRenderer knobs (time_base / auto_cycles /
    persistence / scroll_columns) used by both waveform and midi scenes. Mirrors
    the constructor checks so doctor mode (no scene instance) catches them too."""
    from .voice_scope import BITMAP_W as _SCOPE_BITMAP_W
    from .voice_scope import PERSISTENCE_NAMES, TIME_BASE_NAMES

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
    # ignored for this scene type. Synthesise a hires display_mode so
    # overlay compatibility checks fire against what the scene will
    # actually paint.
    return _build_display_mode("hires")


def _validate_midi(s: SceneCfg) -> DisplayMode:
    if len(s.midi_adsr) != 4:
        raise ValueError(f"midi scene midi_adsr must have 4 entries, got {s.midi_adsr!r}")
    _validate_scope_knobs(s, "midi")
    # MidiScene is bitmap-only (hires oscilloscope) — the SceneCfg `display`
    # field is ignored. Synthesise a hires display_mode so overlay
    # compatibility validates against what the scene will actually paint
    # (and PETSCII overlays are rejected, as on a waveform scene).
    return _build_display_mode("hires")


def _validate_slideshow(s: SceneCfg, cfg: Config) -> DisplayMode:
    _resolve_file_spec_or_explain(
        s, DEFAULT_SLIDESHOW_DIR, PICTURE_EXTS, label="slideshow", drop_hint="a .jpg/.png"
    )
    if s.image_duration_s <= 0:
        raise ValueError(f"slideshow: image_duration_s must be > 0, got {s.image_duration_s!r}")
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
    # `display` defaults to "hires_edges" on SceneCfg; reject any value
    # since the program — not c64cast — drives the VIC.
    if s.display != "hires_edges":
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
        from .orchestrator import resolve_orchestrator

        resolve_orchestrator(s)


def validate_nmi_sample_rate(cfg: Config) -> None:
    """Guard [audio].sample_rate against the NMI handler's cycle budget.

    Raises ConfigError when the configured rate would overrun the $D418 DAC NMI
    handler on the target system (NMIs queue → pitch drop); logs a warning for
    rates inside the entry-latency margin. Thin pass-through to
    `c64.nmi_rate_safety` so the rule lives in one place (shared with --doctor).
    No-op when audio is disabled."""
    if not cfg.audio.enabled:
        return
    level, message = nmi_rate_safety(cfg.ultimate64.system, cfg.audio.sample_rate)
    if level == "error":
        raise ConfigError(f"[audio].sample_rate: {message}")
    if level == "warn":
        log.warning("[audio].sample_rate: %s", message)


def validate_scene_cfg(s: SceneCfg, cfg: Config, *, audio_enabled: bool) -> None:
    """Pre-construction validation for a SceneCfg.

    Runs every check that `build_scene` would surface at load time, without
    instantiating a Scene. Safe to call without api/audio/source — used by
    `doctor.validate_load_result` to collect all configuration errors in one
    pass instead of failing fast on the first one.

    Raises ValueError (display-mode parse, required fields, overlay
    compatibility) or OrchestratorError (orchestrate=true with no
    claiming subclass). The constructor-only webcam check (`source is None`)
    lives in `build_scene` itself — doctor mode runs without a source and
    must not be tripped by it.

    Per-type checks live in `_validate_<type>` helpers, each returning the
    display mode the scene will paint (so the shared overlay-compat loop can
    validate against it). Launcher is the exception — it owns the VIC, so it
    self-validates (including its orchestrator) and we return immediately."""
    from .overlays import build_overlay, validate_for_scene

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
    elif s.type == "slideshow":
        mode = _validate_slideshow(s, cfg)
    elif s.type == "launcher":
        _validate_launcher(s)
        return
    else:
        raise ValueError(
            f"unknown scene type {s.type!r} "
            "(known: webcam, blank, video, waveform, midi, "
            "slideshow, launcher). Note: scrolling_text is now an overlay — "
            "attach it via [[scenes.overlays]]."
        )

    audio_proxy = _AUDIO_SENTINEL if audio_enabled else None
    for ov_cfg in s.overlays:
        ov = build_overlay(ov_cfg, audio_proxy)
        validate_for_scene(ov, mode)

    if s.orchestrate:
        from .orchestrator import resolve_orchestrator

        resolve_orchestrator(s)


def build_scene(
    s: SceneCfg,
    cfg: Config,
    api: C64Backend,
    audio: AudioStreamer | None,
    source: WebcamSource | None,
    *,
    is_ensemble: bool = False,
    reu_available: bool = False,
) -> Scene:
    """Build a single Scene from a SceneCfg.

    Extracted from `scenes_from_config` so the playlist's broadcast
    interrupt machinery (see Playlist._handle_broadcast_interrupt) can
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
    (validation, doctor) leave it False so auto degrades to host-DMA."""
    from .scenes import BlankScene, VideoScene, WebcamScene

    validate_scene_cfg(s, cfg, audio_enabled=audio is not None)

    audio_reu_pump_active = cfg.audio.use_reu_pump
    scene: Scene
    if s.type == "webcam":
        if source is None:
            raise ValueError(
                "webcam scene declared but no WebcamSource was provided — "
                "this should have been caught at cli.py startup"
            )
        mode = _display_mode_for_scene(s.display, s, cfg, reu_available=reu_available)
        name = s.name or f"Webcam {s.display}"
        # Default: follow global [audio].enabled. When `audio` is None here,
        # the streamer wasn't constructed (global is off) so the scene runs
        # silent; when it's a real streamer, the scene picks it up. Set
        # `audio = false` per-scene to opt out even when the global is on.
        scene_audio = None if s.audio is False else audio
        if is_ensemble and scene_audio is not None:
            if s.audio is True:
                log.info(
                    "[%s] live webcam scene: audio suppressed in "
                    "ensemble mode (live scenes never hold the audio "
                    "spotlight)",
                    name,
                )
            scene_audio = None
        scene = WebcamScene(api, scene_audio, mode, source, cfg.audio, name)
    elif s.type == "blank":
        mode = _build_display_mode(
            "blank",
            border=s.border,
            background=s.background,
            use_reu_staged=resolve_use_reu_staged(
                cfg.video.use_reu_staged, "blank", reu_available=reu_available
            ),
        )
        name = s.name or "Blank"
        scene_audio = None if s.audio is False else audio
        if is_ensemble and scene_audio is not None:
            if s.audio is True:
                log.info(
                    "[%s] live blank scene: audio suppressed in "
                    "ensemble mode (live scenes never hold the audio "
                    "spotlight)",
                    name,
                )
            scene_audio = None
        scene = BlankScene(api, scene_audio, mode, cfg.audio, name)
    elif s.type == "video":
        mode = _display_mode_for_scene(s.display, s, cfg, reu_available=reu_available)
        # Default: audio ON for videos (it's part of the file).
        # The user can mute one with `audio = false`.
        scene_audio = None if s.audio is False else audio
        assert s.file is not None  # narrowed by validate_scene_cfg
        scene = VideoScene(
            api,
            scene_audio,
            mode,
            s.file,
            prepend_alignment_marker=(cfg.audio.source_alignment_marker and cfg.audio.use_reu_pump),
            color=cfg.color,
        )
    elif s.type == "waveform":
        from .waveform import WaveformScene

        # If duration_s is unset AND a songlengths DB is configured, let
        # the WaveformScene look up the true length. Explicit duration_s
        # wins over the DB.
        user_duration = s.duration_s
        db = _load_songlengths(cfg.playlist.songlengths_file)
        assert s.file is not None  # narrowed by validate_scene_cfg
        scene = WaveformScene(
            api,
            audio,
            file=s.file,
            song=s.song,
            duration_s=user_duration,
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
        )
        if s.name:
            scene.name = s.name
    elif s.type == "slideshow":
        from .scenes import SlideshowScene

        display = _resolve_slideshow_display(s.display)
        mode = _display_mode_for_scene(display, s, cfg, reu_available=reu_available)
        assert s.file is not None  # narrowed by validate_scene_cfg
        # Pass the *original* display spec (may be "random") so the scene
        # can re-resolve at each setup() for fresh variety in single-scene
        # loops. The build kwargs travel along so the scene can rebuild
        # without re-plumbing through `scene._cfg`. The REU staging setting
        # is handed over as the raw tri-state + the probe verdict (not the
        # resolved bool), so a `display = "random"` rebuild re-decides
        # staging per concrete mode each setup().
        scene = SlideshowScene(
            api,
            mode,
            s.file,
            image_duration_s=s.image_duration_s,
            display_spec=s.display,
            palette_mode=s.palette_mode,
            border=s.border,
            background=s.background,
            style=s.style,
            use_reu_staged=cfg.video.use_reu_staged,
            reu_available=reu_available,
            audio_reu_pump_active=audio_reu_pump_active,
            color=cfg.color,
        )
    elif s.type == "launcher":
        from .scenes import LauncherScene

        assert s.file is not None  # narrowed by validate_scene_cfg
        # No audio streamer: the launched program drives the real SID
        # directly. No display mode / overlays: it owns the VIC.
        scene = LauncherScene(
            api,
            s.file,
            input_source=s.input_source,
            reset_before_launch=s.reset_before_launch,
            min_duration_s=s.min_duration_s,
            max_duration_s=(math.inf if s.max_duration_s is None else s.max_duration_s),
            bypass_audio_lock=s.bypass_audio_lock,
            name=s.name,
        )
    else:  # s.type == "midi" (validator already rejected unknown types)
        from .midi_scene import MidiScene

        a, d, sus, r = s.midi_adsr
        scene = MidiScene(
            api,
            audio,
            port=s.midi_port,
            waveform=s.midi_waveform,
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
    # Video scenes set their own video-driven duration in __init__
    # (math.inf) — `validate_scene_cfg` above already rejected any explicit
    # duration_s on them, so honor that by not overwriting it here.
    if s.duration_s is not None and s.type != "video":
        scene.duration_s = s.duration_s
    if s.target_fps is not None:
        scene.target_fps = float(s.target_fps)
    _attach_overlays(scene, s.overlays, audio)
    # Debug aid: source-bearing scenes draw the playback timecode + frame
    # number into each frame (pre-quantization). Harmless no-op on scenes
    # without a video frame (waveform/launcher/midi ignore the flag).
    scene.show_frame_numbers = cfg.debug.frame_numbers
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
    [video].use_reu_staged "auto" setting (see resolve_use_reu_staged)."""
    from .scenes import VideoScene, WebcamScene
    from .video import _ensure_pyav

    # Validate follower-only scenes here too — they're built lazily at
    # broadcast time via `build_follower_scene`, so without this call a
    # bad cfg would only surface mid-broadcast. (build_scene below runs
    # validate_scene_cfg internally for the scenes that DO build now.)
    for s in cfg.scenes:
        if s.follower_only:
            validate_scene_cfg(s, cfg, audio_enabled=audio is not None)

    base: list[Scene] = [
        build_scene(
            s, cfg, api, audio, source, is_ensemble=is_ensemble, reu_available=reu_available
        )
        for s in cfg.scenes
        if not s.follower_only
    ]

    if not base:
        # Sensible default if user gave us no scenes at all. No audio —
        # live video defaults to silent so it can run at full speed.
        if source is None:
            raise ValueError(
                "no scenes configured and no WebcamSource available — "
                "configure at least one scene or attach a webcam"
            )
        from .modes import HiresDisplayMode

        base.append(
            WebcamScene(
                api, None, HiresDisplayMode(style="edges"), source, cfg.audio, "Live Hi-Res Edges"
            )
        )
        base[-1].duration_s = 30.0

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
    if not _ensure_pyav():
        log.warning(
            "Found %d video files but PyAV is not installed; skipping videos.", len(video_files)
        )
        return base

    from .modes import HiresDisplayMode

    interleaved: list[Scene] = []
    video_idx = 0
    for built in base:
        interleaved.append(built)
        if not isinstance(built, VideoScene):
            interleaved.append(
                VideoScene(
                    api,
                    audio,
                    HiresDisplayMode(style="edges"),
                    video_files[video_idx],
                    prepend_alignment_marker=(
                        cfg.audio.source_alignment_marker and cfg.audio.use_reu_pump
                    ),
                )
            )
            video_idx = (video_idx + 1) % len(video_files)
    return interleaved


VIDEO_EXTS = (".mp4", ".avi", ".mkv", ".mov", ".webm", ".m4v")
SID_EXTS = (".sid",)
PICTURE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
PROGRAM_EXTS = (".prg", ".crt")

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


def _resolve_slideshow_display(spec: str) -> str:
    """Resolve a slideshow scene's `display` config value:

    * `"hires_edges"` (the SceneCfg global default, tuned for live webcam
      Canny-edge stylization) is substituted with `"mhires"` — stills
      benefit most from per-cell color picking. Users wanting bitmap
      output can set `display = "hires"` explicitly.
    * `"random"` picks one of `SLIDESHOW_RANDOM_DISPLAYS` at random; this
      runs at every setup() so single-scene loops get fresh variety.
    * Any other value passes through unchanged.
    """
    if spec == "hires_edges":
        return "mhires"
    if spec == "random":
        return random.choice(SLIDESHOW_RANDOM_DISPLAYS)
    return spec


def _gather_videos(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, f) for f in os.listdir(directory) if f.lower().endswith(VIDEO_EXTS)
    )


_GLOB_CHARS = re.compile(r"[*?\[]")


def resolve_file_spec(spec: str, extensions: tuple[str, ...], *, label: str) -> list[str]:
    """Resolve a comma-separated `file =` spec to a sorted, unique list of
    concrete file paths.

    Each comma-separated entry is one of:
      * a literal file path — included as-is (extension-checked).
      * a directory path — every file inside whose extension is in
        `extensions` is included (non-recursive; mirrors `_gather_videos`).
      * a glob pattern (containing `*`, `?`, or `[`) — expanded via
        `glob.glob`; matches whose extension is in `extensions` are kept.

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
        if entry.lower().startswith(("http://", "https://")):
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
            hits = [
                p for p in glob.glob(entry) if os.path.isfile(p) and p.lower().endswith(extensions)
            ]
            if not hits:
                # A glob with zero hits is almost always a typo — louder
                # than silently shrinking the candidate pool.
                raise ValueError(
                    f"{label}: glob {entry!r} matched no files with extension {extensions}"
                )
            matches.update(hits)
        elif os.path.isdir(entry):
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
