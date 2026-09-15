"""Local preview window + stream recorder, both over `Framebuffer.render()`.

**PreviewWindow must be driven from the process's main thread, and must never
be threaded the way StreamRecorder is.** cv2's HighGUI may only create and
service a window on the main thread — on macOS a hard Cocoa requirement, where
an off-thread `namedWindow` raises "Unknown C++ exception from OpenCV code"
out of the first call. Hence the open/pump/close shape, whose lifecycle
`session._pump_previews_until_done` owns from the otherwise-parked main thread.

See docs/architecture/video-color.md#framebufferpy--previewpy--the-software-mirror-behind-preview-and-recording
and docs/caveats.md#preview-window-fidelity--limits.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time

import cv2

from c64cast._pollthread import PollThread

from .framebuffer import Framebuffer

log = logging.getLogger(__name__)


class PreviewWindow:
    """A window mirroring the U64 display, drawn with cv2's HighGUI.

    Not self-driving: `open()`, `pump()` (repeatedly, at least as often as
    `fps`), and `close()` must all be called from the main thread — see the
    module docstring for why. `pump()` re-renders the framebuffer no faster
    than `fps` and services the window's event loop on every call.
    """

    DEFAULT_SCALE = 3

    def __init__(
        self,
        framebuffer: Framebuffer,
        fps: int = 30,
        scale: int = DEFAULT_SCALE,
        title: str = "c64cast preview",
    ):
        self.fb = framebuffer
        self.fps = max(1, int(fps))
        self.scale = max(1, int(scale))
        self.title = title
        self._open = False
        self._next_draw = 0.0

    @property
    def is_open(self) -> bool:
        """False before `open()`, after `close()`, once the user has closed
        the window, or after a draw failure disabled it."""
        return self._open

    def open(self) -> None:
        """Create the window. WINDOW_AUTOSIZE (not WINDOW_NORMAL) so the
        window tracks the size of the frame we hand it: we upscale by an
        integer factor with INTER_NEAREST ourselves, which keeps C64 pixels
        crisp instead of letting HighGUI interpolate them."""
        if self._open:
            return
        try:
            cv2.namedWindow(self.title, cv2.WINDOW_AUTOSIZE)
        except Exception as e:
            # A headless opencv build or no display. The session runs without it.
            log.error("preview disabled: cannot open a window: %s", e)
            return
        self._open = True
        self._next_draw = 0.0

    def pump(self) -> None:
        """Redraw if the frame deadline has arrived, then service the window's
        event loop. Cheap to over-call; a no-op once the window is gone."""
        if not self._open:
            return
        try:
            now = time.monotonic()
            if now >= self._next_draw:
                bgr = self.fb.render()
                if self.scale != 1:
                    w, h = 320 * self.scale, 200 * self.scale
                    bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_NEAREST)
                cv2.imshow(self.title, bgr)
                self._next_draw = now + 1.0 / self.fps
            # waitKey is what services HighGUI's event loop; without it the
            # window never paints. Its ~1 ms block also paces the caller.
            cv2.waitKey(1)
        except Exception:
            # On the main thread, so an escaping exception would end the session.
            log.exception("preview window failed; disabling it")
            self.close()
            return
        if self._user_closed():
            log.info("preview window closed; session continues")
            self.close()

    def _user_closed(self) -> bool:
        """True once the window has been dismissed via its close button.
        HighGUI has no event queue we can read, so we poll its visibility."""
        try:
            return cv2.getWindowProperty(self.title, cv2.WND_PROP_VISIBLE) < 1
        except Exception:
            return True

    def close(self) -> None:
        """Destroy the window. Idempotent, and safe if `open()` never ran."""
        if not self._open:
            return
        self._open = False
        with contextlib.suppress(Exception):
            cv2.destroyWindow(self.title)
            # destroyWindow only queues the teardown; the pump is what runs it.
            cv2.waitKey(1)


class StreamRecorder:
    """Background thread that grabs Framebuffer renders at `fps` and writes
    them to `output_path` as an MP4. The actual codec depends on what's
    bundled in your opencv build — mp4v works almost everywhere."""

    def __init__(
        self,
        framebuffer: Framebuffer,
        output_path: str,
        fps: int = 30,
        scale: int = 2,
        fourcc: str = "mp4v",
    ):
        self.fb = framebuffer
        self.output_path = output_path
        self.fps = max(1, int(fps))
        self.scale = max(1, int(scale))
        self.fourcc = fourcc
        self._poll = PollThread(self._loop, name="stream-recorder", manual=True, join_timeout=2.0)
        self._writer: cv2.VideoWriter | None = None
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def start(self) -> None:
        w, h = 320 * self.scale, 200 * self.scale
        # Pyright's bundled cv2 stubs miss VideoWriter_fourcc — exists at runtime.
        cc = cv2.VideoWriter_fourcc(*self.fourcc)  # pyright: ignore[reportAttributeAccessIssue]
        self._writer = cv2.VideoWriter(self.output_path, cc, self.fps, (w, h))
        if not self._writer.isOpened():
            self._writer = None
            raise RuntimeError(
                f"recording: cv2.VideoWriter failed to open {self.output_path}; "
                f"check the fourcc ({self.fourcc!r}) and codecs in your opencv "
                "build"
            )
        self._poll.start()
        log.info("recording: %s @ %dx%d %dfps (%s)", self.output_path, w, h, self.fps, self.fourcc)

    def stop(self) -> None:
        self._poll.stop()
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        log.info("recording: stopped after %d frames", self._frame_count)

    def _loop(self, stop: threading.Event):
        period = 1.0 / self.fps
        next_t = time.monotonic()
        try:
            while not stop.is_set():
                now = time.monotonic()
                if now < next_t:
                    stop.wait(timeout=next_t - now)
                    continue
                bgr = self.fb.render()
                if self.scale != 1:
                    w, h = 320 * self.scale, 200 * self.scale
                    bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_NEAREST)
                assert self._writer is not None
                self._writer.write(bgr)
                self._frame_count += 1
                next_t += period
                if time.monotonic() > next_t + period * 5:
                    next_t = time.monotonic()
        except Exception:
            log.exception("recorder crashed")
