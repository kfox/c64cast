- **Video and slideshows on a palette fitted to the source keep their own
  colors.** With `hardware_palette = "source"` or `"auto"`, the boosts meant
  for the machine's stock palette are left out: the saturation lift, the
  default `channel_boost` and the built-in gold band. Faces stay skin-toned
  instead of orange, and grays stay gray. A `channel_boost` or
  `hue_corrections` set in `[color]` still applies.
