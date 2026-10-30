"""Tests for the vision controller: gesture classification + the event-driving
poller. No camera and no mediapipe needed — classification runs on synthetic
landmark fixtures, and the controller is driven by a scripted fake recognizer
(the visual analog of test_keyboard.py's scripted `$028D` FakeApi)."""

from __future__ import annotations

import threading
import time
import unittest
from typing import cast
from unittest import mock

import numpy as np
from _fakes import FrozenClock

from c64cast.app import config as cfgmod
from c64cast.control import vision as vision_mod
from c64cast.control.vision import (
    INDEX_PIP,
    INDEX_TIP,
    MIDDLE_PIP,
    MIDDLE_TIP,
    PINKY_PIP,
    PINKY_TIP,
    RING_PIP,
    RING_TIP,
    THUMB_TIP,
    WRIST,
    Gesture,
    GestureRecognizer,
    HandState,
    VisionController,
    classify_static,
    count_extended_fingers,
    is_pinch,
)
from c64cast.video.video import WebcamSource


def make_hand(points: dict[int, tuple[float, float]]) -> HandState:
    """Build a HandState from a {landmark_index: (x, y)} map; z=0 throughout."""
    arr = np.zeros((21, 3), dtype=np.float32)
    for idx, (x, y) in points.items():
        arr[idx] = (x, y, 0.0)
    return HandState(landmarks=arr)


# Wrist low on the frame (y≈0.9); an extended finger's tip sits higher up
# (smaller y → farther from the wrist) than its PIP joint.
_OPEN_POINTS = {
    WRIST: (0.5, 0.9),
    THUMB_TIP: (0.2, 0.6),  # far from the index tip → not a pinch
    INDEX_PIP: (0.50, 0.6),
    INDEX_TIP: (0.50, 0.35),
    MIDDLE_PIP: (0.55, 0.6),
    MIDDLE_TIP: (0.55, 0.30),
    RING_PIP: (0.60, 0.6),
    RING_TIP: (0.60, 0.35),
    PINKY_PIP: (0.65, 0.6),
    PINKY_TIP: (0.65, 0.40),
}
_FIST_POINTS = {
    WRIST: (0.5, 0.9),
    THUMB_TIP: (0.2, 0.8),
    INDEX_PIP: (0.50, 0.6),
    INDEX_TIP: (0.50, 0.7),  # tip curled toward wrist
    MIDDLE_PIP: (0.55, 0.6),
    MIDDLE_TIP: (0.55, 0.72),
    RING_PIP: (0.60, 0.6),
    RING_TIP: (0.60, 0.72),
    PINKY_PIP: (0.65, 0.6),
    PINKY_TIP: (0.65, 0.70),
}
# Open-hand geometry but thumb tip brought onto the index tip → a pinch.
_PINCH_POINTS = {**_OPEN_POINTS, THUMB_TIP: (0.49, 0.36)}


def open_hand(x: float = 0.5) -> HandState:
    pts = dict(_OPEN_POINTS)
    pts[WRIST] = (x, 0.9)
    return make_hand(pts)


def fist(x: float = 0.5) -> HandState:
    pts = dict(_FIST_POINTS)
    pts[WRIST] = (x, 0.9)
    return make_hand(pts)


def pinch(x: float = 0.5) -> HandState:
    pts = dict(_PINCH_POINTS)
    pts[WRIST] = (x, 0.9)
    return make_hand(pts)


