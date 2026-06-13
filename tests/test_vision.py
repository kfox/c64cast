"""Tests for the vision controller: gesture classification + the event-driving
poller. No camera and no mediapipe needed — classification runs on synthetic
landmark fixtures, and the controller is driven by a scripted fake recognizer
(the visual analogue of test_keyboard.py's scripted `$028D` FakeApi)."""
from __future__ import annotations

import threading
import time
import unittest
from typing import cast
from unittest import mock

import numpy as np

from c64cast.video import WebcamSource
from c64cast.vision import (
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
    THUMB_TIP: (0.2, 0.6),     # far from the index tip → not a pinch
    INDEX_PIP: (0.50, 0.6), INDEX_TIP: (0.50, 0.35),
    MIDDLE_PIP: (0.55, 0.6), MIDDLE_TIP: (0.55, 0.30),
    RING_PIP: (0.60, 0.6), RING_TIP: (0.60, 0.35),
    PINKY_PIP: (0.65, 0.6), PINKY_TIP: (0.65, 0.40),
}
_FIST_POINTS = {
    WRIST: (0.5, 0.9),
    THUMB_TIP: (0.2, 0.8),
    INDEX_PIP: (0.50, 0.6), INDEX_TIP: (0.50, 0.7),   # tip curled toward wrist
    MIDDLE_PIP: (0.55, 0.6), MIDDLE_TIP: (0.55, 0.72),
    RING_PIP: (0.60, 0.6), RING_TIP: (0.60, 0.72),
    PINKY_PIP: (0.65, 0.6), PINKY_TIP: (0.65, 0.70),
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
        self.assertEqual(classify_static(pinch(), pinch_threshold=0.05),
                         Gesture.PINCH)
        self.assertEqual(classify_static(open_hand(), pinch_threshold=0.05),
                         Gesture.OPEN_HAND)
        self.assertEqual(classify_static(fist(), pinch_threshold=0.05),
                         Gesture.NONE)

    def test_pinch_wins_over_open(self):
        # A pinch curls the index, but be explicit that precedence is pinch.
        self.assertEqual(classify_static(pinch(), pinch_threshold=0.05),
                         Gesture.PINCH)

    def test_closed_fist_with_thumb_on_index_is_not_pinch(self):
        # A fist also has thumb near index, but the other fingers are curled.
        # It must NOT classify as a pinch (the raise-a-closed-hand misfire).
        h = make_hand({**_FIST_POINTS, THUMB_TIP: (0.49, 0.69)})
        self.assertTrue(is_pinch(h, 0.05))                 # thumb IS near index
        self.assertEqual(count_extended_fingers(h), 0)     # but hand is closed
        self.assertEqual(classify_static(h, pinch_threshold=0.05), Gesture.NONE)


class FakeSource:
    """Returns a constant dummy frame so the controller has something to feed
    the (fake) recognizer."""

    def __init__(self):
        self._frame = np.zeros((4, 4, 3), dtype=np.uint8)

    def read(self):
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
    # Most tests want immediate firing; gesture_dwell_s=0 -> a 1-frame pose
    # fires. The dwell-gate tests override this explicitly.
    kw.setdefault("gesture_dwell_s", 0.0)
    kw.setdefault("mirror", False)    # don't cv2.flip the dummy frame needlessly
    # FakeSource/FakeRecognizer are structural stand-ins (not subclasses), so
    # cast for the type checker — same pattern as test_keyboard.py's FakeApi.
    return VisionController(cast(WebcamSource, FakeSource()),
                           cast(GestureRecognizer, FakeRecognizer(script, loop=loop)),
                           **kw)


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
        # An open hand for a single frame, then gone — with a multi-frame dwell
        # gate it must NOT cycle (this is the "raising hand flickers a stray
        # cycle" case the gate exists to kill).
        ctl = _controller([open_hand(), None, None, None],
                          gesture_dwell_s=0.05)    # 0.05/0.005 = 10 frames
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        ctl.start(pause, resume, skip_event=skip, cycle_event=cycle)
        time.sleep(0.15)
        self.assertFalse(cycle.is_set(), "a 1-frame pose must not fire")
        ctl.stop()

    def test_dwell_gate_fires_on_held_pose(self):
        ctl = _controller([open_hand()], gesture_dwell_s=0.03)   # 6 frames
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        ctl.start(pause, resume, skip_event=skip, cycle_event=cycle)
        self.assertTrue(cycle.wait(0.5), "a held open hand must cycle after dwell")
        ctl.stop()

    def test_moving_open_hand_does_not_cycle(self):
        # An open hand that keeps MOVING (never held still) must not cycle — the
        # stillness gate is what stops a busy/waving hand from racking up cycles.
        # swipe_velocity huge so the motion can't fire a skip either; loop so the
        # hand keeps jumping instead of settling into a held pose.
        moving = [open_hand(0.2), open_hand(0.6)]
        ctl = _controller(moving, loop=True, gesture_dwell_s=0.02,
                          swipe_velocity=100.0)
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        ctl.start(pause, resume, skip_event=skip, cycle_event=cycle)
        time.sleep(0.15)
        self.assertFalse(cycle.is_set(), "a moving open hand must not cycle")
        ctl.stop()

    def test_fast_swipe_skips(self):
        # Sustained horizontal wrist motion (x flips across the frame) past the
        # settle window → skip. Needs >SWIPE_SETTLE_FRAMES frames of motion.
        ctl = _controller([fist(0.1), fist(0.9), fist(0.1), fist(0.9)],
                          swipe_velocity=1.0)
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        ctl.start(pause, resume, skip_event=skip, cycle_event=cycle)
        self.assertTrue(skip.wait(0.5), "fast wrist motion should set skip_event")
        self.assertFalse(pause.is_set())
        ctl.stop()

    def test_vertical_raise_is_not_a_swipe(self):
        # Raising the hand = fast VERTICAL motion (x constant, y changes). Must
        # not register a swipe — the |dx|>|dy| gate is what makes "raise your
        # hand to open it" not skip.
        raise_up = [make_hand({**_FIST_POINTS, WRIST: (0.5, y)})
                    for y in (0.9, 0.7, 0.5, 0.3, 0.1)]
        ctl = _controller(raise_up, swipe_velocity=1.0)
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        ctl.start(pause, resume, skip_event=skip, cycle_event=cycle)
        time.sleep(0.12)
        self.assertFalse(skip.is_set(), "a vertical raise must not swipe")
        ctl.stop()

    def test_no_event_when_no_hand(self):
        ctl = _controller([None, None, None])
        pause, resume = threading.Event(), threading.Event()
        skip, cycle = threading.Event(), threading.Event()
        ctl.start(pause, resume, skip_event=skip, cycle_event=cycle)
        time.sleep(0.1)
        self.assertFalse(pause.is_set() or skip.is_set() or cycle.is_set())
        ctl.stop()

    def test_pinch_hold_resumes_while_paused(self):
        ctl = _controller([pinch()], hold_threshold_s=0.05)
        pause, resume = threading.Event(), threading.Event()
        pause.set()    # simulate already paused
        ctl.start(pause, resume)
        self.assertTrue(resume.wait(0.5),
                        "sustained pinch while paused should resume")
        ctl.stop()

    def test_no_pause_while_paused_on_swipe(self):
        # Paused: only the pinch-hold resume gesture matters; a swipe is ignored.
        ctl = _controller([fist(0.1), fist(0.9)], swipe_velocity=1.0)
        pause, resume = threading.Event(), threading.Event()
        skip = threading.Event()
        pause.set()
        ctl.start(pause, resume, skip_event=skip)
        time.sleep(0.1)
        self.assertFalse(resume.is_set())
        self.assertFalse(skip.is_set())
        ctl.stop()

    def test_latest_hands_snapshot(self):
        target = open_hand()
        ctl = _controller([target])
        pause, resume = threading.Event(), threading.Event()
        ctl.start(pause, resume)
        time.sleep(0.05)
        snap = ctl.latest_hands()
        self.assertIsNotNone(snap)
        assert snap is not None
        np.testing.assert_array_equal(snap.landmarks, target.landmarks)
        ctl.stop()

    def test_stop_closes_recognizer(self):
        rec = FakeRecognizer([None])
        ctl = VisionController(cast(WebcamSource, FakeSource()),
                               cast(GestureRecognizer, rec),
                               poll_interval_s=0.005)
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
        from c64cast import video
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
                # Equal values, distinct objects (so a consumer mutating one
                # can't corrupt the broker's frame or another consumer's copy).
                np.testing.assert_array_equal(a, frame)
                self.assertIsNot(a, b)
            finally:
                src.release()
            cap.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
