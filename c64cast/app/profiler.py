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
import math
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger("c64cast")

# Longest scene name rendered into a summary line. Scene names come from
# media content — a directory-spec scene renames itself per pick, and a
# video scene prefers the container's own `title` tag — so the profiler
# treats one as untrusted text: capped here, and repr'd in _format_line so
# an embedded newline can't forge a second log record.
_MAX_SCENE_NAME = 64

# How many consecutive idle summary ticks a scene's buckets survive before
# they are dropped. Two ticks (~20s at the default interval) keeps a briefly
# paused scene's window intact while bounding _stats on a long run, whose
# scene names are per-file and therefore unbounded in number.
_IDLE_TICKS_BEFORE_DROP = 2

# Printed first, in this order, so the columns stay stable across lines.
# Any other stage a caller opens is printed after them rather than dropped.
_KNOWN_STAGES = ("cpu_render", "compose", "overlay_compose", "push", "render", "wait")

# Recorded per scene but rendered by _format_line's own count formatting.
_COUNT_STAGES = ("frame_total", "writes", "bytes")


def _nearest_rank(sorted_samples: list[float], p: float) -> float:
    """The nearest-rank percentile of an already-sorted, non-empty list.

    Nearest rank is the ``ceil(p * n)``-th smallest sample, so the 0-based
    index is ``ceil(p * n) - 1``. Truncating instead (``int(p * n)``) lands
    one rank high whenever ``p * n`` is a whole number — at the steady-state
    n=64 that reported the 33rd smallest frame time as the median, and at
    n=2 it reported the maximum."""
    n = len(sorted_samples)
    return sorted_samples[min(n - 1, max(0, math.ceil(p * n) - 1))]


class _Stats:
    """Bounded ring of float samples with avg / p50 / p95 / max readouts.

    A 64-sample window covers roughly 2s at 30fps, which is the right
    horizon for a 10s summary cadence — long enough to smooth single-frame
    outliers, short enough that the numbers track scene transitions."""

    __slots__ = ("_samples",)

    def __init__(self, capacity: int = 64):
        self._samples: deque[float] = deque(maxlen=capacity)

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
        return avg, _nearest_rank(sorted_s, 0.50), _nearest_rank(sorted_s, 0.95), sorted_s[-1]


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
        if interval <= 0:
            log.warning(
                "[debug].profile_interval is %.3f — every frame is still "
                "instrumented, but no summary will ever be printed",
                interval,
            )
        # Two-level dict: scene_name -> stage_name -> _Stats. Scene-level
        # keys always include "frame_total"; counts go under "writes" /
        # "bytes".
        self._stats: dict[str, dict[str, _Stats]] = {}
        self._last_emit: float = 0.0
        # Liveness bookkeeping for emit_if_due: frames recorded per scene,
        # what that count was at the scene's last summary line, and how many
        # summary ticks it has been idle for. Without these, every scene the
        # process has ever rendered re-prints its final 64 samples on every
        # tick, forever, and _stats grows one bucket per distinct scene name.
        self._frames: dict[str, int] = {}
        self._emitted_frames: dict[str, int] = {}
        self._idle_ticks: dict[str, int] = {}
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
            self._frames[scene_name] = self._frames.get(scene_name, 0) + 1
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

    def _forget(self, scene_name: str) -> None:
        """Drop an idle scene's buckets. Its ring holds frames from minutes
        ago, and scene names are per-file on a directory-spec playlist, so
        keeping them is both misleading and unbounded."""
        self._stats.pop(scene_name, None)
        self._frames.pop(scene_name, None)
        self._emitted_frames.pop(scene_name, None)
        self._idle_ticks.pop(scene_name, None)

    def emit_if_due(self, now: float, log: logging.Logger) -> bool:
        """Emit one summary line per *live* scene if the interval has elapsed.
        Returns True when the cadence fired (callers can chain extra
        same-cadence lines), False otherwise.

        A scene that has rendered no frame since its last line is skipped —
        its ring still holds the last 64 frames it did render, so printing it
        again would report minutes-old numbers under a fresh timestamp — and
        dropped once it has been idle for _IDLE_TICKS_BEFORE_DROP ticks."""
        if self.interval <= 0:
            return False
        if self._last_emit == 0.0:
            self._last_emit = now
            return False
        if now - self._last_emit < self.interval:
            return False
        for scene_name, stages in list(self._stats.items()):
            frames = self._frames.get(scene_name, 0)
            if frames == self._emitted_frames.get(scene_name):
                idle = self._idle_ticks.get(scene_name, 0) + 1
                if idle >= _IDLE_TICKS_BEFORE_DROP:
                    self._forget(scene_name)
                else:
                    self._idle_ticks[scene_name] = idle
                continue
            self._idle_ticks.pop(scene_name, None)
            self._emitted_frames[scene_name] = frames
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
        # !r, not raw: a scene name can carry a media file's own title tag,
        # and an interior newline in one would otherwise write a second,
        # fully forged record into the operator's --log-file.
        parts: list[str] = [
            f"profile[{scene_name[:_MAX_SCENE_NAME]!r}] n={n}",
            f"frame {self._fmt_ms(frame_stats.summary())}",
        ]
        extra = sorted(set(stages) - set(_KNOWN_STAGES) - set(_COUNT_STAGES))
        for stage_name in (*_KNOWN_STAGES, *extra):
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
