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

import math
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

    LIVE_PARAMS = {"speed": (0.0, 2.0), "scale": (0.1, 4.0)}

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

    LIVE_PARAMS = {"speed": (0.0, 2.0)}

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


def _periodic_value_noise(
    rng: np.random.Generator, rows: int, w: int, octaves: list[tuple[int, int, float]]
) -> np.ndarray:
    """Value noise of shape (rows, w), tileable in BOTH axes, summed over
    `octaves` of (vertical_cells, horizontal_cells, amplitude). Tileability
    comes from duplicating the first row/column of each octave's random grid
    before bilinear upsampling, so the upsampled endpoints match — a fire
    texture can then scroll past `rows` and wrap with no visible seam. Returns
    float32 normalised to [0, 1]."""
    acc = np.zeros((rows, w), dtype=np.float32)
    for cy, cx, amp in octaves:
        g = rng.random((cy, cx), dtype=np.float32)
        g = np.vstack([g, g[:1]])  # wrap row
        g = np.hstack([g, g[:, :1]])  # wrap col
        up = cv2.resize(g, (w, rows), interpolation=cv2.INTER_LINEAR)
        acc += amp * up
    lo, hi = float(acc.min()), float(acc.max())
    return (acc - lo) / (hi - lo + 1e-6)


@register("fire")
class FireSource(GenerativeSource):
    """Rising fire: an upward-scrolling turbulence texture masked by a
    bottom-hot vertical gradient and colour-mapped black→red→yellow→white
    (`cv2.COLORMAP_HOT` — a near-perfect match for the C64 palette). The
    turbulence is precomputed and *tileable*, so the scroll is a pure function
    of `t` (deterministic, dropped-frames-safe) rather than a stateful cellular
    sim — `render(t, None)` reproduces exactly.

    Reactive (the headline): `level` raises the flames (louder ⇒ taller/hotter),
    `onset` flares them on each transient. Both push more of the field toward
    the yellow/white end of COLORMAP_HOT, so the fire visibly leaps on the beat
    — the most legible music reaction after 16-colour quantization."""

    # Scroll period (texture rows). The flames rise one full period per
    # period/scroll_speed seconds; a tall period keeps the motion organic.
    _PERIOD = 256
    # Reactive gains (None path uses gain=1, flare=0 — plain rising fire).
    _LEVEL_HEIGHT = 0.85  # extra heat gain at full level (taller, hotter flames)
    _ONSET_FLARE = 0.80  # extra heat gain on a full-strength transient

    LIVE_PARAMS = {"scroll_speed": (0.0, 4.0)}

    def __init__(
        self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, scroll_speed: float = 1.1
    ):
        super().__init__(width=width, height=height)
        self.scroll_speed = float(scroll_speed)
        rng = np.random.default_rng(0xF12E)
        self._turb = _periodic_value_noise(
            rng,
            self._PERIOD,
            width,
            octaves=[(4, 3, 1.0), (8, 6, 0.6), (16, 12, 0.35), (32, 24, 0.2)],
        )
        # Bottom-hot vertical gradient: 0 at the top row, 1 at the bottom.
        # The 1.2 power pulls the flame tips down a touch so they taper.
        grad = np.linspace(0.0, 1.0, height, dtype=np.float32) ** 1.2
        self._grad = grad[:, None]  # (H, 1)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        off = int(t * self.scroll_speed * self._PERIOD) % self._PERIOD
        rows = (off + np.arange(self.height)) % self._PERIOD
        turb = self._turb[rows]  # (H, W), scrolled (wraps seamlessly)
        gain, flare = 1.0, 0.0
        if modulation is not None:
            gain = 1.0 + self._LEVEL_HEIGHT * modulation.level
            flare = self._ONSET_FLARE * modulation.onset
        heat = np.clip(turb * self._grad * gain * (1.0 + flare), 0.0, 1.0)
        u8 = (heat * 255.0).astype(np.uint8)
        return cv2.applyColorMap(u8, cv2.COLORMAP_HOT)


