- **A directory of expensive SIDs no longer stalls scene startup unbounded.**
  Each candidate costs a host-emulated INIT plus a 50-pass PLAY pre-flight, and
  the tune prices both; with a per-candidate bound only, a pool of crafted
  tunes measured ~8.8 s of blocked startup before the scene gave up. Both pool
  walks — `waveform` and SID audio — now share one wall-clock analysis budget
  across the whole walk. A candidate refused because that budget ran out says
  so, instead of being reported as a tune that spins on a raster interrupt.
