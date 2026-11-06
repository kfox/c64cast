- **A cleaner, steadier default picture.** Video has less dither texture by
  default. MultiHires video holds still on grainy flat areas, has crisper
  edges, and keeps dark amber and navy from turning green or gray. A video's
  color fit now adapts at each shot cut, so a dark opening no longer blows out
  the bright shots after it. For the old look, set these in `[color]`:
  `dither_strength = 0.5`, `motion_smoothing = 0.25`,
  `auto_fit_strength = 1.0`, `auto_fit_shots = false`,
  `dark_tone_taper = false`, `input_smoothing = 0` and `sharpen = 0`.
