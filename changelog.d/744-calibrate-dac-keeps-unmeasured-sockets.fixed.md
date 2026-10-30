- **`--calibrate-dac` no longer throws away the tables of SID sockets it did
  not measure, and each machine plays the table measured for it.** A run that
  measured only one socket, or only the single `$D400` chip over a TeensyROM+,
  used to rewrite the whole calibration file and drop every other socket's
  table. It now updates the entries it measured and keeps the rest. On an
  Ultimate 64, playback uses the table of the socketed SID answering `$D400`,
  and the built-in curve when none does; `--calibrate-dac` there stops with an
  error, writing nothing, when no socketed SID answers `$D400`. A TeensyROM+
  or an Ultimate II+ plays the one table measured over it. A table an
  older run on an Ultimate 64 filed without a socket is no longer used; run
  `--calibrate-dac` again with the SID mapped to `$D400`.
