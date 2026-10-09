- **A multi-system start with a bad DAC curve on a later system no longer
  provisions the earlier ones first.** With `[audio].dac_curve = "calibrated"`
  and no calibration for the last system, the earlier systems had already
  switched the HDMI mode, set the REU, sampler and master volume, and reset
  the machine before the run stopped. Every system is now opened and its
  curve resolved before any is provisioned.
