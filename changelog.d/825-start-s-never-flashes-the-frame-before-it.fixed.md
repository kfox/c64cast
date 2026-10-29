- **A video scene with `start_s` no longer flashes the frame before its
  start.** When a frame sat exactly on the start time, the one before it was
  briefly queued too, and a busy machine could show it for a frame.
