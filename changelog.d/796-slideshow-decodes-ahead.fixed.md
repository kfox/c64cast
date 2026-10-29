- **A slideshow no longer stalls the picture at each slide change.** The file
  spec is resolved once per pass instead of at every slide, so a `**` glob
  over a large folder no longer runs between frames, and the next picture is
  decoded in the background while the current one is on screen. Large JPEGs
  decode at a reduced scale that still covers what the display mode keeps.
