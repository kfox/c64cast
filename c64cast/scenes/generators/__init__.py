"""Generative video sources — procedural FrameSources for SourceScene.

Each generator computes a BGR frame purely from the scene clock `t`; the
scene's display mode then quantizes it to the C64, so the *same* generator
renders as PETSCII glyphs, a multicolor bitmap, etc. depending on `display`
(the source/display orthogonality the composable-scene model is built on).

Generators are registered by name, one module per source: add a
`@register("name")` subclass of `GenerativeSource` in a new submodule and
import it in the ordered block at the bottom of this file, and it shows up
in config discovery + the `_GENERATIVE_SOURCE_CHOICES` list. The math is pure
numpy and deterministic in `t` (no hidden frame-to-frame state), so a given
scene-time always renders the same frame — which keeps unit tests trivial and
dropped frames harmless.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

import cv2
import numpy as np

from c64cast.scenes.frame_source import BaseFrameSource

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation

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

    # Live-tunable params: name -> (min, max) for a CC-style [0, 1] sweep.
    # midi_control.py scales into this range and setattr()s directly —
    # only declare independent single-numeric fields here (a plain
    # setattr is GIL-atomic; a value split across two fields wouldn't be).
    LIVE_PARAMS: dict[str, tuple[float, float]] = {}

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
    # Spectral split (audio-input sources only — `bands` is empty on the SID
    # path, so both terms are exactly 0.0 there and the SID look is unchanged).
    # Bass drives brightness and treble drives hue, deliberately: that makes a
    # kick and a hi-hat read differently without ever desaturating, which the
    # 16-color quantizer handles badly (a desaturated hue lands in the grays).
    _BASS_VALUE_GAIN = 0.25  # extra value from low-band energy → kicks punch the brightness
    _TREBLE_HUE_GAIN = 0.10  # hue shift from high-band energy → cymbals/hats shimmer the color

    def __init__(self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT):
        self.width = width
        self.height = height

    def read(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        return self.render(t, modulation)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        raise NotImplementedError

    def reset(self) -> None:
        """Clear any inter-frame state. Mirrors `effects.FrameEffect.reset()`; a
        no-op for the pure-in-`t` generators (nothing to clear), overridden by
        the few generators that carry real incremental state (see `SoapSource`
        / `FireworksSource`). Not currently called by `scenes.py` — a fresh
        generator instance is built per scene entry via `build_scene`, so state
        already resets naturally — but declared here for parity with
        `FrameEffect` and defensiveness against a future reused-instance path."""
        return None

    @classmethod
    def _reactive_hue_offset(cls, modulation: MusicModulation) -> float:
        """Extra hue offset from the music: tempo-driven cycling (beat_phase),
        a transient hue kick (onset), and a treble shimmer when the source
        reports a spectrum (0.0 on the SID path, whose `bands` is empty)."""
        return (
            modulation.beat_phase * cls._BEAT_HUE_GAIN
            + modulation.onset * cls._ONSET_HUE_KICK
            + modulation.treble * cls._TREBLE_HUE_GAIN
        )

    @classmethod
    def _reactive_value(cls, modulation: MusicModulation) -> float:
        """HSV value (brightness) from the music: a dimmer rest that flashes on a
        transient, lifts with loudness, and punches with bass energy when the
        source reports a spectrum (0.0 on the SID path). Clipped to [0, 1]."""
        val = (
            cls._V_REST
            + cls._ONSET_FLASH * modulation.onset
            + cls._LEVEL_GAIN * modulation.level
            + cls._BASS_VALUE_GAIN * modulation.bass
        )
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


# Import order IS registration order: generator_names() promises the historical
# declaration order (test_introspect pins tuple equality against
# config._GENERATIVE_SOURCE_CHOICES), so these lines must not be re-sorted.
# isort: off
from .plasma import PlasmaSource as PlasmaSource
from .tunnel import TunnelSource as TunnelSource
from .fire import FireSource as FireSource
from .mandelbrot import MandelbrotSource as MandelbrotSource
from .moire2 import Moire2Source as Moire2Source
from .halo import HaloSource as HaloSource
from .epicycle import EpicycleSource as EpicycleSource
from .hopalong import HopalongSource as HopalongSource
from .rorschach import RorschachSource as RorschachSource
from .hiphotic import HiphoticSource as HiphoticSource
from .metaballs import MetaballsSource as MetaballsSource
from .rotozoomer import RotozoomerSource as RotozoomerSource
from .lissajous import LissajousSource as LissajousSource
from .dna import DnaSource as DnaSource
from .drift import DriftSource as DriftSource
from .colored_bursts import ColoredBurstsSource as ColoredBurstsSource
from .dotswarm import DotSwarmSource as DotSwarmSource
from .game_of_life import GameOfLifeSource as GameOfLifeSource
from .soap import SoapSource as SoapSource
from .fireworks import FireworksSource as FireworksSource
# isort: on
