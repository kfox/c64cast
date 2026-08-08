"""Pluggable video/frame sources for composable scenes.

A `FrameSource` is the "video display source" building block: it produces BGR
numpy frames on demand. A `SourceScene` (see scenes.py) pairs any FrameSource
with a display mode (which quantizes the frame to the C64) and an audio source,
so generative art, a still image, a webcam feed, etc. all flow through one path
— the display mode is orthogonal to the source.

The pre-existing concrete sources in video.py (WebcamSource / AVFileSource)
predate this protocol and keep their own shapes; adapting them onto it is a
later migration. New sources (generators.py) implement the protocol directly,
typically via `BaseFrameSource`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from .modulation import MusicModulation


@runtime_checkable
class FrameSource(Protocol):
    """Produces BGR frames for a SourceScene.

    `read(t, modulation)` returns the frame to display at scene-clock time `t`
    (seconds since scene start), or None if no frame is ready this tick (the
    scene skips the render and tries again next tick). `modulation` is an
    optional music-feature snapshot (None when the scene isn't music-reactive)
    that a reactive source reads to modulate its output; sources that don't care
    ignore it. `finished` lets a *finite* source (a played-through video) end
    the scene; infinite sources (generative art, a live webcam) leave it False
    and the scene's `duration_s` governs. `setup()`/`teardown()` bracket the
    scene lifecycle.
    """

    def setup(self) -> None: ...
    def read(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray | None: ...
    @property
    def finished(self) -> bool: ...
    def teardown(self) -> None: ...


class BaseFrameSource:
    """Convenience base for infinite sources: no-op setup/teardown, never
    finishes. Subclasses implement `read(t, modulation)`. Finite sources (video)
    would override `finished`."""

    def setup(self) -> None:
        return None

    def read(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray | None:
        raise NotImplementedError

    @property
    def finished(self) -> bool:
        return False

    def teardown(self) -> None:
        return None
