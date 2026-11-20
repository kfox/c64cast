- **TeensyROM+ bitmap video plays 12 kHz audio when its frames are REU-staged.**
  A Hires or MultiHires video staged through the REU no longer drops its DAC
  audio to 10 kHz: its frames never go out in the slices that held it there,
  and it keeps the full frame rate at 12 kHz.
