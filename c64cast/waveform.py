"""Waveform scene — plays a SID file on the U64 and visualizes the three
SID voices' waveforms in real-time across the full screen.

The U64's real SID chip plays the tune (via ``api.run_sid_player``, which
DMAs the SID payload + a small player MC into C64 RAM and runs a tiny
BASIC SYS stub — see api.py for the details). The U64's FPGA SID is
faithful to real hardware: SID I/O is write-only and reads of $D400-
$D418 return open-bus zeros, so we can't ask the U64 what the SID is
doing right now. Instead, ``SidHostEmu`` runs the same SID file in
parallel on a host-side py65 6502 emulator and traps writes to $D400-
$D418 into a 25-byte shadow. The poll thread reads that shadow at
system rate (60 NTSC / 50 PAL) to feed ``SIDEmulator``, which mirrors
per-voice state and emits the per-frame oscilloscope traces. Audio
remains U64-native; the host emulator's would-be audio is discarded.

Display is 320×200 hires bitmap. Three strips of 56 rows each; one
pixel per column per voice. Bottom 24 rows carry two text lines (title
+ composer; copyright + SID chip + clock) rendered directly into the
bitmap from the C64 character ROM — see TITLE_ROW / META_ROW.

Coloring:
  * ``per_voice``    — each voice gets a fixed color from `voice_colors`.
  * ``per_waveform`` — each voice colors by its current waveform select
                      (triangle/sawtooth/pulse/noise/off). Colors come
                      from `waveform_colors`.

The scene runs until ``duration_s`` elapses (the U64 SID-play endpoint
doesn't surface a 'finished' signal; pick a duration that matches the
tune length, or use SongLengths data if you have it).

SHIFT cycles to the next subtune on multi-song SIDs (see ``cycle_style``).
Cycle skips subtunes the SongLengths DB flags as shorter than
``MIN_CYCLE_SUBTUNE_S`` (typically game SFX); startup honors the
configured ``song`` regardless of length.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from ._pollthread import PollThread
from .audio import RING_BUFFER_ADDR, RING_BUFFER_END, AudioStreamer
from .backend import C64Backend
from .c64 import CIA2, CPU, ROM, SCREEN, VIC_BANK_0, VIC_BANK_2, RegionID
from .palette import C64_COLORS
from .scenes import Scene
from .sid_host_emu import SidHostEmu, ram_play_access_footprint, ram_write_footprint
from .sidemu import (
    ACCUMULATOR_RANGE,
    WAVE_NOISE,
    WAVE_PULSE,
    WAVE_SAWTOOTH,
    WAVE_TRIANGLE,
    SIDEmulator,
    primary_waveform,
)

if TYPE_CHECKING:
    from .songlengths import LengthsDB

log = logging.getLogger(__name__)

SCREEN_W_CHARS = SCREEN.W_CHARS
BITMAP_W = SCREEN.BITMAP_W
BITMAP_H = SCREEN.BITMAP_H

# Bitmap layout: each 8-row cell row of the screen maps to a contiguous
# 320-byte slice of the bitmap, organized as 40 cells × 8 bytes.
CELL_PX = 8                              # one screen cell = 8 px square
BITMAP_CELL_ROW_BYTES = BITMAP_W         # 320 bytes per cell row

# Three voice strips in hires mode, 7 cell rows each = 56 pixel rows.
# The bottom of the screen carries two text rows (song metadata) — see
# TITLE_ROW / META_ROW — so each voice strip is one cell shorter than the
# spare-cell-row-only layout we'd otherwise use.
#
# Cell-row layout (25 rows total):
#   0-6   voice 1   (56 px)
#   7-13  voice 2   (56 px)
#   14-20 voice 3   (56 px)
#   21    spacer (1 char row gap above the text)
#   22    TITLE_ROW   (left: name + song N/M, right: composer)
#   23    META_ROW    (copyright / SID model / PAL-NTSC)
#   24    spacer (1 char row at the very bottom)
BITMAP_STRIPS = [
    (0,   56),    # voice 1: cell rows 0-6
    (56,  112),   # voice 2: cell rows 7-13
    (112, 168),   # voice 3: cell rows 14-20
]
TITLE_ROW = 22
META_ROW = 23

# VIC register pokes used by _setup_hires. Hex strings match the existing
# write_memory API. Named so the intent (bitmap mode, multicolor off,
# screen page) is readable inline.
D011_HIRES_ON = "3b"      # bitmap mode + display enable, raster MSB clear
D016_STANDARD = "08"      # 40-col, no multicolor
# $D018 selects the screen matrix (bits 7-4 = offset/$0400 within the bank)
# and the bitmap (bit 3 = bitmap at bank+$2000). $18 = matrix at bank+$0400
# + bitmap at bank+$2000 — bank-relative, so it addresses $0400/$2000 in
# bank 0 and $8400/$A000 in bank 2 with only $DD00 (CIA2 port A) changing.
# Bank 1 can't reuse $0400/$2000 (the payload covers the low half of the
# bank), so it puts the matrix at bank+$1400 ($5400) → $D018 = $58. See
# _DISPLAY_BANKS / _choose_display_layout.
D018_HIRES_BITMAP = 0x18  # bank-relative: screen +$0400, bitmap +$2000

# Bank 1 display addresses. The audio ring at $4000-$5FFF is dormant during a
# waveform scene (the SID plays on the real chip; setup() stops the ring), so
# bank 1 is a usable display target — except its low half is typically covered
# by the SID payload. Put the bitmap at $6000 (bank+$2000) and the screen
# matrix at $5400 (bank+$1400), both above a payload that ends below $6000.
_BANK1_SCREEN = 0x5400
_BANK1_BITMAP = 0x6000
D018_BANK1 = 0x58  # matrix nibble 5 ($1400) + bitmap bit 3 ($2000)

# Low-RAM window cleared before a hard-relaunch cycle (see cycle_style). A
# C64 reset's RAMTAS zeroes $0002-$03FF; some tunes (Times of Lore 2-11)
# leave scratch here that the next subtune's INIT mis-reads, so we zero it
# to mimic a fresh machine without the visible boot reset. $00/$01 (the CPU
# port) are left alone. HW-verified necessary on Times of Lore.
_LOW_RAM_CLEAR_LO = 0x0002
_LOW_RAM_CLEAR_HI = 0x0400  # exclusive

# Candidate VIC banks for the waveform display, in preference order:
# (screen_base, bitmap_base, dd00_value, d018_value). Bank 0 is the default;
# bank 2 is the first fallback when the payload/footprint occupies bank 0's
# display; bank 1 is the last resort (Galway's Times of Lore subtunes 2-11
# put live song data in bank 2's $B400, leaving only bank 1 free). Bank 3
# ($C000-$FFFF) overlaps I/O + the player/audio handlers, so it's omitted.
_DISPLAY_BANKS = (
    (VIC_BANK_0.SCREEN, VIC_BANK_0.BITMAP, CIA2.PORT_A_BANK_0, D018_HIRES_BITMAP),
    (VIC_BANK_2.SCREEN, VIC_BANK_2.BITMAP, CIA2.PORT_A_BANK_2, D018_HIRES_BITMAP),
    (_BANK1_SCREEN, _BANK1_BITMAP, CIA2.PORT_A_BANK_1, D018_BANK1),
)

# Upper bound on subtunes scanned by _choose_unified_display_layout. Each
# subtune costs one host-emu footprint run (~0.25 s), all paid once at
# setup(); the cap keeps a high-song-count SID from stalling startup. Above
# it, setup() keeps the per-subtune relocation behavior (still correct, just
# may move the display bank mid-cycle). Times of Lore (11 songs) fits.
_UNIFIED_LAYOUT_MAX_SONGS = 16

COLOR_NIBBLE_MASK = 0x0F

DEFAULT_VOICE_COLORS = ["cyan", "yellow", "light green"]
DEFAULT_WAVEFORM_COLORS = {
    "triangle":  "light green",
    "sawtooth":  "light red",
    "pulse":     "cyan",
    "noise":     "yellow",
    "off":       "dark gray",
}

# Text-row colors. Picked to (a) read against a black background and (b)
# avoid the default voice + waveform palettes so a static line of text
# isn't mistaken for a trace. Title row gets white for emphasis; the
# metadata row is light gray (muted, secondary information).
TITLE_TEXT_COLOR = "white"
METADATA_TEXT_COLOR = "light gray"

# Time-base modes.
TIME_BASE_WALLCLOCK = "wallclock"
TIME_BASE_AUTO = "auto"
TIME_BASE_NAMES = (TIME_BASE_WALLCLOCK, TIME_BASE_AUTO)

# Persistence/echo presets. Maps name → palette indices for past frames
# (oldest first). Each frame's trace is drawn with its corresponding color
# via per-cell screen-RAM writes; the current frame uses the voice's
# regular color. The C64 palette has 3 distinct grays (dark gray / gray /
# light gray) so the longest preset includes "black" as an invisible
# pacing slot, per the user's spec for "long".
#
# "off" disables echoes entirely (fast redraw-from-scratch path, identical
# byte-output to the pre-persistence implementation). "random" is a
# sentinel resolved to one of the named presets at scene setup. Per-frame
# decay is gone: each echo is a hard render at a fixed gray, not a fading
# intensity — fits 1bpp hardware naturally.
PERSISTENCE_ECHOES = {
    "off":    (),
    "short":  ("gray", "light gray"),                      # 2 visible echoes
    "medium": ("dark gray", "gray", "light gray"),         # 3 visible echoes
    "long":   ("black", "dark gray", "gray", "light gray"),# 4 slots (oldest invisible)
}
RANDOM_PERSISTENCE = "random"
PERSISTENCE_NAMES = (*PERSISTENCE_ECHOES.keys(), RANDOM_PERSISTENCE)
# Random pick excludes "off" — the user explicitly didn't ask for a fixed
# preset; the visual reward of random is the echo trail itself.
_PERSISTENCE_RANDOM_CHOICES = ("short", "medium", "long")

# Standard C64 character ROM. First 2 KB = uppercase + graphics charset
# (the one we want — screen-code 0x01 = 'A', 0x20 = ' ', etc.). The path
# matches the existing [preview] charset_path default + big_text's loader
# so all three consumers expect the same artifact.
_CHARGEN_PATH = "assets/roms/characters.901225-01.bin"
_GLYPHS_CACHE: bytes | None = None


def _load_glyphs() -> bytes:
    """Load the 2 KB uppercase charset. Cached process-wide.

    Falls back to framebuffer._builtin_charset() (a cv2-rendered ASCII
    font) if the ROM file is missing — keeps tests + minimal installs
    working, at the cost of glyphs that don't quite look C64-native."""
    global _GLYPHS_CACHE
    if _GLYPHS_CACHE is not None:
        return _GLYPHS_CACHE
    if os.path.exists(_CHARGEN_PATH):
        with open(_CHARGEN_PATH, "rb") as f:
            data = f.read(2048)
        if len(data) >= 2048:
            _GLYPHS_CACHE = data
            return _GLYPHS_CACHE
        log.warning("waveform: charset %s shorter than 2KB; using builtin",
                    _CHARGEN_PATH)
    from .framebuffer import _builtin_charset
    _GLYPHS_CACHE = _builtin_charset()
    return _GLYPHS_CACHE


