- **A single scene that cannot start now ends the run with an error.** A
  one-scene show whose file was missing or unreadable, or whose tune would not
  load, set itself up again many times a second with `loop = true`, logging the
  same error each time, and with `loop = false` exited as if it had played. It
  now stops at once with one error that names the scene and the reason, and
  c64cast exits with status 1, looping or not.
