- **The console's tempo source reads "midi" again once MIDI clock takes the grid
  back from a tap.** A tap switched the reported source to "internal", and a
  later MIDI Start, Continue or running clock never switched it back, so the
  console kept saying "internal" while the DAW drove the beat.