class ClassifyStaticTest(unittest.TestCase):
    def test_pinch_detected(self):
        self.assertTrue(is_pinch(pinch(), 0.05))
        self.assertFalse(is_pinch(open_hand(), 0.05))

    def test_open_hand_counts_four_fingers(self):
        self.assertEqual(count_extended_fingers(open_hand()), 4)
        self.assertEqual(count_extended_fingers(fist()), 0)

    def test_classify(self):
        self.assertEqual(classify_static(pinch(), pinch_threshold=0.05), Gesture.PINCH)
        self.assertEqual(classify_static(open_hand(), pinch_threshold=0.05), Gesture.OPEN_HAND)
        self.assertEqual(classify_static(fist(), pinch_threshold=0.05), Gesture.NONE)

    def test_pinch_wins_over_open(self):
        # A pinch curls the index; precedence is pinch.
        self.assertEqual(classify_static(pinch(), pinch_threshold=0.05), Gesture.PINCH)

    def test_closed_fist_with_thumb_on_index_is_not_pinch(self):
        # A fist also has thumb near index, but the other fingers are curled.
        # It must NOT classify as a pinch (the raise-a-closed-hand misfire).
        h = make_hand({**_FIST_POINTS, THUMB_TIP: (0.49, 0.69)})
        self.assertTrue(is_pinch(h, 0.05))  # thumb IS near index
        self.assertEqual(count_extended_fingers(h), 0)  # but hand is closed
        self.assertEqual(classify_static(h, pinch_threshold=0.05), Gesture.NONE)


class FakeSource:
    """Returns a constant dummy frame so the controller has something to feed
    the (fake) recognizer."""

    def __init__(self):
        self._frame = np.zeros((4, 4, 3), dtype=np.uint8)

    def read(self):
        return self._frame

    def read_live(self):
        return self._frame


class FakeRecognizer:
    """Replays a scripted sequence of HandState|None. By default the last entry
    repeats forever (like test_keyboard.py's FakeApi); with loop=True the script
    cycles, useful for simulating a continuously-moving hand."""

    def __init__(self, script, loop=False):
        self._script = list(script)
        self._loop = loop
        self._lock = threading.Lock()
        self._idx = 0
        self.closed = False

    def process(self, frame, timestamp_ms):
        with self._lock:
            if self._loop:
                hand = self._script[self._idx % len(self._script)]
            else:
                hand = self._script[min(self._idx, len(self._script) - 1)]
            self._idx += 1
        return hand

    def close(self):
        self.closed = True


def _controller(script, *, loop=False, **kw):
    kw.setdefault("poll_interval_s", 0.005)
    kw.setdefault("gesture_cooldown_s", 0.0)
    # gesture_dwell_s=0 fires on a 1-frame pose; the dwell-gate tests
    # override it.
    kw.setdefault("gesture_dwell_s", 0.0)
    kw.setdefault("mirror", False)  # don't cv2.flip the dummy frame needlessly
    # FakeSource/FakeRecognizer are structural stand-ins, not subclasses.
    return VisionController(
        cast(WebcamSource, FakeSource()),
        cast(GestureRecognizer, FakeRecognizer(script, loop=loop)),
        **kw,
    )


def _drive_ticks(ctl, ticks, pause, resume, *, skip=None, cycle=None):
    """Drive the gesture state machine synchronously: one scripted
    recognizer frame per tick on a virtual clock, each exactly
    poll_interval_s apart — no poll thread. The threaded version of the
    negative tests slept and then asserted an event was still clear, which
    stays green even with the feature deleted whenever a loaded box keeps
    the poll thread from running inside the sleep window; and the dwell /
    swipe math depends on real inter-tick dt, so wall time also made the
    POSITIVE preconditions approximate. Mirrors test_keyboard.py's
    _drive_ticks harness."""
    ctl._pause_event = pause
    ctl._resume_event = resume
    ctl._skip_event = skip
    ctl._cycle_event = cycle
    clock = FrozenClock(0.0, "monotonic")
    with mock.patch.object(vision_mod, "time", clock):
        state = vision_mod._GestureState(last_tick=0.0, t0=0.0)
        for _ in range(ticks):
            clock.advance(ctl.poll_interval_s)
            ctl._tick(state)
    return state


