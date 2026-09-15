"""Pixel effects — frame transforms applied before quantization.

A `FrameEffect` reads and transforms a scene's BGR frame each tick. Effects are
applied in `scenes._render_with_overlays`, right before the display mode
downscales + quantizes — so *every* frame-based scene (webcam / video /
slideshow / generative) supports them with no per-scene wiring. The transform
runs at full source resolution; for time-varying or feedback effects the scene
passes the current time `t` and resets effect state at scene setup.

`apply` also takes an optional `MusicModulation` snapshot (the same struct
generators read). When present, an effect modulates itself from it; when it is
`None` each effect falls back to its baseline behavior, and the zoom/shift
effects to the identity transform, so the unmodulated path stays byte-stable for
the offline renderer and the determinism tests.

Effects are registered by name and built via `build_effect(name)`. Add a
`@register("name")` subclass of `FrameEffect` and it shows up in config
discovery + the `_EFFECT_CHOICES` list (a drift test pins the match).

See docs/architecture/scenes.md#effectspy--the-frameeffect-registry.
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
    (the determinism guard the offline renderer + tests rely on).

    A scene holds an ordered `scene.effects` list, applied in order in
    `scenes._render_with_overlays`; the two per-layer knobs below live here so
    every effect gets them. See
    docs/architecture/scenes.md#the-layerable-chain-live-djvj-phase-3.
    """

    name = "base"

    # Bypass toggle (fx_toggle). A plain attribute, so the reader-thread flip is
    # GIL-atomic, and the skip happens in the render loop rather than inside
    # apply(), so a disabled layer is exact identity.
    enabled: bool = True

    # Which MusicModulation feeder drives this layer: "audio" (the scene's
    # feature stream), "clock" (the TempoClock beat grid), or "off" (never
    # react). Set per-scene by config.build_scene.
    mod_source: str = "audio"

    # name -> (min, max) for a CC-style [0, 1] sweep. midi_control.py scales
    # into the range and setattr()s directly, so only independent
    # single-numeric fields belong here: a plain setattr is GIL-atomic, and a
    # value split across two fields would not be.
    LIVE_PARAMS: dict[str, tuple[float, float]] = {}

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
    modulation the decay is exactly the configured `decay`."""

    _ONSET_DECAY = 0.12  # extra decay (longer tail) at a full transient
    _LEVEL_DECAY = 0.06  # extra decay from sustained loudness
    _MAX_DECAY = 0.97  # hard ceiling — must stay < 1 or the tail never fades

    LIVE_PARAMS = {"decay": (0.0, 0.96)}  # stays under _MAX_DECAY's reactive headroom

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
    unchanged.

    `intensity` scales the whole reaction (the sx/ix live knob); 1.0 is the
    baseline. The effect being inert without modulation, that slider is a
    visible no-op on a non-reactive scene."""

    _ONSET_ZOOM = 0.18  # +18% scale at a full transient (the on-beat punch)
    _LEVEL_ZOOM = 0.06  # steady zoom from loudness

    LIVE_PARAMS = {"intensity": (0.0, 2.5)}

    def __init__(self, intensity: float = 1.0):
        self.intensity = float(intensity)

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        if modulation is None:
            return frame
        scale = 1.0 + self.intensity * (
            self._ONSET_ZOOM * modulation.onset + self._LEVEL_ZOOM * modulation.level
        )
        if scale <= 1.0:
            return frame
        h, w = frame.shape[:2]
        ch = int(round(h / scale))
        cw = int(round(w / scale))
        if ch < 1 or cw < 1 or (ch == h and cw == w):
            return frame
        # A center crop stretched back to full size: the content grows toward
        # the edges without shifting.
        y0 = (h - ch) // 2
        x0 = (w - cw) // 2
        crop = frame[y0 : y0 + ch, x0 : x0 + cw]
        return cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)


