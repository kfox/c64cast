- **A CIA-timed SID tune could peg a core for the whole scene, and the guard
  against it was skipped on exactly those tunes.** The oscilloscope and the
  reactive-visuals feature stream size their poll period so one emulated PLAY
  pass fits inside a fraction of it — otherwise a multispeed tune's short
  period and long pass leave the thread running back to back, taking the render
  thread's time with it. The measurement came from the PLAY-rate probe, which
  read the tune's rate at the top of its loop and stopped as soon as it knew
  it. A tune that programs CIA #1 Timer A from its INIT settles the rate before
  the first pass, so the probe stopped having timed nothing, reported a pass
  cost of zero, and the floor never applied: a 399.3 Hz tune kept a 2.50 ms
  poll period against a pass it had never priced. The probe now times a pass
  before it reads the rate. A tune too expensive to probe at all is charged the
  worst a legal pass can cost rather than nothing — those were the same value
  before, and the cheap reading won.
