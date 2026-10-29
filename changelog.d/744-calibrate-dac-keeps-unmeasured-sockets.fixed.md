- **`--calibrate-dac` no longer throws away the tables of SID sockets it did
  not measure.** A run that measured only one socket, or only the single
  `$D400` chip over a link without socket detection, used to rewrite the whole
  calibration file and drop every other socket's table. It now updates the
  entries it measured and keeps the rest.
