- **mhires video shimmers less on flat, grainy areas.** `[color].motion_smoothing`
  now defaults to `"auto"`: 0.3 on video scenes and 0.1 on webcam, generative
  and slideshow scenes, where it was 0.25 everywhere. Set a number to pin one
  value for every scene; `motion_smoothing = 0.25` restores the previous look.
