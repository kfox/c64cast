- **A writer thread that outlived its shutdown can no longer arm the player
  behind teardown's back.** The join that stops it is deliberately bounded, and
  the arm sequence blocks in DMA before it swaps `$0314`, so an abandoned writer
  could finish afterwards and hook the vector to `$C000` with the SID already
  silenced and the next scene running — an orphaned handler rewriting the whole
  REU control block at up to 960 Hz, into the register pair the next scene's
  audio pump reads back as its write head. The arm and the disarm are now
  mutually exclusive, and the arm refuses outright once shutdown has begun.
