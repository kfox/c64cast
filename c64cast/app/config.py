"""Config loading and CLI merging.

Defaults live in the dataclasses below. A TOML file (default search path:
``./c64cast.toml``, override with ``--config PATH``) can override any
of them, and CLI args in turn override the config file. The precedence
is: built-in defaults < config file < CLI flags.

Turning the declarative ``[[scenes]]`` list into real Scene instances is
scene_factory.py's job (`scenes_from_config`) — this module stays clear of
the scene/display runtime so loading a TOML never pulls it in.
"""

from __future__ import annotations

import argparse
import difflib
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from typing import Any

from c64cast.audio.dac_curves import DAC_CURVE_CHOICES
from c64cast.audio.dsp import DSPParams
from c64cast.audio.sampler import SAMPLER_REF_CLOCK_DEFAULT
from c64cast.sid.sid_autoconfig import SID_MODEL_CHOICES
from c64cast.sid.sid_panning import MAX_PANNED_SOURCES, normalize_pan_spec
from c64cast.sid.sid_volume import MAX_VOLUME_SOURCES, normalize_volume_spec
from c64cast.video.dither import DITHER_METHODS
from c64cast.video.palette import CELL_STRATEGIES, COLOR_MATCH_MODES, resolve_color

from . import paths

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
SYSTEM_CHOICES = ("NTSC", "PAL")
# Mirrors backend.BACKENDS; duplicated here so config.py stays import-light
# (it doesn't pull in api.py). tests/test_introspect.py asserts they match.
_BACKEND_CHOICES = ("ultimate", "teensyrom")
# Unlike [ultimate64].sid_model ("off" = don't touch the hardware config),
# the opt-out here is "unknown": there is no hardware config to touch, only
# a claim about the machine that a verdict can be rendered from.
HOST_SID_MODEL_CHOICES = ("auto", "6581", "8580", "unknown")
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
    "hiphotic",
    "metaballs",
    "rotozoomer",
    "lissajous",
    "dna",
    "drift",
    "colored_bursts",
    "dotswarm",
    "game_of_life",
    "soap",
    "fireworks",
)
_EFFECT_CHOICES = (
    "trails",
    "pulse",
    "rgb_shift",
    "blur",
    "strobe",
    "invert",
    "mirror",
    "posterize",
)
# How a reactive effect layer is driven ([[scenes]].mod_source / clip slots):
# "audio" = the scene's SID feature stream (today's behavior), "clock" = the
# [performance] beat grid (MIDI/tap tempo), "off" = never react (baseline). A
# drift note: mirrored in effects.FrameEffect.mod_source's docstring.
_MOD_SOURCE_CHOICES = ("audio", "clock", "off")

# The fixed `param` target holder prefixes (the bit before the first "."), plus
# the layer-addressed effect forms `fx<N>` and `effect[<N>]` that reach a
# specific effect-chain layer (Live DJ/VJ Phase 3). Mirrors the holder
# resolution in midi_control._apply_param — kept independent so config stays
# import-light (the module's standing rule).
_PARAM_HOLDER_PREFIXES = ("effect", "source", "scene", "mode")
_FX_LAYER_HOLDER_RE = re.compile(r"^(?:fx(\d+)|effect\[(\d+)\])$")


def _is_valid_param_holder(holder: str) -> bool:
    """Whether `holder` (the bit before the first "." in a `param` target) names
    a real live-tune holder: one of the fixed prefixes, or a layer-addressed
    effect (`fx0`, `effect[2]`, …)."""
    return holder in _PARAM_HOLDER_PREFIXES or _FX_LAYER_HOLDER_RE.match(holder) is not None


# How a slideshow image is fit to the C64 aspect before the display mode
# downscales it. See scenes._apply_aspect.
_ASPECT_MODE_CHOICES = ("crop", "fit", "stretch")

# Per-scene audio source for composable (generative) scenes — the AudioSource
# building block in audio_source.py. "none" = silence; "mic" = live mic via the
# shared AudioStreamer, streamed to the 4-bit DAC AND analyzed for reactive
# visuals; "listen" = analyze the live input for reactive visuals only, no C64
# audio output (the VJ case); "file" = decode an audio file (mp3/wav/…, needs
# `file`) to the DAC AND analyze it for reactive visuals; "sid" = play a .sid on
# the real chip (needs `file`). "mic"/"listen"/"file" are gated by [audio].enabled
# (they need the shared streamer). Default "none". A drift test pins this list.
_AUDIO_SOURCE_CHOICES = ("none", "mic", "listen", "file", "sid")

# Video-audio backend selector ([audio].backend). "dac" = the 4-bit $D418 NMI
# DAC (every backend; lo-fi, bus-coupled). "sampler" = the U64 "Ultimate Audio"
# FPGA PCM sampler (high fidelity, off the C64 bus; U64 only — see sampler.py).
# "auto" = sampler on a sampler-capable U64 with the feature available, else
# dac. A drift test pins this list.
AUDIO_BACKEND_CHOICES = ("auto", "dac", "sampler")

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
    # Consulted only by the resolved-audio verdict, and only when the link
    # can't read the SID hardware state itself (c64cast/sid/sid_resolved.py);
    # a backend with the U64 SID config API reads the real chips instead.
    host_sid_model: str = field(
        default="auto",
        metadata={
            "help": "SID chip model in the C64 being driven, so a tune asking "
            "for the other model still gets a warning on links that can't read "
            "the SID hardware state (e.g. TeensyROM). 'auto' assumes 6581 on "
            "NTSC / 8580 on PAL and logs that assumption; 'unknown' opts out "
            "of model-match verdicts. Ignored where the live SID state is "
            "readable (U64).",
            "choices": HOST_SID_MODEL_CHOICES,
        },
    )
    dump_char_rom: bool = field(
        default=True,
        metadata={
            "help": "On the first run against a machine, read its character ROM "
            "and cache it, so C64 text renders in the real C64 font instead of a "
            "built-in ASCII substitute. One ~1s step, never repeated; set false "
            "to skip it entirely."
        },
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
        default="http://192.168.2.64",
        metadata={"help": "Base URL of the Ultimate 64 (REST + DMA host)."},
    )
    system: str = field(
        default="NTSC",
        metadata={
            "help": "Target video system timing (affects frame rate + SID PLAY rate).",
            "choices": SYSTEM_CHOICES,
        },
    )
    # See docs/guide/04-setting-up.md for how to enable the DMA service on the
    # U64 itself.
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
    # Applied live to the U64's Audio Mixer before playback and restored at
    # teardown, like sid_model. Panning is per audio SOURCE (socket / UltiSID
    # core), so each tune chip is panned wherever it was routed — see
    # c64cast/sid/sid_panning.py.
    sid_panning: list[int | str] = field(
        default_factory=list,
        metadata={
            "help": "Stereo pan per SID audio source, U64 only. Max 4 entries — "
            "the U64 has one pan control per source (2 SID sockets + 2 UltiSID "
            "cores), and entry N pans the Nth source the tune uses. Each entry "
            "is an int -5..5 (negative = left, 0 = center) or a label ('Left 3', "
            "'Center', 'Right 2'). Empty = auto spread: 1 source centered, "
            "2 [-3, 3], 3 [0, -3, 3], 4 [-2, 2, -5, 5] — ordered so the primary "
            "chip stays nearest center. Fewer positions exist without socketed "
            "SIDs: with none, only the 2 UltiSID cores are pannable, so chips "
            "beyond the 2nd share a pan.",
        },
    )
    # Applied live to the U64's Audio Mixer alongside sid_panning and restored
    # at teardown. Indexed by SOURCE exactly like sid_panning — see
    # c64cast/sid/sid_volume.py.
    sid_volume: list[int | str] = field(
        default_factory=list,
        metadata={
            "help": "Mixer level per SID audio source, U64 only. Max 4 entries — "
            "the U64 has one volume control per source (2 SID sockets + 2 UltiSID "
            "cores), and entry N sets the Nth source the tune uses, same indexing "
            "as sid_panning. Each entry is a dB int (0, -6, 3) or a label "
            "('0 dB', '-6 dB', 'off'). Empty = auto: a source the tune plays on is "
            "raised to 0 dB when it would otherwise be OFF (silent), a source "
            "already at a deliberate level is left alone, and every source the tune "
            "does not use is muted. The ladder is sparse below -18 dB: -42, -36, "
            "-30, -27, -24, then every dB from -18 to +6.",
        },
    )


