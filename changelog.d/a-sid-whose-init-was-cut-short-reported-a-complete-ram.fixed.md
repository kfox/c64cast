- **A SID whose INIT was cut short reported a complete RAM footprint.** The
  footprint places the relocated C64-side player in RAM the tune demonstrably
  never touches, and it carries a flag saying whether the sample can be
  trusted. That flag was computed from the wall clock and one instruction-set
  check, and never from the emulator's own "this routine did not finish" —
  so a tune whose INIT hit its 2 M-cycle cap (a fat decompressor) or its
  deadline handed the player a hole that the rest of INIT was about to fill.
  The result is the exact failure the footprint exists to prevent: silence and
  a crash to BASIC. Every way a run can end short now marks the sample.
