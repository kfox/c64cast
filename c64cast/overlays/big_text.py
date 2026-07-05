"""Classic 1×→8× demo-scene scroller.

Each source PETSCII character (8×8 pixels) is expanded so that every
pixel of the source glyph becomes one solid-block character on the C64
screen: an "on" source pixel maps to SC=$A0 (reverse space) in the
chosen FG color; an "off" pixel stays as SC=$20 / background. A single
source char therefore fills an 8×8 cell footprint, and the rendered
glyph reads as the actual ROM letter, scaled 8× on each axis — the
"chunky bitmap" look that demo-scene scrollers have used since the
mid-80s. See https://codebase64.net/doku.php?id=base:8x_scale_charset_scrolling_message
for the canonical 6502 implementation.

Smooth horizontal scrolling combines four pieces:

1. **Integer pixels per frame.** Motion is driven by a frame counter,
   not wall clock. At setup() the requested speed (`speed_cells_per_s`)
   is snapped to the nearest integer number of screen pixels per frame
   given the scene's target FPS. The text then advances by that exact
   amount each compose() call. The classic trap is `x = int(t * v)` —
   if `v / fps` is not integer, the per-frame delta alternates (e.g.
   1, 2, 1, 1, 2 px) and the eye reads the unevenness as jerk. Frame-
   counted motion is uniform by construction.
2. **Cell-aligned coarse scroll.** Each frame we compute the message's
   leftmost source-pixel screen position rounded DOWN to the nearest
   cell boundary, and write the resulting 8×40 cell strip. Cell content
   only changes when the scroll has crossed an 8-px cell boundary, so
   the screen-RAM upload is mostly a no-op between cell shifts.
3. **Hardware fine X-scroll.** The sub-cell remainder (0–7 px) is
   pushed to VIC-II register $D016 bits 0–2 each frame. The VIC then
   translates the entire display by that many pixels — giving us
   pixel-by-pixel motion without rewriting any character RAM.
4. **Raster-IRQ-driven commit of D016/D018.** The VIC reads $D016
   per scan line and $D018 to find the screen matrix. An HTTP write
   to either lands at an arbitrary moment relative to the raster
   scan, so a mid-frame change paints the top rows with the old value
   and the bottom rows with the new — a visible horizontal tear at
   that scan line. To fix this we never write $D016/$D018 directly:
   compose() writes "shadow" bytes at $C100/$C101 instead, and a
   tiny 6502 raster IRQ handler installed at $C000 copies those into
   $D016/$D018 during VBLANK (raster line 248) once per frame. The
   real VIC regs change only when no visible scan line is reading
   them, so tearing is physically impossible regardless of when the
   HTTP write lands. The handler chains to $EA31 so keyboard scan
   and jiffy clock keep ticking. This is the canonical demoscene
   shadow-register trick — see
   https://codebase64.net/doku.php?id=base:8x_scale_charset_scrolling_message
   for the spirit of the original.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..c64 import KERNAL, RASTER_VBLANK_LINE, SCREEN
from ..palette import C64_COLORS, C64_SPECTRUM_INDICES, resolve_color
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

# Page-flipping addresses (within VIC bank 0). When updating the strip,
# we always write to the page that the VIC is NOT currently displaying,
# then atomically flip D018 to make our new page the displayed one. This
# eliminates the screen-RAM tearing that the U64 HTTP API otherwise
# produces during the multi-byte strip rewrite — the VIC just sees the
# old page until the moment of the D018 flip, then the new one.
SCREEN_PAGE_ADDRS = (0x0400, 0x0C00)
# D018 hi-nibble = screen address / $400; low nibble (bits 1-3 = 010) =
# charset at $1000 (standard ROM). $14 → screen=$0400, $34 → screen=$0C00.
D018_PAGE_VALUES = (0x14, 0x34)

# Raster-IRQ commit handler. Lives at $C000 (the audio NMI lives at
# $C020+ — see audio.py — so $C000-$C01F is free). On every raster IRQ
# at line 248 (top of VBLANK), this routine copies the shadow bytes at
# $C100/$C101 into $D016/$D018, acks the VIC IRQ, and JMPs to the
# kernal default IRQ handler at $EA31 so the keyboard scan + jiffy
# clock still tick. A/X/Y are saved/restored by the kernal IRQ entry
# at $FF48 → $EA81, so this routine doesn't need its own stack save.
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
RASTER_IRQ_LINE = RASTER_VBLANK_LINE  # line 248 — first VBLANK line on PAL/NTSC

_VALID_ROWS = ("top", "middle", "bottom")
_VALID_MSG_KEYS = {"text", "color"}

# SHIFT-driven color cycle. The first stop is the sentinel "no override"
# (each message keeps the color it was configured with); subsequent stops
# override every message with rainbow or a fixed spectrum color. Picked
# stays in effect for every message painted by this overlay until the
# scene tears down (overlays are reconstructed per scene from config, so
# the next scene's big_text starts fresh at "no override").
_CONFIG_COLOR_SENTINEL = -2  # internal: use msg._resolved_color
_RAINBOW_SENTINEL = -1  # matches msg._resolved_color rainbow
# Build the spectrum portion of the cycle from C64_SPECTRUM_INDICES so
# the order matches the rainbow scroller — predictable advancement.
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
    every glyph fills an 8×8 footprint. Scrolls smoothly via cell-
    aligned screen updates + VIC hardware X-scroll for sub-pixel
    motion. Restricted to `blank` and `mcm` scenes — bitmap modes
    don't expose the character matrix at all, and PETSCII scenes have
    their own per-frame char rendering that would fight the scroller.
    """

    PAINTS_INTO_BUFFERS = True
    COMPATIBLE_MODES = ("blank", "mcm")
    HELP = "Demo-scene 8×-scaled horizontally-scrolling big text (blank/mcm only)."
    PARAM_HELP = {
        "messages": "List of message strings (or {text, color} tables) to scroll.",
        "charset_path": "C64 character ROM used to rasterize the big glyphs.",
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
        charset_path: str = "assets/roms/characters.901225-01.bin",
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
        # Source-pixel-per-second = screen-px-per-second since we expand
        # 1 source pixel to 1 screen cell (= 8 screen px). Stored as the
        # *requested* speed; setup() snaps it to integer px/frame using
        # the scene's target FPS, and from there motion is frame-counted.
        self.speed_px_per_s = 8.0 * self.speed_cells_per_s
        self.inter_message_pause_s = float(inter_message_pause_s)
        self.loop = bool(loop)
        # Explicit FPS override for px-per-frame snapping. None = detect
        # from scene at setup(), with 50.0 (PAL) as fallback.
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

        # 2KB PETSCII ROM (or cv2 fallback). Used to look up each source
        # char's 8×8 bitmap when expanding into the cell grid.
        self._charset = self._load_charset(charset_path)

        # Per-message (8, n*8) bool array — one bit per source pixel,
        # row=glyph y, col=glyph x across the whole message. Lazy.
        self._mask_cache: dict[int, np.ndarray] = {}

        # State: which message is currently active and the frame counter
        # within its scroll. _msg_start_t marks the wall-clock time when
        # the message becomes visible (after any inter-message pause).
        self._msg_idx = -1
        self._msg_start_t = 0.0
        self._scroll_frame = 0  # frames elapsed inside the active scroll
        self.start_time = 0.0
        # Ensemble / orchestration mode. None in single-system or local
        # mode (today's behaviour); set by Playlist (for followers) or
        # cli (for conductors) in ensemble mode at scene setup time via
        # the scene._orchestrator / scene._is_conductor stamps.
        self._orchestrator: Any = None
        self._is_conductor: bool = False
        self._system_index: int = 0
        # Tracks the last message we pushed bits for so the conductor
        # only republishes when the active message changes.
        self._published_msg_idx: int = -1

        # Stashed in setup() so compose() can update the shadow X-scroll
        # byte ($C100) every frame. compose() is normally buffer-only,
        # but the smooth-scroll only works if D016 changes once per
        # frame; we drive that via the raster IRQ handler reading from
        # the shadow address we update here.
        self._api = None
        self._last_xscroll_byte = -1
        # Page-flipping state for blank-mode rendering. We write the
        # strip to the "next" page each cell-shift, then flip D018 to
        # display it — the previously-displayed page becomes the next
        # write target. Eliminates VIC scan-line tearing on the
        # 320-byte strip upload.
        self._next_page = 1  # 0 = $0400, 1 = $0C00
        self._last_coarse_x_px = None  # last frame's cell-snapped scroll
        # Snapped at setup() from target_fps + requested speed.
        self._px_per_frame = 1
        # Spectrum used for rainbow coloring; setup() may filter out the
        # scene's BG color so a "rainbow" column never matches the background
        # (which would render that band of text invisible).
        self._rainbow_spectrum = C64_SPECTRUM_INDICES
        # SHIFT-driven color cycle index into COLOR_CYCLE. 0 = no override
        # (each message uses its configured color). cycle_style() advances.
        self._color_cycle_idx = 0

    # ---- charset / glyphs --------------------------------------------------

    @staticmethod
    def _load_charset(path: str) -> bytes:
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read(2048)
            if len(data) >= 2048:
                return data
            log.warning("big_text: charset %s shorter than 2KB; using builtin", path)
        from ..framebuffer import _builtin_charset

        return _builtin_charset()

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
        # Unpack the 8-byte glyph for each source code into 8 rows of 8 bits.
        out = np.empty((GLYPH_CELL_H, n * GLYPH_CELL_W), dtype=np.uint8)
        for i, code in enumerate(codes):
            glyph = np.frombuffer(self._charset[code * 8 : code * 8 + 8], dtype=np.uint8)
            # (8, 1) → (8, 8): MSB is the leftmost pixel.
            out[:, i * 8 : i * 8 + 8] = np.unpackbits(glyph[:, None], axis=1)
        bits = out.astype(bool)
        self._mask_cache[msg_idx] = bits
        return bits

    # ---- lifecycle ---------------------------------------------------------

    def setup(self, api, scene):
        self.start_time = time.time()
        self._msg_idx = -1
        self._msg_start_t = self.start_time
        self._scroll_frame = 0
        self._api = api
        self._last_xscroll_byte = -1
        self._last_coarse_x_px = None
        self._next_page = 1

        # NB: _color_cycle_idx deliberately NOT reset here — overlays
        # survive single-scene loop iterations on the same instance, and
        # cycled style persists across loops + pause/resume (same
        # contract the display_mode follows). A real scene change
        # constructs a fresh overlay from config, which resets via
        # __init__.
        # Pixel-per-frame snap: take the scene's target FPS (or 50.0
        # fallback) and round speed_px_per_s / fps to the nearest
        # integer. Motion below is `frame * _px_per_frame`, so this
        # determines actual on-screen speed.
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
        # Filter the scene's static background color out of the rainbow
        # spectrum so no rainbow column ever paints invisibly against the
        # BG. Only blank scenes expose a static BG; MCM picks bg0 per
        # frame from the source frame so there is nothing to filter against
        # there and we fall back to the full spectrum.
        bg = getattr(scene.display_mode, "background", None)
        if isinstance(bg, int):
            filtered = C64_SPECTRUM_INDICES[bg != C64_SPECTRUM_INDICES]
            if filtered.size:
                self._rainbow_spectrum = filtered
        # Init both screen pages to all SC_SPACE so non-strip cells stay
        # blank no matter which page is displayed. BlankDisplayMode
        # initializes $0400 via its own push(); we initialize $0C00.
        # Display page 0 ($0400) initially; first cell-shift will write
        # to page 1 and update the shadow $D018 byte to flip to it.
        if not self._scene_is_mcm(scene):
            api.write_memory_file("0C00", bytes([SC_BLANK] * 1000))
            self._install_raster_irq(api)
            self._last_xscroll_byte = 0x08

        # Ensemble-mode hookup: the playlist (followers) or cli wrapper
        # (conductor) stamps these on the scene before setup runs. Read
        # via __dict__ so MagicMock-auto-attribute scenes in unit tests
        # don't accidentally look like they've been stamped (MagicMock's
        # __getattr__ creates a child mock on demand without writing to
        # __dict__; real scene-stamps via `scene._orchestrator = orch`
        # DO write to __dict__).
        scene_dict = getattr(scene, "__dict__", {}) or {}
        self._orchestrator = scene_dict.get("_orchestrator")
        self._is_conductor = scene_dict.get("_is_conductor", False)
        self._system_index = scene_dict.get("_system_index", 0)
        self._published_msg_idx = -1
        if self._orchestrator is not None and self._is_conductor:
            # Promote to message 0 here so its bits get published BEFORE
            # orch.begin() fires the follower interrupts. Without this
            # the followers would race to call snapshot() and find bits
            # = None (paint nothing) until the conductor's first compose
            # ran.
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
            # Restore canonical VIC state: standard screen at $0400,
            # 40-column mode, X-scroll = 0. Same coalesced write so the
            # next scene doesn't briefly see a half-restored state.
            api.write_regs("d016", 0x08, 0x00, 0x14)
        # Defensive: if the conductor's scene tears down while the
        # broadcast is still active (CTRL skip mid-scroll, stop_event
        # mid-message, etc.) make sure followers get released. end()
        # is idempotent so a no-op when already ended.
        if self._orchestrator is not None and self._is_conductor and self._orchestrator.is_active():
            self._orchestrator.end()
        self._orchestrator = None
        self._api = None

    # ---- raster IRQ install / uninstall -----------------------------------

    def _install_raster_irq(self, api):
        """Bring up the shadow-register raster IRQ.

        Ordering is what keeps this safe — we must never leave the system
        with $0314/$0315 half-updated and an IRQ source live, or the next
        IRQ will JMP through a torn vector and crash.
        """
        # 1) Upload handler and initialize shadow regs.
        api.write_memory_file(f"{IRQ_HANDLER_ADDR:04X}", RASTER_IRQ_HANDLER)
        api.write_regs(f"{SHADOW_D016_ADDR:04X}", 0x08, D018_PAGE_VALUES[0])
        # 2) Mask all CIA #1 IRQ sources so the kernal jiffy IRQ can't fire
        #    while we change $0314. (CIA #1 timer A keeps running; we just
        #    block the interrupt line. Our raster IRQ will chain to $EA31
        #    later, so jiffy/keyboard still tick at 50/60 Hz.)
        api.write_memory("DC0D", "7F")
        # 3) Disable VIC IRQ sources too — belt and suspenders. No IRQ
        #    source can fire from here until step 6.
        api.write_memory("D01A", "00")
        # 4) Hook the IRQ vector. Single coalesced PUT — the two-byte
        #    vector lands as one DMA transaction so there's no torn-vector
        #    window even if some IRQ were somehow live.
        api.write_regs("0314", IRQ_HANDLER_ADDR & 0xFF, (IRQ_HANDLER_ADDR >> 8) & 0xFF)
        # 5) Program the raster compare register to VBLANK (line 248).
        #    $D011 = $1B is the kernal default (bit 7 = 0 keeps line < 256).
        api.write_memory("D012", f"{RASTER_IRQ_LINE:02X}")
        api.write_memory("D011", "1B")
        # 6) Ack any pending raster IRQ, then enable. From here our
        #    handler fires once per frame at line 248.
        api.write_memory("D019", "01")
        api.write_memory("D01A", "01")

    def _uninstall_raster_irq(self, api):
        """Tear down in the reverse order of install. Each step keeps the
        IRQ environment self-consistent so any IRQ that fires mid-teardown
        lands somewhere sane."""
        # 1) Disable VIC raster IRQ first so it can't fire after we
        #    restore the vector.
        api.write_memory("D01A", "00")
        # 2) Restore IRQ vector to the kernal default ($EA31). With both
        #    raster IRQ and CIA #1 IRQ masked, no IRQ source is live.
        api.write_regs("0314", KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF)
        # 3) Ack any pending raster IRQ before re-enabling CIA #1.
        api.write_memory("D019", "01")
        # 4) Re-enable CIA #1 timer A IRQ — kernal jiffy / keyboard scan
        #    resumes via $EA31 (which is now back in $0314).
        api.write_memory("DC0D", "81")

    def is_busy(self) -> bool:
        # As conductor of an active ensemble broadcast we always need
        # the scene to keep running so the message can finish scrolling
        # off the leftmost system — the orchestrator's
        # follower-window math depends on us continuing to publish
        # abs_scroll_px past our own local screen.
        if self._orchestrator is not None and self._is_conductor and self._orchestrator.is_active():
            return True
        # When looping, the message queue is effectively infinite — busy-defer
        # would prevent the scene from EVER advancing, so let the scene's
        # duration_s be the source of truth instead.
        if self.loop:
            return False
        return self._msg_idx < len(self.messages)

    # ---- conductor publishing ---------------------------------------------

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

    # ---- SHIFT-driven color cycling ---------------------------------------

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
        SHIFT press advances mod len(COLOR_CYCLE) — so after the spectrum
        exhausts you wrap back to "config" and the per-message setup
        becomes active again. Color RAM is rewritten on the next compose()
        when the strip is repainted; no immediate api write is needed
        (and forcing one would race the existing in-flight strip update).
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

    # ---- positioning -------------------------------------------------------

    def _top_cell_row(self) -> int:
        """Top cell row of the 8-row-tall glyph strip."""
        if self.row == "top":
            return 2
        if self.row == "bottom":
            return SCREEN_H_CELLS - GLYPH_CELL_H - 2
        return (SCREEN_H_CELLS - GLYPH_CELL_H) // 2

    # ---- compose -----------------------------------------------------------

    def compose(self, buffers: dict, scene, t: float) -> None:
        # Follower in an ensemble broadcast — render the orchestrator's
        # slice of the conductor's message; ignore our own messages list.
        if self._orchestrator is not None and not self._is_conductor:
            self._compose_follower(buffers, scene)
            return

        # Promote to message 0 on first call.
        if self._msg_idx < 0:
            self._msg_idx = 0
            self._msg_start_t = t
            self._scroll_frame = 0
        if self._msg_idx >= len(self.messages):
            return
        msg = self.messages[self._msg_idx]
        if t < self._msg_start_t:
            # Inter-message pause: the previous message has scrolled off
            # and we're holding the screen blank for inter_message_pause_s
            # before the next one comes in from the right.
            return

        # Conductor in an ensemble broadcast — make sure the active
        # message's bits are published before we render the first frame
        # of it (covers both the initial msg-0 promote-via-_advance_message
        # case after the inter-message pause and the setup() pre-publish).
        self._publish_current_message()

        bits = self._glyph_bits(self._msg_idx)
        n_src_px = bits.shape[1]
        if n_src_px == 0:
            self._advance_message(t)
            return

        # Position in screen pixels of the message's leftmost source pixel.
        # Frame-counted, so the per-frame delta is *exactly* _px_per_frame.
        # That is the difference between "smooth" and "jerky" — wall-clock
        # int() truncation gives uneven steps even on a steady frame rate.
        x_left_px = SCREEN_W_PX - self._scroll_frame * self._px_per_frame
        self._scroll_frame += 1

        # End condition. In local mode the message is done when it has
        # scrolled off this single screen. In conductor (span) mode the
        # message has to scroll all the way off the *leftmost* system,
        # which is way past this screen — defer to the orchestrator's
        # end_threshold_px and keep publishing abs_scroll_px past our
        # own off-screen position so the followers can keep rendering.
        abs_scroll_px = self._scroll_frame * self._px_per_frame
        if self._orchestrator is not None and self._is_conductor:
            self._orchestrator.advance(abs_scroll_px)
            end_threshold = getattr(self._orchestrator, "end_threshold_px", 0)
            if end_threshold > 0 and abs_scroll_px >= end_threshold:
                self._advance_message(t)
                # If we wrapped past the last message in non-loop mode,
                # the broadcast is done — release every follower so
                # they can resume their saved scenes.
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
        position math + advance; the follower overlay in BigTextSpan
        mode (see commit 14) calls this directly with bits + x_left_px
        derived from the orchestrator snapshot.

        `active_color` is an FG color index 0..15, or `_RAINBOW_SENTINEL`
        to color each column with a different palette entry."""
        n_src_px = bits.shape[1]

        # Cell-snap the render position; push the sub-cell remainder to
        # the hardware X-scroll register for pixel-smooth motion.
        coarse_x_px = (x_left_px // 8) * 8
        sub_x = x_left_px - coarse_x_px  # 0..7
        leftmost_cell = coarse_x_px // 8  # screen cell column where the
        # message's first source pixel
        # lands

        in_mcm = self._scene_is_mcm(scene)
        xscroll_byte = 0x08 | (sub_x & 0x07)  # 40-col + X-scroll bits

        # Build the (8, 40) cell pattern: for each visible screen cell c,
        # look up the source pixel at column (c - leftmost_cell).
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

        # ---- Color RAM (shared $D800, no page-flip) -----------------------
        # Fill the ENTIRE 8-row strip with the message's FG color (per-
        # column in rainbow mode) — not just "on" cells. That keeps color
        # RAM constant across scroll frames within a single message; the
        # write_region diff cache then silently absorbs color RAM uploads
        # entirely between message changes (no color/screen race).
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

        # ---- Screen RAM --------------------------------------------------
        if in_mcm:
            # MCM has its own per-scene auto-uploaded charset at $3000
            # and we don't try to page-flip here (the existing approach
            # of mutating the buffer + the scene's push() works fine for
            # the relatively rare MCM big_text use).
            screen_buf = buffers["screen"]
            on_rows, on_cols = np.where(cell_block)
            cell_indices = (top + on_rows) * SCREEN_W_CELLS + on_cols
            screen_buf[cell_indices] = SC_ON_MCM
            return

        # Blank mode: page-flip the strip's screen RAM via shadow $D018.
        # We deliberately do NOT mutate buffers["screen"] — BlankDisplayMode's
        # push() then sees no change vs. its diff cache and writes nothing to
        # $0400, so the only screen-RAM writes are our own targeted offscreen
        # strip uploads. The raster IRQ commits the new $D018 during VBLANK,
        # so the VIC never observes a half-updated strip.
        if self._api is None:
            return

        cell_shifted = coarse_x_px != self._last_coarse_x_px

        if cell_shifted:
            self._last_coarse_x_px = coarse_x_px
            strip = np.full((GLYPH_CELL_H, SCREEN_W_CELLS), SC_BLANK, dtype=np.uint8)
            strip[cell_block] = SC_ON_PETSCII
            offscreen_addr = SCREEN_PAGE_ADDRS[self._next_page] + top * SCREEN_W_CELLS
            self._api.write_memory_file(f"{offscreen_addr:04X}", strip.tobytes())
            # Update both shadow bytes in one coalesced PUT. The raster
            # IRQ at line 248 will commit them together during VBLANK,
            # so the new fine-scroll and the page-flip become visible on
            # the *same* frame with no tear at any scan line.
            self._api.write_regs(
                f"{SHADOW_D016_ADDR:04X}",
                xscroll_byte,
                D018_PAGE_VALUES[self._next_page],
            )
            self._last_xscroll_byte = xscroll_byte
            self._next_page ^= 1
        elif xscroll_byte != self._last_xscroll_byte:
            # Sub-cell motion only — update the shadow $D016 byte. The
            # raster IRQ at line 248 commits it during VBLANK.
            self._api.write_memory(f"{SHADOW_D016_ADDR:04X}", f"{xscroll_byte:02x}")
            self._last_xscroll_byte = xscroll_byte
