"""Three shot-cut detectors run over the same stream of frames, for scoring
against hand-labeled cuts (`video_cut_eval.py`).

Each takes the cropped frame `VideoScene` hands its display mode, one frame
at a time, and reports the index of each frame it calls the first frame of a
new shot. All three look at the frame downscaled the way the existing
detector does it (`rolling_palette._downscale`, 160 px wide).

  * `ProductionHistogram` is c64cast's own detector, run through the
    `RollingForcePalette` object that ships, so its threshold and its
    every-5th-frame throttle are the production ones. `every_frame=True`
    checks every frame instead, which separates the threshold from the
    throttle.
  * `AdaptiveRatio` follows PySceneDetect's AdaptiveDetector.
  * `FfmpegScene` follows ffmpeg's `select` scene score.
"""

from __future__ import annotations

from collections import deque

import cv2
import numpy as np

from c64cast.video import rolling_palette
from c64cast.video.rolling_palette import RollingForcePalette


def analysis_frame(cropped_bgr: np.ndarray) -> np.ndarray:
    return rolling_palette._downscale(cropped_bgr)


class ProductionHistogram:
    def __init__(self, *, every_frame: bool = False) -> None:
        self._palette = RollingForcePalette()
        self._every_frame = every_frame
        self._index = -1

    def push(self, frame: np.ndarray) -> list[int]:
        self._index += 1
        if self._every_frame:
            return [self._index] if self._palette._detect_cut(frame) else []
        self._palette.submit_frame(frame)
        with self._palette._lock:
            cut, self._palette._cut = self._palette._cut, False
        return [self._index] if cut else []


def content_score(prev_hsv: np.ndarray, hsv: np.ndarray) -> float:
    """PySceneDetect's ContentDetector score with its default weights: hue,
    saturation and value 1.0 each, edges 0, so the mean of the three
    channels' mean absolute differences."""
    diff = np.abs(hsv.astype(np.int32) - prev_hsv.astype(np.int32))
    return float(diff.reshape(-1, 3).mean(axis=0).mean())


class AdaptiveRatio:
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


class FfmpegScene:
    """ffmpeg's `select` scene score on luma: min(MAFD, |MAFD - previous
    MAFD|) / 100, where MAFD is the mean absolute frame difference. The
    second term is what keeps steady motion, whose MAFD is high but stable,
    from scoring as a cut."""

    def __init__(self, threshold: float = 0.3) -> None:
        self.threshold = threshold
        self._prev_luma: np.ndarray | None = None
        self._prev_mafd = 0.0
        self._index = -1
        self.scores: list[float] = []

    def push(self, frame: np.ndarray) -> list[int]:
        self._index += 1
        luma = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.int32)
        prev, self._prev_luma = self._prev_luma, luma
        if prev is None:
            self.scores.append(0.0)
            return []
        mafd = float(np.abs(luma - prev).mean())
        score = min(mafd, abs(mafd - self._prev_mafd)) / 100.0
        self._prev_mafd = mafd
        self.scores.append(score)
        return [self._index] if score > self.threshold else []
