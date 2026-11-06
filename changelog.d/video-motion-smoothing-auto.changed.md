- **mhires video shimmers less on flat, grainy areas.** `[color].motion_smoothing`
  now defaults to `"auto"`: 0.3 on video scenes and the previous 0.1 on webcam,
  generative and slideshow scenes. Set a number to pin one value for every
  scene; `motion_smoothing = 0.1` restores the previous look on video.
