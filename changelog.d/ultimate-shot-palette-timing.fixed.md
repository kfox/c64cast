- **On an Ultimate, a video shot's own colors no longer show up a frame or two
  early.** In a double-buffered bitmap display mode the palette now changes in
  the same commit that shows the shot's first frame, as it does on a Kawari,
  instead of recoloring the end of the previous shot and tearing one frame.
  The C64 now answers that change itself, which takes a little longer, so a
  shot there needs 1.5 s rather than 1.3 s to get its own colors.
