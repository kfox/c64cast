- **With `$D418` DAC audio, the next scene no longer starts with a moment
  of the previous one's sound.** A video or mic chunk pushed exactly as a
  scene ended could land in the queue after the scene's teardown had
  cleared it, so it played at the start of the next scene. This was rare.
