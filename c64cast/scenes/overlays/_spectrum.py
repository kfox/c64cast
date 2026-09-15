"""Shared band-magnitude source for the spectrum-analyzer overlays.

`_SpectrumBands.bands_now(scene)` answers "how much energy is in each of N
log-spaced bands right now?" once for both `spectrum_petscii` and
`spectrum_bitmap`, in four tiers: `scene.features().bands`, SID voice synthesis,
a direct FFT of an attached `AudioStreamer`, then zeros. Magnitudes come back
nominally in [0, 1] with `gain` already applied; a caller that maps them to a
pixel/row height does its own clipping.

See docs/architecture/scenes.md#the-shared-spectrum-band-source-overlays_spectrumpy.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

import numpy as np

from c64cast.audio.audio_features import FFT_SIZE, WINDOW, band_edges
from c64cast.video.palette import C64_COLORS

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation
    from c64cast.scenes.scenes import Scene

log = logging.getLogger(__name__)

N_BANDS = 8

# Shared by both spectrum overlays, so a band is the same color whether it is
# drawn as chars or as bitmap pixels.
BAND_COLORS = np.array(
    [
        C64_COLORS["red"],  # band 0 — lowest
        C64_COLORS["orange"],
        C64_COLORS["yellow"],
        C64_COLORS["light green"],
        C64_COLORS["cyan"],
        C64_COLORS["light blue"],
        C64_COLORS["purple"],
        C64_COLORS["light red"],  # band 7 — highest
    ],
    dtype=np.uint8,
)

# The voice-synthesis tier needs its own span: the FFT tiers take their edges
# from the analyzer's bin geometry, which is tied to a sample rate, while a SID
# voice arrives as an absolute frequency in Hz. 40 Hz–8 kHz covers the SID's
# musical range, the low end just under a typical bass line's fundamental.
VOICE_BAND_LO_HZ = 40.0
VOICE_BAND_HI_HZ = 8000.0

# How much of a voice's level spills into the two adjacent bands: with three
# oscillators across eight bands, hard single-band spikes read as disconnected
# blips.
_VOICE_SPILL = 0.45


def rebin(bands: tuple[float, ...] | np.ndarray, n_out: int) -> np.ndarray:
    """Resample `bands` to `n_out` values by linear interpolation over band
    index. Both sides are log-spaced energies, so index-space interpolation is
    the right geometry — no Hz conversion needed. Identity (a copy) when the
    counts already match."""
    src = np.asarray(bands, dtype=np.float32)
    if src.size == n_out:
        return src.copy()
    if src.size == 0:
        return np.zeros(n_out, dtype=np.float32)
    if src.size == 1:
        return np.full(n_out, src[0], dtype=np.float32)
    src_x = np.linspace(0.0, 1.0, src.size, dtype=np.float32)
    out_x = np.linspace(0.0, 1.0, n_out, dtype=np.float32)
    return np.interp(out_x, src_x, src).astype(np.float32)


def voice_bands(feat: MusicModulation, n_out: int) -> np.ndarray:
    """Synthesize `n_out` band magnitudes from a SID feature snapshot.

    Each *gated* voice deposits the snapshot's `level` into the band its
    oscillator frequency falls in (log-spaced over VOICE_BAND_LO/HI_HZ), with a
    `_VOICE_SPILL` skirt either side. Bands combine by max, not sum, so two
    voices in the same band don't read as twice the energy. An ungated or
    zero-frequency voice contributes nothing.

    `MusicModulation` carries only an aggregate `level` (the mean of the voice
    envelopes), not per-voice envelopes, so every lit bar shares a height."""
    out = np.zeros(n_out, dtype=np.float32)
    level = float(feat.level)
    if level <= 0.0:
        return out
    span = math.log(VOICE_BAND_HI_HZ / VOICE_BAND_LO_HZ)
    for freq, gated in zip(feat.voice_freqs, feat.voice_gates, strict=False):
        if not gated or freq <= VOICE_BAND_LO_HZ:
            continue
        pos = math.log(min(freq, VOICE_BAND_HI_HZ) / VOICE_BAND_LO_HZ) / span
        idx = min(n_out - 1, max(0, int(pos * n_out)))
        out[idx] = max(out[idx], level)
        spill = level * _VOICE_SPILL
        if idx > 0:
            out[idx - 1] = max(out[idx - 1], spill)
        if idx + 1 < n_out:
            out[idx + 1] = max(out[idx + 1], spill)
    return out


class _SpectrumBands:
    """Mixin supplying `bands_now()` to the spectrum overlays.

    Expects the host overlay to define `n_bands` (int), `gain` (float) and
    `audio` (the shared `AudioStreamer`, or None — `WANTS_AUDIO` on the overlay
    class is what makes `build_overlay` inject it). Sets up the FFT band edges
    in `_init_bands`, which the host must call from its `__init__`."""

    n_bands: int
    gain: float
    audio: Any

    _edges: np.ndarray
    _warned_no_source: bool

    def _init_bands(self) -> None:
        self._edges = band_edges(self.n_bands, FFT_SIZE)
        self._warned_no_source = False

    def bands_now(self, scene: Scene | None) -> np.ndarray:
        """Band magnitudes for this frame, nominally in [0, 1], `gain` applied.
        See the module docstring for the four-tier precedence."""
        feat = scene.features() if scene is not None else None
        if feat is not None:
            if feat.bands:
                return rebin(feat.bands, self.n_bands) * self.gain
            if any(feat.voice_gates):
                return voice_bands(feat, self.n_bands) * self.gain
        if self.audio is not None:
            return self._fft_bands()
        self._warn_no_source_once(scene)
        return np.zeros(self.n_bands, dtype=np.float32)

    def _fft_bands(self) -> np.ndarray:
        """The pre-features path: FFT the streamer's post-DSP sample tap.

        This is the *post*-DSP tap, unlike `audio_features`' analysis sink:
        with no upstream analyzer to defer to, it is a scope on what the C64 is
        actually playing."""
        samples = self.audio.get_recent_samples(FFT_SIZE)
        if samples.size < FFT_SIZE:
            return np.zeros(self.n_bands, dtype=np.float32)
        spec = np.abs(np.fft.rfft(samples * WINDOW))
        mags = np.zeros(self.n_bands, dtype=np.float32)
        for i in range(self.n_bands):
            lo, hi = int(self._edges[i]), int(self._edges[i + 1])
            if hi <= lo:
                continue
            mags[i] = spec[lo:hi].mean()
        # FFT magnitudes scale with FFT_SIZE, so divide before compressing.
        mags = mags / (FFT_SIZE * 0.5)
        return np.log1p(mags * 100.0 * self.gain)

    def _warn_no_source_once(self, scene: Scene | None) -> None:
        """Say so — once — when no tier can supply data; on a scene with no
        streamer a silent no-op would otherwise look like a rendering bug."""
        if self._warned_no_source:
            return
        self._warned_no_source = True
        log.warning(
            "spectrum overlay on %r has no data source: the scene reports no "
            "music features and no audio input is attached — it will paint "
            "nothing. Enable [audio] (drop --no-audio) for a live-input "
            "spectrum, or attach it to a SID / reactive scene.",
            getattr(scene, "name", scene),
        )
