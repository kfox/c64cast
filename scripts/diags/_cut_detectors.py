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
  * `AdaptiveCutDetector` follows PySceneDetect's AdaptiveDetector; it is
    the one `c64cast.video.shots` runs in production.
  * `FfmpegScene` follows ffmpeg's `select` scene score.
"""

from __future__ import annotations

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
