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
  — Tens of thousands of curated SIDs. Unpack the `C64Music/` tree (or just
    its contents) directly into this directory — `assets/sids/C64Music/` or
    `assets/sids/` itself — and c64cast auto-detects the bundled
    `DOCUMENTS/Songlengths.md5`, so `waveform` scenes get each tune's real
    duration with no config needed (`[playlist].songlengths_file` overrides
    the auto-detected path if you unpack HVSC somewhere else; set it to
    `""` to disable auto-detection). Unpacking elsewhere still works for
    playback — you'd just set `songlengths_file` explicitly to get true
    durations.
- **CSDb releases**: https://csdb.dk/ — Often bundle a `.sid` alongside the
  demo it was written for.

## Licensing caveat

Most SIDs in HVSC are reverse-engineered from commercial games and the
licensing status varies wildly. They are tracked in `.gitignore` for that
reason. Confirm rights before redistributing a config that bundles them.
