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
    # Spectral split (audio-input sources only — `bands` is empty on the SID
    # path, so both terms are exactly 0.0 there and the SID look is unchanged).
    # Bass drives brightness and treble drives hue, deliberately: that makes a
    # kick and a hi-hat read differently without ever desaturating, which the
    # 16-color quantizer handles badly (a desaturated hue lands in the greys).
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

    # `scale` multiplies the 0.05 depth coefficient (the ix live knob): higher
    # packs more concentric rings toward the mouth of the tunnel. 1.0 == the
    # historical fixed depth.
    LIVE_PARAMS = {"speed": (0.0, 2.0), "scale": (0.25, 4.0)}

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.5,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        dx = xs - width / 2.0
        dy = ys - height / 2.0
        r = np.sqrt(dx * dx + dy * dy) + 1e-3
        self._depth = (width * 0.5) / r  # large near centre
        self._angle = np.arctan2(dy, dx) / (2.0 * np.pi)  # -0.5..0.5

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        depth_coeff = 0.05 * self.scale
        if modulation is None:
            hue = self._depth * depth_coeff + self._angle + t * self.speed
            return self._hsv_to_bgr(hue)
        # Reactive: same generic treatment as plasma (tempo cycles the colors,
        # onsets pulse). The depth-driven tunnel shape itself stays time-locked.
        offset = t * self.speed + self._reactive_hue_offset(modulation)
        hue = self._depth * depth_coeff + self._angle + offset
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

    # `intensity` scales the overall heat/flame height (the ix live knob),
    # applied on top of the reactive gain. 1.0 == the historical baseline.
    LIVE_PARAMS = {"scroll_speed": (0.0, 4.0), "intensity": (0.2, 2.0)}

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        scroll_speed: float = 1.1,
        intensity: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.scroll_speed = float(scroll_speed)
        self.intensity = float(intensity)
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
        heat = np.clip(turb * self._grad * gain * (1.0 + flare) * self.intensity, 0.0, 1.0)
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


@register("moire2")
class Moire2Source(GenerativeSource):
    """Two concentric-ring distance fields whose centers drift apart and
    together, summed into a classic moiré interference pattern (each field is
    `sin(distance-to-center * freq)`; xscreensaver's moire2.c gets the same
    beat pattern by XOR-compositing two arc bitmaps — this is the closed-form
    equivalent: a distance field instead of drawn arcs)."""

    LIVE_PARAMS = {"ring_freq": (10.0, 80.0), "drift_speed": (0.0, 2.0)}

    _DRIFT_FRAC = 0.22  # max center separation, as a fraction of width
    _VOICE_FREQ_GAIN = 0.03  # ring-freq nudge per Hz of the driving voice
    _HUE_DRIFT = 0.05  # base hue cycle rate (independent of the music)

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        ring_freq: float = 36.0,
        drift_speed: float = 0.35,
    ):
        super().__init__(width=width, height=height)
        self.ring_freq = float(ring_freq)
        self.drift_speed = float(drift_speed)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        cx, cy = self.width / 2.0, self.height / 2.0
        phase = t * self.drift_speed
        freq_a = freq_b = self.ring_freq
        if modulation is not None:
            # Tempo breathes the center separation; each ring tracks a
            # different voice's pitch so the two families drift apart in
            # frequency, not just in space.
            phase += modulation.beat_phase * 0.15
            freq_a = self.ring_freq + modulation.voice_freqs[0] * self._VOICE_FREQ_GAIN
            freq_b = self.ring_freq + modulation.voice_freqs[1] * self._VOICE_FREQ_GAIN
        sep = self.width * self._DRIFT_FRAC * math.sin(phase)
        ra = np.hypot(self._xs - (cx - sep), self._ys - cy)
        rb = np.hypot(self._xs - (cx + sep), self._ys - cy)
        field = np.sin(ra / freq_a * (2.0 * math.pi)) + np.sin(rb / freq_b * (2.0 * math.pi))
        hue = (field + 2.0) / 4.0 + t * self._HUE_DRIFT
        if modulation is None:
            return self._hsv_to_bgr(hue)
        hue = hue + self._reactive_hue_offset(modulation)
        return self._hsv_to_bgr(hue, val=self._reactive_value(modulation))


@register("halo")
class HaloSource(GenerativeSource):
    """Several soft-edged halos drifting on independent circular orbits,
    additively blended (bright where they overlap, no clear — matching
    xscreensaver's halo.c un-erased canvas). The "trail" halo.c gets by never
    clearing is faked here without carrying state across frames: each halo is
    drawn at a few trailing time-lags with decreasing brightness, all as a
    pure function of `t`.

    Reactive: `level` grows every halo's radius (louder ⇒ bigger blooms); a
    transient (`onset`) flashes in one extra halo centered on the frame,
    invisible at rest (its weight is scaled by `onset` directly)."""

    LIVE_PARAMS = {"drift_speed": (0.0, 2.0), "pulse_speed": (0.0, 3.0)}

    _N_HALOS = 4
    _N_ECHOES = 2
    _ECHO_LAG = 0.05  # seconds between trailing echoes
    _ECHO_DECAY = 0.4  # brightness multiplier per echo step back
    _PATH_FRAC = 0.42  # orbit radius, as a fraction of width/height
    _RADIUS_FRAC = 0.05  # halo radius, as a fraction of width
    _PULSE_FRAC = 0.015  # radius pulse amplitude, as a fraction of width
    _LEVEL_RADIUS_GAIN = 0.7  # extra radius fraction at full `level`
    _ONSET_HALO_RADIUS_FRAC = 0.22

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        drift_speed: float = 0.3,
        pulse_speed: float = 0.9,
    ):
        super().__init__(width=width, height=height)
        self.drift_speed = float(drift_speed)
        self.pulse_speed = float(pulse_speed)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys
        rng = np.random.default_rng(0x4A10)
        self._orbit_rate = rng.uniform(0.5, 1.3, self._N_HALOS)
        # Evenly spaced at t=0 (full-frame coverage from the first frame);
        # each halo's distinct orbit_rate then drifts them in and out of
        # alignment over time rather than clustering by luck of a random draw.
        self._orbit_phase = np.arange(self._N_HALOS) * (2.0 * math.pi / self._N_HALOS)
        self._pulse_rate = rng.uniform(0.6, 1.6, self._N_HALOS)
        self._pulse_phase = rng.uniform(0.0, 2.0 * math.pi, self._N_HALOS)
        hues = rng.uniform(0.0, 1.0, self._N_HALOS).astype(np.float32)
        self._colors = [
            self._hsv_to_bgr(np.full((1, 1), h, np.float32))[0, 0].astype(np.float32) for h in hues
        ]

    def _halo_center(self, i: int, t: float) -> tuple[float, float]:
        ang = self._orbit_phase[i] + t * self.drift_speed * self._orbit_rate[i]
        cx, cy = self.width / 2.0, self.height / 2.0
        rx = self.width * self._PATH_FRAC
        ry = self.height * self._PATH_FRAC
        return cx + rx * math.cos(ang), cy + ry * math.sin(ang)

    def _halo_radius(self, i: int, t: float, level_gain: float) -> float:
        base = self.width * self._RADIUS_FRAC
        pulse = (
            self.width
            * self._PULSE_FRAC
            * math.sin(t * self.pulse_speed * self._pulse_rate[i] + self._pulse_phase[i])
        )
        return (base + pulse) * (1.0 + level_gain)

    def _weight(self, cx: float, cy: float, r: float) -> np.ndarray:
        r = max(r, 1.0)
        d2 = (self._xs - cx) ** 2 + (self._ys - cy) ** 2
        return np.exp(-d2 / (2.0 * r * r))

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        level_gain = 0.0 if modulation is None else self._LEVEL_RADIUS_GAIN * modulation.level
        acc = np.zeros((self.height, self.width, 3), dtype=np.float32)
        for i in range(self._N_HALOS):
            r = self._halo_radius(i, t, level_gain)
            w = np.zeros((self.height, self.width), dtype=np.float32)
            for e in range(self._N_ECHOES):
                te = t - e * self._ECHO_LAG
                cx, cy = self._halo_center(i, te)
                w += (self._ECHO_DECAY**e) * self._weight(cx, cy, r)
            acc += w[..., None] * self._colors[i]
        if modulation is not None and modulation.onset > 0.0:
            cx, cy = self.width / 2.0, self.height / 2.0
            r = self.width * self._ONSET_HALO_RADIUS_FRAC
            flash = (modulation.onset * self._weight(cx, cy, r))[..., None]
            acc += flash * np.array([255.0, 255.0, 255.0], np.float32)
        return np.clip(acc, 0.0, 255.0).astype(np.uint8)


