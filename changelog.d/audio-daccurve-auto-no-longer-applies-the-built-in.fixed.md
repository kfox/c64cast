- **`[audio].dac_curve = "auto"` no longer applies the built-in UltiSID table
  to a real SID chip it couldn't identify.** On an Ultimate II+, and on an
  Ultimate 64 whose SID socket settings couldn't be read, `auto` assumed the
  emulated UltiSID was playing and picked its table. On a real 6581/8580 that
  table plays as heavy distortion. `auto` now uses the 4-bit linear DAC and
  warns, unless a `--calibrate-dac` calibration applies.
