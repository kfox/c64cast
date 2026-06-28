"""Shared 3-voice SID oscilloscope renderer (hires bitmap).

Extracted from :mod:`c64cast.waveform` so both :class:`~c64cast.waveform.WaveformScene`
(SID-file playback) and :class:`~c64cast.midi_scene.MidiScene` (live MIDI input)
can paint the same full-screen 320×200 hires oscilloscope of the three SID voices.

The renderer is **SID-source-agnostic**: it reads per-voice state from a
:class:`~c64cast.sidemu.SIDEmulator` the host scene owns, and draws three
vertically-stacked voice strips plus two bottom text rows. *How* that emulator's
register state is kept current differs per host — WaveformScene mirrors a parallel
py65 6502 (it can't read the U64's write-only SID back), MidiScene feeds its own
register shadow (it computes every byte it sends) — but the rendering is identical.

``VoiceScopeRenderer`` is a **mixin**: methods reference ``self.<attr>`` directly
(rather than taking a helper object) so WaveformScene's byte-output and its test
suite stay unchanged across the extraction. A host scene must provide these
attributes before any render call (the **attribute contract**):

  * ``self.api``            — C64 backend (write_memory / write_regs / write_region)
  * ``self.emulator``       — SIDEmulator with three voices
  * ``self._reg_lock``      — threading.Lock guarding emulator reads/writes
  * ``self._screen_base``   — screen-matrix base address (e.g. $0400)
  * ``self._bitmap_base``   — bitmap base address (e.g. $2000)
  * ``self._dd00``          — CIA2 port-A value selecting the VIC bank
  * ``self._d018``          — $D018 value (matrix + bitmap sub-bank offsets)
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
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

import numpy as np

from .bitmap_text import ascii_to_screen_code as _ascii_to_screen_code
from .bitmap_text import load_glyphs as _load_glyphs
from .c64 import SCREEN, RegionID
from .modes import engage_bitmap_mode
from .palette import C64_COLORS
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

    from .backend import C64Backend
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

# VIC register values _apply_vic_hires_bank passes to modes.engage_bitmap_mode.
# Hex strings match the write_memory API. Named so the intent (bitmap mode,
# multicolor off, screen page) is readable inline.
D011_HIRES_ON = "3b"  # bitmap mode + display enable, raster MSB clear
D016_STANDARD = "08"  # 40-col, no multicolor
# $D018 selects the screen matrix (bits 7-4 = offset/$0400 within the bank)
# and the bitmap (bit 3 = bitmap at bank+$2000). $18 = matrix at bank+$0400
# + bitmap at bank+$2000 — bank-relative.
D018_HIRES_BITMAP = 0x18  # bank-relative: screen +$0400, bitmap +$2000

COLOR_NIBBLE_MASK = 0x0F

DEFAULT_VOICE_COLORS = ["cyan", "yellow", "light green"]
DEFAULT_WAVEFORM_COLORS = {
    "triangle": "light green",
    "sawtooth": "light red",
    "pulse": "cyan",
    "noise": "yellow",
    "off": "dark gray",
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
    "off": (),
    "short": ("gray", "light gray"),  # 2 visible echoes
    "medium": ("dark gray", "gray", "light gray"),  # 3 visible echoes
    "long": ("black", "dark gray", "gray", "light gray"),  # 4 slots (oldest invisible)
}
RANDOM_PERSISTENCE = "random"
PERSISTENCE_NAMES = (*PERSISTENCE_ECHOES.keys(), RANDOM_PERSISTENCE)
# Random pick excludes "off" — the user explicitly didn't ask for a fixed
# preset; the visual reward of random is the echo trail itself.
_PERSISTENCE_RANDOM_CHOICES = ("short", "medium", "long")

# Glyph loading + ASCII→screen-code mapping live in bitmap_text now (shared with
# the on-C64 menu). Aliased to the historical private names so the rest of this
# module — and its byte-for-byte oscilloscope output — is unchanged.


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


class VoiceScopeRenderer:
    """Mixin providing the hires 3-voice oscilloscope render core.

    See the module docstring for the attribute contract a host scene must
    satisfy. All bitmap/screen writes go through ``write_region`` so the
    delta cache absorbs unchanged columns.
    """

    # ---- attribute contract (host scene supplies these; declared here so
    # the type checker sees them on the mixin) ------------------------------
    api: C64Backend
    emulator: SIDEmulator
    _reg_lock: threading.Lock
    _screen_base: int
    _bitmap_base: int
    _dd00: int
    _d018: int

    # ---- knob parsing / buffer allocation ----------------------------------

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
    ) -> None:
        """Validate + normalize the visualization knobs and derive the
        per-voice render modes. Sets every knob attribute in the contract
        plus the (initially-None) per-render buffers. Raises ValueError on
        any invalid knob — matching the prior in-__init__ validation."""
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
        self.color_mode = color_mode
        self.voice_color_names = list(voice_colors or DEFAULT_VOICE_COLORS)
        if len(self.voice_color_names) < 3:
            raise ValueError("voice_scope: voice_colors must have 3 entries")
        wf_defaults = dict(DEFAULT_WAVEFORM_COLORS)
        wf_defaults.update(waveform_colors or {})
        self.waveform_color_names = wf_defaults

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
                f"voice_scope: scroll_columns list must have 3 entries, got {sc_list!r}"
            )
        for x in sc_list:
            if not isinstance(x, int) or x < 0 or x > BITMAP_W:
                raise ValueError(
                    f"voice_scope: scroll_columns entries must be ints in "
                    f"0..{BITMAP_W}, got {sc_list!r}"
                )
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
        self._echo_colors: list[int] = [C64_COLORS.get(n, C64_COLORS["black"]) for n in echo_names]
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

        # Per-render persistent state. Allocated in _alloc_scope_buffers().
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

        # Charset (loaded once, cached process-wide) — set by the bring-up.
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
                np.zeros((bot - top, BITMAP_W), dtype=bool) if mode == "scroll" else None
            )
            self._echo_history.append([])
            self._last_y.append(None)
        self._rows_col = np.arange(BITMAP_H, dtype=np.int32)[:, None]

    # ---- VIC setup ---------------------------------------------------------

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
        # Lazy-load the charset once. Glyph loading is process-wide cached so
        # a second scope scene doesn't re-read the file.
        self._glyphs = _load_glyphs()

    def _init_hires_colors(self) -> None:
        """Write per-voice FG/BG colors to the screen-RAM cells under
        each voice's bitmap strip."""
        for v_idx, (top, bot) in enumerate(BITMAP_STRIPS):
            color = self._initial_voice_color(v_idx)
            cell_row_top = top // CELL_PX
            cell_row_bot = bot // CELL_PX
            n_rows = cell_row_bot - cell_row_top
            byte = (color & COLOR_NIBBLE_MASK) << 4  # FG = color, BG = black
            block = bytes([byte] * (n_rows * SCREEN_W_CHARS))
            self.api.write_region(
                self._screen_base + cell_row_top * SCREEN_W_CHARS,
                block,
                region_id=RegionID.WAVE_SCREEN + v_idx,
            )

    # ---- per-voice color resolution ----------------------------------------

    def _initial_voice_color(self, v_idx: int) -> int:
        if self.color_mode == "per_voice":
            return C64_COLORS.get(self.voice_color_names[v_idx], C64_COLORS["white"])
        return C64_COLORS.get(self.waveform_color_names["off"], C64_COLORS["dark gray"])

    def _voice_color_now(self, v_idx: int) -> int:
        if self.color_mode == "per_voice":
            return C64_COLORS.get(self.voice_color_names[v_idx], C64_COLORS["white"])
        v = self.emulator.voices[v_idx]
        wave = primary_waveform(v.control)
        name = {
            WAVE_TRIANGLE: "triangle",
            WAVE_SAWTOOTH: "sawtooth",
            WAVE_PULSE: "pulse",
            WAVE_NOISE: "noise",
            0: "off",
        }[wave]
        return C64_COLORS.get(self.waveform_color_names[name], C64_COLORS["white"])

    def _repaint_voice_color(self, v_idx: int, color: int | None = None) -> None:
        """Re-write the screen-RAM FG-nibble cells under the given voice's
        bitmap strip with `color` (a C64 palette index), or the voice's current
        color when None. MidiScene passes an explicit gray to dim idle voices."""
        if color is None:
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

    def _paint_text_row(
        self, cell_row: int, text: str, fg: int, bitmap_region_id: int, screen_region_id: int
    ) -> None:
        """Render a 40-char line into one bitmap cell-row + matching FG
        color into screen RAM. Caller supplies the two region IDs so the
        delta cache absorbs unchanged columns on re-paint (a SHIFT-driven
        title repaint typically only changes ~2 digit cells, ~16 bytes)."""
        assert self._glyphs is not None
        assert len(text) == SCREEN_W_CHARS, (
            f"text row must be exactly {SCREEN_W_CHARS} chars, got {len(text)}"
        )
        glyphs = self._glyphs
        bitmap_bytes = bytearray(SCREEN_W_CHARS * CELL_PX)
        for col, ch in enumerate(text):
            sc = _ascii_to_screen_code(ch)
            bitmap_bytes[col * CELL_PX : (col + 1) * CELL_PX] = glyphs[
                sc * CELL_PX : (sc + 1) * CELL_PX
            ]
        bitmap_addr = self._bitmap_base + cell_row * BITMAP_CELL_ROW_BYTES
        self.api.write_region(bitmap_addr, bytes(bitmap_bytes), region_id=bitmap_region_id)
        fg_byte = (fg & COLOR_NIBBLE_MASK) << 4  # FG in high nibble, BG = 0
        screen_addr = self._screen_base + cell_row * SCREEN_W_CHARS
        self.api.write_region(
            screen_addr, bytes([fg_byte] * SCREEN_W_CHARS), region_id=screen_region_id
        )

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

    def _compute_ys(self, v_idx: int, top: int, bot: int, n_new: int) -> np.ndarray:
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
        past_colors_newest_first = list(reversed(self._echo_colors[-n_hist:])) if n_hist else []
        colors_newest_first: list[int] = [
            self._voice_color_now(v_idx),
            *past_colors_newest_first,
        ]
        n_cell_rows = (bot - top) // CELL_PX
        cell_color = np.zeros((n_cell_rows, SCREEN_W_CHARS), dtype=np.uint8)
        claimed = np.zeros((n_cell_rows, SCREEN_W_CHARS), dtype=bool)
        for mask, color in zip(masks_newest_first, colors_newest_first, strict=True):
            # Reshape to (cell_rows, CELL_PX, SCREEN_W_CHARS, CELL_PX) and
            # reduce over the pixel-within-cell axes to "any pixel lit?".
            cell_lit = mask.reshape(n_cell_rows, CELL_PX, SCREEN_W_CHARS, CELL_PX).any(axis=(1, 3))
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

    def _render_hires(self) -> None:
        """Render each voice strip via its configured mode (fast / scroll
        / echo). Per-voice modes are computed once in _init_scope_knobs based
        on scroll_columns + persistence so the per-frame branch is a cheap
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
