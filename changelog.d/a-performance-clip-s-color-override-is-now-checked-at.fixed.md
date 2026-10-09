- **A performance clip's `color` override is now checked at load, the same as a
  scene's.** The `dither`, `motion_smoothing`, `color_match`, `cell_strategy`
  and `flicker_tolerance` checks read `[color]` and every `[[scenes]]` override
  but no `[[performance.clips]]` one, so a bad value in a clip was not caught
  at load. Such a config is now refused at load, naming the clip, and
  `--doctor` reports it.
