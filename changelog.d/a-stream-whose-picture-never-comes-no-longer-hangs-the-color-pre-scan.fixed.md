- **A stream whose picture never comes no longer hangs the color pre-scan.**
  With `force_palette` or `hardware_palette = "source"`, a video scene scans
  the source's colors before playback. On a stream that keeps sending sound
  but no picture, that scan read forever and the scene never started. On a
  network input it now gives up after 30 seconds without a picture and plays
  without the pre-scan.
