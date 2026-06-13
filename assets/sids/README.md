# assets/sids/

PSID files for the `waveform` scene. The Ultimate 64 plays the SID
natively from a small player PRG c64cast uploads into C64 RAM (the
firmware's own `/v1/runners:sidplay` runner is deliberately avoided — it
hijacks the HDMI output with its own UI). The SID registers are
write-only and read back as open-bus zeros, so c64cast can't poll the
chip; instead it runs the same tune in parallel on a host-side
[py65 6502 emulator](../../c64cast/sid_host_emu.py) that traps
`$D400-$D418` writes and feeds an in-process
[SID emulator](../../c64cast/sidemu.py) to drive the per-voice waveform
visualization. PSID-only — RSIDs are refused (see
[docs/caveats.md](../../docs/caveats.md)).

## Sources

- **HVSC** (High Voltage SID Collection): https://hvsc.c64.org/
  — Tens of thousands of curated SIDs. Drop the `C64Music/` tree anywhere
    and point your config at the individual files you want to play.
- **CSDb releases**: https://csdb.dk/ — Often bundle a `.sid` alongside the
  demo it was written for.

## Licensing caveat

Most SIDs in HVSC are reverse-engineered from commercial games and the
licensing status varies wildly. They are tracked in `.gitignore` for that
reason. Confirm rights before redistributing a config that bundles them.
