- **A TeensyROM+ on firmware v0.9 or later can slice large DMA writes while
  DAC audio plays**, letting the 6510 run between 32-byte slices instead of
  halting for the whole write. The `$D418` sample player loses about 3.9×
  fewer interrupts per byte written, so bitmap video with DAC audio drains
  much closer to real time and its measured pitch wobble falls 2–4×. It is
  off by default: slicing costs bulk throughput, and on bitmap video that
  shortens or drops frames that are on screen only briefly, while on a music
  video the audio still sounded better unsliced. Set
  `[teensyrom].dma_slicing` to `auto` (use it when the firmware has it) or
  `on` (also warn when it does not); `dma_slice_bytes` and `dma_slice_gap_us`
  set the slice shape. Writes stay unsliced when no sample player is running,
  and a regular TeensyROM or older firmware never slices.
