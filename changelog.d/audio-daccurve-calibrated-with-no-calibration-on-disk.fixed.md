- **`[audio].dac_curve = "calibrated"` with no calibration on disk now exits
  with an error instead of a traceback, and no longer fails a run with
  `--no-audio`.** The curve was resolved even with audio off, and in an
  ensemble the missing table skipped the teardown of the systems already
  started, leaving their machines as the run had set them up.
