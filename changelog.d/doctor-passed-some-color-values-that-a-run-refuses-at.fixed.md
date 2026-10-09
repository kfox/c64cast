- **`--doctor` passed some color values that a run refuses at startup.** A bad
  `[color].flicker_tolerance`, and a `[[scenes]]` override's `color_match`,
  `cell_strategy` or `motion_smoothing` on a display that setting does not
  affect, passed `--doctor --skip-probe`. `--doctor` now reports them.
