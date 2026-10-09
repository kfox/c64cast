- **If you turn on `[teensyrom].dma_slicing` on a TeensyROM+ with firmware
  v0.9 or later, delete `dac_bitmap_tempo_hires` and `dac_bitmap_tempo_mhires`
  from your `[audio]` section if they came from the example config.** The
  example used to set them to 0.89 and 0.88, and a value in your file still
  wins over the 0.97 a slicing TeensyROM+ resolves to — so bitmap video with
  DAC audio would play about 10% fast. Keep them only if you measured them
  yourself.
