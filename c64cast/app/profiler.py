"""Per-frame profiling harness.

Enabled by the ``--profile`` CLI flag (off by default, zero overhead when
off). Records the wall-clock breakdown of each frame — sleep-to-deadline,
the CPU render path (split into ``compose`` / ``overlay_compose`` /
``push``), and the DMA write counters drained from ``api.stats`` — then
emits a periodic per-scene summary in the existing log stream.

Two collaborators read the global profiler instead of receiving it through
an argument:

  * ``Playlist.run`` opens the frame and the ``wait`` and ``cpu_render``
    top-level stages, and calls ``record_counts`` at the frame boundary.
  * ``scenes._render_with_overlays`` opens ``compose`` / ``overlay_compose``
    / ``push`` sub-stages.

A process-global accessor keeps the second one off the call signature —
there's only ever one Playlist + one API per process, so a singleton is
appropriate.

When profiling is off, ``get_profiler()`` returns a ``NullProfiler`` whose
context managers and methods are no-ops, so the hot path pays only the cost
of one attribute lookup and a Python ``with`` statement (~0.5µs)."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager


class _Stats:
    """Bounded ring of float samples with avg / p50 / p95 / max readouts.

    A 64-sample window covers roughly 2s at 30fps, which is the right
    horizon for a 10s summary cadence — long enough to smooth single-frame
    outliers, short enough that the numbers track scene transitions."""

    __slots__ = ("_samples", "_capacity")

    def __init__(self, capacity: int = 64):
        self._samples: deque[float] = deque(maxlen=capacity)
        self._capacity = capacity

    def add(self, v: float) -> None:
        self._samples.append(v)

    def count(self) -> int:
        return len(self._samples)

    def summary(self) -> tuple[float, float, float, float]:
        """Return (avg, p50, p95, max). Empty ring returns all zeros."""
        n = len(self._samples)
        if n == 0:
            return 0.0, 0.0, 0.0, 0.0
        sorted_s = sorted(self._samples)
        avg = sum(sorted_s) / n
        # nearest-rank percentiles — clamp index to [0, n-1].
        p50 = sorted_s[min(n - 1, int(0.50 * n))]
        p95 = sorted_s[min(n - 1, int(0.95 * n))]
        return avg, p50, p95, sorted_s[-1]


class NullProfiler:
    """No-op profiler returned by ``get_profiler()`` when profiling is off.

    Every method is a stub; the two context-manager methods yield without
    measuring. Used so call sites don't need an ``if profiler:`` guard.
    Parameter names must match ``FrameProfiler``'s so keyword-arg calls
    work against the union type."""

    enabled = False

    @contextmanager
    def frame(self, scene_name: str) -> Iterator[None]:
        del scene_name
        yield

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        del name
        yield

    def record_counts(self, writes: int, bytes_: int) -> None:
        del writes, bytes_

    def emit_if_due(self, now: float, log: logging.Logger) -> bool:
        del now, log
        return False


class FrameProfiler:
    """Active profiler. Collects per-(scene, stage) histograms and emits
    a periodic summary at ``interval`` seconds.

    Threading note: the profiler is touched by the Playlist's main thread
    (top-level stages + counts) and by the same thread inside
    ``_render_with_overlays`` (sub-stages). It is NOT thread-safe across
    arbitrary threads — don't call from worker threads."""

    enabled = True

    def __init__(self, interval: float = 10.0):
        self.interval = interval
        # Two-level dict: scene_name -> stage_name -> _Stats. Scene-level
        # keys always include "frame_total"; counts go under "writes" /
        # "bytes".
        self._stats: dict[str, dict[str, _Stats]] = {}
        self._last_emit: float = 0.0
        # Per-frame scratch: the active scene name and a {stage -> elapsed}
        # accumulator populated by stage() and drained by frame() on exit.
        self._cur_scene: str | None = None
        self._cur_stages: dict[str, float] = {}

    def _bucket(self, scene_name: str, stage_name: str) -> _Stats:
        scene_stats = self._stats.setdefault(scene_name, {})
        s = scene_stats.get(stage_name)
        if s is None:
            s = _Stats()
            scene_stats[stage_name] = s
        return s

    @contextmanager
    def frame(self, scene_name: str) -> Iterator[None]:
        self._cur_scene = scene_name
        self._cur_stages = {}
        t0 = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - t0
            self._bucket(scene_name, "frame_total").add(elapsed)
            for stage_name, dt in self._cur_stages.items():
                self._bucket(scene_name, stage_name).add(dt)
            self._cur_scene = None
            self._cur_stages = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        # If no frame is open we still time but record nothing — guards
        # against profiler use outside the Playlist loop (e.g. setup).
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t0
            if self._cur_scene is not None:
                # Sum so nested calls to the same stage in one frame
                # accumulate (currently only "overlay_compose" iterates).
                self._cur_stages[name] = self._cur_stages.get(name, 0.0) + dt

    def record_counts(self, writes: int, bytes_: int) -> None:
        """Capture the DMA-side numbers for the current frame. Called from
        Playlist.run after process_frame, inside the frame() ctx so the
        scene_name is still set."""
        if self._cur_scene is None:
            return
        self._bucket(self._cur_scene, "writes").add(float(writes))
        self._bucket(self._cur_scene, "bytes").add(float(bytes_))

    def emit_if_due(self, now: float, log: logging.Logger) -> bool:
        """Emit one summary line per scene if the interval has elapsed.
        Returns True when the cadence fired (callers can chain extra
        same-cadence lines), False otherwise."""
        if self.interval <= 0:
            return False
        if self._last_emit == 0.0:
            self._last_emit = now
            return False
        if now - self._last_emit < self.interval:
            return False
        for scene_name, stages in self._stats.items():
            line = self._format_line(scene_name, stages)
            if line is not None:
                log.info(line)
        self._last_emit = now
        return True

    @staticmethod
    def _fmt_ms(seconds_summary: tuple[float, float, float, float]) -> str:
        avg, p50, p95, mx = (v * 1000.0 for v in seconds_summary)
        return f"avg={avg:.1f} p50={p50:.1f} p95={p95:.1f} max={mx:.1f} ms"

    def _format_line(self, scene_name: str, stages: dict[str, _Stats]) -> str | None:
        frame_stats = stages.get("frame_total")
        if frame_stats is None or frame_stats.count() == 0:
            return None
        n = frame_stats.count()
        parts: list[str] = [
            f"profile[{scene_name}] n={n}",
            f"frame {self._fmt_ms(frame_stats.summary())}",
        ]
        for stage_name in ("cpu_render", "compose", "overlay_compose", "push", "render", "wait"):
            s = stages.get(stage_name)
            if s is None or s.count() == 0:
                continue
            parts.append(f"{stage_name} {self._fmt_ms(s.summary())}")
        for count_name, label in (("writes", "writes/frame"), ("bytes", "bytes/frame")):
            s = stages.get(count_name)
            if s is None or s.count() == 0:
                continue
            avg, _, p95, _ = s.summary()
            parts.append(f"{label} avg={avg:.0f} p95={p95:.0f}")
        return " | ".join(parts)


# Module-global accessor — see module docstring for the rationale.
_current: NullProfiler | FrameProfiler = NullProfiler()


def get_profiler() -> NullProfiler | FrameProfiler:
    return _current


def set_profiler(p: NullProfiler | FrameProfiler) -> None:
    global _current
    _current = p
