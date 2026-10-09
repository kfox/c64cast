- **`[audio].dac_curve = "calibrated"` with no calibration now fails before
  touching the machine.** It could first switch the Ultimate's video output
  (making a capture device re-lock), reset the C64 and change its REU and audio
  settings, then put them all back and exit.
