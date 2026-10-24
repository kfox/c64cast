- **Remove `sample_rate = 12000` from `[audio]` if you copied it from the
  example config.** The shipped example used to set it, and a set rate now
  holds for every scene, which keeps bitmap video on a TeensyROM+ at 12 kHz and
  its lower frame rate. Leave the key unset to get the per-scene default.
