- **A `--calibrate-dac` re-run that fails its self-test no longer throws away
  the calibration you had.** The rejected measurement used to replace the
  socket's working table, so playback dropped to the linear curve; the old
  table now stays in use and the rejected run is saved beside it for
  diagnosis. A table for a chip that has since been swapped out is still
  dropped. A hand-edited table holding a value that is not a register byte
  is now refused rather than masked into a curve nobody measured.
