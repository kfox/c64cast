- **REU-staged `hires` and `mhires` video no longer shows a torn frame at
  each cut.** The C64 flipped to the new frame as soon as it finished copying
  it, wherever the picture was being drawn, so each scene change showed one
  frame with the old picture above a line and the new one below. It now flips
  only between frames, and always copies into the bank that is off screen.
