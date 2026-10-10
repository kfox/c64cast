- **REU-staged `mhires` video can no longer show a frame's top cell row in
  the previous frame's colors.** The tear-free bank swap copies color RAM
  just after it flips the bank, and a swap that started late in its window
  could still be copying the top row's colors when the picture began, most
  likely with DAC audio running at a high sample rate. An `mhires` swap now
  starts no later than raster line 40, which leaves room for that copy under
  the heaviest audio load the player allows; a swap that misses it waits one
  field.
