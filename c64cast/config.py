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
from .dac_curves import DAC_CURVE_CHOICES
from .dither import DITHER_METHODS
from .dsp import DSPParams
from .palette import CELL_STRATEGIES, COLOR_MATCH_MODES, resolve_color
from .sampler import SAMPLER_REF_CLOCK_DEFAULT
from .sid_autoconfig import SID_MODEL_CHOICES, resolve_sid_model_cfg

if TYPE_CHECKING:
    from .audio import AudioStreamer
    from .backend import C64Backend
    from .modes import DisplayMode
    from .sampler import UltimateAudioSampler
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
# Field-metadata "apply" hint for the on-C64 menu: "live" = the running scene
# can apply a change in place (zero-flash, via a display-mode/scene setter);
# "rebuild" (the default for unmarked fields) = changing it needs a scene
# rebuild, so the menu shows it read-only this cut. Internal-only — not
# surfaced in the schema, serializer, or example.toml.
_APPLY_CHOICES = ("live", "rebuild")
_MIDI_WAVEFORM_CHOICES = ("triangle", "sawtooth", "pulse", "noise")
_MIDI_FILTER_MODE_CHOICES = ("lowpass", "bandpass", "highpass")
# Mirrors midi_scene.VOICE_MODES (asserted by tests/test_introspect.py).
_MIDI_VOICE_MODE_CHOICES = ("shared", "multitimbral")
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
# Mirror generators.generator_names() / effects.effect_names() (hardcoded to
# keep config import-light; a drift test in test_introspect pins the match).
# Generative video sources + the per-scene pixel effects.
_GENERATIVE_SOURCE_CHOICES = (
    "plasma",
    "tunnel",
    "fire",
    "mandelbrot",
    "moire2",
    "halo",
    "epicycle",
    "hopalong",
    "rorschach",
)
_EFFECT_CHOICES = ("trails", "pulse", "rgb_shift")
# How a slideshow image is fit to the C64 aspect before the display mode
# downscales it. See scenes._apply_aspect.
_ASPECT_MODE_CHOICES = ("crop", "fit", "stretch")

# Per-scene audio source for composable (generative) scenes — the AudioSource
# building block in audio_source.py. "none" = silence; "mic" = live mic via the
# shared AudioStreamer (gated by [audio].enabled); "sid" = play a .sid on the
# real chip (needs `file`). Default "mic" reproduces the pre-field behavior
# (mic when audio is enabled, else silence). A drift test pins this list.
_AUDIO_SOURCE_CHOICES = ("none", "mic", "sid")

# Video-audio backend selector ([audio].backend). "dac" = the 4-bit $D418 NMI
# DAC (every backend; lo-fi, bus-coupled). "sampler" = the U64 "Ultimate Audio"
# FPGA PCM sampler (high fidelity, off the C64 bus; U64 only — see sampler.py).
# "auto" = sampler on a sampler-capable U64 with the feature available, else
# dac. A drift test pins this list.
_AUDIO_BACKEND_CHOICES = ("auto", "dac", "sampler")

# The scene types (mirrors validate_scene_cfg). Used by the introspection
# layer's `applies_to` filtering; declared here so SceneCfg metadata can name
# them symbolically.
SCENE_TYPES = (
    "webcam",
    "blank",
    "video",
    "waveform",
    "midi",
    "asid",
    "slideshow",
    "launcher",
    "generative",
    "wled",
)

