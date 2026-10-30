- **A run with no scenes configured opens the camera again.** With no config
  file and no media arguments, `python -m c64cast -u u64://HOST -d DEVICE` exited
  with "no scenes configured and no WebcamSource available" instead of showing
  the live camera, because the camera was never opened for the fallback scene.
