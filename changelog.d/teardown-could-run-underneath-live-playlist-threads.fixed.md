- **Teardown could run underneath live playlist threads.** `teardown_session`
  documented itself as safe to call from a `finally:` but never stopped the
  playlists, so any escape that skipped the drain — a thread that failed to
  start, an unexpected exception out of the run loop — closed audio, reset and
  closed the API while a worker was still writing to the machine, which is the
  mid-DMA cut that can wedge it into needing a power cycle. It now sets the
  stop flag and drains the threads first (a no-op on the normal path), and the
  thread list is populated as each thread starts so a partial failure is still
  visible to teardown.
