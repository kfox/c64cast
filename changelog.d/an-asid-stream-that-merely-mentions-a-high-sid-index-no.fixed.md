- **An ASID stream that merely mentions a high SID index no longer triggers a
  full 8-SID reconfiguration.** ASID can name up to chip 17, and a chip past
  `asid_max_sids` is downmixed onto the primary SID as documented — but the
  *growth* request was taken from the raw wire index instead, so a single
  message naming chip 11 mapped eight addresses, split the scope into eight
  windows (seven of which could never show anything), panned eight mixer
  sources and re-initialized the ring player.
