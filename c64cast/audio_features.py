"""Audio-input music-feature stream for reactive generative visuals.

The second producer of [MusicModulation](modulation.py), alongside
[music_features.SidFeatureStream](music_features.py). Where the SID stream reads
envelope/gate/frequency out of a host-side 6502 emulator running the same tune
the chip is playing, this one analyzes *actual audio samples* — a live input
(an iRig, a mixer feed, a mic) or, later, a decoded audio file — so a generative
scene can react to music c64cast has no symbolic knowledge of.

Everything downstream of `MusicModulation` is already source-agnostic
(generators.py, the effects chain, wled_sync.py), so this module is the whole
feature: an analyzer plus the poll thread that feeds it.

**Why a separate pre-DSP tap.** `AudioStreamer.get_recent_samples()` already
exposes a 2048-sample mono float ring — the one `overlays/spectrum_petscii.py`
FFTs. But it is filled inside `_encode_and_enqueue`, *after* `_apply_dsp`, and
`[dsp].enabled` defaults **True**: AGC + compressor + limiter sit ahead of it on
the mic path. Those stages exist precisely to flatten dynamics for the 4-bit
DAC, which is exactly the information an onset detector needs — a compressed
kick barely moves the spectral flux. So `AudioStreamer.analysis_sink` taps
earlier (right after the mono downmix, before the gate and the DSP chain) and
feeds an `AnalysisTap` here. The spectrum overlay's tap is deliberately left
alone: it wants to visualize what the C64 is actually playing.

Thread model mirrors `SidFeatureStream`: a `PollThread` owns the analyzer and
updates a snapshot under `_lock`; `features()` reads that snapshot under the
same lock, so the render thread always sees a consistent set. The *writer* into
the tap is a realtime sounddevice callback, so `AnalysisTap.push` does nothing
but a couple of slice assignments under its own short-lived lock.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

from ._pollthread import PollThread
from .modulation import MusicModulation, TempoEstimator

log = logging.getLogger(__name__)

# Default analysis window. 1024 samples is ~85 ms at the 12 kHz DAC rate and
# ~23 ms at 44.1 kHz — short enough for transient timing, long enough that the
# lowest log-spaced band still holds a few bins.
FFT_SIZE = 1024
N_BANDS = 8

# Hann windows are keyed by size and reused — the analyzer builds one per
# stream, the spectrum overlay one for the default size, and neither should
# re-allocate per frame.
_WINDOWS: dict[int, np.ndarray] = {}


def hann_window(fft_size: int) -> np.ndarray:
    """Return the (cached, read-only) Hann window for `fft_size`."""
    w = _WINDOWS.get(fft_size)
    if w is None:
        w = np.hanning(fft_size).astype(np.float32)
        w.setflags(write=False)
        _WINDOWS[fft_size] = w
    return w


# The default-size window, kept as a module constant for the spectrum overlay,
# which has always FFT'd at exactly FFT_SIZE.
WINDOW = hann_window(FFT_SIZE)


def band_edges(n_bands: int, fft_size: int) -> np.ndarray:
    """Return n_bands+1 bin indices (inclusive ranges) for log-spaced bands.

    rfft yields fft_size//2 + 1 bins. We skip bin 0 (DC) and pick log-spaced
    edges through bin (fft_size//2). Shared with `overlays/spectrum_petscii.py`
    so there is exactly one band-edge definition — the overlay's bars and the
    analyzer's `bands` describe the same frequency ranges."""
    n_bins = fft_size // 2
    # log spacing from bin 1 → bin n_bins
    edges = np.logspace(0, np.log10(n_bins), n_bands + 1)
    return np.clip(edges.astype(np.int32), 1, n_bins)


class AnalysisTap:
    """A small lock-protected mono float ring the audio path pushes into and the
    feature thread reads windows out of.

    Same wrap arithmetic as `AudioStreamer._push_to_tap` / `get_recent_samples`,
    lifted rather than shared because the writer here is a realtime callback on
    a streamer that may not exist yet (the tap outlives any single `start_mic`).
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
            # Source block is larger than the ring — keep the tail only.
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
    """Turn successive sample windows into a `MusicModulation`. Pure numpy — no
    threads, no hardware, no I/O — so the whole feature math is unit-testable by
    calling `update()` with synthetic signals.

    Per `update(window, now)`:

    * **level** — block RMS through a one-pole attack/release follower,
      normalized against a slowly-decaying rolling peak so a quiet source still
      reaches full scale. Per-*block*, deliberately: `dsp._ar_envelope` is a
      per-sample Python loop, which is the wrong tool at 60 blocks/sec.
    * **bands** — Hann → rfft → mean magnitude per log-spaced band → `log1p`
      compression (the same curve `spectrum_petscii` has always drawn).
    * **onset** — spectral flux (sum of positive per-band deltas) against an
      adaptive threshold (median of ~1 s of flux history), latched to 1.0 on a
      crossing and otherwise decayed on the same τ as the SID path, so a pulse
      looks identical after 16-color quantization.
    * **bpm / beat_phase** — the shared `TempoEstimator`, fed by those onsets.
    """

    # Onset envelope time constant, matched to SidFeatureStream._ONSET_TAU_S so
    # both producers pulse identically.
    _ONSET_TAU_S = 0.18

    # Level follower. Fast attack so a transient is on screen the frame it
    # happens; slow release so the brightness breathes rather than flickers.
    _ATTACK_S = 0.010
    _RELEASE_S = 0.150
    # Rolling peak for normalization: decays toward _PEAK_FLOOR so a quiet
    # source climbs to full scale within a couple of seconds, while true silence
    # reads as level ≈ 0 rather than being amplified into noise.
    _PEAK_DECAY_S = 2.0
    _PEAK_FLOOR = 0.02

    # Onset detection. The adaptive threshold is the running median of the flux
    # history times _THRESH_MULT, plus an absolute floor so a silent input can't
    # trip on numerical dust (median ≈ 0 would otherwise make any flux a
    # "crossing"). Both are divided by onset_sensitivity: higher ⇒ more onsets.
    _THRESH_MULT = 1.6
    _FLUX_FLOOR = 0.15
    _FLUX_HISTORY_S = 1.0
    # Below this normalized level nothing counts as an onset at all — the guard
    # that keeps a silent room from generating a phantom tempo.
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

        # History length in blocks (~_FLUX_HISTORY_S of flux values).
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

    # ---- per-block analysis -------------------------------------------------

    def update(self, window: np.ndarray, now: float) -> None:
        """Fold one analysis window (mono float, `fft_size` samples) into the
        feature accumulators. `now` is a monotonic timestamp in seconds; the
        elapsed time since the previous call sets every decay rate, so a
        stuttering poll thread degrades smoothly instead of changing the feel."""
        if window.size < self.fft_size:
            return
        dt = self._nominal_dt if self._last_now is None else now - self._last_now
        # Clamp: a scheduler stall must not blow the envelopes away, and a
        # duplicate timestamp must not divide by zero.
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
        # Peak follows instantly upward, decays exponentially toward the floor.
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
        # FFT magnitudes scale with the transform size; normalize first, then
        # log-compress so loud content doesn't dwarf quiet content. Same curve
        # as spectrum_petscii._band_magnitudes.
        mags = mags / (self.fft_size * 0.5)
        log_mags = np.log1p(mags * 100.0)
        self._bands = np.clip(log_mags, 0.0, 1.0)
        return log_mags

    def _update_onset(self, log_mags: np.ndarray, dt: float, now: float) -> None:
        """Spectral flux vs an adaptive threshold; latch or decay `onset`."""
        prev = self._prev_log_mags
        self._prev_log_mags = log_mags
        # Decay first so a fresh onset reads as a clean 1.0 (mirrors
        # SidFeatureStream._process_tick).
        self._onset *= math.exp(-dt / self._ONSET_TAU_S)
        if prev is None:
            return
        flux = float(np.sum(np.maximum(log_mags - prev, 0.0)))
        history = self._flux_history
        history.append(flux)
        if len(history) > self._flux_history_len:
            del history[0]
        if self.level < self._SILENCE_LEVEL:
            return  # silence: no threshold is meaningful, and no phantom tempo
        median = float(np.median(history))
        threshold = (median * self._THRESH_MULT + self._FLUX_FLOOR) / self.onset_sensitivity
        if flux > threshold:
            self._onset = 1.0
            self._tempo.note_onset(now)

    # ---- feature snapshot ---------------------------------------------------

    @property
    def level(self) -> float:
        """Peak-normalized loudness in [0, 1]."""
        return float(min(1.0, self._level_env / max(self._peak, self._PEAK_FLOOR)))

    def snapshot(self) -> MusicModulation:
        """Build the current `MusicModulation`.

        `voice_freqs` / `voice_gates` are zero/False: those are SID-specific and
        have no audio-input analogue, so the handful of generators that read them
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

    # ---- lifecycle ----------------------------------------------------------

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

    # ---- poll thread --------------------------------------------------------

    def _process_tick(self) -> None:
        """Analyze the most recent window. Split out of the thread body so tests
        drive it directly with a hand-filled tap (mirrors
        SidFeatureStream._process_tick). The FFT runs outside the lock; only the
        snapshot swap takes it."""
        window = self._tap.recent(self._fft_size)
        now = time.monotonic()
        with self._lock:
            self._analyzer.update(window, now)
            self._snapshot = self._analyzer.snapshot()

    # ---- feature snapshot ---------------------------------------------------

    def features(self) -> MusicModulation | None:
        """Return the current snapshot, or None before the first tick."""
        with self._lock:
            return self._snapshot
