- **A scene's setup is no longer run again because something else lost a
  write.** The playlist judged whether a setup had landed by whether any
  write on the link had failed meanwhile, so a failed audio or poll-thread
  write during setup re-ran a setup that had gone fine, up to three times.
  It now looks only at the setup's own writes.
