- **A scene could open on the tail of the previous scene's audio.**
  `AudioStreamer.stop()` drained its queue without bumping the splice epoch, so
  a producer that had captured its epoch before that drain — a file decoder
  still mid-encode, or one just released from the backpressure spin — landed a
  blob behind it. The next scene inherited it, and since the bring-up resets the
  pushed count but not the queued one, `position_seconds()` read 0 until it
  drained. `stop()` now bumps the epoch as soon as it clears `running`, the way
  `flush()` bumps ahead of its own drain, and `push_samples` is a no-op once
  stopped.
