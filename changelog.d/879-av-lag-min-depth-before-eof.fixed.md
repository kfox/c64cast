- **The video A/V lag summary's minimum buffer depth now reports decode
  headroom instead of always reading 0.** It took the minimum over the whole
  run, including the frames shown after the end of the file, where the buffer
  always drains to nothing. It now covers only the frames before the end of the
  file, says so (`min buffer depth=N before EOF`), and reads `n/a` when no frame
  was shown before it.