@register("epicycle")
class EpicycleSource(GenerativeSource):
    """Fourier epicycles: a chain of circles, each spinning around the tip of
    the previous, whose combined tip traces `sum_i r_i * exp(j*(w_i t +
    phi_i))` — a chain of rotations composes to the same vector sum regardless
    of framing, so this sums phasors directly rather than nesting rotations.
    Radii follow an odd-harmonic series (`r_i = r0/(2i+1)`, alternating spin
    direction) — the classic square-wave epicycle construction. Renders the
    current arm chain (circle + spoke per arm) plus a fading trail of the
    tip's recent path, drawn as several trailing echoes since `render` is a
    pure function of `t`, not stateful accumulation.

    Reactive: each of the first three arms' angular speed is retuned to track
    a SID voice's live pitch (`voice_freqs`) instead of its fixed harmonic, so
    the chain's shape visibly follows the tune; `level` scales every arm's
    radius (louder ⇒ bigger sweep); a transient briefly flashes the whole
    frame brighter."""

    LIVE_PARAMS = {"speed": (0.0, 2.0)}

    _N_ARMS = 5
    _N_TRAIL = 24
    _TRAIL_LAG = 0.04
    _FREQ_TO_W_GAIN = 0.015  # rad/s of arm speed per Hz of voice pitch
    _LEVEL_RADIUS_GAIN = 0.6
    _ONSET_FLASH_GAIN = 90.0  # max per-channel brightness add on a full onset

    # Radii taper geometrically (`r0 * _RADIUS_RATIO**i`) rather than the
    # stricter harmonic `1/(2i+1)` series: the harmonic decay makes every arm
    # past the first collapse into an illegible cluster at this arm count,
    # while a gentler taper keeps each ring visually distinct (a spirograph
    # look rather than a literal square-wave Fourier reconstruction).
    _RADIUS_RATIO = 0.55

    def __init__(self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, speed: float = 0.6):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        n = self._N_ARMS
        self._w = np.arange(1, n + 1, dtype=np.float64)  # 1, 2, 3, 4, 5
        self._sign = np.array([1.0 if i % 2 == 0 else -1.0 for i in range(n)])
        r0 = min(width, height) * 0.32
        self._radius = r0 * (self._RADIUS_RATIO ** np.arange(n))
        self._colors = [
            self._hsv_to_bgr(np.full((1, 1), i / n, np.float32))[0, 0].tolist() for i in range(n)
        ]

    def _chain(self, t: float, w: np.ndarray, radius_scale: float) -> tuple[np.ndarray, np.ndarray]:
        """Cumulative arm-tip positions (one per arm, chain order)."""
        angles = self.speed * t * w * self._sign
        r = self._radius * radius_scale
        dx = r * np.cos(angles)
        dy = r * np.sin(angles)
        cx = self.width / 2.0 + np.cumsum(dx)
        cy = self.height / 2.0 + np.cumsum(dy)
        return cx, cy

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        w = self._w
        radius_scale = 1.0
        if modulation is not None:
            w = self._w.copy()
            for i, freq in enumerate(modulation.voice_freqs):
                if i < len(w) and freq > 0.0:
                    w[i] = self._w[i] + freq * self._FREQ_TO_W_GAIN
            radius_scale = 1.0 + self._LEVEL_RADIUS_GAIN * modulation.level

        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        cx, cy = self._chain(t, w, radius_scale)
        px, py = self.width / 2.0, self.height / 2.0
        for i in range(self._N_ARMS):
            color = self._colors[i]
            r = max(int(round(self._radius[i] * radius_scale)), 1)
            p0 = (int(round(px)), int(round(py)))
            p1 = (int(round(cx[i])), int(round(cy[i])))
            cv2.circle(frame, p0, r, color, 1, cv2.LINE_AA)
            cv2.line(frame, p0, p1, color, 1, cv2.LINE_AA)
            px, py = cx[i], cy[i]
        for e in range(self._N_TRAIL):
            te = t - e * self._TRAIL_LAG
            tcx, tcy = self._chain(te, w, radius_scale)
            fade = 1.0 - e / self._N_TRAIL
            trail_color = (int(255 * fade),) * 3
            cv2.circle(
                frame, (int(round(tcx[-1])), int(round(tcy[-1]))), 2, trail_color, -1, cv2.LINE_AA
            )
        if modulation is not None and modulation.onset > 0.0:
            flash = int(self._ONSET_FLASH_GAIN * modulation.onset)
            frame = cv2.add(frame, np.full_like(frame, flash))
        return frame


def _hopalong_step(
    x: np.ndarray, y: np.ndarray, a: float, b: float, c: float
) -> tuple[np.ndarray, np.ndarray]:
    """One iteration of Barry Martin's Hopalong map (the `sqrt` variant
    xscreensaver's hopalong.c defaults to): `x' = y - sign(x)*sqrt(|b*x-c|)`,
    `y' = a - x`."""
    nx = y - np.sign(x) * np.sqrt(np.abs(b * x - c))
    ny = a - x
    return nx, ny


