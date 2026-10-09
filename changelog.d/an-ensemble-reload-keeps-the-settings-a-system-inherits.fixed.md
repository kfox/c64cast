- **An ensemble reload keeps the settings a system inherits from the master.**
  A reload (SIGHUP, `POST /reload`, or the console's reload button) rebuilt each
  system from its own file and the command line only, so anything it took from
  the master alone — `[playlist]`, `[interstitial]`, `[color]` and the rest of
  the cascade — went back to its default. A reload now composes each system
  exactly as startup does. On a TeensyROM, an explicit
  `[video].use_reu_staged = true` also stays off after a reload, as it is at
  startup, rather than reaching an REU the backend does not have.
