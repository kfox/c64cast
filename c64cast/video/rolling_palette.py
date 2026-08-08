"""Rolling-window live force_palette driver.

Wraps `palette.RollingColorMapAccumulator` with a worker thread + shot-cut
detection so a LIVE scene (webcam / wled sink / generative) can run
`[color].force_palette` without a pre-scan — the pre-scan force_palette path
(`VideoScene` / `SlideshowScene`) needs a whole file up front, which a webcam or
a network pixel stream doesn't have.

Split of work:

* the **render thread** calls `submit_frame(img)` each rendered frame — cheap:
  stash the latest frame reference (under a lock) and run a throttled shot-cut
  check (downscaled HSV histogram correlation);
* a **worker thread** samples the latest frame at ~1 Hz into the rolling Lab
  window, re-bakes a stable `ColorMap` (warm-start k-means + assignment
  hysteresis live in the accumulator), and **publishes** it — but only when the
  resulting C64 color SET actually changed (or a cut fired). Within an unchanged
  set the warm-start+hysteresis map is visually identical, so not re-installing
  it avoids per-cycle shimmer;
* the render thread calls `poll_colormap()` and installs any published map on
  its display mode.

k-means is ~15-60 ms; keeping it off the render thread means it never stutters
playback. See project_force_palette_analysis_rolling.
"""

from __future__ import annotations

import logging
import threading

import cv2
import numpy as np

from .palette import ColorMap, RollingColorMapAccumulator

log = logging.getLogger(__name__)

# Worker cadence: sample the latest frame + re-bake this often.
_SAMPLE_INTERVAL_S = 1.0
# Shot-cut detection: run the (cheap) histogram compare every Nth submitted
# frame, on the downscaled HSV hue/sat histogram. A correlation below the
# threshold vs the previous checked frame = a cut (fresh palette, snap hidden).
_CUT_CHECK_EVERY = 5
_CUT_HIST_WIDTH = 160
_CUT_H_BINS = 16
_CUT_S_BINS = 8
_CUT_CORREL_THRESHOLD = 0.5


class RollingForcePalette:
    """Threaded driver installing a live, self-adapting forced palette onto a
    scene's display mode. `start()` before the first frame, `submit_frame()` +
    `poll_colormap()` each frame, `stop()` at teardown."""

    def __init__(
        self,
        n_colors: int = 16,
        indices: list[int] | None = None,
        *,
        sample_interval_s: float = _SAMPLE_INTERVAL_S,
    ):
        self._acc = RollingColorMapAccumulator(n_colors=n_colors, indices=indices)
        self._interval = sample_interval_s
        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None  # newest submitted frame (render→worker)
        self._pending: ColorMap | None = None  # published map awaiting install (worker→render)
        self._cut = False  # a shot cut was detected since the last worker cycle
        self._prev_hist: np.ndarray | None = None  # render-thread only (cut detection)
        self._frame_count = 0  # render-thread only (cut-check throttle)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="rolling-fp", daemon=True)
        self._thread.start()

    def submit_frame(self, img_bgr: np.ndarray) -> None:
        """Hand the newest rendered frame to the worker (cheap) + run the
        throttled shot-cut check. Call every frame, before quantization."""
        with self._lock:
            self._latest = img_bgr
        self._frame_count += 1
        if self._frame_count % _CUT_CHECK_EVERY == 0 and self._detect_cut(img_bgr):
            with self._lock:
                self._cut = True

    def poll_colormap(self) -> ColorMap | None:
        """Return a freshly published `ColorMap` to install (once), else None."""
        with self._lock:
            cmap, self._pending = self._pending, None
        return cmap

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _detect_cut(self, img_bgr: np.ndarray) -> bool:
        """Cheap shot-cut test: HSV hue/sat histogram correlation of a downscaled
        frame vs the previous checked one. Render-thread only (no lock needed —
        `_prev_hist` is touched here alone)."""
        h, w = img_bgr.shape[:2]
        if w > _CUT_HIST_WIDTH:
            new_h = max(1, h * _CUT_HIST_WIDTH // w)
            small = cv2.resize(img_bgr, (_CUT_HIST_WIDTH, new_h), interpolation=cv2.INTER_AREA)
        else:
            small = img_bgr
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [_CUT_H_BINS, _CUT_S_BINS], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        prev = self._prev_hist
        self._prev_hist = hist
        if prev is None:
            return False
        corr = cv2.compareHist(prev, hist, cv2.HISTCMP_CORREL)
        return corr < _CUT_CORREL_THRESHOLD

    def _run(self) -> None:
        # `published` (worker-local) is the last color set we installed; a new
        # bake is published only when its set differs (or a cut fired), so a
        # stable scene stops re-installing after it converges.
        published: tuple[int, ...] | None = None
        while not self._stop.wait(self._interval):
            with self._lock:
                frame = self._latest
                cut, self._cut = self._cut, False
            if frame is None:
                continue
            if cut:
                self._acc.clear()  # fresh palette for the new shot
            try:
                self._acc.add(frame)
                new = self._acc.result()
            except Exception:
                log.exception("rolling force_palette: re-bake failed; keeping current map")
                continue
            if new is None:
                continue
            if published is None or cut or set(new.indices) != set(published):
                with self._lock:
                    self._pending = new
                published = new.indices
