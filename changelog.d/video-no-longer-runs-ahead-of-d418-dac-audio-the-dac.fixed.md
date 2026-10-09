- **Video no longer runs ahead of `$D418` DAC audio.** The DAC clock counted a
  sample as played once it reached the C64's ring buffer, but the C64 plays it
  about a third of a second later, so the picture led the sound by that much
  for the whole run. The clock now subtracts what is still waiting in the ring.
  A seek or loop wrap holds the picture for the same third of a second, so the
  picture and the sound change together.
