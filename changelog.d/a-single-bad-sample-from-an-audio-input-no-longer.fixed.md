- **A single bad sample from an audio input no longer silences the rest of
  the scene.** If a capture driver delivered one invalid (NaN or infinite)
  sample, or one so large it overflowed, the DSP stages held on to it and the 4-bit DAC output stayed stuck
  until the next scene. Invalid samples are now treated as silence (or full
  scale, for an infinite one) when they arrive.
