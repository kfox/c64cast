# DAC calibration tables

Per-system Mahoney 8-bit `$D418` DAC calibration tables live here, under
`calibration/dac/<key>.json`, written by:

```bash
scripts/c64cast.sh -u <target> --calibrate-dac
```

Each file is a 256-entry amplitude→`$D418` "sidtable" measured on the SID at
`$D400` of the connected machine (via a Cam Link / UVC audio capture on the SID
output), keyed by the connection target — host address for an Ultimate
(`u64-192.168.2.64.json`), or the serial device / TCP host for a TeensyROM+.

Playback with `[audio].dac_curve = "auto"` (the default) automatically prefers
the calibrated table for the current system when one exists. Set
`dac_curve = "calibrated"` to require it.

**Why per-system:** physical 6581/8580 chips (and SID replacements like
ARM2SID/SwinSID/FPGASID) vary enormously chip-to-chip — a single baked table
can't serve them. The U64's *emulated* UltiSID is deterministic, so it uses the
baked `mahoney_ultisid` table without calibration. See
[`c64cast/dac_calibration.py`](../c64cast/dac_calibration.py) and the
`dac_curve` notes in [`CLAUDE.md`](../CLAUDE.md).

These `.json` tables are **machine-specific captured data and are gitignored**
(only this README is tracked) — like `assets/` and `scripts/diags/out/`.
