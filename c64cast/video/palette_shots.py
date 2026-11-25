"""Where a video needs new colors from the machine, found while its frames
wait in the decode buffer ([color].hardware_palette_shots).

A stretch of the video gets its own 16 colors from its first `FIT_S`
seconds, or fewer where its first frame would otherwise show before its table
could be fitted and loaded: the decoder runs only as far ahead of the picture
as the sound lets it. A stretch starts at a shot cut, or where the frames have drifted
away from the colors the current stretch was fitted to: a cut detector sees
nothing in a dissolve, and a table fitted to the frames before one leaves the
shot after it in the wrong colors.

`PaletteShotTracker.observe` runs on the demux thread for every decoded frame,
as `ShotTracker.observe` does for the auto-fit, and the two are never both in
use: a scene that pushes palettes takes its auto-fit from the pre-scan.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from collections.abc import Callable

import numpy as np

from c64cast.video.palette import hardware_palette_error
from c64cast.video.rolling_palette import _downscale
from c64cast.video.shots import SHOT_CUT_RATIO, AdaptiveCutDetector

FIT_S = 2.0
# A stretch that starts sooner than its table can load after the last one
# keeps the colors of the one before. The floor keeps a machine that loads a
# table quickly from changing colors on every rapid cut.
MIN_STRETCH_FLOOR_S = 0.5
_LOAD_MARGIN_S = 0.1
# The fit runs on a worker thread beside the render and the decode: ~0.1 s
# measured, with room for a busy host.
FIT_ALLOWANCE_S = 0.3
# A stretch has drifted once its frames sit this many times farther from its
# table than the frames it was fitted to, for this many frames running.
DRIFT_RATIO = 1.6
DRIFT_FRAMES = 5

_seq = itertools.count()


def min_stretch_s(load_time_s: float) -> float:
    """The shortest stretch worth its own table on a machine whose writer
    loads one in `load_time_s`."""
    return max(MIN_STRETCH_FLOOR_S, load_time_s + _LOAD_MARGIN_S)


def ready_ahead_s(load_time_s: float) -> float:
    """How long before a stretch's first frame shows its fit frames must be
    in, on a machine whose writer loads a table in `load_time_s`."""
    return load_time_s + FIT_ALLOWANCE_S


class PaletteShot:
    """One stretch: its first frame's pts, the shaped frames its table is
    fitted to, and, once fitted, the table and how far those frames sit from
    it. `starts_over` marks the first stretch after a seek, ahead of which
    nothing still queued will be shown."""

    def __init__(self, pts: float, *, starts_over: bool = False) -> None:
        self.pts = pts
        self.starts_over = starts_over
        self.seq = next(_seq)
        self.frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._fit: tuple[np.ndarray, float] | None = None

    def fitted(self, table_bgr: np.ndarray, baseline: float) -> None:
        with self._lock:
            self._fit = (table_bgr, baseline)

    @property
    def fit(self) -> tuple[np.ndarray, float] | None:
        with self._lock:
            return self._fit


class PaletteShotTracker:
    """Finds stretches in decode order and hands each one to `on_ready` once
    its fit frames are in.

    `shape` turns a decoded picture into what the display mode's quantizer
    sees, which is what the table is fitted to and what drift is measured
    on. A stretch is handed over once it has `FIT_S` of frames, or once its
    first frame is due on screen within `ready_ahead_s`."""

    def __init__(
        self,
        shape: Callable[[np.ndarray], np.ndarray],
        on_ready: Callable[[PaletteShot], None],
        *,
        min_stretch_s: float,
        ready_ahead_s: float,
    ) -> None:
        self._shape = shape
        self._on_ready = on_ready
        self._min_stretch_s = min_stretch_s
        self._ready_ahead_s = ready_ahead_s
        self._detector = AdaptiveCutDetector(ratio=SHOT_CUT_RATIO)
        self._recent: deque[tuple[float, np.ndarray]] = deque(
            maxlen=max(self._detector.window + 1, DRIFT_FRAMES)
        )
        self._shot: PaletteShot | None = None
        self._collecting = False
        self._drifting = 0
        self._restarted = True

    def restart(self, shown_bgr: np.ndarray | None) -> None:
        """Forget the frames before a seek. The next frame observed starts a
        stretch, whatever it shows."""
        self._detector = AdaptiveCutDetector(ratio=SHOT_CUT_RATIO)
        self._recent.clear()
        self._restarted = True

    def observe(
        self, pts: float, frame_bgr: np.ndarray, shown_pts: float | None = None
    ) -> tuple[float, PaletteShot] | None:
        """Fold in the next decoded frame. `shown_pts` is where the picture is
        now, None before it has started. Returns the pts of a frame just
        found to start a stretch, with that stretch."""
        small = _downscale(frame_bgr)
        shaped = self._shape(small)
        self._recent.append((pts, shaped))
        if self._restarted:
            self._restarted = False
            self._detector.push(small)
            return self._start([(pts, shaped)], starts_over=True)
        cut = self._detector.push(small)
        if cut:
            # The detector decides about a frame `window` frames late.
            since = list(self._recent)[-1 - self._detector.window :]
            if self._room_after(since[0][0]):
                if self._collecting and self._shot is not None:
                    del self._shot.frames[-(len(since) - 1) :]
                return self._start(since)
        elif self._drifted(shaped):
            since = list(self._recent)[-DRIFT_FRAMES:]
            if self._room_after(since[0][0]):
                return self._start(since)
        self._collect(pts, shaped, shown_pts)
        return None

    def _room_after(self, pts: float) -> bool:
        return self._shot is None or pts - self._shot.pts >= self._min_stretch_s

    def _drifted(self, shaped: np.ndarray) -> bool:
        fit = None if self._shot is None else self._shot.fit
        if fit is None:
            return False
        table, baseline = fit
        if hardware_palette_error(shaped, table) > DRIFT_RATIO * baseline:
            self._drifting += 1
        else:
            self._drifting = 0
        return self._drifting >= DRIFT_FRAMES

    def _collect(self, pts: float, shaped: np.ndarray, shown_pts: float | None) -> None:
        if not self._collecting or self._shot is None:
            return
        if pts - self._shot.pts >= FIT_S:
            self._finish()
            return
        self._shot.frames.append(shaped)
        if shown_pts is not None and self._shot.pts - shown_pts <= self._ready_ahead_s:
            self._finish()

    def _finish(self) -> None:
        if self._collecting and self._shot is not None:
            self._collecting = False
            self._on_ready(self._shot)

    def _start(
        self, frames: list[tuple[float, np.ndarray]], *, starts_over: bool = False
    ) -> tuple[float, PaletteShot]:
        self._finish()
        shot = PaletteShot(frames[0][0], starts_over=starts_over)
        shot.frames.extend(f for _, f in frames)
        self._shot = shot
        self._collecting = True
        self._drifting = 0
        return shot.pts, shot
