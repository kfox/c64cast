- **A live mic on `[audio].use_reu_pump` plays about 0.3 s behind the input
  at 12 kHz, down from about 0.73 s, and no longer replays old audio under
  mhires.** The 133 ms the host holds is only the first stage of the delay;
  the second is how far the C64 copies ahead of the sample it is playing,
  and nothing chose it. Without a bank-switched video mode the copying started
  behind the playback, so every sample waited most of an 8 KB lap. Under
  `mhires` it started just ahead, and playback could catch up with it and play
  audio from 0.7 s earlier. The scene now sets that lead to 2 KB (about
  170 ms) once both are running, and logs a warning if it cannot.
