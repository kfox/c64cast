- **The REU mic pump ran ~33% slow at the default sample rate.** The C64-side
  pump is paced by CIA #1, whose latch has to be the chunk size times the NMI
  period — a ratio of periods, so it is the same on NTSC and PAL, but it tracks
  `[audio].sample_rate`. The video bring-up derived it from the live NMI latch;
  the mic bring-up wrote a constant whose own definition records it as the
  value for 8 kHz. At the shipped 12 kHz default that asked the pump for 85/128
  of the bytes the NMI drains, so the audio ring under-filled and the NMI
  re-read a lap-old span — the audible stale-data echo the derivation exists to
  prevent. Both paths now share one derivation, one register write and one
  record of what was written, and a latch too large for the two 8-bit registers
  (roughly `sample_rate` below 2 kHz — `nmi_rate_safety` bounds only the fast
  end) is clamped with a warning instead of being silently truncated modulo
  65536 into an arbitrary pump rate.
