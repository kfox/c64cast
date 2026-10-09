- **A bad `[color].flicker_tolerance` was the one color typo that escaped the
  config check.** It had no whole-config validator, so it raised a plain
  `ValueError` from deep inside the display build — a different exit code from
  every sibling `[color]` field, naming only `[color]` and never the scene an
  override came from — and that raise is only reachable from a scene that paints
  a frame, so a bad value in a SID-only or blank-only playlist was never caught
  at all, `--doctor --skip-probe` included.
