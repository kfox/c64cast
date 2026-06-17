"""Music-feature struct that drives reactive generative visuals.

The "music-reactive" building block, parallel to FrameSource / AudioSource: a
small, generator-agnostic snapshot of what the music is doing right now, which a
GenerativeSource reads to scale its own parameters. This decouples *what reacts*
(the generator) from *how the features are measured* (the audio source) — today
a SID host-emulator (music_features.SidFeatureStream), tomorrow an audio tap or
a MIDI event stream, all behind this same struct.

Deliberately tiny and dependency-free (stdlib only) so both the generators
(numpy/cv2) and the SID feature stream (py65) can import it without pulling each
other's heavy deps in.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    """

    level: float
    onset: float
    beat_phase: float
    bpm: float
    voice_freqs: tuple[float, float, float]
    voice_gates: tuple[bool, bool, bool]
