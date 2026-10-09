- **A scene now recovers when the C64 is reset or the Ultimate restarts
  under it** (the reset button, a reset from the Ultimate's menu or web
  remote, a power blip or a firmware crash). It used to keep running against
  a machine that had lost its picture setup, its audio setup and the REU and
  sampler settings c64cast had turned on, so the rest of the scene showed the
  BASIC screen. Now c64cast notices the reset within a moment, logs it, puts
  those settings back and starts the scene over. A restart while the link is
  still down at the end of a scene is caught before the next one starts. A
  program the launcher started owns the machine, so c64cast does not watch
  for a restart under it. A tune that clears the memory c64cast checks looks
  like a reset, so a second reset in one play of a scene is taken to be such
  a tune: c64cast logs a warning and stops checking until the next scene.