class VisionControllerTest(unittest.TestCase):
    def test_pinch_sets_pause(self):
        # Start neutral, then a sustained pinch (script's last entry repeats).
        ctl = _controller([fist(), open_hand(), pinch()])
        pause, resume = threading.Event(), threading.Event()
        ctl.start(pause, resume)
        self.assertTrue(pause.wait(0.5), "pinch should set pause_event")
        self.assertFalse(resume.is_set())
        ctl.stop()

    def test_open_hand_cycles(self):
        ctl = _controller([fist(), open_hand()])
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        ctl.start(pause, resume, skip_event=skip, cycle_event=cycle)
        self.assertTrue(cycle.wait(0.5), "open hand should set cycle_event")
        self.assertFalse(pause.is_set())
        self.assertFalse(skip.is_set())
        ctl.stop()

    def test_dwell_gate_rejects_transient_pose(self):
        # An open hand for a single frame, then gone: with a multi-frame dwell
        # gate it must not cycle, the "raising hand flickers a stray cycle"
        # case the gate exists to kill.
        ctl = _controller(
            [open_hand(), None, None, None], gesture_dwell_s=0.05
        )  # 0.05/0.005 = 10 frames
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        _drive_ticks(ctl, 15, pause, resume, skip=skip, cycle=cycle)
        self.assertFalse(cycle.is_set(), "a 1-frame pose must not fire")

    def test_dwell_gate_fires_on_held_pose(self):
        ctl = _controller([open_hand()], gesture_dwell_s=0.03)  # 6 frames
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        ctl.start(pause, resume, skip_event=skip, cycle_event=cycle)
        self.assertTrue(cycle.wait(0.5), "a held open hand must cycle after dwell")
        ctl.stop()

    def test_moving_open_hand_does_not_cycle(self):
        # An open hand that keeps moving must not cycle: the stillness gate is
        # what stops a waving hand racking up cycles. swipe_velocity is huge
        # so the motion cannot fire a skip either, and the script loops so the
        # hand never settles into a held pose.
        moving = [open_hand(0.2), open_hand(0.6)]
        ctl = _controller(moving, loop=True, gesture_dwell_s=0.02, swipe_velocity=100.0)
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        _drive_ticks(ctl, 12, pause, resume, skip=skip, cycle=cycle)
        self.assertFalse(cycle.is_set(), "a moving open hand must not cycle")

    def test_fast_swipe_skips(self):
        # Sustained horizontal wrist motion past the settle window, needing
        # >SWIPE_SETTLE_FRAMES frames. Driven on the virtual clock: the swipe
        # verdict divides wrist travel by inter-tick time, so real ticks on a
        # loaded runner drop the computed speed below swipe_velocity.
        ctl = _controller([fist(0.1), fist(0.9), fist(0.1), fist(0.9)], swipe_velocity=1.0)
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        _drive_ticks(ctl, 5, pause, resume, skip=skip, cycle=cycle)
        self.assertTrue(skip.is_set(), "fast wrist motion should set skip_event")
        self.assertFalse(pause.is_set())

    def test_vertical_raise_is_not_a_swipe(self):
        # Raising the hand is fast VERTICAL motion (x constant, y changes);
        # the |dx|>|dy| gate is what keeps "raise your hand to open it" from
        # skipping.
        raise_up = [make_hand({**_FIST_POINTS, WRIST: (0.5, y)}) for y in (0.9, 0.7, 0.5, 0.3, 0.1)]
        ctl = _controller(raise_up, swipe_velocity=1.0)
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        _drive_ticks(ctl, 10, pause, resume, skip=skip, cycle=cycle)
        self.assertFalse(skip.is_set(), "a vertical raise must not swipe")

    def test_no_event_when_no_hand(self):
        ctl = _controller([None, None, None])
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        _drive_ticks(ctl, 6, pause, resume, skip=skip, cycle=cycle)
        self.assertFalse(pause.is_set() or skip.is_set() or cycle.is_set())

    def test_pinch_hold_resumes_while_paused(self):
        ctl = _controller([pinch()], hold_threshold_s=0.05)
        pause, resume = threading.Event(), threading.Event()
        pause.set()  # simulate already paused
        ctl.start(pause, resume)
        self.assertTrue(resume.wait(0.5), "sustained pinch while paused should resume")
        ctl.stop()

    def test_a_pinch_held_through_its_resume_does_not_pause_again(self):
        # Paused from another surface, so the controller has no pose of its own
        # on record. The resume takes the playlist a reset and a clear loop, and
        # the hand is still pinching when it comes back.
        ctl = _controller([pinch()], hold_threshold_s=0.05)
        pause, resume = threading.Event(), threading.Event()
        pause.set()
        state = _drive_ticks(ctl, 15, pause, resume)
        self.assertTrue(resume.is_set())
        pause.clear()
        clock = FrozenClock(1.0, "monotonic")
        with mock.patch.object(vision_mod, "time", clock):
            state.last_tick = 1.0
            for _ in range(15):
                clock.advance(ctl.poll_interval_s)
                ctl._tick(state)
        self.assertFalse(pause.is_set(), "the resume pinch must not pause the show again")

    def test_a_new_pinch_after_a_resume_still_pauses(self):
        ctl = _controller([pinch()], hold_threshold_s=0.05)
        pause, resume = threading.Event(), threading.Event()
        pause.set()
        state = _drive_ticks(ctl, 15, pause, resume)
        pause.clear()
        ctl.recognizer = cast(GestureRecognizer, FakeRecognizer([fist(), fist(), pinch()]))
        clock = FrozenClock(1.0, "monotonic")
        with mock.patch.object(vision_mod, "time", clock):
            state.last_tick = 1.0
            for _ in range(5):
                clock.advance(ctl.poll_interval_s)
                ctl._tick(state)
        self.assertTrue(pause.is_set())

    def test_no_pause_while_paused_on_swipe(self):
        # Paused: only the pinch-hold resume gesture matters; a swipe is ignored.
        ctl = _controller([fist(0.1), fist(0.9)], swipe_velocity=1.0)
        pause, resume = threading.Event(), threading.Event()
        skip = threading.Event()
        pause.set()
        _drive_ticks(ctl, 8, pause, resume, skip=skip)
        self.assertFalse(resume.is_set())
        self.assertFalse(skip.is_set())

    def test_latest_hands_snapshot(self):
        target = open_hand()
        ctl = _controller([target])
        pause, resume = threading.Event(), threading.Event()
        _drive_ticks(ctl, 1, pause, resume)
        snap = ctl.latest_hands()
        self.assertIsNotNone(snap)
        assert snap is not None
        np.testing.assert_array_equal(snap.landmarks, target.landmarks)