@register("mandelbrot")
class MandelbrotSource(GenerativeSource):
    """Escape-time Mandelbrot zoom. `t` drives an exponential zoom into a
    fixed point of interest (`_CENTER`, a "seahorse valley" coordinate chosen
    so the starting view already frames the whole familiar Mandelbrot
    silhouette). float64 precision limits how far a zoom can go before
    per-pixel spacing collapses into noise, so the zoom **periodically resets**
    to the starting view rather than degrading — `render(t, None)` stays a
    well-defined pure function of `t` forever, the same determinism contract
    plasma/tunnel/fire already guarantee.

    Iteration count is intentionally fixed regardless of zoom depth: the
    output is quantized to a 16-colour C64 grid, so resolving filament-level
    deep-zoom detail would be invisible anyway — fixing it bounds per-frame
    cost at any zoom depth instead of growing it toward the precision limit.
    """

    LIVE_PARAMS = {"zoom_speed": (0.02, 1.0), "cycle_speed": (0.0, 2.0)}

    _MAX_ITER = 100
    _CENTER = complex(-0.743643887037151, 0.13182590420533)  # seahorse valley
    _HALF_WIDTH = 1.75  # starting view half-width; frames the whole set
    _ZOOM_LIMIT = 1.0e13  # stays well inside float64's precision floor
    _HUE_SCALE = 3.0  # hue cycles per full pass through the smooth iteration count
    # The escape-time loop is the one generator whose per-frame cost scales
    # with pixel count rather than a couple of cheap elementwise ops, and the
    # eventual C64 quantization (16-color, 320x200 at best) throws away detail
    # far finer than this anyway — so compute at half resolution per axis
    # (1/4 the points) and let cv2.resize upscale the finished BGR frame
    # (never the HSV field — hue is circular, so linear-interpolating it
    # would blend the wrong way across the 0/179 wrap).
    _CALC_DIVISOR = 2

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        zoom_speed: float = 0.12,
        cycle_speed: float = 0.15,
    ):
        super().__init__(width=width, height=height)
        self.zoom_speed = float(zoom_speed)
        self.cycle_speed = float(cycle_speed)
        cw = max(1, width // self._CALC_DIVISOR)
        ch = max(1, height // self._CALC_DIVISOR)
        self._calc_size = (width, height)  # (w, h) for cv2.resize's dsize
        ys, xs = np.mgrid[0:ch, 0:cw].astype(np.float64)
        # Offsets from center in units of half the frame WIDTH (for both axes)
        # so the shorter height naturally narrows the view vertically instead
        # of stretching the fractal.
        self._px = (xs - cw / 2.0) / (cw / 2.0)
        self._py = (ys - ch / 2.0) / (cw / 2.0)

    def _scale(self, t: float) -> float:
        rate = max(abs(self.zoom_speed), 1e-3)
        period = math.log(self._ZOOM_LIMIT) / rate
        return math.exp(rate * (t % period))

    def _escape_frac(self, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Per-pixel smooth escape-time fraction in [0, 1) (hue-ready) plus an
        `escaped` mask — False means the point never escaped (inside the set),
        which the caller forces to black regardless of hue."""
        z = np.zeros_like(c)
        active = np.ones(c.shape, dtype=bool)
        escaped = np.zeros(c.shape, dtype=bool)
        smooth = np.zeros(c.shape, dtype=np.float64)
        for i in range(self._MAX_ITER):
            z[active] = z[active] * z[active] + c[active]
            mag = np.abs(z)
            newly = active & (mag > 2.0)
            if newly.any():
                # Smooth (continuous) iteration count — the standard
                # log-log correction that removes escape-time banding.
                smooth[newly] = i + 1 - np.log2(np.log2(mag[newly]))
                escaped[newly] = True
                active[newly] = False
            if not active.any():
                break
        frac = np.mod(smooth / self._MAX_ITER * self._HUE_SCALE, 1.0)
        return frac, escaped

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        half_width = self._HALF_WIDTH / self._scale(t)
        c = self._CENTER + (self._px + 1j * self._py) * half_width
        frac, escaped = self._escape_frac(c)
        if modulation is None:
            hue = frac + t * self.cycle_speed
            val: float | np.ndarray = 0.85
        else:
            hue = frac + t * self.cycle_speed + self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        h, w = frac.shape
        hsv = np.empty((h, w, 3), dtype=np.uint8)
        hsv[..., 0] = (np.mod(hue, 1.0) * 180.0).astype(np.uint8)
        hsv[..., 1] = 255
        val_field = np.where(escaped, val, 0.0)
        hsv[..., 2] = np.clip(val_field * 255.0, 0.0, 255.0).astype(np.uint8)
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        if (w, h) == self._calc_size:
            return bgr
        return cv2.resize(bgr, self._calc_size, interpolation=cv2.INTER_LINEAR)