@register("hopalong")
class HopalongSource(GenerativeSource):
    """Hopalong chaotic point-map attractor, iterated for many parallel
    starting points at once (numpy-vectorized across the batch — each *step*
    is still sequential, the map depends on the previous point) into a
    density accumulator, colour-mapped by (log-scaled) density. A slow
    sinusoidal drift of the `a` constant keeps the attractor's shape breathing
    over time without needing a fundamentally different computation per
    frame; the batch is re-run from scratch every frame (cheap: a few hundred
    vector ops), so the shifting constant is reflected immediately.

    Reactive: `level` and a beat-locked term perturb `a`/`b` continuously
    (the attractor's shape swells with the music); a transient adds a
    temporary kick to `a` — one frame's worth of "the constants jump", not a
    lasting state change, matching the pure-in-`t` contract."""

    LIVE_PARAMS = {"a": (-2.0, 2.0), "drift_speed": (0.0, 1.0)}

    _BATCH = 4000
    _WARMUP = 60
    _ITERS = 140
    _B = 1.0
    _C = 0.0
    _A_DRIFT = 0.5
    _LEVEL_GAIN = 0.35
    _ONSET_GAIN = 0.6
    _BEAT_B_GAIN = 0.01

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        a: float = 1.1,
        drift_speed: float = 0.15,
    ):
        super().__init__(width=width, height=height)
        self.a = float(a)
        self.drift_speed = float(drift_speed)
        rng = np.random.default_rng(0x0A0F)
        self._x0 = rng.uniform(-0.5, 0.5, self._BATCH)
        self._y0 = rng.uniform(-0.5, 0.5, self._BATCH)

    def _density(self, a: float, b: float, c: float) -> np.ndarray:
        x, y = self._x0.copy(), self._y0.copy()
        xs = []
        ys = []
        for i in range(self._WARMUP + self._ITERS):
            x, y = _hopalong_step(x, y, a, b, c)
            if i >= self._WARMUP:
                xs.append(x)
                ys.append(y)
        px_f = np.concatenate(xs)
        py_f = np.concatenate(ys)
        xmin, xmax = px_f.min(), px_f.max()
        ymin, ymax = py_f.min(), py_f.max()
        px = ((px_f - xmin) / (xmax - xmin + 1e-9) * (self.width - 1)).astype(np.int64)
        py = ((py_f - ymin) / (ymax - ymin + 1e-9) * (self.height - 1)).astype(np.int64)
        flat = py * self.width + px
        counts = np.bincount(flat, minlength=self.width * self.height)
        density = counts.reshape(self.height, self.width).astype(np.float32)
        density = np.log1p(density)
        peak = density.max()
        if peak > 0.0:
            density /= peak
        return density

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        a = self.a + self._A_DRIFT * math.sin(t * self.drift_speed)
        b = self._B
        if modulation is not None:
            a += self._LEVEL_GAIN * modulation.level + self._ONSET_GAIN * modulation.onset
            b += self._BEAT_B_GAIN * modulation.beat_phase
        density = self._density(a, b, self._C)
        hue = density * 0.7 + t * 0.04
        val = density
        if modulation is not None:
            hue = hue + self._reactive_hue_offset(modulation)
            val = np.clip(density + 0.4 * modulation.onset, 0.0, 1.0)
        h, w = density.shape
        hsv = np.empty((h, w, 3), dtype=np.uint8)
        hsv[..., 0] = (np.mod(hue, 1.0) * 180.0).astype(np.uint8)
        hsv[..., 1] = 255
        hsv[..., 2] = np.clip(val * 255.0, 0.0, 255.0).astype(np.uint8)
        frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        frame[density <= 1e-6] = 0
        return frame


@register("rorschach")
class RorschachSource(GenerativeSource):
    """Mirrored-symmetric ink-blot: a precomputed 2D random walk (fixed seed
    ⇒ deterministic) cumulative-summed from Gaussian steps, progressively
    revealed as `t` advances and reflected across the vertical center line —
    xscreensaver's rorschach.c animates the same way (draw a few more walk
    points each frame); this stays a pure function of `t` by redrawing
    however much of the (fixed) walk is "revealed" by `t` from scratch each
    frame, rather than accumulating pixels frame to frame. The reveal loops
    (grow, hold briefly at full bloom, reset) so playback never visibly ends.

    Reactive: `level` scales the whole blot larger (louder ⇒ bigger ink
    mass); a strong transient jumps the reveal forward — the "restart" flash
    xscreensaver's mirror-restart evokes, without discarding the walk."""

    LIVE_PARAMS = {"grow_speed": (0.0, 4.0)}

    _N_STEPS = 6000
    _STEP_SIZE = 2.2
    _PERIOD_S = 20.0  # seconds for one grow-then-recede cycle (triangle wave)
    _LEVEL_SCALE_GAIN = 0.5
    _ONSET_JUMP_FRAC = 0.15
    _HUE_DRIFT = 0.05

    def __init__(
        self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, grow_speed: float = 1.0
    ):
        super().__init__(width=width, height=height)
        self.grow_speed = float(grow_speed)
        rng = np.random.default_rng(0x707C)
        steps = rng.normal(0.0, self._STEP_SIZE, size=(self._N_STEPS, 2)).astype(np.float32)
        walk = np.cumsum(steps, axis=0)
        walk -= walk.mean(axis=0)
        span = float(np.abs(walk).max()) + 1e-6
        scale = min(width, height) * 0.42 / span
        self._walk = walk * scale  # (_N_STEPS, 2) offsets from center

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        # Triangle wave (grow then recede) rather than a sawtooth, so the
        # cycle loops with no visible pop back to empty.
        phase = (t * self.grow_speed / self._PERIOD_S) % 2.0
        frac = phase if phase <= 1.0 else 2.0 - phase
        scale = 1.0
        hue = t * self._HUE_DRIFT
        if modulation is not None:
            frac = min(1.0, frac + self._ONSET_JUMP_FRAC * modulation.onset)
            scale = 1.0 + self._LEVEL_SCALE_GAIN * modulation.level
            hue += self._reactive_hue_offset(modulation)
        n_reveal = max(2, int(frac * self._N_STEPS))
        pts = self._walk[:n_reveal] * scale
        cx, cy = self.width / 2.0, self.height / 2.0
        color = self._hsv_to_bgr(np.full((1, 1), hue % 1.0, np.float32))[0, 0].tolist()
        xs = pts[:, 0]
        ys = pts[:, 1]
        px = np.concatenate([cx + xs, cx - xs]).astype(np.int32)
        py = np.concatenate([cy + ys, cy + ys]).astype(np.int32)
        valid = (px >= 0) & (px < self.width) & (py >= 0) & (py < self.height)
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[py[valid], px[valid]] = color
        return cv2.dilate(frame, np.ones((3, 3), np.uint8))


@register("hiphotic")
class HiphoticSource(GenerativeSource):
    """WLED "Hiphotic" port: nested trig interference
    (`sin(cos(x...) + sin(y...) + a)`), reimplemented in continuous float
    instead of WLED's 8-bit sin8/cos8 lookup tables. Unlike Plasma, the
    `t`-driven phase sits *inside* the inner cos/sin terms rather than being
    added on at the end, so the combined field can't be precomputed once and
    modulo'd per frame the way Plasma's can — only the raw `xs`/`ys` pixel
    grids are cached; the rest is recomputed every `render()` call. WLED
    exposes independent X-scale/Y-scale sliders; those collapse here into one
    `scale` LIVE_PARAM (a deliberate simplification)."""

    LIVE_PARAMS = {"speed": (0.1, 8.0), "scale": (0.1, 4.0)}

    # Tuned by eye at 320x200; scale=1.0 ~= WLED's default band density.
    _BASE_FREQ = 0.02

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 1.5,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        k = self.scale * self._BASE_FREQ
        a = t * self.speed
        inner_x = np.cos(self._xs * k + a / 3.0)
        inner_y = np.sin(self._ys * k + a / 4.0)
        hue = (np.sin(inner_x + inner_y + a) + 1.0) * 0.5
        if modulation is None:
            return self._hsv_to_bgr(hue)
        hue = hue + self._reactive_hue_offset(modulation)
        return self._hsv_to_bgr(hue, val=self._reactive_value(modulation))


