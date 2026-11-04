"""Shot cuts in a video, found while its frames wait in the decode buffer, and
the color fit of each new shot, so `VideoScene` can refit `[color].auto_fit`
at a cut instead of carrying the opening's fit across the whole video.

`ShotTracker.observe` runs on the demux thread for every decoded frame. A cut
is decided `AdaptiveCutDetector.window` frames after it, and the new shot's
`ShotFit` keeps folding in frames until it has seen `fit_frames` of them, which
is about the buffer's depth: by the time the cut frame is shown, its fit has
seen as much of the shot as the buffer holds.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable

import cv2
import numpy as np

from c64cast.video.palette import ColorFit, ColorFitAccumulator
from c64cast.video.rolling_palette import _downscale

# PySceneDetect's AdaptiveDetector defaults to 3.0. On a VHS transfer, noise
# raises the neighboring frames' scores, and its real cuts reached only
# 2.0-2.2; at 2.0 the extra detections on clean sources were camera moves and
# flashes, where a refit lands on a change the whole picture makes anyway.
SHOT_CUT_RATIO = 2.0


def content_score(prev_hsv: np.ndarray, hsv: np.ndarray) -> float:
    """PySceneDetect's ContentDetector score with its default weights: hue,
    saturation and value 1.0 each, edges 0, so the mean of the three
    channels' mean absolute differences."""
    diff = np.abs(hsv.astype(np.int32) - prev_hsv.astype(np.int32))
    return float(diff.reshape(-1, 3).mean(axis=0).mean())


class AdaptiveCutDetector:
    """PySceneDetect's AdaptiveDetector: a frame is a cut when its content
    score is at least `ratio` times the mean score of the `window` frames on
    each side of it, and at least `min_score` outright, and the previous cut
    was `min_shot` or more frames ago. The decision about frame t waits for
    frame t + window."""

    def __init__(
        self,
        ratio: float = 3.0,
        window: int = 2,
        min_score: float = 15.0,
        min_shot: int = 15,
    ) -> None:
        self.ratio = ratio
        self.window = window
        self.min_score = min_score
        self.min_shot = min_shot
        self._prev_hsv: np.ndarray | None = None
        self._scores: deque[tuple[int, float]] = deque(maxlen=2 * window + 1)
        self._index = -1
        self._last_cut: int | None = None

    def push(self, frame: np.ndarray) -> list[int]:
        self._index += 1
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        prev, self._prev_hsv = self._prev_hsv, hsv
        if prev is None:
            return []
        self._scores.append((self._index, content_score(prev, hsv)))
        if len(self._scores) < 2 * self.window + 1:
            return []
        target, score = self._scores[self.window]
        others = [s for i, (_, s) in enumerate(self._scores) if i != self.window]
        mean = sum(others) / len(others)
        if mean < 1e-5:
            ratio = 255.0 if score >= self.min_score else 0.0
        else:
            ratio = min(score / mean, 255.0)
        if ratio < self.ratio or score < self.min_score:
            return []
        if self._last_cut is not None and target - self._last_cut < self.min_shot:
            return []
        self._last_cut = target
        return [target]


class ShotFit:
    """The color fit of one shot's first `frames` frames, filled on the demux
    thread and read on the playlist thread."""

    def __init__(self, frames: int) -> None:
        self._lock = threading.Lock()
        self._acc = ColorFitAccumulator(strength=1.0)
        self._remaining = frames

    def add(self, small_bgr: np.ndarray) -> None:
        with self._lock:
            if self._remaining > 0:
                self._acc.add(small_bgr)
                self._remaining -= 1

    def result(self) -> ColorFit | None:
        with self._lock:
            return self._acc.result()


class ShotTracker:
    """Detects cuts in decode order and builds each new shot's `ShotFit`.

    `prepare` turns a decoded frame into the picture the display mode is
    given (`VideoScene` passes its aspect crop), so the fit sees what the
    scene shows."""

    def __init__(self, prepare: Callable[[np.ndarray], np.ndarray], fit_frames: int) -> None:
        self._prepare = prepare
        self._fit_frames = fit_frames
        self._detector = AdaptiveCutDetector(ratio=SHOT_CUT_RATIO)
        self._recent: deque[tuple[float, np.ndarray]] = deque(maxlen=self._detector.window + 1)
        self._shot: ShotFit | None = None

    def observe(self, pts: float, frame_bgr: np.ndarray) -> tuple[float, ShotFit] | None:
        """Fold in the next decoded frame. Returns the pts of a frame just
        found to start a new shot, with that shot's fit, which keeps filling
        from the frames observed after this."""
        small = _downscale(self._prepare(frame_bgr))
        self._recent.append((pts, small))
        found = None
        if self._detector.push(small):
            self._shot = ShotFit(self._fit_frames)
            # The cut frame and the ones after it arrived before the decision.
            for _, earlier in self._recent:
                self._shot.add(earlier)
            found = (self._recent[0][0], self._shot)
        elif self._shot is not None:
            self._shot.add(small)
        return found
