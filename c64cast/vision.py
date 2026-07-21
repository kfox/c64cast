"""Camera-as-input: turn the webcam into a gesture/landmark input device.

Architectural sibling to [keyboard.py](c64cast/keyboard.py)'s
`CommodoreKeyPoller`. A background thread reads frames from the shared
`WebcamSource` broker, runs hand tracking, and translates hand *gestures* into
the same `pause`/`resume`/`skip`/`cycle` thread events the keyboard poller and
the HTTP control plane feed — so the Playlist run loop can't tell which control
surface a press came from. It also exposes the raw hand *landmarks*
(`latest_hands()`) as continuous state for future consumers (fingerpainting,
pose/face scenes).

Gesture → control mapping mirrors the keyboard semantics exactly:

  * PINCH (thumb+index) pressed (running)        → pause_event
  * PINCH held `hold_threshold_s` while paused   → resume_event
  * SWIPE (fast horizontal wrist motion)         → skip_event   (running only)
  * OPEN_HAND (all fingers extended) (running)   → cycle_event

**Performance mode (Live DJ/VJ Phase 6).** With a Playlist bound via
`bind_performance()` (gated on `[vision].performance`), the RUNNING-state
gestures instead drive the clip-launch grid — a hands-free performance surface:

  * SWIPE      → `pl.performance.advance_clip()` (launch the next clip slot)
  * PINCH held → `pl.toggle_effect_layer(0)`     (bypass effect layer 0)
  * OPEN_HAND held → `pl.toggle_effect_layer(1)` (bypass effect layer 1)

Both paths bottom out in the same thread-safe hand-offs the MIDI/web surfaces
use (an enqueued `ClipEvent`, a GIL-atomic `enabled` flip) — no scene mutation
on the poll thread. The paused-state pinch-hold-to-resume gesture is unchanged.

The hand tracker is pluggable behind the `GestureRecognizer` protocol; the
shipped implementation is `MediaPipeHandRecognizer` (MediaPipe Tasks
HandLandmarker), lazy-imported so the rest of the app and the whole test suite
run without the `vision` extra installed. Tests inject a scripted fake
recognizer + fake source, the same way `test_keyboard.py` scripts `$028D`.
"""

from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import cv2
import numpy as np

from ._native_io import silence_native_stderr
from ._pollthread import PollThread

if TYPE_CHECKING:
    from .video import WebcamSource

log = logging.getLogger(__name__)

# MediaPipe Hands landmark indices (21-point model).
WRIST = 0
THUMB_TIP = 4
INDEX_TIP = 8
MIDDLE_TIP = 12
RING_TIP = 16
PINKY_TIP = 20
# Finger PIP joints (the middle knuckle), used for the extended/curled test.
INDEX_PIP = 6
MIDDLE_PIP = 10
RING_PIP = 14
PINKY_PIP = 18

# Downscale frames wider than this before hand inference — full 1080p is wasted
# work for landmark tracking and starves the render pipeline of CPU.
MP_MAX_WIDTH = 640

# Swipe gating: a swipe must be horizontally dominant (|dx| > |dy|) so raising
# the hand (vertical) isn't read as a swipe, and the hand must have been present
# this many ticks first so the entry transient (landmarks settling) is ignored.
SWIPE_SETTLE_FRAMES = 2

# A swipe must persist at least this many consecutive ticks. 1 = a single fast
# horizontal frame fires (the horizontal-dominance + settle gates already reject
# vertical raises and entry transients, so a 2-frame requirement just made
# deliberate swipes feel sluggish).
SWIPE_MIN_FRAMES = 1

# Held poses (pinch / open hand) only accrue their dwell while the wrist is
# moving slower than this (normalized frame-widths/sec). Generous enough that a
# deliberately-held hand can drift a fair bit, low enough that a hand reaching /
# waving / gesturing-while-talking doesn't rack up cycles. Sits below
# swipe_velocity so "still pose" and "fast swipe" don't overlap.
STILL_SPEED = 0.35

# Default download URL for the HandLandmarker model bundle (printed in the
# error when the model file is missing).
MODEL_DOWNLOAD_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)


class Gesture(enum.Enum):
    """A static (single-frame) hand pose. SWIPE is temporal and handled in the
    controller from wrist-motion history, not here."""

    NONE = "none"
    PINCH = "pinch"
    OPEN_HAND = "open_hand"


