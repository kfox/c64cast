- **Long video playback on a TeensyROM+ no longer crashes the C64.** With
  `[teensyrom].dma_slicing` at `auto` (the default) or `locked`, each DMA slice
  was sized from a cycle budget that left out the KERNAL's NMI entry and the
  cycles the display's badlines take, so under steady video writes the `$D418`
  sample player's interrupts piled up inside one another until the stack
  wrapped and the C64 fell into a KERNAL warm start, about a minute and a half
  into a music video at 12 kHz. Slices are now sized to leave room for both:
  17 bytes at 12 kHz on NTSC and 15 on PAL, down from 33.
