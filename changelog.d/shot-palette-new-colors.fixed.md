- **A color that comes into a shot after it starts gets its own palette
  entry.** With `hardware_palette_shots`, a shot's palette is fitted to its
  first two seconds, and a refit waited for the whole picture to drift. A new
  color covering a small part of the frame never moved it far enough, so it
  fell to the nearest color the palette had, often a gray. A refit now also
  starts when part of the picture has no close color in the palette. A cut
  that comes just after such a refit takes over its start, so the palette
  isn't fitted to frames from both sides of the cut.
