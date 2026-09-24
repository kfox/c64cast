"""Shared 3-voice SID oscilloscope renderer (hires bitmap).

A full-screen 320×200 hires oscilloscope of the three SID voices, shared by
:class:`~c64cast.sid.waveform.WaveformScene` (SID-file playback),
:class:`~c64cast.sid.midi_scene.MidiScene` (live MIDI) and
:class:`~c64cast.sid.asid_scene.AsidScene` (ASID stream).

The renderer is **SID-source-agnostic**: it reads per-voice state from a
:class:`~c64cast.sid.sidemu.SIDEmulator` the host scene owns and keeps current
however it likes, and draws three vertically-stacked voice strips plus two bottom
text rows.

``VoiceScopeRenderer`` is a **mixin**: methods reference ``self.<attr>`` directly,
so a host scene must provide these attributes before any render call (the
**attribute contract**):

  * ``self.api``            — C64 backend (write_memory / write_regs / write_region)
  * ``self.emulator``       — SIDEmulator with three voices
  * ``self._reg_lock``      — threading.Lock guarding emulator reads/writes
  * ``self._screen_base``   — screen-matrix base address (e.g. $0400)
  * ``self._bitmap_base``   — bitmap base address (e.g. $2000)
  * ``self._dd00``          — CIA2 port-A value selecting the VIC bank
  * ``self._d018``          — $D018 value (matrix + bitmap sub-bank offsets)
  * ``self._emulators``     — one SIDEmulator per chip window; **required** for
    any host with more than one window (``_scope_emulators()`` falls back to
    ``[self.emulator]`` without it, which renders every window past the first
    blank). Host-supplied — see ``AsidScene.__init__`` /
    ``WaveformScene._rebuild_scope_for_sids``.
  * ``self._glyphs``        — charset bytes (set by ``_apply_vic_hires_bank``)
  * ``self.color_mode``     — "per_voice" | "per_waveform"
  * ``self.voice_color_names`` / ``self.waveform_color_names``
  * the knob attributes set by ``_init_scope_knobs`` (``time_base``,
    ``auto_cycles``, ``scroll_columns``, ``persistence``, ``_echo_*``,
    ``_voice_render_modes``, ``_fast_path``, ``_frame_time_s``) and the per-render
    buffers set by ``_alloc_scope_buffers`` (``_strips``/``_echo_history``/
    ``_last_y``/``_rows_col``).

Bring-up order in a host's ``setup()``: ``_init_scope_knobs(...)`` (usually in
``__init__``) → ``api.invalidate_cache()`` → ``_apply_vic_hires_bank()`` → paint
the two text rows (host-specific content via ``_paint_text_row``) →
``_alloc_scope_buffers()`` → render each frame via ``_render_hires()``.

See docs/architecture/sid.md#voice_scopepy--shared-3-voice-oscilloscope-renderer.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np

from c64cast.hw.c64 import CIA2, SCREEN, VIC, RegionID
from c64cast.scenes.bitmap_text import ascii_to_screen_code as _ascii_to_screen_code
from c64cast.scenes.bitmap_text import load_glyphs as _load_glyphs
from c64cast.video.modes import engage_bitmap_mode
from c64cast.video.palette import C64_COLORS, resolve_color

from .sidemu import (
    ACCUMULATOR_RANGE,
    WAVE_NOISE,
    WAVE_PULSE,
    WAVE_SAWTOOTH,
    WAVE_TRIANGLE,
    primary_waveform,
)

if TYPE_CHECKING:
    import threading

    from c64cast.hw.backend import C64Backend

    from .sidemu import SIDEmulator

log = logging.getLogger(__name__)

SCREEN_W_CHARS = SCREEN.W_CHARS
BITMAP_W = SCREEN.BITMAP_W
BITMAP_H = SCREEN.BITMAP_H

# Bitmap layout: each 8-row cell row of the screen maps to a contiguous
# 320-byte slice of the bitmap, organized as 40 cells × 8 bytes.
CELL_PX = 8  # one screen cell = 8 px square
BITMAP_CELL_ROW_BYTES = BITMAP_W  # 320 bytes per cell row

# Three voice strips in hires mode, 7 cell rows each = 56 pixel rows.
# The bottom of the screen carries two text rows (metadata) — see TITLE_ROW /
# META_ROW — so each voice strip is one cell shorter than the
# spare-cell-row-only layout we'd otherwise use.
#
# Cell-row layout (25 rows total):
#   0-6   voice 1   (56 px)
#   7-13  voice 2   (56 px)
#   14-20 voice 3   (56 px)
#   21    spacer (1 char row gap above the text)
#   22    TITLE_ROW
#   23    META_ROW
#   24    spacer (1 char row at the very bottom)
BITMAP_STRIPS = [
    (0, 56),  # voice 1: cell rows 0-6
    (56, 112),  # voice 2: cell rows 7-13
    (112, 168),  # voice 3: cell rows 14-20
]
TITLE_ROW = 22
META_ROW = 23

# VIC register values _apply_vic_hires_bank passes to modes.engage_bitmap_mode,
# as the hex strings the write_memory API takes. $D018 is not one of them: it
# comes from the host scene's self._d018, which is c64.D018_HIRES_PAGE_A for
# every scene and bank except waveform.py's bank-1 fallback, whose matrix
# offset has to clear the SID payload (waveform.D018_BANK1).
D011_HIRES_ON = "3b"  # bitmap mode + display enable, raster MSB clear
D016_STANDARD = "08"  # 40-col, no multicolor

COLOR_NIBBLE_MASK = 0x0F

DEFAULT_VOICE_COLORS = ["cyan", "yellow", "light green"]
DEFAULT_WAVEFORM_COLORS = {
    "triangle": "light green",
    "sawtooth": "light red",
    "pulse": "cyan",
    "noise": "yellow",
    "off": "dark gray",
}

# Text-row colors: readable against black, and outside the default voice +
# waveform palettes so a static line of text isn't mistaken for a trace.
TITLE_TEXT_COLOR = "white"
METADATA_TEXT_COLOR = "light gray"

# Idle voice strips are drawn in this color, so a released voice's flat trace
# reads as "off"; a sounding voice repaints in its own color. Owned here beside
# the rest of the strip palette, because MidiScene and AsidScene both paint it.
IDLE_VOICE_COLOR = "gray"

# Per-voice render modes. Named because they are written from two places and
# dispatched from a third, and a typo degrades to the fast path in silence.
RENDER_MODE_FAST = "fast"
RENDER_MODE_SCROLL = "scroll"
RENDER_MODE_ECHO = "echo"

TIME_BASE_WALLCLOCK = "wallclock"
TIME_BASE_AUTO = "auto"
TIME_BASE_NAMES = (TIME_BASE_WALLCLOCK, TIME_BASE_AUTO)

# Persistence/echo presets: name → palette colors for past frames, oldest
# first, drawn via per-cell screen-RAM writes with the current frame in the
# voice's regular color. The C64 palette has only 3 distinct grays, so the
# longest preset pads with an invisible "black" slot. A 1bpp bitmap cannot fade
# a pixel, so an echo is a hard render at a dimmer gray, not a decay.
PERSISTENCE_ECHOES = {
    "off": (),
    "short": ("gray", "light gray"),  # 2 visible echoes
    "medium": ("dark gray", "gray", "light gray"),  # 3 visible echoes
    "long": ("black", "dark gray", "gray", "light gray"),  # 4 slots (oldest invisible)
}
RANDOM_PERSISTENCE = "random"
PERSISTENCE_NAMES = (*PERSISTENCE_ECHOES.keys(), RANDOM_PERSISTENCE)
# "off" is excluded: the reward of asking for random is the echo trail itself.
_PERSISTENCE_RANDOM_CHOICES = ("short", "medium", "long")

# Screen code of the C64 left-arrow (←) glyph in the uppercase charset (ASCII
# "_" / PETSCII $5F maps here). Horizontally mirroring it yields a right-arrow
# (→) — a glyph the C64 charset has no native cell for; see _mirror_glyph_h.
LEFT_ARROW_SCREEN_CODE = 0x1F


def restore_char_mode_display(api: C64Backend) -> None:
    """Put VIC bank 0 and the char-mode $D018 back for the next scene.

    Every scope scene renders from a bitmap layout. The next scene's mode engage
    owns $D011 but not the matrix pointer, so a $D018 left on the bitmap layout
    makes a char-mode scene read its matrix from the wrong offset."""
    api.write_memory(f"{CIA2.PORT_A:04X}", f"{CIA2.PORT_A_BANK_0:02X}")
    api.write_memory("d018", f"{VIC.D018_CHAR_DEFAULT:02X}")


def _mirror_glyph_h(glyph: bytes) -> bytes:
    """Horizontally flip an 8-byte cell glyph by bit-reversing each row byte.
    Used to synthesize a right-arrow from the ROM's left-arrow glyph."""
    return bytes(int(f"{b:08b}"[::-1], 2) for b in glyph)


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
        max_left = width - len(right) - 1  # 1-char minimum gap
        if len(left) > max_left:
            left = left[:max_left]
    gap = width - len(left) - len(right)
    return left + (" " * gap) + right


