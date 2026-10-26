- **A malformed controller profile no longer disables every clip pad.** When
  `[midi_control].controller_profile` named a profile file that failed to
  parse, the listener kept the base mapping as intended but also skipped
  folding in each `[[performance.clips]]` entry's own `pad`, so no clip
  launched from its pad for the rest of the run. Clip pads are now mapped
  whether or not the profile loads.
