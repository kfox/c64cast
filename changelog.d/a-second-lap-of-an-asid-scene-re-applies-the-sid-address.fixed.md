- **A second lap of an ASID scene re-applies the SID address map.** Playlists
  reuse scene instances, and nothing reset the multi-SID state at teardown — so
  on lap 2 the growth check saw the chip count it had already reached, never
  re-issued the map the lap-1 teardown had just restored, and went on writing
  chips 2..N to addresses the machine no longer routed there.
