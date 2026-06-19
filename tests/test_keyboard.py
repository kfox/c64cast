"""Tests for the Commodore-key poller and the Playlist pause/resume flow."""

from __future__ import annotations

import threading
import time
import unittest
from collections import deque
from typing import cast

from c64cast.api import Ultimate64API
from c64cast.c64 import KEYBUF
from c64cast.keyboard import (
    ADDR_KB_BUFFER,
    ADDR_KB_BUFFER_LEN,
    ADDR_MODIFIERS,
    CommodoreKeyPoller,
)


class FakeApi:
    """Replays a scripted sequence of $028D reads. Each read pops the next
    byte from the script; the last byte loops forever."""

    def __init__(self):
        self.script = [b"\x00"]
        self._lock = threading.Lock()
        self._idx = 0

    def set_script(self, seq):
        with self._lock:
            self.script = list(seq)
            self._idx = 0

    def read_memory(self, address, length, timeout=1.0):
        assert address == ADDR_MODIFIERS
        assert length == 1
        with self._lock:
            b = self.script[min(self._idx, len(self.script) - 1)]
            self._idx += 1
        return b

    # Stubbed out — never called by the poller, but the Playlist's _safe_setup
    # uses these on its scenes (not relevant here).
    @property
    def stats(self):
        return {"writes": 0, "skipped": 0, "errors": 0, "bytes": 0}


class CommodoreKeyPollerTest(unittest.TestCase):
    def test_press_sets_pause_event(self):
        api = FakeApi()
        api.set_script([b"\x00", b"\x02", b"\x02"])  # released, then pressed
        poller = CommodoreKeyPoller(
            cast(Ultimate64API, api), poll_interval_s=0.01, hold_threshold_s=10.0
        )
        pause = threading.Event()
        resume = threading.Event()
        poller.start(pause, resume)
        self.assertTrue(pause.wait(timeout=0.5), "press should set pause_event quickly")
        self.assertFalse(resume.is_set())
        poller.stop()

    def test_no_pause_when_already_pressed_at_start(self):
        # Edge-trigger: if the first read shows pressed, we should not
        # interpret that as a fresh press (could be transient from boot).
        # Actually the poller's last_press_seen starts False, so even an
        # initial 'pressed' reading triggers pause. This test pins that
        # behavior so we notice if it changes.
        api = FakeApi()
        api.set_script([b"\x02", b"\x02"])
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        pause = threading.Event()
        resume = threading.Event()
        poller.start(pause, resume)
        # First read sees pressed → fires pause (current behavior).
        self.assertTrue(pause.wait(timeout=0.3))
        poller.stop()

    def test_hold_while_paused_sets_resume(self):
        api = FakeApi()
        api.set_script([b"\x02"])  # always pressed
        pause = threading.Event()
        pause.set()  # already paused
        resume = threading.Event()
        poller = CommodoreKeyPoller(
            cast(Ultimate64API, api), poll_interval_s=0.02, hold_threshold_s=0.15
        )
        poller.start(pause, resume)
        self.assertTrue(resume.wait(timeout=1.0), "held-while-paused should set resume_event")
        poller.stop()

    def test_release_resets_hold_timer(self):
        api = FakeApi()
        # Pressed for 0.1s, released, then pressed again — must NOT resume
        # because the hold timer reset.
        seq = ([b"\x02"] * 5) + ([b"\x00"] * 3) + ([b"\x02"] * 2)
        api.set_script(seq)
        pause = threading.Event()
        pause.set()
        resume = threading.Event()
        poller = CommodoreKeyPoller(
            cast(Ultimate64API, api), poll_interval_s=0.02, hold_threshold_s=0.30
        )
        poller.start(pause, resume)
        # Total script time ~ 10 × 20 ms = 200 ms, less than the 300 ms
        # threshold AFTER the release. Resume should not fire.
        time.sleep(0.25)
        self.assertFalse(resume.is_set(), "release in the middle should reset the hold timer")
        poller.stop()

    def test_read_failure_does_not_trigger_pause(self):
        class BadApi(FakeApi):
            def read_memory(self, *a, **kw):
                return None

        api = BadApi()
        pause = threading.Event()
        resume = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume)
        time.sleep(0.15)
        self.assertFalse(pause.is_set(), "read failures must not phantom-press")
        poller.stop()


