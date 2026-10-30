- **A MIDI `jump` mapping to a negative scene, or to `true`/`false`, is now
  refused when the config or controller profile loads.** It used to load and
  then do nothing when pressed; the error now names the cc_map entry.
