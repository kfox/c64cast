- **Reactive visuals on a large-block FLAC file played through the sampler
  keep following the music.** The decoder handed the sampler each decoded
  frame whole, and the sampler's queue counts frames, so with frames of up to
  65535 samples it ran minutes ahead of the sound, past the 30 s the analyzer
  can look back; the visuals then went still. Frames are now pushed in pieces
  of at most 0.1 s.
