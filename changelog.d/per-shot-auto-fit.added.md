- **A video's `auto_fit` refits at each shot cut instead of keeping its
  opening's fit for the whole video.** A stretch made for a dark or flat
  opening no longer blows out the bright shots after it. Cuts are found in the
  frames already decoded ahead of display, so the new fit lands on the cut
  frame. `[color].auto_fit_shots` (default `true`) turns this off.
  `[color].auto_fit_shot_max_rise` (default `1.0`) caps how far a shot may
  strengthen the opening's contrast and saturation. At 1.0 a cut can only
  relax the fit, which reduced cell shimmer on every clip measured. Raise it to
  let dark shots punch harder, at the cost of more shimmer on grainy footage.
  `[color].auto_fit_shot_relax` (default `0.5`) sets how far a shot's fit
  moves from the opening's toward its own: 0 keeps the opening's fit, 1 takes
  the shot's.
