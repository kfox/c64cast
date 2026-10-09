- `AudioStreamer.stats()` renames `late_worst_s` to `late_worst_window_s` and
  adds `running`. Every other counter in that snapshot is cumulative for the
  run, while this one is cleared on each health log line, so the key now says
  which it is. (Public only to the Python API, which carries no stability
  promise at `0.x`; nothing in the CLI or config surface changes.)
