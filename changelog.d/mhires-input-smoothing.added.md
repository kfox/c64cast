- **`[color].input_smoothing` steadies grain on still areas of mhires video.**
  Before quantizing, each pixel that has barely moved is blended with its own
  recent history, so film grain on a flat wall stops flipping pixels between
  two palette colors; pixels that move take the new frame, so motion is not
  smeared. `"auto"` (default) turns it on for video scenes at 0.8 and leaves it
  off elsewhere; `input_smoothing = 0` turns it off.
