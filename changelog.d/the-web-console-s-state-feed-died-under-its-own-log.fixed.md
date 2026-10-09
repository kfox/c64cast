- **The web console's state feed died under its own log volume.** The
  supervisor's log buffer was read by the push loop with no lock while
  build and teardown workers appended to it, and iterating a `deque` that is
  being appended to — or, at its size cap, evicted from — raises
  `RuntimeError: deque mutated during iteration`. The reader's caller
  handled that as a closed socket at debug level, so the browser's only
  channel for session state and log lines dropped and reconnected exactly
  when the log was busiest: a failing build, which is what the buffer exists
  for. Both readers now snapshot under the handler's own lock.
