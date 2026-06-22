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
from typing import TYPE_CHECKING

import numpy as np

from ._pollthread import PollThread
from .audio import RING_BUFFER_ADDR, RING_BUFFER_END, AudioStreamer
from .backend import C64Backend
from .c64 import CIA2, CPU, SCREEN, VIC_BANK_0, VIC_BANK_2, RegionID
from .palette import C64_COLORS
from .scenes import Scene

# SidHeader / parse_sid_header / _sid_payload_extent / _overlaps /
# _play_bank_for_footprints moved to sid_host_emu.py (so SidFileAudioSource can
# reuse them without importing the oscilloscope renderer). Imported here for
# WaveformScene's own use AND re-exported for back-compat: config and tests
# historically do `from .waveform import parse_sid_header / _play_bank_for_footprints`.
from .sid_host_emu import (
    SidHostEmu,
    _overlaps,
    _play_bank_for_footprints,
    _sid_payload_extent,
    parse_sid_header,
    ram_play_access_footprint,
    ram_write_footprint,
)
from .sidemu import SIDEmulator, primary_waveform

# The 3-voice oscilloscope renderer (layout consts, glyph + text-layout
# helpers, VIC hires bring-up, and the per-voice render paths) lives in
# voice_scope.py so MidiScene can share it. Several names are re-exported
# (imported-unused here) because config._validate_waveform + tests/test_waveform
# import them from this module historically.
from .voice_scope import (
    _PERSISTENCE_RANDOM_CHOICES,  # noqa: F401  (re-exported)
    BITMAP_STRIPS,  # noqa: F401  (re-exported)
    BITMAP_W,  # noqa: F401  (re-exported)
    D018_HIRES_BITMAP,
    META_ROW,
    METADATA_TEXT_COLOR,
    PERSISTENCE_NAMES,  # noqa: F401  (re-exported)
    RANDOM_PERSISTENCE,
    SCREEN_W_CHARS,
    TIME_BASE_NAMES,  # noqa: F401  (re-exported)
    TIME_BASE_WALLCLOCK,
    TITLE_ROW,
    TITLE_TEXT_COLOR,
    VoiceScopeRenderer,
    _layout_lcr,
    _layout_lr,
)

if TYPE_CHECKING:
    from .songlengths import LengthsDB

log = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Display-bank selection
#
# The SidHeader / parse_sid_header / _sid_payload_extent / _overlaps /
# _play_bank_for_footprints helpers moved to sid_host_emu.py (so the
# composable SidFileAudioSource can reuse them without importing the
# oscilloscope renderer); they're re-imported above for back-compat. The
# bank-choice helpers below stay here — they're specific to WaveformScene's
# relocatable display.
# ---------------------------------------------------------------------------


def _bank_payload_feasible(
    payload_lo: int, payload_hi: int, screen_base: int, bitmap_base: int
) -> bool:
    """True when the SID payload alone clears a bank's screen + bitmap
    regions (a cheap, footprint-free pre-check used by _load_sid_file)."""
    return not (
        _overlaps(payload_lo, payload_hi, screen_base, SCREEN.N_CELLS)
        or _overlaps(payload_lo, payload_hi, bitmap_base, SCREEN.BITMAP_BYTES)
    )


def _any_display_bank_fits_payload(payload_lo: int, payload_hi: int) -> bool:
    """True when at least one candidate VIC bank's display regions clear the
    payload. Used by _load_sid_file to refuse only the truly hopeless tunes
    (payload covers every bank's display) — the footprint-aware bank choice
    is deferred to _choose_display_layout at setup()."""
    return any(
        _bank_payload_feasible(payload_lo, payload_hi, s, b) for s, b, _, _ in _DISPLAY_BANKS
    )


def _choose_display_layout(
    payload_lo: int, payload_hi: int, footprint: bytes | bytearray
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
            or _overlaps(payload_lo, payload_hi, bitmap_base, SCREEN.BITMAP_BYTES)
            or any(footprint[screen_base:screen_hi])
            or any(footprint[bitmap_base:bitmap_hi])
        )
        if not blocked:
            return screen_base, bitmap_base, dd00, d018
    raise ValueError(
        f"waveform: SID payload ${payload_lo:04X}-${payload_hi:04X} plus its "
        f"runtime footprint leave no free VIC bank for the display (tried "
        f"bank 0 $0400/$2000, bank 2 $8400/$A000, bank 1 $5400/$6000)"
    )


