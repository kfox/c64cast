- **A DAC calibration now puts an ARMSID back into the model it was
  calibrated in when the calibration ran without socket detection** (on
  a TeensyROM+, or when detection failed). The table used to play in whatever
  model the chip happened to be in. The chip at `$D400` is now switched for the
  run and put back afterward, on any link.
