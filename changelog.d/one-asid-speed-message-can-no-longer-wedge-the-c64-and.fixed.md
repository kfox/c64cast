- **One ASID speed message can no longer wedge the C64 and flood the link.** A
  `0x31` carries a frame delta in microseconds, and a delta of 1 asked the
  buffered ring player for a 1 MHz consume rate. Nothing rejected it: the CIA
  helper clamps the *timer latch*, not the rate, so the request landed on the
  fastest timer the chip can run — an IRQ every two cycles into a handler that
  needs hundreds, leaving the 6510 unable to reach the jiffy clock, the keyboard
  scan or anything else until a power cycle. On the host side the computed read
  head then advanced half a million slots a second, so the writer thread chased
  it at the link's maximum rate forever, taking the whole Ultimate DMA socket
  the video render path shares. ASID-derived rates are now clamped to the band
  the protocol and the CIA can actually express (roughly 15-1000 Hz — 16× the
  video rate is the fastest the spec's speed multiplier can ask for), with a
  warning naming the clamp, and the kernal-chain tick divider is bounded to the
  8-bit immediate that carries it instead of being silently truncated. The ASID
  scene's own copy of that rate now goes through the same clamp rather than
  keeping an unclamped value the hardware never received.
