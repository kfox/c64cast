"""Music-feature struct that drives reactive generative visuals.

The "music-reactive" building block, parallel to FrameSource / AudioSource: a
small, generator-agnostic snapshot of what the music is doing right now, which a
GenerativeSource reads to scale its own parameters. This decouples *what reacts*
(the generator) from *how the features are measured* (the audio source) — today
a SID host-emulator (music_features.SidFeatureStream) and an audio-input
analyzer (audio_features.AudioFeatureStream), tomorrow a MIDI event stream, all
behind this same struct.

`TempoEstimator` lives here for the same reason: both producers need the
identical onset-rate → BPM → beat-phase math, and this module is the one place
both can import without dragging in the other's heavy deps.

Deliberately tiny and dependency-free (stdlib only) so both the generators
(numpy/cv2), the SID feature stream (py65), and the audio analyzer (numpy) can
import it without pulling each other's deps in.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MusicModulation:
    """A point-in-time snapshot of music features for driving visuals.

    All fields are normalized or physical and generator-agnostic — the generator
    decides how to map them onto its parameters (see generators.py). A frozen
    snapshot so the render thread reads a consistent set while the feature thread
    builds the next one.

    Fields:
      * `level` — overall intensity, ~[0, 1] (mean of the per-voice envelope
        levels). Drives "louder ⇒ brighter".
      * `onset` — transient strength right now, [0, 1]. Spikes to 1.0 on a note
        attack / hard-restart and decays back toward 0; drives "transient ⇒
        color pulse / flash".
      * `beat_phase` — accumulated beats, the running integral of `bpm / 60` over
        time. Only advances while a tempo is known. Because it's an integral, a
        jittery `bpm` estimate never causes a phase discontinuity — drives a
        tempo-locked cycle rate smoothly.
      * `bpm` — estimated tempo (an onset-rate proxy, not a true beat tracker),
        0.0 when unknown.
      * `voice_freqs` — per-voice oscillator frequency in Hz (0.0 = silent).
      * `voice_gates` — per-voice gate bit (note held).
      * `bands` — N log-spaced band energies in [0, 1], low→high. Empty when the
        source has no spectrum to report (the SID stream reads envelopes, not an
        FFT), which is why it's defaulted — every pre-existing construction site
        stays valid. Read it via `bass`/`mid`/`treble` for generator code that
        shouldn't care how many bands the analyzer was configured for.
    """

    level: float
    onset: float
    beat_phase: float
    bpm: float
    voice_freqs: tuple[float, float, float]
    voice_gates: tuple[bool, bool, bool]
    bands: tuple[float, ...] = ()

    # ---- band folds ---------------------------------------------------------
    # Thirds of whatever band count the analyzer produced, so a generator can say
    # "bass" without knowing that [audio_features].bands is 8 or 16. All three
    # read 0.0 when `bands` is empty, which is what keeps the SID path's visuals
    # byte-identical to before this field existed.

    @property
    def bass(self) -> float:
        """Mean energy of the lowest third of `bands` (0.0 when empty)."""
        return self._band_third(0)

    @property
    def mid(self) -> float:
        """Mean energy of the middle third of `bands` (0.0 when empty)."""
        return self._band_third(1)

    @property
    def treble(self) -> float:
        """Mean energy of the highest third of `bands` (0.0 when empty)."""
        return self._band_third(2)

    def _band_third(self, which: int) -> float:
        n = len(self.bands)
        if n == 0:
            return 0.0
        lo = (n * which) // 3
        hi = (n * (which + 1)) // 3
        if hi <= lo:  # fewer bands than thirds — clamp to a single band
            lo = min(lo, n - 1)
            hi = lo + 1
        chunk = self.bands[lo:hi]
        return float(sum(chunk) / len(chunk))


@dataclass
class TempoEstimator:
    """Onset-rate → BPM → beat-phase, shared by every music-feature producer.

    Not a true beat tracker: it EMAs the interval between onsets, folded into a
    plausible tempo band. `beat_phase` is the running integral of `bpm / 60`, so
    a jittery estimate never causes a phase discontinuity — the cycle rate it
    drives stays smooth. A real beat tracker can be dropped in behind the same
    two-method interface later.

    Lifted verbatim (logic and constants) from `SidFeatureStream`, which was the
    only producer until the audio-input analyzer needed exactly the same math.
    Not thread-safe on its own: callers own the lock (both producers already hold
    one around their feature accumulators).

    Usage per tick: `advance(dt)` every tick, `note_onset(now)` on a transient.
    """

    # Onsets closer than _MIN_IOI are treated as the same beat (near-simultaneous
    # voices / a smeared transient); gaps longer than _MAX_IOI re-anchor without
    # polluting the estimate (a rest / phrase boundary). The derived BPM is
    # clamped to [_BPM_MIN, _BPM_MAX].
    MIN_IOI_S: float = 0.10
    MAX_IOI_S: float = 1.50
    IOI_EMA_ALPHA: float = 0.30
    BPM_MIN: float = 50.0
    BPM_MAX: float = 220.0

    bpm: float = field(default=0.0, init=False)
    beat_phase: float = field(default=0.0, init=False)

    _ioi_ema: float | None = field(default=None, init=False)
    _last_onset_time: float | None = field(default=None, init=False)

    def reset(self) -> None:
        """Clear the estimate + phase so a fresh stream starts clean."""
        self.bpm = 0.0
        self.beat_phase = 0.0
        self._ioi_ema = None
        self._last_onset_time = None

    def advance(self, dt: float) -> None:
        """Integrate `dt` seconds of beat phase. Frozen while tempo is unknown."""
        if self.bpm > 0.0:
            self.beat_phase += (self.bpm / 60.0) * dt

    def note_onset(self, now: float) -> None:
        """Fold an onset at time `now` into the tempo estimate."""
        last = self._last_onset_time
        if last is None:
            self._last_onset_time = now
            return
        ioi = now - last
        if ioi < self.MIN_IOI_S:
            # Another attack on the same beat — keep the earlier reference so the
            # next beat-to-beat interval isn't corrupted.
            return
        self._last_onset_time = now
        if ioi > self.MAX_IOI_S:
            # Long gap (rest / phrase boundary) — re-anchor without polluting
            # the estimate.
            return
        if self._ioi_ema is None:
            self._ioi_ema = ioi
        else:
            a = self.IOI_EMA_ALPHA
            self._ioi_ema = (1.0 - a) * self._ioi_ema + a * ioi
        self.bpm = min(self.BPM_MAX, max(self.BPM_MIN, 60.0 / self._ioi_ema))
