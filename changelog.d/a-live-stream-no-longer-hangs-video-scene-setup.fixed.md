- **A live stream no longer hangs a video scene's setup.** The audio peak scan
  read its input to the end before playback started, and a live stream (or
  any server that keeps sending) has none, so the scene never started and the
  playlist sat on the setup bar. A network stream that reports no length now
  stops the scan 10 seconds after it opens, and any network input stops it
  after 30 seconds without sound; the gain is set from what was read. A file
  that reports its length is still scanned to its end, however slowly it
  arrives.
