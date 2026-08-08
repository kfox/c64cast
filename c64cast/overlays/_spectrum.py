"""Shared band-magnitude source for the spectrum-analyzer overlays.

Both spectrum overlays (`spectrum_petscii`, `spectrum_bitmap`) answer the same
question every frame — "how much energy is in each of N log-spaced bands right
now?" — and differ only in how they draw the answer. That question is answered
here, once, by `_SpectrumBands.bands_now(scene)`.

**Why the scene, not the AudioStreamer.** The original implementation FFT'd
`AudioStreamer.get_recent_samples()` inside the overlay, which made it
`REQUIRES_AUDIO` and therefore blank on a SID/waveform scene (the chip makes the
sound; there is no streamer) while duplicating analysis on a mic/file scene (the
`audio_features.AudioFeatureStream` behind those already ran the identical FFT).
`scene.features()` is the source-agnostic seam — every reactive scene returns a
`modulation.MusicModulation` there — so the overlays read that first and keep the
FFT only as a fallback.

Four tiers, in precedence order:

1. **`features().bands`** — a real spectrum, already Hann → rfft → per-band mean
   → `log1p`-compressed by the upstream analyzer over the *pre-DSP* tap. The
   mic and audio-file paths land here. Rebinned to this overlay's band count.
2. **Voice synthesis** — the SID path (`music_features.SidFeatureStream`,
   `waveform.WaveformScene.features`) reports envelopes and oscillator
   frequencies, *not* a spectrum, so its `bands` is empty by design (see
   modulation.py). Rather than leave a SID tune blank, place each gated voice's
   real frequency into a log-spaced Hz band. This is a three-oscillator
   approximation of a spectrum, not an FFT of one — which for a chiptune is
   most of what the spectrum actually is. It lives here rather than in the SID
   feature producers deliberately: filling `MusicModulation.bands` upstream
   would make `bass`/`mid`/`treble` non-zero on the SID path and change every
   existing SID-reactive generator (see the byte-identical note in
   modulation.py).
3. **The legacy FFT** — `features()` is None but an `AudioStreamer` is attached
   (a plain `reactive = false` mic scene, or a webcam scene with audio on).
   Math unchanged from the pre-features implementation.
4. **Zeros** — no data source. The overlays paint nothing rather than erroring,
   so a spectrum overlay on a silent scene is inert, not fatal.

Magnitudes come back nominally in [0, 1] with `gain` already applied; a caller
that maps them to a pixel/row height does its own clipping.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

import numpy as np

from c64cast.audio.audio_features import FFT_SIZE, WINDOW, band_edges

from ..palette import C64_COLORS

if TYPE_CHECKING:
    from ..modulation import MusicModulation
    from ..scenes import Scene

log = logging.getLogger(__name__)

N_BANDS = 8

# Lowest → highest frequency band color, shared by both spectrum overlays so a
# given band is the same color whether it's drawn as chars or as bitmap pixels.
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

# Frequency span the SID voice-synthesis tier maps across its bands. The FFT
# tiers get their edges from the analyzer's bin geometry (band_edges), which is
# tied to a sample rate; a SID voice arrives as an absolute frequency in Hz, so
# it needs its own span. 40 Hz–8 kHz covers the SID's musical range with the low
# end sitting just under a typical bass line's fundamental.
VOICE_BAND_LO_HZ = 40.0
VOICE_BAND_HI_HZ = 8000.0

# How much of a voice's level spills into the two adjacent bands. With only
# three oscillators across eight bands, hard single-band spikes read as
# disconnected blips; a modest skirt makes them read as a spectrum without
# implying resolution that isn't there.
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
    # Map both onto [0, 1] so the first and last bands stay pinned to the
    # spectrum's ends regardless of the count change.
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
    envelopes), not per-voice envelopes, so every lit bar shares a height and
    the motion the eye reads is the *frequency* motion — the bass holding low
    while the lead walks up the bands."""
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

    # ---- the one data source ------------------------------------------------

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

        Deliberately the *post*-DSP tap (unlike `audio_features`' analysis
        sink): with no upstream analyzer to defer to, this is a scope on what
        the C64 is actually playing. Math is unchanged from before the features
        tiers existed."""
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
        # Normalize: log-compress so loud signals don't dwarf quiet ones.
        # FFT magnitudes scale with FFT_SIZE; divide first.
        mags = mags / (FFT_SIZE * 0.5)
        return np.log1p(mags * 100.0 * self.gain)

    def _warn_no_source_once(self, scene: Scene | None) -> None:
        """Say so — once — when neither tier can supply data. The overlay used
        to be refused at build time by `REQUIRES_AUDIO`; now that it's valid on
        scenes with no streamer, a silent no-op would otherwise look like a
        rendering bug."""
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