def _layout_lcr(left: str, center: str, right: str, width: int = SCREEN_W_CHARS) -> str:
    """Build a width-char line with left/center/right fields. Center is
    placed at the geometric middle when room allows, then nudged off-center
    to avoid colliding with left or right if either is unusually long."""
    left = left[:width]
    center = center[:width]
    right = right[:width]
    # Truncate left first if everything together overflows — copyright is
    # usually the most flexible field (year + label is the gist).
    max_total = width - 2  # leave 1-char gaps each side
    while len(left) + len(center) + len(right) > max_total and len(left) > 1:
        left = left[:-1]
    while len(left) + len(center) + len(right) > max_total and len(right) > 1:
        right = right[:-1]
    if len(left) + len(center) + len(right) > max_total:
        # Even with both sides minimal, center is too wide — truncate it.
        center = center[: max_total - len(left) - len(right)]
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


def _compute_window_slices(n: int) -> list[tuple[int, int]]:
    """Divide the 320px (40-cell) strip width into `n` cell-aligned windows,
    returning ``(x_off_px, width_px)`` per window. The 40 cells split as evenly
    as possible with the remainder given to the earliest windows, so every
    window is a whole number of 8px cells (keeps ``packbits`` + color RAM
    cell-aligned). ``n=1 → [(0, 320)]`` (identity — the single-chip default).

    Used by the multi-chip AsidScene "split" scope: window `c` of a voice strip
    shows chip `c`'s copy of that voice, side by side."""
    if n <= 1:
        return [(0, BITMAP_W)]
    base, rem = divmod(SCREEN_W_CHARS, n)
    slices: list[tuple[int, int]] = []
    x_cell = 0
    for i in range(n):
        cells = base + (1 if i < rem else 0)
        slices.append((x_cell * CELL_PX, cells * CELL_PX))
        x_cell += cells
    return slices


