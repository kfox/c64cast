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
import copy
import difflib
import functools
import logging
import os
import pathlib
import re
import tomllib
from dataclasses import dataclass, field, fields
from typing import Any

from c64cast._redact import redact_secrets
from c64cast.audio.dac_curves import DAC_CURVE_CHOICES
from c64cast.audio.dsp import DSPParams
from c64cast.audio.sampler import SAMPLER_REF_CLOCK_DEFAULT
from c64cast.sid.sid_autoconfig import SID_MODEL_CHOICES
from c64cast.sid.sid_panning import MAX_PANNED_SOURCES, normalize_pan_spec
from c64cast.sid.sid_volume import MAX_VOLUME_SOURCES, normalize_volume_spec
from c64cast.video.dither import DITHER_METHODS
from c64cast.video.flicker import DEFAULT_TOLERANCE, FLICKER_TOLERANCES
from c64cast.video.palette import (
    CELL_STRATEGIES,
    COLOR_MATCH_MODES,
    HIRES_CELL_PICKS,
    resolve_color,
)
from c64cast.wled.wled_sink import DDP_PORT, WLED_REALTIME_PORT

from . import connect, paths

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
SYSTEM_CHOICES = ("auto", "NTSC", "PAL")
# [ultimate64].sid_play_rate. "auto"/"off" plus any positive float (Hz), so the
# schema carries this as a union rather than a plain enum — see schema.py.
SID_PLAY_RATE_CHOICES = ("auto", "off")
SID_VIDEO_MODE_CHOICES = ("off", "auto")
# [ultimate64].hdmi_scan_resolution. "auto"/"keep" plus the firmware's own
# scan_modes[] labels; mirrors hw_provision.HDMI_RESOLUTION_CHOICES, which
# tests/test_introspect.py pins this against.
HDMI_SCAN_RESOLUTION_CHOICES = (
    "auto",
    "keep",
    "SD (480p/576p)",
    "HD (720p)",
    "FullHD (1080p)",
    "PC 800 x 600",
    "PC 1024 x 768",
    "PC 1280 x 1024",
)
# Mirrors backend.BACKENDS; duplicated here so config.py stays import-light
# (it doesn't pull in api.py). tests/test_introspect.py asserts they match.
_BACKEND_CHOICES = ("ultimate", "teensyrom")
# Unlike [ultimate64].sid_model ("off" = don't touch the hardware config),
# the opt-out here is "unknown": there is no hardware config to touch, only
# a claim about the machine that a verdict can be rendered from.
HOST_SID_MODEL_CHOICES = ("auto", "6581", "8580", "unknown")
# Per-chip models for host_sid_chips. No "auto": an entry names a chip the user
# is asserting exists, so there is nothing to infer — "unknown" covers a chip
# whose model they don't know.
HOST_SID_CHIP_MODEL_CHOICES = ("6581", "8580", "unknown")
# How a multi-entry waveform pool treats a tune the machine's own chips can't
# render as authored. "prefer" is a bias, not a filter: it only changes which
# candidate is tried first, so a pool with no match still plays something.
HOST_SID_TUNE_MATCH_CHOICES = ("off", "prefer", "require")
# The window a PSID second/third-SID address byte can land in ($D000 | byte<<4,
# see sid_host_emu._decode_extra_sid_addr), so a declared chip address and a
# tune's declared chip address are range-checked against the same bounds.
_HOST_SID_ADDR_LO = 0xD000
_HOST_SID_ADDR_HI = 0xDFF0
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

