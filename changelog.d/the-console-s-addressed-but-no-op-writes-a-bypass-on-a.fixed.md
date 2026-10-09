- The console's addressed-but-no-op writes (a bypass on a layer the scene does
  not have, a knob the current scene cannot resolve, a jump past the end) log one
  debug line each. The page discards every response body, so a pad that did
  nothing mid-set left no evidence on either side of the wire — "the tap reached
  the host and did nothing" and "the tap never arrived" were indistinguishable.
