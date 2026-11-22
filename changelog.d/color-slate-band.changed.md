- **mcm and mhires no longer blotch a gray-blue wall purple.** A muted
  slate-blue surface, such as a wall, water or an overcast sky, now settles on
  the grays instead of breaking into shifting purple and light-blue patches. A
  second built-in hue band pulls those tones toward gray, judged on the source
  frame's own saturation so a saturated blue or cyan, a color bar's included,
  keeps its color. petscii is unchanged. `[[color.hue_corrections]]` bands can
  do the same with the new `sat_fade` and `gate_on_source` keys, and
  `hue_corrections_replace_defaults = true` drops both built-in bands.
