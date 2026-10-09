- **A preview run whose window had closed could not be stopped.** When
  `[preview]` is on, the main thread drives the window and joins the playlist
  threads itself when it stops — and that join had no timeout. Three ordinary
  paths reach it: the operator closes the window (documented as *not* a stop
  signal), a draw failure disables it, and, on the very first iteration, a
  headless opencv build or a machine with no display, where the window logs
  "preview disabled" and the show carries on. An untimed `join()` parks the
  main thread where no signal handler can run, and on the CLI the SIGINT and
  SIGTERM handlers are the *only* thing that sets the stop flag — so from that
  moment neither Ctrl+C nor a service manager's SIGTERM could end the run,
  teardown never happened, and the machine was left mid-session. It now uses
  the same polling join as the headless path, which is what the three other
  join sites were already changed to for exactly this reason. `[preview]` over
  SSH, and `[preview].enabled = true` on a headless install, are the runs that
  were affected.
