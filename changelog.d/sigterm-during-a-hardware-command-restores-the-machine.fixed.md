- **A SIGTERM during `--dump-char-rom` or `--calibrate-dac` now leaves the
  machine reset.** It used to end the process at once, skipping the reset after
  a dump and the calibration's restore of the mixer volume and SID models.