@dataclass
class HandState:
    """One detected hand at one instant.

    `landmarks` is a (21, 3) float array of normalized coordinates: x and y in
    [0, 1] across the (already mirror-corrected) frame, z is relative depth.
    `handedness` is "Left"/"Right" (or "" if unknown)."""

    landmarks: np.ndarray
    handedness: str = ""

    def point(self, idx: int) -> np.ndarray:
        return self.landmarks[idx]


class GestureRecognizer(Protocol):
    """Frame → primary HandState. Returns None when no hand is detected (or the
    frame is unusable), so the controller can skip the tick — the visual
    analogue of `CommodoreKeyPoller._read_modifiers` returning None."""

    def process(self, frame: np.ndarray, timestamp_ms: int) -> HandState | None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Pure gesture classification (no mediapipe, no camera — directly unit-tested).
# ---------------------------------------------------------------------------


def _dist(a: np.ndarray, b: np.ndarray) -> float:
    # 2D (x,y) distance in normalized frame units; depth (z) ignored so the
    # pinch threshold is a stable on-screen distance regardless of hand depth.
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


def is_pinch(hand: HandState, threshold: float) -> bool:
    """Thumb tip and index tip brought together."""
    return _dist(hand.point(THUMB_TIP), hand.point(INDEX_TIP)) < threshold


def _finger_extended(hand: HandState, tip: int, pip: int) -> bool:
    """A finger counts as extended when its tip is farther from the wrist than
    its PIP joint. Distance-from-wrist is orientation-agnostic (works whether
    the hand points up, down, or sideways), unlike a raw tip.y < pip.y test."""
    wrist = hand.point(WRIST)
    return _dist(hand.point(tip), wrist) > _dist(hand.point(pip), wrist)


def count_extended_fingers(hand: HandState) -> int:
    """Number of the four non-thumb fingers that are extended."""
    return sum(
        _finger_extended(hand, tip, pip)
        for tip, pip in (
            (INDEX_TIP, INDEX_PIP),
            (MIDDLE_TIP, MIDDLE_PIP),
            (RING_TIP, RING_PIP),
            (PINKY_TIP, PINKY_PIP),
        )
    )


# A pinch must have the hand otherwise OPEN (this many of the four fingers
# extended). Thumb-near-index alone can't tell a deliberate pinch sign from a
# fist/closed hand — and a closed hand on its way up (raise → open) would
# otherwise read as a pinch and fire an unwanted pause. HW-observed: a
# deliberate pinch reads 4 extended fingers, a fist reads 0.
PINCH_MIN_EXTENDED = 3


def classify_static(hand: HandState, *, pinch_threshold: float) -> Gesture:
    """Per-frame pose. PINCH (thumb+index together with the hand otherwise open)
    wins over OPEN_HAND; a closed/fist hand is neither (it falls to NONE)."""
    fingers = count_extended_fingers(hand)
    if fingers >= PINCH_MIN_EXTENDED and is_pinch(hand, pinch_threshold):
        return Gesture.PINCH
    if fingers >= 4:
        return Gesture.OPEN_HAND
    return Gesture.NONE


# ---------------------------------------------------------------------------
# MediaPipe-backed recognizer (lazy import — needs the `vision` extra).
# ---------------------------------------------------------------------------

_mp: Any = None
_MP_AVAILABLE: bool | None = None  # tri-state: None = not yet probed


def _ensure_mediapipe() -> bool:
    """Import mediapipe on demand; cache the result. Returns availability.

    Deferred (like video.py's `_ensure_pyav`) so importing c64cast doesn't
    drag in mediapipe + its heavy native deps unless a vision scene runs."""
    global _mp, _MP_AVAILABLE
    if _MP_AVAILABLE is not None:
        return _MP_AVAILABLE
    try:
        import mediapipe as mp

        _mp = mp
        _MP_AVAILABLE = True
    except ImportError:
        _MP_AVAILABLE = False
    return _MP_AVAILABLE


