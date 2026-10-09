- **A `loop_slot` command could grow a preset file without limit.** The console's
  transport verb passed its `slot` straight through with no range check, and
  `LoopPresetStore.save` had deliberately overridden away the shared
  `1..250` guard — so an incrementing slot persisted one unbounded new key per
  event, each save re-reading and rewriting the whole grown file on the playlist
  thread that drives the hardware, with the state feed re-parsing it on every
  push. The slot is bounded at both ends now, and the digits no longer reach the
  OSD line the transport engine draws over the audience output.