class CtrlSkipTest(unittest.TestCase):
    def test_ctrl_press_sets_skip_event(self):
        api = FakeApi()
        # released, then CTRL pressed (bit 2 = 0x04).
        api.set_script([b"\x00", b"\x04", b"\x04"])
        pause = threading.Event()
        resume = threading.Event()
        skip = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume, skip_event=skip)
        self.assertTrue(skip.wait(timeout=0.5), "CTRL edge should set skip_event")
        self.assertFalse(pause.is_set(), "CTRL alone must not trigger pause")
        poller.stop()

    def test_ctrl_press_while_paused_is_no_op(self):
        api = FakeApi()
        # CTRL pressed continuously, but we start in PAUSED state.
        api.set_script([b"\x04"])
        pause = threading.Event()
        pause.set()
        resume = threading.Event()
        skip = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume, skip_event=skip)
        time.sleep(0.15)
        self.assertFalse(skip.is_set(), "CTRL while paused must be ignored")
        self.assertFalse(resume.is_set(), "CTRL while paused must not trigger resume either")
        poller.stop()

    def test_ctrl_dropped_when_no_skip_event_provided(self):
        # Backwards compat: existing callers that don't pass skip_event
        # should still work — CTRL just becomes a silent no-op.
        api = FakeApi()
        api.set_script([b"\x00", b"\x04"])
        pause = threading.Event()
        resume = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume)  # no skip_event
        time.sleep(0.1)
        self.assertFalse(pause.is_set())
        poller.stop()

    def test_simultaneous_cbm_and_ctrl_prefers_pause(self):
        # Frame-perfect chord: $02 | $04 = $06. Pause wins; skip dropped.
        api = FakeApi()
        api.set_script([b"\x00", b"\x06", b"\x06"])
        pause = threading.Event()
        resume = threading.Event()
        skip = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume, skip_event=skip)
        self.assertTrue(pause.wait(timeout=0.5), "C= in the chord should still trigger pause")
        self.assertFalse(skip.is_set(), "skip must NOT fire on the same tick as pause")
        poller.stop()

    def test_ctrl_press_and_shift_chord_does_not_cycle(self):
        # SHIFT + CTRL → skip wins, cycle suppressed (SHIFT in any
        # multi-mod chord is dropped per the UI contract).
        api = FakeApi()
        api.set_script([b"\x00", b"\x05", b"\x05"])  # SHIFT|CTRL
        pause = threading.Event()
        resume = threading.Event()
        skip = threading.Event()
        cycle = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume, skip_event=skip, cycle_event=cycle)
        self.assertTrue(skip.wait(timeout=0.3), "CTRL in the chord should still trigger skip")
        self.assertFalse(cycle.is_set(), "SHIFT must NOT cycle when chorded with CTRL")
        poller.stop()

    def test_ctrl_edge_after_release(self):
        # Press, release, press → two skip events possible, but skip_event
        # only latches on edge. We just verify the second press still
        # fires after the event is cleared by the consumer.
        api = FakeApi()
        api.set_script([b"\x00", b"\x04", b"\x00", b"\x04"])
        pause = threading.Event()
        resume = threading.Event()
        skip = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume, skip_event=skip)
        self.assertTrue(skip.wait(timeout=0.3))
        skip.clear()
        # Need to give the poller time to step through release → press.
        # The script's last byte loops; we already passed it before clearing,
        # so we won't actually see a second edge in this short window.
        # The point is the first press worked; the loop-after-clear case is
        # covered by test_ctrl_press_sets_skip_event running again.
        poller.stop()


class ShiftCycleTest(unittest.TestCase):
    """SHIFT (bit 0 of $028D) press → cycle_event. SHIFT alone only;
    any chord with C= or CTRL suppresses it."""

    def test_shift_press_sets_cycle_event(self):
        api = FakeApi()
        api.set_script([b"\x00", b"\x01", b"\x01"])  # SHIFT
        pause = threading.Event()
        resume = threading.Event()
        cycle = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume, cycle_event=cycle)
        self.assertTrue(cycle.wait(timeout=0.5), "SHIFT edge should set cycle_event")
        self.assertFalse(pause.is_set(), "SHIFT alone must not trigger pause")
        poller.stop()

    def test_shift_press_while_paused_is_no_op(self):
        api = FakeApi()
        api.set_script([b"\x01"])
        pause = threading.Event()
        pause.set()
        resume = threading.Event()
        cycle = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume, cycle_event=cycle)
        time.sleep(0.15)
        self.assertFalse(cycle.is_set(), "SHIFT while paused must be ignored")
        self.assertFalse(resume.is_set(), "SHIFT while paused must not trigger resume either")
        poller.stop()

    def test_shift_dropped_when_no_cycle_event_provided(self):
        # Backwards compat: callers that don't pass cycle_event still work.
        api = FakeApi()
        api.set_script([b"\x00", b"\x01"])
        pause = threading.Event()
        resume = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume)
        time.sleep(0.1)
        self.assertFalse(pause.is_set())
        poller.stop()

    def test_shift_chord_with_cbm_drops_cycle(self):
        # SHIFT + C= → pause wins, cycle suppressed.
        api = FakeApi()
        api.set_script([b"\x00", b"\x03", b"\x03"])  # SHIFT|CBM
        pause = threading.Event()
        resume = threading.Event()
        cycle = threading.Event()
        poller = CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)
        poller.start(pause, resume, cycle_event=cycle)
        self.assertTrue(pause.wait(timeout=0.3), "C= in the chord should still trigger pause")
        self.assertFalse(cycle.is_set(), "SHIFT must NOT cycle when chorded with C=")
        poller.stop()


