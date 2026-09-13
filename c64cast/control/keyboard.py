"""Polls the C64's keyboard modifier state and drives pause/resume/skip/cycle.

The kernal IRQ scans the keyboard at 60 Hz and writes the modifier bits to
$028D (:data:`BIT_SHIFT` / :data:`BIT_COMMODORE` / :data:`BIT_CONTROL`).
:class:`CommodoreKeyPoller` reads it every ``poll_interval_s`` and turns edge
transitions into the thread events the Playlist's run loop watches.

With the on-C64 menu wired it additionally drains the kernal keyboard buffer —
NDX ($00C6) + KEYD ($0277), the same buffer the U64's CMD_KEYB opcode injects
into — so SPACE toggles the menu and cursor/RETURN codes drive it.

See docs/architecture/control.md#keyboardpy--commodore-key-pauseresume-ctrl-key-skip-shift-key-style-cycle.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

from c64cast._pollthread import PollThread
from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import KEYBUF, SCREEN

ADDR_MODIFIERS = SCREEN.MODIFIERS
ADDR_KB_BUFFER_LEN = SCREEN.KB_BUFFER_LEN
ADDR_KB_BUFFER = SCREEN.KB_BUFFER
_KB_BUFFER_LEN_HEX = f"{SCREEN.KB_BUFFER_LEN:04X}"
_KB_BUFFER_MAX = 10  # KEYD is 10 bytes; clamp a bogus NDX read
BIT_SHIFT = 0x01
BIT_COMMODORE = 0x02
BIT_CONTROL = 0x04

# Cursor + RETURN codes that drive menu navigation. SPACE is the open/close
# toggle, handled separately and never enqueued.
_NAV_CODES = frozenset(
    {KEYBUF.CRSR_DOWN, KEYBUF.CRSR_UP, KEYBUF.CRSR_RIGHT, KEYBUF.CRSR_LEFT, KEYBUF.RETURN}
)
# Debounce for SPACE→toggle: the kernal repeats a held SPACE.
_SPACE_COOLDOWN_S = 0.4


@dataclass
class _KeyPollState:
    """Mutable per-tick state of the key poll loop (one per _loop run).
    Mirrors vision._GestureState — the two pollers share this shape."""

    held_since: float | None = None  # C= hold-to-resume timer
    last_cbm_seen: bool = False
    last_ctrl_seen: bool = False
    last_shift_seen: bool = False
    last_space_toggle: float = 0.0  # monotonic time of the last SPACE menu toggle

    def set_baselines(self, cbm: bool, ctrl: bool, shift: bool) -> None:
        self.last_cbm_seen = cbm
        self.last_ctrl_seen = ctrl
        self.last_shift_seen = shift


class CommodoreKeyPoller:
    def __init__(
        self,
        api: C64Backend,
        poll_interval_s: float = 0.1,
        hold_threshold_s: float = 3.0,
        name: str = "system",
    ):
        self.api = api
        self.name = name
        # Per-instance logger so ensemble runs can tell which system a press
        # came from. A child of `c64cast.control.keyboard`, so the tests'
        # assertLogs on that name still matches via the logging hierarchy.
        self.log = logging.getLogger(f"c64cast.control.keyboard.{name}")
        self.poll_interval_s = poll_interval_s
        self.hold_threshold_s = hold_threshold_s
        self._poll = PollThread(self._loop, name="cbm-key-poll", manual=True, join_timeout=1.0)
        self._pause_event: threading.Event | None = None
        self._resume_event: threading.Event | None = None
        self._skip_event: threading.Event | None = None
        self._cycle_event: threading.Event | None = None
        self._menu_event: threading.Event | None = None
        self._menu_active: threading.Event | None = None
        self._menu_eligible: threading.Event | None = None
        self._nav_queue: deque[int] | None = None

    def start(
        self,
        pause_event: threading.Event,
        resume_event: threading.Event,
        skip_event: threading.Event | None = None,
        cycle_event: threading.Event | None = None,
        menu_event: threading.Event | None = None,
        menu_active: threading.Event | None = None,
        menu_eligible: threading.Event | None = None,
        nav_queue: deque[int] | None = None,
    ):
        """Begin polling.

        pause_event   set when C= is pressed during normal play.
        resume_event  set when C= is held `hold_threshold_s` while paused.
        skip_event    (optional) set when CTRL is pressed during normal
                      play, signaling the playlist to advance. Ignored
                      while paused. If None, CTRL is silently dropped.
        cycle_event   (optional) set when SHIFT is pressed during normal
                      play, signaling the playlist to cycle the current
                      scene's display style. Ignored while paused, and
                      ignored on any tick where C= or CTRL is also held.
                      If None, SHIFT is silently dropped.
        menu_event    (optional) toggled when SPACE is pressed — opens the
                      on-C64 menu when running, closes it when open. If None,
                      the keyboard buffer is never read and SPACE/nav are
                      ignored entirely (keeps the read load to $028D-only).
        menu_active   (optional) set by the Playlist while the menu overlay is
                      open. While set, the whole pause/skip/cycle branch is
                      suspended and cursor/RETURN codes are enqueued for nav.
        menu_eligible (optional) set by the Playlist when the current scene can
                      host the menu. Gates buffer access: the poller only
                      drains/clears NDX ($00C6) when the menu is open or this
                      is set, so a kernal-input launcher's own $00C6 watch is
                      never disturbed.
        nav_queue     (optional) bounded deque the poller appends decoded
                      PETSCII cursor/RETURN codes to while `menu_active`."""
        self._pause_event = pause_event
        self._resume_event = resume_event
        self._skip_event = skip_event
        self._cycle_event = cycle_event
        self._menu_event = menu_event
        self._menu_active = menu_active
        self._menu_eligible = menu_eligible
        self._nav_queue = nav_queue
        self._poll.start()

    def stop(self):
        self._poll.stop()

    def _read_modifiers(self) -> int | None:
        """Read $028D, return the raw modifier byte or None on read failure.

        We return None (rather than 0) so the caller can distinguish
        'no modifiers pressed' from 'unable to tell'. A failed read
        shouldn't accidentally trigger any state change."""
        data = self.api.read_memory(ADDR_MODIFIERS, 1)
        if data is None or len(data) < 1:
            return None
        return data[0]

    def _drain_kbbuf(self) -> list[int]:
        """Read and consume the kernal keyboard buffer.

        Reads NDX ($00C6); if non-zero, reads that many decoded PETSCII codes
        from KEYD ($0277…) and zeroes NDX to consume them — the BASIC clear
        loop never GETINs them itself, so without the clear we'd re-process
        the same codes every tick. Returns the codes in arrival order; an
        empty list on no keys OR any read failure (the None-on-failure guard
        means a dropped read never fabricates input). On a buffer read failure
        NDX is left intact so the keystrokes survive to the next tick."""
        ndx = self.api.read_memory(ADDR_KB_BUFFER_LEN, 1)
        if ndx is None or len(ndx) < 1:
            return []
        count = ndx[0]
        if count == 0:
            return []
        count = min(count, _KB_BUFFER_MAX)
        data = self.api.read_memory(ADDR_KB_BUFFER, count)
        if data is None or len(data) < count:
            return []
        self.api.write_memory(_KB_BUFFER_LEN_HEX, "00")
        return list(data[:count])

    def _loop(self, stop: threading.Event):
        assert self._pause_event is not None
        assert self._resume_event is not None
        state = _KeyPollState()
        while not stop.wait(self.poll_interval_s):
            self._tick(state)

    def _tick(self, st: _KeyPollState) -> None:
        """One poll tick: read $028D, drain the kernal key buffer when the
        menu wants it, then dispatch to the menu-open / paused / running
        handler. A failed read keeps prior state (try again next tick)."""
        assert self._pause_event is not None
        mod = self._read_modifiers()
        if mod is None:
            return
        cbm = bool(mod & BIT_COMMODORE)
        ctrl = bool(mod & BIT_CONTROL)
        shift = bool(mod & BIT_SHIFT)

        # Only when the menu is wired AND open or the scene is eligible: the
        # drain zeroes $00C6, which must not happen under a kernal-input
        # launcher scene that watches it itself.
        menu_open = self._menu_active is not None and self._menu_active.is_set()
        eligible = self._menu_eligible is not None and self._menu_eligible.is_set()
        keys: list[int] = []
        if self._menu_event is not None and (menu_open or eligible):
            keys = self._drain_kbbuf()

        if menu_open:
            self._tick_menu_open(st, keys, cbm, ctrl, shift)
            return
        self._maybe_open_menu(st, keys)
        if self._pause_event.is_set():
            self._tick_paused(st, cbm, ctrl, shift)
        else:
            self._tick_running(st, cbm, ctrl, shift)

    def _tick_menu_open(
        self, st: _KeyPollState, keys: list[int], cbm: bool, ctrl: bool, shift: bool
    ) -> None:
        """MENU OPEN: suspend pause/skip/cycle entirely. SPACE toggles the
        menu closed (debounced); cursor/RETURN codes are nav events. The
        kernal already folded SHIFT into the cursor codes (CRSR-up/left =
        $91/$9D), so direction rides on the code."""
        now = time.monotonic()
        for code in keys:
            if code == KEYBUF.SPACE and self._menu_event is not None:
                if now - st.last_space_toggle >= _SPACE_COOLDOWN_S:
                    self._menu_event.set()
                    st.last_space_toggle = now
            elif code in _NAV_CODES and self._nav_queue is not None:
                self._nav_queue.append(code)
        # Keep the modifier edge baselines current so a SHIFT/C=/CTRL held
        # across the menu session can't fire a phantom event as it closes.
        st.set_baselines(cbm, ctrl, shift)
        st.held_since = None

    def _maybe_open_menu(self, st: _KeyPollState, keys: list[int]) -> None:
        """SPACE while running opens the menu. (While paused the scene is
        torn down to the BASIC screen, so a menu would be meaningless;
        the run loop is blocked in _handle_pause and wouldn't see it.)"""
        assert self._pause_event is not None
        if not (
            keys
            and KEYBUF.SPACE in keys
            and self._menu_event is not None
            and not self._pause_event.is_set()
        ):
            return
        now = time.monotonic()
        if now - st.last_space_toggle >= _SPACE_COOLDOWN_S:
            self.log.info("SPACE press detected — opening menu")
            self._menu_event.set()
            st.last_space_toggle = now

    def _tick_paused(self, st: _KeyPollState, cbm: bool, ctrl: bool, shift: bool) -> None:
        """PAUSED: only the C= hold-to-resume gesture matters. CTRL and SHIFT
        are explicitly ignored per the UI contract."""
        assert self._resume_event is not None
        if cbm:
            if st.held_since is None:
                st.held_since = time.monotonic()
            elif time.monotonic() - st.held_since >= self.hold_threshold_s:
                self.log.info("C= held %.1fs while paused — resuming", self.hold_threshold_s)
                self._resume_event.set()
                st.held_since = None
        else:
            st.held_since = None
        st.set_baselines(cbm, ctrl, shift)

    def _tick_running(self, st: _KeyPollState, cbm: bool, ctrl: bool, shift: bool) -> None:
        """RUNNING: edge-trigger C= press → pause, CTRL press → skip, SHIFT
        press → cycle. Chord rules:
          C= + CTRL same tick → pause wins, skip dropped (user is
            trying to freeze the scene; don't skip past it).
          SHIFT held with C= or CTRL → SHIFT dropped (user reaching
            for pause/skip with thumb on shift shouldn't phantom-
            cycle the style)."""
        assert self._pause_event is not None
        cbm_edge = cbm and not st.last_cbm_seen
        ctrl_edge = ctrl and not st.last_ctrl_seen
        shift_edge = shift and not st.last_shift_seen
        if cbm_edge:
            self.log.info("C= press detected — pausing")
            self._pause_event.set()
        elif ctrl_edge and self._skip_event is not None:
            self.log.info("CTRL press detected — skipping to next scene")
            self._skip_event.set()
        elif shift_edge and not cbm and not ctrl and self._cycle_event is not None:
            self.log.info("SHIFT press detected — cycling display style")
            self._cycle_event.set()
        st.set_baselines(cbm, ctrl, shift)
        st.held_since = None
