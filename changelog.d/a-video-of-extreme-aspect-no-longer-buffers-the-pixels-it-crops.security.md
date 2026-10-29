- **A video of extreme aspect ratio no longer fills memory with pixels it never
  shows.** A video scene crops every frame to the C64's 1.6:1, but the decoded
  frame was buffered before the crop, and a frame much wider or taller than
  that (16000x250, say) could not be downscaled without losing the part kept.
  Such a frame took 12 MB against under 1 MB for a 4K one, times the 240
  frames buffered. Frames are now cut to the part the crop keeps as they are
  decoded; what reaches the screen is unchanged.
