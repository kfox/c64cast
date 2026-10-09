- **A multispeed tune could peg a CPU core for a whole scene.** The host
  emulator's catch-up batch is bounded by a fraction of one poll period, but a
  tune sets both the PLAY rate that period comes from (a CIA #1 Timer A latch,
  up to 8x the video rate) and the cost of a PLAY pass — and a pass runs before
  the clock is consulted, because truncating one would leave the oscilloscope
  showing half a frame's register writes. A 400 Hz latch against a 10 ms pass
  gave a 401% duty cycle, back to back, for the scene's whole duration. The
  poll thread's wakeup period is now floored against a measured pass cost so a
  pass fits inside its allowance, and a batch that blows its allowance on a
  single pass says so instead of looking complete. The scope (or the reactive
  visuals) then lags the audio, which is visible and logged once, rather than
  starving the render thread.
