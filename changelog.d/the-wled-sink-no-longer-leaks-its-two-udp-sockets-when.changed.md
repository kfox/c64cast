- The WLED sink no longer leaks its two UDP sockets when `start()` is called
  again after the receive thread has died with the sockets still open — the
  old sockets are closed first, and a stale `bind_error` from a prior failed
  start no longer survives into a later successful one.
