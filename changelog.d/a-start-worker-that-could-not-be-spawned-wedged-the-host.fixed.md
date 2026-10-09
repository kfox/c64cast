- A start worker that could not be spawned wedged the host permanently in
  `starting`: nothing else leaves that state, so `start`/`switch` refused
  forever and a stop only armed a flag. The failed spawn now rolls the
  generation back, lands in `error` (which is startable) and still raises.
