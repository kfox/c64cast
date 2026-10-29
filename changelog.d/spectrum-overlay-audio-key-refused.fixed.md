- **An `audio` key on a spectrum overlay is now a config error.** It used to
  replace the audio stream the overlay reads, so the config loaded and the
  overlay failed on its first frame and stayed blank.
