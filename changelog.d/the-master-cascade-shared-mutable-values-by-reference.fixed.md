- The master cascade shared mutable values by reference: `hue_corrections`,
  `performance.clips`, `host_sid_chips` and `sid_panning`/`sid_volume` were
  handed to every inheriting system as the *same* list or dict object as the
  master's and each other's, so one system mutating one in place would have
  mutated every system's, invisibly at the config layer. They are deep-copied
  now.
