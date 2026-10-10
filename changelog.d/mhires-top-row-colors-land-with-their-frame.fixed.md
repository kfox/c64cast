- **Double-buffered `hires` and `mhires` video can no longer start a bank
  swap too late to finish before the picture begins.** A swap that started
  late in its window, with DAC audio running, could
  land on the first picture line, and on REU-staged `mhires` could still be
  copying the top cell row's colors there, so that frame showed its top row
  in the previous frame's colors. Swaps now start no later than raster line
  43, REU-staged `hires` swaps, which also write the border, no later than
  line 42, and REU-staged `mhires` swaps no later than line 38, which leaves room
  for the swap under the heaviest audio load the player allows; a swap that
  misses its window waits one field.