class VisionTimingConfigTest(unittest.TestCase):
    """The controller divides by `poll_interval_s` and rounds the dwell into a
    frame count, so a value it cannot run with has to stop the config load
    rather than the stack build."""

    def test_a_timing_the_controller_cannot_run_with_is_refused(self):
        refused = {
            "poll_interval_s": (0.0, -0.1, float("nan"), float("inf")),
            "gesture_dwell_s": (float("nan"), float("inf")),
            "gesture_cooldown_s": (float("nan"), float("inf")),
            "hold_threshold_s": (float("nan"), float("inf")),
        }
        for name, values in refused.items():
            for bad in values:
                cfg = cfgmod.Config()
                setattr(cfg.vision, name, bad)
                with self.subTest(name=name, value=bad):
                    with self.assertRaisesRegex(ValueError, rf"\[vision\]\.{name}"):
                        cfgmod.validate_sections(cfg)

    def test_the_defaults_and_zero_waits_load(self):
        cfgmod.validate_sections(cfgmod.Config())
        cfg = cfgmod.Config()
        for name in ("gesture_dwell_s", "gesture_cooldown_s", "hold_threshold_s"):
            setattr(cfg.vision, name, 0)
        cfgmod.validate_sections(cfg)

    def test_the_refused_values_are_the_ones_that_break_the_controller(self):
        builds = {
            "poll_interval_s=0": lambda: _controller([None], poll_interval_s=0.0),
            "gesture_dwell_s=nan": lambda: _controller([None], gesture_dwell_s=float("nan")),
            "gesture_dwell_s=inf": lambda: _controller([None], gesture_dwell_s=float("inf")),
        }
        for label, build in builds.items():
            with (
                self.subTest(label),
                self.assertRaises((ZeroDivisionError, ValueError, OverflowError)),
            ):
                build()


