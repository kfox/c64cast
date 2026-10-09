- **A `wled` scene's `sink_allow` accepted an IPv6 entry the sink can never
  match.** The pixel sink binds `AF_INET` only, so the peer address it compares
  against is always a dotted quad — an IPv6 allowlist entry produced a config
  that validated cleanly and then silently dropped every sender, with no log
  line. It is refused at validate time now, naming the IPv4 requirement.