@register("rgb_shift")
class RgbShiftEffect(FrameEffect):
    """Chromatic split: a transient slews the red and blue channels apart
    horizontally (opposite directions), an RGB-shift glitch shudder that snaps
    on the beat and relaxes as `onset` decays; loudness adds a steady split.
    Stateless. No modulation → identity (zero separation).

    `intensity` scales the whole reaction (the sx/ix live knob); 1.0 is the
    baseline. Inert without modulation, so this slider is a visible no-op on a
    non-reactive scene."""

    _ONSET_SHIFT = 6.0  # px of R/B separation at a full transient
    _LEVEL_SHIFT = 2.0  # steady separation from loudness

    LIVE_PARAMS = {"intensity": (0.0, 2.5)}

    def __init__(self, intensity: float = 1.0):
        self.intensity = float(intensity)

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        if modulation is None:
            return frame
        shift = int(
            round(
                self.intensity
                * (self._ONSET_SHIFT * modulation.onset + self._LEVEL_SHIFT * modulation.level)
            )
        )
        if shift <= 0:
            return frame
        # BGR: channel 0 = blue, channel 2 = red, rolled opposite ways with G
        # left put, so the image core reads through the colored fringes.
        # np.roll's wrapped edge column reads as part of the glitch.
        out = frame.copy()
        out[..., 0] = np.roll(frame[..., 0], shift, axis=1)
        out[..., 2] = np.roll(frame[..., 2], -shift, axis=1)
        return out


@register("blur")
class BlurEffect(FrameEffect):
    """Gaussian blur (`cv2.GaussianBlur`) — the first blur primitive in the
    codebase, added as an enabler for future dot/trail-family generator ports
    (WLED leans on `SEGMENT.blur` throughout its 2D effects).

    Unlike `pulse`/`rgb_shift`, this is NOT reactive-only: the default
    `intensity` is 0.0, so the "identity without modulation" guarantee comes
    from the base value rather than from `modulation is None`, and a reactive
    scene adds an onset kick on top of the configured base. Stateless.

    `intensity` is used directly as `cv2.GaussianBlur`'s `sigmaX` (cv2 derives
    the kernel size from sigma), and is named for the `effect.intensity`
    live-param convention rather than for blur."""

    _ONSET_KICK = 3.0  # extra sigma at a full transient, on top of the base

    LIVE_PARAMS = {"intensity": (0.0, 8.0)}

    def __init__(self, intensity: float = 0.0):
        self.intensity = float(intensity)

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        sigma = self.intensity
        if modulation is not None:
            sigma += self._ONSET_KICK * modulation.onset
        if sigma <= 0.05:
            return frame
        return cv2.GaussianBlur(frame, (0, 0), sigmaX=sigma)


@register("strobe")
class StrobeEffect(FrameEffect):
    """Tempo-locked strobe: blanks the frame to black for part of every beat, so
    the picture flashes on the grid. The one effect that earns its keep from
    `mod_source = "clock"` — pointed at the Phase-1 beat grid it locks to the
    bar; pointed at SID audio it flashes on the beat envelope instead.

    Purely phase-driven off `modulation.beat_phase`: the beat is split into a lit
    fraction (`duty`) and a dark remainder, and `rate` multiplies the phase so a
    rate of 2 strobes twice per beat, 4 four times, etc. With no modulation (or
    `mod_source = "off"`) there's no phase to read, so it's the identity
    transform — a non-reactive scene that selects `strobe` sees its frame
    unchanged, and the byte-stable determinism guarantee holds.

    `duty` in (0,1] is the lit fraction of each strobe cycle (1.0 = always lit =
    effectively off); `rate` is strobes per beat."""

    LIVE_PARAMS = {"duty": (0.05, 1.0), "rate": (1.0, 16.0)}

    def __init__(self, duty: float = 0.5, rate: float = 1.0):
        self.duty = float(duty)
        self.rate = float(rate)

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        if modulation is None or self.duty >= 1.0:
            return frame
        # `beat_phase` is the monotonic beat integral, jitter-immune, so the
        # flash never stutters.
        cycle = (modulation.beat_phase * max(1.0, self.rate)) % 1.0
        if cycle < self.duty:
            return frame
        # A fresh black frame: never mutate the caller's buffer.
        return np.zeros_like(frame)


