- **`--serve` could leave a background poller running after it shut down.** The
  session supervisor started its `session-reap` poller outside the lock that
  publishes the `running` transition, and `close()` stops that poller once and
  never returns to it — so a build still in flight when `close()` gave up
  waiting came up behind that stop, cleared the stop event and spawned a fresh
  poller with nothing left to stop it, ticking against a closed supervisor for
  the life of the process. The browser screen feed brought its
  `screen-sweeper` thread up the same way, where a watcher that registered a
  moment before `close()` could strand a sweeper of its own, or fail the
  request outright with an `AttributeError` if the close landed between the two
  statements. Both threads now start inside the lock that publishes the state
  they depend on, and neither starts once its owner is closing.
