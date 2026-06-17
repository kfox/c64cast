"""Pixel effects — frame transforms applied before quantization.

A `FrameEffect` reads and transforms a scene's BGR frame each tick. Effects are
applied in `scenes._render_with_overlays`, right before the display mode
downscales + quantizes — so *every* frame-based scene (webcam / video /
slideshow / generative) supports them with no per-scene wiring. The transform
runs at full source resolution; for time-varying or feedback effects the scene
passes the current time `t` and resets effect state at scene setup.

Effects are registered by name and built via `build_effect(name)`. Add a
`@register("name")` subclass of `FrameEffect` and it shows up in config
discovery + the `_EFFECT_CHOICES` list.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import numpy as np

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
    """Base: transform a BGR frame. Stateful effects override `reset()`."""

    name = "base"

    def apply(self, frame: np.ndarray, t: float) -> np.ndarray:
        raise NotImplementedError

    def reset(self) -> None:
        """Clear any inter-frame state. Called by Scene.setup so a looping or
        re-entered scene starts clean (no trail bleeding across iterations)."""
        return None


@register("trails")
class TrailsEffect(FrameEffect):
    """Feedback / echo trails: each frame is max-blended with a decayed copy of
    the previous output, so moving content leaves a fading comet tail. `decay`
    in [0,1) sets the tail length (higher = longer)."""

    def __init__(self, decay: float = 0.85):
        self.decay = float(decay)
        self._prev: np.ndarray | None = None

    def reset(self) -> None:
        self._prev = None

    def apply(self, frame: np.ndarray, t: float) -> np.ndarray:
        if self._prev is None or self._prev.shape != frame.shape:
            self._prev = frame.astype(np.float32)
            return frame
        out = np.maximum(frame.astype(np.float32), self._prev * self.decay)
        self._prev = out
        return out.astype(np.uint8)
