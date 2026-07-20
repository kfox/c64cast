"""Source-timeline alignment marker for Cam Link captures.

Captured audio is sample-accurate (48 kHz from the capture device) but
its time origin drifts vs the source video by an unknown offset — each
capture session starts recording at a different wall-clock moment than
the C64 starts playing. To compare captures against each other or
against the source, all captures need a shared anchor.

This module synthesizes a brief, content-unique marker waveform that:
  * Gets prepended to the encoded 4-bit audio before REU upload, so it
    plays through the EXACT same NMI → SID DAC → Cam Link pipeline as
    the real audio. Marker artifacts mirror real-audio artifacts.
  * Has a sharp autocorrelation peak (linear chirp), so cross-correlation
    against the capture finds it unambiguously.
  * Won't appear in natural video source material (no music or
    speech sweeps through 200 → 3500 Hz in 100 ms).

Usage at capture-analysis time:
    cap = read_wav("/tmp/u64_dayinlife_X.wav")  # int16, 48 kHz
    t0 = find_marker_in_capture(cap)             # capture sample at marker start
    src_t0 = t0 + MARKER_DURATION_SAMPLES_48K    # source content starts here
    # source time T plays at capture sample = src_t0 + T * 48000
"""

from __future__ import annotations

import numpy as np

# 100 ms linear chirp: long enough for clean correlation SNR even under
# capture noise, short enough that listeners barely notice the pre-roll
# blip at scene start. 200-3500 Hz fits inside the ~6 kHz Nyquist of the
# 12 kHz default sample rate with margin against aliasing.
MARKER_DURATION_S = 0.1
MARKER_FREQ_START_HZ = 200.0
MARKER_FREQ_END_HZ = 3500.0

# Capture rate used by Cam Link / sounddevice; the upsampling factor
# below depends on this.
DEFAULT_CAPTURE_RATE = 48000
# Default playback rate assumed by the marker synthesis/analysis helpers.
# AudioCfg.sample_rate now defaults to 12000; pass the real playback_rate to
# the builders below to match a capture made at the current rate.
DEFAULT_PLAYBACK_RATE = 8000


def _chirp_float(sample_rate: int) -> np.ndarray:
    """Float64 chirp samples in ±1.0. Linear sweep from start_hz to end_hz
    over MARKER_DURATION_S, instantaneous phase = ∫ 2π·f(t) dt."""
    n = int(MARKER_DURATION_S * sample_rate)
    t = np.arange(n) / sample_rate
    k = (MARKER_FREQ_END_HZ - MARKER_FREQ_START_HZ) / MARKER_DURATION_S
    phase = 2 * np.pi * (MARKER_FREQ_START_HZ * t + 0.5 * k * t * t)
    return np.sin(phase)


def synthesize_marker_4bit(sample_rate: int = DEFAULT_PLAYBACK_RATE) -> bytes:
    """4-bit SID DAC volume codes (0-15, one per byte) for the marker.
    Same encoding the rest of audio_4bit uses: float in [-1, 1] →
    (float + 1) * 7.5 → clip to [0, 15] → uint8."""
    floats = _chirp_float(sample_rate)
    vol = np.clip((floats + 1.0) * 7.5, 0, 15).astype(np.uint8)
    return vol.tobytes()


def marker_duration_samples(sample_rate: int = DEFAULT_PLAYBACK_RATE) -> int:
    """Number of 4-bit bytes the marker occupies at the given rate."""
    return int(MARKER_DURATION_S * sample_rate)


def synthesize_capture_reference(
    capture_rate: int = DEFAULT_CAPTURE_RATE,
    playback_rate: int = DEFAULT_PLAYBACK_RATE,
) -> np.ndarray:
    """Reference waveform AS IT WOULD APPEAR at the capture device, ready
    to cross-correlate against a captured WAV.

    Models the path: 4-bit code → SID volume nibble → DC offset on the
    SID output → Cam Link 48 kHz sampling. The capture sees a staircase
    where each 4-bit volume code is held for (capture_rate / playback_rate)
    output samples — sample-and-hold from the 6510 only writing $D418
    at the NMI rate. Amplitude scale is approximate; cross-
    correlation is amplitude-invariant after mean subtraction so the
    exact scale doesn't matter for peak detection."""
    vol_bytes = synthesize_marker_4bit(playback_rate)
    vol = np.frombuffer(vol_bytes, dtype=np.uint8).astype(np.float32)
    # Center the 4-bit codes around 0: values 0..15 → -7.5..+7.5 ×
    # arbitrary amplitude. Real capture amplitude depends on input gain;
    # mean subtraction in find_marker_in_capture neutralizes it.
    centered = vol - 7.5
    factor = capture_rate // playback_rate
    upsampled = np.repeat(centered, factor)
    return upsampled


def find_marker_in_capture(
    capture: np.ndarray,
    capture_rate: int = DEFAULT_CAPTURE_RATE,
    playback_rate: int = DEFAULT_PLAYBACK_RATE,
) -> int:
    """Return the capture-sample index where the marker BEGINS.

    Source content (the actual video audio) starts MARKER_DURATION_S
    later; callers add ``int(MARKER_DURATION_S * capture_rate)`` to get
    the source-time-0 anchor.

    Cross-correlation via FFT. Both signals are mean-subtracted so the
    correlation is amplitude-invariant. Peak position in the time-domain
    correlation = template start in the signal."""
    ref = synthesize_capture_reference(capture_rate, playback_rate)
    cap = capture.astype(np.float64)
    cap = cap - cap.mean()
    refd = ref.astype(np.float64)
    refd = refd - refd.mean()
    n = len(cap) + len(refd) - 1
    nfft = 1 << (n - 1).bit_length()
    cap_fft = np.fft.rfft(cap, nfft)
    ref_fft = np.fft.rfft(refd, nfft)
    corr = np.fft.irfft(cap_fft * np.conj(ref_fft))[: len(cap)]
    return int(np.argmax(corr))
