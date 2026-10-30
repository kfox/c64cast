- **Gesture control without a camera turns itself off instead of stopping the
  show.** With `[vision].enabled` and no webcam scene, a camera that could not
  be opened ended the run at startup. c64cast now warns that vision control is
  disabled and plays on, as it already did for a missing `vision` extra or
  model file. A camera that a webcam scene or clip needs still stops the run.
