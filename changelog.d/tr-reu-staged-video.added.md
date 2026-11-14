- **REU-staged bitmap video on the TeensyROM+.** Where the TeensyROM+ firmware
  supports WriteREU and REU emulation is on (Special IO set to an REU in the
  TeensyROM menu), `hires` and `mhires` scenes now stage frames through the REU
  bank-swap, as they do on an Ultimate. On mhires that cuts the video's DMA
  onto the C64 bus from about 290 KiB/s to about 1 KiB/s; the frames still
  cross the USB link, into the REU. Buffered ASID playback uses the same REU.
  Older firmware, or REU emulation left off, keeps host-DMA video as before.
  The character modes and the REU audio pump stay on host DMA on a TeensyROM+.
