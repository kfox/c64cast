"""Process-wide musical beat grid for live performance (Phase 1 of the
Live DJ/VJ arc — see docs/architecture/control.md → "Live performance").

`TempoClock` is a tiny, dependency-light beat grid that every performance
consumer reads the same way a generator reads :class:`MusicModulation` today —
one grid, many consumers (launch quantization, effect tempo-lock, WLED). It has
two drive modes:

- **External MIDI clock** — the :mod:`midi_control` reader thread feeds it the
  real-time bytes ``0xF8`` clock (24 PPQN), ``0xFA`` start / ``0xFB`` continue /
  ``0xFC`` stop, and ``0xF2`` song-position, straight off the wire. `beat_phase`
  advances one 1/24-beat per clock pulse, so it tracks the DAW *exactly*, and a
  jittery inter-pulse interval never causes a phase discontinuity (mirrors the
  jitter-immunity rationale of :attr:`MusicModulation.beat_phase`).
- **Internal / tap tempo** — with no external clock, the grid free-runs at a
  static ``[performance].bpm``, and a mapped ``tempo_tap`` pad averages the
  inter-tap intervals into a live BPM (re-anchoring the downbeat to each tap).

The whole thing is host-side memory only: the feed methods do GIL-cheap writes
under a small lock and touch **no** DMA socket — the same rule the rest of
:mod:`midi_control` follows on its reader thread (clock at 24 PPQN @ 200 BPM is
only ~80 msg/s, trivially under any ceiling). Reads (`beat_phase` / `bar_phase`
/ `bpm` / `running`) extrapolate the phase from the last event under the same
lock, so a render/consumer thread always sees a consistent snapshot.

`ClockModulationSource` wraps a `TempoClock` as a `MusicModulation` feeder, so
effects/generators can be driven by MIDI tempo exactly as they are by SID audio
today, with no new effect wiring (the Phase-2/3 ``mod_source = clock`` selector
is what routes it). Deliberately stdlib-only (plus the leaf
:mod:`c64cast.modulation`) so it imports nowhere near mido or the heavy
render deps.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from .modulation import MusicModulation

if TYPE_CHECKING:
    from .config import PerformanceCfg

# Plausible tempo band. A pulse-derived or tap-derived BPM outside this range is
# treated as noise (a dropped/duplicated clock byte, a stray double-tap) and
# ignored rather than yanking the grid to an absurd tempo.
_BPM_MIN = 20.0
_BPM_MAX = 400.0

# EMA weight for smoothing the per-pulse BPM estimate. Low enough that a single
# jittery inter-pulse interval barely moves the displayed tempo, high enough to
# follow a real tempo ramp within a beat or two.
_PULSE_EMA_ALPHA = 0.2

# Tap-tempo history is cleared when this long passes with no tap (a new tap group
# starts a fresh average instead of blending across an old one).
_TAP_RESET_S = 2.0


class TempoClock:
    """A process-wide beat grid, GIL-atomically readable by every consumer.

    Construct one per :class:`~c64cast.playlist.Playlist` (mirrors
    ``Playlist.transport`` / ``Playlist.live_tracker``); the feed methods are
    called from the MIDI reader thread and the read side (`beat_phase` etc.)
    from the playlist/consumer threads. All state lives behind ``_lock`` — every
    method is cheap in-memory work, never any DMA/network I/O.

    Phase model: ``phase(now) = _phase_base + (now - _base_time) * bpm/60`` while
    running. External clock pulses snap ``_phase_base`` forward by exactly
    ``1/PPQN`` per pulse and re-anchor ``_base_time``, so the grid tracks the
    incoming clock exactly; between pulses the extrapolation (capped at one
    pulse's worth) keeps the phase smooth. Internal/tap mode has no pulses, so
    the extrapolation *is* the advance, uncapped, at the static/tapped BPM."""

    #: MIDI clock resolution — 24 pulses per quarter note, the fixed standard.
    PPQN = 24

    def __init__(
        self,
        *,
        bpm: float = 120.0,
        beats_per_bar: int = 4,
        source: str = "internal",
        now: float | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._beats_per_bar = max(1, int(beats_per_bar))
        self._source = source
        self._bpm = float(bpm)
        t = now if now is not None else time.monotonic()
        # phase(now) = _phase_base + advance-since-_base_time (see class docstring).
        self._phase_base = 0.0
        self._base_time = t
        # Internal mode free-runs immediately; MIDI mode waits for the first
        # clock/start/continue byte to start advancing.
        self._running = source != "midi"
        # Flips True once any external transport/clock byte arrives; gates the
        # per-pulse extrapolation cap (external is pulse-truthed, internal isn't).
        self._external = False
        self._last_pulse_time: float | None = None
        self._pulse_bpm: float | None = None
        self._tap_times: deque[float] = deque(maxlen=8)

    # ---- phase math (caller holds _lock) -----------------------------------
    def _phase_at_locked(self, now: float) -> float:
        if not self._running:
            return self._phase_base
        advance = max(0.0, now - self._base_time) * self._bpm / 60.0
        if self._external:
            # Between pulses, never overrun the next pulse's 1/PPQN increment —
            # keeps the phase from running ahead of a late clock byte and then
            # jerking backwards when it lands.
            advance = min(advance, 1.0 / self.PPQN)
        return self._phase_base + advance

    # ---- external clock feed (MIDI reader thread) --------------------------
    def clock_pulse(self, now: float) -> None:
        """A ``0xF8`` MIDI clock byte (24 per quarter note). Advances the grid by
        exactly 1/24 beat and refreshes the BPM estimate from the inter-pulse
        interval. A free-running clock with no preceding Start still starts the
        grid on its first pulse."""
        with self._lock:
            self._external = True
            first = self._last_pulse_time is None
            self._running = True
            if not first:
                self._phase_base += 1.0 / self.PPQN
                dt = now - self._last_pulse_time  # type: ignore[operator]
                if dt > 1e-6:
                    inst = 60.0 / (self.PPQN * dt)
                    if _BPM_MIN <= inst <= _BPM_MAX:
                        if self._pulse_bpm is None:
                            self._pulse_bpm = inst
                        else:
                            self._pulse_bpm = (
                                1.0 - _PULSE_EMA_ALPHA
                            ) * self._pulse_bpm + _PULSE_EMA_ALPHA * inst
                        self._bpm = self._pulse_bpm
            self._base_time = now
            self._last_pulse_time = now

    def start(self, now: float) -> None:
        """A ``0xFA`` Start: reset to the top of bar 1, beat 0, and run."""
        with self._lock:
            self._external = True
            self._running = True
            self._phase_base = 0.0
            self._base_time = now
            self._last_pulse_time = None

    def continue_(self, now: float) -> None:
        """A ``0xFB`` Continue: resume from the current phase (or wherever a
        preceding ``0xF2`` song-position parked it)."""
        with self._lock:
            self._external = True
            self._running = True
            self._base_time = now
            self._last_pulse_time = None

    def stop(self, now: float) -> None:
        """A ``0xFC`` Stop: freeze the phase where it is and hold."""
        with self._lock:
            self._phase_base = self._phase_at_locked(now)
            self._base_time = now
            self._running = False
            self._last_pulse_time = None

    def song_position(self, sixteenths: int, now: float) -> None:
        """A ``0xF2`` Song Position Pointer. Its value is in MIDI beats (a MIDI
        beat = 6 clock pulses = one sixteenth note), so ``beat_phase`` (quarter
        notes) is ``sixteenths / 4`` — e.g. SPP 16 == 4 beats == one bar of 4/4.
        Sets the parked phase without changing the run state (a DAW sends SPP
        while stopped, then Continue)."""
        with self._lock:
            self._external = True
            self._phase_base = max(0, int(sixteenths)) / 4.0
            self._base_time = now
            self._last_pulse_time = None

    # ---- internal tap tempo (MIDI reader thread) ---------------------------
    def tap(self, now: float) -> None:
        """Register a tap-tempo hit. Averages the recent inter-tap intervals into
        a live BPM, switches the grid to internal drive, and re-anchors the
        downbeat to this tap. The first tap of a group (or after a >2 s gap) just
        seeds the timing; two or more set the tempo."""
        with self._lock:
            if self._tap_times and (now - self._tap_times[-1]) > _TAP_RESET_S:
                self._tap_times.clear()
            self._tap_times.append(now)
            self._external = False
            self._source = "internal"
            if len(self._tap_times) >= 2:
                taps = list(self._tap_times)
                intervals = [b - a for a, b in zip(taps, taps[1:], strict=False)]
                avg = sum(intervals) / len(intervals)
                if avg > 0:
                    bpm = 60.0 / avg
                    if _BPM_MIN <= bpm <= _BPM_MAX:
                        self._bpm = bpm
            # Snap the phase to a whole beat at the tapped instant so taps land
            # on the beat grid (the downbeat re-anchors to the performer's hand).
            self._phase_base = float(round(self._phase_at_locked(now)))
            self._base_time = now
            self._running = True

    # ---- reads (consumer threads) ------------------------------------------
    def beat_phase_at(self, now: float | None = None) -> float:
        """Accumulated beats (quarter notes) at ``now`` — the running integral of
        bpm/60, monotonic while running. The quantity a tempo-locked cycle rate
        integrates against."""
        t = now if now is not None else time.monotonic()
        with self._lock:
            return self._phase_at_locked(t)

    def bar_phase_at(self, now: float | None = None) -> float:
        """Accumulated bars at ``now`` (``beat_phase / beats_per_bar``). A launch
        quantizer detects a bar boundary by watching ``floor(bar_phase)`` tick."""
        return self.beat_phase_at(now) / self._beats_per_bar

    @property
    def beat_phase(self) -> float:
        return self.beat_phase_at()

    @property
    def bar_phase(self) -> float:
        return self.bar_phase_at()

    @property
    def bpm(self) -> float:
        with self._lock:
            return self._bpm

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def beats_per_bar(self) -> int:
        return self._beats_per_bar

    @property
    def source(self) -> str:
        with self._lock:
            return self._source

    # ---- mido bridge -------------------------------------------------------
    def feed_message(self, msg: Any, now: float | None = None) -> bool:
        """Feed a mido message; return True if it was a clock/transport/SPP
        message this clock consumed (so the caller can skip further dispatch).
        Duck-typed on ``msg.type`` / ``msg.pos`` so this module never imports
        mido."""
        mt = getattr(msg, "type", None)
        if mt not in ("clock", "start", "continue", "stop", "songpos"):
            return False
        t = now if now is not None else time.monotonic()
        if mt == "clock":
            self.clock_pulse(t)
        elif mt == "start":
            self.start(t)
        elif mt == "continue":
            self.continue_(t)
        elif mt == "stop":
            self.stop(t)
        else:  # songpos
            self.song_position(int(getattr(msg, "pos", 0)), t)
        return True


class ClockModulationSource:
    """Wraps a :class:`TempoClock` as a :class:`MusicModulation` feeder, so a
    reactive effect/generator can be driven by the MIDI beat grid exactly as it
    is by SID audio (the ``mod_source = "clock"`` selector, Phases 3/6).

    `beat_phase` and `bpm` come straight from the clock. Since there is no audio
    signal, `level`/`onset` are synthesized from a beat-pulse envelope: `onset`
    spikes to 1.0 on each beat and decays over the opening fraction of the beat,
    and `level` rides a gentle floor plus that pulse. The mapping is a pure
    function of the phase (no hidden state), so a byte-stable render stays
    deterministic. Voices report silent (the clock carries no per-voice data)."""

    #: Beats over which the per-beat onset envelope decays back to 0.
    _ONSET_WINDOW_BEATS = 0.25

    def __init__(self, clock: TempoClock) -> None:
        self._clock = clock

    def features(self, now: float | None = None) -> MusicModulation:
        t = now if now is not None else time.monotonic()
        phase = self._clock.beat_phase_at(t)
        bpm = self._clock.bpm
        silent = (0.0, 0.0, 0.0)
        gates = (False, False, False)
        if not self._clock.running:
            return MusicModulation(
                level=0.0,
                onset=0.0,
                beat_phase=phase,
                bpm=bpm,
                voice_freqs=silent,
                voice_gates=gates,
            )
        frac = phase - math.floor(phase)
        onset = max(0.0, 1.0 - frac / self._ONSET_WINDOW_BEATS)
        level = 0.5 + 0.5 * onset
        return MusicModulation(
            level=level,
            onset=onset,
            beat_phase=phase,
            bpm=bpm,
            voice_freqs=silent,
            voice_gates=gates,
        )


def build_tempo_clock(performance: PerformanceCfg | None = None) -> TempoClock:
    """Build a :class:`TempoClock` from a ``[performance]`` config section (or a
    120-BPM 4/4 internal default when there's none). Duck-typed on the cfg's
    ``tempo_source``/``bpm``/``beats_per_bar`` so tempo.py needn't import
    config.py."""
    if performance is None:
        return TempoClock()
    return TempoClock(
        bpm=performance.bpm,
        beats_per_bar=performance.beats_per_bar,
        source=performance.tempo_source,
    )
