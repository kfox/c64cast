- **`[audio_features].bands` of 10 or more no longer leaves the lowest band
  dead.** At the default 1024-sample window the lowest band read zero
  forever, which weakened the bass that drives brightness. Every band now
  covers at least one frequency bin, and a band count larger than the window
  can split is refused with an error instead of producing empty bands.
