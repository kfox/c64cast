"""Polls the U64 for keyboard modifier state and drives pause/resume/skip/cycle.

The kernal IRQ scans the keyboard at 60 Hz and writes the modifier-key
state to $028D:
  bit 0  SHIFT
  bit 1  COMMODORE
  bit 2  CONTROL

We poll $028D over the HTTP read endpoint every `poll_interval_s`
(default 100 ms = 10 Hz) and translate edge transitions into thread
events the Playlist's run loop watches:

  * COMMODORE pressed (running)   → pause_event
  * COMMODORE held `hold_threshold_s` while paused → resume_event
  * CONTROL pressed (running)     → skip_event   (advance to next interstitial)
  * CONTROL anything while paused → no-op (deliberate; the user's UI
    contract is "pause means only the resume-hold can do anything")
  * SHIFT pressed (running)       → cycle_event  (rotate display style)
  * SHIFT anything while paused   → no-op (same UI contract)

Chord rule: SHIFT is dropped on any tick where C= or CTRL is also held,
so a user reaching for pause/skip with a thumb on shift doesn't get a
phantom cycle. C= + CTRL still prefers pause over skip.

When the on-C64 menu is wired (`menu_event`/`menu_active`/`nav_queue` passed
to `start`), we additionally read the current-key matrix code at $00CB:
  * SPACE pressed              → menu_event (toggle the menu open/closed)
  * while `menu_active` is set  → the entire pause/skip/cycle branch is
    suspended (so SHIFT becomes a navigation modifier, not a cycle trigger);
    every non-SPACE key edge is pushed onto `nav_queue` as `(code, shift)`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from ._pollthread import PollThread
from .backend import C64Backend
from .c64 import KEY, SCREEN

ADDR_MODIFIERS = SCREEN.MODIFIERS
ADDR_CUR_KEY = SCREEN.CUR_KEY
BIT_SHIFT = 0x01
BIT_COMMODORE = 0x02
BIT_CONTROL = 0x04


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
        # Per-instance logger so ensemble runs can tell which system a
        # given press came from. Child of the existing c64cast.keyboard
        # logger, so assertLogs("c64cast.keyboard", ...) in tests still
        # matches via the logging hierarchy.
        self.log = logging.getLogger(f"c64cast.keyboard.{name}")
        self.poll_interval_s = poll_interval_s
        self.hold_threshold_s = hold_threshold_s
        self._poll = PollThread(self._loop, name="cbm-key-poll", manual=True, join_timeout=1.0)
        self._pause_event: threading.Event | None = None
        self._resume_event: threading.Event | None = None
        self._skip_event: threading.Event | None = None
        self._cycle_event: threading.Event | None = None
        self._menu_event: threading.Event | None = None
        self._menu_active: threading.Event | None = None
        self._nav_queue: deque[tuple[int, bool]] | None = None

    def start(
        self,
        pause_event: threading.Event,
        resume_event: threading.Event,
        skip_event: threading.Event | None = None,
        cycle_event: threading.Event | None = None,
        menu_event: threading.Event | None = None,
        menu_active: threading.Event | None = None,
        nav_queue: deque[tuple[int, bool]] | None = None,
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
                      $00CB is never read and SPACE/nav are ignored entirely
                      (keeps the read load to $028D-only).
        menu_active   (optional) set by the Playlist while the menu overlay is
                      open. While set, the whole pause/skip/cycle branch is
                      suspended and non-SPACE key edges are enqueued for nav.
        nav_queue     (optional) bounded deque the poller appends `(matrix
                      code, shift_held)` tuples to while `menu_active`."""
        self._pause_event = pause_event
        self._resume_event = resume_event
        self._skip_event = skip_event
        self._cycle_event = cycle_event
        self._menu_event = menu_event
        self._menu_active = menu_active
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

    def _read_key(self) -> int | None:
        """Read $00CB (matrix code of the key currently down, 64 = none).
        None on read failure, distinguished from KEY.NONE."""
        data = self.api.read_memory(ADDR_CUR_KEY, 1)
        if data is None or len(data) < 1:
            return None
        return data[0]

    def _loop(self, stop: threading.Event):
        assert self._pause_event is not None
        assert self._resume_event is not None
        held_since: float | None = None
        last_cbm_seen = False  # edge detect for pause trigger
        last_ctrl_seen = False  # edge detect for skip trigger
        last_shift_seen = False  # edge detect for cycle trigger
        last_key = KEY.NONE  # edge detect for SPACE/nav (only when menu wired)

        while not stop.wait(self.poll_interval_s):
            mod = self._read_modifiers()
            if mod is None:
                # Read failed — keep prior state, try again next tick.
                continue
            cbm = bool(mod & BIT_COMMODORE)
            ctrl = bool(mod & BIT_CONTROL)
            shift = bool(mod & BIT_SHIFT)

            # Current-key edge detection — only when the menu is wired, so the
            # read load stays $028D-only for non-menu runs (and the existing
            # FakeApi in tests, which only serves $028D, keeps working).
            key_edge: int | None = None
            if self._menu_event is not None:
                key = self._read_key()
                if key is not None:
                    if key != KEY.NONE and key != last_key:
                        key_edge = key
                    last_key = key

            if self._menu_active is not None and self._menu_active.is_set():
                # MENU OPEN: suspend pause/skip/cycle entirely. SPACE toggles
                # the menu closed; every other key edge is a nav event carrying
                # the live SHIFT state (SHIFT = "reverse" modifier).
                if key_edge == KEY.SPACE and self._menu_event is not None:
                    self._menu_event.set()
                elif key_edge is not None and self._nav_queue is not None:
                    self._nav_queue.append((key_edge, shift))
                # Keep the modifier edge baselines current so a SHIFT/C=/CTRL
                # held across the menu session can't fire a phantom event the
                # tick the menu closes.
                last_cbm_seen = cbm
                last_ctrl_seen = ctrl
                last_shift_seen = shift
                held_since = None
                continue

            if (
                key_edge == KEY.SPACE
                and self._menu_event is not None
                and not self._pause_event.is_set()
            ):
                # SPACE while running opens the menu. (While paused the scene is
                # torn down to the BASIC screen, so a menu would be meaningless;
                # the run loop is blocked in _handle_pause and wouldn't see it.)
                self.log.info("SPACE press detected — opening menu")
                self._menu_event.set()

            if self._pause_event.is_set():
                # PAUSED: only the C= hold-to-resume gesture matters.
                # CTRL and SHIFT are explicitly ignored per the UI contract.
                if cbm:
                    if held_since is None:
                        held_since = time.monotonic()
                    elif time.monotonic() - held_since >= self.hold_threshold_s:
                        self.log.info(
                            "C= held %.1fs while paused — resuming", self.hold_threshold_s
                        )
                        self._resume_event.set()
                        held_since = None
                else:
                    held_since = None
                last_cbm_seen = cbm
                last_ctrl_seen = ctrl
                last_shift_seen = shift
            else:
                # RUNNING: edge-trigger C= press → pause, CTRL press → skip,
                # SHIFT press → cycle. Chord rules:
                #   C= + CTRL same tick → pause wins, skip dropped (user is
                #     trying to freeze the scene; don't skip past it).
                #   SHIFT held with C= or CTRL → SHIFT dropped (user reaching
                #     for pause/skip with thumb on shift shouldn't phantom-
                #     cycle the style).
                cbm_edge = cbm and not last_cbm_seen
                ctrl_edge = ctrl and not last_ctrl_seen
                shift_edge = shift and not last_shift_seen
                if cbm_edge:
                    self.log.info("C= press detected — pausing")
                    self._pause_event.set()
                elif ctrl_edge and self._skip_event is not None:
                    self.log.info("CTRL press detected — skipping to next scene")
                    self._skip_event.set()
                elif shift_edge and not cbm and not ctrl and self._cycle_event is not None:
                    self.log.info("SHIFT press detected — cycling display style")
                    self._cycle_event.set()
                last_cbm_seen = cbm
                last_ctrl_seen = ctrl
                last_shift_seen = shift
                held_since = None
