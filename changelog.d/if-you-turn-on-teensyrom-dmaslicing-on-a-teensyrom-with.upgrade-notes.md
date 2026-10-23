- **On a TeensyROM+, delete `dac_bitmap_tempo_hires` and
  `dac_bitmap_tempo_mhires` from your `[audio]` section if they came from the
  example config.** The example used to set them to 0.89 and 0.88, and a value
  in your file still wins over what the connected hardware resolves to: 1.0 on
  firmware that locks its writes to the sample player (the default when the
  firmware can), 0.97 with `[teensyrom].dma_slicing = "on"`. Bitmap video with
  DAC audio would otherwise play about 10% fast. Keep them only if you
  measured them yourself.
