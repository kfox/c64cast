- **`c64cast --list-dac-calibrations` lists the DAC calibrations stored on
  this computer.** Each row gives the name to pass to
  `--dac-calibration-profile` (a profile name, or a device-keyed file such as
  `tr-…` or `ultimate-…`), the backend that measured it, when, and each SID
  socket it covers with the chip detected there. It needs no config file and
  no hardware.
