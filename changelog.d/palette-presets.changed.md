- **`pepto` is now Pepto's measured VIC-II palette, and a real C64 gets it.**
  The table c64cast called `pepto` was the brighter, more saturated one the
  C64-Wiki lists, about ΔE 25 from Pepto's measurement. `pepto` now holds
  Pepto's PAL measurement, and `host_palette = "auto"` uses it for a stock
  VIC-II, so video on a TeensyROM+ or Ultimate II+ picks colors against what the
  chip really shows. The old table is `c64-wiki`.
