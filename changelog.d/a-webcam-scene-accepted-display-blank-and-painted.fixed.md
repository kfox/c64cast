- **A `webcam` scene accepted `display = "blank"` and painted nothing.** Every
  other frame-bearing scene type refuses it with guidance, because blank mode
  ignores the frame it is handed; webcam was the one type with no validator, so
  the run opened the camera, grabbed frames and showed an empty screen with no
  error anywhere. `display = "random"` on a webcam now gets the same "only
  slideshow does" message its siblings give instead of a raw "unknown display
  mode".
