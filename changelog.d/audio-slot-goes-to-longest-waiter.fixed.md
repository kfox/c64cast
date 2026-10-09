- **In an ensemble, a system waiting for the audio slot can no longer be
  starved by another system.** A system whose playlist was all
  audio-bearing scenes released the slot and took it back within
  microseconds, so a system waiting for it (a single looping scene, a
  jump, a setup that had waited out a link outage) could wait for ever.
  The slot now goes to whichever system has waited longest. A scene's
  "UP NEXT" card that waited out an outage no longer waits for the slot
  on the next scene's behalf; the playlist resolves that scene again when
  the card ends.
