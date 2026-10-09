- **The WLED pixel sink's teardown bell is verified before it is trusted, and
  says so when it cannot be opened.** Windows has no native `socketpair`, so
  CPython binds a loopback listener and accepts without checking who connected
  — a local process that won that race owned the bell, and ringing it ended the
  receive thread while the source went on serving its last frame. A pair that
  fails or comes back wrong is now discarded with a warning, instead of
  silently reverting to the slower teardown whose timeout warning this exists
  to prevent.
