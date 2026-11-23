- **mcm and mhires keep muted blue muted.** A gray-blue surface, such as a
  wall, water or an overcast sky, no longer breaks into a palette's purple and
  light blue: a pixel whose source is a muted slate blue now pays for any
  palette color more colorful than the source itself, so it settles on the
  grays or the nearest muted blue the active palette has. The test runs on the
  source frame, before auto-fit and the saturation lift, so a saturated blue
  or cyan, a color bar's included, keeps its color, and it adapts to whichever
  host palette is in use. mhires skips it while it blends (`flicker_tolerance`
  on with `palette_mode = "percell"`). petscii is unchanged. The new
  `[color].slate_keep` setting, on by default, turns it off.
