- **A hostile or broken tune can no longer freeze playback indefinitely.** The
  host-side 6502 the oscilloscope runs in parallel bounded each INIT/PLAY call
  by *emulated cycles*, and py65 charges zero cycles for the 105 undocumented
  opcodes it does not implement — so a PLAY built out of those spun for free,
  measured at 7-21 seconds per frame against a budget meant to be 4 ms. Calls
  are now bounded by interpreter steps as well, an unimplemented opcode ends
  the pass with a warning instead of derailing the instruction stream, and the
  SHIFT-cycle's subtune search is capped the way scene setup's already was — a
  tune declaring 65535 subtunes used to walk all of them, one full emulation
  run each, on the render thread. A tune whose PLAY uses an undocumented
  opcode still plays: those are a normal idiom in hand-written players and the
  real 6510 runs them. What such a tune loses is the trust placed in the RAM
  footprint sampled from it, not its place in the playlist.
