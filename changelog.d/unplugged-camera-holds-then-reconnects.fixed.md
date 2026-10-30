- **An unplugged camera no longer freezes on its last frame for good, and is
  picked up again when it is plugged back in.** A webcam scene keeps showing
  the last frame for about three seconds, then black, and goes live again on
  its own once the camera returns; c64cast logs once when it goes and once when
  it is back. It reconnects only to the same camera, by name and USB ID, never
  to another one that took its place, which needs the `camera` extra; without
  it a camera that was unplugged is not reopened, and the picture stays black
  unless the original connection starts sending again. Gesture control ignores
  the held frame, so a hand frozen mid-pinch no longer resumes a paused show.
