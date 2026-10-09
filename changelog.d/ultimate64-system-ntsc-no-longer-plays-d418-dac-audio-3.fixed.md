- **`[ultimate64] system = "ntsc"` no longer plays `$D418` DAC audio 3.9%
  fast.** The lowercase spelling was accepted, but the NMI timer compared it
  against `"NTSC"` exactly and fell through to the PAL clock, so an NTSC
  machine ran the 12 kHz default at 12472 Hz — at the live-pipeline overrun
  onset — while pacing and the video clock assumed 12015 Hz. The value is now
  stored in its declared spelling when it loads, and the timer takes its clock
  from the shared `cpu_clock()`.
