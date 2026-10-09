- **Ending a WLED pixel-sink scene no longer warns that its thread is wedged.**
  The receive thread waits up to 0.25 s in `select` for the next datagram, and
  teardown gave it 0.5 s to notice the stop — a 2x margin, tight enough that a
  busy machine lost the race and logged `PollThread 'wled-sink' did not stop
  within 0.5s; it is still running` about a thread that was merely waiting.
  Teardown now wakes the wait directly instead of outlasting it, so the sink
  stops in well under a millisecond and the warning means what it says.