class _FakePerfSession:
    def __init__(self) -> None:
        self.advanced = 0

    def advance_clip(self):
        self.advanced += 1
        return 1


class _FakePerfPlaylist:
    """The bind_performance() surface: pl.performance.advance_clip and
    pl.toggle_effect_layer, both thread-safe no-ops here that just record."""

    def __init__(self) -> None:
        self.performance = _FakePerfSession()
        self.toggled: list[int] = []

    def toggle_effect_layer(self, slot: int):
        self.toggled.append(slot)
        return True


class VisionPerformanceModeTest(unittest.TestCase):
    """Live DJ/VJ Phase 6: a bound playlist remaps RUNNING gestures to the
    clip-launch grid (swipe = next clip, pinch = fx0 bypass, open = fx1 bypass)."""

    # Driven through _drive_ticks like the transport tests above: the swipe
    # decision divides wrist movement by real inter-tick dt, so a loaded
    # box stretches dt and the gesture legitimately never fires.

    def test_swipe_launches_next_clip_not_skip(self):
        ctl = _controller([fist(0.1), fist(0.9), fist(0.1), fist(0.9)], swipe_velocity=1.0)
        perf = _FakePerfPlaylist()
        ctl.bind_performance(perf)
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        _drive_ticks(ctl, 5, pause, resume, skip=skip, cycle=cycle)
        self.assertGreaterEqual(perf.performance.advanced, 1, "swipe should advance the clip grid")
        self.assertFalse(skip.is_set(), "in performance mode a swipe must not skip")

    def test_pinch_toggles_fx_layer_0_not_pause(self):
        ctl = _controller([fist(), pinch()])
        perf = _FakePerfPlaylist()
        ctl.bind_performance(perf)
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        _drive_ticks(ctl, 4, pause, resume, skip=skip, cycle=cycle)
        self.assertIn(0, perf.toggled, "pinch-hold should bypass fx layer 0")
        self.assertFalse(pause.is_set(), "in performance mode a pinch must not pause")

    def test_open_hand_toggles_fx_layer_1_not_cycle(self):
        ctl = _controller([fist(), open_hand()])
        perf = _FakePerfPlaylist()
        ctl.bind_performance(perf)
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        _drive_ticks(ctl, 4, pause, resume, skip=skip, cycle=cycle)
        self.assertIn(1, perf.toggled, "open-hand-hold should bypass fx layer 1")
        self.assertFalse(cycle.is_set(), "in performance mode open-hand must not cycle style")

    def test_paused_pinch_still_resumes_in_performance_mode(self):
        # hold_threshold 0.05 s at 0.005 s virtual ticks is held after 10
        # ticks; drive 15 so the accrual provably crosses it.
        ctl = _controller([pinch()], hold_threshold_s=0.05)
        perf = _FakePerfPlaylist()
        ctl.bind_performance(perf)
        pause, resume = threading.Event(), threading.Event()
        pause.set()
        _drive_ticks(ctl, 15, pause, resume)
        self.assertTrue(resume.is_set(), "pinch-hold while paused should still resume")

    def test_stop_closes_recognizer(self):
        rec = FakeRecognizer([None])
        ctl = VisionController(
            cast(WebcamSource, FakeSource()), cast(GestureRecognizer, rec), poll_interval_s=0.005
        )
        pause, resume = threading.Event(), threading.Event()
        ctl.start(pause, resume)
        ctl.stop()
        self.assertTrue(rec.closed)


