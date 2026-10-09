- An abandoned switch was invisible to the console. When the previous session
  would not come down inside the timeout, the supervisor logged to the host's
  stderr and returned — no `last_error`, no transition — so a browser holding
  the `202` and the generation it had been promised saw nothing change at
  all, with `last_error: null`. Every way a switch can be abandoned now
  parks the reason in `last_error` and re-notifies the feed, and a teardown
  that *raised* does the same instead of settling as a clean `idle`.
