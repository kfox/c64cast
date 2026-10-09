- **A run on an Ultimate whose `Vol Master` is OFF is no longer silent.**
  Firmware 3.15 adds a master level to the audio mixer (F2 → Audio Mixer on an
  Ultimate 64, Audio Output Settings on an Ultimate II+) and multiplies it into
  every source, so at OFF nothing is heard whatever the per-source rows say —
  while c64cast reported the sampler audible and set the SID levels as if it
  were. A run that wants audio now raises `Vol Master` from OFF to 0 dB, live
  and never saved to flash, and puts it back at teardown; any other level is
  left as you set it, and `sid_volume` levels are relative to it. `--doctor`
  reports the master level and names it when it is OFF, and DAC calibration
  measures at master unity. Firmware without the setting (3.14 and earlier,
  the C64 Ultimate's 1.1.0) behaves as before.
