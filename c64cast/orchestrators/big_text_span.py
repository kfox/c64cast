"""Cross-ensemble big_text scroll — the span pattern.

When the *rightmost* system's playlist enters a scene with a `big_text`
overlay and `orchestrate = true`, every other system is interrupted
and renders a slice of the same message as if all N screens formed one
320·N-pixel canvas. The message scrolls right-to-left, entering on the
rightmost system, exiting off the leftmost.

This module ships the orchestrator subclass. The big_text overlay's
conductor + follower hooks — which drive `publish_bits`/`advance` from
the rightmost system's render path, and consume `snapshot()` from the
followers' — live in `c64cast/overlays/big_text.py`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import numpy as np

from ..ensemble import Ensemble
from ..orchestrator import Orchestrator, OrchestratorError, register_orchestrator
from ..overlays.big_text import SCREEN_W_PX

if TYPE_CHECKING:
    from ..config import SceneCfg


@register_orchestrator
class BigTextSpanOrchestrator(Orchestrator):
    """Span-mode orchestrator: the conductor's message scrolls across
    all N screens (rightmost → leftmost). Each follower computes its
    local 320-pixel window from the published `abs_scroll_px` and the
    follower's index in the left-to-right system order."""

    def __init__(self, ensemble: Ensemble, conductor_name: str):
        super().__init__(ensemble, conductor_name)
        # Per-broadcast state guarded by its own lock so snapshot reads
        # don't contend with the base class's _lock (which guards
        # begin/end). Allocated in __init__ (not _on_begin) because
        # the conductor's big_text overlay calls publish_bits BEFORE
        # begin to ensure followers see populated state the moment
        # their interrupt event fires.
        self._state_lock = threading.Lock()
        self._abs_scroll_px = 0
        self._bits: np.ndarray | None = None
        self._color = 0
        self._rainbow = False
        self._px_per_frame = 1
        self._screen_w_px = SCREEN_W_PX

    @classmethod
    def claims(cls, scene_cfg: SceneCfg) -> bool:
        """A blank or mcm scene with at least one big_text overlay."""
        if scene_cfg.type not in ("blank", "mcm"):
            return False
        return any(o.get("type") == "big_text" for o in scene_cfg.overlays)

    def _on_begin(self, cfg: SceneCfg) -> None:
        # The rightmost system *must* be the conductor because the
        # message enters from the right edge of that screen. If the
        # user puts orchestrate=true on a non-rightmost system, fail
        # cleanly so the conductor's playlist can fall back to local
        # rendering (begin() reraises out of the playlist's caller).
        rightmost_name = self.ensemble.stacks[-1].name
        if self.conductor_name != rightmost_name:
            raise OrchestratorError(
                f"big_text span: conductor must be the rightmost system "
                f"({rightmost_name!r}), got {self.conductor_name!r}. "
                "Move the orchestrate=true scene to the rightmost "
                "per-system TOML."
            )
        # Reset only the scroll counter — `bits` is set by publish_bits
        # which the conductor's big_text overlay calls BEFORE begin() so
        # followers see populated state the moment their interrupt
        # event fires. Clearing it here would clobber that publish.
        # _on_end is what clears bits at the end of a broadcast.
        with self._state_lock:
            self._abs_scroll_px = 0

    def _on_end(self) -> None:
        # Allow the GC to reclaim the bits array immediately. The
        # follower scenes will be torn down on resume; any in-flight
        # snapshot() reads see the cleared state and render nothing.
        with self._state_lock:
            self._bits = None

    def publish_bits(
        self, *, bits: np.ndarray, color: int, rainbow: bool, px_per_frame: int
    ) -> None:
        """Conductor → orchestrator: install the per-message render
        inputs. Called from the rightmost system's big_text setup()
        once per message; followers consume them via snapshot().

        `bits` is the (8, n_src_px) bool glyph array. `color` is the
        FG color index or _RAINBOW_SENTINEL. `rainbow` is True iff color
        is _RAINBOW_SENTINEL (cached separately so followers don't have
        to know the sentinel value). `px_per_frame` is the per-frame
        scroll step the conductor's animation uses."""
        with self._state_lock:
            self._bits = bits
            self._color = color
            self._rainbow = rainbow
            self._px_per_frame = px_per_frame

    def advance(self, abs_scroll_px: int) -> None:
        """Conductor → orchestrator: publish the latest absolute scroll
        position. Called once per conductor frame, after its local
        render. The integer reflects "pixels scrolled past the
        rightmost system's right edge" — at 0 the message is just
        about to enter the rightmost screen; at
        `end_threshold_px` it has scrolled fully off the leftmost
        screen and the broadcast can end."""
        with self._state_lock:
            self._abs_scroll_px = abs_scroll_px

    def snapshot(self) -> dict[str, Any]:
        """Follower → orchestrator: read the current broadcast state.
        Returns a fresh dict each call (no aliasing) so followers can
        compute their local x_left_px and color without holding the
        state lock past the call."""
        with self._state_lock:
            return {
                "abs_scroll_px": self._abs_scroll_px,
                "bits": self._bits,
                "color": self._color,
                "rainbow": self._rainbow,
                "px_per_frame": self._px_per_frame,
                "screen_w_px": self._screen_w_px,
            }

    @property
    def end_threshold_px(self) -> int:
        """Total scroll distance for the message's leading edge to
        leave the leftmost screen. The conductor checks this each frame
        to decide when to advance to the next message (or end the
        broadcast on the last message)."""
        with self._state_lock:
            if self._bits is None:
                return 0
            n_src_px = self._bits.shape[1]
            n = len(self.ensemble.stacks)
            return self._screen_w_px * n + n_src_px * 8

    def local_x_left_px(self, follower_index: int, abs_scroll_px: int | None = None) -> int:
        """Compute a follower's local `x_left_px` given its left-to-
        right index (0 = leftmost, N-1 = rightmost) and the current
        abs_scroll_px (defaults to the orchestrator's latest published
        value). Pure function — exposed for unit tests + the follower
        overlay's compose path."""
        if abs_scroll_px is None:
            abs_scroll_px = self._abs_scroll_px
        n = len(self.ensemble.stacks)
        systems_to_my_right = (n - 1) - follower_index
        return self._screen_w_px * (1 + systems_to_my_right) - abs_scroll_px