class WebcamSourceBrokerTest(unittest.TestCase):
    """The shared camera broker: a grab thread owns the capture, read() hands
    out independent copies. Patches cv2.VideoCapture so no real camera opens."""

    def _fake_cap(self, frame):
        cap = mock.MagicMock()
        cap.isOpened.return_value = True
        cap.read.return_value = (True, frame)
        return cap

    def test_read_returns_independent_copies(self):
        from c64cast.video import video

        frame = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
        cap = self._fake_cap(frame)
        with mock.patch.object(video.cv2, "VideoCapture", return_value=cap):
            src = video.WebcamSource(0)
            try:
                # Give the grab thread a moment to populate the latest frame.
                deadline = time.time() + 1.0
                a = src.read()
                while a is None and time.time() < deadline:
                    time.sleep(0.01)
                    a = src.read()
                b = src.read()
                self.assertIsNotNone(a)
                self.assertIsNotNone(b)
                assert a is not None and b is not None
                # Equal values, distinct objects, so a consumer mutating one cannot
                # corrupt the broker's frame or another consumer's copy.
                np.testing.assert_array_equal(a, frame)
                self.assertIsNot(a, b)
            finally:
                src.release()
            cap.release.assert_called_once()


class _ScriptedCap:
    """A `cv2.VideoCapture` stand-in. `read()` answers from `frames`, then
    ``(False, None)`` for good: OpenCV's answer for a device that is gone."""

    def __init__(self, frames=(), *, opened=True):
        self.frames = list(frames)
        self.opened = opened
        self.released = False

    def isOpened(self):
        return self.opened

    def read(self):
        if self.released or not self.frames:
            return False, None
        return True, self.frames.pop(0)

    def release(self):
        self.released = True


class _ClockedStop:
    """The grab loop's stop event, with every wait spent on the test's clock."""

    def __init__(self, clock):
        self.clock = clock

    def is_set(self):
        return False

    def wait(self, seconds):
        self.clock.advance(seconds)
        return False