#: The config sections a **reload** picks up. A reload re-reads each system's
#: TOML and hands the playlist a fresh scene list, so `[[scenes]]` (always) plus
#: these take effect on a running show; everything else — the connection, the
#: audio and video threads, the control surfaces — is built once at startup and
#: needs the session restarted. `session.reload_all` is what makes this true and
#: a test pins the two together; it lives here so the web console can say, at
#: the moment of saving, which of your changes a reload will actually apply.
RELOADABLE_SECTIONS: frozenset[str] = frozenset({"interstitial", "playlist"})
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
# them symbolically. `applies_to` means scene types and nothing else — only
# SceneCfg fields carry it, and every value is a member of this tuple
# (tests/test_introspect.py pins both halves). A section field that is
# meaningful only on some backend or display mode says so in its `help`:
# one metadata key silently spanning three vocabularies is a trap for the
# first consumer that applies the documented rule generically.
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
    # Machines with an internal dual-SID mod (ARM2SID, SIDFX, DualSID) carry a
    # second chip the single-valued host_sid_model can't describe — often set to
    # the *other* model, which is the whole point of running one. Keyed by
    # address rather than a parallel list of models so the two can't desync;
    # matching against a tune's chips is by address anyway.
    host_sid_chips: dict[str, str] = field(
        default_factory=dict,
        metadata={
            "help": "Internal SID chips in the C64 being driven, as "
            "address=model (e.g. d400='6581', d420='8580') — for machines with "
            "a dual-SID mod, whose second chip host_sid_model can't describe. "
            "When set it supersedes host_sid_model, so no NTSC/PAL assumption "
            "is made. Ignored where the live SID state is readable (U64).",
        },
    )
    # Tune *selection*, as opposed to the chip configuration above: on a link
    # that can't re-place chips, a 2SID tune or a wrong-model tune is heard as
    # authored on the Ultimate's own output and as mush through the C64's AV
    # output, and no setting can change that. What can be changed is which tune
    # a directory pool picks.
    #
    # Default "off" because a directory the user pointed at is a statement of
    # what they want played, and quietly narrowing it to what this machine
    # renders best is their call to make. Acts only on a declaration, never on
    # the NTSC/PAL assumption — see sid_resolved.host_chip_fit.
    host_sid_tune_match: str = field(
        default="off",
        metadata={
            "help": "Bias a multi-file waveform pool toward tunes the C64's own "
            "SID chips can play as authored (right model, and a chip at every "
            "address the tune drives). 'prefer' tries fitting tunes first but "
            "falls back to the rest; 'require' skips non-fitting tunes outright. "
            "Needs host_sid_chips or an explicit host_sid_model — an assumed "
            "model is never acted on. Ignored where the live SID state is "
            "readable (U64), which re-places chips per tune instead.",
            "choices": HOST_SID_TUNE_MATCH_CHOICES,
        },
    )
    # The 16 colors the display shows are a property of the machine driving it,
    # not of the link — an Ultimate 64's FPGA VIC and a real VIC-II are ~25
    # counts per channel apart, and the quantizer picks indices by distance, so
    # aiming at the wrong table sends ~19% of pixels to the wrong color. Same
    # reasoning as host_sid_model above: declared here, resolved against the
    # machine when the machine can answer.
    host_palette: str = field(
        default="auto",
        metadata={
            "help": "The 16 colors the C64 being driven actually emits, which "
            "the quantizer aims at. 'auto' (default) reads it from the machine "
            "where it can — an Ultimate 64 reports its own palette — and "
            "otherwise assumes a real VIC-II. 'u64' is the Ultimate 64's own "
            "table; 'pepto' is the classic VIC-II rendering, right for a real "
            "C64 (so for an Ultimate II+, and for a TeensyROM+ in a breadbin). "
            "Can also be the path to a VICE .vpl file, which is how to describe "
            "a machine with a custom palette loaded.",
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
        metadata={
            "help": "Base URL of the Ultimate 64 (REST + DMA host). A bare host and "
            "the u64://HOST form -u/--url takes both work here and read as "
            "http://HOST; a ?query knob does not, because every one of them is a "
            "field in this section."
        },
    )
    system: str = field(
        default="auto",
        metadata={
            "help": "Machine timing standard (affects frame rate, CPU clock, SID PLAY "
            "rate). 'auto' reads it from the Ultimate's live System Mode; on a "
            "backend that can't be asked, or under --skip-probe, it falls back to NTSC.",
            "choices": SYSTEM_CHOICES,
        },
    )
    # Fixing SID playback TEMPO. The kernal's jiffy IRQ — which the SID player
    # chains PLAY onto — runs at ~60 Hz on BOTH standards, so a PAL vsync tune
    # plays ~19.7% fast unless something reprograms CIA #1 Timer A. This does.
    # Orthogonal to sid_video_mode below, which fixes pitch.
    sid_play_rate: str | float = field(
        default="auto",
        metadata={
            "help": "PLAY-call rate for vsync-timed SID tunes. 'auto' = the tune's "
            "native frame rate from its PSID clock flag (PAL tunes at ~50.12 Hz); "
            "'off' = leave the kernal jiffy rate alone (~60 Hz on both standards, so "
            "PAL tunes run ~20% fast — the pre-1.9 behavior); a number pins every "
            "vsync tune to that rate in Hz. CIA-timed (multispeed) tunes always "
            "self-time and are never overridden.",
            "choices": SID_PLAY_RATE_CHOICES,
        },
    )
    # Fixing SID playback PITCH. Opt-in because it retunes the HDMI output
    # (576p50 vs 480p60) and every capture device has to re-lock — some don't
    # handle 576p50 well at all.
    sid_video_mode: str = field(
        default="off",
        metadata={
            "help": "Switch the Ultimate's System Mode so the machine's PAL/NTSC "
            "timing matches [ultimate64].system, correcting SID pitch (phi2 differs "
            "3.8% between standards). 'off' leaves it alone. Ultimate 64 only; live "
            "and volatile, restored at teardown. Changes the HDMI output mode.",
            "choices": SID_VIDEO_MODE_CHOICES,
        },
    )
    hdmi_scan_resolution: str = field(
        default="auto",
        metadata={
            "help": "The Ultimate 64's HDMI upscaler. 'auto' raises SD to HD (720p) "
            "only when sid_video_mode retimes the machine — PAL timing at SD puts "
            "576p50 on the wire and some capture devices cannot lock to it, while "
            "the same machine at 720p50 captures cleanly. 'keep' never touches it; "
            "a scan-mode label sets it for the run (the 'PC' modes are passed "
            "through from the firmware but are untested under PAL timing). Live and "
            "volatile, restored at teardown. Newer U64 boards only (older firmware "
            "has no such setting).",
            "choices": HDMI_SCAN_RESOLUTION_CHOICES,
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
            "a .sid file's PSID header requests: on the U64 by remapping to a "
            "matching physical socket or an UltiSID core, on the Ultimate II+ by "
            "setting each emulated SID that snoops a tune chip to that model. "
            "'off' disables. An explicit '6581'/'8580' forces that model for "
            "every chip, ignoring the header.",
            "choices": SID_MODEL_CHOICES,
        },
    )
    # Applied live to the device's SID mixer before playback and restored at
    # teardown, like sid_model. Panning is per audio SOURCE (socket / UltiSID
    # core / U2+ emulated stereo SID), so each tune chip is panned wherever it
    # was routed — see c64cast/sid/sid_panning.py.
    sid_panning: list[int | str] = field(
        default_factory=list,
        metadata={
            "help": "Stereo pan per SID audio source (U64, or the Ultimate II+'s "
            "2 emulated stereo SIDs). Max 4 entries — one pan control per source "
            "(the U64 has 2 SID sockets + 2 UltiSID cores), and entry N pans the "
            "Nth source the tune uses. Each entry is an int -5..5 (negative = "
            "left, 0 = center) or a label ('Left 3', 'Center', 'Right 2'). "
            "Empty = auto spread: 1 source centered, 2 [-3, 3], 3 [0, -3, 3], "
            "4 [-2, 2, -5, 5] — ordered so the primary chip stays nearest "
            "center. Fewer positions exist without socketed SIDs: with none, "
            "only the 2 FPGA sources are pannable, so chips beyond the 2nd "
            "share a pan.",
        },
    )
    # Applied live to the device's SID mixer alongside sid_panning and restored
    # at teardown. Indexed by SOURCE exactly like sid_panning — see
    # c64cast/sid/sid_volume.py.
    sid_volume: list[int | str] = field(
        default_factory=list,
        metadata={
            "help": "Mixer level per SID audio source (U64, or the Ultimate II+'s "
            "2 emulated stereo SIDs). Max 4 entries — one volume control per "
            "source, and entry N sets the Nth source the tune uses, same indexing "
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
    # Resolution is per-scene at build time (scene_factory.resolve_use_reu_staged),
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
    # Resolved per-scene at build time (scene_factory.resolve_double_buffer).
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
    setup_progress_bar: bool = field(
        default=True,
        metadata={
            "help": "Diagonal-striped bar along screen row 22 while a video scene "
            "buffers (container open, color pre-scan, audio encode, REU upload). "
            "No text or numbers — the right edge is 100%. The first video frame "
            "wipes it. Set false for an untouched screen during setup."
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
            "divider of the reference clock in [audio].sampler_clock_hz, which is "
            "also the resample target — so the quantization is a small constant "
            "pitch offset, drift-free. Do not read a 6.25 MHz nominal into this: "
            "the shipped default clock is the measured ~6.16 MHz (see "
            "sampler_clock_hz), and the divider is computed against whatever that "
            "field says."
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
        metadata={
            "help": "Interstitial text color: a C64 color name, 'rainbow', or 'random'.",
            "vocabulary": "c64color",
        },
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
            # Which media kind(s) this means depends on the *scene type*, not
            # the field — see introspect.SCENE_MEDIA_KINDS. "media" just tells
            # a console this is a browsable path, the way "c64color" tells it
            # a string is a palette entry.
            "vocabulary": "media",
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
    sink_ddp_port: int = field(
        default=DDP_PORT,
        metadata={
            "help": "WLED sink: UDP port for DDP-protocol senders (LedFx / "
            "xLights / Jinx!, wled scenes only). Override when another "
            "process on this host already owns the standard port. "
            f"Default {DDP_PORT}.",
            "applies_to": ("wled",),
        },
    )
    sink_wled_port: int = field(
        default=WLED_REALTIME_PORT,
        metadata={
            "help": "WLED sink: UDP port for WLED's own realtime protocol "
            "(wled scenes only). Override when another process on this host "
            f"already owns the standard port. Default {WLED_REALTIME_PORT}.",
            "applies_to": ("wled",),
        },
    )
    sink_allow: list[str] = field(
        default_factory=list,
        metadata={
            "help": "WLED sink: sender IP addresses allowed to write pixels "
            '(wled scenes only), e.g. ["192.168.2.10"]. Empty (default) '
            "accepts any sender that reaches the bound port — DDP and WLED "
            "realtime carry no authentication of their own, so this is the "
            "only barrier against another host on the LAN injecting frames "
            "into the broadcast.",
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
            "vocabulary": "c64color",
            "applies_to": ("waveform", "midi", "asid"),
        },
    )
    waveform_colors: dict[str, str] = field(
        default_factory=dict,
        metadata={
            "help": "Per-waveform-type colors (e.g. pulse=cyan) for color_mode=per_waveform.",
            "vocabulary": "c64color",
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
            "vocabulary": "c64color",
        },
    )
    background: int | str = field(
        default=0,
        metadata={
            "help": "Background color (blank scenes): a C64 color name (fuzzy + "
            'case-insensitive, e.g. "light blue") or a palette index 0..15.',
            "applies_to": ("blank",),
            "vocabulary": "c64color",
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
    # `applies_to` omits `launcher` because scene_factory._validate_launcher
    # hard-rejects overlays there (the launched program owns screen + color
    # RAM) — without it, --describe, the wizard and the web console all offer
    # a key the loader refuses. _CLIP_SCENE_FIELD_DENY encodes the same fact.
    overlays: list[dict[str, Any]] = field(
        default_factory=list,
        metadata={
            "help": "List of overlay tables ([[scenes.overlays]]); see --list-overlays.",
            "applies_to": tuple(t for t in SCENE_TYPES if t != "launcher"),
        },
    )
    # Per-scene [color] override, stored as the raw authored keys (not a
    # materialized ColorCfg) so a scene can override a field back to its
    # dataclass default even when the global [color] set it away from that
    # default — see scene_color() and docs/architecture/video-color.md.
    color: dict[str, Any] = field(
        default_factory=dict,
        metadata={
            "help": "Per-scene [color] override ([scenes.color] sub-table): any "
            "[color] field set here replaces the global value for this scene "
            "only. Unset fields follow the global [color] section. See "
            "`--describe color` for the field list.",
            "applies_to": ("webcam", "video", "slideshow", "generative", "wled"),
        },
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
            "index 0..15. A list's length sets the color count.",
            "vocabulary": "c64color",
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
    hires_cell_pick: str = field(
        default="error-min",
        metadata={
            "help": "How hires picks each 8×8 cell's foreground color against the "
            "global background. 'error-min' (default) picks the color minimizing "
            "that cell's own reconstruction error. 'sample' reads a single pixel "
            "per cell — ~0.8 ms/frame cheaper, but measurably worse on both "
            "accuracy and frame-to-frame stability, so it's for tight CPU budgets "
            "only. Only affects the hires 'normal' style (the edges styles are "
            "fixed 2-color).",
            "choices": HIRES_CELL_PICKS,
            "apply": "live",
        },
    )
    flicker_tolerance: str = field(
        default=DEFAULT_TOLERANCE,
        metadata={
            "help": "Temporal color blending for the hires/mhires display modes, "
            "and how much visible flicker you'll accept to get it: hold two "
            "screen pages and "
            "alternate them at the VIC field rate, so the eye fuses each cell's "
            "pair of hardware colors into a shade the VIC cannot draw. Targets "
            "gradient banding — spatial dither already synthesizes intermediate "
            "colors wherever there is texture to hide them in. Every candidate "
            "pair was scored by eye, blind, and this picks how far down that "
            "scale to go: 'off' (default) no blending, 16 colors; 'clean' only "
            "pairs that fused, 24 colors on an Ultimate 64; 'subtle' adds mildly "
            "unsteady pairs, 30; 'visible' adds pairs that visibly flicker, 39. "
            "Pairs scored worse than that are recorded but offered by no setting "
            "— they reconstruct no better than 'visible' does, so they would "
            "trade flicker for nothing. 'visible' is itself inside the "
            "photosensitive-seizure band: treat it as an effect you chose, not a "
            "palette upgrade. Blending does not survive a 30 fps capture at any "
            "setting. What alternates is the screen matrix, so a mode blends "
            "exactly the colors it keeps there: hires both colors of every cell "
            "('normal' style only), mhires its c1 and c2 — its c3 is color RAM "
            "and its background is $D021, neither of which the field flip can "
            "reach, so both stay real colors. mhires also needs "
            "palette_mode = 'percell' (the global-4 modes pick one set for the "
            "whole frame, so no cell has a decision for a pair to win) and "
            "pins color_match and cell_strategy, which blending measurably needs.",
            "choices": tuple(FLICKER_TOLERANCES),
        },
    )
    flicker_max_luma_delta: float = field(
        default=0.075,
        metadata={
            "help": "How far apart in brightness the two colors of a flicker pair "
            "may be, as a fraction of peak white in linear light (hires/mhires "
            "display modes, with flicker_tolerance on). This is a "
            "photosensitivity control, not a quality knob: alternation at the "
            "field rate is hazardous in proportion to luminance modulation "
            "depth. WARNS above 0.10, and again above 0.12 where modulation "
            "approaches the 20%-of-peak-white flash criterion the guidance is "
            "written around — but does not refuse, because a pair you have "
            "looked at and accepted outranks the number. Nothing unscored can "
            "get in however wide it is set. Do not read a lower value as less "
            "flicker — scored by eye, ΔY predicts fusion barely at all "
            "(r=+0.26), which is what flicker_tolerance is for. It does bound "
            "what that setting can reach: 0.075 (default) holds "
            "flicker_tolerance = 'clean' to 5 of its 8 pairs on an Ultimate 64, "
            "and to 3 of 8 on the VIC-II rendering, whose luminances put five "
            "cleanly-fusing pairs above 0.12. Which pairs qualify depends on "
            "[hardware].host_palette, since it is the emitted light that fuses.",
        },
    )
    flicker_score_pairs: list[str] = field(
        default_factory=list,
        metadata={
            "help": "DIAGNOSTIC (hires/mhires display modes). Replace the flicker "
            "blend set with exactly these "
            'color pairs, written as "Blue+Brown" or "6+9" — the same shape the '
            "arming log prints. Ignores both the scored tiers and "
            "flicker_max_luma_delta, so it can put pairs on screen that no "
            "flicker_tolerance admits and no safety cap allows. That is the point: "
            "it exists so scripts/diags/flicker_score_grid.py can score the pairs "
            "the tier table is built from, which it could not do if it were "
            "restricted by that same table. Not a tuning knob — a pair reachable "
            "only this way was excluded on evidence. Cannot switch blending on by "
            "itself: flicker_tolerance must still be set, and every structural gate "
            "still applies. Empty (default) uses the scored set.",
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


#: Bind addresses that reach only this machine. A control plane on one of
#: these is exposed to whoever already has a shell here; anything else is
#: exposed to the network, which is what `allow_unauthenticated` gates.
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")


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
    # Precedence: C64CAST_CONTROL_TOKEN env var > this field > none (open),
    # mirroring `dma_password` so a config that lives in a shared repo doesn't
    # have to carry the credential.
    token: str = field(
        default="",
        metadata={
            "help": "Shared token required on every control-plane request, including "
            "the /perf console and its WebSocket. Empty = no authentication (the "
            "historical behavior). Prefer the C64CAST_CONTROL_TOKEN env var."
        },
    )
    viewer_token: str = field(
        default="",
        metadata={
            "help": "Optional second token granting read-only access (GET/HEAD only): "
            "the /perf console watches but can't launch. Ignored unless `token` is set. "
            "Prefer the C64CAST_CONTROL_VIEWER_TOKEN env var."
        },
    )
    # An open plane on loopback is reachable only by someone who already has a
    # shell here, so it stays allowed and unprompted; off-loopback is the
    # combination this gates. Kept as an opt-out rather than dropping the open
    # mode: a trusted, isolated show network is a real deployment.
    allow_unauthenticated: bool = field(
        default=False,
        metadata={
            "help": "Permit binding `host` to a non-loopback address with no `token` set. "
            "Off by default — an open plane on the network lets anything that can reach "
            "the port drive the run. Loopback needs no opt-in."
        },
    )


@dataclass
class WebCfg:
    """The long-lived web console host (`--serve`). Off by default; requires
    the `web` extra.

    Unlike [control], which serves *alongside* a session the CLI already owns,
    this replaces the process model: the server starts first and owns the
    sessions, so `enabled` and `--serve` mean the same thing."""

    enabled: bool = field(
        default=False,
        metadata={
            "help": "Run the web console host instead of a one-shot session — the "
            "server owns the C64 and starts/stops shows on request (same as --serve); "
            "requires the 'web' extra."
        },
    )
    host: str = field(
        default="127.0.0.1", metadata={"help": "Bind address for the web console host."}
    )
    port: int = field(default=8123, metadata={"help": "Bind port for the web console host."})
    # No "empty = open" option, unlike [control]: this surface starts and stops
    # hardware, so an absent token is generated and persisted rather than
    # standing for "no authentication". Precedence: C64CAST_WEB_TOKEN env var >
    # this field > `token_file` > the generated one under the data dir.
    token: str = field(
        default="",
        metadata={
            "help": "Shared token required on every web-console request. Empty = read "
            "`token_file`, else generate one under the data dir and print it at startup "
            "(this surface is never unauthenticated). Prefer the C64CAST_WEB_TOKEN env var."
        },
    )
    token_file: str = field(
        default="",
        metadata={
            "help": "Read the shared token from this file instead of storing it in the "
            "config (one line, whitespace-stripped). Ignored when `token` or "
            "C64CAST_WEB_TOKEN is set."
        },
    )
    viewer_token: str = field(
        default="",
        metadata={
            "help": "Optional second token granting read-only access (GET/HEAD only): "
            "watch the state feed, but never start, stop or edit. Read-only is not "
            "telemetry-only — the tier also lists the `media_read_only`/"
            "`media_read_write` trees and reads the session log tail, so hand the "
            "link to someone you would let browse those. Reading a `config_roots` "
            "config body is NOT included (that route refuses a viewer, because a "
            "config may carry [ultimate64].dma_password and the [web]/[control] "
            "tokens inline). Prefer the C64CAST_WEB_VIEWER_TOKEN env var."
        },
    )
    autostart: bool = field(
        default=False,
        metadata={
            "help": "Start the config the host was launched with as soon as it comes up, "
            "rather than waiting for a browser to ask (headless / launchd boxes)."
        },
    )

    # Appliance-only. A normal `--serve` (a laptop, a dev machine) never sets
    # this, so its exposure is opt-in per SECURITY.md rather than something
    # every headless install inherits. See docs/architecture/control.md ->
    # "setup_gate.py" for why the window it opens is bounded by construction.
    # Declared below `autostart` rather than beside the token fields it relates
    # to, and deliberately: the reference appendix's worked `[web]` fragment is
    # the first four fields with a showable default (_SNIPPET_KEYS in
    # scripts/gen_reference_appendices.py), and a switch nobody should turn on
    # by hand does not belong in the example everyone copies.
    setup_wizard: bool = field(
        default=False,
        metadata={
            "help": "Serve a one-time, unauthenticated setup form (connection target + "
            "token choice) until it is completed, instead of the normal token-gated "
            "console. For a pre-provisioned appliance image only — leave this off on a "
            "console you configure yourself. See `c64cast --reset-setup`."
        },
    )
    screen_fps: float = field(
        default=10.0,
        metadata={
            "help": "How often the console's live screen picture is refreshed, in "
            "frames per second (0 turns the screen off entirely). The picture is "
            "the Ultimate 64's own VIC stream, so this caps how often the host "
            "encodes a frame, not how fast the machine sends. Ultimate 64 only — "
            "an Ultimate II+ has no VIC of its own and a TeensyROM+ no video path."
        },
    )
    settle_s: float = field(
        default=3.0,
        metadata={
            "help": "Seconds to leave the hardware alone between tearing one session "
            "down and building the next: the U64's DMA service refuses new connections "
            "for a few seconds after one closes, and a camera will not reopen instantly."
        },
    )
    # A list rather than one directory because show configs and the packaged
    # examples usually live apart, and copying one next to the other to make it
    # visible is how a config browser starts growing a file manager.
    config_roots: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Directories the web console may browse and edit .toml configs in. "
            "Empty = the directory the host was launched from. Nothing outside these "
            "is readable or writable, symlinks included; a config saved here can still "
            "name media anywhere, so treat write access as shell-equivalent."
        },
    )
    # Kind -> directory, unlike `config_roots`'s flat list: which directory an
    # upload of that kind lands in has to be stated, not guessed from a name
    # (a directory called "clips" tells you nothing) or from its contents (a
    # brand-new empty one has none to guess from). The same directories are
    # also what every kind's browsing offers, same as before this field grew
    # a write side.
    media_read_write: dict[str, str] = field(
        default_factory=dict,
        metadata={
            "help": "Kind (video/sid/picture/program/audio) to directory, both "
            "browsable and uploadable to. Unset kinds fall back to the loader's own "
            "defaults (assets/videos, assets/sids, assets/pictures, assets/programs); "
            "audio has no default, so name one to allow audio uploads. Set a kind to "
            '"" to disable uploading it while leaving the rest at their defaults '
            "(this is the only way to turn one off — an empty table means every "
            "default applies). Uploads never overwrite: a name already taken is "
            "renamed clip-2.mp4, clip-3.mp4, and so on."
        },
    )
    media_read_only: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Additional directories the media picker may browse but never "
            "write to — a library you want offered without exposing it to uploads, "
            'e.g. ["~/Movies", "/mnt/hvsc"]. Empty adds nothing.'
        },
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
# "config stays import-light" rule; see scene_factory.validate_midi_control_cfg).
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
            "mode (like --frame-numbers). This is the run's baseline; the web "
            "console's PERF button and a double-tap of an osd.position pad both "
            "silence the OSD live for a performance (they are one control at two "
            "surfaces), and either restores this setting when switched back off.",
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
            # Built from the constant rather than re-typed: this help is the
            # only per-key documentation a list[dict] field can carry to
            # --describe, the JSON schema and the wizard, and the hand-written
            # enumeration had fallen four actions behind _MIDI_ACTION_CHOICES.
            "record, 0x09 pause — for mmc), action ("
            + "|".join(repr(a) for a in _MIDI_ACTION_CHOICES)
            + "); 'jump' also needs an int scene; 'param' also needs "
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
# What a [[performance.clips]] table means for each launch key it omits. Named
# here because `clips` is a list[dict]: the per-key `default` metadata machinery
# never sees these, so _validate_clips, clip_scene_cfg and the field's own help
# (the only surface --describe / the schema / the wizard can render for a
# list-of-tables field) all have to read one source.
_CLIP_DEFAULTS: dict[str, Any] = {
    "type": "webcam",
    "launch": "trigger",
    "quantize": "bar",
    "pad_type": "note",
    "loop": True,
}

# [performance].tempo_source. Named so the field metadata and
# _validate_performance read one tuple instead of two hand-kept copies.
_TEMPO_SOURCE_CHOICES = ("internal", "midi", "audio")


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
            "choices": _TEMPO_SOURCE_CHOICES,
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
            "no grid. Only `slot` is required; the keys a table omits default to "
            + ", ".join(f"{k} = {v!r}" for k, v in _CLIP_DEFAULTS.items())
            + ' — note that `quantize` defaults to the bar, not to "off", and '
            "that a looping continuous-frame clip (webcam/blank/slideshow/"
            "generative/wled) is pinned to duration_s = 0 so it holds until the "
            "next launch."
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
    # Mode 1's default endpoint is 0.0.0.0:8080 — off-loopback out of the box,
    # unlike [control], which defaults to 127.0.0.1. So this gate is reached by
    # a plain `listen = "enabled"`, not just by someone who typed an address.
    # Kept as an opt-out rather than dropping the open mode, and for the same
    # reason [control].allow_unauthenticated is: LAN discovery from the WLED app
    # is the entire feature, and the protocol has no credential to offer.
    allow_unauthenticated: bool = field(
        default=False,
        metadata={
            "help": "Permit binding `listen` to a non-loopback address. Off by "
            "default — the WLED JSON API carries no token, is advertised over "
            "mDNS, and can pause the run, jump scenes, sweep live params, force "
            "the palette and write presets. Loopback needs no opt-in."
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
    web: WebCfg = field(default_factory=WebCfg)
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
            # A syntax error on a credential-bearing line would otherwise copy
            # the credential into this message, which cli.py logs at error level
            # and --log-file mirrors to disk — and a TOML typo is exactly the
            # error whose log someone pastes into an issue. The position and the
            # parser's message carry all the diagnostic value; the value does
            # not. The caret is dropped when the line was redacted because the
            # substitution moves the columns it would point at.
            safe = redact_secrets(offending)
            out.append(f"    {safe}")
            if safe == offending:
                out.append(f"    {' ' * (colno - 1)}^")
    else:
        out.append(f"  {msg}")
    return "\n".join(out)


@dataclass(frozen=True)
class UnknownKey:
    """One key a TOML file declared that no dataclass field accepts.

    Collected during load rather than logged on the spot so the caller
    chooses the presentation: a normal run logs these as warnings, while
    `--doctor` renders them as CONFIG diagnostics inside the report body
    (a preamble log line above the report reads as noise next to the
    formatted `[WARN]` rows, which is how a misplaced key stayed invisible
    long enough to be mistaken for a working config)."""

    #: The table the key was found in, or "" when `key` names an unrecognized
    #: *table* — the shape the per-section walk cannot see, since a table the
    #: loader applies to nothing never reaches `_apply_section` at all.
    section: str
    key: str
    source: str | None = None
    hint: str | None = None

    def describe(self) -> str:
        """One-line rendering shared by the log path and the doctor row."""
        where = f"{self.source}: " if self.source else ""
        if not self.section:
            return f"{where}unknown config table [{self.key}] — ignored"
        return f"{where}[{self.section}] unknown config key {self.key!r} — ignored"


def _dedupe_unknown(records: list[UnknownKey]) -> list[UnknownKey]:
    """Collapse identical (source, section, key) records, preserving order.

    The machine-settings file is re-applied once per system in ensemble mode
    (plus for the master defaults and the cascade baseline), so one stray key
    there would otherwise be reported N+2 times — same file, same table, same
    key, one problem to fix."""
    seen: set[UnknownKey] = set()
    out: list[UnknownKey] = []
    for r in records:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# Sections whose keys live on a dataclass reachable from Config. Used only to
# build the cross-section suggestion index; the apply path walks
# _TOML_SCALAR_SECTIONS + the [color]/[[scenes]] special cases as before.
@functools.lru_cache(maxsize=1)
def _known_key_index() -> dict[str, tuple[str, ...]]:
    """Map every valid config key to the section(s) that accept it.

    Built from the same dataclass fields the apply path and the JSON schema
    read, so it cannot drift from what a section actually takes. Used to turn
    "unknown here" into "valid, but you put it in the wrong table" — the case
    plain within-section difflib can never catch, because the key is spelled
    perfectly and simply belongs elsewhere.
    """
    index: dict[str, list[str]] = {}
    probe = Config()
    for name in (*_TOML_SCALAR_SECTIONS, "color"):
        for f in fields(getattr(probe, name)):
            if f.metadata.get("internal"):
                continue  # derived run state, not a key any table accepts
            index.setdefault(f.name, []).append(name)
    for f in fields(SceneCfg):
        index.setdefault(f.name, []).append("[scenes]")
    return {k: tuple(v) for k, v in index.items()}


def _unknown_key_hint(section_name: str, key: str, valid: set[str]) -> str | None:
    """Best available "did you mean" for an unknown key, most useful first:
    an exact match in another section, then a near-miss within this section,
    then a near-miss anywhere."""
    elsewhere = tuple(s for s in _known_key_index().get(key, ()) if s != section_name)
    if elsewhere:
        where = " or ".join(f"[{s}]" for s in elsewhere)
        return f"{key!r} is not a [{section_name}] key, but {where} accepts it — move it there."
    close = difflib.get_close_matches(key, valid, n=1)
    if close:
        return f"did you mean {close[0]!r}?"
    # The cross-section pool is ~20x the size of one section's, so difflib's
    # default 0.6 cutoff starts volunteering junk ('strayA' -> 'storage', 0.62).
    # Real typos score far higher ('dither_strenth' -> 'dither_strength', 0.97),
    # so a stricter bar costs nothing and keeps a wrong guess from sending
    # someone to edit the wrong table.
    across = difflib.get_close_matches(key, set(_known_key_index()), n=1, cutoff=0.85)
    if across:
        other = _known_key_index()[across[0]]
        where = " or ".join(f"[{s}]" for s in other)
        return f"did you mean {across[0]!r} in {where}?"
    return None


def _apply_section(
    dc: Any,
    data: dict[str, Any],
    section_name: str,
    unknown: list[UnknownKey] | None = None,
    *,
    source: str | None = None,
) -> None:
    """Overwrite dc fields with values from a TOML section dict, collecting
    unknown keys so typos don't pass silently.

    When `unknown` is None the key is logged immediately (the standalone
    `load()` callers — SIGHUP reload, the interstitial factory — have no
    collector to drain). Otherwise it is appended for the caller to present.

    A field whose annotation is exactly ``bool`` refuses a non-bool value. Every
    consumer of a bool field is a plain truthiness test, so a quoted TOML
    ``allow_unauthenticated = "false"`` stored the truthy string "false" and
    meant the *opposite* of what it read as — on two of the switches that decide
    network exposure. The tri-states (``bool | str``) are unaffected: their
    annotation is not ``bool``, and their own validators own them.

    A field carrying ``internal`` metadata is derived run state, not a config
    key, so it is treated like an unknown one: ``cc_map_is_default`` is set
    False only when a layer really authored a ``cc_map``, and authoring the
    flag directly inverted the controller-profile merge with no cc_map in
    sight."""
    valid = {f.name for f in fields(dc) if not f.metadata.get("internal")}
    annotations = {f.name: f.type for f in fields(dc)}
    for k, v in data.items():
        if k not in valid:
            rec = UnknownKey(section_name, k, source, _unknown_key_hint(section_name, k, valid))
            if unknown is None:
                log.warning("%s%s", rec.describe(), f" ({rec.hint})" if rec.hint else "")
            else:
                unknown.append(rec)
            continue
        if annotations[k] in ("bool", bool) and not isinstance(v, bool):
            raise ConfigError(
                f"[{section_name}].{k} must be true or false, got {v!r} — "
                "a quoted value is a string, and a non-empty string reads as true"
            )
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


def _ultimate_base_url(raw: str) -> str:
    """The base URL a scheme-carrying ``[ultimate64].url`` names, or a
    ``ConfigError`` saying which field the value actually belongs in."""
    try:
        spec = connect.parse_connection_uri(raw)
    except connect.ConnectionURIError as e:
        raise ConfigError(f"[ultimate64].url: {e}") from e
    shown = connect.redact_target(raw)
    if spec.backend != "ultimate":
        raise ConfigError(
            f"[ultimate64].url: {shown!r} selects the {spec.backend} backend. In a config "
            "the backend is [hardware].backend and the endpoint is that backend's own "
            "section — this field is the Ultimate's base URL."
        )
    if spec.dma_port is not None:
        raise ConfigError(
            f"[ultimate64].url: {shown!r} carries a query param. Those exist for -u/--url, "
            "which has nowhere else to put a per-link knob; in a config, set the field "
            f"itself (dma_port = {spec.dma_port})."
        )
    return spec.url or raw


def _normalize_ultimate_url(u64: Ultimate64Cfg) -> None:
    """Accept on ``[ultimate64].url`` every target that can only mean this
    machine, and rewrite it to the base URL the REST client wants.

    ``u64://192.168.2.64`` and ``http://192.168.2.64`` name the same machine and
    ``-u/--url`` takes either, but this field went straight to ``requests``,
    which has no adapter for ``u64://``. That surfaced as a bare "could not reach
    the hardware" at startup, with the real reason at debug level and the fix a
    reader reaches for (check the cabling) the wrong one.

    A bare host is taken too — the shipped example has always said so, and it is
    where the two surfaces are *right* to differ: a scheme is how ``-u`` picks a
    backend, so it has to insist on one, while a value already inside the
    ``[ultimate64]`` section has nothing left to pick.

    Normalize first, then validate *always*. Prefixing a scheme-less value with
    ``http://`` and returning it unchecked let a bare host skip
    :func:`connect.parse_connection_uri` entirely, so the two refusals this
    field's own help promises — no ``user:pass@`` netloc (which
    ``--save-settings`` would then write to ``settings.toml`` and echo to
    stdout) and no ``?query`` knob — applied to ``http://host`` and not to
    ``host``."""
    raw = u64.url.strip()
    candidate = f"http://{raw}" if raw and "://" not in raw else raw
    resolved = _ultimate_base_url(candidate)
    if resolved != u64.url:
        log.debug(
            "[ultimate64].url %r reads as %r",
            connect.redact_target(u64.url),
            connect.redact_target(resolved),
        )
    u64.url = resolved


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
    if perf.tempo_source not in _TEMPO_SOURCE_CHOICES:
        raise ValueError(
            "[performance].tempo_source must be one of "
            f"{', '.join(repr(c) for c in _TEMPO_SOURCE_CHOICES)}, "
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
    choice sets, an in-range `pad` bound at most once, a real scene `type`, and
    no unknown keys (difflib "did you mean"). The embedded scene spec's deeper
    validation (display/file per type) is deferred to build time — build_scene
    runs the full validate_scene_cfg when the clip is fired.

    `pad` uniqueness is checked for the same reason `slot`'s is: midi_control.
    _add_clip_pad_mappings skips a (kind, number) it has already bound, so a pad
    declared twice inside one file leaves the second clip unfirable with no
    message. (A collision *across* systems at that call site is deliberate and
    documented there; one inside a single grid is an authoring mistake.)"""
    allowed = _clip_allowed_keys()
    seen_slots: set[int] = set()
    seen_pads: dict[tuple[str, int], int] = {}
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
        stype = clip.get("type", _CLIP_DEFAULTS["type"])
        if stype not in SCENE_TYPES:
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) type must be one of "
                f"{SCENE_TYPES}, got {stype!r}"
            )
        launch = clip.get("launch", _CLIP_DEFAULTS["launch"])
        if launch not in _CLIP_LAUNCH_CHOICES:
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) launch must be one of "
                f"{_CLIP_LAUNCH_CHOICES}, got {launch!r}"
            )
        quantize = clip.get("quantize", _CLIP_DEFAULTS["quantize"])
        if quantize not in _CLIP_QUANTIZE_CHOICES:
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) quantize must be one of "
                f"{_CLIP_QUANTIZE_CHOICES}, got {quantize!r}"
            )
        pad_type = clip.get("pad_type", _CLIP_DEFAULTS["pad_type"])
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
        if pad is not None:
            owner = seen_pads.get((pad_type, pad))
            if owner is not None:
                raise ValueError(
                    f"[[performance.clips]][{i}] (slot {slot}) {pad_type} pad {pad} "
                    f"is already bound by slot {owner} — one pad fires one clip"
                )
            seen_pads[(pad_type, pad)] = slot
        loop = clip.get("loop")
        if loop is not None and not isinstance(loop, bool):
            raise ValueError(
                f"[[performance.clips]][{i}] (slot {slot}) loop must be true/false, got {loop!r}"
            )


def clip_scene_type(clip: dict[str, Any]) -> str:
    """The scene type a ``[[performance.clips]]`` table will build, without
    building it — the table's own ``type`` or the clip default.

    ``session.build_stack`` needs this before it opens the camera, and it
    cannot ask :func:`clip_scene_cfg`: that runs the full ``_apply_section``
    pass, which raises on any unrelated bad key in the table."""
    return str(clip.get("type", _CLIP_DEFAULTS["type"]))


def clip_scene_cfg(clip: dict[str, Any]) -> SceneCfg:
    """Build a :class:`SceneCfg` from a [[performance.clips]] table by stripping
    the launch/pad keys and applying the remaining scene-spec fields — the same
    field set (and `_apply_section` path) a declared ``[[scenes]]`` block uses,
    so a clip inherits every scene knob for free. Called by the launch engine's
    build factory (see session.build_stack / performance.PerformanceSession).

    `loop` and `duration_s` interact: a looping non-video clip is forced to
    ``duration_s = 0`` (run forever) so it holds until another clip fires; the
    launch engine re-runs a finished video clip instead (video rejects
    duration_s). A one-shot clip keeps its scene-type default duration."""
    scene_keys = {k: v for k, v in clip.items() if k not in _CLIP_LAUNCH_KEYS}
    sc = SceneCfg()
    _apply_section(sc, scene_keys, "performance.clips")
    if (
        clip.get("loop", _CLIP_DEFAULTS["loop"])
        and sc.type in _CLIP_CONTINUOUS_TYPES
        and sc.duration_s is None
    ):
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
    resolve_panning normalizes them at apply time.

    The guard is on shape, not truthiness: 0 is a *legal* pan value (Center),
    so `if not u64.sid_panning` would have let a scalar `sid_panning = 0` past
    the list check below — and resolve_panning's own falsy test then applies
    the auto-spread, which for two sources is [-3, +3], the opposite of
    centered. A non-falsy scalar (`sid_panning = -3`) was already rejected, so
    the two spellings of one mistake got opposite treatment."""
    if not isinstance(u64.sid_panning, list):
        raise ValueError(
            f"ultimate64.sid_panning must be a list of pan values, got {u64.sid_panning!r}"
        )
    if not u64.sid_panning:
        return
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
    sid_volume.resolve_volumes normalizes them at apply time.

    Shape-guarded rather than truthiness-guarded for the same reason as
    _validate_sid_panning: 0 means 0 dB here, so a scalar `sid_volume = 0`
    would otherwise skip the list check and be silently replaced by the
    auto-spread downstream."""
    if not isinstance(u64.sid_volume, list):
        raise ValueError(f"ultimate64.sid_volume must be a list of levels, got {u64.sid_volume!r}")
    if not u64.sid_volume:
        return
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


def _validate_host_sid_chips(hw: HardwareCfg) -> None:
    """Range-check [hardware].host_sid_chips at load/doctor time. A typo'd
    address here would otherwise surface as a chip silently missing from the
    resolved-audio verdict — the one line whose job is to be trusted.

    Shape before emptiness, as in _validate_sid_panning: a falsy non-table
    (`host_sid_chips = 0`) must still reach the type check."""
    if not isinstance(hw.host_sid_chips, dict):
        raise ValueError(
            f"hardware.host_sid_chips must be a table of address = model, got {hw.host_sid_chips!r}"
        )
    if not hw.host_sid_chips:
        return
    for address, model in hw.host_sid_chips.items():
        try:
            value = int(str(address).lstrip("$"), 16)
        except ValueError:
            raise ValueError(
                f"hardware.host_sid_chips key {address!r} is not a hex address "
                f"(want e.g. d400, d420)"
            ) from None
        if not (_HOST_SID_ADDR_LO <= value <= _HOST_SID_ADDR_HI) or value % 0x10:
            raise ValueError(
                f"hardware.host_sid_chips address ${value:04X} is out of range — "
                f"a SID base sits on a $10 boundary in "
                f"${_HOST_SID_ADDR_LO:04X}-${_HOST_SID_ADDR_HI:04X}"
            )
        if model not in HOST_SID_CHIP_MODEL_CHOICES:
            raise ValueError(
                f"hardware.host_sid_chips[{address}] = {model!r} — want one of "
                f"{', '.join(HOST_SID_CHIP_MODEL_CHOICES)}"
            )


def _validate_host_sid_tune_match(hw: HardwareCfg) -> None:
    """Check [hardware].host_sid_tune_match at load/doctor time. A typo would
    otherwise read as "off" and silently do nothing, which is indistinguishable
    from the feature not working."""
    if hw.host_sid_tune_match not in HOST_SID_TUNE_MATCH_CHOICES:
        raise ValueError(
            f"hardware.host_sid_tune_match = {hw.host_sid_tune_match!r} — want one of "
            f"{', '.join(HOST_SID_TUNE_MATCH_CHOICES)}"
        )


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


# `choices` metadata is enforced generically over the scalar sections (see
# _validate_choice_fields), so a newly added choices field is checked by
# construction instead of by somebody remembering to hand-write a validator.
# These are the documented exceptions; a test asserts every name here is still
# a real choices field, so an exemption cannot outlive the field it excuses.
#
# SceneCfg is deliberately out of scope: scene_factory.validate_scene_cfg
# checks scene fields per type, with messages that know which type is building.
# So is [color], for a second reason on top of that one — every choices field
# it has is already enforced by a session/mode validator, and the web console's
# layer-blame report (config_store._blame_layers) is built on those refusals
# arriving from validate_configs with a *loadable* config in hand.
_CHOICES_OPEN: dict[str, str] = {
    # "auto"/"off" plus any positive float (Hz) — see the field's own help.
    "ultimate64.sid_play_rate": "also accepts a rate in Hz",
}
# Fields matched case-insensitively rather than exactly, because the value is
# case-normalized downstream (hw/backend.py and hw/hw_provision.py both
# `.upper()` it, each with a comment saying nothing at load enforces the
# canonical spelling) — so `system = "ntsc"` works today and has to keep
# working, while `system = "ntscc"` should not.
_CHOICES_CASE_INSENSITIVE: frozenset[str] = frozenset({"ultimate64.system"})


def _validate_choice_fields(cfg: Config) -> None:
    """Reject a scalar-section string value that is outside its declared
    `choices`.

    The metadata is this module's single source of truth, but nothing used to
    hold a value to it: enforcement was a hand-written per-field validator, and
    the fields nobody wrote one for failed *open*. `sid_video_mode` is the
    sharp one — hw_provision tests it as `!= "off"`, so any typo retimed the
    machine and switched the HDMI output mode. `host_sid_tune_match`'s own
    validator already states the principle ("a typo would otherwise read as
    'off' and silently do nothing, which is indistinguishable from the feature
    not working"); this applies it to every choices field at once."""
    for section_name in _TOML_SCALAR_SECTIONS:
        section = getattr(cfg, section_name)
        for f in fields(section):
            choices = f.metadata.get("choices")
            key = f"{section_name}.{f.name}"
            if not choices or key in _CHOICES_OPEN:
                continue
            value = getattr(section, f.name)
            if not isinstance(value, str):
                continue
            if key in _CHOICES_CASE_INSENSITIVE:
                if value.casefold() in {str(c).casefold() for c in choices}:
                    continue
            elif value in choices:
                continue
            raise ValueError(
                f"[{section_name}].{f.name} = {value!r} — want one of "
                f"{', '.join(repr(c) for c in choices)}"
            )


def resolved_force_palette(color: ColorCfg) -> tuple[int, list[int] | None]:
    """Derive the (n_colors, indices) pair the color-map accumulator wants from
    the unified force_palette_colors field (validated/normalized by
    _validate_force_palette): a list -> (len, list); an int -> (count, None)."""
    fp = color.force_palette_colors
    if isinstance(fp, list):
        idx = [int(c) for c in fp]
        return len(idx), idx
    return int(fp), None


def scene_color(cfg: Config, s: SceneCfg) -> ColorCfg:
    """The effective [color] section for scene `s`: the global [color] with
    `s.color`'s authored keys applied over it.

    `s.color` stores the raw authored keys rather than a materialized
    ColorCfg specifically so a scene can override a field back to its
    dataclass default even when the global section set it away from that
    default — a "differs from ColorCfg()" merge (the idiom
    apply_master_defaults uses for the machine-settings cascade) would treat
    such an override as unauthored and let the global value win instead.

    Returns `cfg.color` itself (no copy) when the scene has no override — the
    common case, and it keeps every no-override scene sharing one object.
    `hue_corrections` is an all-or-nothing replace: a scene that sets it swaps
    the whole list rather than extending the global's."""
    if not s.color:
        return cfg.color
    color = copy.deepcopy(cfg.color)
    raw = dict(s.color)
    hue_corrections = raw.pop("hue_corrections", None)
    _apply_section(color, raw, "scenes.color")
    if hue_corrections is not None:
        color.hue_corrections = [dict(hc) for hc in hue_corrections]
    _validate_force_palette(color)
    return color


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
    "web",
    "midi_control",
    "performance",
    "menu",
    "wled",
)


# Top-level TOML keys that are a real table somewhere in the loader, so a key
# outside this set is a misspelled or misplaced *table* — the one stray-key
# shape the per-section walk could never see, because a table nobody applies
# never reaches _apply_section.
_KNOWN_TOML_TABLES: frozenset[str] = frozenset(
    {*_TOML_SCALAR_SECTIONS, "color", "scenes", "ensemble"}
)


def _hue_correction_rows(raw: Any) -> list[dict[str, Any]]:
    """The authored [[color.hue_corrections]] tables, as a fresh list of dicts.

    A layer that declares the key REPLACES the list; it does not extend it.
    Appending made the layers *concatenate* — machine settings declaring band X
    plus a project TOML declaring band Y gave [X, Y], with no way for the
    project file to override, reorder or remove X — against the documented
    "every layer above the defaults overrides the ones below it", and against
    :func:`scene_color`, which has always treated the same field as an
    all-or-nothing replace. It also broke the serializer's round trip:
    config_serialize writes a list-of-tables whole or not at all, so [X] + [X,
    Y] wrote [X, Y] and reloading appended onto the machine layer again to give
    [X, X, Y]."""
    if not isinstance(raw, list):
        raise ValueError(f"color.hue_corrections must be a list of tables, got {raw!r}")
    rows: list[dict[str, Any]] = []
    for hc in raw:
        if not isinstance(hc, dict):
            raise ValueError(f"color.hue_corrections entry must be a table, got {hc!r}")
        rows.append(dict(hc))
    return rows


def validate_sections(cfg: Config) -> None:
    """Run every load-time section validator over `cfg`.

    Called from :func:`_apply_toml_sections`, so a bad value is refused in the
    layer that wrote it (machine settings, a project/per-system file, or the
    ensemble master), and again from :func:`merge_cli`, which is the last
    layer: the CLI and the env vars write straight into an already-validated
    Config, so a value arriving that way used to reach the run unchecked and
    fail mid-show instead — the exact thing each of these validators' own
    docstring says it exists to prevent."""
    _validate_use_reu_staged(cfg.video)
    _validate_double_buffer(cfg.video)
    _validate_video_device(cfg.video)
    _validate_audio_device(cfg.audio)
    _normalize_ultimate_url(cfg.ultimate64)
    _validate_performance(cfg.performance)
    _validate_sid_panning(cfg.ultimate64)
    _validate_sid_volume(cfg.ultimate64)
    _validate_host_sid_chips(cfg.hardware)
    _validate_host_sid_tune_match(cfg.hardware)
    _validate_choice_fields(cfg)
    _validate_force_palette(cfg.color)


def _apply_toml_sections(
    cfg: Config,
    data: dict[str, Any],
    *,
    source: str,
    unknown: list[UnknownKey] | None = None,
) -> None:
    """Apply the scalar + [color] sections of a parsed TOML dict onto `cfg`
    in place.

    Shared by :func:`load` (a project / per-system file),
    :func:`apply_machine_settings` (the machine-settings file) and
    :func:`load_master` (the ensemble master's own sections) so all three go
    through identical unknown-key difflib warnings, the full validator battery,
    and the [color]/hue_corrections special case. Deliberately does NOT handle
    [[scenes]] or [ensemble] — those are load- / master-specific. `source`
    names the origin — it rides along on each collected UnknownKey so an
    ensemble report can say which file the stray key is in."""
    log.debug("applying config sections from %s", source)
    for name in _TOML_SCALAR_SECTIONS:
        if name in data:
            _apply_section(getattr(cfg, name), data[name], name, unknown, source=source)

    # Record whether any layer explicitly authored a cc_map. Monotonic (only ever
    # set False, default True): once machine settings OR the project/per-system
    # file OR the ensemble master specifies cc_map, the effective mapping is the
    # user's own, not the shipped defaults — which flips the profile-merge order
    # (see midi_control.resolve_effective_cc_map). Every layer routes through here.
    mc = data.get("midi_control")
    if isinstance(mc, dict) and "cc_map" in mc:
        cfg.midi_control.cc_map_is_default = False

    # [color] is handled separately from the scalar section loop because it
    # carries a list-of-tables field (hue_corrections) that must be pulled out
    # before _apply_section, same as [[scenes.overlays]] in load().
    if "color" in data:
        raw_color = dict(data["color"])
        raw_hc = raw_color.pop("hue_corrections", None)
        _apply_section(cfg.color, raw_color, "color", unknown, source=source)
        if raw_hc is not None:
            cfg.color.hue_corrections = _hue_correction_rows(raw_hc)

    for table in data:
        if table in _KNOWN_TOML_TABLES:
            continue
        close = difflib.get_close_matches(table, _KNOWN_TOML_TABLES, n=1)
        rec = UnknownKey("", table, source, f"did you mean [{close[0]}]?" if close else None)
        if unknown is None:
            log.warning("%s%s", rec.describe(), f" ({rec.hint})" if rec.hint else "")
        else:
            unknown.append(rec)

    validate_sections(cfg)


def _settings_state_key(path: pathlib.Path) -> tuple[str, int, int]:
    """(path, mtime_ns, size) — a machine-settings file's identity *and*
    content state, so a once-per-process diagnostic still fires again after
    `--save-settings` rewrites the file mid-run."""
    try:
        st = path.stat()
    except OSError:
        return (str(path), 0, 0)
    return (str(path), st.st_mtime_ns, st.st_size)


#: Banned tables already reported for a given machine-settings file state.
_warned_banned_settings_tables: set[tuple[str, int, int, str]] = set()


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
        if banned not in data:
            continue
        # Deduped on the same schedule as the INFO line below: one stray table
        # in this file is one problem, not N+2 of them on an ensemble run.
        seen_key = (*_settings_state_key(path), banned)
        if seen_key not in _warned_banned_settings_tables:
            _warned_banned_settings_tables.add(seen_key)
            log.warning(
                "machine settings %s: [%s] ignored — machine settings hold "
                "cross-run defaults (connection, device, …), not playlists",
                path,
                banned,
            )
        data.pop(banned, None)
    return data


# Machine-settings files this process has already announced, keyed on
# (path, mtime_ns, size). The layer is re-applied once per system in ensemble
# mode plus twice more (the master defaults and the cascade baseline), so an
# N-system wall printed the same INFO line N+2 times for one file — the exact
# repetition `_dedupe_unknown` exists to collapse for unknown keys, and one
# line was the whole point of a line whose job is making a surprising default's
# origin discoverable. Keyed on the file's state, not just its path, so a run
# that saves machine settings and re-reads them announces the new content.
_announced_machine_settings: set[tuple[str, int, int]] = set()


def apply_machine_settings(cfg: Config, unknown: list[UnknownKey] | None = None) -> Config:
    """Overlay the machine-settings file onto `cfg` in place (defaults →
    machine settings), returning it.

    This is the lowest layer above the dataclass defaults; everything that
    applies afterward still wins (project / per-system TOML, the master
    cascade, CLI flags, the ``C64CAST_DMA_PASSWORD`` env var). When a file was
    actually loaded, one INFO line per process logs its path, the tables it
    supplied and a rough field count, so the origin of a surprising default is
    discoverable — naming the tables, because "(4 fields)" cannot tell an
    operator that this is the layer that turned a network switch on."""
    data = load_machine_settings()
    if not data:
        return cfg
    path = paths.settings_path()
    _apply_toml_sections(cfg, data, source=str(path), unknown=unknown)
    seen_key = _settings_state_key(path)
    if seen_key not in _announced_machine_settings:
        _announced_machine_settings.add(seen_key)
        n_fields = sum(len(v) for v in data.values() if isinstance(v, dict))
        log.info(
            "machine settings: %s (%d fields in %s)",
            path,
            n_fields,
            ", ".join(f"[{k}]" for k in sorted(data)),
        )
    return cfg


def machine_baseline(unknown: list[UnknownKey] | None = None) -> Config:
    """A fresh Config carrying the machine-settings layer and nothing above it.

    This is the "nothing was set *here*" reference for every layer that has to
    tell an authored value from an inherited one. Two callers need it and they
    need it for the same reason: :func:`apply_master_defaults` decides whether a
    per-system file set a field, and :func:`config_serialize.dumps` decides
    whether a field is worth writing — and a field the machine layer supplies
    was not set by either the system or the file.

    Reads the settings file on every call, which is what makes it correct rather
    than cached: a run that saves machine settings and then serializes a config
    must measure against the file as it now is.

    `unknown` exists so a caller that is already collecting stray keys can hand
    its list over. Without one, `_apply_section` falls back to logging each
    stray machine-settings key on the spot — a bare `log.warning` above
    `--doctor`'s formatted report, which is the exact out-of-band presentation
    the collect-then-present split exists to avoid, and which `_dedupe_unknown`
    cannot collapse because the record never enters the list."""
    return apply_machine_settings(Config(), unknown)


def load(path: str | None, unknown: list[UnknownKey] | None = None) -> Config:
    """Load a Config from a TOML file path, or from the default search path
    if `path` is None, or return defaults if neither exists.

    `path` semantics:
      - None  → look for ./c64cast.toml; missing is fine.
      - str   → load that file; missing raises ConfigError.

    The machine-settings layer (:func:`apply_machine_settings`) is applied
    first — before the file's own sections — so the file (and later CLI/env)
    override it.

    Parse failures (TOML syntax errors, missing file when path is given)
    raise `ConfigError` with a message formatted for end-user display.

    `unknown` collects stray keys for the caller to present; when it is None
    they are logged as they are found (see `_apply_section`)."""
    cfg = Config()
    apply_machine_settings(cfg, unknown)
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

    _apply_toml_sections(cfg, data, source=path, unknown=unknown)

    for raw in data.get("scenes", []):
        sc = SceneCfg()
        # Pull overlays and the [scenes.color] override out before
        # _apply_section so we keep the original dicts intact (each overlay
        # class validates its own kwargs; color is validated below against a
        # throwaway ColorCfg so a typo'd key gets the same unknown-key
        # difflib hint as a top-level [color] key).
        raw_overlays = raw.pop("overlays", [])
        raw_color = raw.pop("color", {})
        _apply_section(sc, raw, "scenes", unknown, source=path)
        for ov_raw in raw_overlays:
            if not isinstance(ov_raw, dict):
                raise ValueError(f"scenes.overlays entry must be a table, got {ov_raw!r}")
            sc.overlays.append(dict(ov_raw))
        if raw_color:
            if not isinstance(raw_color, dict):
                raise ValueError(f"scenes.color must be a table, got {raw_color!r}")
            # hue_corrections is a real ColorCfg field (list[dict]), so this
            # also validates a [[scenes.color.hue_corrections]] block's shape;
            # scene_color() is what gives it replace-not-extend semantics.
            _apply_section(ColorCfg(), dict(raw_color), "scenes.color", unknown, source=path)
            sc.color = dict(raw_color)
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
# Together with _NEVER_CASCADE_SECTIONS below this is a TOTAL classification of
# the scalar sections plus [color]: every one is listed exactly once, and
# tests/test_ensemble_config.py asserts the partition. It used to be a
# don't-list in a comment, which is how six sections came to be missing from the
# master apply path with nothing to notice — including [hardware] and
# [teensyrom], listed here as cascading while `defaults.hardware` was never
# populated from the master file at all.
_CASCADE_SECTIONS: tuple[tuple[str, frozenset[str]], ...] = (
    ("hardware", frozenset()),
    # serial_port + host are per-system (each TR has its own device/IP),
    # so they never inherit a master default — like ultimate64.url.
    ("teensyrom", frozenset({"serial_port", "host"})),
    ("ultimate64", frozenset({"url"})),
    ("audio", frozenset()),
    # The audio-pipeline shaping sections, on the same footing as [audio]
    # itself: nothing in either names a per-system identity, and a wall wants
    # one DSP chain / one analyzer tuning.
    ("dsp", frozenset()),
    ("audio_features", frozenset()),
    # Gesture control is built per system, but the camera it reads comes from
    # [video].device (which never cascades), so the tuning here is a wall-wide
    # default like any other.
    ("vision", frozenset()),
    # [wled].listen is read off cfgs[0] for the whole process (like [control]),
    # so a master [wled] that did not cascade could not be honored at all.
    ("wled", frozenset()),
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

# The scalar sections that never cascade, each with the reason. [[scenes]] is
# absent from both lists because it is not a scalar section: playlists are
# per-system by nature, and sharing scenes across systems is what the
# [ensemble] orchestrate hook is for, not a side-effect of config cascading.
_NEVER_CASCADE_SECTIONS: dict[str, str] = {
    "video": "device names one physical capture device",
    "control": "one control plane serves the whole ensemble (LoadResult.master_control)",
    "web": "process-wide: one host serves the whole ensemble (LoadResult.master_web)",
    "midi_control": "process-wide control surface (LoadResult.master_midi_control)",
}

# The never-cascade sections a master TOML can still put to work, because
# LoadResult hands them to the runtime directly. Anything else in
# _NEVER_CASCADE_SECTIONS reaches nothing from a master file, which load_master
# says out loud rather than discarding in silence.
_MASTER_PROCESS_WIDE_SECTIONS: frozenset[str] = frozenset({"control", "web", "midi_control"})


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

    Mutable values are deep-copied on the way in. A bare `setattr` handed every
    inheriting system the *same* list/dict object as the master and as each
    other — `color.hue_corrections`, `performance.clips`,
    `hardware.host_sid_chips`, `ultimate64.sid_panning`/`sid_volume` are all
    mutable — so one system mutating one in place would mutate every system's,
    invisibly at the config layer. Nothing does that today; the rest of this
    module copies defensively anyway (`dict(hc)` per hue band, a per-instance
    default cc_map, `scene_color`'s deepcopy) precisely so shared state cannot
    leak between Configs, and the cascade runs once per system at startup.

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
                setattr(sys_section, f.name, copy.deepcopy(master_val))
    return sys_cfg


def resolve_recording_path(
    recording: RecordingCfg,
    system_name: str,
    *,
    is_ensemble: bool,
    baseline: RecordingCfg | None = None,
) -> str:
    """Per-system output file for `[recording]`, unexpanded.

    cv2.VideoWriter has no notion of sharing a file, so N systems opening one
    path produce one truncated stream rather than N recordings. In an ensemble
    a system that left `path` at the default gets the system name folded into
    the stem — `recording.mp4` -> `recording-left.mp4` — which is the same
    disambiguation the preview window applies to its HighGUI title.

    An explicit per-system `path` is honored verbatim: the user naming the file
    outranks a scheme for naming it, and two systems pointed at one name are
    caught by doctor rather than silently renamed. "Explicit" is the same
    approximation :func:`apply_master_defaults` makes, measured against the
    same reference: the **machine-overlaid** baseline, not the dataclass
    default. That distinction is the whole finding — `[recording]` goes through
    the machine-settings layer, so a `settings.toml` carrying `path` made every
    system in an ensemble look explicit, skip the per-system stem, and point N
    `cv2.VideoWriter`s at one file, through the one layer every other layering
    decision in this module treats as unset.

    `baseline` lets a caller that already built one (`machine_baseline()`, or
    the cascade baseline `load_master` reuses) pass it in; without one it is
    read here, which costs a settings-file read per call.
    """
    if not is_ensemble:
        return recording.path
    blank = baseline if baseline is not None else machine_baseline().recording
    if recording.path != blank.path:
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
    not per-system-cascaded (see _CASCADE_SECTIONS); `master_web` is the
    [web] one, and defaults to a blank section for the callers that build a
    LoadResult without a master TOML at all (quick playback).

    `unknown_keys` carries every stray TOML key found across all layers
    (machine settings, master, per-system) so `--doctor` can report them as
    CONFIG rows; a normal run logs them instead (see cli._log_unknown_keys)."""

    cfgs: list[Config]
    names: list[str]
    paths: list[str | None]
    is_ensemble: bool
    master_control: ControlPlaneCfg
    master_midi_control: MidiControlCfg
    master_web: WebCfg = field(default_factory=WebCfg)
    unknown_keys: list[UnknownKey] = field(default_factory=list)


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
                master_web=cfg.web,
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

    unknown: list[UnknownKey] = []

    if "ensemble" not in raw:
        cfg = load(path, unknown)
        return LoadResult(
            cfgs=[cfg],
            names=["system"],
            paths=[path],
            is_ensemble=False,
            master_control=cfg.control,
            master_midi_control=cfg.midi_control,
            master_web=cfg.web,
            unknown_keys=_dedupe_unknown(unknown),
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
    #
    # The master's own sections go through the SAME apply loop as a project or
    # per-system file. A hand-written tuple of (section, dataclass) pairs used
    # to stand in for it here, and had drifted: six sections a master file may
    # legally carry (hardware, teensyrom, vision, dsp, audio_features, wled)
    # never reached _apply_section at all, so they produced neither an applied
    # value nor an UnknownKey — and [hardware]/[teensyrom] are in
    # _CASCADE_SECTIONS, so `[hardware] backend = "teensyrom"` in a master read
    # as nothing while the cascade dutifully copied the machine layer instead.
    # It also ran 3 of the 10 validators, so a master [ultimate64].sid_panning
    # was cascaded into every system without the check whose whole purpose is
    # to fire before the mixer is configured.
    defaults = Config()
    apply_machine_settings(defaults, unknown)
    _apply_toml_sections(defaults, raw, source=path, unknown=unknown)

    inert = sorted(
        s for s in raw if s in _NEVER_CASCADE_SECTIONS and s not in _MASTER_PROCESS_WIDE_SECTIONS
    )
    if inert:
        log.warning(
            "[%s] ensemble master carries %s — ignored (%s), so put these in the "
            "per-system configs",
            path,
            ", ".join(f"[{s}]" for s in inert),
            "; ".join(f"[{s}]: {_NEVER_CASCADE_SECTIONS[s]}" for s in inert),
        )

    # The "unset" baseline for the per-system cascade is a machine-overlaid
    # Config (not a blank one): a field coming only from the machine layer must
    # still be treated as "this system didn't set it" so the master TOML can
    # override it — machine < master < per-system. Built once, reused per system.
    cascade_baseline = machine_baseline(unknown)

    master_dir = os.path.dirname(os.path.abspath(path))
    cfgs: list[Config] = []
    sys_paths: list[str | None] = []
    for entry in ensemble.systems:
        sub_path = entry.config
        if not os.path.isabs(sub_path):
            sub_path = os.path.join(master_dir, sub_path)
        sys_cfg = load(sub_path, unknown)
        sys_cfg = apply_master_defaults(defaults, sys_cfg, baseline=cascade_baseline)
        # Per-system Configs never carry ensemble metadata themselves —
        # only the master TOML does. (Belt and braces: load() never sets
        # ensemble either since it doesn't know about [ensemble].)
        sys_cfg.ensemble = None
        cfgs.append(sys_cfg)
        sys_paths.append(sub_path)
    _warn_audio_only_ensemble(cfgs, [e.name for e in ensemble.systems])
    # The process-wide sections come off `defaults`, which no merge_cli call
    # ever sees — so the env layer has to be applied here or it never reaches
    # the control plane the ensemble actually binds.
    apply_env_credentials(defaults)
    return LoadResult(
        cfgs=cfgs,
        names=[e.name for e in ensemble.systems],
        paths=sys_paths,
        is_ensemble=True,
        master_control=defaults.control,
        master_midi_control=defaults.midi_control,
        master_web=defaults.web,
        unknown_keys=_dedupe_unknown(unknown),
    )


# Scene types that can hold the ensemble audio slot. `generative` is here for
# one arm only: scene_factory._build_generative builds a SidFileAudioSource
# (`wants_audio_lock = True`) for `audio_source = "sid"`, which
# ComposableScene.competes_for_audio_lock then reports — so a playlist of
# generative+sid scenes really does contend, and omitting the type meant
# _warn_audio_only_ensemble stayed silent on the exact contention footgun it
# exists to catch. tests/test_ensemble_config.py pins the mirror.
_AUDIO_BEARING_SCENE_TYPES = frozenset(
    {"video", "waveform", "midi", "asid", "launcher", "generative"}
)


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
    # A generative scene contends only when its audio source drives the real
    # chip; mic/listen/file/none all leave the slot alone.
    if s.type == "generative":
        return s.audio_source == "sid"
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
    "serve": ("web", "enabled"),
    "verbose": ("debug", "verbose"),
    "heartbeat": ("debug", "heartbeat"),
    "skip_probe": ("debug", "skip_probe"),
    "log_file": ("debug", "log_file"),
    "profile": ("debug", "profile"),
    "profile_interval": ("debug", "profile_interval"),
    "frame_numbers": ("debug", "frame_numbers"),
}


def apply_env_credentials(cfg: Config) -> Config:
    """Fold the credential env vars onto `cfg`, returning it.

    ``C64CAST_DMA_PASSWORD`` → ``[ultimate64].dma_password``,
    ``C64CAST_CONTROL_TOKEN`` → ``[control].token``,
    ``C64CAST_CONTROL_VIEWER_TOKEN`` → ``[control].viewer_token`` — so a
    credential can be supplied without putting it in a checked-in TOML file.
    (``C64CAST_WEB_TOKEN``/``C64CAST_WEB_VIEWER_TOKEN`` are re-read by
    ``serve.py`` at bind time and are not folded here.)

    Separate from :func:`merge_cli` because the ensemble's process-wide
    ``[control]`` section is not any per-system Config: it is
    ``LoadResult.master_control``, an object no ``merge_cli`` call ever
    touches, so the env tokens landed on N Configs the runtime does not read
    while the plane came up on whatever the shared master TOML declared — the
    opposite of what the field's own help promises ("env var > this field, so
    a config that lives in a shared repo doesn't have to carry the
    credential"). :func:`load_master` calls this on the master defaults.

    **An exported-but-empty variable counts as unset**, not as "blank it".
    `VAR=$UNSET_OTHER` in a service unit or `docker -e VAR` is the classic way
    to get one, and clearing a configured token there means the run either
    refuses to start (a non-loopback bind) or comes up with no authentication
    (loopback) — neither of which is what the empty value was trying to say.
    To run with no token, leave the field empty and the variable unset."""
    env_pw = os.environ.get("C64CAST_DMA_PASSWORD")
    if env_pw:
        cfg.ultimate64.dma_password = env_pw
    env_token = os.environ.get("C64CAST_CONTROL_TOKEN")
    if env_token:
        cfg.control.token = env_token
    env_viewer = os.environ.get("C64CAST_CONTROL_VIEWER_TOKEN")
    if env_viewer:
        cfg.control.viewer_token = env_viewer
    return cfg


def merge_cli(cfg: Config, args: argparse.Namespace) -> Config:
    """For each CLI option whose value is not None, overwrite the matching
    config field. Argparse must use ``default=None`` for every overridable
    option (so "user didn't pass it" is distinguishable from "user passed
    the default").

    Then folds in the credential env vars (:func:`apply_env_credentials`) and
    re-runs the section validators (:func:`validate_sections`), because this is
    the last layer that writes into the Config and every validator until now
    fired at parse time, one layer below.

    Not quite the last word on the *connection*: `cli._resolve_configs` applies
    the scheme-aware ``-u/--url`` / ``$C64CAST_URL`` target after this, since
    one string has to be able to pick the backend and the endpoint together."""
    for dest, (section, key) in CLI_TO_CFG.items():
        if not hasattr(args, dest):
            continue
        val = getattr(args, dest)
        if val is None:
            continue
        setattr(getattr(cfg, section), key, val)
    apply_env_credentials(cfg)
    validate_sections(cfg)
    return cfg
