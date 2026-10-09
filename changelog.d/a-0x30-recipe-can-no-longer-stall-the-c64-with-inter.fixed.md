- **A `0x30` recipe can no longer stall the C64 with inter-write waits.** The
  number of writes per frame was capped, but their *cost* was not: 28 writes
  each carrying the protocol's maximum wait are over half a 60 Hz NTSC frame for
  a single SID, and two chips already outrun the period. That does not drop a
  frame — the timer fires again before the handler returns, so the 6510 never
  leaves it and the jiffy clock and keyboard scan stop. Each frame is now priced
  against the consume period and its waits scaled down to fit, with a one-time
  warning; every register write still reaches the chip.