class VoiceScopeRenderer:
    """Mixin providing the hires oscilloscope render core.

    Renders three vertically-stacked voice strips; each strip is optionally
    subdivided into ``_n_windows`` side-by-side horizontal windows (one per SID
    chip — the multi-chip "split" scope). With one window (the default) the
    output is byte-identical to the pre-multi-chip renderer, so WaveformScene /
    MidiScene are unaffected.

    See the module docstring for the attribute contract a host scene must
    satisfy. All bitmap/screen writes go through ``write_region`` so the
    delta cache absorbs unchanged columns.
    """

    # Live-tunable params: name -> (min, max) for a CC-style [0, 1] sweep (the
    # sx/ix WLED sliders + the identical midi-CC seam). Resolved via the
    # `scene.` target prefix — a scope scene *is* the renderer, so it has no
    # source/effect holder for the live-param resolvers to reach.
    LIVE_PARAMS: dict[str, tuple[float, float]] = {"gain": (0.25, 3.0)}
    gain: float

    # The attribute contract, declared so the type checker sees it on the mixin.
    api: C64Backend
    emulator: SIDEmulator
    _reg_lock: threading.Lock
    _screen_base: int
    _bitmap_base: int
    _dd00: int
    _d018: int
    # Multi-chip split scope: window `c` of a strip is sourced from
    # ``_emulators[c]``. Host-supplied; without it ``_scope_emulators()`` falls
    # back to ``[self.emulator]`` and every window past the first renders blank.
    _emulators: list[SIDEmulator]
    _n_windows: int
    _window_slices: list[tuple[int, int]]
    # Window index → chip index. Identity by default (chip 0 leftmost); a scene
    # that pans its chips reorders this so columns run left-to-right across the
    # stereo field instead of by chip number (see sid_panning.window_order_for_pans).
    _window_chip_order: list[int]
    # A class default so no host has to supply it; the warning sets the
    # instance attribute. Same shape as AsidScene's `_warned_downmix`.
    _warned_forced_fast: bool = False

    def _scope_emulators(self) -> list[SIDEmulator]:
        """The per-window SID sources, in window (left-to-right) order —
        ``self._emulators`` permuted by ``_window_chip_order`` if a host scene
        set it, else the single ``self.emulator`` (single-chip default)."""
        emus = getattr(self, "_emulators", None)
        if not emus:
            return [self.emulator]
        order = getattr(self, "_window_chip_order", None)
        if not order:
            return emus
        return [emus[chip] for chip in order if chip < len(emus)]

    def set_window_chip_order(self, order: Sequence[int]) -> None:
        """Set which chip each scope column shows, left to right. Ignored unless
        `order` is a permutation of the current window count: a mismatched order
        (a stale one from a different chip count) is dropped and the *current*
        order stays in place, rather than dropping or duplicating a chip's
        window. Identity on a reflow is the caller's doing, not this method's —
        `_set_window_count` resets to identity first, and every caller reflows
        before it pans."""
        if sorted(order) != list(range(self._n_windows)):
            log.debug(
                "scope: ignoring window order %s for %d window(s)", list(order), self._n_windows
            )
            return
        self._window_chip_order = list(order)

    def _resolve_render_modes(self) -> tuple[list[str], bool]:
        """Derive the per-voice render modes (and the all-"fast" shortcut flag)
        from the configured knobs. Pure in `scroll_columns` + `_echo_depth`, so
        construction and a live reflow always reach the same answer — which is
        what lets `_set_window_count` *re-derive* rather than clobber.

        Echo is only meaningful for a voice that isn't scrolling: scroll already
        gives a natural "trail off the left edge", and mixing the two
        double-draws the same trace at different x's every frame. The all-fast
        flag is the shortcut for "no per-voice persistent state needed at all"."""
        modes: list[str] = []
        for sn in self.scroll_columns:
            if sn > 0:
                modes.append(RENDER_MODE_SCROLL)
            elif self._echo_depth > 0:
                modes.append(RENDER_MODE_ECHO)
            else:
                modes.append(RENDER_MODE_FAST)
        return modes, all(m == RENDER_MODE_FAST for m in modes)

    def _set_window_count(self, n: int) -> None:
        """Reflow the split scope to `n` chip windows (multi-chip scenes call
        this when the SID count changes, and `_init_scope_knobs` calls it once
        at construction so there is only one copy of this layout). Recomputes
        the cell-aligned window slices, resets the column order to chip order (a
        caller that pans re-applies its own order after), and re-derives the
        per-voice render modes — forcing the fast path for n>1, and restoring
        the configured scroll/echo when the count shrinks back to 1.

        The force is announced once per instance: a user who configured
        `persistence`/`scroll_columns` and then hits a multi-chip stream would
        otherwise watch the trails vanish with nothing in the log. Once, because
        the message is a consequence of the knobs plus `n` and so is identical
        every time, while the reflow is not a one-off — a playlist reuses scene
        instances and re-runs `setup()` each lap, and `WaveformScene` reflows per
        tune. Repeats go to DEBUG rather than nowhere, so `-v` still shows each
        reflow."""
        self._n_windows = max(1, n)
        self._window_slices = _compute_window_slices(self._n_windows)
        self._window_chip_order = list(range(self._n_windows))
        self._voice_render_modes, self._fast_path = self._resolve_render_modes()
        if self._n_windows > 1 and not self._fast_path:
            forced = (
                "voice_scope: scroll/persistence not supported for the multi-chip "
                "split scope — forcing the fast render path"
            )
            if self._warned_forced_fast:
                log.debug(forced)
            else:
                self._warned_forced_fast = True
                log.warning(forced)
            self._voice_render_modes = [RENDER_MODE_FAST] * len(BITMAP_STRIPS)
            self._fast_path = True

    def _init_scope_knobs(
        self,
        *,
        color_mode: str,
        voice_colors: list | None,
        waveform_colors: dict | None,
        time_base: str,
        auto_cycles: float,
        persistence: str,
        scroll_columns: int | list[int],
        frame_time_s: float,
        n_windows: int = 1,
    ) -> None:
        """Validate + normalize the visualization knobs and derive the
        per-voice render modes. Sets every knob attribute in the contract
        plus the (initially-None) per-render buffers. Raises ValueError on
        any invalid knob — matching the prior in-__init__ validation.

        ``n_windows`` (default 1) is the number of side-by-side chip windows per
        strip; the layout itself is applied by ``_set_window_count``, which also
        derives the render modes. Single-chip scenes (waveform/midi) omit it →
        byte-identical output. When >1 the per-voice scroll/echo modes are
        forced to "fast", announced once per instance (per-window persistence
        buffers are out of scope for v1)."""
        if color_mode not in ("per_voice", "per_waveform"):
            raise ValueError("voice_scope: color_mode must be 'per_voice' or 'per_waveform'")
        if time_base not in TIME_BASE_NAMES:
            raise ValueError(
                f"voice_scope: time_base must be one of {TIME_BASE_NAMES}, got {time_base!r}"
            )
        if auto_cycles <= 0:
            raise ValueError(f"voice_scope: auto_cycles must be > 0, got {auto_cycles!r}")
        if persistence not in PERSISTENCE_NAMES:
            raise ValueError(
                f"voice_scope: persistence must be one of {PERSISTENCE_NAMES}, got {persistence!r}"
            )

        self._frame_time_s = frame_time_s
        self.gain = 1.0
        self.color_mode = color_mode
        self.voice_color_names = list(voice_colors or DEFAULT_VOICE_COLORS)
        if len(self.voice_color_names) < 3:
            raise ValueError("voice_scope: voice_colors must have 3 entries")
        wf_defaults = dict(DEFAULT_WAVEFORM_COLORS)
        wf_defaults.update(waveform_colors or {})
        self.waveform_color_names = wf_defaults

        self.time_base = time_base
        self.auto_cycles = float(auto_cycles)

        # Each entry is the number of new columns drawn (and the strip shifted
        # left by) per frame for that voice; 0 = no scroll, full-frame redraw.
        if isinstance(scroll_columns, int):
            sc_list = [scroll_columns, scroll_columns, scroll_columns]
        else:
            sc_list = list(scroll_columns)
        if len(sc_list) != 3:
            raise ValueError(
                f"voice_scope: scroll_columns list must have 3 entries, got {sc_list!r}"
            )
        for x in sc_list:
            if not isinstance(x, int) or x < 0 or x > BITMAP_W:
                raise ValueError(
                    f"voice_scope: scroll_columns entries must be ints in "
                    f"0..{BITMAP_W}, got {sc_list!r}"
                )
        self.scroll_columns: list[int] = sc_list

        # The "random" sentinel resolves now, so the chosen preset is stable
        # across setup/teardown cycles within this scene instance.
        self.persistence_config = persistence
        if persistence == RANDOM_PERSISTENCE:
            self.persistence = random.choice(_PERSISTENCE_RANDOM_CHOICES)
        else:
            self.persistence = persistence
        # One history slot per entry, oldest first; the current frame is
        # overlaid on top in the voice's regular color.
        echo_names = PERSISTENCE_ECHOES[self.persistence]
        self._echo_colors: list[int] = [
            resolve_color(n, default=C64_COLORS["black"]) for n in echo_names
        ]
        self._echo_depth = len(self._echo_colors)
        # Derived by _set_window_count below, and again on every runtime reflow.
        self._voice_render_modes: list[str] = []
        self._fast_path = True

        # Per-render persistent state, allocated in _alloc_scope_buffers().
        # _strips: per-voice scroll-mode bool strip, persisting across frames so
        #          the shift can rotate old samples left.
        # _echo_history: per-voice past bool masks, oldest first, up to
        #          _echo_depth of them.
        # _last_y: per-voice last column's y from the previous frame, so the
        #          first new scroll column connects instead of being a self-dot.
        # _rows_col: cached row-index broadcast column used by every path.
        self._strips: list[np.ndarray | None] | None = None
        self._echo_history: list[list[np.ndarray]] | None = None
        self._last_y: list[int | None] | None = None
        self._rows_col: np.ndarray | None = None

        if n_windows < 1:
            raise ValueError(f"voice_scope: n_windows must be >= 1, got {n_windows!r}")
        self._set_window_count(n_windows)

        # Loaded by _apply_vic_hires_bank.
        self._glyphs: bytes | None = None

    def _alloc_scope_buffers(self) -> None:
        """Allocate the per-voice persistent render buffers — only what each
        voice's mode actually needs (most scenes hit one mode per voice)."""
        self._strips = []
        self._echo_history = []
        self._last_y = []
        for v_idx, (top, bot) in enumerate(BITMAP_STRIPS):
            mode = self._voice_render_modes[v_idx]
            self._strips.append(
                np.zeros((bot - top, BITMAP_W), dtype=bool) if mode == RENDER_MODE_SCROLL else None
            )
            self._echo_history.append([])
            self._last_y.append(None)
        self._rows_col = np.arange(BITMAP_H, dtype=np.int32)[:, None]

    def _apply_vic_hires_bank(self) -> None:
        """Point VIC at the current display bank ($DD00/$D018), clear its
        bitmap + screen matrix, paint the per-voice colors, and load the
        charset. The host scene paints its own title/meta text rows after.

        The VIC bring-up goes through the shared ``modes.engage_bitmap_mode``
        primitive — the SAME clear-then-flip path the Hires/MultiHires display
        modes use — so the engage clean-field invariant (zero $2000 + the screen
        matrix BEFORE the $D011 bitmap-mode flip) and any future VIC-register
        change live in one place. The scope's legitimate differences are passed
        as arguments: it RELOCATES the VIC bank (``dd00`` + ``bitmap_base`` /
        ``screen_base`` / ``d018`` — bank 0↔2 per the SID's footprint) and clears
        via the delta-cached ``write_region`` path under stable region IDs
        (``WAVE_BITMAP`` / ``WAVE_SCREEN_CLEAR``) so the FULL screen matrix —
        including the spacer rows (21, 24) the per-voice/title/meta paints don't
        cover — is zeroed; in a relocated bank those cells are otherwise
        uninitialized RAM that would render as garbage.

        Callers must ``invalidate_cache()`` first so a bank switch over the
        same addresses gets a clean delta baseline."""
        engage_bitmap_mode(
            self.api,
            d011=D011_HIRES_ON,
            d018=f"{self._d018:02X}",
            d016=D016_STANDARD,
            bitmap_base=self._bitmap_base,
            screen_base=self._screen_base,
            dd00=self._dd00,
            border=0x00,
            bg0=0x00,
            clear_region_ids=(RegionID.WAVE_BITMAP, RegionID.WAVE_SCREEN_CLEAR),
        )
        self._init_hires_colors()
        # Process-wide cached, so a second scope scene doesn't re-read the file.
        self._glyphs = _load_glyphs()

    def _init_hires_colors(self) -> None:
        """Write per-voice FG/BG colors to the screen-RAM cells under
        each voice's bitmap strip (one color per chip window)."""
        for v_idx in range(len(BITMAP_STRIPS)):
            color = self._initial_voice_color(v_idx)
            self._paint_strip_color_row(v_idx, [color] * self._n_windows)

    def _paint_strip_color_row(self, v_idx: int, window_colors: list[int]) -> None:
        """Write the whole voice strip's FG-nibble color block once (region
        ``WAVE_SCREEN + v_idx``), coloring each chip window's cell columns from
        ``window_colors`` (one palette index per window, length == n_windows).

        With one window this is byte-identical to a uniform strip fill. Writing
        the entire strip as a single region keeps the delta cache's stable-
        address contract intact (per-window sub-address writes under one region
        id would corrupt it)."""
        top, bot = BITMAP_STRIPS[v_idx]
        cell_row_top = top // CELL_PX
        n_rows = (bot - top) // CELL_PX
        row = np.zeros(SCREEN_W_CHARS, dtype=np.uint8)  # BG=black in cells not covered
        for (x_off, w), color in zip(self._window_slices, window_colors, strict=False):
            c0 = x_off // CELL_PX
            c1 = (x_off + w) // CELL_PX
            row[c0:c1] = (color & COLOR_NIBBLE_MASK) << 4  # FG in high nibble
        block = np.tile(row, n_rows).tobytes()
        self.api.write_region(
            self._screen_base + cell_row_top * SCREEN_W_CHARS,
            block,
            region_id=RegionID.WAVE_SCREEN + v_idx,
        )

    def _initial_voice_color(self, v_idx: int) -> int:
        if self.color_mode == "per_voice":
            return resolve_color(self.voice_color_names[v_idx], default=C64_COLORS["white"])
        return resolve_color(self.waveform_color_names["off"], default=C64_COLORS["dark gray"])

    def _voice_color_now(self, v_idx: int, emulator: SIDEmulator | None = None) -> int:
        if self.color_mode == "per_voice":
            return resolve_color(self.voice_color_names[v_idx], default=C64_COLORS["white"])
        emu = emulator if emulator is not None else self.emulator
        v = emu.voices[v_idx]
        wave = primary_waveform(v.control)
        name = {
            WAVE_TRIANGLE: "triangle",
            WAVE_SAWTOOTH: "sawtooth",
            WAVE_PULSE: "pulse",
            WAVE_NOISE: "noise",
            0: "off",
        }[wave]
        return resolve_color(self.waveform_color_names[name], default=C64_COLORS["white"])

    def _repaint_voice_color(self, v_idx: int, color: int | None = None) -> None:
        """Re-write the screen-RAM FG-nibble cells under the given voice's
        bitmap strip with `color` (a C64 palette index), or the voice's current
        color when None — broadcast to every chip window. MidiScene passes an
        explicit gray to dim idle voices. Multi-chip scenes that need distinct
        per-window colors call ``_paint_strip_color_row`` directly."""
        if color is None:
            color = self._voice_color_now(v_idx)
        self._paint_strip_color_row(v_idx, [color] * self._n_windows)

    def _paint_text_row(
        self,
        cell_row: int,
        text: str,
        fg: int,
        bitmap_region_id: int,
        screen_region_id: int,
        glyph_overrides: dict[str, bytes] | None = None,
    ) -> None:
        """Render a 40-char line into one bitmap cell-row + matching FG
        color into screen RAM. Caller supplies the two region IDs so the
        delta cache absorbs unchanged columns on re-paint (a SHIFT-driven
        title repaint typically only changes ~2 digit cells, ~16 bytes).

        ``glyph_overrides`` maps a character to an explicit 8-byte cell glyph,
        bypassing the ROM lookup for that char — used to place synthesized
        glyphs (e.g. a mirrored right-arrow) the charset has no native cell for.
        Defaults to None → byte-identical to the ROM-only path for all other
        callers."""
        assert self._glyphs is not None
        assert len(text) == SCREEN_W_CHARS, (
            f"text row must be exactly {SCREEN_W_CHARS} chars, got {len(text)}"
        )
        glyphs = self._glyphs
        bitmap_bytes = bytearray(SCREEN_W_CHARS * CELL_PX)
        for col, ch in enumerate(text):
            override = glyph_overrides.get(ch) if glyph_overrides else None
            if override is not None:
                cell = override
            else:
                sc = _ascii_to_screen_code(ch)
                cell = glyphs[sc * CELL_PX : (sc + 1) * CELL_PX]
            bitmap_bytes[col * CELL_PX : (col + 1) * CELL_PX] = cell
        bitmap_addr = self._bitmap_base + cell_row * BITMAP_CELL_ROW_BYTES
        self.api.write_region(bitmap_addr, bytes(bitmap_bytes), region_id=bitmap_region_id)
        fg_byte = (fg & COLOR_NIBBLE_MASK) << 4  # FG in high nibble, BG = 0
        screen_addr = self._screen_base + cell_row * SCREEN_W_CHARS
        self.api.write_region(
            screen_addr, bytes([fg_byte] * SCREEN_W_CHARS), region_id=screen_region_id
        )

    def _build_title_line(self) -> str:
        """Subclass hook: the 40-char top info row (see _paint_info_rows)."""
        raise NotImplementedError

    def _build_meta_line(self) -> str:
        """Subclass hook: the 40-char second info row (see _paint_info_rows)."""
        raise NotImplementedError

    def _paint_info_rows(self) -> None:
        """Paint the two 40-char info rows (title + meta) in the shared scope
        colors and delta-cache regions. AsidScene and MidiScene supply the two
        line builders above; WaveformScene keeps its own two-step painters
        because its metadata row needs a synthesized-glyph override."""
        title_fg = C64_COLORS.get(TITLE_TEXT_COLOR, C64_COLORS["white"])
        self._paint_text_row(
            TITLE_ROW,
            self._build_title_line(),
            title_fg,
            RegionID.WAVE_TITLE_BITMAP,
            RegionID.WAVE_TITLE_SCREEN,
        )
        meta_fg = C64_COLORS.get(METADATA_TEXT_COLOR, C64_COLORS["light gray"])
        self._paint_text_row(
            META_ROW,
            self._build_meta_line(),
            meta_fg,
            RegionID.WAVE_META_BITMAP,
            RegionID.WAVE_META_SCREEN,
        )

    def _voice_time_window_s(
        self, v_idx: int, n_cols: int, *, emulator: SIDEmulator | None = None
    ) -> float:
        """Return the audio time spanned by `n_cols` columns for voice
        v_idx (of `emulator`, defaulting to the scene's primary SID).

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
        emu = emulator if emulator is not None else self.emulator
        if self.time_base == TIME_BASE_WALLCLOCK:
            full_window = self._frame_time_s
        else:
            v = emu.voices[v_idx]
            if v.is_silent():
                full_window = self._frame_time_s
            else:
                # SID freq (Hz) = freq_reg * clock / 2^24; period = 1/freq_hz.
                period_s = ACCUMULATOR_RANGE / (v.freq * emu.clock)
                full_window = self.auto_cycles * period_s
        return full_window * n_cols / BITMAP_W

    def _compute_ys(
        self, v_idx: int, top: int, bot: int, n_new: int, *, emulator: SIDEmulator | None = None
    ) -> np.ndarray:
        """Sample n_new audio samples for voice v_idx (of `emulator`, default
        the primary SID) at the per-voice time window and map to pixel-row y in
        absolute bitmap coords (top..bot-1)."""
        emu = emulator if emulator is not None else self.emulator
        mid = (top + bot) // 2
        half_h = (bot - top) // 2 - 1
        # The lock covers the emulator reads + sample synthesis only, and is
        # released before mask packing and DMA: the poll thread writes the same
        # voice state, and must never be blocked across the wire.
        with self._reg_lock:
            time_window_s = self._voice_time_window_s(v_idx, n_new, emulator=emu)
            samples = emu.voice_samples(v_idx, n_new, time_window_s)
        # The clip keeps an overdriven `gain` inside the strip.
        ys = (mid - samples * half_h * self.gain).astype(np.int32)
        np.clip(ys, top, bot - 1, out=ys)
        return ys

    def _span_mask(self, ys: np.ndarray, top: int, bot: int, prev_y: int | None) -> np.ndarray:
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
        strip_rows = self._rows_col[: bot - top]
        return (strip_rows >= lo[None, :]) & (strip_rows <= hi[None, :])

    def _write_bitmap_strip(self, v_idx: int, top: int, bot: int, mask: np.ndarray) -> None:
        """Pack a (strip_h, BITMAP_W) bool mask into the C64 hires bitmap
        memory layout and DMA it to the strip's bitmap region."""
        cell_row_top = top // CELL_PX
        cell_row_bot = bot // CELL_PX
        n_cell_rows = cell_row_bot - cell_row_top
        packed = np.packbits(mask, axis=1)  # (strip_h, 40)
        bitmap_strip = (
            packed.reshape(n_cell_rows, CELL_PX, SCREEN_W_CHARS).transpose(0, 2, 1).tobytes()
        )
        self.api.write_region(
            self._bitmap_base + cell_row_top * BITMAP_CELL_ROW_BYTES,
            bitmap_strip,
            region_id=RegionID.WAVE_BITMAP + v_idx,
        )

    def _render_voice_fast(self, v_idx: int, top: int, bot: int) -> None:
        """Default redraw-from-scratch: sample → mask → pack → write.
        No persistent state. Output bytes are identical to the pre-knob
        bool-canvas implementation when all voices take this path.

        With multiple chip windows, each window `c` is sampled from
        ``emulators[c]`` into its horizontal slice of one full-strip canvas,
        with a 1px dark gutter between chips, then a single bitmap write."""
        slices = self._window_slices
        if len(slices) == 1:
            # Identity path — byte-for-byte the single-chip render.
            ys = self._compute_ys(v_idx, top, bot, BITMAP_W)
            mask = self._span_mask(ys, top, bot, prev_y=None)
            self._write_bitmap_strip(v_idx, top, bot, mask)
            return
        emus = self._scope_emulators()
        canvas = np.zeros((bot - top, BITMAP_W), dtype=bool)
        last = len(slices) - 1
        for c, (x_off, w) in enumerate(slices):
            if c >= len(emus):
                continue  # no chip for this window yet → leave it blank
            ys = self._compute_ys(v_idx, top, bot, w, emulator=emus[c])
            submask = self._span_mask(ys, top, bot, prev_y=None)
            canvas[:, x_off : x_off + w] = submask
            if c < last:  # 1px separator gutter between chips
                canvas[:, x_off + w - 1] = False
        self._write_bitmap_strip(v_idx, top, bot, canvas)

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
        strip[:, BITMAP_W - scroll_n :] = mask
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
        assert (
            self._strips is not None and self._echo_history is not None and self._last_y is not None
        )
        history = self._echo_history[v_idx]
        ys = self._compute_ys(v_idx, top, bot, BITMAP_W)
        current_mask = self._span_mask(ys, top, bot, prev_y=None)

        # Past frames stay lit until they age out of the ring buffer.
        combined = current_mask.copy()
        for past in history:
            combined |= past

        # Per-cell color: walk newest→oldest, assigning each cell to the color
        # of the freshest trace whose mask has any pixel in it. Sliced via plain
        # `len` rather than a negative index — `_echo_colors[-0:]` would return
        # the full list on a first-frame warm-up instead of empty.
        masks_newest_first: list[np.ndarray] = [current_mask, *reversed(history)]
        n_hist = len(history)
        past_colors_newest_first = list(reversed(self._echo_colors[-n_hist:])) if n_hist else []
        colors_newest_first: list[int] = [
            self._voice_color_now(v_idx),
            *past_colors_newest_first,
        ]
        n_cell_rows = (bot - top) // CELL_PX
        cell_color = np.zeros((n_cell_rows, SCREEN_W_CHARS), dtype=np.uint8)
        claimed = np.zeros((n_cell_rows, SCREEN_W_CHARS), dtype=bool)
        for mask, color in zip(masks_newest_first, colors_newest_first, strict=True):
            # Reduce over the two pixel-within-cell axes: "any pixel lit?"
            cell_lit = mask.reshape(n_cell_rows, CELL_PX, SCREEN_W_CHARS, CELL_PX).any(axis=(1, 3))
            new_claims = cell_lit & ~claimed
            cell_color[new_claims] = color & COLOR_NIBBLE_MASK
            claimed |= new_claims

        # FG-nibble bytes: high nibble = FG color, BG = 0/black.
        screen_bytes = (cell_color << 4).astype(np.uint8).tobytes()
        cell_row_top = top // CELL_PX
        self.api.write_region(
            self._screen_base + cell_row_top * SCREEN_W_CHARS,
            screen_bytes,
            region_id=RegionID.WAVE_SCREEN + v_idx,
        )

        self._write_bitmap_strip(v_idx, top, bot, combined)

        history.append(current_mask)
        if len(history) > self._echo_depth:
            history.pop(0)

    def _render_hires(self) -> None:
        """Render each voice strip via its configured mode (fast / scroll
        / echo). Per-voice modes are derived by ``_resolve_render_modes`` from
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
            if mode == RENDER_MODE_SCROLL:
                self._render_voice_scroll(v_idx, top, bot)
            elif mode == RENDER_MODE_ECHO:
                self._render_voice_echo(v_idx, top, bot)
            else:
                self._render_voice_fast(v_idx, top, bot)