@register("metaballs")
class MetaballsSource(GenerativeSource):
    """WLED "Metaballs" port: 3 moving "ball" centers blended into a classic
    inverse-distance metaball field. All 3 ball paths are closed-form
    functions of `t` in WLED's own source too — `beatsin8` is phase-linear in
    wall-clock time (no running accumulator), so ball 1 ports directly as a
    Lissajous sine pair; balls 2 & 3 use `perlin8` point samples, which this
    codebase has no primitive for, so they're replaced with a 2-term
    incommensurate-frequency sine "wander" (the same pure-trig
    organic-motion trick `hopalong`/`epicycle` already use elsewhere) — a
    documented simplification, not a literal noise port. Per frame: 3 scalar
    ball positions (a handful of scalar `sin()` calls) plus one vectorized
    distance field over the precomputed pixel grid."""

    LIVE_PARAMS = {"speed": (0.05, 5.0)}

    _W1X = 0.9
    _W1Y = 1.1
    _BALL2 = {"fx": (0.11, 0.178), "fy": (0.13, 0.210), "px": (0.0, 1.7), "py": (0.9, 2.4)}
    _BALL3 = {"fx": (0.17, 0.275), "fy": (0.19, 0.307), "px": (2.1, 0.4), "py": (1.2, 3.0)}
    _THRESHOLD = 60.0
    # WLED's raw `color/threshold` value maps cleanly to brightness on the
    # small (16-64px) matrices it targets, but decays too fast to read as
    # anything but a dim smudge at this generator's much larger 320x200 native
    # resolution — this gamma lifts the mid/low range for legibility after C64
    # quantization (background pixels, already ~0, stay ~0; it's a display
    # tone curve, not a change to the underlying distance-field math).
    _VALUE_GAMMA = 0.6

    def __init__(self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, speed: float = 1.0):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._amp = min(width, height) * 0.35

    def _wander(self, tt: float, spec: dict[str, tuple[float, float]]) -> tuple[float, float]:
        fx0, fx1 = spec["fx"]
        fy0, fy1 = spec["fy"]
        px0, px1 = spec["px"]
        py0, py1 = spec["py"]
        dx = 0.6 * math.sin(tt * fx0 + px0) + 0.4 * math.sin(tt * fx1 + px1)
        dy = 0.6 * math.sin(tt * fy0 + py0) + 0.4 * math.sin(tt * fy1 + py1)
        return self._cx + self._amp * dx, self._cy + self._amp * dy

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        tt = t * self.speed
        x1 = self._cx + self._amp * math.sin(tt * self._W1X)
        y1 = self._cy + self._amp * math.sin(tt * self._W1Y)
        x2, y2 = self._wander(tt, self._BALL2)
        x3, y3 = self._wander(tt, self._BALL3)
        d1 = np.hypot(self._xs - x1, self._ys - y1)
        d2 = np.hypot(self._xs - x2, self._ys - y2)
        d3 = np.hypot(self._xs - x3, self._ys - y3)
        dist = 2.0 * d1 + d2 + d3
        color = 1000.0 / np.maximum(dist, 1.0)
        in_range = color < self._THRESHOLD
        val = np.clip(color / self._THRESHOLD, 0.0, 1.0) ** self._VALUE_GAMMA
        hue = 0.55 - val * 0.55
        if modulation is not None:
            hue = np.mod(hue + self._reactive_hue_offset(modulation), 1.0)
            val = val * self._reactive_value(modulation)
        val = np.where(in_range, val, 0.0)
        # Per-pixel `val` (not the scalar `_hsv_to_bgr` accepts) needs the
        # manual HSV build Mandelbrot/Hopalong already use for the same reason.
        h, w = val.shape
        hsv = np.empty((h, w, 3), dtype=np.uint8)
        hsv[..., 0] = (np.mod(hue, 1.0) * 180.0).astype(np.uint8)
        hsv[..., 1] = 255
        hsv[..., 2] = np.clip(val * 255.0, 0.0, 255.0).astype(np.uint8)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


