- **A live stream no longer hangs a video scene's setup.** The audio peak scan
  read its input to the end before playback started, and a live stream (or
  any server that keeps sending) has none, so the scene never started and the
  playlist sat on the setup bar. On a network input the scan now stops after
  10 seconds and sets the gain from what it read.
