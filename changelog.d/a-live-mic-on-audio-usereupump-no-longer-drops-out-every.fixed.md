- **A live mic on `[audio].use_reu_pump` no longer drops out every couple of
  seconds after the machine speeds up mid-scene.** When the mic's delay had to
  be reset (the C64 caught up with the computer, or fell far behind), the
  correction that had been running before the reset carried on. If the C64 had
  meanwhile sped up, for instance as a bank-switched video mode got lighter,
  that stale correction caught it up again within seconds, and each reset is a
  short silence: in simulation, about 24 silences a minute. The correction now
  restarts from the speed the C64 is actually running at.