class MediaPipeHandRecognizer:
    """`GestureRecognizer` backed by MediaPipe Tasks HandLandmarker."""

    def __init__(
        self,
        model_path: str,
        *,
        num_hands: int = 1,
        min_detection_confidence: float = 0.7,
        min_tracking_confidence: float = 0.5,
    ):
        if not _ensure_mediapipe():
            raise RuntimeError(
                "vision controller requires mediapipe: "
                "install with `uv sync --extra vision` "
                "(or `pip install c64cast[vision]`)"
            )
        if not os.path.exists(model_path):
            raise RuntimeError(
                f"HandLandmarker model not found at {model_path!r}. Download it "
                f"to that path:\n  {MODEL_DOWNLOAD_URL}\n"
                "or set [vision].model_path to where you saved it."
            )
        # Import the Tasks submodules explicitly — `import mediapipe` does not
        # guarantee `tasks.python.vision` is bound as an attribute.
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        # Image / ImageFormat are top-level on the mediapipe package; cache them
        # so the per-frame `process()` path doesn't re-resolve attributes.
        self._mp_image = _mp.Image
        self._image_format = _mp.ImageFormat
        base = mp_python.BaseOptions(model_asset_path=model_path)
        options = mp_vision.HandLandmarkerOptions(
            base_options=base,
            num_hands=num_hands,
            min_hand_detection_confidence=min_detection_confidence,
            min_hand_presence_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
            running_mode=mp_vision.RunningMode.VIDEO,
        )
        # create_from_options emits the GL/XNNPACK/feedback noise; the warm-up
        # inference (timestamp 0; real ticks always land at > 0) emits the
        # landmark_projection warning. Run both inside the fd silence so all of
        # mediapipe's startup chatter is swallowed here on the main thread.
        with silence_native_stderr():
            self._landmarker = mp_vision.HandLandmarker.create_from_options(options)
            warm = self._mp_image(
                image_format=self._image_format.SRGB, data=np.zeros((64, 64, 3), dtype=np.uint8)
            )
            self._landmarker.detect_for_video(warm, 0)
        # VIDEO mode requires strictly increasing timestamps per instance. The
        # warm-up consumed 0; track the last value so process() can never feed
        # a stale or equal timestamp regardless of the caller's clock.
        self._last_ts_ms = 0

    def process(self, frame: np.ndarray, timestamp_ms: int) -> HandState | None:
        # Hand tracking doesn't need full sensor resolution — a 1080p webcam
        # frame is ~9x the pixels of a 640-wide one and makes inference (and the
        # CPU it steals from the render pipeline) much heavier. Downscale first;
        # landmarks come back normalized [0,1], so coordinates are unaffected.
        h, w = frame.shape[:2]
        if w > MP_MAX_WIDTH:
            scale = MP_MAX_WIDTH / w
            frame = cv2.resize(
                frame, (MP_MAX_WIDTH, round(h * scale)), interpolation=cv2.INTER_AREA
            )
        # cv2 frames are BGR; MediaPipe wants RGB.
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = self._mp_image(image_format=self._image_format.SRGB, data=rgb)
        # Clamp to strictly increasing — honor the caller's clock when it's
        # ahead, else bump by 1ms (guards the first call vs the ts=0 warm-up and
        # any same-millisecond collisions).
        ts = max(timestamp_ms, self._last_ts_ms + 1)
        self._last_ts_ms = ts
        result = self._landmarker.detect_for_video(mp_image, ts)
        if not result.hand_landmarks:
            return None
        lms = result.hand_landmarks[0]
        arr = np.array([[lm.x, lm.y, lm.z] for lm in lms], dtype=np.float32)
        handedness = ""
        if result.handedness and result.handedness[0]:
            handedness = result.handedness[0][0].category_name
        return HandState(landmarks=arr, handedness=handedness)

    def close(self) -> None:
        self._landmarker.close()


# ---------------------------------------------------------------------------
# VisionController — sibling to CommodoreKeyPoller.
# ---------------------------------------------------------------------------