@register("invert")
class InvertEffect(FrameEffect):
    """Photo-negative: blends the frame toward its color inverse (`255 - px`).
    `mix` in [0,1] sets how far — 1.0 (default) is a full invert, 0.0 a no-op —
    so a knob can crossfade the negative in, and a `fx_toggle` can flick the
    whole layer. Not reactive: `mix` is a static/live value, independent of
    `modulation`, so the effect is byte-stable on every scene."""

    LIVE_PARAMS = {"mix": (0.0, 1.0)}

    def __init__(self, mix: float = 1.0):
        self.mix = float(mix)

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        mix = max(0.0, min(1.0, self.mix))
        if mix <= 0.0:
            return frame
        inv = cv2.bitwise_not(frame)
        if mix >= 1.0:
            return inv
        return cv2.addWeighted(inv, mix, frame, 1.0 - mix, 0.0)


@register("mirror")
class MirrorEffect(FrameEffect):
    """Symmetry fold: reflects one half of the frame onto the other, the classic
    VJ kaleidoscope-lite look. `axis` picks the fold — a live-cyclable choice
    (`horizontal` mirrors left→right, `vertical` top→bottom, `quad` both for a
    four-way kaleidoscope). Not reactive; byte-stable on every scene.

    The one effect in the family with a `LIVE_CHOICES` discrete knob rather than
    a scalar: a note/pad mapped to `effect.axis` cycles the fold, a CC buckets
    across the three."""

    LIVE_CHOICES = {"axis": ("horizontal", "vertical", "quad")}

    def __init__(self, axis: str = "horizontal"):
        self.axis = axis if axis in self.LIVE_CHOICES["axis"] else "horizontal"

    # DisplayMode's set/get_live_choice contract, which
    # midi_control._apply_param and the WLED bridge drive with no
    # effect-specific code. `api` is unused — an effect writes no VIC registers
    # — and kept for parity with that contract.
    def set_live_choice(self, api: object, name: str, value: str) -> str | None:
        if name == "axis" and value in self.LIVE_CHOICES["axis"]:
            self.axis = value
            return f"axis {value}"
        return None

    def get_live_choice(self, name: str) -> str | None:
        return self.axis if name == "axis" else None

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        axis = self.axis
        out = frame
        if axis in ("horizontal", "quad"):
            out = out.copy()
            w = out.shape[1]
            half = w // 2
            if half > 0:
                out[:, w - half :] = out[:, :half][:, ::-1]
        if axis in ("vertical", "quad"):
            out = out if out is not frame else out.copy()
            h = out.shape[0]
            half = h // 2
            if half > 0:
                out[h - half :, :] = out[:half, :][::-1, :]
        return out


@register("posterize")
class PosterizeEffect(FrameEffect):
    """Level crush: quantizes each channel to `levels` steps, flattening the
    image into hard poster bands (a look that also pre-simplifies the frame for
    the C64 palette reduction downstream). `levels` in [2,32]; low values band
    hard, high values approach the source. Not reactive; byte-stable."""

    LIVE_PARAMS = {"levels": (2.0, 32.0)}

    def __init__(self, levels: float = 6.0):
        self.levels = float(levels)

    def apply(
        self, frame: np.ndarray, t: float, modulation: MusicModulation | None = None
    ) -> np.ndarray:
        levels = int(round(self.levels))
        if levels >= 256 or levels <= 1:
            return frame
        # Each band expands back to the full range so the brightest reaches 255,
        # where a plain floor would cap below white. The uint16 intermediate
        # keeps the multiply from overflowing uint8.
        step = 256 // levels
        if step <= 1:
            return frame
        idx = frame // step  # 0..levels-1
        span = 255 // (levels - 1)
        return (idx.astype(np.uint16) * span).clip(0, 255).astype(np.uint8)
