- **The diag tools that run c64cast find their capture device before they
  start it.** `run_and_capture.py`, `doublebuffer_tear_ab.py`,
  `flicker_tear_ab.py`, `menu_inject.py --frames` and `flicker_score_grid.py`
  looked for the camera only once c64cast had booted the C64, so a missing or
  ambiguous capture device failed after the machine had been reset and
  driven, and the tools then reset it again. They now look first and exit in
  seconds without touching the machine.
