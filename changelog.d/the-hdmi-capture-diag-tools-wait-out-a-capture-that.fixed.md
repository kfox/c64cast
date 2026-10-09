- **The HDMI capture diag tools wait out a capture that returns no frame for a
  moment.** `scripts/diags/hdmi_capture.py` gave up on the first read that
  came back empty, so a capture landing while the HDMI link was settling
  failed and read as a dead capture stick. It now retries for 5 seconds, and
  the error names the likely causes (the HDMI link renegotiating, no signal,
  the device held by another program) and points at
  `c64cast --list-devices`. `menu_inject.py`, `run_and_capture.py` and
  `scripts/capture_guide_figure.py` retry their stills the same way; the
  last two used to drop a failed still silently and now print why.