def _ascii_to_screen_code(ch: str) -> int:
    """Map a single ASCII character to its C64 screen code (uppercase set).

    Letters A-Z → screen codes 0x01-0x1A; @ → 0x00; everything else
    passes through as ord(ch) & 0xFF (digits + most punctuation are
    identical between ASCII and screen codes). Chars the charset can't
    represent fall back to space (0x20) so unknown bytes render as a
    blank cell instead of a graphics glyph that would look like noise."""
    c = ord(ch.upper())
    if 0x40 <= c <= 0x5F:
        return (c - 0x40) & 0x3F            # @, A-Z, [\]^_
    if 0x20 <= c <= 0x3F:
        return c                            # space, digits, !"#... ?
    return 0x20                             # unknown → blank


def _layout_lr(left: str, right: str, width: int = SCREEN_W_CHARS) -> str:
    """Build a width-char line: `left` left-justified, `right` right-justified,
    spaces filling the gap. Both sides are truncated to keep at least one
    space of separation between them."""
    left = left[:width]
    right = right[:width]
    if len(left) + len(right) >= width:
        # Cap right at half the width first (composer is usually shorter
        # than title), then truncate left to whatever's left.
        max_right = max(1, width // 2 - 1)
        if len(right) > max_right:
            right = right[:max_right]
        max_left = width - len(right) - 1   # 1-char minimum gap
        if len(left) > max_left:
            left = left[:max_left]
    gap = width - len(left) - len(right)
    return left + (" " * gap) + right


def _layout_lcr(left: str, center: str, right: str,
                width: int = SCREEN_W_CHARS) -> str:
    """Build a width-char line with left/center/right fields. Center is
    placed at the geometric middle when room allows, then nudged off-center
    to avoid colliding with left or right if either is unusually long."""
    left = left[:width]
    center = center[:width]
    right = right[:width]
    # Truncate left first if everything together overflows — copyright is
    # usually the most flexible field (year + label is the gist).
    max_total = width - 2                    # leave 1-char gaps each side
    while len(left) + len(center) + len(right) > max_total and len(left) > 1:
        left = left[:-1]
    while len(left) + len(center) + len(right) > max_total and len(right) > 1:
        right = right[:-1]
    if len(left) + len(center) + len(right) > max_total:
        # Even with both sides minimal, center is too wide — truncate it.
        center = center[:max_total - len(left) - len(right)]
    # Try geometric center first.
    center_start = width // 2 - len(center) // 2
    center_start = max(center_start, len(left) + 1)
    center_start = min(center_start, width - len(right) - len(center) - 1)
    line = [" "] * width
    for i, c in enumerate(left):
        line[i] = c
    for i, c in enumerate(center):
        line[center_start + i] = c
    for i, c in enumerate(right):
        line[width - len(right) + i] = c
    return "".join(line)


# ---------------------------------------------------------------------------
# SID file header
# ---------------------------------------------------------------------------

@dataclass
class SidHeader:
    magic: str
    version: int
    num_songs: int
    start_song: int
    name: str
    author: str
    released: str
    # Decoded PSID v2+ flags. None on v1 headers (no flags field).
    clock: str | None         # "PAL", "NTSC", "PAL+NTSC", "?" or None
    sid_model: str | None     # "6581", "8580", "6581+8580", "?" or None


# PSID v2+ flags byte 1 (low-order) layout — clock at bits 2-3, primary
# SID model at bits 4-5. Higher-order model bits for 2nd/3rd SIDs live in
# byte 0 of the 2-byte flags field; the waveform UI only surfaces the
# primary chip + clock so we ignore the rest.
_CLOCK_TABLE = {0: "?", 1: "PAL", 2: "NTSC", 3: "PAL+NTSC"}
_MODEL_TABLE = {0: "?", 1: "6581", 2: "8580", 3: "6581+8580"}


def _sid_payload_extent(sid_bytes: bytes) -> tuple[int, int]:
    """Return (load_addr, end_addr_exclusive) for the SID's payload bytes
    once loaded on the C64. Mirrors the load-address handling in
    `api.parse_psid_for_player` without re-running its full validation —
    used by WaveformScene to refuse tunes whose payload would clobber
    the hires bitmap or screen RAM area when the scene sets up its
    display. Assumes the SID header has already been validated (magic +
    minimum length) by `parse_sid_header`."""
    data_offset = int.from_bytes(sid_bytes[6:8], "big")
    load_addr = int.from_bytes(sid_bytes[8:10], "big")
    payload = sid_bytes[data_offset:]
    if load_addr == 0 and len(payload) >= 2:
        load_addr = payload[0] | (payload[1] << 8)
        payload = payload[2:]
    return load_addr, load_addr + len(payload)


def _overlaps(lo: int, hi: int, region_lo: int, region_size: int) -> bool:
    """True when [lo, hi) overlaps the region [region_lo, region_lo+size)."""
    region_hi = region_lo + region_size
    return lo < region_hi and hi > region_lo


def _bank_payload_feasible(payload_lo: int, payload_hi: int,
                           screen_base: int, bitmap_base: int) -> bool:
    """True when the SID payload alone clears a bank's screen + bitmap
    regions (a cheap, footprint-free pre-check used by _load_sid_file)."""
    return not (_overlaps(payload_lo, payload_hi, screen_base, SCREEN.N_CELLS)
                or _overlaps(payload_lo, payload_hi,
                             bitmap_base, SCREEN.BITMAP_BYTES))


def _any_display_bank_fits_payload(payload_lo: int, payload_hi: int) -> bool:
    """True when at least one candidate VIC bank's display regions clear the
    payload. Used by _load_sid_file to refuse only the truly hopeless tunes
    (payload covers every bank's display) — the footprint-aware bank choice
    is deferred to _choose_display_layout at setup()."""
    return any(_bank_payload_feasible(payload_lo, payload_hi, s, b)
               for s, b, _, _ in _DISPLAY_BANKS)


def _choose_display_layout(payload_lo: int, payload_hi: int,
                           footprint: bytes | bytearray
                           ) -> tuple[int, int, int, int]:
    """Pick (screen_base, bitmap_base, dd00, d018) for the waveform display.

    Returns the first candidate VIC bank whose screen + bitmap regions are
    clear of both the SID payload and the tune's runtime access footprint
    (`footprint` should be the PLAY read+write footprint from
    [ram_play_access_footprint] — a region PLAY merely *reads*, like Galway's
    per-song data at $B400, is live and must not be painted over). Raises
    ValueError when no bank is free. See _DISPLAY_BANKS."""
    for screen_base, bitmap_base, dd00, d018 in _DISPLAY_BANKS:
        screen_hi = screen_base + SCREEN.N_CELLS
        bitmap_hi = bitmap_base + SCREEN.BITMAP_BYTES
        blocked = (
            _overlaps(payload_lo, payload_hi, screen_base, SCREEN.N_CELLS)
            or _overlaps(payload_lo, payload_hi, bitmap_base,
                         SCREEN.BITMAP_BYTES)
            or any(footprint[screen_base:screen_hi])
            or any(footprint[bitmap_base:bitmap_hi]))
        if not blocked:
            return screen_base, bitmap_base, dd00, d018
    raise ValueError(
        f"waveform: SID payload ${payload_lo:04X}-${payload_hi:04X} plus its "
        f"runtime footprint leave no free VIC bank for the display (tried "
        f"bank 0 $0400/$2000, bank 2 $8400/$A000, bank 1 $5400/$6000)")


def _choose_unified_display_layout(sid_bytes: bytes, payload_lo: int,
                                   payload_hi: int, num_songs: int
                                   ) -> tuple[int, int, int, int] | None:
    """Pick ONE VIC bank free for the UNION of every subtune's PLAY-access
    footprint, so SHIFT-cycling never has to relocate the display.

    Per-subtune `_choose_display_layout` (the fallback) can land different
    subtunes on different banks — Galway's Times of Lore puts song 1 on bank
    2 (bank 2 is free for *its* footprint and ranks ahead of bank 1) but
    songs 2-11 on bank 1 (they read per-song data from bank 2's $B400). The
    bank move during a live cycle glitches the matrix (garbled text). If a
    single bank clears the OR of all subtunes' PLAY footprints, using it for
    every subtune means the display is fixed for the whole tune and cycling
    only ever repaints — no $DD00/$D018 move.

    Returns the shared layout, or None when no single bank fits all subtunes
    (caller falls back to per-subtune `_choose_display_layout`). Cost is one
    host-emu footprint run per subtune; the caller bounds the song count."""
    union = np.zeros(0x10000, dtype=np.uint8)
    for song in range(1, num_songs + 1):
        fp = ram_play_access_footprint(sid_bytes, song=song)
        union |= np.frombuffer(bytes(fp), dtype=np.uint8)
    try:
        return _choose_display_layout(payload_lo, payload_hi,
                                      union.tobytes())
    except ValueError:
        return None


def _play_bank_for_footprints(write_fp: bytes | bytearray,
                              access_fp: bytes | bytearray) -> int | None:
    """Return the $01 CPU-port override the player should use around JSR play,
    or None to let api.run_sid_player's address-keyed heuristic decide.

    The heuristic banks on the play *address* page, but a tune can read its
    live song data from RAM under BASIC ROM ($A000-$BFFF) while its code sits
    below it — Galway's Times of Lore subtunes 2-11 copy per-song data to
    $B400 at INIT and read it back every PLAY. With the default $37 (BASIC
    mapped) PLAY reads ROM there instead of the data → silence. We return
    $36 (BASIC out) when PLAY reads an address under BASIC ROM that the tune
    also *wrote* — proof it's RAM data, not the ROM itself. A tune that reads
    BASIC ROM *as data* (e.g. Galway's Comic Bakery table) writes nothing
    there, so the intersection is empty and we keep $37."""
    lo, hi = ROM.BASIC_LO, ROM.BASIC_HI
    w = np.frombuffer(bytes(write_fp[lo:hi]), dtype=np.uint8)
    a = np.frombuffer(bytes(access_fp[lo:hi]), dtype=np.uint8)
    if bool((w & a).any()):
        return CPU.PORT_BASIC_OUT
    return None


def parse_sid_header(data: bytes) -> SidHeader:
    """Parse the PSID/RSID v1+ header. Validates magic, returns metadata.

    Reads the v2+ flags field at offset 0x76 (2 bytes, big-endian) to
    surface SID chip model + PAL/NTSC clock. v1 headers (length 118)
    leave both as None."""
    if len(data) < 22:
        raise ValueError("SID file too short to contain a header")
    magic = data[:4]
    if magic not in (b"PSID", b"RSID"):
        raise ValueError(
            f"not a SID file (expected PSID/RSID magic, got {magic!r})")
    version = int.from_bytes(data[4:6], "big")
    clock: str | None = None
    sid_model: str | None = None
    if version >= 2 and len(data) >= 0x78:
        # flags lives at 0x76 (16 bits big-endian); clock/model bits are in
        # the low byte (0x77).
        flags_lo = data[0x77]
        clock = _CLOCK_TABLE[(flags_lo >> 2) & 0x03]
        sid_model = _MODEL_TABLE[(flags_lo >> 4) & 0x03]
    return SidHeader(
        magic=magic.decode("ascii"),
        version=version,
        num_songs=int.from_bytes(data[14:16], "big"),
        start_song=int.from_bytes(data[16:18], "big"),
        name=data[22:54].rstrip(b"\x00").decode("latin-1", "replace"),
        author=data[54:86].rstrip(b"\x00").decode("latin-1", "replace"),
        released=data[86:118].rstrip(b"\x00").decode("latin-1", "replace") if len(data) >= 118 else "",
        clock=clock,
        sid_model=sid_model,
    )


# ---------------------------------------------------------------------------
# Waveform scene
# ---------------------------------------------------------------------------

class WaveformScene(Scene):
    WANTS_AUDIO_LOCK = True

    # SHIFT-cycle "is this subtune worth showing" floor. Many SIDs carry
    # game SFX as their tail subtunes (1-3 s blips); landing on one shows
    # a flat scope trace for the bulk of the displayed time. When the
    # SongLengths DB knows a candidate's length and it falls below this
    # threshold, cycle_style advances again. Only applies on SHIFT —
    # startup honors whatever song the user configured, no matter how
    # short — and only when duration_s wasn't explicitly pinned by the
    # user (an explicit duration is itself a strong "play this" signal).
    MIN_CYCLE_SUBTUNE_S = 5.0

    # End-of-tune silence detection. When all three voice envelopes sit
    # below ENV_SILENCE_EPS continuously for END_SILENCE_S seconds — after
    # the tune has produced at least some sound — the scene ends so the
    # playlist advances (single-scene mode replays from INIT) instead of
    # holding a frozen flat scope. The window is generous so brief musical
    # rests don't trip it; it only ever shortens playback.
    END_SILENCE_S = 6.0
    ENV_SILENCE_EPS = 1e-3

    # Host-emu PLAY pre-flight. After loading a tune we run this many PLAY
    # passes; if EVERY one bails at the host emulator's cycle cap (instead
    # of returning normally in the usual ~1-2k cycles), the tune spins on a
    # raster/IRQ this pure-Python 6502 never provides. Such a tune can't be
    # rendered faithfully by the scope AND would hang the C64-side player —
    # its `SEI; JSR init` sits with IRQs masked, so the kernal IRQ never
    # fires, $028D stops updating, and the machine goes dead/silent (the
    # Hollywood Poker Pro failure). Reject it so the picker skips to the
    # next candidate. 50 passes ≈ 1 s of PLAY @ 50 Hz — long enough to be
    # unambiguous, short enough that a healthy tune adds only ~5 ms.
    _PLAY_PREFLIGHT_TICKS = 50

    # Upper bound on PLAY ticks the poll thread will execute in a single
    # wakeup to catch the host emulator up to wall-clock (see _poll_regs).
    # A py65 PLAY pass is ~0.2 ms, so 120 ticks (~2 s of music) cost ~25 ms
    # — bounds the burst after a long stall/suspend while still resyncing
    # within a couple of wakeups (throughput is ~4500 ticks/s, far above
    # the 50/60 Hz target). The normal startup catch-up is only ~6-18 ticks.
    _MAX_CATCHUP_TICKS = 120

    # PLAY passes to run when probing a tune's effective PLAY rate (see
    # _detect_play_rate_hz). Many multispeed players program CIA #1 Timer A
    # from PLAY, not INIT — Galway's Times of Lore is a ~2x multispeed whose
    # Timer A latch is only written on the FIRST PLAY pass. play_rate_hz reads
    # that latch, so the true rate is unknowable until PLAY has run at least
    # once. The probe stops as soon as a multispeed write appears; a vsync tune
    # (no Timer A write) runs all passes and falls back to the video rate. 64
    # passes (~1 s @ 60 Hz) costs only a few ms on a throwaway host emu.
    _RATE_PROBE_TICKS = 64

    def __init__(self, api: C64Backend, audio: AudioStreamer | None,
                 file: str,
                 song: int = 0,
                 duration_s: float | None = None,
                 target_fps: float | None = None,
                 system: str = "NTSC",
                 color_mode: str = "per_voice",
                 voice_colors: list | None = None,
                 waveform_colors: dict | None = None,
                 time_base: str = TIME_BASE_WALLCLOCK,
                 auto_cycles: float = 4.0,
                 persistence: str = "off",
                 scroll_columns: int | list[int] = 0,
                 reg_poll_hz: float | None = None,
                 songlengths_db: LengthsDB | None = None):
        """Initialize the scene.

        file: a `resolve_file_spec` spec — single .sid path, a directory,
              a glob, or a comma-separated combination. A multi-entry pool
              picks a random candidate at each `setup()` (so single-scene
              loops rotate through the pool); a single literal path stays
              deterministic. Candidates whose payload overlaps the hires
              bitmap or screen RAM area are skipped at pick time with a
              log — bounded retries before the scene aborts.
        duration_s: if None, looks up the subtune's length in the
                    SongLengths DB (if provided); else defaults to 180s.
        songlengths_db: optional preloaded LengthsDB instance for
                    auto-duration lookup. See c64cast/songlengths.py.
        time_base / auto_cycles / persistence / scroll_columns: see
                    PERSISTENCE_ECHOES and the per-voice render loop in
                    _render_hires(). Defaults match the redraw-from-scratch
                    wallclock-locked behavior of the prior implementation.
        """
        from .config import SID_EXTS, resolve_file_spec
        if color_mode not in ("per_voice", "per_waveform"):
            raise ValueError(
                "waveform: color_mode must be 'per_voice' or 'per_waveform'")
        if time_base not in TIME_BASE_NAMES:
            raise ValueError(
                f"waveform: time_base must be one of {TIME_BASE_NAMES}, "
                f"got {time_base!r}")
        if auto_cycles <= 0:
            raise ValueError(
                f"waveform: auto_cycles must be > 0, got {auto_cycles!r}")
        if persistence not in PERSISTENCE_NAMES:
            raise ValueError(
                f"waveform: persistence must be one of {PERSISTENCE_NAMES}, "
                f"got {persistence!r}")

        # Stash the spec + config args so each setup() can re-pick (and so
        # cycle_style() can re-resolve the per-song duration the same way
        # __init__ does the first one).
        self.file_spec = file
        self._song_arg = song
        self.songlengths_db = songlengths_db
        self._explicit_duration_s = duration_s

        # Initial resolution: __init__ raises on bad specs (mirrors
        # validate_scene_cfg). Also raises if every candidate fails the
        # payload-extent check below. setup() re-picks from a fresh
        # rescan, so directory contents can change between iterations.
        self._candidates = resolve_file_spec(file, SID_EXTS, label="waveform")
        self._pick_and_load_sid()

        name = (self.header.name.strip()
                or os.path.splitext(os.path.basename(self._sid_file))[0])
        super().__init__(api, audio, None,
                         f"SID: {name} #{self.song}")
        # True when prepare_next() has already picked+loaded this
        # iteration's tune (and refreshed self.name); setup() then skips
        # the re-pick to avoid loading the SID twice. See
        # CommercialScene._prepared for the full rationale.
        self._prepared = False
        # Default to HALF the system video rate (30 NTSC / 25 PAL). An
        # oscilloscope reads fine at half-rate (see the Cam Link captures in
        # the framerate/DMA investigation), and a half-integer divisor keeps
        # the render an exact submultiple of the video standard so the
        # wallclock phase-lock below stays clean. Critically it halves the
        # per-frame DMA write volume: each frame pushes 3 voice bitmap strips
        # that nearly all change every frame, so full rate is ~170 writes/s —
        # right at the ~200/s DMA ceiling. HW-verified 2026-06-09: at ~170/s
        # into a bank-2-relocated display ($A000-$BFFF, used when the SID
        # payload overlaps bank 0's bitmap) the U64 power-cycles itself
        # mid-tune (Times_of_Lore); at half rate (~90/s) the same tune plays
        # its full length cleanly. See docs/caveats.md. An explicit
        # target_fps (CLI/TOML) still wins. The host-emu poll rate is
        # independent (self._video_hz below) and stays at the full video rate
        # so the scope keeps tracking every PLAY tick.
        if target_fps is None:
            target_fps = 25.0 if system.upper() == "PAL" else 30.0
        self.target_fps = float(target_fps)
        # One displayed row of waveform covers exactly one display frame
        # of audio time — locks the trace's visible phase to wall-clock.
        self._frame_time_s = 1.0 / self.target_fps
        self.system = system
        self.duration_s = float(self._resolve_duration_for_current_sid())
        self.color_mode = color_mode
        self.voice_color_names = list(voice_colors or DEFAULT_VOICE_COLORS)
        if len(self.voice_color_names) < 3:
            raise ValueError("waveform: voice_colors must have 3 entries")
        wf_defaults = dict(DEFAULT_WAVEFORM_COLORS)
        wf_defaults.update(waveform_colors or {})
        self.waveform_color_names = wf_defaults

        self.emulator = SIDEmulator(system=system)
        self._reg_buf: bytes | None = None
        self._reg_lock = threading.Lock()
        # Default the poll rate to the system video rate — that's the
        # SID's effective PLAY-per-frame cadence on a kernal IRQ, so
        # matching it keeps the host emulator's writes in step with
        # what the real 6510 is doing on the U64.
        # PLAY tick rate. The host emulator must advance the song at the
        # SAME rate the real U64 calls PLAY, or the scope drifts out of sync
        # with the audio. That's the video rate for vsync tunes, but a
        # CIA-timed (multispeed) tune programs its own faster rate — resolved
        # per-tune from the host emulator in _resolve_poll_rate(). An explicit
        # reg_poll_hz pins the rate and disables auto-detection.
        self._user_reg_poll_hz = reg_poll_hz
        self._video_hz = 50.0 if system.upper() == "PAL" else 60.0
        # Host-emu clock anchor. The poll thread derives its PLAY-tick count
        # from wall-clock elapsed since the real SID started (set in setup()
        # after run_sid_player), not from the number of poll wakeups — see
        # _poll_regs. Set before the poll thread is built so they always exist.
        self._sid_start_time = 0.0
        self._ticks_done = 0
        self._resolve_poll_rate()

        self.start_time = 0.0
        # Track the last waveform per voice so per_waveform color RAM
        # writes only fire on transitions.
        self._last_voice_wave: list[int] = [-1, -1, -1]
        # End-of-tune silence detection (Part 3): arm only after the tune
        # has actually produced sound, then end the scene after a sustained
        # all-voices-silent window so a short non-looping subtune doesn't
        # hold a frozen flat scope for the rest of duration_s.
        self._ever_sounded = False
        self._silence_since: float | None = None

        # Time-base + auto-cycles knobs.
        self.time_base = time_base
        self.auto_cycles = float(auto_cycles)

        # Scroll: normalize scalar → list-of-3. Each entry is the number
        # of new columns drawn (and the strip is shifted left by) per
        # frame for that voice. 0 = no scroll (full-frame redraw).
        if isinstance(scroll_columns, int):
            sc_list = [scroll_columns, scroll_columns, scroll_columns]
        else:
            sc_list = list(scroll_columns)
        if len(sc_list) != 3:
            raise ValueError(
                f"waveform: scroll_columns list must have 3 entries, "
                f"got {sc_list!r}")
        for x in sc_list:
            if not isinstance(x, int) or x < 0 or x > BITMAP_W:
                raise ValueError(
                    f"waveform: scroll_columns entries must be ints in "
                    f"0..{BITMAP_W}, got {sc_list!r}")
        self.scroll_columns: list[int] = sc_list

        # Persistence: resolve "random" sentinel now so the chosen preset
        # is stable across setup/teardown cycles within this scene instance.
        self.persistence_config = persistence
        if persistence == RANDOM_PERSISTENCE:
            self.persistence = random.choice(_PERSISTENCE_RANDOM_CHOICES)
        else:
            self.persistence = persistence
        # Past-frame color ramp (palette indices, oldest first). Each entry
        # corresponds to one history slot drawn at that gray; the current
        # frame is overlaid on top in the voice's regular color.
        echo_names = PERSISTENCE_ECHOES[self.persistence]
        self._echo_colors: list[int] = [
            C64_COLORS.get(n, C64_COLORS["black"]) for n in echo_names
        ]
        self._echo_depth = len(self._echo_colors)
        # Echo mode is per-voice only meaningful when no scroll: scroll
        # already gives a natural "trail off the left edge" effect, and
        # mixing the two double-draws the same trace at different x's
        # every frame. Compute the per-voice render mode up front.
        # Tri-state per voice: "fast" / "scroll" / "echo".
        self._voice_render_modes: list[str] = []
        for sn in self.scroll_columns:
            if sn > 0:
                self._voice_render_modes.append("scroll")
            elif self._echo_depth > 0:
                self._voice_render_modes.append("echo")
            else:
                self._voice_render_modes.append("fast")

        # Per-render persistent state. Allocated in _setup_hires().
        # _strips: per-voice scroll-mode bool strip (only used by scroll
        #          path; persists across frames so the shift can rotate
        #          old samples left).
        # _echo_history: per-voice list of past bool masks, oldest first,
        #          length up to _echo_depth (only used by echo path).
        # _last_y: per-voice last column's y from the previous frame
        #          (scroll path uses this so the first new column connects
        #          to the last drawn column instead of being a self-dot).
        # _rows_col: cached row-index broadcast column used by every path.
        self._strips: list[np.ndarray | None] | None = None
        self._echo_history: list[list[np.ndarray]] | None = None
        self._last_y: list[int | None] | None = None
        self._rows_col: np.ndarray | None = None
        # Fast-path detection: no per-voice persistent state is needed at
        # all when every voice is in "fast" mode.
        self._fast_path = all(m == "fast" for m in self._voice_render_modes)

        # Hires-only: 2 KB charset (loaded once, cached process-wide) and
        # the cell column where the song number's first digit lands in the
        # title row. The latter is recomputed each time the title row is
        # rebuilt, since title-truncation rules can move the number's
        # position.
        self._glyphs: bytes | None = None
        self._song_num_col: int = 0

        # Display location (VIC bank), resolved per-tune in setup() by
        # _choose_display_layout and re-resolved per-subtune in cycle_style().
        # Defaults to bank 0; relocates to bank 2 or bank 1 when the tune's
        # payload/footprint occupies bank 0's display.
        self._screen_base: int = VIC_BANK_0.SCREEN
        self._bitmap_base: int = VIC_BANK_0.BITMAP
        self._dd00: int = CIA2.PORT_A_BANK_0
        self._d018: int = D018_HIRES_BITMAP
        # One display bank free for the UNION of every subtune's PLAY
        # footprint, so SHIFT-cycling never relocates the display (kills the
        # garbled-text glitch on a live bank move — e.g. Times of Lore song 1
        # on bank 2 vs songs 2-11 on bank 1). Computed once per SID in setup()
        # and reused by cycle_style for every subtune. None = no single bank
        # fits all subtunes (or num_songs out of range) → per-subtune
        # relocation fallback. _unified_layout_for caches the SID path so the
        # hard-relaunch re-setup() and pool re-picks recompute only on change.
        self._unified_layout: tuple[int, int, int, int] | None = None
        self._unified_layout_for: str | None = None
        # Whether the song currently playing needs $01=$36 (BASIC out) for its
        # PLAY — i.e. it reads live data from RAM under BASIC ROM. cycle_style
        # uses this to decide between the fast in-place cue and a hard relaunch:
        # entering the $36 group from a $37 song needs the relaunch; cycling
        # within the group (or back out to a $37 song) works via cue. Set by
        # setup() and cycle_style. See cycle_style.
        self._current_needs_basic_out: bool = False

    # ---- SID selection / loading -------------------------------------------

    # Each candidate gets one attempt; on a payload-extent rejection we
    # log and move on to the next. Bounded so a directory full of bad SIDs
    # eventually surfaces as a hard failure instead of silent retry.
    _MAX_PICK_ATTEMPTS = 8

    def _resolve_candidates(self) -> list[str]:
        """Re-resolve the spec at setup time so directory contents can
        change between iterations (newly dropped SIDs are picked up)."""
        from .config import SID_EXTS, resolve_file_spec
        return resolve_file_spec(self.file_spec, SID_EXTS, label="waveform")

    def _load_sid_file(self, path: str) -> None:
        """Load + parse + validate one SID at `path`. Raises ValueError on
        any rejection (header parse, song-out-of-range, payload overlap).
        On success sets self._sid_file, self.sid_bytes, self.header,
        self.song, self._host_emu and self._song_num_width."""
        if not os.path.exists(path):
            raise ValueError(f"waveform: SID file not found: {path}")
        with open(path, "rb") as f:
            sid_bytes = f.read()
        header = parse_sid_header(sid_bytes)
        if self._song_arg < 0 or (self._song_arg > header.num_songs
                                  and self._song_arg != 0):
            raise ValueError(
                f"waveform: song {self._song_arg} out of range "
                f"0..{header.num_songs} for {os.path.basename(path)}")
        # Refuse only SIDs whose payload would clobber EVERY candidate VIC
        # bank's display regions — i.e. no bank can host the hires bitmap +
        # screen RAM. The display relocates to bank 2 ($8400/$A000) or bank 1
        # ($5400/$6000) when the payload overlaps bank 0's ($0400/$2000); only
        # tunes spanning every bank (e.g. Last_Ninja_2 $2700-$CF8F) are
        # hopeless. The footprint-aware bank choice is made at setup() by
        # _choose_display_layout — this is just the cheap payload-only
        # pre-filter that lets bad candidates be skipped in a multi-file pool.
        # See _DISPLAY_BANKS.
        payload_lo, payload_hi = _sid_payload_extent(sid_bytes)
        if not _any_display_bank_fits_payload(payload_lo, payload_hi):
            raise ValueError(
                f"waveform: SID payload ${payload_lo:04X}-${payload_hi:04X} "
                f"overlaps the display regions of every candidate VIC bank "
                f"(bank 0 $0400/$2000, bank 2 $8400/$A000, bank 1 $5400/$6000); "
                f"WaveformScene can't place the bitmap + screen RAM without "
                f"clobbering the tune. Pick a smaller SID.")

        self._sid_file = path
        self.sid_bytes = sid_bytes
        self.header = header
        self.song = self._song_arg if self._song_arg > 0 else header.start_song
        # The host emulator runs the same SID file in parallel on a
        # pure-Python 6502 so we can recover live $D4xx register state
        # — the U64's SID is faithful to real hardware (writes only,
        # reads return 0). Construction loads + runs INIT once; each
        # tick_play() advances one PLAY pass. See sid_host_emu.py.
        self._host_emu = SidHostEmu(self.sid_bytes, song=self.song)
        # PLAY pre-flight: reject tunes whose PLAY spins past the cycle cap
        # on every pass (see _PLAY_PREFLIGHT_TICKS). Done here so the picker
        # skips them and a single-file scene aborts with a clear message,
        # rather than the C64-side player hanging the machine at setup().
        capped_all = True
        for _ in range(self._PLAY_PREFLIGHT_TICKS):
            self._host_emu.tick_play()
            if not self._host_emu.last_routine_capped:
                capped_all = False
                break
        if capped_all:
            raise ValueError(
                f"waveform: {os.path.basename(path)} PLAY never completes "
                f"within the host emulator's cycle cap over "
                f"{self._PLAY_PREFLIGHT_TICKS} passes — the tune spins on a "
                f"raster/IRQ the player environment doesn't provide; it would "
                f"hang the C64-side player (silent + unresponsive). Refused.")
        # The pre-flight advanced the emulator by up to one non-capped PLAY
        # pass (it breaks on the first that returns). That ~20 ms head start
        # is cosmetically irrelevant to the scope and not worth a rebuild.
        # Song-number column width is derived from num_songs — recompute
        # so multi-pick scenes get the right padding per chosen SID.
        self._song_num_width = len(str(max(self.header.num_songs, 1)))

    def _pick_and_load_sid(self) -> None:
        """Re-resolve the spec, pick a random candidate, load it. Retries
        on ValueError up to _MAX_PICK_ATTEMPTS with the offending file
        removed from the shuffled pool. Raises if every attempt fails."""
        self._candidates = self._resolve_candidates()
        pool = list(self._candidates)
        random.shuffle(pool)
        last_error: Exception | None = None
        for path in pool[:self._MAX_PICK_ATTEMPTS]:
            try:
                self._load_sid_file(path)
            except ValueError as e:
                log.warning("waveform: skipping %s: %s",
                            os.path.basename(path), e)
                last_error = e
                continue
            if len(self._candidates) > 1:
                log.info("waveform: picked %s from %d candidates",
                         os.path.basename(path), len(self._candidates))
            return
        # Every attempted candidate failed validation. Re-raise the last
        # error so the caller (validate_scene_cfg or setup) sees a real
        # message instead of a generic "no SID loaded".
        raise ValueError(
            f"waveform: file spec {self.file_spec!r} resolved to "
            f"{len(self._candidates)} candidate(s) but none could be "
            f"loaded; last error: {last_error}")

    def _resolve_duration_for_current_sid(self) -> float:
        """Compute the playback duration for self.sid_bytes + self.song.
        Order: explicit user duration_s > songlengths DB > 180s default."""
        if self._explicit_duration_s is not None:
            return float(self._explicit_duration_s)
        if self.songlengths_db is not None:
            looked_up = self.songlengths_db.lookup(self.sid_bytes, self.song)
            if looked_up is not None:
                log.info("waveform: songlengths matched %s #%d → %.1fs",
                         os.path.basename(self._sid_file), self.song,
                         looked_up)
                return float(looked_up)
        return 180.0

    # ---- bring-up / bring-down ---------------------------------------------

    def _repick_sid(self) -> bool:
        """Multi-entry pool: re-pick + load a tune so directories rotate
        between iterations, then refresh derived state (duration, title row,
        scene name — with the SID's embedded title, extension-stripped
        basename fallback) to reflect it. Returns False if every candidate
        was rejected / the directory is now empty."""
        try:
            self._pick_and_load_sid()
        except ValueError as e:
            log.error("waveform: %s", e)
            return False
        self.duration_s = float(self._resolve_duration_for_current_sid())
        name = (self.header.name.strip()
                or os.path.splitext(os.path.basename(self._sid_file))[0])
        self.name = f"SID: {name} #{self.song}"
        return True

    def prepare_next(self) -> None:
        """Pick + load the upcoming tune now (multi-entry pools only) so the
        preceding interstitial shows the real SID title rather than the
        previously-played tune. setup() consumes this pick — the SID isn't
        loaded twice."""
        if len(self._candidates) > 1 and self._repick_sid():
            self._prepared = True

    def setup(self):
        self.is_done = False
        # Multi-entry pool: re-pick so directories rotate between
        # iterations (skipped when prepare_next() already did it this
        # iteration). Single-entry pool: keep __init__'s pick (and any
        # cycle_style mutation of self.song) — re-loading the same SID is
        # wasted work and would reset the subtune to start_song each
        # repeat. On total failure (every candidate rejected, or
        # directory now empty) log and let the playlist advance.
        if self._prepared:
            self._prepared = False
        elif len(self._candidates) > 1 and not self._repick_sid():
            super().setup()
            self.is_done = True
            return
        self.start_time = time.time()
        self._last_voice_wave = [-1, -1, -1]
        self._ever_sounded = False
        self._silence_since = None
        overlay_names = [getattr(ov, "name", type(ov).__name__)
                         for ov in self.overlays]
        ov_str = ", ".join(overlay_names) if overlay_names else "no overlays"
        # Surface the resolved knobs so a `persistence = "random"` config
        # leaves a trace of which preset actually got picked.
        persist_label = self.persistence
        if self.persistence_config == RANDOM_PERSISTENCE:
            persist_label = f"{self.persistence} (random)"
        log.info(
            "scene %r: SID color_mode=%s duration=%.1fs @ %.0ffps "
            "time_base=%s persistence=%s scroll=%s [%s]",
            self.name, self.color_mode, self.duration_s,
            self.target_fps, self.time_base, persist_label,
            self.scroll_columns, ov_str,
        )

        # Stop our 4-bit DAC streaming if it's running — the SID is about
        # to write $D418 itself.
        if self.audio is not None:
            try:
                self.audio.stop()
            except Exception:
                log.exception("waveform: pre-SID audio stop failed")

        # Footprint the tune on a throwaway host emu in two views:
        #   * the full INIT+PLAY *write* footprint places the player's
        #     relocation hole (the player MC must survive INIT too — tunes
        #     like Beat_Dis use the page past their payload as INIT scratch).
        #   * the PLAY *read+write* footprint drives the display-bank choice:
        #     the bitmap is painted after INIT and refreshed every frame, so
        #     a region the tune only scratches at INIT is paintable — but a
        #     region PLAY *reads* every frame is live data we must not
        #     clobber. Galway's Times of Lore copies a per-song block into
        #     bank 2's $B400 at INIT and reads it back there on every PLAY;
        #     the write-only view missed that read and clobbered it.
        footprint = ram_write_footprint(self.sid_bytes, song=self.song)
        display_footprint = ram_play_access_footprint(self.sid_bytes,
                                                      song=self.song)
        payload_lo, payload_hi = _sid_payload_extent(self.sid_bytes)

        # For a multi-song SID, try to pin ONE display bank free for the union
        # of every subtune's PLAY footprint so SHIFT-cycling never relocates
        # the display (the bank move on a live cycle garbles the matrix — e.g.
        # Times of Lore song 1 wants bank 2, songs 2-11 want bank 1). Cached
        # per SID path: the hard-relaunch re-setup() and an unchanged pool pick
        # reuse it; a different SID (pool re-pick) recomputes. Bounded by song
        # count so a many-subtune SID doesn't stall startup. None = no single
        # bank fits all → fall through to the per-subtune choice below.
        n_songs = self.header.num_songs
        if 1 < n_songs <= _UNIFIED_LAYOUT_MAX_SONGS:
            if self._unified_layout_for != self._sid_file:
                self._unified_layout = _choose_unified_display_layout(
                    self.sid_bytes, payload_lo, payload_hi, n_songs)
                self._unified_layout_for = self._sid_file
        else:
            self._unified_layout = None
            self._unified_layout_for = None

        # Choose the VIC bank for the display: the unified bank when one fits
        # all subtunes, else bank 0 ($0400/$2000) by default, relocating to
        # bank 2 ($8400/$A000) or bank 1 ($5400/$6000) when the payload or PLAY
        # access footprint occupies it. $DD00 selects the bank and $D018 the
        # sub-bank screen/bitmap offset. On no free bank, abort the scene
        # (playlist advances) — same graceful path as a playback failure.
        try:
            if self._unified_layout is not None:
                (self._screen_base, self._bitmap_base, self._dd00,
                 self._d018) = self._unified_layout
                log.info("waveform: display bank pinned for all %d subtunes "
                         "(screen=$%04X bitmap=$%04X $DD00=$%02X $D018=$%02X) "
                         "— SHIFT-cycle won't relocate", n_songs,
                         self._screen_base, self._bitmap_base, self._dd00,
                         self._d018)
            else:
                (self._screen_base, self._bitmap_base, self._dd00,
                 self._d018) = _choose_display_layout(payload_lo, payload_hi,
                                                      display_footprint)
        except ValueError as e:
            log.error("waveform: %s — scene aborting.", e)
            self.is_done = True
            return
        if self._dd00 != CIA2.PORT_A_BANK_0:
            log.info("waveform: display relocated to VIC bank @ "
                     "screen=$%04X bitmap=$%04X ($DD00=$%02X $D018=$%02X) — "
                     "payload $%04X-$%04X / PLAY footprint occupies bank 0's "
                     "display", self._screen_base, self._bitmap_base,
                     self._dd00, self._d018, payload_lo, payload_hi)

        # Build the player "avoid" bitmap: the tune's footprint + this
        # scene's CHOSEN display regions + the audio ring. Slice-assignment
        # keeps it cheap.
        avoid = bytearray(footprint)
        for lo, hi in (
                (self._screen_base, self._screen_base + SCREEN.N_CELLS),
                (self._bitmap_base, self._bitmap_base + SCREEN.BITMAP_BYTES),
                (RING_BUFFER_ADDR, RING_BUFFER_END)):
            avoid[lo:hi] = b"\x01" * (hi - lo)

        # Decide the PLAY $01 bank: $36 (BASIC out) when this subtune reads
        # live song data from RAM under BASIC ROM (e.g. ToL 2-11 at $B400),
        # else None (heuristic). See _play_bank_for_footprints.
        play_bank = _play_bank_for_footprints(footprint, display_footprint)
        self._current_needs_basic_out = play_bank == CPU.PORT_BASIC_OUT

        # Upload the SID payload + tiny player MC and kick the BASIC SYS
        # stub. The real 6510 then drives INIT + PLAY on a CIA #1 IRQ
        # that chains to kernal $EA31, so display + keyboard scan stay
        # under our control. See api.run_sid_player for the layout.
        try:
            self.api.run_sid_player(self.sid_bytes, song=self.song,
                                    avoid=avoid, play_bank=play_bank)
        except Exception as e:
            log.error("waveform: SID playback failed to start (%s) — "
                      "scene aborting. PSID-only; RSIDs, SIDs that load "
                      "below $0820, and SIDs under KERNAL ROM "
                      "($E000-$FFFF) are rejected.", e)
            self.is_done = True
            return

        # Anchor the host-emu clock to when the real SID started. The poll
        # thread (below) derives its PLAY-tick count from wall-clock elapsed
        # since this instant — so it catches up through the _setup_hires
        # bitmap-clear gap and stays locked to the audio, rather than
        # starting late and drifting behind. See _poll_regs.
        self._sid_start_time = time.time()
        self._ticks_done = 0

        # Configure VIC for hires bitmap mode. Paints over the full
        # screen so no scene state from before matters.
        self.api.invalidate_cache()
        self._setup_hires()

        # Resolve the host-emu PLAY rate for this tune (vsync vs CIA
        # multispeed) and (re)build the poll thread at that period — the
        # pool re-pick above may have loaded a different tune. Then start it.
        self._resolve_poll_rate()
        self._poll.start()

    def teardown(self):
        super().teardown()
        self._poll.stop()
        # Order: vector first, then silence. If silence happened first,
        # the IRQ could fire between the volume-clear and gate-clears,
        # rewriting both. Flush after the vector write so it has actually
        # landed before silence_sid issues its writes; flush after silence
        # so the SID is genuinely quiet before the next scene begins.
        try:
            self.api.restore_kernal_irq_vector()
            self.api.flush()
            self.api.silence_sid()
            # The player MC's `JMP *` spin survives teardown by design (see
            # api.SID_PLAYER_MC_TEMPLATE docstring), so BASIC's GOTO 20 loop
            # is no longer running and the kernal editor's cursor-blink path
            # is reachable. Without this suppression, a subsequent PETSCII
            # scene (BlankScene, etc.) visibly blinks one cell at the saved
            # cursor position. Verified live on U64 hardware 2026-05-26.
            self.api.suppress_cursor_blink()
            # Restore VIC bank 0 + the default $D018 so the next scene's
            # bank-0 display renders (a no-op when we never relocated; the
            # next scene's mode setup also writes $D018, but restore it for
            # symmetry). Mirrors modes.py teardown.
            self.api.write_memory(f"{CIA2.PORT_A:04X}",
                                  f"{CIA2.PORT_A_BANK_0:02X}")
            self.api.write_memory("d018", f"{D018_HIRES_BITMAP:02X}")
            self.api.flush()
        except Exception:
            log.exception("waveform: teardown silence/restore failed")

    def cycle_style(self, api: C64Backend) -> str | None:
        """SHIFT handler: advance to the next subtune in the SID.

        Single-subtune SIDs return None (nothing to cycle to). For
        multi-subtune SIDs the sequence is: cue the C64-side re-INIT
        stub (uploaded by `api.run_sid_player` at scene setup) to call
        INIT(new song) on the next kernal IRQ tick, rebuild the host
        emulator on the new song, reset the duration timer, re-point the
        display to a VIC bank free for the new subtune, and repaint. The
        player MC + audio stay put — no run_prg, no machine reset, no
        flicker — just one IRQ tick (≤16ms NTSC / ≤20ms PAL) of audible gap.

        Candidates are skipped (and the next tried, bounded at n-1
        attempts) when either: the SongLengths DB knows the length and it's
        below MIN_CYCLE_SUBTUNE_S (a game SFX — best-effort, no DB → no
        skip), OR the subtune's PLAY footprint leaves no free VIC bank for
        the display (e.g. a subtune whose live data covers every bank). If
        every candidate is rejected, the first is taken anyway so SHIFT
        always changes the song — the scope may not render but audio plays.

        Note: the C64-side player keeps the RAM location chosen at setup()
        from the START song's write footprint — cue_song_reinit doesn't
        re-upload it. A different subtune that uses that RAM as scratch
        could disturb the player; re-footprinting the *player* per cycle
        would mean a full run_sid_player round-trip and reintroduce the
        flicker this cue path exists to avoid. The display bank IS
        re-chosen per cycle (cheap — just VIC regs + a repaint). Acceptable
        best-effort for v1.
        """
        n = self.header.num_songs
        if n <= 1:
            return None

        # Silence the outgoing subtune FIRST, before the (CPU-bound) candidate
        # footprinting below. cycle_style runs on the main render thread, so
        # that footprinting blocks process_frame and the scope visibly freezes
        # the instant SHIFT is handled — silencing here cuts the audio in
        # lockstep with that freeze instead of letting the old tune sound
        # through the footprint + cue / hard-relaunch work (the lingering audio
        # the user hears otherwise). Order mirrors teardown: unhook our IRQ
        # first (else the next PLAY tick rewrites the SID), flush so the vector
        # lands, then zero $D418 + gates. cue_song_reinit / setup()'s
        # run_sid_player re-installs the vector and restarts PLAY for the new
        # subtune. The host-emu poll thread keeps ticking until _poll.stop()
        # below — harmless, since the blocked main thread paints nothing.
        try:
            self.api.restore_kernal_irq_vector()
            self.api.flush()
            self.api.silence_sid()
            self.api.flush()
        except Exception:
            log.exception("waveform: cycle pre-silence failed")

        payload_lo, payload_hi = _sid_payload_extent(self.sid_bytes)

        # Walk candidates from the next subtune, skipping ones that are too
        # short (SFX, when the DB knows) or un-renderable (no free VIC bank
        # for this subtune's PLAY footprint). Capture the chosen subtune's
        # display layout so we don't re-footprint it below. Bounded at n-1
        # attempts: if every candidate is rejected, land on the first so
        # SHIFT still changes the song (audio plays even if the scope can't).
        first_candidate = (self.song % n) + 1
        new_song = first_candidate
        chosen_duration: float | None = None
        chosen_layout: tuple[int, int, int, int] | None = None
        chosen_access_fp: bytearray | None = None
        skipped_short: list[tuple[int, float]] = []
        skipped_unrender: list[int] = []
        candidate = first_candidate
        for _ in range(n - 1):
            looked_up: float | None = None
            if (self._explicit_duration_s is None
                    and self.songlengths_db is not None):
                looked_up = self.songlengths_db.lookup(
                    self.sid_bytes, candidate)
                if (looked_up is not None
                        and looked_up < self.MIN_CYCLE_SUBTUNE_S):
                    skipped_short.append((candidate, looked_up))
                    candidate = (candidate % n) + 1
                    continue
            fp = ram_play_access_footprint(self.sid_bytes, song=candidate)
            if self._unified_layout is not None:
                # One bank was pinned for every subtune at setup() — never
                # relocate (a per-subtune _choose_display_layout could pick an
                # earlier-preference bank and reintroduce the live bank move
                # this pin exists to avoid). The union fits every subtune, so
                # no candidate is unrenderable here.
                layout = self._unified_layout
            else:
                try:
                    layout = _choose_display_layout(payload_lo, payload_hi, fp)
                except ValueError:
                    skipped_unrender.append(candidate)
                    candidate = (candidate % n) + 1
                    continue
            new_song = candidate
            chosen_duration = looked_up
            chosen_layout = layout
            chosen_access_fp = fp
            break
        # No `else`: if every candidate was rejected, new_song stays at
        # first_candidate with chosen_layout=None; we keep the current
        # display bank below (the new subtune may render imperfectly, but
        # the SHIFT still takes effect and audio plays).

        for sn, sl in skipped_short:
            log.info("waveform: cycle skipping song %d/%d (%.1fs < %.1fs "
                     "min)", sn, n, sl, self.MIN_CYCLE_SUBTUNE_S)
        for sn in skipped_unrender:
            log.info("waveform: cycle skipping song %d/%d (no free VIC bank "
                     "for its PLAY footprint)", sn, n)

        # Decide the new subtune's PLAY $01 bank (only for a chosen,
        # renderable candidate — the write footprint run is skipped for the
        # degenerate all-rejected fallback, where cue restores the heuristic
        # default). $36 when PLAY reads RAM the tune wrote under BASIC ROM.
        chosen_play_bank: int | None = None
        if chosen_access_fp is not None:
            write_fp = ram_write_footprint(self.sid_bytes, song=new_song)
            chosen_play_bank = _play_bank_for_footprints(
                write_fp, chosen_access_fp)

        # Stop the poll thread so it can't tick the host emulator while we
        # rebuild it. (The outgoing subtune was already silenced at the top of
        # cycle_style, before the footprinting above.)
        self._poll.stop()

        # A hard relaunch is needed only when ENTERING the under-BASIC-ROM
        # group ($36) from a song that didn't need it. HW-verified on Times of
        # Lore: cueing song 1 ($37, payload-based data) → song 2 ($36, reads
        # $B400) only beeps then goes silent — song 1's fresh INIT leaves the
        # machine in a state the in-place re-INIT into the $B400 mechanism
        # can't recover. But once inside the group, cueing $36→$36 (2→3) and
        # even back out $36→$37 (→song 1) both play fine. So relaunch only on
        # the $37→$36 crossing: clear low RAM (what a reset's RAMTAS zeroes),
        # then let setup() re-DMA a pristine payload + re-run the full player
        # startup + rebuild display/emu/poll. NOT a machine reset — just a
        # brief VIC-mode flash from run_prg. Everything else uses the fast,
        # flicker-free cue below.
        new_needs_basic_out = chosen_play_bank == CPU.PORT_BASIC_OUT
        if new_needs_basic_out and not self._current_needs_basic_out:
            self.song = new_song
            try:
                self.api.write_memory_file(
                    f"{_LOW_RAM_CLEAR_LO:04X}",
                    bytes(_LOW_RAM_CLEAR_HI - _LOW_RAM_CLEAR_LO))
                self.api.flush()
            except Exception:
                log.exception("waveform: cycle low-RAM clear failed")
            # _prepared keeps self.song (no pool re-pick). setup() re-chooses
            # the display bank + play_bank, re-DMAs payload, re-runs player,
            # rebuilds host emu + poll, and resets start_time (full duration).
            self._prepared = True
            self.setup()
            if self.is_done:
                return None
            if self._explicit_duration_s is not None:
                self.duration_s = float(self._explicit_duration_s)
            elif chosen_duration is not None:
                self.duration_s = float(chosen_duration)
            log.info("waveform: cycle hard-relaunched song %d/%d (reads RAM "
                     "under BASIC ROM — needs $36 + a clean INIT)",
                     self.song, n)
            return f"song {self.song}/{n}"

        # Fast flicker-free path for normal subtunes: cue the C64-side re-INIT
        # stub. Patches the song operand at $C401, patches the player MC's
        # playBank for the new subtune (cue_song_reinit doesn't rebuild the
        # MC, so a $36-needing subtune would otherwise keep the prior song's
        # $37 and play silent — though those go through the relaunch path
        # above), then atomically swaps $0314/$0315 → $C400; the very next
        # kernal IRQ tick runs the stub (JSR init / restore $D418 / restore
        # $0314 → $C31D / chain to $EA31). PLAY resumes on the new subtune.
        try:
            api.cue_song_reinit(new_song, play_bank=chosen_play_bank)
        except Exception:
            log.exception("waveform: cycle_style cue_song_reinit failed "
                          "for song %d", new_song)
            self.is_done = True
            return None

        self.song = new_song
        self._current_needs_basic_out = new_needs_basic_out
        self._host_emu = SidHostEmu(self.sid_bytes, song=self.song)
        # Re-resolve duration. Explicit user value always wins. Otherwise
        # use the length the skip loop already looked up (so we don't
        # re-query the DB for the same song). On a DB miss or all-skipped
        # fall-through, keep the prior duration_s — per-song lookup miss
        # shouldn't truncate, and the all-skipped case is rare enough
        # that "use whatever we had" is the least-surprising fallback.
        if self._explicit_duration_s is not None:
            self.duration_s = float(self._explicit_duration_s)
        elif chosen_duration is not None:
            self.duration_s = float(chosen_duration)
            log.info("waveform: songlengths matched %s #%d → %.1fs",
                     os.path.basename(self._sid_file), self.song,
                     self.duration_s)

        # Reset clocks so the new song gets its full duration and the
        # envelope dt doesn't accumulate the cycle-induced gap.
        now = time.time()
        self.start_time = now
        self._last_voice_wave = [-1, -1, -1]
        self._ever_sounded = False
        self._silence_since = None

        # Zero per-voice persistent state so a scroll trail or echo
        # history from the prior subtune doesn't ghost-merge into the new
        # one. Only the in-memory buffers — the U64 bitmap will catch up
        # on the next _render_hires() call.
        if self._strips is not None:
            for s in self._strips:
                if s is not None:
                    s.fill(False)
        if self._echo_history is not None:
            for h in self._echo_history:
                h.clear()
        if self._last_y is not None:
            for i in range(len(self._last_y)):
                self._last_y[i] = None

        # Re-anchor the host-emu clock: the re-INIT stub runs on the next
        # kernal IRQ (~1 frame) so the new subtune's PLAY tick 0 lines up
        # with now. Reset the tick counter so the poll thread's wall-clock
        # catch-up starts fresh for this subtune. See _poll_regs.
        self._sid_start_time = now
        self._ticks_done = 0

        # Re-resolve the PLAY rate (the new subtune may be vsync vs the old
        # one's CIA multispeed, or a different multispeed rate) and rebuild
        # the poll thread, then start it. _resolve_poll_rate builds a fresh
        # PollThread; stop+start is the supported restart pattern.
        self._resolve_poll_rate()
        self._poll.start()

        # Re-point the display if the new subtune needs a different VIC bank
        # than the current one (e.g. Times of Lore: song 1 → bank 2, songs
        # 2-11 → bank 1). cue_song_reinit doesn't touch VIC, so we move
        # $DD00/$D018, clear the new bank, and repaint via _apply_display_bank.
        # When the bank is unchanged (or no renderable candidate was found),
        # just repaint the title row so the song number updates — the delta
        # cache is still valid (bitmap + screen RAM weren't disturbed).
        if chosen_layout is not None and chosen_layout != (
                self._screen_base, self._bitmap_base, self._dd00, self._d018):
            (self._screen_base, self._bitmap_base, self._dd00,
             self._d018) = chosen_layout
            log.info("waveform: cycle relocated display to screen=$%04X "
                     "bitmap=$%04X ($DD00=$%02X $D018=$%02X) for song %d",
                     self._screen_base, self._bitmap_base, self._dd00,
                     self._d018, self.song)
            self.api.invalidate_cache()
            self._apply_display_bank()
        else:
            self._paint_title_row()

        return f"song {self.song}/{n}"

    def _detect_play_rate_hz(self) -> float:
        """Return the current subtune's effective PLAY rate in Hz.

        A user-pinned reg_poll_hz wins outright. Otherwise probe the rate on a
        THROWAWAY host emulator: many multispeed players program CIA #1 Timer A
        from their PLAY routine rather than INIT (Galway's Times of Lore writes
        it on the first PLAY — a ~2x multispeed), so SidHostEmu.play_rate_hz
        only reports the true rate once PLAY has run at least once. Reading the
        rate straight after a fresh INIT (which is exactly what cycle_style and
        a pool re-pick do) therefore mis-detects such tunes as plain vsync and
        ticks the scope at HALF the song's real rate — the voices then come in
        on screen progressively later than you hear them (worst for late
        entrants), the classic post-cycle "warped + delayed" scope.

        The probe runs on its own emu so the scene's real host emu keeps its
        exact song position (the wall-clock catch-up in _poll_regs owns that),
        and it's cheap: one INIT + a few PLAY passes (~a few ms). It stops the
        instant a multispeed rate appears; a genuine vsync tune writes no
        Timer A, runs all _RATE_PROBE_TICKS passes, and returns video_hz.

        On the fresh-launch path the scene's own host emu was already
        PLAY-pre-flighted by _load_sid_file, so it would self-detect — but
        probing unconditionally keeps every entry point (init, setup re-pick,
        SHIFT cycle) on one correct code path."""
        if self._user_reg_poll_hz is not None:
            return float(self._user_reg_poll_hz)
        probe = SidHostEmu(self.sid_bytes, song=self.song)
        rate = probe.play_rate_hz(self._video_hz, self.emulator.clock)
        for _ in range(self._RATE_PROBE_TICKS):
            if abs(rate - self._video_hz) > 0.5:
                break  # multispeed Timer A latch seen — rate is known
            probe.tick_play()
            rate = probe.play_rate_hz(self._video_hz, self.emulator.clock)
        return float(rate)

    def _resolve_poll_rate(self) -> None:
        """Set the host-emu PLAY tick rate to the current tune's real rate and
        (re)build the poll thread at that period.

        The real U64 calls PLAY once per video frame for a vsync tune, but at
        its programmed CIA #1 Timer A rate for a CIA-timed (multispeed) tune —
        which can be ~1.5x+ the frame rate. The host emulator must advance the
        song at the same rate or the scope drifts behind the audio (a late-
        entering voice appears on screen well after you hear it). The rate is
        derived from the host emulator (which has run INIT) via
        SidHostEmu.play_rate_hz, using the U64's system clock. An explicit
        user reg_poll_hz pins the rate and skips detection.

        Called from __init__ and again whenever the tune changes (setup()'s
        pool re-pick, cycle_style()'s subtune switch) since the new tune may
        have a different rate. run_first=True so the first wakeup catches the
        host emulator up immediately (covering the _setup_hires bitmap-clear
        gap). See _poll_regs for the wall-clock catch-up model."""
        rate = self._detect_play_rate_hz()
        if (self._user_reg_poll_hz is None
                and abs(rate - self._video_hz) > 0.5):
            log.info("waveform: %s is CIA-timed (multispeed) — host "
                     "emulator PLAY rate %.1f Hz (%.2fx video) to track "
                     "the real chip's audio",
                     os.path.basename(self._sid_file), rate,
                     rate / self._video_hz)
        self._reg_poll_hz = float(rate)
        # Per-PLAY-tick dt used to advance the ADSR envelope — must match the
        # tick rate so the envelope tracks wall-clock.
        self._poll_dt = 1.0 / max(rate, 5.0)
        self._poll = PollThread(self._poll_regs,
                                period=self._poll_dt,
                                name="sid-reg-poll",
                                run_first=True)

    def _poll_regs(self) -> None:
        """Advance the host emulator to the PLAY-tick count wall-clock says
        the real SID has reached, snapshotting its $D400-$D418 shadow and
        stepping the SIDEmulator register/ADSR state on EACH caught-up tick
        — all on this thread, which sees EVERY PLAY tick.

        The tick count is derived from elapsed wall-clock since the real SID
        started (self._sid_start_time), NOT from the number of poll wakeups.
        A naive "one PLAY per wakeup" loop runs slightly under the SID's PLAY
        cadence (each wakeup is wait(period) + the py65 PLAY cost) and starts
        late (this thread only spins up after _setup_hires clears the 8 KB
        bitmap, while the real SID has been playing since run_sid_player), so
        the scope drifts progressively behind the audio — most visible as a
        voice's trace staying flat for a beat after you hear it come in.
        Catching up to the wall-clock target each wakeup pins voice onsets +
        envelopes to what the audience hears.

        Register tracking + envelope advance run per caught-up tick (not just
        on the final snapshot) so a gate that pulses on then off within the
        catch-up batch isn't missed — otherwise a percussive/arp voice's
        envelope would stay stuck at 0 and its strip flat. The render thread
        only reads voice state and advances the display-phase accumulator
        (both under self._reg_lock)."""
        target = round((time.time() - self._sid_start_time)
                       * self._reg_poll_hz)
        n = target - self._ticks_done
        if n <= 0:
            # Ahead of (or exactly on) schedule — let wall-clock catch up.
            return
        n = min(n, self._MAX_CATCHUP_TICKS)
        for _ in range(n):
            self._host_emu.tick_play()
            snapshot = self._host_emu.regs()
            retrig = self._host_emu.retriggers()
            with self._reg_lock:
                self._reg_buf = snapshot
                self.emulator.update_registers(snapshot, retrigger=retrig)
                self.emulator.advance_envelopes(self._poll_dt)
        self._ticks_done += n

    # ---- VIC setup ---------------------------------------------------------

    def _apply_display_bank(self):
        """Point VIC at the current display bank ($DD00/$D018), clear its
        bitmap + screen matrix, and repaint the per-voice colors + title/meta
        rows. Shared by _setup_hires (initial) and cycle_style (per-subtune
        bank switch — a different subtune may free a different VIC bank).
        Does NOT (re)allocate the per-voice persistent buffers: those depend
        only on the render modes, not the bank. Callers must invalidate_cache
        first so a bank switch over the same addresses gets a clean baseline."""
        # Select the VIC bank ($DD00) + sub-bank screen/bitmap offset ($D018).
        # Bank 0/2 use $D018=$18 (bank-relative); bank 1 uses $58 (matrix at
        # $5400, above the payload). Writing bank 0 too is harmless and keeps
        # setup symmetric with the teardown restore.
        self.api.write_memory(f"{CIA2.PORT_A:04X}", f"{self._dd00:02X}")
        # Same VIC pokes as HiresDisplayMode.setup().
        self.api.write_memory("d011", D011_HIRES_ON)
        self.api.write_memory("d018", f"{self._d018:02X}")
        self.api.write_memory("d016", D016_STANDARD)
        self.api.write_regs("d020", 0x00, 0x00)
        # Clear bitmap + the FULL screen matrix once. The matrix clear zeroes
        # the spacer rows (21, 24) the per-voice/title/meta paints below don't
        # cover — in a relocated VIC bank those cells are uninitialized RAM
        # that would otherwise render as garbage (bank 0 got them blanked by
        # the BASIC clear screen). Per-voice/title/meta writes overwrite their
        # rows on top (distinct region_ids; invalidate_cache already ran).
        self.api.write_region(self._bitmap_base, bytes(SCREEN.BITMAP_BYTES),
                              region_id=RegionID.WAVE_BITMAP)
        self.api.write_region(self._screen_base, bytes(SCREEN.N_CELLS),
                              region_id=RegionID.WAVE_SCREEN_CLEAR)
        self._init_hires_colors()
        # Lazy-load the charset once + paint the static song-metadata rows.
        # Glyph loading is process-wide cached so a second WaveformScene
        # doesn't re-read the file.
        self._glyphs = _load_glyphs()
        self._paint_title_row()
        self._paint_metadata_row()

    def _setup_hires(self):
        self._apply_display_bank()
        # Per-voice persistent buffers — only allocate what each voice
        # actually needs (most scenes hit only one mode per voice).
        self._strips = []
        self._echo_history = []
        self._last_y = []
        for v_idx, (top, bot) in enumerate(BITMAP_STRIPS):
            mode = self._voice_render_modes[v_idx]
            self._strips.append(
                np.zeros((bot - top, BITMAP_W), dtype=bool)
                if mode == "scroll" else None
            )
            self._echo_history.append([])
            self._last_y.append(None)
        self._rows_col = np.arange(BITMAP_H, dtype=np.int32)[:, None]

    def _init_hires_colors(self):
        """Write per-voice FG/BG colors to the screen-RAM cells under
        each voice's bitmap strip."""
        for v_idx, (top, bot) in enumerate(BITMAP_STRIPS):
            color = self._initial_voice_color(v_idx)
            cell_row_top = top // CELL_PX
            cell_row_bot = bot // CELL_PX
            n_rows = cell_row_bot - cell_row_top
            byte = ((color & COLOR_NIBBLE_MASK) << 4)  # FG = color, BG = black
            block = bytes([byte] * (n_rows * SCREEN_W_CHARS))
            self.api.write_region(
                self._screen_base + cell_row_top * SCREEN_W_CHARS,
                block,
                region_id=RegionID.WAVE_SCREEN + v_idx,
            )

    # ---- per-voice color resolution ----------------------------------------

    def _initial_voice_color(self, v_idx: int) -> int:
        if self.color_mode == "per_voice":
            return C64_COLORS.get(self.voice_color_names[v_idx],
                                  C64_COLORS["white"])
        return C64_COLORS.get(self.waveform_color_names["off"],
                              C64_COLORS["dark gray"])

    def _voice_color_now(self, v_idx: int) -> int:
        if self.color_mode == "per_voice":
            return C64_COLORS.get(self.voice_color_names[v_idx],
                                  C64_COLORS["white"])
        v = self.emulator.voices[v_idx]
        wave = primary_waveform(v.control)
        name = {
            WAVE_TRIANGLE: "triangle",
            WAVE_SAWTOOTH: "sawtooth",
            WAVE_PULSE:    "pulse",
            WAVE_NOISE:    "noise",
            0:             "off",
        }[wave]
        return C64_COLORS.get(self.waveform_color_names[name],
                              C64_COLORS["white"])

    # ---- per-frame render --------------------------------------------------

    def process_frame(self, current_time: float) -> bool:
        if self.is_done:
            return False
        if (current_time - self.start_time) >= self.duration_s:
            return False

        # Register tracking + ADSR advancement happen on the poll thread now
        # (see _poll_regs). Here we only read the resulting voice state under
        # the lock to drive coloring + the end-of-tune silence check.
        with self._reg_lock:
            controls = [v.control for v in self.emulator.voices]
            env_levels = [v.envelope_level for v in self.emulator.voices]

        # End-of-tune detection: a short non-looping subtune that has gone
        # fully silent ends the scene early instead of holding a flat scope.
        if self._check_end_of_tune(current_time, env_levels):
            return False

        # Per-waveform color updates (only emit when the waveform select
        # actually changes — change-detection avoids the cost in normal frames).
        if self.color_mode == "per_waveform":
            for v_idx in range(3):
                wave_now = primary_waveform(controls[v_idx])
                if wave_now != self._last_voice_wave[v_idx]:
                    self._repaint_voice_color(v_idx)
                    self._last_voice_wave[v_idx] = wave_now

        self._render_hires()
        return True

    def _check_end_of_tune(self, current_time: float,
                           env_levels: list[float]) -> bool:
        """Return True when the tune has been silent long enough to end the
        scene. Arms only after the first audible envelope (so a slow-to-start
        tune isn't killed), tracks the start of the current all-silent
        window, and fires after END_SILENCE_S of continuous silence."""
        sounding = any(e >= self.ENV_SILENCE_EPS for e in env_levels)
        if sounding:
            self._ever_sounded = True
            self._silence_since = None
            return False
        if not self._ever_sounded:
            return False
        if self._silence_since is None:
            self._silence_since = current_time
            return False
        if current_time - self._silence_since >= self.END_SILENCE_S:
            log.info("waveform: %r silent %.1fs after sounding — ending scene",
                     self.name, self.END_SILENCE_S)
            return True
        return False

    def _repaint_voice_color(self, v_idx: int):
        """Re-write the screen-RAM FG-nibble cells under the given voice's
        bitmap strip with its current color."""
        color = self._voice_color_now(v_idx)
        top, bot = BITMAP_STRIPS[v_idx]
        cell_row_top = top // 8
        n_rows = (bot - top) // 8
        block = bytes([((color & 0x0F) << 4)] * (n_rows * SCREEN_W_CHARS))
        self.api.write_region(
            self._screen_base + cell_row_top * SCREEN_W_CHARS,
            block,
            region_id=RegionID.WAVE_SCREEN + v_idx,
        )

    # ---- hires text rows ---------------------------------------------------

    def _build_title_line(self) -> tuple[str, int]:
        """Return (40-char title line, cell column of song-number's first
        digit). Song number is always zero-padded to the width of
        num_songs (so "03/11" not "3/11") — keeps the SHIFT-update window
        a constant width and column regardless of which subtune is current."""
        name = (self.header.name.strip() or
                os.path.basename(self._sid_file)).upper()
        author = self.header.author.strip().upper() or "?"
        w = self._song_num_width
        # The space + "(SONG " prefix is included in the suffix so the
        # song-number column is derivable from len(left) + len(prefix).
        prefix = " (SONG "
        suffix_after_num = f"/{self.header.num_songs})"
        # Reserve right-side budget for the composer + the always-present
        # song-suffix. Whatever's left goes to the name.
        max_author = 18
        if len(author) > max_author:
            author = author[:max_author]
        fixed_right_w = len(prefix) + w + len(suffix_after_num) + len(author) + 1
        max_name = SCREEN_W_CHARS - fixed_right_w
        if len(name) > max_name:
            name = name[:max(0, max_name)]
        song_str = f"{self.song:0{w}d}"
        left = f"{name}{prefix}{song_str}{suffix_after_num}"
        line = _layout_lr(left, author)
        # song_num_col is where the first digit lands in the final line
        # (left starts at column 0, name + prefix precede the number).
        song_col = len(name) + len(prefix)
        return line, song_col

    def _build_metadata_line(self) -> str:
        copyright_str = (self.header.released.strip() or "?").upper()
        chip = self.header.sid_model or "?"
        clock = self.header.clock or "?"
        return _layout_lcr(copyright_str, chip, clock)

    def _paint_text_row(self, cell_row: int, text: str, fg: int,
                        bitmap_region_id: int, screen_region_id: int) -> None:
        """Render a 40-char line into one bitmap cell-row + matching FG
        color into screen RAM. Caller supplies the two region IDs so the
        delta cache absorbs unchanged columns on re-paint (the SHIFT-driven
        title repaint typically only changes ~2 digit cells, ~16 bytes)."""
        assert self._glyphs is not None
        assert len(text) == SCREEN_W_CHARS, (
            f"text row must be exactly {SCREEN_W_CHARS} chars, got {len(text)}")
        glyphs = self._glyphs
        bitmap_bytes = bytearray(SCREEN_W_CHARS * CELL_PX)
        for col, ch in enumerate(text):
            sc = _ascii_to_screen_code(ch)
            bitmap_bytes[col * CELL_PX:(col + 1) * CELL_PX] = (
                glyphs[sc * CELL_PX:(sc + 1) * CELL_PX])
        bitmap_addr = self._bitmap_base + cell_row * BITMAP_CELL_ROW_BYTES
        self.api.write_region(bitmap_addr, bytes(bitmap_bytes),
                              region_id=bitmap_region_id)
        fg_byte = (fg & COLOR_NIBBLE_MASK) << 4   # FG in high nibble, BG = 0
        screen_addr = self._screen_base + cell_row * SCREEN_W_CHARS
        self.api.write_region(screen_addr,
                              bytes([fg_byte] * SCREEN_W_CHARS),
                              region_id=screen_region_id)

    def _paint_title_row(self) -> None:
        line, song_col = self._build_title_line()
        self._song_num_col = song_col
        fg = C64_COLORS.get(TITLE_TEXT_COLOR, C64_COLORS["white"])
        self._paint_text_row(TITLE_ROW, line, fg,
                             RegionID.WAVE_TITLE_BITMAP,
                             RegionID.WAVE_TITLE_SCREEN)

    def _paint_metadata_row(self) -> None:
        line = self._build_metadata_line()
        fg = C64_COLORS.get(METADATA_TEXT_COLOR, C64_COLORS["light gray"])
        self._paint_text_row(META_ROW, line, fg,
                             RegionID.WAVE_META_BITMAP,
                             RegionID.WAVE_META_SCREEN)

    # ---- hires rendering ---------------------------------------------------

    def _voice_time_window_s(self, v_idx: int, n_cols: int) -> float:
        """Return the audio time spanned by `n_cols` columns for voice
        v_idx.

        Per-column time is consistent regardless of mode: in scroll mode,
        a batch of n_new columns covers (n_new / BITMAP_W) of the
        full-screen window. This is what keeps the trace shape stable
        across the scroll boundary — without it, scroll mode samples
        many full cycles into a few pixels and the trace looks random.

        wallclock: a full screen-width window = one display-frame of
        audio time.
        auto: a full screen-width window = auto_cycles * (1/freq_hz).
        Falls back to wallclock for silent voices (freq=0, wave=off, or
        envelope=0)."""
        if self.time_base == TIME_BASE_WALLCLOCK:
            full_window = self._frame_time_s
        else:
            v = self.emulator.voices[v_idx]
            wave = primary_waveform(v.control)
            if v.freq == 0 or wave == 0 or v.envelope_level <= 0.0:
                full_window = self._frame_time_s
            else:
                # SID freq (Hz) = freq_reg * clock / 2^24; period = 1/freq_hz.
                period_s = ACCUMULATOR_RANGE / (v.freq * self.emulator.clock)
                full_window = self.auto_cycles * period_s
        return full_window * n_cols / BITMAP_W

    # ---- per-voice render helpers -----------------------------------------

    def _compute_ys(self, v_idx: int, top: int, bot: int,
                    n_new: int) -> np.ndarray:
        """Sample n_new audio samples for voice v_idx at the per-voice
        time window and map to pixel-row y in absolute bitmap coords
        (top..bot-1)."""
        mid = (top + bot) // 2
        half_h = (bot - top) // 2 - 1
        # Hold the register lock only for the emulator reads + sample
        # synthesis (voice state is written by the poll thread; the display
        # accumulator is advanced here). Released before mask packing + DMA
        # so the poll thread is never blocked across the wire.
        with self._reg_lock:
            time_window_s = self._voice_time_window_s(v_idx, n_new)
            samples = self.emulator.voice_samples(v_idx, n_new, time_window_s)
        ys = (mid - samples * half_h).astype(np.int32)
        np.clip(ys, top, bot - 1, out=ys)
        return ys

    def _span_mask(self, ys: np.ndarray, top: int, bot: int,
                   prev_y: int | None) -> np.ndarray:
        """Build a (strip_h, len(ys)) bool mask, filling the vertical span
        between adjacent x's so a sharp jump doesn't leave a one-pixel
        gap. `prev_y` (absolute coord) is the y of the column immediately
        to the LEFT of column 0 — when provided, the mask connects the
        first new column to that prior y instead of degenerating to a
        single-pixel self-dot (used by scroll mode for continuity).
        Pass None on the first frame or in fast/echo modes."""
        assert self._rows_col is not None
        ys_prev = np.empty_like(ys)
        ys_prev[0] = ys[0] if prev_y is None else prev_y
        ys_prev[1:] = ys[:-1]
        lo = np.minimum(ys_prev, ys) - top
        hi = np.maximum(ys_prev, ys) - top
        strip_rows = self._rows_col[:bot - top]
        return (strip_rows >= lo[None, :]) & (strip_rows <= hi[None, :])

    def _write_bitmap_strip(self, v_idx: int, top: int, bot: int,
                            mask: np.ndarray) -> None:
        """Pack a (strip_h, BITMAP_W) bool mask into the C64 hires bitmap
        memory layout and DMA it to the strip's bitmap region."""
        cell_row_top = top // CELL_PX
        cell_row_bot = bot // CELL_PX
        n_cell_rows = cell_row_bot - cell_row_top
        packed = np.packbits(mask, axis=1)                  # (strip_h, 40)
        bitmap_strip = (packed.reshape(
            n_cell_rows, CELL_PX, SCREEN_W_CHARS)
                              .transpose(0, 2, 1)
                              .tobytes())
        self.api.write_region(
            self._bitmap_base + cell_row_top * BITMAP_CELL_ROW_BYTES,
            bitmap_strip,
            region_id=RegionID.WAVE_BITMAP + v_idx,
        )

    # ---- the three per-voice render paths ---------------------------------

    def _render_voice_fast(self, v_idx: int, top: int, bot: int) -> None:
        """Default redraw-from-scratch: sample → mask → pack → write.
        No persistent state. Output bytes are identical to the pre-knob
        bool-canvas implementation when all voices take this path."""
        ys = self._compute_ys(v_idx, top, bot, BITMAP_W)
        mask = self._span_mask(ys, top, bot, prev_y=None)
        self._write_bitmap_strip(v_idx, top, bot, mask)

    def _render_voice_scroll(self, v_idx: int, top: int, bot: int) -> None:
        """FIFO scroll: shift the persistent strip left by N cols, draw
        the new N cols on the right edge. The first new column connects
        to the previous frame's last column via _last_y so the trace
        doesn't fragment at the scroll boundary."""
        assert self._strips is not None and self._last_y is not None
        strip = self._strips[v_idx]
        assert strip is not None, "scroll voice missing its bool strip"
        scroll_n = self.scroll_columns[v_idx]
        strip[:, :-scroll_n] = strip[:, scroll_n:]
        strip[:, -scroll_n:] = False
        ys = self._compute_ys(v_idx, top, bot, scroll_n)
        mask = self._span_mask(ys, top, bot, prev_y=self._last_y[v_idx])
        strip[:, BITMAP_W - scroll_n:] = mask
        self._last_y[v_idx] = int(ys[-1])
        self._write_bitmap_strip(v_idx, top, bot, strip)

    def _render_voice_echo(self, v_idx: int, top: int, bot: int) -> None:
        """N-frame echo: keep the last echo_depth bool masks per voice
        and render them in progressively darker grays, with the current
        frame on top in the voice's regular color.

        Per-cell color picking: walk newest→oldest; the first frame whose
        trace has any lit pixel in a given 8×8 cell claims that cell's
        FG color. Unclaimed cells fall back to the voice's regular color
        with FG = BG so they read as background even if a stray pixel
        slips through (defensive — the bitmap is the source of truth)."""
        assert (self._strips is not None
                and self._echo_history is not None
                and self._last_y is not None)
        history = self._echo_history[v_idx]
        ys = self._compute_ys(v_idx, top, bot, BITMAP_W)
        current_mask = self._span_mask(ys, top, bot, prev_y=None)

        # Combined bitmap is the OR of all history + current — past
        # frames stay lit until they age out of the ring buffer.
        combined = current_mask.copy()
        for past in history:
            combined |= past

        # Per-cell color: walk newest→oldest, assigning each cell to the
        # color of the freshest trace whose mask has any pixel in it.
        # masks_newest_first[0] is the current frame in voice color;
        # masks_newest_first[1..] are past frames in increasingly old
        # grays. Length is up to echo_depth+1. Slice via plain `len`
        # rather than negative index — `_echo_colors[-0:]` would return
        # the full list on a first-frame warm-up instead of empty.
        masks_newest_first: list[np.ndarray] = [current_mask, *reversed(history)]
        n_hist = len(history)
        past_colors_newest_first = (
            list(reversed(self._echo_colors[-n_hist:])) if n_hist else []
        )
        colors_newest_first: list[int] = [
            self._voice_color_now(v_idx),
            *past_colors_newest_first,
        ]
        n_cell_rows = (bot - top) // CELL_PX
        cell_color = np.zeros((n_cell_rows, SCREEN_W_CHARS), dtype=np.uint8)
        claimed = np.zeros((n_cell_rows, SCREEN_W_CHARS), dtype=bool)
        for mask, color in zip(masks_newest_first, colors_newest_first,
                               strict=True):
            # Reshape to (cell_rows, CELL_PX, SCREEN_W_CHARS, CELL_PX) and
            # reduce over the pixel-within-cell axes to "any pixel lit?".
            cell_lit = (mask.reshape(n_cell_rows, CELL_PX,
                                     SCREEN_W_CHARS, CELL_PX)
                            .any(axis=(1, 3)))
            new_claims = cell_lit & ~claimed
            cell_color[new_claims] = color & COLOR_NIBBLE_MASK
            claimed |= new_claims

        # Push the (cell_rows × 40) color matrix to screen RAM as FG-nibble
        # bytes (high nibble = FG color, BG = 0/black).
        screen_bytes = (cell_color << 4).astype(np.uint8).tobytes()
        cell_row_top = top // CELL_PX
        self.api.write_region(
            self._screen_base + cell_row_top * SCREEN_W_CHARS,
            screen_bytes,
            region_id=RegionID.WAVE_SCREEN + v_idx,
        )

        self._write_bitmap_strip(v_idx, top, bot, combined)

        # Rotate the ring: append current, drop oldest if at capacity.
        history.append(current_mask)
        if len(history) > self._echo_depth:
            history.pop(0)

    def _render_hires(self):
        """Render each voice strip via its configured mode (fast / scroll
        / echo). Per-voice modes are computed once in __init__ based on
        scroll_columns + persistence so the per-frame branch is a cheap
        dispatch.

        Fast: no state, redraws from scratch. Identical to the pre-knob
        implementation when persistence=off and scroll_columns=0.
        Scroll: persistent bool strip, shift left + draw new N cols
                connecting to previous frame's last y.
        Echo: ring of past bool masks, OR for bitmap, per-cell screen-RAM
              colors picked newest-first. _repaint_voice_color stays a no-op
              in this mode because the per-cell writes overwrite it every
              frame."""
        assert self._rows_col is not None
        for v_idx, (top, bot) in enumerate(BITMAP_STRIPS):
            mode = self._voice_render_modes[v_idx]
            if mode == "scroll":
                self._render_voice_scroll(v_idx, top, bot)
            elif mode == "echo":
                self._render_voice_echo(v_idx, top, bot)
            else:
                self._render_voice_fast(v_idx, top, bot)
