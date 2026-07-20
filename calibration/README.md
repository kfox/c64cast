# DAC calibration tables — legacy location

> **This is the *old* location.** As of the machine-settings / canonical-data-dir
> change, DAC calibration tables live under the **user data directory**, not the
> repo checkout:
>
> ```
> ~/.local/share/c64cast/calibration/dac/<key>.json      # Linux / macOS (XDG)
> %LOCALAPPDATA%\c64cast\calibration\dac\<key>.json       # Windows
> ```
>
> Override the whole data root with **`$C64CAST_DATA_DIR`** (e.g. a dev who wants
> in-repo data can `export C64CAST_DATA_DIR="$PWD"`). `c64cast --doctor` prints
> the resolved location and, if it finds old files here, the exact `mv` to
> migrate them. The path is resolved by [`c64cast/paths.py`](../c64cast/paths.py)
> (`calibration_dir()`), so it works from a repo checkout, a `pip install`, or a
> PyPI wheel.

Per-system Mahoney 8-bit `$D418` DAC calibration tables are written by:

```bash
scripts/c64cast.sh -u <target> --calibrate-dac
```

Each file is a 256-entry amplitude→`$D418` "sidtable" measured on the SID at
`$D400` of the connected machine (via a Cam Link / UVC audio capture on the SID
output), keyed by a **stable device identity** — the Ultimate's REST
`unique_id`, or a TeensyROM+'s USB serial number (see
[`c64cast/dac_calibration.py`](../c64cast/dac_calibration.py)).

Playback with `[audio].dac_curve = "auto"` (the default) automatically prefers
the calibrated table for the current system when one exists. Set
`dac_curve = "calibrated"` to require it.

**Why per-system:** physical 6581/8580 chips (and SID replacements like
ARM2SID/SwinSID/FPGASID) vary enormously chip-to-chip — a single baked table
can't serve them. The U64's *emulated* UltiSID is deterministic, so it uses the
baked `mahoney_ultisid` table without calibration. See the `dac_curve` notes in
[`docs/architecture.md`](../docs/architecture.md).

These `.json` tables are **machine-specific captured data and are gitignored**
at this legacy location (only this README is tracked) — so an old file left
here can never become committable.
