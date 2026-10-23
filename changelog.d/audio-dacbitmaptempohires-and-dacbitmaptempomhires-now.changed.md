- **`[audio].dac_bitmap_tempo_hires` and `dac_bitmap_tempo_mhires` now default
  to the value measured for the connected hardware** rather than a fixed 0.89 /
  0.88. Those are still what an Ultimate 64 and a TeensyROM writing unsliced
  get; a TeensyROM+ whose writes are locked to the sample player gets 1.0, and
  one slicing without the lock gets 0.97, since their sample players lose no
  or far fewer interrupts and would otherwise play bitmap video fast. A
  value you set yourself still wins.
