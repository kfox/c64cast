"""Generative video sources — procedural FrameSources for SourceScene.

Each generator computes a BGR frame purely from the scene clock `t`; the
scene's display mode then quantizes it to the C64, so the *same* generator
renders as PETSCII glyphs, a multicolor bitmap, etc. depending on `display`
(the source/display orthogonality the composable-scene model is built on).

Generators are registered by name, mirroring petscii_styles / backgrounds:
add a `@register("name")` subclass of `GenerativeSource` and it shows up in
config discovery + the `_GENERATIVE_SOURCE_CHOICES` list. The math is pure
numpy and deterministic in `t` (no hidden frame-to-frame state), so a given
scene-time always renders the same frame — which keeps unit tests trivial and
dropped frames harmless.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

import cv2
import numpy as np

from .frame_source import BaseFrameSource

if TYPE_CHECKING:
    from .modulation import MusicModulation

# Native render resolution. The display mode downscales to its own grid
# (40×25 / 80×50 / 320×200 / 160×200), so this only sets the detail the
# generator computes at — 320×200 matches the richest bitmap mode.
GEN_WIDTH = 320
GEN_HEIGHT = 200

REGISTRY: dict[str, type[GenerativeSource]] = {}

_GenT = TypeVar("_GenT", bound="type[GenerativeSource]")


def register(name: str) -> Callable[[_GenT], _GenT]:
    """Class decorator registering a GenerativeSource under a config name.
    Mirrors the overlay / background `@register` pattern."""

    def deco(cls: _GenT) -> _GenT:
        REGISTRY[name] = cls
        cls.name = name
        return cls

    return deco


def generator_names() -> tuple[str, ...]:
    """Registered generator names, in declaration order (the source of truth
    for config's `_GENERATIVE_SOURCE_CHOICES`; a drift test pins the match)."""
    return tuple(REGISTRY.keys())


def build_generator(
    name: str, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT
) -> GenerativeSource:
    if name not in REGISTRY:
        raise ValueError(f"unknown generative source {name!r}; choices: {sorted(REGISTRY)}")
    return REGISTRY[name](width=width, height=height)


class GenerativeSource(BaseFrameSource):
    """Base for procedural frame sources. Subclasses implement `render(t,
    modulation)`.

    Reactive path: `render(t, None)` is the pure, deterministic-in-`t` behavior
    (unchanged forever — the offline renderer + drift tests depend on it). When a
    music-reactive scene passes a `MusicModulation`, the subclass scales its
    params from the shared helpers below — keeping the visual math pure while the
    *measurement* of those features lives entirely in the audio source.
    """

    name = "base"

    # Reactive-modulation mapping constants (used only on the music-reactive
    # render path; the unmodulated path never touches them). Tuned on real HW
    # (Cam Link A/B vs the static path) so the reaction is unmistakable after
    # 16-color quantization — the C64's coarse palette + MCM's population-based
    # bg pick swallow a timid offset, so the gains are deliberately punchy.
    _BEAT_HUE_GAIN = 0.22  # hue cycles added per accumulated beat → tempo-driven cycle rate
    _ONSET_HUE_KICK = 0.22  # hue jump on a transient, decays with `onset` → color pulse
    _V_REST = 0.50  # dim resting HSV value so onsets + loudness clearly flash up
    _ONSET_FLASH = 0.45  # sharp value punch on a transient (the on-beat flash)
    _LEVEL_GAIN = 0.32  # value lift from overall loudness (envelope breathing)

    def __init__(self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT):
        self.width = width
        self.height = height

    def read(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        return self.render(t, modulation)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        raise NotImplementedError

    @classmethod
    def _reactive_hue_offset(cls, modulation: MusicModulation) -> float:
        """Extra hue offset from the music: tempo-driven cycling (beat_phase)
        plus a transient hue kick (onset)."""
        return modulation.beat_phase * cls._BEAT_HUE_GAIN + modulation.onset * cls._ONSET_HUE_KICK

    @classmethod
    def _reactive_value(cls, modulation: MusicModulation) -> float:
        """HSV value (brightness) from the music: a dimmer rest that flashes on a
        transient and lifts with loudness, clipped to [0, 1]."""
        val = cls._V_REST + cls._ONSET_FLASH * modulation.onset + cls._LEVEL_GAIN * modulation.level
        return float(min(1.0, max(0.0, val)))

    @staticmethod
    def _hsv_to_bgr(hue: np.ndarray, sat: float = 1.0, val: float = 1.0) -> np.ndarray:
        """Map a (H,W) float hue field in [0,1) to a saturated BGR frame.
        Full S/V by default so the result quantizes to vivid C64 colors."""
        h, w = hue.shape
        hsv = np.empty((h, w, 3), dtype=np.uint8)
        hsv[..., 0] = (np.mod(hue, 1.0) * 180.0).astype(np.uint8)  # OpenCV H is 0..179
        hsv[..., 1] = int(round(sat * 255))
        hsv[..., 2] = int(round(val * 255))
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


@register("plasma")
class PlasmaSource(GenerativeSource):
    """Classic sine-sum plasma whose hue cycles over time. The spatial field
    is precomputed once; per-frame work is one modulo + HSV→BGR convert."""

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.35,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        field = (
            np.sin(xs / 16.0)
            + np.sin(ys / 8.0)
            + np.sin((xs + ys) / 16.0)
            + np.sin(np.sqrt((xs - width / 2.0) ** 2 + (ys - height / 2.0) ** 2) / 8.0)
        )
        # Normalise to ~[0,1] so `scale` maps to a predictable number of hue cycles.
        self._field = (field - field.min()) / (field.max() - field.min() + 1e-6)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        if modulation is None:
            hue = self._field * self.scale + t * self.speed
            return self._hsv_to_bgr(hue)
        # Reactive: beat_phase speeds the hue cycle with the tempo; an onset kicks
        # the hue and flashes the brightness. beat_phase is frozen while silent,
        # so this degrades smoothly to the baseline drift when nothing's playing.
        hue = self._field * self.scale + t * self.speed + self._reactive_hue_offset(modulation)
        return self._hsv_to_bgr(hue, val=self._reactive_value(modulation))


@register("tunnel")
class TunnelSource(GenerativeSource):
    """Infinite-zoom tunnel: hue is driven by per-pixel depth (1/radius) and
    angle, scrolled over time. Depth + angle fields are precomputed once."""

    def __init__(self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, speed: float = 0.5):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        dx = xs - width / 2.0
        dy = ys - height / 2.0
        r = np.sqrt(dx * dx + dy * dy) + 1e-3
        self._depth = (width * 0.5) / r  # large near centre
        self._angle = np.arctan2(dy, dx) / (2.0 * np.pi)  # -0.5..0.5

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        if modulation is None:
            hue = self._depth * 0.05 + self._angle + t * self.speed
            return self._hsv_to_bgr(hue)
        # Reactive: same generic treatment as plasma (tempo cycles the colors,
        # onsets pulse). The depth-driven tunnel shape itself stays time-locked.
        offset = t * self.speed + self._reactive_hue_offset(modulation)
        hue = self._depth * 0.05 + self._angle + offset
        return self._hsv_to_bgr(hue, val=self._reactive_value(modulation))
