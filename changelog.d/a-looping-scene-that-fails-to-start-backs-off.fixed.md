- **A looping single scene that could not start retried on every frame.** A
  one-scene show with `loop = true` whose file was missing or unreadable set
  itself up again many times a second, logging the same error each time. It now
  waits 1 s before the next try, doubling up to 30 s, with one warning per
  try, and goes back to normal as soon as the scene starts. A skip retries at
  once.
