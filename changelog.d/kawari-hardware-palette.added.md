- **`[color].hardware_palette` now works on a C64 whose VIC-II is a Kawari
  with a configurable RGB palette.** Video and slideshow scenes push their own
  16 colors to it, and its own palette comes back at exit. Nothing is ever
  saved to the Kawari's EEPROM. A Kawari keeps its palette through a reset, so
  a run that ends early leaves its colors on the machine; the next run notices
  and puts the Kawari's own palette back first. A hires mode an earlier
  program left on is turned off at startup. Behind a TeensyROM+, on Kawari
  firmware 1.16 or newer, each shot of a video gets its own colors, as on an
  Ultimate 64, and they change between frames rather than partway down the
  picture; behind an Ultimate II+, or on older firmware, a video keeps one
  palette.
