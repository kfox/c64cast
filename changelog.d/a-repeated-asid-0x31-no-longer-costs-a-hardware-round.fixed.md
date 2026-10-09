- **A repeated ASID `0x31` no longer costs a hardware round trip each time.** An
  identical speed request is dropped instead of re-programming the CIA timer —
  each retune blocks on the single shared Ultimate DMA socket that the video
  render path also uses, and a host that sends `0x31` every frame was spending a
  large share of the write budget saying nothing new.
