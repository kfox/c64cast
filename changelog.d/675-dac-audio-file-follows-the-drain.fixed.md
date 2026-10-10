- **An audio file on the `$D418` DAC no longer plays about 6 % slow and flat
  under a busy scene.** A generative scene that redraws the screen every frame
  writes enough over the bus to cost the NMI player about one tick in twenty,
  in char and bitmap modes alike, so `audio_source = "file"` with
  `[audio].backend = "dac"` played the track that much slow and low. The file is
  now resampled to the rate the C64 actually plays it at, measured as it plays,
  so the track keeps its own speed and pitch after the first few seconds. The
  Ultimate Audio sampler path is unchanged.