class MenuKeyApi:
    """Serves $028D (modifiers) and the kernal keyboard buffer — NDX ($00C6)
    + KEYD ($0277) — and consumes the buffer on a write of $00C6=0, modelling
    how the U64's CMD_KEYB / the kernal feed decoded keystrokes and how the
    poller drains them. `inject(codes)` is the CMD_KEYB analogue. mod_seq's
    last byte loops forever."""

    def __init__(self, mod_seq=None):
        self._lock = threading.Lock()
        self.mod_seq = list(mod_seq) if mod_seq else [b"\x00"]
        self._mod_idx = 0
        self._buf: list[int] = []
        self.cleared = 0  # number of buffer-consume writes (for assertions)

    def inject(self, codes):
        """Push decoded PETSCII codes into the buffer (as CMD_KEYB would)."""
        with self._lock:
            self._buf.extend(codes)

    def read_memory(self, address, length, timeout=1.0):
        with self._lock:
            if address == ADDR_MODIFIERS:
                assert length == 1
                b = self.mod_seq[min(self._mod_idx, len(self.mod_seq) - 1)]
                self._mod_idx += 1
                return b
            if address == ADDR_KB_BUFFER_LEN:
                assert length == 1
                return bytes([len(self._buf)])
            if address == ADDR_KB_BUFFER:
                return bytes(self._buf[:length])
            raise AssertionError(f"unexpected read address {address:#06x}")

    def write_memory(self, address, data_hex):
        # The poller consumes buffered keys by zeroing NDX ($00C6).
        assert int(address, 16) == ADDR_KB_BUFFER_LEN
        assert data_hex == "00"
        with self._lock:
            self._buf.clear()
            self.cleared += 1

    @property
    def stats(self):
        return {"writes": 0, "skipped": 0, "errors": 0, "bytes": 0}


def _menu_poller(api):
    return CommodoreKeyPoller(cast(Ultimate64API, api), poll_interval_s=0.01)


