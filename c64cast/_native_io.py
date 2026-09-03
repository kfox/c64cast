"""Process-level stderr muting for native-library chatter.

Some of our native dependencies write diagnostics straight to file
descriptor 2, bypassing Python's `logging`, `sys.stderr`, and any
library-level verbosity flags:

* MediaPipe's C++/absl logging (GL/XNNPACK init, the benign "feedback
  manager" + "landmark_projection NORM_RECT square ROI" warnings).
* OpenCV's AVFoundation/FFmpeg backend, when probing camera indices past
  the highest valid one.
* The Obj-C runtime's "Class AVFFrameReceiver/AVFAudioReceiver is
  implemented in both ..." warning, emitted once when PyAV's bundled
  libavdevice loads on top of cv2's (different major versions, same
  AVFoundation device classes) — harmless, neither file-decode path uses
  the avfoundation input device.

An fd-level redirect is the only thing that catches these. Scope it as
tightly as possible (around the single import / construction / probe that
emits the noise) so it never swallows real stderr from elsewhere.

fd 2 is process-global, so overlapping (non-nested) callers on different
threads can't each just dup/restore it independently: one call site
(video._ensure_pyav) is reachable lazily from playlist worker threads, and
an ensemble runs one such worker per system. A module-level depth counter
makes the redirect reentrant across both nesting and overlap — only the
outermost `__enter__` touches fd 2, and only the matching outermost
`__exit__` restores it — so two overlapping callers can never leave fd 2
pinned to /dev/null once both have exited.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Iterator

_lock = threading.Lock()
_depth = 0
_saved_fd: int | None = None


@contextlib.contextmanager
def silence_native_stderr() -> Iterator[None]:
    """Temporarily redirect the process stderr fd (2) to /dev/null.

    Reentrant and thread-safe (see the module docstring): only the first
    caller in and the last caller out actually touch fd 2.
    """
    global _depth, _saved_fd
    with _lock:
        _depth += 1
        first = _depth == 1
        if first:
            sys.stderr.flush()
            try:
                saved = os.dup(2)
                try:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    try:
                        os.dup2(devnull, 2)
                    finally:
                        os.close(devnull)
                except BaseException:
                    os.close(saved)
                    raise
            except BaseException:
                _depth -= 1
                raise
            _saved_fd = saved
    try:
        yield
    finally:
        with _lock:
            _depth -= 1
            last = _depth == 0
            saved = _saved_fd
            if last:
                _saved_fd = None
        if last:
            assert saved is not None
            os.dup2(saved, 2)
            os.close(saved)