def _choose_unified_display_layout(
    sid_bytes: bytes, payload_lo: int, payload_hi: int, num_songs: int
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
        return _choose_display_layout(payload_lo, payload_hi, union.tobytes())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Waveform scene
# ---------------------------------------------------------------------------


class WaveformScene(VoiceScopeRenderer, Scene):
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

    def __init__(
        self,
        api: C64Backend,
        audio: AudioStreamer | None,
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
        songlengths_db: LengthsDB | None = None,
    ):
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
        # Knob validation (color_mode/time_base/auto_cycles/persistence/
        # scroll_columns) now lives in VoiceScopeRenderer._init_scope_knobs,
        # called below after the file load + super().__init__.

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

        name = self.header.name.strip() or os.path.splitext(os.path.basename(self._sid_file))[0]
        super().__init__(api, audio, None, f"SID: {name} #{self.song}")
        # True when prepare_next() has already picked+loaded this
        # iteration's tune (and refreshed self.name); setup() then skips
        # the re-pick to avoid loading the SID twice. See
        # VideoScene._prepared for the full rationale.
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
        self.system = system
        self.duration_s = float(self._resolve_duration_for_current_sid())
        # Visualization knobs + per-voice render modes + persistent buffers.
        # One displayed row of waveform covers exactly one display frame of
        # audio time (frame_time_s) — locks the trace's visible phase to
        # wall-clock. Validates color_mode/time_base/auto_cycles/persistence/
        # scroll_columns and raises ValueError on any bad value.
        self._init_scope_knobs(
            color_mode=color_mode,
            voice_colors=voice_colors,
            waveform_colors=waveform_colors,
            time_base=time_base,
            auto_cycles=auto_cycles,
            persistence=persistence,
            scroll_columns=scroll_columns,
            frame_time_s=1.0 / self.target_fps,
        )

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

        # Cell column where the song number's first digit lands in the title
        # row — recomputed each time the title row is rebuilt, since title-
        # truncation rules can move the number's position. (self._glyphs is
        # initialized by _init_scope_knobs and loaded by _apply_vic_hires_bank.)
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
        if self._song_arg < 0 or (self._song_arg > header.num_songs and self._song_arg != 0):
            raise ValueError(
                f"waveform: song {self._song_arg} out of range "
                f"0..{header.num_songs} for {os.path.basename(path)}"
            )
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
                f"clobbering the tune. Pick a smaller SID."
            )

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
                f"hang the C64-side player (silent + unresponsive). Refused."
            )
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
        for path in pool[: self._MAX_PICK_ATTEMPTS]:
            try:
                self._load_sid_file(path)
            except ValueError as e:
                log.warning("waveform: skipping %s: %s", os.path.basename(path), e)
                last_error = e
                continue
            if len(self._candidates) > 1:
                log.info(
                    "waveform: picked %s from %d candidates",
                    os.path.basename(path),
                    len(self._candidates),
                )
            return
        # Every attempted candidate failed validation. Re-raise the last
        # error so the caller (validate_scene_cfg or setup) sees a real
        # message instead of a generic "no SID loaded".
        raise ValueError(
            f"waveform: file spec {self.file_spec!r} resolved to "
            f"{len(self._candidates)} candidate(s) but none could be "
            f"loaded; last error: {last_error}"
        )

    def _resolve_duration_for_current_sid(self) -> float:
        """Compute the playback duration for self.sid_bytes + self.song.
        Order: explicit user duration_s > songlengths DB > 180s default."""
        if self._explicit_duration_s is not None:
            return float(self._explicit_duration_s)
        if self.songlengths_db is not None:
            looked_up = self.songlengths_db.lookup(self.sid_bytes, self.song)
            if looked_up is not None:
                log.info(
                    "waveform: songlengths matched %s #%d → %.1fs",
                    os.path.basename(self._sid_file),
                    self.song,
                    looked_up,
                )
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
        name = self.header.name.strip() or os.path.splitext(os.path.basename(self._sid_file))[0]
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
        overlay_names = [getattr(ov, "name", type(ov).__name__) for ov in self.overlays]
        ov_str = ", ".join(overlay_names) if overlay_names else "no overlays"
        # Surface the resolved knobs so a `persistence = "random"` config
        # leaves a trace of which preset actually got picked.
        persist_label = self.persistence
        if self.persistence_config == RANDOM_PERSISTENCE:
            persist_label = f"{self.persistence} (random)"
        log.info(
            "scene %r: SID color_mode=%s duration=%.1fs @ %.0ffps "
            "time_base=%s persistence=%s scroll=%s [%s]",
            self.name,
            self.color_mode,
            self.duration_s,
            self.target_fps,
            self.time_base,
            persist_label,
            self.scroll_columns,
            ov_str,
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
        display_footprint = ram_play_access_footprint(self.sid_bytes, song=self.song)
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
                    self.sid_bytes, payload_lo, payload_hi, n_songs
                )
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
                (self._screen_base, self._bitmap_base, self._dd00, self._d018) = (
                    self._unified_layout
                )
                log.info(
                    "waveform: display bank pinned for all %d subtunes "
                    "(screen=$%04X bitmap=$%04X $DD00=$%02X $D018=$%02X) "
                    "— SHIFT-cycle won't relocate",
                    n_songs,
                    self._screen_base,
                    self._bitmap_base,
                    self._dd00,
                    self._d018,
                )
            else:
                (self._screen_base, self._bitmap_base, self._dd00, self._d018) = (
                    _choose_display_layout(payload_lo, payload_hi, display_footprint)
                )
        except ValueError as e:
            log.error("waveform: %s — scene aborting.", e)
            self.is_done = True
            return
        if self._dd00 != CIA2.PORT_A_BANK_0:
            log.info(
                "waveform: display relocated to VIC bank @ "
                "screen=$%04X bitmap=$%04X ($DD00=$%02X $D018=$%02X) — "
                "payload $%04X-$%04X / PLAY footprint occupies bank 0's "
                "display",
                self._screen_base,
                self._bitmap_base,
                self._dd00,
                self._d018,
                payload_lo,
                payload_hi,
            )

        # Build the player "avoid" bitmap: the tune's footprint + this
        # scene's CHOSEN display regions + the audio ring. Slice-assignment
        # keeps it cheap.
        avoid = bytearray(footprint)
        for lo, hi in (
            (self._screen_base, self._screen_base + SCREEN.N_CELLS),
            (self._bitmap_base, self._bitmap_base + SCREEN.BITMAP_BYTES),
            (RING_BUFFER_ADDR, RING_BUFFER_END),
        ):
            avoid[lo:hi] = b"\x01" * (hi - lo)

        # Decide the PLAY $01 bank: $36 (BASIC out) when this subtune reads
        # live song data from RAM under BASIC ROM (e.g. ToL 2-11 at $B400),
        # else None (heuristic). See _play_bank_for_footprints.
        play_bank = _play_bank_for_footprints(footprint, display_footprint)
        self._current_needs_basic_out = play_bank == CPU.PORT_BASIC_OUT

        # Upload the SID payload + tiny player MC, DEFERRING the audible start
        # so the oscilloscope is on screen before the first note (the whole
        # point of this scene — show the waveforms *during* playback, not just
        # play a tune). The real 6510 then drives INIT + PLAY on a CIA #1 IRQ
        # that chains to kernal $EA31, so display + keyboard scan stay under our
        # control. See api.run_sid_player for the layout.
        #
        # Backend ordering differs and both are honored by defer_audio /
        # begin_sid_audio: the TeensyROM loads the player silent and only starts
        # it (a $0314 vector-swap) at begin_sid_audio, so the scope is up first;
        # the Ultimate's run_prg is a synchronous reset that re-inits VIC to text
        # mode, so it starts audio here and we (re)assert the bitmap right after
        # (begin_sid_audio is a no-op there). Either way the scope is painted
        # before — or within a frame of — the audio.
        try:
            self.api.run_sid_player(
                self.sid_bytes,
                song=self.song,
                avoid=avoid,
                play_bank=play_bank,
                defer_audio=True,
            )
        except Exception as e:
            log.error(
                "waveform: SID playback failed to start (%s) — "
                "scene aborting. PSID-only; RSIDs, SIDs that load "
                "below $0820, and SIDs under KERNAL ROM "
                "($E000-$FFFF) are rejected.",
                e,
            )
            self.is_done = True
            return

        # Configure VIC for hires bitmap mode FIRST, while the SID is still
        # silent on a deferring backend. Paints over the full screen so no scene
        # state from before matters.
        self.api.invalidate_cache()
        self._setup_hires()

        # Now release audio (no-op if it already started synchronously). Anchor
        # the host-emu clock to when the real SID actually started — the backend
        # records it (TR: this instant; U64: during run_sid_player, before
        # _setup_hires). The poll thread derives its PLAY-tick count from
        # wall-clock elapsed since this anchor, so it stays locked to the audio
        # rather than drifting. See _poll_regs.
        self.api.begin_sid_audio()
        self._sid_start_time = self.api.sid_audio_start_time() or time.time()
        self._ticks_done = 0

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
            self.api.write_memory(f"{CIA2.PORT_A:04X}", f"{CIA2.PORT_A_BANK_0:02X}")
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
            if self._explicit_duration_s is None and self.songlengths_db is not None:
                looked_up = self.songlengths_db.lookup(self.sid_bytes, candidate)
                if looked_up is not None and looked_up < self.MIN_CYCLE_SUBTUNE_S:
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
            log.info(
                "waveform: cycle skipping song %d/%d (%.1fs < %.1fs min)",
                sn,
                n,
                sl,
                self.MIN_CYCLE_SUBTUNE_S,
            )
        for sn in skipped_unrender:
            log.info(
                "waveform: cycle skipping song %d/%d (no free VIC bank for its PLAY footprint)",
                sn,
                n,
            )

        # Decide the new subtune's PLAY $01 bank (only for a chosen,
        # renderable candidate — the write footprint run is skipped for the
        # degenerate all-rejected fallback, where cue restores the heuristic
        # default). $36 when PLAY reads RAM the tune wrote under BASIC ROM.
        chosen_play_bank: int | None = None
        if chosen_access_fp is not None:
            write_fp = ram_write_footprint(self.sid_bytes, song=new_song)
            chosen_play_bank = _play_bank_for_footprints(write_fp, chosen_access_fp)

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
                    f"{_LOW_RAM_CLEAR_LO:04X}", bytes(_LOW_RAM_CLEAR_HI - _LOW_RAM_CLEAR_LO)
                )
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
            log.info(
                "waveform: cycle hard-relaunched song %d/%d (reads RAM "
                "under BASIC ROM — needs $36 + a clean INIT)",
                self.song,
                n,
            )
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
            log.exception("waveform: cycle_style cue_song_reinit failed for song %d", new_song)
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
            log.info(
                "waveform: songlengths matched %s #%d → %.1fs",
                os.path.basename(self._sid_file),
                self.song,
                self.duration_s,
            )

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
            self._screen_base,
            self._bitmap_base,
            self._dd00,
            self._d018,
        ):
            (self._screen_base, self._bitmap_base, self._dd00, self._d018) = chosen_layout
            log.info(
                "waveform: cycle relocated display to screen=$%04X "
                "bitmap=$%04X ($DD00=$%02X $D018=$%02X) for song %d",
                self._screen_base,
                self._bitmap_base,
                self._dd00,
                self._d018,
                self.song,
            )
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
        if self._user_reg_poll_hz is None and abs(rate - self._video_hz) > 0.5:
            log.info(
                "waveform: %s is CIA-timed (multispeed) — host "
                "emulator PLAY rate %.1f Hz (%.2fx video) to track "
                "the real chip's audio",
                os.path.basename(self._sid_file),
                rate,
                rate / self._video_hz,
            )
        self._reg_poll_hz = float(rate)
        # Per-PLAY-tick dt used to advance the ADSR envelope — must match the
        # tick rate so the envelope tracks wall-clock.
        self._poll_dt = 1.0 / max(rate, 5.0)
        self._poll = PollThread(
            self._poll_regs, period=self._poll_dt, name="sid-reg-poll", run_first=True
        )

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
        target = round((time.time() - self._sid_start_time) * self._reg_poll_hz)
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
        bitmap + screen matrix, repaint the per-voice colors + load the
        charset (all in VoiceScopeRenderer._apply_vic_hires_bank), then paint
        the song title/metadata rows (WaveformScene-specific content). Shared
        by _setup_hires (initial) and cycle_style (per-subtune bank switch — a
        different subtune may free a different VIC bank). Does NOT (re)allocate
        the per-voice persistent buffers: those depend only on the render
        modes, not the bank. Callers must invalidate_cache first so a bank
        switch over the same addresses gets a clean baseline."""
        self._apply_vic_hires_bank()
        self._paint_title_row()
        self._paint_metadata_row()

    def _setup_hires(self):
        self._apply_display_bank()
        self._alloc_scope_buffers()

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

    def _check_end_of_tune(self, current_time: float, env_levels: list[float]) -> bool:
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
            log.info(
                "waveform: %r silent %.1fs after sounding — ending scene",
                self.name,
                self.END_SILENCE_S,
            )
            return True
        return False

    # ---- hires text rows ---------------------------------------------------

    def _build_title_line(self) -> tuple[str, int]:
        """Return (40-char title line, cell column of song-number's first
        digit). Song number is always zero-padded to the width of
        num_songs (so "03/11" not "3/11") — keeps the SHIFT-update window
        a constant width and column regardless of which subtune is current."""
        name = (self.header.name.strip() or os.path.basename(self._sid_file)).upper()
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
            name = name[: max(0, max_name)]
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

    def _paint_title_row(self) -> None:
        line, song_col = self._build_title_line()
        self._song_num_col = song_col
        fg = C64_COLORS.get(TITLE_TEXT_COLOR, C64_COLORS["white"])
        self._paint_text_row(
            TITLE_ROW, line, fg, RegionID.WAVE_TITLE_BITMAP, RegionID.WAVE_TITLE_SCREEN
        )

    def _paint_metadata_row(self) -> None:
        line = self._build_metadata_line()
        fg = C64_COLORS.get(METADATA_TEXT_COLOR, C64_COLORS["light gray"])
        self._paint_text_row(
            META_ROW, line, fg, RegionID.WAVE_META_BITMAP, RegionID.WAVE_META_SCREEN
        )
