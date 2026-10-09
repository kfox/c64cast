- **The ensemble master silently discarded six of its own sections.**
  `load_master` applied the master TOML through a hand-written tuple of
  `(section, dataclass)` pairs instead of the shared apply loop, and the two
  had drifted: `[hardware]`, `[teensyrom]`, `[vision]`, `[dsp]`,
  `[audio_features]` and `[wled]` never reached `_apply_section` at all, so
  they produced neither an applied value nor an unknown-key record — no
  warning, no `--doctor` row, nothing. `[hardware]` and `[teensyrom]` are
  *listed as cascading*, so the cascade dutifully ran over a
  `defaults.hardware` nothing had populated: a master `[hardware] backend =
  "teensyrom"` read as nothing while every system in the wall quietly dialed
  the default Ultimate URL. The tuple also ran 3 of the 10 load-time
  validators, so a master `[ultimate64].sid_panning = [99]` was copied into
  every system and failed mid-show when the mixer was configured — exactly
  what that validator's docstring says it exists to prevent. The master now
  goes through `_apply_toml_sections` like any other file, so it inherits
  every validator, the unknown-key hints and the `[color]` handling.
  `[hardware]`, `[teensyrom]`, `[dsp]`, `[audio_features]`, `[vision]` and
  `[wled]` cascade from a master for the first time.
