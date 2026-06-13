"""Local preview window + stream recorder.

PreviewWindow opens a pygame window that mirrors whatever the U64 is
displaying (using the Framebuffer's reconstruction). StreamRecorder
captures the same framebuffer to a video file via cv2.VideoWriter.

Both sit on top of `Framebuffer.render()`, which is the heavy lift.
This module is mostly orchestration: a thread that periodically asks
for a render and pumps it into the consumer.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any

import cv2

from .framebuffer import Framebuffer

log = logging.getLogger(__name__)

# Typed as Any so Pyright doesn't flag every pygame.XXX call as accessing
# attributes of None — the PYGAME_AVAILABLE flag is the runtime guard.
try:
    import pygame as _pygame
    pygame: Any = _pygame
    PYGAME_AVAILABLE = True
except ImportError:
    pygame = None
    PYGAME_AVAILABLE = False


class PreviewWindow:
    """A pygame window mirroring the U64 display. Runs an internal thread
    that re-renders the framebuffer at `fps` and blits to the window."""

    DEFAULT_SCALE = 3

    def __init__(self, framebuffer: Framebuffer,
                 fps: int = 30, scale: int = DEFAULT_SCALE,
                 title: str = "c64cast preview"):
        if not PYGAME_AVAILABLE:
            raise RuntimeError(
                "preview requires pygame: pip install c64cast[preview]")
        self.fb = framebuffer
        self.fps = max(1, int(fps))
        self.scale = max(1, int(scale))
        self.title = title
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._screen = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="preview-window")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self):
        try:
            pygame.init()
            w, h = 320 * self.scale, 200 * self.scale
            self._screen = pygame.display.set_mode((w, h))
            pygame.display.set_caption(self.title)
            clock = pygame.time.Clock()
            while not self._stop.is_set():
                # Drain events so the window stays responsive (closing the
                # window only stops the thread; the main session continues).
                for evt in pygame.event.get():
                    if evt.type == pygame.QUIT:
                        self._stop.set()
                bgr = self.fb.render()
                # pygame wants RGB axis order, surfarray expects (W, H, 3).
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                surf = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
                surf = pygame.transform.scale(surf, (w, h))
                self._screen.blit(surf, (0, 0))
                pygame.display.flip()
                clock.tick(self.fps)
        except Exception:
            log.exception("preview window crashed")
        finally:
            with contextlib.suppress(Exception):
                pygame.quit()


class StreamRecorder:
    """Background thread that grabs Framebuffer renders at `fps` and writes
    them to `output_path` as an MP4. The actual codec depends on what's
    bundled in your opencv build — mp4v works almost everywhere."""

    def __init__(self, framebuffer: Framebuffer,
                 output_path: str,
                 fps: int = 30,
                 scale: int = 2,
                 fourcc: str = "mp4v"):
        self.fb = framebuffer
        self.output_path = output_path
        self.fps = max(1, int(fps))
        self.scale = max(1, int(scale))
        self.fourcc = fourcc
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._writer: cv2.VideoWriter | None = None
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def start(self):
        w, h = 320 * self.scale, 200 * self.scale
        # Pyright's bundled cv2 stubs miss VideoWriter_fourcc — exists at runtime.
        cc = cv2.VideoWriter_fourcc(*self.fourcc)  # pyright: ignore[reportAttributeAccessIssue]
        self._writer = cv2.VideoWriter(self.output_path, cc, self.fps, (w, h))
        if not self._writer.isOpened():
            self._writer = None
            raise RuntimeError(
                f"recording: cv2.VideoWriter failed to open {self.output_path}; "
                f"check the fourcc ({self.fourcc!r}) and codecs in your opencv "
                "build")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="stream-recorder")
        self._thread.start()
        log.info("recording: %s @ %dx%d %dfps (%s)",
                 self.output_path, w, h, self.fps, self.fourcc)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        log.info("recording: stopped after %d frames", self._frame_count)

    def _loop(self):
        period = 1.0 / self.fps
        next_t = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now < next_t:
                    self._stop.wait(timeout=next_t - now)
                    continue
                bgr = self.fb.render()
                if self.scale != 1:
                    w, h = 320 * self.scale, 200 * self.scale
                    bgr = cv2.resize(bgr, (w, h),
                                     interpolation=cv2.INTER_NEAREST)
                assert self._writer is not None
                self._writer.write(bgr)
                self._frame_count += 1
                next_t += period
                # If we fell way behind (slow disk?), snap forward.
                if time.monotonic() > next_t + period * 5:
                    next_t = time.monotonic()
        except Exception:
            log.exception("recorder crashed")
