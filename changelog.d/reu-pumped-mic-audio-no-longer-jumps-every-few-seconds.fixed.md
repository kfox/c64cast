- **REU-pumped mic audio no longer jumps every few seconds.** With
  `[audio].use_reu_pump`, the C64-side pump fed the audio ring at the nominal
  rate while the reader lost ticks to video DMA, so the pump lapped it about
  every 12 s under petscii (every 30 to 60 s under mhires), and each lap
  skipped most of a second of audio. The pump's rate now follows the reader,
  steered from the measured ring lead about once a second; the excess input is
  dropped in short crossfaded splices instead, and a rate change the network
  drops is sent again with the next correction. Both mic loops share one read
  of the C64 a second, where they took up to three, since reading an
  Ultimate's memory during playback is what risks wedging it. `[audio].reu_pump_governor` (on by
  default) turns the rate steering off.
