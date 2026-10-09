- **`[audio].dac_bitmap_tempo_hires` and `dac_bitmap_tempo_mhires` now default
  to the value measured for the connected hardware** rather than a fixed 0.89 /
  0.88. Those are still what an Ultimate 64 and a TeensyROM writing unsliced
  get; a TeensyROM+ slicing its writes gets 0.97, since its sample player
  loses far fewer interrupts and would otherwise play bitmap video ~9% fast. A
  value you set yourself still wins.
