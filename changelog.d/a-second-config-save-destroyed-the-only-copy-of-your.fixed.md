- **A second config save destroyed the only copy of your hand-written show
  file.** Saving back live-tune changes copied the config to one fixed
  `<name>.bak` on *every* save, so the second save's backup was the first
  save's output — and `--overwrite` saves on every normal exit and Ctrl+C when
  anything was tuned, so two runs of a tuned show was all it took. The log line
  said "(backup .bak)" throughout, which read as reassurance. The `.bak` is now
  written once, only when it does not already exist, so it stays the file you
  authored however many times the show saves over itself; the log says which of
  the two happened. There is no undo for the previous save any more — the file
  worth keeping is the one nothing generated.
