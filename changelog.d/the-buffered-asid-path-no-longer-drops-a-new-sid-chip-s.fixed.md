- **The buffered ASID path no longer drops a new SID chip's first frame.** That
  frame arrives before the chip has an address, and because this path sends
  register *deltas*, discarding it lost the chip's initial ADSR, pulse width and
  control setup for good — while the oscilloscope still showed a
  correctly-configured voice the hardware was not playing. Deltas for a
  not-yet-mapped chip are now carried forward, as the non-buffered path already
  did.