class WebcamSourceLossTest(unittest.TestCase):
    """A camera that stops sending: the last frame is held for the grace period
    and then black, the camera is reopened by who it is and never by its old
    index, and vision is never handed the held frame."""

    def setUp(self):
        from c64cast.video import video

        self.video = video
        self.clock = FrozenClock(100.0, "monotonic")
        self.frame = np.full((4, 4, 3), 7, dtype=np.uint8)
        self.first = _ScriptedCap([self.frame])
        self.opened: list[tuple] = []
        self.caps = [self.first]
        self.identity: tuple | None = ("Cam Link 4K", "0fd9:0066")
        self.where: tuple[int, int] | None = None
        self.lookups = 0

        def open_capture(*args):
            self.opened.append(args)
            return self.caps.pop(0)

        def find_camera(identity):
            self.lookups += 1
            self.assertEqual(identity, self.identity)
            return self.where

        for patcher in (
            mock.patch.object(video, "time", self.clock),
            mock.patch.object(video, "PollThread"),
            mock.patch.object(video.cv2, "VideoCapture", side_effect=open_capture),
            mock.patch("c64cast.control.camera.camera_identity", lambda _i: self.identity),
            mock.patch("c64cast.control.camera.find_camera", find_camera),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stop = cast(threading.Event, _ClockedStop(self.clock))

    def _source(self):
        src = self.video.WebcamSource(0)
        self.addCleanup(src.release)
        return src

    def _run(self, src, seconds):
        until = self.clock.monotonic() + seconds
        while self.clock.monotonic() < until:
            src._grab_step(self.stop)
            self.clock.advance(0.01)

    def test_a_lost_camera_holds_its_last_frame_then_serves_black(self):
        src = self._source()
        with self.assertLogs("c64cast.video.video", "WARNING") as logs:
            self._run(src, 1.0)
        held = src.read()
        assert held is not None
        np.testing.assert_array_equal(held, self.frame)
        self._run(src, self.video.CAMERA_LOSS_GRACE_S)
        black = src.read()
        assert black is not None
        self.assertEqual(black.shape, self.frame.shape)
        self.assertFalse(black.any())
        self.assertEqual(len(logs.records), 1, "one warning on loss, not one per attempt")

    def test_vision_is_never_handed_the_held_frame(self):
        src = self._source()
        src._grab_step(self.stop)
        self.assertIsNotNone(src.read_live())
        with self.assertLogs("c64cast.video.video", "WARNING"):
            self._run(src, self.video.FRAME_STALE_S + 0.1)
        self.assertIsNotNone(src.read())
        self.assertIsNone(src.read_live())

    def test_a_camera_that_comes_back_is_reopened_and_goes_live(self):
        src = self._source()
        with self.assertLogs("c64cast.video.video", "WARNING"):
            self._run(src, 2.0)
        live = np.full((4, 4, 3), 9, dtype=np.uint8)
        self.caps.append(_ScriptedCap([live] * 1000))
        self.where = (3, 1200)
        with self.assertLogs("c64cast.video.video", "INFO") as logs:
            self._run(src, 6.0)
        self.assertEqual(self.opened[-1], (3, 1200))
        self.assertTrue(self.first.released)
        frame = src.read_live()
        assert frame is not None
        np.testing.assert_array_equal(frame, live)
        self.assertEqual([r.getMessage() for r in logs.records], ["camera 0 is back"])

    def test_a_camera_nobody_can_name_is_not_reopened_by_its_old_index(self):
        # Another camera, the built-in one say, may sit at that index by now.
        self.identity = None
        src = self._source()
        with self.assertLogs("c64cast.video.video", "WARNING"):
            self._run(src, 20.0)
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.lookups, 0)

    def test_a_camera_reopened_while_the_source_stops_is_closed_again(self):
        src = self._source()
        fresh = _ScriptedCap()
        self.caps.append(fresh)
        self.where = (3, 1200)
        stopping = mock.Mock(is_set=mock.Mock(return_value=True))
        src._try_reopen(cast(threading.Event, stopping))
        self.assertTrue(fresh.released)
        self.assertIsNot(src.cap, fresh)

    def test_a_source_released_during_the_lookup_opens_nothing(self):
        src = self._source()
        self.where = (3, 1200)
        lookup = mock.Mock(side_effect=lambda _identity: (src.release(), self.where)[1])
        with mock.patch("c64cast.control.camera.find_camera", lookup):
            src._try_reopen(self.stop)
        lookup.assert_called_once()
        self.assertEqual(len(self.opened), 1)
        self.assertIsNone(src.cap)

    def test_reopen_attempts_back_off(self):
        src = self._source()
        with self.assertLogs("c64cast.video.video", "WARNING"):
            self._run(src, 30.0)
        # 0.5 s doubling to 5 s: about a dozen tries in 30 s, not thousands.
        self.assertLessEqual(self.lookups, 12)
        self.assertGreaterEqual(self.lookups, 5)


class VisionStaleFrameTest(unittest.TestCase):
    def test_a_hand_frozen_mid_pinch_does_not_resume_the_show(self):
        class _HeldFrameSource:
            def read(self):
                return np.zeros((4, 4, 3), dtype=np.uint8)

            def read_live(self):
                return None

        ctl = VisionController(
            cast(WebcamSource, _HeldFrameSource()),
            cast(GestureRecognizer, FakeRecognizer([pinch()])),
            poll_interval_s=0.005,
            hold_threshold_s=0.05,
            gesture_cooldown_s=0.0,
            gesture_dwell_s=0.0,
            mirror=False,
        )
        pause, resume = threading.Event(), threading.Event()
        pause.set()
        _drive_ticks(ctl, 40, pause, resume)
        self.assertFalse(resume.is_set())


if __name__ == "__main__":
    unittest.main()