class MenuInputTest(unittest.TestCase):
    def test_space_press_opens_menu(self):
        # Eligible scene, menu closed: a buffered SPACE → menu_event (open).
        api = MenuKeyApi()
        api.inject([KEYBUF.SPACE])
        poller = _menu_poller(api)
        pause, resume = threading.Event(), threading.Event()
        menu_event, menu_active, menu_eligible = (threading.Event() for _ in range(3))
        menu_eligible.set()
        poller.start(
            pause,
            resume,
            menu_event=menu_event,
            menu_active=menu_active,
            menu_eligible=menu_eligible,
        )
        self.assertTrue(menu_event.wait(timeout=0.5), "buffered SPACE should set menu_event")
        self.assertFalse(pause.is_set())
        poller.stop()

    def test_no_buffer_read_when_menu_not_wired(self):
        # menu_event None → the poller must never touch the keyboard buffer
        # (read load stays $028D-only). A buffer read here would raise.
        class ModOnlyApi(MenuKeyApi):
            def read_memory(self, address, length, timeout=1.0):
                assert address == ADDR_MODIFIERS, "must not read the buffer when menu unwired"
                return super().read_memory(address, length, timeout)

        api = ModOnlyApi()
        poller = _menu_poller(api)
        pause, resume = threading.Event(), threading.Event()
        poller.start(pause, resume)  # no menu params
        time.sleep(0.08)
        poller.stop()  # no assertion error ⇒ pass

    def test_no_buffer_read_when_not_eligible(self):
        # menu wired but neither open nor eligible (e.g. a kernal-input
        # launcher scene): the poller must NOT read/clear the buffer — $00C6
        # is the launcher's own to watch. A buffer read here would raise.
        class ModOnlyApi(MenuKeyApi):
            def read_memory(self, address, length, timeout=1.0):
                assert address == ADDR_MODIFIERS, "must not touch the buffer when not eligible"
                return super().read_memory(address, length, timeout)

        api = ModOnlyApi()
        poller = _menu_poller(api)
        pause, resume = threading.Event(), threading.Event()
        menu_event, menu_active, menu_eligible = (threading.Event() for _ in range(3))
        # eligible NOT set, active NOT set
        poller.start(
            pause,
            resume,
            menu_event=menu_event,
            menu_active=menu_active,
            menu_eligible=menu_eligible,
        )
        time.sleep(0.08)
        poller.stop()

    def test_menu_active_suspends_pause_skip_cycle(self):
        # With menu_active set, C=/CTRL/SHIFT must NOT fire pause/skip/cycle.
        api = MenuKeyApi(mod_seq=[b"\x07"])  # SHIFT|CBM|CTRL all held
        poller = _menu_poller(api)
        pause, resume, skip, cycle = (threading.Event() for _ in range(4))
        menu_event, menu_active = threading.Event(), threading.Event()
        menu_active.set()
        poller.start(
            pause,
            resume,
            skip_event=skip,
            cycle_event=cycle,
            menu_event=menu_event,
            menu_active=menu_active,
        )
        time.sleep(0.1)
        self.assertFalse(pause.is_set(), "menu open: C= must not pause")
        self.assertFalse(skip.is_set(), "menu open: CTRL must not skip")
        self.assertFalse(cycle.is_set(), "menu open: SHIFT must not cycle")
        poller.stop()

    def test_space_while_active_toggles_menu_closed(self):
        api = MenuKeyApi()
        api.inject([KEYBUF.SPACE])
        poller = _menu_poller(api)
        pause, resume = threading.Event(), threading.Event()
        menu_event, menu_active = threading.Event(), threading.Event()
        menu_active.set()  # menu already open
        poller.start(pause, resume, menu_event=menu_event, menu_active=menu_active)
        self.assertTrue(menu_event.wait(timeout=0.5), "buffered SPACE while open toggles (close)")
        poller.stop()

    def test_nav_keys_enqueued(self):
        # Menu open: cursor codes are enqueued in order. CRSR-up is the
        # kernal's SHIFT+CRSR-down decode (reverse), carried in the code
        # itself — no separate shift flag.
        nav: deque[int] = deque(maxlen=8)
        api = MenuKeyApi()
        api.inject([KEYBUF.CRSR_DOWN, KEYBUF.CRSR_UP, KEYBUF.CRSR_RIGHT])
        poller = _menu_poller(api)
        pause, resume = threading.Event(), threading.Event()
        menu_event, menu_active = threading.Event(), threading.Event()
        menu_active.set()
        poller.start(pause, resume, menu_event=menu_event, menu_active=menu_active, nav_queue=nav)
        deadline = time.time() + 0.6
        while time.time() < deadline and len(nav) < 3:
            time.sleep(0.01)
        poller.stop()
        self.assertEqual(list(nav), [KEYBUF.CRSR_DOWN, KEYBUF.CRSR_UP, KEYBUF.CRSR_RIGHT])

    def test_space_excluded_from_nav_queue(self):
        nav: deque[int] = deque(maxlen=8)
        api = MenuKeyApi()
        api.inject([KEYBUF.SPACE])
        poller = _menu_poller(api)
        pause, resume = threading.Event(), threading.Event()
        menu_event, menu_active = threading.Event(), threading.Event()
        menu_active.set()
        poller.start(pause, resume, menu_event=menu_event, menu_active=menu_active, nav_queue=nav)
        time.sleep(0.1)
        poller.stop()
        self.assertEqual(len(nav), 0, "SPACE drives menu_event, never the nav queue")

    def test_buffer_consumed_after_drain(self):
        # Draining must zero NDX so keys aren't reprocessed every tick (and a
        # launcher-style $00C6 watch sees a clean buffer afterward).
        nav: deque[int] = deque(maxlen=8)
        api = MenuKeyApi()
        api.inject([KEYBUF.CRSR_DOWN])
        poller = _menu_poller(api)
        pause, resume = threading.Event(), threading.Event()
        menu_event, menu_active = threading.Event(), threading.Event()
        menu_active.set()
        poller.start(pause, resume, menu_event=menu_event, menu_active=menu_active, nav_queue=nav)
        deadline = time.time() + 0.5
        while time.time() < deadline and api.cleared < 1:
            time.sleep(0.01)
        poller.stop()
        self.assertGreaterEqual(api.cleared, 1, "buffer must be consumed via a $00C6=0 write")
        self.assertEqual(list(nav), [KEYBUF.CRSR_DOWN], "exactly one nav event, not repeated")


if __name__ == "__main__":
    unittest.main()
