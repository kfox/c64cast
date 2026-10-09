- Smaller supervisor fixes: the log buffer's `tail(limit=0)` returned the
  entire retained tail rather than nothing (`rows[-0:]` is the whole list);
  the reap path and an operator stop spawned worker threads with identical
  names, so a straggler logged by name could not be attributed to either; the
  60-second teardown-wait line was logged on every exit path including the
  ones where no session ever existed; and a length mismatch between configs
  and system names in the after-a-crash safe-state reset dropped a machine
  with nothing logged.
