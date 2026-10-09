- Shutting the host down during a switch left a brand-new session holding the
  machine. `close()` woke the parked switch worker on the very transition it
  was waiting for, the worker claimed a new generation, and the join then
  waited for that build to *finish* — so a Ctrl+C during a console switch
  returned with a session running, the run marker written and the hardware
  held, straight into the force-exit backstop. `close()` is now terminal (a
  start or switch afterward is refused, an in-flight switch bails) and
  re-asserts idle after its join, so a start that squeezed through is still
  torn down.
