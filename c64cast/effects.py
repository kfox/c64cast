"""Pixel effects — frame transforms applied before quantization.

A `FrameEffect` reads and transforms a scene's BGR frame each tick. Effects are
applied in `scenes._render_with_overlays`, right before the display mode
downscales + quantizes — so *every* frame-based scene (webcam / video /
slideshow / generative) supports them with no per-scene wiring. The transform
runs at full source resolution; for time-varying or feedback effects the scene
passes the current time `t` and resets effect state at scene setup.

Music-reactive path: `apply` also takes an optional `MusicModulation` snapshot
(the same struct generators read — level / onset / beat_phase). When present, an
effect modulates itself from it (a transient punches the zoom, splits the RGB
channels, lengthens the trail). When it's `None` — every non-music-reactive
scene today, since only `SourceScene` with a SID audio source produces a feature
stream — each effect falls back to its baseline behavior, and the zoom/shift
effects fall back to the identity transform. So the unmodulated path is
byte-stable and the offline renderer / determinism tests stay valid.

Effects are registered by name and built via `build_effect(name)`. Add a
`@register("name")` subclass of `FrameEffect` and it shows up in config
discovery + the `_EFFECT_CHOICES` list (a drift test pins the match).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

import cv2
import numpy as np

if TYPE_CHECKING:
    from .modulation import MusicModulation

REGISTRY: dict[str, type[FrameEffect]] = {}

_EffT = TypeVar("_EffT", bound="type[FrameEffect]")


def register(name: str) -> Callable[[_EffT], _EffT]:
    def deco(cls: _EffT) -> _EffT:
        REGISTRY[name] = cls
        cls.name = name
        return cls

    return deco


def effect_names() -> tuple[str, ...]:
    """Registered effect names, in declaration order (source of truth for
    config's `_EFFECT_CHOICES`; a drift test pins the match)."""
    return tuple(REGISTRY.keys())


def build_effect(name: str) -> FrameEffect:
    if name not in REGISTRY:
        raise ValueError(f"unknown effect {name!r}; choices: {sorted(REGISTRY)}")
    return REGISTRY[name]()


class FrameEffect:
    """Base: transform a BGR frame. Stateful effects override `reset()`.

    `apply(frame, t, modulation)` returns the transformed frame. `modulation` is
    an optional `MusicModulation` snapshot — `None` on every non-reactive scene,
    a live struct on a music-reactive `SourceScene`. Reactive effects read it;
    others ignore the arg. The `modulation is None` path must stay byte-stable
    (the determinism guard the offline renderer + tests rely on)."""

    name = "base"

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        raise NotImplementedError

    def reset(self) -> None:
        """Clear any inter-frame state. Called by Scene.setup so a looping or
        re-entered scene starts clean (no trail bleeding across iterations)."""
        return None


@register("trails")
class TrailsEffect(FrameEffect):
    """Feedback / echo trails: each frame is max-blended with a decayed copy of
    the previous output, so moving content leaves a fading comet tail. `decay`
    in [0,1) sets the tail length (higher = longer).

    Reactive: a transient (`onset`) and sustained loudness (`level`) lengthen the
    tail momentarily, so the trail blooms on the beat and tightens between hits.
    The effective decay is clamped below 1.0 (a decay of 1 never fades). With no
    modulation the decay is exactly the configured `decay` — unchanged behavior."""

    # Reactive decay boosts (None path uses the configured decay verbatim).
    _ONSET_DECAY = 0.12  # extra decay (longer tail) at a full transient
    _LEVEL_DECAY = 0.06  # extra decay from sustained loudness
    _MAX_DECAY = 0.97  # hard ceiling — must stay < 1 or the tail never fades

    def __init__(self, decay: float = 0.85):
        self.decay = float(decay)
        self._prev: np.ndarray | None = None

    def reset(self) -> None:
        self._prev = None

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        decay = self.decay
        if modulation is not None:
            decay = min(
                self._MAX_DECAY,
                decay + self._ONSET_DECAY * modulation.onset + self._LEVEL_DECAY * modulation.level,
            )
        if self._prev is None or self._prev.shape != frame.shape:
            self._prev = frame.astype(np.float32)
            return frame
        out = np.maximum(frame.astype(np.float32), self._prev * decay)
        self._prev = out
        return out.astype(np.uint8)


@register("pulse")
class PulseEffect(FrameEffect):
    """Beat-punch zoom: a transient punches the frame scale up (zoom-in toward
    center), relaxing back as `onset` decays; sustained loudness adds a gentle
    steady zoom. Stateless — the whole reaction comes from `modulation`.

    With no modulation (or a scale that rounds to 1.0) it's the identity
    transform, so a non-reactive scene that selects `pulse` sees its frame
    unchanged — nothing to react to, nothing happens."""

    _ONSET_ZOOM = 0.18  # +18% scale at a full transient (the on-beat punch)
    _LEVEL_ZOOM = 0.06  # steady zoom from loudness

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        if modulation is None:
            return frame
        scale = 1.0 + self._ONSET_ZOOM * modulation.onset + self._LEVEL_ZOOM * modulation.level
        if scale <= 1.0:
            return frame
        h, w = frame.shape[:2]
        ch = int(round(h / scale))
        cw = int(round(w / scale))
        if ch < 1 or cw < 1 or (ch == h and cw == w):
            return frame
        # Center-crop a smaller window and stretch it back to full size — the
        # content grows toward the edges (a zoom-in punch) without shifting.
        y0 = (h - ch) // 2
        x0 = (w - cw) // 2
        crop = frame[y0 : y0 + ch, x0 : x0 + cw]
        return cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)


@register("rgb_shift")
class RgbShiftEffect(FrameEffect):
    """Chromatic split: a transient slews the red and blue channels apart
    horizontally (opposite directions), an RGB-shift glitch shudder that snaps
    on the beat and relaxes as `onset` decays; loudness adds a steady split.
    Stateless. No modulation ⇒ identity (zero separation)."""

    _ONSET_SHIFT = 6.0  # px of R/B separation at a full transient
    _LEVEL_SHIFT = 2.0  # steady separation from loudness

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        if modulation is None:
            return frame
        shift = int(
            round(self._ONSET_SHIFT * modulation.onset + self._LEVEL_SHIFT * modulation.level)
        )
        if shift <= 0:
            return frame
        # BGR: channel 0 = blue, channel 2 = red. Roll them opposite ways; G
        # stays put so the image core reads through the colored fringes. np.roll
        # wraps a thin column at the edges, which reads as part of the glitch.
        out = frame.copy()
        out[..., 0] = np.roll(frame[..., 0], shift, axis=1)
        out[..., 2] = np.roll(frame[..., 2], -shift, axis=1)
        return out
