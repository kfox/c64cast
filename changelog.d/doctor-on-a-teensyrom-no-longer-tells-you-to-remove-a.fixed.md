- **`--doctor` on a TeensyROM no longer warns about a sampler setting you never
  made.** Every TeensyROM config with a video scene and the default
  `[audio].backend = "auto"` got a warning to turn off a REU opt-in, while an
  explicit `backend = "sampler"` never got the warning that the 4-bit DAC plays
  instead. The default now reports nothing, and the explicit setting gets that
  warning.
