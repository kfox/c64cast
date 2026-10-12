- **A TeensyROM+ with `[teensyrom].dma_slicing` on no longer slows every write
  while the C64 is paused or REU emulation is running.** The firmware refuses
  sliced writes then, and each refusal cost about 0.3 s; every write asked
  again and paid it. A refusal is now remembered: writes go unsliced without
  asking, and the next ask waits 1 s, doubling up to 16 s while the refusals
  continue. Slicing resumes on the first ask after the bus comes free. While
  writes go unsliced, bitmap + DAC video starts from the unsliced tempo figure
  (0.88 / 0.89) instead of 0.97, and so does a slice shape other than the
  default 32 bytes / 40 µs, the only one 0.97 was measured at.
