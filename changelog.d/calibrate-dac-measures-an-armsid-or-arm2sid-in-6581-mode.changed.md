- **`--calibrate-dac` measures an ARMSID or ARM2SID in 6581 mode.** It used to
  measure the chip in whatever model it was left in, so the table you got
  depended on the menu or the last tune played. The chip is switched to 6581
  for the measurement and put back into its own model when the run ends, on
  a failure or Ctrl+C too. This works on an Ultimate 64 and on a TeensyROM+.
  A run that plays through the table switches the chip to 6581 for the length
  of the run, as before. A table you measured earlier in 8580 mode still plays
  in 8580 mode.
