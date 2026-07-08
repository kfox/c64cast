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
