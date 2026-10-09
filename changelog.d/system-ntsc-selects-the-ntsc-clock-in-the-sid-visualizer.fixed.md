- **`system = "ntsc"` selects the NTSC clock in the SID visualizer.** The
  setting is documented and validated as case-insensitive, but the emulator
  compared it against `"NTSC"` exactly, so any lowercase spelling silently got
  the PAL clock — 3.7% off, which also fed the PLAY-rate probe and drifted the
  scope about seven seconds behind the audio over a three-minute tune. The
  spelling is normalized once, where the clock is chosen, for all four scenes
  that build one.
