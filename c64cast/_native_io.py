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
emits the noise, on the main thread before worker threads start) so it
never swallows real stderr from elsewhere.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator


@contextlib.contextmanager
def silence_native_stderr() -> Iterator[None]:
    """Temporarily redirect the process stderr fd (2) to /dev/null."""
    sys.stderr.flush()
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        os.close(devnull)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
