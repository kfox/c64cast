- **A second lap of an ASID scene no longer inherits the previous stream's
  cadence, chip count, or queued frames.** Playlists reuse scene instances, and
  setup hands the stored frame rate straight to the player — so lap 1's host
  chose the CIA rate lap 2 came up at, before a byte of the new stream arrived.
  The wire-owned state is reset on activation, including the PAL/NTSC standard a
  `0x31` may have switched (a hardware default restored for the wrong standard
  leaves the jiffy clock ~3.8% off).
