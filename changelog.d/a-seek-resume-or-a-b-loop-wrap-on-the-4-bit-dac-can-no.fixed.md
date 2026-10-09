- **A seek, resume or A/B loop wrap on the 4-bit DAC can no longer leave the
  audio clock behind the sound for the rest of the scene.** A splice that
  caught a block of audio as it entered the queue dropped its samples from the
  clock but kept them counted as waiting, so the picture lagged the sound by
  that block until the scene ended.
