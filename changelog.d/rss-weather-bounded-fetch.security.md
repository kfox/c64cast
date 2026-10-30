- **An RSS feed or weather service can no longer hang its overlay or exhaust
  memory with its reply's body.** A server that sent the body a byte at a
  time, or without end, used to hold the background fetch forever, freezing
  the ticker or corner for the rest of the run while the reply grew in memory.
  A body now has to arrive within 20 seconds and 4 MiB, or it is dropped with a warning and the
  last good value stays on screen.