class VisionController:
    """Watches the camera for gestures and drives the playlist control events.

    Mirrors `CommodoreKeyPoller`'s shape: same `start(...)`/`stop()` surface so
    the Playlist starts/stops it interchangeably with the keyboard poller."""

    def __init__(
        self,
        source: WebcamSource,
        recognizer: GestureRecognizer,
        *,
        poll_interval_s: float = 0.066,
        hold_threshold_s: float = 3.0,
        gesture_cooldown_s: float = 1.0,
        gesture_dwell_s: float = 0.4,
        pinch_threshold: float = 0.05,
        swipe_velocity: float = 0.4,
        mirror: bool = True,
        name: str = "system",
    ):
        self.source = source
        self.recognizer = recognizer
        self.name = name
        # Child of the c64cast.vision logger so ensemble runs can tell which
        # system a gesture came from (same convention as keyboard.py).
        self.log = logging.getLogger(f"c64cast.vision.{name}")
        self.poll_interval_s = poll_interval_s
        self.hold_threshold_s = hold_threshold_s
        self.gesture_cooldown_s = gesture_cooldown_s
        # A pose (pinch / open hand) must be held STILL this many ticks before
        # it fires — the dwell gate (see STILL_SPEED). It separates a held pose
        # (pause / cycle) from a hand merely passing through that pose, and from
        # a busy/moving hand that happens to be open. Swipe is motion, not a
        # pose, so it bypasses the gate.
        self._dwell_frames = max(1, round(gesture_dwell_s / poll_interval_s))
        self.pinch_threshold = pinch_threshold
        # swipe_velocity is in normalized frame-widths per second. HW-tuned:
        # deliberate swipes peak ~0.5-1.1, slow drift stays < ~0.2.
        self.swipe_velocity = swipe_velocity
        self.mirror = mirror
        self._poll = PollThread(self._loop, name="vision-poll", manual=True, join_timeout=1.0)
        self._pause_event: threading.Event | None = None
        self._resume_event: threading.Event | None = None
        self._skip_event: threading.Event | None = None
        self._cycle_event: threading.Event | None = None
        # Optional performance binding (Live DJ/VJ Phase 6). When set (via
        # bind_performance, gated on [vision].performance), the RUNNING-state
        # gestures drive the clip-launch grid instead of transport: swipe =
        # advance to the next clip, pinch-hold = bypass fx layer 0, open-hand-hold
        # = bypass fx layer 1. Kept as a duck-typed handle (the Playlist) so
        # vision.py stays import-light — it only calls `.performance.advance_clip`
        # (enqueue-only, thread-safe) and `.toggle_effect_layer` (a GIL-atomic
        # bool write), never mutating a scene on this poll thread.
        self._perf: Any = None
        # Continuous-state snapshot for future consumers (fingerpainting).
        self._state_lock = threading.Lock()
        self._latest_hand: HandState | None = None

    def start(
        self,
        pause_event: threading.Event,
        resume_event: threading.Event,
        skip_event: threading.Event | None = None,
        cycle_event: threading.Event | None = None,
        menu_event: threading.Event | None = None,
        menu_active: threading.Event | None = None,
        menu_eligible: threading.Event | None = None,
        nav_queue: Any = None,
    ):
        """Begin watching. Event semantics match `CommodoreKeyPoller.start`.

        menu_event/menu_active/menu_eligible/nav_queue are accepted for
        signature parity with the keyboard poller (the Playlist starts both
        controllers identically); gesture-driven menu open/nav is a later
        phase, so they're unused here."""
        self._pause_event = pause_event
        self._resume_event = resume_event
        self._skip_event = skip_event
        self._cycle_event = cycle_event
        self._poll.start()

    def stop(self):
        self._poll.stop()
        try:
            self.recognizer.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            self.log.debug("recognizer.close() failed", exc_info=True)

    def bind_performance(self, playlist: Any) -> None:
        """Route RUNNING-state gestures to the clip-launch grid instead of
        transport (Live DJ/VJ Phase 6). `playlist` is the owning Playlist; wired
        by cli.build_stack when [vision].performance is on. Call before/after
        start() interchangeably — the poll loop reads `self._perf` each tick."""
        self._perf = playlist

    def latest_hands(self) -> HandState | None:
        """Thread-safe snapshot of the most recent detected hand (or None)."""
        with self._state_lock:
            return self._latest_hand

    def _read_hand(self, timestamp_ms: int) -> HandState | None:
        """Grab the latest frame, mirror it, and run the recognizer.

        Returns None on no-frame / no-hand / recognizer error — the caller
        treats that as 'couldn't tell' and skips the tick (no state change),
        the visual analogue of a failed $028D read."""
        frame = self.source.read()
        if frame is None:
            return None
        if self.mirror:
            frame = cv2.flip(frame, 1)
        try:
            hand = self.recognizer.process(frame, timestamp_ms)
        except Exception:  # noqa: BLE001 — a bad frame shouldn't kill the thread
            self.log.debug("recognizer.process() failed", exc_info=True)
            return None
        with self._state_lock:
            self._latest_hand = hand
        return hand

    @staticmethod
    def _wrist_xy(hand: HandState | None) -> tuple[float, float] | None:
        if hand is None:
            return None
        return float(hand.point(WRIST)[0]), float(hand.point(WRIST)[1])

    def _loop(self, stop: threading.Event):
        assert self._pause_event is not None
        assert self._resume_event is not None
        held_since: float | None = None  # pinch-hold timer (resume)
        last_static = Gesture.NONE  # for the dwell run-length
        static_run = 0  # consecutive STILL ticks of the pose
        static_fired = False  # one fire per held pose
        swipe_run = 0  # consecutive swipe-eligible ticks
        last_fire = 0.0  # cooldown timer
        prev_wrist: tuple[float, float] | None = None
        hand_frames = 0  # consecutive ticks a hand is present
        last_tick = time.monotonic()
        # detect_for_video requires monotonically increasing ms timestamps.
        t0 = time.monotonic()

        while not stop.wait(self.poll_interval_s):
            now = time.monotonic()
            dt = now - last_tick
            last_tick = now
            hand = self._read_hand(int((now - t0) * 1000))
            hand_frames = hand_frames + 1 if hand is not None else 0
            static = (
                classify_static(hand, pinch_threshold=self.pinch_threshold)
                if hand is not None
                else Gesture.NONE
            )

            # Wrist motion since the last tick.
            cur = self._wrist_xy(hand)
            if cur is not None and prev_wrist is not None and dt > 0:
                dx, dy = cur[0] - prev_wrist[0], cur[1] - prev_wrist[1]
                speed = (dx * dx + dy * dy) ** 0.5 / dt
                horiz_speed = abs(dx) / dt
                horizontal = abs(dx) > abs(dy)  # sideways, not a vertical raise
            else:
                speed = horiz_speed = 0.0
                horizontal = False
            still = cur is not None and speed < STILL_SPEED

            if self._pause_event.is_set():
                # PAUSED: only the pinch hold-to-resume gesture matters
                # (mirrors keyboard.py — CTRL/SHIFT analogues are ignored).
                if static == Gesture.PINCH:
                    if held_since is None:
                        held_since = now
                    elif now - held_since >= self.hold_threshold_s:
                        self.log.info(
                            "pinch held %.1fs while paused — resuming", self.hold_threshold_s
                        )
                        self._resume_event.set()
                        held_since = None
                else:
                    held_since = None
                prev_wrist, swipe_run = cur, 0
                continue

            # RUNNING.
            #  * Swipe = SUSTAINED (>= SWIPE_MIN_FRAMES ticks), horizontally-
            #    dominant, fast wrist motion. A vertical raise (|dy|>|dx|) or a
            #    one-frame spike doesn't count.
            #  * Pinch / open-hand are HELD poses: they only accrue dwell while
            #    the hand is STILL, so a busy/moving hand (reaching, gesturing
            #    while talking) doesn't rack up cycles, and a hand passing
            #    through "open" on the way to a pinch/swipe can't flicker one.
            #  Priority: a moving hand is a swipe, a still hand is a pose. One
            #  fire per tick; a shared cooldown debounces.
            settled = hand_frames > SWIPE_SETTLE_FRAMES
            swipe_frame = settled and horizontal and horiz_speed >= self.swipe_velocity
            swipe_run = swipe_run + 1 if swipe_frame else 0
            is_swipe = swipe_run >= SWIPE_MIN_FRAMES

            if static != last_static:
                static_run = 0
                static_fired = False
            static_run = static_run + 1 if (static != Gesture.NONE and still) else 0
            last_static = static
            held = static_run >= self._dwell_frames and not static_fired

            cooled = now - last_fire >= self.gesture_cooldown_s
            perf = self._perf  # snapshot once (bind_performance may set it live)

            if cooled and is_swipe and perf is not None:
                slot = perf.performance.advance_clip()
                self.log.info("swipe detected — launching next clip (slot %s)", slot)
                last_fire = now
                swipe_run = 0
            elif cooled and is_swipe and self._skip_event is not None:
                self.log.info("swipe detected — skipping to next scene")
                self._skip_event.set()
                last_fire = now
                swipe_run = 0
            elif cooled and held and static == Gesture.PINCH and perf is not None:
                enabled = perf.toggle_effect_layer(0)
                self.log.info("pinch held — fx layer 0 %s", "on" if enabled else "bypass")
                last_fire = now
                static_fired = True
            elif cooled and held and static == Gesture.PINCH:
                self.log.info("pinch held — pausing")
                self._pause_event.set()
                last_fire = now
                static_fired = True
            elif cooled and held and static == Gesture.OPEN_HAND and perf is not None:
                enabled = perf.toggle_effect_layer(1)
                self.log.info("open hand held — fx layer 1 %s", "on" if enabled else "bypass")
                last_fire = now
                static_fired = True
            elif cooled and held and static == Gesture.OPEN_HAND and self._cycle_event is not None:
                self.log.info("open hand held — cycling display style")
                self._cycle_event.set()
                last_fire = now
                static_fired = True

            prev_wrist = cur
            held_since = None
