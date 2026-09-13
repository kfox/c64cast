"""Audio-input music-feature stream for reactive generative visuals.

An :class:`AnalysisTap` the audio path pushes mono floats into, an
:class:`AudioFeatureAnalyzer` that turns windows of them into a
`MusicModulation`, and the :class:`AudioFeatureStream` poll thread between
them.

See docs/architecture/audio.md#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

from c64cast._pollthread import PollThread
from c64cast.scenes.modulation import MusicModulation, TempoEstimator

log = logging.getLogger(__name__)

FFT_SIZE = 1024
N_BANDS = 8

_WINDOWS: dict[int, np.ndarray] = {}


def hann_window(fft_size: int) -> np.ndarray:
    """Return the (cached, read-only) Hann window for `fft_size`."""
    w = _WINDOWS.get(fft_size)
    if w is None:
        w = np.hanning(fft_size).astype(np.float32)
        w.setflags(write=False)
        _WINDOWS[fft_size] = w
    return w


# Imported by overlays/_spectrum.py, which FFTs at exactly FFT_SIZE.
WINDOW = hann_window(FFT_SIZE)


def band_edges(n_bands: int, fft_size: int) -> np.ndarray:
    """Return n_bands+1 bin indices (inclusive ranges) for log-spaced bands.

    rfft yields fft_size//2 + 1 bins. We skip bin 0 (DC) and pick log-spaced
    edges through bin (fft_size//2). Shared with `overlays/spectrum_petscii.py`
    so there is exactly one band-edge definition — the overlay's bars and the
    analyzer's `bands` describe the same frequency ranges."""
    n_bins = fft_size // 2
    edges = np.logspace(0, np.log10(n_bins), n_bands + 1)
    return np.clip(edges.astype(np.int32), 1, n_bins)


class AnalysisTap:
    """A small lock-protected mono float ring the audio path pushes into and the
    feature thread reads windows out of.

    Same wrap arithmetic as `AudioStreamer._push_to_tap` / `get_recent_samples`;
    the tap outlives any single `start_mic`.
    """

    def __init__(self, size: int = FFT_SIZE * 4):
        if size < 1:
            raise ValueError("AnalysisTap: size must be positive")
        self.size = int(size)
        self._buf = np.zeros(self.size, dtype=np.float32)
        self._write = 0
        self._lock = threading.Lock()

    def push(self, mono: np.ndarray) -> None:
        """Append mono float samples (nominally [-1, 1]). Called from the audio
        callback — keep it to slice assignments."""
        n = mono.size
        if n == 0:
            return
        if n >= self.size:
            with self._lock:
                self._buf[:] = mono[-self.size :]
                self._write = 0
            return
        with self._lock:
            end = self._write + n
            if end <= self.size:
                self._buf[self._write : end] = mono
            else:
                split = self.size - self._write
                self._buf[self._write :] = mono[:split]
                self._buf[: end - self.size] = mono[split:]
            self._write = end % self.size

    def recent(self, n: int) -> np.ndarray:
        """Return the most recent `n` samples, oldest first, as a fresh copy
        (so the caller can't race the writer). `n` is clamped to the ring size;
        before enough audio has arrived the head reads as zeros."""
        n = min(int(n), self.size)
        out = np.empty(n, dtype=np.float32)
        with self._lock:
            start = (self._write - n) % self.size
            tail = self.size - start
            if n <= tail:
                out[:] = self._buf[start : start + n]
            else:
                out[:tail] = self._buf[start:]
                out[tail:] = self._buf[: n - tail]
        return out


class AudioFeatureAnalyzer:
    """Turn successive sample windows into a `MusicModulation`.

    Pure numpy — no threads, no hardware, no I/O — so the whole feature math is
    unit-testable by calling `update()` with synthetic signals. Each
    `update(window, now)` refreshes `level`, `bands`, `onset` and the shared
    `TempoEstimator`'s `bpm`/`beat_phase`.

    See docs/architecture/audio.md#the-analyzer.
    """

    # Matched to SidFeatureStream._ONSET_TAU_S so both producers pulse alike.
    _ONSET_TAU_S = 0.18

    _ATTACK_S = 0.010
    _RELEASE_S = 0.150
    _PEAK_DECAY_S = 2.0
    _PEAK_FLOOR = 0.02

    _THRESH_MULT = 1.6
    _FLUX_FLOOR = 0.15
    _FLUX_HISTORY_S = 1.0
    _SILENCE_LEVEL = 0.02

    def __init__(
        self,
        sample_rate: float,
        *,
        n_bands: int = N_BANDS,
        fft_size: int = FFT_SIZE,
        onset_sensitivity: float = 1.0,
        nominal_dt: float = 1.0 / 60.0,
    ):
        if n_bands < 1:
            raise ValueError("audio features: bands must be >= 1")
        if fft_size < 32:
            raise ValueError("audio features: fft_size must be >= 32")
        self.sample_rate = float(sample_rate)
        self.n_bands = int(n_bands)
        self.fft_size = int(fft_size)
        self.onset_sensitivity = max(1e-3, float(onset_sensitivity))
        self._nominal_dt = float(nominal_dt)

        self._window = hann_window(self.fft_size)
        self._edges = band_edges(self.n_bands, self.fft_size)
        self._tempo = TempoEstimator()

        self._flux_history_len = max(4, int(round(self._FLUX_HISTORY_S / max(nominal_dt, 1e-3))))
        self.reset()

    def reset(self) -> None:
        """Clear all accumulators so a restarted stream begins clean."""
        self._level_env = 0.0
        self._peak = self._PEAK_FLOOR
        self._onset = 0.0
        self._bands = np.zeros(self.n_bands, dtype=np.float32)
        self._prev_log_mags: np.ndarray | None = None
        self._flux_history: list[float] = []
        self._last_now: float | None = None
        self._tempo.reset()

    def update(self, window: np.ndarray, now: float) -> None:
        """Fold one analysis window (mono float, `fft_size` samples) into the
        feature accumulators. `now` is a monotonic timestamp in seconds; the
        elapsed time since the previous call sets every decay rate, so a
        stuttering poll thread degrades smoothly instead of changing the feel."""
        if window.size < self.fft_size:
            return
        dt = self._nominal_dt if self._last_now is None else now - self._last_now
        # Bounded so a scheduler stall can't flatten the envelopes, nor a
        # duplicate timestamp divide by zero.
        dt = min(max(dt, 1e-4), 1.0)
        self._last_now = now

        self._update_level(window, dt)
        log_mags = self._update_bands(window)
        self._update_onset(log_mags, dt, now)
        self._tempo.advance(dt)

    def _update_level(self, window: np.ndarray, dt: float) -> None:
        """Block RMS → attack/release follower → peak-normalized [0, 1]."""
        rms = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
        tau = self._ATTACK_S if rms > self._level_env else self._RELEASE_S
        coef = 1.0 - math.exp(-dt / tau)
        self._level_env += (rms - self._level_env) * coef
        decayed = self._PEAK_FLOOR + (self._peak - self._PEAK_FLOOR) * math.exp(
            -dt / self._PEAK_DECAY_S
        )
        self._peak = max(self._level_env, decayed, self._PEAK_FLOOR)

    def _update_bands(self, window: np.ndarray) -> np.ndarray:
        """FFT → per-band mean magnitude → log1p compression. Returns the
        *unclipped* log magnitudes (the flux detector wants the headroom above
        1.0 that a loud transient produces); `self._bands` gets the clipped
        [0, 1] view the consumers see."""
        spec = np.abs(np.fft.rfft(window * self._window))
        mags = np.zeros(self.n_bands, dtype=np.float32)
        for i in range(self.n_bands):
            lo, hi = int(self._edges[i]), int(self._edges[i + 1])
            if hi <= lo:
                continue
            mags[i] = spec[lo:hi].mean()
        # Same normalize + log1p curve as _spectrum._SpectrumBands._fft_bands.
        mags = mags / (self.fft_size * 0.5)
        log_mags = np.log1p(mags * 100.0)
        self._bands = np.clip(log_mags, 0.0, 1.0)
        return log_mags

    def _update_onset(self, log_mags: np.ndarray, dt: float, now: float) -> None:
        """Spectral flux vs an adaptive threshold; latch or decay `onset`."""
        prev = self._prev_log_mags
        self._prev_log_mags = log_mags
        # Decay before the latch below, so a fresh onset reads as a clean 1.0.
        self._onset *= math.exp(-dt / self._ONSET_TAU_S)
        if prev is None:
            return
        flux = float(np.sum(np.maximum(log_mags - prev, 0.0)))
        history = self._flux_history
        history.append(flux)
        if len(history) > self._flux_history_len:
            del history[0]
        if self.level < self._SILENCE_LEVEL:
            return
        median = float(np.median(history))
        threshold = (median * self._THRESH_MULT + self._FLUX_FLOOR) / self.onset_sensitivity
        if flux > threshold:
            self._onset = 1.0
            self._tempo.note_onset(now)

    @property
    def level(self) -> float:
        """Peak-normalized loudness in [0, 1]."""
        return float(min(1.0, self._level_env / max(self._peak, self._PEAK_FLOOR)))

    def snapshot(self) -> MusicModulation:
        """Build the current `MusicModulation`.

        `voice_freqs` / `voice_gates` are zero/False: those are SID-specific and
        have no audio-input analog, so the handful of generators that read them
        (moire, kaleidoscope) fall back to their base geometry and react through
        level / onset / beat_phase / bands like everything else."""
        return MusicModulation(
            level=self.level,
            onset=self._onset,
            beat_phase=self._tempo.beat_phase,
            bpm=self._tempo.bpm,
            voice_freqs=(0.0, 0.0, 0.0),
            voice_gates=(False, False, False),
            bands=tuple(float(b) for b in self._bands),
        )


class AudioFeatureStream:
    """Poll thread pulling windows out of an `AnalysisTap` into an
    `AudioFeatureAnalyzer`, exposing a live `MusicModulation` snapshot.

    Construct cheaply, `start()` to spin up the thread, `features()` from the
    render thread, `stop()` at teardown — the same lifecycle as
    `SidFeatureStream`, so `AudioSource` implementations treat the two alike.
    `features()` returns None before `start()`.
    """

    def __init__(
        self,
        tap: AnalysisTap,
        sample_rate: float,
        *,
        n_bands: int = N_BANDS,
        fft_size: int = FFT_SIZE,
        poll_hz: float = 60.0,
        onset_sensitivity: float = 1.0,
    ):
        self._tap = tap
        self._poll_hz = max(5.0, float(poll_hz))
        self._poll_dt = 1.0 / self._poll_hz
        self._fft_size = int(fft_size)
        self._analyzer = AudioFeatureAnalyzer(
            sample_rate,
            n_bands=n_bands,
            fft_size=fft_size,
            onset_sensitivity=onset_sensitivity,
            nominal_dt=self._poll_dt,
        )
        self._lock = threading.Lock()
        self._snapshot: MusicModulation | None = None
        self._poll: PollThread | None = None

    def start(self) -> None:
        """Start the poll thread. A second call while running is a no-op."""
        if self._poll is not None and self._poll.is_running():
            return
        with self._lock:
            self._analyzer.reset()
            self._snapshot = None
        self._poll = PollThread(
            self._process_tick, period=self._poll_dt, name="audio-features", run_first=True
        )
        self._poll.start()

    def stop(self) -> None:
        """Stop the poll thread. Pure host-side; no hardware I/O."""
        if self._poll is not None:
            self._poll.stop()
            self._poll = None

    def _process_tick(self) -> None:
        """Analyze the most recent window. The FFT runs outside the lock; only
        the snapshot swap takes it."""
        window = self._tap.recent(self._fft_size)
        now = time.monotonic()
        with self._lock:
            self._analyzer.update(window, now)
            self._snapshot = self._analyzer.snapshot()

    def features(self) -> MusicModulation | None:
        """Return the current snapshot, or None before the first tick."""
        with self._lock:
            return self._snapshot
