"""Source-timeline alignment marker for capture-card recordings.

See docs/architecture/audio.md#audio_markerpy--the-capture-alignment-marker.
"""

from __future__ import annotations

import numpy as np

from c64cast.audio.audio_handlers import encode_floats_to_dac

MARKER_DURATION_S = 0.1
MARKER_FREQ_START_HZ = 200.0
MARKER_FREQ_END_HZ = 3500.0

DEFAULT_CAPTURE_RATE = 48000
DEFAULT_PLAYBACK_RATE = 8000


def _chirp_float(sample_rate: int) -> np.ndarray:
    """Float64 chirp samples in ±1.0, sweeping MARKER_FREQ_START_HZ to
    MARKER_FREQ_END_HZ over MARKER_DURATION_S."""
    n = int(MARKER_DURATION_S * sample_rate)
    t = np.arange(n) / sample_rate
    k = (MARKER_FREQ_END_HZ - MARKER_FREQ_START_HZ) / MARKER_DURATION_S
    phase = 2 * np.pi * (MARKER_FREQ_START_HZ * t + 0.5 * k * t * t)
    return np.sin(phase)


def synthesize_marker(
    sample_rate: int = DEFAULT_PLAYBACK_RATE, curve: np.ndarray | None = None
) -> bytes:
    """The marker as ``$D418`` bytes, one per sample, through the same
    ``encode_floats_to_dac`` the track itself goes through. ``curve`` is the
    streamer's ``dac_curve``: under a Mahoney curve the track holds full
    ``$D418`` bytes, so a bare 0-15 volume code would play the marker at the
    wrong level and without the filter bits. Undithered, so the bytes are
    reproducible."""
    return encode_floats_to_dac(_chirp_float(sample_rate), dither=False, curve=curve).tobytes()


def synthesize_marker_4bit(sample_rate: int = DEFAULT_PLAYBACK_RATE) -> bytes:
    """4-bit SID DAC volume codes (0-15, one per byte) for the marker: the
    linear path of ``synthesize_marker``."""
    return synthesize_marker(sample_rate)


def marker_duration_samples(sample_rate: int = DEFAULT_PLAYBACK_RATE) -> int:
    """Number of 4-bit bytes the marker occupies at the given rate."""
    return int(MARKER_DURATION_S * sample_rate)


def synthesize_capture_reference(
    capture_rate: int = DEFAULT_CAPTURE_RATE,
    playback_rate: int = DEFAULT_PLAYBACK_RATE,
) -> np.ndarray:
    """Reference waveform AS IT WOULD APPEAR at the capture device, ready
    to cross-correlate against a captured WAV.

    Models the path 4-bit code → SID volume nibble → capture sampling:
    a staircase holding each code for ``capture_rate / playback_rate``
    output samples. Amplitude scale is approximate."""
    vol_bytes = synthesize_marker_4bit(playback_rate)
    vol = np.frombuffer(vol_bytes, dtype=np.uint8).astype(np.float32)
    centered = vol - 7.5
    # The true ratio, not its floor: at the 12032 Hz NTSC rate a 48 kHz capture
    # holds each code 3.99 samples, and flooring to 3 shortens the reference
    # by a quarter and moves the correlation peak.
    n_out = int(round(len(centered) * capture_rate / playback_rate))
    idx = np.minimum((np.arange(n_out) * playback_rate) // capture_rate, len(centered) - 1)
    return centered[idx]


def find_marker_in_capture(
    capture: np.ndarray,
    capture_rate: int = DEFAULT_CAPTURE_RATE,
    playback_rate: int = DEFAULT_PLAYBACK_RATE,
) -> int:
    """Return the capture-sample index where the marker BEGINS.

    Source content (the actual video audio) starts MARKER_DURATION_S
    later; callers add ``int(MARKER_DURATION_S * capture_rate)`` to get
    the source-time-0 anchor.

    Cross-correlation via FFT, both signals mean-subtracted so the
    correlation is amplitude-invariant."""
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