@register("rotozoomer")
class RotozoomerSource(GenerativeSource):
    """WLED "Rotozoomer" port: a static XOR bit-pattern texture (`(x*4) ^
    (y*4)`, precomputed + colorized once) sampled through a rotating/zooming
    affine transform. WLED integrates its rotation angle once per render call
    (`angle -= 0.03 + (speed-128)*0.0002`), tied to WLED's own frame cadence
    rather than wall-clock time — incompatible with this codebase's
    pure-function-of-`t` contract, so the angle is redefined here as a closed
    form, `angle(t) = -speed * t`, exactly the same "phase advances linearly
    with `t`" pattern Plasma/Tunnel already use for their hue rotation. Also
    the first use of `cv2.warpAffine` in this codebase: `BORDER_WRAP` mirrors
    WLED's modulo-wrapped texture lookup. WLED's alternate Perlin-noise
    texture mode ("Alt") is not ported — a documented scope-narrowing, not an
    oversight."""

    LIVE_PARAMS = {"speed": (0.0, 4.0), "scale": (0.2, 4.0)}

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.5,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.uint16)
        pattern = ((xs * 4) ^ (ys * 4)) & 0xFF
        hue = pattern.astype(np.float32) / 255.0
        self._texture = self._hsv_to_bgr(hue)
        self._center = (width / 2.0, height / 2.0)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        angle_deg = math.degrees(-self.speed * t)
        matrix = cv2.getRotationMatrix2D(self._center, angle_deg, self.scale)
        frame = cv2.warpAffine(
            self._texture,
            matrix,
            (self.width, self.height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_WRAP,
        )
        if modulation is None:
            return frame
        gain = self._reactive_value(modulation)
        return np.clip(frame.astype(np.float32) * gain, 0.0, 255.0).astype(np.uint8)


@register("lissajous")
class LissajousSource(GenerativeSource):
    """WLED "Lissajous" port: a classic XY curve (`x = sin(theta*freq_x +
    phase)`, `y = cos(theta*2 + phase)`) sampled at a fixed number of points
    along its parametrization. WLED's own version already redraws all 256
    points from scratch on every render call (a `fadeToBlackBy` trail is
    layered on top for a soft cometary look, but the curve itself is fully
    drawn each time, not accumulated) — so, unlike the halo/epicycle family,
    this needs no synthetic time-lag echo to look continuous: `render(t,
    None)` samples the whole curve fresh from a closed form every frame.
    WLED's independent X-frequency and rotation-speed sliders map to `scale`
    (curve shape) and `speed` (rotation rate)."""

    LIVE_PARAMS = {"speed": (0.0, 4.0), "scale": (0.2, 6.0)}

    _N_POINTS = 256
    _Y_FREQ = 2.0  # fixed y-axis frequency (WLED hardcodes `i*2` for the cos term)
    _HUE_CYCLES = 1.0  # hue cycles once per full curve sweep
    _LEVEL_GAIN = 0.25
    _BEAT_PHASE_GAIN = 0.3

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.6,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        self._theta = np.linspace(
            0.0, 2.0 * math.pi, self._N_POINTS, endpoint=False, dtype=np.float64
        )
        self._i_frac = np.linspace(0.0, 1.0, self._N_POINTS, endpoint=False, dtype=np.float32)
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._amp_x = (width / 2.0) * 0.92
        self._amp_y = (height / 2.0) * 0.92

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        phase = t * self.speed
        level_gain = 0.0
        hue_off = 0.0
        val = 1.0
        if modulation is not None:
            phase += modulation.beat_phase * self._BEAT_PHASE_GAIN
            level_gain = self._LEVEL_GAIN * modulation.level
            hue_off = self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        xs = self._cx + self._amp_x * (1.0 + level_gain) * np.sin(self._theta * self.scale + phase)
        ys = self._cy + self._amp_y * (1.0 + level_gain) * np.cos(
            self._theta * self._Y_FREQ + phase
        )
        px = np.clip(xs, 0, self.width - 1).astype(np.int32)
        py = np.clip(ys, 0, self.height - 1).astype(np.int32)
        hue = self._i_frac * self._HUE_CYCLES + t * 0.05 + hue_off
        colors = self._hsv_to_bgr(hue[None, :], val=val)[0]
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[py, px] = colors
        return cv2.dilate(frame, np.ones((2, 2), np.uint8))


@register("dna")
class DnaSource(GenerativeSource):
    """WLED "DNA" port: two sine strands sweeping the full frame width,
    phase-shifted by half a cycle (`pi`, matching WLED's `i*4` vs `i*4+128`
    offset) so they wind around a shared center line like a double helix;
    color cycles per column + time. WLED redraws every column on each render
    call — its softening comes entirely from `SEGMENT.blur`, not from state
    carried between frames — so this ports directly as a pure function of
    `t`: each column's y-position is a closed-form `sin`, sampled fresh every
    frame. Pair with the `blur` effect (see effect-trails.toml for the
    pattern) for WLED's own soft-edged look; unblurred it reads as a crisp
    oscilloscope-style double trace."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.3, 4.0)}

    _PERIOD_CYCLES = 3.0  # full sine cycles across the frame width at scale=1.0
    _AMP_FRAC = 0.38  # strand amplitude, as a fraction of height
    _LEVEL_AMP_GAIN = 0.3
    _BEAT_PHASE_GAIN = 0.6

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.5,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        self._xs = np.arange(width, dtype=np.int32)
        self._xfrac = self._xs.astype(np.float32) / width
        self._cy = height / 2.0
        self._amp = height * self._AMP_FRAC

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        phase = t * self.speed * 2.0 * math.pi
        w = self._xfrac * self._PERIOD_CYCLES * self.scale * 2.0 * math.pi
        amp = self._amp
        hue_off = 0.0
        val = 1.0
        if modulation is not None:
            phase += modulation.beat_phase * self._BEAT_PHASE_GAIN
            amp *= 1.0 + self._LEVEL_AMP_GAIN * modulation.level
            hue_off = self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        y1 = self._cy + amp * np.sin(w + phase)
        y2 = self._cy + amp * np.sin(w + phase + math.pi)
        y1i = np.clip(y1, 0, self.height - 1).astype(np.int32)
        y2i = np.clip(y2, 0, self.height - 1).astype(np.int32)
        hue1 = self._xfrac * 0.6 + t * 0.05 + hue_off
        hue2 = self._xfrac * 0.6 + 0.5 + t * 0.05 + hue_off
        colors1 = self._hsv_to_bgr(hue1[None, :], val=val)[0]
        colors2 = self._hsv_to_bgr(hue2[None, :], val=val)[0]
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[y1i, self._xs] = colors1
        frame[y2i, self._xs] = colors2
        return cv2.dilate(frame, np.ones((3, 3), np.uint8))


@register("drift")
class DriftSource(GenerativeSource):
    """WLED "Drift" port: a rotating spiral trail — for radii `i` stepping
    outward from center, a point at angle `t*(maxDim-i)` traces a full
    spiral arm every frame. Like `lissajous`, WLED already redraws the whole
    arm (`i` from 1 to maxDim) on every render call, so this ports as a pure
    function of `t` with no synthetic echo needed. Always draws both the
    `(sin,cos)` point AND its `(cos,sin)` mirror — WLED gates the mirror
    behind a "Twin" checkbox this codebase has no per-scene boolean toggle
    for, so it's always-on here, a deliberate simplification that gives a
    fuller, more symmetric rose by default."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.3, 2.0)}

    _STEP = 0.25
    _HUE_SCALE = 0.08
    _HUE_DRIFT = 0.05
    _LEVEL_GAIN = 0.3

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.5,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._max_dim = min(width, height) / 2.0
        self._i = np.arange(1.0, self._max_dim, self._STEP, dtype=np.float64)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        radius_scale = self.scale
        hue_off = 0.0
        val = 1.0
        if modulation is not None:
            radius_scale *= 1.0 + self._LEVEL_GAIN * modulation.level
            hue_off = self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        i = self._i
        angle = t * self.speed * (self._max_dim - i)
        r = i * radius_scale
        s = np.sin(angle)
        c = np.cos(angle)
        x1 = np.clip(self._cx + r * s, 0, self.width - 1).astype(np.int32)
        y1 = np.clip(self._cy + r * c, 0, self.height - 1).astype(np.int32)
        x2 = np.clip(self._cx + r * c, 0, self.width - 1).astype(np.int32)
        y2 = np.clip(self._cy + r * s, 0, self.height - 1).astype(np.int32)
        hue = np.mod(i * self._HUE_SCALE + t * self._HUE_DRIFT + hue_off, 1.0).astype(np.float32)
        colors = self._hsv_to_bgr(hue[None, :], val=val)[0]
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[y1, x1] = colors
        frame[y2, x2] = colors
        return cv2.dilate(frame, np.ones((2, 2), np.uint8))


@register("colored_bursts")
class ColoredBurstsSource(GenerativeSource):
    """WLED "Colored Bursts" port: several lines burst from one common,
    slowly-orbiting point out to per-line endpoints that trace their own
    faster orbits — WLED's shared start point has no per-line phase offset,
    while the per-line `i*24`/`i*48+64` phase spread on the *other* endpoint
    is what fans the lines out into a burst. A short trailing-echo stack
    (the same pattern `halo`/`epicycle` use) stands in for WLED's own
    `fadeToBlackBy` accumulation, since this must stay a pure function of
    `t`; echoes are drawn oldest-first so the brightest (most recent)
    position always paints on top."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.3, 3.0)}

    _N_LINES = 6
    _N_ECHOES = 3
    _ECHO_LAG = 0.05
    _ECHO_DECAY = 0.45
    _ONSET_FLASH_GAIN = 90.0
    _LEVEL_GAIN = 0.4
    _BEAT_PHASE_GAIN = 0.4

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.6,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._amp = min(width, height) * 0.4
        n = self._N_LINES
        self._end_phase_x = np.arange(n, dtype=np.float64) * (2.0 * math.pi / 12.0)
        self._end_phase_y = np.arange(n, dtype=np.float64) * (2.0 * math.pi / 7.0) + 1.1
        self._colors = [
            self._hsv_to_bgr(np.full((1, 1), i / n, np.float32))[0, 0].tolist() for i in range(n)
        ]

    def _endpoints(
        self, tt: float, amp: float
    ) -> tuple[tuple[float, float], np.ndarray, np.ndarray]:
        ax = self._cx + amp * math.sin(tt * 0.9)
        ay = self._cy + amp * math.sin(tt * 0.7)
        bx = self._cx + amp * np.sin(tt * 1.6 + self._end_phase_x)
        by = self._cy + amp * np.sin(tt * 1.3 + self._end_phase_y)
        return (ax, ay), bx, by

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        tt = t * self.speed
        amp = self._amp * self.scale
        onset = 0.0
        if modulation is not None:
            tt += modulation.beat_phase * self._BEAT_PHASE_GAIN
            amp *= 1.0 + self._LEVEL_GAIN * modulation.level
            onset = modulation.onset
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        for e in reversed(range(self._N_ECHOES)):
            te = tt - e * self._ECHO_LAG
            fade = self._ECHO_DECAY**e
            (ax, ay), bx, by = self._endpoints(te, amp)
            p0 = (int(round(ax)), int(round(ay)))
            for j in range(self._N_LINES):
                p1 = (int(round(bx[j])), int(round(by[j])))
                color = tuple(int(c * fade) for c in self._colors[j])
                cv2.line(frame, p0, p1, color, 1, cv2.LINE_AA)
        if onset > 0.0:
            flash = int(self._ONSET_FLASH_GAIN * onset)
            frame = cv2.add(frame, np.full_like(frame, flash))
        return frame


@register("dotswarm")
class DotSwarmSource(GenerativeSource):
    """A WLED "beatsin dot swarm" port covering the shared shape of several
    kin effects — Black Hole, Frizzles, Sindots, Squared Swirl, Drift Rose —
    which all boil down to the same primitive: a handful of points, each
    independently orbiting via a bounded sine (`beatsin8` in WLED) at its own
    frequency, color-cycled and blended. Rather than port each as its own
    near-identical generator, this ports the shared primitive ONCE with a
    fixed, varied per-dot frequency assortment (echoing the spread across all
    of them) plus a fixed white center dot (Black Hole's signature). A short
    trailing-echo stack fakes WLED's `fadeToBlackBy` persistence, the same
    pattern `halo`/`epicycle` use; echoes are drawn oldest-first so the
    brightest (most recent) position always paints on top."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.2, 2.0)}

    _N_DOTS = 12
    _N_ECHOES = 4
    _ECHO_LAG = 0.045
    _ECHO_DECAY = 0.5
    _ORBIT_FRAC = 0.42  # max orbit reach, as a fraction of min(width,height)
    _DOT_RADIUS = 2
    _LEVEL_GAIN = 0.25
    _BEAT_PHASE_GAIN = 0.3
    _ONSET_FLASH_GAIN = 60.0

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 0.7,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        self._cx = width / 2.0
        self._cy = height / 2.0
        self._orbit = min(width, height) * self._ORBIT_FRAC
        rng = np.random.default_rng(0xD07A)
        n = self._N_DOTS
        # Varied, deliberately non-harmonic per-dot frequencies (mirrors the
        # spread of distinct beatsin8 rates each WLED kin effect hand-picks).
        self._fx = rng.uniform(0.4, 2.6, n)
        self._fy = rng.uniform(0.4, 2.6, n)
        self._px = rng.uniform(0.0, 2.0 * math.pi, n)
        self._py = rng.uniform(0.0, 2.0 * math.pi, n)
        self._reach = rng.uniform(0.35, 1.0, n)
        hues = (np.arange(n) / n).astype(np.float32)
        self._colors = [
            self._hsv_to_bgr(np.full((1, 1), h, np.float32))[0, 0].tolist() for h in hues
        ]

    def _positions(self, tt: float, orbit: float) -> tuple[np.ndarray, np.ndarray]:
        x = self._cx + orbit * self._reach * np.sin(tt * self._fx + self._px)
        y = self._cy + orbit * self._reach * np.sin(tt * self._fy + self._py)
        return x, y

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        tt = t * self.speed
        orbit = self._orbit * self.scale
        gain = 1.0
        onset = 0.0
        if modulation is not None:
            tt += modulation.beat_phase * self._BEAT_PHASE_GAIN
            orbit *= 1.0 + self._LEVEL_GAIN * modulation.level
            gain = self._reactive_value(modulation) * 1.3
            onset = modulation.onset
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        for e in reversed(range(self._N_ECHOES)):
            te = tt - e * self._ECHO_LAG
            fade = self._ECHO_DECAY**e
            xs, ys = self._positions(te, orbit)
            for j in range(self._N_DOTS):
                color = tuple(int(c * fade) for c in self._colors[j])
                cv2.circle(
                    frame,
                    (int(round(xs[j])), int(round(ys[j]))),
                    self._DOT_RADIUS,
                    color,
                    -1,
                    cv2.LINE_AA,
                )
        cx, cy = int(round(self._cx)), int(round(self._cy))
        cv2.circle(frame, (cx, cy), self._DOT_RADIUS, (255, 255, 255), -1, cv2.LINE_AA)
        if gain != 1.0:
            frame = np.clip(frame.astype(np.float32) * gain, 0.0, 255.0).astype(np.uint8)
        if onset > 0.0:
            frame = cv2.add(frame, np.full_like(frame, int(self._ONSET_FLASH_GAIN * onset)))
        return frame


def _life_step(grid: np.ndarray, hue: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One Conway generation (standard B3/S23, torus-wrapped via `np.roll`).
    `hue` carries per-cell color; a newly-born cell's hue is the (linear, not
    circular — a documented simplification) mean of its exactly-3 live parent
    neighbors' hues, WLED's Game of Life's "parent color inheritance" touch.
    Dead cells' hue is left stale (irrelevant — never rendered)."""
    shifts = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    neighbor_count = np.zeros_like(grid, dtype=np.int8)
    hue_sum = np.zeros_like(hue)
    alive_hue = np.where(grid, hue, 0.0)
    for dy, dx in shifts:
        shifted_alive = np.roll(np.roll(grid, dy, axis=0), dx, axis=1)
        neighbor_count += shifted_alive
        hue_sum += np.roll(np.roll(alive_hue, dy, axis=0), dx, axis=1)
    born = (~grid) & (neighbor_count == 3)
    survive = grid & ((neighbor_count == 2) | (neighbor_count == 3))
    new_grid = born | survive
    # Born cells always have exactly 3 live neighbors (the B3 rule), so the
    # accumulated hue_sum / 3 is their new hue's mean; survivors keep theirs.
    new_hue = np.where(born, hue_sum / 3.0, hue)
    return new_grid, new_hue


@register("game_of_life")
class GameOfLifeSource(GenerativeSource):
    """WLED "Game Of Life" port: Conway's Game of Life on a coarse grid
    (chunky upscaled cells — reads great after C64 quantization, especially on
    PETSCII), with WLED's signature parent-color inheritance (a newly-born
    cell's hue is the mean of its live parents' hues).

    Unlike the dot/line family (Tier 2), this is a genuinely *stateful*
    simulation — generation N can't be computed without generation N-1 — so it
    can't be a closed-form function of `t` the way plasma/tunnel are. It stays
    a **pure** function of `t` anyway (unlike `SoapSource`/`FireworksSource`
    below) by replaying the whole simulation from a fixed-seed initial soup
    for `floor(t / STEP_S)` generations every time it's asked for a frame —
    the same trick `mandelbrot`/`hopalong` use to stay pure despite doing real
    per-frame work. A capped `_EPOCH_GENERATIONS` bounds replay cost and
    doubles as WLED's adaptive "stagnation restart" (detecting a dead/looping
    board and reseeding) — here it's a fixed-length cycle instead of adaptive
    detection, a documented simplification in the same spirit as
    `rotozoomer`'s closed-form angle or `metaballs`' perlin substitution. An
    instance-level cache (keyed on the reachable `(epoch, generation)` pair,
    not on call order) makes sequential real playback cheap — stepping
    forward from the last-computed generation instead of replaying from
    scratch — without weakening the purity guarantee: a cache miss (a new
    epoch, or `t` landing before the cached generation) always re-derives from
    the fixed seed, so the result never depends on *how* a given `t` was
    reached, only on `t` itself."""

    LIVE_PARAMS = {"speed": (0.1, 4.0)}

    _CELL_PX = 4  # grid resolution: width/height divided by this
    _SEED = 0x60FE
    _DENSITY = 0.28  # fraction of cells alive in a fresh soup
    _STEP_S = 0.15  # seconds per generation at speed=1.0
    _EPOCH_GENERATIONS = 200  # replay cap per epoch (~hopalong's iteration budget)

    def __init__(self, *, width: int = GEN_WIDTH, height: int = GEN_HEIGHT, speed: float = 1.0):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self._grid_w = max(4, width // self._CELL_PX)
        self._grid_h = max(4, height // self._CELL_PX)
        self._epoch_s = self._STEP_S * self._EPOCH_GENERATIONS
        self._cache_epoch: int | None = None
        self._cache_gen = -1
        self._cache_grid: np.ndarray | None = None
        self._cache_hue: np.ndarray | None = None

    def reset(self) -> None:
        self._cache_epoch = None
        self._cache_gen = -1
        self._cache_grid = None
        self._cache_hue = None

    def _seed_epoch(self, epoch: int) -> tuple[np.ndarray, np.ndarray]:
        rng = np.random.default_rng(self._SEED + epoch)
        grid = rng.random((self._grid_h, self._grid_w)) < self._DENSITY
        hue = rng.random((self._grid_h, self._grid_w)).astype(np.float32)
        return grid, hue

    def _state_at(self, epoch: int, gen: int) -> tuple[np.ndarray, np.ndarray]:
        if self._cache_epoch == epoch and gen >= self._cache_gen:
            assert self._cache_grid is not None and self._cache_hue is not None
            grid, hue, cur_gen = self._cache_grid, self._cache_hue, self._cache_gen
        else:
            grid, hue = self._seed_epoch(epoch)
            cur_gen = 0
        while cur_gen < gen:
            grid, hue = _life_step(grid, hue)
            cur_gen += 1
        self._cache_epoch, self._cache_gen = epoch, gen
        self._cache_grid, self._cache_hue = grid, hue
        return grid, hue

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        tt = max(0.0, t) * self.speed
        epoch = int(tt // self._epoch_s)
        local = tt - epoch * self._epoch_s
        gen = min(self._EPOCH_GENERATIONS, int(local // self._STEP_S))
        grid, hue = self._state_at(epoch, gen)
        hue_off = 0.0
        val = 1.0
        if modulation is not None:
            hue_off = self._reactive_hue_offset(modulation)
            val = self._reactive_value(modulation)
        small = np.zeros((self._grid_h, self._grid_w, 3), dtype=np.uint8)
        alive_idx = np.nonzero(grid)
        if alive_idx[0].size:
            cell_hue = np.mod(hue[alive_idx] + hue_off, 1.0).astype(np.float32)
            small[alive_idx] = self._hsv_to_bgr(cell_hue[None, :], val=val)[0]
        return cv2.resize(small, (self.width, self.height), interpolation=cv2.INTER_NEAREST)


@register("soap")
class SoapSource(GenerativeSource):
    """WLED "Soap" port: a persistent color buffer smeared/advected each tick
    by a slowly-rotating noise-driven flow field — the classic swirling
    soap-film look. WLED derives its flow from `perlin8`; this codebase has no
    Perlin primitive, so (mirroring `metaballs`'/`hopalong`'s precedent of
    substituting an existing tool rather than adding one) it reuses the
    tileable value-noise helper `_periodic_value_noise` already built for
    `FireSource`, sampled twice for independent x/y flow components.

    Unlike `GameOfLifeSource` above, replaying this from scratch every frame
    is too expensive (a full-buffer `cv2.remap` per generation, not a handful
    of scalar ops), so this carries **real incremental state**: `render(t,
    ...)` tracks elapsed scene-clock time since the last call and advances a
    fixed-size-tick accumulator (the standard fixed-timestep-with-accumulator
    pattern — handles variable frame arrival / dropped frames gracefully,
    same spirit as the pure generators' "dropped frames harmless" guarantee,
    just via accumulation instead of recomputation). A call whose `t` doesn't
    advance (repeated or a backward jump) takes no step and re-returns the
    current buffer, so `render(t, None)` is still stable for a fixed,
    non-advancing `t` — the property the shared determinism test checks —
    even though (unlike the pure generators) jumping directly to an arbitrary
    `t` on a fresh instance does *not* reproduce the same frame as advancing
    there gradually; state is genuinely carried, not replayed. A small
    fraction of the original seed pattern is blended back in every step (an
    energy-injection term) so repeated bilinear remapping doesn't decay the
    buffer to a flat gray over a long-running scene."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.2, 3.0)}

    _STEP_S = 0.08
    _PHASE_STEP = 0.012  # radians the flow field's rotation advances per step
    _FLOW_FRAC = 0.05  # base flow displacement, as a fraction of min(width,height)
    _INJECT = 0.006  # per-step blend-back of the original seed pattern

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 1.0,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        self._xs = xs
        self._ys = ys
        rng = np.random.default_rng(0x50A9)
        seed_hue = _periodic_value_noise(rng, height, width, octaves=[(3, 4, 1.0), (6, 8, 0.5)])
        self._seed_buf = self._hsv_to_bgr(seed_hue).astype(np.float32)
        flow_a = _periodic_value_noise(rng, height, width, octaves=[(4, 5, 1.0), (9, 11, 0.5)])
        flow_b = _periodic_value_noise(rng, height, width, octaves=[(5, 4, 1.0), (11, 9, 0.5)])
        self._flow_a = flow_a * 2.0 - 1.0
        self._flow_b = flow_b * 2.0 - 1.0
        self._flow_amp = min(width, height) * self._FLOW_FRAC
        self._buf = self._seed_buf.copy()
        self._phase = 0.0
        self._last_t = 0.0
        self._accum = 0.0

    def reset(self) -> None:
        self._buf = self._seed_buf.copy()
        self._phase = 0.0
        self._last_t = 0.0
        self._accum = 0.0

    def _step(self) -> None:
        self._phase += self._PHASE_STEP
        c, s = math.cos(self._phase), math.sin(self._phase)
        vx = self._flow_a * c + self._flow_b * s
        vy = -self._flow_a * s + self._flow_b * c
        amp = self._flow_amp * self.scale
        map_x = (self._xs + vx * amp).astype(np.float32)
        map_y = (self._ys + vy * amp).astype(np.float32)
        warped = cv2.remap(
            self._buf, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP
        )
        self._buf = warped * (1.0 - self._INJECT) + self._seed_buf * self._INJECT

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        dt = t - self._last_t
        self._last_t = t
        if dt > 0.0:
            self._accum += dt * max(self.speed, 0.0)
            while self._accum >= self._STEP_S:
                self._step()
                self._accum -= self._STEP_S
        gain = 1.0 if modulation is None else self._reactive_value(modulation) * 1.3
        frame = np.clip(self._buf * gain, 0.0, 255.0).astype(np.uint8)
        return frame


@register("fireworks")
class FireworksSource(GenerativeSource):
    """WLED "Fireworks" port — the flagship of WLED's shared particle-system
    engine (which also drives Volcano/Ballpit/Waterfall/Impact/Attractor/
    Galaxy as different emitter/gravity presets on the same primitive; only
    the fireworks preset is ported this batch — see
    `[[project_wled_pattern_port_candidates]]` for the deferred variants).

    A small fixed-size particle pool (preallocated numpy arrays — position /
    velocity / age / life / hue — updated with vectorized array ops, no
    per-particle Python loop) simulates: shells launch upward on a randomized
    schedule, arc under gravity, and explode into a burst of particles on a
    randomized fuse timer; particles then fall under gravity with velocity
    drag, fading out over their lifetime.

    Like `SoapSource`, this carries real incremental state (particle physics
    can't be cheaply replayed from an arbitrary `t` — position depends on the
    whole integration history) via the same tick-accumulator pattern: no
    advance in `t` -> no physics step -> the current frame is re-returned
    unchanged. Shell/particle spawn timing draws from a `numpy` Generator
    advanced once per step (not reseeded per call), so a given *real playback
    sequence* is reproducible run-to-run but — unlike the pure generators —
    not byte-identical for an arbitrary directly-requested `t` on a fresh
    instance; this is the deliberate tradeoff that comes with genuine particle
    state, documented rather than worked around.

    No synthetic per-particle trail is drawn (unlike halo/epicycle/
    colored_bursts' time-lag echoes) — pairing this scene with the existing
    `trails` FrameEffect gives the classic streak look for free, the same way
    `dna`/`metaballs` lean on `blur` rather than reinventing persistence
    per-generator."""

    LIVE_PARAMS = {"speed": (0.0, 3.0), "scale": (0.3, 3.0)}

    _STEP_S = 1.0 / 30.0
    _MAX_SHELLS = 6
    _MAX_PARTICLES = 260
    _BURST_SIZE = 45
    _GRAVITY_FRAC = 0.55  # px/s^2 of downward accel, as a fraction of height
    _LAUNCH_SPEED_FRAC = 0.9  # shell launch speed, as a fraction of height/s
    _DRAG = 0.985  # per-tick multiplicative velocity decay (particles only)
    _FUSE_TICKS_RANGE = (14, 26)  # ticks before a shell explodes
    _LAUNCH_INTERVAL_RANGE = (0.4, 1.1)  # seconds between shell launches at speed=1
    _PARTICLE_SPEED_FRAC = 0.35  # burst particle speed, as a fraction of height/s
    _LIFE_RANGE = (0.7, 1.3)  # seconds a burst particle survives
    _DOT_RADIUS_KERNEL = 2
    _ONSET_THRESHOLD = 0.55
    _LEVEL_INTERVAL_GAIN = 0.6  # loudness shortens the next launch interval

    def __init__(
        self,
        *,
        width: int = GEN_WIDTH,
        height: int = GEN_HEIGHT,
        speed: float = 1.0,
        scale: float = 1.0,
    ):
        super().__init__(width=width, height=height)
        self.speed = float(speed)
        self.scale = float(scale)
        self._rng = np.random.default_rng(0xF12E ^ 0x5040)
        self._gravity = height * self._GRAVITY_FRAC
        self._launch_speed = height * self._LAUNCH_SPEED_FRAC
        self._particle_speed = height * self._PARTICLE_SPEED_FRAC
        self._init_state()
        self._last_t = 0.0
        self._accum = 0.0

    def _init_state(self) -> None:
        n_s = self._MAX_SHELLS
        self._shell_alive = np.zeros(n_s, dtype=bool)
        self._shell_x = np.zeros(n_s, dtype=np.float32)
        self._shell_y = np.zeros(n_s, dtype=np.float32)
        self._shell_vy = np.zeros(n_s, dtype=np.float32)
        self._shell_hue = np.zeros(n_s, dtype=np.float32)
        self._shell_fuse = np.zeros(n_s, dtype=np.int32)
        self._shell_age = np.zeros(n_s, dtype=np.int32)

        n_p = self._MAX_PARTICLES
        self._p_alive = np.zeros(n_p, dtype=bool)
        self._p_x = np.zeros(n_p, dtype=np.float32)
        self._p_y = np.zeros(n_p, dtype=np.float32)
        self._p_vx = np.zeros(n_p, dtype=np.float32)
        self._p_vy = np.zeros(n_p, dtype=np.float32)
        self._p_age = np.zeros(n_p, dtype=np.float32)
        self._p_life = np.zeros(n_p, dtype=np.float32)
        self._p_hue = np.zeros(n_p, dtype=np.float32)
        self._next_launch_s = 0.0
        self._sim_t = 0.0

    def reset(self) -> None:
        self._init_state()
        self._last_t = 0.0
        self._accum = 0.0

    def _launch_shell(self) -> None:
        free = np.nonzero(~self._shell_alive)[0]
        if free.size == 0:
            return
        i = int(free[0])
        self._shell_alive[i] = True
        self._shell_x[i] = self._rng.uniform(0.2, 0.8) * self.width
        self._shell_y[i] = float(self.height - 1)
        self._shell_vy[i] = -self._launch_speed * self._rng.uniform(0.85, 1.15)
        self._shell_hue[i] = self._rng.uniform(0.0, 1.0)
        lo, hi = self._FUSE_TICKS_RANGE
        self._shell_fuse[i] = self._rng.integers(lo, hi + 1)
        self._shell_age[i] = 0

    def _explode(self, x: float, y: float, hue: float) -> None:
        free = np.nonzero(~self._p_alive)[0]
        k = min(self._BURST_SIZE, free.size)
        if k == 0:
            return
        idx = free[:k]
        angles = self._rng.uniform(0.0, 2.0 * math.pi, k)
        speeds = self._rng.uniform(0.4, 1.0, k) * self._particle_speed * self.scale
        self._p_alive[idx] = True
        self._p_x[idx] = x
        self._p_y[idx] = y
        self._p_vx[idx] = np.cos(angles) * speeds
        self._p_vy[idx] = np.sin(angles) * speeds
        self._p_age[idx] = 0.0
        lo, hi = self._LIFE_RANGE
        self._p_life[idx] = self._rng.uniform(lo, hi, k)
        self._p_hue[idx] = np.mod(hue + self._rng.uniform(-0.06, 0.06, k), 1.0)

    def _step(self) -> None:
        dt = self._STEP_S
        self._sim_t += dt
        if self._sim_t >= self._next_launch_s:
            self._launch_shell()
            lo, hi = self._LAUNCH_INTERVAL_RANGE
            self._next_launch_s = self._sim_t + self._rng.uniform(lo, hi)

        alive = self._shell_alive
        if alive.any():
            self._shell_vy[alive] += self._gravity * dt
            self._shell_y[alive] += self._shell_vy[alive] * dt
            self._shell_age[alive] += 1
            fuse_done = alive & (self._shell_age >= self._shell_fuse)
            offscreen = alive & (self._shell_y < 0.0)
            for i in np.nonzero(fuse_done | offscreen)[0]:
                if fuse_done[i]:
                    self._explode(
                        float(self._shell_x[i]), float(self._shell_y[i]), float(self._shell_hue[i])
                    )
                self._shell_alive[i] = False

        palive = self._p_alive
        if palive.any():
            self._p_vy[palive] += self._gravity * dt
            self._p_vx[palive] *= self._DRAG
            self._p_vy[palive] *= self._DRAG
            self._p_x[palive] += self._p_vx[palive] * dt
            self._p_y[palive] += self._p_vy[palive] * dt
            self._p_age[palive] += dt
            self._p_alive &= (self._p_age < self._p_life) & (self._p_y < self.height + 8)

    def render(self, t: float, modulation: MusicModulation | None = None) -> np.ndarray:
        dt = t - self._last_t
        self._last_t = t
        if dt > 0.0:
            speed = max(self.speed, 0.0)
            level_gain = 0.0 if modulation is None else self._LEVEL_INTERVAL_GAIN * modulation.level
            self._accum += dt * speed
            while self._accum >= self._STEP_S:
                self._step()
                self._accum -= self._STEP_S
            if level_gain > 0.0:
                self._next_launch_s = max(
                    self._sim_t, self._next_launch_s - level_gain * self._STEP_S
                )
        # A strong transient bursts immediately regardless of whether a physics
        # tick fired this call — an onset is a discrete "the beat hit" reaction,
        # not something that should wait on the tick accumulator.
        if modulation is not None and modulation.onset > self._ONSET_THRESHOLD:
            self._explode(
                float(self._rng.uniform(0.2, 0.8) * self.width),
                float(self._rng.uniform(0.2, 0.6) * self.height),
                float(self._rng.uniform(0.0, 1.0)),
            )

        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        val = 1.0 if modulation is None else self._reactive_value(modulation)

        s_idx = np.nonzero(self._shell_alive)[0]
        p_idx = np.nonzero(self._p_alive)[0]
        if s_idx.size:
            sx = np.clip(self._shell_x[s_idx], 0, self.width - 1).astype(np.int32)
            sy = np.clip(self._shell_y[s_idx], 0, self.height - 1).astype(np.int32)
            scolors = self._hsv_to_bgr(self._shell_hue[s_idx][None, :], val=val)[0]
            frame[sy, sx] = scolors
        if p_idx.size:
            px = np.clip(self._p_x[p_idx], 0, self.width - 1).astype(np.int32)
            py = np.clip(self._p_y[p_idx], 0, self.height - 1).astype(np.int32)
            fade = np.clip(1.0 - self._p_age[p_idx] / self._p_life[p_idx], 0.0, 1.0)
            pcolors = self._hsv_to_bgr(self._p_hue[p_idx][None, :], val=val)[0]
            pcolors = (pcolors.astype(np.float32) * fade[:, None]).astype(np.uint8)
            frame[py, px] = pcolors
        if s_idx.size or p_idx.size:
            frame = cv2.dilate(
                frame, np.ones((self._DOT_RADIUS_KERNEL, self._DOT_RADIUS_KERNEL), np.uint8)
            )
        return frame
