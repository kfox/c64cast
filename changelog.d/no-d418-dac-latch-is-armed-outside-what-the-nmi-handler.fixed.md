- **No `$D418` DAC latch is armed outside what the NMI handler and the CIA
  timer allow.** A `pitch_mult_*` above about 1.13 at 12 kHz used to arm an NMI
  period shorter than the handler's safe budget, a zero multiplier crashed and
  a negative one locked the CPU in the handler; every latch is now held to the
  budget and a non-positive multiplier is an error. With `nmi_rate_adaptive`
  on, a rate past the safe ceiling played at the ceiling while the video clock
  assumed the rate asked for (2.7 % flat at 14 kHz NTSC). **A `sample_rate`
  inside the handler's entry-latency margin (about 13.7–15 kHz NTSC, 13.2–14.5
  kHz PAL) is now refused at load instead of warned about**, as is one below
  about 16 Hz, which the 16-bit timer truncated; where load cannot see the
  machine (`system = "auto"` on a PAL unit), the timer arms the nearest safe
  latch, reports that rate, and logs a WARNING.
