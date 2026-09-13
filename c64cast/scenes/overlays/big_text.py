"""Classic 1×→8× demo-scene scroller.

See docs/architecture/scenes.md#big_text--the-8-scroller.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from c64cast.hw.c64 import KERNAL, RASTER_VBLANK_LINE, SCREEN
from c64cast.video.palette import C64_COLORS, C64_SPECTRUM_INDICES, resolve_color

from . import (
    Overlay,
    ascii_to_screen,
    register,
)

log = logging.getLogger(__name__)

SCREEN_W_CELLS = 40
SCREEN_H_CELLS = 25
SCREEN_W_PX = 320
SCREEN_H_PX = 200

GLYPH_CELL_H = 8  # source glyph is 8 px tall → 8 screen rows
GLYPH_CELL_W = 8  # 8 px wide per source char → 8 screen cols

# Screen codes for the "on" and "off" pixels in each scene mode.
SC_BLANK = SCREEN.SC_SPACE  # space — invisible against the background
SC_ON_PETSCII = SCREEN.SC_FULL_BLOCK  # inverse-space / solid block, standard ROM
SC_ON_MCM = 0xFF  # all 4 sub-pixels = FG in MCM's 2×2 charset

# Within VIC bank 0: the strip is written to whichever page the VIC is not
# displaying, then $D018 flips to it.
SCREEN_PAGE_ADDRS = (0x0400, 0x0C00)
# D018 hi-nibble = screen address / $400; low nibble (bits 1-3 = 010) =
# charset at $1000 (standard ROM). $14 → screen=$0400, $34 → screen=$0C00.
D018_PAGE_VALUES = (0x14, 0x34)

# $C000-$C01F is free: the audio NMI routine starts at $C020 (audio_handlers).
# A/X/Y are saved and restored by the kernal IRQ entry at $FF48 → $EA81, so this
# routine needs no stack save of its own.
IRQ_HANDLER_ADDR = 0xC000
SHADOW_D016_ADDR = 0xC100
SHADOW_D018_ADDR = 0xC101
RASTER_IRQ_HANDLER = bytes(
    [
        0xAD,
        0x00,
        0xC1,  # LDA $C100   ; shadow D016
        0x8D,
        0x16,
        0xD0,  # STA $D016
        0xAD,
        0x01,
        0xC1,  # LDA $C101   ; shadow D018
        0x8D,
        0x18,
        0xD0,  # STA $D018
        0xA9,
        0x01,  # LDA #$01
        0x8D,
        0x19,
        0xD0,  # STA $D019   ; ack raster IRQ
        0x4C,
        0x31,
        0xEA,  # JMP $EA31   ; chain to kernal (kbd scan + jiffy)
    ]
)
RASTER_IRQ_LINE = RASTER_VBLANK_LINE  # line 248 — first line past the last badline

_VALID_ROWS = ("top", "middle", "bottom")
_VALID_MSG_KEYS = {"text", "color"}

# The SHIFT-driven color cycle's first stop is "no override"; the rest override
# every message with rainbow or a fixed spectrum color.
_CONFIG_COLOR_SENTINEL = -2  # internal: use msg._resolved_color
_RAINBOW_SENTINEL = -1  # matches msg._resolved_color rainbow
# In C64_SPECTRUM_INDICES order, so the cycle advances like the rainbow scroller.
_SPECTRUM_NAME_FOR_INDEX = {v: k for k, v in C64_COLORS.items()}
COLOR_CYCLE: tuple[int, ...] = (
    _CONFIG_COLOR_SENTINEL,
    _RAINBOW_SENTINEL,
    *(int(i) for i in C64_SPECTRUM_INDICES),
)
COLOR_CYCLE_LABELS: tuple[str, ...] = (
    "config",
    "rainbow",
    *(_SPECTRUM_NAME_FOR_INDEX[int(i)] for i in C64_SPECTRUM_INDICES),
)


@dataclass
class BigTextMessage:
    text: str
    color: str = "white"  # C64 color name | "rainbow" | "random"
    _resolved_color: int = field(default=-1, init=False)


def _resolve_color(name: str) -> int:
    """Color name → palette index 0..15. 'rainbow' → -1 sentinel (per-cell
    rotation handled at render time). 'random' picks once from the spectrum."""
    if name == "rainbow":
        return -1
    if name == "random":
        return int(random.choice(C64_SPECTRUM_INDICES))
    try:
        return resolve_color(name)
    except ValueError:
        raise ValueError(
            f"big_text: unknown color {name!r}. Use a C64 color name, 'rainbow', or 'random'."
        ) from None


@register("big_text")
class BigTextOverlay(Overlay):
    """Classic-demo-style horizontally-scrolling big text.

    Source PETSCII characters expand 1 source-pixel → 1 screen-cell, so
    every glyph fills an 8×8 footprint. Scrolls smoothly via cell-aligned
    screen updates + VIC hardware X-scroll for sub-pixel motion.
    Restricted to `blank` and `mcm` scenes.
    """

    PAINTS_INTO_BUFFERS = True
    COMPATIBLE_MODES = ("blank", "mcm")
    HELP = "Demo-scene 8×-scaled horizontally-scrolling big text (blank/mcm only)."
    PARAM_HELP = {
        "messages": "List of message strings (or {text, color} tables) to scroll.",
        "charset_path": "C64 character ROM used to rasterize the big glyphs "
        "(unset = the one c64cast dumped off your C64; see --dump-char-rom).",
        "row": "Vertical placement: 'top', 'middle', or 'bottom'.",
        "speed_cells_per_s": "Scroll speed in character cells per second.",
        "inter_message_pause_s": "Pause between consecutive messages.",
        "loop": "Loop the message list forever (false = play once then advance).",
        "target_fps": "Override FPS used for px-per-frame snapping; unset = detect.",
    }

    def __init__(
        self,
        messages: list,
        *,
        charset_path: str | None = None,
        row: str = "middle",
        speed_cells_per_s: float = 8.0,
        inter_message_pause_s: float = 1.5,
        loop: bool = True,
        target_fps: float | None = None,
    ):
        if not messages:
            raise ValueError("big_text: messages must be non-empty")
        if row not in _VALID_ROWS:
            raise ValueError(f"big_text: row must be one of {_VALID_ROWS}, got {row!r}")
        self.row = row
        self.speed_cells_per_s = float(speed_cells_per_s)
        # One source pixel becomes one screen cell (8 screen px). The
        # *requested* speed: setup() snaps it to integer px/frame.
        self.speed_px_per_s = 8.0 * self.speed_cells_per_s
        self.inter_message_pause_s = float(inter_message_pause_s)
        self.loop = bool(loop)
        # None = detect from the scene at setup(), with 50.0 (PAL) as fallback.
        self.target_fps = float(target_fps) if target_fps else None

        self.messages: list[BigTextMessage] = []
        for m in messages:
            if isinstance(m, BigTextMessage):
                msg = m
            elif isinstance(m, dict):
                unknown = set(m) - _VALID_MSG_KEYS
                if unknown:
                    raise ValueError(
                        f"big_text: unknown message keys {sorted(unknown)} "
                        f"(allowed: {sorted(_VALID_MSG_KEYS)})"
                    )
                if "text" not in m:
                    raise ValueError(f"big_text: message missing 'text': {m!r}")
                msg = BigTextMessage(**m)
            else:
                raise ValueError(f"big_text: bad message {m!r}")
            msg._resolved_color = _resolve_color(msg.color)
            self.messages.append(msg)

        # 2 KB PETSCII ROM (or cv2 fallback), read for each source char's 8×8
        # bitmap when expanding into the cell grid.
        self._charset = self._load_charset(charset_path)

        # Per-message (8, n*8) bool array, one bit per source pixel. Lazy.
        self._mask_cache: dict[int, np.ndarray] = {}

        self._msg_idx = -1
        # When the message becomes visible, after any inter-message pause.
        self._msg_start_t = 0.0
        self._scroll_frame = 0  # frames elapsed inside the active scroll
        self.start_time = 0.0
        # Ensemble mode: stamped onto the scene before setup() by the Playlist
        # (followers) or cli (conductors). None in single-system or local mode.
        self._orchestrator: Any = None
        self._is_conductor: bool = False
        self._system_index: int = 0
        # The last message bits were pushed for, so the conductor republishes
        # only when the active message changes.
        self._published_msg_idx: int = -1

        # Stashed in setup(): compose() is otherwise buffer-only, but the
        # smooth scroll needs the shadow X-scroll byte at $C100 written every
        # frame for the raster IRQ handler to commit.
        self._api = None
        self._last_xscroll_byte = -1
        self._next_page = 1  # 0 = $0400, 1 = $0C00
        self._last_coarse_x_px = None  # last frame's cell-snapped scroll
        self._px_per_frame = 1
        # setup() may drop the scene's BG color from this, so no rainbow column
        # paints invisibly against the background.
        self._rainbow_spectrum = C64_SPECTRUM_INDICES
        # Index into COLOR_CYCLE; 0 = no override. cycle_style() advances it.
        self._color_cycle_idx = 0

    @staticmethod
    def _load_charset(path: str | None) -> bytes:
        """The 2 KB charset this scroller expands into cells. `path` overrides
        the automatic resolution (a dumped ROM under the data dir, else the cv2
        fallback) — see :mod:`c64cast.hw.char_rom`."""
        from c64cast.hw.char_rom import load_glyphs

        return load_glyphs(path)

    @staticmethod
    def _scene_is_mcm(scene) -> bool:
        return getattr(scene.display_mode, "name", "") == "mcm"

    def _glyph_bits(self, msg_idx: int) -> np.ndarray:
        """(8, n*8) bool array of every source pixel in the message,
        concatenated. row=glyph y (0..7), col=glyph x across all N chars."""
        cached = self._mask_cache.get(msg_idx)
        if cached is not None:
            return cached
        text = self.messages[msg_idx].text
        codes = ascii_to_screen(text)
        n = len(codes)
        if n == 0:
            bits = np.zeros((GLYPH_CELL_H, 0), dtype=bool)
            self._mask_cache[msg_idx] = bits
            return bits
        out = np.empty((GLYPH_CELL_H, n * GLYPH_CELL_W), dtype=np.uint8)
        for i, code in enumerate(codes):
            glyph = np.frombuffer(self._charset[code * 8 : code * 8 + 8], dtype=np.uint8)
            # (8, 1) → (8, 8): MSB is the leftmost pixel.
            out[:, i * 8 : i * 8 + 8] = np.unpackbits(glyph[:, None], axis=1)
        bits = out.astype(bool)
        self._mask_cache[msg_idx] = bits
        return bits

    def setup(self, api, scene):
        self.start_time = time.time()
        self._msg_idx = -1
        self._msg_start_t = self.start_time
        self._scroll_frame = 0
        self._api = api
        self._last_xscroll_byte = -1
        self._last_coarse_x_px = None
        self._next_page = 1

        # _color_cycle_idx is not reset here: the same overlay instance
        # survives single-scene loop iterations and pause/resume, and its
        # cycled style persists across them like the display mode's does.
        def _num(x):
            return x if isinstance(x, int | float) and x > 0 else None

        fps = _num(self.target_fps) or _num(getattr(scene, "target_fps", None))
        if fps is None:
            dm = getattr(scene, "display_mode", None)
            fps = _num(getattr(dm, "default_target_fps", None)) if dm else None
        if fps is None:
            fps = 50.0
        self._px_per_frame = max(1, int(round(self.speed_px_per_s / fps)))
        log.info(
            "big_text: %.1f px/s requested -> %d px/frame @ %.0f fps (actual %.1f px/s)",
            self.speed_px_per_s,
            self._px_per_frame,
            fps,
            self._px_per_frame * fps,
        )
        # Only blank scenes expose a static BG to filter the rainbow spectrum
        # against; MCM picks bg0 per frame, so there it stays the full spectrum.
        bg = getattr(scene.display_mode, "background", None)
        if isinstance(bg, int):
            filtered = C64_SPECTRUM_INDICES[bg != C64_SPECTRUM_INDICES]
            if filtered.size:
                self._rainbow_spectrum = filtered
        # Both screen pages start all SC_SPACE so non-strip cells stay blank
        # whichever is displayed; BlankDisplayMode's own push() covers $0400.
        # Page 0 is displayed first, so the first cell-shift writes page 1.
        if not self._scene_is_mcm(scene):
            api.write_memory_file("0C00", bytes([SC_BLANK] * 1000))
            self._install_raster_irq(api)
            self._last_xscroll_byte = 0x08

        # Read via __dict__ so a MagicMock scene does not look stamped: its
        # __getattr__ makes a child mock on demand without writing __dict__,
        # where a real `scene._orchestrator = orch` stamp does.
        scene_dict = getattr(scene, "__dict__", {}) or {}
        self._orchestrator = scene_dict.get("_orchestrator")
        self._is_conductor = scene_dict.get("_is_conductor", False)
        self._system_index = scene_dict.get("_system_index", 0)
        self._published_msg_idx = -1
        if self._orchestrator is not None and self._is_conductor:
            # Promote to message 0 here so its bits are published BEFORE
            # orch.begin() fires the follower interrupts; otherwise a follower's
            # snapshot() finds bits = None and paints nothing until the
            # conductor's first compose has run.
            if self.messages:
                self._msg_idx = 0
                self._msg_start_t = self.start_time
                self._scroll_frame = 0
                self._publish_current_message()
            scene_cfg = scene_dict.get("_cfg")
            if scene_cfg is not None:
                self._orchestrator.begin(scene_cfg)

    def teardown(self, api, scene):
        if not self._scene_is_mcm(scene):
            self._uninstall_raster_irq(api)
            # Standard screen at $0400, 40-column mode, X-scroll = 0 — one
            # coalesced write, so the next scene never sees a half restore.
            api.write_regs("d016", 0x08, 0x00, 0x14)
        # Releases the followers when the conductor's scene tears down mid
        # broadcast (a CTRL skip, a stop_event). end() is idempotent.
        if self._orchestrator is not None and self._is_conductor and self._orchestrator.is_active():
            self._orchestrator.end()
        self._orchestrator = None
        self._api = None

    def _install_raster_irq(self, api):
        """Bring up the shadow-register raster IRQ.

        Ordering is what keeps this safe — we must never leave the system
        with $0314/$0315 half-updated and an IRQ source live, or the next
        IRQ will JMP through a torn vector and crash.
        """
        api.write_memory_file(f"{IRQ_HANDLER_ADDR:04X}", RASTER_IRQ_HANDLER)
        api.write_regs(f"{SHADOW_D016_ADDR:04X}", 0x08, D018_PAGE_VALUES[0])
        # Mask every CIA #1 IRQ source so the kernal jiffy IRQ cannot fire while
        # $0314 changes. Timer A keeps running — only the interrupt line is
        # blocked — and the raster handler chains to $EA31 below.
        api.write_memory("DC0D", "7F")
        # VIC IRQ sources off too: nothing can fire until the enable below.
        api.write_memory("D01A", "00")
        # One coalesced PUT, so the two-byte vector lands as a single DMA
        # transaction with no torn-vector window.
        api.write_regs("0314", IRQ_HANDLER_ADDR & 0xFF, (IRQ_HANDLER_ADDR >> 8) & 0xFF)
        # Raster compare at VBLANK. $D011 = $1B is the kernal default, whose
        # bit 7 = 0 keeps the compare line below 256.
        api.write_memory("D012", f"{RASTER_IRQ_LINE:02X}")
        api.write_memory("D011", "1B")
        # Ack any pending raster IRQ, then enable: the handler now fires once
        # per frame at line 248.
        api.write_memory("D019", "01")
        api.write_memory("D01A", "01")

    def _uninstall_raster_irq(self, api):
        """Tear down in the reverse order of install. Each step keeps the
        IRQ environment self-consistent so any IRQ that fires mid-teardown
        lands somewhere sane."""
        # Raster IRQ off first, so it cannot fire after the vector is restored.
        api.write_memory("D01A", "00")
        # Back to the kernal default ($EA31); with the raster and CIA #1 IRQs
        # both masked, no source is live.
        api.write_regs("0314", KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
        # Ack any pending raster IRQ before re-enabling CIA #1.
        api.write_memory("D019", "01")
        # Kernal jiffy / keyboard scan resumes via the restored $0314.
        api.write_memory("DC0D", "81")

    def is_busy(self) -> bool:
        # A conductor keeps the scene running until the message has scrolled off
        # the leftmost system: the orchestrator's follower-window math needs
        # abs_scroll_px to keep being published past this screen.
        if self._orchestrator is not None and self._is_conductor and self._orchestrator.is_active():
            return True
        # A looping message queue is effectively infinite, so busy-defer would
        # stop the scene from ever advancing; duration_s decides instead.
        if self.loop:
            return False
        return self._msg_idx < len(self.messages)

    def _publish_current_message(self) -> None:
        """Push the active message's glyph bits + color settings to the
        orchestrator. Called from setup() for message 0 and from compose()
        whenever _msg_idx changes (so the conductor publishes once per
        message, not once per frame). No-op if not in conductor mode."""
        if self._orchestrator is None or not self._is_conductor:
            return
        if self._msg_idx < 0 or self._msg_idx >= len(self.messages):
            return
        if self._published_msg_idx == self._msg_idx:
            return
        bits = self._glyph_bits(self._msg_idx)
        msg = self.messages[self._msg_idx]
        color = self._active_color(msg)
        rainbow = color == _RAINBOW_SENTINEL
        self._orchestrator.publish_bits(
            bits=bits, color=color, rainbow=rainbow, px_per_frame=self._px_per_frame
        )
        self._published_msg_idx = self._msg_idx

    def _active_color(self, msg: BigTextMessage) -> int:
        """Resolve the FG color for this paint. -2 sentinel in the cycle
        means "use the message's configured color"; everything else
        overrides every message regardless of its own resolved color."""
        cycle_val = COLOR_CYCLE[self._color_cycle_idx]
        if cycle_val == _CONFIG_COLOR_SENTINEL:
            return msg._resolved_color
        return cycle_val

    def cycle_style(self, api, scene) -> str | None:
        """Rotate the active color through COLOR_CYCLE.

        Initial state is index 0 = use configured per-message color. Each
        SHIFT press advances mod len(COLOR_CYCLE), wrapping back to "config"
        once the spectrum exhausts. Color RAM is rewritten on the next
        compose(); an immediate write would race the in-flight strip update.
        """
        self._color_cycle_idx = (self._color_cycle_idx + 1) % len(COLOR_CYCLE)
        return COLOR_CYCLE_LABELS[self._color_cycle_idx]

    def _advance_message(self, t: float) -> None:
        next_idx = self._msg_idx + 1
        if self.loop and next_idx >= len(self.messages):
            next_idx = 0
        self._msg_idx = next_idx
        self._msg_start_t = t + self.inter_message_pause_s
        self._scroll_frame = 0

    def _top_cell_row(self) -> int:
        """Top cell row of the 8-row-tall glyph strip."""
        if self.row == "top":
            return 2
        if self.row == "bottom":
            return SCREEN_H_CELLS - GLYPH_CELL_H - 2
        return (SCREEN_H_CELLS - GLYPH_CELL_H) // 2

    def compose(self, buffers: dict, scene, t: float) -> None:
        # A follower renders the orchestrator's slice of the conductor's
        # message and ignores its own messages list.
        if self._orchestrator is not None and not self._is_conductor:
            self._compose_follower(buffers, scene)
            return

        if self._msg_idx < 0:
            self._msg_idx = 0
            self._msg_start_t = t
            self._scroll_frame = 0
        if self._msg_idx >= len(self.messages):
            return
        msg = self.messages[self._msg_idx]
        if t < self._msg_start_t:
            # Inter-message pause: the screen holds blank until the next
            # message comes in from the right.
            return

        # A conductor publishes the active message's bits before rendering its
        # first frame, whether it arrived here from setup() or a pause.
        self._publish_current_message()

        bits = self._glyph_bits(self._msg_idx)
        n_src_px = bits.shape[1]
        if n_src_px == 0:
            self._advance_message(t)
            return

        # Frame-counted, so the per-frame delta is *exactly* _px_per_frame;
        # wall-clock int() truncation gives uneven steps even at a steady frame
        # rate, and the eye reads that as jerk.
        x_left_px = SCREEN_W_PX - self._scroll_frame * self._px_per_frame
        self._scroll_frame += 1

        # Locally the message ends when it has scrolled off this screen; in
        # conductor (span) mode it has to clear the *leftmost* system, so the
        # orchestrator's end_threshold_px decides and abs_scroll_px keeps being
        # published past this screen for the followers to render against.
        abs_scroll_px = self._scroll_frame * self._px_per_frame
        if self._orchestrator is not None and self._is_conductor:
            self._orchestrator.advance(abs_scroll_px)
            end_threshold = getattr(self._orchestrator, "end_threshold_px", 0)
            if end_threshold > 0 and abs_scroll_px >= end_threshold:
                self._advance_message(t)
                # Past the last message in non-loop mode the broadcast is done:
                # release every follower to resume its saved scene.
                if self._msg_idx >= len(self.messages):
                    self._orchestrator.end()
                return
        else:
            if x_left_px <= -n_src_px * 8:
                self._advance_message(t)
                return

        self._render_at(bits, x_left_px, self._active_color(msg), scene, buffers)

    def _compose_follower(self, buffers: dict, scene) -> None:
        """Follower-mode render: read state from the orchestrator and
        paint this system's slice of the global content. The follower's
        own `messages` / `loop` / `speed` / `color` settings are ignored
        — those are owned by the conductor. The message and its color
        (including the rainbow sentinel) flow through `snap["color"]`,
        so a conductor configured with `color = "rainbow"` paints rainbow
        on every follower screen too. The follower's own local rainbow
        spectrum is still honored at render time (so each follower can
        filter its own background color out of the spectrum)."""
        orch = self._orchestrator
        if orch is None or not orch.is_active():
            return
        snap = orch.snapshot()
        bits = snap.get("bits")
        if bits is None:
            return
        active_color = snap.get("color", 0)
        local_x_left_px = orch.local_x_left_px(self._system_index, snap.get("abs_scroll_px", 0))
        self._render_at(bits, local_x_left_px, active_color, scene, buffers)

    def _render_at(
        self, bits: np.ndarray, x_left_px: int, active_color: int, scene, buffers: dict
    ) -> None:
        """Paint glyph `bits` so its leftmost source pixel sits at
        `x_left_px` of this screen (negative = scrolled past the left
        edge; > SCREEN_W_PX = off the right edge).

        Pure render — does not advance `_scroll_frame`, pick a message,
        or check end-of-scroll. The conductor's compose() above handles
        position math + advance; the follower path (`_compose_follower`
        above) calls this directly with bits + x_left_px derived from
        the orchestrator snapshot.

        `active_color` is an FG color index 0..15, or `_RAINBOW_SENTINEL`
        to color each column with a different palette entry."""
        n_src_px = bits.shape[1]

        # The sub-cell remainder goes to the hardware X-scroll register, which
        # is what makes the motion pixel-smooth.
        coarse_x_px = (x_left_px // 8) * 8
        sub_x = x_left_px - coarse_x_px  # 0..7
        leftmost_cell = coarse_x_px // 8  # cell column of the first source px

        in_mcm = self._scene_is_mcm(scene)
        xscroll_byte = 0x08 | (sub_x & 0x07)  # 40-col + X-scroll bits

        # For each visible screen cell c, the source pixel at column
        # (c - leftmost_cell).
        cell_block = np.zeros((GLYPH_CELL_H, SCREEN_W_CELLS), dtype=bool)
        cols = np.arange(SCREEN_W_CELLS)
        src_cols = cols - leftmost_cell
        visible = (src_cols >= 0) & (src_cols < n_src_px)
        if visible.any():
            v = np.where(visible)[0]
            cell_block[:, v] = bits[:, src_cols[v]]

        rainbow = active_color == _RAINBOW_SENTINEL
        fg_color = active_color if not rainbow else 0
        top = self._top_cell_row()

        # The ENTIRE 8-row strip takes the FG color, not just the "on" cells:
        # color RAM then stays constant across a message's scroll frames and the
        # write_region diff cache absorbs it between message changes.
        color_buf = buffers["color"]
        if rainbow:
            spec = self._rainbow_spectrum
            col_colors = spec[np.arange(SCREEN_W_CELLS) % len(spec)]
        else:
            col_colors = np.full(SCREEN_W_CELLS, fg_color, dtype=np.uint8)
        if in_mcm:
            strip_row_colors = ((col_colors & 0x07) | 0x08).astype(np.uint8)
        else:
            strip_row_colors = (col_colors & 0x0F).astype(np.uint8)
        strip_start = top * SCREEN_W_CELLS
        strip_end = (top + GLYPH_CELL_H) * SCREEN_W_CELLS
        color_buf[strip_start:strip_end] = np.tile(strip_row_colors, GLYPH_CELL_H)

        if in_mcm:
            # MCM has its own per-scene auto-uploaded charset at $3000 and does
            # not page-flip: mutate the buffer and let the scene's push() carry
            # it.
            screen_buf = buffers["screen"]
            on_rows, on_cols = np.where(cell_block)
            cell_indices = (top + on_rows) * SCREEN_W_CELLS + on_cols
            screen_buf[cell_indices] = SC_ON_MCM
            return

        # Blank mode page-flips the strip's screen RAM via the shadow $D018,
        # committed by the raster IRQ during VBLANK, so the VIC never observes a
        # half-updated strip. buffers["screen"] is left alone: BlankDisplayMode's
        # push() then finds no change against its diff cache and writes nothing
        # to $0400, leaving the offscreen strip uploads as the only screen
        # writes.
        if self._api is None:
            return

        cell_shifted = coarse_x_px != self._last_coarse_x_px

        if cell_shifted:
            self._last_coarse_x_px = coarse_x_px
            strip = np.full((GLYPH_CELL_H, SCREEN_W_CELLS), SC_BLANK, dtype=np.uint8)
            strip[cell_block] = SC_ON_PETSCII
            offscreen_addr = SCREEN_PAGE_ADDRS[self._next_page] + top * SCREEN_W_CELLS
            self._api.write_memory_file(f"{offscreen_addr:04X}", strip.tobytes())
            # Both shadow bytes in one coalesced PUT, so the raster IRQ commits
            # the new fine-scroll and the page-flip on the *same* frame.
            self._api.write_regs(
                f"{SHADOW_D016_ADDR:04X}",
                xscroll_byte,
                D018_PAGE_VALUES[self._next_page],
            )
            self._last_xscroll_byte = xscroll_byte
            self._next_page ^= 1
        elif xscroll_byte != self._last_xscroll_byte:
            # Sub-cell motion only: the raster IRQ commits the shadow $D016.
            self._api.write_memory(f"{SHADOW_D016_ADDR:04X}", f"{xscroll_byte:02x}")
            self._last_xscroll_byte = xscroll_byte
