- **The highest `[audio].sample_rate` the `$D418` DAC accepts is now about
  12.8 kHz on NTSC and 12.3 kHz on PAL, down from about 13.6 and 13.1 kHz.**
  A rate above 12864 Hz on NTSC or 12393 Hz on PAL is refused at load, and
  `--doctor` reports it. At the old ceiling about 1 % of samples played twice
  and skipped the next: when a badline stretched the interrupt handler, the
  next interrupt could arrive before the previous sample was stored. The
  12 kHz default is unchanged.
