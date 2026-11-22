- **mcm and mhires video no longer turns gray walls, beige cases and faces
  cyan.** Left unset, `[color].channel_boost` is now no gain on mcm and mhires
  (it was a `[1.3, 1.2, 1.0]` blue/green lift), and `auto_fit_white_level` is
  220 there, so a lit wall or sky keeps some headroom below white. petscii
  keeps the lift and a white level of 255, since its glyphs thin out without
  them. A new built-in hue band lifts bright, saturated gold and amber, such as
  a light-cycle trail, onto yellow or orange instead of brown on all three
  modes; `hue_corrections_replace_defaults = true` drops it. To get the earlier
  look back, set `channel_boost = [1.3, 1.2, 1.0]`,
  `auto_fit_white_level = 255` and `hue_corrections_replace_defaults = true`
  under `[color]`, or under one scene's `[scenes.color]`.