# Scene types that render a numpy frame (and so support a per-scene `effect`).
# Excludes blank (no frame), waveform/midi (self-rendered bitmap, bypass the
# frame→display helper), and launcher (the program owns the VIC).
_EFFECT_SCENE_TYPES = frozenset({"webcam", "video", "slideshow", "generative", "wled"})


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
            "help": "Serial device for transport=serial over a plain USB data "
            "cable (e.g. /dev/cu.usbmodem* or COM3; NOT an FTDI null-modem "
            "cable). On macOS, leave unset to auto-detect the TeensyROM by its "
            "USB serial number; required (no auto-detect yet) on other platforms."
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
    # Auto-provision the U64's REU for runs that hard-require it. When a config
    # opts into an REU-staged path as a hard requirement ([audio].use_reu_pump
    # or an explicit [video].use_reu_staged = true — the same condition
    # --doctor checks), c64cast PUTs "RAM Expansion Unit" = Enabled + "REU
    # Size" = 16 MB over the REST config API at startup, LIVE and VOLATILE
    # (never saved to flash, so it reverts on the next power-cycle), and
    # restores the originals at teardown. This removes the manual "F2 -> C64 and
    # Cartridge Settings -> RAM Expansion Unit -> Enabled" step those paths used
    # to require (and that --doctor errored on). The default use_reu_staged =
    # "auto" is left alone — it self-heals to host-DMA double-buffer (also
    # tear-free), so no machine config is touched for it. No effect on backends
    # without an REU (TeensyROM) or under --skip-probe (we never write config we
    # can't first read back). Set false to manage the REU yourself.
    auto_reu: bool = field(
        default=True,
        metadata={
            "help": "Auto-enable + size the U64 REU (live, volatile, restored at "
            "teardown) for runs that hard-require it ([audio].use_reu_pump or "
            "explicit [video].use_reu_staged = true). Removes the manual F2 "
            "enable step. false = manage the REU yourself. No effect on no-REU "
            "backends or under --skip-probe."
        },
    )
    sid_model: str = field(
        default="auto",
        metadata={
            "help": "Auto-configure the SID chip model (6581/8580) to match what "
            "a .sid file's PSID header requests, remapping to a matching physical "
            "socket or an UltiSID core if needed. 'off' disables. An explicit "
            "'6581'/'8580' forces that model for every chip, ignoring the header.",
            "choices": SID_MODEL_CHOICES,
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
    # Host-DMA double-buffer (page flip) for tear-free bitmap video on backends
    # WITHOUT a usable REU — the TeensyROM, whose slow cycle-clean bus DMA tears
    # a single-buffered mhires frame (the per-cell "sparkle"). The host writes
    # each frame's bitmap+screen into the OFF-screen VIC bank, then a tiny raster
    # IRQ flips $DD00 at vblank, so the visible bank is never written mid-display.
    # Needs no REU (mhires color RAM, the un-banked $D800, still tears briefly —
    # the c3 slot; bitmap+screen go tear-free). Unlike REU staging the IRQ does
    # no in-IRQ DMA, so the flip is shimmer-free and text overlays render crisp.
    #
    # Tri-state — true | false | "auto" (default):
    #   * "auto" enables it for bitmap modes (hires/mhires) when REU staging is
    #     NOT active (mutually exclusive — both flip $DD00) AND the backend has
    #     no REU at all (so this is its only tear-free path). The U64's fast DMA
    #     doesn't visibly tear single-buffered, so auto leaves it on host-DMA.
    #   * true forces it on for bitmap modes (on any backend); false off.
    # Resolved per-scene at build time (config.resolve_double_buffer).
    double_buffer: bool | str = field(
        default="auto",
        metadata={
            "help": "Host-DMA double-buffer (page flip) for tear-free bitmap video where "
            'REU staging can\'t help. "auto" (default) enables it for bitmap modes '
            "(hires/mhires) when REU staging is off and either the backend has no "
            "REU (e.g. TeensyROM) or the scene has a text overlay (whose presence "
            "turns the REU path off to dodge bank-swap shimmer, otherwise leaving "
            "single-buffer host-DMA that tears on cuts). true forces it on for "
            "bitmap modes, false off; gated off when the REU mic pump is active "
            "(shared $0314). Independent of [video].use_reu_staged (the REU path)."
        },
    )


@dataclass
class AudioCfg:
    enabled: bool = field(
        default=True,
        metadata={
            "help": "Master switch for SID audio streaming (the 4-bit $D418 DAC). "
            "On by default; mute with the --no-audio CLI flag."
        },
    )
    device: int = field(
        default=-1, metadata={"help": "Audio input device index; -1 = system default microphone."}
    )
    sample_rate: int = field(
        default=12000,
        metadata={
            "help": "Audio sample rate in Hz fed to the SID DAC. Default 12000 lifts "
            "the Nyquist to ~6.0 kHz so fricatives/sibilants survive (8000 lost them). "
            "HW-verified clean on a real NTSC U64-II via a pitch A/B sweep (no NMI "
            "handler overrun) in both char and bitmap modes, and safe on PAL. Note the "
            "REAL streaming ceiling sits BELOW the isolated-handler ceiling "
            "(max_safe_sample_rate ~13.6 kHz NTSC): the host-DMA audio ring writes "
            "themselves halt the 6510 and steal cycles from the NMI handler, so the "
            "overrun onset under the live pipeline was measured at ~12500 Hz (identical "
            "in char and bitmap — the audio feed, not the video, is the driver). 12000 "
            "keeps margin below that. Rates past the isolated-handler ceiling are "
            "rejected at load (see c64.nmi_rate_safety). Sampler-backend playback uses "
            "[audio].sampler_sample_rate instead."
        },
    )
    # Video-audio backend. The sampler (U64 "Ultimate Audio" FPGA PCM, see
    # sampler.py) plays straight from REU with zero SID/$D418/NMI/CPU, so it is
    # vastly higher fidelity than the 4-bit DAC and immune to the bus-halt
    # problems the DAC fights. "auto" picks it on a sampler-capable U64 when the
    # feature is available (else falls back to the DAC); "dac" forces the lo-fi
    # 4-bit DAC (the only path on TeensyROM); "sampler" forces the sampler and
    # warns+falls-back to the DAC if it isn't available. Resolved per video scene
    # in build_scene via resolve_audio_backend; mic/webcam audio stays on the DAC.
    backend: str = field(
        default="auto",
        metadata={
            "help": "Video-audio backend: 'auto' (sampler on a capable U64, else "
            "DAC), 'dac' (4-bit $D418 NMI DAC, all backends, lo-fi), or 'sampler' "
            "(U64 'Ultimate Audio' FPGA PCM, high fidelity, off the C64 bus).",
            "choices": _AUDIO_BACKEND_CHOICES,
        },
    )
    sampler_sample_rate: int = field(
        default=44100,
        metadata={
            "help": "Sample rate (Hz) for the Ultimate Audio sampler backend. "
            "1000..48000; default 44100 (CD quality). The FPGA plays at the nearest "
            "divider of its 6.25 MHz reference (a <0.5% constant pitch offset, "
            "drift-free)."
        },
    )
    sampler_bits: int = field(
        default=16,
        metadata={
            "help": "PCM bit depth for the Ultimate Audio sampler backend: 8 (signed) "
            "or 16 (signed little-endian). Default 16."
        },
    )
    sampler_clock_hz: int = field(
        default=SAMPLER_REF_CLOCK_DEFAULT,
        metadata={
            "help": "Ultimate Audio sampler reference clock (Hz), used to derive the "
            "rate divider AND the resample target so they stay matched (heard speed = "
            "real_clock / this). Default is the MEASURED effective clock of the shipping "
            "U64 firmware (~6160000 Hz): the FPGA runs ~1.44% slow vs the 6250000 Hz "
            "design nominal, so nominal made sampler audio drift against video. This is a "
            "firmware property (same across U64 units), not per-unit — so it ships baked "
            "in. If a firmware update fixes the clock (or on hardware that clocks it "
            "correctly), set 6250000. Re-measure with scripts/diags/sampler_av_align_calib.py "
            "(prints the value). Only affects the sampler backend."
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
    # Mahoney 8-bit $D418 companding. "auto" (default) picks the best curve for
    # the connected system: a per-unit calibrated table if one exists (see
    # --calibrate-dac), else "mahoney_ultisid" on the Ultimate (its emulated SID
    # is deterministic), else "linear" (a physical/unknown SID with no
    # calibration — the baked emulated table would not match it). "linear" = the
    # classic 4-bit volume-nibble DAC. "mahoney_ultisid" parks the SID voices as
    # DC sources and writes the full $D418 byte per sample (volume + filter-mode
    # + 3-off bits) for ~6-7 effective bits, using a baked table measured on the
    # U64's emulated UltiSID. "calibrated" forces this system's calibrated table
    # (errors if none). Non-linear curves are mutually exclusive with digi_boost.
    # See dac_curves.py, dac_calibration.py + CLAUDE.md.
    dac_curve: str = field(
        default="auto",
        metadata={
            "help": "SID $D418 DAC companding curve. 'auto' (default) = per-system "
            "calibrated table if present, else 'mahoney_ultisid' on the Ultimate, "
            "else 'linear'. 'linear' = classic 4-bit volume nibble. 'mahoney_ultisid' "
            "= Mahoney 8-bit technique (full $D418 byte, ~6-7 effective bits) with the "
            "baked emulated-UltiSID table. 'calibrated' = this system's per-unit table "
            "from --calibrate-dac (errors if none). Non-linear curves require the "
            "Mahoney SID env (auto-installed) and are mutually exclusive with digi_boost.",
            "choices": DAC_CURVE_CHOICES,
        },
    )
    # Overrides system_calibration_key's auto-derived identity (device
    # unique_id / TR USB serial number / legacy host-based fallback) with a
    # user-chosen name. Mainly for a roaming TeensyROM+: it has no config API,
    # so it can't tell which physical SID it's currently plugged into — naming
    # a profile at --calibrate-dac time and passing the same name on every
    # playback run against that host is the only way to keep calibrations
    # straight when the cartridge moves between machines. See dac_calibration.py.
    dac_calibration_profile: str | None = field(
        default=None,
        metadata={
            "help": "Override the auto-derived calibration file key (device unique_id / "
            "TR USB serial) with this name — calibration/dac/profile-<name>.json. Use "
            "when a TeensyROM+ moves between physical C64s: name each host's calibration "
            "once at --calibrate-dac time, then pass the same name on every playback run "
            "against that host."
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
    # Adaptive NMI-rate compensation: a closed loop that RAISES the nominal NMI
    # rate to cancel the video slowdown from bus-halt-stolen NMI ticks. Built
    # when bitmap video cost ~2-14% of ticks — but the bitmap+digi fps cap, the
    # VideoScene frame dedup, and REU-staged double-buffering have since driven
    # that loss to ~0 (HW 2026-07-02: with NO compensation, DAC-path mhires video
    # plays at +0.07% on a near-static clip and -0.01% on a high-motion one). With
    # the loss gone the loop only INJECTS error: its dR/dt R estimator reads ~12%
    # high (torn DMA read-back of the $C025/$C026 read pointer), so it drives the
    # latch the wrong way — measured -8.5% slow on one clip, content-dependent and
    # non-deterministic. So DEFAULT OFF: playing at the nominal latch is dead-on
    # (host_dma_servo still centers the ring — that's orthogonal to pitch). Kept
    # as a knob for platforms that may still lose ticks (PAL, TeensyROM+), where
    # the estimator bias would need fixing first. See the nmi_adaptive_rate_obsolete
    # note + scripts/diags/nmi_pitch_ab.py.
    nmi_rate_adaptive: bool = field(
        default=False,
        metadata={
            "help": "Adaptive NMI-rate compensation: closed-loop on the measured "
            "C64 consumer rate, raises the NMI rate to cancel a video slowdown "
            "from bus-halt-stolen NMI ticks. DEFAULT OFF — modern fps caps + "
            "REU-staged double-buffer drove that loss to ~0, so this only adds "
            "pitch error now. Supersedes pitch_mult_* when on. Host-DMA path only."
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
    # ---- host-DMA servo pitch compensation (static; per-mode) ---------------
    # These STATIC per-mode playback-rate multipliers apply only when
    # nmi_rate_adaptive = false (now the default). Each cancels the video
    # slowdown from bus-halt-stolen NMI ticks for one display mode: >1.0 speeds
    # playback up, 1.0 = no change. The AudioStreamer converts a multiplier to a
    # shorter CIA #2 Timer A period (faster NMI → faster R; rate and latch are
    # inversely related). `hires_edges` scenes use pitch_mult_hires (same VIC
    # fetch).
    #
    # ALL DEFAULT 1.0 (no compensation). The earlier bitmap defaults (hires 1.02,
    # mhires 1.015) were ear-tuned when bitmap video cost ~2% of NMI ticks — but
    # the bitmap+digi fps cap + REU-staged double-buffer since drove that loss to
    # ~0 (HW 2026-07-02: DAC-path mhires video plays at +0.07% PITCH with NO
    # compensation; 1.015 now overcorrects to +1.36% HIGH). So the modern U64-II
    # NTSC platform wants no static PITCH compensation. Re-tune per system ONLY if
    # a platform actually shows pitch drift (PAL @ 50fps, or the lower-latency TR+
    # backend, may differ — measure with scripts/diags/nmi_pitch_ab.py).
    #
    # NOTE: that "+0.07%" measurement was PITCH only (a pure-tone frequency read),
    # and is correct. It is TEMPO-BLIND: on the host-DMA DAC path over a bitmap
    # mode the content still plays ~12% SLOW at that correct pitch (the servo
    # under-drains the ring). Tempo is fixed SEPARATELY by dac_bitmap_tempo_*
    # below (time-domain pre-compression), not by these NMI-rate multipliers.
    pitch_mult_petscii: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for PETSCII mode "
            "(light char-mode load). 1.0 = none (default; U64-II NTSC is dead-on)."
        },
    )
    pitch_mult_hires: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for Hires / Hires-edges "
            "modes. 1.0 = none (default; modern fps caps + REU staging leave ~0 "
            "loss on U64-II NTSC). Re-tune only if a platform (PAL/TR+) drifts."
        },
    )
    pitch_mult_mhires: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for MultiHires mode. "
            "1.0 = none (default; modern fps caps + REU staging leave ~0 loss on "
            "U64-II NTSC). Re-tune only if a platform (PAL/TR+) drifts."
        },
    )
    pitch_mult_mcm: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for MCM mode "
            "(char-based, light load; U64-II NTSC: good at 1.0)."
        },
    )
    pitch_mult_blank: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for Blank mode "
            "(no video input; 1.0 = none)."
        },
    )
    # ---- bitmap + $D418-DAC tempo compensation (static; per-mode) -----------
    # ORTHOGONAL to pitch_mult_* (which shorten the C64 NMI rate to fix PITCH).
    # These fix TEMPO on the host-DMA 4-bit DAC path over a BITMAP display mode
    # only. There, the audio worker shares the single socket-DMA link with heavy
    # REU bank-swap bitmap writes; the host-DMA servo reads the ring pointer
    # biased under that load and throttles the worker ~12%, so video (slaved to
    # the drain clock) + audio play ~1/value SLOW at CORRECT pitch (the $D418
    # output rate stays ≈ sample_rate — a pitch-preserving time stretch, the ring
    # under-fills and the NMI re-reads samples). The fix pre-compresses the
    # content in the time domain by 1/value (audio time-compressed pitch-
    # preserving via atempo; video PTS × value) so the system's own ~1/value
    # stretch lands both at real time, in sync, pitch intact. `hires_edges`
    # scenes use dac_bitmap_tempo_hires (same VIC fetch as hires). No effect on
    # the off-bus Ultimate Audio sampler (the U64 video default), the REU pump,
    # or char modes (petscii/mcm/blank) — those stay at real time already.
    #
    # Default 0.88 = the measured U64-II NTSC mhires speed fraction (clock/wall).
    # Other platforms (U64+PAL, U2P, TR+ PAL/NTSC) have different fractions —
    # measure per platform with scripts/diags/mhires_tempo_clock_ab.py and set
    # here. 1.0 = compensation off.
    dac_bitmap_tempo_hires: float = field(
        default=0.89,
        metadata={
            "help": "Observed $D418-DAC playback-speed fraction on Hires / "
            "Hires-edges bitmap modes (measure via clock/wall). Content is "
            "time-compressed by 1/value (pitch-preserving) so bitmap+DAC video "
            "plays at real time. 1.0 = off. Host-DMA DAC path only — no effect "
            "on the Ultimate Audio sampler or the REU pump. Default 0.89 = "
            "U64-II NTSC (Hires drains slightly faster than MHires); re-measure "
            "per platform (PAL / TR+)."
        },
    )
    dac_bitmap_tempo_mhires: float = field(
        default=0.88,
        metadata={
            "help": "Observed $D418-DAC playback-speed fraction on MultiHires "
            "bitmap mode (measure via clock/wall). Content is time-compressed by "
            "1/value (pitch-preserving) so bitmap+DAC video plays at real time. "
            "1.0 = off. Host-DMA DAC path only — no effect on the Ultimate Audio "
            "sampler or the REU pump. Default 0.88 = U64-II NTSC; re-measure per "
            "platform (PAL / TR+)."
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
        default=False,
        metadata={
            "help": "Insert a video from videos_dir after each scene (multi-scene playlists "
            "only; ignored in single-scene mode)."
        },
    )
    songlengths_file: str | None = field(
        default=None,
        metadata={
            "help": "Path to an HVSC Songlengths.md5 file; gives waveform scenes their "
            "true duration when duration_s is unset. Left unset (the default), an "
            "unpacked HVSC under assets/sids/ (either the whole C64Music/ tree or "
            "just its contents) is auto-detected. Set to an empty string to disable "
            "auto-detection."
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
    fade_duration_s: float = field(
        default=0.4,
        metadata={
            "help": "Fade-in/out duration (seconds) at scene setup/teardown: non-black "
            "pixels rise from black on entry and sink to black on a normal scene end, "
            "across every compose-based display mode. 0 disables (hard cuts). A CTRL "
            "skip aborts an in-progress fade immediately."
        },
    )


@dataclass
class SceneCfg:
    type: str = field(default="webcam", metadata={"help": "Scene kind.", "choices": SCENE_TYPES})
    display: str | None = field(
        default=None,
        metadata={
            "help": "VIC-II display mode. Unset resolves per scene type: 'mhires' "
            "for video (richest bitmap mode, suits arbitrary film/photo content) "
            "and 'hires_edges' for webcam/blank/slideshow/generative (tuned for "
            "live Canny-edge stylization). waveform and midi are bitmap-only "
            "(both ignore this); slideshow also accepts 'random'. generative "
            "renders a frame so any quantizing mode works (not 'blank'/'random').",
            "choices": _DISPLAY_CHOICES,
            "applies_to": ("webcam", "blank", "video", "slideshow", "generative", "wled"),
        },
    )
    name: str | None = field(
        default=None,
        metadata={"help": "Display name (shown in interstitials/logs; ensemble match key)."},
    )
    # None = scene-type default: webcam/blank run forever in a single-scene
    # playlist (else 30s so a rotation still advances), songlengths-or-30s for
    # waveform/midi, 30s for slideshow/generative. 0 = run forever (any type).
    # Video scenes reject any value (video-driven).
    duration_s: float | None = field(
        default=None,
        metadata={
            "help": "Seconds before auto-advance; 0 = run forever. Unset = "
            "scene-type default (webcam/blank run forever when they're the "
            "only scene, else 30s; waveform = song length or 30s; "
            "slideshow/generative = 30s). "
            "Video scenes reject this (they run until the file ends). "
            "For launcher this is the idle timeout (reset by player input).",
            "applies_to": (
                "webcam",
                "blank",
                "waveform",
                "midi",
                "asid",
                "slideshow",
                "launcher",
                "generative",
                "wled",
            ),
            "apply": "live",
        },
    )
    # See resolve_file_spec for the comma-separated path/dir/glob grammar.
    file: str | None = field(
        default=None,
        metadata={
            "help": "Asset spec (comma-separated paths/dirs/globs). Videos for "
            "video, .sid for waveform, images for slideshow, "
            ".prg/.crt for launcher, .sid for generative when "
            "audio_source = sid.",
            "applies_to": ("video", "waveform", "slideshow", "launcher", "generative"),
        },
    )
    # Start offset for video playback. Quick playback (`c64cast MEDIA…`) fills
    # this from a URL's t=/start= timestamp; it can also be set directly on a
    # [[scenes]] video. Honored by VideoScene -> AVFileSource (container seek to
    # the keyframe at/just-before this time). Video-only; rejected elsewhere.
    start_s: float | None = field(
        default=None,
        metadata={
            "help": "Seconds into the source to begin playback (video only). "
            "Quick playback fills this from a URL's t=/start= timestamp; "
            "can also be set directly on a [[scenes]] video. "
            "Unset/0 = play from the start.",
            "applies_to": ("video",),
        },
    )
    image_duration_s: float = field(
        default=5.0,
        metadata={
            "help": "Per-image dwell time before advancing (total runtime is duration_s).",
            "applies_to": ("slideshow",),
        },
    )
    aspect_mode: str = field(
        default="crop",
        metadata={
            "help": "How each image is fit to the C64 4:2.5 aspect: 'crop' "
            "(center-crop to fill — the default, edges lost), 'fit' "
            "(letterbox/pillarbox so the whole image shows, padded black), or "
            "'stretch' (distort to fill, no padding or cropping).",
            "choices": _ASPECT_MODE_CHOICES,
            "applies_to": ("slideshow",),
        },
    )
    target_fps: float | None = field(
        default=None,
        metadata={
            "help": "Per-scene frame-rate cap; unset = playlist default (60/50). "
            "Bitmap (hires/mhires) video/webcam/generative scenes default "
            "lower to stay under the DMA bus-halt ceiling: 20 fps while "
            "streaming digitized audio, else half rate (30/25). "
            "Waveform/midi/asid default to half rate too.",
            "apply": "live",
        },
    )
    # None = follow global [audio].enabled; False forces off; True is a no-op
    # when the global is off. waveform/midi ignore this (they drive the SID).
    audio: bool | None = field(
        default=None,
        metadata={
            "help": "Per-scene audio override. Unset follows [audio].enabled; "
            "false mutes this scene only.",
            "applies_to": ("webcam", "blank", "video", "generative"),
        },
    )
    # Generative scene: which procedural video source to render.
    source: str = field(
        default="plasma",
        metadata={
            "help": "Generative video source to render (generative scenes only).",
            "choices": _GENERATIVE_SOURCE_CHOICES,
            "applies_to": ("generative",),
        },
    )
    # Generative scene: the audio building block paired with the video source.
    audio_source: str = field(
        default="mic",
        metadata={
            "help": "Audio for a generative scene: 'none' = silent; 'mic' = live "
            "mic (only when [audio].enabled); 'sid' = play the `file` .sid on "
            "the real chip. Default 'mic' matches pre-field behavior. A SID "
            "source forces a host-DMA display and needs a char display "
            "(petscii/mcm) for most tunes (see `file`).",
            "choices": _AUDIO_SOURCE_CHOICES,
            "applies_to": ("generative",),
        },
    )
    # Generative scene: drive the visuals from the music. Only takes effect with
    # audio_source = sid today (a host-side SID emulator supplies the features);
    # inert for mic/none (no feature stream yet).
    reactive: bool = field(
        default=True,
        metadata={
            "help": "Generative scene: let the music drive the visuals — BPM "
            "cycles the colors, transients pulse them. Only takes effect with "
            "audio_source = 'sid' (a host-side SID emulator supplies the "
            "features, adding no U64 traffic); inert for mic/none. Set false to "
            "keep the pure time-driven look.",
            "applies_to": ("generative",),
        },
    )
    # WLED pixel-sink scene: the virtual LED-matrix dimensions a sender streams
    # to. The display mode downscales this to the C64 grid, so it only sets how
    # many pixels the sink expects — it MUST match the sender's configured
    # matrix (a WLED-ecosystem sender is set up for a specific pixel count).
    sink_width: int = field(
        default=320,
        metadata={
            "help": "WLED sink: virtual LED-matrix width in pixels a sender "
            "streams to (wled scenes only). Must match the sender's configured "
            "matrix; the display mode downscales it to the C64. Default 320.",
            "applies_to": ("wled",),
        },
    )
    sink_height: int = field(
        default=200,
        metadata={
            "help": "WLED sink: virtual LED-matrix height in pixels a sender "
            "streams to (wled scenes only). Must match the sender's configured "
            "matrix; the display mode downscales it to the C64. Default 200.",
            "applies_to": ("wled",),
        },
    )
    # Per-scene pixel effect applied to the source frame before quantization.
    effect: str | None = field(
        default=None,
        metadata={
            "help": "Pixel effect applied to the frame before quantization "
            "(unset = none). Works on any frame-bearing scene. 'trails' echoes "
            "moving content; 'pulse' beat-punches the zoom; 'rgb_shift' slews "
            "the color channels apart on a transient. pulse/rgb_shift only "
            "visibly react on a music-reactive scene (generative + audio_source "
            "= 'sid'); elsewhere they're inert (no feature stream to react to).",
            "choices": _EFFECT_CHOICES,
            "applies_to": ("webcam", "video", "slideshow", "generative", "wled"),
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
            "applies_to": ("webcam", "blank", "video", "generative"),
        },
    )
    # waveform-specific kwargs — passed straight through to WaveformScene.
    song: int = field(
        default=0,
        metadata={
            "help": "SID subtune index to play (0 = the SID's default; 1-based "
            "otherwise). For generative scenes, only with audio_source = sid.",
            "applies_to": ("waveform", "generative"),
        },
    )
    color_mode: str = field(
        default="per_voice",
        metadata={
            "help": "Oscilloscope coloring: fixed per voice, or by current waveform type.",
            "choices": _COLOR_MODE_CHOICES,
            "applies_to": ("waveform", "midi", "asid"),
        },
    )
    voice_colors: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Per-voice trace colors (C64 color names) for color_mode=per_voice.",
            "applies_to": ("waveform", "midi", "asid"),
        },
    )
    waveform_colors: dict[str, str] = field(
        default_factory=dict,
        metadata={
            "help": "Per-waveform-type colors (e.g. pulse=cyan) for color_mode=per_waveform.",
            "applies_to": ("waveform", "midi", "asid"),
        },
    )
    time_base: str = field(
        default="wallclock",
        metadata={
            "help": "Scope time window: 'wallclock' (1 row = 1 frame) or 'auto' "
            "(per-voice window sized so auto_cycles cycles fit).",
            "choices": _TIME_BASE_CHOICES,
            "applies_to": ("waveform", "midi", "asid"),
        },
    )
    auto_cycles: float = field(
        default=4.0,
        metadata={
            "help": "Complete cycles per render window when time_base = 'auto'.",
            "applies_to": ("waveform", "midi", "asid"),
        },
    )
    persistence: str = field(
        default="off",
        metadata={
            "help": "Trace decay/trail length ('off' redraws each frame).",
            "choices": _PERSISTENCE_CHOICES,
            "applies_to": ("waveform", "midi", "asid"),
        },
    )
    # Scalar broadcasts to all 3 voices; a list of 3 assigns per voice.
    scroll_columns: int | list[int] = field(
        default=0,
        metadata={
            "help": "FIFO-scroll the strip left by N columns/frame (0 = redraw). "
            "Int or a list of 3 per-voice ints.",
            "applies_to": ("waveform", "midi", "asid"),
        },
    )
    # ASID scene kwargs.
    asid_port: str | None = field(
        default=None,
        metadata={
            "help": "MIDI input port name substring the ASID host streams to; "
            "unset = first available port.",
            "applies_to": ("asid",),
        },
    )
    asid_multi_sid: bool = field(
        default=True,
        metadata={
            "help": "Honor ASID multi-SID streams (commands 0x50-0x5F) by "
            "configuring the U64 for multiple SIDs and routing each chip to its "
            "own address (prefers physical socket SIDs). U64 only — ignored on "
            "backends without the config API, where extra chips downmix to the "
            "primary SID.",
            "applies_to": ("asid",),
        },
    )
    asid_max_sids: int = field(
        default=8,
        metadata={
            "help": "Cap on the number of SID chips a multi-SID ASID stream may "
            "map on the U64 (1-8). Chips beyond the cap downmix to the primary "
            "SID.",
            "applies_to": ("asid",),
        },
    )
    asid_buffered_player: str = field(
        default="auto",
        metadata={
            "help": "Cycle-accurate buffered playback: consume ASID frames on a "
            "C64-side REU ring player (CIA #1 Timer A IRQ) instead of coalescing "
            "block writes on the host. Fixes dropped frames on multispeed tunes "
            "(0x31 up to 16x) — arps/vibrato/hard restarts survive — and honors "
            "the 0x30 write-order/wait recipe. U64 only (needs a bus-clean REU): "
            "'auto' = on when the backend has an REU, else the coalesced path; "
            "'on' = force it (warns + falls back on a no-REU backend); 'off' = "
            "always coalesce.",
            "choices": ("auto", "on", "off"),
            "applies_to": ("asid",),
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
            "help": "Default SID waveform for MIDI notes (the starting waveform "
            "for every voice; SHIFT cycles it, incl. into combined waveforms).",
            "choices": _MIDI_WAVEFORM_CHOICES,
            "applies_to": ("midi",),
        },
    )
    midi_voice_waveforms: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Per-voice starting waveforms (up to 3, e.g. "
            "['pulse', 'sawtooth', 'triangle']). Each entry is one waveform or a "
            "'+'-combo. 'pulse+triangle' is the combined wave that reliably sounds "
            "on a 6581; sawtooth combos AND down to near-silence there (audible "
            "may differ on 8580). Empty = every voice uses midi_waveform; fewer "
            "than 3 repeats the last.",
            "applies_to": ("midi",),
        },
    )
    midi_voice_mode: str = field(
        default="shared",
        metadata={
            "help": "Voice allocation: 'shared' = one MIDI channel spread across "
            "the 3 voices (mono melody over a sustain pad); 'multitimbral' = MIDI "
            "channels route to fixed voices (see midi_voice_channels).",
            "choices": _MIDI_VOICE_MODE_CHOICES,
            "applies_to": ("midi",),
        },
    )
    midi_voice_channels: list[int] = field(
        default_factory=lambda: [1, 2, 3],
        metadata={
            "help": "Multitimbral channel→voice map: MIDI channels (1..16) for "
            "voices 1/2/3, in order. Only used when midi_voice_mode = "
            "'multitimbral'; notes on other channels are ignored.",
            "applies_to": ("midi",),
        },
    )
    midi_program_change: bool = field(
        default=True,
        metadata={
            "help": "Honor MIDI Program Change to select a voice's waveform "
            "(shared mode = all voices; multitimbral = the message's channel).",
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
            "applies_to": ("webcam", "video", "slideshow", "generative", "wled"),
            "apply": "live",
        },
    )
    text_double_height: bool = field(
        default=False,
        metadata={
            "help": "On mhires, render text overlays (clock/marquee/…) at double "
            "height — 16px / 2 cell rows — for across-the-room legibility. "
            "Text is always double-WIDE on mhires (8x8 glyph spans 2 of the "
            "4px cells); this toggle adds the vertical stretch. Ignored on "
            "other display modes.",
            "applies_to": ("webcam", "video", "slideshow", "generative", "wled"),
        },
    )
    style: str = field(
        default="default",
        metadata={
            "help": "PETSCII glyph/color style (only when display = 'petscii'); "
            "'random' picks one at setup.",
            "choices": _STYLE_CHOICES,
            "applies_to": ("webcam", "video", "slideshow", "generative", "wled"),
            "apply": "live",
        },
    )
    border: int | str = field(
        default=0,
        metadata={
            "help": "Border color (blank scenes): a C64 color name (fuzzy + "
            'case-insensitive, e.g. "light blue") or a palette index 0..15.',
            "applies_to": ("blank",),
        },
    )
    background: int | str = field(
        default=0,
        metadata={
            "help": "Background color (blank scenes): a C64 color name (fuzzy + "
            'case-insensitive, e.g. "light blue") or a palette index 0..15.',
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
    force_palette_colors: int | list[int | str] = field(
        default=16,
        metadata={
            "help": "How force_palette allocates C64 colors: either an int count "
            "of distinct colors to spread the source across (2..16), OR an "
            "explicit list of colors to whitelist — each a color name (fuzzy + "
            'case-insensitive, e.g. "light blue", "lgrn", "blk") or an '
            "index 0..15. A list's length sets the color count."
        },
    )
    dither: str = field(
        default="auto",
        metadata={
            "help": "Spatial dither applied before nearest-palette quantization "
            "on mhires/mcm/hires. 'auto' picks the best method that's actually "
            "useful for the scene: floyd-steinberg (highest quality) for static "
            "scenes (slideshow), blue_noise (vectorized, temporally stable — no "
            "added shimmer, and no Bayer grid structure) for motion scenes "
            "(video/webcam/generative). Any value can be forced on any scene; "
            "floyd-steinberg/atkinson are a Python-level per-pixel loop and can "
            "shimmer frame-to-frame on motion; 'ordered' (Bayer) is the older "
            "motion default and still available if the cross-hatch pattern is "
            "wanted (see docs/caveats.md).",
            "choices": ("auto",) + DITHER_METHODS,
        },
    )
    dither_strength: float = field(
        default=0.5,
        metadata={
            "help": "Dither strength, roughly 0..2.0. For 'ordered'/'blue_noise' "
            "it scales the threshold spread (same scale for both, so switching "
            "between them doesn't need a strength retune); for "
            "floyd-steinberg/atkinson it scales how much of each pixel's "
            "quantization error is diffused to its neighbors (1.0 = the "
            "textbook kernel weights)."
        },
    )
    color_match: str = field(
        default="auto",
        metadata={
            "help": "Color space for the nearest-palette match on the quantizing "
            "modes (mcm/mhires/hires/petscii). 'perceptual' measures nearest-color "
            "in CIE-Lab (perceptually uniform — picks the color the eye calls "
            "closest, e.g. a warm gray → orange/brown, not muddy gray). 'rgb' is "
            "the classic brightness-weighted BGR metric. Both keep the "
            "channel_boost + gray-penalty shaping; only the distance space "
            "differs. 'auto' (default) picks perceptual on every quantizing mode "
            "(a no-op on hires edges / blank, which pick no colors).",
            "choices": ("auto",) + COLOR_MATCH_MODES,
        },
    )
    cell_strategy: str = field(
        default="auto",
        metadata={
            "help": "How mhires percell mode fills each 4×8 cell's 3 per-cell "
            "color slots from the colors present in that cell. 'frequency' = the "
            "3 most-common (temporally stable). 'luminance' = darkest/median/"
            "brightest (preserves a cell's full tonal span). 'contrast' = the two "
            "luma extremes plus the color farthest from both. 'error-min' = the "
            "trio minimizing the cell's reconstruction error (best quality, "
            "costlier). 'auto' (default) uses error-min for static scenes "
            "(slideshow — composed once) and frequency for motion scenes "
            "(video/webcam/generative, where frequency's stability avoids "
            "per-frame slot churn). Only affects mhires with palette_mode=percell.",
            "choices": ("auto",) + CELL_STRATEGIES,
        },
    )
    motion_smoothing: float = field(
        default=0.25,
        metadata={
            "help": "Temporal smoothing for mhires percell mode, 0..1. The percell "
            "path smooths its per-cell color choices over time (an EMA over color "
            "counts plus per-pixel/per-cell decision hysteresis) to suppress "
            "frame-to-frame flicker on noisy video. That smoothing trades "
            "motion-tracking for stability, so on a hard shot cut an outline from "
            "the previous shot lingers as an after-image for a moment. 1.0 (full "
            "smoothing) is the most stable but ghostiest; 0.0 tracks the source "
            "exactly (no after-image) but can flicker on grainy content. The "
            "default 0.25 was picked by hardware A/B as the best ghost/flicker "
            "balance. Lower it if after-images still bother you, raise it if "
            "motion shimmers. No effect on other modes or palette_modes.",
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


_MIDI_CC_TYPE_CHOICES = ("cc", "note", "pc")
_MIDI_ACTION_CHOICES = (
    "pause",
    "resume",
    "toggle_pause",
    "skip",
    "cycle_style",
    "jump",
    "param",
)

# Shipped out of the box so MIDI control works with no config edits, per a
# typical 16-pad-grid + knob-bank live controller (Launch Control XL / APC
# style). See midi_control.py's module docstring for the full mapping
# rationale; kept here (not imported from midi_control.py) so config stays
# import-light, same rationale as the DAC_CURVE_CHOICES-style constants above.
_DEFAULT_MIDI_CC_MAP: tuple[dict[str, Any], ...] = (
    {"type": "note", "number": 36, "action": "skip"},
    {"type": "note", "number": 37, "action": "cycle_style"},
    {"type": "note", "number": 38, "action": "toggle_pause"},
    {"type": "note", "number": 39, "action": "jump", "scene": 0},  # "home"/panic
    # Scene-jump bank: notes 40-55 -> scenes 0-15 (a 16-pad grid row/block),
    # and the same bank via Program Change for foot-controller performers.
    *({"type": "note", "number": 40 + i, "action": "jump", "scene": i} for i in range(16)),
    *({"type": "pc", "number": i, "action": "jump", "scene": i} for i in range(16)),
    # Knob bank: deliberately clear of MidiScene's CC1/7/71-75 synth-control
    # range, in case a shared controller feeds both via a virtual MIDI Thru.
    # A CC mapped to a scene whose current effect/source doesn't declare that
    # LIVE_PARAM is a silent no-op — safe to leave mapped across any playlist.
    {"type": "cc", "number": 13, "action": "param", "target": "effect.decay"},
    {"type": "cc", "number": 14, "action": "param", "target": "source.speed"},
    {"type": "cc", "number": 15, "action": "param", "target": "source.scale"},
    {"type": "cc", "number": 16, "action": "param", "target": "source.scroll_speed"},
)


@dataclass
class MidiControlCfg:
    """Process-wide MIDI control surface for live performance: scene jumps,
    style cycling, transport, and live effect/generator parameter sweeps
    from a MIDI controller. Off by default; requires the `midi` extra.

    Opens its OWN mido.open_input() — a separate port from any MidiScene's,
    even if both read the same physical controller via OS-level MIDI
    routing (mido ports are exclusive opens). One listener governs the
    whole ensemble (mirrors [control]): MIDI channel selects which system a
    message targets, so a performer retargets with a controller-side
    channel switch instead of a config/menu round trip."""

    enabled: bool = field(
        default=False,
        metadata={"help": "Run the MIDI control listener; requires the 'midi' extra."},
    )
    port: str | None = field(
        default=None,
        metadata={
            "help": "MIDI input port name (substring match, case-insensitive). "
            "None = first available port."
        },
    )
    broadcast_channel: int = field(
        default=16,
        metadata={
            "help": "1-based MIDI channel that targets every system at once in "
            "ensemble mode. Other channels 1..N target the Nth system in "
            "ensemble order. Ignored in single-system mode (the one playlist "
            "is always the target)."
        },
    )
    jump_transition: str = field(
        default="cut",
        metadata={
            "help": "How a 'jump' action changes scenes: 'cut' (instant, no "
            "interstitial — the live-performance default) or 'interstitial' "
            "(routes through the normal UP-NEXT card).",
            "choices": ("cut", "interstitial"),
        },
    )
    cc_map: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(d) for d in _DEFAULT_MIDI_CC_MAP],
        metadata={
            "help": "MIDI-message -> action mappings ([[midi_control.cc_map]] "
            "tables); see --describe section:midi_control. Set to [] to disable "
            "the shipped defaults, or override/extend individual entries. Each "
            "entry: type ('cc'|'note'|'pc'), number (0-127), action "
            "('pause'|'resume'|'toggle_pause'|'skip'|'cycle_style'|'jump'|"
            "'param'); 'jump' also needs an int scene; 'param' also needs a "
            "string target ('effect.<name>' or 'source.<name>', matching a "
            "LIVE_PARAMS entry on the current scene's effect/generator)."
        },
    )


@dataclass
class MenuCfg:
    """On-C64 menu. When enabled, SPACE on the C64 keyboard opens an on-screen
    panel of context-sensitive knobs for the current scene (palette mode, style,
    forced palette, etc.) with a live preview; cursor keys navigate, RETURN
    saves. Needs a backend that can read C64 memory; a no-op on a read-free
    backend (an older TeensyROM firmware without ReadC64Mem). The Ultimate and
    cycle-clean TR+ (fw v0.7.2.5+) both read."""

    enabled: bool = field(
        default=False,
        metadata={"help": "Enable the on-C64 SPACE-key menu for live scene tweaks."},
    )
    prompt_to_save: bool = field(
        default=True,
        metadata={
            "help": "On menu exit with unsaved changes, offer to write them back to "
            "the source config file. False = apply to the running scene only, never "
            "persist (handy for conventions/demos)."
        },
    )


@dataclass
class WledCfg:
    """Two-directional bridge to the WLED LED-controller ecosystem.

    **Mode 3 — broadcast** (`broadcast`): whichever SID-driven scene is on
    screen (waveform, or a generative scene with audio_source = "sid") is turned
    into a WLED Audio Sync V2 stream and multicast on the LAN, so real WLED LED
    matrices/strips react to the music with no microphone on the WLED side (set
    Sound Sync = "Receive" on the target WLED). Pure UDP; no extra dependency.

    **Mode 1 — listen** (`listen`): c64cast advertises itself as a virtual WLED
    device (mDNS `_wled._tcp`) and serves a subset of the WLED JSON API, so the
    WLED mobile app / python-wled / Home Assistant can discover and control it —
    WLED effects ↔ scenes, on/off + brightness ↔ transport, sliders ↔ live scene
    params. Requires the `wled` extra (zeroconf + fastapi + uvicorn).

    `broadcast` and `listen` each combine on/off **and** endpoint in one value:
    `"disabled"` (or unset) = off; `"enabled"` = on with that mode's default
    host+port; `"[host][:port]"` = on with overrides (a bare `"HOST"` sets the
    host, a leading `":PORT"` sets only the port). Broadcast defaults to the WLED
    multicast group 239.0.0.1:11988; listen defaults to 0.0.0.0:8080."""

    broadcast: str | None = field(
        default=None,
        metadata={
            "help": "Mode 3 (audio-sync out). 'disabled' (default) | 'enabled' | "
            "'[host][:port]'. 'enabled' multicasts to WLED's default group "
            "239.0.0.1:11988 (every WLED with 'Receive' enabled reacts); give a "
            "unicast '[host][:port]' to target one device."
        },
    )
    rate_hz: float = field(
        default=50.0,
        metadata={
            "help": "Broadcast rate in Hz (Mode 3). WLED expects roughly "
            "frame-rate updates; ~40-60 is typical."
        },
    )
    listen: str | None = field(
        default=None,
        metadata={
            "help": "Mode 1 (control surface in). 'disabled' (default) | 'enabled' "
            "| '[host][:port]'. 'enabled' binds the WLED JSON API on "
            "0.0.0.0:8080; override the bind with '[host][:port]'. Needs the "
            "'wled' extra."
        },
    )
    name: str = field(
        default="c64cast",
        metadata={
            "help": "Friendly/mDNS device name advertised in Mode 1 (what the WLED "
            "app shows for this virtual device)."
        },
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
    midi_control: MidiControlCfg = field(default_factory=MidiControlCfg)
    menu: MenuCfg = field(default_factory=MenuCfg)
    wled: WledCfg = field(default_factory=WledCfg)
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


def _validate_double_buffer(video: VideoCfg) -> None:
    """The tri-state [video].double_buffer accepts only a bool or the literal
    string "auto" — same shape as use_reu_staged. Catch a typo at load time."""
    v = video.double_buffer
    if isinstance(v, bool):
        return
    if v != "auto":
        raise ValueError(f'[video].double_buffer must be true, false, or "auto", got {v!r}')


def _validate_force_palette(color: ColorCfg) -> None:
    """Range-check + normalize the [color].force_palette_colors knob at
    load/doctor time so a bad value surfaces before the playlist runs, not
    mid-stream at pre-scan. A list of color names/indices is resolved and
    written back as a canonical list[int] (so serialization stays stable)."""
    fp = color.force_palette_colors
    if isinstance(fp, list):
        if not (2 <= len(fp) <= 16):
            raise ValueError(
                f"color.force_palette_colors list must have 2..16 entries, got {len(fp)}"
            )
        try:
            color.force_palette_colors = [resolve_color(c) for c in fp]
        except ValueError as e:
            raise ValueError(f"color.force_palette_colors: {e}") from e
    elif isinstance(fp, bool) or not isinstance(fp, int):
        raise ValueError(
            f"color.force_palette_colors must be an int (2..16) or a list of colors, got {fp!r}"
        )
    elif not (2 <= fp <= 16):
        raise ValueError(f"color.force_palette_colors must be in 2..16, got {fp}")


def resolved_force_palette(color: ColorCfg) -> tuple[int, list[int] | None]:
    """Derive the (n_colors, indices) pair the color-map accumulator wants from
    the unified force_palette_colors field (validated/normalized by
    _validate_force_palette): a list -> (len, list); an int -> (count, None)."""
    fp = color.force_palette_colors
    if isinstance(fp, list):
        idx = [int(c) for c in fp]
        return len(idx), idx
    return int(fp), None


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
        ("midi_control", cfg.midi_control),
        ("menu", cfg.menu),
        ("wled", cfg.wled),
    ):
        if section in data:
            _apply_section(dc, data[section], section)

    _validate_use_reu_staged(cfg.video)
    _validate_double_buffer(cfg.video)

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
    ("menu", frozenset()),
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
    single-system mode this is just the loaded config's [control]).
    `master_midi_control` is the [midi_control] analog — also process-wide,
    not per-system-cascaded (see _CASCADE_SECTIONS)."""

    cfgs: list[Config]
    names: list[str]
    paths: list[str | None]
    is_ensemble: bool
    master_control: ControlPlaneCfg
    master_midi_control: MidiControlCfg


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
                master_midi_control=cfg.midi_control,
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
            master_midi_control=cfg.midi_control,
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
        ("midi_control", defaults.midi_control),
        ("menu", defaults.menu),
    ):
        if section in raw:
            _apply_section(dc, raw[section], section)

    _validate_use_reu_staged(defaults.video)
    _validate_double_buffer(defaults.video)

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
        master_midi_control=defaults.midi_control,
    )


_AUDIO_BEARING_SCENE_TYPES = frozenset({"video", "waveform", "midi", "asid", "launcher"})


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
# merge_cli to know which CLI flags map onto which config fields. The
# connection fields ([hardware].backend, [ultimate64].url/dma_port,
# [teensyrom].*) are deliberately absent: they come from the scheme-aware
# -u/--url target (see connect.py), applied separately so the URI can pick the
# backend + transport in one string instead of a fan of flags.
CLI_TO_CFG = {
    "system": ("ultimate64", "system"),
    "sid_model": ("ultimate64", "sid_model"),
    "device": ("video", "device"),
    "audio": ("audio", "enabled"),
    "audio_device": ("audio", "device"),
    "sample_rate": ("audio", "sample_rate"),
    "mic_sensitivity": ("audio", "mic_sensitivity"),
    "noise_gate": ("audio", "noise_gate"),
    "dac_calibration_profile": ("audio", "dac_calibration_profile"),
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
    scene's display mode (the host-DMA page-flip path — see modes.py
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
) -> DisplayMode:
    # border/background may be a C64 color name or an index; resolve to a plain
    # index here — the single point every scene's border/background flows
    # through — so the mode constructors (and callers) only ever see an int.
    border = resolve_color(border)
    background = resolve_color(background)
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
    dither_strength = color.dither_strength
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
        )
    if name == "petscii":
        return PETSCIIDisplayMode(
            style=style,
            use_reu_staged=use_reu_staged,
            channel_boost=channel_boost,
            hue_corrections=hue_corrections,
            hue_corrections_replace=hue_corrections_replace,
            perceptual=perceptual,
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
    from .overlays import paints_into_buffers

    display = resolve_scene_display(display, s.type)
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
    return _build_display_mode(
        display,
        palette_mode=s.palette_mode,
        border=s.border,
        background=s.background,
        style=s.style,
        use_reu_staged=use_reu_staged,
        double_buffer=double_buffer,
        audio_reu_pump_active=cfg.audio.use_reu_pump,
        color=cfg.color,
        text_double_height=s.text_double_height,
        dither_method=resolve_dither_method(cfg.color.dither, s.type),
        cell_strategy=resolve_cell_strategy(cfg.color.cell_strategy, s.type),
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
        from .quickcast import _ytdlp_available, url_needs_ytdlp

        if url_needs_ytdlp(s.file.strip()) and not _ytdlp_available():
            raise ValueError(
                f"video: {s.file!r} is a URL that needs yt-dlp to resolve, but the "
                "`yt` extra isn't installed. Install it (`uv sync --extra yt`, or "
                "`pip install c64cast[yt]`), or use a direct media URL / local file."
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
    # field is ignored. Synthesise a hires display_mode so overlay
    # compatibility validates against what the scene will actually paint
    # (and PETSCII overlays are rejected, as on a waveform scene).
    return _build_display_mode("hires")


def _validate_asid(s: SceneCfg) -> DisplayMode:
    # AsidScene carries the SID state in the stream, so it has no synth knobs
    # to validate — only the shared oscilloscope knobs. Like MidiScene it's
    # bitmap-only (hires), so synthesise a hires display_mode for overlay
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
    # mic / none: standard frame-source display (REU staging allowed).
    return _display_mode_for_scene(s.display, s, cfg)


def _check_first_sid_clears_display(s: SceneCfg, mode: DisplayMode, display: str) -> None:
    """Load-time guard: confirm the first resolvable .sid candidate's payload
    clears the (fixed bank-0) display regions. Best-effort fast-fail — a
    multi-entry pool may have other candidates, so this only raises when the
    first one parses and demonstrably conflicts (setup() does the authoritative
    per-pick check with bounded retry). Missing/unparseable files are left for
    setup() to surface."""
    from .sid_host_emu import parse_sid_header, payload_overlaps_bank0_display

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
    # dac_calibration.resolve_dac_curve_for_backend).
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


def validate_dither_cfg(cfg: Config) -> None:
    """Guard [color].dither/dither_strength: reject an unknown method name or
    an out-of-range strength."""
    if cfg.color.dither not in DITHER_CHOICES:
        raise ConfigError(
            f"[color].dither must be one of {', '.join(DITHER_CHOICES)}, got {cfg.color.dither!r}"
        )
    if not 0.0 <= cfg.color.dither_strength <= 2.0:
        raise ConfigError(
            f"[color].dither_strength must be 0..2.0, got {cfg.color.dither_strength}"
        )


def validate_motion_smoothing_cfg(cfg: Config) -> None:
    """Guard [color].motion_smoothing: reject an out-of-range value (0..1)."""
    if not 0.0 <= cfg.color.motion_smoothing <= 1.0:
        raise ConfigError(
            f"[color].motion_smoothing must be 0..1.0, got {cfg.color.motion_smoothing}"
        )


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


def validate_color_match_cfg(cfg: Config) -> None:
    """Guard [color].color_match: reject an unknown value."""
    if cfg.color.color_match not in COLOR_MATCH_CHOICES:
        raise ConfigError(
            f"[color].color_match must be one of {', '.join(COLOR_MATCH_CHOICES)}, "
            f"got {cfg.color.color_match!r}"
        )


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


def validate_cell_strategy_cfg(cfg: Config) -> None:
    """Guard [color].cell_strategy: reject an unknown value."""
    if cfg.color.cell_strategy not in CELL_STRATEGY_CHOICES:
        raise ConfigError(
            f"[color].cell_strategy must be one of {', '.join(CELL_STRATEGY_CHOICES)}, "
            f"got {cfg.color.cell_strategy!r}"
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
    if not 1 <= midi_cfg.broadcast_channel <= 16:
        raise ConfigError(
            f"[midi_control].broadcast_channel must be 1..16, got {midi_cfg.broadcast_channel}"
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
                or target.split(".", 1)[0] not in ("effect", "source")
            ):
                raise ConfigError(
                    f"[midi_control].cc_map[{i}] action 'param' needs a string 'target' "
                    "of the form 'effect.<name>' or 'source.<name>', got "
                    f"{target!r}"
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
    if broadcast_on and not _has_sid_scene(cfg):
        log.warning(
            "[wled] broadcast enabled but no SID-driven scene (waveform, or "
            "generative with audio_source = 'sid') in the playlist — nothing "
            "will be broadcast."
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
    lives in `build_scene` itself — doctor mode runs without a source and
    must not be tripped by it.

    Per-type checks live in `_validate_<type>` helpers, each returning the
    display mode the scene will paint (so the shared overlay-compat loop can
    validate against it). Launcher is the exception — it owns the VIC, so it
    self-validates (including its orchestrator) and we return immediately."""
    from .overlays import build_overlay, validate_for_scene

    # Per-scene pixel effect: validated up front (before the launcher early
    # return) so it's caught on every type. Only frame-bearing scenes support it.
    if s.effect is not None:
        if s.effect not in _EFFECT_CHOICES:
            raise ValueError(f"effect must be one of {_EFFECT_CHOICES} or unset, got {s.effect!r}")
        if s.type not in _EFFECT_SCENE_TYPES:
            raise ValueError(
                f"effect is not supported on {s.type!r} scenes (they don't render a "
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
        from .orchestrator import resolve_orchestrator

        resolve_orchestrator(s)


def _frame_push_default_fps(
    mode: DisplayMode,
    has_digitized_audio: bool,
    system: str,
    *,
    off_bus_audio: bool = False,
) -> float | None:
    """Default ``target_fps`` for a frame-pushing scene that can stream the
    4-bit ``$D418`` digitized-audio DAC (video / live webcam / generative-mic).

    Bitmap modes (hires/mhires) push a full ~9-10 KB frame every frame; each
    DMA write halts the C64 bus, and when the digitized-audio DAC is *also*
    streaming, the combined halt load tears the picture at the system rate.
    So a bitmap scene streaming digitized audio caps at **20 fps** (both NTSC
    and PAL), and a bitmap scene without it at **half** the system rate
    (30 NTSC / 25 PAL). Char modes (petscii/blank) are cheap — a 1 KB
    delta-cached screen — so they keep the playlist system default; this
    returns ``None`` for them and the caller leaves ``target_fps`` unset.

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
    if not mode.is_bitmapped:
        return None
    if has_digitized_audio:
        return 20.0
    if off_bus_audio:
        return 50.0 if system.upper() == "PAL" else 60.0
    return 25.0 if system.upper() == "PAL" else 30.0


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
    (validation, doctor) leave it False so auto degrades to host-DMA.

    `sampler_available` is the probe's verdict on whether the U64's Ultimate
    Audio sampler is exposed + routed; it resolves [audio].backend for video
    scenes (see resolve_audio_backend). False without a probe → DAC."""
    from .scenes import BlankScene, SourceScene, VideoScene, WebcamScene

    validate_scene_cfg(s, cfg, audio_enabled=audio is not None)

    audio_reu_pump_active = cfg.audio.use_reu_pump
    # Whether THIS backend has an REU at all (capability, not "REU enabled" —
    # that's reu_available). Resolves the [video].double_buffer "auto" host-DMA
    # page-flip path on no-REU backends (the TeensyROM). See resolve_double_buffer.
    backend_supports_reu = api.profile.supports_reu
    scene: Scene
    if s.type == "webcam":
        if source is None:
            raise ValueError(
                "webcam scene declared but no WebcamSource was provided — "
                "this should have been caught at cli.py startup"
            )
        display = resolve_scene_display(s.display, s.type)
        mode = _display_mode_for_scene(
            display,
            s,
            cfg,
            reu_available=reu_available,
            backend_supports_reu=backend_supports_reu,
        )
        name = s.name or f"Webcam {display}"
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
        if s.target_fps is None:
            fps = _frame_push_default_fps(mode, scene_audio is not None, cfg.ultimate64.system)
            if fps is not None:
                scene.target_fps = fps
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
        mode = _display_mode_for_scene(
            s.display,
            s,
            cfg,
            reu_available=reu_available,
            backend_supports_reu=backend_supports_reu,
        )
        # Default: audio ON for videos (it's part of the file).
        # The user can mute one with `audio = false`. Distinct name from the
        # other branches' `scene_audio` because this one may hold the sampler.
        video_audio: AudioStreamer | UltimateAudioSampler | None = (
            None if s.audio is False else audio
        )
        # Resolve the video-audio backend. On a sampler-capable U64 with the
        # Ultimate Audio sampler available, swap the shared 4-bit DAC streamer
        # for a per-scene UltimateAudioSampler (high fidelity, off the C64 bus —
        # see sampler.py). It satisfies the same scene-facing audio contract
        # (sample_rate / position_seconds / push_samples / stop), so VideoScene
        # drives it polymorphically; mic/webcam scenes keep the shared DAC.
        using_sampler = False
        if video_audio is not None:
            backend = resolve_audio_backend(
                cfg.audio.backend,
                supports_sampler=api.profile.supports_sampler,
                sampler_available=sampler_available,
            )
            if backend == "sampler":
                from .sampler import UltimateAudioSampler

                video_audio = UltimateAudioSampler(
                    api,
                    sample_rate=cfg.audio.sampler_sample_rate,
                    bits=cfg.audio.sampler_bits,
                    ref_clock_hz=cfg.audio.sampler_clock_hz,
                )
                using_sampler = True
        # Bitmap + $D418-DAC tempo compensation. On the host-DMA 4-bit DAC path
        # over a bitmap mode, heavy REU bank-swap bitmap writes bias the audio
        # servo and time-stretch playback ~1/s SLOW at correct pitch. Pre-
        # compress the content by 1/s (audio time-compress + video PTS × s) so it
        # nets to real time. Gated OFF (tempo_scale 1.0) for the off-bus sampler,
        # the REU pump, char modes, and muted scenes — none of which stretch.
        # `using_sampler` False with audio present means the DAC path.
        from .modes import BitmapDisplayMode, MultiHiresDisplayMode

        tempo_scale = 1.0
        if (
            video_audio is not None
            and not using_sampler
            and not cfg.audio.use_reu_pump
            and isinstance(mode, BitmapDisplayMode)
        ):
            tempo_scale = (
                cfg.audio.dac_bitmap_tempo_mhires
                if isinstance(mode, MultiHiresDisplayMode)
                else cfg.audio.dac_bitmap_tempo_hires
            )
        assert s.file is not None  # narrowed by validate_scene_cfg
        # A single media URL (YouTube et al.) is resolved here — the ONE
        # resolution path shared with quick playback — so config-driven videos
        # accept URLs too. Its t=/start= timestamp folds into start_s (an
        # explicit start_s wins), and the resolved title becomes the scene name.
        # Local files / dir / glob / multi specs are untouched.
        file_spec = s.file
        start_s = s.start_s
        video_name = s.name
        if _is_single_url_spec(s.file):
            from .quickcast import resolve_video_url

            stream_url, url_start_s, title = resolve_video_url(s.file.strip())
            file_spec = stream_url
            if start_s is None:
                start_s = url_start_s
            if video_name is None:
                video_name = title
        scene = VideoScene(
            api,
            video_audio,
            mode,
            file_spec,
            prepend_alignment_marker=(cfg.audio.source_alignment_marker and cfg.audio.use_reu_pump),
            color=cfg.color,
            start_s=start_s or 0.0,
            tempo_scale=tempo_scale,
        )
        if video_name:
            scene.name = video_name
        if s.target_fps is None:
            # The sampler plays entirely off the C64 bus, so it neither imposes
            # the 4-bit DAC's bitmap fps cap (the DAC's NMI + ring DMAWRITEs
            # compete with frame uploads for the bus) nor the muted half-rate
            # cap (its REU-staged frame uploads are bus-clean, not host DMA).
            # So sampler bitmap video uncaps to the system rate (60/50) — and
            # because VideoScene dedups, the effective push rate then equals the
            # source video's fps (24fps clip → 24/s, etc.). DAC video stays 20;
            # muted bitmap stays 30/25. See _frame_push_default_fps.
            has_digitized_audio = video_audio is not None and not using_sampler
            fps = _frame_push_default_fps(
                mode, has_digitized_audio, cfg.ultimate64.system, off_bus_audio=using_sampler
            )
            if fps is not None:
                scene.target_fps = fps
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
            sid_model=resolve_sid_model_cfg(cfg),
        )
        if s.name:
            scene.name = s.name
    elif s.type == "slideshow":
        from .scenes import SlideshowScene

        display = _resolve_slideshow_display(s.display)
        mode = _display_mode_for_scene(
            display, s, cfg, reu_available=reu_available, backend_supports_reu=backend_supports_reu
        )
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
            double_buffer=cfg.video.double_buffer,
            reu_available=reu_available,
            backend_supports_reu=backend_supports_reu,
            audio_reu_pump_active=audio_reu_pump_active,
            color=cfg.color,
            text_double_height=s.text_double_height,
            aspect_mode=s.aspect_mode,
        )
    elif s.type == "generative":
        from .audio_source import (
            AudioSource,
            MicAudioSource,
            NullAudioSource,
            SidFileAudioSource,
        )
        from .generators import build_generator

        gen = build_generator(s.source)
        name = s.name or f"Generative {s.source}"
        audio_src: AudioSource
        if s.audio_source == "sid":
            # Force host-DMA: the SID player owns the $0314 IRQ for PLAY, so the
            # display must NOT install the REU bank-swap raster IRQ (it would
            # collide). The SID drives the chip directly — no DAC streamer, plays
            # regardless of [audio].enabled, and is NOT subject to the ensemble
            # live-mic suppression (it legitimately holds the audio spotlight;
            # wants_audio_lock=True gates the slot). scene_audio stays None.
            mode = _display_mode_for_scene(s.display, s, cfg, force_host_dma=True)
            assert s.file is not None  # narrowed by _validate_generative
            audio_src = SidFileAudioSource(
                api,
                s.file,
                song=s.song,
                display_mode=mode,
                system=cfg.ultimate64.system,
                reactive=s.reactive,
                sid_model=resolve_sid_model_cfg(cfg),
            )
            scene = SourceScene(api, None, mode, gen, audio_src, name)
            # Bitmap displays push a full ~9-10 KB frame via host DMAWRITE; at
            # full system rate that competes with the SID player's per-frame
            # PLAY IRQ for the bus. Default such scenes to half-rate (like
            # WaveformScene) for safety; a char display stays full-rate, and an
            # explicit target_fps (applied at the end of build_scene) still wins.
            if s.target_fps is None and mode.is_bitmapped:
                scene.target_fps = 25.0 if cfg.ultimate64.system.upper() == "PAL" else 30.0
        else:
            mode = _display_mode_for_scene(
                s.display,
                s,
                cfg,
                reu_available=reu_available,
                backend_supports_reu=backend_supports_reu,
            )
            # mic / none: the live-frame audio path. Like webcam/blank, a live
            # mic source is suppressed in ensemble mode.
            scene_audio = None if s.audio is False else audio
            if is_ensemble and scene_audio is not None:
                if s.audio is True:
                    log.info(
                        "[%s] generative scene: audio suppressed in ensemble mode "
                        "(live scenes never hold the audio spotlight)",
                        name,
                    )
                scene_audio = None
            if s.audio_source == "mic" and scene_audio is not None:
                audio_src = MicAudioSource(scene_audio, cfg.audio, display_mode=mode)
            else:
                # "none", or "mic" with audio disabled → silence.
                audio_src = NullAudioSource()
            scene = SourceScene(api, scene_audio, mode, gen, audio_src, name)
            # A mic-source generative scene is digitized-audio-capable like
            # webcam/video, so it gets the same bitmap frame-push caps (20 fps
            # while the DAC streams, half rate otherwise). The "none" source
            # never drives the DAC, so it keeps the playlist default.
            if s.target_fps is None and s.audio_source == "mic":
                fps = _frame_push_default_fps(mode, scene_audio is not None, cfg.ultimate64.system)
                if fps is not None:
                    scene.target_fps = fps
    elif s.type == "wled":
        from .audio_source import NullAudioSource
        from .wled_sink import WLEDSource

        # A network pixel sink: the frame arrives over UDP, no audio, no SID.
        # It's just another FrameSource behind the SourceScene seam — the
        # display mode quantizes the received BGR frame to the C64 unchanged.
        mode = _display_mode_for_scene(
            s.display,
            s,
            cfg,
            reu_available=reu_available,
            backend_supports_reu=backend_supports_reu,
        )
        wled_source = WLEDSource(s.sink_width, s.sink_height)
        name = s.name or "WLED sink"
        scene = SourceScene(api, None, mode, wled_source, NullAudioSource(), name)
        # Bitmap displays push a full ~9-10 KB frame per update; default to
        # half rate like the other frame scenes (an explicit target_fps, applied
        # at the end of build_scene, still wins).
        if s.target_fps is None and mode.is_bitmapped:
            scene.target_fps = 25.0 if cfg.ultimate64.system.upper() == "PAL" else 30.0
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
    elif s.type == "midi":
        from .midi_scene import MidiScene

        a, d, sus, r = s.midi_adsr
        scene = MidiScene(
            api,
            audio,
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
    else:  # s.type == "asid" (validator already rejected unknown types)
        from .asid_scene import AsidScene

        scene = AsidScene(
            api,
            audio,
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
            name=s.name or "ASID",
        )
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
    # Per-scene pixel effect (validated frame-bearing in validate_scene_cfg).
    # Applied to the source frame in scenes._render_with_overlays.
    if s.effect is not None:
        from .effects import build_effect

        scene.effect = build_effect(s.effect)
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
            s,
            cfg,
            api,
            audio,
            source,
            is_ensemble=is_ensemble,
            reu_available=reu_available,
            sampler_available=sampler_available,
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
            vid_mode = HiresDisplayMode(style="edges")
            vid_scene = VideoScene(
                api,
                audio,
                vid_mode,
                video_files[video_idx],
                prepend_alignment_marker=(
                    cfg.audio.source_alignment_marker and cfg.audio.use_reu_pump
                ),
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
