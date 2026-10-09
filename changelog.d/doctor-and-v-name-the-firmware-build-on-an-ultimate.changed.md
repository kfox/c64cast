- **`--doctor` and `-v` name the firmware build.** On an Ultimate, doctor's
  CONNECTIVITY section gains a `<system> (device)` row naming the machine and
  its firmware; from firmware 3.15a it reads, for example,
  `Ultimate 64-II B95B01 (firmware 3.15a build dddd29b2, FPGA 125, core 1.50)`, and the connect-time `connected device:`
  log line carries the same build hash at `-v`. Every run's connect line now
  shows the core version too. Older firmware and the C64 Ultimate leave out
  what they do not report.
