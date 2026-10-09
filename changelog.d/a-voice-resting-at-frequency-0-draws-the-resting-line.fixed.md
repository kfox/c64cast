- **A voice resting at frequency 0 draws the resting line, not a flat line
  pinned near the top of its strip.** With the waveform bits still selected and
  the envelope still open, a zero frequency froze the phase accumulator and
  every sample took the same value. The scope's own time-base picker already
  counted that case as silent; both now ask one predicate on the voice.
