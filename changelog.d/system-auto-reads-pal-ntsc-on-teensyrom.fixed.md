- **`[ultimate64].system = "auto"` now detects a PAL C64 behind a TeensyROM+
  or an Ultimate II+.** Those backends have no System Mode to ask, so `auto`
  always became NTSC, and a PAL machine ran with NTSC's frame rate, CPU clock
  and SID PLAY rate while the log said the backend "can't report" its timing.
  `auto` now reads the PAL/NTSC flag the KERNAL sets at `$02A6` on reset, and
  `--doctor` checks an explicit `system` against it. A failed read or a value
  other than 0 or 1 still falls back to NTSC, with a log that says the read
  failed.
