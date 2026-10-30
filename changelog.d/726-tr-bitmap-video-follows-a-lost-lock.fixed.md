- **A TeensyROM+ bitmap video no longer plays slow after losing its write lock
  mid-scene.** A scene that started with WriteC64Spans locked to the sample
  player kept its full-speed tempo when the lock was lost, so the rest of the
  clip played slow until the next scene. It now follows the slower drain, as a
  scene starting unlocked always did.
