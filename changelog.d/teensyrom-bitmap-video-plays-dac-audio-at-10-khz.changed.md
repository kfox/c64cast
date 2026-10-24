- **Bitmap video on a TeensyROM+ keeps its full frame rate with DAC audio.**
  While a TeensyROM+ locks its DMA slices to the sample store (the
  `[teensyrom].dma_slicing` default), a Hires or MultiHires scene now plays its
  `$D418` audio at 10 kHz instead of 12 kHz. The longer interrupt period lets
  each slice carry nearly twice the bytes, so a MultiHires video that dropped
  to about 14 fps at 12 kHz holds its full 20 fps; in a blind listening test
  the audio sounded no different. Character modes and every other machine
  stay at 12 kHz. `[audio].sample_rate` is now unset by default; setting it,
  or passing `--sample-rate`, fixes the rate for every scene as before.
