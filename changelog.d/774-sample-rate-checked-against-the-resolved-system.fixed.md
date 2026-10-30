- **An `[audio].sample_rate` too fast for a PAL machine is refused when
  `system = "auto"` resolves to PAL.** The load-time check could only assume
  NTSC, whose ceiling is higher, so a rate between the two ceilings passed on
  a PAL machine. The rate is now checked again once the machine's standard is
  read, and a live `--doctor` reports it against that standard too.
