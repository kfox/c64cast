- **mcm and mhires video keeps some headroom below white, and bright gold
  lands on yellow.** Left unset, `[color].auto_fit_white_level` is now 220 on
  mcm and mhires, so a lit wall or sky no longer clips to white or cyan;
  petscii keeps 255, since its glyphs thin out under headroom. A new built-in
  hue band lifts bright, saturated gold and amber, such as a light-cycle trail,
  onto yellow or orange instead of brown on all three modes;
  `hue_corrections_replace_defaults = true` drops it. To get the earlier look
  back, set `auto_fit_white_level = 255` and
  `hue_corrections_replace_defaults = true` under `[color]`, or under one
  scene's `[scenes.color]`.
