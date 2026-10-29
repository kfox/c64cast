- **An Ultimate no longer piles up `/TEMP/C64CAST_1`, `_2`, `_3` copies.**
  c64cast started its programs through the firmware's REST `run_prg`
  runner, which saves each upload to the device's `/Temp` and never deletes
  it, so each launch after the first in a power cycle saved the same program
  under a new numbered name, and the C64 printed that name while loading. The
  BASIC clear loop, the SID player and `.prg` launches now load straight
  from the DMA connection, and nothing is written to `/Temp`. A `.crt`
  cartridge still goes through the REST runner.