@dataclass
class VideoCfg:
    device: int | str = field(
        default=-1,
        metadata={
            "help": (
                "Webcam device: an integer cv2 index (-1 = system default camera, "
                "cv2 index 0), or a string matched to a camera by name substring (e.g. "
                '"Cam Link") or USB VID:PID (e.g. "0fd9:0066"). String selection needs '
                "the 'camera' extra; run --list-devices to see names + VID:PID."
            )
        },
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
    device: int | str = field(
        default=-1,
        metadata={
            "help": (
                "Audio input device: an integer index (-1 = system default microphone), "
                'or a string matched to an input device by name substring (e.g. "Cam Link"). '
                "Run --list-devices to see names + indices."
            )
        },
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
            "rejected at load, and --doctor reports them. Sampler-backend playback uses "
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
            "choices": AUDIO_BACKEND_CHOICES,
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
            "correctly), set 6250000. The repository carries a diagnostic script that "
            "re-measures it and prints the value. Only affects the sampler backend."
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
    # See the audio.py digi_boost note in docs/architecture.md for the full
    # rationale. Essential on
    # 8580s and emulated SIDs; on a 6581 it just raises output level.
    digi_boost: bool = field(
        default=False,
        metadata={
            "help": "EXPERIMENTAL: lock SID voices to a DC pulse so the ADSR D/As bias "
            "the master mixer, raising $D418 playback level."
        },
    )
    # Mahoney 8-bit $D418 companding. "auto" (default) picks the best curve for
    # the SID that actually answers $D400: a per-unit calibrated table if one
    # applies (see --calibrate-dac), else "mahoney_ultisid" when an UltiSID core
    # owns that address (the emulated SID is deterministic), else "linear" (a
    # physical/unknown SID with no calibration — the baked emulated table would
    # not match it, see dac_curve_resolve.py). "linear" = the
    # classic 4-bit volume-nibble DAC. "mahoney_ultisid" parks the SID voices as
    # DC sources and writes the full $D418 byte per sample (volume + filter-mode
    # + 3-off bits) for ~6-7 effective bits, using a baked table measured on the
    # U64's emulated UltiSID. "calibrated" forces this system's calibrated table
    # (errors if none). Non-linear curves are mutually exclusive with digi_boost.
    # See dac_curves.py, dac_curve_resolve.py + docs/architecture.md.
    dac_curve: str = field(
        default="auto",
        metadata={
            "help": "SID $D418 DAC companding curve. 'auto' (default) = calibrated "
            "table for the SID answering $D400 if present, else 'mahoney_ultisid' "
            "when an UltiSID core owns $D400, else 'linear' (an uncalibrated "
            "physical chip — run --calibrate-dac to measure it). "
            "'linear' = classic 4-bit volume nibble. 'mahoney_ultisid' "
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
    # straight when the cartridge moves between machines. See dac_calibration_store.py.
    dac_calibration_profile: str | None = field(
        default=None,
        metadata={
            "help": "Override the auto-derived calibration file key (device unique_id / "
            "TR USB serial) with a name — calibration/dac/profile-<name>.json, or an "
            "existing file's own name (e.g. the device-keyed 'ultimate-<id>' that "
            "--calibrate-dac writes), used as-is — or with "
            "a path to a calibration file, used as given. Use when a TeensyROM+ moves "
            "between physical C64s (name each host's calibration once at --calibrate-dac "
            "time, then pass the same name on every playback run against that host), or "
            "to reuse one machine's calibration from another backend (a path, since that "
            "file is keyed by the other backend's device identity)."
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
    # See the audio.py REU-pump note in docs/architecture.md. Eliminates the
    # host-DMA 'gurgling'
    # artifact on real hardware by streaming from REU SRAM instead.
    use_reu_pump: bool = field(
        default=False,
        metadata={
            "help": "EXPERIMENTAL: stream video/mic audio from a REU ring "
            "(bus-clean) instead of per-write host DMA. Requires REU enabled."
        },
    )
    # See the audio.py REU-pump note in docs/architecture.md. The C64-side
    # pump (CIA #1 rate) and
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
    # See c64cast.audio.audio_marker for the find-marker analysis helper. Only the
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
    #
    # THESE KNOBS ARE QUANTIZED — they look continuous and are not. The NMI
    # period is an integer PHI2 cycle count, so NmiTimer.compensated_latch rounds:
    # period = round((nominal+1) / mult). At the default 12 kHz the nominal
    # NTSC period is 85 cycles, so ONE STEP IS ~1.2% and every request lands on
    # that grid:
    #
    #     1.005 (+0.5%) -> period 85 -> +0.00%   (a no-op)
    #     1.010 (+1.0%) -> period 84 -> +1.19%
    #     1.015 (+1.5%) -> period 84 -> +1.19%   (same latch as 1.010)
    #     1.020 (+2.0%) -> period 83 -> +2.41%
    #
    # This retro-explains the ear-tuned defaults above: 1.015 was measured
    # +1.36% high on hardware, which tracks the QUANTIZED +1.19%, not the
    # +1.5% that was asked for. Sub-step pitch trim is not expressible here —
    # a finer correction has to come from the content side (resampling), the
    # way dac_bitmap_tempo_* fixes tempo. The grid coarsens as sample_rate
    # rises (fewer cycles per period): ~1.2% at 12 kHz, ~0.8% at 8 kHz.
    # AudioStreamer.effective_rate is the same quantization seen at mult=1.0.
    pitch_mult_petscii: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for PETSCII mode "
            "(light char-mode load). 1.0 = none (default; U64-II NTSC is dead-on)."
            " Quantized: the NMI period is an integer cycle count, so a "
            "request rounds onto the latch grid (~1.2% steps at 12 kHz) — "
            "1.005 is a no-op, 1.015 lands on +1.19%."
        },
    )
    pitch_mult_hires: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for Hires / Hires-edges "
            "modes. 1.0 = none (default; modern fps caps + REU staging leave ~0 "
            "loss on U64-II NTSC). Re-tune only if a platform (PAL/TR+) drifts."
            " Quantized: the NMI period is an integer cycle count, so a "
            "request rounds onto the latch grid (~1.2% steps at 12 kHz) — "
            "1.005 is a no-op, 1.015 lands on +1.19%."
        },
    )
    pitch_mult_mhires: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for MultiHires mode. "
            "1.0 = none (default; modern fps caps + REU staging leave ~0 loss on "
            "U64-II NTSC). Re-tune only if a platform (PAL/TR+) drifts."
            " Quantized: the NMI period is an integer cycle count, so a "
            "request rounds onto the latch grid (~1.2% steps at 12 kHz) — "
            "1.005 is a no-op, 1.015 lands on +1.19%."
        },
    )
    pitch_mult_mcm: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for MCM mode "
            "(char-based, light load; U64-II NTSC: good at 1.0)."
            " Quantized: the NMI period is an integer cycle count, so a "
            "request rounds onto the latch grid (~1.2% steps at 12 kHz) — "
            "1.005 is a no-op, 1.015 lands on +1.19%."
        },
    )
    pitch_mult_blank: float = field(
        default=1.00,
        metadata={
            "help": "Host-DMA servo playback-rate multiplier for Blank mode "
            "(no video input; 1.0 = none)."
            " Quantized: the NMI period is an integer cycle count, so a "
            "request rounds onto the latch grid (~1.2% steps at 12 kHz) — "
            "1.005 is a no-op, 1.015 lands on +1.19%."
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

    See [c64cast/control/vision.py](c64cast/control/vision.py). Needs the `vision` extra
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
    performance: bool = field(
        default=False,
        metadata={
            "help": "Live DJ/VJ Phase 6: remap the RUNNING-state gestures to "
            "clip-launch performance actions instead of transport — swipe = "
            "launch the next [[performance.clips]] slot, pinch-hold = bypass "
            "effect layer 0, open-hand-hold = bypass effect layer 1. Off "
            "(default) keeps the transport mapping (swipe=skip, pinch=pause, "
            "open-hand=cycle style). Pinch-hold-to-resume while paused is "
            "unchanged either way. Needs a [[performance.clips]] grid for the "
            "clip-advance gesture to do anything."
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
    # See docs/reference/02-config-rules.md for single- vs multi-scene behavior.
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
            "streaming digitized audio, else half rate (30/25). Generative and "
            "webcam scenes take that 20 fps cap in CHAR modes too whenever "
            "audio is on the 4-bit DAC — they repaint every tick (no dedup), so "
            "the frame writes contend with the audio ring for the DMA socket. "
            "Off-bus Ultimate Audio sampler playback keeps the high default. "
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
        default="none",
        metadata={
            "help": "Audio for a generative scene: 'none' = silent (default); "
            "'mic' = live audio input — an instrument/mixer feed via an "
            "interface, or a mic — streamed to the 4-bit DAC and analyzed; "
            "'listen' = analyze the live input for reactive visuals ONLY, with "
            "no C64 audio output (the VJ case: the real sound is on a PA and "
            "only the visuals track it — and, freed from the DAC rate, it "
            "captures full-bandwidth at [audio_features].listen_sample_rate); "
            "'sid' = play the `file` .sid on the real chip. 'mic'/'listen' need "
            "[audio].enabled for the capture subsystem. 'mic', 'listen' and "
            "'sid' all drive reactive visuals (see `reactive`); the input "
            "analyzer is tunable under [audio_features]. A SID source forces a "
            "host-DMA display and needs a char display (petscii/mcm) for most "
            "tunes (see `file`).",
            "choices": _AUDIO_SOURCE_CHOICES,
            "applies_to": ("generative",),
        },
    )
    # Generative scene: drive the visuals from the music. Two producers supply
    # the features — a host-side SID emulator (audio_source = sid) or the
    # audio-input analyzer (audio_source = mic). Inert for "none".
    reactive: bool = field(
        default=True,
        metadata={
            "help": "Generative scene: let the music drive the visuals — BPM "
            "cycles the colors, transients pulse them, bass reads differently "
            "from treble. Works with audio_source = 'sid' (a host-side SID "
            "emulator supplies the features, adding no U64 traffic) and "
            "'mic'/'listen' (the live input is analyzed on the host — see "
            "[audio_features]); inert for 'none'. Set false to keep the pure "
            "time-driven look (and, for 'listen', to skip capture entirely).",
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
    # Ordered pixel-effect chain (Live DJ/VJ Phase 3) — an alternative to the
    # single `effect` above. Each layer is applied in order; every layer is
    # independently live-tunable (fx<N>.<param>) and bypass-toggleable
    # (fx_toggle). Mutually exclusive with `effect` (set one or the other).
    effects: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Ordered pixel-effect chain applied before quantization, e.g. "
            'effects = ["trails", "rgb_shift", "strobe"]. Each is one of the '
            "`effect` choices; layers apply in order and are individually "
            "tunable (map a CC to fx0.<param>/fx1.<param>…) and bypass-"
            "toggleable live (fx_toggle). Mutually exclusive with the single "
            "`effect` field. Empty = none.",
            "choices": _EFFECT_CHOICES,
            "applies_to": ("webcam", "video", "slideshow", "generative", "wled"),
        },
    )
    # Which modulation feeder drives the reactive effect layers on this scene.
    mod_source: str = field(
        default="audio",
        metadata={
            "help": "What drives this scene's reactive effect layers: 'audio' "
            "(the SID feature stream — needs a music-reactive scene, i.e. "
            "generative + audio_source = 'sid'), 'clock' (the [performance] beat "
            "grid, so effects lock to MIDI/tap tempo on any scene — the way to "
            "tempo-lock a 'strobe'), or 'off' (never react — layers use their "
            "static baseline). Applies to every effect layer on the scene.",
            "choices": _MOD_SOURCE_CHOICES,
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
    # See the modes.py section of docs/architecture.md for the per-mode
    # palette_mode semantics.
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
    # orchestrate/follower_only drive the conductor/follower broadcast
    # protocol in c64cast/app/orchestrator.py; see docs/architecture.md.
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
    """Local window mirroring what the U64 displays, drawn with cv2 (a hard
    dep — no extra needed). Off by default: it wants a desktop session, and
    it costs a host-side re-render of every frame."""

    enabled: bool = field(
        default=False,
        metadata={
            "help": "Open a local window mirroring the U64 display "
            "(needs a desktop session; no extra required)."
        },
    )
    fps: int = field(default=30, metadata={"help": "Preview window refresh rate."})
    scale: int = field(
        default=3, metadata={"help": "Integer pixel scale factor for the preview window."}
    )
    charset_path: str | None = field(
        default=None,
        metadata={
            "help": "C64 character ROM used to render char modes in the preview. "
            "Unset = resolve automatically (the dump c64cast takes off your own "
            "C64 on the first run; see --dump-char-rom)."
        },
    )


@dataclass
class RecordingCfg:
    """Capture the rendered display to a video file. Uses cv2.VideoWriter,
    so all you need is the `opencv-python` core dep."""

    enabled: bool = field(
        default=False,
        metadata={"help": "Record the rendered display to a video file (cv2.VideoWriter)."},
    )
    path: str = field(
        default="recording.mp4",
        metadata={
            "help": "Output video file path. Does not cascade from an ensemble "
            "master: a system that leaves this alone records to "
            "'recording-<system>.mp4' so the wall's systems don't overwrite "
            "each other. Setting it explicitly uses that path verbatim."
        },
    )
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
            "help": "EXTREME forced-palette remap (mcm/mhires): k-means the "
            "source into N clusters and map each to a DISTINCT C64 color so "
            "all N colors are used. Pre-scanned for video + slideshow; adapts "
            "live (rolling, warm-start + hysteresis) for webcam/wled/generative. "
            "Deliberate false-color (NOT faithful) — off by default; also "
            "reachable via the SHIFT cycle's 'percell+forced' stop once enabled. "
            "Tip: `--suggest-palette FILE` ranks a good force_palette_colors set."
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
class AudioFeaturesCfg:
    """Analyzer that turns live audio input into reactive-visual features.

    A generative scene with `audio_source = "mic"` and `reactive = true` runs
    this over a PRE-DSP tap of the input (see c64cast/audio/audio_features.py): block
    RMS becomes `level`, an FFT becomes log-spaced `bands`, spectral flux
    becomes `onset`, and the onset rate becomes `bpm`/`beat_phase`. That is the
    same `MusicModulation` a SID tune produces via the host-side emulator, so
    the generators, the effect chain and the WLED broadcaster all react to it
    without knowing where it came from.

    Defaults are tuned for music through a line input (an iRig, a mixer feed).
    The only knob most setups touch is `onset_sensitivity`."""

    bands: int = field(
        default=8,
        metadata={
            "help": "Number of log-spaced frequency bands the analyzer reports "
            "(low→high). Generators fold these into bass/mid/treble thirds, so "
            "multiples of 3 are not required; 8 matches the spectrum_petscii "
            "overlay's bands. More bands = finer spectral detail, no meaningful "
            "cost."
        },
    )
    onset_sensitivity: float = field(
        default=1.0,
        metadata={
            "help": "Transient-detection sensitivity. The spectral-flux "
            "threshold is divided by this, so >1 fires onsets more readily "
            "(sparse/soft material, a quiet feed) and <1 fires less (dense or "
            "heavily compressed material where everything reads as a transient). "
            "1.0 is the tuned default."
        },
    )
    poll_hz: float = field(
        default=60.0,
        metadata={
            "help": "Analysis rate in Hz. 60 matches a full-rate display, so "
            "every rendered frame sees fresh features. Lower it only to save "
            "host CPU; below ~30 transients start to smear."
        },
    )
    fft_size: int = field(
        default=1024,
        metadata={
            "help": "Analysis window in samples. Larger = finer frequency "
            "resolution but blurrier transient timing; 1024 is the balance point "
            "at the DAC's sample rates."
        },
    )
    listen_sample_rate: int = field(
        default=44100,
        metadata={
            "help": "Capture rate in Hz for audio_source = 'listen' (the "
            "listen-only path, which never feeds the DAC and so isn't bound to "
            "its ~12 kHz rate). 44100 gives the analyzer full-bandwidth audio — "
            "real hi-hat energy above the DAC's 6 kHz Nyquist and cleaner "
            "transients. Ignored by audio_source = 'mic' (that path analyzes at "
            "the DAC rate, matching what the C64 actually plays)."
        },
    )


@dataclass
class DSPCfg:
    """Host-side audio DSP applied to float samples BEFORE the 4-bit $D418 DAC
    quantization (see c64cast/audio/dsp.py). The DAC has ~24 dB of usable range;
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


_MIDI_CC_TYPE_CHOICES = ("cc", "note", "pc", "mmc")
_MIDI_ACTION_CHOICES = (
    "pause",
    "resume",
    "toggle_pause",
    "skip",
    "cycle_style",
    "jump",
    "param",
    # DJ-style video transport (MIDI live-tune Phase 2). Mirrored in
    # midi_control.py's own copy — tests/test_live_tune.py (or wherever the
    # LIVE_CHOICES-style drift test lives) pins the two lists together so
    # they can't diverge.
    "transport.play_pause",
    "transport.stop",
    "transport.loop_toggle",
    "transport.rw",
    "transport.ff",
    "transport.jog",
    # Record workflow + loop preset pads (MIDI live-tune Phase 3).
    "transport.record",
    "loop_slot",
    # Live OSD toggle (MIDI live-tune Phase 5): tap flips top/bottom, a
    # double-tap (<400 ms) hides the OSD, a tap while hidden re-enables it.
    "osd.position",
    # Tap tempo (Live-performance Phase 1): each hit feeds the internal beat
    # grid's tap averager. Mirrored in midi_control._ACTIONS.
    "tempo_tap",
    # Clip launch (Live-performance Phase 2): note/PC/pad -> clip `slot`, fired
    # quantized to the beat grid. Needs an int `slot` >= 1. Mirrored in
    # midi_control._ACTIONS.
    "clip_launch",
    # Effect-layer bypass toggle (Live-performance Phase 3): note/PC/pad flips
    # the `enabled` flag of effect layer `slot` (0-based) on the current scene.
    # Needs an int `slot` >= 0. Mirrored in midi_control._ACTIONS.
    "fx_toggle",
    # Look snapshot / recall pads (Live-performance Phase 6): `look_save` captures
    # the active clip + effect-chain state to look `slot`; `look_recall` re-fires
    # it. Both need an int `slot` >= 1. Mirrored in midi_control._ACTIONS.
    "look_save",
    "look_recall",
)
# MMC transport command bytes recognized in a `type: "mmc"` cc_map entry —
# mirrors midi_control._MMC_COMMANDS (kept independent per the module's
# "config stays import-light" rule; see validate_midi_control_cfg).
_MIDI_MMC_COMMAND_CHOICES = (0x01, 0x02, 0x04, 0x05, 0x06, 0x09)

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
    osd: str = field(
        default="bottom",
        metadata={
            "help": "On-screen display for live-tune feedback: a brief 'param "
            "value' message appears when you sweep a knob or change a mode via "
            "MIDI/WLED, then fades. 'top' or 'bottom' picks the corner; 'off' "
            "disables it. Rendered pre-quantization so it shows on every display "
            "mode (like --frame-numbers).",
            "choices": ("bottom", "top", "off"),
        },
    )
    loop_audio: str = field(
        default="on",
        metadata={
            "help": "What happens to a video's audio once a transport.* action "
            "touches the scene: 'on' (default) keeps audio playing and re-syncs "
            "it across every seek/pause/loop splice; 'mute' restores the Phase-2 "
            "escape valve (audio mutes for the rest of that scene's run). "
            "Falls back to mute behavior automatically when the scene has no "
            "audio. The REU-pump audio path is always forced off under "
            "transport regardless.",
            "choices": ("on", "mute"),
        },
    )
    cc_map: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(d) for d in _DEFAULT_MIDI_CC_MAP],
        metadata={
            "help": "MIDI-message -> action mappings ([[midi_control.cc_map]] "
            "tables); see --describe section:midi_control. Set to [] to disable "
            "the shipped defaults, or override/extend individual entries. Each "
            "entry: type ('cc'|'note'|'pc'|'mmc'), number (0-127 for cc/note/pc; "
            "an MMC command byte — 0x01 stop, 0x02 play, 0x04 FF, 0x05 RW, 0x06 "
            "record, 0x09 pause — for mmc), action ('pause'|'resume'|"
            "'toggle_pause'|'skip'|'cycle_style'|'jump'|'param'|"
            "'transport.play_pause'|'transport.stop'|'transport.loop_toggle'|"
            "'transport.rw'|'transport.ff'|'transport.jog'|'transport.record'|"
            "'loop_slot'); 'jump' also needs an int scene; 'param' also needs "
            "a string target ('effect.<name>', 'source.<name>', 'scene.<name>' "
            "for scope scenes, or 'mode.<name>' for the display mode's live "
            "color knobs — dither_strength/method, motion_smoothing, "
            "auto_fit_strength, cell_strategy, color_match, palette_mode). A "
            "knob (cc) sweeps a scalar or bucket-selects a choice; a note/pad "
            "cycles a choice. The transport.* actions give DJ-style control of "
            "a playing video scene (pause-in-place, seek, RW/FF with "
            "acceleration while a note is held, an A/B loop, and a rotary "
            "jog — 'mode' 'abs'|'rel', default 'rel'); once touched, that "
            "scene's audio follows every seek/pause/loop by default (see "
            "loop_audio, and docs/architecture.md's transport note). A 'mmc' "
            "entry also "
            "matches an MMC transport SysEx from a DAW/controller. "
            "'transport.record' arms a loop (Record -> Stop workflow, red "
            "border while armed); 'loop_slot' also needs an int 'slot' >= 1 "
            "(a pad number) and recalls that per-video saved loop on a plain "
            "press, saves the current loop into it while Stop is held, or "
            "clears it while Record is held (note mappings only — an mmc "
            "record/stop can't reliably hold for the chord, since MMC has no "
            "release event). 'look_save'/'look_recall' (Phase 6) each need an "
            "int 'slot' >= 1 — a look captures the active clip + effect-chain "
            "state on save and re-fires it on recall."
        },
    )
    controller_profile: str = field(
        default="auto",
        metadata={
            "help": "Which learned controller profile (from --midi-setup) to "
            "layer under this config's cc_map. 'auto' (default) loads the stored "
            "profile whose learned port name matches the opened MIDI port; a "
            "'<name>' loads that named profile (the file stem under the "
            "controllers data dir); 'off' ignores profiles entirely. Merge "
            "precedence is shipped-defaults < profile < an explicit cc_map here: "
            "with no cc_map set, a profile can reclaim the default note/CC "
            "assignments; an explicit cc_map (including []) always wins over the "
            "profile. Requires [midi_control] to be enabled; needs no extra."
        },
    )
    # Non-persisted: True until a TOML layer (machine settings, project/per-system,
    # or master) actually specifies a `cc_map` key. It decides the profile-merge
    # order (see midi_control.resolve_effective_cc_map): when the user authored no
    # cc_map (still the shipped defaults), a profile layers OVER the defaults and
    # can reclaim them; once the user wrote an explicit cc_map, their entries win
    # over the profile and the defaults are not re-injected. `compare=False` keeps
    # it out of Config equality (so load(dumps(cfg)) == cfg holds), and the
    # `internal` metadata keeps it out of --describe / the schema / serialized TOML
    # (introspect._field_docs skips it).
    cc_map_is_default: bool = field(default=True, compare=False, metadata={"internal": True})


# Clip-launch grid choices (Live DJ/VJ Phase 2). Mirrored in
# performance.py's launch engine; the drift/validation lives in
# _validate_clips below so config stays the authority.
_CLIP_LAUNCH_CHOICES = ("trigger", "gate", "toggle")
_CLIP_QUANTIZE_CHOICES = ("off", "beat", "bar")
_CLIP_PAD_TYPE_CHOICES = ("note", "pc")
# Keys a [[performance.clips]] table carries on top of the scene-spec fields it
# shares with [[scenes]] (SceneCfg): the launch semantics + pad binding.
_CLIP_LAUNCH_KEYS: tuple[str, ...] = ("slot", "pad", "pad_type", "launch", "quantize", "loop")
# Scene-spec fields a clip may NOT carry (deferred to a later phase / ensemble-
# only). Overlays + orchestrate/follower belong to declared [[scenes]] only.
_CLIP_SCENE_FIELD_DENY: frozenset[str] = frozenset({"overlays", "orchestrate", "follower_only"})


@dataclass
class PerformanceCfg:
    """Live-performance tempo/beat grid (Phase 1 of the Live DJ/VJ arc).

    Drives a process-wide :class:`~c64cast.control.tempo.TempoClock` — a musical beat
    grid every performance consumer reads GIL-atomically (launch quantization,
    effect tempo-lock, WLED). The grid takes tempo from an external MIDI clock
    (fed by [midi_control]'s reader thread, so `[midi_control].enabled` must be
    on to receive one) or free-runs internally at `bpm`. All tempo handling is
    in-memory only — it never touches the DMA socket."""

    tempo_source: str = field(
        default="internal",
        metadata={
            "help": "Where the beat grid gets its tempo: 'internal' (free-run at "
            "`bpm`, with a `tempo_tap` pad for live tapping), 'midi' (follow an "
            "external MIDI clock — 0xF8 clock / start / stop / song-position — "
            "which arrives via the [midi_control] listener, so enable that too), "
            "or 'audio' (lock to the beat the live-input analyzer detects — the "
            "audio_source = 'mic'/'listen' scene's reactive tempo drives launch "
            "quantize, mod_source='clock' effects and WLED tempo). With 'midi' or "
            "'audio' the grid idles until the first clock byte / detected tempo.",
            "choices": ("internal", "midi", "audio"),
        },
    )
    bpm: float = field(
        default=120.0,
        metadata={
            "help": "Static tempo (beats per minute) for internal drive, and the "
            "starting tempo before an external clock is measured. A tap-tempo pad "
            "overrides it live."
        },
    )
    beats_per_bar: int = field(
        default=4,
        metadata={
            "help": "Beats per bar (the numerator of the time signature) — sets "
            "where bar boundaries fall for bar-quantized launches and bar-locked "
            "effects."
        },
    )
    clock_port: str | None = field(
        default=None,
        metadata={
            "help": "MIDI input port to read the external clock from when it "
            "arrives on a DIFFERENT port than the [midi_control] control surface "
            "(substring match, case-insensitive). None (default) = read clock on "
            "the same port as control. Only used when tempo_source = 'midi'."
        },
    )
    midi_feedback: bool = field(
        default=False,
        metadata={
            "help": "Light a grid controller's pads to show performance state "
            "(Live DJ/VJ Phase 4): loaded clip pads dim, the arming pad blinks, "
            "the live clip bright, and enabled effect-chain layers lit — all over "
            "a MIDI OUTPUT port ([midi_control] must be enabled). The C64 screen "
            "stays audience-facing; this replaces on-screen readouts with "
            "controller LEDs. The velocity->color convention is per-controller and "
            "comes from the learned controller profile's `feedback` block "
            "(--midi-setup writes it); Launchpad-X palette defaults otherwise. "
            "Needs a grid that lights pads from note-on velocity (Novation "
            "Launchpad, Akai APC/MPC, Ableton Push); Arturia and other "
            "SysEx-only controllers won't light — use the web console for those."
        },
    )
    feedback_port: str | None = field(
        default=None,
        metadata={
            "help": "MIDI OUTPUT port for LED feedback (substring match, "
            "case-insensitive). None (default) = the profile's own port, else the "
            "same device as the [midi_control] input, else the first output. Only "
            "used when midi_feedback = true."
        },
    )
    clips: list[dict[str, Any]] = field(
        default_factory=list,
        metadata={
            "help": "Clip-launch grid ([[performance.clips]], Live DJ/VJ Phase 2): "
            "each table is a scene spec (any [[scenes]] field — type, file, "
            "source, display, name, duration_s, effect …) plus launch "
            "semantics: `slot` (1-based id, unique), `pad`/`pad_type` (the "
            "note/PC number that fires it, auto-mapped when [midi_control] is "
            'on), `launch` ("trigger"|"gate"|"toggle"), `quantize` '
            '("off"|"beat"|"bar" — align the swap to the beat grid), and '
            "`loop` (repeat until another clip fires). Fired from a controller "
            "or the web console; the scene is built on a background thread "
            "during the count-in and swapped in on the grid boundary. Empty = "
            "no grid."
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
    broadcast_tempo_fallback: bool = field(
        default=False,
        metadata={
            "help": "Mode 3 performance glue (Live DJ/VJ Phase 6): when the "
            "on-screen scene has NO SID features to broadcast (a video, webcam, "
            "or slideshow), fall back to the [performance] beat grid so WLED "
            "strips keep pulsing to the MIDI/tap tempo instead of going dark. "
            "The synthesized pulse spikes on each beat (from the TempoClock "
            "phase); only active while the grid is running. Off (default) = a "
            "non-SID scene broadcasts nothing, matching pre-Phase-6 behavior. A "
            "SID-driven scene always wins over the fallback."
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
    audio_features: AudioFeaturesCfg = field(default_factory=AudioFeaturesCfg)
    control: ControlPlaneCfg = field(default_factory=ControlPlaneCfg)
    midi_control: MidiControlCfg = field(default_factory=MidiControlCfg)
    performance: PerformanceCfg = field(default_factory=PerformanceCfg)
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


def _validate_video_device(video: VideoCfg) -> None:
    """Offline syntax check for [video].device — an int index, or a string
    matched by camera name substring / USB VID:PID. Rejects a malformed VID:PID
    at load time; actual name/VID resolution (which enumerates hardware and
    needs the 'camera' extra) is deferred to WebcamSource construction."""
    from c64cast.control import (
        camera,  # local: keep the optional-feature module off the hot import path
    )

    camera.parse_camera_device(video.device, field_name="[video].device")


def _validate_audio_device(audio: AudioCfg) -> None:
    """Offline syntax check for [audio].device — an int index or a string matched
    to a sounddevice input by name substring. PortAudio devices have no USB
    VID:PID, so (unlike [video].device) the only failure mode is an empty string;
    actual name->index resolution is deferred to AudioStreamer at runtime."""
    if isinstance(audio.device, str) and not audio.device.strip():
        raise ConfigError("[audio].device: empty audio device string")


def _validate_performance(perf: PerformanceCfg) -> None:
    """Range/choice-check [performance] at load time so a bad tempo grid surfaces
    before the run, not mid-performance. Raises ValueError (wrapped like the
    other section validators here)."""
    if perf.tempo_source not in ("internal", "midi", "audio"):
        raise ValueError(
            "[performance].tempo_source must be 'internal', 'midi' or 'audio', "
            f"got {perf.tempo_source!r}"
        )
    if isinstance(perf.bpm, bool) or not isinstance(perf.bpm, (int, float)):
        raise ValueError(f"[performance].bpm must be a number, got {perf.bpm!r}")
    if not 20.0 <= float(perf.bpm) <= 400.0:
        raise ValueError(f"[performance].bpm must be 20..400, got {perf.bpm}")
    if isinstance(perf.beats_per_bar, bool) or not isinstance(perf.beats_per_bar, int):
        raise ValueError(f"[performance].beats_per_bar must be an int, got {perf.beats_per_bar!r}")
    if not 1 <= perf.beats_per_bar <= 32:
        raise ValueError(f"[performance].beats_per_bar must be 1..32, got {perf.beats_per_bar}")
    if perf.clock_port is not None and not isinstance(perf.clock_port, str):
        raise ValueError(
            f"[performance].clock_port must be a string or unset, got {perf.clock_port!r}"
        )
    if not isinstance(perf.midi_feedback, bool):
        raise ValueError(
            f"[performance].midi_feedback must be true/false, got {perf.midi_feedback!r}"
        )
    if perf.feedback_port is not None and not isinstance(perf.feedback_port, str):
        raise ValueError(
            f"[performance].feedback_port must be a string or unset, got {perf.feedback_port!r}"
        )
    _validate_clips(perf.clips)


# The full set of keys a [[performance.clips]] table may use: the launch/pad
# keys plus every SceneCfg field it's allowed to carry (built lazily so it
# tracks SceneCfg without a hand-maintained duplicate).
def _clip_allowed_keys() -> set[str]:
    scene_fields = {f.name for f in fields(SceneCfg)} - _CLIP_SCENE_FIELD_DENY
    return scene_fields | set(_CLIP_LAUNCH_KEYS)


def _validate_clips(clips: list[dict[str, Any]]) -> None:
    """Validate the [[performance.clips]] grid at load time: each entry is a
    table with a unique int `slot` >= 1, launch/quantize/pad_type within their
    choice sets, an in-range `pad`, a real scene `type`, and no unknown keys
    (difflib "did you mean"). The embedded scene spec's deeper validation
    (display/file per type) is deferred to build time — build_scene runs the
    full validate_scene_cfg when the clip is fired."""
    allowed = _clip_allowed_keys()
    seen_slots: set[int] = set()
    for i, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise ValueError(f"[[performance.clips]][{i}] must be a table, got {clip!r}")
        for k in clip:
            if k not in allowed:
                close = difflib.get_close_matches(k, allowed, n=1)
                hint = f" — did you mean {close[0]!r}?" if close else ""
                raise ValueError(f"[[performance.clips]][{i}] unknown key {k!r}{hint}")
        slot = clip.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
            raise ValueError(f"[[performance.clips]][{i}] needs an int `slot` >= 1, got {slot!r}")
        if slot in seen_slots:
            raise ValueError(f"[[performance.clips]][{i}] duplicate slot {slot}")
        seen_slots.add(slot)
        stype = clip.get("type", "webcam")
        if stype not in SCENE_TYPES:
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) type must be one of "
                f"{SCENE_TYPES}, got {stype!r}"
            )
        launch = clip.get("launch", "trigger")
        if launch not in _CLIP_LAUNCH_CHOICES:
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) launch must be one of "
                f"{_CLIP_LAUNCH_CHOICES}, got {launch!r}"
            )
        quantize = clip.get("quantize", "bar")
        if quantize not in _CLIP_QUANTIZE_CHOICES:
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) quantize must be one of "
                f"{_CLIP_QUANTIZE_CHOICES}, got {quantize!r}"
            )
        pad_type = clip.get("pad_type", "note")
        if pad_type not in _CLIP_PAD_TYPE_CHOICES:
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) pad_type must be one of "
                f"{_CLIP_PAD_TYPE_CHOICES}, got {pad_type!r}"
            )
        pad = clip.get("pad")
        if pad is not None and (
            isinstance(pad, bool) or not isinstance(pad, int) or not 0 <= pad <= 127
        ):
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) pad must be 0..127, got {pad!r}"
            )
        loop = clip.get("loop")
        if loop is not None and not isinstance(loop, bool):
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) loop must be true/false, got {loop!r}"
            )


def clip_scene_cfg(clip: dict[str, Any]) -> SceneCfg:
    """Build a :class:`SceneCfg` from a [[performance.clips]] table by stripping
    the launch/pad keys and applying the remaining scene-spec fields — the same
    field set (and `_apply_section` path) a declared ``[[scenes]]`` block uses,
    so a clip inherits every scene knob for free. Called by the launch engine's
    build factory (see cli.build_stack / performance.PerformanceSession).

    `loop` and `duration_s` interact: a looping non-video clip is forced to
    ``duration_s = 0`` (run forever) so it holds until another clip fires; the
    launch engine re-runs a finished video clip instead (video rejects
    duration_s). A one-shot clip keeps its scene-type default duration."""
    scene_keys = {k: v for k, v in clip.items() if k not in _CLIP_LAUNCH_KEYS}
    sc = SceneCfg()
    _apply_section(sc, scene_keys, "performance.clips")
    if clip.get("loop", True) and sc.type in _CLIP_CONTINUOUS_TYPES and sc.duration_s is None:
        # Loop forever: hold the continuous-frame scene on screen until the next
        # clip launch, rather than auto-advancing at the scene-type default
        # (e.g. 30 s). Audio-bearing/video clips instead re-setup on is_done (the
        # launch engine's loop path), so their timing/song-length logic is kept.
        sc.duration_s = 0.0
    return sc


# Clip scene types whose "loop" is a continuous hold (run-forever) rather than a
# re-setup on end — the frame-based visual generators with no natural endpoint.
_CLIP_CONTINUOUS_TYPES = frozenset({"webcam", "blank", "slideshow", "generative", "wled"})


def _validate_sid_panning(u64: Ultimate64Cfg) -> None:
    """Range-check [ultimate64].sid_panning at load/doctor time so a bad pan
    value surfaces before the playlist runs, not mid-scene when the mixer is
    configured. Values stay as authored (ints or labels); sid_panning.
    resolve_panning normalizes them at apply time."""
    if not u64.sid_panning:
        return
    if not isinstance(u64.sid_panning, list):
        raise ValueError(
            f"ultimate64.sid_panning must be a list of pan values, got {u64.sid_panning!r}"
        )
    if len(u64.sid_panning) > MAX_PANNED_SOURCES:
        raise ValueError(
            f"ultimate64.sid_panning accepts at most {MAX_PANNED_SOURCES} entries "
            f"(the U64 has one pan control per audio source: 2 SID sockets + 2 "
            f"UltiSID cores), got {len(u64.sid_panning)}"
        )
    try:
        normalize_pan_spec(u64.sid_panning)
    except ValueError as e:
        raise ValueError(f"ultimate64.sid_panning: {e}") from e


def _validate_sid_volume(u64: Ultimate64Cfg) -> None:
    """Range-check [ultimate64].sid_volume at load/doctor time so a level the
    mixer can't represent surfaces before the playlist runs, not mid-scene when
    the mixer is configured. Values stay as authored (ints or labels);
    sid_volume.resolve_volumes normalizes them at apply time."""
    if not u64.sid_volume:
        return
    if not isinstance(u64.sid_volume, list):
        raise ValueError(f"ultimate64.sid_volume must be a list of levels, got {u64.sid_volume!r}")
    if len(u64.sid_volume) > MAX_VOLUME_SOURCES:
        raise ValueError(
            f"ultimate64.sid_volume accepts at most {MAX_VOLUME_SOURCES} entries "
            f"(the U64 has one volume control per audio source: 2 SID sockets + 2 "
            f"UltiSID cores), got {len(u64.sid_volume)}"
        )
    try:
        normalize_volume_spec(u64.sid_volume)
    except ValueError as e:
        raise ValueError(f"ultimate64.sid_volume: {e}") from e


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


# Scalar config sections whose TOML section name equals the Config attribute
# name. Applied uniformly by _apply_toml_sections. [color] is handled out of
# band (it carries the hue_corrections list-of-tables); [[scenes]]/[ensemble]
# never go through here (playlists/ensemble metadata are load-/master-specific).
_TOML_SCALAR_SECTIONS: tuple[str, ...] = (
    "hardware",
    "teensyrom",
    "ultimate64",
    "video",
    "audio",
    "vision",
    "interstitial",
    "playlist",
    "debug",
    "preview",
    "recording",
    "dsp",
    "audio_features",
    "control",
    "midi_control",
    "performance",
    "menu",
    "wled",
)


def _apply_toml_sections(cfg: Config, data: dict[str, Any], *, source: str) -> None:
    """Apply the scalar + [color] sections of a parsed TOML dict onto `cfg`
    in place.

    Shared by :func:`load` (a project / per-system file) and
    :func:`apply_machine_settings` (the machine-settings file) so both go
    through identical unknown-key difflib warnings, the tri-state / device
    validations, and the [color]/hue_corrections special case. Deliberately
    does NOT handle [[scenes]] or [ensemble] — those are load- / master-
    specific. `source` names the origin for the debug log only (the unknown-key
    warnings already carry the section name)."""
    log.debug("applying config sections from %s", source)
    for name in _TOML_SCALAR_SECTIONS:
        if name in data:
            _apply_section(getattr(cfg, name), data[name], name)

    # Record whether any layer explicitly authored a cc_map. Monotonic (only ever
    # set False, default True): once machine settings OR the project/per-system
    # file specifies cc_map, the effective mapping is the user's own, not the
    # shipped defaults — which flips the profile-merge order (see
    # midi_control.resolve_effective_cc_map). Both layers route through here.
    mc = data.get("midi_control")
    if isinstance(mc, dict) and "cc_map" in mc:
        cfg.midi_control.cc_map_is_default = False

    _validate_use_reu_staged(cfg.video)
    _validate_double_buffer(cfg.video)
    _validate_video_device(cfg.video)
    _validate_audio_device(cfg.audio)
    _validate_performance(cfg.performance)
    _validate_sid_panning(cfg.ultimate64)
    _validate_sid_volume(cfg.ultimate64)

    # [color] is handled separately from the scalar section loop because it
    # carries a list-of-tables field (hue_corrections) that must be pulled out
    # before _apply_section, same as [[scenes.overlays]] in load().
    if "color" in data:
        raw_color = dict(data["color"])
        raw_hc = raw_color.pop("hue_corrections", [])
        _apply_section(cfg.color, raw_color, "color")
        for hc in raw_hc:
            if not isinstance(hc, dict):
                raise ValueError(f"color.hue_corrections entry must be a table, got {hc!r}")
            cfg.color.hue_corrections.append(dict(hc))
        _validate_force_palette(cfg.color)


def load_machine_settings() -> dict[str, Any]:
    """Parse the machine-settings TOML at :func:`paths.settings_path` into a
    raw dict (the machine layer that applies to *every* run type — see
    :func:`apply_machine_settings`).

    A missing file returns ``{}`` (machine settings are optional). A parse or
    permission error raises :class:`ConfigError` naming the path. ``[[scenes]]``
    and ``[ensemble]`` are rejected with a warning and dropped: machine settings
    hold cross-run defaults (connection, capture device, SID model, …), not
    playlists or ensemble topology."""
    path = paths.settings_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except PermissionError as e:
        raise ConfigError(f"Could not read machine settings {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(_format_toml_error(str(path), e)) from e

    for banned in ("scenes", "ensemble"):
        if banned in data:
            log.warning(
                "machine settings %s: [%s] ignored — machine settings hold "
                "cross-run defaults (connection, device, …), not playlists",
                path,
                banned,
            )
            data.pop(banned, None)
    return data


def apply_machine_settings(cfg: Config) -> Config:
    """Overlay the machine-settings file onto `cfg` in place (defaults →
    machine settings), returning it.

    This is the lowest layer above the dataclass defaults; everything that
    applies afterward still wins (project / per-system TOML, the master
    cascade, CLI flags, the ``C64CAST_DMA_PASSWORD`` env var). When a file was
    actually loaded, one INFO line logs its path + a rough field count so the
    origin of a surprising default is discoverable."""
    data = load_machine_settings()
    if not data:
        return cfg
    _apply_toml_sections(cfg, data, source="machine settings")
    n_fields = sum(len(v) for v in data.values() if isinstance(v, dict))
    log.info("machine settings: %s (%d fields)", paths.settings_path(), n_fields)
    return cfg


def load(path: str | None) -> Config:
    """Load a Config from a TOML file path, or from the default search path
    if `path` is None, or return defaults if neither exists.

    `path` semantics:
      - None  → look for ./c64cast.toml; missing is fine.
      - str   → load that file; missing raises ConfigError.

    The machine-settings layer (:func:`apply_machine_settings`) is applied
    first — before the file's own sections — so the file (and later CLI/env)
    override it.

    Parse failures (TOML syntax errors, missing file when path is given)
    raise `ConfigError` with a message formatted for end-user display."""
    cfg = Config()
    apply_machine_settings(cfg)
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

    _apply_toml_sections(cfg, data, source=path)

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
    # path is per-system: every system records its own stream, and a cascaded
    # master path would point N cv2.VideoWriters at one file. Systems that
    # leave it alone get a name-derived default instead (resolve_recording_path).
    ("recording", frozenset({"path"})),
    ("color", frozenset()),
    ("performance", frozenset()),
    ("menu", frozenset()),
)


def apply_master_defaults(
    defaults: Config, sys_cfg: Config, baseline: Config | None = None
) -> Config:
    """Cascade master-TOML defaults into a per-system Config.

    For each cascaded section, fields that the per-system file left at the
    `baseline` value inherit the master's value (when the master itself
    set something other than the baseline). Fields the per-system file
    explicitly set keep their values.

    `baseline` is the "unset" reference the comparison is measured against.
    It defaults to a fresh blank Config (dataclass defaults), but the ensemble
    loader passes a **machine-settings-overlaid** Config so that a value coming
    only from the machine layer counts as "not set by this system" and can
    still be overridden by the master TOML — keeping the precedence
    machine < master < per-system intact.

    Approximation worth knowing about: "the user explicitly set this field"
    is detected as "the field value differs from the `baseline` instance".
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
        blank = getattr(baseline, section_name) if baseline is not None else type(sys_section)()
        for f in fields(sys_section):
            if f.name in skip_fields:
                continue
            blank_val = getattr(blank, f.name)
            master_val = getattr(master_section, f.name)
            sys_val = getattr(sys_section, f.name)
            if sys_val == blank_val and master_val != blank_val:
                setattr(sys_section, f.name, master_val)
    return sys_cfg


def resolve_recording_path(recording: RecordingCfg, system_name: str, *, is_ensemble: bool) -> str:
    """Per-system output file for `[recording]`, unexpanded.

    cv2.VideoWriter has no notion of sharing a file, so N systems opening one
    path produce one truncated stream rather than N recordings. In an ensemble
    a system that left `path` at the default gets the system name folded into
    the stem — `recording.mp4` -> `recording-left.mp4` — which is the same
    disambiguation the preview window applies to its HighGUI title.

    An explicit per-system `path` is honored verbatim: the user naming the file
    outranks a scheme for naming it, and two systems pointed at one name are
    caught by doctor rather than silently renamed. "Explicit" is the same
    approximation :func:`apply_master_defaults` makes — a value differing from
    the dataclass default — so a per-system file that spells out the default
    is treated as not having set it, and still gets a distinct name.
    """
    if not is_ensemble or recording.path != RecordingCfg.path:
        return recording.path
    stem, ext = os.path.splitext(recording.path)
    return f"{stem}-{system_name}{ext}"


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
            # No config file anywhere — still apply the machine layer so a
            # bare `c64cast` run (no --config, no positional media) inherits
            # machine settings (connection, device, …).
            cfg = Config()
            apply_machine_settings(cfg)
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

    # Master defaults start from the machine layer (defaults → machine →
    # master), so a machine setting is the baseline the master TOML overrides.
    defaults = Config()
    apply_machine_settings(defaults)
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
        ("performance", defaults.performance),
        ("menu", defaults.menu),
    ):
        if section in raw:
            _apply_section(dc, raw[section], section)

    # Same cc_map-authored tracking as _apply_toml_sections, for the master TOML
    # (which applies its sections through this separate path). [midi_control] is
    # process-wide, so the master is the authoritative layer in ensemble mode.
    master_mc = raw.get("midi_control")
    if isinstance(master_mc, dict) and "cc_map" in master_mc:
        defaults.midi_control.cc_map_is_default = False

    _validate_use_reu_staged(defaults.video)
    _validate_double_buffer(defaults.video)
    _validate_performance(defaults.performance)

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

    # The "unset" baseline for the per-system cascade is a machine-overlaid
    # Config (not a blank one): a field coming only from the machine layer must
    # still be treated as "this system didn't set it" so the master TOML can
    # override it — machine < master < per-system. Built once, reused per system.
    machine_baseline = Config()
    apply_machine_settings(machine_baseline)

    master_dir = os.path.dirname(os.path.abspath(path))
    cfgs: list[Config] = []
    sys_paths: list[str | None] = []
    for entry in ensemble.systems:
        sub_path = entry.config
        if not os.path.isabs(sub_path):
            sub_path = os.path.join(master_dir, sub_path)
        sys_cfg = load(sub_path)
        sys_cfg = apply_master_defaults(defaults, sys_cfg, baseline=machine_baseline)
        # Per-system Configs never carry ensemble metadata themselves —
        # only the master TOML does. (Belt and braces: load() never sets
        # ensemble either since it doesn't know about [ensemble].)
        sys_cfg.ensemble = None
        cfgs.append(sys_cfg)
        sys_paths.append(sub_path)
    _warn_audio_only_ensemble(cfgs, [e.name for e in ensemble.systems])
    return LoadResult(
        cfgs=cfgs,
        names=[e.name for e in ensemble.systems],
        paths=sys_paths,
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
