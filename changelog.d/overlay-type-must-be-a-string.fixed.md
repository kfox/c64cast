- **An overlay whose `type` is not a string is a config error, not a crash.**
  `type = ["clock"]` in a `[[scenes.overlays]]` table raised a Python
  traceback out of `--doctor` and a run; it is now reported as a config error.
