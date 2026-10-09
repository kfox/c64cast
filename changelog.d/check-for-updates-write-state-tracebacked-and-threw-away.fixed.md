- `--check-for-updates --write-state` tracebacked, and threw away the
  network answer it already held, when the data root could not be written —
  read-only (`$C64CAST_DATA_DIR` into a squashfs), full, or with a directory
  planted where `update_check.json` belongs, which `os.replace` cannot
  rename over. `record_check` now warns and carries on, so the answer still
  prints and `c64cast-update-check.service` still exits within the
  `SuccessExitStatus` it enumerates. A non-UTF-8 `update_check.json` also
  raised `UnicodeDecodeError` out of `read_update_state` (guarded by `except
  OSError`, and a decode error is a `ValueError`) into both readers,
  including the script that runs at every SSH login — and because
  `record_check` reads before it writes, the run that would have replaced
  the bad file died first and the slot could never repair itself.
